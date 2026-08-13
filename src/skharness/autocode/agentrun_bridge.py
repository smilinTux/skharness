"""skcapstone agent_run execute bridge (P1, design doc
docs/specs/2026-08-13-skharness-execute-bridge-arch.md).

Wires the R1 seam (``skcapstone.agent_run.set_execute_dispatcher``) into the
sandboxed and graded ``skharness.autocode`` engine, without disturbing the
(disabled) autopilot engine, skcode's ratify endpoint, or any existing
caller.

Composition, not modification: one WorkItem, one isolated worktree, one
sandboxed harness round (``AgentRunDirectExecutor.run``, a narrow subclass of
``DirectExecutor``), an independent twin-gate grade over the resulting diff
(``ratify``, side-effect-free), then commit + push + a DRAFT PR
(``AgentRunDirectExecutor.finalize``, inherited from ``DirectExecutor``
verbatim). Structurally incapable of merging: ``DirectExecutor._merge`` raises
unconditionally and ``DirectExecutor.finalize`` has no merge path.

Isolation (design doc section 5): this module never calls ``register()`` (the
bridge executor is never added to the shared ``EXECUTORS`` registry; it is
constructed directly, per dispatch), always constructs with ``board=None``
(claim is overridden to journal-only, so any accidental board access is a
loud ``AttributeError`` instead of a silent write), namespaces the run
journal and the WorkItem ref under ``airun-<card_id>``, and the git branch
under ``autopilot/airun-<card_id>`` (the ``autopilot/`` prefix is
``DirectExecutor.finalize``'s own convention; the ``airun-`` ref segment is
what keeps it out of a real autopilot build's namespace).
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from typing import Callable

from . import autopilot_cost, joules
from .config import Config
from .direct import DirectExecutor
from .harness import build_harness
from .journal import handle as journal_handle
from .ratify import ratify
from .types import GateResult, QualityMode, RepoSpec, WorkItem, coerce_quality


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AgentRunDirectExecutor(DirectExecutor):
    """Bridge-local subclass of ``DirectExecutor``, never registered in
    ``EXECUTORS`` (constructed directly by :func:`execute_dispatch`, one
    instance per dispatch).

    Exactly three overrides (design doc section 1), all subclass-local, all
    *narrowing* behavior. Everything else -- worktree creation with
    self-healing, ``_stage_work``, ``_diff``, ``_commit_and_push``,
    ``_pr_base``, ``prune_worktree``, the direct-mode
    ``GateResult(mode="direct")``, and the structural merge refusal
    (``_merge`` raises unconditionally) -- is inherited verbatim from
    direct.py / engineering.py.
    """

    kind = "agentrun-direct"

    def claim(self, item: WorkItem) -> None:
        """agent_run already claimed the card under its own lease
        (``agent_run.claim_run``). There is no coord task file for a shadow
        card (a GTD/ITIL-derived card has no ``tasks/<id>-*.json``), so
        ``Board.claim_task`` would raise ``ValueError`` -> ``ClaimRaced``.
        Record the claim in the run journal only; ``board=None`` makes any
        accidental board call a loud ``AttributeError`` instead of a silent
        write."""
        self.journal.record_claim(item.ref, claimed_at=_now_iso())

    def _settle_economics(self, item: WorkItem, sha: str) -> None:
        """P1: no joule mint from ad-hoc agent-run builds."""
        return None

    def _open_pr(self, repo: RepoSpec, pr_branch: str, item: WorkItem) -> str:
        """Identical to ``EngineeringExecutor._open_pr`` but opens the PR as
        a DRAFT and records the url on ``self`` so the dispatcher can return
        it in ``links["pr"]``."""
        base = self._pr_base(repo)
        proc = subprocess.run(
            ["gh", "pr", "create", "--head", pr_branch, "--base", base, "--draft",
             "--title", f"autopilot: {item.payload.get('title', item.ref)}",
             "--body", f"Autopilot task {item.ref}"],
            cwd=repo.path, capture_output=True, text=True)
        if proc.returncode != 0:
            # never eat a PR-open failure silently: the branch is already
            # pushed, so a swallowed error looks like "no work done".
            print(f"agentrun-bridge: gh pr create failed for {pr_branch} "
                  f"(base={base}): {proc.stderr.strip()}")
        self.pr_url = proc.stdout.strip()
        return self.pr_url


class _ActivityDigestShim:
    """Captures ``digest.queue_decision`` text into the dispatcher's returned
    activity instead of writing a GTD item (design doc section 5 isolation:
    "no autopilot-branded GTD item, so the autopilot decision resolver never
    sees bridge work"). The kanban card in review IS the review surface for
    bridge work."""

    def __init__(self, activity: list[dict]) -> None:
        self._activity = activity

    def queue_decision(self, *, prompt: str, options: dict,
                        action_ref: str | None = None, priority: str = "normal") -> None:
        self._activity.append({"atype": "action", "text": prompt})


def _refuse(reason: str, activity: list[dict] | None = None,
            links: dict | None = None) -> dict:
    """A well-formed fail-closed refusal dict (design doc section 1's error
    contract): ``process_one`` records it and moves the card to review with
    the reason visible. Never a partial success."""
    act = list(activity) if activity else []
    act.append({"atype": "error", "text": reason})
    return {"summary": f"execute refused (bridge): {reason}",
            "activity": act, "links": dict(links) if links else {}}


def _resolve_repo_label(card, cfg: Config) -> tuple[RepoSpec | None, str]:
    """Exactly one ``repo:<name>`` label on the card, resolved against
    ``cfg.repo_map`` (design doc section 2). No inference: the bridge never
    parses the instruction text for a repo name and never defaults to "the
    obvious repo"."""
    names = [lbl.split(":", 1)[1] for lbl in (getattr(card, "labels", None) or [])
              if lbl.startswith("repo:")]
    if not names:
        return None, "no target repo on the card; add a repo:<name> label"
    if len(names) > 1:
        return None, "ambiguous target (" + ", ".join(f"repo:{n}" for n in names) + ")"
    repo = cfg.repo_map.get(names[0])
    if repo is None:
        return None, f"repo:{names[0]} is not in autopilot.yaml repo_map"
    return repo, ""


def build_execute_dispatcher() -> Callable[[dict], dict] | None:
    """Check the static prerequisites (design doc section 3, the static
    subset of rows 4-8: a non-empty ``repo_map`` and a resolvable harness)
    and return the dispatcher closure, or ``None`` (fail-closed) when either
    is missing. Returning ``None`` is a first-class outcome, not an error --
    the R1 seam simply stays unwired."""
    cfg = Config.load()
    if not cfg.repo_map:
        return None
    try:
        build_harness(cfg)
    except Exception:
        return None
    return execute_dispatch


def execute_dispatch(context: dict) -> dict:
    """``fn(context) -> {summary, activity, links}`` (the R1 dispatcher
    contract). Design doc section 1's exact call sequence: fold the card,
    resolve exactly one ``repo:<name>`` label, load a fresh ``Config``,
    apply the per-run policy checks, resolve the harness, run ONE sandboxed
    round (``AgentRunDirectExecutor.run``), grade it independently
    (``ratify``, side-effect-free), and -- only if the sandboxed run itself
    passed -- commit + push + open a DRAFT PR (``finalize``, fed the
    ``mode="direct"`` result, never the gated ``ratify`` result).

    Every failure path returns a well-formed refusal dict (never an
    exception, never a partial side effect reported as success)."""
    activity: list[dict] = []
    card_id = context.get("card_id")
    try:
        from skcapstone.mcp_tools._helpers import _shared_root
        from skcoord.card_store import CardStore

        home = _shared_root()
        card = CardStore(home).fold(card_id)
        if card is None:
            return _refuse(f"card {card_id!r} not found", activity)

        cfg = Config.load()                        # fresh instance, no singleton
        if not cfg.repo_map:
            return _refuse("autopilot.yaml missing or repo_map is empty", activity)

        repo, reason = _resolve_repo_label(card, cfg)
        if repo is None:
            return _refuse(reason, activity)

        if not cfg.live_execution:
            return _refuse(
                f"live_execution is not enabled in autopilot.yaml (repo:{repo.name})",
                activity)
        if repo.automerge or repo.name in cfg.automerge_repos:
            return _refuse(
                f"repo:{repo.name} is automerge-enabled; the bridge refuses "
                "automerge repos in P1 even though it structurally cannot merge",
                activity)
        if coerce_quality(repo.min_quality) == QualityMode.GATED:
            return _refuse(
                f"repo:{repo.name} min_quality floor demands the gated "
                "crown-jewel engine; P1 does not provide it (see P3)",
                activity)

        # Fail-closed daily cost cap (autopilot_cost): the ONE place `now()`
        # is read for cost tracking. A read failure on the ledger must never
        # block an otherwise-legitimate run, so it is treated as "no prior
        # spend today" rather than propagated.
        today = datetime.now(timezone.utc).date().isoformat()
        try:
            pre = autopilot_cost.day_total(today)["cost_usd"]
        except Exception:  # noqa: BLE001 -- cost tracking must never break the bridge
            pre = 0.0
        if pre >= cfg.caps.max_usd_per_day:
            try:
                autopilot_cost.check_and_alert_caps(
                    cfg=cfg, today=today, day_cost=pre, this_run_tokens=0)
            except Exception:  # noqa: BLE001
                pass
            return _refuse(
                f"daily cost cap ${cfg.caps.max_usd_per_day:.2f} reached "
                f"(spent ${pre:.2f} today); raise max_usd_per_day to continue",
                activity)

        harness = build_harness(cfg)

        instruction = context.get("instruction", "")
        description = instruction
        if card.description:
            description = f"{instruction}\n\n{card.description}".strip()
        acceptance = card.meta.get("acceptance") or [instruction]

        item = WorkItem(
            kind="agentrun-direct", ref=f"airun-{card_id}", source="agent-run",
            repo=repo.name,
            payload={
                "title": context.get("title", ""),
                "description": description,
                "acceptance": acceptance,
                "tags": [f"repo:{repo.name}"],
                "unblocked": True, "verdict": "valid",
                "priority": getattr(card, "priority", None),
            })

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"airun-{card_id}-{stamp}"
        run_journal = journal_handle(run_id)
        digest = _ActivityDigestShim(activity)
        ex = AgentRunDirectExecutor(cfg, board=None, journal=run_journal, digest=digest)

        # Cost capture: wrap harness.run_task (DirectExecutor.run's own call
        # site, one level down) with a closure that records the HarnessResult's
        # cost/tokens into `captured` ADDITIVELY, and restore the real callable
        # no matter what. Guarded: a mocked/incomplete harness (unit tests) with
        # no run_task attribute is left untouched, never AttributeError'd.
        # `captured["raw"]` keeps the run_task result's claude-code JSON (when
        # present) so settlement usage can recover the input/output token split
        # via BuildUsage.from_claude_json (design doc J1 step 1).
        captured = {"cost_usd": 0.0, "tokens": 0, "raw": None}

        def _accrue(res) -> None:
            try:
                captured["cost_usd"] += float(getattr(res, "cost_usd", 0.0) or 0.0)
                captured["tokens"] += int(getattr(res, "tokens", 0) or 0)
                raw = getattr(res, "raw", None)
                if raw:
                    captured["raw"] = raw
            except Exception:  # noqa: BLE001 -- never let capture break a real result
                pass

        _real_run_task = getattr(harness, "run_task", None)
        if _real_run_task is not None:
            def _cost_capturing_run_task(brief, _real=_real_run_task):
                res = _real(brief)
                _accrue(res)
                return res
            harness.run_task = _cost_capturing_run_task
        try:
            gr: GateResult = ex.run(item, harness)      # ONE sandboxed round, no grade
        finally:
            if _real_run_task is not None:
                harness.run_task = _real_run_task
        activity.append({"atype": "action",
                          "text": f"sandboxed run: passed={gr.passed} notes={gr.notes}"})

        wt = run_journal.worktree_for(item.ref)
        # Close the grade-cost gap (design doc J1 step 2): ratify() calls
        # harness.grade, an LLM call whose cost is otherwise captured nowhere.
        # Wrap it with the same accrue-and-restore closure so the run's true
        # joule cost (ledger row AND settled usage) includes the grade.
        _real_grade = getattr(harness, "grade", None)
        if _real_grade is not None:
            def _cost_capturing_grade(brief, _real=_real_grade):
                res = _real(brief)
                _accrue(res)
                return res
            harness.grade = _cost_capturing_grade
        try:
            rr = ratify(repo, wt, item.payload["acceptance"], harness)   # grade only
        finally:
            if _real_grade is not None:
                harness.grade = _real_grade
        activity.append({
            "atype": "action",
            "text": (f"independent grade: score={rr.score} "
                     f"twin gate {'PASS' if rr.passed else 'not passed'}: {rr.notes}"),
        })

        def _record_cost_and_check_caps(pr_url_for_record: str) -> None:
            """Ledger write + activity line + cap alert. Wrapped defensively so
            a cost-tracking bug can NEVER turn this run's real outcome
            (pass/refuse, PR opened or not) into a crash."""
            try:
                day_after = pre + captured["cost_usd"]
                activity.append({
                    "atype": "action",
                    "text": (f"run cost ${captured['cost_usd']:.4f}, "
                             f"{captured['tokens']} tokens; today "
                             f"${day_after:.2f}/${cfg.caps.max_usd_per_day:.0f}"),
                })
                autopilot_cost.record_run(
                    card_id=card_id, repo=repo.name, tokens=captured["tokens"],
                    cost_usd=captured["cost_usd"], passed=bool(gr.passed),
                    pr=pr_url_for_record, ts=_now_iso(), run_id=run_id)
                autopilot_cost.check_and_alert_caps(
                    cfg=cfg, today=today, day_cost=day_after,
                    this_run_tokens=captured["tokens"])
            except Exception:  # noqa: BLE001 -- cost tracking must never break the bridge
                pass

        if not gr.passed:
            if wt:
                ex.prune_worktree(repo, wt)
            _record_cost_and_check_caps("")
            return _refuse(f"sandboxed run did not pass: {gr.notes}", activity)

        ex.finalize(item, gr)          # commit + push + DRAFT PR; gr.mode == "direct"
        branch = f"autopilot/{item.ref}"
        pr_url = getattr(ex, "pr_url", "")
        _record_cost_and_check_caps(pr_url)
        if not pr_url:
            return _refuse(
                f"branch {branch} pushed but the PR could not be opened "
                "(gh pr create failed); open it by hand",
                activity, links={"branch": branch})

        # Wallet settlement (design doc J1 section 3, step 3): settle to the
        # JouleWallet only on the REAL ratify twin-gate verdict, never the
        # ungated direct-mode gr.passed. AgentRunDirectExecutor._settle_economics
        # stays a permanent no-op; this is the one place that holds `rr` in
        # hand. Deduped by card_id (section 6's J1 hardening rule) and wrapped
        # end-to-end in try/except so a wallet failure never turns a shipped
        # draft PR into a refusal.
        if rr.passed:
            try:
                if autopilot_cost.already_settled(card_id):
                    activity.append({"atype": "action",
                                      "text": "joules already settled for this card"})
                else:
                    try:
                        head_sha = ex._head_sha(wt)
                    except Exception:  # noqa: BLE001 -- a sha lookup failure must
                        head_sha = ""  # never block settlement; proof_hash falls
                        # back to task_ref inside joules.settle.

                    # Usage: fold BOTH run_task's and grade's captured cost into
                    # one BuildUsage whose cost_usd equals captured["cost_usd"]
                    # exactly (the run's true joule cost), while still recovering
                    # the input/output token split from run_task's raw JSON when
                    # available, for UsageTracker fidelity.
                    usage = joules.BuildUsage()
                    if captured.get("raw"):
                        from_raw = joules.BuildUsage.from_claude_json(captured["raw"])
                        usage.add(cost_usd=captured["cost_usd"],
                                  input_tokens=from_raw.input_tokens,
                                  output_tokens=from_raw.output_tokens,
                                  turns=from_raw.turns, model=from_raw.model)
                    else:
                        usage.add(cost_usd=captured["cost_usd"],
                                  output_tokens=captured["tokens"], turns=1)

                    # Q4: charge/credit the REQUESTING agent (context["agent"],
                    # the R1 seam identity), never the runner node's own env
                    # agent -- the economy is a per-agent P&L, not a per-node one.
                    settle_agent = context.get("agent") or "lumina"
                    econ = joules.settle(
                        agent=settle_agent, task_ref=f"airun-{card_id}",
                        priority=getattr(card, "priority", None), score=rr.score,
                        usage=usage, commit_sha=head_sha, home=home)
                    autopilot_cost.record_settlement(
                        card_id=card_id, commit_sha=head_sha, agent=settle_agent,
                        minted=econ.minted, spent=econ.spent_joules,
                        net=econ.net_joules, balance_after=econ.balance_after,
                        ts=_now_iso())
                    activity.append({
                        "atype": "action",
                        "text": (f"joules settled: +{econ.minted} "
                                 f"-{econ.spent_joules} = net {econ.net_joules} "
                                 f"(balance {econ.balance_after})")})
            except Exception as exc:  # noqa: BLE001 -- a settlement failure must
                # never turn a shipped draft PR into a refusal.
                activity.append({
                    "atype": "action",
                    "text": f"joule settlement failed (build unaffected): {exc}"})

        summary = (f"draft PR {pr_url}; independent grade "
                   f"{rr.score if rr.score is not None else 'n/a'}/5, "
                   f"twin gate {'PASS' if rr.passed else 'not passed'}; "
                   "human review required")
        return {"summary": summary, "activity": activity,
                "links": {"pr": pr_url, "branch": branch}}
    except Exception as exc:  # noqa: BLE001 -- fail-closed: no exception ever
        # escapes to the R1 seam. A bug caught here must read as a refusal,
        # never as a crash that could be mistaken for "nothing happened".
        activity.append({"atype": "error", "text": str(exc)})
        return {"summary": f"execute refused (bridge): unexpected error: {exc}",
                "activity": activity, "links": {}}
