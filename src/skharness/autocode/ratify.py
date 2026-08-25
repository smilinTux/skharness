"""ratify: the one-shot quality gate (extraction ADR Decision 3).

Runs ONE grade cycle over an EXISTING worktree diff and returns the twin-gated
GateResult WITHOUT merging, committing, or pushing. It COMPOSES the exact
per-round internals of EngineeringExecutor (stage work, diff against base,
external CI verdict, diff coverage, harness.grade, the pinned twin-gate
predicate); it reimplements none of them. This is the function skcode's
POST /api/v1/sessions/{sid}/ratify endpoint calls: reuse, never re-derive.
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from .ci import diff_coverage, external_ci_verdict
from .engineering import EngineeringExecutor, strip_promise, twin_gate_passed
from .types import GateResult, GradeBrief, RepoSpec

if TYPE_CHECKING:                       # avoid a runtime import cycle
    from skharness.harness import Harness


def _current_branch(worktree: str) -> str:
    """The worktree's checked-out branch, for the CI twin's github-actions poll.

    A read-only rev-parse; unused by local:/none CI. Falls back to "HEAD" on a
    detached head so external_ci_verdict always has a branch string."""
    proc = subprocess.run(["git", "-C", worktree, "rev-parse", "--abbrev-ref", "HEAD"],
                          capture_output=True, text=True)
    return proc.stdout.strip() or "HEAD"


def ratify(repo: RepoSpec, worktree: str, acceptance: list[str],
           harness: "Harness") -> GateResult:
    """Grade an existing worktree diff behind the twin gate, WITHOUT merging.

    The exact grade path a Ralph round runs, minus the loop and minus finalize:
    stage the harness's edits (new/untracked test files included), diff against
    base, run the external CI verdict + diff coverage OUTSIDE the harness, ask the
    harness to grade, then apply the pinned twin_gate_passed predicate. Returns a
    GateResult carrying the score, passed, and (promise-stripped) notes. It does
    NOT commit, NOT push, NOT merge: ratify only grades.
    """
    # Reuse the engine's pure-git helpers. _stage_work / _diff / _head_sha use only
    # repo + worktree (never board/journal/config), so a helper-only executor is
    # safe and the stage+diff is byte-identical to a real round.
    started_at = datetime.now(timezone.utc)
    ex = EngineeringExecutor(config=None, board=None, journal=None)
    ex._stage_work(worktree)            # make new/untracked test files visible to all arms
    diff = ex._diff(repo, worktree)     # staged diff against base (staging is idempotent)
    branch = _current_branch(worktree)
    head_sha = ex._head_sha(worktree)
    ci_status = external_ci_verdict(repo, branch, head_sha, worktree=worktree, diff=diff)
    cov = diff_coverage(repo, worktree, diff)
    card_id = Path(worktree).name
    gb = GradeBrief(task_id=card_id, repo=repo, worktree=worktree,
                    diff=diff, acceptance=acceptance,
                    ci_status=ci_status, diff_coverage=cov)
    gr = harness.grade(gb)              # fresh grade over the existing diff
    finished_at = datetime.now(timezone.utc)
    passed = twin_gate_passed(gr, ci_status, cov, repo)   # the ONE pinned predicate
    # ratify is called from skcode's ratify endpoint, not the orchestrator, so it
    # writes no outcome row. It populates `outcome` anyway: a field that some
    # construction sites may skip is optional in practice, and an optional
    # terminal-state field is how the vocabulary rots back into a sentinel.
    # A non-pass here is the gate failing to close over an existing diff, which
    # is the same terminal state the gated loop calls ci_red; ratify has no
    # rounds, so no_op and salvage cannot arise. tokens/cost stay 0 because
    # ratify runs no build: it grades a diff someone else already paid for.
    outcome = "pass" if passed else "ci_red"
    notes = strip_promise(gr.notes)
    _write_ratify_run_record(
        card_id=card_id, repo=repo, harness=harness, score=gr.score, passed=passed,
        notes=notes, outcome=outcome, ci_status=ci_status, cov=cov,
        started_at=started_at, finished_at=finished_at)
    return GateResult(score=gr.score, passed=passed,
                      notes=notes, artifact=gr.artifact,
                      outcome=outcome)


def _write_ratify_run_record(*, card_id: str, repo: RepoSpec, harness, score,
                             passed: bool, notes: str, outcome: str,
                             ci_status: str, cov: float | None, started_at,
                             finished_at) -> None:
    """Write one RunRecord at the twin-gate verdict boundary (card c672529e),
    the same boundary the Ralph loop writes at (engineering.py's
    ``_write_run_record``), so lane 1 (this function) and lane 2 (the Ralph
    loop) leave behind the exact same provenance shape for the exact same
    predicate. ratify() carries no run/card identity of its own (it is
    stateless by design, per this module's docstring), so a fresh run_id is
    minted per call -- one ratify call is its own run, never resumed.

    Best-effort: see run_record_writer.write_verdict_run_record. A broken
    provenance writer must never fail a ratify call.
    """
    from . import run_record_writer
    from .orchestrator import _adapter_name, _grader_model, _model_requested

    run_id = f"airun-ratify-{card_id}-{started_at:%Y%m%dT%H%M%SZ}"
    run_record_writer.write_verdict_run_record(
        run_id=run_id,
        card_id=card_id,
        repository=repo.name,
        round=1,
        adapter=_adapter_name(harness),
        # ratify() carries no payload (no `item`): it grades an existing diff,
        # never dispatches one, so there is no work_grade to bucket against.
        model_requested=_model_requested(None, harness),
        grader_model=_grader_model(None, harness),
        effort_tier="gated",  # ratify composes the exact gated internals
        work_grade=None,
        score=score,
        passed=passed,
        notes=notes,
        outcome=outcome,
        ci_status=ci_status,
        diff_coverage=cov,
        min_diff_coverage=repo.min_diff_coverage,
        pr=None,
        retries=0,
        # ratify() carries no card payload (see the docstring above), so
        # there is nothing to resolve an escalation reason from.
        payload=None,
        started_at=started_at,
        finished_at=finished_at,
        source_namespace="skharness.autocode.ratify.ratify",
    )
