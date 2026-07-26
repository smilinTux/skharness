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
tests use a fake runner and never touch real tmux. Read-only: there is no
spawn/inject/kill path here.
"""
from __future__ import annotations

import asyncio
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

    async def list_sessions(self) -> list[SessionDescriptor]:
        out = self._runner([
            "tmux", "list-windows", "-t", self.tmux_session,
            "-F", "#{window_name}\t#{window_activity}",
        ])
        live = parse_windows(out, host=self.host)
        historical = scan_historical(self.sessions_root, host=self.host)
        return live + historical

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
