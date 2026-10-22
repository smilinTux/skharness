"""Repeated-trial statistics and deterministic Pareto-front computation."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Mapping, Sequence

from skharness.arena.models import Result, VerificationState


class MetricDirection(str, Enum):
    """Whether a metric improves by increasing or decreasing its value."""

    MAXIMIZE = "maximize"
    MINIMIZE = "minimize"


@dataclass(frozen=True)
class MetricObjective:
    """One objective and, optionally, a hard admissibility interval."""

    name: str
    direction: MetricDirection
    minimum: float | None = None
    maximum: float | None = None
    tolerance: float = 0.0

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("metric objective name must not be empty")
        for label, value in (
            ("minimum", self.minimum),
            ("maximum", self.maximum),
            ("tolerance", self.tolerance),
        ):
            if value is not None and not math.isfinite(value):
                raise ValueError(f"{label} must be finite")
        if self.minimum is not None and self.maximum is not None:
            if self.minimum > self.maximum:
                raise ValueError("minimum must not exceed maximum")
        if self.tolerance < 0:
            raise ValueError("tolerance must be non-negative")

    def accepts(self, value: float) -> bool:
        if not math.isfinite(value):
            return False
        if self.minimum is not None and value < self.minimum - self.tolerance:
            return False
        if self.maximum is not None and value > self.maximum + self.tolerance:
            return False
        return True


@dataclass(frozen=True)
class MetricSummary:
    """Distribution summary; CI is a two-sided normal approximation."""

    count: int
    mean: float
    standard_deviation: float
    minimum: float
    maximum: float
    confidence_low: float
    confidence_high: float


def summarize(values: Iterable[float], *, confidence_z: float = 1.96) -> MetricSummary:
    """Summarize finite observations without silently dropping bad samples."""

    observations = tuple(float(value) for value in values)
    if not observations:
        raise ValueError("at least one observation is required")
    if not math.isfinite(confidence_z) or confidence_z < 0:
        raise ValueError("confidence_z must be finite and non-negative")
    if not all(math.isfinite(value) for value in observations):
        raise ValueError("metric observations must all be finite")

    mean = statistics.fmean(observations)
    deviation = statistics.stdev(observations) if len(observations) > 1 else 0.0
    margin = confidence_z * deviation / math.sqrt(len(observations))
    return MetricSummary(
        count=len(observations),
        mean=mean,
        standard_deviation=deviation,
        minimum=min(observations),
        maximum=max(observations),
        confidence_low=mean - margin,
        confidence_high=mean + margin,
    )


@dataclass(frozen=True)
class ParetoCandidate:
    """A verified experiment's objective summaries."""

    experiment_id: str
    metrics: Mapping[str, MetricSummary]


@dataclass(frozen=True)
class VerifiedParetoCandidate:
    """Candidate whose admission is cryptographically bound to a valid Result."""

    candidate: ParetoCandidate
    result_hash: str

    @classmethod
    def from_result(
        cls, result: Result, metrics: Mapping[str, MetricSummary]
    ) -> "VerifiedParetoCandidate":
        if result.verification is not VerificationState.VALID:
            raise ValueError("Pareto admission requires a verified-valid result")
        return cls(ParetoCandidate(result.experiment_id, metrics), result.content_hash)


def _dominates(
    left: ParetoCandidate,
    right: ParetoCandidate,
    objectives: Sequence[MetricObjective],
) -> bool:
    weakly_better = True
    strictly_better = False
    for objective in objectives:
        left_value = left.metrics[objective.name].mean
        right_value = right.metrics[objective.name].mean
        tolerance = objective.tolerance
        if objective.direction is MetricDirection.MAXIMIZE:
            weakly_better &= left_value >= right_value - tolerance
            strictly_better |= left_value > right_value + tolerance
        else:
            weakly_better &= left_value <= right_value + tolerance
            strictly_better |= left_value < right_value - tolerance
    return weakly_better and strictly_better


def pareto_frontier(
    candidates: Iterable[ParetoCandidate],
    objectives: Sequence[MetricObjective],
) -> tuple[ParetoCandidate, ...]:
    """Return admissible non-dominated candidates, sorted by stable identity.

    Missing, non-finite, and hard-constraint-violating candidates are excluded.
    Duplicate experiment IDs are rejected because accepting both would make the
    derived view depend on input ordering.
    """

    if not objectives:
        raise ValueError("at least one Pareto objective is required")
    names = [objective.name for objective in objectives]
    if len(names) != len(set(names)):
        raise ValueError("Pareto objective names must be unique")

    ordered = sorted(candidates, key=lambda candidate: candidate.experiment_id)
    ids = [candidate.experiment_id for candidate in ordered]
    if any(not experiment_id.strip() for experiment_id in ids):
        raise ValueError("experiment IDs must not be empty")
    if len(ids) != len(set(ids)):
        raise ValueError("experiment IDs must be unique")

    admissible = []
    for candidate in ordered:
        if all(
            objective.name in candidate.metrics
            and objective.accepts(candidate.metrics[objective.name].mean)
            for objective in objectives
        ):
            admissible.append(candidate)

    return tuple(
        candidate
        for candidate in admissible
        if not any(
            _dominates(other, candidate, objectives)
            for other in admissible
            if other.experiment_id != candidate.experiment_id
        )
    )


def verified_pareto_frontier(
    candidates: Iterable[VerifiedParetoCandidate],
    objectives: Sequence[MetricObjective],
) -> tuple[VerifiedParetoCandidate, ...]:
    """Compute a frontier whose input type can only be built from valid results."""

    materialized = tuple(candidates)
    by_id = {item.candidate.experiment_id: item for item in materialized}
    if len(by_id) != len(materialized):
        raise ValueError("verified candidate experiment IDs must be unique")
    frontier = pareto_frontier((item.candidate for item in materialized), objectives)
    return tuple(by_id[item.experiment_id] for item in frontier)
