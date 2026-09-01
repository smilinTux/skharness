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
import os
import re
import secrets
import subprocess
import time
from pathlib import Path
from typing import AsyncIterator, Callable

from skharness.events import EventType, SessionEvent
from skharness.harness import (
    Harness,
    HarnessCapabilities,
    HarnessSession,
    SessionDescriptor,
    SpawnRejected,
)

Runner = Callable[[list[str]], str]
#: A git runner returns a CompletedProcess so spawn can read the RETURNCODE (a
#: bad branch / a failed worktree add must be observable), unlike the str tmux
#: runner. Injectable so tests never touch a real repo.
GitRunner = Callable[[list[str]], "subprocess.CompletedProcess"]

_SID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_HARNESS = "claude-code"
_MONITOR_WINDOW = "monitor"
_DEFAULT_SESSIONS_ROOT = Path.home() / ".skcapstone" / "agents"
_DEFAULT_WORKTREE_ROOT = Path.home() / ".skcapstone" / "skcode" / "worktrees"
#: A minimal PATH handed to a spawned child (env -i wipes the environment, so a
#: child needs an explicit PATH to resolve `claude`, `git`, etc.).
_DEFAULT_CHILD_PATH = (
    os.path.expanduser("~/.local/bin")
    + ":"
    + os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
)


#: CR-6.2 C1 (cloud OAuth token exfil). By CONSTRUCTION the sandbox profile does
#: NOT receive the operator's real Anthropic subscription token
#: (``CLAUDE_CODE_OAUTH_TOKEN``): a sandbox session is the untrusted,
#: ``--dangerously-skip-permissions``, prompt-injectable surface CR-6 is meant to
#: contain, and a prompt-injected sandbox agent with that token in its env could
#: exfiltrate it. The token is withheld from sandbox by DEFAULT; the ONLY way to
#: put it back is this explicit, documented opt-in (default OFF). ``full`` sessions
#: (allowlisted, trusted operator identity) always get it when present. Prefer the
#: gateway / Ornith models (see :data:`_GATEWAY_MODELS`), which never touch this
#: token at all.
_SANDBOX_CLOUD_TOKEN_ENV = "SKCODE_SANDBOX_ALLOW_CLOUD_TOKEN"

#: Compose-form model ids that route THROUGH skgateway (cloud-free) rather than
#: to the Anthropic cloud. skgateway now exposes an Anthropic-compat
#: ``/v1/messages`` frontend, so the SAME ``claude`` runner can reach these by
#: pointing ANTHROPIC_BASE_URL at the gateway (see :meth:`_build_env`). skgateway
#: routes ``sk-default`` (registry role) to local ornith and
#: ``qwen3.8-27b-huihui-abliterated-q4_k_m`` to the chiap08 27B abliterated
#: backend (it replaced the retired ``ornith-big`` 35B, which now 404s).
_GATEWAY_MODELS = {"sk-default", "qwen3.8-27b-huihui-abliterated-q4_k_m"}

#: Map the compose-form model ids onto the values passed to ``claude --model``.
#: For Anthropic ids this is the concrete ``claude`` alias (sonnet/opus). For
#: gateway ids it is the model NAME skgateway routes on (passed verbatim to the
#: gateway's Anthropic frontend). --model is ALWAYS passed; unknown ids fall
#: closed to a safe concrete Anthropic model.
_MODEL_MAP = {
    "claude-sonnet-5": "sonnet",
    "claude-opus-4-8": "opus",
    "sk-default": "sk-default",   # skgateway registry role -> ornith (cloud-free)
    # skgateway -> chiap08 27B abliterated (cloud-free); replaced ornith-big 35B
    "qwen3.8-27b-huihui-abliterated-q4_k_m": "qwen3.8-27b-huihui-abliterated-q4_k_m",
}


_TRUTHY = {"1", "true", "yes", "on"}


def _env_truthy(name: str) -> bool:
    """True when env var ``name`` is set to a truthy value (1/true/yes/on)."""
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def sandbox_cloud_token_allowed() -> bool:
    """True only when the explicit opt-in (:data:`_SANDBOX_CLOUD_TOKEN_ENV`) is set.

    Default is OFF: the sandbox profile does NOT receive the operator's cloud
    OAuth token (CR-6.2 C1). Flip the env only for a deliberate, documented case.
    """
    return _env_truthy(_SANDBOX_CLOUD_TOKEN_ENV)


def is_gateway_model(model: str | None) -> bool:
    """True when ``model`` routes through skgateway (cloud-free) rather than the
    Anthropic cloud. See :data:`_GATEWAY_MODELS`."""
    return (model or "").strip() in _GATEWAY_MODELS


def map_model(model: str | None) -> str:
    """Resolve a compose-form model id to the ``claude --model`` value.

    Anthropic ids -> a concrete ``claude`` alias; gateway ids -> the model name
    skgateway routes on. Unknown / empty ids fall back to ``"sonnet"`` (never left
    blank), so spawn can always pass ``--model``. See :data:`_MODEL_MAP`.
    """
    key = (model or "").strip()
    return _MODEL_MAP.get(key, "sonnet")


def _default_runner(argv: list[str]) -> str:
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    return proc.stdout


def _default_git_runner(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def parse_repo_allowlist(value) -> list[str]:
    """Parse the SKCODE_DISPATCH_REPOS allowlist into realpath'd repo roots.

    ``value`` is either a comma-separated string (the env form) or a list. Blank
    entries are dropped and every entry is normalized with ``os.path.realpath`` so
    the membership check in :meth:`ClaudeCodeHarness.spawn` compares canonical
    paths (defeats ``..`` / symlink games). An empty / unset allowlist yields ``[]``,
    which spawn treats as DENY ALL (fail closed): no repo can be dispatched until
    the operator explicitly lists one.
    """
    if not value:
        return []
    items = value.split(",") if isinstance(value, str) else list(value)
    out: list[str] = []
    for raw in items:
        entry = (raw or "").strip()
        if not entry:
            continue
        out.append(os.path.realpath(os.path.expanduser(entry)))
    return out


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


#: The agent session archive is SHARED (jarvis heartbeats, cron dumps, etc.), not
#: skcode-only, so it fills with non-interactive records that are noise in the Code
#: list. Skip the obvious machine-generated ones (request dumps, cron rollups, the
#: bare ``sessions`` index) so the list shows real coding sessions.
_HISTORICAL_SKIP = re.compile(r"(?:request_dump|_cron_|^sessions$)")


def scan_historical(sessions_root, *, host: str, limit: int = 20) -> list[SessionDescriptor]:
    """Enumerate ~/.skcapstone/agents/<agent>/sessions/*.json (or *.jsonl) as
    ended SessionDescriptors (state='ended'), newest first, capped at `limit`.
    Machine-generated records (cron dumps, request dumps) are filtered out, see
    :data:`_HISTORICAL_SKIP`."""
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
            if _HISTORICAL_SKIP.search(f.stem):
                continue
            try:
                mtime = f.stat().st_mtime
            except OSError:
                mtime = 0.0
            # Flat sid, joined with "-" not "/". A slash here is fatal in three
            # ways and it silently broke every per-session route: Starlette's
            # {sid} never matches a "/", so GET /sessions/{sid}, its /events
            # archive, the WS tail, inject, ratify, deny and cancel all missed
            # their route entirely (404 before any auth ran, which surfaced to
            # the operator as a bogus 1008 "unauthorized"). It also violated
            # this module's own _SID_RE contract, enforced at seven call sites
            # including stream(). And SessionEventStore._path() is
            # root / sid / events.jsonl with no validation, so admitting "/"
            # into a sid turns it into a path-traversal sink.
            #
            # Fixing this by declaring the routes as {sid:path} would be wrong:
            # GET /sessions/{sid} is declared before /events and /stream, so a
            # greedy match swallows them, and it would leave the traversal sink
            # open. The producer is the right place.
            sid = f"{agent_dir.name}-{f.stem}"
            if not _SID_RE.match(sid):
                continue
            found.append((mtime, SessionDescriptor(
                sid=sid, host=host, harness=_HARNESS,
                state="ended", last_activity=mtime,
            )))
    found.sort(key=lambda t: t[0], reverse=True)
    return [sd for _, sd in found[:limit]]


def _stringify_tool_content(content) -> str:
    """Flatten a tool_result ``content`` into display text.

    Anthropic allows tool_result content to be either a plain string OR a list of
    content blocks (each ``{"type":"text","text":...}`` or otherwise). Join the
    text of block lists; pass a string through; JSON-dump anything else so the
    parser never loses information and never raises."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and "text" in block:
                parts.append(str(block["text"]))
            else:
                parts.append(json.dumps(block))
        return "\n".join(parts)
    if content is None:
        return ""
    return json.dumps(content)


def parse_stream_json_line(line: str, *, ts: float = 0.0) -> list[SessionEvent]:
    """Parse ONE ``claude --output-format stream-json`` line into SessionEvents.

    Structured output for DIRECT sessions (Task 3-B1): instead of scraping the
    rendered TUI, direct sessions run ``claude -p --output-format stream-json
    --verbose`` and this maps each emitted JSON event onto typed SessionEvents.
    One line can yield SEVERAL events (an assistant message with N content blocks
    -> N events) or ZERO.

    FAIL SOFT by contract: a blank line, non-JSON (the pipe-pane capture also
    picks up a trailing ``ESC[?25h`` and a "no stdin" stderr warning), a
    non-object, a missing ``type``, or an unknown ``type`` all yield ``[]`` and
    never raise. The event schema can shift across claude versions, so this only
    reads the fields it needs and ignores the rest.
    """
    s = (line or "").strip()
    if not s:
        return []
    try:
        obj = json.loads(s)
    except (ValueError, TypeError):
        return []
    if not isinstance(obj, dict):
        return []
    etype = obj.get("type")
    if etype == "system":
        # Only the init event is worth surfacing (model + session id); other
        # system subtypes are internal noise.
        if obj.get("subtype") != "init":
            return []
        model = obj.get("model") or "?"
        return [SessionEvent(
            type=EventType.STATUS, ts=ts,
            text=f"session started · model={model}",
            data={"subtype": "init", "model": obj.get("model"),
                  "session_id": obj.get("session_id")},
        )]
    if etype in ("assistant", "user"):
        msg = obj.get("message") or {}
        content = msg.get("content")
        # Some shapes carry a bare string instead of a block list.
        if isinstance(content, str):
            if etype == "assistant" and content:
                return [SessionEvent(type=EventType.ASSISTANT_TEXT, text=content, ts=ts)]
            return []
        if not isinstance(content, list):
            return []
        events: list[SessionEvent] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text" and block.get("text"):
                events.append(SessionEvent(
                    type=EventType.ASSISTANT_TEXT, text=str(block["text"]), ts=ts))
            elif btype == "tool_use":
                events.append(SessionEvent(
                    type=EventType.TOOL_CALL, ts=ts,
                    text=str(block.get("name") or ""),
                    data={"id": block.get("id"), "name": block.get("name"),
                          "input": block.get("input")}))
            elif btype == "tool_result":
                events.append(SessionEvent(
                    type=EventType.TOOL_RESULT, ts=ts,
                    text=_stringify_tool_content(block.get("content")),
                    data={"tool_use_id": block.get("tool_use_id"),
                          "is_error": block.get("is_error")}))
        return events
    if etype == "result":
        is_error = bool(obj.get("is_error"))
        subtype = obj.get("subtype")
        text = "turn failed" if is_error else "turn complete"
        return [SessionEvent(
            type=EventType.STATUS, ts=ts, text=text,
            data={"subtype": subtype, "is_error": is_error,
                  "num_turns": obj.get("num_turns"),
                  "stop_reason": obj.get("stop_reason")},
        )]
    return []


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
        # --- dispatch (spawn) config (skcode P2) ---
        dispatch_repos=None,
        worktree_root: Path | None = None,
        git_runner: GitRunner | None = None,
        claude_bin: str = "claude",
        claude_base_args: list[str] | None = None,
        full_agent: str | None = None,
        full_home: Path | None = None,
        mcp_config: str | None = None,
        child_path: str | None = None,
        gateway_base: str | None = None,
        gateway_token: str | None = None,
    ) -> None:
        self.host = host
        self.tmux_session = tmux_session
        self.sessions_root = Path(sessions_root) if sessions_root else _DEFAULT_SESSIONS_ROOT
        self._runner = runner or _default_runner
        self.poll_interval = poll_interval
        self.max_polls = max_polls
        # Dispatch config. The allowlist defaults to the SKCODE_DISPATCH_REPOS env
        # (comma list); unset => empty => DENY ALL. Never falls open.
        raw_allow = dispatch_repos if dispatch_repos is not None else os.environ.get(
            "SKCODE_DISPATCH_REPOS", "")
        self.dispatch_repos = parse_repo_allowlist(raw_allow)
        self.worktree_root = Path(worktree_root) if worktree_root else _DEFAULT_WORKTREE_ROOT
        self._git = git_runner or _default_git_runner
        self.claude_bin = claude_bin
        self.claude_base_args = list(claude_base_args) if claude_base_args else []
        # For the FULL profile only: the real operator identity + home. For SANDBOX
        # these are never wired (enforced by absence, not by a flag), see _build_env.
        self.full_agent = (full_agent or os.environ.get("SKAGENT") or "lumina").strip()
        self.full_home = Path(full_home) if full_home else Path.home()
        self.mcp_config = mcp_config
        self.child_path = child_path or _DEFAULT_CHILD_PATH
        # skgateway Anthropic-compat frontend: gateway models point `claude` here
        # (ANTHROPIC_BASE_URL) instead of the Anthropic cloud. Loopback skgateway
        # ignores the token (auth is off), but `claude` needs a non-empty auth
        # value set, so default one; both are overridable via env for a remote gw.
        self.gateway_base = gateway_base or os.environ.get(
            "SKCODE_GATEWAY_BASE", "http://localhost:18780")
        self.gateway_token = gateway_token or os.environ.get(
            "SKCODE_GATEWAY_TOKEN", "sk-local")
        # CR-6.2 C2 (inject blast radius): the set of session ids THIS daemon
        # actually spawned. inject is scoped to these by default so it can only
        # drive skcode-spawned sessions, never an arbitrary full-privilege agent
        # window (lumina/jarvis) that merely shares the `skchat-agents` tmux.
        self._spawned_sids: set[str] = set()
        # B2: for an interactive (resumable) session, the spawn context inject needs
        # to rebuild the `claude -p --resume` env + argv (profile/model/agent; the
        # worktree is derived from the sid). Populated only for interactive spawns,
        # so inject's destructive respawn-pane can ONLY target a session THIS daemon
        # started as resumable. In-memory (same lifetime as _spawned_sids).
        self._resume_ctx: dict[str, dict] = {}
        # C-13: sessions an operator has REFUSED via :meth:`deny`. The latch is
        # what makes a deny mean something after the fact: a refused session is
        # never resumed again by this daemon (:meth:`inject` checks it first), so
        # the refusal outlives the single moment the button was pressed. In-memory
        # (same lifetime as _spawned_sids), so a daemon restart forgets it exactly
        # as it forgets which sessions it spawned.
        self._denied_sids: set[str] = set()

    def capabilities(self) -> HarnessCapabilities:
        # Session plane over a PTY (tmux): reads + P1 inject/archive + P2 spawn.
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

    async def cancel(self, sid: str) -> dict:
        """Cancel a live session: kill its process group, then drop the window.

        A HARD stop (skcode C-6), not the graceful stop-and-persist of
        :meth:`archive`: no transcript is captured or written. Every tmux pane
        this harness spawns is its own session/process-group leader (`tmux
        new-window` execs the launch argv directly, no intervening shell, see
        :meth:`spawn`), so the pane's PID IS the process group id; sending the
        kill to the NEGATIVE pid reaches that leader AND every child process it
        started (a tool call, a background job), so nothing is left running as
        an orphan. The tmux window is then removed so the session drops off
        :meth:`list_sessions`.

        Idempotent + safe: an invalid sid, or a sid with no live tmux window
        (already ended / never running), returns a clean ``cancelled: False``
        no-op rather than raising, and never touches tmux or a process.
        """
        if not _SID_RE.match(sid):
            return {"sid": sid, "cancelled": False, "reason": "invalid session id"}

        live_ids = {s.sid for s in self._list_windows()}
        if sid not in live_ids:
            return {
                "sid": sid,
                "cancelled": False,
                "reason": "no live session (already ended or never running)",
            }

        target = f"{self.tmux_session}:{sid}"
        pid_out = self._runner(["tmux", "list-panes", "-t", target, "-F", "#{pane_pid}"])
        pid = ""
        stripped = (pid_out or "").strip()
        if stripped:
            pid = stripped.splitlines()[0].strip()
        if pid.isdigit():
            # Negative pid == kill(2) targets the whole process GROUP, not just
            # this one process, so a child the session started dies with it.
            self._runner(["kill", "-KILL", f"-{pid}"])
        self._runner(["tmux", "kill-window", "-t", target])
        return {"sid": sid, "cancelled": True}

    async def deny(self, sid: str) -> dict:
        """Refuse a session: stop what it is doing and stop it being resumed (C-13).

        This is the operator-refusal verb, and it is deliberately NOT built on
        :meth:`inject`. Two facts about this harness make an inject-based "Deny"
        a fiction: every session is launched with
        ``--dangerously-skip-permissions`` (so no permission prompt is ever
        waiting on the far side for a keystroke to answer), and inject is not a
        keystroke at all (it respawns the pane with ``claude -p --resume`` and
        the text as a NEW turn). Sending "n" through that path does not refuse
        anything; it asks the agent to do something with the letter n.

        A real refusal here is made of the two things this harness can actually
        actuate, and it reports both honestly:

        1. INTERRUPT the in-flight turn. SIGINT to the NEGATIVE pane pid reaches
           the whole process group (every pane this harness spawns is its own
           process-group leader, see :meth:`spawn`), so a tool call or child job
           the session started stops too. Unlike :meth:`cancel` this is not a
           SIGKILL and the tmux window survives (``remain-on-exit``), so the
           refusal stops the WORK without destroying the record of it.
        2. LATCH the refusal. The sid goes into ``_denied_sids``, and
           :meth:`inject` refuses a denied session from then on, so the session
           cannot be quietly resumed past the operator's "no".

        The result distinguishes REFUSED from COULD-NOT-REFUSE, which is the
        whole point of the card:

        * ``denied: False`` (+ a reason) for an invalid sid, a window this daemon
           did not spawn, or a session with no live window: nothing was refused
           and nothing is claimed. Never a raise, never a fake success.
        * ``denied: True`` with ``interrupted: True`` when a live turn was
           actually signalled, or ``interrupted: False`` when the turn had already
           finished (nothing in flight to stop) and only the latch took effect.

        Idempotent: denying an already-denied session returns ``denied: True``
        again (it is still refused) and re-interrupts only if something is
        somehow running again.
        """
        if not _SID_RE.match(sid):
            return {"sid": sid, "denied": False, "reason": "invalid session id"}

        # Same blast-radius gate as inject (CR-6.2 C2): a deny signals a process
        # group, so it must never reach an arbitrary window of the shared
        # `skchat-agents` tmux (a full-privilege lumina/jarvis runtime).
        if not self._inject_target_allowed(sid):
            return {
                "sid": sid,
                "denied": False,
                "reason": ("not a daemon-spawned session (deny is scoped to "
                           "sessions this daemon spawned)"),
            }

        live_ids = {s.sid for s in self._list_windows()}
        if sid not in live_ids:
            return {
                "sid": sid,
                "denied": False,
                "reason": "no live session (already ended or never running)",
            }

        target = f"{self.tmux_session}:{sid}"
        # pane_dead is tmux's own answer to "has this pane's process exited?" (the
        # window outlives it because spawn sets remain-on-exit). Ask BEFORE
        # signalling so "interrupted" reports what really happened rather than
        # whether a kill command was issued.
        pane_out = self._runner(
            ["tmux", "list-panes", "-t", target, "-F", "#{pane_pid} #{pane_dead}"])
        fields = ((pane_out or "").strip().splitlines() or [""])[0].split()
        pid = fields[0] if fields else ""
        dead = fields[1] if len(fields) > 1 else ""
        interrupted = False
        if pid.isdigit() and dead != "1":
            # Negative pid == kill(2) targets the process GROUP. SIGINT, not
            # SIGKILL: the turn is refused, the window and its scrollback stay.
            self._runner(["kill", "-INT", f"-{pid}"])
            interrupted = True

        self._denied_sids.add(sid)
        return {
            "sid": sid,
            "denied": True,
            "interrupted": interrupted,
            "reason": ("in-flight turn interrupted; session refused (not resumable)"
                       if interrupted else
                       "nothing in flight to interrupt; session refused (not resumable)"),
        }

    def _inject_target_allowed(self, sid: str) -> bool:
        """CR-6.2 C2: may inject reach ``sid``?

        By default inject is scoped to sessions THIS daemon spawned
        (``_spawned_sids``), so a caller holding a ``skcode.inject`` token can NOT
        drive keystrokes into an arbitrary window of the shared ``skchat-agents``
        tmux (e.g. a full-privilege lumina/jarvis runtime). The documented escape
        hatch ``SKCODE_INJECT_ANY_WINDOW`` (default OFF) restores the old
        any-live-window behavior for a deliberate operator case.
        """
        if _env_truthy("SKCODE_INJECT_ANY_WINDOW"):
            return True
        return sid in self._spawned_sids

    async def inject(self, sid: str, text: str) -> dict:
        """Inject an operator follow-up into a running session (P1, B2 resume).

        The session runs headless stream-json, so a follow-up is delivered as a
        fresh ``claude -p --resume <session_id>`` turn RESPAWNED in the same tmux
        pane, NOT raw keystrokes into a TUI (claude does not read a pty stdin under
        ``--print``). The resumed turn's structured events append to the same
        `.skcode/stream.jsonl`, so :meth:`stream` surfaces them with no client
        change. This is a WRITE verb; the daemon route stays behind the capauth gate.

        Gated by CONSTRUCTION:
        - CR-6.2 C2: only a session THIS daemon spawned
          (:meth:`_inject_target_allowed`).
        - B2: only a session recorded as RESUMABLE (``_resume_ctx``, interactive
          spawns only), because ``respawn-pane -k`` is DESTRUCTIVE (it replaces the
          pane's process). So inject can never kill+respawn an arbitrary window,
          even past the C2 escape hatch.

        Idempotent + safe: an invalid sid, a non-daemon-spawned / non-resumable
        target, a sid with no live window, or a session whose turn 1 has not emitted
        its init event yet (no session_id) each return a clean ``injected: False``
        no-op and NEVER respawn anything.
        """
        if not _SID_RE.match(sid):
            return {"sid": sid, "injected": False, "reason": "invalid session id"}

        # C-13: a session an operator DENIED is not resumable. Checked first and
        # by construction, so the refusal is enforced here rather than being a
        # thing the UI is trusted to remember.
        if sid in self._denied_sids:
            return {
                "sid": sid,
                "injected": False,
                "reason": "session was denied by an operator (refused; not resumable)",
            }

        if not self._inject_target_allowed(sid):
            return {
                "sid": sid,
                "injected": False,
                "reason": ("not a daemon-spawned session (inject is scoped to "
                           "sessions this daemon spawned)"),
            }

        live_ids = {s.sid for s in self._list_windows()}
        if sid not in live_ids:
            return {
                "sid": sid,
                "injected": False,
                "reason": "no live session (already archived or never running)",
            }

        ctx = self._resume_ctx.get(sid)
        if not ctx:
            return {
                "sid": sid,
                "injected": False,
                "reason": ("not a resumable session (only interactive sessions this "
                           "daemon spawned can be injected)"),
            }

        session_id = self._read_session_id(sid)
        if not session_id:
            return {
                "sid": sid,
                "injected": False,
                "reason": "session not ready (turn 1 has not emitted its init event yet)",
            }

        worktree = self.worktree_root / sid
        env = self._build_env(ctx["profile"], ctx["agent"], worktree, ctx["model"])
        resume = self._resume_argv(ctx["profile"], ctx["model"], session_id, text)
        env_argv = ["env", "-i", *[f"{k}={v}" for k, v in env.items()]]
        target = f"{self.tmux_session}:{sid}"
        # Respawn the pane with the resume turn. respawn-pane execs the argv after
        # '--' DIRECTLY (no sh -c), same as new-window; -k replaces the prior
        # (usually exited) one-shot process; -c scopes cwd to the worktree. The
        # message is a distinct argv element, never shell-parsed; the sid is
        # charset-validated above.
        self._runner([
            "tmux", "respawn-pane", "-k", "-c", str(worktree), "-t", target,
            "--", *env_argv, *resume,
        ])
        # Re-attach pipe-pane so the resumed turn's JSONL appends to the same log.
        # NO -o here: `-o` is a TOGGLE ("open only if none exists"), and respawn
        # preserves the spawn-time pipe, so `-o` would CLOSE it and the resumed
        # turn's output would never reach the file. Plain pipe-pane unconditionally
        # (re)opens the pipe; `cat >>` keeps appending to the same log.
        self._runner([
            "tmux", "pipe-pane", "-t", target,
            f"cat >> '{self._stream_log_path(sid)}'",
        ])
        return {"sid": sid, "injected": True}

    # --- spawn: start a NEW session (the Dispatch unlock, skcode P2) -----------

    def _branch_ok(self, branch: str) -> bool:
        """Validate a branch name via ``git check-ref-format --branch`` (argv).

        This is git's own ref-name validator, so it rejects everything git would
        (leading '-', '..', control chars, trailing '.lock', etc.). Runs through the
        injectable git runner (never a shell), and returns True only on rc == 0.
        """
        if not branch or not isinstance(branch, str):
            return False
        try:
            r = self._git(["git", "check-ref-format", "--branch", branch])
        except Exception:
            return False
        return getattr(r, "returncode", 1) == 0

    def _build_env(self, profile: str, agent: str, worktree: Path,
                   model: str | None = None) -> dict[str, str]:
        """Construct the child's ENTIRE environment for a profile (spec 6.2).

        Enforcement is by CONSTRUCTION, not by a flag: the returned dict is the
        complete environment the child gets (it is handed to ``env -i`` so nothing
        is inherited). The SANDBOX profile therefore has NO ``SKAGENT`` and no sk*
        wiring at all, and its ``HOME`` points at the worktree so ``~/.skcapstone``
        resolves into the (empty) worktree rather than the real agent home. The FULL
        profile wires ``SKAGENT`` and the real ``HOME``. A bug that flipped a flag
        could never leak Lumina's identity/memory into a sandbox, because the
        sandbox env simply does not contain it.
        """
        env = {
            "PATH": self.child_path,
            "TERM": "xterm-256color",
            "LANG": os.environ.get("LANG", "C.UTF-8"),
        }
        # Model ACCESS (not operator identity): a spawned claude session needs a
        # credential to reach the model. This is model-access only, NOT SKAGENT /
        # memory / MCP, so it is compatible with the sandbox's no-operator-context
        # rule and is required for the session to actually run.
        if is_gateway_model(model):
            # Cloud-free path: point `claude` at skgateway's Anthropic /v1/messages
            # frontend, which routes to a local model (ornith / chiap08). DROP the
            # cloud OAuth token so claude uses the gateway base URL rather than
            # preferring the Anthropic subscription path. The gateway token is
            # model-access only (same category as the OAuth token), so it stays
            # sandbox-compatible.
            env["ANTHROPIC_BASE_URL"] = self.gateway_base
            env["ANTHROPIC_AUTH_TOKEN"] = self.gateway_token
            # A gateway model id (sk-default / qwen3.8-27b-huihui-abliterated-q4_k_m)
            # ships a context-window for, so it otherwise prints a noisy
            # "not a model this version recognizes" warning into the transcript
            # and assumes a 200k window. Suppress the enforcement so the session
            # output stays clean; skgateway owns the real routing + limits.
            env["CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT"] = "1"
        else:
            # Anthropic cloud path: CLAUDE_CODE_OAUTH_TOKEN is subscription
            # model-access. CR-6.2 C1: it is passed to the trusted ``full`` profile
            # when present, but WITHHELD from the untrusted ``sandbox`` profile by
            # default so a prompt-injected sandbox agent can never read the
            # operator's real Anthropic token out of its env. The only way to put
            # it back into a sandbox is the explicit, documented opt-in
            # (SKCODE_SANDBOX_ALLOW_CLOUD_TOKEN); prefer gateway/Ornith models,
            # which drop the cloud token entirely (see the branch above).
            _oauth = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
            _cloud_ok = profile == "full" or sandbox_cloud_token_allowed()
            if _oauth and _cloud_ok:
                env["CLAUDE_CODE_OAUTH_TOKEN"] = _oauth
        if profile == "full":
            # Trusted interactive session: real operator identity + home wired.
            env["HOME"] = str(self.full_home)
            env["SKAGENT"] = agent
        else:
            # SANDBOX: no SKAGENT, no sk* MCP, HOME scoped to the worktree so no
            # home-dir sk* path (~/.skcapstone/...) is reachable.
            env["HOME"] = str(worktree)
        return env

    def _claude_argv(self, profile: str, prompt: str, model: str) -> list[str]:
        """Build the headless ``claude`` launch argv for a NEW session (turn 1).

        Both session modes launch identically (B2): ``claude -p
        --dangerously-skip-permissions --output-format stream-json --verbose
        [--mcp-config ...] --model <m> <prompt>``. Print mode (-p) runs the prompt
        and skips the first-run onboarding wizard; ``--output-format stream-json
        --verbose`` emits newline-delimited JSON events (parsed by
        :func:`parse_stream_json_line`) instead of a rendered TUI. The mode
        difference is downstream only: a DIRECT session is one-shot, an INTERACTIVE
        session is resumable via :meth:`inject` (``claude -p --resume``).

        The prompt is a distinct argv element (DATA), never interpolated into a
        shell string. The sk* MCP config is added ONLY for the full profile;
        ``--model`` is ALWAYS passed, mapped via :func:`map_model`.
        """
        argv = [self.claude_bin, "-p", "--dangerously-skip-permissions",
                "--output-format", "stream-json", "--verbose",
                *self.claude_base_args]
        if profile == "full" and self.mcp_config:
            argv += ["--mcp-config", str(self.mcp_config)]
        argv += ["--model", map_model(model)]
        # Prompt LAST, as its own argv value. tmux new-window '--' execs the argv
        # directly (verified: no sh -c), so the prompt is never shell-parsed.
        argv += [str(prompt or "")]
        return argv

    def _resume_argv(self, profile: str, model: str, session_id: str,
                     text: str) -> list[str]:
        """Build the ``claude -p --resume`` argv for an inject follow-up (B2).

        Same headless flags as :meth:`_claude_argv`, plus ``--resume <session_id>``
        to continue the prior conversation, with the operator message as the LAST
        argv element (DATA, never shell-parsed). ``session_id`` is a claude-issued
        UUID read from the session's own stream-json init event.
        """
        argv = [self.claude_bin, "-p", "--resume", str(session_id),
                "--dangerously-skip-permissions",
                "--output-format", "stream-json", "--verbose",
                *self.claude_base_args]
        if profile == "full" and self.mcp_config:
            argv += ["--mcp-config", str(self.mcp_config)]
        argv += ["--model", map_model(model)]
        argv += [str(text or "")]
        return argv

    async def spawn(self, desc: SessionDescriptor, *, prompt: str) -> HarnessSession:
        """Start a NEW claude-code session in an isolated worktree + tmux window.

        Every RCE input guard (spec 7.3) runs BEFORE any subprocess, and each fails
        CLOSED with :class:`SpawnRejected` (never a silent proceed):

        1. profile is one of {sandbox, full};
        2. repo is on the per-host allowlist (canonical-path match; empty allowlist
           denies all);
        3. branch passes ``git check-ref-format --branch``;
        4. the composed ``<agent>-<id>`` name matches ``[A-Za-z0-9_-]+``.

        Only then does it ``git worktree add`` the repo+branch under the scoped
        worktree root and open a tmux window running ``claude`` with a
        construction-enforced profile environment (``env -i`` + the exact env from
        :meth:`_build_env`). All subprocess calls are argv lists (never shell). The
        returned :class:`HarnessSession` carries the sid = the tmux window name, so
        the new session shows up in :meth:`list_sessions`.
        """
        profile = (desc.quality or "sandbox").strip().lower()
        if profile not in ("sandbox", "full"):
            raise SpawnRejected(f"invalid profile {profile!r} (want 'sandbox' or 'full')")

        # Session mode: "direct" (print/one-shot) or "interactive" (stays open for
        # injected follow-ups). Fail closed on an unknown mode (never proceed).
        mode = (desc.mode or "direct").strip().lower()
        if mode not in ("direct", "interactive"):
            raise SpawnRejected(f"invalid mode {mode!r} (want 'direct' or 'interactive')")

        # 2. repo is OPTIONAL. WITH a repo: it must be on the allowlist and the
        #    session runs in an isolated worktree off the validated base branch.
        #    WITHOUT a repo: a repo-less scratch session (fast/direct model work)
        #    in an isolated empty scratch dir with no repo access, so the repo
        #    allowlist gate does not apply. The auth + authz + sandbox gates still
        #    apply in BOTH cases (this never widens who may dispatch).
        repo = (desc.repo or "").strip()
        repo_real = ""
        branch = ""
        if repo:
            if not self.dispatch_repos:
                raise SpawnRejected(
                    "repo allowlist is empty (SKCODE_DISPATCH_REPOS unset): deny all")
            repo_real = os.path.realpath(os.path.expanduser(repo))
            if repo_real not in self.dispatch_repos:
                raise SpawnRejected(f"repo {repo!r} is not on the dispatch allowlist")
            # 3. branch via git's own validator (only meaningful with a repo).
            branch = (desc.branch or "main").strip()
            if not self._branch_ok(branch):
                raise SpawnRejected(f"branch {branch!r} failed git check-ref-format")

        # 4. name charset. The agent prefix is the real identity for FULL and a
        #    fixed 'sandbox' for SANDBOX (never the real identity), plus a random id.
        agent = self.full_agent if profile == "full" else "sandbox"
        if not _SID_RE.match(agent):
            raise SpawnRejected(f"agent name {agent!r} breaks the [A-Za-z0-9_-]+ charset")
        sid = f"{agent}-{secrets.token_hex(4)}"
        if not _SID_RE.match(sid):
            raise SpawnRejected(f"session name {sid!r} breaks the [A-Za-z0-9_-]+ charset")

        # --- all guards passed: now (and only now) touch the machine ---
        worktree = self.worktree_root / sid
        try:
            self.worktree_root.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        if repo_real:
            # Each repo session gets its OWN fresh branch off the requested base via
            # `worktree add -b`: dispatching onto `main` (already checked out in the
            # primary repo) does not conflict, and each session stays isolated on its
            # own branch (the autocode-worktree pattern). Base branch validated above;
            # the new branch name is charset-safe by construction (`skcode/<sid>`).
            session_branch = f"skcode/{sid}"
            wt = self._git(
                ["git", "-C", repo_real, "worktree", "add", "-b", session_branch, str(worktree), branch]
            )
            if getattr(wt, "returncode", 1) != 0:
                raise SpawnRejected(
                    f"git worktree add failed: {getattr(wt, 'stderr', '') or 'unknown error'}")
        else:
            # Repo-less scratch session (fast/direct model work): an isolated empty
            # dir, no repo access. HOME points here (env -i), so nothing leaks.
            try:
                worktree.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise SpawnRejected(f"scratch dir create failed: {exc}")

        # B2: both modes launch headless stream-json (-p skips onboarding), so no
        # ~/.claude.json seed is written for either. The mode difference is only
        # that interactive is resumable via inject.
        env = self._build_env(profile, agent, worktree, desc.model)
        launch = self._claude_argv(profile, prompt, desc.model)
        env_argv = ["env", "-i", *[f"{k}={v}" for k, v in env.items()]]
        # EVERY session emits stream-json; make the capture dir now so the pipe-pane
        # `cat >>` (attached below) can write the JSONL into it. Its existence is
        # ALSO the signal stream() uses to parse structurally vs screen-scrape.
        stream_log = self._stream_log_path(sid)
        try:
            stream_log.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        # Ensure the target tmux session exists (a fresh host has no tmux server,
        # so `new-window -t skchat-agents` would silently fail and the session
        # would never appear in list_sessions). `new-session -d` creates it if
        # absent; if it already exists tmux errors harmlessly and we ignore it.
        # argv-only.
        self._runner(["tmux", "new-session", "-d", "-s", self.tmux_session])
        # tmux new-window with the command AFTER '--' as separate argv elements is
        # exec'd DIRECTLY by tmux (no sh -c), so neither the env pairs nor the
        # prompt are ever shell-interpreted. The sid is charset-validated above.
        self._runner([
            "tmux", "new-window", "-t", self.tmux_session, "-n", sid,
            "-c", str(worktree), "--", *env_argv, *launch,
        ])
        # Keep the pane after the print-mode command exits so its output stays
        # viewable + listable instead of the window vanishing the instant claude
        # finishes. Set immediately (well within the multi-second model call).
        self._runner([
            "tmux", "set-window-option", "-t", f"{self.tmux_session}:{sid}",
            "remain-on-exit", "on",
        ])
        # Copy the pane's output (the stream-json JSONL) to the capture file so
        # stream() can tail + parse it. pipe-pane runs its command via /bin/sh, but
        # the command is a FIXED `cat >> '<path>'`: the path is worktree_root (fixed
        # config) / sid (charset-validated [A-Za-z0-9_-]+) / .skcode/stream.jsonl,
        # so it contains no shell metacharacters and nothing operator-supplied. The
        # claude launch itself stays shell-free (exec'd directly by new-window
        # above); only this side-channel copy uses sh, with a path this code owns.
        self._runner([
            "tmux", "pipe-pane", "-o", "-t", f"{self.tmux_session}:{sid}",
            f"cat >> '{stream_log}'",
        ])

        # CR-6.2 C2: remember this is a daemon-spawned session so inject may reach
        # it (and ONLY it, plus its siblings). Recorded after the window is created.
        self._spawned_sids.add(sid)
        # B2: an interactive session is resumable; record what inject needs to
        # rebuild the `claude -p --resume` env + argv. Direct sessions are one-shot
        # and get NO resume context, so inject's destructive respawn cannot touch
        # them (or any window this daemon did not start as resumable).
        if mode == "interactive":
            self._resume_ctx[sid] = {
                "profile": profile, "model": desc.model, "agent": agent,
            }

        return HarnessSession(
            sid=sid,
            descriptor=SessionDescriptor(
                sid=sid, host=self.host, harness=self.name, repo=repo_real,
                branch=branch, model=desc.model, state="running", quality=profile,
                permission_mode=desc.permission_mode, mode=mode,
            ),
            status="running",
            branch=branch,
        )

    def _stream_log_path(self, sid: str) -> Path:
        """The structured stream-json capture file for a DIRECT session:
        ``<worktree_root>/<sid>/.skcode/stream.jsonl``. Deterministic from the sid
        (worktree = worktree_root / sid), so :meth:`stream` can find it without any
        spawn-time state. The parent ``.skcode`` dir's existence is the signal that
        a session is structured (spawn creates it only for direct mode)."""
        return self.worktree_root / sid / ".skcode" / "stream.jsonl"

    def _read_session_id(self, sid: str) -> str | None:
        """Return the LATEST ``system/init`` session_id from the session's
        stream.jsonl, or None if there is no init event yet (turn 1 not started /
        not far enough) or no file.

        inject ``--resume`` chains from the MOST RECENT turn: a resumed turn emits a
        fresh init, so the last init's session_id carries all prior turns. Fail soft
        (blank / non-JSON lines are skipped, never raise)."""
        latest = None
        try:
            with open(self._stream_log_path(sid), "r", encoding="utf-8",
                      errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if (isinstance(obj, dict) and obj.get("type") == "system"
                            and obj.get("subtype") == "init"
                            and obj.get("session_id")):
                        latest = obj["session_id"]
        except FileNotFoundError:
            return None
        return latest

    async def stream(self, sid: str) -> AsyncIterator[SessionEvent]:
        now = time.time()
        if not _SID_RE.match(sid):
            yield SessionEvent(type=EventType.STATUS, text="invalid session id", ts=now)
            return
        yield SessionEvent(type=EventType.STATUS, text="attached", ts=now)
        # A DIRECT session captures structured stream-json to .skcode/stream.jsonl;
        # its .skcode dir existing means "parse events, do not screen-scrape". A
        # legacy / interactive session has no such dir and keeps the capture-pane
        # path below.
        if self._stream_log_path(sid).parent.exists():
            async for ev in self._stream_structured(sid):
                yield ev
            return
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

    async def _stream_structured(self, sid: str) -> AsyncIterator[SessionEvent]:
        """Tail ``.skcode/stream.jsonl`` and yield typed events (Task 3-B1).

        Reads only COMPLETE newline-terminated lines each poll and buffers any
        partial trailing line (the writer may be mid-flush), the same discipline
        as the capture-pane tail. Each complete line is parsed with
        :func:`parse_stream_json_line` (fail-soft: terminal noise / partial JSON
        yield no events, never an exception). The file may not exist for the first
        few polls (pipe-pane creates it on first output); that is treated as empty
        until it appears. Read position is tracked by byte offset so growth is
        picked up incrementally.
        """
        log = self._stream_log_path(sid)
        offset = 0
        buf = ""
        polls = 0
        while self.max_polls is None or polls < self.max_polls:
            try:
                with open(log, "r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(offset)
                    chunk = fh.read()
                    offset = fh.tell()
            except FileNotFoundError:
                chunk = ""
            if chunk:
                buf += chunk
                # Keep the last (possibly partial) line in the buffer; only parse
                # up to the final newline.
                lines = buf.split("\n")
                buf = lines.pop()
                for line in lines:
                    for ev in parse_stream_json_line(line, ts=time.time()):
                        yield ev
            polls += 1
            if self.max_polls is not None and polls >= self.max_polls:
                break
            await asyncio.sleep(self.poll_interval)
