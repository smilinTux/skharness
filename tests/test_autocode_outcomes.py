"""S2 conformance: every terminal return carries a REAL terminal state.

GateResult.outcome defaults to UNRECORDED, which means "no terminal state was
recorded", never "this succeeded". This module pins that every terminal return
replaces that sentinel with a value from the closed five-value vocabulary, and
that the five sites produce five DISTINCT values.

The distinctness assertion is the load-bearing one. "outcome is populated" is
satisfied by a single constant, which would silently destroy the vocabulary this
epic exists to preserve, and a suite that cannot tell a vocabulary from a
constant carries no information about either.

Every outcome below is read off the WIRED path (executor.run / ratify), never
hand-injected onto a GateResult, so a field the live path never actually sets
cannot pass here.

No em/en dashes anywhere (SKWorld hard rule).
"""
import types as _t

import pytest

from skharness.autocode import ratify
from skharness.autocode.direct import DirectExecutor
from skharness.autocode.engineering import EngineeringExecutor
from skharness.autocode.types import (GATE_OUTCOMES, UNRECORDED, GateResult,
                                      HarnessResult, RepoSpec, WorkItem)


def _spec(name="skrender"):
    return RepoSpec(name=name, path=f"/repos/{name}", base_branch="main",
                    integration_branch="develop", test_cmd="pytest", ci="none")


@pytest.fixture
def cfg():
    return _t.SimpleNamespace(repo_map={"skrender": _spec()}, automerge_repos=[])


def _item():
    return WorkItem(kind="engineering", ref="t1", source="coord", repo=None,
                    payload={"tags": ["repo:skrender"], "title": "t",
                             "description": "d", "acceptance": ["a"],
                             "unblocked": True, "verdict": "valid"})


def _executor(mocker, cfg, cls=EngineeringExecutor, ci_status="green", cov=0.95):
    """A real executor with only the git/CI/coverage edges faked, so run() takes
    its genuine control flow to a genuine terminal return."""
    ex = cls(cfg, board=mocker.Mock(), journal=mocker.Mock(), digest=mocker.Mock(),
             agent_name="autopilot")
    mocker.patch.object(ex, "make_worktree", return_value="/wt/t1")
    mocker.patch.object(ex, "_diff", return_value="DIFF")
    mocker.patch.object(ex, "_head_sha", return_value="sha1")
    mocker.patch("skharness.autocode.engineering.external_ci_verdict",
                 return_value=ci_status)
    mocker.patch("skharness.autocode.engineering.diff_coverage", return_value=cov)
    harness = mocker.Mock(name="harness")
    harness.name = "pi"
    harness.run_task.return_value = HarnessResult(ok=True, artifact=None, tokens=7,
                                                  cost_usd=0.02, raw={})
    return ex, harness


def _grade(score, notes=""):
    return GateResult(score=score, passed=False, notes=notes, artifact="pr")


# --------------------------------------------------------------------------- #
# The five wired sites, one per terminal state                                #
# --------------------------------------------------------------------------- #

def _outcome_no_op(mocker, cfg):
    ex, harness = _executor(mocker, cfg)
    mocker.patch.object(ex, "_diff", return_value="")        # no diff, twice
    return ex.run(_item(), harness)


def _outcome_pass(mocker, cfg):
    ex, harness = _executor(mocker, cfg)
    harness.grade.side_effect = [_grade(5, "ok <promise>COMPLETE</promise>")]
    return ex.run(_item(), harness)


def _outcome_salvage(mocker, cfg):
    ex, harness = _executor(mocker, cfg)
    harness.grade.side_effect = [_grade(None)] * 6           # inconclusive + CI green
    mocker.patch.object(ex, "_salvage_to_review", return_value="https://gh/pr/9")
    return ex.run(_item(), harness)


def _outcome_ci_red(mocker, cfg):
    ex, harness = _executor(mocker, cfg)
    harness.grade.side_effect = [_grade(4, "one gap")] * 6   # never closes the gate
    return ex.run(_item(), harness)


def _outcome_direct_pass(mocker, cfg):
    ex, harness = _executor(mocker, cfg, cls=DirectExecutor)
    return ex.run(_item(), harness)


def _outcome_direct_fail(mocker, cfg):
    ex, harness = _executor(mocker, cfg, cls=DirectExecutor)
    harness.run_task.return_value = HarnessResult(ok=False, artifact=None, tokens=7,
                                                  cost_usd=0.02, raw={})
    return ex.run(_item(), harness)


_SITES = {
    "no_op": _outcome_no_op,
    "pass": _outcome_pass,
    "salvage": _outcome_salvage,
    "ci_red": _outcome_ci_red,
    "direct_fail": _outcome_direct_fail,
}


@pytest.mark.parametrize("expected,drive", sorted(_SITES.items()))
def test_each_terminal_return_carries_its_own_outcome(mocker, cfg, expected, drive):
    res = drive(mocker, cfg)
    assert res.outcome == expected
    assert res.outcome in GATE_OUTCOMES
    assert res.outcome != UNRECORDED       # the sentinel must not survive a terminal


def test_the_five_terminal_sites_produce_five_DISTINCT_outcomes(mocker, cfg):
    """The load-bearing assertion. A single constant satisfies "outcome is
    populated" while destroying the vocabulary, so assert the sites do not
    collapse into each other. If any two ever agree, this goes red."""
    outcomes = [drive(mocker, cfg).outcome for _name, drive in sorted(_SITES.items())]
    assert len(outcomes) == 5
    assert len(set(outcomes)) == 5, f"terminal states collapsed: {outcomes}"
    assert set(outcomes) == GATE_OUTCOMES   # exactly the closed vocabulary, no more


def test_direct_pass_and_direct_fail_are_not_the_same_state(mocker, cfg):
    """The direct executor is the one site with two outcomes, so its two branches
    must be distinguishable: an ungated run that FAILED must never read 'pass'."""
    assert _outcome_direct_pass(mocker, cfg).outcome == "pass"
    assert _outcome_direct_fail(mocker, cfg).outcome == "direct_fail"


# --------------------------------------------------------------------------- #
# tokens + cost_usd: the FAILURE paths are the censored half                   #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name,rounds", [("no_op", 2), ("salvage", 1), ("ci_red", 4)])
def test_failure_paths_carry_their_real_tokens_and_cost(mocker, cfg, name, rounds):
    """A pass recording its cost was never the problem. These three are, and a
    zero here is indistinguishable from a run that cost nothing."""
    res = _SITES[name](mocker, cfg)
    assert res.tokens == 7 * rounds
    assert res.cost_usd == pytest.approx(0.02 * rounds)


def test_pass_path_carries_its_tokens_without_taking_them_off_the_books(mocker, cfg):
    """The pass return must READ the accrued usage, not take it: finalize's
    _settle_economics is the one path allowed to pop it and mint."""
    ex, harness = _executor(mocker, cfg)
    harness.grade.side_effect = [_grade(5, "ok <promise>COMPLETE</promise>")]
    res = ex.run(_item(), harness)
    assert res.tokens == 7 and res.cost_usd == pytest.approx(0.02)
    assert ex._build_usage["t1"].tokens == 7      # still on the books for settle


def test_direct_returns_carry_the_runs_real_tokens_and_cost(mocker, cfg):
    for drive in (_outcome_direct_pass, _outcome_direct_fail):
        res = drive(mocker, cfg)
        assert res.tokens == 7 and res.cost_usd == pytest.approx(0.02)


def test_capledger_reads_a_non_zero_number_off_a_real_result(mocker, cfg):
    """orchestrator.py feeds the CapLedger via getattr(result, "tokens", 0), which
    has been adding zero on every path because GateResult never carried the
    field. Pin the value the ledger actually reads, off a wired result."""
    res = _outcome_ci_red(mocker, cfg)
    assert getattr(res, "tokens", 0) == 28
    assert getattr(res, "cost_usd", 0.0) == pytest.approx(0.08)


# --------------------------------------------------------------------------- #
# ratify: not an orchestrator path, but the field is not optional              #
# --------------------------------------------------------------------------- #

class _FakeHarness:
    name = "fake"

    def __init__(self, gr):
        self._gr = gr

    def grade(self, brief):
        return self._gr


@pytest.fixture
def _no_git(mocker):
    def fake_run(argv, *a, **k):
        return _t.SimpleNamespace(stdout="", stderr="", returncode=0)

    mocker.patch("skharness.autocode.engineering.subprocess.run", side_effect=fake_run)
    mocker.patch("skharness.autocode.ratify.subprocess.run", side_effect=fake_run)


def _ratify(mocker, grade, ci_status, cov):
    mocker.patch("skharness.autocode.ratify.external_ci_verdict", return_value=ci_status)
    mocker.patch("skharness.autocode.ratify.diff_coverage", return_value=cov)
    return ratify(_spec(), "/wt/existing", ["a"], _FakeHarness(grade))


def test_ratify_pass_carries_an_outcome(mocker, _no_git):
    res = _ratify(mocker, _grade(5, "ok <promise>COMPLETE</promise>"), "green", 0.95)
    assert res.passed is True and res.outcome == "pass"


def test_ratify_non_pass_carries_an_outcome(mocker, _no_git):
    """ratify writes no outcome row (skcode calls it, not the orchestrator), but
    it MUST still populate the field or the field is optional in practice, which
    is how a vocabulary rots."""
    res = _ratify(mocker, _grade(3, "thin"), "green", 0.95)
    assert res.passed is False
    assert res.outcome in GATE_OUTCOMES and res.outcome != UNRECORDED
