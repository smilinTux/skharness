"""ClaudeCodeAdapter on the shared BaseCliAdapter + Docker Sandbox. Keeps the
deny-by-default tool firewall (fail closed) from claude_code.py; the spawn is the
harness-agnostic Sandbox."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ..claude_code import ForbiddenToolError, is_forbidden
from ..sandbox import AuthMount, Sandbox
from .base import BaseCliAdapter, extract_json

_CRED_PATH = "~/.claude/.credentials.json"
#: Treat a token within this many seconds of expiry as already stale (clock skew
#: plus room for a multi-round run to outlive a just-valid token).
_EXPIRY_SKEW_SEC = 300


def _oauth_token(cred_path: str | None = None) -> str | None:
    """The OAuth token to inject as CLAUDE_CODE_OAUTH_TOKEN so the sandboxed CLI
    authenticates (the bare .credentials.json mount is NOT read by the in-image
    CLI, which reports 'Not logged in'). Fail soft: None -> no token env.

    Resolution order:
    1. An explicitly provisioned ``CLAUDE_CODE_OAUTH_TOKEN`` in the environment.
       This is the RELIABLE path for headless/cron runs: mint a long-lived token
       once with ``claude setup-token`` and set it here, so a run never dies on
       the ~8h interactive access-token expiry.
    2. The short-lived ``accessToken`` from the host .credentials.json. Convenient
       for interactive hosts, but it expires; when it is past (or within
       ``_EXPIRY_SKEW_SEC`` of) ``expiresAt`` we log and still return it so the
       failure surfaces as a clear 401 rather than a silent no-token, but the
       env-token path above is what a scheduled run should use.

    ``cred_path`` defaults to the module-level ``_CRED_PATH`` resolved at call
    time (not bound as a default arg), so the path stays overridable in tests."""
    env_tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if env_tok:
        return env_tok
    try:
        cred = json.loads(Path(cred_path or _CRED_PATH).expanduser().read_text())
        oauth = cred.get("claudeAiOauth", {})
        tok = oauth.get("accessToken")
        expires_at = oauth.get("expiresAt")
    except (OSError, ValueError):
        return None
    if not tok:
        return None
    if isinstance(expires_at, (int, float)) and expires_at / 1000.0 <= time.time() + _EXPIRY_SKEW_SEC:
        print("autopilot: host claude access token is expired/near-expiry; provision a "
              "long-lived CLAUDE_CODE_OAUTH_TOKEN (claude setup-token) for headless runs")
    return tok


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

    def _argv(self, prompt: str, light: bool = False) -> list[str]:
        # light = a JUDGMENT call (assess/grade): answer the JSON question in a
        # single turn with NO tools. These calls need no repo access, but running
        # them as a full agentic session (many turns + Bash/Edit/Write, cwd mounted
        # at a huge home dir) let the model wander and HEDGE to needs_decision on a
        # task it otherwise grades valid -- the flake that stranded runs at phase 0.
        # One turn, no tools => it just answers, fast and stable.
        if light:
            return ["claude", "-p", prompt, "--dangerously-skip-permissions",
                    "--max-turns", "1", "--output-format", "json"]
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
