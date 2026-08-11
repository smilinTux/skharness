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
from types import SimpleNamespace

from .types import (WorkItem, AssessBrief, DecisionItem, QualityMode, Verdict,
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


def _norm_title(title: str) -> str:
    """Normalize a card title for duplicate detection: lowercased, whitespace
    collapsed. Two titles that differ only in case or spacing dedup to one."""
    return " ".join((title or "").split()).lower()


def _iter_tasks(existing_tasks):
    """Yield task dicts from a by-id map, a plain list, or None (empty)."""
    if not existing_tasks:
        return
    values = existing_tasks.values() if isinstance(existing_tasks, dict) else existing_tasks
    for t in values:
        if isinstance(t, dict):
            yield t


def _card_parent(task: dict) -> str | None:
    """Parent epic id a card is linked to, via a ``parent:<id>`` tag or
    meta.autopilot.parent. None for a top-level / hand-carded card."""
    for tag in (task.get("tags") or []):
        if isinstance(tag, str) and tag.startswith("parent:"):
            return tag.split(":", 1)[1]
    return ((task.get("meta") or {}).get("autopilot") or {}).get("parent")


def _existing_children(existing_tasks, parent_id: str) -> list[dict]:
    """Cards already linked to ``parent_id`` (parent:<id> tag / meta.autopilot.parent).

    An epic that already has children is never re-decomposed: create-or-skip at the
    epic level. Catches epics a human hand-carded children for (no meta.decomposed
    flag) and epics whose prior decompose run crashed before mark_decomposed.
    """
    return [t for t in _iter_tasks(existing_tasks) if _card_parent(t) == parent_id]


def _ground_card(task: dict, config):
    """Host-side repo grounding for a card: fills codebase_context with facts and a
    concreteness score. Model-free, read-only. Ungrounded (grounded=False) when the
    card has no repo tag or the tree is dirty/unexpected -> caller keeps text-only
    assess. Never raises: any grounding error degrades to ungrounded."""
    from .grounding import Grounding, ground_card, repo_profile
    name = repo_tag(task)
    spec = config.repo(name) if (name and hasattr(config, "repo")) else None
    if not spec:
        return Grounding(grounded=False)
    brief = SimpleNamespace(title=task.get("title", ""),
                            description=task.get("description", ""),
                            acceptance=task.get("acceptance_criteria") or [])
    try:
        gr = ground_card(brief, getattr(spec, "path", None),
                         base_branch=getattr(spec, "base_branch", None))
        # Layer 2 (prevent-at-source): prepend the repo language profile so both
        # assess and decompose conform to the repo's architecture and never emit
        # foreign-language subtasks (the Go-in-Python failure).
        prof = repo_profile(getattr(spec, "path", None))
        if prof and gr.context:
            gr.context = (f"REPO PROFILE: language={prof['language']} "
                          f"(write {prof['language']} only, name real "
                          f"{prof['ext']} files/symbols).\n" + gr.context)
        return gr
    except Exception:
        return Grounding(grounded=False)


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
        # Board.create_task takes a Task object (not kwargs) and returns a Path;
        # build the Task, create it, and record its id. (Previously called with
        # kwargs, which only "worked" against a mock and raised on a real Board.)
        made.append(_create_child(board, title=spec.get("title", ""),
                                  description=spec.get("description", ""),
                                  tags=["autopilot", "autopilot-untriaged"]))
    return made


def _create_child(board, *, title: str, description: str, tags: list[str],
                  acceptance_criteria: list[str] | None = None,
                  dependencies: list[str] | None = None, meta: dict | None = None,
                  task_id: str | None = None) -> str:
    """Create a coord child task via the real Board.create_task(Task) contract and
    return its id. skcapstone is an optional sibling, so Task is imported lazily
    (mirrors the other lazy skcapstone imports in this module)."""
    from skcapstone.coordination import Task

    kw = dict(title=title, description=description, tags=tags,
              acceptance_criteria=acceptance_criteria or [],
              dependencies=dependencies or [], created_by="autopilot")
    if meta is not None:
        kw["meta"] = meta
    if task_id is not None:
        kw["id"] = task_id
    task = Task(**kw)
    board.create_task(task)
    return task.id


def _decompose_card(board, harness, task: dict, brief: AssessBrief, *, caps: Caps,
                    run_id: str, decisions: list, config=None,
                    existing_tasks=None, dry_run: bool = False,
                    run_budget: list | None = None) -> None:
    """Split a too-coarse parent into buildable child subtasks, park the parent.
    Guardrails: idempotent (skip if already decomposed OR the epic already has
    children on the board), create-or-skip per child (never re-create a
    same-title child), depth-capped (ceiling -> needs_decision, no infinite
    trees), child-count-capped, and empty/over-cap -> needs_decision (never a
    silent drop). Children are born ``autopilot-untriaged`` so a human releases
    them before they build (unless decompose_autobuild).

    ``existing_tasks`` is the run's view of the board (by-id map or list) used for
    the create-or-skip guards; when omitted the guards see nothing (legacy behavior).
    """
    tid = task.get("id")
    ap = (task.get("meta") or {}).get("autopilot") or {}
    if ap.get("decomposed"):                       # idempotency: already split (parked)
        return
    # Create-or-skip at the EPIC level: if the board already carries children for
    # this epic (parent:<id> tag / meta.autopilot.parent) -- hand-carded by a human,
    # or written by a prior run that crashed before mark_decomposed -- re-splitting
    # would duplicate them (this is the 2026-08-03 mass-pass failure). No-op.
    if _existing_children(existing_tasks, tid):
        return
    # REPO INVARIANT (fixes the 164 no-repo children): an epic with no single
    # repo:<name> tag cannot be routed to a codebase, so its children would be
    # unbuildable orphans. Route the epic to a human to assign a repo instead of
    # emitting no-repo children. Runs BEFORE decompose() so we never even split it.
    if not repo_tag(task):
        decisions.append(DecisionItem(
            qid=stable_qid(f"decompose-norepo {tid}", tid),
            prompt=f"Epic {tid} has no single repo:<name> tag; assign one before it "
                   "can be split into buildable children.",
            options={"map": "add-repo-tag", "skip": "skip"},
            action_ref=tid, priority=task.get("priority") or "high"))
        return
    depth = int(ap.get("decomp_depth", 0) or 0)
    if depth >= getattr(caps, "max_decompose_depth", 2):
        decisions.append(DecisionItem(
            qid=stable_qid(f"decompose-depth {tid}", tid),
            prompt=f"Task {tid} is still too coarse at max decompose depth "
                   f"({depth}); a human should scope it.",
            options={"promote": "promote", "skip": "skip"},
            action_ref=tid, priority=task.get("priority") or "high"))
        return
    specs = harness.decompose(brief)
    max_children = getattr(caps, "max_subtasks_per_card", 8)
    if not specs or len(specs) > max_children:
        # empty (inconclusive) OR wants more than the cap (it's an epic): a human
        # scopes it -- never silently drop the parent, never over-split.
        decisions.append(DecisionItem(
            qid=stable_qid(f"decompose-failed {tid}", tid),
            prompt=(f"Task {tid} was flagged too coarse but decompose returned "
                    f"{len(specs)} subtasks (need 1..{max_children}); scope it by hand."),
            options={"promote": "promote", "skip": "skip"},
            action_ref=tid, priority=task.get("priority") or "high"))
        return
    # COHERENCE GATE (layer 1): if any child is written for the wrong
    # language/paradigm (e.g. Go structs/`.go` files in a Python repo), the model
    # misread the repo, so the WHOLE split is untrustworthy. Do NOT create garbage
    # children -- route the parent to a human, reusing the fail-safe pattern.
    from .grounding import child_incoherence, repo_profile
    spec = config.repo(repo_tag(task)) if (config and repo_tag(task)
                                           and hasattr(config, "repo")) else None
    profile = repo_profile(getattr(spec, "path", None)) if spec else {}
    incoherent = next((child_incoherence(s, profile) for s in specs
                       if child_incoherence(s, profile)), None) if profile else None
    if incoherent:
        decisions.append(DecisionItem(
            qid=stable_qid(f"decompose-incoherent {tid}", tid),
            prompt=(f"Task {tid} decompose produced a subtask incoherent with the "
                    f"{profile.get('language')} repo ({incoherent}); the split is "
                    "unreliable, scope it by hand."),
            options={"promote": "promote", "skip": "skip"},
            action_ref=tid, priority=task.get("priority") or "high"))
        return
    if dry_run:
        return
    # PER-RUN BUDGET (anti-flood, defense in depth behind the scope gate): even a
    # scoped --tag run that matches many epics cannot carpet-split the board. If
    # this epic's full child set does not fit the remaining run budget, defer the
    # WHOLE epic to a decision rather than partially splitting it (a partial split
    # would park the parent as if done and strand the rest).
    n_children = len(specs[:max_children])
    if run_budget is not None and n_children > run_budget[0]:
        decisions.append(DecisionItem(
            qid=stable_qid(f"decompose-budget {tid}", tid),
            prompt=f"Epic {tid} needs {n_children} children but the per-run decompose "
                   f"budget ({run_budget[0]} left) is exhausted; re-run scoped to it "
                   "next pass.",
            options={"skip": "skip"}, action_ref=tid,
            priority=task.get("priority") or "high"))
        return
    parent_tags = [t for t in (task.get("tags") or [])
                   if t.startswith(("repo:", "quality:"))]
    autobuild = bool(getattr(_decompose_card, "_autobuild", False))
    # Create-or-skip at the CHILD level: never create a child whose normalized
    # title already exists as a child of this epic OR as an unlinked hand-carded
    # card (the exact-title duplicates the mass pass produced). Seed the seen set
    # from those cards, then dedup within this batch too.
    seen_titles = {
        _norm_title(t.get("title", ""))
        for t in _iter_tasks(existing_tasks)
        if t.get("id") != tid and _card_parent(t) in (tid, None)
    }
    child_ids: list[str] = []
    for i, s in enumerate(specs[:max_children]):
        norm = _norm_title(s.get("title", ""))
        if norm in seen_titles:                    # duplicate (parent, title) -> skip
            continue
        seen_titles.add(norm)
        tags = list(parent_tags) + ["autopilot", f"parent:{tid}"]
        if not autobuild:
            # Born STAGED into the "Proposed" lane: autopilot-staged hides the child
            # from OPEN/unblocked entirely (never selected or built), autopilot-untriaged
            # is the legacy build gate. A human runs `skos autopilot release <epic>` to
            # promote the whole set. This is what keeps a scoped decomposition from
            # dumping unreviewed children onto the active buildable backlog.
            tags += ["autopilot-untriaged", "autopilot-staged"]
        child_id = stable_qid(f"{tid}:{s.get('title', i)}", tid)[:8]
        _create_child(board, title=s.get("title", ""), description=s.get("description", ""),
                      tags=tags, acceptance_criteria=s.get("acceptance") or [],
                      meta={"autopilot": {"parent": tid, "decomp_depth": depth + 1,
                                          "staged": not autobuild}},
                      task_id=child_id)
        child_ids.append(child_id)
    if run_budget is not None:                     # consume the per-run child budget
        run_budget[0] -= len(child_ids)
    if child_ids:                                  # only park the epic if we created work
        board.mark_decomposed(tid, child_ids, run_id=run_id)


def release_epic(epic_id: str, *, board=None, tasks=None,
                 run_id: str | None = None) -> list[str]:
    """Promote an epic's STAGED children into the active buildable backlog.

    Strips ``autopilot-staged`` (which hides a card in the Proposed lane and out of
    unblocked/selection) and ``autopilot-untriaged`` (the build gate) from every card
    tagged ``parent:<epic_id>``. This is the human "pick-up" action after reviewing a
    scoped decomposition. Idempotent: cards without the staged tag are skipped.
    Returns the released child ids.
    """
    if board is None:
        from skcapstone.coordination import Board
        from skcapstone.mcp_tools._helpers import _shared_root
        board = Board(_shared_root())
    if tasks is None:
        tasks = load_raw_tasks(_default_tasks_dir())
    run_id = run_id or _new_run_id()
    released: list[str] = []
    for t in _iter_tasks(tasks):
        if _card_parent(t) != epic_id:
            continue
        if "autopilot-staged" not in (t.get("tags") or []):
            continue
        board.update_task(t.get("id"),
                          remove_tags=["autopilot-staged", "autopilot-untriaged"],
                          run_id=run_id)
        released.append(t.get("id"))
    return released


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
    # Per-run decompose budget (mutable holder, threaded into _decompose_card so the
    # cap spans ALL epics in this run, not just one). Anti-flood defense in depth.
    dc_budget = [getattr(caps, "max_decompose_children_per_run", 24)]
    for tid in ids:
        t = by_id.get(tid)
        if not t or t.get("status") in ("completed", "closed", "obsolete"):
            continue
        # An obsolete card is marked on the task file at meta.autopilot.obsolete
        # (close_task_obsolete writes there, since task files carry no status
        # field). Without this check the marker is cosmetic: the card returns
        # through unblocked_task_ids and is re-assessed every run, so neither the
        # engine's own obsolete closures nor a manual stale-sweep ever stick.
        ap_meta = (t.get("meta") or {}).get("autopilot") or {}
        if ap_meta.get("obsolete"):
            continue
        # A decomposed parent is parked (mark_decomposed); its children carry the
        # work. Skip it symmetric with the obsolete marker so it is not re-split.
        if ap_meta.get("decomposed"):
            continue
        gr = _ground_card(t, config)
        brief = AssessBrief(task_id=tid, title=t.get("title", ""),
                            description=t.get("description", ""),
                            acceptance=t.get("acceptance_criteria") or [],
                            tags=t.get("tags") or [], repo=repo_tag(t),
                            codebase_context=gr.context or codebase_context)
        v = harness.assess(brief)
        # Concreteness gate: a repo-tagged card that assess called `valid` but whose
        # acceptance resolves NOTHING in the repo facts (and is not greenfield) is,
        # by construction, too vague to build in one diff -> downgrade to decompose
        # instead of sending it to a doomed build. Ungrounded/inconclusive cards are
        # untouched (they still fail open to the twin gate).
        if (v.verdict == "valid" and gr.grounded and gr.concreteness is not None
                and gr.concreteness < getattr(caps, "concreteness_floor", 0.34)
                and not gr.net_new):
            v = Verdict(verdict="decompose",
                        reason=f"repo card resolves no named artifact "
                               f"(concreteness={gr.concreteness:.2f}); split into "
                               f"buildable subtasks",
                        concreteness=gr.concreteness)
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
        elif v.verdict == "decompose":
            # SCOPE GATE (fixes the board-wide sweep that flooded ~821 cards): an
            # UNSCOPED (daily/board-wide) run never carpet-splits. A too-coarse epic
            # becomes a "scope it" decision; it is only split when an operator
            # explicitly scopes the run to it (--task/--tasks/--tag). This ties
            # decomposition to an epic actually being picked up.
            if not scoped:
                decisions.append(DecisionItem(
                    qid=stable_qid(f"decompose-scope {tid}", tid),
                    prompt=f"Epic {tid} is too coarse to build; split it with a scoped "
                           f"run (`skos autopilot triage --tasks {tid}`).",
                    options={"skip": "skip"}, action_ref=tid,
                    priority=t.get("priority") or "high"))
            else:
                _decompose_card(board, harness, t, brief, caps=caps, run_id=run_id,
                                decisions=decisions, config=config, existing_tasks=by_id,
                                dry_run=dry_run, run_budget=dc_budget)
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
                 decisions, run_id: str, state=None, enabled: bool = True,
                 workers: int | None = None, session_registry=None) -> dict:
    """Run each routed item's produce-then-grade loop, write each round's score to
    the coord record, finalize cleared items and escalate non-converging ones.

    Concurrency: up to ``caps.max_concurrent`` items run AT ONCE (a thread per
    item). Each item's build is fully isolated -- its own worktree, sandbox, and
    PR branch -- so the expensive parts (the sandbox rounds, and finalize's CI
    wait) run in parallel, unlocked. The only shared state is guarded briefly:
    the git repo + the "autopilot" agent file are serialized inside the executor
    (``_GIT_LOCK`` / ``_BOARD_LOCK``); the run's ledger/decisions/state/journal
    are serialized here by ``_lock``. The token/dollar ceiling is checked before
    each item starts. max_concurrent<=1 keeps the exact old sequential behaviour.

    ``session_registry`` (an ``AutocodeSessionRegistry``, spec 5.1 / card C-1
    AC3): when given, each item's build registers itself as a skcode session
    (source=autocode) around ``ex.run``, updates on finalize/escalate/error,
    and ends when the item's processing is done, so hostd's GET /sessions
    shows autocode runs on the same rail as interactive ones, "for free".
    Defaults None -- no registration, the exact old behaviour -- so every
    existing caller (and every test that does not pass one) is unaffected."""
    state = dict(state or {})
    _lock = threading.Lock()
    _budget_hit = [False]                           # append the budget decision ONCE
    # Resource-based autoscaler: scale the worker count to THIS host's capacity
    # (min | recommended | max | <int>), clamped to the hard cap. One config runs
    # correctly on a 4-core box and a big laptop -- each scales to itself.
    from .autoscale import describe, resolve
    hard_cap = int(getattr(caps, "max_concurrent", 3) or 3)
    # ``workers`` is an explicit override: it pins the pool size EXACTLY (clamped
    # only to [1, hard_cap]) and bypasses host resource probing. Production leaves
    # it None so the autoscaler scales to the actual box; tests and operators can
    # pin a deterministic value independent of the host's core/RAM/disk capacity.
    if workers is None:
        workers = resolve(getattr(caps, "concurrency", "recommended"), hard_cap=hard_cap)
    else:
        workers = max(1, min(int(workers), hard_cap))
    if len(selected) > 1:
        health.record("swarm_concurrency", workers=workers,
                      mode=getattr(caps, "concurrency", "recommended"), items=len(selected))
        print(f"autopilot: {describe(getattr(caps, 'concurrency', 'recommended'), hard_cap)}")

    def _register_session(sid: str, item) -> None:
        # Best-effort by construction: a session-registry write failure must
        # never break a build (same posture as the cleanup/audit writes
        # elsewhere in this module), so it is recorded and swallowed, not
        # raised.
        if session_registry is None:
            return
        try:
            session_registry.register(sid=sid, repo=item.repo or "",
                                      last_message=f"{item.kind}:{item.ref}")
        except Exception as exc:                    # noqa: BLE001 - never break a build
            health.record("session_registry_error", sid=sid, phase="register",
                          error=str(exc)[:120])

    def _end_session(sid: str, last_message: str) -> None:
        if session_registry is None:
            return
        try:
            session_registry.end(sid, last_message=last_message)
        except Exception as exc:                    # noqa: BLE001 - never break a build
            health.record("session_registry_error", sid=sid, phase="end",
                          error=str(exc)[:120])

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
        # Card C-1 AC3: register this item's build as a skcode session
        # (source=autocode) so it appears on hostd's GET /sessions rail for
        # the duration of the build, same identity every round of this item
        # reuses (one sid per (run_id, item.ref), not per round).
        sid = f"autocode-{run_id}-{item.ref}"
        _register_session(sid, item)
        try:
            result = ex.run(item, harness)          # ISOLATED build -- unlocked
        except ClaimRaced as exc:
            with _lock:
                state[item.ref] = {"state": "claim-raced", "detail": str(exc)}
                journal.write_run(run_id, {"run_id": run_id, "phase": "swarm",
                                           "items": dict(state)})
            _end_session(sid, "claim-raced")
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
        _end_session(sid, entry["state"])

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
             tasks=None, tag: str | None = None, placer=None,
             triage_only: bool = False, session_registry=None) -> dict:
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

    if triage_only:
        # Board-hygiene sweep: phase0 already refined/closed/decomposed cards and
        # queued human decisions. Stop BEFORE selecting anything to build, so a
        # scheduled `skos autopilot triage` cleans the board ahead of the build run
        # without ever spending a sandbox. Reports what it did.
        report = phase3_report(decisions, dry_run=dry)
        journal.write_run(run_id, {"run_id": run_id, "phase": "triage-only",
                                   "candidates": len(candidates),
                                   "decisions": len(decisions), "dry_run": dry})
        return {"run_id": run_id, "dry_run": dry, "triage_only": True,
                "candidates": len(candidates), "decisions": len(decisions),
                "report": report}

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
                             state=state, enabled=config.enabled,
                             session_registry=session_registry)

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
           harness: str = "stub", tasks=None, tag: str | None = None,
           triage_only: bool = False) -> dict:
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
    # A triage-only sweep needs the REAL harness (it assesses/decomposes with the
    # model) but never builds -- run_once short-circuits before phase2. It still
    # requires live_execution because decompose() calls the sandboxed model.
    if triage_only:
        if not getattr(config, "live_execution", False):
            return {"disabled": "triage requires harness.live_execution=true "
                                "(it assesses/decomposes with the model)."}
        name = None if harness in ("stub", "", None) else harness
        return run_once(board=board, harness=build_harness(config, name), config=config,
                        dry_run=dry_run, task=task, tasks=tasks, tag=tag, triage_only=True)
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
    # Card C-1 AC3: a real (live/canary) build registers its items as skcode
    # sessions (source=autocode) so they show up on hostd's GET /sessions rail
    # while they run. Wired only here at the CLI-level outer edge, the same
    # place every other real dependency (board, harness) is constructed;
    # run_once/phase2_swarm default session_registry=None so every direct
    # caller (including the whole test suite) is unaffected.
    from .sessions import AutocodeSessionRegistry
    return run_once(board=board, harness=h, config=config, dry_run=False,
                    task=task, tasks=tasks, tag=tag,
                    session_registry=AutocodeSessionRegistry())
