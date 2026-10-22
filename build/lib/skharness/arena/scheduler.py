"""Bounded, lease-based admission for Evolution Arena experiment attempts.

The scheduler is deliberately independent of the trajectory store: it owns volatile
admission state while append-only experiment events own durable truth.  A caller must
record the returned lease/attempt transitions before starting external work.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum


class AdmissionReason(str, Enum):
    """Stable reasons an attempt cannot be admitted immediately."""

    CAPACITY = "capacity"
    DUPLICATE = "duplicate"
    INVALID = "invalid"


@dataclass(frozen=True)
class ResourceRequest:
    """Resources reserved for one attempt; all values are non-negative."""

    cpu: float = 1.0
    ram_gb: float = 1.0
    gpu: int = 0
    vram_gb: float = 0.0
    gateway_slots: int = 1
    budget_units: float = 0.0
    verifier_slots: int = 0

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if value < 0:
                raise ValueError(f"{name} must be non-negative")

    def fits(self, available: "ResourceRequest") -> bool:
        """Return whether every requested dimension fits ``available``."""
        return all(getattr(self, key) <= getattr(available, key) for key in vars(self))

    def plus(self, other: "ResourceRequest") -> "ResourceRequest":
        return ResourceRequest(
            **{key: getattr(self, key) + getattr(other, key) for key in vars(self)}
        )

    def minus(self, other: "ResourceRequest") -> "ResourceRequest":
        return ResourceRequest(
            **{key: getattr(self, key) - getattr(other, key) for key in vars(self)}
        )


@dataclass(frozen=True)
class AttemptRequest:
    """Idempotent request to reserve capacity for an experiment attempt."""

    challenge_id: str
    experiment_id: str
    attempt_id: str
    idempotency_key: str
    resources: ResourceRequest = field(default_factory=ResourceRequest)

    def __post_init__(self) -> None:
        for name in ("challenge_id", "experiment_id", "attempt_id", "idempotency_key"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")


@dataclass
class Lease:
    """One admitted attempt. Expiry is based on the injected monotonic clock."""

    lease_id: str
    request: AttemptRequest
    acquired_at: float
    expires_at: float
    cancelled: bool = False

    def active(self, now: float) -> bool:
        return not self.cancelled and now < self.expires_at


@dataclass(frozen=True)
class Admission:
    """Result of admission. Duplicate delivery returns the existing lease."""

    admitted: bool
    lease: Lease | None = None
    reason: AdmissionReason | None = None
    duplicate: bool = False
    blocked_resources: tuple[str, ...] = ()


class LeaseScheduler:
    """Thread-safe resource admission with heartbeat, cancellation and reclaim.

    This class performs no process killing and no durable writes.  The controller
    composes it with the Pi runner and event store, keeping volatile leases rebuildable
    and external side effects behind an explicit integration boundary.
    """

    def __init__(
        self,
        capacity: ResourceRequest,
        *,
        lease_ttl_s: float = 60.0,
        clock=time.monotonic,
    ) -> None:
        if lease_ttl_s <= 0:
            raise ValueError("lease_ttl_s must be positive")
        self.capacity = capacity
        self.lease_ttl_s = float(lease_ttl_s)
        self._clock = clock
        self._leases: dict[str, Lease] = {}
        self._by_key: dict[str, str] = {}
        self._lock = threading.RLock()

    def _active(self, now: float) -> list[Lease]:
        return [lease for lease in self._leases.values() if lease.active(now)]

    def used(self) -> ResourceRequest:
        """Return the resource vector held by active leases."""
        with self._lock:
            now = self._clock()
            total = ResourceRequest(
                cpu=0, ram_gb=0, gpu=0, vram_gb=0, gateway_slots=0, verifier_slots=0
            )
            for lease in self._active(now):
                total = total.plus(lease.request.resources)
            return total

    def available(self) -> ResourceRequest:
        """Return currently unreserved capacity."""
        return self.capacity.minus(self.used())

    def admit(self, request: AttemptRequest) -> Admission:
        """Admit once, reject for capacity, or return an active duplicate lease."""
        with self._lock:
            now = self._clock()
            existing_id = self._by_key.get(request.idempotency_key)
            if existing_id:
                existing = self._leases[existing_id]
                if existing.request != request:
                    return Admission(False, reason=AdmissionReason.DUPLICATE)
                if existing.active(now):
                    return Admission(True, lease=existing, duplicate=True)
                # The same exact attempt may be retried after expiry. It receives a
                # new lease id, while the durable attempt/event history remains intact.
            if not request.resources.fits(self.available()):
                available = self.available()
                blocked = tuple(
                    name
                    for name in vars(request.resources)
                    if getattr(request.resources, name) > getattr(available, name)
                )
                return Admission(
                    False,
                    reason=AdmissionReason.CAPACITY,
                    blocked_resources=blocked,
                )
            lease = Lease(
                lease_id=uuid.uuid4().hex,
                request=request,
                acquired_at=now,
                expires_at=now + self.lease_ttl_s,
            )
            self._leases[lease.lease_id] = lease
            self._by_key[request.idempotency_key] = lease.lease_id
            return Admission(True, lease=lease)

    def heartbeat(self, lease_id: str) -> Lease | None:
        """Extend an active lease; never resurrect an expired/cancelled lease."""
        with self._lock:
            now = self._clock()
            lease = self._leases.get(lease_id)
            if lease is None or not lease.active(now):
                return None
            lease.expires_at = now + self.lease_ttl_s
            return lease

    def active_lease(self, lease_id: str) -> Lease | None:
        """Resolve a lease only while it remains active, for authorization joins."""
        with self._lock:
            lease = self._leases.get(lease_id)
            return lease if lease is not None and lease.active(self._clock()) else None

    def cancel(self, lease_id: str) -> bool:
        """Release a lease idempotently and mark it cancelled for audit callers."""
        with self._lock:
            lease = self._leases.get(lease_id)
            if lease is None:
                return False
            lease.cancelled = True
            return True

    def release(self, lease_id: str) -> bool:
        """Release capacity while retaining idempotency history in durable events."""
        with self._lock:
            lease = self._leases.pop(lease_id, None)
            if lease is None:
                return False
            if self._by_key.get(lease.request.idempotency_key) == lease_id:
                self._by_key.pop(lease.request.idempotency_key, None)
            return True

    def reclaim_expired(self) -> list[Lease]:
        """Remove and return expired leases for controller orphan finalization."""
        with self._lock:
            now = self._clock()
            expired = [
                lease
                for lease in self._leases.values()
                if not lease.cancelled and now >= lease.expires_at
            ]
            for lease in expired:
                self.release(lease.lease_id)
            return expired

    def snapshot(self) -> dict:
        """Bounded operational view; high-cardinality details belong in traces."""
        with self._lock:
            now = self._clock()
            active = self._active(now)
            return {
                "active_leases": len(active),
                "capacity": vars(self.capacity),
                "used": vars(self.used()),
                "available": vars(self.available()),
                "next_expiry_s": min(
                    (max(0.0, lease.expires_at - now) for lease in active),
                    default=None,
                ),
            }

    def lease_records(self) -> list[dict]:
        """Return active lease identities for an authenticated structured view.

        Unlike :meth:`snapshot`, these records are intentionally high-cardinality
        and must never be converted to metric labels.
        """
        with self._lock:
            now = self._clock()
            return [
                {
                    "lease_id": lease.lease_id,
                    "challenge_id": lease.request.challenge_id,
                    "experiment_id": lease.request.experiment_id,
                    "attempt_id": lease.request.attempt_id,
                    "acquired_at_monotonic": lease.acquired_at,
                    "expires_in_s": max(0.0, lease.expires_at - now),
                    "resources": vars(lease.request.resources),
                }
                for lease in sorted(self._active(now), key=lambda item: item.lease_id)
            ]
