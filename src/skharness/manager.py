"""SessionManager — ties registry + spawner. Spawn creates an isolated worker and
registers it RUNNING; kill tears it down and marks it ENDED."""

from __future__ import annotations

import os
import time

from skharness.registry import SessionRegistry
from skharness.session import Session, SessionStatus
from skharness.spawner import Spawner


class SessionManager:
    def __init__(self, *, registry: SessionRegistry, spawner: Spawner) -> None:
        self.registry = registry
        self.spawner = spawner

    async def spawn(self, *, agent: str, prompt: str, repo: str) -> Session:
        sid = os.urandom(6).hex()
        s = Session(id=sid, agent=agent, repo=repo, created_at=time.time())
        self.registry.add(s)
        s = await self.spawner.spawn(s, prompt=prompt)
        s.status = SessionStatus.RUNNING
        self.registry.update(s)
        return s

    async def kill(self, session_id: str) -> None:
        await self.spawner.kill(session_id)
        self.registry.set_status(session_id, SessionStatus.ENDED)

    def list(self) -> list[Session]:
        return self.registry.live()

    def attach_url(self, session_id: str) -> str | None:
        s = self.registry.get(session_id)
        return s.web_url if s and s.status != SessionStatus.ENDED else None
