"""Immutable, content-addressed contracts for the Evolution Arena."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def canonical_bytes(value: BaseModel | dict[str, Any]) -> bytes:
    """Return stable UTF-8 JSON bytes; hashes never depend on key insertion order."""
    raw = value.model_dump(mode="json", exclude_none=True) if isinstance(value, BaseModel) else value
    return json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def canonical_digest(value: BaseModel | dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DatasetRef(FrozenModel):
    uri: str
    digest: str


class ModelRef(FrozenModel):
    model_id: str
    digest: str
    tokenizer_id: str | None = None
    tokenizer_digest: str | None = None
    allowed_variants: tuple[str, ...] = ()
    required_modalities: tuple[str, ...] = ("text",)


class HardwareSpec(FrozenModel):
    hardware_class: str
    gpu_count: int = Field(ge=0)
    vram_bytes: int | None = Field(default=None, ge=0)
    image_digest: str
    driver_constraints: tuple[str, ...] = ()
    runtime_constraints: tuple[str, ...] = ()


class MetricSpec(FrozenModel):
    name: str
    unit: str
    objective: Literal["minimize", "maximize", "constraint"]
    aggregation: str = "mean"
    threshold: float | None = None


class BudgetSpec(FrozenModel):
    wall_seconds: float | None = Field(default=None, gt=0)
    token_limit: int | None = Field(default=None, gt=0)
    cost_limit: float | None = Field(default=None, ge=0)
    energy_joules: float | None = Field(default=None, ge=0)
    concurrency: int = Field(default=1, gt=0)


class ChallengeSpec(FrozenModel):
    schema_version: Literal["arena.challenge.v1"] = "arena.challenge.v1"
    id: str
    version: str
    title: str
    owner: str
    created_at: datetime
    repository_url: str
    base_commit: str
    writable_paths: tuple[str, ...]
    protected_paths: tuple[str, ...] = ()
    task_template: str
    model: ModelRef
    hardware: HardwareSpec
    public_dataset: DatasetRef
    withheld_dataset_ref: DatasetRef
    metrics: tuple[MetricSpec, ...]
    warmup_runs: int = Field(default=0, ge=0)
    repetitions: int = Field(default=1, gt=0)
    seeds: tuple[int, ...] = ()
    confidence_rule: str
    budgets: BudgetSpec
    allowed_optimizations: tuple[str, ...] = ()
    prohibited_optimizations: tuple[str, ...] = ()
    network_capabilities: tuple[str, ...] = ()
    tool_capabilities: tuple[str, ...] = ()
    verifier_version: str
    rubric_version: str
    policy_version: str
    promotion_requirements: tuple[str, ...]
    rollback_requirements: tuple[str, ...]

    @property
    def content_hash(self) -> str:
        return canonical_digest(self)

    @model_validator(mode="after")
    def validate_paths_and_metrics(self) -> ChallengeSpec:
        if set(self.writable_paths) & set(self.protected_paths):
            raise ValueError("writable_paths and protected_paths must not overlap")
        names = [metric.name for metric in self.metrics]
        if not names or len(names) != len(set(names)):
            raise ValueError("metrics must be non-empty and uniquely named")
        if set(self.allowed_optimizations) & set(self.prohibited_optimizations):
            raise ValueError("optimization classes cannot be both allowed and prohibited")
        return self


class ExperimentState(str, Enum):
    PROPOSED = "proposed"
    ADMITTED = "admitted"
    RUNNING = "running"
    PROVISIONAL = "provisional"
    VERIFYING = "verifying"
    VALID = "valid"
    INVALID = "invalid"
    INCONCLUSIVE = "inconclusive"
    FAILED = "failed"
    CANCELLED = "cancelled"


class VerificationState(str, Enum):
    UNVERIFIED = "unverified"
    VERIFYING = "verifying"
    VALID = "valid"
    INVALID = "invalid"
    INCONCLUSIVE = "inconclusive"


class ArtifactRef(FrozenModel):
    digest: str
    media_type: str = "application/octet-stream"
    size: int = Field(ge=0)
    role: str


class Provenance(FrozenModel):
    actor: str
    node: str
    session_id: str
    action: str
    target: str
    observed_prior_state: str | None = None
    signature: str | None = None


class Experiment(FrozenModel):
    schema_version: Literal["arena.experiment.v1"] = "arena.experiment.v1"
    id: str
    attempt: int = Field(default=1, gt=0)
    parent_id: str | None = None
    reproduces_id: str | None = None
    changed_dimensions: tuple[str, ...] = ()
    challenge_hash: str
    actor: str
    harness: str
    card_id: str | None = None
    run_id: str
    repository_base_sha: str
    repository_result_sha: str | None = None
    image_digest: str
    sbom_digest: str
    requested_route: str
    requested_model: str
    served_model: str | None = None
    gateway_request_id: str | None = None
    gateway_backend_id: str | None = None
    configuration: dict[str, Any] = Field(default_factory=dict)
    seeds: tuple[int, ...] = ()
    hardware_telemetry: dict[str, Any] = Field(default_factory=dict)
    budgets: BudgetSpec
    created_at: datetime
    artifacts: tuple[ArtifactRef, ...] = ()

    @property
    def content_hash(self) -> str:
        return canonical_digest(self)

    @model_validator(mode="after")
    def validate_lineage(self) -> Experiment:
        if self.id in {self.parent_id, self.reproduces_id}:
            raise ValueError("an experiment cannot be its own parent or reproduction target")
        if self.parent_id is not None and not self.changed_dimensions:
            raise ValueError("a mutation with parent_id must identify changed_dimensions")
        return self


class Observation(FrozenModel):
    value: float
    seed: int | None = None
    warm: bool = False
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Measurement(FrozenModel):
    metric: str
    unit: str
    observations: tuple[Observation, ...]
    mean: float
    standard_deviation: float
    confidence_low: float | None = None
    confidence_high: float | None = None
    best_of_n: float | None = None

    @model_validator(mode="after")
    def validate_sample_count(self) -> Measurement:
        if not self.observations:
            raise ValueError("measurements require raw observations")
        if self.standard_deviation < 0:
            raise ValueError("standard_deviation cannot be negative")
        return self


class Result(FrozenModel):
    schema_version: Literal["arena.result.v1"] = "arena.result.v1"
    experiment_id: str
    experiment_hash: str
    challenge_hash: str
    verification: VerificationState = VerificationState.UNVERIFIED
    verification_reason: str | None = None
    measurements: tuple[Measurement, ...]
    artifacts: tuple[ArtifactRef, ...] = ()
    created_at: datetime

    @property
    def content_hash(self) -> str:
        return canonical_digest(self)


class ExperimentEvent(FrozenModel):
    schema_version: Literal["arena.event.v1"] = "arena.event.v1"
    event_id: str
    writer_id: str
    sequence: int = Field(ge=1)
    experiment_id: str
    attempt: int = Field(default=1, gt=0)
    from_state: ExperimentState | None = None
    to_state: ExperimentState
    timestamp: datetime
    provenance: Provenance
    payload: dict[str, Any] = Field(default_factory=dict)
    prior_event_hash: str | None = None
    event_hash: str | None = None

    @model_validator(mode="after")
    def validate_transition(self) -> ExperimentEvent:
        allowed: dict[ExperimentState | None, set[ExperimentState]] = {
            None: {ExperimentState.PROPOSED},
            ExperimentState.PROPOSED: {ExperimentState.ADMITTED, ExperimentState.CANCELLED},
            ExperimentState.ADMITTED: {
                ExperimentState.RUNNING,
                ExperimentState.CANCELLED,
                ExperimentState.FAILED,
            },
            ExperimentState.RUNNING: {
                ExperimentState.PROVISIONAL,
                ExperimentState.CANCELLED,
                ExperimentState.FAILED,
            },
            ExperimentState.PROVISIONAL: {ExperimentState.VERIFYING},
            ExperimentState.VERIFYING: {
                ExperimentState.VALID,
                ExperimentState.INVALID,
                ExperimentState.INCONCLUSIVE,
            },
        }
        if self.to_state not in allowed.get(self.from_state, set()):
            raise ValueError(f"illegal experiment transition {self.from_state!r} -> {self.to_state!r}")
        return self

    def calculated_hash(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude={"event_hash"}))

    def sealed(self) -> ExperimentEvent:
        calculated = self.calculated_hash()
        if self.event_hash is not None and self.event_hash != calculated:
            raise ValueError("event_hash does not match canonical event content")
        return self.model_copy(update={"event_hash": calculated})
