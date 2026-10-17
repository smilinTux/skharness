"""Governed card to session routing and request attribution."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from skharness.card_router import (
    CardClaimRejected,
    CardSessionRouter,
    CardStoreClaimGate,
    EligibleCard,
)
from skharness.harnesses.pi import PiHarness
from skharness.pool import PoolController


class _Gate:
    def __init__(self):
        self.claims = []

    def claim(self, card_id, agent_id):
        self.claims.append((card_id, agent_id))
        return {"event_id": f"event-{card_id}"}


def _cards(count):
    return [
        EligibleCard(f"card{number}", "/repo", "main", f"prompt {number}")
        for number in range(count)
    ]


def test_n_cards_route_to_n_distinct_assignments_and_duplicate_is_refused(tmp_path):
    gate = _Gate()
    router = CardSessionRouter(gate, worktree_root=tmp_path / "worktrees")
    assignments = router.route(_cards(3), 3)

    for field in ("card_id", "agent_id", "session_id", "worktree", "branch"):
        assert len({getattr(item, field) for item in assignments}) == 3
    assert len(gate.claims) == 3

    duplicate = EligibleCard("same", "/repo", "main", "p")
    with pytest.raises(CardClaimRejected, match="same card"):
        router.route([duplicate, duplicate], 2)
    assert len(gate.claims) == 3, "duplicate input must fail before either claim"


def test_router_uses_injected_cardstore_gate_not_board_projection(tmp_path):
    gate = _Gate()
    router = CardSessionRouter(gate, worktree_root=tmp_path)
    assignment = router.route(_cards(1), 1)[0]
    assert gate.claims == [(assignment.card_id, assignment.agent_id)]


def test_cardstore_gate_folds_dependencies_and_appends_serialized_claim(tmp_path):
    store = SimpleNamespace()
    cards = {
        "card1": SimpleNamespace(owner=None, status=SimpleNamespace(value="backlog"), dependencies=["dep"]),
        "dep": SimpleNamespace(owner=None, status=SimpleNamespace(value="done"), dependencies=[]),
    }
    store.fold = lambda card_id: cards.get(card_id)
    appended = []
    store.append_event = lambda *args, **kwargs: appended.append((args, kwargs)) or {"event_id": "e1"}

    class _Lock:
        def __enter__(self): return self
        def __exit__(self, *_): return False
    gate = CardStoreClaimGate(tmp_path, store=store, lock=lambda *_: _Lock())

    gate.claim("card1", "agent1")
    assert appended[0][0][:3] == ("card1", "claim", "agent1")
    assert appended[0][1]["owner"] == "agent1"


def test_pi_assignment_identity_reaches_request_headers_and_distinct_worktrees(tmp_path):
    class Tmux:
        def __init__(self): self.live = set()
        def __call__(self, argv):
            if "new-window" in argv: self.live.add(argv[argv.index("-n") + 1])
            if "list-windows" in argv:
                return "\n".join(f"{sid}\t1" for sid in self.live)
            return ""

    def git(argv):
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    repo = tmp_path / "repo"
    repo.mkdir()
    tmux = Tmux()
    harness = PiHarness(
        gateway_base="http://gateway.invalid/v1",
        runner=tmux,
        git_runner=git,
        dispatch_repos=[str(repo)],
        worktree_root=tmp_path / "worktrees",
        sessions_root=tmp_path / "sessions",
        child_path="/usr/bin:/bin",
    )
    pool = PoolController(harness)
    router = CardSessionRouter(_Gate(), worktree_root=tmp_path / "worktrees")
    cards = [EligibleCard(f"card{n}", str(repo), "main", f"prompt {n}") for n in range(2)]
    assignments = router.route(cards, 2)

    import asyncio
    members = asyncio.run(pool.scale_assignments(lane="coding", assignments=assignments))
    assert len(members) == 2
    assert len({member.worktree for member in members}) == 2
    for assignment in assignments:
        config = json.loads(
            (Path(assignment.worktree) / ".pi-coding-agent" / "models.json").read_text()
        )
        headers = config["providers"]["skgw"]["headers"]
        assert headers == {
            "x-agent-id": assignment.agent_id,
            "x-session-id": assignment.session_id,
            "x-sk-card-id": assignment.card_id,
        }
