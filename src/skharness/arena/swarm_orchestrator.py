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
    PhaseAuthorization,
    PhaseReceipt,
    ScoutAssessment,
    SubagentContract,
    SubagentDisposition,
    SubagentResult,
    SwarmContractError,
    SwarmPlan,
    SwarmRole,
    bind_phase_inputs,
    require_contract_plan,
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
        # Isolated builders can produce divergent commits. Until a trusted
        # controller-owned integration/cherry-pick stage exists, admitting more
        # than one would create work the tester cannot safely join. Parallelize
        # read-only scouting now; keep exactly one write authority.
        return SwarmTopology(
            card_size,
            scouts,
            1,
            1,
            "large_parallel_scout_single_builder_until_trusted_integration",
        )


@dataclass(frozen=True)
class WorkerExecution:
    result: SubagentResult
    usage: BudgetUsage


WorkerExecutor = Callable[[SubagentContract], WorkerExecution]
WorkerStopper = Callable[[str], None]
AttestationProvider = Callable[
    [tuple[SubagentResult, ...], tuple[PhaseReceipt, ...]],
    VerifierAttestation | None,
]


@dataclass(frozen=True)
class SwarmRunReport:
    results: tuple[SubagentResult, ...]
    completion: CompletionDecision
    a2a_event_digests: tuple[str, ...]
    cancelled_lease_ids: tuple[str, ...] = ()
    phase_receipts: tuple[PhaseReceipt, ...] = ()
    phase_authorization_hashes: tuple[str, ...] = ()
    failure_reasons: tuple[str, ...] = ()


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

    def __init__(
        self,
        scheduler: SwarmScheduler,
        completion_gate: SwarmCompletionGate,
        journal: A2AJournal,
        plan: SwarmPlan,
        shutdown_grace_s: float = 2.0,
    ) -> None:
        self.scheduler = scheduler
        self.completion_gate = completion_gate
        self.journal = journal
        self.plan = plan
        if shutdown_grace_s <= 0:
            raise ValueError("shutdown_grace_s must be positive")
        self.shutdown_grace_s = shutdown_grace_s
        if plan.identity != scheduler.identity:
            raise SwarmContractError("swarm plan identity does not match scheduler")

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
        by_id = {contract.contract_id: contract for contract in contracts}
        planned_ids = {
            contract_id for phase in self.plan.phases for contract_id in phase.contract_ids
        }
        if len(by_id) != len(contracts) or set(by_id) != planned_ids:
            raise SwarmContractError("contracts do not exactly satisfy the immutable swarm plan")
        for contract in contracts:
            require_contract_plan(contract, self.plan)
        results: list[SubagentResult] = []
        receipts: list[PhaseReceipt] = []
        event_digests: list[str] = []
        authorization_hashes: list[str] = []
        failures: list[str] = []
        cancelled: set[str] = set()

        register_plan = getattr(self.scheduler, "register_plan", None)
        if callable(register_plan):
            register_plan(self.plan)

        for phase in self.plan.phases:
            templates = tuple(by_id[contract_id] for contract_id in phase.contract_ids)
            predecessor_receipts = tuple(
                item for item in receipts if item.phase_id in phase.predecessor_phase_ids
            )
            if phase.predecessor_phase_ids:
                try:
                    phase_contracts = tuple(
                        bind_phase_inputs(item, predecessor_receipts, plan=self.plan)
                        for item in templates
                    )
                except SwarmContractError as exc:
                    failures.append(f"phase_lineage_invalid:{phase.phase_id}:{exc}")
                    break
            else:
                if any(item.phase_inputs or item.inputs_bound_at for item in templates):
                    failures.append(f"root_phase_has_inputs:{phase.phase_id}")
                    break
                phase_contracts = templates
            for contract in phase_contracts:
                self.scheduler.register(contract)
            try:
                authorizations = (
                    {
                        item.contract_id: self.scheduler.issue_phase_authorization(
                            item.contract_id,
                            predecessor_receipt_hashes=tuple(
                                receipt.content_hash for receipt in predecessor_receipts
                            ),
                        )
                        for item in phase_contracts
                    }
                    if phase.predecessor_phase_ids
                    else {}
                )
            except Exception as exc:
                failures.append(
                    f"phase_authorization_failed:{phase.phase_id}:{type(exc).__name__}"
                )
                break
            phase_results, phase_events, phase_failures = self._run_phase(
                phase_contracts,
                authorizations,
                execute,
                stop,
            )
            results.extend(phase_results)
            event_digests.extend(phase_events)
            authorization_hashes.extend(
                item.content_hash for item in authorizations.values()
            )
            failures.extend(phase_failures)
            expected_ids = set(phase.contract_ids)
            actual_ids = [item.contract_id for item in phase_results]
            if len(actual_ids) != len(expected_ids) or set(actual_ids) != expected_ids:
                failures.append(f"phase_result_cardinality_mismatch:{phase.phase_id}")
            completed_ids = [
                item.contract_id
                for item in phase_results
                if item.disposition is SubagentDisposition.COMPLETED
            ]
            if len(completed_ids) != len(expected_ids) or set(completed_ids) != expected_ids:
                failures.append(
                    f"phase_completed_cardinality_mismatch:{phase.phase_id}"
                )
            if any(item.disposition is not SubagentDisposition.COMPLETED for item in phase_results):
                failures.append(f"phase_not_completed:{phase.phase_id}")
            if any(
                item.role is SwarmRole.SCOUT
                and item.scout_assessment is not ScoutAssessment.ACTIONABLE
                for item in phase_results
            ):
                failures.append(f"scout_not_actionable:{phase.phase_id}")
            if failures:
                break
            contracts_by_id = {item.contract_id: item for item in phase_contracts}
            for contract_id in phase.contract_ids:
                try:
                    receipt = self.scheduler.record_phase_receipt(
                        contracts_by_id[contract_id].lease_id,
                        predecessor_receipt_hashes=tuple(
                            item.content_hash for item in predecessor_receipts
                        ),
                    )
                    receipts.append(receipt)
                except SwarmContractError as exc:
                    failures.append(f"phase_receipt_invalid:{phase.phase_id}:{exc}")
            if failures:
                break

        results_by_id = {item.contract_id: item for item in results}
        ordered = tuple(
            results_by_id[contract_id]
            for phase in self.plan.phases
            for contract_id in phase.contract_ids
            if contract_id in results_by_id
        )
        receipt_tuple = tuple(receipts)
        attestation = attest(ordered, receipt_tuple) if not failures else None
        decision = self.completion_gate.evaluate(ordered, receipt_tuple, attestation)
        if failures or not decision.authorized:
            reason = "phase_execution_failed" if failures else "completion_gate_denied"
            cancelled.update(self.scheduler.cancel_team(reason=reason))
            self._stop_pending(stop)
        return SwarmRunReport(
            results=ordered,
            completion=decision,
            a2a_event_digests=tuple(event_digests),
            cancelled_lease_ids=tuple(sorted(cancelled)),
            phase_receipts=receipt_tuple,
            phase_authorization_hashes=tuple(authorization_hashes),
            failure_reasons=tuple(sorted(set(failures))),
        )

    def _run_phase(
        self,
        contracts: tuple[SubagentContract, ...],
        authorizations: dict[str, PhaseAuthorization],
        execute: WorkerExecutor,
        stop: WorkerStopper,
    ) -> tuple[list[SubagentResult], list[str], list[str]]:
        admitted: list[SubagentContract] = []
        event_digests: list[str] = []
        failures: list[str] = []
        for contract in contracts:
            try:
                admission = self.scheduler.admit(
                    contract.contract_id,
                    idempotency_key=f"{contract.team_id}:{contract.contract_id}",
                    authorization=authorizations.get(contract.contract_id),
                )
            except Exception as exc:
                failures.append(
                    f"worker_admission_exception:{contract.contract_id}:{type(exc).__name__}"
                )
                break
            if not admission.admitted or admission.duplicate:
                failures.append(
                    f"worker_admission_denied:{contract.contract_id}:{admission.reason}"
                )
                break
            admitted.append(contract)
            event_digests.append(
                self.journal.append(
                    self._assignment_event(
                        contract, authorizations.get(contract.contract_id)
                    )
                )
            )

        if failures:
            return [], event_digests, failures

        results: list[SubagentResult] = []
        pool = ThreadPoolExecutor(max_workers=len(admitted), thread_name_prefix="arena-swarm")
        futures: dict[Future[WorkerExecution], SubagentContract] = {
            pool.submit(execute, contract): contract for contract in admitted
        }
        pending = set(futures)
        late_futures: dict[Future[WorkerExecution], SubagentContract] = {}
        try:
            while pending:
                done, pending = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
                for contract in (futures[item] for item in pending):
                    self.scheduler.heartbeat(contract.lease_id)
                expired = set(self.scheduler.reap_timeouts())
                if expired:
                    for lease_id in expired:
                        expired_contract = next(
                            (
                                item
                                for item in contracts
                                if item.lease_id == lease_id
                            ),
                            None,
                        )
                        failures.append(
                            "worker_timed_out:"
                            + (
                                expired_contract.contract_id
                                if expired_contract is not None
                                else lease_id
                            )
                        )
                        stop(lease_id)
                        self.scheduler.acknowledge_stopped(lease_id)
                    expired_futures = {
                        future: futures[future]
                        for future in pending
                        if futures[future].lease_id in expired
                    }
                    late_futures.update(expired_futures)
                    pending -= set(expired_futures)
                for future in done:
                    contract = futures[future]
                    try:
                        execution = future.result()
                        settlement = self.scheduler.observe_terminal_usage(
                            contract.lease_id,
                            execution.usage,
                            delivery_id=f"terminal-usage:{contract.contract_id}:1",
                        )
                        if settlement.over_budget:
                            result = self._overage_result(
                                contract,
                                execution.result,
                                settlement.overage_dimensions,
                            )
                            self.scheduler.record_overage_result(contract.lease_id, result)
                            failures.append(
                                f"worker_over_budget:{contract.contract_id}:"
                                + ",".join(settlement.overage_dimensions)
                            )
                        else:
                            self.scheduler.complete(contract.lease_id, execution.result)
                            result = execution.result
                    except Exception as exc:
                        self.scheduler.cancel_worker(
                            contract.lease_id, reason=f"worker_exception:{type(exc).__name__}"
                        )
                        failures.append(
                            f"worker_exception:{contract.contract_id}:{type(exc).__name__}"
                        )
                        continue
                    results.append(result)
                    event_digests.append(self.journal.append(self._result_event(contract, result)))
            if late_futures:
                self._drain_late_futures(
                    late_futures,
                    results=results,
                    event_digests=event_digests,
                    failures=failures,
                )
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return results, event_digests, failures

    def _drain_late_futures(
        self,
        futures: dict[Future[WorkerExecution], SubagentContract],
        *,
        results: list[SubagentResult],
        event_digests: list[str],
        failures: list[str],
    ) -> None:
        done, hung = wait(set(futures), timeout=self.shutdown_grace_s)
        for future in done:
            contract = futures[future]
            result = self._observe_late_future(contract, future)
            if result is None:
                failures.append(f"late_worker_result_invalid:{contract.contract_id}")
                continue
            results.append(result)
            event_digests.append(self.journal.append(self._result_event(contract, result)))
        for future in hung:
            contract = futures[future]
            failures.append(f"worker_shutdown_grace_exceeded:{contract.contract_id}")
            future.add_done_callback(
                lambda item, bound_contract=contract: self._observe_late_future(
                    bound_contract, item
                )
            )

    def _observe_late_future(
        self,
        contract: SubagentContract,
        future: Future[WorkerExecution],
    ) -> SubagentResult | None:
        """Retain a late worker's evidence without changing controller finality."""

        try:
            execution = future.result()
            settlement = self.scheduler.observe_terminal_usage(
                contract.lease_id,
                execution.usage,
                delivery_id=f"terminal-usage-late:{contract.contract_id}:1",
            )
            lease = self.scheduler.lease(contract.lease_id)
            terminal_reason = (
                lease.terminal_reason if lease is not None and lease.terminal_reason else "timed_out"
            )
            reasons = [terminal_reason]
            if settlement.over_budget:
                reasons.append("budget_exceeded")
                reasons.extend(
                    f"budget_exceeded:{item}" for item in settlement.overage_dimensions
                )
            result = SubagentResult.from_contract(
                contract,
                disposition=SubagentDisposition.FAILED,
                summary="Worker returned after the controller terminalized its lease.",
                reason_codes=tuple(dict.fromkeys(reasons)),
                evidence_refs=execution.result.evidence_refs,
                scout_assessment=(
                    ScoutAssessment.BLOCKED if contract.role is SwarmRole.SCOUT else None
                ),
                scout_findings=(
                    execution.result.scout_findings
                    if contract.role is SwarmRole.SCOUT
                    else ()
                ),
                controller_terminal_reason=terminal_reason,
                started_at=execution.result.started_at,
                finished_at=execution.result.finished_at,
            )
            self.scheduler.record_terminal_result(contract.lease_id, result)
            return result
        except Exception:
            # The phase already failed closed. The bounded caller records a
            # failure reason; asynchronous observers must never raise on a worker
            # thread and destabilize controller shutdown.
            return None

    @staticmethod
    def _overage_result(
        contract: SubagentContract,
        worker_result: SubagentResult,
        dimensions: tuple[str, ...],
    ) -> SubagentResult:
        return SubagentResult.from_contract(
            contract,
            disposition=SubagentDisposition.FAILED,
            summary="Worker terminal usage exceeded its immutable contract budget.",
            reason_codes=("budget_exceeded",)
            + tuple(f"budget_exceeded:{item}" for item in dimensions),
            evidence_refs=worker_result.evidence_refs,
            scout_assessment=(
                ScoutAssessment.BLOCKED if contract.role is SwarmRole.SCOUT else None
            ),
            started_at=worker_result.started_at,
            finished_at=worker_result.finished_at,
        )

    def _stop_pending(self, stop: WorkerStopper) -> None:
        for lease_id in self.scheduler.stop_requests():
            stop(lease_id)
            self.scheduler.acknowledge_stopped(lease_id)

    @staticmethod
    def _assignment_event(
        contract: SubagentContract,
        authorization: PhaseAuthorization | None,
    ) -> A2AEvent:
        return A2AEvent.from_contract(
            contract,
            event_id=f"{contract.contract_id}:assignment",
            sender_agent_id=contract.parent_agent_id,
            recipient_agent_id=contract.child_agent_id,
            kind=A2AEventKind.ASSIGNMENT,
            sequence=1,
            body=contract.task,
            payload={
                "plan_hash": contract.plan_hash,
                "phase_id": contract.phase_id,
                "input_result_hashes": contract.input_result_hashes,
                "input_evidence_refs": contract.input_evidence_refs,
                "phase_authorization_hash": (
                    authorization.content_hash if authorization is not None else None
                ),
            },
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
