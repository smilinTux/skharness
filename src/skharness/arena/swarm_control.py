"""Durable scheduling authority for bounded Arena subagent teams.

The immutable contracts live in :mod:`skharness.arena.swarm`. This module keeps
the trusted controller's volatile concerns separate: per-child liveness leases,
idempotent delivery, aggregate reservation, cancellation and restart recovery.
It deliberately has no process-launch or card-promotion authority. Callers must
stop every lease ID returned by cancellation/timeout methods, and only the
independent verifier gate may promote a child result.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .swarm import (
    BudgetExceededError,
    BudgetUsage,
    SubagentContract,
    SubagentResult,
    SwarmContractError,
    SwarmIdentity,
    TeamBudget,
    TeamBudgetLedger,
)


class SwarmStateError(RuntimeError):
    """Scheduling state is corrupt or an invalid lifecycle action was requested."""


class WorkerLeaseState(str, Enum):
    ACTIVE = "active"
    FINISHED = "finished"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class SwarmAdmissionReason(str, Enum):
    BUDGET = "budget"
    WRITE_CONFLICT = "write_conflict"
    DUPLICATE = "duplicate"
    TEAM_CANCELLED = "team_cancelled"


@dataclass
class WorkerLease:
    """Controller-owned liveness record for one immutable child contract."""

    lease_id: str
    contract: SubagentContract
    idempotency_key: str
    acquired_at: float
    expires_at: float
    deadline_at: float
    state: WorkerLeaseState = WorkerLeaseState.ACTIVE
    usage: BudgetUsage = field(default_factory=BudgetUsage)
    result: SubagentResult | None = None
    terminal_reason: str | None = None
    stop_required: bool = False

    def active(self, now: float) -> bool:
        return self.state is WorkerLeaseState.ACTIVE and now < min(
            self.expires_at, self.deadline_at
        )


@dataclass(frozen=True)
class SwarmAdmission:
    admitted: bool
    lease: WorkerLease | None = None
    reason: SwarmAdmissionReason | None = None
    duplicate: bool = False
    detail: str | None = None


class SwarmScheduler:
    """Thread-safe team scheduler with content-checked durable checkpoints."""

    _SCHEMA = "arena.swarm-control.v1"

    def __init__(
        self,
        budget: TeamBudget,
        *,
        identity: SwarmIdentity,
        orchestrator_id: str,
        lease_ttl_s: float = 60.0,
        clock=time.time,
        state_path: str | Path | None = None,
    ) -> None:
        if not orchestrator_id.strip():
            raise SwarmContractError("orchestrator_id must not be empty")
        if lease_ttl_s <= 0:
            raise SwarmContractError("lease_ttl_s must be positive")
        self.budget = budget
        self.identity = identity
        self.orchestrator_id = orchestrator_id
        self.lease_ttl_s = float(lease_ttl_s)
        self._clock = clock
        self._state_path = Path(state_path) if state_path is not None else None
        self._ledger = TeamBudgetLedger(budget)
        self._contracts: dict[str, SubagentContract] = {}
        self._leases: dict[str, WorkerLease] = {}
        self._by_key: dict[str, str] = {}
        self._usage_deliveries: dict[str, tuple[str, BudgetUsage]] = {}
        self._cancelled = False
        self._cancel_reason: str | None = None
        self._lock = threading.RLock()

    def register(self, contract: SubagentContract) -> None:
        """Register a contract only when every immutable team binding matches."""
        self._require_team_contract(contract)
        with self._lock:
            prior = self._contracts.get(contract.contract_id)
            if prior is not None:
                if prior != contract:
                    raise SwarmContractError("contract ID collision")
                return
            if any(item.child_agent_id == contract.child_agent_id for item in self._contracts.values()):
                raise SwarmContractError("a child identity may have only one team contract")
            if any(item.lease_id == contract.lease_id for item in self._contracts.values()):
                raise SwarmContractError("worker lease ID collision")
            self._contracts[contract.contract_id] = contract
            self._persist()

    def _require_team_contract(self, contract: SubagentContract) -> None:
        mismatches: list[str] = []
        if contract.team_id != self.budget.team_id:
            mismatches.append("team_id")
        if contract.identity != self.identity:
            mismatches.append("identity")
        if contract.parent_agent_id != self.orchestrator_id:
            mismatches.append("parent_agent_id")
        if mismatches:
            raise SwarmContractError(
                "child contract is not bound to scheduler authority: " + ", ".join(mismatches)
            )

    def admit(self, contract_id: str, *, idempotency_key: str) -> SwarmAdmission:
        """Reserve the child's full budget and write scope exactly once."""
        key = idempotency_key.strip()
        if not key:
            raise SwarmContractError("idempotency_key must not be empty")
        with self._lock:
            self._expire_locked(self._clock())
            contract = self._contracts.get(contract_id)
            if contract is None:
                raise SwarmContractError("subagent contract is not registered")
            prior_id = self._by_key.get(key)
            if prior_id is not None:
                prior = self._leases[prior_id]
                same_contract = prior.contract.contract_id == contract_id
                active_duplicate = same_contract and prior.active(self._clock())
                return SwarmAdmission(
                    admitted=active_duplicate,
                    lease=prior,
                    reason=None if active_duplicate else SwarmAdmissionReason.DUPLICATE,
                    duplicate=True,
                    detail=None if same_contract else "idempotency key belongs to another contract",
                )
            if self._cancelled:
                return SwarmAdmission(False, reason=SwarmAdmissionReason.TEAM_CANCELLED)
            if any(lease.contract.contract_id == contract_id for lease in self._leases.values()):
                return SwarmAdmission(
                    False,
                    reason=SwarmAdmissionReason.DUPLICATE,
                    duplicate=True,
                    detail="contract already has an attempt",
                )
            try:
                self._ledger.reserve(contract)
            except BudgetExceededError as exc:
                return SwarmAdmission(
                    False, reason=SwarmAdmissionReason.BUDGET, detail=str(exc)
                )
            except SwarmContractError as exc:
                # Contracts were validated at registration. The remaining ledger
                # policy rejection is overlapping same-worktree write ownership.
                return SwarmAdmission(
                    False,
                    reason=SwarmAdmissionReason.WRITE_CONFLICT,
                    detail=str(exc),
                )
            now = self._clock()
            deadline = now + contract.budget.wall_seconds
            lease = WorkerLease(
                lease_id=contract.lease_id,
                contract=contract,
                idempotency_key=key,
                acquired_at=now,
                expires_at=min(now + self.lease_ttl_s, deadline),
                deadline_at=deadline,
            )
            self._leases[lease.lease_id] = lease
            self._by_key[key] = lease.lease_id
            self._persist()
            return SwarmAdmission(True, lease=lease)

    def heartbeat(self, lease_id: str) -> WorkerLease | None:
        """Extend liveness without ever extending the child budget deadline."""
        with self._lock:
            now = self._clock()
            lease = self._leases.get(lease_id)
            if lease is None or not lease.active(now):
                return None
            lease.expires_at = min(now + self.lease_ttl_s, lease.deadline_at)
            self._persist()
            return lease

    def charge(
        self,
        lease_id: str,
        usage: BudgetUsage,
        *,
        delivery_id: str,
    ) -> WorkerLease:
        """Apply one idempotent usage delta within the pre-reserved child ceiling."""
        delivery = delivery_id.strip()
        if not delivery:
            raise SwarmContractError("delivery_id must not be empty")
        with self._lock:
            self._expire_locked(self._clock())
            lease = self._leases.get(lease_id)
            if lease is None or not lease.active(self._clock()):
                raise SwarmStateError("usage requires an active worker lease")
            prior = self._usage_deliveries.get(delivery)
            if prior is not None:
                if prior != (lease_id, usage):
                    raise SwarmStateError("usage delivery ID collision")
                return lease
            updated = lease.usage.plus(usage)
            self._require_within_child(updated, lease.contract)
            lease.usage = updated
            self._usage_deliveries[delivery] = (lease_id, usage)
            self._persist()
            return lease

    @staticmethod
    def _require_within_child(usage: BudgetUsage, contract: SubagentContract) -> None:
        limit = contract.budget
        if (
            usage.wall_seconds > limit.wall_seconds
            or usage.tokens > limit.token_limit
            or usage.tool_calls > limit.tool_call_limit
            or usage.cost > limit.cost_limit
        ):
            raise BudgetExceededError("reported usage exceeds the child reservation")

    def complete(self, lease_id: str, result: SubagentResult) -> WorkerLease:
        """Accept a structured child result, without granting card completion."""
        with self._lock:
            self._expire_locked(self._clock())
            lease = self._leases.get(lease_id)
            if lease is None:
                raise SwarmStateError("unknown worker lease")
            if lease.state is WorkerLeaseState.FINISHED and lease.result == result:
                return lease
            if not lease.active(self._clock()):
                raise SwarmStateError("worker lease is no longer active")
            expected = lease.contract
            if (
                result.contract_id != expected.contract_id
                or result.contract_hash != expected.content_hash
                or result.identity != expected.identity
                or result.agent_id != expected.child_agent_id
                or result.role is not expected.role
            ):
                raise SwarmStateError("subagent result is not bound to the exact worker contract")
            self._finalize_elapsed(lease)
            lease.state = WorkerLeaseState.FINISHED
            lease.result = result
            self._ledger.settle(expected.contract_id, lease.usage)
            self._persist()
            return lease

    def cancel_worker(self, lease_id: str, *, reason: str) -> tuple[str, ...]:
        """Cancel one child idempotently and return the process lease to stop."""
        if not reason.strip():
            raise SwarmContractError("cancellation reason must not be empty")
        with self._lock:
            lease = self._leases.get(lease_id)
            if lease is None or lease.state is not WorkerLeaseState.ACTIVE:
                return ()
            self._finalize_elapsed(lease)
            lease.state = WorkerLeaseState.CANCELLED
            lease.terminal_reason = reason
            lease.stop_required = True
            self._ledger.settle(lease.contract.contract_id, lease.usage)
            self._persist()
            return (lease_id,)

    def cancel_team(self, *, reason: str) -> tuple[str, ...]:
        """Cascade cancellation to every child and permanently close admission."""
        if not reason.strip():
            raise SwarmContractError("cancellation reason must not be empty")
        with self._lock:
            if self._cancelled:
                return ()
            self._cancelled = True
            self._cancel_reason = reason
            active = tuple(
                sorted(
                    lease.lease_id
                    for lease in self._leases.values()
                    if lease.state is WorkerLeaseState.ACTIVE
                )
            )
            for lease_id in active:
                lease = self._leases[lease_id]
                self._finalize_elapsed(lease)
                lease.state = WorkerLeaseState.CANCELLED
                lease.terminal_reason = reason
                lease.stop_required = True
                self._ledger.settle(lease.contract.contract_id, lease.usage)
            self._persist()
            return active

    def reap_timeouts(self) -> tuple[str, ...]:
        """Finalize expired children and return their process lease IDs to stop."""
        with self._lock:
            expired = self._expire_locked(self._clock())
            if expired:
                self._persist()
            return expired

    def _expire_locked(self, now: float) -> tuple[str, ...]:
        expired = tuple(
            sorted(
                lease.lease_id
                for lease in self._leases.values()
                if lease.state is WorkerLeaseState.ACTIVE
                and now >= min(lease.expires_at, lease.deadline_at)
            )
        )
        for lease_id in expired:
            lease = self._leases[lease_id]
            self._finalize_elapsed(lease)
            lease.state = WorkerLeaseState.TIMED_OUT
            lease.terminal_reason = (
                "hard_deadline_exceeded"
                if now >= lease.deadline_at
                else "heartbeat_expired"
            )
            lease.stop_required = True
            self._ledger.settle(lease.contract.contract_id, lease.usage)
        return expired

    def _finalize_elapsed(self, lease: WorkerLease) -> None:
        elapsed = min(
            lease.contract.budget.wall_seconds,
            max(lease.usage.wall_seconds, self._clock() - lease.acquired_at),
        )
        lease.usage = lease.usage.model_copy(update={"wall_seconds": elapsed})

    def lease(self, lease_id: str) -> WorkerLease | None:
        with self._lock:
            return self._leases.get(lease_id)

    def stop_requests(self) -> tuple[str, ...]:
        """Return every terminal process whose shutdown is not yet acknowledged."""
        with self._lock:
            return tuple(
                sorted(lease.lease_id for lease in self._leases.values() if lease.stop_required)
            )

    def acknowledge_stopped(self, lease_id: str) -> bool:
        """Durably acknowledge process cleanup without resurrecting its lease."""
        with self._lock:
            lease = self._leases.get(lease_id)
            if lease is None or not lease.stop_required:
                return False
            lease.stop_required = False
            self._persist()
            return True

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            ledger = self._ledger.snapshot()
            return {
                "team_id": self.budget.team_id,
                "identity": self.identity.model_dump(mode="json"),
                "orchestrator_id": self.orchestrator_id,
                "cancelled": self._cancelled,
                "cancel_reason": self._cancel_reason,
                "active_workers": len(ledger.active_contract_ids),
                "budget": ledger.model_dump(mode="json"),
            }

    def _state(self) -> dict[str, Any]:
        leases = []
        for lease in sorted(self._leases.values(), key=lambda item: item.lease_id):
            leases.append(
                {
                    "lease_id": lease.lease_id,
                    "contract_id": lease.contract.contract_id,
                    "idempotency_key": lease.idempotency_key,
                    "acquired_at": lease.acquired_at,
                    "expires_at": lease.expires_at,
                    "deadline_at": lease.deadline_at,
                    "state": lease.state.value,
                    "usage": lease.usage.model_dump(mode="json"),
                    "result": lease.result.model_dump(mode="json") if lease.result else None,
                    "terminal_reason": lease.terminal_reason,
                    "stop_required": lease.stop_required,
                }
            )
        return {
            "schema": self._SCHEMA,
            "budget": self.budget.model_dump(mode="json"),
            "identity": self.identity.model_dump(mode="json"),
            "orchestrator_id": self.orchestrator_id,
            "lease_ttl_s": self.lease_ttl_s,
            "contracts": [
                item.model_dump(mode="json")
                for item in sorted(self._contracts.values(), key=lambda item: item.contract_id)
            ],
            "leases": leases,
            "by_key": dict(sorted(self._by_key.items())),
            "usage_deliveries": {
                key: {"lease_id": lease_id, "usage": usage.model_dump(mode="json")}
                for key, (lease_id, usage) in sorted(self._usage_deliveries.items())
            },
            "cancelled": self._cancelled,
            "cancel_reason": self._cancel_reason,
        }

    def _persist(self) -> None:
        if self._state_path is None:
            return
        state = self._state()
        canonical = json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
        envelope = {
            "checkpoint_hash": "sha256:" + hashlib.sha256(canonical).hexdigest(),
            "state": state,
        }
        payload = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._state_path.with_name(f".{self._state_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._state_path)
            directory_fd = os.open(self._state_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)

    @classmethod
    def recover(cls, state_path: str | Path, *, clock=time.time) -> SwarmScheduler:
        """Recover idempotency/ownership state and fail closed on expired leases."""
        path = Path(state_path)
        try:
            envelope = json.loads(path.read_bytes())
            state = envelope["state"]
            canonical = json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
            expected_hash = "sha256:" + hashlib.sha256(canonical).hexdigest()
            if envelope["checkpoint_hash"] != expected_hash:
                raise SwarmStateError("swarm control checkpoint hash mismatch")
            if state["schema"] != cls._SCHEMA:
                raise SwarmStateError("unsupported swarm control checkpoint schema")
            scheduler = cls(
                TeamBudget.model_validate(state["budget"]),
                identity=SwarmIdentity.model_validate(state["identity"]),
                orchestrator_id=state["orchestrator_id"],
                lease_ttl_s=state["lease_ttl_s"],
                clock=clock,
                state_path=path,
            )
            for raw in state["contracts"]:
                contract = SubagentContract.model_validate(raw)
                scheduler._require_team_contract(contract)
                scheduler._contracts[contract.contract_id] = contract
            for raw in state["leases"]:
                contract = scheduler._contracts[raw["contract_id"]]
                lease = WorkerLease(
                    lease_id=raw["lease_id"],
                    contract=contract,
                    idempotency_key=raw["idempotency_key"],
                    acquired_at=raw["acquired_at"],
                    expires_at=raw["expires_at"],
                    deadline_at=raw["deadline_at"],
                    state=WorkerLeaseState(raw["state"]),
                    usage=BudgetUsage.model_validate(raw["usage"]),
                    result=(
                        SubagentResult.model_validate(raw["result"])
                        if raw["result"] is not None
                        else None
                    ),
                    terminal_reason=raw["terminal_reason"],
                    stop_required=raw["stop_required"],
                )
                scheduler._leases[lease.lease_id] = lease
            scheduler._by_key = dict(state["by_key"])
            scheduler._usage_deliveries = {
                key: (raw["lease_id"], BudgetUsage.model_validate(raw["usage"]))
                for key, raw in state["usage_deliveries"].items()
            }
            scheduler._cancelled = bool(state["cancelled"])
            scheduler._cancel_reason = state["cancel_reason"]
            scheduler._rebuild_ledger()
            scheduler._validate_recovered_state()
        except SwarmStateError:
            raise
        except Exception as exc:
            raise SwarmStateError("invalid swarm control checkpoint") from exc
        scheduler.reap_timeouts()
        return scheduler

    def _rebuild_ledger(self) -> None:
        self._ledger = TeamBudgetLedger(self.budget)
        terminal = sorted(
            (
                lease
                for lease in self._leases.values()
                if lease.state is not WorkerLeaseState.ACTIVE
            ),
            key=lambda item: item.lease_id,
        )
        active = sorted(
            (
                lease
                for lease in self._leases.values()
                if lease.state is WorkerLeaseState.ACTIVE
            ),
            key=lambda item: item.lease_id,
        )
        for lease in terminal:
            self._ledger.reserve(lease.contract)
            self._ledger.settle(lease.contract.contract_id, lease.usage)
        for lease in active:
            self._ledger.reserve(lease.contract)

    def _validate_recovered_state(self) -> None:
        if len({item.child_agent_id for item in self._contracts.values()}) != len(
            self._contracts
        ):
            raise SwarmStateError("recovered child identity is assigned more than once")
        if len({item.lease_id for item in self._contracts.values()}) != len(self._contracts):
            raise SwarmStateError("recovered worker lease ID is assigned more than once")
        for lease in self._leases.values():
            if self._contracts.get(lease.contract.contract_id) != lease.contract:
                raise SwarmStateError("lease references an unknown or changed contract")
            if lease.lease_id != lease.contract.lease_id:
                raise SwarmStateError("lease identity differs from immutable contract")
            if (lease.state is WorkerLeaseState.FINISHED) != (lease.result is not None):
                raise SwarmStateError("finished lease and structured result are inconsistent")
            if lease.stop_required and lease.state not in {
                WorkerLeaseState.CANCELLED,
                WorkerLeaseState.TIMED_OUT,
            }:
                raise SwarmStateError("only cancelled or timed-out leases may require cleanup")
            if lease.result is not None:
                result = lease.result
                contract = lease.contract
                if (
                    result.contract_id != contract.contract_id
                    or result.contract_hash != contract.content_hash
                    or result.identity != contract.identity
                    or result.agent_id != contract.child_agent_id
                    or result.role is not contract.role
                ):
                    raise SwarmStateError("result is not bound to its recovered contract")
        if len(self._by_key) != len(self._leases):
            raise SwarmStateError("idempotency index is incomplete")
        for key, lease_id in self._by_key.items():
            lease = self._leases.get(lease_id)
            if lease is None or lease.idempotency_key != key:
                raise SwarmStateError("idempotency index is inconsistent")
        delivered: dict[str, BudgetUsage] = {}
        for delivery, (lease_id, usage) in self._usage_deliveries.items():
            if not delivery or lease_id not in self._leases:
                raise SwarmStateError("usage delivery index is inconsistent")
            self._require_within_child(usage, self._leases[lease_id].contract)
            delivered[lease_id] = delivered.get(lease_id, BudgetUsage()).plus(usage)
        for lease_id, lease in self._leases.items():
            usage = delivered.get(lease_id, BudgetUsage())
            recorded = lease.usage
            if (
                usage.wall_seconds > recorded.wall_seconds
                or usage.tokens != recorded.tokens
                or usage.tool_calls != recorded.tool_calls
                or usage.cost != recorded.cost
            ):
                raise SwarmStateError("recorded usage does not match idempotent deliveries")
