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
    ex = EngineeringExecutor(config=None, board=None, journal=None)
    ex._stage_work(worktree)            # make new/untracked test files visible to all arms
    diff = ex._diff(repo, worktree)     # staged diff against base (staging is idempotent)
    branch = _current_branch(worktree)
    head_sha = ex._head_sha(worktree)
    ci_status = external_ci_verdict(repo, branch, head_sha, worktree=worktree, diff=diff)
    cov = diff_coverage(repo, worktree, diff)
    gb = GradeBrief(task_id=Path(worktree).name, repo=repo, worktree=worktree,
                    diff=diff, acceptance=acceptance,
                    ci_status=ci_status, diff_coverage=cov)
    gr = harness.grade(gb)              # fresh grade over the existing diff
    passed = twin_gate_passed(gr, ci_status, cov, repo)   # the ONE pinned predicate
    return GateResult(score=gr.score, passed=passed,
                      notes=strip_promise(gr.notes), artifact=gr.artifact)
