import pytest

from skharness.autocode.types import (
    WorkItem,
    RepoSpec,
    AssessBrief,
    TaskBrief,
    GradeBrief,
    GateResult,
    GATE_OUTCOMES,
    UNRECORDED,
    Verdict,
    HarnessResult,
    HarnessProvenanceReason,
    DecisionItem,
)


def test_repospec_defaults():
    r = RepoSpec(
        name="skos",
        path="/x",
        base_branch="main",
        integration_branch="autopilot",
        test_cmd="pytest -q",
        ci="github-actions",
    )
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
    ab = AssessBrief(
        task_id="t1",
        title="T",
        description="d",
        acceptance=["a"],
        tags=["repo:skos"],
        repo="skos",
        codebase_context="ctx",
    )
    tb = TaskBrief(
        task_id="t1",
        repo=rs,
        worktree="/wt",
        title="T",
        description="d",
        acceptance=["a"],
        prior_feedback=None,
        round=1,
    )
    gb = GradeBrief(
        task_id="t1",
        repo=rs,
        worktree="/wt",
        diff="+x",
        acceptance=["a"],
        ci_status="green",
        diff_coverage=0.9,
    )
    gr = GateResult(score=5, passed=True, notes="", artifact=None)
    hr = HarnessResult(ok=True, artifact="/wt", tokens=10, cost_usd=0.01, raw={})
    di = DecisionItem(qid="q1", prompt="?", options={"yes": 1}, action_ref=None, priority="high")
    assert wi.repo == "skos" and gr.passed and hr.tokens == 10 and di.qid == "q1"
    assert ab.tags == ["repo:skos"] and tb.round == 1 and gb.ci_status == "green"
    assert hr.model_requested is None and hr.model_served is None
    assert hr.backend_served is None and hr.gateway_req_id is None
    assert hr.model_served_reason is None
    assert hr.backend_served_reason is None and hr.gateway_req_id_reason is None


def test_harness_result_provenance_reasons_are_closed_and_field_specific():
    base = dict(ok=True, artifact=None, tokens=0, cost_usd=0.0, raw={})
    result = HarnessResult(
        **base,
        model_served_reason=(HarnessProvenanceReason.MODEL_SERVED_NOT_OBSERVED),
    )
    assert result.model_served_reason is HarnessProvenanceReason.MODEL_SERVED_NOT_OBSERVED
    for reason in (
        HarnessProvenanceReason.MODEL_SERVED_PARTIAL,
        HarnessProvenanceReason.MODEL_SERVED_CONFLICT,
        HarnessProvenanceReason.MODEL_SERVED_INCOMPLETE_STREAM,
    ):
        assert HarnessResult(**base, model_served_reason=reason).model_served_reason is reason

    with pytest.raises(ValueError, match="not a recognized provenance reason"):
        HarnessResult(**base, model_served_reason="assistant says probably local")
    with pytest.raises(ValueError, match="model_served_reason cannot use"):
        HarnessResult(
            **base,
            model_served_reason=(HarnessProvenanceReason.BACKEND_SERVED_NOT_OBSERVED),
        )
    with pytest.raises(ValueError, match="requires model_served to be None"):
        HarnessResult(
            **base,
            model_served="served-model",
            model_served_reason=(HarnessProvenanceReason.MODEL_SERVED_NOT_OBSERVED),
        )


def test_gateresult_outcome_vocabulary_is_exactly_five_values():
    # Load-bearing: this set is closed. If a sixth value is added to GATE_OUTCOMES
    # without updating this test, this assertion fails.
    assert GATE_OUTCOMES == {"pass", "ci_red", "no_op", "salvage", "direct_fail"}
    assert len(GATE_OUTCOMES) == 5


def test_gateresult_outcome_and_cost_fields_have_safe_defaults():
    # Existing construction sites across the repo do not pass outcome/tokens/cost_usd.
    # They must keep constructing without error, with tokens/cost_usd defaulting to 0/0.0.
    gr = GateResult(score=5, passed=True, notes="", artifact=None)
    assert gr.outcome == UNRECORDED
    assert gr.tokens == 0
    assert gr.cost_usd == 0.0


def test_unrecorded_is_not_in_gate_outcomes():
    # UNRECORDED means "no terminal state was recorded". It must never be mistaken
    # for a member of the closed five-value terminal-state vocabulary.
    assert UNRECORDED not in GATE_OUTCOMES


def test_gateresult_default_outcome_does_not_report_pass():
    # A default-constructed GateResult (no outcome passed) must NOT read as a success.
    # If anyone ever changes the default back to "pass", this test goes red.
    gr = GateResult(score=5, passed=True, notes="", artifact=None)
    assert gr.outcome != "pass"
    assert gr.outcome == UNRECORDED


def test_gateresult_accepts_each_vocabulary_value():
    for outcome in GATE_OUTCOMES:
        gr = GateResult(
            score=5, passed=True, notes="", artifact=None, outcome=outcome, tokens=42, cost_usd=1.5
        )
        assert gr.outcome == outcome
        assert gr.tokens == 42
        assert gr.cost_usd == 1.5


def test_gateresult_rejects_invalid_outcome():
    # Negative control: an outcome outside the closed vocabulary must be rejected,
    # not silently accepted.
    with pytest.raises(ValueError):
        GateResult(score=5, passed=True, notes="", artifact=None, outcome="totally_bogus")
