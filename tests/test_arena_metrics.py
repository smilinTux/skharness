from __future__ import annotations

import math

import pytest

from skharness.arena.metrics import (
    MetricDirection,
    MetricObjective,
    ParetoCandidate,
    pareto_frontier,
    summarize,
)


def test_summary_preserves_distribution_and_confidence_interval():
    result = summarize([10.0, 12.0, 14.0])

    assert result.count == 3
    assert result.mean == 12.0
    assert result.standard_deviation == 2.0
    assert result.minimum == 10.0
    assert result.maximum == 14.0
    assert result.confidence_low == pytest.approx(9.736786, rel=1e-5)
    assert result.confidence_high == pytest.approx(14.263214, rel=1e-5)


@pytest.mark.parametrize("values", [[], [math.nan], [math.inf], [1.0, -math.inf]])
def test_summary_refuses_empty_or_non_finite_evidence(values):
    with pytest.raises(ValueError):
        summarize(values)


def _candidate(name, throughput, latency, quality=0.9):
    return ParetoCandidate(
        experiment_id=name,
        metrics={
            "throughput": summarize([throughput]),
            "latency": summarize([latency]),
            "quality": summarize([quality]),
        },
    )


def test_frontier_is_multi_objective_constrained_and_deterministic():
    objectives = (
        MetricObjective("throughput", MetricDirection.MAXIMIZE),
        MetricObjective("latency", MetricDirection.MINIMIZE),
        MetricObjective("quality", MetricDirection.MAXIMIZE, minimum=0.8),
    )
    candidates = [
        _candidate("balanced", 100, 10),
        _candidate("dominated", 90, 12),
        _candidate("fast", 120, 15),
        _candidate("invalid-quality", 1000, 1, quality=0.1),
    ]

    forward = pareto_frontier(candidates, objectives)
    reverse = pareto_frontier(reversed(candidates), objectives)

    assert [item.experiment_id for item in forward] == ["balanced", "fast"]
    assert forward == reverse


def test_frontier_rejects_duplicate_identity_and_objective_names():
    candidate = _candidate("same", 1, 1)
    objective = MetricObjective("throughput", MetricDirection.MAXIMIZE)
    with pytest.raises(ValueError, match="experiment IDs must be unique"):
        pareto_frontier([candidate, candidate], [objective])
    with pytest.raises(ValueError, match="objective names must be unique"):
        pareto_frontier([candidate], [objective, objective])


def test_tolerance_does_not_create_false_strict_dominance():
    objective = MetricObjective("throughput", MetricDirection.MAXIMIZE, tolerance=0.1)
    candidates = [_candidate("a", 10.00, 1), _candidate("b", 10.05, 1)]
    assert [item.experiment_id for item in pareto_frontier(candidates, [objective])] == [
        "a",
        "b",
    ]
