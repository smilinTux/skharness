"""Trusted orchestration of bounded Pi subagent teams.

Workers execute narrow contracts.  This coordinator owns admission, ordering,
team cancellation, A2A evidence, and the independent completion gate; it never
allows a worker result to mutate card state directly.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from .swarm import (
    A2AEvent,
    A2AEventKind,
    BudgetUsage,
    SubagentContract,
    SubagentDisposition,
    SubagentResult,
    SwarmContractError,
    SwarmRole,
)
from .swarm_control import SwarmScheduler
from .swarm_verifier import CompletionDecision, SwarmCompletionGate, VerifierAttestation
from .trajectory import CardSize


class SwarmPhase(str, Enum):
    SCOUT = "scout"
    BUILD = "build"
    TEST = "test"


@dataclass(frozen=True)
class SwarmTopology:
    card_size: CardSize
    scout_count: int
    builder_count: int
    tester_count: int
    reason: str

    @property
    def uses_subagents(self) -> bool:
        return self.scout_count + self.builder_count + self.tester_count > 1


class SwarmTopologyPolicy:
    """Conservative S/M/L policy; small work is single-agent by default."""

    @staticmethod
    def select(
        card_size: CardSize,
        *,
        independent_workstreams: int = 1,
        cross_repository: bool = False,
    ) -> SwarmTopology:
        if independent_workstreams < 1:
            raise ValueError("independent_workstreams must be positive")
        if card_size is CardSize.SMALL and independent_workstreams == 1:
            return SwarmTopology(card_size, 0, 1, 0, "small_single_agent_default")
        if card_size is CardSize.MEDIUM:
            return SwarmTopology(card_size, 1, 1, 1, "medium_scout_build_test")
        scouts = min(2, independent_workstreams) if cross_repository else 1
        builders = min(3, independent_workstreams)
        return SwarmTopology(card_size, scouts, builders, 1, "large_bounded_swarm")


@dataclass(frozen=True)
class WorkerExecution:
    result: SubagentResult
    usage: BudgetUsage


WorkerExecutor = Callable[[SubagentContract], WorkerExecution]
WorkerStopper = Callable[[str], None]
AttestationProvider = Callable[[tuple[SubagentResult, ...]], VerifierAttestation | None]


@dataclass(frozen=True)
class SwarmRunReport:
    results: tuple[SubagentResult, ...]
    completion: CompletionDecision
    a2a_event_digests: tuple[str, ...]
    cancelled_lease_ids: tuple[str, ...] = ()


class A2AJournal:
    """Append-only local trajectory for parent/child messages."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def append(self, event: A2AEvent) -> str:
        row = event.model_dump(mode="json") | {"content_hash": event.content_hash}
        encoded = (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode()
        with self._lock:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, encoded)
                os.fsync(fd)
            finally:
                os.close(fd)
        return event.content_hash


class TrustedSwarmOrchestrator:
    """Run role phases under controller-owned leases and verifier authority."""

    _PHASE_ROLES = (
        (SwarmPhase.SCOUT, frozenset({SwarmRole.SCOUT})),
        (SwarmPhase.BUILD, frozenset({SwarmRole.BUILDER})),
        (SwarmPhase.TEST, frozenset({SwarmRole.TESTER})),
    )

    def __init__(
        self,
        scheduler: SwarmScheduler,
        completion_gate: SwarmCompletionGate,
        journal: A2AJournal,
    ) -> None:
        self.scheduler = scheduler
        self.completion_gate = completion_gate
        self.journal = journal

    def run(
        self,
        contracts: Iterable[SubagentContract],
        *,
        execute: WorkerExecutor,
        stop: WorkerStopper,
        attest: AttestationProvider,
    ) -> SwarmRunReport:
        contracts = tuple(contracts)
        if not contracts:
            raise SwarmContractError("a swarm run requires at least one contract")
        for contract in contracts:
            self.scheduler.register(contract)
        results: list[SubagentResult] = []
        event_digests: list[str] = []
        cancelled: set[str] = set()

        for _, roles in self._PHASE_ROLES:
            phase_contracts = tuple(item for item in contracts if item.role in roles)
            if not phase_contracts:
                continue
            phase_results, phase_events = self._run_phase(phase_contracts, execute, stop)
            results.extend(phase_results)
            event_digests.extend(phase_events)
            if any(item.disposition is not SubagentDisposition.COMPLETED for item in phase_results):
                cancelled.update(self.scheduler.cancel_team(reason="non_completed_phase"))
                self._stop_pending(stop)
                break

        ordered = tuple(sorted(results, key=lambda item: item.contract_id))
        attestation = attest(ordered)
        decision = self.completion_gate.evaluate(ordered, attestation)
        if not decision.authorized:
            cancelled.update(self.scheduler.cancel_team(reason="completion_gate_denied"))
            self._stop_pending(stop)
        return SwarmRunReport(
            results=ordered,
            completion=decision,
            a2a_event_digests=tuple(event_digests),
            cancelled_lease_ids=tuple(sorted(cancelled)),
        )

    def _run_phase(
        self,
        contracts: tuple[SubagentContract, ...],
        execute: WorkerExecutor,
        stop: WorkerStopper,
    ) -> tuple[list[SubagentResult], list[str]]:
        admitted: list[SubagentContract] = []
        event_digests: list[str] = []
        for contract in contracts:
            admission = self.scheduler.admit(
                contract.contract_id, idempotency_key=f"{contract.team_id}:{contract.contract_id}"
            )
            if not admission.admitted or admission.duplicate:
                raise SwarmContractError(
                    f"worker admission failed for {contract.contract_id}: {admission.reason}"
                )
            admitted.append(contract)
            event_digests.append(self.journal.append(self._assignment_event(contract)))

        results: list[SubagentResult] = []
        pool = ThreadPoolExecutor(max_workers=len(admitted), thread_name_prefix="arena-swarm")
        futures: dict[Future[WorkerExecution], SubagentContract] = {
            pool.submit(execute, contract): contract for contract in admitted
        }
        pending = set(futures)
        try:
            while pending:
                done, pending = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
                for contract in (futures[item] for item in pending):
                    self.scheduler.heartbeat(contract.lease_id)
                expired = set(self.scheduler.reap_timeouts())
                if expired:
                    for lease_id in expired:
                        stop(lease_id)
                        self.scheduler.acknowledge_stopped(lease_id)
                    pending = {
                        future
                        for future in pending
                        if futures[future].lease_id not in expired
                    }
                for future in done:
                    contract = futures[future]
                    try:
                        execution = future.result()
                        self.scheduler.charge(
                            contract.lease_id,
                            execution.usage,
                            delivery_id=f"usage:{contract.contract_id}:1",
                        )
                        self.scheduler.complete(contract.lease_id, execution.result)
                        result = execution.result
                    except Exception as exc:
                        self.scheduler.cancel_worker(
                            contract.lease_id, reason=f"worker_exception:{type(exc).__name__}"
                        )
                        self._stop_pending(stop)
                        raise
                    results.append(result)
                    event_digests.append(self.journal.append(self._result_event(contract, result)))
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return results, event_digests

    def _stop_pending(self, stop: WorkerStopper) -> None:
        for lease_id in self.scheduler.stop_requests():
            stop(lease_id)
            self.scheduler.acknowledge_stopped(lease_id)

    @staticmethod
    def _assignment_event(contract: SubagentContract) -> A2AEvent:
        return A2AEvent.from_contract(
            contract,
            event_id=f"{contract.contract_id}:assignment",
            sender_agent_id=contract.parent_agent_id,
            recipient_agent_id=contract.child_agent_id,
            kind=A2AEventKind.ASSIGNMENT,
            sequence=1,
            body=contract.task,
            created_at=datetime.now(timezone.utc),
        )

    @staticmethod
    def _result_event(contract: SubagentContract, result: SubagentResult) -> A2AEvent:
        return A2AEvent.from_contract(
            contract,
            event_id=f"{contract.contract_id}:result",
            sender_agent_id=contract.child_agent_id,
            recipient_agent_id=contract.parent_agent_id,
            kind=A2AEventKind.RESULT,
            sequence=2,
            prior_event_id=f"{contract.contract_id}:assignment",
            body=result.disposition.value,
            payload={"result_hash": result.content_hash},
            created_at=datetime.now(timezone.utc),
        )
