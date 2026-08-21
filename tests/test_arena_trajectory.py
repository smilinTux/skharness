import json

import pytest

from skharness.arena.trajectory import (
    CardSize,
    EvidenceModelRouter,
    ModelTrial,
    PhaseBudget,
    UnqualifiedRouteError,
    compact_pi_events,
)


def _trial(model, size, *, ok=True, duration=100, edit=20):
    return ModelTrial(model, size, ok, duration, edit)


def test_router_requires_fixed_s_m_l_evidence_and_a_success_for_target():
    incomplete = EvidenceModelRouter([_trial("fast", CardSize.SMALL)])
    with pytest.raises(UnqualifiedRouteError):
        incomplete.route(CardSize.SMALL)

    failed = EvidenceModelRouter(
        [_trial("fast", size, ok=size is not CardSize.SMALL) for size in CardSize]
    )
    with pytest.raises(UnqualifiedRouteError):
        failed.route(CardSize.SMALL)


def test_router_prefers_measured_success_duration_then_first_edit():
    trials = []
    for size in CardSize:
        trials.extend(
            [
                _trial("qwen", size, duration=200, edit=30),
                _trial("ornith", size, duration=100, edit=40),
            ]
        )
    assert EvidenceModelRouter(trials).route(CardSize.LARGE) == "ornith"


def test_phase_budget_is_positive_and_emits_a_bounded_contract():
    budget = PhaseBudget(1, 2, 3, 4)
    assert budget.total_s == 10
    assert "test 4s" in budget.prompt_contract()
    with pytest.raises(ValueError):
        PhaseBudget(0, 2, 3, 4)


def test_compaction_deduplicates_envelopes_and_bounds_tool_output():
    repeated = json.dumps({"type": "tool", "result": "x" * 500}).encode()
    compacted = compact_pi_events(repeated + b"\n" + repeated + b"\n", max_event_bytes=256)
    lines = [json.loads(line) for line in compacted.splitlines()]
    assert lines[0]["type"] == "evidence_truncated"
    assert lines[0]["bytes"] > 256
    assert lines[1] == {"type": "duplicate_events", "count": 1}


def test_plain_process_output_remains_byte_exact():
    assert compact_pi_events(b"diagnostic without newline") == b"diagnostic without newline"
