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
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from .swarm import (
    BudgetExceededError,
    BudgetUsage,
    PhaseAuthorization,
    PhaseReceipt,
    SubagentContract,
    SubagentDisposition,
    SubagentResult,
    SwarmContractError,
    SwarmIdentity,
    SwarmPlan,
    TeamBudget,
    TeamBudgetLedger,
    require_contract_plan,
)


class SwarmStateError(RuntimeError):
    """Scheduling state is corrupt or an invalid lifecycle action was requested."""


class WorkerLeaseState(str, Enum):
    ACTIVE = "active"
    FINISHED = "finished"
    OVER_BUDGET = "over_budget"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class SwarmAdmissionReason(str, Enum):
    BUDGET = "budget"
    WRITE_CONFLICT = "write_conflict"
    AUTHORIZATION = "authorization"
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
    # Raw observed evidence may exceed the contract. Ledger accounting is capped
    # separately so an overage cannot strand a reservation or disappear from audit.
    usage: BudgetUsage = field(default_factory=BudgetUsage)
    settled_usage: BudgetUsage | None = None
    overage_dimensions: tuple[str, ...] = ()
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


@dataclass(frozen=True)
class UsageSettlement:
    """Controller classification for an absolute terminal usage observation."""

    lease_id: str
    observed: BudgetUsage
    accounted: BudgetUsage
    overage_dimensions: tuple[str, ...] = ()
    duplicate: bool = False

    @property
    def over_budget(self) -> bool:
        return bool(self.overage_dimensions)


class SwarmScheduler:
    """Thread-safe team scheduler with content-checked durable checkpoints."""

    _SCHEMA = "arena.swarm-control.v2"
    _LEGACY_SCHEMA = "arena.swarm-control.v1"

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
        self._plan: SwarmPlan | None = None
        self._contracts: dict[str, SubagentContract] = {}
        self._leases: dict[str, WorkerLease] = {}
        self._by_key: dict[str, str] = {}
        self._usage_deliveries: dict[str, tuple[str, BudgetUsage]] = {}
        self._terminal_usage_deliveries: dict[str, tuple[str, BudgetUsage]] = {}
        self._receipts: dict[str, PhaseReceipt] = {}
        self._receipt_by_contract: dict[str, str] = {}
        self._authorizations: dict[str, PhaseAuthorization] = {}
        self._consumed_authorizations: dict[str, str] = {}
        self._cancelled = False
        self._cancel_reason: str | None = None
        self._lock = threading.RLock()

    def register_plan(self, plan: SwarmPlan) -> None:
        """Pin one immutable phase DAG before any child contract is registered."""
        if plan.identity != self.identity:
            raise SwarmContractError("swarm plan identity does not match scheduler identity")
        with self._lock:
            if self._plan is not None:
                if self._plan != plan:
                    raise SwarmContractError("scheduler already owns a different swarm plan")
                return
            if self._contracts or self._leases:
                raise SwarmContractError("swarm plan must be registered before child work")
            self._plan = plan
            self._persist()

    def register(self, contract: SubagentContract) -> None:
        """Register a contract only when every immutable team binding matches."""
        self._require_team_contract(contract)
        with self._lock:
            if self._plan is None:
                raise SwarmContractError("an immutable swarm plan is required before contracts")
            require_contract_plan(contract, self._plan)
            prior = self._contracts.get(contract.contract_id)
            if prior is not None:
                if prior != contract:
                    raise SwarmContractError("contract ID collision")
                return
            if any(
                item.child_agent_id == contract.child_agent_id for item in self._contracts.values()
            ):
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

    def record_phase_receipt(
        self,
        lease_id: str,
        *,
        predecessor_receipt_hashes: tuple[str, ...] = (),
        recorded_at: datetime | None = None,
    ) -> PhaseReceipt:
        """Issue one controller-owned receipt from an exact finished worker result."""
        with self._lock:
            lease = self._leases.get(lease_id)
            if (
                lease is None
                or lease.state is not WorkerLeaseState.FINISHED
                or lease.result is None
            ):
                raise SwarmStateError("phase receipts require an exactly finished worker result")
            prior_hash = self._receipt_by_contract.get(lease.contract.contract_id)
            if prior_hash is not None:
                return self._receipts[prior_hash]
            predecessors = self._owned_receipts(predecessor_receipt_hashes)
            receipt = PhaseReceipt.from_result(
                lease.contract,
                lease.result,
                predecessors=predecessors,
                recorded_at=recorded_at,
            )
            self._receipts[receipt.content_hash] = receipt
            self._receipt_by_contract[receipt.contract_id] = receipt.content_hash
            self._persist()
            return receipt

    def issue_phase_authorization(
        self,
        contract_id: str,
        *,
        predecessor_receipt_hashes: tuple[str, ...],
        authorized_at: datetime | None = None,
    ) -> PhaseAuthorization:
        """Authorize a non-root contract from scheduler-owned predecessor receipts."""
        with self._lock:
            contract = self._contracts.get(contract_id)
            if contract is None:
                raise SwarmContractError("phase authorization requires a registered contract")
            predecessors = self._owned_receipts(predecessor_receipt_hashes)
            authorization = PhaseAuthorization.issue(
                contract,
                predecessors,
                authorized_by=self.orchestrator_id,
                authorized_at=authorized_at,
            )
            prior = next(
                (
                    item
                    for item in self._authorizations.values()
                    if item.contract_id == contract_id
                ),
                None,
            )
            if prior is not None and prior != authorization:
                raise SwarmStateError("contract already has a different phase authorization")
            self._authorizations[authorization.content_hash] = authorization
            self._persist()
            return authorization

    def _owned_receipts(self, hashes: tuple[str, ...]) -> tuple[PhaseReceipt, ...]:
        if len(hashes) != len(set(hashes)):
            raise SwarmContractError("predecessor receipt hashes must be unique")
        try:
            receipts = tuple(self._receipts[item] for item in hashes)
        except KeyError as exc:
            raise SwarmContractError("phase input is not a scheduler-owned receipt") from exc
        for receipt in receipts:
            lease_id = next(
                (
                    lease.lease_id
                    for lease in self._leases.values()
                    if lease.contract.contract_id == receipt.contract_id
                ),
                None,
            )
            lease = self._leases.get(lease_id) if lease_id is not None else None
            if (
                lease is None
                or lease.state is not WorkerLeaseState.FINISHED
                or lease.result is None
                or lease.result.content_hash != receipt.result_hash
            ):
                raise SwarmStateError("phase receipt is not backed by a finished scheduler lease")
        return receipts

    def admit(
        self,
        contract_id: str,
        *,
        idempotency_key: str,
        authorization: PhaseAuthorization | None = None,
    ) -> SwarmAdmission:
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
                    detail=None
                    if same_contract
                    else "idempotency key belongs to another contract",
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
                authorization_hash = self._require_admission_authorization(contract, authorization)
            except (SwarmContractError, SwarmStateError) as exc:
                return SwarmAdmission(
                    False,
                    reason=SwarmAdmissionReason.AUTHORIZATION,
                    detail=str(exc),
                )
            try:
                self._ledger.reserve(contract)
            except BudgetExceededError as exc:
                return SwarmAdmission(False, reason=SwarmAdmissionReason.BUDGET, detail=str(exc))
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
            if authorization_hash is not None:
                self._consumed_authorizations[authorization_hash] = lease.lease_id
            self._persist()
            return SwarmAdmission(True, lease=lease)

    def _require_admission_authorization(
        self,
        contract: SubagentContract,
        authorization: PhaseAuthorization | None,
    ) -> str | None:
        if self._plan is None:
            raise SwarmStateError("scheduler has no immutable swarm plan")
        phase = require_contract_plan(contract, self._plan)
        if not phase.predecessor_phase_ids:
            if authorization is not None:
                raise SwarmContractError("root phases must not consume downstream authorization")
            if contract.phase_inputs:
                raise SwarmContractError("root phase contract cannot contain predecessor inputs")
            return None
        if authorization is None:
            raise SwarmContractError("non-root phase admission requires authorization")
        authorization.require_contract(contract, orchestrator_id=self.orchestrator_id)
        digest = authorization.content_hash
        if self._authorizations.get(digest) != authorization:
            raise SwarmStateError("phase authorization was not issued by this scheduler")
        if digest in self._consumed_authorizations:
            raise SwarmStateError("phase authorization has already been consumed")
        predecessors = self._owned_receipts(authorization.predecessor_receipt_hashes)
        expected_contracts = {
            contract_id
            for phase_id in phase.predecessor_phase_ids
            for contract_id in self._plan.phase(phase_id).contract_ids
        }
        if {item.contract_id for item in predecessors} != expected_contracts or len(
            predecessors
        ) != len(expected_contracts):
            raise SwarmStateError("authorization omits or substitutes planned predecessors")
        if any(item.disposition is not SubagentDisposition.COMPLETED for item in predecessors):
            raise SwarmStateError("authorization predecessors are not completed")
        return digest

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

    def observe_terminal_usage(
        self,
        lease_id: str,
        observed: BudgetUsage,
        *,
        delivery_id: str,
    ) -> UsageSettlement:
        """Durably retain absolute terminal usage, including contract overages.

        Normal callers use this before :meth:`complete`. If the observation is
        over budget, the lease becomes terminal immediately and its reservation is
        settled at the contract ceiling. A late result from a timed-out/cancelled
        process updates audit evidence without clearing its pending stop request.
        """

        delivery = delivery_id.strip()
        if not delivery:
            raise SwarmContractError("delivery_id must not be empty")
        with self._lock:
            lease = self._leases.get(lease_id)
            if lease is None:
                raise SwarmStateError("terminal usage references an unknown worker lease")
            prior = self._terminal_usage_deliveries.get(delivery)
            if prior is not None:
                if prior != (lease_id, observed):
                    raise SwarmStateError("terminal usage delivery ID collision")
                return self._usage_settlement(lease, duplicate=True)
            if any(
                recorded_lease_id == lease_id
                for recorded_lease_id, _ in self._terminal_usage_deliveries.values()
            ):
                raise SwarmStateError("worker lease already has a terminal usage observation")
            if lease.state is WorkerLeaseState.FINISHED:
                raise SwarmStateError("finished usage cannot be replaced after result acceptance")

            retained = self._usage_max(lease.usage, observed)
            dimensions = self._overage_dimensions(retained, lease.contract)
            accounted = self._cap_usage(retained, lease.contract)
            lease.usage = retained
            lease.overage_dimensions = dimensions
            self._terminal_usage_deliveries[delivery] = (lease_id, observed)

            if lease.state is WorkerLeaseState.ACTIVE and dimensions:
                lease.state = WorkerLeaseState.OVER_BUDGET
                lease.terminal_reason = "budget_exceeded:" + ",".join(dimensions)
                lease.settled_usage = accounted
                self._ledger.settle(lease.contract.contract_id, accounted)
            elif lease.state is not WorkerLeaseState.ACTIVE:
                lease.settled_usage = accounted
                # The previous terminal transition already settled this lease.
                # Rebuild the bounded ledger to apply the capped late evidence.
                self._rebuild_ledger()
            self._persist()
            return self._usage_settlement(lease)

    def record_terminal_result(self, lease_id: str, result: SubagentResult) -> WorkerLease:
        """Attach fail-closed evidence without changing a controller terminal state.

        A late process result can never resurrect or complete a lease after the
        controller has classified it as cancelled, timed out, or over budget.
        The orchestrator may instead retain one exact ``FAILED`` result whose
        reason codes describe the controller decision and any observed overage.
        """

        with self._lock:
            lease = self._leases.get(lease_id)
            if lease is None:
                raise SwarmStateError("terminal result references an unknown worker lease")
            if lease.state not in {
                WorkerLeaseState.OVER_BUDGET,
                WorkerLeaseState.TIMED_OUT,
                WorkerLeaseState.CANCELLED,
            }:
                raise SwarmStateError("worker lease is not a controller terminal state")
            self._require_result_binding(lease.contract, result)
            self._require_controller_terminal_result(lease, result)
            if lease.result is not None:
                if lease.result == result:
                    return lease
                raise SwarmStateError("worker lease already has a different structured result")
            lease.result = result
            self._persist()
            return lease

    def record_overage_result(self, lease_id: str, result: SubagentResult) -> WorkerLease:
        """Compatibility wrapper requiring terminal usage overage classification."""

        with self._lock:
            lease = self._leases.get(lease_id)
            if lease is None or not lease.overage_dimensions:
                raise SwarmStateError("overage result requires classified terminal usage")
        return self.record_terminal_result(lease_id, result)

    @staticmethod
    def _require_controller_terminal_result(lease: WorkerLease, result: SubagentResult) -> None:
        if result.disposition is not SubagentDisposition.FAILED:
            raise SwarmStateError("controller terminal result must be FAILED")
        has_budget_reason = any(
            item == "budget_exceeded" or item.startswith("budget_exceeded:")
            for item in result.reason_codes
        )
        if lease.overage_dimensions and not has_budget_reason:
            raise SwarmStateError("terminal overage result requires a budget reason")
        if (
            lease.state in {WorkerLeaseState.TIMED_OUT, WorkerLeaseState.CANCELLED}
            and (
                result.controller_terminal_reason != lease.terminal_reason
                or lease.terminal_reason not in result.reason_codes
            )
        ):
            raise SwarmStateError(
                "late terminal result must bind the exact controller terminal reason"
            )

    @classmethod
    def _usage_settlement(cls, lease: WorkerLease, *, duplicate: bool = False) -> UsageSettlement:
        return UsageSettlement(
            lease_id=lease.lease_id,
            observed=lease.usage,
            accounted=lease.settled_usage or cls._cap_usage(lease.usage, lease.contract),
            overage_dimensions=lease.overage_dimensions,
            duplicate=duplicate,
        )

    @staticmethod
    def _usage_max(left: BudgetUsage, right: BudgetUsage) -> BudgetUsage:
        return BudgetUsage(
            wall_seconds=max(left.wall_seconds, right.wall_seconds),
            tokens=max(left.tokens, right.tokens),
            tool_calls=max(left.tool_calls, right.tool_calls),
            cost=max(left.cost, right.cost),
        )

    @staticmethod
    def _overage_dimensions(usage: BudgetUsage, contract: SubagentContract) -> tuple[str, ...]:
        limit = contract.budget
        return tuple(
            name
            for name, used, allowed in (
                ("wall_seconds", usage.wall_seconds, limit.wall_seconds),
                ("tokens", usage.tokens, limit.token_limit),
                ("tool_calls", usage.tool_calls, limit.tool_call_limit),
                ("cost", usage.cost, limit.cost_limit),
            )
            if used > allowed
        )

    @staticmethod
    def _cap_usage(usage: BudgetUsage, contract: SubagentContract) -> BudgetUsage:
        limit = contract.budget
        return BudgetUsage(
            wall_seconds=min(usage.wall_seconds, limit.wall_seconds),
            tokens=min(usage.tokens, limit.token_limit),
            tool_calls=min(usage.tool_calls, limit.tool_call_limit),
            cost=min(usage.cost, limit.cost_limit),
        )

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
            self._require_result_binding(expected, result)
            self._finalize_elapsed(lease)
            lease.state = WorkerLeaseState.FINISHED
            lease.result = result
            lease.settled_usage = lease.usage
            self._ledger.settle(expected.contract_id, lease.settled_usage)
            self._persist()
            return lease

    @staticmethod
    def _require_result_binding(contract: SubagentContract, result: SubagentResult) -> None:
        if (
            result.contract_id != contract.contract_id
            or result.contract_hash != contract.content_hash
            or result.identity != contract.identity
            or result.plan_hash != contract.plan_hash
            or result.phase_id != contract.phase_id
            or result.lease_id != contract.lease_id
            or result.agent_id != contract.child_agent_id
            or result.role is not contract.role
        ):
            raise SwarmStateError("subagent result is not bound to the exact worker contract")

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
            lease.settled_usage = lease.usage
            self._ledger.settle(lease.contract.contract_id, lease.settled_usage)
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
                lease.settled_usage = lease.usage
                self._ledger.settle(lease.contract.contract_id, lease.settled_usage)
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
                "hard_deadline_exceeded" if now >= lease.deadline_at else "heartbeat_expired"
            )
            lease.stop_required = True
            lease.settled_usage = lease.usage
            self._ledger.settle(lease.contract.contract_id, lease.settled_usage)
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
                    "settled_usage": (
                        lease.settled_usage.model_dump(mode="json")
                        if lease.settled_usage is not None
                        else None
                    ),
                    "overage_dimensions": lease.overage_dimensions,
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
            "plan": self._plan.model_dump(mode="json") if self._plan is not None else None,
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
            "terminal_usage_deliveries": {
                key: {"lease_id": lease_id, "usage": usage.model_dump(mode="json")}
                for key, (lease_id, usage) in sorted(self._terminal_usage_deliveries.items())
            },
            "receipts": [
                item.model_dump(mode="json")
                for item in sorted(self._receipts.values(), key=lambda item: item.content_hash)
            ],
            "receipt_by_contract": dict(sorted(self._receipt_by_contract.items())),
            "authorizations": [
                item.model_dump(mode="json")
                for item in sorted(
                    self._authorizations.values(), key=lambda item: item.content_hash
                )
            ],
            "consumed_authorizations": dict(sorted(self._consumed_authorizations.items())),
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
            if state["schema"] not in {cls._SCHEMA, cls._LEGACY_SCHEMA}:
                raise SwarmStateError("unsupported swarm control checkpoint schema")
            scheduler = cls(
                TeamBudget.model_validate(state["budget"]),
                identity=SwarmIdentity.model_validate(state["identity"]),
                orchestrator_id=state["orchestrator_id"],
                lease_ttl_s=state["lease_ttl_s"],
                clock=clock,
                state_path=path,
            )
            if state.get("plan") is None:
                raise SwarmStateError("swarm checkpoint predates immutable plan authority")
            scheduler._plan = SwarmPlan.model_validate(state["plan"])
            if scheduler._plan.identity != scheduler.identity:
                raise SwarmStateError("recovered swarm plan identity mismatch")
            for raw in state["contracts"]:
                contract = SubagentContract.model_validate(raw)
                scheduler._require_team_contract(contract)
                require_contract_plan(contract, scheduler._plan)
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
                    settled_usage=(
                        BudgetUsage.model_validate(raw["settled_usage"])
                        if raw.get("settled_usage") is not None
                        else (
                            BudgetUsage.model_validate(raw["usage"])
                            if raw["state"] != WorkerLeaseState.ACTIVE.value
                            else None
                        )
                    ),
                    overage_dimensions=tuple(raw.get("overage_dimensions", ())),
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
            scheduler._terminal_usage_deliveries = {
                key: (raw["lease_id"], BudgetUsage.model_validate(raw["usage"]))
                for key, raw in state.get("terminal_usage_deliveries", {}).items()
            }
            scheduler._receipts = {
                item.content_hash: item
                for item in (PhaseReceipt.model_validate(raw) for raw in state.get("receipts", ()))
            }
            scheduler._receipt_by_contract = dict(state.get("receipt_by_contract", {}))
            scheduler._authorizations = {
                item.content_hash: item
                for item in (
                    PhaseAuthorization.model_validate(raw)
                    for raw in state.get("authorizations", ())
                )
            }
            scheduler._consumed_authorizations = dict(state.get("consumed_authorizations", {}))
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
            (lease for lease in self._leases.values() if lease.state is WorkerLeaseState.ACTIVE),
            key=lambda item: item.lease_id,
        )
        for lease in terminal:
            if lease.settled_usage is None:
                raise SwarmStateError("terminal lease is missing capped settlement usage")
            self._ledger.reserve(lease.contract)
            self._ledger.settle(lease.contract.contract_id, lease.settled_usage)
        for lease in active:
            self._ledger.reserve(lease.contract)

    def _validate_recovered_state(self) -> None:
        if self._plan is None:
            raise SwarmStateError("recovered scheduler is missing its immutable plan")
        if len({item.child_agent_id for item in self._contracts.values()}) != len(self._contracts):
            raise SwarmStateError("recovered child identity is assigned more than once")
        if len({item.lease_id for item in self._contracts.values()}) != len(self._contracts):
            raise SwarmStateError("recovered worker lease ID is assigned more than once")
        for lease in self._leases.values():
            if self._contracts.get(lease.contract.contract_id) != lease.contract:
                raise SwarmStateError("lease references an unknown or changed contract")
            if lease.lease_id != lease.contract.lease_id:
                raise SwarmStateError("lease identity differs from immutable contract")
            require_contract_plan(lease.contract, self._plan)
            if lease.state is WorkerLeaseState.ACTIVE:
                if lease.settled_usage is not None or lease.result is not None:
                    raise SwarmStateError("active lease cannot contain terminal settlement")
            elif lease.settled_usage is None:
                raise SwarmStateError("terminal lease is missing capped settlement usage")
            if lease.state is WorkerLeaseState.FINISHED and lease.result is None:
                raise SwarmStateError("finished lease is missing its structured result")
            if lease.state is WorkerLeaseState.OVER_BUDGET and not lease.overage_dimensions:
                raise SwarmStateError("over-budget lease is missing overage classification")
            if lease.stop_required and lease.state not in {
                WorkerLeaseState.CANCELLED,
                WorkerLeaseState.TIMED_OUT,
            }:
                raise SwarmStateError("only cancelled or timed-out leases may require cleanup")
            if lease.result is not None:
                self._require_result_binding(lease.contract, lease.result)
                if lease.state is not WorkerLeaseState.FINISHED:
                    self._require_controller_terminal_result(lease, lease.result)
            dimensions = self._overage_dimensions(lease.usage, lease.contract)
            if dimensions != lease.overage_dimensions:
                raise SwarmStateError("recorded overage dimensions do not match observed usage")
            if lease.settled_usage is not None and lease.settled_usage != self._cap_usage(
                lease.usage, lease.contract
            ):
                raise SwarmStateError("capped settlement does not match observed usage")
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
        terminal_observed: dict[str, BudgetUsage] = {}
        for delivery, (lease_id, usage) in self._terminal_usage_deliveries.items():
            if not delivery or lease_id not in self._leases or lease_id in terminal_observed:
                raise SwarmStateError("terminal usage delivery index is inconsistent")
            terminal_observed[lease_id] = usage
        for lease_id, lease in self._leases.items():
            usage = self._usage_max(
                delivered.get(lease_id, BudgetUsage()),
                terminal_observed.get(lease_id, BudgetUsage()),
            )
            recorded = lease.usage
            if (
                usage.wall_seconds > recorded.wall_seconds
                or usage.tokens != recorded.tokens
                or usage.tool_calls != recorded.tool_calls
                or usage.cost != recorded.cost
            ):
                raise SwarmStateError("recorded usage does not match idempotent deliveries")
        if set(self._receipt_by_contract.values()) != set(self._receipts):
            raise SwarmStateError("phase receipt index is inconsistent")
        for contract_id, digest in self._receipt_by_contract.items():
            receipt = self._receipts.get(digest)
            if receipt is None or receipt.contract_id != contract_id:
                raise SwarmStateError("phase receipt contract index is inconsistent")
            predecessors = self._owned_receipts(receipt.predecessor_receipt_hashes)
            lease = next(
                (
                    item
                    for item in self._leases.values()
                    if item.contract.contract_id == contract_id
                ),
                None,
            )
            if lease is None or lease.result is None:
                raise SwarmStateError("phase receipt lacks a scheduler-owned result")
            expected = PhaseReceipt.from_result(
                lease.contract,
                lease.result,
                predecessors=predecessors,
                recorded_at=receipt.recorded_at,
            )
            if expected != receipt:
                raise SwarmStateError("phase receipt differs from scheduler-owned evidence")
        for digest, authorization in self._authorizations.items():
            if digest != authorization.content_hash:
                raise SwarmStateError("phase authorization content hash mismatch")
            contract = self._contracts.get(authorization.contract_id)
            if contract is None:
                raise SwarmStateError("phase authorization references an unknown contract")
            authorization.require_contract(contract, orchestrator_id=self.orchestrator_id)
            self._owned_receipts(authorization.predecessor_receipt_hashes)
        for digest, lease_id in self._consumed_authorizations.items():
            authorization = self._authorizations.get(digest)
            lease = self._leases.get(lease_id)
            if (
                authorization is None
                or lease is None
                or authorization.contract_id != lease.contract.contract_id
            ):
                raise SwarmStateError("consumed phase authorization index is inconsistent")
