"""SessionRegistry — track/persist agent sessions (json now; a coord/skmem-pg
adapter is P3 behind this same interface)."""

from __future__ import annotations

import json
from pathlib import Path

from skharness.session import Session, SessionStatus

_DEFAULT = Path.home() / ".skharness" / "sessions.json"


class SessionRegistry:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else _DEFAULT
        self._s: dict[str, Session] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for d in raw.get("sessions", []):
            try:
                s = Session.from_dict(d)
                self._s[s.id] = s
            except (TypeError, ValueError):
                continue

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"sessions": [s.to_dict() for s in self._s.values()]},
                       indent=2), encoding="utf-8")

    def add(self, s: Session) -> None:
        self._s[s.id] = s
        self._save()

    def get(self, sid: str) -> Session | None:
        return self._s.get(sid)

    def update(self, s: Session) -> None:
        self._s[s.id] = s
        self._save()

    def set_status(self, sid: str, status: SessionStatus) -> None:
        s = self._s.get(sid)
        if s is not None:
            s.status = status
            self._save()

    def live(self) -> list[Session]:
        return [s for s in self._s.values() if s.status != SessionStatus.ENDED]
