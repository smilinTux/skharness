"""Autocode runs register as skcode sessions (card C-1 AC3, spec 5.1).

"Autocode runs already execute inside skharness. Register the orchestrator's
runs as sessions with source=autocode so they appear in the same rail for
free. hostd sessions and autocode runs are literally the same stream."

The orchestrator (``orchestrator.py::phase2_swarm``) and the skcode-hostd
daemon (``daemon.py``) are not guaranteed to be the same process (a scheduled
`skos autopilot run` and the always-on hostd daemon are typically separate),
so this registry is flat-file backed (house style: flat files as truth) under
the SAME per-session directory the SessionEvent archive uses
(``~/.skcapstone/skcode/sessions/<sid>/``), just a different filename
(``session.json`` beside ``events.jsonl``). The daemon merges these in via
``build_daemon_app(list_autocode_sessions=registry.list)``.

Registration is best-effort BY THE CALLER's choice, not by construction: every
method here can raise on a genuine I/O problem (the orchestrator wraps calls
in its own try/except, matching the existing pattern for audit/journal writes
that "must never fail a run"). This module does not swallow errors itself so
a real bug is never silently invisible; the orchestrator decides how much
protection it wants around it.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from skharness.harness import SessionDescriptor

SOURCE_AUTOCODE = "autocode"
_DESCRIPTOR_FILENAME = "session.json"


def sessions_root() -> Path:
    """``~/.skcapstone/skcode/sessions`` (or ``$SKCODE_STATE_DIR/sessions``).

    Same env convention as ``skharness.session_events.default_sessions_root``
    (and ``serve.skcode_state_dir``), read directly here rather than imported,
    to keep this module free of any daemon-side import.
    """
    state = os.environ.get("SKCODE_STATE_DIR")
    base = Path(state) if state else Path.home() / ".skcapstone" / "skcode"
    return base / "sessions"


class AutocodeSessionRegistry:
    """Registers/updates/ends autocode runs as SessionDescriptor rows.

    Flat-file backed: one small JSON descriptor per sid, alongside (but never
    overlapping with) that sid's ``events.jsonl`` under the same directory.
    """

    def __init__(self, *, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else sessions_root()

    def register(self, *, sid: str, host: str = "", repo: str = "",
                model: str = "", last_message: str = "") -> SessionDescriptor:
        """Create (or overwrite) sid's descriptor as a running autocode session."""
        desc = SessionDescriptor(
            sid=sid, host=host, harness="autocode", repo=repo, model=model,
            state="running", last_activity=time.time(),
            last_message=last_message, source=SOURCE_AUTOCODE,
        )
        self._write(desc)
        return desc

    def update(self, sid: str, *, state: str | None = None,
              last_message: str | None = None) -> SessionDescriptor | None:
        """Update sid's state/last_message in place. None (unknown sid) is a
        clean no-op, never a raise: an update racing a registry wipe (or a run
        that never registered) must not break the caller's own flow."""
        desc = self.get(sid)
        if desc is None:
            return None
        if state is not None:
            desc.state = state
        if last_message is not None:
            desc.last_message = last_message
        desc.last_activity = time.time()
        self._write(desc)
        return desc

    def end(self, sid: str, *, last_message: str | None = None) -> None:
        """Mark sid ended (never deletes the record: it stays visible as history,
        the same way the claude-code harness's historical sessions do)."""
        self.update(sid, state="ended", last_message=last_message)

    def get(self, sid: str) -> SessionDescriptor | None:
        path = self._path(sid)
        if not path.is_file():
            return None
        try:
            return SessionDescriptor.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return None

    def list(self) -> list[SessionDescriptor]:
        """Every registered autocode session (running + ended), for
        ``daemon.py``'s ``list_autocode_sessions`` provider."""
        if not self.root.is_dir():
            return []
        out: list[SessionDescriptor] = []
        for d in sorted(self.root.iterdir()):
            path = d / _DESCRIPTOR_FILENAME
            if not path.is_file():
                continue
            try:
                out.append(SessionDescriptor.from_dict(
                    json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, ValueError):
                continue
        return out

    def _path(self, sid: str) -> Path:
        return self.root / sid / _DESCRIPTOR_FILENAME

    def _write(self, desc: SessionDescriptor) -> None:
        path = self._path(desc.sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(desc.to_dict(), sort_keys=True), encoding="utf-8")


__all__ = ["AutocodeSessionRegistry", "sessions_root", "SOURCE_AUTOCODE"]
