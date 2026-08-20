"""Durable experiment lifecycle controller over volatile resource leases."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timezone

from .models import ExperimentEvent, ExperimentState, Provenance
from .scheduler import Admission, AdmissionReason, AttemptRequest, LeaseScheduler
from .status import BoundedArenaMetrics
from .store import ArenaStore


class InvalidTransitionError(RuntimeError):
    """A caller attempted a lifecycle transition outside the arena state machine."""


_TRANSITIONS = {
    None: {ExperimentState.PROPOSED},
    ExperimentState.PROPOSED: {ExperimentState.ADMITTED, ExperimentState.CANCELLED},
    ExperimentState.ADMITTED: {ExperimentState.RUNNING, ExperimentState.CANCELLED,
                               ExperimentState.FAILED},
    ExperimentState.RUNNING: {ExperimentState.PROVISIONAL, ExperimentState.CANCELLED,
                              ExperimentState.FAILED},
    ExperimentState.PROVISIONAL: {ExperimentState.VERIFYING},
    ExperimentState.VERIFYING: {ExperimentState.VALID, ExperimentState.INVALID,
                                ExperimentState.INCONCLUSIVE},
}


class ArenaController:
    """Coordinate leases and append state changes before external side effects.

    The controller is synchronous by design. A process/container supervisor can call
    ``running`` immediately before spawn and ``finish_run`` after collection. Durable
    state remains reconstructable independently of this object's memory.
    """

    def __init__(
        self,
        store: ArenaStore,
        scheduler: LeaseScheduler,
        *,
        writer_id: str,
        actor: str,
        node: str,
        session_id: str,
        now: Callable[[], datetime] | None = None,
        metrics: BoundedArenaMetrics | None = None,
    ) -> None:
        self.store = store
        self.scheduler = scheduler
        self.writer_id = writer_id
        self.actor = actor
        self.node = node
        self.session_id = session_id
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.metrics = metrics
        self._lease_by_attempt: dict[tuple[str, int], str] = {}
        self._transition_lock = threading.RLock()

    def _events(self, experiment_id: str, attempt: int) -> list[ExperimentEvent]:
        return [event for event in self.store.read_all_events()
                if event.experiment_id == experiment_id and event.attempt == attempt]

    def state(self, experiment_id: str, attempt: int = 1) -> ExperimentState | None:
        events = self._events(experiment_id, attempt)
        return events[-1].to_state if events else None

    def _append(
        self,
        experiment_id: str,
        attempt: int,
        to_state: ExperimentState,
        *,
        payload: dict | None = None,
    ) -> ExperimentEvent:
        with self._transition_lock:
            current = self.state(experiment_id, attempt)
            if to_state not in _TRANSITIONS.get(current, set()):
                raise InvalidTransitionError(
                    f"cannot transition {current!r} -> {to_state.value!r}"
                )
            segment = self.store.read_segment(self.writer_id)
            event = ExperimentEvent(
                event_id=uuid.uuid4().hex,
                writer_id=self.writer_id,
                sequence=len(segment) + 1,
                experiment_id=experiment_id,
                attempt=attempt,
                from_state=current,
                to_state=to_state,
                timestamp=self._now(),
                provenance=Provenance(
                    actor=self.actor,
                    node=self.node,
                    session_id=self.session_id,
                    action=f"arena.experiment.{to_state.value}",
                    target=f"experiment:{experiment_id}:attempt:{attempt}",
                    observed_prior_state=current.value if current else None,
                ),
                payload=payload or {},
                prior_event_hash=segment[-1].event_hash if segment else None,
            )
            appended = self.store.append_event(event)
        if self.metrics is not None:
            self.metrics.transition(to_state)
            self.metrics.add("attempts")
        return appended

    def propose(self, experiment_id: str, attempt: int = 1, *, payload=None) -> ExperimentEvent:
        return self._append(experiment_id, attempt, ExperimentState.PROPOSED, payload=payload)

    def admit(self, request: AttemptRequest, *, attempt_number: int = 1) -> Admission:
        if request.attempt_id != str(attempt_number):
            raise ValueError("AttemptRequest.attempt_id must equal attempt_number")
        current = self.state(request.experiment_id, attempt_number)
        if current not in {
            ExperimentState.PROPOSED, ExperimentState.ADMITTED, ExperimentState.RUNNING
        }:
            raise InvalidTransitionError("attempt must be proposed before admission")
        admission = self.scheduler.admit(request)
        # After a controller restart the durable state may say admitted/running
        # while the volatile scheduler has forgotten its lease. Never interpret
        # that as permission to spawn a second Pi process. Recovery must first
        # terminalize the orphan; a new numbered attempt can then be proposed.
        if current in {ExperimentState.ADMITTED, ExperimentState.RUNNING} and not (
            admission.admitted and admission.duplicate
        ):
            if admission.lease is not None:
                self.scheduler.release(admission.lease.lease_id)
            if self.metrics is not None:
                self.metrics.admission("duplicate")
            return Admission(False, reason=AdmissionReason.DUPLICATE)
        if self.metrics is not None:
            outcome = "admitted" if admission.admitted else (
                admission.reason.value if admission.reason is not None else "invalid"
            )
            self.metrics.admission(outcome)
        if admission.admitted and not admission.duplicate and current == ExperimentState.PROPOSED:
            lease = admission.lease
            self._append(
                request.experiment_id,
                attempt_number,
                ExperimentState.ADMITTED,
                payload={"lease_id": lease.lease_id, "expires_at": lease.expires_at},
            )
            self._lease_by_attempt[(request.experiment_id, attempt_number)] = lease.lease_id
        elif admission.admitted and admission.duplicate:
            self._lease_by_attempt[(request.experiment_id, attempt_number)] = admission.lease.lease_id
        return admission

    def running(self, experiment_id: str, attempt: int = 1) -> ExperimentEvent:
        """Record running immediately before the supervisor spawns the worker."""
        return self._append(experiment_id, attempt, ExperimentState.RUNNING)

    def finish_run(
        self,
        experiment_id: str,
        attempt: int = 1,
        *,
        successful: bool,
        payload: dict | None = None,
    ) -> ExperimentEvent:
        state = ExperimentState.PROVISIONAL if successful else ExperimentState.FAILED
        event = self._append(experiment_id, attempt, state, payload=payload)
        if self.metrics is not None and not successful and (payload or {}).get("reason") == "oom":
            self.metrics.add("oom")
        self._release(experiment_id, attempt)
        return event

    def heartbeat(self, experiment_id: str, attempt: int = 1) -> bool:
        lease_id = self._lease_by_attempt.get((experiment_id, attempt))
        return bool(lease_id and self.scheduler.heartbeat(lease_id))

    def cancel(
        self,
        experiment_id: str,
        attempt: int = 1,
        *,
        stop: Callable[[], None] | None = None,
        payload: dict | None = None,
    ) -> ExperimentEvent:
        """Invoke the supervisor stop hook before recording terminal cancellation."""
        # Hold the transition lock across stop + terminal append. Once cancellation
        # has begun, a process-exit thread cannot win with provisional/failed or
        # append from a stale writer head; cancellation is the terminal truth.
        with self._transition_lock:
            if stop is not None:
                stop()
            if self.metrics is not None:
                self.metrics.add("cancellation")
            event = self._append(experiment_id, attempt, ExperimentState.CANCELLED,
                                 payload=payload)
            self._release(experiment_id, attempt)
            return event

    def _release(self, experiment_id: str, attempt: int) -> None:
        lease_id = self._lease_by_attempt.pop((experiment_id, attempt), None)
        if lease_id:
            self.scheduler.release(lease_id)

    def reclaim_orphans(self) -> list[ExperimentEvent]:
        """Fail attempts whose leases expired while admitted or running."""
        events: list[ExperimentEvent] = []
        for lease in self.scheduler.reclaim_expired():
            key = (lease.request.experiment_id, int(lease.request.attempt_id))
            self._lease_by_attempt.pop(key, None)
            current = self.state(*key)
            if current in {ExperimentState.ADMITTED, ExperimentState.RUNNING}:
                if self.metrics is not None:
                    self.metrics.admission("expired")
                    self.metrics.add("lease_expiry")
                events.append(self._append(
                    key[0], key[1], ExperimentState.FAILED,
                    payload={"reason": "lease_expired", "lease_id": lease.lease_id},
                ))
        return events
