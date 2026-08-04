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


#: The onboarding version stamped into the seeded ~/.claude.json for interactive
#: sessions. It only needs to be a version claude accepts as "already onboarded"
#: so the first-run wizard is skipped; the exact value is not load-bearing.
_SEED_ONBOARDING_VERSION = "2.1.119"

#: Compose-form model ids that route THROUGH skgateway (cloud-free) rather than
#: to the Anthropic cloud. skgateway now exposes an Anthropic-compat
#: ``/v1/messages`` frontend, so the SAME ``claude`` runner can reach these by
#: pointing ANTHROPIC_BASE_URL at the gateway (see :meth:`_build_env`). skgateway
#: routes ``sk-default`` (registry role) to local ornith and ``ornith-big`` to the
#: chiap08 35B backend.
_GATEWAY_MODELS = {"sk-default", "ornith-big"}

#: Map the compose-form model ids onto the values passed to ``claude --model``.
#: For Anthropic ids this is the concrete ``claude`` alias (sonnet/opus). For
#: gateway ids it is the model NAME skgateway routes on (passed verbatim to the
#: gateway's Anthropic frontend). --model is ALWAYS passed; unknown ids fall
#: closed to a safe concrete Anthropic model.
_MODEL_MAP = {
    "claude-sonnet-5": "sonnet",
    "claude-opus-4-8": "opus",
    "sk-default": "sk-default",   # skgateway registry role -> ornith (cloud-free)
    "ornith-big": "ornith-big",   # skgateway -> chiap08 ornith 35B (cloud-free)
}


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
        else:
            # Anthropic cloud path: CLAUDE_CODE_OAUTH_TOKEN is subscription
            # model-access, passed to both profiles when present in the host env.
            _oauth = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
            if _oauth:
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

    def _claude_argv(self, profile: str, prompt: str, mode: str, model: str) -> list[str]:
        """Build the ``claude`` launch argv for a session ``mode``.

        The prompt is a distinct argv element (DATA), never interpolated into a
        shell string. The sk* MCP config is added ONLY for the full profile; the
        sandbox profile never references it. ``--model`` is ALWAYS passed, mapped
        via :func:`map_model`.

        DIRECT mode (default): ``claude -p --dangerously-skip-permissions ... --model <m> <prompt>``.
        Print mode (-p) runs the prompt non-interactively and produces output,
        skipping the first-run onboarding wizard that would otherwise block a
        fresh-HOME sandbox forever. --dangerously-skip-permissions lets the
        session act without per-action approval; it is confined to the sandbox
        worktree / scratch dir and the no-operator-context env.

        INTERACTIVE mode: ``claude ... --model <m> <prompt>`` (NO -p, NO
        --dangerously-skip-permissions). The prompt runs, then the TUI STAYS OPEN
        in manual mode ready for injected follow-ups (the inject path). Interactive
        runs WITHOUT --dangerously-skip-permissions on purpose: claude asks
        per-action permission (safer, not less safe). The first-run dialogs are
        skipped instead by the seeded ~/.claude.json spawn writes into the worktree
        HOME (see :meth:`_write_interactive_seed`).
        """
        if mode == "interactive":
            argv = [self.claude_bin, *self.claude_base_args]
        else:
            argv = [self.claude_bin, "-p", "--dangerously-skip-permissions", *self.claude_base_args]
        if profile == "full" and self.mcp_config:
            argv += ["--mcp-config", str(self.mcp_config)]
        argv += ["--model", map_model(model)]
        # Prompt LAST, as its own argv value. tmux new-window '--' execs the argv
        # directly (verified: no sh -c), so the prompt is never shell-parsed.
        argv += [str(prompt or "")]
        return argv

    def _write_interactive_seed(self, worktree: Path) -> Path:
        """Seed ``<worktree>/.claude.json`` so an interactive session skips ALL
        first-run dialogs (onboarding, trust-folder, bypass-warning).

        The child's HOME is the worktree (``env -i`` scopes it there), so claude
        reads this file as ``~/.claude.json``. The ``projects`` key MUST be keyed
        by the absolute worktree path (the child's HOME + cwd) for the trust dialog
        to be pre-accepted. Interactive mode has no ``-p`` to auto-skip the wizard,
        so this seed is what lets a fresh HOME launch straight into the session.
        """
        wt = str(worktree)
        seed = {
            "hasCompletedOnboarding": True,
            "lastOnboardingVersion": _SEED_ONBOARDING_VERSION,
            "theme": "dark",
            "numStartups": 5,
            "bypassPermissionsModeAccepted": True,
            "projects": {
                wt: {
                    "hasTrustDialogAccepted": True,
                    "hasCompletedProjectOnboarding": True,
                    "allowedTools": [],
                }
            },
        }
        # The worktree dir already exists after `git worktree add` / the scratch
        # mkdir; ensure it anyway so seeding never races the dir into existence.
        worktree.mkdir(parents=True, exist_ok=True)
        path = worktree / ".claude.json"
        path.write_text(json.dumps(seed, indent=2))
        return path

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

        # Interactive mode has no -p to auto-skip the first-run wizard, so seed a
        # ~/.claude.json (keyed by the worktree = the child's HOME) that pre-accepts
        # every first-run dialog. Direct mode's -p already skips onboarding, so no
        # seed is written there.
        if mode == "interactive":
            self._write_interactive_seed(worktree)
        env = self._build_env(profile, agent, worktree, desc.model)
        launch = self._claude_argv(profile, prompt, mode, desc.model)
        env_argv = ["env", "-i", *[f"{k}={v}" for k, v in env.items()]]
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
