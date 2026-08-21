from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from skharness.arena import (
    A2AEvent,
    A2AEventKind,
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
    TeamBudgetLedger,
)

NOW = datetime(2026, 8, 21, tzinfo=timezone.utc)
DIGEST = "sha256:" + "1" * 64
OTHER_DIGEST = "sha256:" + "2" * 64


def identity(**updates) -> SwarmIdentity:
    values = {
        "card_id": "card-1234",
        "card_hash": OTHER_DIGEST,
        "base_commit": "a" * 40,
        "evidence_id": DIGEST,
        "trajectory_id": "trajectory-1234",
    }
    values.update(updates)
    return SwarmIdentity(**values)


def contract(
    identifier: str,
    *,
    role: SwarmRole = SwarmRole.SCOUT,
    worktree_id: str = "worktree-main",
    writable_paths: tuple[str, ...] | None = None,
    budget: ExecutionBudget | None = None,
) -> SubagentContract:
    if writable_paths is None:
        writable_paths = ("src/skharness/arena",) if role is SwarmRole.BUILDER else ()
    return SubagentContract(
        contract_id=identifier,
        team_id="team-1",
        identity=identity(),
        parent_agent_id="orchestrator-1",
        child_agent_id=f"agent-{identifier}",
        role=role,
        task="Inspect only the assigned source and report structured evidence.",
        readable_paths=("src/skharness/arena", "tests"),
        writable_paths=writable_paths,
        protected_paths=("tests/hidden",),
        tool_allowlist=("read_file", "rg"),
        budget=budget or ExecutionBudget(
            wall_seconds=60,
            token_limit=1_000,
            tool_call_limit=20,
            cost_limit=1.0,
        ),
        lease_id=f"lease-{identifier}",
        worktree_id=worktree_id,
        issued_at=NOW,
    )


def test_contracts_are_immutable_versioned_and_content_addressed():
    item = contract("scout-1")
    assert item.content_hash.startswith("sha256:")
    assert item.identity.content_hash.startswith("sha256:")
    with pytest.raises(ValidationError):
        item.task = "broaden scope"
    document = item.model_dump()
    document["schema_version"] = "arena.swarm.contract.v999"
    with pytest.raises(ValidationError, match="schema_version"):
        SubagentContract.model_validate(document)


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"base_commit": "abc123"}, "base_commit"),
        ({"card_hash": "coord:latest"}, "card_hash"),
        ({"evidence_id": "evidence-latest"}, "evidence_id"),
        ({"card_id": "card with spaces"}, "card_id"),
        ({"trajectory_id": ""}, "trajectory_id"),
    ],
)
def test_shared_identity_requires_immutable_unambiguous_ids(updates, message):
    with pytest.raises(ValidationError, match=message):
        identity(**updates)


def test_child_roles_have_fail_closed_path_and_tool_scopes():
    builder = contract("builder", role=SwarmRole.BUILDER)
    assert builder.writable_paths == ("src/skharness/arena",)

    with pytest.raises(ValidationError, match="declared writable"):
        contract("builder-ro", role=SwarmRole.BUILDER, writable_paths=())
    with pytest.raises(ValidationError, match="must be read-only"):
        contract("scout-rw", writable_paths=("src",))
    with pytest.raises(ValidationError, match="must not overlap"):
        SubagentContract.model_validate(
            builder.model_dump()
            | {"writable_paths": ("tests",), "protected_paths": ("tests/hidden",)}
        )
    with pytest.raises(ValidationError, match="repository-relative"):
        SubagentContract.model_validate(
            builder.model_dump() | {"writable_paths": ("/etc",)}
        )
    with pytest.raises(ValidationError, match="orchestrator"):
        SubagentContract.model_validate(builder.model_dump() | {"role": "orchestrator"})


def test_result_factory_binds_exact_contract_and_requires_evidence():
    builder = contract("builder", role=SwarmRole.BUILDER)
    result = SubagentResult.from_contract(
        builder,
        disposition=SubagentDisposition.COMPLETED,
        summary="Patch and focused checks completed; controller must still verify.",
        evidence_refs=(OTHER_DIGEST,),
        observed_commit="b" * 40,
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=20),
    )
    assert result.contract_hash == builder.content_hash
    assert result.identity == builder.identity
    assert result.agent_id == builder.child_agent_id
    assert result.role is SwarmRole.BUILDER

    with pytest.raises(ValidationError, match="immutable evidence"):
        SubagentResult.from_contract(
            builder,
            disposition=SubagentDisposition.COMPLETED,
            summary="Trust me",
            observed_commit="b" * 40,
            started_at=NOW,
            finished_at=NOW,
        )
    with pytest.raises(ValidationError, match="reason codes"):
        SubagentResult.from_contract(
            builder,
            disposition=SubagentDisposition.BLOCKED,
            summary="Blocked",
            started_at=NOW,
            finished_at=NOW,
        )


def test_a2a_events_are_contract_bound_and_parent_child_only():
    scout = contract("scout")
    assignment = A2AEvent.from_contract(
        scout,
        event_id="event-1",
        sender_agent_id=scout.parent_agent_id,
        recipient_agent_id=scout.child_agent_id,
        kind=A2AEventKind.ASSIGNMENT,
        sequence=1,
        body="Inspect the declared paths.",
        created_at=NOW,
    )
    assert assignment.identity.trajectory_id == "trajectory-1234"
    assert assignment.contract_hash == scout.content_hash
    assert assignment.content_hash.startswith("sha256:")

    with pytest.raises(ValidationError, match="must be sent by the parent"):
        A2AEvent.model_validate(
            assignment.model_dump()
            | {
                "sender_agent_id": scout.child_agent_id,
                "recipient_agent_id": scout.parent_agent_id,
            }
        )
    with pytest.raises(ValidationError, match="parent/child edge"):
        A2AEvent.model_validate(
            assignment.model_dump() | {"recipient_agent_id": "unrelated-agent"}
        )


def test_team_budget_reserves_globally_and_settles_actual_usage():
    ledger = TeamBudgetLedger(
        TeamBudget(
            team_id="team-1",
            wall_seconds=120,
            token_limit=2_000,
            tool_call_limit=40,
            cost_limit=2.0,
            max_concurrency=2,
        )
    )
    first = contract("scout-1")
    second = contract("tester-1", role=SwarmRole.TESTER)
    ledger.reserve(first)
    ledger.reserve(second)
    snapshot = ledger.snapshot()
    assert snapshot.reserved == BudgetUsage(
        wall_seconds=120,
        tokens=2_000,
        tool_calls=40,
        cost=2.0,
    )
    assert snapshot.remaining == BudgetUsage()

    with pytest.raises(BudgetExceededError, match="concurrency"):
        ledger.reserve(contract("verifier-1", role=SwarmRole.VERIFIER))
    ledger.settle(
        first.contract_id,
        BudgetUsage(wall_seconds=10, tokens=100, tool_calls=3, cost=0.1),
    )
    after = ledger.snapshot()
    assert after.consumed.wall_seconds == 10
    assert after.remaining.wall_seconds == 50
    assert after.active_contract_ids == (second.contract_id,)

    ledger.reserve(
        contract(
            "verifier-1",
            role=SwarmRole.VERIFIER,
            budget=ExecutionBudget(
                wall_seconds=50,
                token_limit=900,
                tool_call_limit=17,
                cost_limit=0.9,
            ),
        )
    )
    with pytest.raises(BudgetExceededError, match="child reservation"):
        ledger.settle(
            second.contract_id,
            BudgetUsage(wall_seconds=61),
        )


def test_budget_rejects_overbooking_before_children_start():
    ledger = TeamBudgetLedger(
        TeamBudget(
            team_id="team-1",
            wall_seconds=100,
            token_limit=10_000,
            tool_call_limit=100,
            cost_limit=10,
            max_concurrency=3,
        )
    )
    ledger.reserve(contract("first"))
    with pytest.raises(BudgetExceededError, match="aggregate"):
        ledger.reserve(contract("second"))


def test_same_worktree_builders_require_exclusive_write_scopes():
    ledger = TeamBudgetLedger(
        TeamBudget(
            team_id="team-1",
            wall_seconds=300,
            token_limit=10_000,
            tool_call_limit=200,
            cost_limit=10,
            max_concurrency=3,
        )
    )
    first = contract("builder-1", role=SwarmRole.BUILDER)
    ledger.reserve(first)
    with pytest.raises(SwarmContractError, match="overlapping writable"):
        ledger.reserve(
            contract(
                "builder-2",
                role=SwarmRole.BUILDER,
                writable_paths=("src/skharness",),
            )
        )
    ledger.reserve(
        contract(
            "builder-isolated",
            role=SwarmRole.BUILDER,
            worktree_id="worktree-isolated",
        )
    )
