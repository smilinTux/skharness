"""Twin-gate conformance: pins the crown-jewel predicate forever.

The autocode quality gate in `skharness.autocode.engineering.EngineeringExecutor.run`
passes a task ONLY when ALL FOUR arms hold on the same round:

    gr.score == 5
    AND is_complete(gr.notes)                 # independent grader promise token
    AND ci_status == "green"                  # external CI twin
    AND cov is not None AND cov >= repo.min_diff_coverage

This test drives the REAL executor loop (no reimplementation of the boolean) and
asserts the gate passes only on the all-arms-green case and fails when any single
arm is missing. Nothing in the extraction may weaken this gate; if a change makes
this test fail, the change is wrong.
"""
import types as _t

import pytest

from skharness.autocode.engineering import EngineeringExecutor, is_complete
from skharness.autocode.types import GateResult, HarnessResult, RepoSpec, WorkItem


def _spec(name="skrender"):
    # min_diff_coverage defaults to 0.8; ci="none" keeps finalize out of the picture.
    return RepoSpec(name=name, path=f"/repos/{name}", base_branch="main",
                    integration_branch="develop", test_cmd="pytest", ci="none")


@pytest.fixture
def cfg():
    return _t.SimpleNamespace(repo_map={"skrender": _spec()}, automerge_repos=[])


def _drive(mocker, cfg, grades, ci_status, cov):
    """Run the REAL EngineeringExecutor.run gate with the CI/coverage twins mocked
    to fixed values and the grader returning the supplied GateResult(s)."""
    ex = EngineeringExecutor(cfg, board=mocker.Mock(), journal=mocker.Mock())
    mocker.patch.object(ex, "make_worktree", return_value="/wt/t1")
    mocker.patch.object(ex, "prune_worktree")
    mocker.patch.object(ex, "_diff", return_value="DIFF")
    mocker.patch.object(ex, "_head_sha", return_value="sha1")
    mocker.patch("skharness.autocode.engineering.external_ci_verdict",
                 return_value=ci_status)
    mocker.patch("skharness.autocode.engineering.diff_coverage", return_value=cov)
    harness = mocker.Mock(name="harness")
    harness.name = "claude-code"
    harness.run_task.return_value = HarnessResult(ok=True, artifact=None, tokens=1,
                                                  cost_usd=0.0, raw={})
    harness.grade.side_effect = grades
    item = WorkItem(kind="engineering", ref="t1", source="coord", repo=None,
                    payload={"tags": ["repo:skrender"], "title": "t",
                             "description": "d", "acceptance": ["a"]})
    return ex.run(item, harness)


def _five_complete():
    return GateResult(score=5, passed=True,
                      notes="ready <promise>COMPLETE</promise>", artifact="pr")


# ---- all four arms green: the ONLY configuration that passes -----------------

def test_gate_passes_only_when_all_four_arms_green(mocker, cfg):
    res = _drive(mocker, cfg, [_five_complete()], ci_status="green", cov=0.95)
    assert res.passed is True and res.score == 5


# ---- each arm removed in isolation must FAIL the gate ------------------------

def test_gate_fails_when_score_is_four(mocker, cfg):
    grades = [GateResult(score=4, passed=False,
                         notes="one gap <promise>COMPLETE</promise>", artifact=None)] * 4
    res = _drive(mocker, cfg, grades, ci_status="green", cov=0.95)
    assert res.passed is False


def test_gate_fails_when_promise_incomplete(mocker, cfg):
    # score 5, CI green, coverage fine, but NO COMPLETE promise token in notes.
    grades = [GateResult(score=5, passed=False, notes="looks good, no promise",
                         artifact=None)] * 4
    res = _drive(mocker, cfg, grades, ci_status="green", cov=0.95)
    assert res.passed is False


def test_gate_fails_when_ci_not_green(mocker, cfg):
    grades = [_five_complete() for _ in range(4)]
    res = _drive(mocker, cfg, grades, ci_status="red", cov=0.95)
    assert res.passed is False


def test_gate_fails_when_coverage_below_threshold(mocker, cfg):
    grades = [_five_complete() for _ in range(4)]
    res = _drive(mocker, cfg, grades, ci_status="green", cov=0.5)  # < 0.8 default
    assert res.passed is False


def test_gate_fails_when_coverage_is_none(mocker, cfg):
    grades = [_five_complete() for _ in range(4)]
    res = _drive(mocker, cfg, grades, ci_status="green", cov=None)
    assert res.passed is False


# ---- the promise-token helper the gate ANDs in, pinned directly --------------

def test_is_complete_requires_exact_promise_token():
    assert is_complete("<promise>COMPLETE</promise>") is True
    assert is_complete("all done, definitely COMPLETE") is False   # prose is not a token
    assert is_complete("<promise>WORKING</promise>") is False       # wrong signal
    assert is_complete(None) is False
