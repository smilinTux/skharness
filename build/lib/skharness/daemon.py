"""Tailnet-only skcode host daemon and Atlas observation/control boundary.

The read plane exposes sessions, jobs, Arena status, and a replayable live activity
journal. The write plane exposes separately scoped inject, dispatch/cancel, and
receipt-driven Atlas commands. Every actuator is bearer/PDP/audit gated and fails
closed when its production dependency is absent. Activity remains observation-only.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response

from skharness.activity import (
    ActivityContext,
    ActivityCorruptionError,
    ActivityJournal,
    ActivityKind,
)
from skharness.arena.models import ExperimentState
from skharness.arena.status import ArenaStatusService, objectives_from_query
from skharness.auth import Verifier, check_token, require_bearer
from skharness.autocode import ratify as _ratify
from skharness.autocode.types import RepoSpec
from skharness.control import (
    TERMINAL_CONTROL_STATUSES,
    ControlAction,
    ControlCommand,
    ControlConflictError,
    ControlCorruptionError,
    ControlJournal,
    ControlStatus,
    ControlTargetKind,
)
from skharness.events import EventType, SessionEvent
from skharness.harness import Harness, SessionDescriptor, SpawnRejected
from skharness.jobs import JobRun
from skharness.manifest import skcode_module_manifest
from skharness.session_events import SessionEventStore

# The dispatch authorization seam (spec 7.4): given the authenticated subject and
# the request resource, return a decision exposing ``.allow`` (bool) and, ideally,
# ``.obligations`` (each carrying an audit record). This is exactly the shape of
# ``capauth.authz.decide(...)``'s Decision; serve.py wires the real PDP. The daemon
# NEVER decides policy itself: no authorizer configured => dispatch is denied.
Authorizer = Callable[[str, dict, dict], Any]
ControlHandler = Callable[[ControlCommand], Any]

# Advisory targets provider for GET /dispatch/targets: returns the repos/harnesses/
# hosts/profiles a device MAY target here. Advisory only; /dispatch re-enforces.
TargetsProvider = Callable[[], dict]

# Emergency-brake predicate: True => dispatch is paused (returns 503). Injected so
# the persisted pause flag (operator_cli pause-dispatch) drives it.
PausePredicate = Callable[[], bool]

# A host resolves one of its sessions to the (repo, worktree, acceptance) triple
# ratify needs; None means the session has no gradable worktree. Injected so the
# daemon stays free of any repo-map/worktree convention of its own.
RatifyResolver = Callable[[SessionDescriptor], "tuple[RepoSpec, str, list[str]] | None"]

# Autocode orchestrator runs registering themselves as sessions (source=autocode,
# spec 5.1, card C-1 AC3): a provider of the CURRENT live/recent autocode-run
# SessionDescriptors, merged into GET /sessions and GET /sessions/{sid} alongside
# the harness's own (interactive) rows. None (the default) merges nothing, so a
# bare test double sees exactly the harness's sessions, unchanged.
AutocodeSessionsProvider = Callable[[], "list[SessionDescriptor]"]

# Cron/scheduler ledger view (spec section 8, card C-8): a provider of the CURRENT
# JobRun rows, read fresh on every call (the ledger is a live, append-only file
# owned by the scheduler, not something this daemon caches). None (the default)
# reports no jobs known, so a bare test double never touches any real ledger path.
JobsProvider = Callable[[], "list[JobRun]"]

# Published skwatchdog digest artifact (card C-14a): a provider of the CURRENT
# raw digest.json bytes, or None when nothing has been published yet. Read fresh
# on every call (the artifact is a file owned by skos, not something this daemon
# caches). None (the default) means "nothing published", so a bare test double
# never touches a real path. The bytes are served exactly as read: this daemon
# never parses or reformats the digest JSON, so it can never fabricate or "fix" a
# quiet day out of a missing or malformed artifact.
DigestProvider = Callable[[], "bytes | None"]

# Scope split on the remote-control surface (R2.4): a verified skcode token must
# carry SCOPE_READ to view (list/get/stream), and SCOPE_WRITE to actuate
# (inject/ratify). Enabling the verifier with a read-only token grants view
# WITHOUT arming keystroke-inject. The deny-all default carries no scopes and so
# denies both regardless.
SCOPE_READ = "skcode.stream"
SCOPE_WRITE = "skcode.inject"
# The THIRD scope, for the RCE surface: spawning a NEW session (spec 7.4). It is
# strictly above inject: a device may stream and inject into existing sessions
# without ever being able to spawn a new one. Dispatch requires this scope AND a
# separate authz allow decision AND an audit sink (all fail closed).
SCOPE_DISPATCH = "skcode.dispatch"

# The capauth capability the inject/ratify PDP decides on (CR-6.2 C2/C8). Same
# string as SCOPE_WRITE; seeded VERIFIED in capauth.authz.DEFAULT_RULES (C3), so
# routing inject/ratify through decide() enforces the enrollment-mode floor in
# code, not only at token issuance.
INJECT_CAPABILITY = SCOPE_WRITE


# --------------------------------------------------------------------------- #
# Route-coverage classification (SKWorld Authorization Model L1.3; CR-6.2 C3).
# Every HTTP/WS route this daemon serves is exactly ONE class: PUBLIC (no bearer)
# or gated on a named scope. The coverage-completeness test enumerates the LIVE
# app route table and asserts every served route is declared here, so a new gated
# route can never ship unclassified (the same class of gap the skchat dataplane
# coverage gate closes). ``skcode.stream`` is scope-only (read/view, no PDP
# decision); ``skcode.inject`` and ``skcode.dispatch`` are additionally PDP-decided
# and carry a capauth DEFAULT_RULES row.
# --------------------------------------------------------------------------- #
PUBLIC_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/.well-known/skworld-module.json"),
        ("GET", "/"),
        ("GET", "/app"),
        ("GET", "/livez"),
        ("GET", "/readyz"),
    }
)

#: (METHOD, path_format) -> required scope for every gated route. "WS" is the
#: websocket stream (browsers cannot set headers, so its token rides the query).
ROUTE_SCOPES: dict[tuple[str, str], str] = {
    ("GET", "/api/v1/hosts/self"): SCOPE_READ,
    ("GET", "/api/v1/sessions"): SCOPE_READ,
    ("GET", "/api/v1/sessions/{sid}"): SCOPE_READ,
    ("GET", "/api/v1/sessions/{sid}/events"): SCOPE_READ,
    ("GET", "/api/v1/activity"): SCOPE_READ,
    ("GET", "/api/v1/control/{command_id}"): SCOPE_READ,
    ("GET", "/api/v1/jobs"): SCOPE_READ,
    ("GET", "/api/v1/watchdog/digest"): SCOPE_READ,
    ("GET", "/api/v1/arena/status"): SCOPE_READ,
    ("GET", "/api/v1/arena/challenges"): SCOPE_READ,
    ("GET", "/api/v1/arena/attempts"): SCOPE_READ,
    ("GET", "/api/v1/arena/runs"): SCOPE_READ,
    ("GET", "/api/v1/arena/jobs"): SCOPE_READ,
    ("GET", "/api/v1/arena/failures"): SCOPE_READ,
    ("GET", "/api/v1/arena/leases"): SCOPE_READ,
    ("GET", "/api/v1/arena/verifications"): SCOPE_READ,
    ("GET", "/api/v1/arena/frontier"): SCOPE_READ,
    ("GET", "/api/v1/arena/lineage/{experiment_id}"): SCOPE_READ,
    ("GET", "/api/v1/arena/metrics"): SCOPE_READ,
    ("GET", "/api/v1/dispatch/targets"): SCOPE_DISPATCH,
    ("POST", "/api/v1/sessions/{sid}/ratify"): SCOPE_WRITE,
    ("POST", "/api/v1/sessions/{sid}/inject"): SCOPE_WRITE,
    ("POST", "/api/v1/sessions/{sid}/deny"): SCOPE_WRITE,
    ("POST", "/api/v1/dispatch"): SCOPE_DISPATCH,
    ("POST", "/api/v1/sessions/{sid}/cancel"): SCOPE_DISPATCH,
    # The route's minimum scope is inject. Action-level enforcement below raises
    # cancel/pause/resume/retry to dispatch before policy evaluation.
    ("POST", "/api/v1/control"): SCOPE_WRITE,
    ("WS", "/api/v1/sessions/{sid}/stream"): SCOPE_READ,
    ("WS", "/api/v1/activity/stream"): SCOPE_READ,
}

#: Scopes that route through the capauth PDP (decide) and so REQUIRE a rule row in
#: capauth.authz.DEFAULT_RULES. ``skcode.stream`` is deliberately excluded (it is a
#: scope-only read capability with no PDP decision).
PDP_SCOPES: frozenset[str] = frozenset({SCOPE_WRITE, SCOPE_DISPATCH})


def classify_route(method: str, path: str) -> tuple[str | None, str | None]:
    """Classify a served route as ("public", None) | ("gated", scope) | (None, None).

    (None, None) means UNCLASSIFIED, which the coverage test treats as a failure:
    a gated route with no declared scope is unsafe to ship (it might not be gated
    at all, or be gated on an unknown scope).
    """
    key = (method.upper(), path)
    if key in PUBLIC_ROUTES:
        return ("public", None)
    scope = ROUTE_SCOPES.get(key)
    if scope is not None:
        return ("gated", scope)
    return (None, None)


def _content_hash(text: str | None) -> str:
    """A sha256 hex digest of injected content for the audit line (CR-6.2 C3).

    The raw keystrokes are NEVER logged (a session may be sent secrets); the audit
    records only this digest + a length, so an inject is attributable to WHAT was
    sent (comparable across lines) without disclosing the content itself.
    """
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


_CLIENT_DIR = Path(__file__).parent / "client"
_PLACEHOLDER = (
    "<!doctype html><meta charset=utf-8><title>skcode-hostd</title>"
    "<h1>skcode-hostd</h1><p>Read-only client not installed yet.</p>"
)


def _client_html(client_dir: Path | None) -> str:
    base = client_dir if client_dir is not None else _CLIENT_DIR
    index = Path(base) / "index.html"
    if index.exists():
        return index.read_text(encoding="utf-8")
    return _PLACEHOLDER


def build_daemon_app(
    *,
    harness: Harness,
    verify_caller: Verifier,
    host_id: str = ".158",
    client_dir: Path | None = None,
    resolve_ratify: RatifyResolver | None = None,
    emit_event: Callable[[str, SessionEvent], None] | None = None,
    audit_log: Callable[[str], None] | None = None,
    authorize_dispatch: Authorizer | None = None,
    authorize_inject: Authorizer | None = None,
    dispatch_targets: TargetsProvider | None = None,
    dispatch_paused: PausePredicate | None = None,
    event_store: SessionEventStore | None = None,
    list_autocode_sessions: AutocodeSessionsProvider | None = None,
    list_jobs: JobsProvider | None = None,
    read_digest: DigestProvider | None = None,
    arena_status: ArenaStatusService | None = None,
    activity_journal: ActivityJournal | None = None,
    control_journal: ControlJournal | None = None,
    control_handler: ControlHandler | None = None,
) -> FastAPI:
    app = FastAPI(title="skcode-hostd")
    # SessionEvent v2 (spec 5.1, card C-1): assigns seq/sid/source at append and
    # (when the caller configures a persisting store) archives to the capped
    # per-session JSONL. Defaults to an in-memory-only store (persist=False) so
    # a bare test double, or a daemon nobody wired persistence for, still gets
    # correct seq assignment without ever touching disk; serve.py wires a real
    # persisting store for the live daemon.
    store = event_store if event_store is not None else SessionEventStore(persist=False)
    arena = arena_status or ArenaStatusService(
        require_gateway=True, require_verifier=True, require_gpu=False
    )

    def _session_source(sid: str) -> str:
        if list_autocode_sessions is not None:
            for s in list_autocode_sessions():
                if s.sid == sid:
                    return s.source or "autocode"
        return "interactive"

    async def _all_sessions() -> list[SessionDescriptor]:
        rows = list(await harness.list_sessions())
        if list_autocode_sessions is not None:
            rows = rows + list(list_autocode_sessions())
        return rows

    activity_pumps: dict[str, asyncio.Task] = {}
    activity_supervisor: asyncio.Task | None = None
    job_activity_state: dict[str, tuple] = {}

    async def _pump_session_activity(sid: str) -> None:
        """Fan one harness session into the durable activity rail without a viewer."""

        if activity_journal is None:
            return
        source = _session_source(sid)
        session_agent_id = "session-agent-" + _content_hash(sid)
        try:
            context = ActivityContext(
                session_id=sid, agent_id=session_agent_id, source=source
            )
        except ValueError:
            context = ActivityContext(
                session_id="session-" + _content_hash(sid),
                agent_id=session_agent_id,
                source=source,
            )
        kinds = {
            EventType.STATUS: ActivityKind.STATUS,
            EventType.ASSISTANT_TEXT: ActivityKind.ASSISTANT_TEXT,
            EventType.TOOL_CALL: ActivityKind.TOOL_CALL,
            EventType.TOOL_RESULT: ActivityKind.TOOL_RESULT,
            EventType.DIFF: ActivityKind.FILE_CHANGE,
            EventType.NEEDS_INPUT: ActivityKind.DISPOSITION,
        }
        try:
            async for event in harness.stream(sid):
                data: dict[str, Any] = {}
                if event.type in {EventType.TOOL_CALL, EventType.TOOL_RESULT}:
                    data = {
                        "tool": event.data.get("name"),
                        "is_error": bool(event.data.get("is_error", False)),
                    }
                summary = event.text
                if event.type is EventType.TOOL_CALL:
                    summary = f"{data.get('tool') or 'tool'} started"
                elif event.type is EventType.TOOL_RESULT:
                    summary = f"{data.get('tool') or 'tool'} finished"
                elif event.type is EventType.DIFF:
                    summary = "worktree change observed"
                await asyncio.to_thread(
                    activity_journal.publish,
                    context,
                    kinds[event.type],
                    summary=summary,
                    data=data,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one producer cannot kill hostd
            try:
                await asyncio.to_thread(
                    activity_journal.publish,
                    context,
                    ActivityKind.ERROR,
                    summary="interactive session activity pump failed",
                    data={"error_type": type(exc).__name__},
                )
            except Exception:  # noqa: BLE001 - journal failure is surfaced by readiness/docs
                pass

    async def _supervise_session_activity() -> None:
        while True:
            try:
                rows = await _all_sessions()
                live_rows = {
                    row.sid: row
                    for row in rows
                    if row.state in {"running", "spawning"}
                    and (row.source or "interactive") in {"interactive", "attach"}
                }
                stale = [
                    activity_pumps.pop(sid)
                    for sid in set(activity_pumps) - set(live_rows)
                ]
                for task in stale:
                    task.cancel()
                if stale:
                    await asyncio.gather(*stale, return_exceptions=True)
                for row in live_rows.values():
                    existing = activity_pumps.get(row.sid)
                    if existing is None or existing.done():
                        activity_pumps[row.sid] = asyncio.create_task(
                            _pump_session_activity(row.sid),
                            name=f"skcode-activity-{row.sid}",
                        )
                if activity_journal is not None and list_jobs is not None:
                    for job in await asyncio.to_thread(list_jobs):
                        signature = (
                            job.status,
                            job.last_start,
                            job.dur_s,
                            job.stale,
                            job.host,
                        )
                        if job_activity_state.get(job.job) == signature:
                            continue
                        job_activity_state[job.job] = signature
                        try:
                            context = ActivityContext(
                                session_id="job-" + _content_hash(job.job),
                                job_id=job.job,
                                source="scheduler",
                            )
                        except ValueError:
                            context = ActivityContext(
                                session_id="job-" + _content_hash(job.job),
                                job_id="job-" + _content_hash(job.job),
                                source="scheduler",
                            )
                        await asyncio.to_thread(
                            activity_journal.publish,
                            context,
                            ActivityKind.ERROR
                            if job.status == "failed"
                            else ActivityKind.STATUS,
                            summary=f"job state: {job.status}",
                            data={
                                "job": job.job,
                                "status": job.status,
                                "host": job.host,
                                "last_start": job.last_start,
                                "duration_s": job.dur_s,
                                "stale": job.stale,
                                "staleness_s": job.staleness_s,
                            },
                        )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - retry bounded discovery on next interval
                pass
            await asyncio.sleep(2)

    async def _start_activity_supervisor() -> None:
        nonlocal activity_supervisor
        if activity_journal is not None:
            activity_supervisor = asyncio.create_task(
                _supervise_session_activity(), name="skcode-activity-supervisor"
            )

    async def _stop_activity_supervisor() -> None:
        tasks = [task for task in [activity_supervisor, *activity_pumps.values()] if task]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @asynccontextmanager
    async def _lifespan(_app):
        await _start_activity_supervisor()
        try:
            yield
        finally:
            await _stop_activity_supervisor()

    app.router.lifespan_context = _lifespan

    def _auth(authorization: str | None, required_scope: str | None = None) -> None:
        require_bearer(authorization, verify_caller, required_scope)

    def _authed_context(authorization: str | None, required_scope: str | None):
        """Enforce the bearer + scope gate AND return the verified auth context.

        ``require_bearer`` raises 401/403 on any failure, so control only returns
        here for an ALLOWED caller; we then re-verify to recover the AuthContext
        (verifiers are pure) so the dispatch route can read the subject fqid off it.
        """
        token = require_bearer(authorization, verify_caller, required_scope)
        return verify_caller(token)

    def _subject(auth_result) -> str:
        subj = getattr(auth_result, "subject", None)
        return subj.strip() if isinstance(subj, str) and subj.strip() else "unknown-device"

    def _emit_audit(record: dict) -> None:
        # audit_log is a plain str sink; serialize the structured record.
        if audit_log is not None:
            audit_log(json.dumps(record, default=str, sort_keys=True))

    def _emit_decision_obligations(decision) -> None:
        # Honour the PDP's audit obligations (spec 7.4: every decision, allow OR
        # deny, carries an audit obligation the PEP must write).
        for ob in getattr(decision, "obligations", None) or []:
            data = getattr(ob, "data", None)
            if data is None and hasattr(ob, "model_dump"):
                data = ob.model_dump()
            _emit_audit({"kind": getattr(ob, "kind", "audit"), "data": data or {}})

    def _activity_context_for_control(target_kind: ControlTargetKind, target_id: str):
        values = {
            "session_id": target_id if target_kind is ControlTargetKind.SESSION else "",
            "run_id": target_id if target_kind is ControlTargetKind.RUN else "",
            "agent_id": target_id if target_kind is ControlTargetKind.AGENT else "",
            "job_id": target_id if target_kind is ControlTargetKind.JOB else "",
        }
        if target_kind is ControlTargetKind.SESSION:
            values["agent_id"] = "session-agent-" + _content_hash(target_id)
        if not values["session_id"]:
            identity = f"control-{target_kind.value}-{target_id}"
            try:
                values["session_id"] = identity
                return ActivityContext(**values, source="atlas-control")
            except ValueError:
                values["session_id"] = "control-" + _content_hash(identity)
        try:
            return ActivityContext(**values, source="atlas-control")
        except ValueError:
            digest = _content_hash(target_id)
            return ActivityContext(
                session_id=values["session_id"] or f"control-{digest}",
                run_id=f"run-{digest}" if values["run_id"] else "",
                agent_id=f"agent-{digest}" if values["agent_id"] else "",
                job_id=f"job-{digest}" if values["job_id"] else "",
                source="atlas-control",
            )

    def _publish_control_activity(command, status: ControlStatus, detail: str = ""):
        if activity_journal is None:
            return None
        try:
            event = activity_journal.publish(
                _activity_context_for_control(command.target_kind, command.target_id),
                ActivityKind.STATUS if status is ControlStatus.APPLIED else ActivityKind.DISPOSITION,
                summary=f"Atlas control {status.value}: {command.action.value}",
                data={
                    "command_id": command.command_id,
                    "target_kind": command.target_kind.value,
                    "target_id": command.target_id,
                    "action": command.action.value,
                    "status": status.value,
                    "payload_digest": command.payload_digest,
                    "detail": detail,
                },
            )
            return event.cursor
        except Exception:  # noqa: BLE001 - control receipt remains authoritative
            return None

    def _enforce_inject_floor(subject: str, resource: dict) -> None:
        """CR-6.2 C2/C8: the PDP enrollment-mode floor for the write surface.

        When an inject authorizer is configured (production wiring), route the
        inject/ratify write through ``capauth.authz.decide`` the way dispatch does,
        requiring the verified-tier ``skcode.inject`` capability. A deny is 403 and
        audited; the actuation is never reached. When no authorizer is configured
        (test doubles that exercise only the shipped scope split), the scope gate
        stands alone and this is a no-op. The live daemon always wires the
        authorizer (serve.build_inject_authorizer), so the floor is enforced in
        code there, not only at token issuance.
        """
        if authorize_inject is None:
            return
        decision = authorize_inject(subject, resource, {})
        _emit_decision_obligations(decision)
        if not bool(getattr(decision, "allow", False)):
            _emit_audit(
                {
                    "event": "skcode.inject",
                    "subject": subject,
                    "resource": resource,
                    "decision": "deny",
                    "reason": getattr(decision, "reason", ""),
                }
            )
            raise HTTPException(403, "inject not authorized")

    @app.get("/.well-known/skworld-module.json")
    async def module_manifest(request: Request):
        # Public discovery metadata (NO bearer): the shell reads the manifest to
        # learn skcode's entry, nav, and required auth audience/scopes BEFORE it
        # has a token. It carries no secrets. URLs are origin-relative to the
        # request, so they resolve against wherever this host actually answers.
        return JSONResponse(skcode_module_manifest(str(request.base_url)))

    @app.get("/livez")
    async def livez():
        return JSONResponse(arena.liveness())

    @app.get("/readyz")
    async def readyz():
        body = arena.readiness()
        return JSONResponse(body, status_code=200 if body["ready"] else 503)

    @app.get("/api/v1/arena/status")
    async def arena_status_route(authorization: str | None = Header(default=None)):
        _auth(authorization, SCOPE_READ)
        return JSONResponse(arena.status())

    @app.get("/api/v1/arena/challenges")
    async def arena_challenges_route(authorization: str | None = Header(default=None)):
        _auth(authorization, SCOPE_READ)
        return JSONResponse({"challenges": arena.challenges()})

    @app.get("/api/v1/arena/attempts")
    async def arena_attempts_route(
        challenge_id: str | None = None,
        state: str | None = None,
        limit: int = 100,
        authorization: str | None = Header(default=None),
    ):
        _auth(authorization, SCOPE_READ)
        if state is not None and state not in {item.value for item in ExperimentState}:
            raise HTTPException(400, "unknown experiment state")
        return JSONResponse(
            {"attempts": arena.attempts(challenge_id=challenge_id, state=state, limit=limit)}
        )

    @app.get("/api/v1/arena/verifications")
    async def arena_verifications_route(
        limit: int = 100, authorization: str | None = Header(default=None)
    ):
        _auth(authorization, SCOPE_READ)
        return JSONResponse({"verifications": arena.verifications(limit=limit)})

    @app.get("/api/v1/arena/runs")
    async def arena_runs_route(limit: int = 100, authorization: str | None = Header(default=None)):
        _auth(authorization, SCOPE_READ)
        return JSONResponse({"runs": arena.runs(limit=limit)})

    @app.get("/api/v1/arena/jobs")
    async def arena_jobs_route(limit: int = 100, authorization: str | None = Header(default=None)):
        _auth(authorization, SCOPE_READ)
        return JSONResponse({"jobs": arena.scheduled_jobs(limit=limit)})

    @app.get("/api/v1/arena/failures")
    async def arena_failures_route(
        limit: int = 100, authorization: str | None = Header(default=None)
    ):
        _auth(authorization, SCOPE_READ)
        return JSONResponse({"failures": arena.failures(limit=limit)})

    @app.get("/api/v1/arena/leases")
    async def arena_leases_route(authorization: str | None = Header(default=None)):
        _auth(authorization, SCOPE_READ)
        return JSONResponse({"leases": arena.leases()})

    @app.get("/api/v1/arena/frontier")
    async def arena_frontier_route(
        challenge_hash: str, objectives: str, authorization: str | None = Header(default=None)
    ):
        _auth(authorization, SCOPE_READ)
        try:
            parsed = objectives_from_query(objectives)
            rows = arena.frontier(challenge_hash, parsed)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return JSONResponse({"challenge_hash": challenge_hash, "frontier": rows})

    @app.get("/api/v1/arena/lineage/{experiment_id}")
    async def arena_lineage_route(
        experiment_id: str, authorization: str | None = Header(default=None)
    ):
        _auth(authorization, SCOPE_READ)
        row = arena.lineage(experiment_id)
        if row is None:
            raise HTTPException(404, "experiment not found")
        return JSONResponse(row)

    @app.get("/api/v1/arena/metrics")
    async def arena_metrics_route(authorization: str | None = Header(default=None)):
        _auth(authorization, SCOPE_READ)
        return Response(arena.metrics.render(arena.status()), media_type="text/plain")

    @app.get("/api/v1/hosts/self")
    async def hosts_self(authorization: str | None = Header(default=None)):
        _auth(authorization, SCOPE_READ)
        return JSONResponse({"host": host_id, "harness": harness.name, "ok": True})

    @app.get("/api/v1/sessions")
    async def list_sessions(authorization: str | None = Header(default=None)):
        _auth(authorization, SCOPE_READ)
        rows = await _all_sessions()
        return JSONResponse({"sessions": [s.to_dict() for s in rows]})

    @app.get("/api/v1/sessions/{sid}")
    async def get_session(sid: str, authorization: str | None = Header(default=None)):
        _auth(authorization, SCOPE_READ)
        for s in await _all_sessions():
            if s.sid == sid:
                return JSONResponse(s.to_dict())
        raise HTTPException(404, "session not found")

    @app.get("/api/v1/sessions/{sid}/events")
    async def session_events(
        sid: str,
        before_seq: int | None = None,
        limit: int = 100,
        authorization: str | None = Header(default=None),
    ):
        # Archive paging over the capped per-session JSONL (spec 5.3): the client
        # reconnect/scrollback path, same read scope as the live WS tail. When the
        # daemon was built with no persisting event_store (persist=False, the
        # default here), this is simply always empty -- there is nothing archived
        # to page through, never an error.
        _auth(authorization, SCOPE_READ)
        rows = store.read_page(sid, before_seq=before_seq, limit=limit)
        return JSONResponse({"sid": sid, "events": rows})

    @app.get("/api/v1/activity")
    async def activity_replay(
        after: int = 0,
        limit: int = 200,
        session_id: str = "",
        run_id: str = "",
        agent_id: str = "",
        job_id: str = "",
        card_id: str = "",
        contract_id: str = "",
        lease_id: str = "",
        role: str = "",
        kind: str = "",
        authorization: str | None = Header(default=None),
    ):
        """Bounded cursor replay for Atlas and the built-in activity window."""

        _auth(authorization, SCOPE_READ)
        if activity_journal is None:
            return JSONResponse(
                {
                    "events": [],
                    "window": {
                        "retained_from_cursor": 1,
                        "head_cursor": 0,
                        "retained_events": 0,
                    },
                    "next_cursor": after,
                }
            )
        try:
            rows = await asyncio.to_thread(
                activity_journal.read_after,
                after,
                limit=limit,
                session_id=session_id,
                run_id=run_id,
                agent_id=agent_id,
                job_id=job_id,
                card_id=card_id,
                contract_id=contract_id,
                lease_id=lease_id,
                role=role,
                kind=kind,
            )
            window = await asyncio.to_thread(activity_journal.window)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except ActivityCorruptionError as exc:
            raise HTTPException(503, "activity journal failed integrity validation") from exc
        return JSONResponse(
            {
                "events": [event.to_dict() for event in rows],
                "window": window,
                "next_cursor": rows[-1].cursor if rows else after,
            }
        )

    @app.get("/api/v1/control/{command_id}")
    async def control_status(
        command_id: str, authorization: str | None = Header(default=None)
    ):
        """Read one Atlas command and its latest controller receipt."""

        _auth(authorization, SCOPE_READ)
        if control_journal is None:
            raise HTTPException(501, "control journal is not configured")
        try:
            command, receipt = await asyncio.to_thread(control_journal.get, command_id)
        except KeyError as exc:
            raise HTTPException(404, "control command not found") from exc
        except ControlCorruptionError as exc:
            raise HTTPException(503, "control journal failed integrity validation") from exc
        return JSONResponse({"command": command.to_public_dict(), "receipt": receipt.to_dict()})

    @app.post("/api/v1/control")
    async def atlas_control(
        request: Request, authorization: str | None = Header(default=None)
    ):
        """Submit an authenticated Atlas steering command.

        Session message/cancel commands are applied synchronously through the
        existing harness seams. Run/agent/job commands remain durably queued for
        their owning controller; a queued receipt never claims actuation.
        """

        try:
            body = await request.json()
            target_kind = ControlTargetKind(body["target_kind"])
            target_id = str(body["target_id"])
            action = ControlAction(body["action"])
            idempotency_key = str(body["idempotency_key"])
            expected_state = str(body.get("expected_state") or "")
            payload = dict(body.get("payload") or {})
            ttl_s = float(body.get("ttl_s", 300))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(422, "invalid control command") from exc
        message_action = action in {
            ControlAction.MESSAGE,
            ControlAction.NEEDS_INPUT_RESPONSE,
        }
        required_scope = SCOPE_WRITE if message_action else SCOPE_DISPATCH
        auth = _authed_context(authorization, required_scope)
        if audit_log is None:
            raise HTTPException(501, "audit sink not configured; control denied")
        authorizer = authorize_inject if message_action else authorize_dispatch
        if authorizer is None:
            raise HTTPException(501, "authz PDP not configured; control denied")
        if control_journal is None:
            raise HTTPException(501, "control journal is not configured")
        subject = _subject(auth)
        resource = {
            "host": host_id,
            "target_kind": target_kind.value,
            "target_id": target_id,
            "action": action.value,
        }
        decision = authorizer(subject, resource, {})
        _emit_decision_obligations(decision)
        allow = bool(getattr(decision, "allow", False))
        _emit_audit(
            {
                "event": "skcode.atlas.control",
                "subject": subject,
                "resource": resource,
                "decision": "allow" if allow else "deny",
                "reason": getattr(decision, "reason", ""),
                "payload_sha256": _content_hash(
                    json.dumps(payload, sort_keys=True, default=str)
                ),
            }
        )
        if not allow:
            raise HTTPException(403, "control not authorized")
        actor = subject
        try:
            ActivityContext(session_id=actor)
        except ValueError:
            actor = "actor-" + _content_hash(subject)
        try:
            command, receipt, replayed = await asyncio.to_thread(
                control_journal.submit,
                actor=actor,
                idempotency_key=idempotency_key,
                target_kind=target_kind,
                target_id=target_id,
                action=action,
                payload=payload,
                expected_state=expected_state,
                ttl_s=ttl_s,
            )
        except ControlConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ControlCorruptionError, OSError) as exc:
            raise HTTPException(503, "control journal unavailable") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

        if replayed:
            return JSONResponse(
                {"command": command.to_public_dict(), "receipt": receipt.to_dict(), "replayed": replayed},
                status_code=200 if receipt.status in TERMINAL_CONTROL_STATUSES else 202,
            )
        if target_kind is not ControlTargetKind.SESSION:
            if control_handler is None:
                cursor = _publish_control_activity(command, ControlStatus.QUEUED)
                receipt = await asyncio.to_thread(
                    control_journal.record,
                    command.command_id,
                    ControlStatus.QUEUED,
                    controller="skcode-hostd",
                    detail="awaiting owning controller",
                    activity_cursor=cursor,
                )
                return JSONResponse(
                    {"command": command.to_public_dict(), "receipt": receipt.to_dict(), "replayed": False},
                    status_code=202,
                )
            await asyncio.to_thread(
                control_journal.record,
                command.command_id,
                ControlStatus.APPLYING,
                controller="skcode-hostd",
            )
            try:
                if inspect.iscoroutinefunction(control_handler):
                    result = control_handler(command)
                else:
                    result = await asyncio.to_thread(control_handler, command)
                if inspect.isawaitable(result):
                    result = await result
                if not isinstance(result, dict):
                    raise TypeError("control handler must return a mapping")
                status = ControlStatus(result.get("status", "rejected"))
                if status not in TERMINAL_CONTROL_STATUSES:
                    raise ValueError("control handler must return a terminal status")
                detail = str(result.get("detail") or "")[:1_024]
            except Exception as exc:  # noqa: BLE001 - handler failure becomes a receipt
                status = ControlStatus.REJECTED
                detail = type(exc).__name__
            cursor = _publish_control_activity(command, status, detail)
            receipt = await asyncio.to_thread(
                control_journal.record,
                command.command_id,
                status,
                controller="target-owner",
                detail=detail,
                activity_cursor=cursor,
            )
            return JSONResponse(
                {"command": command.to_public_dict(), "receipt": receipt.to_dict(), "replayed": False}
            )

        if command.expected_state:
            session = next(
                (item for item in await harness.list_sessions() if item.sid == target_id),
                None,
            )
            actual_state = session.state if session is not None else "missing"
            if actual_state != command.expected_state:
                detail = (
                    f"expected state {command.expected_state}; observed {actual_state}"
                )
                cursor = _publish_control_activity(command, ControlStatus.CONFLICT, detail)
                receipt = await asyncio.to_thread(
                    control_journal.record,
                    command.command_id,
                    ControlStatus.CONFLICT,
                    controller="skcode-hostd",
                    detail=detail,
                    activity_cursor=cursor,
                )
                return JSONResponse(
                    {"command": command.to_public_dict(), "receipt": receipt.to_dict()},
                    status_code=409,
                )

        supported = {
            ControlAction.MESSAGE,
            ControlAction.NEEDS_INPUT_RESPONSE,
            ControlAction.CANCEL,
        }
        if action not in supported:
            cursor = _publish_control_activity(command, ControlStatus.UNSUPPORTED)
            receipt = await asyncio.to_thread(
                control_journal.record,
                command.command_id,
                ControlStatus.UNSUPPORTED,
                controller="skcode-hostd",
                detail="interactive harness does not implement this action",
                activity_cursor=cursor,
            )
            return JSONResponse({"command": command.to_public_dict(), "receipt": receipt.to_dict()})

        receipt = await asyncio.to_thread(
            control_journal.record,
            command.command_id,
            ControlStatus.APPLYING,
            controller="skcode-hostd",
        )
        try:
            if action in {ControlAction.MESSAGE, ControlAction.NEEDS_INPUT_RESPONSE}:
                text = command.payload.get("text")
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("session message requires nonblank payload.text")
                result = await harness.inject(target_id, text)
                applied = bool(result.get("injected"))
            else:
                result = await harness.cancel(target_id)
                applied = bool(result.get("cancelled"))
            status = ControlStatus.APPLIED if applied else ControlStatus.REJECTED
            detail = str(result.get("reason") or "")[:1_024]
        except Exception as exc:  # noqa: BLE001 - receipt must record target failure
            status = ControlStatus.REJECTED
            detail = type(exc).__name__
        cursor = _publish_control_activity(command, status, detail)
        receipt = await asyncio.to_thread(
            control_journal.record,
            command.command_id,
            status,
            controller="skcode-hostd",
            detail=detail,
            activity_cursor=cursor,
        )
        return JSONResponse({"command": command.to_public_dict(), "receipt": receipt.to_dict()})

    @app.get("/api/v1/jobs")
    async def list_jobs_route(authorization: str | None = Header(default=None)):
        # Read-only view over the cron/scheduler ledger (spec section 8, card
        # C-8). Same read scope as sessions/events: it is a VIEW, never a store,
        # so it needs no write scope and no PDP decision. hostd owns none of this
        # data -- the scheduler does. Atlas job controls use the separate durable
        # command mailbox and remain queued until the scheduler owner consumes them. When no
        # jobs provider is wired (bare test doubles, or a host with no cron
        # ledger yet), this reports an empty list rather than 404/500: "no jobs
        # known yet" is a valid, unremarkable state.
        _auth(authorization, SCOPE_READ)
        rows: list[JobRun] = list_jobs() if list_jobs is not None else []
        return JSONResponse({"jobs": [r.to_dict() for r in rows]})

    @app.get("/api/v1/watchdog/digest")
    async def digest_route(authorization: str | None = Header(default=None)):
        # Read-only view over the published skwatchdog digest artifact (card
        # C-14a, answering C-14). Same read scope as sessions/jobs: it is a
        # VIEW, never a store, so it needs no write scope and no PDP decision.
        # hostd owns none of this data -- skos does -- and there is no
        # publish/regenerate/delete route here or anywhere else in this daemon.
        #
        # Fail safe and honest (card C-14a's hard rule): "no digest published
        # yet" and "today was quiet" are DIFFERENT facts, so they must never
        # collapse into the same response. When no provider is wired, or the
        # provider reports nothing to read (missing directory, missing file,
        # a permission error), this is a 404: an honest "nothing published
        # yet", never a 200 with a fabricated empty digest that would look
        # exactly like a real quiet day. When something WAS published, the
        # raw bytes are served exactly as read (never parsed, never
        # reformatted) with a 200 -- including when the file on disk is not
        # valid JSON, so a corrupt artifact degrades to a body the client
        # cannot parse (a third, distinguishable, honest state) rather than
        # a 500 or a silently "fixed" digest.
        _auth(authorization, SCOPE_READ)
        raw = read_digest() if read_digest is not None else None
        if raw is None:
            raise HTTPException(404, "no digest has been published yet")
        return Response(content=raw, media_type="application/json")

    @app.post("/api/v1/sessions/{sid}/ratify")
    async def ratify_session(sid: str, authorization: str | None = Header(default=None)):
        # The ONE write-ish route: grade-only. It runs the autocode twin gate over
        # the session's EXISTING worktree diff. It never merges/commits/pushes (see
        # skharness.autocode.ratify), so it does not modify the repo. It is still
        # a WRITE-class action (it actuates a grade over the session), so it needs
        # SCOPE_WRITE: a read-only token cannot trigger it. CR-6.2 C2/C8: it also
        # passes the PDP mode floor (verified skcode.inject) when the authorizer is
        # wired, exactly like inject.
        auth = _authed_context(authorization, SCOPE_WRITE)
        _enforce_inject_floor(_subject(auth), {"host": host_id, "sid": sid, "action": "ratify"})
        session = next((s for s in await harness.list_sessions() if s.sid == sid), None)
        if session is None:
            raise HTTPException(404, "session not found")
        if resolve_ratify is None:
            raise HTTPException(501, "ratify is not configured on this host")
        ctx = resolve_ratify(session)
        if ctx is None:
            raise HTTPException(409, "session has no gradable worktree")
        repo, worktree, acceptance = ctx
        result = _ratify(repo, worktree, acceptance, harness)  # grade only, no merge
        if audit_log is not None:
            audit_log(f"ratify {sid} {'PASS' if result.passed else 'FAIL'} score={result.score}")
        if not result.passed:
            # A failed gate needs an operator: emit a needs_input event (this drives
            # the sk-alert push in production).
            if emit_event is not None:
                emit_event(
                    sid,
                    SessionEvent(
                        type=EventType.NEEDS_INPUT,
                        text=f"ratify failed for {sid} (score={result.score})",
                        data={
                            "sid": sid,
                            "score": result.score,
                            "passed": False,
                            "notes": result.notes,
                        },
                    ),
                )
        return JSONResponse(
            {
                "sid": sid,
                "score": result.score,
                "passed": result.passed,
                "notes": result.notes,
                "artifact": result.artifact,
                "mode": result.mode,
            }
        )

    @app.post("/api/v1/sessions/{sid}/inject")
    async def inject_session(
        sid: str, request: Request, authorization: str | None = Header(default=None)
    ):
        # The P1 session WRITE surface: send operator text into a running session
        # as keystrokes. Gated exactly like the other bearer routes and fails
        # closed BEFORE any actuation: with the P0 deny-all verifier a request
        # with no/invalid token is 401/403 and harness.inject is never reached.
        # A read-only (skcode.stream) token is 403 here too: inject needs
        # SCOPE_WRITE, so enabling view never arms keystroke-inject.
        auth = _authed_context(authorization, SCOPE_WRITE)
        subject = _subject(auth)
        # CR-6.2 C2/C8: PDP mode floor. When configured, inject routes through
        # capauth.authz.decide requiring the verified-tier skcode.inject
        # capability; a deny is 403 (audited) and the harness is never reached.
        _enforce_inject_floor(subject, {"host": host_id, "sid": sid, "action": "inject"})
        try:
            body = await request.json()
        except Exception:
            body = {}
        text = (body or {}).get("text", "")
        result = await harness.inject(sid, text)
        # CR-6.2 C3: enrich the inject audit with the subject + a content HASH
        # (never the raw keystrokes) so the action is attributable to WHO + WHAT
        # without recording secrets typed into a session.
        _emit_audit(
            {
                "event": "skcode.inject",
                "subject": subject,
                "sid": sid,
                "decision": "allow",
                "injected": bool(result.get("injected")),
                "reason": result.get("reason", ""),
                "content_sha256": _content_hash(text),
                "content_len": len(text or ""),
            }
        )
        return JSONResponse(result)

    @app.post("/api/v1/sessions/{sid}/deny")
    async def deny_session(sid: str, authorization: str | None = Header(default=None)):
        # The REFUSAL half of the needs_input banner (card C-13). Approve calls
        # ratify; before this route existed, Deny was a POST to /inject carrying a
        # literal "n", which is not a refusal: it is a fresh message to the
        # session, and a 200 told the operator nothing about whether anything was
        # actually refused. This route actuates ``harness.deny`` instead, which
        # interrupts the in-flight turn and latches the session as refused.
        #
        # Gated EXACTLY like ratify (its Approve twin), not like cancel: the same
        # SCOPE_WRITE bearer scope and the same PDP mode floor over the same
        # skcode.inject capability, so refusing costs the caller precisely what
        # approving costs and no operator who can Approve is left unable to Deny.
        # A read-only (skcode.stream) token is 403 here, and with the PDP wired a
        # below-floor subject is 403 (audited) with the harness never reached.
        auth = _authed_context(authorization, SCOPE_WRITE)
        subject = _subject(auth)
        _enforce_inject_floor(subject, {"host": host_id, "sid": sid, "action": "deny"})
        # Idempotent + honest by construction: harness.deny returns a clean
        # {"denied": False, "reason": ...} for an unknown / already-finished /
        # out-of-scope session rather than raising, so this route never 500s on a
        # stale sid AND never reports a refusal that did not take effect as a
        # success. The client reads ``denied`` (was it refused at all) and
        # ``interrupted`` (was in-flight work really stopped); a 200 alone is
        # never the answer.
        result = await harness.deny(sid)
        _emit_audit(
            {
                "event": "skcode.deny",
                "subject": subject,
                "sid": sid,
                # "decision" is the AUTHORIZATION outcome (the PDP let this caller
                # refuse); "denied" is the REFUSAL outcome (the harness actually
                # refused something). They are different questions and both belong
                # in the record.
                "decision": "allow",
                "denied": bool(result.get("denied")),
                "interrupted": bool(result.get("interrupted")),
                "reason": result.get("reason", ""),
            }
        )
        return JSONResponse(result)

    @app.get("/api/v1/dispatch/targets")
    async def dispatch_targets_route(authorization: str | None = Header(default=None)):
        # ADVISORY ONLY (spec 4.2): tells a dispatch-capable device which repos /
        # harnesses / hosts / profiles it may target here, so the UI never offers a
        # forbidden action. It is NOT the enforcement point: POST /dispatch
        # re-derives and re-checks everything server-side and never trusts the
        # client's target choice. Gated on the dispatch scope, so a view/inject-only
        # device sees nothing to dispatch.
        _auth(authorization, SCOPE_DISPATCH)
        base = {
            "harnesses": [harness.name],
            "hosts": [host_id],
            "repos": [],
            "profiles": ["sandbox", "full"],
        }
        if dispatch_targets is not None:
            base.update(dispatch_targets() or {})
        return JSONResponse({"advisory": True, **base})

    @app.post("/api/v1/dispatch")
    async def dispatch_route(request: Request, authorization: str | None = Header(default=None)):
        # The RCE surface. Fail-closed gate order (spec 7.4):
        #   0. emergency brake: if dispatch is paused, 503 REGARDLESS of auth, and
        #      nothing downstream (auth, authz, spawn) runs.
        if dispatch_paused is not None and dispatch_paused():
            raise HTTPException(503, "dispatch paused")
        #   1. authn + scope: no token => 401, wrong/insufficient scope => 403.
        auth = _authed_context(authorization, SCOPE_DISPATCH)
        #   2. audit + authz MUST be configured, else deny (never allow blind).
        if audit_log is None:
            raise HTTPException(501, "audit sink not configured; dispatch denied")
        if authorize_dispatch is None:
            raise HTTPException(501, "authz PDP not configured; dispatch denied")
        #   3. parse + shape the request (auth is NOT input validation).
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body or {}
        repo = str(body.get("repo", "") or "")
        branch = str(body.get("branch", "") or "")
        profile = str(body.get("profile", "sandbox") or "sandbox")
        permission_mode = str(body.get("permission_mode", "manual") or "manual")
        # Session mode: "direct" (print/one-shot) or "interactive" (stays open).
        # Validate here (400) so a bad mode never reaches spawn; this is input
        # validation only and does NOT change any auth/authz/scope gate above.
        mode = str(body.get("mode", "direct") or "direct")
        if mode not in ("direct", "interactive"):
            raise HTTPException(400, f"invalid mode {mode!r} (want 'direct' or 'interactive')")
        prompt = str(body.get("prompt", "") or "")
        subject = _subject(auth)
        resource = {"host": host_id, "repo": repo, "branch": branch, "profile": profile}
        #   4. authz decision (the allow gate) + audit ALWAYS (allow or deny).
        decision = authorize_dispatch(subject, resource, {"permission_mode": permission_mode})
        _emit_decision_obligations(decision)
        allow = bool(getattr(decision, "allow", False))
        _emit_audit(
            {
                "event": "skcode.dispatch",
                "subject": subject,
                "decision": "allow" if allow else "deny",
                "request": {
                    "harness": str(body.get("harness", "") or ""),
                    "host": host_id,
                    "repo": repo,
                    "branch": branch,
                    "profile": profile,
                    "permission_mode": permission_mode,
                    "mode": mode,
                    "model": str(body.get("model", "") or ""),
                    "prompt_len": len(prompt),
                },
                "reason": getattr(decision, "reason", ""),
            }
        )
        if not allow:
            raise HTTPException(403, "dispatch not authorized")
        #   5. spawn. The harness re-runs the RCE input guards (allowlist / branch /
        #      charset) and fails closed with SpawnRejected => 400 (never a 5xx),
        #      so a bad repo/branch/name never reaches a subprocess.
        desc = SessionDescriptor(
            host=host_id,
            harness=str(body.get("harness", "") or harness.name),
            repo=repo,
            branch=branch,
            model=str(body.get("model", "") or ""),
            quality=profile,
            permission_mode=permission_mode,
            mode=mode,
        )
        try:
            session = await harness.spawn(desc, prompt=prompt)
        except SpawnRejected as exc:
            _emit_audit(
                {
                    "event": "skcode.dispatch.rejected",
                    "subject": subject,
                    "resource": resource,
                    "reason": str(exc),
                }
            )
            raise HTTPException(400, f"spawn rejected: {exc}")
        _emit_audit(
            {
                "event": "skcode.dispatch.spawned",
                "subject": subject,
                "sid": session.sid,
                "resource": resource,
            }
        )
        return JSONResponse(
            {
                "sid": session.sid,
                "status": session.status,
                "branch": session.branch,
                "profile": profile,
                "mode": mode,
            }
        )

    @app.post("/api/v1/sessions/{sid}/cancel")
    async def cancel_session(sid: str, authorization: str | None = Header(default=None)):
        # Cancel a LIVE session (spec section 8). It rides the DISPATCH scope
        # through the SAME PDP decision path as POST /dispatch: a caller needs
        # skcode.dispatch AND an authz allow, exactly like spawning a new
        # session, so a read-only or inject-only token can never cancel. Fail
        # closed like dispatch: no audit sink or no authz PDP configured => 501,
        # never a silent allow.
        auth = _authed_context(authorization, SCOPE_DISPATCH)
        if audit_log is None:
            raise HTTPException(501, "audit sink not configured; cancel denied")
        if authorize_dispatch is None:
            raise HTTPException(501, "authz PDP not configured; cancel denied")
        subject = _subject(auth)
        resource = {"host": host_id, "sid": sid, "action": "cancel"}
        decision = authorize_dispatch(subject, resource, {})
        _emit_decision_obligations(decision)
        allow = bool(getattr(decision, "allow", False))
        _emit_audit(
            {
                "event": "skcode.cancel",
                "subject": subject,
                "sid": sid,
                "decision": "allow" if allow else "deny",
                "reason": getattr(decision, "reason", ""),
            }
        )
        if not allow:
            raise HTTPException(403, "cancel not authorized")
        # Idempotent + safe by construction: harness.cancel returns a clean
        # {"cancelled": False, "reason": ...} for an unknown/already-finished
        # session rather than raising, so this route never 500s on a stale sid.
        result = await harness.cancel(sid)
        return JSONResponse(result)

    @app.websocket("/api/v1/sessions/{sid}/stream")
    async def stream(websocket: WebSocket, sid: str):
        # Browsers cannot set headers on a WebSocket, so the token rides the query
        # string. Fail closed (close 1008) BEFORE accept on a bad/missing token.
        token = websocket.query_params.get("token")
        if not check_token(token, verify_caller, SCOPE_READ):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        source = _session_source(sid)
        try:
            async for ev in harness.stream(sid):
                # SessionEvent v2 (spec 5.1): assign seq/sid/source at append,
                # here at the one point every live event actually passes through
                # the daemon, then (when persistence is configured) archive it.
                stamped = store.append(sid, ev, source=source)
                await websocket.send_json(stamped.to_dict())
        except WebSocketDisconnect:
            return
        await websocket.close()

    @app.websocket("/api/v1/activity/stream")
    async def activity_stream(websocket: WebSocket):
        """Cursor-resumable activity tail with bounded batches and heartbeats."""

        token = websocket.query_params.get("token")
        if not check_token(token, verify_caller, SCOPE_READ):
            await websocket.close(code=1008)
            return
        try:
            cursor = int(websocket.query_params.get("after", "0"))
            if cursor < 0:
                raise ValueError
        except ValueError:
            await websocket.close(code=1008)
            return
        filters = {
            "session_id": websocket.query_params.get("session_id", ""),
            "run_id": websocket.query_params.get("run_id", ""),
            "agent_id": websocket.query_params.get("agent_id", ""),
            "job_id": websocket.query_params.get("job_id", ""),
            "card_id": websocket.query_params.get("card_id", ""),
            "contract_id": websocket.query_params.get("contract_id", ""),
            "lease_id": websocket.query_params.get("lease_id", ""),
            "role": websocket.query_params.get("role", ""),
            "kind": websocket.query_params.get("kind", ""),
        }
        await websocket.accept()
        heartbeat_at = asyncio.get_running_loop().time()
        try:
            while True:
                if activity_journal is None:
                    rows = []
                    window = {
                        "retained_from_cursor": 1,
                        "head_cursor": 0,
                        "retained_events": 0,
                    }
                else:
                    try:
                        rows = await asyncio.to_thread(
                            activity_journal.read_after,
                            cursor,
                            limit=100,
                            **filters,
                        )
                        window = await asyncio.to_thread(activity_journal.window)
                    except ValueError:
                        await websocket.close(code=1008)
                        return
                    except ActivityCorruptionError:
                        await websocket.send_json(
                            {
                                "type": "error",
                                "code": "activity_integrity_failure",
                            }
                        )
                        await websocket.close(code=1011)
                        return
                retained = int(window["retained_from_cursor"])
                if cursor and cursor < retained - 1:
                    await websocket.send_json(
                        {
                            "type": "gap",
                            "requested_after": cursor,
                            **window,
                        }
                    )
                    cursor = retained - 1
                    continue
                if rows:
                    for event in rows:
                        await websocket.send_json(
                            {"type": "activity", "event": event.to_dict()}
                        )
                        cursor = event.cursor
                    heartbeat_at = asyncio.get_running_loop().time()
                    continue
                now = asyncio.get_running_loop().time()
                if now - heartbeat_at >= 10:
                    await websocket.send_json(
                        {
                            "type": "heartbeat",
                            "cursor": cursor,
                            **window,
                        }
                    )
                    heartbeat_at = now
                await asyncio.sleep(0.25)
        except WebSocketDisconnect:
            return

    @app.get("/", response_class=HTMLResponse)
    @app.get("/app", response_class=HTMLResponse)
    async def client_page():
        return HTMLResponse(_client_html(client_dir))

    return app
