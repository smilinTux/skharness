"""Pi worker adapter for the trusted swarm orchestrator."""

from __future__ import annotations

import math
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from skharness.autocode.sandbox import LaunchSpec

from .runner import PiExperimentRunner, RunOutcome
from .scheduler import Admission, AttemptRequest
from .swarm import BudgetUsage, SubagentContract, SubagentDisposition, SubagentResult
from .swarm_orchestrator import WorkerExecution
from .trajectory import CardSize


@dataclass(frozen=True)
class PiSwarmLaunch:
    request: AttemptRequest
    spec: LaunchSpec
    card_size: CardSize
    requested_model: str
    timeout_s: float


RunnerFactory = Callable[[SubagentContract], PiExperimentRunner]
LaunchFactory = Callable[[SubagentContract], PiSwarmLaunch]
CommitObserver = Callable[[SubagentContract], str]


class PiSwarmWorkerRuntime:
    """Translate exact child contracts into Pi runs and structured child results."""

    def __init__(
        self,
        *,
        runner_factory: RunnerFactory,
        launch_factory: LaunchFactory,
        observe_commit: CommitObserver,
    ) -> None:
        self.runner_factory = runner_factory
        self.launch_factory = launch_factory
        self.observe_commit = observe_commit
        self._active: dict[str, PiExperimentRunner] = {}
        self._lock = threading.RLock()

    def execute(self, contract: SubagentContract) -> WorkerExecution:
        runner = self.runner_factory(contract)
        launch = self.launch_factory(contract)
        launch = replace(
            launch,
            spec=replace(
                launch.spec,
                scoped_readable_paths=list(contract.readable_paths),
                scoped_writable_paths=list(contract.writable_paths),
            ),
        )
        started = datetime.now(timezone.utc)
        with self._lock:
            self._active[contract.lease_id] = runner
        try:
            raw = runner.execute(
                launch.request,
                launch.spec,
                timeout_s=min(launch.timeout_s, contract.budget.wall_seconds),
                card_size=launch.card_size,
                requested_model=launch.requested_model,
            )
        finally:
            with self._lock:
                self._active.pop(contract.lease_id, None)
        finished = datetime.now(timezone.utc)
        if isinstance(raw, Admission):
            disposition = SubagentDisposition.FAILED
            reason_codes = (f"admission:{raw.reason.value if raw.reason else 'unknown'}",)
            evidence_refs: tuple[str, ...] = ()
            summary = "Pi worker was not admitted"
            usage = BudgetUsage()
        else:
            disposition = self._disposition(raw)
            reason_codes = () if disposition is SubagentDisposition.COMPLETED else (
                raw.classification,
            )
            evidence_refs = tuple(dict.fromkeys((raw.stdout_digest, raw.stderr_digest)))
            summary = f"Pi worker terminal classification: {raw.classification}"
            duration = (raw.metrics or {}).get("duration_s", 0)
            usage = BudgetUsage(
                wall_seconds=min(
                    contract.budget.wall_seconds,
                    max(0, math.ceil(duration)) if isinstance(duration, (int, float)) else 0,
                )
            )
        result = SubagentResult.from_contract(
            contract,
            disposition=disposition,
            summary=summary,
            reason_codes=reason_codes,
            evidence_refs=evidence_refs,
            observed_commit=(
                self.observe_commit(contract)
                if disposition is SubagentDisposition.COMPLETED
                and contract.role.value == "builder"
                else None
            ),
            started_at=started,
            finished_at=finished,
        )
        return WorkerExecution(result=result, usage=usage)

    def stop(self, lease_id: str) -> None:
        with self._lock:
            runner = self._active.get(lease_id)
        if runner is not None:
            runner.supervisor.cancel()

    @staticmethod
    def _disposition(outcome: RunOutcome) -> SubagentDisposition:
        if outcome.disposition == "blocked":
            return SubagentDisposition.BLOCKED
        if outcome.disposition == "needs_input":
            return SubagentDisposition.NEEDS_INPUT
        if outcome.successful:
            return SubagentDisposition.COMPLETED
        return SubagentDisposition.FAILED
