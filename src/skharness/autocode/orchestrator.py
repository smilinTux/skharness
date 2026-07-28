"""The harness-agnostic meta-orchestrator: phases 0-3, dry-run, kill switch,
caps, resume. Engineering executor internals live in engineering.py (Phase E);
here we only wire the phases, routing, decision queue, and guardrails.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .types import (WorkItem, AssessBrief, DecisionItem, QualityMode,
                    QUALITY_RANK, coerce_quality, ClaimRaced)
from .executor import EXECUTORS
from .config import Caps, Config
from .harness import build_harness
from . import fleet_dispatch, health, journal


@dataclass
class CapLedger:
    """Running token/dollar tally, checked between items."""
    caps: Caps
    tokens: int = 0
    usd: float = 0.0

    def add(self, tokens: int = 0, usd: float = 0.0) -> None:
        self.tokens += int(tokens or 0)
        self.usd += float(usd or 0.0)

    def exceeded(self) -> bool:
        return (self.tokens > self.caps.max_tokens_per_run
                or self.usd > self.caps.max_usd_per_day)


def kill_switch_active(enabled: bool) -> bool:
    """True when the run must stop cleanly: env override or disabled config."""
    if os.environ.get("SKOS_AUTOPILOT_OFF") == "1":
        return True
    return not enabled


def stable_qid(prompt: str, action_ref: str | None) -> str:
    """Deterministic 12-char decision id over (action_ref, prompt)."""
    return hashlib.sha256(f"{action_ref}|{prompt}".encode("utf-8")).hexdigest()[:12]


def load_raw_tasks(tasks_dir) -> list[dict]:
    """Load coord tasks as raw dicts so ``meta`` is visible (spec Phase 0)."""
    d = Path(tasks_dir)
    out: list[dict] = []
    if not d.exists():
        return out
    for p in sorted(d.glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    return out


def repo_tag(task: dict) -> str | None:
    """The single ``repo:<name>`` tag, or None when absent/ambiguous."""
    repos = [t.split(":", 1)[1] for t in (task.get("tags") or []) if t.startswith("repo:")]
    return repos[0] if len(repos) == 1 else None


def _quality_from_tags(task: dict) -> QualityMode | None:
    """A per-task `quality:<mode>` tag (mirrors the `repo:<name>` tag vocabulary),
    or None when absent. An unrecognized value fails closed (coerce -> gated)."""
    for t in (task.get("tags") or []):
        if t.startswith("quality:"):
            return coerce_quality(t.split(":", 1)[1])
    return None


def _repo_floor(task: dict, config) -> QualityMode | None:
    """The per-repo quality FLOOR (RepoSpec.min_quality, toggle spec G6), or None."""
    name = repo_tag(task)
    if not name or config is None:
        return None
    repo = getattr(config, "repo_map", {}).get(name)
    mq = getattr(repo, "min_quality", None)
    return None if mq is None else coerce_quality(mq)


def resolve_quality(task: dict, config=None) -> QualityMode:
    """Resolve the effective QualityMode for a task (toggle spec 2.2 chain):
    per-task `quality:` tag > interface default (`config.default_quality`) > gated.
    Then RAISE to the per-repo floor if one is set (a floor can only strengthen,
    never weaken; quality is never lowered implicitly)."""
    q = _quality_from_tags(task)
    if q is None:
        q = coerce_quality(getattr(config, "default_quality", None))
    floor = _repo_floor(task, config)
    if floor is not None and QUALITY_RANK[floor] > QUALITY_RANK[q]:
        q = floor
    return q


def classify_kind(task: dict, config=None) -> str:
    """Route to an executor kind by repo tag (+ quality) then source (skjoule vocab).

    A repo-tagged task routes to `engineering-direct` only when its resolved quality
    is DIRECT; every other quality (GATED, and fail-safe NONE) routes to the gated
    `engineering` crown-jewel path."""
    if any(t.startswith("repo:") for t in (task.get("tags") or [])):
        return ("engineering-direct"
                if resolve_quality(task, config) == QualityMode.DIRECT else "engineering")
    return {"itil": "ops", "email": "research", "telegram": "research",
            "order": "orders", "calendar": "calendar"}.get(task.get("source", "coord"),
                                                            "engineering")


def _to_workitem(task: dict, *, unblocked: bool = True, verdict: str = "valid",
                 config=None) -> WorkItem:
    """Build a WorkItem, enriching the payload with the phase-0 facts the executor's
    selectable/run contract reads (unblocked, verdict) and mapping coord's
    acceptance_criteria onto the `acceptance` key the executor expects.

    Normalization happens here exactly once (toggle spec 2.2): the resolved
    QualityMode (tag > default > gated, then raised to the repo floor) is written
    into `payload["quality"]`, and `classify_kind` reads the same chain to pick the
    executor kind. Downstream code reads only the normalized value."""
    payload = {**task, "unblocked": unblocked, "verdict": verdict,
               "acceptance": task.get("acceptance_criteria") or task.get("acceptance") or [],
               "quality": resolve_quality(task, config).value}
    return WorkItem(kind=classify_kind(task, config), ref=task["id"],
                    source=task.get("source", "coord"), repo=repo_tag(task), payload=payload)


def deepdive_spawn(board, proposals, *, caps: Caps, run_id: str,
                   dry_run: bool = False) -> list[str]:
    """Create new coord tasks from deep-dive proposals, capped and marked
    ``autopilot-untriaged`` so they are never auto-selected (spec section 14)."""
    made: list[str] = []
    for spec in (proposals or [])[: caps.new_tasks_per_run]:
        if dry_run:
            made.append("(dry-run)")
            continue
        made.append(board.create_task(title=spec.get("title", ""),
                                      description=spec.get("description", ""),
                                      tags=["autopilot", "autopilot-untriaged"]))
    return made


def phase0_assess(*, board, harness, tasks_dir, caps: Caps, run_id: str,
                  dry_run: bool = False, codebase_context: str = "",
                  deepdive_proposals=None, only: str | None = None,
                  only_ids=None, only_tag: str | None = None, config=None
                  ) -> tuple[list[WorkItem], list[DecisionItem]]:
    """Reclaim stale claims, compute unblocked, assess each candidate, apply the
    verdict (stale rewrite / obsolete close / needs_decision queue), spawn capped
    deep-dive tasks. Returns (candidates, decisions).

    Selection SCOPE (so a run assesses only the chosen work, not the whole board):
      ``only``     -- a single task id (targeted canary/--task run).
      ``only_ids`` -- an explicit BATCH of task ids (--tasks): the run assesses +
                      builds exactly these, in their given order.
      ``only_tag`` -- only unblocked tasks carrying this tag (--tag, e.g. a
                      per-node assignment tag or ``autopilot``).
    Any scope means "no new deep-dive work is spawned" -- a scoped run does the
    named work and nothing else. Unscoped -> the full board-wide triage.
    """
    if not dry_run:
        for agent in sorted({"autopilot", fleet_dispatch.claim_agent_name()}):
            board.release_stale_claims(agent, 3600)
    by_id = {t.get("id"): t for t in load_raw_tasks(tasks_dir)}
    candidates: list[WorkItem] = []
    decisions: list[DecisionItem] = []
    if only_ids:
        ids = list(only_ids)
    elif only_tag:
        ids = [tid for tid in sorted(board.unblocked_task_ids())
               if only_tag in ((by_id.get(tid) or {}).get("tags") or [])]
    elif only:
        ids = [only]
    else:
        ids = sorted(board.unblocked_task_ids())
    scoped = bool(only or only_ids or only_tag)
    for tid in ids:
        t = by_id.get(tid)
        if not t or t.get("status") in ("completed", "closed", "obsolete"):
            continue
        brief = AssessBrief(task_id=tid, title=t.get("title", ""),
                            description=t.get("description", ""),
                            acceptance=t.get("acceptance_criteria") or [],
                            tags=t.get("tags") or [], repo=repo_tag(t),
                            codebase_context=codebase_context)
        v = harness.assess(brief)
        if v.verdict == "valid":
            candidates.append(_to_workitem(t, verdict="valid", config=config))
        elif v.verdict == "stale":
            if not dry_run:
                board.update_task(tid, description=v.updated_description,
                                  acceptance_criteria=v.updated_acceptance, run_id=run_id)
            candidates.append(_to_workitem(t, verdict="stale", config=config))
        elif v.verdict == "obsolete":
            if not dry_run:
                board.close_task_obsolete(tid, v.reason, run_id=run_id)
        elif v.verdict == "needs_decision":
            decisions.append(DecisionItem(qid=stable_qid(v.reason or tid, tid),
                                          prompt=v.reason or f"Task {tid} needs a decision.",
                                          options={"promote": "promote", "skip": "skip"},
                                          action_ref=tid, priority=t.get("priority") or "high"))
    if not scoped:                      # a targeted/batch/tag run never spawns new work
        deepdive_spawn(board, deepdive_proposals, caps=caps, run_id=run_id, dry_run=dry_run)
    return candidates, decisions


def is_untriaged(item: WorkItem) -> bool:
    return "autopilot-untriaged" in (item.payload.get("tags") or [])


def phase1_triage(candidates, harness, *, repo_map, decisions,
                  executors=None) -> list[tuple[WorkItem, object]]:
    """Select unblocked+valid+in-scope items whose executor.selectable is True and
    route them. Non-selectable or decision-shaped items go straight to the decision
    queue (the executor's escalate is NOT called for selectable=False). untriaged
    items are never auto-selected."""
    selected: list[tuple[WorkItem, object]] = []
    repo_map = repo_map or {}
    table = executors if executors is not None else EXECUTORS
    for item in candidates:
        if is_untriaged(item):
            continue
        ex = table.get(item.kind)
        if ex is None:
            decisions.append(DecisionItem(qid=stable_qid(f"no-exec:{item.kind}", item.ref),
                prompt=f"No executor registered for kind '{item.kind}' (task {item.ref}).",
                options={"skip": "skip"}, action_ref=item.ref, priority="medium"))
            continue
        if item.kind == "engineering" and (item.repo is None or item.repo not in repo_map):
            decisions.append(DecisionItem(qid=stable_qid("which-repo", item.ref),
                prompt=f"Task {item.ref} has no known repo:<name>; add to repo_map or route?",
                options={"map": "add-to-repo_map", "skip": "skip"},
                action_ref=item.ref, priority="high"))
            continue
        if ex.selectable(item):
            selected.append((item, ex))
        else:
            decisions.append(DecisionItem(qid=stable_qid("not-selectable", item.ref),
                prompt=f"Task {item.ref} ({item.kind}) is not autonomously actionable; needs you.",
                options={"take": "take", "defer": "defer"},
                action_ref=item.ref, priority="medium"))
    return selected


def phase2_swarm(selected, *, harness, board, caps: Caps, ledger: CapLedger,
                 decisions, run_id: str, state=None, enabled: bool = True) -> dict:
    """Run each routed item's produce-then-grade loop, write each round's score to
    the coord record, finalize cleared items and escalate non-converging ones.

    Concurrency: up to ``caps.max_concurrent`` items run AT ONCE (a thread per
    item). Each item's build is fully isolated -- its own worktree, sandbox, and
    PR branch -- so the expensive parts (the sandbox rounds, and finalize's CI
    wait) run in parallel, unlocked. The only shared state is guarded briefly:
    the git repo + the "autopilot" agent file are serialized inside the executor
    (``_GIT_LOCK`` / ``_BOARD_LOCK``); the run's ledger/decisions/state/journal
    are serialized here by ``_lock``. The token/dollar ceiling is checked before
    each item starts. max_concurrent<=1 keeps the exact old sequential behaviour."""
    state = dict(state or {})
    _lock = threading.Lock()
    _budget_hit = [False]                           # append the budget decision ONCE
    # Resource-based autoscaler: scale the worker count to THIS host's capacity
    # (min | recommended | max | <int>), clamped to the hard cap. One config runs
    # correctly on a 4-core box and a big laptop -- each scales to itself.
    from .autoscale import describe, resolve
    hard_cap = int(getattr(caps, "max_concurrent", 3) or 3)
    workers = resolve(getattr(caps, "concurrency", "recommended"), hard_cap=hard_cap)
    if len(selected) > 1:
        health.record("swarm_concurrency", workers=workers,
                      mode=getattr(caps, "concurrency", "recommended"), items=len(selected))
        print(f"autopilot: {describe(getattr(caps, 'concurrency', 'recommended'), hard_cap)}")

    def _process(item, ex) -> None:
        if kill_switch_active(enabled):
            return
        with _lock:
            if ledger.exceeded():
                if not _budget_hit[0]:              # only the first over-budget item escalates
                    _budget_hit[0] = True
                    decisions.append(DecisionItem(qid=stable_qid("budget-hit", run_id),
                        prompt="Autopilot hit its budget ceiling (token/dollar limits); stopped early.",
                        options={"ok": "acknowledge"}, action_ref=run_id, priority="high"))
                return
        try:
            result = ex.run(item, harness)          # ISOLATED build -- unlocked
        except ClaimRaced as exc:
            with _lock:
                state[item.ref] = {"state": "claim-raced", "detail": str(exc)}
                journal.write_run(run_id, {"run_id": run_id, "phase": "swarm",
                                           "items": dict(state)})
            return
        with _lock:
            ledger.add(getattr(result, "tokens", 0), getattr(result, "cost_usd", 0.0))
            rnd = int((state.get(item.ref, {}).get("round", 0) or 0)) + 1
        if result.passed:
            try:
                ex.finalize(item, result)           # long CI wait -- unlocked; git/board self-guarded
                entry = {"state": "finalized", "round": rnd, "score": result.score}
            except Exception as exc:                # noqa: BLE001 - must never vanish
                # A gate-PASSED item whose finalize (CI re-check / PR open / merge)
                # raises must not silently disappear: _commit_and_push may already
                # have pushed autopilot/<ref>, so a swallowed error reads as "no
                # work done". Surface it as an operator decision instead.
                with _lock:
                    decisions.append(DecisionItem(
                        qid=stable_qid("finalize-failed", item.ref),
                        prompt=(f"Task {item.ref} PASSED the gate but finalize failed: "
                                f"{type(exc).__name__}: {exc}. Branch autopilot/{item.ref} "
                                f"may already be pushed; open/merge it manually or retry."),
                        options={"retry": "retry", "skip": "skip"},
                        action_ref=item.ref, priority="high"))
                entry = {"state": "finalize-failed", "round": rnd, "score": result.score}
        else:
            d = ex.escalate(item, result.notes)
            with _lock:
                decisions.append(d)
            entry = {"state": "escalated", "round": rnd, "score": result.score}
        with _lock:
            state[item.ref] = entry
            journal.write_run(run_id, {"run_id": run_id, "phase": "swarm", "items": dict(state)})

    if workers <= 1:
        for item, ex in selected:                   # exact old sequential path
            if kill_switch_active(enabled):
                break
            _process(item, ex)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_process, item, ex) for item, ex in selected]
            for f in futures:
                f.result()                          # surface any worker exception
    return state


def write_decision(d: DecisionItem) -> str | None:
    """Delegate to digest.queue_decision, the single decision-write path."""
    from . import digest as digest_mod
    return digest_mod.queue_decision(d.prompt, d.options, d.action_ref, d.priority, qid=d.qid)


def _decision_preview(d: DecisionItem) -> dict:
    return {"id": None, "source": "autopilot", "source_ref": f"autopilot:{d.qid}",
            "priority": d.priority, "created_at": "",
            "decision": {"qid": d.qid, "prompt": d.prompt, "options": d.options,
                         "answered": False}}


def phase3_report(decisions, *, dry_run: bool = False, digest_date: str | None = None) -> dict:
    """Build the numbered digest and (unless dry-run) write each decision to GTD and
    persist the manifest. sk-alert SEND is Phase F; this only builds."""
    from . import digest as digest_mod
    digest_date = digest_date or datetime.now(timezone.utc).date().isoformat()
    if dry_run:
        preview = digest_mod.build_manifest([_decision_preview(d) for d in decisions],
                                            digest_date=digest_date)
        return {"dry_run": True, "digest_preview": digest_mod.build_digest_text(preview),
                "decisions": len(decisions)}
    for d in decisions:
        write_decision(d)
    manifest = digest_mod.build_manifest(digest_date=digest_date)
    digest_mod.write_manifest(manifest)
    return {"dry_run": False, "manifest": manifest,
            "digest_text": digest_mod.build_digest_text(manifest)}


def _new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _default_tasks_dir() -> Path:
    home = Path(os.environ.get("SKCAPSTONE_HOME", str(Path.home() / ".skcapstone")))
    return home / "coordination" / "tasks"


def build_executors(config, board, run_id: str) -> dict:
    """Assemble this run's executor table: the stub registry plus a fresh
    EngineeringExecutor bound to run_id's journal handle. Per-run by design, so a
    second run in one process never reuses a stale RunHandle. Does not mutate the
    global EXECUTORS."""
    from . import stubs as _stubs        # noqa: F401  import self-registers the stubs
    from . import digest as digest_mod
    from .direct import DirectExecutor
    from .engineering import EngineeringExecutor
    table = dict(EXECUTORS)
    handle = journal.handle(run_id)
    table["engineering"] = EngineeringExecutor(config, board, handle, digest_mod)
    # the simple/unattended toggle: same helpers, one run, no gate, never merges.
    table["engineering-direct"] = DirectExecutor(config, board, handle, digest_mod)
    return table


def run_once(*, board, harness, config, tasks_dir=None, run_id=None, dry_run=None,
             ledger: CapLedger | None = None, deepdive_proposals=None,
             executors=None, task: str | None = None,
             tasks=None, tag: str | None = None, placer=None) -> dict:
    """Execute one daily cycle: assess -> triage -> swarm -> report. Journals
    per-item state so a re-run resumes (see the resume task). Guardrails (kill
    switch, caps, dry-run) are layered in the following tasks."""
    run_id = run_id or _new_run_id()
    dry = config.dry_run if dry_run is None else dry_run
    caps = config.caps
    ledger = ledger or CapLedger(caps)

    prior = journal.read_run(run_id) or {}
    state = dict(prior.get("items") or {})
    done = {ref for ref, st in state.items()
            if st.get("state") in ("finalized", "escalated")}

    def _checkpoint(phase: str):
        journal.write_run(run_id, {"run_id": run_id, "phase": phase,
                                   "stopped": "kill_switch", "items": state})
        return {"run_id": run_id, "dry_run": dry, "stopped": "kill_switch"}

    if kill_switch_active(config.enabled):
        return _checkpoint("assess")

    if not dry:
        # Advisory self-check: surface the failure modes that silently stalled
        # live runs (un-shimmed node, stale proxy image, expired token) BEFORE a
        # coding round is wasted. Never blocks -- a doctor bug must not wedge a run.
        from . import doctor
        for c in doctor.preflight():
            if c.status != "ok":
                print(f"autopilot preflight [{c.status}] {c.name}: {c.detail}"
                      + (f" -- fix: {c.fix}" if c.fix else ""))

    candidates, decisions = phase0_assess(
        board=board, harness=harness, tasks_dir=tasks_dir or _default_tasks_dir(),
        caps=caps, run_id=run_id, dry_run=dry, deepdive_proposals=deepdive_proposals,
        only=task, only_ids=tasks, only_tag=tag, config=config)

    if kill_switch_active(config.enabled):
        return _checkpoint("triage")

    executors = executors if executors is not None else build_executors(config, board, run_id)
    selected = phase1_triage(candidates, harness, repo_map=config.repo_map,
                             decisions=decisions, executors=executors)
    selected = [(it, ex) for it, ex in selected if it.ref not in done]  # resume: skip settled
    if task is not None:
        selected = [(it, ex) for it, ex in selected if it.ref == task]
    elif tasks:
        _batch = set(tasks)
        selected = [(it, ex) for it, ex in selected if it.ref in _batch]
    elif tag:
        selected = [(it, ex) for it, ex in selected
                    if tag in (it.payload.get("tags") or [])]

    off_node: list[tuple] = []
    if not dry:
        if kill_switch_active(config.enabled):
            return _checkpoint("swarm")
        if placer is None and getattr(config, "fleet_dispatch", True):
            placer = fleet_dispatch.default_placer()
        selected, off_node = fleet_dispatch.partition_local(
            selected, placer=placer, self_node=fleet_dispatch.self_node())
        for item, decision in off_node:
            state[item.ref] = {"state": "off-node", "node": decision.node,
                               "reason": decision.reason}
        state = phase2_swarm(selected, harness=harness, board=board, caps=caps,
                             ledger=ledger, decisions=decisions, run_id=run_id,
                             state=state, enabled=config.enabled)

    report = phase3_report(decisions, dry_run=dry)
    journal.write_run(run_id, {"run_id": run_id, "phase": "report", "items": state,
                               "decisions": len(decisions), "dry_run": dry,
                               "preview": report.get("digest_preview") if dry else None})

    # Spin-down: reclaim THIS run's transient build artifacts (worktrees + exited
    # sandbox containers/networks) so disk/RAM don't creep across runs. Only on a
    # LIVE run; the mode (cold|teardown|off) comes from config. Best-effort.
    cleanup_out = None
    if not dry:
        try:
            mode = getattr(config, "cleanup_after_run", "cold")
            if mode and mode != "off":
                from . import cleanup
                repo_paths = [r.path for r in getattr(config, "repo_map", {}).values()
                              if getattr(r, "path", None)]
                cleanup_out = cleanup.reclaim(mode, repo_paths=repo_paths,
                                              refs=[it.ref for it, _ in selected])
                health.record("cleanup", **{k: v for k, v in cleanup_out.items()
                                            if k != "error"})
                print(f"autopilot: cleanup {cleanup_out}")
        except Exception as exc:                       # cleanup must never fail a run
            health.record("cleanup_error", error=str(exc)[:120])
    return {"run_id": run_id, "dry_run": dry,
            "selected": [it.ref for it, _ in selected],
            "off_node": [{"ref": it.ref, "node": d.node, "reason": d.reason}
                         for it, d in off_node],
            "decisions": len(decisions), "report": report, "cleanup": cleanup_out}


def run_cli(*, dry_run: bool = True, canary: bool = False, task=None,
           harness: str = "stub", tasks=None, tag: str | None = None) -> dict:
    """CLI bridge for `skos autopilot run`. Dry-run (default) runs against the
    StubHarness. Canary/live build the real sandboxed harness, but only when
    harness.live_execution is enabled in config (else a clear disabled message).

    Selection scope: ``task`` = one card (canary); ``tasks`` = an explicit BATCH
    of card ids; ``tag`` = only cards carrying that tag. A scoped run assesses +
    builds exactly the named work (nothing board-wide) on the autoscaled pool.
    """
    config = Config.load()
    from skcapstone.coordination import Board
    from skcapstone.mcp_tools._helpers import _shared_root
    board = Board(_shared_root())
    if dry_run and not canary:
        from .harness import StubHarness
        return run_once(board=board, harness=StubHarness(), config=config, dry_run=True,
                        task=task, tasks=tasks, tag=tag)
    if not getattr(config, "live_execution", False):
        return {"disabled": "live/canary requires harness.live_execution=true in "
                            "autopilot.yaml; enable only after the v1.5 confinement "
                            "proof passes."}
    if canary and not task:
        return {"error": "a canary requires --task <id> (it targets one task)."}
    name = None if harness in ("stub", "", None) else harness
    h = build_harness(config, name)
    return run_once(board=board, harness=h, config=config, dry_run=False,
                    task=task, tasks=tasks, tag=tag)
