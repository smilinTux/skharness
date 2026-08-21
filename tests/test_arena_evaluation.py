import pytest

from skharness.arena.evaluation import (
    EvaluationError,
    Plane,
    PlaneBinding,
    TraceStep,
    Trial,
    Variant,
    authorize_training_promotion,
    evaluation_report,
    export_rl_trace,
)


def matrix():
    return [Trial(v, seed, rep, .8, .02, 100, 50, True)
            for v in Variant for seed in (7, 11) for rep in range(2)]


def bindings():
    return [PlaneBinding(Plane.TRAIN, "train-v1", "cred-train"),
            PlaneBinding(Plane.EVAL, "heldout-v1", "cred-eval"),
            PlaneBinding(Plane.PROD, "prod-v1", "cred-prod")]


def test_complete_fixed_seed_matrix_reports_every_required_measure():
    report = evaluation_report(matrix(), seeds=(7, 11), repetitions=2)
    assert set(report["variants"]) == {v.value for v in Variant}
    assert set(report["variants"]["full-loop"]) == {
        "trials", "quality_mean", "cost_usd_mean", "tokens_mean",
        "latency_ms_mean", "recovery_rate", "reward_hacks", "verifier_disagreements",
    }


def test_partial_or_duplicate_matrix_is_rejected():
    rows = matrix()
    with pytest.raises(EvaluationError, match="incomplete"):
        evaluation_report(rows[:-1], seeds=(7, 11), repetitions=2)
    with pytest.raises(EvaluationError, match="duplicate"):
        evaluation_report(rows + [rows[0]], seeds=(7, 11), repetitions=2)


def test_trace_export_preserves_exact_fields_and_separates_training_use():
    out = export_rl_trace([
        TraceStep("assistant", (1, 2), (-.1, -.2), {"temperature": 0.2}, "ornith"),
        TraceStep("tool-response", (3,), (-.3,), {"temperature": 0.2}, "ornith"),
    ])
    assert out["steps"][0]["training_use"] == "rl"
    assert out["steps"][1]["training_use"] == "world-model-sft"
    assert out["steps"][0]["token_ids"] == (1, 2)


def test_malformed_trace_is_rejected():
    with pytest.raises(EvaluationError):
        export_rl_trace([TraceStep("assistant", (1,), (), {}, "")])


def test_promotion_requires_disjoint_planes_regression_canary_and_approval():
    out = authorize_training_promotion(bindings(), held_out_regression_passed=True,
                                       canary_passed=True, explicit_approval="operator:chef",
                                       reward_hacks=0)
    assert out["authorized"] is True


@pytest.mark.parametrize("change,match", [
    ({"held_out_regression_passed": False}, "regression"),
    ({"canary_passed": False}, "canary"),
    ({"explicit_approval": None}, "approval"),
    ({"reward_hacks": 1}, "reward-hack"),
])
def test_each_slow_gate_fails_closed(change, match):
    kwargs = dict(held_out_regression_passed=True, canary_passed=True,
                  explicit_approval="operator:chef", reward_hacks=0)
    kwargs.update(change)
    with pytest.raises(EvaluationError, match=match):
        authorize_training_promotion(bindings(), **kwargs)


def test_shared_dataset_or_credential_is_rejected():
    rows = bindings()
    rows[1] = PlaneBinding(Plane.EVAL, "train-v1", "cred-eval")
    with pytest.raises(EvaluationError, match="datasets"):
        authorize_training_promotion(rows, held_out_regression_passed=True,
                                     canary_passed=True, explicit_approval="operator:chef",
                                     reward_hacks=0)
