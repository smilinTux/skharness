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
from .failure_memory import build_prior_feedback, build_prior_success_feedback
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
        # Direct mode has no rounds, so this card's ONLY prior context is what
        # previous runs recorded. It used to start from None unconditionally,
        # which is what made a failed direct build repeat itself verbatim.
        tb = TaskBrief(task_id=item.ref, repo=repo, worktree=wt,
                       title=p.get("title", ""), description=p.get("description", ""),
                       acceptance=p.get("acceptance", []),
                       prior_feedback=build_prior_feedback(p), round=1,
                       prior_success_feedback=build_prior_success_feedback(p))
        res = harness.run_task(tb)              # ONE round; no harness.grade() ever
        diff = self._diff(repo, wt)            # same staging (new files in, byproducts out)
        ok = bool(getattr(res, "ok", False))
        passed = ok and bool(diff.strip())
        if not passed:
            self._record_attempt(
                item, round=1, outcome="direct_fail",
                tried=f"ungated single run of {p.get('title', item.ref)!r}",
                why_failed=("the harness produced an empty diff" if ok else
                            "the harness run reported not-ok"),
                replacement_hint=("check the acceptance is not already satisfied "
                                  "on the base branch" if ok else ""))
        # Direct mode has exactly two terminal states and they must stay
        # distinguishable: an ungated run that FAILED must never read as a pass.
        # tokens/cost come straight off the one HarnessResult this mode produces
        # (direct runs no rounds, so it accrues no multi-round BuildUsage), which
        # also stops the CapLedger reading a zero for every direct build.
        return GateResult(score=None, passed=passed,
                          notes="direct mode: UNGATED single run; review required",
                          artifact=wt, mode=QualityMode.DIRECT.value,
                          outcome=("pass" if passed else "direct_fail"),
                          tokens=int(getattr(res, "tokens", 0) or 0),
                          cost_usd=float(getattr(res, "cost_usd", 0.0) or 0.0))

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
        if result.passed:
            # Direct-mode work passed its gate + ships a reviewed PR: settle its
            # joule P&L too (inherited from EngineeringExecutor, best-effort).
            self._settle_economics(item, self._head_sha(wt))
            # S18: direct mode's success path was untouched by S9 as well, so a
            # card worked in direct mode remembered its failures and none of its
            # passes. It records through the same inherited helper, with the
            # UNGATED status stated in the entry itself: a direct pass means the
            # harness ran and produced a reviewable diff, NOT that a twin gate
            # verified it, and a later round must not read one as the other.
            self._record_success(
                item, round=1,
                outcome=(getattr(result, "outcome", "pass") or "pass"),
                tried=f"ungated single run of {item.payload.get('title', item.ref)!r}",
                why_succeeded=("an UNGATED direct-mode run produced a reviewable "
                               "diff; no grade, no twin gate, human review pending"),
                approach_hint="")
        pr_url = self._open_pr(repo, pr_branch, item)
        self.digest.queue_decision(
            prompt=f"Merge PR {pr_url} for task {item.ref}? "
                   f"(UNGATED direct-mode work, review before merge)",
            options={"yes": "merge", "no": "close", "defer": "later"},
            action_ref=f"merge:{item.ref}", priority="high")
        # task stays CLAIMED (never completed) until the operator merges or closes
