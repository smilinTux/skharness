"""ClaudeCodeAdapter on the shared BaseCliAdapter + Docker Sandbox. Keeps the
deny-by-default tool firewall (fail closed) from claude_code.py; the spawn is the
harness-agnostic Sandbox."""
from __future__ import annotations

import json
from pathlib import Path

from ..claude_code import ForbiddenToolError, is_forbidden
from ..sandbox import AuthMount, Sandbox
from .base import BaseCliAdapter, extract_json

_CRED_PATH = "~/.claude/.credentials.json"


def _oauth_token(cred_path: str | None = None) -> str | None:
    """The Claude OAuth access token from the host credential file, or None when
    it is absent/unreadable/unparseable. The sandboxed `claude` CLI authenticates
    from CLAUDE_CODE_OAUTH_TOKEN in the environment; the bare .credentials.json
    mount is NOT read by the CLI in the sandbox image (it reports 'Not logged in'),
    so the token is injected as env instead. Fail soft: None -> no token env.

    ``cred_path`` defaults to the module-level ``_CRED_PATH`` resolved at call time
    (not bound as a default arg), so the path stays overridable in tests."""
    try:
        raw = Path(cred_path or _CRED_PATH).expanduser().read_text()
        tok = json.loads(raw).get("claudeAiOauth", {}).get("accessToken")
    except (OSError, ValueError):
        return None
    return tok or None


class ClaudeCodeAdapter(BaseCliAdapter):
    name = "claude-code"

    def __init__(self, allowed_tools, mcp_endpoints=None, live_execution: bool = False,
                 sandbox=None, image=None, max_turns: int = 50):
        for t in allowed_tools:
            if is_forbidden(t):
                raise ForbiddenToolError(
                    f"tool {t!r} is denied by the autopilot firewall (fail closed)")
        self.allowed_tools = list(allowed_tools)
        self.image = image or "sandbox-claude:1"
        # cap the agentic loop so a round returns within the sandbox run_timeout
        self.max_turns = int(max_turns)
        egress = list(mcp_endpoints or [])
        if "api.anthropic.com" not in egress:
            egress.append("api.anthropic.com")
        super().__init__(sandbox or Sandbox(live_execution=live_execution),
                         egress_hosts=egress, live_execution=live_execution)

    def capabilities(self):
        return {"session_resume": True, "structured_output": "json",
                "sandbox": True, "tool_restrictions": True,
                "task_plane": True, "session_plane": False,
                "headless_api": "none", "hot_set_model": False}

    def _argv(self, prompt: str) -> list[str]:
        # Bound the agentic loop. Without --max-turns, `claude -p` runs unbounded
        # turns: on a focused TDD task it writes the correct code early, then keeps
        # exploring/re-verifying until the sandbox run_timeout (1800s) KILLS it at
        # exit 124 — before it emits its final JSON, so the orchestrator never gets
        # control to grade/commit/PR. A bounded round always RETURNS; the Ralph loop
        # (fresh session, re-reads disk each round) continues any unfinished work.
        return ["claude", "-p", prompt, "--dangerously-skip-permissions",
                "--max-turns", str(self.max_turns),
                "--output-format", "json", "--allowedTools", ",".join(self.allowed_tools)]

    def _image(self) -> str:
        return self.image

    def _auth_mounts(self):
        return [AuthMount("~/.claude/.credentials.json",
                          "/home/sbx/.claude/.credentials.json")]

    def _auth_env(self):
        # Inject the OAuth access token so the sandboxed CLI authenticates. Without
        # this the CLI reports "Not logged in", assess/grade return {}, and every
        # live task escalates at phase 0. Empty when no credential is available.
        tok = _oauth_token()
        return {"CLAUDE_CODE_OAUTH_TOKEN": tok} if tok else {}

    def _parse(self, raw: dict) -> dict:
        # claude-code --output-format json wraps the model reply as a STRING in
        # `result`; the assess/grade answer is JSON inside that string.
        inner = raw.get("result")
        if isinstance(inner, dict):
            return inner
        if isinstance(inner, str):
            obj = extract_json(inner)
            if obj is not None:
                return obj
        return raw if isinstance(raw, dict) else {}
