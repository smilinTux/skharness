"""Pi session-plane harness over the existing guarded tmux spawn path.

Pi's task-plane adapter lives in :mod:`skharness.autocode.adapters.pi`; this is
intentionally a separate, long-lived session-plane implementation.  The routing
recipe is the one proven by that adapter: Pi does not honour
``OPENAI_BASE_URL``, so every spawn receives a private ``PI_CODING_AGENT_DIR``
containing ``models.json`` with an ``openai-completions`` ``skgw`` provider,
``supportsDeveloperRole=false``, and provider-level attribution headers.  Pi is
launched with ``--model skgw/<model>``, ``--api-key``, ``--no-session`` and JSON
mode, whose assistant ``message_end`` content is parsed below.

The security-sensitive lifecycle is inherited, not copied, from
:class:`ClaudeCodeHarness`.  In particular, ``spawn`` is the exact existing
implementation and therefore runs the four fail-closed guards in its established
order (profile, allowlisted canonical repo, git ref validation, session-name
regex), while ``archive`` captures and durably writes the transcript before it
stops the tmux window.  This subclass only replaces Pi-specific configuration,
argv, listing metadata, and structured-event parsing.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import AsyncIterator

from skharness.events import EventType, SessionEvent
from skharness.harness import BackgroundTask, HarnessCapabilities, SessionDescriptor
from skharness.harnesses.claude_code import ClaudeCodeHarness, parse_windows

_HARNESS = "pi"
_DEFAULT_MODEL = "sk-codex"
_DEFAULT_MAX_TOKENS = 131072
_LOCAL_API_KEY = "sk-local"


def parse_pi_json_line(line: str, *, ts: float = 0.0) -> list[SessionEvent]:
    """Parse one line from ``pi --mode json`` without trusting terminal noise.

    Pi's final model reply is an assistant ``message_end`` event.  Its text
    blocks are joined into one assistant event so callers see the exact answer,
    including a JSON answer when the prompt requested one.  Blank, malformed,
    non-object, non-assistant, and content-free events fail soft as ``[]``.
    """
    value = (line or "").strip()
    if not value:
        return []
    try:
        event = json.loads(value)
    except (TypeError, ValueError):
        return []
    if not isinstance(event, dict) or event.get("type") != "message_end":
        return []
    message = event.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    text = "".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    )
    if not text:
        return []
    return [SessionEvent(type=EventType.ASSISTANT_TEXT, text=text, ts=ts)]


class PiHarness(ClaudeCodeHarness):
    """Session-plane Pi harness injected directly into ``PoolController``.

    ``ClaudeCodeHarness.spawn`` and ``ClaudeCodeHarness.archive`` are deliberately
    not overridden.  That is the reuse boundary which keeps one implementation of
    the spawn guards and transcript-first teardown rather than creating another
    subtly different copy for Pi.
    """

    name = _HARNESS

    def __init__(
        self,
        *,
        pi_bin: str = "pi",
        default_model: str = _DEFAULT_MODEL,
        gateway_base: str | None = None,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.pi_bin = pi_bin
        self.default_model = self._model_name(default_model)
        self.pi_gateway_base = (
            gateway_base or os.environ.get("SKCODE_PI_GATEWAY_BASE", "")
        ).strip()
        if not self.pi_gateway_base:
            raise ValueError(
                "PiHarness gateway route is required: pass gateway_base or set "
                "SKCODE_PI_GATEWAY_BASE"
            )
        self.max_tokens = int(max_tokens)

    @staticmethod
    def _model_name(model: str | None) -> str:
        value = (model or _DEFAULT_MODEL).strip()
        return value.removeprefix("skgw/") or _DEFAULT_MODEL

    def capabilities(self) -> HarnessCapabilities:
        return {
            "session_resume": False,
            "structured_output": "json",
            "sandbox": False,
            "tool_restrictions": False,
            "task_plane": False,
            "session_plane": True,
            "headless_api": "pty",
            "hot_set_model": False,
        }

    def _list_windows(self) -> list[SessionDescriptor]:
        out = self._runner(
            [
                "tmux",
                "list-windows",
                "-t",
                self.tmux_session,
                "-F",
                "#{window_name}\t#{window_activity}",
            ]
        )
        sessions = parse_windows(out, host=self.host)
        for session in sessions:
            session.harness = self.name
        return sessions

    def _historical_sessions(self, *, limit: int = 20) -> list[SessionDescriptor]:
        """Read only archives produced by this harness from the shared root."""
        found: list[SessionDescriptor] = []
        if not self.sessions_root.exists():
            return found
        for path in self.sessions_root.glob("*/sessions/*.json"):
            try:
                record = json.loads(path.read_text())
                if not isinstance(record, dict) or record.get("harness") != self.name:
                    continue
                sid = str(record["sid"])
                found.append(
                    SessionDescriptor(
                        sid=sid,
                        host=str(record.get("host") or self.host),
                        harness=self.name,
                        state="ended",
                        last_activity=float(record.get("archived_at") or path.stat().st_mtime),
                    )
                )
            except (KeyError, OSError, TypeError, ValueError):
                continue
        found.sort(key=lambda session: session.last_activity, reverse=True)
        return found[:limit]

    async def list_sessions(self) -> list[SessionDescriptor]:
        return self._list_windows() + self._historical_sessions()

    def _build_env(
        self, profile: str, agent: str, worktree: Path, model: str | None = None,
        card_id: str = "",
    ) -> dict[str, str]:
        """Build the complete ``env -i`` environment and private Pi config.

        The provider headers are literal validated values already accepted by the
        inherited session-name guard: ``x-agent-id`` names the full operator or
        the fixed sandbox principal, and ``x-session-id`` is the tmux sid.  Pi's
        ``!``/``$`` magic header prefixes are therefore unreachable.
        """
        sid = worktree.name
        model_name = self._model_name(model or self.default_model)
        config_dir = worktree / ".pi-coding-agent"
        config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        config = {
            "providers": {
                "skgw": {
                    "baseUrl": self.pi_gateway_base,
                    "api": "openai-completions",
                    # The deployed local route uses Pi's required non-secret
                    # placeholder. Never persist a caller token in the worktree.
                    "apiKey": _LOCAL_API_KEY,
                    "compat": {"supportsDeveloperRole": False},
                    "headers": {
                        "x-agent-id": agent,
                        "x-session-id": sid,
                        **({"x-sk-card-id": card_id} if card_id else {}),
                    },
                    "models": [
                        {
                            "id": model_name,
                            "limit": {
                                "context": self.max_tokens,
                                "output": self.max_tokens,
                            },
                        }
                    ],
                }
            }
        }
        config_path = config_dir / "models.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        config_path.chmod(0o600)

        env = {
            "PATH": self.child_path,
            "TERM": "xterm-256color",
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "PI_CODING_AGENT_DIR": str(config_dir),
        }
        if profile == "full":
            env["HOME"] = str(self.full_home)
            env["SKAGENT"] = agent
        else:
            env["HOME"] = str(worktree)
        # Deliberately no OPENAI_BASE_URL: Pi ignores it. models.json is the route.
        return env

    def _claude_argv(self, profile: str, prompt: str, model: str) -> list[str]:
        """Pi launch hook used by the inherited guarded ``spawn`` implementation."""
        del profile
        model_name = self._model_name(model or self.default_model)
        return [
            self.pi_bin,
            "-p",
            str(prompt or ""),
            "--mode",
            "json",
            "--no-session",
            "--approve",
            "--model",
            f"skgw/{model_name}",
            "--api-key",
            _LOCAL_API_KEY,
        ]

    async def _stream_structured(self, sid: str) -> AsyncIterator[SessionEvent]:
        """Tail Pi's JSONL capture and parse complete assistant message events."""
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
                lines = buf.split("\n")
                buf = lines.pop()
                for line in lines:
                    for event in parse_pi_json_line(line, ts=time.time()):
                        yield event
            polls += 1
            if self.max_polls is not None and polls >= self.max_polls:
                break
            await asyncio.sleep(self.poll_interval)

    async def inject(self, sid: str, text: str) -> dict:
        """Pi is launched ``--no-session``; follow-up injection is unsupported."""
        del text
        return {
            "sid": sid,
            "injected": False,
            "reason": "PiHarness uses --no-session and cannot resume a turn",
        }

    async def get_branch(self, sid: str) -> str:
        for session in await self.list_sessions():
            if session.sid == sid:
                return session.branch
        return ""

    async def background_tasks(self, sid: str) -> list[BackgroundTask]:
        del sid
        return []


__all__ = ["PiHarness", "parse_pi_json_line"]
