"""Lease-bound attempt ownership, inboxes, and explicit agent-to-agent ACLs."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from .scheduler import Lease, LeaseScheduler


class AccessDeniedError(PermissionError):
    """An agent lacks a current lease or explicit peer grant."""


@dataclass(frozen=True)
class AttemptOwnership:
    experiment_id: str
    attempt_id: str
    lease_id: str
    owner: str


@dataclass(frozen=True)
class AgentMessage:
    id: str
    sender: str
    recipient: str
    experiment_id: str
    body: str
    created_at: datetime


class CollaborationAccess:
    """Domain PEP joining collaboration writes to active scheduler leases.

    Read-only experiment discovery is intentionally outside this PEP. Any operation
    that mutates an attempt or sends experiment-scoped A2A messages must pass here.
    """

    def __init__(self, scheduler: LeaseScheduler) -> None:
        self.scheduler = scheduler
        self._ownership: dict[tuple[str, str], AttemptOwnership] = {}
        self._grants: set[tuple[str, str]] = set()
        self._inboxes: dict[str, list[AgentMessage]] = {}
        self._lock = threading.RLock()

    def bind(self, lease: Lease, *, owner: str) -> AttemptOwnership:
        if not owner.strip() or self.scheduler.active_lease(lease.lease_id) is not lease:
            raise AccessDeniedError("ownership requires an active scheduler lease")
        key = (lease.request.experiment_id, lease.request.attempt_id)
        ownership = AttemptOwnership(*key, lease.lease_id, owner)
        with self._lock:
            prior = self._ownership.get(key)
            if prior is not None and prior != ownership:
                raise AccessDeniedError("attempt already has a different owner")
            self._ownership[key] = ownership
        return ownership

    def require_owner(self, experiment_id: str, attempt_id: str, *, actor: str) -> Lease:
        with self._lock:
            ownership = self._ownership.get((experiment_id, attempt_id))
        if ownership is None or ownership.owner != actor:
            raise AccessDeniedError("actor does not own this attempt")
        lease = self.scheduler.active_lease(ownership.lease_id)
        if lease is None:
            raise AccessDeniedError("attempt ownership lease is no longer active")
        return lease

    def grant_peer(self, *, actor: str, owner: str, peer: str) -> None:
        if not owner.strip() or not peer.strip() or owner == peer:
            raise ValueError("A2A grants require two distinct agent identities")
        if actor != owner:
            raise AccessDeniedError("only an agent may grant access to its A2A channel")
        with self._lock:
            self._grants.add((owner, peer))

    def revoke_peer(self, *, owner: str, peer: str) -> None:
        with self._lock:
            self._grants.discard((owner, peer))

    def send(
        self,
        *,
        sender: str,
        recipient: str,
        experiment_id: str,
        attempt_id: str,
        body: str,
    ) -> AgentMessage:
        self.require_owner(experiment_id, attempt_id, actor=sender)
        with self._lock:
            if (sender, recipient) not in self._grants:
                raise AccessDeniedError("sender has no A2A grant for recipient")
            if not body.strip():
                raise ValueError("message body must not be empty")
            message = AgentMessage(
                id=uuid.uuid4().hex,
                sender=sender,
                recipient=recipient,
                experiment_id=experiment_id,
                body=body,
                created_at=datetime.now(timezone.utc),
            )
            self._inboxes.setdefault(recipient, []).append(message)
            return message

    def inbox(self, *, actor: str) -> tuple[AgentMessage, ...]:
        with self._lock:
            return tuple(self._inboxes.get(actor, ()))

    def owned_attempts(self, *, actor: str) -> tuple[AttemptOwnership, ...]:
        with self._lock:
            owned = [item for item in self._ownership.values() if item.owner == actor]
        return tuple(
            sorted(
                (item for item in owned if self.scheduler.active_lease(item.lease_id) is not None),
                key=lambda item: (item.experiment_id, item.attempt_id),
            )
        )

    def reproduce(
        self,
        catalog,
        immutable_evidence_id: str,
        *,
        experiment_id: str,
        attempt_id: str,
        actor: str,
        **fields,
    ):
        """Construct a reproduction only for the active owner of its new attempt."""

        self.require_owner(experiment_id, attempt_id, actor=actor)
        return catalog.reproduce_evidence(
            immutable_evidence_id,
            experiment_id=experiment_id,
            actor=actor,
            **fields,
        )

    def mutate(
        self,
        catalog,
        parent_id: str,
        *,
        experiment_id: str,
        attempt_id: str,
        actor: str,
        **fields,
    ):
        """Construct a mutation only for the active owner of its new attempt."""

        self.require_owner(experiment_id, attempt_id, actor=actor)
        return catalog.mutate(
            parent_id,
            experiment_id=experiment_id,
            actor=actor,
            **fields,
        )
