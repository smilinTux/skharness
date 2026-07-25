"""DirectExecutor: the simple/unattended quality mode (toggle spec Decision 2).

`direct` mode runs the harness ONCE in an isolated worktree and produces a
reviewable artifact (branch + diff + PR). It DROPS the ceremony that makes a
result trustworthy without a human (Ralph rounds, the independent 1-5 grader,
the external CI twin, the diff-coverage arm) and, structurally, it NEVER merges:
its terminal states are exactly "PR + review decision" or "escalation".

It is composed entirely from EngineeringExecutor's existing helpers by subclassing
it, so claim + lease, the isolated `autopilot/<ref>` worktree, the staging
discipline (`_stage_work` -> new files visible in the diff a human reviews),
`_commit_and_push`, `_pr_base`, `_open_pr`, `escalate`, and `prune_worktree` are
reused verbatim. The crown-jewel run loop and twin gate in engineering.py are not
touched; direct mode routes AROUND them as a separate executor kind, never THROUGH
a loosened gate.
"""
from __future__ import annotations

from .engineering import EngineeringExecutor
from .types import GateResult, QualityMode, TaskBrief, WorkItem


class DirectExecutor(EngineeringExecutor):
    kind = "engineering-direct"

    def selectable(self, item: WorkItem) -> bool:
        """Same gate as EngineeringExecutor EXCEPT the acceptance/deliverable
        requirement is relaxed to 'description non-empty'. Trivial tasks (a typo
        fix) are the whole point of this mode; demanding formal acceptance
        criteria for one would defeat it. Isolation and known-repo checks stay."""
        p = item.payload
        tags = p.get("tags", [])
        if not p.get("unblocked"):
            return False
        if p.get("verdict") != "valid":
            return False
        if "autopilot-untriaged" in tags:
            return False
        if self.resolve_repo(item) is None:   # exactly-one-known-repo, verbatim
            return False
        if not (p.get("description") or "").strip():
            return False
        return True

    def run(self, item: WorkItem, harness) -> GateResult:
        """ONE sandboxed run in an isolated worktree. No grade, no twin gate."""
        self.claim(item)                        # claim + lease (shared helper)
        repo = self.resolve_repo(item)
        p = item.payload
        wt = self.make_worktree(item, repo)     # isolated autopilot/<ref> worktree
        self.journal.set_worktree(item.ref, wt)  # so finalize() finds this worktree
        tb = TaskBrief(task_id=item.ref, repo=repo, worktree=wt,
                       title=p.get("title", ""), description=p.get("description", ""),
                       acceptance=p.get("acceptance", []),
                       prior_feedback=None, round=1)
        res = harness.run_task(tb)              # ONE round; no harness.grade() ever
        diff = self._diff(repo, wt)            # same staging (new files in, byproducts out)
        passed = bool(getattr(res, "ok", False)) and bool(diff.strip())
        return GateResult(score=None, passed=passed,
                          notes="direct mode: UNGATED single run; review required",
                          artifact=wt, mode=QualityMode.DIRECT.value)

    def _merge(self, repo, pr_branch) -> str:
        """HARD GUARDRAIL (G1): direct-mode work is ungated and MUST NOT merge to a
        protected branch. The inherited merge is refused structurally; a human
        merges the PR after review, which is exactly the review this mode forces."""
        raise RuntimeError(
            "DirectExecutor must never merge: direct-mode work is ungated and "
            "requires human review before any merge (toggle spec G1/G4).")

    def finalize(self, item: WorkItem, result: GateResult) -> None:
        """Commit + push the work branch, open a PR, queue the review decision.
        NO merge branch exists in this method. The guard makes the invariant loud:
        a merge is only ever legal for a gated result, which this executor never
        produces, so the merge path is unreachable."""
        # assert mode == gated before any merge could happen: a gated result does
        # not belong here (EngineeringExecutor owns the twin-gated merge path).
        if result.mode == QualityMode.GATED.value:
            raise RuntimeError(
                "DirectExecutor cannot finalize a gated result; gated work is "
                "finalized by EngineeringExecutor behind the twin gate.")
        repo = self.resolve_repo(item)
        wt = self.journal.worktree_for(item.ref)
        pr_branch = f"autopilot/{item.ref}"
        self._commit_and_push(repo, wt, pr_branch, item)   # harness edits are uncommitted
        pr_url = self._open_pr(repo, pr_branch, item)
        self.digest.queue_decision(
            prompt=f"Merge PR {pr_url} for task {item.ref}? "
                   f"(UNGATED direct-mode work, review before merge)",
            options={"yes": "merge", "no": "close", "defer": "later"},
            action_ref=f"merge:{item.ref}", priority="high")
        # task stays CLAIMED (never completed) until the operator merges or closes
