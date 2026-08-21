"""Pure RunRecord schema and provenance validation (coord card 8967bf22)."""

from __future__ import annotations

import ast
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from skharness.autocode.run_record import (
    RUN_RECORD_JOURNAL_FIELD,
    RUN_RECORD_JOURNAL_TEMPLATE,
    RUN_RECORD_SCHEMA_VERSION,
    AggregateScope,
    AggregateState,
    AttributionState,
    EnergyMeasurement,
    Evidence,
    EvidenceObservation,
    EvidenceState,
    GateState,
    GatewayRequestProvenance,
    RecordOrigin,
    RequestCost,
    RequestedRouteKind,
    RequestTiming,
    RunRecord,
    SamplingComparison,
    SamplingProvenance,
    SamplingSnapshot,
    SourcePointer,
    TaskShape,
    TokenUsage,
    validate_run_record,
)

NOW = datetime(2026, 8, 21, 16, 0, tzinfo=timezone.utc)
LEGACY_SOURCE = "autopilot-cost/ledger.jsonl#sha256:" + "a" * 64
LIVE_SOURCE = "controller/execution-envelope#sha256:" + "b" * 64


def _pointer(source: str, json_pointer: str) -> SourcePointer:
    return SourcePointer(record_source=source, json_pointer=json_pointer)


def _observed(value, source: str):
    return Evidence(
        state=EvidenceState.OBSERVED,
        value=value,
        observations=(EvidenceObservation(source=source, value=value),),
    )


def _absent():
    return Evidence(state=EvidenceState.ABSENT, value=None, observations=())


def _request(
    sequence: int = 1,
    *,
    served_model: str = "qwen3.8-27b-huihui-abliterated-q4_k_m",
    sampling: SamplingProvenance | None = None,
) -> GatewayRequestProvenance:
    requested_sampling = SamplingSnapshot(temperature=0.2, max_tokens=512)
    return GatewayRequestProvenance(
        sequence=sequence,
        request_id=_observed(f"req-{sequence}", "skgateway.request_log.request_id"),
        requested_role="sk-creative",
        requested_model="sk-creative",
        requested_route_kind=RequestedRouteKind.ROLE,
        served_model=_observed(served_model, "skgateway.request_log.model_served"),
        backend=_observed("reg:qwen38", "skgateway.request_log.backend"),
        status=_observed(200, "skgateway.request_log.status"),
        usage=_observed(
            TokenUsage(
                input_tokens=57,
                output_tokens=32,
                cache_read_tokens=3,
                cache_write_tokens=0,
                total_tokens=89,
            ),
            "skgateway.token_usage",
        ),
        cost=_observed(
            RequestCost(
                input_usd=0.001,
                output_usd=0.002,
                cache_read_usd=0.0001,
                cache_write_usd=0,
                total_usd=0.0031,
            ),
            "skgateway.cost_log",
        ),
        energy=_observed(
            EnergyMeasurement(joules=18.5, basis="node-attributed", node="chiap41"),
            "skgateway.energy_log",
        ),
        timing=_observed(
            RequestTiming(started_at=NOW, first_token_ms=120, total_ms=830),
            "skgateway.request_log",
        ),
        sampling=sampling
        or SamplingProvenance(
            requested=requested_sampling,
            requested_source="pi.request",
            observed=_observed(requested_sampling, "skgateway.effective_sampling"),
            comparison=SamplingComparison.MATCHED,
        ),
    )


def _live_record(**overrides) -> RunRecord:
    data = {
        "schema_version": RUN_RECORD_SCHEMA_VERSION,
        "origin": RecordOrigin.LIVE,
        "record_sources": (LIVE_SOURCE,),
        "run_id": "airun-card-20260821T160000Z",
        "card_id": "card-1",
        "repository": "skharness",
        "round": 1,
        "agent": "lumina",
        "session_id": "session-abc",
        "node": "chiap41",
        "agent_source": "SKAGENT",
        "session_id_source": "SK_SESSION_ID",
        "node_source": "socket.gethostname",
        "attribution_state": AttributionState.OBSERVED,
        "adapter": "pi",
        "model_requested": "sk-creative",
        "model_served": "qwen3.8-27b-huihui-abliterated-q4_k_m",
        "model_served_state": AggregateState.OBSERVED,
        "effort_tier": "medium",
        "task_shape": TaskShape(
            size="M",
            risk="medium",
            sensitivity="internal",
            model_class="creative",
            source=_pointer(LIVE_SOURCE, "/card/work_grade"),
        ),
        "tokens": 89,
        "usage_scope": AggregateScope.ORDERED_GATEWAY_REQUESTS,
        "usage_state": AggregateState.OBSERVED,
        "usage_sources": (_pointer(LIVE_SOURCE, "/gateway_requests/0/usage/value/total_tokens"),),
        "cost_usd": 0.0031,
        "cost_scope": AggregateScope.ORDERED_GATEWAY_REQUESTS,
        "cost_state": AggregateState.OBSERVED,
        "cost_sources": (_pointer(LIVE_SOURCE, "/gateway_requests/0/cost/value/total_usd"),),
        "energy_joules": 18.5,
        "energy_scope": AggregateScope.ORDERED_GATEWAY_REQUESTS,
        "energy_state": AggregateState.OBSERVED,
        "energy_sources": (_pointer(LIVE_SOURCE, "/gateway_requests/0/energy/value/joules"),),
        "score": 5,
        "passed": True,
        "gate_state": GateState.OBSERVED,
        "outcome": "pass",
        "terminal_state": "finalized",
        "quality_mode": "twin-gate",
        "grader_model": "sk-code",
        "retries": 0,
        "pr": None,
        "escalation_reason": None,
        "started_at": NOW,
        "finished_at": NOW + timedelta(seconds=1),
        "recorded_at": NOW + timedelta(seconds=2),
        "gateway_requests": (_request(),),
    }
    data.update(overrides)
    return RunRecord(**data)


def _two_request_aggregates() -> dict:
    return {
        "tokens": 178,
        "usage_sources": tuple(
            _pointer(LIVE_SOURCE, f"/gateway_requests/{index}/usage/value/total_tokens")
            for index in range(2)
        ),
        "cost_usd": 0.0062,
        "cost_sources": tuple(
            _pointer(LIVE_SOURCE, f"/gateway_requests/{index}/cost/value/total_usd")
            for index in range(2)
        ),
        "energy_joules": 37.0,
        "energy_sources": tuple(
            _pointer(LIVE_SOURCE, f"/gateway_requests/{index}/energy/value/joules")
            for index in range(2)
        ),
    }


def test_schema_keeps_all_card_required_scalars_at_top_level():
    schema = RunRecord.model_json_schema()
    required = set(schema["required"])
    assert {
        "agent",
        "session_id",
        "node",
        "adapter",
        "model_requested",
        "model_served",
        "effort_tier",
        "cost_usd",
        "task_shape",
        "score",
        "passed",
        "run_id",
        "started_at",
        "finished_at",
        "recorded_at",
    } <= required


def test_complete_record_round_trips_and_has_stable_content_hash():
    record = _live_record()
    payload = record.model_dump(mode="json")
    restored = validate_run_record(payload)

    assert restored == record
    assert restored.gateway_requests[0].request_id.value == "req-1"
    assert restored.gateway_requests[0].usage.value.total_tokens == 89
    assert restored.energy_joules == 18.5
    assert restored.gateway_requests[0].sampling.comparison is SamplingComparison.MATCHED
    assert restored.content_hash == record.content_hash
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", record.content_hash)


def test_record_and_all_nested_evidence_are_immutable_and_strict():
    record = _live_record()
    with pytest.raises(ValidationError):
        record.agent = "other"
    with pytest.raises(ValidationError):
        RunRecord.model_validate({**record.model_dump(), "invented": True})


def test_gateway_requests_are_exactly_ordered_and_gap_free():
    with pytest.raises(ValidationError, match="ordered consecutively"):
        _live_record(gateway_requests=(_request(2), _request(1)))

    with pytest.raises(ValidationError, match="ordered consecutively"):
        _live_record(gateway_requests=(_request(1), _request(3)))


def test_request_provenance_distinguishes_absence_from_conflict():
    absent = _absent()
    assert absent.state is EvidenceState.ABSENT

    conflict = Evidence(
        state=EvidenceState.CONFLICT,
        value=None,
        observations=(
            EvidenceObservation(source="pi.stdout", value="model-a"),
            EvidenceObservation(source="gateway.request_log", value="model-b"),
        ),
    )
    assert conflict.state is EvidenceState.CONFLICT

    with pytest.raises(ValidationError, match="two distinct observations"):
        Evidence(
            state=EvidenceState.CONFLICT,
            value=None,
            observations=(
                EvidenceObservation(source="one", value="same"),
                EvidenceObservation(source="two", value="same"),
            ),
        )
    with pytest.raises(ValidationError, match="no value or observations"):
        Evidence(state=EvidenceState.ABSENT, value="invented", observations=())


@pytest.mark.parametrize(
    "value,observation_value",
    [("", ""), ("served-model", "  ")],
)
def test_observed_string_evidence_rejects_blank_values(value, observation_value):
    with pytest.raises(ValidationError, match="must not be blank"):
        Evidence(
            state=EvidenceState.OBSERVED,
            value=value,
            observations=(EvidenceObservation(source="gateway", value=observation_value),),
        )


def test_top_level_served_model_cannot_hide_multi_request_substitution():
    requests = (_request(1, served_model="model-a"), _request(2, served_model="model-b"))
    record = _live_record(
        model_served=None,
        model_served_state=AggregateState.CONFLICT,
        gateway_requests=requests,
        **_two_request_aggregates(),
    )
    assert record.model_served is None

    with pytest.raises(ValidationError, match="faithfully summarize"):
        _live_record(
            model_served="model-a",
            model_served_state=AggregateState.OBSERVED,
            gateway_requests=requests,
        )


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("tokens", 90, "usage total must exactly equal"),
        ("cost_usd", 0.004, "cost total must exactly equal"),
        ("energy_joules", 19.0, "energy total must exactly equal"),
    ],
)
def test_observed_aggregates_must_exactly_match_ordered_requests(field, value, message):
    with pytest.raises(ValidationError, match=message):
        _live_record(**{field: value})


def test_mixed_missing_request_accounting_is_explicitly_partial():
    second = _request(2).model_copy(
        update={"usage": _absent(), "cost": _absent(), "energy": _absent()}
    )
    record = _live_record(
        tokens=None,
        usage_state=AggregateState.PARTIAL,
        usage_sources=(_pointer(LIVE_SOURCE, "/gateway_requests/0/usage/value/total_tokens"),),
        cost_usd=None,
        cost_state=AggregateState.PARTIAL,
        cost_sources=(_pointer(LIVE_SOURCE, "/gateway_requests/0/cost/value/total_usd"),),
        energy_joules=None,
        energy_state=AggregateState.PARTIAL,
        energy_sources=(_pointer(LIVE_SOURCE, "/gateway_requests/0/energy/value/joules"),),
        gateway_requests=(_request(1), second),
    )
    assert record.usage_state is AggregateState.PARTIAL
    assert record.cost_state is AggregateState.PARTIAL
    assert record.energy_state is AggregateState.PARTIAL


def test_conflicting_request_usage_fails_closed_without_a_total():
    usage_a = _request().usage.value
    usage_b = TokenUsage(
        input_tokens=57,
        output_tokens=33,
        cache_read_tokens=3,
        cache_write_tokens=0,
        total_tokens=90,
    )
    conflict = Evidence(
        state=EvidenceState.CONFLICT,
        value=None,
        observations=(
            EvidenceObservation(source="pi.response", value=usage_a),
            EvidenceObservation(source="gateway.token_usage", value=usage_b),
        ),
    )
    request = _request().model_copy(update={"usage": conflict})
    record = _live_record(
        tokens=None,
        usage_state=AggregateState.CONFLICT,
        gateway_requests=(request,),
    )
    assert record.tokens is None
    assert record.usage_state is AggregateState.CONFLICT

    with pytest.raises(ValidationError, match="conflict usage cannot claim"):
        _live_record(tokens=89, usage_state=AggregateState.CONFLICT, gateway_requests=(request,))


def test_no_request_accounting_is_explicitly_absent():
    record = _live_record(
        model_served=None,
        model_served_state=AggregateState.ABSENT,
        tokens=None,
        usage_state=AggregateState.ABSENT,
        usage_sources=(),
        cost_usd=None,
        cost_state=AggregateState.ABSENT,
        cost_sources=(),
        energy_joules=None,
        energy_state=AggregateState.ABSENT,
        energy_sources=(),
        gateway_requests=(),
    )
    assert record.usage_state is AggregateState.ABSENT


def test_requested_model_must_match_each_gateway_call():
    request = _request().model_copy(
        update={"requested_model": "sk-code", "requested_role": "sk-code"}
    )
    with pytest.raises(ValidationError, match="model_requested must match every request"):
        _live_record(gateway_requests=(request,))


@pytest.mark.parametrize(
    "requested,observed,comparison",
    [
        (
            SamplingSnapshot(temperature=0.2),
            _absent(),
            SamplingComparison.REQUESTED_ONLY,
        ),
        (
            SamplingSnapshot(temperature=0.2),
            _observed(SamplingSnapshot(temperature=0.7), "gateway"),
            SamplingComparison.SUBSTITUTED,
        ),
        (None, _absent(), SamplingComparison.ABSENT),
        (
            None,
            _observed(SamplingSnapshot(temperature=0.7), "gateway"),
            SamplingComparison.OBSERVED_ONLY,
        ),
    ],
)
def test_sampling_requested_vs_observed_states_are_explicit(requested, observed, comparison):
    provenance = SamplingProvenance(
        requested=requested,
        requested_source="pi.request" if requested is not None else None,
        observed=observed,
        comparison=comparison,
    )
    assert provenance.comparison is comparison


def test_sampling_cannot_claim_match_when_only_requested_value_exists():
    with pytest.raises(ValidationError, match="requested_only"):
        SamplingProvenance(
            requested=SamplingSnapshot(temperature=0.2),
            requested_source="pi.request",
            observed=_absent(),
            comparison=SamplingComparison.MATCHED,
        )


def test_sampling_conflict_preserves_both_effective_observations():
    requested = SamplingSnapshot(temperature=0.2)
    conflict = Evidence(
        state=EvidenceState.CONFLICT,
        value=None,
        observations=(
            EvidenceObservation(source="pi.response", value=requested),
            EvidenceObservation(
                source="gateway.telemetry", value=SamplingSnapshot(temperature=0.7)
            ),
        ),
    )
    provenance = SamplingProvenance(
        requested=requested,
        requested_source="pi.request",
        observed=conflict,
        comparison=SamplingComparison.CONFLICT,
    )
    assert len(provenance.observed.observations) == 2


def _historical_record(**overrides) -> RunRecord:
    data = _live_record().model_dump()
    data.update(
        origin=RecordOrigin.HISTORICAL,
        agent=None,
        session_id=None,
        node=None,
        agent_source=None,
        session_id_source=None,
        node_source=None,
        attribution_state=AttributionState.HISTORICAL_UNATTRIBUTABLE,
        adapter=None,
        model_requested=None,
        model_served=None,
        model_served_state=AggregateState.ABSENT,
        effort_tier=None,
        grader_model=None,
        score=None,
        passed=None,
        gate_state=GateState.HISTORICAL_UNATTRIBUTABLE,
        record_sources=(LEGACY_SOURCE,),
        task_shape=TaskShape(
            size="M",
            risk="medium",
            sensitivity="internal",
            model_class="creative",
            source=_pointer(LEGACY_SOURCE, "/work_grade"),
        ),
        usage_scope=AggregateScope.LEGACY_RECORD,
        usage_sources=(_pointer(LEGACY_SOURCE, "/tokens"),),
        cost_scope=AggregateScope.LEGACY_RECORD,
        cost_sources=(_pointer(LEGACY_SOURCE, "/cost_usd"),),
        energy_scope=AggregateScope.LEGACY_RECORD,
        energy_sources=(_pointer(LEGACY_SOURCE, "/energy_joules"),),
        started_at=None,
        finished_at=None,
        recorded_at=NOW,
        gateway_requests=(),
    )
    data.update(overrides)
    return RunRecord(**data)


def test_historical_record_is_explicitly_unattributable_but_keeps_known_cost():
    record = _historical_record()
    assert record.origin is RecordOrigin.HISTORICAL
    assert record.attribution_state is AttributionState.HISTORICAL_UNATTRIBUTABLE
    assert record.agent is record.session_id is record.node is None
    assert record.started_at is record.finished_at is None
    assert record.cost_usd == 0.0031
    assert record.cost_sources == (_pointer(LEGACY_SOURCE, "/cost_usd"),)
    assert record.record_sources == (LEGACY_SOURCE,)


def test_historical_record_source_must_bind_exact_legacy_bytes():
    with pytest.raises(ValidationError, match="must end with a sha256 digest"):
        _historical_record(record_sources=("autopilot-cost/ledger.jsonl:row-12",))


@pytest.mark.parametrize("forbidden", ["inferred", "default", "projection"])
def test_historical_record_rejects_non_firsthand_source_classes(forbidden):
    source = f"legacy/{forbidden}/row#sha256:" + "c" * 64
    with pytest.raises(ValidationError, match="inferred/default/projection"):
        _historical_record(record_sources=(source,))


def test_retained_historical_fields_require_exact_digest_bound_pointers():
    with pytest.raises(ValidationError, match="exact scalar field"):
        _historical_record(usage_sources=(_pointer(LEGACY_SOURCE, "/estimated_tokens"),))

    other_source = "legacy/other-row#sha256:" + "d" * 64
    with pytest.raises(ValidationError, match="not declared in record_sources"):
        _historical_record(cost_sources=(_pointer(other_source, "/cost_usd"),))

    wrong_shape = TaskShape(
        size="M",
        risk="medium",
        sensitivity="internal",
        model_class="creative",
        source=_pointer(LEGACY_SOURCE, "/derived_work_grade"),
    )
    with pytest.raises(ValidationError, match="must point to /work_grade"):
        _historical_record(task_shape=wrong_shape)


@pytest.mark.parametrize(
    "overrides",
    [
        {"tokens": None, "usage_state": AggregateState.ABSENT, "usage_sources": ()},
        {"cost_usd": None, "cost_state": AggregateState.ABSENT, "cost_sources": ()},
        {"task_shape": None},
    ],
)
def test_historical_usage_cost_or_task_shape_may_be_explicitly_absent(overrides):
    record = _historical_record(**overrides)
    if "tokens" in overrides:
        assert record.usage_state is AggregateState.ABSENT
    if "cost_usd" in overrides:
        assert record.cost_state is AggregateState.ABSENT
    if "task_shape" in overrides:
        assert record.task_shape is None


@pytest.mark.parametrize("pointer", ["/projection/tokens", "/rows/*/tokens", "/rows/../tokens"])
def test_source_pointer_rejects_projection_wildcard_and_parent_paths(pointer):
    with pytest.raises(ValidationError):
        _pointer(LEGACY_SOURCE, pointer)


@pytest.mark.parametrize("field", ["started_at", "finished_at"])
def test_historical_execution_timestamps_are_explicitly_absent(field):
    with pytest.raises(ValidationError, match="execution timestamps must be explicitly absent"):
        _historical_record(**{field: NOW})


def test_historical_validator_rejects_invented_identity_or_request_join():
    with pytest.raises(ValidationError, match="cannot be backfilled"):
        _historical_record(agent="lumina", agent_source="default")
    with pytest.raises(ValidationError, match="cannot be backfilled"):
        _historical_record(gateway_requests=(_request(),))


@pytest.mark.parametrize(
    "field,value",
    [
        ("adapter", "pi"),
        ("model_requested", "sk-creative"),
        ("effort_tier", "medium"),
        ("grader_model", "sk-code"),
    ],
)
def test_historical_validator_rejects_unsourced_route_attribution(field, value):
    with pytest.raises(ValidationError, match="execution/model attribution cannot be backfilled"):
        _historical_record(**{field: value})


@pytest.mark.parametrize(
    "field,source_field",
    [
        ("agent", "agent_source"),
        ("session_id", "session_id_source"),
        ("node", "node_source"),
    ],
)
def test_live_identity_requires_each_observed_field(field, source_field):
    with pytest.raises(ValidationError, match="observed identity requires"):
        _live_record(**{field: None, source_field: None})


def test_live_identity_rejects_partial_disposition_even_with_two_fields():
    with pytest.raises(ValidationError, match="fully observed identity"):
        _live_record(attribution_state=AttributionState.PARTIAL)


def test_timestamps_are_timezone_aware_and_monotonic():
    with pytest.raises(ValidationError, match="timezone-aware"):
        _live_record(started_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValidationError, match="timestamps must satisfy"):
        _live_record(finished_at=NOW - timedelta(seconds=1))


def test_live_records_require_controller_known_route_fields():
    with pytest.raises(ValidationError, match="live records require model_requested"):
        _live_record(model_requested=None)


def test_observed_gate_requires_both_score_and_pass_fail():
    with pytest.raises(ValidationError, match="requires score and pass/fail"):
        _live_record(score=None)


def test_nonfinite_accounting_is_not_durable_json_evidence():
    with pytest.raises(ValidationError):
        _live_record(cost_usd=float("nan"))


def test_schema_declares_existing_atomic_journal_destination_but_has_no_writer_dependency():
    assert RUN_RECORD_JOURNAL_TEMPLATE.endswith("coordination/autopilot/runs/<run_id>.json")
    assert RUN_RECORD_JOURNAL_FIELD == "items.<card_id>.run_records[]"

    source_root = Path(__file__).resolve().parents[1] / "src" / "skharness" / "autocode"
    production_importers = []
    for path in source_root.rglob("*.py"):
        if path.name == "run_record.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            absolute = isinstance(node, ast.Import) and any(
                alias.name == "skharness.autocode.run_record" for alias in node.names
            )
            from_absolute = (
                isinstance(node, ast.ImportFrom)
                and node.level == 0
                and node.module == "skharness.autocode.run_record"
            )
            from_relative = (
                isinstance(node, ast.ImportFrom)
                and node.level == 1
                and node.module == "run_record"
            )
            package_import = (
                isinstance(node, ast.ImportFrom)
                and (
                    (node.level == 0 and node.module == "skharness.autocode")
                    or (node.level == 1 and node.module is None)
                )
                and any(alias.name == "run_record" for alias in node.names)
            )
            if absolute or from_absolute or from_relative or package_import:
                production_importers.append(path)
                break
    assert production_importers == []
