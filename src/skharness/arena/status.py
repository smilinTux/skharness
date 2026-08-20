"""Truthful derived status and bounded observability for the Evolution Arena.

This module owns no lifecycle state.  It derives query views from immutable specs,
append-only events, result providers, and the volatile scheduler snapshot.  Detailed
identities stay in authenticated JSON records; the metrics renderer deliberately
admits only enumerated, bounded labels.
"""

from __future__ import annotations

import json
import math
import os
import threading
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .lineage import LineageGraph
from .metrics import (
    MetricDirection,
    MetricObjective,
    VerifiedParetoCandidate,
    verified_pareto_frontier,
)
from .models import ChallengeSpec, Experiment, ExperimentEvent, ExperimentState, Result
from .scheduler import LeaseScheduler
from .store import ArenaStore


@dataclass(frozen=True)
class ProbeResult:
    """One dependency observation; ``None`` is explicitly unknown, never healthy."""

    ok: bool | None
    detail: str = ""

    @property
    def state(self) -> str:
        return "unknown" if self.ok is None else ("ok" if self.ok else "error")


Probe = Callable[[], ProbeResult | bool | None]


def _probe(probe: Probe | None) -> ProbeResult:
    if probe is None:
        return ProbeResult(None, "probe not configured")
    try:
        value = probe()
    except Exception as exc:  # dependency failure is data, not an API 500
        return ProbeResult(False, f"{type(exc).__name__}: {exc}")
    if isinstance(value, ProbeResult):
        return value
    return ProbeResult(value, "")


class BoundedArenaMetrics:
    """Small in-process registry whose labels cannot contain run identities."""

    _STATES = frozenset(item.value for item in ExperimentState)
    _VERDICTS = frozenset({"unverified", "verifying", "valid", "invalid", "inconclusive"})
    _OUTCOMES = frozenset({"admitted", "capacity", "duplicate", "invalid", "expired"})
    _SIGNALS = frozenset({
        "attempts", "lease_expiry", "tokens", "cost", "joules", "oom",
        "cancellation", "gateway_errors", "frontier_movement", "promotions",
        "rollbacks", "delayed_incidents",
    })

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._transitions: Counter[str] = Counter()
        self._verifications: Counter[str] = Counter()
        self._admissions: Counter[str] = Counter()
        self._gateway_errors = 0
        self._signals: Counter[str] = Counter()

    @staticmethod
    def _bounded(value: str, allowed: frozenset[str], label: str) -> str:
        if value not in allowed:
            raise ValueError(f"unsupported {label} label {value!r}")
        return value

    def transition(self, state: ExperimentState | str) -> None:
        value = state.value if isinstance(state, ExperimentState) else state
        with self._lock:
            self._transitions[self._bounded(value, self._STATES, "state")] += 1

    def verification(self, verdict: str) -> None:
        with self._lock:
            self._verifications[self._bounded(verdict, self._VERDICTS, "verdict")] += 1

    def admission(self, outcome: str) -> None:
        with self._lock:
            self._admissions[self._bounded(outcome, self._OUTCOMES, "outcome")] += 1

    def gateway_error(self) -> None:
        with self._lock:
            self._gateway_errors += 1

    def add(self, signal: str, value: float = 1.0) -> None:
        """Record a bounded counter signal; identities are not accepted as labels."""
        if not math.isfinite(value) or value < 0:
            raise ValueError("metric counter increments must be finite and non-negative")
        with self._lock:
            self._signals[self._bounded(signal, self._SIGNALS, "signal")] += value

    def render(self, status: Mapping[str, Any]) -> str:
        """Render OpenMetrics text without challenge/experiment/run/card labels."""
        scheduler = status.get("scheduler", {})
        lines = [
            "# HELP skharness_arena_ready Whether all required arena dependencies are ready.",
            "# TYPE skharness_arena_ready gauge",
            f"skharness_arena_ready {1 if status.get('ready') else 0}",
            "# HELP skharness_arena_active_leases Current active arena leases.",
            "# TYPE skharness_arena_active_leases gauge",
            f"skharness_arena_active_leases {int(scheduler.get('active_leases', 0))}",
        ]
        with self._lock:
            for state, value in sorted(self._transitions.items()):
                lines.append(f'skharness_arena_transitions_total{{state="{state}"}} {value}')
            for verdict, value in sorted(self._verifications.items()):
                lines.append(
                    f'skharness_arena_verifications_total{{verdict="{verdict}"}} {value}'
                )
            for outcome, value in sorted(self._admissions.items()):
                lines.append(f'skharness_arena_admissions_total{{outcome="{outcome}"}} {value}')
            lines.append(f"skharness_arena_gateway_errors_total {self._gateway_errors}")
            for signal, value in sorted(self._signals.items()):
                lines.append(f'skharness_arena_signal_total{{signal="{signal}"}} {value}')
        return "\n".join(lines) + "\n"


class ArenaStatusService:
    """Read-only operational and domain views over arena authoritative state."""

    def __init__(
        self,
        *,
        store: ArenaStore | None = None,
        scheduler: LeaseScheduler | None = None,
        experiments: Callable[[], Iterable[Experiment]] | None = None,
        results: Callable[[], Iterable[Result]] | None = None,
        gateway_probe: Probe | None = None,
        verifier_probe: Probe | None = None,
        gpu_probe: Probe | None = None,
        require_gateway: bool = True,
        require_verifier: bool = True,
        require_gpu: bool = False,
        metrics: BoundedArenaMetrics | None = None,
    ) -> None:
        self.store = store
        self.scheduler = scheduler
        self._experiments = experiments or (store.read_experiments if store else lambda: ())
        self._results = results or (store.read_results if store else lambda: ())
        self._probes = {
            "store": self._store_probe,
            "skgateway": gateway_probe,
            "verifier": verifier_probe,
            "gpu": gpu_probe,
        }
        self._required = {
            "store": store is not None,
            "skgateway": require_gateway,
            "verifier": require_verifier,
            "gpu": require_gpu,
        }
        self.metrics = metrics or BoundedArenaMetrics()

    def _store_probe(self) -> ProbeResult:
        if self.store is None:
            return ProbeResult(None, "arena store not configured")
        self.store.read_all_events()
        required_dirs = (self.store.events_dir, self.store.artifacts_dir,
                         self.store.specs_dir, self.store.experiments_dir,
                         self.store.results_dir)
        unwritable = [str(path) for path in required_dirs if not os.access(path, os.W_OK)]
        if unwritable:
            return ProbeResult(False, "unwritable arena directories: " + ", ".join(unwritable))
        return ProbeResult(True)

    def liveness(self) -> dict[str, Any]:
        return {"live": True, "component": "skharness-arena"}

    def readiness(self) -> dict[str, Any]:
        dependencies: dict[str, dict[str, Any]] = {}
        ready = True
        for name, probe in self._probes.items():
            result = _probe(probe)
            required = self._required[name]
            dependencies[name] = {
                "state": result.state,
                "required": required,
                "detail": result.detail,
            }
            if required and result.ok is not True:
                ready = False
        return {"ready": ready, "dependencies": dependencies}

    def status(self) -> dict[str, Any]:
        ready = self.readiness()
        events = self._events()
        current = self._current_attempts(events)
        counts = Counter(row["state"] for row in current)
        return {
            **self.liveness(),
            **ready,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "attempts": {"total": len(current), "by_state": dict(sorted(counts.items()))},
            "scheduler": self.scheduler.snapshot() if self.scheduler else {
                "active_leases": 0, "configured": False
            },
        }

    def _events(self) -> list[ExperimentEvent]:
        return self.store.read_all_events() if self.store is not None else []

    @staticmethod
    def _current_attempts(events: Sequence[ExperimentEvent]) -> list[dict[str, Any]]:
        latest: dict[tuple[str, int], ExperimentEvent] = {}
        for event in events:
            key = (event.experiment_id, event.attempt)
            prior = latest.get(key)
            if prior is None or (event.timestamp, event.writer_id, event.sequence) > (
                prior.timestamp, prior.writer_id, prior.sequence
            ):
                latest[key] = event
        return [
            {
                "experiment_id": event.experiment_id,
                "attempt": event.attempt,
                "state": event.to_state.value,
                "updated_at": event.timestamp.isoformat(),
                "writer_id": event.writer_id,
                "event_hash": event.event_hash,
                "payload": event.payload,
            }
            for _, event in sorted(latest.items())
        ]

    def attempts(
        self, *, challenge_id: str | None = None, state: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        # challenge_id is carried in proposal/admission payloads until Experiment
        # persistence is composed; missing evidence does not get guessed.
        events = self._events()
        rows = self._current_attempts(events)
        if challenge_id is not None:
            challenge_by_attempt: dict[tuple[str, int], str] = {}
            for event in events:
                observed = event.payload.get("challenge_id")
                if isinstance(observed, str):
                    challenge_by_attempt[(event.experiment_id, event.attempt)] = observed
            rows = [row for row in rows if challenge_by_attempt.get(
                (row["experiment_id"], row["attempt"])) == challenge_id]
        if state is not None:
            rows = [row for row in rows if row["state"] == state]
        return rows[: max(1, min(limit, 500))]

    def leases(self) -> list[dict[str, Any]]:
        return self.scheduler.lease_records() if self.scheduler is not None else []

    def challenges(self) -> list[dict[str, Any]]:
        if self.store is None:
            return []
        rows = []
        for path in sorted(self.store.specs_dir.glob("*.json")):
            spec = ChallengeSpec.model_validate_json(path.read_bytes())
            rows.append({
                "id": spec.id,
                "version": spec.version,
                "title": spec.title,
                "content_hash": spec.content_hash,
                "hardware_class": spec.hardware.hardware_class,
                "required_model": spec.model.model_id,
            })
        return rows

    def verifications(self, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = sorted(self._results(), key=lambda row: (row.created_at, row.experiment_id))
        return [
            {
                "experiment_id": row.experiment_id,
                "challenge_hash": row.challenge_hash,
                "verification": row.verification.value,
                "reason": row.verification_reason,
                "result_hash": row.content_hash,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows[-max(1, min(limit, 500)) :]
        ]

    def lineage(self, experiment_id: str) -> dict[str, Any] | None:
        experiments = tuple(self._experiments())
        by_id = {item.id: item for item in experiments}
        if experiment_id not in by_id:
            return None
        graph = LineageGraph(experiments)
        item = by_id[experiment_id]
        return {
            "experiment_id": experiment_id,
            "parent_id": item.parent_id,
            "reproduces_id": item.reproduces_id,
            "ancestors": [row.id for row in graph.ancestors(experiment_id)],
            "descendants": [row.id for row in graph.descendants(experiment_id)],
        }

    def frontier(
        self, challenge_hash: str, objectives: Sequence[MetricObjective]
    ) -> list[dict[str, Any]]:
        candidates = []
        for result in self._results():
            if result.challenge_hash != challenge_hash or result.verification.value != "valid":
                continue
            summaries = {
                measurement.metric: _measurement_summary(measurement)
                for measurement in result.measurements
            }
            candidates.append(VerifiedParetoCandidate.from_result(result, summaries))
        return [
            {"experiment_id": row.candidate.experiment_id,
             "metrics": {name: vars(summary) for name, summary in row.candidate.metrics.items()},
             "verification_evidence_id": row.result_hash}
            for row in verified_pareto_frontier(candidates, objectives)
        ]


def _measurement_summary(measurement):
    from .metrics import MetricSummary

    values = [observation.value for observation in measurement.observations]
    return MetricSummary(
        count=len(values),
        mean=measurement.mean,
        standard_deviation=measurement.standard_deviation,
        minimum=min(values),
        maximum=max(values),
        confidence_low=measurement.confidence_low
        if measurement.confidence_low is not None else measurement.mean,
        confidence_high=measurement.confidence_high
        if measurement.confidence_high is not None else measurement.mean,
    )


def objectives_from_query(raw: str) -> tuple[MetricObjective, ...]:
    """Parse ``name:maximize,name:minimize`` with no executable/config syntax."""
    objectives = []
    for part in raw.split(","):
        try:
            name, direction = part.split(":", 1)
            objectives.append(MetricObjective(name.strip(), MetricDirection(direction.strip())))
        except (ValueError, TypeError) as exc:
            raise ValueError("objectives must be name:maximize,name:minimize") from exc
    if not objectives:
        raise ValueError("at least one objective is required")
    return tuple(objectives)


def structured_record(record: Mapping[str, Any]) -> str:
    """Stable JSON helper for high-cardinality logs/traces."""
    return json.dumps(record, default=str, sort_keys=True, separators=(",", ":"))
