"""Immutable contracts and fail-closed budget accounting for Arena swarms.

The controller remains the authority for scheduling, card state, and completion.
These domain objects only describe bounded child work and its evidence. A child's
``completed`` disposition is not a promotion decision; an independent verifier
must still authorize final completion.
"""

from __future__ import annotations

import re
import threading
from datetime import datetime
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


class SubagentContract(FrozenModel):
    """One lease-bound, narrowly scoped parent-to-child assignment."""

    schema_version: Literal["arena.swarm.contract.v1"] = "arena.swarm.contract.v1"
    contract_id: str
    team_id: str
    identity: SwarmIdentity
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

    @model_validator(mode="after")
    def validate_scope(self) -> SubagentContract:
        required = {
            "contract_id": self.contract_id,
            "team_id": self.team_id,
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
        return self

    @property
    def content_hash(self) -> str:
        return canonical_digest(self)


class SubagentResult(FrozenModel):
    """Structured child outcome; never sufficient by itself to complete a card."""

    schema_version: Literal["arena.swarm.result.v1"] = "arena.swarm.result.v1"
    contract_id: str
    contract_hash: str
    identity: SwarmIdentity
    agent_id: str
    role: SwarmRole
    disposition: SubagentDisposition
    summary: str
    reason_codes: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    observed_commit: str | None = None
    started_at: datetime
    finished_at: datetime

    @model_validator(mode="after")
    def validate_result(self) -> SubagentResult:
        if not self.contract_id.strip() or not self.agent_id.strip() or not self.summary.strip():
            raise ValueError("result contract, agent, and summary must not be empty")
        if not _DIGEST_RE.fullmatch(self.contract_hash):
            raise ValueError("contract_hash must be a lowercase sha256 digest")
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
        if (
            self.role is SwarmRole.BUILDER
            and self.disposition is SubagentDisposition.COMPLETED
            and self.observed_commit is None
        ):
            raise ValueError("completed builder results require an observed commit")
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
            agent_id=contract.child_agent_id,
            role=contract.role,
            **fields,
        )

    @property
    def content_hash(self) -> str:
        return canonical_digest(self)


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


def _validate_relative_path(value: str) -> None:
    path = PurePosixPath(value)
    if path.is_absolute() or value != path.as_posix() or ".." in path.parts or value in {"", "."}:
        raise ValueError("path scopes must be normalized repository-relative paths")


def _paths_overlap(left: str, right: str) -> bool:
    left_path = PurePosixPath(left)
    right_path = PurePosixPath(right)
    return left_path == right_path or left_path in right_path.parents or right_path in left_path.parents
