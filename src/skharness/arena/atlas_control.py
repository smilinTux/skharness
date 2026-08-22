"""Atlas command consumer owned by the trusted swarm controller.

The bridge intentionally implements only cancellation today.  It never writes
the scheduler checkpoint directly and cannot expand a child contract.  Message,
pause, resume, and retry remain explicit unsupported outcomes until Pi exposes a
safe controller-owned turn boundary for them.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable

from skharness.activity import ActivityContext, ActivityJournal, ActivityKind
from skharness.control import (
    ControlAction,
    ControlCommand,
    ControlJournal,
    ControlStatus,
    ControlTargetKind,
)

from .swarm import SubagentContract
from .swarm_control import SwarmScheduler

WorkerStopper = Callable[[str], bool]


class SwarmAtlasControlOwner:
    """Poll and apply Atlas commands under scheduler and runtime authority."""

    def __init__(
        self,
        *,
        scheduler: SwarmScheduler,
        contracts: Iterable[SubagentContract],
        stop_worker: WorkerStopper,
        control_journal: ControlJournal,
        activity_journal: ActivityJournal | None = None,
        controller_id: str = "swarm-controller",
        poll_interval_s: float = 0.1,
    ) -> None:
        contracts = tuple(contracts)
        if not contracts:
            raise ValueError("swarm Atlas control owner requires planned contracts")
        if poll_interval_s <= 0:
            raise ValueError("Atlas control poll interval must be positive")
        self.scheduler = scheduler
        self.stop_worker = stop_worker
        self.control_journal = control_journal
        self.activity_journal = activity_journal
        self.controller_id = controller_id
        self.poll_interval_s = float(poll_interval_s)
        self.run_id = scheduler.identity.trajectory_id
        self._contracts = {contract.child_agent_id: contract for contract in contracts}
        self._agent_leases = {
            contract.child_agent_id: contract.lease_id for contract in contracts
        }
        if len(self._agent_leases) != len(contracts):
            raise ValueError("swarm Atlas targets require unique child agent IDs")
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("swarm Atlas control owner already started")
        self._thread = threading.Thread(
            target=self._serve,
            name=f"atlas-swarm-control-{self.run_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout_s: float = 2.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout_s)
            if self._thread.is_alive():
                raise RuntimeError("swarm Atlas control owner did not stop")
        self.raise_if_failed()

    def raise_if_failed(self) -> None:
        if self._error is not None:
            raise RuntimeError("swarm Atlas control owner failed") from self._error

    def _serve(self) -> None:
        try:
            while not self._stop_event.is_set():
                self.process_once()
                self._stop_event.wait(self.poll_interval_s)
            self.process_once()
        except BaseException as exc:  # noqa: BLE001 - surfaced by stop/qualification
            self._error = exc

    def process_once(self) -> int:
        commands = (
            *self.control_journal.pending(target_kind=ControlTargetKind.RUN),
            *self.control_journal.pending(target_kind=ControlTargetKind.AGENT),
        )
        owned = [command for command in commands if self._owns(command)]
        for command in owned:
            receipt, claimed = self.control_journal.claim(
                command.command_id, controller=self.controller_id
            )
            if not claimed:
                continue
            try:
                status, detail = self._apply(command)
            except Exception as exc:  # noqa: BLE001 - command receives a terminal receipt
                status, detail = ControlStatus.REJECTED, type(exc).__name__
            cursor = self._publish(command, status, detail)
            self.control_journal.record(
                command.command_id,
                status,
                controller=self.controller_id,
                detail=detail,
                activity_cursor=cursor,
            )
        return len(owned)

    def _owns(self, command: ControlCommand) -> bool:
        if command.target_kind is ControlTargetKind.RUN:
            return command.target_id == self.run_id
        if command.target_kind is ControlTargetKind.AGENT:
            return command.target_id in self._agent_leases
        return False

    def _state(self, command: ControlCommand) -> str:
        if command.target_kind is ControlTargetKind.AGENT:
            lease = self.scheduler.lease(self._agent_leases[command.target_id])
            return lease.state.value if lease is not None else "pending"
        snapshot = self.scheduler.snapshot()
        if snapshot["cancelled"]:
            return "cancelled"
        return "running" if snapshot["active_workers"] else "pending"

    def _apply(self, command: ControlCommand) -> tuple[ControlStatus, str]:
        observed_state = self._state(command)
        if command.expected_state and command.expected_state != observed_state:
            return (
                ControlStatus.CONFLICT,
                f"expected state {command.expected_state}; observed {observed_state}",
            )
        if command.action is not ControlAction.CANCEL:
            return (
                ControlStatus.UNSUPPORTED,
                "swarm owner currently supports cancel only; no safe Pi turn boundary exists",
            )
        reason = f"atlas_command:{command.command_id}"
        if command.target_kind is ControlTargetKind.AGENT:
            lease_ids = self.scheduler.cancel_worker(
                self._agent_leases[command.target_id], reason=reason
            )
            if not lease_ids:
                return ControlStatus.REJECTED, "agent lease is not active"
        else:
            lease_ids = self.scheduler.cancel_team(reason=reason)

        failed = []
        for lease_id in lease_ids:
            if self.stop_worker(lease_id):
                self.scheduler.acknowledge_stopped(lease_id)
            else:
                failed.append(lease_id)
        if failed:
            return (
                ControlStatus.REJECTED,
                "worker quiescence was not proven for " + ",".join(sorted(failed)),
            )
        return ControlStatus.APPLIED, f"cancelled {len(lease_ids)} active worker(s)"

    def _publish(
        self, command: ControlCommand, status: ControlStatus, detail: str
    ) -> int | None:
        if self.activity_journal is None:
            return None
        try:
            context = ActivityContext(
                session_id=self.run_id,
                run_id=self.run_id,
                agent_id=(
                    command.target_id
                    if command.target_kind is ControlTargetKind.AGENT
                    else ""
                ),
                source="atlas-control",
                card_id=self.scheduler.identity.card_id,
                card_hash=self.scheduler.identity.card_hash,
                trajectory_id=self.run_id,
                team_id=self.scheduler.budget.team_id,
                parent_agent_id=self.scheduler.orchestrator_id,
                contract_id=(
                    self._contracts[command.target_id].contract_id
                    if command.target_kind is ControlTargetKind.AGENT
                    else ""
                ),
                contract_hash=(
                    self._contracts[command.target_id].content_hash
                    if command.target_kind is ControlTargetKind.AGENT
                    else ""
                ),
                plan_hash=next(iter(self._contracts.values())).plan_hash,
                lease_id=(
                    self._agent_leases[command.target_id]
                    if command.target_kind is ControlTargetKind.AGENT
                    else ""
                ),
                base_commit=self.scheduler.identity.base_commit,
                evidence_id=self.scheduler.identity.evidence_id,
            )
            event = self.activity_journal.publish(
                context,
                ActivityKind.STATUS
                if status is ControlStatus.APPLIED
                else ActivityKind.DISPOSITION,
                summary=f"Atlas swarm control {status.value}: {command.action.value}",
                data={
                    "command_id": command.command_id,
                    "target_kind": command.target_kind.value,
                    "target_id": command.target_id,
                    "action": command.action.value,
                    "status": status.value,
                    "payload_digest": command.payload_digest,
                    "detail": detail,
                },
            )
            return event.cursor
        except Exception:  # noqa: BLE001 - control receipt remains authoritative
            return None


__all__ = ["SwarmAtlasControlOwner"]
