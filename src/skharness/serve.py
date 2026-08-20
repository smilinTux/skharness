"""skcode-hostd runner. Binds a Tailscale IP ONLY (never 0.0.0.0), port 9394.

Port 9390 is owned by the skcomms broker_server (its honest, documented default).
skcode-hostd therefore takes 9394 as its ratified default (SKWorld platform spec
R0.4) so the two never collide on a shared host. Pass --port to override.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import platform
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from skharness.arena import ArenaJobService, ArenaStatusService, ArenaStore, ProbeResult
from skharness.arena.collaboration import RefinementJournal
from skharness.auth import AuthContext, Verifier
from skharness.autocode.sessions import AutocodeSessionRegistry
from skharness.daemon import build_daemon_app
from skharness.digest import read_latest_digest
from skharness.harnesses.claude_code import ClaudeCodeHarness, parse_repo_allowlist
from skharness.jobs import read_job_runs
from skharness.session_events import SessionEventStore

DEFAULT_PORT = 9394

_WILDCARD = {"0.0.0.0", "::"}

# The audience a wire token must be scoped to for skcode-hostd (spec R4.2).
SKCODE_AUDIENCE = "skcode"

# The capability the dispatch PDP decides on (spec 7.4).
DISPATCH_CAPABILITY = "skcode.dispatch"

# The capability the inject/ratify PDP decides on (CR-6.2 C2/C8). Verified tier
# (RCE keystroke-inject into a running agent PTY). Seeded in
# capauth.authz.DEFAULT_RULES (C3).
INJECT_CAPABILITY = "skcode.inject"

# CR-3.2: the daemon now converges on REAL capauth token verification by DEFAULT
# (see select_verifier). REAL_VERIFIER_ENV is kept for backward compatibility as a
# redundant explicit opt-in (truthy still forces the real verifier); it is no
# longer required, because real is the default.
REAL_VERIFIER_ENV = "SKCODE_REAL_VERIFIER"

# Escape hatch: force the deny-all placeholder even when capauth is importable.
# Truthy -> deny-all. This is the ONLY way to turn the real verifier OFF, and it
# still fails CLOSED (denies everything). Unset/off keeps the real capauth
# verifier (the CR-3.2 default).
FORCE_DENY_ENV = "SKCODE_FORCE_DENY_ALL"
_TRUTHY = {"1", "true", "yes", "on"}


def _env_truthy(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    return default if value is None else value.strip().lower() in _TRUTHY


def build_http_probe(env_name: str):
    """Build a bounded dependency probe from an explicitly configured health URL.

    No URL means unknown. Only a 2xx response proves health; connection errors,
    authentication failures, and malformed configuration are reported as errors.
    """
    url = os.environ.get(env_name, "").strip()
    if not url:
        return lambda: ProbeResult(None, f"{env_name} not configured")

    def _check() -> ProbeResult:
        try:
            request = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(request, timeout=1.0) as response:
                status = int(response.status)
            if 200 <= status < 300:
                return ProbeResult(True, f"HTTP {status}")
            return ProbeResult(False, f"HTTP {status}")
        except (OSError, ValueError, urllib.error.URLError) as exc:
            return ProbeResult(False, f"{type(exc).__name__}: {exc}")

    return _check


def build_gpu_probe():
    """Observe NVIDIA runtime truth; absence/timeout is not treated as healthy."""

    def _check() -> ProbeResult:
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=uuid,memory.total", "--format=csv,noheader"],
                capture_output=True,
                check=False,
                text=True,
                timeout=2.0,
            )
        except FileNotFoundError:
            return ProbeResult(None, "nvidia-smi not installed")
        except subprocess.TimeoutExpired:
            return ProbeResult(False, "nvidia-smi timed out")
        rows = [row.strip() for row in result.stdout.splitlines() if row.strip()]
        if result.returncode != 0:
            return ProbeResult(False, result.stderr.strip() or "nvidia-smi failed")
        if not rows:
            return ProbeResult(None, "no NVIDIA GPU telemetry returned")
        return ProbeResult(True, f"{len(rows)} GPU(s) observed")

    return _check


def build_arena_status_service() -> ArenaStatusService:
    """Compose the live arena state root and explicitly required dependencies."""
    enabled = _env_truthy("SKHARNESS_ARENA_ENABLED")
    arena_root = skcode_state_dir() / "arena"
    store = ArenaStore(arena_root)
    jobs = ArenaJobService(arena_root / "job-runs.jsonl", node=platform.node())
    refinements = RefinementJournal(
        arena_root / "refinements", approvers=(), evidence_exists=lambda _digest: False
    )
    return ArenaStatusService(
        store=store,
        refinements=refinements.events,
        scheduled_runs=jobs.status,
        gateway_probe=build_http_probe("SKHARNESS_ARENA_SKGATEWAY_HEALTH_URL"),
        verifier_probe=build_http_probe("SKHARNESS_ARENA_VERIFIER_HEALTH_URL"),
        gpu_probe=build_gpu_probe(),
        serving_backend_probe=build_http_probe("SKHARNESS_ARENA_SERVING_BACKEND_HEALTH_URL"),
        require_gateway=enabled,
        require_verifier=enabled,
        require_gpu=enabled and _env_truthy("SKHARNESS_ARENA_REQUIRE_GPU"),
        require_serving_backend=(enabled and _env_truthy("SKHARNESS_ARENA_REQUIRE_GPU")),
    )


def resolve_bind(host: str | None) -> str:
    if not host or host.strip() in _WILDCARD:
        raise SystemExit(
            "skcode-hostd refuses to bind a wildcard/public address; "
            "pass a Tailscale IP via --host"
        )
    return host.strip()


def build_default_verifier() -> Verifier:
    # P0 placeholder: fail closed. Real capauth verification is wired with the
    # pairing work (spec 7.6); a read-only MVP must never accept a token blindly.
    def _deny_all(token: str) -> bool:
        return False

    return _deny_all


def build_capauth_verifier(home: Path | None = None) -> Verifier:
    """A real capauth verifier for skcode-hostd (R2.4), scope-carrying.

    Accepts a caller ONLY when the bearer is a valid capauth SKCODE-audience
    token: the wire form is base64url of ``export_token(...)``, so the verifier
    base64url-decodes it, ``import_token``s the JSON, then requires
    ``verify_audience_token(t, "skcode")`` (signature + time validity + audience
    match). It is capauth-only and self-contained.

    On success it returns an :class:`AuthContext` carrying the token's granted
    scopes (its ``capabilities``), NOT a bare ``True``. That is what lets the
    daemon split read from write on one valid token: read routes require
    ``skcode.stream`` and write routes require ``skcode.inject``, both checked
    against this context via ``has_scope``. The audience/signature/time gate is
    unchanged; scopes are only READ off the already-verified token, never used to
    widen the accept decision.

    It fails CLOSED on any parse/verify error: a non-base64 string, non-token
    JSON, an expired/garbage/unsigned token, a wrong-audience token, or an
    unscoped (legacy audience=None) token all return False. ``home`` selects the
    capauth keyring home; ``None`` uses capauth's default (~/.skcapstone).
    """
    # Import inside the factory so the module has no hard capauth import at load
    # time (the deny-all default path stays capauth-free).
    from capauth import import_token, verify_audience_token

    def _verify(token: str):
        try:
            token = (token or "").strip()
            if not token:
                return False
            # base64url decode, tolerating missing '=' padding.
            padded = token + "=" * (-len(token) % 4)
            token_json = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
            signed = import_token(token_json)
            if not verify_audience_token(signed, SKCODE_AUDIENCE, home=home):
                return False
            # Verified: expose the token's granted scopes so routes can split
            # read (skcode.stream) from write (skcode.inject), plus the subject
            # fqid so the dispatch route can pass it to the authz PDP.
            return AuthContext(
                scopes=frozenset(signed.payload.capabilities or ()),
                subject=getattr(signed.payload, "subject", None),
            )
        except Exception:
            # Fail closed on ANY error: bad base64, bad JSON, bad token, keyring
            # miss, etc. Never let a caller through on an exception.
            return False

    return _verify


def skcode_state_dir() -> Path:
    """The per-host skcode state dir (pause flag, audit log, worktrees live here).

    Rooted at ``SKCODE_STATE_DIR`` when set (tests point it at a tmp dir),
    otherwise ``~/.skcapstone/skcode``.
    """
    root = os.environ.get("SKCODE_STATE_DIR")
    return Path(root) if root else Path.home() / ".skcapstone" / "skcode"


def build_audit_log():
    """A structured audit sink for the dispatch surface (spec 7.4).

    Appends one JSON line per event to ``<state>/audit.log``. The dispatch route
    REQUIRES an audit sink to be configured (fails closed to 501 without one), so
    every allow/deny/spawn/reject is recorded. Best-effort on I/O errors: an audit
    write failure must never crash the daemon, but the sink is always present.
    """
    path = skcode_state_dir() / "audit.log"

    def _audit(line: str) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": time.time(), "record": line}) + "\n")
        except OSError:
            pass

    return _audit


def build_dispatch_authorizer(home: Path | None = None):
    """Wire the real capauth authz PDP for the dispatch capability (spec 7.4).

    Returns a callable ``(subject, resource, context) -> Decision`` that delegates
    to ``capauth.authz.decide`` with a rule table that ADDS a ``skcode.dispatch``
    rule (VERIFIED enrollment, RCE being the most sensitive capability) on top of
    capauth's seeded defaults. This adds no policy engine of its own: the daemon is
    the PEP, capauth is the PDP. The decision carries the mandatory audit
    obligation the daemon then writes.

    Fails closed: if capauth cannot be imported, returns None so the daemon denies
    dispatch (501 authz-not-configured) rather than allowing it unauthenticated.
    """
    try:
        from capauth.authz import DEFAULT_RULES, CapabilityRule, Decision, decide
        from capauth.pairing import EnrollmentMode
    except Exception:
        return None

    rules = dict(DEFAULT_RULES)
    rules[DISPATCH_CAPABILITY] = CapabilityRule(
        capability=DISPATCH_CAPABILITY,
        required_capability=DISPATCH_CAPABILITY,
        minimum_mode=EnrollmentMode.VERIFIED,
        description="Spawn a NEW agent session (RCE): most sensitive, verified only.",
    )

    def _authorize(subject: str, resource: dict, context: dict):
        dec = decide(subject, DISPATCH_CAPABILITY, resource, context, base_dir=home, rules=rules)
        # HARDENED 2026-08-05: the `full` profile spawns a session with the real
        # operator identity + HOME + MCP (the widest blast radius). Restrict it to
        # an explicit subject allowlist; every other verified operator is
        # sandbox-only. Reuse the decision's audit obligation so the PEP still
        # records exactly one audit entry. Lumina is the default allowed subject.
        if (
            dec.allow
            and str(resource.get("profile", "")) == "full"
            and not full_profile_allowed(subject)
        ):
            return Decision(
                allow=False,
                reason=(
                    f"full profile denied for {subject!r}: not on "
                    "SKCODE_FULL_PROFILE_SUBJECTS (sandbox-only operator)"
                ),
                obligations=dec.obligations,
            )
        return dec

    return _authorize


def build_inject_authorizer(home: Path | None = None):
    """Wire the real capauth authz PDP for the inject/ratify/deny write surface
    (CR-6.2 C2/C8; card C-13 added deny).

    Mirrors :func:`build_dispatch_authorizer` but for ``skcode.inject`` at the
    VERIFIED floor, so inject/ratify/deny enforce the enrollment-mode floor in CODE (a
    ``decide`` allow), not only at token issuance. Returns a callable
    ``(subject, resource, context) -> Decision``.

    Fails CLOSED: if capauth cannot be imported, returns a deny-all authorizer
    (every inject denied) rather than None, so a broken capauth install can never
    silently drop the floor back to scope-only. The bearer/scope gate still runs
    first, and the deny-all verifier pin still denies before this is ever reached.
    """
    try:
        from capauth.authz import DEFAULT_RULES, CapabilityRule, decide
        from capauth.pairing import EnrollmentMode
    except Exception:

        def _deny_all(subject: str, resource: dict, context: dict):
            class _D:
                allow = False
                reason = "capauth unavailable: inject denied (fail closed)"
                obligations: list = []

            return _D()

        return _deny_all

    rules = dict(DEFAULT_RULES)
    # skcode.inject is now seeded in DEFAULT_RULES (C3); re-assert it here as
    # belt-and-suspenders so the floor holds even against an older capauth.
    rules[INJECT_CAPABILITY] = CapabilityRule(
        capability=INJECT_CAPABILITY,
        required_capability=INJECT_CAPABILITY,
        minimum_mode=EnrollmentMode.VERIFIED,
        description="Send operator keystrokes into a running agent PTY (RCE): verified only.",
    )

    def _authorize(subject: str, resource: dict, context: dict):
        return decide(subject, INJECT_CAPABILITY, resource, context, base_dir=home, rules=rules)

    return _authorize


#: Subjects allowed to dispatch the `full` profile (real identity + HOME + MCP).
#: Comma-separated env; defaults to the enrolled operator. Everyone else who
#: passes the dispatch gate is restricted to the sandbox profile.
_DEFAULT_FULL_SUBJECTS = "lumina@chef.skworld.io"


def full_profile_allowed(subject: str) -> bool:
    """True when ``subject`` may dispatch the ``full`` profile (see
    ``SKCODE_FULL_PROFILE_SUBJECTS``). Blank/unknown subjects are never allowed."""
    if not subject:
        return False
    raw = os.environ.get("SKCODE_FULL_PROFILE_SUBJECTS", _DEFAULT_FULL_SUBJECTS)
    allowed = {s.strip() for s in raw.split(",") if s.strip()}
    return subject in allowed


def build_dispatch_targets():
    """Advisory targets provider: the repos on this host's dispatch allowlist.

    Truthful to the server-side allowlist (SKCODE_DISPATCH_REPOS), so the UI only
    offers repos the daemon would actually accept. Advisory only; /dispatch
    re-enforces the allowlist in the harness spawn guard.
    """

    def _targets() -> dict:
        return {"repos": parse_repo_allowlist(os.environ.get("SKCODE_DISPATCH_REPOS", ""))}

    return _targets


def build_jobs_provider():
    """Wire ``GET /api/v1/jobs`` (spec section 8, card C-8) to the REAL cron
    ledger at its default path (``~/.skcapstone/logs/cron-ledger.jsonl``, or
    ``$SKCODE_CRON_LEDGER_PATH`` when set).

    Reads fresh on every call (:func:`skharness.jobs.read_job_runs` opens the
    ledger file itself); this daemon caches nothing about jobs, matching the
    "the Code section is a view, never a store" rule. Fails safe by
    construction: a missing/empty/malformed ledger degrades to an empty or
    partial list, never an exception, so this callable can never make the
    route 500.
    """

    def _jobs():
        return read_job_runs()

    return _jobs


def build_digest_provider():
    """Wire ``GET /api/v1/watchdog/digest`` (card C-14a) to the REAL published
    digest artifact at its default path
    (``~/.skcapstone/watchdog/digests/latest/digest.json``, or
    ``$SKCODE_WATCHDOG_DIGEST_PATH`` when set) -- the exact file
    ``skos.watchdog.publish.publish_digest`` writes.

    Reads fresh on every call (:func:`skharness.digest.read_latest_digest`
    opens the file itself); this daemon caches nothing about the digest,
    matching the "the Code section is a view, never a store" rule. Fails
    safe by construction: a missing directory, a missing file, or a
    permission error all degrade to ``None`` (served as 404, "no digest
    published yet"), never an exception, so this callable can never make
    the route 500.
    """

    def _digest():
        return read_latest_digest()

    return _digest


def select_verifier() -> Verifier:
    """Pick the verifier the daemon runs with (CR-3.2: real capauth by default).

    The daemon now performs REAL capauth token verification by default: a caller
    is accepted only with a valid, signed, unexpired, unrevoked, skcode-audience
    capauth token (see ``build_capauth_verifier``). Deny-all is the fail-closed
    FALLBACK, never the happy path. It is returned only when:

    * ``SKCODE_FORCE_DENY_ALL`` is truthy (operator escape hatch), OR
    * capauth cannot be imported / the verifier cannot be constructed (capauth
      unreachable). Construction failure is caught here and falls back to
      deny-all, so a broken capauth install DENIES every caller rather than
      crashing the daemon or, worse, letting anyone through.

    Either way the RCE surface stays gated: the real verifier itself fails closed
    on every missing/invalid/expired/revoked/wrong-audience token, and the
    fallback denies all. There is no configuration in which a bad or absent token
    is accepted. Routes, the bind guard, and the gate wiring are untouched.
    """
    if os.environ.get(FORCE_DENY_ENV, "").strip().lower() in _TRUTHY:
        return build_default_verifier()
    try:
        return build_capauth_verifier()
    except Exception:
        # capauth unreachable / import or construction error -> fail CLOSED.
        return build_default_verifier()


def main(argv: list[str] | None = None) -> int | None:
    """``skcode-hostd`` entry point.

    ``skcode-hostd operator ...`` routes to the operator-facet CLI (spec 4.2, the
    seam Atlas's skcode adapter drives). Anything else is the daemon runner, whose
    ``--host`` contract is unchanged (backwards compatible with
    ``python -m skharness --host <ip>``).
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "operator":
        from skharness.operator_cli import main as operator_main

        return operator_main(args[1:])
    _serve(args)
    return 0


def _serve(argv: list[str]) -> None:
    import uvicorn

    parser = argparse.ArgumentParser(prog="skcode-hostd")
    parser.add_argument("--host", required=True, help="Tailscale IP to bind")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host-id", default=".158", help="node id for hosts/self")
    args = parser.parse_args(argv)

    host = resolve_bind(args.host)
    # The harness reads its dispatch allowlist from SKCODE_DISPATCH_REPOS by default
    # (empty => deny all) and scopes worktrees under the skcode state dir.
    harness = ClaudeCodeHarness(
        host=args.host_id,
        worktree_root=skcode_state_dir() / "worktrees",
    )
    from skharness.operator_cli import dispatch_is_paused

    # SessionEvent v2 (card C-1, spec 5.3): a real, persisting event store, and
    # the autocode session registry merged into GET /sessions so orchestrator
    # runs (source=autocode) appear on the same rail as this harness's
    # interactive sessions. Both root under skcode_state_dir()/sessions, the
    # SAME per-sid directory (events.jsonl + session.json side by side).
    sessions_dir = skcode_state_dir() / "sessions"
    event_store = SessionEventStore(root=sessions_dir)
    autocode_registry = AutocodeSessionRegistry(root=sessions_dir)

    app = build_daemon_app(
        harness=harness,
        verify_caller=select_verifier(),
        host_id=args.host_id,
        audit_log=build_audit_log(),
        authorize_dispatch=build_dispatch_authorizer(),
        authorize_inject=build_inject_authorizer(),
        dispatch_targets=build_dispatch_targets(),
        dispatch_paused=dispatch_is_paused,
        event_store=event_store,
        list_autocode_sessions=autocode_registry.list,
        list_jobs=build_jobs_provider(),
        read_digest=build_digest_provider(),
        arena_status=build_arena_status_service(),
    )
    uvicorn.run(app, host=host, port=args.port)
