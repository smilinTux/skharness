"""Spawner seam (spec §4): create/kill a worker. FakeSpawner for CI; TmuxSpawner
(P1) does git-worktree + tmux + ttyd/sshx for real."""

from __future__ import annotations

from abc import ABC, abstractmethod

from skharness.session import Session


class Spawner(ABC):
    @abstractmethod
    async def spawn(self, session: Session, *, prompt: str) -> Session:
        """Create the worker (worktree + tmux + web-terminal); return the session
        with worktree/tmux/web_url filled in."""

    @abstractmethod
    async def kill(self, session_id: str) -> None: ...


class FakeSpawner(Spawner):
    """In-memory spawner for CI — records calls, returns deterministic handles."""

    def __init__(self, base_url: str = "http://tailnet:7700") -> None:
        self._base = base_url
        self.spawned: list[str] = []
        self.killed: list[str] = []

    async def spawn(self, session: Session, *, prompt: str) -> Session:
        session.worktree = f"/wt/{session.id}"
        session.tmux = f"sk-{session.id}"
        session.web_url = f"{self._base}/t/{session.id}"
        self.spawned.append(session.id)
        return session

    async def kill(self, session_id: str) -> None:
        self.killed.append(session_id)
