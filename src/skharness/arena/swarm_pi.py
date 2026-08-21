"""Pi worker adapter for the trusted swarm orchestrator."""

from __future__ import annotations

import json
import math
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from skharness.autocode.sandbox import LaunchSpec

from .runner import PiExperimentRunner, RunOutcome
from .scheduler import Admission, AttemptRequest
from .swarm import (
    BudgetUsage,
    ScoutAssessment,
    ScoutFinding,
    SubagentContract,
    SubagentDisposition,
    SubagentResult,
    SwarmRole,
)
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

SCOUT_TERMINAL_CONTRACT = """Scout terminal contract (exact headings required):
SCOUT_ASSESSMENT: ACTIONABLE|NO_ACTION|BLOCKED|NEEDS_INPUT
For ACTIONABLE or NO_ACTION, include at least one concrete path-scoped line:
SCOUT_FINDING: relative/repository/path.py:LINE - specific non-placeholder finding
An ordinary zero exit or prose claim is not an actionable assessment."""


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
                argv=self._bounded_argv(contract, launch.spec),
                scoped_readable_paths=list(contract.readable_paths),
                scoped_writable_paths=list(contract.writable_paths),
                inspection_scope=(
                    replace(
                        launch.spec.inspection_scope,
                        max_calls=min(
                            launch.spec.inspection_scope.max_calls,
                            contract.budget.tool_call_limit,
                        ),
                    )
                    if launch.spec.inspection_scope is not None
                    else None
                ),
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
            scout_findings: tuple[ScoutFinding, ...] = ()
            summary = "Pi worker was not admitted"
            usage = BudgetUsage()
        else:
            disposition = self._disposition(raw)
            reason_codes = () if disposition is SubagentDisposition.COMPLETED else (
                raw.classification,
            )
            scout_findings = tuple(
                ScoutFinding.model_validate(item) for item in raw.scout_findings
            )
            evidence_refs = tuple(
                dict.fromkeys(
                    (
                        raw.stdout_digest,
                        raw.stderr_digest,
                        *(item.digest for item in scout_findings),
                    )
                )
            )
            summary = f"Pi worker terminal classification: {raw.classification}"
            duration = (raw.metrics or {}).get("duration_s", 0)
            tokens = (raw.metrics or {}).get("tokens", 0)
            tool_calls = (raw.metrics or {}).get("tool_calls", 0)
            cost = (raw.metrics or {}).get("cost", 0.0)
            usage = BudgetUsage(
                wall_seconds=min(
                    contract.budget.wall_seconds,
                    max(0, math.ceil(duration)) if isinstance(duration, (int, float)) else 0,
                ),
                tokens=tokens if isinstance(tokens, int) and tokens >= 0 else 0,
                tool_calls=(
                    tool_calls if isinstance(tool_calls, int) and tool_calls >= 0 else 0
                ),
                cost=float(cost) if isinstance(cost, (int, float)) and cost >= 0 else 0.0,
            )
        scout_assessment = None
        if contract.role is SwarmRole.SCOUT:
            normalized = self._trusted_scout_assessment(raw, disposition)
            if normalized is None:
                disposition = SubagentDisposition.BLOCKED
                reason_codes = tuple(
                    dict.fromkeys((*reason_codes, "scout_assessment_missing_or_invalid"))
                )
                summary = "Scout lacked a valid path-scoped terminal assessment"
                scout_assessment = ScoutAssessment.BLOCKED
            else:
                scout_assessment, disposition = normalized
                if disposition is not SubagentDisposition.COMPLETED and not reason_codes:
                    reason_codes = (f"scout:{scout_assessment.value}",)
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
            scout_assessment=scout_assessment,
            scout_findings=scout_findings if contract.role is SwarmRole.SCOUT else (),
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

    @staticmethod
    def _trusted_scout_assessment(
        outcome: RunOutcome | Admission,
        disposition: SubagentDisposition,
    ) -> tuple[ScoutAssessment, SubagentDisposition] | None:
        if isinstance(outcome, Admission):
            return None
        try:
            assessment = ScoutAssessment(outcome.scout_assessment)
        except (TypeError, ValueError):
            # Exact negative STATUS headings already ratchet trust downward, but
            # never infer ACTIONABLE/NO_ACTION from process success.
            if disposition is SubagentDisposition.BLOCKED:
                return ScoutAssessment.BLOCKED, SubagentDisposition.BLOCKED
            if disposition is SubagentDisposition.NEEDS_INPUT:
                return ScoutAssessment.NEEDS_INPUT, SubagentDisposition.NEEDS_INPUT
            return None
        normalized = {
            ScoutAssessment.ACTIONABLE: SubagentDisposition.COMPLETED,
            ScoutAssessment.NO_ACTION: SubagentDisposition.COMPLETED,
            ScoutAssessment.BLOCKED: SubagentDisposition.BLOCKED,
            ScoutAssessment.NEEDS_INPUT: SubagentDisposition.NEEDS_INPUT,
        }
        expected = normalized[assessment]
        if expected is SubagentDisposition.COMPLETED and disposition is not expected:
            return None
        return assessment, expected

    @staticmethod
    def _bounded_argv(contract: SubagentContract, spec: LaunchSpec) -> list[str]:
        argv = list(spec.argv)
        try:
            prompt_index = argv.index("-p") + 1
        except (ValueError, IndexError) as exc:
            if contract.role is SwarmRole.SCOUT or contract.phase_inputs:
                raise ValueError("Pi swarm launch lacks an explicit prompt argument") from exc
            return argv
        if contract.role is SwarmRole.SCOUT:
            argv[prompt_index] = f"{argv[prompt_index]}\n\n{SCOUT_TERMINAL_CONTRACT}"
        predecessor_findings = tuple(
            finding
            for phase_input in contract.phase_inputs
            for finding in phase_input.scout_findings
        )
        if predecessor_findings:
            evidence = [item.model_dump(mode="json") for item in predecessor_findings]
            argv[prompt_index] += (
                "\n\nTrusted predecessor scout evidence (typed JSON; do not reinterpret hashes):\n"
                + json.dumps(evidence, sort_keys=True, separators=(",", ":"))
            )
        return argv
