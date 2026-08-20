from __future__ import annotations

from skharness.arena.scheduler import (
    AdmissionReason,
    AttemptRequest,
    LeaseScheduler,
    ResourceRequest,
)


class Clock:
    def __init__(self) -> None:
        self.now = 10.0

    def __call__(self) -> float:
        return self.now


def request(key="key", attempt="a", *, gpu=0, gateway=1):
    return AttemptRequest(
        challenge_id="challenge",
        experiment_id="experiment",
        attempt_id=attempt,
        idempotency_key=key,
        resources=ResourceRequest(cpu=1, ram_gb=2, gpu=gpu, vram_gb=8 if gpu else 0,
                                  gateway_slots=gateway),
    )


def test_admission_reserves_capacity_and_rejects_overload():
    clock = Clock()
    scheduler = LeaseScheduler(
        ResourceRequest(cpu=2, ram_gb=4, gpu=1, vram_gb=16, gateway_slots=1),
        clock=clock,
    )
    first = scheduler.admit(request(gpu=1))
    assert first.admitted
    second = scheduler.admit(request("second", "b"))
    assert not second.admitted
    assert second.reason == AdmissionReason.CAPACITY
    assert scheduler.snapshot()["active_leases"] == 1


def test_duplicate_delivery_returns_same_active_lease():
    scheduler = LeaseScheduler(ResourceRequest(cpu=2, ram_gb=4, gateway_slots=2))
    req = request()
    first = scheduler.admit(req)
    duplicate = scheduler.admit(req)
    assert duplicate.admitted and duplicate.duplicate
    assert duplicate.lease is first.lease
    assert scheduler.snapshot()["active_leases"] == 1


def test_idempotency_key_collision_with_different_attempt_fails():
    scheduler = LeaseScheduler(ResourceRequest(cpu=2, ram_gb=4, gateway_slots=2))
    assert scheduler.admit(request()).admitted
    collision = scheduler.admit(request(attempt="different"))
    assert collision.reason == AdmissionReason.DUPLICATE


def test_heartbeat_does_not_resurrect_expired_lease_and_reclaim_frees_capacity():
    clock = Clock()
    scheduler = LeaseScheduler(
        ResourceRequest(cpu=1, ram_gb=2, gateway_slots=1), lease_ttl_s=5, clock=clock
    )
    lease = scheduler.admit(request()).lease
    clock.now = 16
    assert scheduler.heartbeat(lease.lease_id) is None
    assert scheduler.reclaim_expired() == [lease]
    assert scheduler.admit(request("new", "b")).admitted


def test_cancel_and_release_are_idempotent():
    scheduler = LeaseScheduler(ResourceRequest(cpu=1, ram_gb=2, gateway_slots=1))
    lease = scheduler.admit(request()).lease
    assert scheduler.cancel(lease.lease_id)
    assert scheduler.cancel(lease.lease_id)
    assert scheduler.snapshot()["active_leases"] == 0
    assert scheduler.release(lease.lease_id)
    assert not scheduler.release(lease.lease_id)


def test_negative_resources_and_empty_ids_are_rejected():
    try:
        ResourceRequest(cpu=-1)
    except ValueError as exc:
        assert "cpu" in str(exc)
    else:
        raise AssertionError("negative resource accepted")
    try:
        AttemptRequest("", "e", "a", "key")
    except ValueError as exc:
        assert "challenge_id" in str(exc)
    else:
        raise AssertionError("empty challenge accepted")
