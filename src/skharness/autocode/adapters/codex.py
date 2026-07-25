"""CodexStubAdapter: fail-closed placeholder. codex is not installed on this node
(only a stub is on PATH). Registered so the harness registry surface is complete
and swap is proven; replace with a real adapter when codex is present."""
from __future__ import annotations

from skharness.harness import Harness

from ..claude_code import HarnessUnavailable

_MSG = "codex is not installed on this node; register a real codex adapter to use it"


class CodexStubAdapter(Harness):
    name = "codex"

    def __init__(self, *args, **kwargs):
        pass

    def capabilities(self):
        # Exposes the task-plane interface (assess/run_task/grade below); it is
        # merely unavailable on this node until a real codex is installed.
        return {"session_resume": False, "structured_output": "none",
                "sandbox": False, "tool_restrictions": False,
                "task_plane": True, "session_plane": False,
                "headless_api": "none", "hot_set_model": False}

    def assess(self, brief):
        raise HarnessUnavailable(_MSG)

    def run_task(self, brief):
        raise HarnessUnavailable(_MSG)

    def grade(self, brief):
        raise HarnessUnavailable(_MSG)
