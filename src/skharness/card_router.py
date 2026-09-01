"""Governed CardStore card to session assignment router.

The router is the scale-up boundary: one eligible card becomes exactly one
claimed assignment with its own identity, session id, worktree and branch. It
never reads the board projection. The claim gate folds and locks the canonical
append-only CardStore before appending a serialized claim event.
"""
from __future__ import annotations

import re
import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, ContextManager, Protocol, Sequence

_CARD_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class CardClaimRejected(RuntimeError):
    """The authoritative CardStore refused an assignment claim."""


@dataclass(frozen=True)
class EligibleCard:
    """Operator-selected card inputs that are not trusted for claim state."""

    card_id: str
    repo: str
    base_branch: str
    prompt: str


@dataclass(frozen=True)
class SessionAssignment:
    """One immutable, distinct card to session routing decision."""

    card_id: str
    repo: str
    base_branch: str
    prompt: str
    agent_id: str
    session_id: str
    worktree: str
    branch: str
    claim_event_id: str


class ClaimGate(Protocol):
    def claim(self, card_id: str, agent_id: str) -> dict: ...


class CardStoreClaimGate:
    """Atomic canonical claim gate, independent of the board projection."""

    def __init__(
        self,
        home: Path,
        *,
        lock_timeout_s: float = 5.0,
        store: Any = None,
        lock: Callable[[Path, str, float], ContextManager] | None = None,
    ) -> None:
        self.home = Path(home)
        self.lock_timeout_s = lock_timeout_s
        if store is None or lock is None:
            # Keep skcoord optional for users of the manual pool. It becomes a
            # hard dependency only when the governed CardStore gate is built.
            from skcoord.card_store import CardStore, card_mutation_lock
            store = store or CardStore(self.home)
            lock = lock or card_mutation_lock
        self.store = store
        self._lock = lock

    def claim(self, card_id: str, agent_id: str) -> dict:
        if not _CARD_ID_RE.fullmatch(card_id):
            raise CardClaimRejected(f"invalid card id {card_id!r}")
        with self._lock(self.home, card_id, self.lock_timeout_s):
            card = self.store.fold(card_id)
            if card is None:
                raise CardClaimRejected(f"CardStore card {card_id} not found")
            if card.owner is not None or getattr(card.status, "value", card.status) != "backlog":
                raise CardClaimRejected(
                    f"CardStore card {card_id} is not claimable: "
                    f"status={card.status.value}, owner={card.owner!r}"
                )
            incomplete = []
            for dependency in card.dependencies:
                dep = self.store.fold(dependency)
                if dep is None or getattr(dep.status, "value", dep.status) != "done":
                    incomplete.append(dependency)
            if incomplete:
                raise CardClaimRejected(
                    f"CardStore card {card_id} has incomplete dependencies: {incomplete!r}"
                )
            # CardStore.append_event owns JSON serialization, line parsing for
            # idempotency, append locking, flush and fsync. No JSON is concatenated.
            return self.store.append_event(
                card_id,
                "claim",
                agent_id,
                owner=agent_id,
                claim_revision=uuid.uuid4().hex,
                transition_id=uuid.uuid4().hex,
            )


class CardSessionRouter:
    """Claim distinct canonical cards and mint distinct session assignments."""

    def __init__(self, gate: ClaimGate, *, worktree_root: Path) -> None:
        self.gate = gate
        self.worktree_root = Path(worktree_root)
        self._assigned_cards: set[str] = set()

    def route(self, cards: Sequence[EligibleCard], count: int) -> list[SessionAssignment]:
        if count < 0:
            raise ValueError("assignment count must be >= 0")
        if len(cards) < count:
            raise CardClaimRejected(
                f"requested {count} assignments but only {len(cards)} candidates supplied"
            )
        selected = list(cards[:count])
        ids = [card.card_id for card in selected]
        if len(ids) != len(set(ids)) or self._assigned_cards.intersection(ids):
            raise CardClaimRejected("the same card cannot be assigned to two sessions")

        assignments: list[SessionAssignment] = []
        for card in selected:
            token = secrets.token_hex(6)
            agent_id = f"skw-{card.card_id}-{token}"
            session_id = f"{agent_id}-{secrets.token_hex(4)}"
            event = self.gate.claim(card.card_id, agent_id)
            assignment = SessionAssignment(
                card_id=card.card_id,
                repo=card.repo,
                base_branch=card.base_branch,
                prompt=card.prompt,
                agent_id=agent_id,
                session_id=session_id,
                worktree=str(self.worktree_root / session_id),
                branch=f"skcode/{session_id}",
                claim_event_id=str(event["event_id"]),
            )
            self._assigned_cards.add(card.card_id)
            assignments.append(assignment)
        return assignments


__all__ = [
    "CardClaimRejected",
    "CardSessionRouter",
    "CardStoreClaimGate",
    "EligibleCard",
    "SessionAssignment",
]
