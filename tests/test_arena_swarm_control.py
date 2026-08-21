from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from skharness.arena.swarm import (
    BudgetExceededError,
    BudgetUsage,
    ExecutionBudget,
    SubagentContract,
    SubagentDisposition,
    SubagentResult,
    SwarmContractError,
    SwarmIdentity,
    SwarmRole,
    TeamBudget,
)
from skharness.arena.swarm_control import (
    SwarmAdmissionReason,
    SwarmScheduler,
    SwarmStateError,
    WorkerLeaseState,
)

NOW = datetime(2026, 8, 21, tzinfo=timezone.utc)
DIGEST = "sha256:" + "1" * 64
CARD_HASH = "sha256:" + "2" * 64


class Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def identity() -> SwarmIdentity:
    return SwarmIdentity(
        card_id="card-1",
        card_hash=CARD_HASH,
        base_commit="a" * 40,
        evidence_id=DIGEST,
        trajectory_id="trajectory-1",
    )


def team_budget(**updates) -> TeamBudget:
    values = {
        "team_id": "team-1",
        "wall_seconds": 240,
        "token_limit": 4_000,
        "tool_call_limit": 80,
        "cost_limit": 4.0,
        "max_concurrency": 4,
    }
    values.update(updates)
    return TeamBudget(**values)


def contract(
    name: str,
    *,
    role: SwarmRole = SwarmRole.SCOUT,
    worktree: str = "shared",
    paths: tuple[str, ...] | None = None,
    seconds: float = 60,
) -> SubagentContract:
    if paths is None:
        paths = ("src/skharness/arena",) if role is SwarmRole.BUILDER else ()
    return SubagentContract(
        contract_id=name,
        team_id="team-1",
        identity=identity(),
        parent_agent_id="orchestrator",
        child_agent_id=f"agent-{name}",
        role=role,
        task=f"Perform bounded {role.value} work for {name}.",
        readable_paths=("src/skharness", "tests"),
        writable_paths=paths,
        protected_paths=("tests/hidden",),
        tool_allowlist=("rg", "read_file"),
        budget=ExecutionBudget(
            wall_seconds=seconds,
            token_limit=1_000,
            tool_call_limit=20,
            cost_limit=1.0,
        ),
        lease_id=f"lease-{name}",
        worktree_id=worktree,
        issued_at=NOW,
    )


def scheduler(clock: Clock, **budget_updates) -> SwarmScheduler:
    return SwarmScheduler(
        team_budget(**budget_updates),
        identity=identity(),
        orchestrator_id="orchestrator",
        lease_ttl_s=20,
        clock=clock,
    )


def result(item: SubagentContract, disposition=SubagentDisposition.COMPLETED):
    completed = disposition is SubagentDisposition.COMPLETED
    return SubagentResult.from_contract(
        item,
        disposition=disposition,
        summary="Bounded child work finished.",
        reason_codes=() if completed else ("blocked_dependency",),
        evidence_refs=(DIGEST,) if completed else (),
        observed_commit="b" * 40 if completed and item.role is SwarmRole.BUILDER else None,
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=5),
    )


def test_admission_is_contract_bound_and_duplicate_delivery_is_idempotent():
    clock = Clock()
    control = scheduler(clock)
    scout = contract("scout")
    control.register(scout)
    first = control.admit(scout.contract_id, idempotency_key="delivery-1")
    duplicate = control.admit(scout.contract_id, idempotency_key="delivery-1")
    assert first.admitted
    assert duplicate.admitted and duplicate.duplicate
    assert duplicate.lease is first.lease

    other = contract("other")
    control.register(other)
    collision = control.admit(other.contract_id, idempotency_key="delivery-1")
    assert not collision.admitted
    assert collision.reason is SwarmAdmissionReason.DUPLICATE


def test_concurrent_duplicate_admission_creates_one_worker_lease():
    clock = Clock()
    control = scheduler(clock)
    scout = contract("scout")
    control.register(scout)
    with ThreadPoolExecutor(max_workers=8) as pool:
        admissions = list(
            pool.map(
                lambda _: control.admit(scout.contract_id, idempotency_key="same"),
                range(32),
            )
        )
    assert {item.lease.lease_id for item in admissions if item.lease} == {scout.lease_id}
    assert sum(not item.duplicate for item in admissions) == 1
    assert control.snapshot()["active_workers"] == 1


def test_write_ownership_conflicts_only_inside_the_same_worktree():
    clock = Clock()
    control = scheduler(clock)
    first = contract("builder-a", role=SwarmRole.BUILDER, paths=("src/skharness",))
    overlap = contract("builder-b", role=SwarmRole.BUILDER, paths=("src/skharness/arena",))
    isolated = contract(
        "builder-c",
        role=SwarmRole.BUILDER,
        worktree="isolated-c",
        paths=("src/skharness/arena",),
    )
    for item in (first, overlap, isolated):
        control.register(item)
    assert control.admit(first.contract_id, idempotency_key="a").admitted
    denied = control.admit(overlap.contract_id, idempotency_key="b")
    assert denied.reason is SwarmAdmissionReason.WRITE_CONFLICT
    assert "overlapping writable" in denied.detail
    assert control.admit(isolated.contract_id, idempotency_key="c").admitted


def test_global_budget_is_reserved_before_workers_start_and_released_at_actual_usage():
    clock = Clock()
    control = scheduler(clock, wall_seconds=100, max_concurrency=2)
    first = contract("first", seconds=60)
    second = contract("second", seconds=60)
    control.register(first)
    control.register(second)
    first_lease = control.admit(first.contract_id, idempotency_key="first").lease
    denied = control.admit(second.contract_id, idempotency_key="second")
    assert denied.reason is SwarmAdmissionReason.BUDGET

    clock.now += 10
    control.complete(first_lease.lease_id, result(first))
    assert control.admit(second.contract_id, idempotency_key="second").admitted
    snapshot = control.snapshot()["budget"]
    assert snapshot["consumed"]["wall_seconds"] == 10
    assert snapshot["reserved"]["wall_seconds"] == 60


def test_usage_delivery_is_idempotent_and_cannot_exceed_child_reservation():
    clock = Clock()
    control = scheduler(clock)
    scout = contract("scout")
    control.register(scout)
    lease = control.admit(scout.contract_id, idempotency_key="scout").lease
    delta = BudgetUsage(tokens=100, tool_calls=3, cost=0.1)
    control.charge(lease.lease_id, delta, delivery_id="usage-1")
    control.charge(lease.lease_id, delta, delivery_id="usage-1")
    assert lease.usage.tokens == 100
    with pytest.raises(SwarmStateError, match="collision"):
        control.charge(
            lease.lease_id,
            BudgetUsage(tokens=101),
            delivery_id="usage-1",
        )
    with pytest.raises(BudgetExceededError, match="child reservation"):
        control.charge(
            lease.lease_id,
            BudgetUsage(tokens=1_000),
            delivery_id="usage-2",
        )


def test_heartbeat_never_extends_hard_deadline_and_timeout_releases_authority():
    clock = Clock()
    control = scheduler(clock)
    scout = contract("scout", seconds=25)
    control.register(scout)
    lease = control.admit(scout.contract_id, idempotency_key="scout").lease
    clock.now += 15
    control.heartbeat(lease.lease_id)
    assert lease.expires_at == lease.deadline_at == 1_025
    clock.now = 1_025
    assert control.reap_timeouts() == (lease.lease_id,)
    assert lease.state is WorkerLeaseState.TIMED_OUT
    assert lease.terminal_reason == "hard_deadline_exceeded"
    assert control.heartbeat(lease.lease_id) is None
    assert control.stop_requests() == (lease.lease_id,)
    assert control.acknowledge_stopped(lease.lease_id)
    assert control.stop_requests() == ()


def test_team_cancellation_is_cascading_idempotent_and_closes_admission():
    clock = Clock()
    control = scheduler(clock)
    first = contract("first")
    second = contract("second")
    third = contract("third")
    for item in (first, second, third):
        control.register(item)
    control.admit(first.contract_id, idempotency_key="first")
    control.admit(second.contract_id, idempotency_key="second")
    assert control.cancel_team(reason="operator_cancelled") == (
        first.lease_id,
        second.lease_id,
    )
    assert control.cancel_team(reason="operator_cancelled") == ()
    denied = control.admit(third.contract_id, idempotency_key="third")
    assert denied.reason is SwarmAdmissionReason.TEAM_CANCELLED


def test_completion_requires_exact_structured_contract_result():
    clock = Clock()
    control = scheduler(clock)
    scout = contract("scout")
    other = contract("other")
    control.register(scout)
    lease = control.admit(scout.contract_id, idempotency_key="scout").lease
    with pytest.raises(SwarmStateError, match="exact worker contract"):
        control.complete(lease.lease_id, result(other))
    accepted = result(scout, SubagentDisposition.BLOCKED)
    assert control.complete(lease.lease_id, accepted).result == accepted
    assert control.complete(lease.lease_id, accepted).state is WorkerLeaseState.FINISHED
    assert control.snapshot()["active_workers"] == 0


def test_restart_recovers_lease_budget_write_scope_and_idempotency(tmp_path):
    clock = Clock()
    path = tmp_path / "swarm-control.json"
    control = SwarmScheduler(
        team_budget(),
        identity=identity(),
        orchestrator_id="orchestrator",
        lease_ttl_s=20,
        clock=clock,
        state_path=path,
    )
    builder = contract("builder", role=SwarmRole.BUILDER)
    overlap = contract(
        "overlap", role=SwarmRole.BUILDER, paths=("src/skharness/arena/swarm.py",)
    )
    control.register(builder)
    control.register(overlap)
    lease = control.admit(builder.contract_id, idempotency_key="builder-run").lease
    control.charge(
        lease.lease_id,
        BudgetUsage(tokens=123, tool_calls=4),
        delivery_id="usage-1",
    )

    recovered = SwarmScheduler.recover(path, clock=clock)
    duplicate = recovered.admit(builder.contract_id, idempotency_key="builder-run")
    assert duplicate.admitted and duplicate.duplicate
    assert duplicate.lease.usage.tokens == 123
    denied = recovered.admit(overlap.contract_id, idempotency_key="overlap-run")
    assert denied.reason is SwarmAdmissionReason.WRITE_CONFLICT

    clock.now += 21
    expired = SwarmScheduler.recover(path, clock=clock)
    assert expired.lease(lease.lease_id).state is WorkerLeaseState.TIMED_OUT
    assert expired.stop_requests() == (lease.lease_id,)
    terminal_duplicate = expired.admit(builder.contract_id, idempotency_key="builder-run")
    assert not terminal_duplicate.admitted and terminal_duplicate.duplicate


def test_corrupt_restart_checkpoint_fails_closed(tmp_path):
    clock = Clock()
    path = tmp_path / "swarm-control.json"
    control = SwarmScheduler(
        team_budget(),
        identity=identity(),
        orchestrator_id="orchestrator",
        clock=clock,
        state_path=path,
    )
    control.register(contract("scout"))
    envelope = json.loads(path.read_text())
    envelope["state"]["orchestrator_id"] = "attacker"
    path.write_text(json.dumps(envelope))
    with pytest.raises(SwarmStateError, match="hash mismatch"):
        SwarmScheduler.recover(path, clock=clock)


def test_contract_from_another_team_or_identity_is_rejected():
    clock = Clock()
    control = scheduler(clock)
    foreign = contract("foreign").model_copy(update={"team_id": "other-team"})
    with pytest.raises(SwarmContractError, match="team_id"):
        control.register(foreign)
