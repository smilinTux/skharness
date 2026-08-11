"""SessionEventStore - assigns seq at append, persists a capped per-session
archive (skcode Code-section card C-1, spec 2026-08-11 section 5.3).

VERIFIED at implementation time (the card asked this be checked first): the
daemon's WS route (``daemon.py::stream``) had NO in-memory event buffer of any
kind before this module. It called ``harness.stream(sid)`` and forwarded each
``SessionEvent.to_dict()`` straight onto the socket, re-deriving events from
scratch (tmux capture-pane diff, or a byte-offset tail of the harness's own
``stream.jsonl``) on every new connection. Nothing was ever buffered or kept
as a ``SessionEvent``, so there was no ``seq`` to assign and nothing to page
through on reconnect. This module is that missing buffer, not a rewire of an
existing one.

Two responsibilities, one class:

* Append-time ``seq`` assignment: a plain in-memory ``dict[sid, int]``
  counter, process-local by construction. It is NEVER read from or seeded off
  the persisted file, so a fresh process (a daemon restart) starts every
  session's counter at 1 again even though the file may already hold events
  with higher seq numbers from the prior process. That reset is intentional
  (spec 5.1); see ``events.py`` and the explicit test in
  ``tests/test_session_events.py``.
* A bounded per-session JSONL archive under
  ``~/.skcapstone/skcode/sessions/<sid>/events.jsonl`` (flat files as truth,
  house style), size-capped by trimming the oldest lines once a session's
  file exceeds ``max_bytes``. ``persist=False`` (the default when the daemon
  is built without an explicit store) skips the file entirely and only
  assigns seq/sid/source, so tests and any harness that never configures a
  root never touch disk.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import replace
from pathlib import Path

from skharness.events import SessionEvent

#: Default per-session cap. Generous enough to hold a long working session's
#: worth of tool_call/tool_result/assistant_text lines at SKWorld's single-
#: operator volumes (spec 5.3: "a capped JSONL per session is enough"), small
#: enough that a runaway session can never grow unbounded on disk.
DEFAULT_MAX_BYTES = 2_000_000

_EVENTS_FILENAME = "events.jsonl"


def default_sessions_root() -> Path:
    """``~/.skcapstone/skcode/sessions`` (or ``$SKCODE_STATE_DIR/sessions``).

    Mirrors ``serve.skcode_state_dir()``'s env convention without importing
    it (importing serve.py here would risk a cycle: serve -> daemon ->
    session_events -> serve), so this reads the same ``SKCODE_STATE_DIR``
    env var directly.
    """
    state = os.environ.get("SKCODE_STATE_DIR")
    base = Path(state) if state else Path.home() / ".skcapstone" / "skcode"
    return base / "sessions"


class SessionEventStore:
    """Per-session append-time seq assignment + optional capped JSONL archive."""

    def __init__(self, *, root: Path | None = None, persist: bool = True,
                max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self.root = Path(root) if root is not None else default_sessions_root()
        self.persist = persist
        self.max_bytes = max_bytes
        # Process-local, in-memory ONLY. This is the whole reset-on-restart
        # contract: a new SessionEventStore (== a new daemon process) starts
        # every sid back at 0, regardless of what is already on disk.
        self._seq: dict[str, int] = {}
        self._lock = threading.Lock()

    # ---- append (the "session buffer") -----------------------------------

    def append(self, sid: str, event: SessionEvent, *,
              source: str = "interactive") -> SessionEvent:
        """Assign the next per-session seq, stamp sid/source, persist, return.

        Returns a NEW SessionEvent (never mutates ``event`` in place), so the
        caller's original object is left untouched, e.g. a harness fixture's
        seeded event list is safe to reuse across reads.
        """
        with self._lock:
            seq = self._seq.get(sid, 0) + 1
            self._seq[sid] = seq
        stamped = replace(event, seq=seq, sid=sid, source=source)
        if self.persist:
            self._persist(sid, stamped)
        return stamped

    # ---- archive paging ----------------------------------------------------

    def read_page(self, sid: str, *, before_seq: int | None = None,
                 limit: int = 100) -> list[dict]:
        """The archive page for ``GET .../events?before_seq=N&limit=M``.

        Ascending-seq order (oldest of the page first, matching the live
        stream's own order), capped to ``limit`` rows. ``before_seq`` filters
        to rows with ``seq < before_seq`` (backward paging: "the M events
        before N"); omitted, the page is simply the newest ``limit`` rows.
        Reads the persisted file fresh every call (no in-memory cache of
        archive content), so a size-capped trim is always reflected.
        """
        rows = self._read_all(sid)
        if before_seq is not None:
            rows = [r for r in rows if r.get("seq", 0) < before_seq]
        rows.sort(key=lambda r: r.get("seq", 0))
        if limit is not None and limit > 0:
            rows = rows[-limit:]
        return rows

    # ---- internals -----------------------------------------------------

    def _path(self, sid: str) -> Path:
        return self.root / sid / _EVENTS_FILENAME

    def _read_all(self, sid: str) -> list[dict]:
        path = self._path(sid)
        if not path.exists():
            return []
        rows: list[dict] = []
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            return []
        return rows

    def _persist(self, sid: str, event: SessionEvent) -> None:
        path = self._path(sid)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.to_dict(), sort_keys=True) + "\n")
        except OSError:
            return
        self._enforce_cap(path)

    def _enforce_cap(self, path: Path) -> None:
        """Size-cap by trimming the OLDEST lines once ``path`` exceeds the cap.

        Best-effort: any I/O error here just leaves the file over-cap for one
        more append rather than raising (persistence must never crash the
        stream). O(n) in the file's current line count, acceptable at the
        capped size this is bounded to (spec 5.3: SKWorld volumes are low).
        """
        try:
            if path.stat().st_size <= self.max_bytes:
                return
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return
        while lines and sum(len(line) + 1 for line in lines) > self.max_bytes:
            lines.pop(0)
        try:
            text = "\n".join(lines) + ("\n" if lines else "")
            path.write_text(text, encoding="utf-8")
        except OSError:
            pass


__all__ = ["SessionEventStore", "DEFAULT_MAX_BYTES", "default_sessions_root"]
