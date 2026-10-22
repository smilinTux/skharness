"""Executable liveness tests use typed fake tmux receipts only."""

from __future__ import annotations

import subprocess

import pytest

from skharness.harness import SessionDescriptor
from skharness.harnesses.claude_code import ClaudeCodeHarness, CommandResult
from skharness.harnesses.pi import PiHarness


class PaneRunner:
    def __init__(self, pane: str = "", *, returncode: int = 0) -> None:
        self.pane = pane
        self.returncode = returncode
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> CommandResult:
        self.calls.append(list(argv))
        return CommandResult(tuple(argv), self.returncode, self.pane, "")


def owned(pane: str, *, returncode: int = 0):
    runner = PaneRunner(pane, returncode=returncode)
    harness = ClaudeCodeHarness(runner=runner)
    harness._spawned_sids.add("agent-deadbeef")
    harness._resource_ids["agent-deadbeef"] = "@7"
    harness._spawned_executables["agent-deadbeef"] = "claude"
    return harness, runner


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pane", "returncode", "alive"),
    [
        ("", 1, False),
        ("@7\\t\\tclaude\\t0", 0, False),
        ("@7\\t321\\tclaude\\t0", 0, True),
        ("@7\\t321\\tclaude\\t0", 0, True),
        ("@7\\t321\\tbash\\t0", 0, False),
        ("@7\\t321\\tclaude\\t1", 0, False),
        ("@7\\t321\\tclaude\\t0\n@8\\t322\\tclaude\\t0", 0, False),
    ],
    ids=[
        "missing-target",
        "missing-pid",
        "healthy-idle-zero-children",
        "expected-executable-with-child",
        "bare-shell-mismatch",
        "exited-worker",
        "duplicate-name",
    ],
)
async def test_owned_liveness_is_exact_pane_identity_only(pane, returncode, alive):
    harness, runner = owned(pane, returncode=returncode)
    assert await harness.is_alive("agent-deadbeef") is alive
    assert runner.calls == [
        [
            "tmux",
            "list-panes",
            "-t",
            "@7",
            "-F",
            "#{window_id}\\t#{pane_pid}\\t#{pane_current_command}\\t#{pane_dead}",
        ]
    ]
    assert all(call[0] == "tmux" for call in runner.calls)  # never ps/pgrep/children


@pytest.mark.asyncio
async def test_wrong_exact_window_identity_is_dead():
    harness, _ = owned("@8\\t321\\tclaude\\t0")
    assert await harness.is_alive("agent-deadbeef") is False


@pytest.mark.asyncio
async def test_unowned_pane_fails_closed_without_command():
    runner = PaneRunner("@7\\t321\\tclaude\\t0")
    harness = ClaudeCodeHarness(runner=runner)
    assert await harness.is_alive("unowned-deadbeef") is False
    assert runner.calls == []


@pytest.mark.asyncio
async def test_spawn_records_exact_pane_executable(tmp_path):
    calls: list[list[str]] = []

    def runner(argv):
        calls.append(list(argv))
        if "new-window" in argv or "display-message" in argv:
            return CommandResult(tuple(argv), 0, "@1\n", "")
        return CommandResult(tuple(argv), 0, "", "")

    def git_runner(argv):
        return subprocess.CompletedProcess(argv, 0, "", "")

    harness = ClaudeCodeHarness(
        runner=runner,
        git_runner=git_runner,
        worktree_root=tmp_path,
        sessions_root=tmp_path / "sessions",
        claude_bin="/opt/claude-exact",
    )
    session = await harness.spawn(SessionDescriptor(model="sk-default"), prompt="idle")
    assert harness._spawned_executables == {session.sid: "claude-exact"}


def test_pi_inherits_single_probe_without_override():
    assert PiHarness.is_alive is ClaudeCodeHarness.is_alive
