"""Fail-closed qualification of immutable live execution records."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .metrics import MetricDirection, MetricObjective
from .models import Measurement, Observation, Result, VerificationState
from .status import ArenaStatusService
from .store import ArenaStore
from .verifier import (
    ControlKind,
    IndependentVerifier,
    PrivateEvaluationHandle,
    ProvisionalSubmission,
    TrialEvidence,
    VerificationPolicy,
    VerificationStatus,
)


@dataclass(frozen=True)
class FrozenExactOutputBackend:
    """Verifier-owned evaluator for one deliberately narrow frozen challenge.

    This proves the execution/evidence/admission plumbing.  Exact byte equality is
    not presented as evidence of general model semantic quality.
    """

    expected_output: bytes
    artifacts: Mapping[str, bytes]
    expected_model: str

    def run_trial(self, submission, private_evaluation, trial_index):
        del private_evaluation, trial_index
        output = self.artifacts[submission.artifact_digest]
        return TrialEvidence(
            metrics={"exact_output": float(output == self.expected_output)},
            served_model_digest=self.expected_model,
            artifact_digest=submission.artifact_digest,
            modalities_exercised=frozenset({"text"}),
        )

    def run_control(self, control, private_evaluation):
        del private_evaluation
        return control is ControlKind.GOLD


def qualify_execution_records(
    records: Sequence[Mapping[str, Any]],
    store: ArenaStore,
    *,
    expected_output: str = "qualified",
    expected_harness: str = "pi",
) -> dict[str, Any]:
    """Persist records and admit only independently exact-output-valid results."""
    from .models import Experiment

    artifacts: dict[str, bytes] = {}
    parsed = []
    for record in records:
        experiment = Experiment.model_validate(record["experiment"])
        result = Result.model_validate(record["result"])
        if experiment.content_hash != record.get("experiment_hash"):
            raise ValueError("immutable experiment hash mismatch")
        if result.content_hash != record.get("result_hash"):
            raise ValueError("immutable result hash mismatch")
        if result.experiment_hash != experiment.content_hash:
            raise ValueError("result does not identify its immutable experiment")
        if result.artifacts != experiment.artifacts:
            raise ValueError("experiment and result artifact lineage differs")
        if result.verification is not VerificationState.UNVERIFIED:
            raise ValueError("live execution records must arrive unverified")
        raw = str(record["assistant_output"]).encode()
        artifact_digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        if not result.artifacts or result.artifacts[0].digest != artifact_digest:
            raise ValueError("assistant output does not match its artifact digest")
        if store.put_artifact(raw) != artifact_digest:
            raise ValueError("artifact persistence digest mismatch")
        store.put_experiment(experiment)
        store.put_result(result)
        artifacts[artifact_digest] = raw
        parsed.append((experiment, result))

    candidates = [
        (experiment, result)
        for experiment, result in parsed
        if experiment.harness == expected_harness
    ]
    if len(candidates) != 1:
        raise ValueError(f"expected exactly one {expected_harness!r} execution record")
    experiment, provisional = candidates[0]
    model_identity = experiment.served_model or experiment.requested_model
    submission = ProvisionalSubmission(
        experiment.id,
        experiment.challenge_hash,
        provisional.artifacts[0].digest,
        model_identity,
        claimed_metrics={
            measurement.metric: measurement.mean for measurement in provisional.measurements
        },
    )
    policy = VerificationPolicy(
        challenge_hash=experiment.challenge_hash,
        expected_model_digest=model_identity,
        repetitions=3,
        objectives=(MetricObjective("exact_output", MetricDirection.MAXIMIZE, minimum=1.0),),
        required_modalities=frozenset({"text"}),
    )
    private = PrivateEvaluationHandle(
        "arena-frozen-exact-output", "v1", "verifier-owned-capability"
    )
    verdict = IndependentVerifier(
        FrozenExactOutputBackend(expected_output.encode(), artifacts, model_identity)
    ).verify(submission, policy, private)
    now = datetime.now(timezone.utc)
    summary = verdict.summaries.get("exact_output")
    value = summary.mean if summary is not None else 0.0
    verified = Result(
        experiment_id=experiment.id,
        experiment_hash=experiment.content_hash,
        challenge_hash=experiment.challenge_hash,
        verification=VerificationState(verdict.status.value),
        verification_reason=",".join(verdict.reasons) or None,
        measurements=(
            Measurement(
                metric="exact_output",
                unit="boolean",
                observations=tuple(
                    Observation(value=value, seed=seed, recorded_at=now) for seed in (11, 29, 47)
                ),
                mean=value,
                standard_deviation=0,
            ),
        ),
        artifacts=provisional.artifacts,
        created_at=now,
    )
    store.put_result(verified)
    frontier = ArenaStatusService(store=store).frontier(
        experiment.challenge_hash,
        (MetricObjective("exact_output", MetricDirection.MAXIMIZE, minimum=1.0),),
    )
    return {
        "gate": "frozen-verifier-owned-exact-output-v1",
        "scope": "execution-plumbing-only-not-general-semantic-quality",
        "experiment_id": experiment.id,
        "status": verdict.status.value,
        "reasons": list(verdict.reasons),
        "verified_result_hash": verified.content_hash,
        "private_evaluation": {"id": private.evaluation_id, "version": private.version},
        "frontier": frontier,
        "admitted": verdict.status is VerificationStatus.VALID
        and any(row["experiment_id"] == experiment.id for row in frontier),
    }
