"""Engineering work-type executor: resolve repo, claim + lease, produce in an
isolated worktree, grade to 5/5 behind the external-CI twin gate, finalize.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import health
from .ci import external_ci_verdict, diff_coverage
from .types import DecisionItem, GateResult, GradeBrief, RepoSpec, TaskBrief, WorkItem


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Concurrency guards for on-host parallelism (phase2_swarm runs up to
# caps.max_concurrent builds at once). The BUILDS themselves are isolated
# (per-card worktree + sandbox + PR branch) and run unlocked; only two shared
# resources need serializing, both held BRIEFLY (seconds), never across a build:
#   _GIT_LOCK   -- the repo's worktree list + branch refs (git worktree add /
#                  remove / prune, commit+push): concurrent git on one repo races
#                  on .git locks.
#   _BOARD_LOCK -- the shared "autopilot" agent file (claim/complete do a
#                  read-modify-write of ONE file; distinct-card task files are
#                  already safe and are not guarded).
_GIT_LOCK = threading.Lock()
_BOARD_LOCK = threading.Lock()


_PROMISE = re.compile(r"<promise>\s*([A-Z_]+)\s*</promise>")


def parse_promise(text: str | None) -> str | None:
    """Return the SIGNAL inside a <promise>SIGNAL</promise> tag, else None."""
    m = _PROMISE.search(text or "")
    return m.group(1) if m else None


def strip_promise(text: str | None) -> str:
    """Remove any promise tag(s) and trim, for display / feedback carry-forward."""
    return _PROMISE.sub("", text or "").strip()


def is_complete(text: str | None, signal: str = "COMPLETE") -> bool:
    """True only for a real promise tag carrying exactly `signal`."""
    return parse_promise(text) == signal


def twin_gate_passed(gr: GateResult, ci_status: str, cov: float | None,
                     repo: RepoSpec) -> bool:
    """The load-bearing twin gate: LLM 5/5 + an independent COMPLETE promise token,
    ANDed with external CI green and diff-coverage at/above the repo floor.

    The SINGLE source of truth for the crown-jewel predicate: EngineeringExecutor.run
    (the gated Ralph loop) and the ratify one-shot both call it, so the two grade
    paths can never drift. The gate conformance test pins it through run(); do not
    weaken it."""
    cov_ok = cov is not None and cov >= repo.min_diff_coverage
    return (gr.score == 5 and is_complete(gr.notes)
            and ci_status == "green" and cov_ok)


class EngineeringExecutor:
    kind = "engineering"

    def __init__(self, config, board, journal, digest=None, *,
                 agent_name: str | None = None) -> None:
        self.config = config
        self.board = board
        self.journal = journal
        self.digest = digest
        from .fleet_dispatch import claim_agent_name
        self.agent_name = agent_name or claim_agent_name()
        # Per-build LLM usage, keyed by item.ref, accrued across rounds in run()
        # and settled into the SKJoule wallet at finalize() on a twin-gate pass.
        self._build_usage: dict = {}   # item.ref -> joules.BuildUsage

    def _agent(self) -> str:
        """The agent whose wallet earns/pays for this build's work."""
        import os

        return (os.environ.get("SKAGENT")
                or getattr(self.config, "agent", None)
                or "lumina")

    def _accrue_usage(self, ref: str, hr) -> None:
        """Fold one implement round's tokens + cost into the build's running usage.

        Best-effort telemetry: never raises into the build loop.
        """
        try:
            from .joules import BuildUsage

            u = self._build_usage.setdefault(ref, BuildUsage())
            raw = getattr(hr, "raw", None)
            if isinstance(raw, dict) and raw.get("usage"):
                r = BuildUsage.from_claude_json(raw)
                u.add(input_tokens=r.input_tokens, output_tokens=r.output_tokens,
                      cost_usd=r.cost_usd, turns=r.turns, model=r.model)
            else:  # older/stub result: fall back to the summed fields
                u.add(output_tokens=int(getattr(hr, "tokens", 0) or 0),
                      cost_usd=float(getattr(hr, "cost_usd", 0.0) or 0.0), turns=1)
        except Exception as exc:
            health.record("usage_accrue_error", task=ref, error=str(exc)[:120])

    def _settle_economics(self, item: WorkItem, sha: str) -> None:
        """Settle the build's joule P&L on a twin-gate pass (mint value, spend real
        token cost). Best-effort: a wallet failure never affects the finalized PR."""
        try:
            from .joules import BuildUsage, settle

            usage = self._build_usage.pop(item.ref, None) or BuildUsage()
            econ = settle(self._agent(), item.ref,
                          priority=item.payload.get("priority"),
                          score=5, usage=usage, commit_sha=sha)
            health.record("build_economics", task=item.ref, minted=econ.minted,
                          cost_usd=econ.cost_usd, net_joules=econ.net_joules,
                          joules_per_usd=round(econ.joules_per_usd, 2),
                          tokens=econ.tokens, recorded=econ.recorded)
            if econ.recorded:
                print(f"autopilot[{item.ref}] {econ.summary()}")
        except Exception as exc:
            health.record("build_economics_error", task=item.ref, error=str(exc)[:120])

    def _repo_names(self, item: WorkItem) -> list[str]:
        return [t.split(":", 1)[1] for t in item.payload.get("tags", [])
                if t.startswith("repo:")]

    def resolve_repo(self, item: WorkItem) -> RepoSpec | None:
        names = self._repo_names(item)
        if len(names) != 1:
            return None
        return self.config.repo_map.get(names[0])

    def selectable(self, item: WorkItem) -> bool:
        p = item.payload
        tags = p.get("tags", [])
        if not p.get("unblocked"):
            return False
        if p.get("verdict") != "valid":
            return False
        if "autopilot-untriaged" in tags:
            return False
        if self.resolve_repo(item) is None:   # also enforces exactly-one-known
            return False
        if not (p.get("acceptance") or p.get("deliverable")):
            return False
        return True

    def claim(self, item: WorkItem) -> None:
        """Claim the coord task before any work (a second runtime cannot double-
        execute), then record the lease start so a crash is reclaimable. The
        claimer is node-scoped (autopilot-<node>) so a stale fleet placement
        loses the race loudly (ClaimRaced) instead of double-running."""
        from .types import ClaimRaced
        with _BOARD_LOCK:                   # shared agent file (read-modify-write)
            try:
                self.board.claim_task(self.agent_name, item.ref)
            except ValueError as exc:
                raise ClaimRaced(f"{item.ref}: {exc}") from exc
        self.journal.record_claim(item.ref, claimed_at=_now_iso())

    def _worktree_path(self, item: WorkItem, repo: RepoSpec) -> str:
        base = Path(repo.path)
        return str(base.parent / f"{base.name}-wt" / item.ref)

    def make_worktree(self, item: WorkItem, repo: RepoSpec) -> str:
        wt = self._worktree_path(item, repo)
        try:
            Path(wt).parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        branch = f"autopilot/{item.ref}"
        # Self-healing: a prior attempt on this task may have left the worktree
        # dir and/or the local branch behind (a killed run, an escalation after
        # coding, a crash mid-finalize). `git worktree add -b` hard-fails on a
        # pre-existing PATH or BRANCH, which would strand EVERY retry of the task
        # (observed live: the loop's attempt 1 created autopilot/<ref>, attempts
        # 2-4 all died here). Clear the stale local state first so a re-run is
        # idempotent. Only local state is touched; a pushed origin branch is left
        # for finalize's push to reconcile.
        with _GIT_LOCK:                     # serialize shared-repo worktree/ref writes
            self._clear_stale_worktree(repo, wt, branch, item.ref)
            subprocess.run(["git", "-C", repo.path, "worktree", "add", "-b",
                            branch, wt, repo.base_branch],
                           check=True, capture_output=True, text=True)
        return wt

    def _clear_stale_worktree(self, repo: RepoSpec, wt: str, branch: str, ref: str) -> None:
        """Best-effort removal of a leftover worktree dir + local branch so
        make_worktree is idempotent across retries. Never raises."""
        healed = False
        r = subprocess.run(["git", "-C", repo.path, "worktree", "remove", "--force", wt],
                           capture_output=True, text=True)
        healed = healed or r.returncode == 0
        subprocess.run(["git", "-C", repo.path, "worktree", "prune"],
                       capture_output=True, text=True)
        if Path(wt).exists():                        # untracked leftovers on disk
            shutil.rmtree(wt, ignore_errors=True)
            healed = True
        r = subprocess.run(["git", "-C", repo.path, "branch", "-D", branch],
                           capture_output=True, text=True)
        healed = healed or r.returncode == 0
        if healed:
            health.record("worktree_healed", task=ref, branch=branch)

    def prune_worktree(self, repo: RepoSpec, wt: str) -> None:
        with _GIT_LOCK:                     # serialize shared-repo worktree writes
            subprocess.run(["git", "-C", repo.path, "worktree", "remove", "--force", wt],
                           capture_output=True, text=True)
            subprocess.run(["git", "-C", repo.path, "worktree", "prune"],
                           capture_output=True, text=True)

    _MAX_ROUNDS = 4

    def _stage_work(self, wt: str) -> None:
        """Stage the harness's edits INCLUDING new/untracked files, minus CI/coverage
        byproducts. The harness writes new files (e.g. fresh test files) but never
        `git add`s them; a plain `git diff` omits untracked files, so the grade would
        see 'no tests present', and scoped CI + diff-coverage would never run the new
        tests (coverage reads ~0 on the new source). The twin gate then can NEVER pass
        a correct TDD change. Staging first is what makes new tests visible to all
        three gate arms."""
        subprocess.run(["git", "-C", wt, "add", "-A"], capture_output=True, text=True)
        subprocess.run(["git", "-C", wt, "reset", "-q", "--",
                        "coverage.xml", ".coverage", ".pytest_cache",
                        ":(glob)**/__pycache__/**", ":(glob)**/*.pyc"],
                       capture_output=True, text=True)

    def _diff(self, repo: RepoSpec, wt: str) -> str:
        # Stage first so untracked new files (fresh test files!) appear in the diff;
        # `--cached` then diffs the full staged worktree against base.
        self._stage_work(wt)
        proc = subprocess.run(["git", "-C", wt, "diff", "--cached", repo.base_branch],
                              capture_output=True, text=True)
        return proc.stdout

    def _head_sha(self, wt: str) -> str:
        proc = subprocess.run(["git", "-C", wt, "rev-parse", "HEAD"],
                              capture_output=True, text=True)
        return proc.stdout.strip()

    def escalate(self, item: WorkItem, reason: str) -> DecisionItem:
        """Queue a decision for a non-converging item (mirrors the stub shape)."""
        qid = hashlib.sha1(f"engineering:{item.ref}:{reason}".encode()).hexdigest()[:12]
        return DecisionItem(qid=qid,
                            prompt=f"Engineering task {item.ref} did not converge: {reason}",
                            options={"take": "take", "defer": "defer"},
                            action_ref=item.ref, priority="high")

    def run(self, item: WorkItem, harness) -> GateResult:
        self.claim(item)                    # claim before any work: no double-execution
        repo = self.resolve_repo(item)
        p = item.payload
        wt = self.make_worktree(item, repo)
        self.journal.set_worktree(item.ref, wt)   # so finalize() can find this worktree
        pr_branch = f"autopilot/{item.ref}"
        feedback: str | None = None
        last: GateResult | None = None
        empty_rounds = 0
        for rnd in range(1, self._MAX_ROUNDS + 1):
            # Ralph: a FRESH harness session that re-reads disk state each round.
            tb = TaskBrief(task_id=item.ref, repo=repo, worktree=wt,
                           title=p.get("title", ""), description=p.get("description", ""),
                           acceptance=p.get("acceptance", []),
                           prior_feedback=feedback, round=rnd)
            hr = harness.run_task(tb)
            self._accrue_usage(item.ref, hr)   # token/cost telemetry for the joule P&L
            diff = self._diff(repo, wt)
            # No-op guard (efficiency): a build that produces NO diff can never
            # pass the gate, so grinding all MAX_ROUNDS on it burns tokens for
            # nothing -- the exact failure that made a stale card (its work already
            # on the base branch) cost ~28 min. One empty round may be a flaky
            # run, so retry ONCE with explicit feedback; a second empty round means
            # the change is genuinely already present (stale card) or the agent
            # cannot write, so bail with a distinct no-op result the operator can
            # act on (mark complete / revise) rather than a generic gate failure.
            if not diff.strip():
                empty_rounds += 1
                health.record("empty_diff_round", task=item.ref, round=rnd,
                              consecutive=empty_rounds)
                if empty_rounds >= 2:
                    return GateResult(
                        score=None, passed=False, artifact=None,
                        notes=("no-op: the agent produced no diff in 2 rounds. The "
                               "acceptance is likely ALREADY satisfied on the base "
                               "branch (stale card) or the harness cannot write. "
                               "This is not a gate failure -- review the card."))
                feedback = ("You produced NO changes to the repository. If the "
                            "acceptance criteria are ALREADY satisfied by existing "
                            "code on this branch, do not re-implement -- instead make "
                            "a minimal no-op-safe adjustment ONLY if something is "
                            "genuinely missing. Otherwise create/edit the required "
                            "files now; an empty diff cannot pass review.")
                continue   # nothing to grade on an empty diff; skip CI/grade
            empty_rounds = 0
            ci_status = external_ci_verdict(repo, pr_branch, self._head_sha(wt),
                                            worktree=wt, diff=diff)
            cov = diff_coverage(repo, wt, diff)
            gb = GradeBrief(task_id=item.ref, repo=repo, worktree=wt, diff=diff,
                            acceptance=p.get("acceptance", []),
                            ci_status=ci_status, diff_coverage=cov)
            gr = harness.grade(gb)              # fresh, no shared context with run_task
            self.board.score_task(item.ref, round=rnd, score=(gr.score or 0),
                                  notes=strip_promise(gr.notes), harness=harness.name)
            last = gr
            # deterministic twin gate: LLM 5/5 + promise ANDed with CI green +
            # coverage. The predicate is the shared twin_gate_passed (also used by
            # the ratify one-shot) so the gate has one definition, never two.
            if twin_gate_passed(gr, ci_status, cov, repo):
                return GateResult(score=5, passed=True,
                                  notes=strip_promise(gr.notes), artifact=gr.artifact)
            # Grade-resilience: the grader could not certify (score None == the
            # adapter returned no parseable verdict even after its retries -- a
            # flaky/transient grade), BUT the DETERMINISTIC signals are strong: CI
            # green + coverage met + a real diff. Sound work must not be stranded or
            # burn the remaining rounds on a grader that will not answer. Salvage it
            # to a HUMAN-reviewed PR (never auto-merged -- the grade never said 5).
            cov_ok = cov is None or cov >= getattr(repo, "min_diff_coverage", 0.8)
            if gr.score is None and ci_status == "green" and diff.strip() and cov_ok:
                health.record("grade_inconclusive_ci_green", task=item.ref, round=rnd)
                pr_url = self._salvage_to_review(item, repo, wt, pr_branch)
                return GateResult(
                    score=None, passed=False, artifact=pr_url,
                    notes=(f"grade inconclusive but CI green + coverage met; opened "
                           f"PR {pr_url} for human review (NOT auto-merged)."))
            feedback = strip_promise(gr.notes)
        return GateResult(score=(last.score if last else None), passed=False,
                          notes=f"did not converge in {self._MAX_ROUNDS} rounds: "
                                f"{strip_promise(last.notes) if last else ''}",
                          artifact=(last.artifact if last else None))

    def _merge(self, repo: RepoSpec, pr_branch: str) -> str:
        subprocess.run(["git", "-C", repo.path, "checkout", repo.integration_branch],
                       check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", repo.path, "merge", "--no-ff", pr_branch],
                       check=True, capture_output=True, text=True)
        proc = subprocess.run(["git", "-C", repo.path, "rev-parse", "HEAD"],
                              capture_output=True, text=True)
        return proc.stdout.strip()

    def _commit_and_push(self, repo: RepoSpec, wt: str, pr_branch: str, item: WorkItem) -> None:
        """Commit the harness's worktree edits onto pr_branch and push. The harness
        edits the worktree but does not commit, so a PR/merge would otherwise have
        no commits. Push is best-effort (a repo with no origin stays local)."""
        # same staging as the gate (new files in, CI/coverage byproducts out)
        with _GIT_LOCK:                     # serialize shared-repo commit/push/ref writes
            self._stage_work(wt)
            title = item.payload.get("title", item.ref)
            subprocess.run(["git", "-C", wt, "commit", "-m", f"autopilot: {title}"],
                           capture_output=True, text=True)      # no-op commit tolerated
            subprocess.run(["git", "-C", wt, "push", "-u", "origin", pr_branch],
                           capture_output=True, text=True)      # best-effort (needs origin)

    def _pr_base(self, repo: RepoSpec) -> str:
        """The base branch to open the PR against. Prefer integration_branch, but
        fall back to base_branch when the integration branch does not exist on
        origin. Learned the hard way: a configured integration_branch that was
        never created (e.g. 'autopilot/integration') makes `gh pr create` fail
        AFTER the work is committed+pushed — the branch lands but no PR opens, and
        the failure was swallowed. base_branch always exists (we branched from it)."""
        chk = subprocess.run(["git", "-C", repo.path, "ls-remote", "--heads",
                              "origin", repo.integration_branch],
                             capture_output=True, text=True)
        if chk.stdout.strip():
            return repo.integration_branch
        return repo.base_branch

    def _open_pr(self, repo: RepoSpec, pr_branch: str, item: WorkItem) -> str:
        base = self._pr_base(repo)
        proc = subprocess.run(
            ["gh", "pr", "create", "--head", pr_branch, "--base", base,
             "--title", f"autopilot: {item.payload.get('title', item.ref)}",
             "--body", f"Autopilot task {item.ref}"],
            cwd=repo.path, capture_output=True, text=True)
        if proc.returncode != 0:
            # never eat a PR-open failure silently: the branch is already pushed,
            # so a swallowed error looks like "no work done". Surface it.
            print(f"autopilot: gh pr create failed for {pr_branch} (base={base}): "
                  f"{proc.stderr.strip()}")
        return proc.stdout.strip()

    def _salvage_to_review(self, item: WorkItem, repo: RepoSpec, wt: str,
                           pr_branch: str) -> str:
        """The grader could not certify but CI + coverage hold: commit + push the
        work and open a PR for HUMAN review. NEVER auto-merges (the grade never
        reached 5). Returns the PR url; the orchestrator escalates it as a decision.
        """
        self._commit_and_push(repo, wt, pr_branch, item)
        pr_url = self._open_pr(repo, pr_branch, item)
        self._build_usage.pop(item.ref, None)   # drop unsettled usage (no mint on a non-pass)
        health.record("grade_salvaged_to_review", task=item.ref, pr=pr_url)
        print(f"autopilot[{item.ref}] grade inconclusive; salvaged to PR {pr_url} for review")
        return pr_url

    def finalize(self, item: WorkItem, result: GateResult) -> None:
        # G2 defense-in-depth: this executor owns the twin-gated merge path, so a
        # non-gated result must never reach it. A missing `mode` attribute is
        # treated as gated for back-compat (every pre-toggle GateResult had no
        # mode field at all). Refuse BEFORE any commit/merge/push.
        if getattr(result, "mode", "gated") != "gated":
            raise RuntimeError(
                "EngineeringExecutor cannot finalize a non-gated result; "
                "non-gated work is finalized by its own executor "
                "(toggle spec G1/G2/G4).")
        repo = self.resolve_repo(item)
        wt = self.journal.worktree_for(item.ref)
        pr_branch = f"autopilot/{item.ref}"
        self._commit_and_push(repo, wt, pr_branch, item)   # harness edits are uncommitted
        if result.passed:
            # Verified work reached finalize: settle the build's joule P&L (mint the
            # value, spend the real token cost) now that a commit SHA exists.
            self._settle_economics(item, self._head_sha(wt))
        # Always open the PR: it is the visible record AND the surface GitHub CI
        # runs on. Auto-merge then merges THAT PR on GitHub (updating origin +
        # leaving history), never a silent local merge.
        pr_url = self._open_pr(repo, pr_branch, item)
        automerge = (repo.name in self.config.automerge_repos
                     and repo.ci != "none" and result.passed and repo.automerge)
        if automerge:
            # GitHub-safe auto-merge: the twin gate already passed (local CI green +
            # score 5 + coverage); before merging to a shared/deployed branch we ALSO
            # require the repo's real GitHub checks to be green, and we HOLD for human
            # review on any red or on a security-scanner flag (e.g. GitGuardian) --
            # a flagged PR must never auto-merge. Never merge on an unknown/timeout.
            verdict = self._github_checks_verdict(repo, pr_branch)
            if verdict == "green":
                # Prune the local worktree BEFORE the merge: `gh pr merge --delete-branch`
                # also deletes the LOCAL branch, which errors while a worktree still holds
                # it, making a SUCCESSFUL GitHub merge look like a failure (false "held").
                # CI, coverage, and the PR are already done, so the worktree is unneeded.
                self.prune_worktree(repo, wt)
                if self._gh_merge(repo, pr_branch):
                    self.board._write_task_raw(
                        item.ref,
                        lambda d: d.setdefault("meta", {}).setdefault("autopilot", {})
                                  .__setitem__("merge", {"pr": pr_url, "branch": pr_branch,
                                                         "ts": _now_iso(), "auto": True}))
                    with _BOARD_LOCK:           # shared agent file
                        self.board.complete_task(self.agent_name, item.ref)
                    health.record("automerge", task=item.ref, pr=pr_url, verdict="green")
                    print(f"autopilot[{item.ref}] AUTO-MERGED {pr_url} (GitHub CI green)")
                    return
            health.record("automerge_held", task=item.ref, pr=pr_url, verdict=verdict)
            self.digest.queue_decision(
                prompt=(f"auto-merge HELD ({verdict}) for PR {pr_url} task {item.ref} -- "
                        f"review then merge/close"),
                options={"yes": "merge", "no": "close", "defer": "later"},
                action_ref=f"merge:{item.ref}", priority="high")
            print(f"autopilot[{item.ref}] auto-merge held ({verdict}); {pr_url} queued for review")
            return
        # PR-only (auto-merge off for this repo): queue the review decision.
        self.digest.queue_decision(
            prompt=f"Merge PR {pr_url} for task {item.ref}?",
            options={"yes": "merge", "no": "close", "defer": "later"},
            action_ref=f"merge:{item.ref}", priority="high")
        # leave the task claimed (not completed) until the operator approves

    # -- GitHub-safe auto-merge helpers --------------------------------------
    _AUTOMERGE_CI_TIMEOUT = 1500     # seconds to wait for GitHub checks to settle
    _AUTOMERGE_CORE = ("lint", "test", "qa", "pytest")   # quality gates that MUST pass
    _AUTOMERGE_SECURITY = ("gitguardian", "security")    # a flag here HOLDS the merge

    def _github_checks_verdict(self, repo: RepoSpec, pr_branch: str) -> str:
        """Poll the PR's GitHub checks. Returns one of:

          green   -- every discovered core CI check passed and no security check failed
          red     -- a core CI check failed (do not merge)
          blocked -- a security check (GitGuardian) failed -> hold for human review
          timeout -- core checks still pending at the deadline (never merge on unknown)

        Release jobs (publish-*) and any other unrelated checks are ignored -- they
        are not quality gates for the change.
        """
        deadline = time.monotonic() + self._AUTOMERGE_CI_TIMEOUT

        def _hit(names, name):
            n = (name or "").lower()
            return any(k in n for k in names)

        while True:
            proc = subprocess.run(
                ["gh", "pr", "checks", pr_branch, "--json", "name,bucket"],
                cwd=repo.path, capture_output=True, text=True)
            try:
                checks = json.loads(proc.stdout or "[]")
            except Exception:
                checks = []
            core = [c for c in checks if _hit(self._AUTOMERGE_CORE, c.get("name"))]
            sec = [c for c in checks if _hit(self._AUTOMERGE_SECURITY, c.get("name"))]
            if any(c.get("bucket") == "fail" for c in sec):
                return "blocked"
            if any(c.get("bucket") == "fail" for c in core):
                return "red"
            pending = [c for c in core + sec if c.get("bucket") == "pending"]
            if core and not pending:
                return "green"        # all core passed, no security fail, none pending
            if time.monotonic() >= deadline:
                return "timeout"      # never green on unknown/still-pending
            time.sleep(20)

    def _gh_merge(self, repo: RepoSpec, pr_branch: str) -> bool:
        """Merge the PR on GitHub (updates origin, deletes the branch). Returns
        False on failure (e.g. a required check GitHub itself blocks on) so the
        caller falls back to a human decision rather than silently dropping it."""
        proc = subprocess.run(
            ["gh", "pr", "merge", pr_branch, "--merge", "--delete-branch"],
            cwd=repo.path, capture_output=True, text=True)
        if proc.returncode != 0:
            print(f"autopilot: gh pr merge failed for {pr_branch}: {proc.stderr.strip()}")
        return proc.returncode == 0


def _revert_impl(board, config, task_id: str, agent: str = "autopilot") -> dict:
    """Revert the recorded merge commit and reopen the coord task.

    Governance: autopilot reverts only a merge it recorded (meta.autopilot.merge).
    """
    task = next((t for t in board.load_tasks() if t.id == task_id), None)
    if task is None:
        raise ValueError(f"unknown task {task_id}")
    merge = (task.meta or {}).get("autopilot", {}).get("merge")
    if not merge or not merge.get("sha"):
        raise ValueError(f"no recorded merge for {task_id}")
    name = next((t.split(":", 1)[1] for t in task.tags if t.startswith("repo:")), None)
    repo = config.repo_map[name]
    subprocess.run(["git", "-C", repo.path, "checkout", repo.integration_branch],
                   check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", repo.path, "revert", "--no-edit", merge["sha"]],
                   check=True, capture_output=True, text=True)
    af = board.load_agent(agent)
    if af is not None and task_id in af.completed_tasks:
        af.completed_tasks.remove(task_id)     # reopen: undo the completion
        board.save_agent(af)
    board._write_task_raw(
        task_id,
        lambda d: d.setdefault("meta", {}).setdefault("autopilot", {})
                  .__setitem__("reverted", {"sha": merge["sha"], "ts": _now_iso()}))
    return {"task_id": task_id, "reverted_sha": merge["sha"], "reopened": True}


def _load_board():
    from skcapstone.coordination import Board
    from skcapstone.mcp_tools._helpers import _shared_root
    return Board(_shared_root())


def _load_config():
    from skharness.autocode import config
    return config.load()


def revert(task_id: str, agent: str = "autopilot") -> dict:
    """One-arg convenience for the CLI: load Board + Config, delegate to _revert_impl."""
    return _revert_impl(_load_board(), _load_config(), task_id, agent)
