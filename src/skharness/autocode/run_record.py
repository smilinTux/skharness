"""Canonical, writer-free execution record schema (coord card ``8967bf22``).

This module defines what a durable autocode run record means.  It deliberately
does not write records and no production writer imports it yet.  A later card
will persist validated records in the existing atomic run journal at
``items.<card_id>.run_records[]``; that journal record is the source of truth,
never a coordination-board projection.

Historical rows may be represented only with ``origin="historical"`` and an
explicitly unattributable identity.  The validator rejects attempts to attach
an agent, session, node, or gateway request to such a row: a backfill must not
invent facts that were not captured when the run executed.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RUN_RECORD_SCHEMA_VERSION = "skharness.autocode.run-record.v1"
RUN_RECORD_JOURNAL_TEMPLATE = "~/.skcapstone/coordination/autopilot/runs/<run_id>.json"
RUN_RECORD_JOURNAL_FIELD = "items.<card_id>.run_records[]"
_DIGEST_SOURCE_PATTERN = re.compile(r".+#sha256:[0-9a-f]{64}")
_FORBIDDEN_SOURCE_TOKENS = frozenset({"default", "inferred", "projection"})


def _not_blank(value: str | None, *, field_name: str) -> str | None:
    if value is not None and not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    return value


def _canonical_json(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class FrozenModel(BaseModel):
    """Strict immutable base for source-of-truth records."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class RecordOrigin(str, Enum):
    LIVE = "live"
    HISTORICAL = "historical"


class AttributionState(str, Enum):
    OBSERVED = "observed"
    PARTIAL = "partial"
    HISTORICAL_UNATTRIBUTABLE = "historical_unattributable"


class EvidenceState(str, Enum):
    OBSERVED = "observed"
    ABSENT = "absent"
    CONFLICT = "conflict"


class AggregateState(str, Enum):
    OBSERVED = "observed"
    PARTIAL = "partial"
    ABSENT = "absent"
    CONFLICT = "conflict"


class AggregateScope(str, Enum):
    """What the top-level token/cost/energy scalar totals exactly cover."""

    ORDERED_GATEWAY_REQUESTS = "ordered_gateway_requests"
    LEGACY_RECORD = "legacy_record"


class GateState(str, Enum):
    OBSERVED = "observed"
    ABSENT = "absent"
    HISTORICAL_UNATTRIBUTABLE = "historical_unattributable"


class RequestedRouteKind(str, Enum):
    ROLE = "role"
    BUCKET = "bucket"
    CONCRETE_MODEL = "concrete_model"


class SourcePointer(FrozenModel):
    """Exact JSON Pointer under one digest-addressed source record."""

    record_source: str
    json_pointer: str

    @model_validator(mode="after")
    def is_exact_and_firsthand(self) -> "SourcePointer":
        if _DIGEST_SOURCE_PATTERN.fullmatch(self.record_source) is None:
            raise ValueError("record_source must end with a sha256 digest")
        locator = self.record_source.rsplit("#sha256:", 1)[0]
        locator_tokens = set(re.split(r"[^a-z0-9]+", locator.lower()))
        if locator_tokens & _FORBIDDEN_SOURCE_TOKENS:
            raise ValueError("source pointers cannot reference inferred/default/projection data")

        pointer = self.json_pointer
        if not pointer.startswith("/") or pointer == "/":
            raise ValueError("json_pointer must identify one exact non-root field")
        parts = pointer[1:].split("/")
        if any(not part or "*" in part or part == ".." for part in parts):
            raise ValueError("json_pointer cannot contain empty, wildcard, or parent segments")
        if any(re.search(r"~(?![01])", part) for part in parts):
            raise ValueError("json_pointer contains an invalid JSON Pointer escape")
        pointer_tokens = set(re.split(r"[^a-z0-9]+", pointer.lower()))
        if pointer_tokens & _FORBIDDEN_SOURCE_TOKENS:
            raise ValueError("source pointers cannot reference inferred/default/projection data")
        return self


EvidenceT = TypeVar("EvidenceT")


class EvidenceObservation(FrozenModel, Generic[EvidenceT]):
    """One firsthand source's value; sources are names, not inferred trust."""

    source: str
    value: EvidenceT

    @field_validator("source")
    @classmethod
    def source_is_not_blank(cls, value: str) -> str:
        return _not_blank(value, field_name="source")  # type: ignore[return-value]


class Evidence(FrozenModel, Generic[EvidenceT]):
    """A value plus enough observations to distinguish absence from conflict."""

    state: EvidenceState
    value: EvidenceT | None
    observations: tuple[EvidenceObservation[EvidenceT], ...]

    @model_validator(mode="after")
    def state_matches_evidence(self) -> "Evidence[EvidenceT]":
        string_values = [self.value, *(observation.value for observation in self.observations)]
        if any(isinstance(value, str) and not value.strip() for value in string_values):
            raise ValueError("string evidence values must not be blank")

        if self.state is EvidenceState.ABSENT:
            if self.value is not None or self.observations:
                raise ValueError("absent evidence must have no value or observations")
            return self

        if self.state is EvidenceState.OBSERVED:
            if self.value is None or not self.observations:
                raise ValueError("observed evidence requires a value and observation")
            if any(observation.value != self.value for observation in self.observations):
                raise ValueError("observed evidence observations must agree with value")
            return self

        if self.value is not None:
            raise ValueError("conflicting evidence cannot claim a resolved value")
        distinct = {_canonical_json(observation.value) for observation in self.observations}
        if len(distinct) < 2:
            raise ValueError("conflicting evidence requires two distinct observations")
        return self


class TokenUsage(FrozenModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_read_tokens: int = Field(ge=0)
    cache_write_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)


class RequestCost(FrozenModel):
    input_usd: float = Field(ge=0)
    output_usd: float = Field(ge=0)
    cache_read_usd: float = Field(ge=0)
    cache_write_usd: float = Field(ge=0)
    total_usd: float = Field(ge=0)
    currency: str = "USD"

    @field_validator("currency")
    @classmethod
    def currency_is_usd(cls, value: str) -> str:
        if value != "USD":
            raise ValueError("run-record costs are normalized to USD")
        return value


class EnergyMeasurement(FrozenModel):
    joules: float = Field(ge=0)
    basis: str
    node: str | None

    @field_validator("basis", "node")
    @classmethod
    def strings_are_not_blank(cls, value: str | None, info) -> str | None:
        return _not_blank(value, field_name=info.field_name)


class RequestTiming(FrozenModel):
    started_at: datetime | None
    first_token_ms: float | None = Field(ge=0)
    total_ms: float | None = Field(ge=0)

    @model_validator(mode="after")
    def has_a_measurement(self) -> "RequestTiming":
        if self.started_at is None and self.first_token_ms is None and self.total_ms is None:
            raise ValueError("request timing must contain at least one measurement")
        if (
            self.first_token_ms is not None
            and self.total_ms is not None
            and self.first_token_ms > self.total_ms
        ):
            raise ValueError("first_token_ms cannot exceed total_ms")
        if self.started_at is not None and self.started_at.utcoffset() is None:
            raise ValueError("request started_at must be timezone-aware")
        return self


class SamplingParameter(FrozenModel):
    name: str
    value: str | int | float | bool | None

    @field_validator("name")
    @classmethod
    def name_is_not_blank(cls, value: str) -> str:
        return _not_blank(value, field_name="name")  # type: ignore[return-value]


class SamplingSnapshot(FrozenModel):
    temperature: float | None = Field(default=None, ge=0)
    top_p: float | None = Field(default=None, ge=0, le=1)
    top_k: int | None = Field(default=None, ge=0)
    max_tokens: int | None = Field(default=None, ge=1)
    seed: int | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    provider_parameters: tuple[SamplingParameter, ...] = ()

    @model_validator(mode="after")
    def is_nonempty_and_canonical(self) -> "SamplingSnapshot":
        known = (
            self.temperature,
            self.top_p,
            self.top_k,
            self.max_tokens,
            self.seed,
            self.frequency_penalty,
            self.presence_penalty,
        )
        if all(value is None for value in known) and not self.provider_parameters:
            raise ValueError("sampling snapshot must contain at least one parameter")
        names = [parameter.name for parameter in self.provider_parameters]
        if names != sorted(names) or len(names) != len(set(names)):
            raise ValueError("provider sampling parameters must be unique and name-sorted")
        return self


class SamplingComparison(str, Enum):
    MATCHED = "matched"
    SUBSTITUTED = "substituted"
    REQUESTED_ONLY = "requested_only"
    OBSERVED_ONLY = "observed_only"
    ABSENT = "absent"
    CONFLICT = "conflict"


class SamplingProvenance(FrozenModel):
    """Requested sampling is never silently relabelled as observed sampling."""

    requested: SamplingSnapshot | None
    requested_source: str | None
    observed: Evidence[SamplingSnapshot]
    comparison: SamplingComparison

    @field_validator("requested_source")
    @classmethod
    def source_is_not_blank(cls, value: str | None) -> str | None:
        return _not_blank(value, field_name="requested_source")

    @model_validator(mode="after")
    def comparison_matches_values(self) -> "SamplingProvenance":
        if (self.requested is None) != (self.requested_source is None):
            raise ValueError("requested sampling and requested_source must appear together")

        if self.observed.state is EvidenceState.CONFLICT:
            expected = SamplingComparison.CONFLICT
        elif self.requested is None and self.observed.state is EvidenceState.ABSENT:
            expected = SamplingComparison.ABSENT
        elif self.requested is not None and self.observed.state is EvidenceState.ABSENT:
            expected = SamplingComparison.REQUESTED_ONLY
        elif self.requested is None:
            expected = SamplingComparison.OBSERVED_ONLY
        elif self.requested == self.observed.value:
            expected = SamplingComparison.MATCHED
        else:
            expected = SamplingComparison.SUBSTITUTED

        if self.comparison is not expected:
            raise ValueError(f"sampling comparison must be {expected.value}")
        return self


class GatewayRequestProvenance(FrozenModel):
    """One gateway call in the exact order made by a multi-turn Pi run."""

    sequence: int = Field(ge=1)
    request_id: Evidence[str]
    requested_role: str | None
    requested_model: str
    requested_route_kind: RequestedRouteKind
    served_model: Evidence[str]
    backend: Evidence[str]
    status: Evidence[int]
    usage: Evidence[TokenUsage]
    cost: Evidence[RequestCost]
    energy: Evidence[EnergyMeasurement]
    timing: Evidence[RequestTiming]
    sampling: SamplingProvenance

    @field_validator("requested_role", "requested_model")
    @classmethod
    def strings_are_not_blank(cls, value: str | None, info) -> str | None:
        return _not_blank(value, field_name=info.field_name)

    @model_validator(mode="after")
    def route_and_status_are_valid(self) -> "GatewayRequestProvenance":
        if self.requested_route_kind is RequestedRouteKind.ROLE:
            if self.requested_role is None or self.requested_role != self.requested_model:
                raise ValueError("role requests must retain the exact role in requested_model")
        elif self.requested_role is not None:
            raise ValueError("requested_role is only valid for role routes")

        for observation in self.status.observations:
            if not 100 <= observation.value <= 599:
                raise ValueError("observed HTTP status must be between 100 and 599")
        if self.status.value is not None and not 100 <= self.status.value <= 599:
            raise ValueError("HTTP status must be between 100 and 599")
        return self


class TaskShape(FrozenModel):
    size: str | None
    risk: str | None
    sensitivity: str | None
    model_class: str | None
    source: SourcePointer

    @field_validator("size", "risk", "sensitivity", "model_class")
    @classmethod
    def strings_are_not_blank(cls, value: str | None, info) -> str | None:
        return _not_blank(value, field_name=info.field_name)

    @model_validator(mode="after")
    def has_a_shape(self) -> "TaskShape":
        if all(
            value is None for value in (self.size, self.risk, self.sensitivity, self.model_class)
        ):
            raise ValueError("task shape must contain at least one measured dimension")
        return self


class RunRecord(FrozenModel):
    """One immutable execution outcome and its ordered request provenance."""

    schema_version: str
    origin: RecordOrigin
    record_sources: tuple[str, ...]
    run_id: str
    card_id: str
    repository: str
    round: int = Field(ge=1)

    # Required top-level identity scalars plus their actual sources.
    agent: str | None
    session_id: str | None
    node: str | None
    agent_source: str | None
    session_id_source: str | None
    node_source: str | None
    attribution_state: AttributionState

    # Required top-level execution/routing scalars.
    adapter: str | None
    model_requested: str | None
    model_served: str | None
    model_served_state: AggregateState
    effort_tier: str | None
    task_shape: TaskShape | None

    # Required top-level accounting and gate scalars.
    tokens: int | None = Field(ge=0)
    usage_scope: AggregateScope
    usage_state: AggregateState
    usage_sources: tuple[SourcePointer, ...]
    cost_usd: float | None = Field(ge=0)
    cost_scope: AggregateScope
    cost_state: AggregateState
    cost_sources: tuple[SourcePointer, ...]
    energy_joules: float | None = Field(ge=0)
    energy_scope: AggregateScope
    energy_state: AggregateState
    energy_sources: tuple[SourcePointer, ...]
    score: int | None = Field(ge=1, le=5)
    passed: bool | None
    gate_state: GateState

    # Existing terminal vocabulary that this schema supersedes, not duplicates.
    outcome: str | None
    terminal_state: str | None
    quality_mode: str | None
    grader_model: str | None
    retries: int = Field(ge=0)
    pr: str | None
    escalation_reason: str | None

    started_at: datetime | None
    finished_at: datetime | None
    recorded_at: datetime
    gateway_requests: tuple[GatewayRequestProvenance, ...]

    @field_validator(
        "schema_version",
        "run_id",
        "card_id",
        "repository",
        "agent",
        "session_id",
        "node",
        "agent_source",
        "session_id_source",
        "node_source",
        "adapter",
        "model_requested",
        "model_served",
        "effort_tier",
        "outcome",
        "terminal_state",
        "quality_mode",
        "grader_model",
        "pr",
        "escalation_reason",
    )
    @classmethod
    def strings_are_not_blank(cls, value: str | None, info) -> str | None:
        return _not_blank(value, field_name=info.field_name)

    @field_validator("record_sources")
    @classmethod
    def sources_are_canonical(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("record_sources entries must not be blank")
        if list(values) != sorted(values) or len(values) != len(set(values)):
            raise ValueError("record_sources must be unique and sorted")
        if any(_DIGEST_SOURCE_PATTERN.fullmatch(value) is None for value in values):
            raise ValueError("record_sources entries must end with a sha256 digest")
        for value in values:
            locator = value.rsplit("#sha256:", 1)[0]
            tokens = set(re.split(r"[^a-z0-9]+", locator.lower()))
            if tokens & _FORBIDDEN_SOURCE_TOKENS:
                raise ValueError(
                    "record_sources cannot reference inferred/default/projection data"
                )
        return values

    @field_validator("usage_sources", "cost_sources", "energy_sources")
    @classmethod
    def pointers_are_canonical(
        cls, values: tuple[SourcePointer, ...], info
    ) -> tuple[SourcePointer, ...]:
        keys = [(pointer.record_source, pointer.json_pointer) for pointer in values]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError(f"{info.field_name} must be unique and sorted")
        return values

    @model_validator(mode="after")
    def validate_record(self) -> "RunRecord":
        if self.schema_version != RUN_RECORD_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {RUN_RECORD_SCHEMA_VERSION!r}")
        for timestamp in (self.started_at, self.finished_at, self.recorded_at):
            if timestamp is not None and timestamp.utcoffset() is None:
                raise ValueError("run-record timestamps must be timezone-aware")

        self._validate_identity()
        self._validate_source_pointers()
        self._validate_gate()
        self._validate_aggregate(
            "usage", self.tokens, self.usage_scope, self.usage_state, self.usage_sources
        )
        self._validate_aggregate(
            "cost", self.cost_usd, self.cost_scope, self.cost_state, self.cost_sources
        )
        self._validate_aggregate(
            "energy",
            self.energy_joules,
            self.energy_scope,
            self.energy_state,
            self.energy_sources,
        )
        self._validate_requests()
        return self

    def _validate_identity(self) -> None:
        values = (self.agent, self.session_id, self.node)
        sources = (self.agent_source, self.session_id_source, self.node_source)
        for value, source in zip(values, sources, strict=True):
            if (value is None) != (source is None):
                raise ValueError("each identity value and source must appear together")

        present = sum(value is not None for value in values)
        if (
            self.origin is RecordOrigin.LIVE
            and self.attribution_state is not AttributionState.OBSERVED
        ):
            raise ValueError("live records require fully observed identity")
        if self.origin is RecordOrigin.HISTORICAL:
            if self.attribution_state is not AttributionState.HISTORICAL_UNATTRIBUTABLE:
                raise ValueError("historical records must be explicitly unattributable")
            if present or self.gateway_requests:
                raise ValueError("historical attribution/request provenance cannot be backfilled")
            routed_facts = {
                "adapter": self.adapter,
                "model_requested": self.model_requested,
                "model_served": self.model_served,
                "effort_tier": self.effort_tier,
                "grader_model": self.grader_model,
            }
            if any(value is not None for value in routed_facts.values()):
                raise ValueError(
                    "historical execution/model attribution cannot be backfilled: "
                    + ", ".join(name for name, value in routed_facts.items() if value is not None)
                )
            if not self.record_sources:
                raise ValueError("historical records require an exact legacy record source")
            if any(
                _DIGEST_SOURCE_PATTERN.fullmatch(source) is None for source in self.record_sources
            ):
                raise ValueError(
                    "historical record sources must bind a legacy row with a sha256 digest"
                )
            if self.started_at is not None or self.finished_at is not None:
                raise ValueError("historical execution timestamps must be explicitly absent")
            if (
                self.usage_scope is not AggregateScope.LEGACY_RECORD
                or self.cost_scope is not AggregateScope.LEGACY_RECORD
                or self.energy_scope is not AggregateScope.LEGACY_RECORD
            ):
                raise ValueError("historical accounting scope must be legacy_record")
        elif self.attribution_state is AttributionState.HISTORICAL_UNATTRIBUTABLE:
            raise ValueError("live records cannot claim historical_unattributable identity")
        elif self.attribution_state is AttributionState.OBSERVED and present != 3:
            raise ValueError("observed identity requires agent, session_id, and node")
        elif self.attribution_state is AttributionState.PARTIAL and not 0 < present < 3:
            raise ValueError("partial identity requires one or two observed values")

        if self.origin is RecordOrigin.LIVE:
            required = {
                "adapter": self.adapter,
                "model_requested": self.model_requested,
                "effort_tier": self.effort_tier,
            }
            missing = [name for name, value in required.items() if value is None]
            if missing:
                raise ValueError("live records require " + ", ".join(missing))
            if not self.record_sources:
                raise ValueError("live records require an execution record source")
            if self.started_at is None or self.finished_at is None:
                raise ValueError("live records require started_at and finished_at")
            if not self.started_at <= self.finished_at <= self.recorded_at:
                raise ValueError(
                    "timestamps must satisfy started_at <= finished_at <= recorded_at"
                )
            if (
                self.usage_scope is not AggregateScope.ORDERED_GATEWAY_REQUESTS
                or self.cost_scope is not AggregateScope.ORDERED_GATEWAY_REQUESTS
                or self.energy_scope is not AggregateScope.ORDERED_GATEWAY_REQUESTS
            ):
                raise ValueError("live accounting scope must be ordered_gateway_requests")

    def _validate_source_pointers(self) -> None:
        pointers = [*self.usage_sources, *self.cost_sources, *self.energy_sources]
        if self.task_shape is not None:
            pointers.append(self.task_shape.source)
        declared = set(self.record_sources)
        dangling = [
            pointer.record_source for pointer in pointers if pointer.record_source not in declared
        ]
        if dangling:
            raise ValueError("field source pointer is not declared in record_sources")
        if (
            self.origin is RecordOrigin.HISTORICAL
            and self.task_shape is not None
            and self.task_shape.source.json_pointer != "/work_grade"
        ):
            raise ValueError("historical task shape source must point to /work_grade")

    def _validate_gate(self) -> None:
        if self.gate_state is GateState.OBSERVED:
            if self.score is None or self.passed is None:
                raise ValueError("observed gate requires score and pass/fail")
        elif self.score is not None or self.passed is not None:
            raise ValueError("an absent/unattributable gate cannot claim score or pass/fail")
        if self.gate_state is GateState.HISTORICAL_UNATTRIBUTABLE:
            if self.origin is not RecordOrigin.HISTORICAL:
                raise ValueError("only historical records can have an unattributable gate")

    @staticmethod
    def _validate_aggregate(
        name: str,
        value: int | float | None,
        scope: AggregateScope,
        state: AggregateState,
        sources: tuple[SourcePointer, ...],
    ) -> None:
        if state is AggregateState.OBSERVED:
            if value is None or not sources:
                raise ValueError(f"observed {name} requires a value and source")
        elif value is not None:
            raise ValueError(f"{state.value} {name} cannot claim an aggregate value")
        elif state is AggregateState.ABSENT and sources:
            raise ValueError(f"absent {name} cannot claim sources")
        elif state in {AggregateState.PARTIAL, AggregateState.CONFLICT} and not sources:
            raise ValueError(f"{state.value} {name} requires evidence sources")
        legacy_pointer = {
            "usage": "/tokens",
            "cost": "/cost_usd",
            "energy": "/energy_joules",
        }[name]
        if scope is AggregateScope.LEGACY_RECORD and any(
            pointer.json_pointer != legacy_pointer for pointer in sources
        ):
            raise ValueError(f"legacy {name} sources must point to its exact scalar field")

    def _validate_requests(self) -> None:
        sequences = [request.sequence for request in self.gateway_requests]
        if sequences != list(range(1, len(sequences) + 1)):
            raise ValueError("gateway requests must be ordered consecutively from sequence 1")

        if self.model_requested is not None:
            if any(
                request.requested_model != self.model_requested
                for request in self.gateway_requests
            ):
                raise ValueError("top-level model_requested must match every request")

        states = [request.served_model.state for request in self.gateway_requests]
        observed = {
            request.served_model.value
            for request in self.gateway_requests
            if request.served_model.state is EvidenceState.OBSERVED
        }
        if any(state is EvidenceState.CONFLICT for state in states) or len(observed) > 1:
            expected_state, expected_value = AggregateState.CONFLICT, None
        elif observed and any(state is EvidenceState.ABSENT for state in states):
            expected_state, expected_value = AggregateState.PARTIAL, None
        elif observed:
            expected_state, expected_value = AggregateState.OBSERVED, next(iter(observed))
        else:
            expected_state, expected_value = AggregateState.ABSENT, None
        if self.model_served_state is not expected_state or self.model_served != expected_value:
            raise ValueError("top-level model_served must faithfully summarize request evidence")

        self._reconcile_request_aggregate(
            "usage",
            self.tokens,
            self.usage_scope,
            self.usage_state,
            [request.usage for request in self.gateway_requests],
            lambda usage: Decimal(usage.total_tokens),
        )
        self._reconcile_request_aggregate(
            "cost",
            self.cost_usd,
            self.cost_scope,
            self.cost_state,
            [request.cost for request in self.gateway_requests],
            lambda cost: Decimal(str(cost.total_usd)),
        )
        self._reconcile_request_aggregate(
            "energy",
            self.energy_joules,
            self.energy_scope,
            self.energy_state,
            [request.energy for request in self.gateway_requests],
            lambda energy: Decimal(str(energy.joules)),
        )

    @staticmethod
    def _reconcile_request_aggregate(
        name: str,
        value: int | float | None,
        scope: AggregateScope,
        state: AggregateState,
        evidence: list[Evidence],
        extract,
    ) -> None:
        if scope is not AggregateScope.ORDERED_GATEWAY_REQUESTS:
            return

        states = [item.state for item in evidence]
        if any(item is EvidenceState.CONFLICT for item in states):
            expected_state, expected_total = AggregateState.CONFLICT, None
        elif states and all(item is EvidenceState.OBSERVED for item in states):
            expected_state = AggregateState.OBSERVED
            expected_total = sum(
                (extract(item.value) for item in evidence if item.value is not None), Decimal(0)
            )
        elif any(item is EvidenceState.OBSERVED for item in states):
            expected_state, expected_total = AggregateState.PARTIAL, None
        else:
            expected_state, expected_total = AggregateState.ABSENT, None

        if state is not expected_state:
            raise ValueError(
                f"top-level {name} state must be {expected_state.value} for request evidence"
            )
        if expected_total is None:
            if value is not None:
                raise ValueError(f"top-level {name} cannot claim a total from incomplete evidence")
            return
        if value is None or Decimal(str(value)) != expected_total:
            raise ValueError(f"top-level {name} total must exactly equal ordered request evidence")

    @property
    def content_hash(self) -> str:
        payload = _canonical_json(self.model_dump(mode="json"))
        return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_run_record(data: RunRecord | dict) -> RunRecord:
    """Validate an already-built record or untrusted JSON-shaped input."""

    return RunRecord.model_validate(data)


__all__ = [
    "AggregateScope",
    "AggregateState",
    "AttributionState",
    "EnergyMeasurement",
    "Evidence",
    "EvidenceObservation",
    "EvidenceState",
    "GateState",
    "GatewayRequestProvenance",
    "RecordOrigin",
    "RequestCost",
    "RequestedRouteKind",
    "RequestTiming",
    "RUN_RECORD_JOURNAL_FIELD",
    "RUN_RECORD_JOURNAL_TEMPLATE",
    "RUN_RECORD_SCHEMA_VERSION",
    "RunRecord",
    "SamplingComparison",
    "SamplingParameter",
    "SamplingProvenance",
    "SamplingSnapshot",
    "SourcePointer",
    "TaskShape",
    "TokenUsage",
    "validate_run_record",
]
