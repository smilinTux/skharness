"""ratify one-shot: the twin-gated grade over an EXISTING worktree, NEVER merges.

Drives the real ratify() composition with fake CI/coverage and a fake grader;
records subprocess to prove NO git merge/commit/push runs (ratify grades only).
The pass/fail matrix mirrors the crown-jewel twin gate arm-for-arm.
"""
import types as _t

import pytest

from skharness.autocode import ratify
from skharness.autocode.types import GateResult, RepoSpec


def _spec(name="skrender"):
    # ci="none" keeps the CI twin out of subprocess; min_diff_coverage defaults 0.8.
    return RepoSpec(name=name, path=f"/repos/{name}", base_branch="main",
                    integration_branch="develop", test_cmd="pytest", ci="none")


class _FakeHarness:
    name = "fake"

    def __init__(self, grade_result):
        self._gr = grade_result
        self.graded = None

    def grade(self, brief):
        self.graded = brief
        return self._gr


def _five_complete():
    return GateResult(score=5, passed=True,
                      notes="ready <promise>COMPLETE</promise>", artifact="pr")


@pytest.fixture
def rec(mocker):
    """Record every git subprocess the ratify path shells out, returning empty
    stdout, so no real git runs and we can assert the recorded argv never merges."""
    calls = []

    def fake_run(argv, *a, **k):
        calls.append(list(argv))
        return _t.SimpleNamespace(stdout="", stderr="", returncode=0)

    mocker.patch("skharness.autocode.engineering.subprocess.run", side_effect=fake_run)
    mocker.patch("skharness.autocode.ratify.subprocess.run", side_effect=fake_run)
    return calls


def _run(mocker, grade, ci_status, cov, acceptance=None):
    mocker.patch("skharness.autocode.ratify.external_ci_verdict", return_value=ci_status)
    mocker.patch("skharness.autocode.ratify.diff_coverage", return_value=cov)
    h = _FakeHarness(grade)
    res = ratify(_spec(), "/wt/existing", acceptance or ["a"], h)
    return res, h


# ---- pass/fail matrix mirrors the twin gate, arm for arm ---------------------

def test_ratify_passes_only_when_all_arms_green(mocker, rec):
    res, _ = _run(mocker, _five_complete(), ci_status="green", cov=0.95)
    assert res.passed is True and res.score == 5
    assert "<promise>" not in res.notes          # promise token stripped from notes


def test_ratify_fails_when_score_not_five(mocker, rec):
    res, _ = _run(mocker, GateResult(score=4, passed=False,
                  notes="gap <promise>COMPLETE</promise>", artifact=None),
                  ci_status="green", cov=0.95)
    assert res.passed is False


def test_ratify_fails_when_promise_incomplete(mocker, rec):
    res, _ = _run(mocker, GateResult(score=5, passed=False,
                  notes="looks good, no promise", artifact=None),
                  ci_status="green", cov=0.95)
    assert res.passed is False


def test_ratify_fails_when_ci_not_green(mocker, rec):
    res, _ = _run(mocker, _five_complete(), ci_status="red", cov=0.95)
    assert res.passed is False


def test_ratify_fails_when_coverage_below_floor(mocker, rec):
    res, _ = _run(mocker, _five_complete(), ci_status="green", cov=0.5)   # < 0.8
    assert res.passed is False


def test_ratify_fails_when_coverage_none(mocker, rec):
    res, _ = _run(mocker, _five_complete(), ci_status="green", cov=None)
    assert res.passed is False


# ---- ratify GRADES ONLY: never merges, commits, or pushes -------------------

def test_ratify_never_merges_commits_or_pushes(mocker, rec):
    _run(mocker, _five_complete(), ci_status="green", cov=0.95)
    joined = [" ".join(c) for c in rec]
    assert rec, "expected ratify to stage/diff via git"
    assert not any("merge" in c for c in joined)
    assert not any("commit" in c for c in joined)
    assert not any("push" in c for c in joined)
    assert not any("checkout" in c for c in joined)


def test_ratify_passes_acceptance_and_twin_verdicts_to_grader(mocker, rec):
    _, h = _run(mocker, _five_complete(), ci_status="green", cov=0.95,
                acceptance=["must foo", "must bar"])
    assert h.graded.acceptance == ["must foo", "must bar"]
    assert h.graded.ci_status == "green"
    assert h.graded.diff_coverage == 0.95
