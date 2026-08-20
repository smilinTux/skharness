from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from skharness.arena.metrics import MetricDirection, MetricObjective
from skharness.arena.models import (
    ArtifactRef,
    BudgetSpec,
    Experiment,
    Measurement,
    Observation,
    Result,
)
from skharness.arena.qualification import (
    FrozenExactOutputBackend,
    qualify_execution_records,
)
from skharness.arena.status import ArenaStatusService
from skharness.arena.store import ArenaStore, CorruptEventLogError
from skharness.arena.verifier import ControlKind, PrivateEvaluationHandle


def record(output: str, *, claimed_score: float = 1.0) -> dict:
    """Build one immutable Pi-shaped execution record."""
    now = datetime.now(timezone.utc)
    raw = output.encode()
    artifact = ArtifactRef(
        digest="sha256:" + hashlib.sha256(raw).hexdigest(),
        media_type="text/plain",
        size=len(raw),
        role="assistant-output",
    )
    experiment = Experiment(
        id="qualification:pi",
        challenge_hash="sha256:frozen-challenge",
        actor="service:test",
        harness="pi",
        card_id="card-test",
        run_id="qualification:pi",
        repository_url="https://example.invalid/repo.git",
        repository_base_sha="fixture",
        image_digest="pi:test",
        sbom_digest="fixture",
        requested_route="skgateway/openai-compatible",
        requested_model="reference",
        served_model="served-reference",
        budgets=BudgetSpec(wall_seconds=20),
        created_at=now,
        artifacts=(artifact,),
    )
    observation = Observation(value=claimed_score, recorded_at=now)
    result = Result(
        experiment_id=experiment.id,
        experiment_hash=experiment.content_hash,
        challenge_hash=experiment.challenge_hash,
        measurements=(
            Measurement(
                metric="claimed_score",
                unit="ratio",
                observations=(observation,),
                mean=claimed_score,
                standard_deviation=0,
            ),
        ),
        artifacts=(artifact,),
        created_at=now,
    )
    return {
        "experiment": experiment.model_dump(mode="json"),
        "experiment_hash": experiment.content_hash,
        "result": result.model_dump(mode="json"),
        "result_hash": result.content_hash,
        "assistant_output": output,
    }


def test_valid_pi_record_is_persisted_verified_and_admitted(tmp_path):
    store = ArenaStore(tmp_path)

    evidence = qualify_execution_records([record("qualified")], store)

    assert evidence["status"] == "valid"
    assert evidence["admitted"] is True
    assert evidence["scope"] == "execution-plumbing-only-not-general-semantic-quality"
    assert evidence["private_evaluation"] == {"id": "arena-frozen-exact-output", "version": "v1"}
    results = store.read_results()
    assert {result.verification.value for result in results} == {"unverified", "valid"}
    assert store.read_experiments()[0].harness == "pi"
    status = ArenaStatusService(store=store)
    assert (
        status.frontier(
            "sha256:frozen-challenge",
            (MetricObjective("exact_output", MetricDirection.MAXIMIZE, minimum=1),),
        )[0]["experiment_id"]
        == "qualification:pi"
    )


def test_planted_false_high_score_is_invalid_and_never_reaches_frontier(tmp_path):
    store = ArenaStore(tmp_path)

    evidence = qualify_execution_records(
        [record("not-qualified", claimed_score=999_999_999)], store
    )

    assert evidence["status"] == "invalid"
    assert evidence["reasons"] == ["constraint_failed:exact_output"]
    assert evidence["admitted"] is False
    assert evidence["frontier"] == []
    assert (
        ArenaStatusService(store=store).frontier(
            "sha256:frozen-challenge",
            (MetricObjective("exact_output", MetricDirection.MAXIMIZE, minimum=1),),
        )
        == []
    )


def test_tampered_immutable_output_is_rejected_before_persistence(tmp_path):
    item = record("qualified")
    item["assistant_output"] = "tampered"

    try:
        qualify_execution_records([item], ArenaStore(tmp_path))
    except ValueError as exc:
        assert "artifact digest" in str(exc)
    else:
        raise AssertionError("tampered execution output must fail closed")


def test_store_rejects_record_whose_filename_is_not_its_content_hash(tmp_path):
    store = ArenaStore(tmp_path)
    qualify_execution_records([record("qualified")], store)
    path = next(store.results_dir.glob("*.json"))
    path.rename(path.with_name("0" * 64 + ".json"))

    try:
        store.read_results()
    except CorruptEventLogError as exc:
        assert "hash mismatch" in str(exc)
    else:
        raise AssertionError("misnamed content-addressed record must fail closed")


def test_frozen_evaluator_controls_execute_real_exact_output_behavior():
    backend = FrozenExactOutputBackend(
        expected_output=b"qualified", artifacts={}, expected_model="model"
    )
    handle = PrivateEvaluationHandle("frozen", "v1", "private")

    assert backend.run_control(ControlKind.GOLD, handle) is True
    assert backend.run_control(ControlKind.NO_OP, handle) is False
    assert backend.run_control(ControlKind.ADVERSARIAL, handle) is False
