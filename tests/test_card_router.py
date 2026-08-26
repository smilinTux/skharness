"""Governed card router tests with fake launchers only."""

from __future__ import annotations

from collections import deque

import pytest

from skharness.card_router import (
    CardRoute,
    CardRouteError,
    CardSessionRouter,
    ClaimRejectedError,
    LaunchInvariantError,
    LaunchReceipt,
)


class FakeClaimGate:
    """Small ownership gate with the conflict semantics the router relies on."""

    def __init__(self) -> None:
        self.owners: dict[str, str] = {}
        self.claims: list[tuple[str, str]] = []
        self.releases: list[tuple[str, str, str]] = []

    def claim_task(self, agent_name: str, task_id: str, force: bool = False):
        del force
        owner = self.owners.get(task_id)
        if owner is not None and owner != agent_name:
            raise ValueError(f"Task {task_id} already claimed by {owner}")
        self.owners[task_id] = agent_name
        self.claims.append((agent_name, task_id))

    def release_claim(self, owner: str, task_id: str, actor: str = "") -> bool:
        self.releases.append((owner, task_id, actor))
        if self.owners.get(task_id) != owner:
            return False
        del self.owners[task_id]
        return True


class FakeLauncher:
    """Records assignments and returns their reserved facts without I/O."""

    def __init__(self) -> None:
        self.assignments = []
        self.request_log = []

    async def __call__(self, assignment):
        self.assignments.append(assignment)
        receipt = LaunchReceipt(
            session_id=f"session-{len(self.assignments)}",
            card_id=assignment.card_id,
            agent_identity=assignment.agent_identity,
            worktree=assignment.worktree,
            branch=assignment.branch,
        )
        self.request_log.append(
            {
                "session_id": receipt.session_id,
                "card_id": assignment.card_id,
                "agent_id": assignment.agent_identity,
            }
        )
        return receipt


def tokens(*values: str):
    queue = deque(values)
    return queue.popleft


def routes(count: int) -> list[CardRoute]:
    return [
        CardRoute(
            card_id=f"card{number}",
            repo=f"/repos/repo{number}",
            base_branch="main",
            prompt=f"work card {number}",
        )
        for number in range(count)
    ]


@pytest.mark.asyncio
async def test_n_cards_yield_distinct_claimed_assignments_and_launcher_identity(tmp_path):
    gate = FakeClaimGate()
    launcher = FakeLauncher()
    router = CardSessionRouter(
        gate,
        launcher,
        worktree_root=tmp_path / "worktrees",
        identity_token=tokens("aa11", "bb22", "cc33"),
    )

    result = await router.route(routes(3))

    assert len(result) == 3
    assert len({item.assignment.card_id for item in result}) == 3
    assert len({item.assignment.agent_identity for item in result}) == 3
    assert len({item.assignment.worktree for item in result}) == 3
    assert len({item.assignment.branch for item in result}) == 3
    assert len({item.receipt.session_id for item in result}) == 3
    assert [assignment.card_id for assignment in launcher.assignments] == [
        "card0",
        "card1",
        "card2",
    ]
    assert gate.owners == {
        item.assignment.card_id: item.assignment.agent_identity for item in result
    }
    for item in result:
        assert item.receipt.card_id == item.assignment.card_id
        assert item.receipt.agent_identity == item.assignment.agent_identity
    assert [row["card_id"] for row in launcher.request_log] == ["card0", "card1", "card2"]
    assert [row["agent_id"] for row in launcher.request_log] == [
        item.assignment.agent_identity for item in result
    ]


@pytest.mark.asyncio
async def test_duplicate_card_in_one_batch_is_rejected_before_claim_or_launch(tmp_path):
    gate = FakeClaimGate()
    launcher = FakeLauncher()
    router = CardSessionRouter(
        gate,
        launcher,
        worktree_root=tmp_path,
        identity_token=tokens("aa11", "bb22"),
    )
    duplicate = CardRoute("samecard", "/repo", "main", "work")

    with pytest.raises(CardRouteError, match="duplicate card"):
        await router.route([duplicate, duplicate])

    assert gate.claims == []
    assert launcher.assignments == []


@pytest.mark.asyncio
async def test_duplicate_card_across_batches_loses_claim_gate_and_never_launches_twice(tmp_path):
    gate = FakeClaimGate()
    launcher = FakeLauncher()
    router = CardSessionRouter(
        gate,
        launcher,
        worktree_root=tmp_path,
        identity_token=tokens("first1", "second2"),
    )
    route = CardRoute("samecard", "/repo", "main", "work")

    first = await router.route([route])
    with pytest.raises(ClaimRejectedError, match="samecard"):
        await router.route([route])

    assert len(first) == 1
    assert len(launcher.assignments) == 1
    assert gate.owners["samecard"] == first[0].assignment.agent_identity


@pytest.mark.asyncio
async def test_batch_claim_failure_releases_prior_claim_and_launches_nothing(tmp_path):
    gate = FakeClaimGate()
    gate.owners["card1"] = "another-worker"
    launcher = FakeLauncher()
    router = CardSessionRouter(
        gate,
        launcher,
        worktree_root=tmp_path,
        identity_token=tokens("aa11", "bb22"),
    )

    with pytest.raises(ClaimRejectedError, match="card1"):
        await router.route(routes(2))

    assert "card0" not in gate.owners
    assert gate.owners["card1"] == "another-worker"
    assert launcher.assignments == []
    assert gate.releases[0][1] == "card0"


@pytest.mark.asyncio
async def test_launcher_cannot_substitute_card_identity_worktree_or_branch(tmp_path):
    gate = FakeClaimGate()

    async def bad_launcher(assignment):
        return LaunchReceipt(
            session_id="session-1",
            card_id="different-card",
            agent_identity=assignment.agent_identity,
            worktree=assignment.worktree,
            branch=assignment.branch,
        )

    router = CardSessionRouter(
        gate,
        bad_launcher,
        worktree_root=tmp_path,
        identity_token=tokens("aa11"),
    )

    with pytest.raises(LaunchInvariantError, match="honor"):
        await router.route(routes(1))


@pytest.mark.asyncio
@pytest.mark.needs_skcapstone
async def test_real_board_claim_gate_mirrors_distinct_owners_into_cardstore(tmp_path, monkeypatch):
    """The routing mutation goes through Board.claim_task, not a display payload."""
    from skcoord.card import Column
    from skcoord.card_store import CardStore
    from skcoord.coordination import Board, Task

    monkeypatch.delenv("SKCOORD_CARD_STORE", raising=False)
    board = Board(tmp_path)
    board.create_task(Task(id="card0", title="First eligible leaf"))
    board.create_task(Task(id="card1", title="Second eligible leaf"))
    launcher = FakeLauncher()
    router = CardSessionRouter.from_cardstore(
        tmp_path,
        launcher,
        worktree_root=tmp_path / "worktrees",
        identity_token=tokens("aa11", "bb22"),
    )

    result = await router.route(routes(2))

    folded = [CardStore(tmp_path).fold(item.assignment.card_id) for item in result]
    assert [card.status for card in folded] == [Column.DOING, Column.DOING]
    assert [card.owner for card in folded] == [item.assignment.agent_identity for item in result]
    assert len(set(card.owner for card in folded)) == 2


@pytest.mark.asyncio
@pytest.mark.needs_skcapstone
async def test_cardstore_claim_gate_prevents_two_sessions_for_one_card(tmp_path, monkeypatch):
    """Two routers cannot turn one CardStore card into two fake sessions."""
    from skcoord.coordination import Board, Task

    monkeypatch.delenv("SKCOORD_CARD_STORE", raising=False)
    Board(tmp_path).create_task(Task(id="samecard", title="One eligible leaf"))
    launcher = FakeLauncher()
    first = CardSessionRouter.from_cardstore(
        tmp_path,
        launcher,
        worktree_root=tmp_path / "worktrees",
        identity_token=tokens("first1"),
    )
    second = CardSessionRouter.from_cardstore(
        tmp_path,
        launcher,
        worktree_root=tmp_path / "worktrees",
        identity_token=tokens("second2"),
    )
    route = CardRoute("samecard", "/repo", "main", "work")

    routed = await first.route([route])
    with pytest.raises(ClaimRejectedError, match="samecard"):
        await second.route([route])

    assert len(routed) == 1
    assert len(launcher.assignments) == 1
    assert len(launcher.request_log) == 1


@pytest.mark.asyncio
@pytest.mark.needs_skcapstone
async def test_real_claim_gate_refuses_blocked_dependency_before_fake_launch(
    tmp_path, monkeypatch
):
    from skcoord.coordination import Board, Task

    monkeypatch.delenv("SKCOORD_CARD_STORE", raising=False)
    board = Board(tmp_path)
    board.create_task(
        Task(id="blocked1", title="Blocked leaf", dependencies=["missing-dependency"])
    )
    launcher = FakeLauncher()
    router = CardSessionRouter.from_cardstore(
        tmp_path,
        launcher,
        worktree_root=tmp_path / "worktrees",
        identity_token=tokens("aa11"),
    )

    with pytest.raises(ClaimRejectedError, match="blocked1"):
        await router.route([CardRoute("blocked1", "/repo", "main", "work")])

    assert launcher.assignments == []
