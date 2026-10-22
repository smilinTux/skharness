"""Immutable contracts and fail-closed budget accounting for Arena swarms.

The controller remains the authority for scheduling, card state, and completion.
These domain objects only describe bounded child work and its evidence. A child's
``completed`` disposition is not a promotion decision; an independent verifier
must still authorize final completion.
"""

from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from enum import Enum
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import Field, model_validator

from .models import FrozenModel, canonical_digest

_COMMIT_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


class SwarmContractError(ValueError):
    """A swarm contract is ambiguous, over-broad, or conflicts with policy."""


class BudgetExceededError(SwarmContractError):
    """A child would exceed its allocation or the aggregate team allocation."""


class SwarmRole(str, Enum):
    ORCHESTRATOR = "orchestrator"
    SCOUT = "scout"
    BUILDER = "builder"
    TESTER = "tester"
    VERIFIER = "verifier"


class SubagentDisposition(str, Enum):
    COMPLETED = "completed"
    BLOCKED = "blocked"
    NEEDS_INPUT = "needs_input"
    FAILED = "failed"


class ScoutAssessment(str, Enum):
    ACTIONABLE = "actionable"
    NO_ACTION = "no_action"
    BLOCKED = "blocked"
    NEEDS_INPUT = "needs_input"


class ScoutFinding(FrozenModel):
    """Bounded path-scoped scout evidence, never an arbitrary prose handoff."""

    path: str
    line: int | None = Field(default=None, ge=1)
    detail: str = Field(min_length=12, max_length=500)
    digest: str

    @model_validator(mode="after")
    def validate_finding(self) -> ScoutFinding:
        _validate_relative_path(self.path)
        if self.detail != self.detail.strip() or "\n" in self.detail or "\r" in self.detail:
            raise ValueError("scout finding detail must be one normalized line")
        lowered = self.detail.casefold()
        if lowered in {"placeholder", "unknown", "none", "n/a", "todo", "tbd"} or any(
            item in lowered for item in ("lorem ipsum", "fill this", "placeholder")
        ):
            raise ValueError("scout finding detail must not be placeholder prose")
        if self.digest != canonical_digest(self.digest_payload):
            raise ValueError("scout finding digest does not match its typed content")
        return self

    @property
    def digest_payload(self) -> dict[str, Any]:
        return {"path": self.path, "line": self.line, "detail": self.detail}

    @classmethod
    def create(cls, *, path: str, line: int | None, detail: str) -> ScoutFinding:
        payload = {"path": path, "line": line, "detail": detail}
        return cls(**payload, digest=canonical_digest(payload))


class A2AEventKind(str, Enum):
    ASSIGNMENT = "assignment"
    PROGRESS = "progress"
    QUESTION = "question"
    RESPONSE = "response"
    CANCELLATION = "cancellation"
    RESULT = "result"


class SwarmIdentity(FrozenModel):
    """Shared immutable attribution copied into every child and A2A event."""

    card_id: str
    card_hash: str
    base_commit: str
    evidence_id: str
    trajectory_id: str

    @model_validator(mode="after")
    def validate_identity(self) -> SwarmIdentity:
        if not self.card_id.strip() or any(char.isspace() for char in self.card_id):
            raise ValueError("card_id must be a non-empty opaque ID without whitespace")
        if not _DIGEST_RE.fullmatch(self.card_hash):
            raise ValueError("card_hash must bind immutable card content with sha256")
        if not _COMMIT_RE.fullmatch(self.base_commit):
            raise ValueError("base_commit must be a full lowercase Git object ID")
        if not _DIGEST_RE.fullmatch(self.evidence_id):
            raise ValueError("evidence_id must be a lowercase sha256 digest")
        if not self.trajectory_id.strip() or any(
            char.isspace() for char in self.trajectory_id
        ):
            raise ValueError("trajectory_id must be a non-empty opaque ID without whitespace")
        return self

    @property
    def content_hash(self) -> str:
        return canonical_digest(self)


class ExecutionBudget(FrozenModel):
    """Hard reservation requested by one child before it starts."""

    wall_seconds: float = Field(gt=0)
    token_limit: int = Field(gt=0)
    tool_call_limit: int = Field(gt=0)
    cost_limit: float = Field(ge=0)


class TeamBudget(FrozenModel):
    """Aggregate ceiling shared by all current and completed child work."""

    team_id: str
    wall_seconds: float = Field(gt=0)
    token_limit: int = Field(gt=0)
    tool_call_limit: int = Field(gt=0)
    cost_limit: float = Field(ge=0)
    max_concurrency: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_team_id(self) -> TeamBudget:
        if not self.team_id.strip():
            raise ValueError("team_id must not be empty")
        return self


class BudgetUsage(FrozenModel):
    wall_seconds: float = Field(default=0, ge=0)
    tokens: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    cost: float = Field(default=0, ge=0)

    def plus(self, other: BudgetUsage) -> BudgetUsage:
        return BudgetUsage(
            wall_seconds=self.wall_seconds + other.wall_seconds,
            tokens=self.tokens + other.tokens,
            tool_calls=self.tool_calls + other.tool_calls,
            cost=self.cost + other.cost,
        )


class SwarmPhaseSpec(FrozenModel):
    """One exact worker wave in an immutable swarm plan DAG."""

    phase_id: str
    role: SwarmRole
    contract_ids: tuple[str, ...]
    predecessor_phase_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_phase(self) -> SwarmPhaseSpec:
        if not self.phase_id.strip():
            raise ValueError("phase_id must not be empty")
        if self.role in {SwarmRole.ORCHESTRATOR, SwarmRole.VERIFIER}:
            raise ValueError("swarm plans contain worker phases, not authority roles")
        _require_unique(self.contract_ids, "contract_ids")
        _require_unique(self.predecessor_phase_ids, "predecessor_phase_ids")
        if not self.contract_ids:
            raise ValueError("every planned phase requires at least one exact contract ID")
        if self.phase_id in self.predecessor_phase_ids:
            raise ValueError("a phase cannot depend on itself")
        if self.role is SwarmRole.SCOUT and self.predecessor_phase_ids:
            raise ValueError("scout phases must be roots")
        return self


class SwarmPlan(FrozenModel):
    """Immutable exact phase DAG authorized before any child is admitted."""

    schema_version: Literal["arena.swarm.plan.v1"] = "arena.swarm.plan.v1"
    plan_id: str
    identity: SwarmIdentity
    phases: tuple[SwarmPhaseSpec, ...]
    created_at: datetime

    @model_validator(mode="after")
    def validate_dag(self) -> SwarmPlan:
        if not self.plan_id.strip() or not self.phases:
            raise ValueError("a swarm plan requires an ID and at least one phase")
        _require_aware(self.created_at, "created_at")
        phase_ids = [phase.phase_id for phase in self.phases]
        if len(phase_ids) != len(set(phase_ids)):
            raise ValueError("planned phase IDs must be unique")
        contract_ids = [item for phase in self.phases for item in phase.contract_ids]
        if len(contract_ids) != len(set(contract_ids)):
            raise ValueError("a contract ID may appear in exactly one planned phase")
        seen: dict[str, SwarmPhaseSpec] = {}
        for phase in self.phases:
            missing = set(phase.predecessor_phase_ids) - set(seen)
            if missing:
                raise ValueError(
                    "predecessors must exist earlier in topological plan order: "
                    + ", ".join(sorted(missing))
                )
            predecessor_roles = {seen[item].role for item in phase.predecessor_phase_ids}
            if phase.role is SwarmRole.BUILDER and predecessor_roles - {SwarmRole.SCOUT}:
                raise ValueError("builder phases may depend only on scout phases")
            if phase.role is SwarmRole.TESTER:
                if not predecessor_roles or predecessor_roles != {SwarmRole.BUILDER}:
                    raise ValueError("tester phases require builder predecessors")
            seen[phase.phase_id] = phase
        return self

    def phase(self, phase_id: str) -> SwarmPhaseSpec:
        matches = [phase for phase in self.phases if phase.phase_id == phase_id]
        if len(matches) != 1:
            raise SwarmContractError(f"phase is not present exactly once in plan: {phase_id}")
        return matches[0]

    @property
    def content_hash(self) -> str:
        return canonical_digest(self)


class PhaseInput(FrozenModel):
    """Immutable evidence handed from one completed phase into the next."""

    source_phase_id: str
    source_contract_id: str
    source_contract_hash: str
    source_role: SwarmRole
    source_receipt_hash: str
    source_result_hash: str
    identity_hash: str
    evidence_refs: tuple[str, ...]
    scout_findings: tuple[ScoutFinding, ...] = ()
    output_commit: str

    @model_validator(mode="after")
    def validate_input(self) -> PhaseInput:
        if not self.source_phase_id.strip() or not self.source_contract_id.strip():
            raise ValueError("phase input source IDs must not be empty")
        for name in (
            "source_contract_hash",
            "source_receipt_hash",
            "source_result_hash",
            "identity_hash",
        ):
            if not _DIGEST_RE.fullmatch(getattr(self, name)):
                raise ValueError(f"{name} must be an immutable sha256 digest")
        _require_unique(self.evidence_refs, "evidence_refs")
        if not self.evidence_refs or any(
            not _DIGEST_RE.fullmatch(item) for item in self.evidence_refs
        ):
            raise ValueError("phase inputs require immutable evidence refs")
        if not _COMMIT_RE.fullmatch(self.output_commit):
            raise ValueError("phase input output_commit must be a full Git object ID")
        finding_digests = [item.digest for item in self.scout_findings]
        if len(finding_digests) != len(set(finding_digests)):
            raise ValueError("phase input scout findings must be unique")
        if self.source_role is not SwarmRole.SCOUT and self.scout_findings:
            raise ValueError("only scout phase inputs may carry scout findings")
        if any(item not in self.evidence_refs for item in finding_digests):
            raise ValueError("scout finding digests must remain in phase input evidence")
        return self

    @classmethod
    def from_receipt(cls, receipt: PhaseReceipt) -> PhaseInput:
        return cls(
            source_phase_id=receipt.phase_id,
            source_contract_id=receipt.contract_id,
            source_contract_hash=receipt.contract_hash,
            source_role=receipt.role,
            source_receipt_hash=receipt.content_hash,
            source_result_hash=receipt.result_hash,
            identity_hash=receipt.identity_hash,
            evidence_refs=receipt.evidence_refs,
            scout_findings=receipt.scout_findings,
            output_commit=receipt.output_commit,
        )


class SubagentContract(FrozenModel):
    """One lease-bound, narrowly scoped parent-to-child assignment."""

    schema_version: Literal["arena.swarm.contract.v2"] = "arena.swarm.contract.v2"
    contract_id: str
    team_id: str
    identity: SwarmIdentity
    plan_hash: str
    phase_id: str
    parent_agent_id: str
    child_agent_id: str
    role: SwarmRole
    task: str
    readable_paths: tuple[str, ...]
    writable_paths: tuple[str, ...] = ()
    protected_paths: tuple[str, ...] = ()
    tool_allowlist: tuple[str, ...]
    budget: ExecutionBudget
    lease_id: str
    worktree_id: str
    issued_at: datetime
    phase_inputs: tuple[PhaseInput, ...] = ()
    inputs_bound_at: datetime | None = None

    @model_validator(mode="after")
    def validate_scope(self) -> SubagentContract:
        required = {
            "contract_id": self.contract_id,
            "team_id": self.team_id,
            "phase_id": self.phase_id,
            "parent_agent_id": self.parent_agent_id,
            "child_agent_id": self.child_agent_id,
            "task": self.task,
            "lease_id": self.lease_id,
            "worktree_id": self.worktree_id,
        }
        if any(not value.strip() for value in required.values()):
            raise ValueError("swarm contract identifiers and task must not be empty")
        if self.parent_agent_id == self.child_agent_id:
            raise ValueError("parent and child agent identities must be distinct")
        if not _DIGEST_RE.fullmatch(self.plan_hash):
            raise ValueError("plan_hash must bind an immutable swarm plan")
        _require_aware(self.issued_at, "issued_at")
        if self.role is SwarmRole.ORCHESTRATOR:
            raise ValueError("an orchestrator cannot be assigned as a child role")
        if not self.readable_paths:
            raise ValueError("a child requires an explicit readable path scope")
        if not self.tool_allowlist:
            raise ValueError("a child requires an explicit tool allowlist")
        _require_unique(self.readable_paths, "readable_paths")
        _require_unique(self.writable_paths, "writable_paths")
        _require_unique(self.protected_paths, "protected_paths")
        _require_unique(self.tool_allowlist, "tool_allowlist")
        for path in self.readable_paths + self.writable_paths + self.protected_paths:
            _validate_relative_path(path)
        if self.role is SwarmRole.BUILDER and not self.writable_paths:
            raise ValueError("builder contracts require declared writable paths")
        if self.role in {SwarmRole.SCOUT, SwarmRole.TESTER, SwarmRole.VERIFIER} and (
            self.writable_paths
        ):
            raise ValueError(f"{self.role.value} contracts must be read-only")
        if any(
            _paths_overlap(writable, protected)
            for writable in self.writable_paths
            for protected in self.protected_paths
        ):
            raise ValueError("writable and protected path scopes must not overlap")
        source_contract_ids = [item.source_contract_id for item in self.phase_inputs]
        source_receipt_hashes = [item.source_receipt_hash for item in self.phase_inputs]
        if len(source_contract_ids) != len(set(source_contract_ids)) or len(
            source_receipt_hashes
        ) != len(set(source_receipt_hashes)):
            raise ValueError("phase inputs must uniquely identify upstream results")
        if self.phase_inputs and self.inputs_bound_at is None:
            raise ValueError("downstream phase inputs require a binding timestamp")
        if not self.phase_inputs and self.inputs_bound_at is not None:
            raise ValueError("root contracts cannot claim an input binding timestamp")
        if self.inputs_bound_at is not None:
            _require_aware(self.inputs_bound_at, "inputs_bound_at")
        if any(item.identity_hash != self.identity.content_hash for item in self.phase_inputs):
            raise ValueError("phase inputs must share the exact swarm identity")
        return self

    @property
    def input_result_hashes(self) -> tuple[str, ...]:
        return tuple(item.source_result_hash for item in self.phase_inputs)

    @property
    def input_evidence_refs(self) -> tuple[str, ...]:
        return tuple(
            sorted({evidence for item in self.phase_inputs for evidence in item.evidence_refs})
        )

    @property
    def content_hash(self) -> str:
        return canonical_digest(self)


class SubagentResult(FrozenModel):
    """Structured child outcome; never sufficient by itself to complete a card."""

    schema_version: Literal["arena.swarm.result.v2"] = "arena.swarm.result.v2"
    contract_id: str
    contract_hash: str
    identity: SwarmIdentity
    plan_hash: str
    phase_id: str
    lease_id: str
    agent_id: str
    role: SwarmRole
    disposition: SubagentDisposition
    summary: str
    reason_codes: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    observed_commit: str | None = None
    scout_assessment: ScoutAssessment | None = None
    scout_findings: tuple[ScoutFinding, ...] = ()
    controller_terminal_reason: str | None = None
    started_at: datetime
    finished_at: datetime

    @model_validator(mode="after")
    def validate_result(self) -> SubagentResult:
        if any(
            not value.strip()
            for value in (
                self.contract_id,
                self.phase_id,
                self.lease_id,
                self.agent_id,
                self.summary,
            )
        ):
            raise ValueError("result contract, agent, and summary must not be empty")
        if not _DIGEST_RE.fullmatch(self.contract_hash):
            raise ValueError("contract_hash must be a lowercase sha256 digest")
        if not _DIGEST_RE.fullmatch(self.plan_hash):
            raise ValueError("plan_hash must bind an immutable swarm plan")
        if self.role is SwarmRole.ORCHESTRATOR:
            raise ValueError("orchestrators do not produce subagent results")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at cannot precede started_at")
        _require_unique(self.reason_codes, "reason_codes")
        _require_unique(self.evidence_refs, "evidence_refs")
        if any(not _DIGEST_RE.fullmatch(item) for item in self.evidence_refs):
            raise ValueError("evidence_refs must contain lowercase sha256 digests")
        if self.disposition is SubagentDisposition.COMPLETED and not self.evidence_refs:
            raise ValueError("completed child results require immutable evidence")
        if self.disposition is not SubagentDisposition.COMPLETED and not self.reason_codes:
            raise ValueError("non-completed child results require reason codes")
        if self.observed_commit is not None and not _COMMIT_RE.fullmatch(self.observed_commit):
            raise ValueError("observed_commit must be a full lowercase Git object ID")
        if self.controller_terminal_reason is not None:
            if (
                not self.controller_terminal_reason.strip()
                or self.disposition is not SubagentDisposition.FAILED
                or self.controller_terminal_reason not in self.reason_codes
            ):
                raise ValueError(
                    "controller terminal reason requires a matching FAILED reason code"
                )
        if (
            self.role is SwarmRole.BUILDER
            and self.disposition is SubagentDisposition.COMPLETED
            and self.observed_commit is None
        ):
            raise ValueError("completed builder results require an observed commit")
        if self.role is SwarmRole.SCOUT:
            if self.scout_assessment is None:
                raise ValueError("scout results require a typed assessment")
            assessment_dispositions = {
                ScoutAssessment.ACTIONABLE: {SubagentDisposition.COMPLETED},
                ScoutAssessment.NO_ACTION: {SubagentDisposition.COMPLETED},
                ScoutAssessment.BLOCKED: {SubagentDisposition.BLOCKED},
                ScoutAssessment.NEEDS_INPUT: {SubagentDisposition.NEEDS_INPUT},
            }
            budget_failure = (
                self.scout_assessment is ScoutAssessment.BLOCKED
                and self.disposition is SubagentDisposition.FAILED
                and any(item == "budget_exceeded" for item in self.reason_codes)
            )
            controller_failure = (
                self.scout_assessment is ScoutAssessment.BLOCKED
                and self.disposition is SubagentDisposition.FAILED
                and self.controller_terminal_reason is not None
            )
            if (
                self.disposition not in assessment_dispositions[self.scout_assessment]
                and not budget_failure
                and not controller_failure
            ):
                raise ValueError("scout assessment conflicts with its structured disposition")
            finding_digests = [item.digest for item in self.scout_findings]
            if len(finding_digests) != len(set(finding_digests)):
                raise ValueError("scout findings must be unique")
            if any(item not in self.evidence_refs for item in finding_digests):
                raise ValueError("scout finding digests must be retained as result evidence")
            if self.scout_assessment in {
                ScoutAssessment.ACTIONABLE,
                ScoutAssessment.NO_ACTION,
            } and not self.scout_findings:
                raise ValueError("positive scout assessments require typed path findings")
        elif self.scout_assessment is not None or self.scout_findings:
            raise ValueError("only scout results may carry scout assessment evidence")
        return self

    @classmethod
    def from_contract(
        cls,
        contract: SubagentContract,
        **fields: Any,
    ) -> SubagentResult:
        """Bind a result to the exact contract without caller-supplied attribution."""

        return cls(
            contract_id=contract.contract_id,
            contract_hash=contract.content_hash,
            identity=contract.identity,
            plan_hash=contract.plan_hash,
            phase_id=contract.phase_id,
            lease_id=contract.lease_id,
            agent_id=contract.child_agent_id,
            role=contract.role,
            **fields,
        )

    @property
    def content_hash(self) -> str:
        return canonical_digest(self)


class PhaseReceipt(FrozenModel):
    """Controller receipt for one exact lease/result and its predecessor chain."""

    schema_version: Literal["arena.swarm.phase-receipt.v1"] = (
        "arena.swarm.phase-receipt.v1"
    )
    receipt_id: str
    plan_hash: str
    identity_hash: str
    phase_id: str
    contract_id: str
    contract_hash: str
    lease_id: str
    role: SwarmRole
    result_hash: str
    disposition: SubagentDisposition
    scout_assessment: ScoutAssessment | None = None
    predecessor_receipt_hashes: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...]
    scout_findings: tuple[ScoutFinding, ...] = ()
    input_commit: str
    output_commit: str
    result_finished_at: datetime
    recorded_at: datetime

    @model_validator(mode="after")
    def validate_receipt(self) -> PhaseReceipt:
        if any(
            not value.strip()
            for value in (self.receipt_id, self.phase_id, self.contract_id, self.lease_id)
        ):
            raise ValueError("phase receipt IDs must not be empty")
        for name in ("plan_hash", "identity_hash", "contract_hash", "result_hash"):
            if not _DIGEST_RE.fullmatch(getattr(self, name)):
                raise ValueError(f"{name} must be an immutable sha256 digest")
        _require_unique(self.predecessor_receipt_hashes, "predecessor_receipt_hashes")
        _require_unique(self.evidence_refs, "evidence_refs")
        if any(not _DIGEST_RE.fullmatch(item) for item in self.predecessor_receipt_hashes):
            raise ValueError("predecessor receipts must be immutable sha256 digests")
        if not self.evidence_refs or any(
            not _DIGEST_RE.fullmatch(item) for item in self.evidence_refs
        ):
            raise ValueError("phase receipts require immutable result evidence")
        if not _COMMIT_RE.fullmatch(self.input_commit) or not _COMMIT_RE.fullmatch(
            self.output_commit
        ):
            raise ValueError("phase receipt commits must be full Git object IDs")
        finding_digests = [item.digest for item in self.scout_findings]
        if len(finding_digests) != len(set(finding_digests)):
            raise ValueError("phase receipt scout findings must be unique")
        if self.role is not SwarmRole.SCOUT and self.scout_findings:
            raise ValueError("only scout receipts may carry scout findings")
        if any(item not in self.evidence_refs for item in finding_digests):
            raise ValueError("scout finding digests must remain receipt evidence")
        _require_aware(self.result_finished_at, "result_finished_at")
        _require_aware(self.recorded_at, "recorded_at")
        if self.recorded_at < self.result_finished_at:
            raise ValueError("a phase receipt cannot predate its result")
        return self

    @classmethod
    def from_result(
        cls,
        contract: SubagentContract,
        result: SubagentResult,
        *,
        predecessors: tuple[PhaseReceipt, ...] = (),
        recorded_at: datetime | None = None,
    ) -> PhaseReceipt:
        _require_result_contract_binding(contract, result)
        expected_inputs = tuple(
            sorted(
                (PhaseInput.from_receipt(item) for item in predecessors),
                key=lambda item: item.source_contract_id,
            )
        )
        if contract.phase_inputs != expected_inputs:
            raise SwarmContractError("phase receipt predecessors do not match contract inputs")
        input_commits = {item.output_commit for item in predecessors}
        if len(input_commits) > 1:
            raise SwarmContractError("a phase cannot consume conflicting predecessor commits")
        input_commit = next(iter(input_commits), contract.identity.base_commit)
        output_commit = result.observed_commit or input_commit
        now = recorded_at or datetime.now(timezone.utc)
        return cls(
            receipt_id=f"{contract.contract_id}:{result.content_hash}",
            plan_hash=contract.plan_hash,
            identity_hash=contract.identity.content_hash,
            phase_id=contract.phase_id,
            contract_id=contract.contract_id,
            contract_hash=contract.content_hash,
            lease_id=contract.lease_id,
            role=contract.role,
            result_hash=result.content_hash,
            disposition=result.disposition,
            scout_assessment=result.scout_assessment,
            predecessor_receipt_hashes=tuple(item.content_hash for item in predecessors),
            evidence_refs=result.evidence_refs,
            scout_findings=result.scout_findings,
            input_commit=input_commit,
            output_commit=output_commit,
            result_finished_at=result.finished_at,
            recorded_at=now,
        )

    @property
    def content_hash(self) -> str:
        return canonical_digest(self)


class PhaseAuthorization(FrozenModel):
    """Controller authorization required before admitting any non-root contract."""

    schema_version: Literal["arena.swarm.phase-authorization.v1"] = (
        "arena.swarm.phase-authorization.v1"
    )
    authorization_id: str
    plan_hash: str
    identity_hash: str
    phase_id: str
    contract_id: str
    contract_hash: str
    lease_id: str
    predecessor_receipt_hashes: tuple[str, ...]
    input_result_hashes: tuple[str, ...]
    input_evidence_refs: tuple[str, ...]
    input_commit: str
    authorized_by: str
    authorized_at: datetime

    @model_validator(mode="after")
    def validate_authorization(self) -> PhaseAuthorization:
        if any(
            not value.strip()
            for value in (
                self.authorization_id,
                self.phase_id,
                self.contract_id,
                self.lease_id,
                self.authorized_by,
            )
        ):
            raise ValueError("phase authorization IDs must not be empty")
        for name in ("plan_hash", "identity_hash", "contract_hash"):
            if not _DIGEST_RE.fullmatch(getattr(self, name)):
                raise ValueError(f"{name} must be an immutable sha256 digest")
        for name, values in (
            ("predecessor_receipt_hashes", self.predecessor_receipt_hashes),
            ("input_result_hashes", self.input_result_hashes),
            ("input_evidence_refs", self.input_evidence_refs),
        ):
            _require_unique(values, name)
            if not values or any(not _DIGEST_RE.fullmatch(item) for item in values):
                raise ValueError(f"{name} require immutable sha256 digests")
        if not _COMMIT_RE.fullmatch(self.input_commit):
            raise ValueError("authorization input_commit must be a full Git object ID")
        _require_aware(self.authorized_at, "authorized_at")
        return self

    @classmethod
    def issue(
        cls,
        contract: SubagentContract,
        predecessors: tuple[PhaseReceipt, ...],
        *,
        authorized_by: str,
        authorized_at: datetime | None = None,
    ) -> PhaseAuthorization:
        if not contract.phase_inputs or not predecessors:
            raise SwarmContractError("only non-root contracts receive phase authorization")
        expected_inputs = tuple(
            sorted(
                (PhaseInput.from_receipt(item) for item in predecessors),
                key=lambda item: item.source_contract_id,
            )
        )
        if contract.phase_inputs != expected_inputs:
            raise SwarmContractError("authorization predecessors do not match contract inputs")
        input_commits = {item.output_commit for item in predecessors}
        if len(input_commits) != 1:
            raise SwarmContractError("authorization requires one exact predecessor commit")
        now = authorized_at or datetime.now(timezone.utc)
        if contract.inputs_bound_at is None or now < contract.inputs_bound_at:
            raise SwarmContractError("authorization cannot predate the contract input binding")
        return cls(
            authorization_id=f"{contract.contract_id}:authorize:{contract.content_hash}",
            plan_hash=contract.plan_hash,
            identity_hash=contract.identity.content_hash,
            phase_id=contract.phase_id,
            contract_id=contract.contract_id,
            contract_hash=contract.content_hash,
            lease_id=contract.lease_id,
            predecessor_receipt_hashes=tuple(item.content_hash for item in predecessors),
            input_result_hashes=contract.input_result_hashes,
            input_evidence_refs=contract.input_evidence_refs,
            input_commit=next(iter(input_commits)),
            authorized_by=authorized_by,
            authorized_at=now,
        )

    def require_contract(self, contract: SubagentContract, *, orchestrator_id: str) -> None:
        expected = {
            "plan_hash": contract.plan_hash,
            "identity_hash": contract.identity.content_hash,
            "phase_id": contract.phase_id,
            "contract_id": contract.contract_id,
            "contract_hash": contract.content_hash,
            "lease_id": contract.lease_id,
            "input_result_hashes": contract.input_result_hashes,
            "input_evidence_refs": contract.input_evidence_refs,
        }
        mismatches = [name for name, value in expected.items() if getattr(self, name) != value]
        if self.authorized_by != orchestrator_id:
            mismatches.append("authorized_by")
        if mismatches:
            raise SwarmContractError(
                "phase authorization is not bound to the exact contract: "
                + ", ".join(mismatches)
            )

    @property
    def content_hash(self) -> str:
        return canonical_digest(self)


def bind_phase_inputs(
    contract: SubagentContract,
    predecessors: tuple[PhaseReceipt, ...],
    *,
    plan: SwarmPlan,
    bound_at: datetime | None = None,
) -> SubagentContract:
    """Create or verify a downstream contract against exact completed receipts."""

    phase = require_contract_plan(contract, plan)
    if not phase.predecessor_phase_ids:
        raise SwarmContractError("root contracts must not be bound to phase inputs")
    expected_contract_ids = {
        contract_id
        for phase_id in phase.predecessor_phase_ids
        for contract_id in plan.phase(phase_id).contract_ids
    }
    actual_contract_ids = {item.contract_id for item in predecessors}
    if actual_contract_ids != expected_contract_ids or len(predecessors) != len(
        expected_contract_ids
    ):
        raise SwarmContractError("predecessor receipts do not satisfy the exact plan cardinality")
    for receipt in predecessors:
        if receipt.plan_hash != plan.content_hash:
            raise SwarmContractError("predecessor receipt belongs to another plan")
        if receipt.identity_hash != contract.identity.content_hash:
            raise SwarmContractError("predecessor receipt belongs to another swarm identity")
        if receipt.phase_id not in phase.predecessor_phase_ids:
            raise SwarmContractError("predecessor receipt comes from an unplanned phase")
        if receipt.disposition is not SubagentDisposition.COMPLETED:
            raise SwarmContractError("downstream work requires completed predecessor receipts")
        if receipt.role is SwarmRole.SCOUT and (
            receipt.scout_assessment is not ScoutAssessment.ACTIONABLE
        ):
            raise SwarmContractError("downstream work requires an actionable scout assessment")
    inputs = tuple(
        sorted(
            (PhaseInput.from_receipt(item) for item in predecessors),
            key=lambda item: item.source_contract_id,
        )
    )
    if contract.phase_inputs:
        if contract.phase_inputs != inputs:
            raise SwarmContractError("pre-bound phase inputs do not match observed results")
        return contract
    now = bound_at or datetime.now(timezone.utc)
    if any(now < item.result_finished_at for item in predecessors):
        raise SwarmContractError("phase inputs cannot be bound before predecessor results exist")
    document = contract.model_dump(mode="python") | {
        "phase_inputs": inputs,
        "inputs_bound_at": now,
    }
    return SubagentContract.model_validate(document)


def require_contract_plan(contract: SubagentContract, plan: SwarmPlan) -> SwarmPhaseSpec:
    """Fail closed unless a contract is one exact member of its immutable plan."""

    if contract.identity != plan.identity or contract.plan_hash != plan.content_hash:
        raise SwarmContractError("contract identity or plan hash does not match swarm plan")
    phase = plan.phase(contract.phase_id)
    if contract.contract_id not in phase.contract_ids or contract.role is not phase.role:
        raise SwarmContractError("contract is not an exact member of its planned phase")
    return phase


def _require_result_contract_binding(
    contract: SubagentContract,
    result: SubagentResult,
) -> None:
    expected = (
        contract.contract_id,
        contract.content_hash,
        contract.identity,
        contract.plan_hash,
        contract.phase_id,
        contract.lease_id,
        contract.child_agent_id,
        contract.role,
    )
    actual = (
        result.contract_id,
        result.contract_hash,
        result.identity,
        result.plan_hash,
        result.phase_id,
        result.lease_id,
        result.agent_id,
        result.role,
    )
    if actual != expected:
        raise SwarmContractError("result is not bound to the exact phase contract")


class A2AEvent(FrozenModel):
    """Trajectory-bound event that can only travel over one parent/child edge."""

    schema_version: Literal["arena.swarm.a2a.v1"] = "arena.swarm.a2a.v1"
    event_id: str
    contract_id: str
    contract_hash: str
    identity: SwarmIdentity
    parent_agent_id: str
    child_agent_id: str
    sender_agent_id: str
    recipient_agent_id: str
    kind: A2AEventKind
    sequence: int = Field(ge=1)
    body: str
    created_at: datetime
    prior_event_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_edge(self) -> A2AEvent:
        if any(
            not value.strip()
            for value in (
                self.event_id,
                self.contract_id,
                self.parent_agent_id,
                self.child_agent_id,
                self.sender_agent_id,
                self.recipient_agent_id,
                self.body,
            )
        ):
            raise ValueError("A2A event identifiers and body must not be empty")
        if not _DIGEST_RE.fullmatch(self.contract_hash):
            raise ValueError("contract_hash must be a lowercase sha256 digest")
        if self.parent_agent_id == self.child_agent_id:
            raise ValueError("A2A parent and child must be distinct")
        if {self.sender_agent_id, self.recipient_agent_id} != {
            self.parent_agent_id,
            self.child_agent_id,
        }:
            raise ValueError("A2A messages are restricted to the contract parent/child edge")
        parent_to_child = {
            A2AEventKind.ASSIGNMENT,
            A2AEventKind.RESPONSE,
            A2AEventKind.CANCELLATION,
        }
        child_to_parent = {
            A2AEventKind.PROGRESS,
            A2AEventKind.QUESTION,
            A2AEventKind.RESULT,
        }
        if self.kind in parent_to_child and self.sender_agent_id != self.parent_agent_id:
            raise ValueError(f"{self.kind.value} events must be sent by the parent")
        if self.kind in child_to_parent and self.sender_agent_id != self.child_agent_id:
            raise ValueError(f"{self.kind.value} events must be sent by the child")
        return self

    @classmethod
    def from_contract(cls, contract: SubagentContract, **fields: Any) -> A2AEvent:
        return cls(
            contract_id=contract.contract_id,
            contract_hash=contract.content_hash,
            identity=contract.identity,
            parent_agent_id=contract.parent_agent_id,
            child_agent_id=contract.child_agent_id,
            **fields,
        )

    @property
    def content_hash(self) -> str:
        return canonical_digest(self)


class TeamBudgetSnapshot(FrozenModel):
    team_id: str
    consumed: BudgetUsage
    reserved: BudgetUsage
    remaining: BudgetUsage
    active_contract_ids: tuple[str, ...]


class TeamBudgetLedger:
    """Thread-safe reservation ledger that prevents aggregate budget overbooking."""

    def __init__(self, budget: TeamBudget) -> None:
        self.budget = budget
        self._consumed = BudgetUsage()
        self._active: dict[str, SubagentContract] = {}
        self._settled: dict[str, BudgetUsage] = {}
        self._lock = threading.RLock()

    def reserve(self, contract: SubagentContract) -> None:
        with self._lock:
            if contract.team_id != self.budget.team_id:
                raise SwarmContractError("contract belongs to a different team budget")
            if contract.contract_id in self._active or contract.contract_id in self._settled:
                raise SwarmContractError("contract budget has already been reserved or settled")
            if len(self._active) >= self.budget.max_concurrency:
                raise BudgetExceededError("team concurrency budget exhausted")
            self._require_exclusive_write_scope(contract)
            requested = self._reservation_usage(contract)
            projected = self._consumed.plus(self._reserved()).plus(requested)
            self._require_within_team(projected)
            self._active[contract.contract_id] = contract

    def settle(self, contract_id: str, usage: BudgetUsage) -> None:
        with self._lock:
            try:
                contract = self._active[contract_id]
            except KeyError as exc:
                raise SwarmContractError("only an active reservation may be settled") from exc
            self._require_within_contract(usage, contract.budget)
            projected = self._consumed.plus(usage)
            self._require_within_team(projected)
            del self._active[contract_id]
            self._settled[contract_id] = usage
            self._consumed = projected

    def snapshot(self) -> TeamBudgetSnapshot:
        with self._lock:
            reserved = self._reserved()
            allocated = self._consumed.plus(reserved)
            remaining = BudgetUsage(
                wall_seconds=self.budget.wall_seconds - allocated.wall_seconds,
                tokens=self.budget.token_limit - allocated.tokens,
                tool_calls=self.budget.tool_call_limit - allocated.tool_calls,
                cost=self.budget.cost_limit - allocated.cost,
            )
            return TeamBudgetSnapshot(
                team_id=self.budget.team_id,
                consumed=self._consumed,
                reserved=reserved,
                remaining=remaining,
                active_contract_ids=tuple(sorted(self._active)),
            )

    def _reserved(self) -> BudgetUsage:
        reserved = BudgetUsage()
        for contract in self._active.values():
            reserved = reserved.plus(self._reservation_usage(contract))
        return reserved

    def _require_exclusive_write_scope(self, candidate: SubagentContract) -> None:
        if not candidate.writable_paths:
            return
        for active in self._active.values():
            if active.worktree_id != candidate.worktree_id:
                continue
            if any(
                _paths_overlap(left, right)
                for left in active.writable_paths
                for right in candidate.writable_paths
            ):
                raise SwarmContractError(
                    "active children cannot share an overlapping writable scope in one worktree"
                )

    def _require_within_contract(
        self,
        usage: BudgetUsage,
        limit: ExecutionBudget,
    ) -> None:
        if (
            usage.wall_seconds > limit.wall_seconds
            or usage.tokens > limit.token_limit
            or usage.tool_calls > limit.tool_call_limit
            or usage.cost > limit.cost_limit
        ):
            raise BudgetExceededError("reported usage exceeds the child reservation")

    def _require_within_team(self, usage: BudgetUsage) -> None:
        if (
            usage.wall_seconds > self.budget.wall_seconds
            or usage.tokens > self.budget.token_limit
            or usage.tool_calls > self.budget.tool_call_limit
            or usage.cost > self.budget.cost_limit
        ):
            raise BudgetExceededError("aggregate child work exceeds the team budget")

    @staticmethod
    def _reservation_usage(contract: SubagentContract) -> BudgetUsage:
        return BudgetUsage(
            wall_seconds=contract.budget.wall_seconds,
            tokens=contract.budget.token_limit,
            tool_calls=contract.budget.tool_call_limit,
            cost=contract.budget.cost_limit,
        )


def _require_unique(items: tuple[str, ...], field: str) -> None:
    if any(not item.strip() for item in items):
        raise ValueError(f"{field} entries must not be empty")
    if len(items) != len(set(items)):
        raise ValueError(f"{field} entries must be unique")


def _require_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


def _validate_relative_path(value: str) -> None:
    path = PurePosixPath(value)
    if path.is_absolute() or value != path.as_posix() or ".." in path.parts or value in {"", "."}:
        raise ValueError("path scopes must be normalized repository-relative paths")


def _paths_overlap(left: str, right: str) -> bool:
    left_path = PurePosixPath(left)
    right_path = PurePosixPath(right)
    return left_path == right_path or left_path in right_path.parents or right_path in left_path.parents
