from datetime import datetime, timezone

import pytest

from skharness.arena.controller import ArenaController, InvalidTransitionError
from skharness.arena.models import ExperimentState
from skharness.arena.scheduler import AttemptRequest, LeaseScheduler, ResourceRequest
from skharness.arena.status import BoundedArenaMetrics
from skharness.arena.store import ArenaStore


class Clock:
    monotonic = 1.0

    def __call__(self):
        return self.monotonic


def controller(tmp_path, clock):
    return ArenaController(
        ArenaStore(tmp_path),
        LeaseScheduler(ResourceRequest(cpu=1, ram_gb=2, gateway_slots=1),
                       lease_ttl_s=5, clock=clock),
        writer_id="controller",
        actor="capauth:codex@skworld.io",
        node="test-node",
        session_id="run:test",
        now=lambda: datetime(2026, 8, 20, tzinfo=timezone.utc),
    )


def request():
    return AttemptRequest("challenge", "experiment", "1", "experiment:1",
                          ResourceRequest(cpu=1, ram_gb=2, gateway_slots=1))


def test_full_execution_path_is_durable_and_releases_capacity(tmp_path):
    clock = Clock()
    ctl = controller(tmp_path, clock)
    ctl.propose("experiment")
    assert ctl.admit(request()).admitted
    ctl.running("experiment")
    ctl.finish_run("experiment", successful=True, payload={"artifact": "sha256:abc"})
    assert ctl.state("experiment") == ExperimentState.PROVISIONAL
    assert ctl.scheduler.snapshot()["active_leases"] == 0
    events = ArenaStore(tmp_path).read_all_events()
    assert [event.to_state for event in events] == [
        ExperimentState.PROPOSED,
        ExperimentState.ADMITTED,
        ExperimentState.RUNNING,
        ExperimentState.PROVISIONAL,
    ]
    assert events[-1].payload == {"artifact": "sha256:abc"}


def test_capacity_rejection_does_not_claim_admitted_state(tmp_path):
    ctl = ArenaController(
        ArenaStore(tmp_path), LeaseScheduler(ResourceRequest(cpu=0, ram_gb=0,
                                                             gateway_slots=0)),
        writer_id="controller", actor="actor", node="node", session_id="run:test",
    )
    ctl.propose("experiment")
    assert not ctl.admit(request()).admitted
    assert ctl.state("experiment") == ExperimentState.PROPOSED


def test_cancellation_stops_worker_before_terminal_event(tmp_path):
    clock = Clock()
    ctl = controller(tmp_path, clock)
    ctl.propose("experiment")
    ctl.admit(request())
    ctl.running("experiment")
    observed = []
    ctl.cancel("experiment", stop=lambda: observed.append(ctl.state("experiment")))
    assert observed == [ExperimentState.RUNNING]
    assert ctl.state("experiment") == ExperimentState.CANCELLED
    assert ctl.scheduler.snapshot()["active_leases"] == 0


def test_expired_lease_becomes_failed_orphan_event(tmp_path):
    clock = Clock()
    ctl = controller(tmp_path, clock)
    ctl.propose("experiment")
    ctl.admit(request())
    ctl.running("experiment")
    clock.monotonic = 7
    events = ctl.reclaim_orphans()
    assert len(events) == 1
    assert events[0].to_state == ExperimentState.FAILED
    assert events[0].payload["reason"] == "lease_expired"


def test_invalid_transition_fails_without_appending(tmp_path):
    ctl = controller(tmp_path, Clock())
    with pytest.raises(InvalidTransitionError):
        ctl.running("missing")
    assert ctl.store.read_all_events() == []


def test_controller_emits_bounded_lifecycle_signals(tmp_path):
    clock = Clock()
    metrics = BoundedArenaMetrics()
    ctl = ArenaController(
        ArenaStore(tmp_path),
        LeaseScheduler(ResourceRequest(cpu=1, ram_gb=2, gateway_slots=1), clock=clock),
        writer_id="controller", actor="actor", node="node", session_id="session",
        metrics=metrics,
    )
    ctl.propose("experiment")
    ctl.admit(request())
    ctl.running("experiment")
    ctl.finish_run("experiment", successful=False, payload={"reason": "oom"})
    rendered = metrics.render({"ready": True, "scheduler": ctl.scheduler.snapshot()})
    assert 'state="proposed"' in rendered
    assert 'state="failed"' in rendered
    assert 'outcome="admitted"' in rendered
    assert 'signal="oom"' in rendered
    assert "experiment" not in rendered
