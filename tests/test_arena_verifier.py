from __future__ import annotations

from dataclasses import fields

import pytest

from skharness.arena.metrics import MetricDirection, MetricObjective
from skharness.arena.verifier import (
    ControlKind,
    IndependentVerifier,
    PrivateEvaluationHandle,
    ProvisionalSubmission,
    TrialEvidence,
    VerificationPolicy,
    VerificationStatus,
)

PRIVATE = PrivateEvaluationHandle("hidden-suite", "v3", "secret-capability")
SUBMISSION = ProvisionalSubmission(
    experiment_id="exp-1",
    challenge_hash="sha256:challenge",
    artifact_digest="sha256:artifact",
    requested_model_digest="sha256:model",
    claimed_metrics={"throughput": 999999999.0},
)
POLICY = VerificationPolicy(
    challenge_hash="sha256:challenge",
    expected_model_digest="sha256:model",
    repetitions=3,
    objectives=(
        MetricObjective("throughput", MetricDirection.MAXIMIZE, minimum=90),
        MetricObjective("quality", MetricDirection.MAXIMIZE, minimum=0.8),
    ),
    required_modalities=frozenset({"text", "image"}),
)


def evidence(**overrides):
    values = {
        "metrics": {"throughput": 100.0, "quality": 0.9},
        "served_model_digest": "sha256:model",
        "artifact_digest": "sha256:artifact",
        "modalities_exercised": frozenset({"text", "image"}),
    }
    values.update(overrides)
    return TrialEvidence(**values)


class Backend:
    def __init__(self, trials=None, controls=None):
        self.trials = list(trials or [evidence(), evidence(), evidence()])
        self.controls = controls or {
            ControlKind.GOLD: True,
            ControlKind.NO_OP: False,
            ControlKind.ADVERSARIAL: False,
        }
        self.handles = []

    def run_trial(self, submission, private_evaluation, trial_index):
        self.handles.append(private_evaluation)
        return self.trials[trial_index]

    def run_control(self, control, private_evaluation):
        self.handles.append(private_evaluation)
        return self.controls[control]


def test_valid_verdict_uses_repeated_observations_not_claimed_score():
    backend = Backend(
        [
            evidence(metrics={"throughput": 90.0, "quality": 0.9}),
            evidence(metrics={"throughput": 100.0, "quality": 0.9}),
            evidence(metrics={"throughput": 110.0, "quality": 0.9}),
        ]
    )

    verdict = IndependentVerifier(backend).verify(SUBMISSION, POLICY, PRIVATE)

    assert verdict.status is VerificationStatus.VALID
    assert verdict.summaries["throughput"].mean == 100.0
    assert verdict.summaries["throughput"].count == 3
    assert verdict.private_evaluation_id == "hidden-suite"
    assert all(handle is PRIVATE for handle in backend.handles)


def test_worker_submission_contract_has_no_private_material_field():
    names = {item.name for item in fields(ProvisionalSubmission)}
    assert "private_evaluation" not in names
    assert "withheld_ref" not in names
    assert "_capability" not in repr(PRIVATE)


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"served_model_digest": "sha256:other"}, "served_model_mismatch"),
        ({"artifact_digest": "sha256:other"}, "artifact_digest_mismatch"),
        ({"completed_work": False}, "skipped_work"),
        ({"output_truncated": True}, "output_truncated"),
        ({"cache_disclosed": False}, "cache_undisclosed"),
        ({"modalities_exercised": frozenset({"text"})}, "required_modality_missing"),
    ],
)
def test_gaming_signals_make_submission_invalid(override, reason):
    backend = Backend([evidence(**override)] * 3)
    verdict = IndependentVerifier(backend).verify(SUBMISSION, POLICY, PRIVATE)
    assert verdict.status is VerificationStatus.INVALID
    assert reason in verdict.reasons


@pytest.mark.parametrize(
    "controls",
    [
        {ControlKind.GOLD: False, ControlKind.NO_OP: False, ControlKind.ADVERSARIAL: False},
        {ControlKind.GOLD: True, ControlKind.NO_OP: True, ControlKind.ADVERSARIAL: False},
        {ControlKind.GOLD: True, ControlKind.NO_OP: False, ControlKind.ADVERSARIAL: True},
    ],
)
def test_broken_or_gameable_verifier_controls_are_inconclusive(controls):
    verdict = IndependentVerifier(Backend(controls=controls)).verify(SUBMISSION, POLICY, PRIVATE)
    assert verdict.status is VerificationStatus.INCONCLUSIVE


def test_quality_constraint_failure_is_invalid_even_with_high_throughput():
    trials = [evidence(metrics={"throughput": 1_000_000, "quality": 0.1})] * 3
    verdict = IndependentVerifier(Backend(trials)).verify(SUBMISSION, POLICY, PRIVATE)
    assert verdict.status is VerificationStatus.INVALID
    assert verdict.reasons == ("constraint_failed:quality",)


def test_missing_nan_or_infinite_metric_is_invalid():
    for metrics in (
        {"throughput": 100.0},
        {"throughput": float("nan"), "quality": 0.9},
        {"throughput": float("inf"), "quality": 0.9},
    ):
        verdict = IndependentVerifier(
            Backend([evidence(metrics=metrics)] * 3)
        ).verify(SUBMISSION, POLICY, PRIVATE)
        assert verdict.status is VerificationStatus.INVALID
        assert verdict.reasons == ("invalid_metric_evidence",)


def test_challenge_or_requested_model_mismatch_rejects_before_backend_runs():
    backend = Backend()
    wrong_challenge = ProvisionalSubmission(
        "exp-1", "wrong", "sha256:artifact", "sha256:model"
    )
    wrong_model = ProvisionalSubmission(
        "exp-1", "sha256:challenge", "sha256:artifact", "wrong"
    )
    verifier = IndependentVerifier(backend)
    assert verifier.verify(wrong_challenge, POLICY, PRIVATE).reasons == (
        "challenge_hash_mismatch",
    )
    assert verifier.verify(wrong_model, POLICY, PRIVATE).reasons == (
        "requested_model_mismatch",
    )
    assert backend.handles == []


def test_backend_failure_is_inconclusive_not_valid_or_invalid():
    class BrokenBackend(Backend):
        def run_trial(self, submission, private_evaluation, trial_index):
            raise TimeoutError("verification node unavailable")

    verdict = IndependentVerifier(BrokenBackend()).verify(SUBMISSION, POLICY, PRIVATE)
    assert verdict.status is VerificationStatus.INCONCLUSIVE
    assert verdict.reasons == ("trial_infrastructure_error",)


def test_verification_policy_requires_repetition():
    with pytest.raises(ValueError, match="at least two repetitions"):
        VerificationPolicy(
            challenge_hash="hash",
            expected_model_digest="model",
            repetitions=1,
            objectives=(MetricObjective("quality", MetricDirection.MAXIMIZE),),
        )


def test_verifier_observer_receives_every_derived_verdict():
    observed = []
    verifier = IndependentVerifier(Backend(), observe_verdict=observed.append)
    verdict = verifier.verify(SUBMISSION, POLICY, PRIVATE)
    assert observed == [verdict]
    assert observed[0].status is VerificationStatus.VALID
