"""Safe recovery for provider failures before a worker produces useful output.

The recovery path is intentionally small and independent of verdict/evidence joins:
transport observations are receipts, while lifecycle projections are separate
records.  A receipt never implies a verdict.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, TextIO


@dataclass(frozen=True)
class TransportReceipt:
    card_id: str
    owner: str
    claim_revision: int
    model_lane: str
    response_class: str
    status_code: int
    useful_output: bool = False

    def __post_init__(self) -> None:
        if self.claim_revision < 0 or self.status_code < 0:
            raise ValueError("claim revision and status code must be non-negative")
        for name in ("card_id", "owner", "model_lane", "response_class"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")

    def as_dict(self) -> dict:
        return {"type": "transport_receipt", **self.__dict__}


@dataclass(frozen=True)
class WorkerProjection:
    card_id: str
    worker_id: str
    state: str
    candidate_sha256: str | None = None

    def as_dict(self) -> dict:
        return {"type": "worker_projection", **self.__dict__}


class AppendOnlyEventStore:
    """A strict JSONL store. Every existing and new line is parsed before append."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def serialize(event: dict) -> str:
        return json.dumps(event, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def parse(line: str) -> dict:
        value = json.loads(line)
        if not isinstance(value, dict) or not isinstance(value.get("type"), str):
            raise ValueError("event must be an object with a type")
        return value

    def append(self, event: dict) -> None:
        encoded = self.serialize(event)
        self.parse(encoded)
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    if line.strip():
                        self.parse(line)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(encoded + "\n")

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as stream:
            return [self.parse(line) for line in stream if line.strip()]


@dataclass(frozen=True)
class RecoveryResult:
    receipt: TransportReceipt
    projection: WorkerProjection
    retry_allowed: bool
    candidate_sha256: str | None


class WorkerRecovery:
    """Release only the matching claim and allow one safe retry."""

    def __init__(self, events: AppendOnlyEventStore):
        self.events = events
        self._retried: set[tuple[str, int]] = set()

    def provider_failure(
        self,
        receipt: TransportReceipt,
        projection: WorkerProjection,
        *,
        release_claim: Callable[[str, int], bool],
        candidate: bytes | str | None = None,
        retryable: bool = True,
        side_effectful: bool = False,
        terminal: bool = False,
    ) -> RecoveryResult:
        """Record transport and terminal projection, then release exact claim.

        Retry is bounded by card and claim revision and is disallowed for terminal,
        side-effectful, or non-retryable work.  The receipt is written separately
        from the projection so callers cannot mistake lifecycle for evidence.
        """
        if receipt.card_id != projection.card_id:
            raise ValueError("receipt and projection card mismatch")
        digest = None
        if candidate is not None:
            raw = candidate.encode() if isinstance(candidate, str) else candidate
            digest = hashlib.sha256(raw).hexdigest()
        self.events.append(receipt.as_dict())
        self.events.append(replace(projection, state="terminal", candidate_sha256=digest).as_dict())
        released = release_claim(receipt.card_id, receipt.claim_revision)
        if not released:
            raise RuntimeError("claim changed before exact release")
        key = (receipt.card_id, receipt.claim_revision)
        allowed = retryable and not side_effectful and not terminal and key not in self._retried
        if allowed:
            self._retried.add(key)
        return RecoveryResult(receipt, replace(projection, state="terminal", candidate_sha256=digest), allowed, digest)

    def review_failure(self, card_id: str, candidate: bytes | str, owner: str) -> WorkerProjection:
        """Return failed review to reviewable without changing candidate bytes."""
        raw = candidate.encode() if isinstance(candidate, str) else candidate
        projection = WorkerProjection(card_id, owner, "reviewable", hashlib.sha256(raw).hexdigest())
        self.events.append(projection.as_dict())
        return projection
