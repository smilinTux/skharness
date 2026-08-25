"""Focused PiHarness session-plane tests; no real tmux, git, Pi, or network."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from skharness.events import EventType
from skharness.harness import Harness, SessionDescriptor, SpawnRejected
from skharness.harnesses.pi import PiHarness, parse_pi_json_line
from skharness.pool import PoolController


class FakeTmux:
    def __init__(self) -> None:
        self.live: set[str] = set()
        self.calls: list[list[str]] = []
        self.archive_checks: list[tuple[str, bool]] = []
        self.expected_archive: dict[str, Path] = {}

    def __call__(self, argv: list[str]) -> str:
        self.calls.append(list(argv))
        if "new-session" in argv or "set-window-option" in argv or "pipe-pane" in argv:
            return ""
        if "new-window" in argv:
            self.live.add(argv[argv.index("-n") + 1])
            return ""
        if "list-windows" in argv:
            return "monitor\t1\n" + "".join(f"{sid}\t2\n" for sid in sorted(self.live))
        if "capture-pane" in argv:
            sid = argv[argv.index("-t") + 1].rsplit(":", 1)[-1]
            path = self.expected_archive.get(sid)
            self.archive_checks.append(("capture", bool(path and path.exists())))
            return json.dumps(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "done"}],
                    },
                }
            ) + "\n"
        if "kill-window" in argv:
            sid = argv[argv.index("-t") + 1].rsplit(":", 1)[-1]
            path = self.expected_archive.get(sid)
            self.archive_checks.append(("kill", bool(path and path.exists())))
            self.live.discard(sid)
            return ""
        raise AssertionError(f"unexpected tmux call: {argv}")


def git_ok():
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> subprocess.CompletedProcess:
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    return runner, calls


def harness(tmp_path: Path, *, repo: Path | None = None, full_agent: str = "pi-worker"):
    repo = repo or tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    tmux = FakeTmux()
    git, git_calls = git_ok()
    value = PiHarness(
        host="chiap02",
        runner=tmux,
        git_runner=git,
        dispatch_repos=[str(repo)],
        worktree_root=tmp_path / "worktrees",
        sessions_root=tmp_path / "agents",
        full_agent=full_agent,
        full_home=tmp_path / "home",
        child_path="/usr/bin:/bin",
        gateway_base="http://chiap01.example:18780/v1",
        gateway_api_key="synthetic-test-key",
        default_model="sk-codex",
    )
    return value, tmux, git_calls, repo


def new_window(calls: list[list[str]]) -> list[str]:
    return next(call for call in calls if "new-window" in call)


def child_env(call: list[str]) -> dict[str, str]:
    start = call.index("env")
    assert call[start + 1] == "-i"
    result = {}
    for value in call[start + 2 :]:
        if value == "pi":
            break
        key, separator, item = value.partition("=")
        assert separator
        result[key] = item
    return result


def assert_no_machine_touch(tmux: FakeTmux, git_calls: list[list[str]]) -> None:
    assert tmux.calls == []
    assert git_calls == []


def test_is_session_plane_harness_and_reuses_guarded_lifecycle():
    value = PiHarness(runner=lambda _argv: "", gateway_api_key="synthetic-test-key")
    assert isinstance(value, Harness)
    assert value.name == "pi"
    assert value.capabilities()["session_plane"] is True
    assert value.capabilities()["structured_output"] == "json"
    assert value.capabilities()["task_plane"] is False
    # Load-bearing reuse proof: Pi does not carry a fifth guard or teardown copy.
    assert "spawn" not in PiHarness.__dict__
    assert "archive" not in PiHarness.__dict__


# The four inherited fail-closed guards, in their established spawn order.


@pytest.mark.asyncio
async def test_guard_1_profile_rejects_before_repo_git_or_tmux(tmp_path):
    value, tmux, git_calls, repo = harness(tmp_path)
    with pytest.raises(SpawnRejected, match="profile"):
        await value.spawn(
            SessionDescriptor(repo=str(repo), branch="main", quality="root"), prompt="x"
        )
    assert_no_machine_touch(tmux, git_calls)


@pytest.mark.asyncio
async def test_guard_2_empty_repo_allowlist_denies_all(tmp_path):
    value, tmux, git_calls, repo = harness(tmp_path)
    value.dispatch_repos = []
    with pytest.raises(SpawnRejected, match="deny all"):
        await value.spawn(
            SessionDescriptor(repo=str(repo), branch="main", quality="sandbox"), prompt="x"
        )
    assert_no_machine_touch(tmux, git_calls)


@pytest.mark.asyncio
async def test_guard_2_repo_not_on_allowlist_rejects(tmp_path):
    value, tmux, git_calls, _repo = harness(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(SpawnRejected, match="allowlist"):
        await value.spawn(
            SessionDescriptor(repo=str(other), branch="main", quality="sandbox"), prompt="x"
        )
    assert_no_machine_touch(tmux, git_calls)


@pytest.mark.asyncio
async def test_guard_3_branch_uses_git_check_ref_format_and_never_spawns(tmp_path):
    value, tmux, git_calls, repo = harness(tmp_path)

    def reject_ref(argv: list[str]) -> subprocess.CompletedProcess:
        git_calls.append(list(argv))
        assert "check-ref-format" in argv
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="bad ref")

    value._git = reject_ref
    with pytest.raises(SpawnRejected, match="check-ref-format"):
        await value.spawn(
            SessionDescriptor(repo=str(repo), branch="--bad", quality="sandbox"), prompt="x"
        )
    assert git_calls == [["git", "check-ref-format", "--branch", "--bad"]]
    assert tmux.calls == []


@pytest.mark.asyncio
async def test_guard_4_session_regex_rejects_unsafe_agent_before_machine_touch(tmp_path):
    value, tmux, git_calls, repo = harness(tmp_path, full_agent="bad;agent$(id)")
    with pytest.raises(SpawnRejected, match="charset"):
        await value.spawn(
            SessionDescriptor(repo=str(repo), branch="main", quality="full"), prompt="x"
        )
    # The branch validator is guard 3; worktree creation and tmux remain untouched.
    assert git_calls == [["git", "check-ref-format", "--branch", "main"]]
    assert tmux.calls == []


@pytest.mark.asyncio
async def test_spawn_builds_isolated_pi_routing_and_attribution_config(tmp_path):
    value, tmux, git_calls, repo = harness(tmp_path)
    session = await value.spawn(
        SessionDescriptor(
            repo=str(repo), branch="main", model="sk-codex", quality="sandbox"
        ),
        prompt="Return one word",
    )

    call = new_window(tmux.calls)
    env = child_env(call)
    assert "OPENAI_BASE_URL" not in env
    config_dir = Path(env["PI_CODING_AGENT_DIR"])
    assert config_dir == tmp_path / "worktrees" / session.sid / ".pi-coding-agent"
    config = json.loads((config_dir / "models.json").read_text())
    provider = config["providers"]["skgw"]
    assert provider["baseUrl"] == "http://chiap01.example:18780/v1"
    assert provider["api"] == "openai-completions"
    assert provider["compat"] == {"supportsDeveloperRole": False}
    assert provider["models"][0]["id"] == "sk-codex"
    assert provider["headers"] == {
        "x-agent-id": "sandbox",
        "x-session-id": session.sid,
    }

    pi_argv = call[call.index("pi") :]
    assert pi_argv[:3] == ["pi", "-p", "Return one word"]
    assert pi_argv[pi_argv.index("--mode") + 1] == "json"
    assert "--no-session" in pi_argv
    assert pi_argv[pi_argv.index("--model") + 1] == "skgw/sk-codex"
    assert "--api-key" in pi_argv
    assert any("worktree" in git_call and "add" in git_call for git_call in git_calls)


def test_parse_assistant_message_end_content_text():
    line = json.dumps(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "hello "},
                    {"type": "thinking", "thinking": "not output"},
                    {"type": "text", "text": "world"},
                ],
            },
        }
    )
    events = parse_pi_json_line(line, ts=12.5)
    assert len(events) == 1
    assert events[0].type == EventType.ASSISTANT_TEXT
    assert events[0].text == "hello world"
    assert events[0].ts == 12.5


@pytest.mark.parametrize(
    "line",
    ["", "not-json", "[]", '{"type":"message_end","message":{"role":"user"}}'],
)
def test_parse_ignores_non_assistant_or_malformed_lines(line):
    assert parse_pi_json_line(line) == []


@pytest.mark.asyncio
async def test_archive_persists_transcript_before_stopping_window(tmp_path):
    value, tmux, _git_calls, repo = harness(tmp_path)
    session = await value.spawn(
        SessionDescriptor(repo=str(repo), branch="main", quality="sandbox"), prompt="x"
    )
    expected = tmp_path / "agents" / "sandbox" / "sessions" / f"{session.sid}.json"
    tmux.expected_archive[session.sid] = expected

    result = await value.archive(session.sid)

    assert result["archived"] is True
    assert expected.exists()
    assert tmux.archive_checks == [("capture", False), ("kill", True)]
    record = json.loads(expected.read_text())
    assert record["harness"] == "pi"
    assert "message_end" in record["transcript"]


@pytest.mark.asyncio
async def test_pool_controller_injects_pi_harness_spawn_two_drain_one_destroy(tmp_path):
    value, tmux, _git_calls, repo = harness(tmp_path)
    pool = PoolController(value)

    first = await pool.spawn(
        lane="pi", repo=str(repo), branch="main", prompt="first", model="sk-codex"
    )
    second = await pool.spawn(
        lane="pi", repo=str(repo), branch="main", prompt="second", model="sk-codex"
    )
    for member in (first, second):
        tmux.expected_archive[member.sid] = (
            tmp_path / "agents" / "sandbox" / "sessions" / f"{member.sid}.json"
        )

    assert len(tmux.live) == 2
    assert (await pool.drain(first.sid))["archived"] is True
    assert first.sid not in tmux.live and second.sid in tmux.live
    destroyed = await pool.destroy()
    assert [result["sid"] for result in destroyed] == [second.sid]
    assert tmux.live == set()
    assert all(member.drained for member in pool.members("pi"))
