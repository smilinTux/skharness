"""claude-code harness adapter, read-only session plane (skcode P0).

Lists + tails the sessions that jarvis-heartbeat and skharness already create:
the `skchat-agents` tmux session with windows named `<agent>-<short_id>` (the
`monitor` window is skipped), tailed with `tmux capture-pane -p`, plus the
historical `~/.skcapstone/agents/<agent>/sessions/` dirs.

This is a SEPARATE harness from the autocode Docker task-plane ClaudeCodeAdapter
(src/skharness/autocode/adapters/claude_code.py). It subclasses the unified
Harness contract and implements ONLY the read-only session subset (list_sessions
+ stream); it declares session_plane=True, headless_api="pty". It is wired
directly by the skcode-hostd daemon, not registered in the shared HARNESSES dict,
so the task-plane claude-code adapter there is untouched.

All tmux calls go through an injectable argv `runner` (never shell=True), so unit
tests use a fake runner and never touch real tmux. The read subset is
list_sessions + stream; the write verbs are `archive` (stop + persist) and
`inject` (send operator text into a running session as keystrokes, skcode P1).
`archive` is NOT a destructive kill: it persists the session's transcript to the
historical sessions dir FIRST, then stops the tmux window. `inject` targets the
same tmux window with `send-keys` and is safe on a missing/invalid sid (a clean
no-op that never touches tmux). There is no spawn path here.
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import time
from pathlib import Path
from typing import AsyncIterator, Callable

from skharness.events import EventType, SessionEvent
from skharness.harness import Harness, HarnessCapabilities, SessionDescriptor

Runner = Callable[[list[str]], str]

_SID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_HARNESS = "claude-code"
_MONITOR_WINDOW = "monitor"
_DEFAULT_SESSIONS_ROOT = Path.home() / ".skcapstone" / "agents"


def _default_runner(argv: list[str]) -> str:
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    return proc.stdout


def parse_windows(out: str, *, host: str) -> list[SessionDescriptor]:
    """Parse `tmux list-windows -F '#{window_name}\t#{window_activity}'` output
    into live SessionDescriptors. Skips the `monitor` window and blank lines; the
    session id IS the window name, and the agent is the name up to the last '-'."""
    sessions: list[SessionDescriptor] = []
    for raw in out.splitlines():
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        parts = line.split("\t")
        name = parts[0].strip()
        if not name or name == _MONITOR_WINDOW:
            continue
        activity = 0.0
        if len(parts) > 1 and parts[1].strip():
            try:
                activity = float(parts[1].strip())
            except ValueError:
                activity = 0.0
        sessions.append(SessionDescriptor(
            sid=name, host=host, harness=_HARNESS, state="running",
            last_activity=activity,
        ))
    return sessions


def scan_historical(sessions_root, *, host: str, limit: int = 50) -> list[SessionDescriptor]:
    """Enumerate ~/.skcapstone/agents/<agent>/sessions/*.json (or *.jsonl) as
    ended SessionDescriptors (state='ended'), newest first, capped at `limit`."""
    root = Path(sessions_root)
    if not root.exists():
        return []
    found: list[tuple[float, SessionDescriptor]] = []
    for agent_dir in sorted(root.iterdir()):
        sdir = agent_dir / "sessions"
        if not sdir.is_dir():
            continue
        for f in sdir.iterdir():
            if f.suffix not in (".json", ".jsonl"):
                continue
            try:
                mtime = f.stat().st_mtime
            except OSError:
                mtime = 0.0
            found.append((mtime, SessionDescriptor(
                sid=f"{agent_dir.name}/{f.stem}", host=host, harness=_HARNESS,
                state="ended", last_activity=mtime,
            )))
    found.sort(key=lambda t: t[0], reverse=True)
    return [sd for _, sd in found[:limit]]


def new_lines(prev: str, cur: str) -> list[str]:
    """Lines present in `cur` beyond the common prefix of `prev` (naive tail diff
    for the read-only capture-pane poll)."""
    prev_lines = prev.splitlines()
    cur_lines = cur.splitlines()
    i = 0
    while i < len(prev_lines) and i < len(cur_lines) and prev_lines[i] == cur_lines[i]:
        i += 1
    return cur_lines[i:]


class ClaudeCodeHarness(Harness):
    name = _HARNESS

    def __init__(
        self,
        *,
        host: str = ".158",
        tmux_session: str = "skchat-agents",
        sessions_root: Path | None = None,
        runner: Runner | None = None,
        poll_interval: float = 1.0,
        max_polls: int | None = None,
    ) -> None:
        self.host = host
        self.tmux_session = tmux_session
        self.sessions_root = Path(sessions_root) if sessions_root else _DEFAULT_SESSIONS_ROOT
        self._runner = runner or _default_runner
        self.poll_interval = poll_interval
        self.max_polls = max_polls

    def capabilities(self) -> HarnessCapabilities:
        # Read-only session plane over a PTY (tmux). No task plane, no writes.
        return {"session_resume": True, "structured_output": "none",
                "sandbox": False, "tool_restrictions": False,
                "task_plane": False, "session_plane": True,
                "headless_api": "pty", "hot_set_model": False}

    def _list_windows(self) -> list[SessionDescriptor]:
        out = self._runner([
            "tmux", "list-windows", "-t", self.tmux_session,
            "-F", "#{window_name}\t#{window_activity}",
        ])
        return parse_windows(out, host=self.host)

    async def list_sessions(self) -> list[SessionDescriptor]:
        live = self._list_windows()
        historical = scan_historical(self.sessions_root, host=self.host)
        return live + historical

    def _persist_transcript(self, sid: str, transcript: str) -> Path:
        """Write the session's final transcript to the historical sessions dir so
        `scan_historical` (and thus `list_sessions`) picks it up as an ended row.

        The window name is `<agent>-<short_id>`; the agent is the part up to the
        last '-'. The record is a JSON file under `<root>/<agent>/sessions/<sid>.json`
        so the archived session survives with its transcript after the PTY is gone.
        """
        agent = sid.rsplit("-", 1)[0] if "-" in sid else sid
        sdir = self.sessions_root / agent / "sessions"
        sdir.mkdir(parents=True, exist_ok=True)
        path = sdir / f"{sid}.json"
        record = {
            "sid": sid,
            "agent": agent,
            "harness": self.name,
            "host": self.host,
            "state": "archived",
            "archived_at": time.time(),
            "transcript": transcript,
        }
        path.write_text(json.dumps(record, indent=2))
        return path

    async def archive(self, sid: str) -> dict:
        """Archive = STOP + PERSIST a session (never a destructive kill).

        Persists the session's full tmux scrollback to the historical sessions dir
        FIRST, then stops the session's tmux window with `tmux kill-window`. Ordering
        is load-bearing: the transcript is on disk before the PTY is stopped, so a
        failure can never leave a stopped session with a lost transcript.

        Idempotent + safe: an invalid sid, or a sid with no live tmux window (already
        archived / never running), returns a clean ``archived: False`` no-op result
        rather than raising, and never touches tmux.
        """
        if not _SID_RE.match(sid):
            return {"sid": sid, "archived": False, "reason": "invalid session id"}

        live_ids = {s.sid for s in self._list_windows()}
        if sid not in live_ids:
            # Already gone / never running: nothing to stop, no-op (idempotent).
            return {
                "sid": sid,
                "archived": False,
                "reason": "no live session (already archived or never running)",
            }

        target = f"{self.tmux_session}:{sid}"
        # 1. PERSIST first: capture the full scrollback (-S - = from the top).
        transcript = self._runner(["tmux", "capture-pane", "-p", "-S", "-", "-t", target])
        path = self._persist_transcript(sid, transcript)
        # 2. STOP only after the transcript is durable: kill just this window, not
        #    the whole `skchat-agents` session (least-destructive stop of the PTY).
        self._runner(["tmux", "kill-window", "-t", target])
        return {"sid": sid, "archived": True, "transcript_path": str(path)}

    async def inject(self, sid: str, text: str) -> dict:
        """Inject operator `text` into a running session as tmux keystrokes (P1).

        Sends `text` followed by an Enter keypress to the session's tmux window
        via the injectable argv runner (never shell=True):
        ``tmux send-keys -t skchat-agents:<sid> <text> Enter``. This is a WRITE
        verb; the daemon route that calls it stays behind the capauth bearer gate.

        Idempotent + safe: an invalid sid, or a sid with no live tmux window
        (already archived / never running), returns a clean ``injected: False``
        no-op result rather than raising, and NEVER touches tmux.
        """
        if not _SID_RE.match(sid):
            return {"sid": sid, "injected": False, "reason": "invalid session id"}

        live_ids = {s.sid for s in self._list_windows()}
        if sid not in live_ids:
            return {
                "sid": sid,
                "injected": False,
                "reason": "no live session (already archived or never running)",
            }

        target = f"{self.tmux_session}:{sid}"
        # Send the text, then a separate Enter keypress so the running CLI reads
        # it as a submitted line. argv-based runner => no shell, and the sid is
        # charset-validated above, so nothing here is shell-interpolated.
        self._runner(["tmux", "send-keys", "-t", target, text, "Enter"])
        return {"sid": sid, "injected": True}

    async def stream(self, sid: str) -> AsyncIterator[SessionEvent]:
        now = time.time()
        if not _SID_RE.match(sid):
            yield SessionEvent(type=EventType.STATUS, text="invalid session id", ts=now)
            return
        yield SessionEvent(type=EventType.STATUS, text="attached", ts=now)
        prev = ""
        polls = 0
        target = f"{self.tmux_session}:{sid}"
        while self.max_polls is None or polls < self.max_polls:
            cur = self._runner(["tmux", "capture-pane", "-p", "-t", target])
            for line in new_lines(prev, cur):
                yield SessionEvent(type=EventType.ASSISTANT_TEXT, text=line,
                                   ts=time.time())
            prev = cur
            polls += 1
            if self.max_polls is not None and polls >= self.max_polls:
                break
            await asyncio.sleep(self.poll_interval)
