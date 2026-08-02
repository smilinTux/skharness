import json
import os
from pathlib import Path
import types as _t
import pytest
from skharness.autocode.engineering import EngineeringExecutor, _revert_impl
from skharness.autocode.types import WorkItem, RepoSpec, GateResult, HarnessResult


def _spec(name):
    return RepoSpec(name=name, path=f"/repos/{name}", base_branch="main",
                    integration_branch="develop", test_cmd="pytest", ci="none")


@pytest.fixture
def cfg():
    return _t.SimpleNamespace(repo_map={"skrender": _spec("skrender")},
                              automerge_repos=[])


def _item(tags, **payload):
    payload.setdefault("tags", tags)
    return WorkItem(kind="engineering", ref="t1", source="coord", repo=None, payload=payload)


def test_kind_is_engineering(cfg):
    ex = EngineeringExecutor(cfg, board=object(), journal=object())
    assert ex.kind == "engineering"


def test_resolves_single_known_repo_tag(cfg):
    ex = EngineeringExecutor(cfg, board=object(), journal=object())
    spec = ex.resolve_repo(_item(["repo:skrender", "backend"]))
    assert spec is not None and spec.name == "skrender" and spec.path == "/repos/skrender"


def test_unknown_repo_tag_resolves_none(cfg):
    ex = EngineeringExecutor(cfg, board=object(), journal=object())
    assert ex.resolve_repo(_item(["repo:nope"])) is None


def test_two_repo_tags_resolves_none(cfg):
    ex = EngineeringExecutor(cfg, board=object(), journal=object())
    assert ex.resolve_repo(_item(["repo:skrender", "repo:other"])) is None


def test_no_repo_tag_resolves_none(cfg):
    ex = EngineeringExecutor(cfg, board=object(), journal=object())
    assert ex.resolve_repo(_item(["backend"])) is None


def _sel_item(**over):
    p = dict(unblocked=True, verdict="valid", tags=["repo:skrender"],
             acceptance=["does X"])
    p.update(over)
    return WorkItem(kind="engineering", ref="t1", source="coord", repo=None, payload=p)


def test_selectable_happy_path(cfg):
    ex = EngineeringExecutor(cfg, board=object(), journal=object())
    assert ex.selectable(_sel_item()) is True


def test_not_selectable_when_blocked(cfg):
    ex = EngineeringExecutor(cfg, board=object(), journal=object())
    assert ex.selectable(_sel_item(unblocked=False)) is False


def test_not_selectable_when_not_valid(cfg):
    ex = EngineeringExecutor(cfg, board=object(), journal=object())
    assert ex.selectable(_sel_item(verdict="stale")) is False


def test_not_selectable_unknown_repo(cfg):
    ex = EngineeringExecutor(cfg, board=object(), journal=object())
    assert ex.selectable(_sel_item(tags=["repo:nope"])) is False


def test_not_selectable_untriaged(cfg):
    ex = EngineeringExecutor(cfg, board=object(), journal=object())
    assert ex.selectable(_sel_item(tags=["repo:skrender", "autopilot-untriaged"])) is False


def test_not_selectable_when_not_code_shaped(cfg):
    ex = EngineeringExecutor(cfg, board=object(), journal=object())
    assert ex.selectable(_sel_item(acceptance=[], deliverable="")) is False


def test_selectable_via_deliverable_without_acceptance(cfg):
    ex = EngineeringExecutor(cfg, board=object(), journal=object())
    assert ex.selectable(_sel_item(acceptance=[], deliverable="ship the reloader")) is True


def test_claim_calls_board_then_journal(mocker, cfg):
    board = mocker.Mock()
    journal = mocker.Mock()
    manager = mocker.Mock()
    manager.attach_mock(board.claim_task, "claim")
    manager.attach_mock(journal.record_claim, "record")
    ex = EngineeringExecutor(cfg, board=board, journal=journal, agent_name="autopilot")
    item = WorkItem(kind="engineering", ref="t1", source="coord", repo=None,
                    payload={"tags": ["repo:skrender"]})
    ex.claim(item)
    board.claim_task.assert_called_once_with("autopilot", "t1")
    assert journal.record_claim.call_args.kwargs.get("claimed_at") or \
           journal.record_claim.call_args.args
    assert [c[0] for c in manager.mock_calls] == ["claim", "record"]


def test_make_worktree_git_argv(mocker, cfg):
    run = mocker.patch("skharness.autocode.engineering.subprocess.run",
                       return_value=mocker.Mock(returncode=0, stdout="", stderr=""))
    ex = EngineeringExecutor(cfg, board=object(), journal=object())
    item = WorkItem(kind="engineering", ref="t1", source="coord", repo=None, payload={})
    spec = cfg.repo_map["skrender"]
    wt = ex.make_worktree(item, spec)
    # the pre-clear (self-healing) fires subprocess calls first; find the add
    adds = [c.args[0] for c in run.call_args_list
            if c.args[0][3:5] == ["worktree", "add"]]
    argv = adds[0]
    assert argv[:6] == ["git", "-C", "/repos/skrender", "worktree", "add", "-b"]
    assert argv[6] == "autopilot/t1"          # new branch name
    assert argv[7] == wt                       # worktree path
    assert argv[8] == "main"                   # base_branch checkout point


def test_make_worktree_is_idempotent_across_retries(tmp_path):
    """Self-healing: a second make_worktree for the same task must succeed even
    though the first left the worktree dir and local branch behind (the collision
    that stranded every retry live)."""
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "HOME": str(tmp_path), "PATH": os.environ["PATH"]}
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True, env=env)
    (repo / "f").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True, env=env)
    spec = RepoSpec(name="r", path=str(repo), base_branch="main", integration_branch="main",
                    test_cmd="pytest", ci="none")
    cfg = _t.SimpleNamespace(repo_map={"r": spec}, automerge_repos=[])
    ex = EngineeringExecutor(cfg, board=object(), journal=object())
    item = WorkItem(kind="engineering", ref="t9", source="coord", repo=None, payload={})
    wt1 = ex.make_worktree(item, spec)
    assert Path(wt1).exists()
    # second call for the SAME ref would previously raise CalledProcessError
    wt2 = ex.make_worktree(item, spec)
    assert Path(wt2).exists() and wt2 == wt1


def test_prune_worktree_git_argv(mocker, cfg):
    run = mocker.patch("skharness.autocode.engineering.subprocess.run",
                       return_value=mocker.Mock(returncode=0, stdout="", stderr=""))
    ex = EngineeringExecutor(cfg, board=object(), journal=object())
    ex.prune_worktree(cfg.repo_map["skrender"], "/repos/skrender-wt/t1")
    calls = [c.args[0] for c in run.call_args_list]
    assert ["git", "-C", "/repos/skrender", "worktree", "remove", "--force",
            "/repos/skrender-wt/t1"] in calls
    assert ["git", "-C", "/repos/skrender", "worktree", "prune"] in calls


def test_parse_promise_extracts_signal():
    from skharness.autocode.engineering import parse_promise
    assert parse_promise("done here <promise>COMPLETE</promise>") == "COMPLETE"


def test_parse_promise_none_when_absent():
    from skharness.autocode.engineering import parse_promise
    assert parse_promise("still working, not COMPLETE yet") is None


def test_is_complete_requires_the_tag_not_prose():
    from skharness.autocode.engineering import is_complete
    assert is_complete("<promise>COMPLETE</promise>") is True
    assert is_complete("not COMPLETE yet") is False          # false-positive resistance
    assert is_complete("<promise>WORKING</promise>") is False  # wrong signal


def test_strip_promise_removes_tag_and_trims():
    from skharness.autocode.engineering import strip_promise
    assert strip_promise("great work <promise>COMPLETE</promise>") == "great work"


def _run_ex(mocker, cfg, grades, ci_status="green", cov=0.95):
    ex = EngineeringExecutor(cfg, board=mocker.Mock(), journal=mocker.Mock(),
                             agent_name="autopilot")
    mocker.patch.object(ex, "make_worktree", return_value="/wt/t1")
    mocker.patch.object(ex, "prune_worktree")
    mocker.patch.object(ex, "_diff", return_value="DIFF")
    mocker.patch.object(ex, "_head_sha", return_value="sha1")
    mocker.patch("skharness.autocode.engineering.external_ci_verdict", return_value=ci_status)
    mocker.patch("skharness.autocode.engineering.diff_coverage", return_value=cov)
    harness = mocker.Mock(name="harness")
    harness.name = "claude-code"
    harness.run_task.return_value = HarnessResult(ok=True, artifact=None, tokens=1,
                                                  cost_usd=0.0, raw={})
    harness.grade.side_effect = grades
    item = WorkItem(kind="engineering", ref="t1", source="coord", repo=None,
                    payload={"tags": ["repo:skrender"], "title": "t",
                             "description": "d", "acceptance": ["a"]})
    return ex, harness, item


def test_run_claims_before_work(mocker, cfg):
    grades = [GateResult(score=5, passed=True,
                         notes="ready <promise>COMPLETE</promise>", artifact="pr")]
    ex, harness, item = _run_ex(mocker, cfg, grades)
    ex.run(item, harness)
    ex.board.claim_task.assert_called_once_with("autopilot", "t1")


def test_run_persists_worktree_for_finalize(mocker, cfg):
    grades = [GateResult(score=5, passed=True,
                         notes="ready <promise>COMPLETE</promise>", artifact="pr")]
    ex, harness, item = _run_ex(mocker, cfg, grades)
    ex.run(item, harness)
    ex.journal.set_worktree.assert_called_once_with("t1", "/wt/t1")


def test_escalate_returns_decision_item_for_the_task(cfg):
    ex = EngineeringExecutor(cfg, board=object(), journal=object())
    item = WorkItem(kind="engineering", ref="t1", source="coord", repo=None,
                    payload={"tags": ["repo:skrender"]})
    d = ex.escalate(item, "did not converge in 4 rounds")
    assert d.action_ref == "t1"
    assert d.prompt and "t1" in d.prompt


def test_run_stops_at_five_with_green_gate(mocker, cfg):
    grades = [GateResult(score=3, passed=False, notes="thin tests", artifact=None),
              GateResult(score=5, passed=True,
                         notes="ready <promise>COMPLETE</promise>", artifact="pr")]
    ex, harness, item = _run_ex(mocker, cfg, grades)
    res = ex.run(item, harness)
    assert res.passed is True and res.score == 5
    assert harness.run_task.call_count == 2 and harness.grade.call_count == 2
    assert ex.board.score_task.call_count == 2
    rounds = [c.kwargs["round"] for c in ex.board.score_task.call_args_list]
    assert rounds == [1, 2]


def test_run_caps_at_four_rounds_then_fails(mocker, cfg):
    grades = [GateResult(score=4, passed=False, notes="one gap", artifact=None)] * 6
    ex, harness, item = _run_ex(mocker, cfg, grades)
    res = ex.run(item, harness)
    assert res.passed is False
    assert harness.grade.call_count == 4        # round cap 4
    assert ex.board.score_task.call_count == 4


def test_run_salvages_to_review_when_grade_inconclusive_but_ci_green(mocker, cfg):
    # Grade-resilience: the grader returns no score (inconclusive) but CI is green,
    # coverage is met, and the diff is real -> salvage to a human-reviewed PR after
    # ONE round instead of stranding sound work or burning all 4 rounds.
    grades = [GateResult(score=None, passed=False, notes="", artifact=None)] * 6
    ex, harness, item = _run_ex(mocker, cfg, grades)   # _diff="DIFF", ci=green, cov=0.95
    salvage = mocker.patch.object(ex, "_salvage_to_review", return_value="https://gh/pr/9")
    res = ex.run(item, harness)
    assert res.passed is False                 # never a gate pass (grade never said 5)
    salvage.assert_called_once()               # opened a PR for review
    assert "https://gh/pr/9" in (res.notes or "")
    assert harness.grade.call_count == 1       # bailed after ONE round, not 4


def test_run_does_not_salvage_when_ci_red(mocker, cfg):
    # Inconclusive grade + RED ci must NOT salvage (no PR); it keeps trying / fails.
    grades = [GateResult(score=None, passed=False, notes="", artifact=None)] * 6
    ex, harness, item = _run_ex(mocker, cfg, grades, ci_status="red")
    salvage = mocker.patch.object(ex, "_salvage_to_review", return_value="x")
    res = ex.run(item, harness)
    assert res.passed is False
    salvage.assert_not_called()                # red CI -> no salvage
    assert harness.grade.call_count == 4       # ran the full cap


def test_run_bails_fast_on_noop_empty_diff(mocker, cfg):
    # Efficiency: a build that produces NO diff (a stale card whose work is already
    # on the base branch, or a harness that cannot write) can never pass the gate,
    # so it must bail after one retry -- never grind all 4 rounds, never grade an
    # empty diff. This is the exact 28-min waste the guard kills.
    ex, harness, item = _run_ex(mocker, cfg, grades=[])
    mocker.patch.object(ex, "_diff", return_value="")   # agent produced no changes
    res = ex.run(item, harness)
    assert res.passed is False
    assert "no-op" in (res.notes or "").lower()
    assert harness.run_task.call_count == 2   # one flaky-retry, then bail (not 4)
    assert harness.grade.call_count == 0      # never grade / CI / score an empty diff
    assert ex.board.score_task.call_count == 0


def test_twin_gate_blocks_merge_when_ci_red_even_at_five(mocker, cfg):
    grades = [GateResult(score=5, passed=True,
                         notes="<promise>COMPLETE</promise>", artifact="pr")] * 6
    ex, harness, item = _run_ex(mocker, cfg, grades, ci_status="red")
    res = ex.run(item, harness)
    assert res.passed is False                  # CI red overrides a 5/5
    assert harness.grade.call_count == 4


def test_twin_gate_blocks_when_coverage_under_min(mocker, cfg):
    grades = [GateResult(score=5, passed=True,
                         notes="<promise>COMPLETE</promise>", artifact="pr")] * 6
    ex, harness, item = _run_ex(mocker, cfg, grades, ci_status="green", cov=0.5)
    res = ex.run(item, harness)
    assert res.passed is False


def _final_ex(mocker, cfg, repo_name, ci_status="green"):
    ex = EngineeringExecutor(cfg, board=mocker.Mock(), journal=mocker.Mock(),
                             digest=mocker.Mock(), agent_name="autopilot")
    ex.journal.worktree_for.return_value = "/wt/t1"
    # the finalize guard checks the worktree still exists on disk; these tests
    # use a fake path, so treat it as present (the missing-worktree case has its
    # own test, test_finalize_escalates_when_worktree_missing).
    mocker.patch("skharness.autocode.engineering.os.path.isdir", return_value=True)
    mocker.patch.object(ex, "_head_sha", return_value="sha1")
    mocker.patch.object(ex, "_commit_and_push")     # git commit+push of harness edits
    mocker.patch.object(ex, "prune_worktree")
    mocker.patch.object(ex, "_gh_merge", return_value=True)
    mocker.patch.object(ex, "_github_checks_verdict", return_value=ci_status)
    mocker.patch.object(ex, "_open_pr", return_value="https://gh/pr/1")
    item = WorkItem(kind="engineering", ref="t1", source="coord", repo=None,
                    payload={"tags": [f"repo:{repo_name}"], "title": "t"})
    return ex, item


def test_finalize_escalates_when_worktree_missing(mocker):
    # A gate-passed item whose worktree was pruned/lost must raise a clear,
    # actionable RuntimeError (the orchestrator escalates it), NOT a cryptic
    # `TypeError: expected str... not NoneType` from a None path in subprocess.
    spec = _spec("skrender")
    cfg = _t.SimpleNamespace(repo_map={"skrender": spec}, automerge_repos=[])
    ex, item = _final_ex(mocker, cfg, "skrender")
    ex.journal.worktree_for.return_value = None      # worktree lost
    commit = mocker.patch.object(ex, "_commit_and_push")
    with pytest.raises(RuntimeError, match="worktree.*missing"):
        ex.finalize(item, GateResult(score=5, passed=True, notes="", artifact="pr"))
    commit.assert_not_called()                       # never reaches the None-path subprocess


def test_finalize_automerges_when_whitelisted_and_green(mocker):
    spec = _spec("skrender")
    spec.automerge = True
    spec.ci = "github"
    cfg = _t.SimpleNamespace(repo_map={"skrender": spec}, automerge_repos=["skrender"])
    ex, item = _final_ex(mocker, cfg, "skrender", ci_status="green")
    ex.finalize(item, GateResult(score=5, passed=True, notes="", artifact="pr"))
    ex._gh_merge.assert_called_once()               # merged ON GitHub, not locally
    ex.board.complete_task.assert_called_once_with("autopilot", "t1")
    ex.board._write_task_raw.assert_called_once()   # records meta.autopilot.merge
    ex.digest.queue_decision.assert_not_called()


def test_finalize_carveout_holds_protected_diff(mocker):
    """The drill: a score-5, CI-green diff that touches a guardrail file must be
    HELD for human review, never auto-merged. This is what keeps the operator
    from loosening its own leash."""
    spec = _spec("skrender")
    spec.automerge = True
    spec.ci = "github"
    cfg = _t.SimpleNamespace(repo_map={"skrender": spec}, automerge_repos=["skrender"])
    ex, item = _final_ex(mocker, cfg, "skrender", ci_status="green")
    mocker.patch.object(ex, "_fleet_root", return_value="/nonexistent-fleet-root")
    mocker.patch.object(ex, "_changed_paths", return_value=["src/skcapstone/itil.py"])
    ex.finalize(item, GateResult(score=5, passed=True, notes="", artifact="pr"))
    ex._gh_merge.assert_not_called()                # never merges a guardrail change
    ex._github_checks_verdict.assert_not_called()   # short-circuits before the CI poll
    ex.digest.queue_decision.assert_called_once()   # held for human review
    ex.board.complete_task.assert_not_called()


def test_finalize_carveout_allows_normal_diff(mocker):
    """A normal diff (no guardrail files) still auto-merges: the carve-out does
    not over-block ordinary work."""
    spec = _spec("skrender")
    spec.automerge = True
    spec.ci = "github"
    cfg = _t.SimpleNamespace(repo_map={"skrender": spec}, automerge_repos=["skrender"])
    ex, item = _final_ex(mocker, cfg, "skrender", ci_status="green")
    mocker.patch.object(ex, "_fleet_root", return_value="/nonexistent-fleet-root")
    mocker.patch.object(ex, "_changed_paths", return_value=["src/skcapstone/fleet/cron.py"])
    ex.finalize(item, GateResult(score=5, passed=True, notes="", artifact="pr"))
    ex._gh_merge.assert_called_once()               # ordinary work merges as before


def test_finalize_prunes_worktree_before_merge(mocker):
    """Auto-merge must prune the LOCAL worktree BEFORE `gh pr merge --delete-branch`,
    else deleting the worktree-held branch fails and a successful GitHub merge is
    misread as a failure. Regression: a pi build reported 'held (green)' despite
    the PR merging on GitHub."""
    spec = _spec("skrender")
    spec.automerge = True
    spec.ci = "github"
    cfg = _t.SimpleNamespace(repo_map={"skrender": spec}, automerge_repos=["skrender"])
    ex, item = _final_ex(mocker, cfg, "skrender", ci_status="green")
    order = mocker.Mock()
    order.attach_mock(ex.prune_worktree, "prune")
    order.attach_mock(ex._gh_merge, "merge")
    ex.finalize(item, GateResult(score=5, passed=True, notes="", artifact="pr"))
    seq = [c[0] for c in order.mock_calls]
    assert seq.index("prune") < seq.index("merge")


def test_finalize_pr_only_when_not_whitelisted(mocker):
    spec = _spec("skrender")
    spec.automerge = True
    spec.ci = "github"
    cfg = _t.SimpleNamespace(repo_map={"skrender": spec}, automerge_repos=[])  # not whitelisted
    ex, item = _final_ex(mocker, cfg, "skrender", ci_status="green")
    ex.finalize(item, GateResult(score=5, passed=True, notes="", artifact="pr"))
    ex._gh_merge.assert_not_called()
    ex._github_checks_verdict.assert_not_called()   # no CI poll when not whitelisted
    ex.board.complete_task.assert_not_called()      # left claimed
    ex._commit_and_push.assert_called_once()        # harness edits committed + pushed
    ex._open_pr.assert_called_once()
    ex.digest.queue_decision.assert_called_once()


def test_finalize_holds_when_github_ci_red(mocker):
    spec = _spec("skrender")
    spec.automerge = True
    spec.ci = "github"
    cfg = _t.SimpleNamespace(repo_map={"skrender": spec}, automerge_repos=["skrender"])
    ex, item = _final_ex(mocker, cfg, "skrender", ci_status="red")
    ex.finalize(item, GateResult(score=5, passed=True, notes="", artifact="pr"))
    ex._gh_merge.assert_not_called()                # never merge a red PR
    ex._open_pr.assert_called_once()
    ex.digest.queue_decision.assert_called_once()   # held for review
    ex.board.complete_task.assert_not_called()


def test_finalize_holds_when_security_flagged(mocker):
    # A GitGuardian (or other security-scanner) flag must HOLD the merge for human
    # review, even with the twin gate + core CI green.
    spec = _spec("skrender")
    spec.automerge = True
    spec.ci = "github"
    cfg = _t.SimpleNamespace(repo_map={"skrender": spec}, automerge_repos=["skrender"])
    ex, item = _final_ex(mocker, cfg, "skrender", ci_status="blocked")
    ex.finalize(item, GateResult(score=5, passed=True, notes="", artifact="pr"))
    ex._gh_merge.assert_not_called()
    ex.digest.queue_decision.assert_called_once()
    ex.board.complete_task.assert_not_called()


def _verdict_for(mocker, checks):
    ex = EngineeringExecutor(_t.SimpleNamespace(repo_map={}, automerge_repos=[]),
                             board=mocker.Mock(), journal=mocker.Mock())
    mocker.patch("skharness.autocode.engineering.subprocess.run",
                 return_value=_t.SimpleNamespace(stdout=json.dumps(checks), returncode=0))
    return ex._github_checks_verdict(_spec("skrender"), "b")


def test_github_verdict_green_ignores_release_jobs(mocker):
    # Core gates pass, GitGuardian passes, a failing publish-* release job is IGNORED.
    v = _verdict_for(mocker, [
        {"name": "lint", "bucket": "pass"},
        {"name": "test (3.12)", "bucket": "pass"},
        {"name": "qa (3.11)", "bucket": "pass"},
        {"name": "pytest (py3.12)", "bucket": "pass"},
        {"name": "GitGuardian Security Checks", "bucket": "pass"},
        {"name": "publish-npm", "bucket": "fail"},   # release job, not a quality gate
    ])
    assert v == "green"


def test_github_verdict_red_on_core_failure(mocker):
    v = _verdict_for(mocker, [{"name": "test (3.12)", "bucket": "fail"},
                              {"name": "lint", "bucket": "pass"}])
    assert v == "red"


def test_github_verdict_blocked_on_security_flag(mocker):
    v = _verdict_for(mocker, [{"name": "lint", "bucket": "pass"},
                              {"name": "test (3.12)", "bucket": "pass"},
                              {"name": "GitGuardian Security Checks", "bucket": "fail"}])
    assert v == "blocked"


def _verdict_for_spec(mocker, spec, checks):
    ex = EngineeringExecutor(_t.SimpleNamespace(repo_map={}, automerge_repos=[]),
                             board=mocker.Mock(), journal=mocker.Mock())
    mocker.patch("skharness.autocode.engineering.subprocess.run",
                 return_value=_t.SimpleNamespace(stdout=json.dumps(checks), returncode=0))
    return ex._github_checks_verdict(spec, "b")


def test_github_verdict_advisory_check_does_not_block(mocker):
    # A repo whose GitHub lint job is `continue-on-error` declares advisory_checks=["lint"].
    # A failing lint check must NOT hold the merge when the real gates (tests) are green.
    spec = _spec("skrender")
    spec.advisory_checks = ["lint"]
    v = _verdict_for_spec(mocker, spec, [
        {"name": "lint", "bucket": "fail"},              # advisory -> not a gate
        {"name": "test (3.12)", "bucket": "pass"},
        {"name": "GitGuardian Security Checks", "bucket": "pass"},
    ])
    assert v == "green"


def test_github_verdict_advisory_does_not_relax_real_failures(mocker):
    # advisory_checks=["lint"] must not mask a genuine core-test failure.
    spec = _spec("skrender")
    spec.advisory_checks = ["lint"]
    v = _verdict_for_spec(mocker, spec, [
        {"name": "lint", "bucket": "fail"},
        {"name": "test (3.12)", "bucket": "fail"},        # a real gate failed -> red
    ])
    assert v == "red"


def test_github_verdict_lint_still_gates_by_default(mocker):
    # Without advisory_checks, lint remains a hard gate (strict default preserved).
    v = _verdict_for(mocker, [{"name": "lint", "bucket": "fail"},
                              {"name": "test (3.12)", "bucket": "pass"}])
    assert v == "red"


def test_revert_reverts_sha_and_reopens(mocker):
    spec = _spec("skrender")
    cfg = _t.SimpleNamespace(repo_map={"skrender": spec}, automerge_repos=[])
    task = _t.SimpleNamespace(id="t1", tags=["repo:skrender"],
                              meta={"autopilot": {"merge": {"sha": "mergesha"}}})
    agent = _t.SimpleNamespace(agent="autopilot", completed_tasks=["t1", "t9"])
    board = mocker.Mock()
    board.load_tasks.return_value = [task]
    board.load_agent.return_value = agent
    run = mocker.patch("skharness.autocode.engineering.subprocess.run",
                       return_value=mocker.Mock(returncode=0, stdout="", stderr=""))
    out = _revert_impl(board, cfg, "t1")
    argvs = [c.args[0] for c in run.call_args_list]
    assert ["git", "-C", "/repos/skrender", "revert", "--no-edit", "mergesha"] in argvs
    # reopened: dropped from the agent's completed_tasks and saved
    assert "t1" not in agent.completed_tasks and "t9" in agent.completed_tasks
    board.save_agent.assert_called_once_with(agent)
    board._write_task_raw.assert_called_once()   # records meta.autopilot.reverted
    # Minor1: _revert_impl returns a result dict, not None
    assert out == {"task_id": "t1", "reverted_sha": "mergesha", "reopened": True}


def test_revert_raises_without_recorded_merge(mocker):
    task = _t.SimpleNamespace(id="t1", tags=["repo:skrender"], meta={"autopilot": {}})
    board = mocker.Mock()
    board.load_tasks.return_value = [task]
    cfg = _t.SimpleNamespace(repo_map={"skrender": _spec("skrender")}, automerge_repos=[])
    with pytest.raises(ValueError):
        _revert_impl(board, cfg, "t1")


# ── _diff must include NEW/untracked files (the harness writes tests but never
# `git add`s them; a plain `git diff` omitted them, so grade/CI/coverage saw no
# tests and the twin gate could never pass a TDD change) ──────────────────────

def _git(wt, *args):
    import subprocess
    return subprocess.run(["git", "-C", str(wt), *args], capture_output=True, text=True)


def test_diff_includes_untracked_new_files_and_excludes_coverage_byproducts(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "a@b.c")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "src.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    # harness edits: modify a tracked file + write a NEW untracked test + a byproduct
    (tmp_path / "src.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    (tmp_path / "test_new.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    (tmp_path / "coverage.xml").write_text("<coverage/>\n", encoding="utf-8")

    ex = EngineeringExecutor(_t.SimpleNamespace(repo_map={}, automerge_repos=[]),
                             board=object(), journal=object())
    diff = ex._diff(_spec("r").__class__(name="r", path=str(tmp_path), base_branch="main",
                     integration_branch="develop", test_cmd="pytest", ci="none"), str(tmp_path))
    assert "test_new.py" in diff          # the untracked new test IS visible now
    assert "+y = 2" in diff               # the tracked modification too
    assert "coverage.xml" not in diff     # CI/coverage byproducts stay excluded


# ── _pr_base: fall back to base_branch when integration_branch doesn't exist on
# origin (a missing integration branch made gh pr create fail AFTER push, silently
# eating the PR) ──────────────────────────────────────────────────────────────

def _pr_spec():
    return RepoSpec(name="skchat", path="/repos/skchat", base_branch="main",
                    integration_branch="autopilot/integration", test_cmd="pytest", ci="none")


def test_pr_base_falls_back_to_base_when_integration_branch_absent(mocker):
    ex = EngineeringExecutor(_t.SimpleNamespace(repo_map={}, automerge_repos=[]),
                             board=object(), journal=object())
    import subprocess
    mocker.patch("skharness.autocode.engineering.subprocess.run",
                 return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""))
    assert ex._pr_base(_pr_spec()) == "main"        # integration branch absent -> base


def test_pr_base_uses_integration_branch_when_it_exists(mocker):
    ex = EngineeringExecutor(_t.SimpleNamespace(repo_map={}, automerge_repos=[]),
                             board=object(), journal=object())
    import subprocess
    mocker.patch("skharness.autocode.engineering.subprocess.run",
                 return_value=subprocess.CompletedProcess(
                     args=[], returncode=0,
                     stdout="abc123\trefs/heads/autopilot/integration\n", stderr=""))
    assert ex._pr_base(_pr_spec()) == "autopilot/integration"
