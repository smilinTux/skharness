"""Session model — one isolated agent worker."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum


class SessionStatus(str, Enum):
    SPAWNING = "spawning"
    RUNNING = "running"
    ENDED = "ended"


@dataclass(eq=True)
class Session:
    id: str
    agent: str
    repo: str
    worktree: str = ""
    tmux: str = ""
    web_url: str = ""
    status: SessionStatus = SessionStatus.SPAWNING
    created_at: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Session":
        d = dict(d)
        d["status"] = SessionStatus(d.get("status", "spawning"))
        known = {"id", "agent", "repo", "worktree", "tmux", "web_url",
                 "status", "created_at"}
        return cls(**{k: v for k, v in d.items() if k in known})
