import pytest

from skharness.autocode.types import (
    WorkItem, RepoSpec, AssessBrief, TaskBrief, GradeBrief,
    GateResult, GATE_OUTCOMES, Verdict, HarnessResult, DecisionItem,
)


def test_repospec_defaults():
    r = RepoSpec(name="skos", path="/x", base_branch="main",
                 integration_branch="autopilot", test_cmd="pytest -q",
                 ci="github-actions")
    assert r.coverage_cmd is None
    assert r.ci_poll_timeout == 1200
    assert r.automerge is False and r.auto_revert is False
    assert r.min_diff_coverage == 0.8


def test_verdict_optional_fields_default_none():
    v = Verdict(verdict="valid", reason="ok")
    assert v.updated_description is None and v.updated_acceptance is None


def test_all_contracts_construct():
    wi = WorkItem(kind="engineering", ref="t1", source="coord", repo="skos", payload={})
    rs = RepoSpec("skos", "/x", "main", "ap", "pytest", "none")
    ab = AssessBrief(task_id="t1", title="T", description="d", acceptance=["a"],
                     tags=["repo:skos"], repo="skos", codebase_context="ctx")
    tb = TaskBrief(task_id="t1", repo=rs, worktree="/wt", title="T", description="d",
                   acceptance=["a"], prior_feedback=None, round=1)
    gb = GradeBrief(task_id="t1", repo=rs, worktree="/wt", diff="+x",
                    acceptance=["a"], ci_status="green", diff_coverage=0.9)
    gr = GateResult(score=5, passed=True, notes="", artifact=None)
    hr = HarnessResult(ok=True, artifact="/wt", tokens=10, cost_usd=0.01, raw={})
    di = DecisionItem(qid="q1", prompt="?", options={"yes": 1}, action_ref=None, priority="high")
    assert wi.repo == "skos" and gr.passed and hr.tokens == 10 and di.qid == "q1"
    assert ab.tags == ["repo:skos"] and tb.round == 1 and gb.ci_status == "green"


def test_gateresult_outcome_vocabulary_is_exactly_five_values():
    # Load-bearing: this set is closed. If a sixth value is added to GATE_OUTCOMES
    # without updating this test, this assertion fails.
    assert GATE_OUTCOMES == {"pass", "ci_red", "no_op", "salvage", "direct_fail"}
    assert len(GATE_OUTCOMES) == 5


def test_gateresult_outcome_and_cost_fields_have_safe_defaults():
    # Existing construction sites across the repo do not pass outcome/tokens/cost_usd.
    # They must keep constructing without error, with tokens/cost_usd defaulting to 0/0.0.
    gr = GateResult(score=5, passed=True, notes="", artifact=None)
    assert gr.outcome in GATE_OUTCOMES
    assert gr.tokens == 0
    assert gr.cost_usd == 0.0


def test_gateresult_accepts_each_vocabulary_value():
    for outcome in GATE_OUTCOMES:
        gr = GateResult(score=5, passed=True, notes="", artifact=None, outcome=outcome,
                         tokens=42, cost_usd=1.5)
        assert gr.outcome == outcome
        assert gr.tokens == 42
        assert gr.cost_usd == 1.5


def test_gateresult_rejects_invalid_outcome():
    # Negative control: an outcome outside the closed vocabulary must be rejected,
    # not silently accepted.
    with pytest.raises(ValueError):
        GateResult(score=5, passed=True, notes="", artifact=None, outcome="totally_bogus")
