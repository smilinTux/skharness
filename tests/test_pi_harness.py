"""Focused PiHarness session-plane tests; no real tmux, git, Pi, or network."""

from __future__ import annotations

import asyncio
import errno
import json
import multiprocessing as mp
import os
import subprocess
from pathlib import Path

import pytest

from skharness.events import EventType
from skharness.harness import (
    Harness,
    SessionDescriptor,
    SpawnOwnershipError,
    SpawnRejected,
)
from skharness.harnesses.claude_code import CommandResult
from skharness.harnesses.pi import PiHarness, parse_pi_json_line
from skharness.pool import PoolController
from skharness.securefs import SecureDir, SecurePathError


class FakeTmux:
    def __init__(self) -> None:
        self.live: set[str] = set()
        self.calls: list[list[str]] = []
        self.archive_checks: list[tuple[str, bool]] = []
        self.expected_archive: dict[str, Path] = {}
        self.ids: dict[str, str] = {}
        self._next_id = 1

    def __call__(self, argv: list[str]) -> str:
        self.calls.append(list(argv))
        if "new-session" in argv or "set-window-option" in argv or "pipe-pane" in argv:
            return ""
        if "new-window" in argv:
            sid = argv[argv.index("-n") + 1]
            resource_id = f"@{self._next_id}"
            self._next_id += 1
            self.live.add(sid)
            self.ids[resource_id] = sid
            return resource_id + "\n"
        if "display-message" in argv:
            target = argv[argv.index("-t") + 1]
            return target + "\n" if target in self.ids else ""
        if "list-windows" in argv:
            return "monitor\t1\n" + "".join(f"{sid}\t2\n" for sid in sorted(self.live))
        if "capture-pane" in argv:
            target = argv[argv.index("-t") + 1]
            sid = self.ids.get(target, target.rsplit(":", 1)[-1])
            path = self.expected_archive.get(sid)
            self.archive_checks.append(("capture", bool(path and path.exists())))
            return (
                json.dumps(
                    {
                        "type": "message_end",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "done"}],
                        },
                    }
                )
                + "\n"
            )
        if "kill-window" in argv:
            target = argv[argv.index("-t") + 1]
            sid = self.ids.pop(target, target.rsplit(":", 1)[-1])
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


def harness(
    tmp_path: Path,
    *,
    repo: Path | None = None,
    full_agent: str = "pi-worker",
    **pi_kwargs,
):
    repo = repo or tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    tmux = FakeTmux()
    git, git_calls = git_ok()
    options = {
        "worktree_root": tmp_path / "worktrees",
        "sessions_root": tmp_path / "agents",
        **pi_kwargs,
    }
    value = PiHarness(
        host="chiap02",
        runner=tmux,
        git_runner=git,
        dispatch_repos=[str(repo)],
        full_agent=full_agent,
        full_home=tmp_path / "home",
        child_path="/usr/bin:/bin",
        gateway_base="http://chiap01.example:18790/v1",
        default_model="sk-codex",
        **options,
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
    value = PiHarness(runner=lambda _argv: "", gateway_base="http://gateway.test/v1")
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
        SessionDescriptor(repo=str(repo), branch="main", model="sk-codex", quality="sandbox"),
        prompt="Return one word",
    )

    call = new_window(tmux.calls)
    env = child_env(call)
    assert "OPENAI_BASE_URL" not in env
    config_dir = Path(env["PI_CODING_AGENT_DIR"])
    assert config_dir == Path(value._config_dirs[session.sid].proc_path)
    assert (tmp_path / "pi-config" / session.sid).samefile(config_dir)
    assert not (tmp_path / "pi-config" / session.sid).is_relative_to(tmp_path / "worktrees")
    config = json.loads((config_dir / "models.json").read_text())
    provider = config["providers"]["skgw"]
    assert provider["baseUrl"] == "http://chiap01.example:18790/v1"
    assert provider["api"] == "openai-completions"
    assert provider["apiKey"] == "sk-local"
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
    assert pi_argv[pi_argv.index("--api-key") + 1] == "sk-local"
    assert any("worktree" in git_call and "add" in git_call for git_call in git_calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("link_kind", ["absolute", "relative"])
async def test_real_git_repo_pi_config_symlink_cannot_escape_controller_root(tmp_path, link_kind):
    """A committed absolute/relative config symlink is materialized by real git.

    Pi must ignore that repository-controlled location and write only below the
    separately owned config root.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link_target = str(outside) if link_kind == "absolute" else "../../outside"
    (repo / ".pi-coding-agent").symlink_to(link_target, target_is_directory=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "test"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", ".pi-coding-agent"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "adversarial symlink"], check=True)
    branch = subprocess.run(
        ["git", "-C", str(repo), "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    tmux = FakeTmux()
    value = PiHarness(
        runner=tmux,
        dispatch_repos=[str(repo)],
        worktree_root=tmp_path / "worktrees",
        config_root=tmp_path / "controller-config",
        sessions_root=tmp_path / "agents",
        gateway_base="http://gateway.test/v1",
    )

    session = await value.spawn(
        SessionDescriptor(repo=str(repo), branch=branch, quality="sandbox"), prompt="x"
    )

    assert (tmp_path / "worktrees" / session.sid / ".pi-coding-agent").is_symlink()
    assert not (outside / "models.json").exists()
    config = tmp_path / "controller-config" / session.sid / "models.json"
    assert config.is_file()
    assert Path(child_env(new_window(tmux.calls))["PI_CODING_AGENT_DIR"]).samefile(config.parent)


@pytest.mark.asyncio
async def test_caller_secret_is_never_persisted_or_passed_to_pi(tmp_path, monkeypatch):
    caller_secret = "caller-secret-that-must-not-land"
    monkeypatch.setenv("SKCODE_GATEWAY_TOKEN", caller_secret)
    value, tmux, _git_calls, repo = harness(tmp_path, gateway_token=caller_secret)

    session = await value.spawn(
        SessionDescriptor(repo=str(repo), branch="main", quality="sandbox"), prompt="x"
    )

    config_path = tmp_path / "pi-config" / session.sid / "models.json"
    persisted = config_path.read_text()
    call = new_window(tmux.calls)
    assert caller_secret not in persisted
    assert caller_secret not in call
    assert json.loads(persisted)["providers"]["skgw"]["apiKey"] == "sk-local"
    assert call[call.index("--api-key") + 1] == "sk-local"


def test_missing_route_is_refused(monkeypatch):
    monkeypatch.delenv("SKCODE_PI_GATEWAY_BASE", raising=False)
    with pytest.raises(ValueError, match="gateway route is required"):
        PiHarness(runner=lambda _argv: "")


def test_environment_route_is_preserved(monkeypatch):
    monkeypatch.setenv("SKCODE_PI_GATEWAY_BASE", "http://environment.test:18790/v1")
    value = PiHarness(runner=lambda _argv: "")
    assert value.pi_gateway_base == "http://environment.test:18790/v1"


def test_explicit_effective_route_is_preserved():
    value = PiHarness(runner=lambda _argv: "", gateway_base="http://gateway.test:29999/v1")
    assert value.pi_gateway_base == "http://gateway.test:29999/v1"


def test_config_root_inside_worktrees_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="outside repository worktrees"):
        PiHarness(
            runner=lambda _argv: "",
            gateway_base="http://gateway.test/v1",
            worktree_root=tmp_path / "worktrees",
            config_root=tmp_path / "worktrees" / "pi-config",
        )


def test_config_root_traversal_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="traversal"):
        PiHarness(
            runner=lambda _argv: "",
            gateway_base="http://gateway.test/v1",
            worktree_root=tmp_path / "worktrees",
            config_root=tmp_path / "state" / ".." / "pi-config",
        )


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
async def test_worktree_root_ancestor_symlink_is_rejected_without_git_or_tmux(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "state-link").symlink_to(outside, target_is_directory=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    value, tmux, git_calls, _repo = harness(
        tmp_path,
        repo=repo,
        worktree_root=tmp_path / "state-link" / "worktrees",
    )

    with pytest.raises(SpawnRejected, match="(worktree|reservation) root"):
        await value.spawn(
            SessionDescriptor(repo=str(repo), branch="main", quality="sandbox"),
            prompt="x",
        )

    assert not (outside / "worktrees").exists()
    assert not any("worktree" in call for call in git_calls)
    assert not any("new-window" in call for call in tmux.calls)


def test_transcript_root_ancestor_symlink_fails_before_write_or_teardown(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "state-link").symlink_to(outside, target_is_directory=True)
    value, tmux, _git_calls, _repo = harness(
        tmp_path, sessions_root=tmp_path / "state-link" / "agents"
    )
    sid = "sandbox-transcriptroot"
    tmux.live.add(sid)

    result = asyncio.run(value.archive(sid))

    assert result["archived"] is False
    assert result["transcript_persisted"] is False
    assert result["teardown_succeeded"] is False
    assert sid in tmux.live
    assert not (outside / "agents").exists()


def test_config_root_ancestor_symlink_is_rejected_without_escape(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "state-link").symlink_to(outside, target_is_directory=True)
    value, tmux, _git_calls, _repo = harness(
        tmp_path, config_root=tmp_path / "state-link" / "pi-config"
    )

    with pytest.raises(SpawnRejected, match="config reservation"):
        value._build_env("sandbox", "sandbox", tmp_path / "worktrees" / "sandbox-safe")

    assert not (outside / "pi-config").exists()
    assert not any("new-window" in call for call in tmux.calls)


def test_config_parent_swap_cannot_redirect_models_write(tmp_path, monkeypatch):
    value, _tmux, _git_calls, _repo = harness(tmp_path)
    original_mkdir = SecureDir.mkdir
    outside = tmp_path / "outside"
    outside.mkdir()

    def swap_after_open(self, name, **kwargs):
        directory = original_mkdir(self, name, **kwargs)
        moved = tmp_path / "held-config"
        value.config_root.rename(moved)
        value.config_root.symlink_to(outside, target_is_directory=True)
        return directory

    monkeypatch.setattr(SecureDir, "mkdir", swap_after_open)
    env = value._build_env("sandbox", "sandbox", tmp_path / "worktrees" / "sandbox-safe")

    assert Path(env["PI_CODING_AGENT_DIR"]).joinpath("models.json").is_file()
    assert (tmp_path / "held-config" / "sandbox-safe" / "models.json").is_file()
    assert not (outside / "sandbox-safe" / "models.json").exists()


def test_models_bytes_are_unlinkable_at_former_verification_write_boundary(tmp_path, monkeypatch):
    value, _tmux, _git_calls, _repo = harness(tmp_path)
    outside = tmp_path / "outside-models.json"
    original_publish = SecureDir._publish_exclusive

    def publish_after_try_link(self, fd, name, *, mode):
        with pytest.raises(FileNotFoundError):
            os.link(value.config_root / "sandbox-safe" / "models.json", outside)
        original_publish(self, fd, name, mode=mode)

    monkeypatch.setattr(SecureDir, "_publish_exclusive", publish_after_try_link)
    env = value._build_env("sandbox", "sandbox", tmp_path / "worktrees" / "sandbox-safe")

    model_path = Path(env["PI_CODING_AGENT_DIR"]) / "models.json"
    assert model_path.is_file()
    assert not outside.exists()
    assert os.stat(model_path).st_nlink == 1


def _same_uid_publication_controller(root: str, operation: str, conn) -> None:
    state = SecureDir.anchor(Path(root) / "state")
    if operation == "audit":
        state.append_durable("audit.log", b"old-record\n")
    original_write = SecureDir._write_all
    attacked = False

    def pause_before_write(fd: int, data: bytes, *, message: str) -> None:
        nonlocal attacked
        if not attacked:
            attacked = True
            conn.send({"pid": os.getpid(), "fd": fd})
            assert conn.recv() == "attack-complete"
        original_write(fd, data, message=message)

    SecureDir._write_all = staticmethod(pause_before_write)
    try:
        if operation == "models":
            fd = state.write_exclusive("models.json", b'{"route":"private"}')
            os.close(fd)
        else:
            state.append_durable("audit.log", b"mandatory-record\n")
        conn.send("published")
    finally:
        SecureDir._write_all = staticmethod(original_write)
        state.close()
        conn.close()


@pytest.mark.parametrize(
    ("operation", "final_name", "expected"),
    [
        ("models", "models.json", b'{"route":"private"}'),
        ("audit", "audit.log", b"old-record\nmandatory-record\n"),
    ],
)
def test_same_uid_proc_fd_alias_is_denied_before_private_write(
    tmp_path, operation, final_name, expected
):
    outside = tmp_path / "outside"
    outside.mkdir()
    parent, child = mp.Pipe()
    process = mp.Process(
        target=_same_uid_publication_controller,
        args=(str(tmp_path / operation), operation, child),
    )
    process.start()
    receipt = parent.recv()
    outside_fd = os.open(outside, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(PermissionError) as denied:
            os.link(
                f"/proc/{receipt['pid']}/fd/{receipt['fd']}",
                f"{operation}.alias",
                dst_dir_fd=outside_fd,
                follow_symlinks=True,
            )
        assert denied.value.errno in (errno.EACCES, errno.EPERM)
    finally:
        os.close(outside_fd)
    assert not list(outside.iterdir())
    parent.send("attack-complete")
    assert parent.recv() == "published"
    process.join(10)
    assert process.exitcode == 0
    assert (tmp_path / operation / "state" / final_name).read_bytes() == expected
    assert not list(outside.iterdir())


def _preopened_proc_controller(root: str, conn) -> None:
    conn.send(os.getpid())
    assert conn.recv() == "proc-opened"
    state = SecureDir.anchor(Path(root) / "state")
    original_write = SecureDir._write_all

    def pause_before_write(fd: int, data: bytes, *, message: str) -> None:
        conn.send(fd)
        assert conn.recv() == "attack-complete"
        original_write(fd, data, message=message)

    SecureDir._write_all = staticmethod(pause_before_write)
    try:
        fd = state.write_exclusive("models.json", b"private")
        os.close(fd)
        conn.send("published")
    finally:
        SecureDir._write_all = staticmethod(original_write)
        state.close()
        conn.close()


def test_preopened_proc_fd_directory_cannot_bypass_publication_domain(tmp_path):
    parent, child = mp.Pipe()
    process = mp.Process(target=_preopened_proc_controller, args=(str(tmp_path), child))
    process.start()
    pid = parent.recv()
    proc_fd_dir = os.open(f"/proc/{pid}/fd", os.O_RDONLY | os.O_DIRECTORY)
    parent.send("proc-opened")
    sensitive_fd = parent.recv()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_fd = os.open(outside, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(PermissionError) as denied:
            os.link(
                str(sensitive_fd),
                "alias",
                src_dir_fd=proc_fd_dir,
                dst_dir_fd=outside_fd,
                follow_symlinks=True,
            )
        assert denied.value.errno in (errno.EACCES, errno.EPERM)
    finally:
        os.close(outside_fd)
        os.close(proc_fd_dir)
    parent.send("attack-complete")
    assert parent.recv() == "published"
    process.join(10)
    assert process.exitcode == 0
    assert not (outside / "alias").exists()
    assert (tmp_path / "state" / "models.json").read_bytes() == b"private"


def test_unavailable_publication_domain_fails_closed_before_sensitive_open(tmp_path, monkeypatch):
    from skharness import securefs

    opened = []
    original_open = securefs.os.open

    def fail_set(option, argument=0):
        if option == securefs._PR_GET_DUMPABLE:
            return 1
        raise SecurePathError(errno.ENOTSUP, securefs._SECURITY_DOMAIN_REQUIREMENT)

    def record_open(*args, **kwargs):
        opened.append((args, kwargs))
        return original_open(*args, **kwargs)

    state = SecureDir.anchor(tmp_path / "state")
    monkeypatch.setattr(securefs, "_prctl", fail_set)
    monkeypatch.setattr(securefs.os, "open", record_open)
    with pytest.raises(SecurePathError, match="separate UID"):
        state.write_exclusive("models.json", b"private")
    assert opened == []
    assert not (tmp_path / "state" / "models.json").exists()
    state.close()


def test_reservation_root_ancestor_symlink_is_rejected_without_escape(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "state-link").symlink_to(outside, target_is_directory=True)
    value, tmux, _git_calls, _repo = harness(
        tmp_path, reservation_root=tmp_path / "state-link" / "reservations"
    )

    with pytest.raises(SpawnRejected, match="reservation root"):
        value._reserve_sid("sandbox")

    assert not (outside / "reservations").exists()
    assert not any("new-window" in call for call in tmux.calls)


@pytest.mark.asyncio
async def test_archive_list_failure_is_truthful_and_never_captures_or_kills(tmp_path):
    value, tmux, _git_calls, _repo = harness(tmp_path)
    sid = "sandbox-listfailure"
    value._spawned_sids.add(sid)

    def fail_list(argv):
        if "list-windows" in argv:
            return CommandResult(tuple(argv), 1, "", "server unavailable")
        raise AssertionError(f"must not actuate after list failure: {argv}")

    value._runner = fail_list
    result = await value.archive(sid)

    assert result["archived"] is False
    assert result["transcript_persisted"] is False
    assert result["teardown_succeeded"] is False
    assert "list-windows" in result["reason"]
    assert not any("capture-pane" in call or "kill-window" in call for call in tmux.calls)


@pytest.mark.asyncio
async def test_archive_transcript_failure_is_receipted_and_window_survives(tmp_path):
    value, tmux, _git_calls, _repo = harness(tmp_path)
    sid = "sandbox-transcriptfailure"
    tmux.live.add(sid)
    value._secure_sessions = SecureDir.anchor(tmp_path / "agents")
    (tmp_path / "agents").rename(tmp_path / "agents-held")
    (tmp_path / "agents").write_text("not a directory")
    # Descriptor anchoring means the parent replacement itself cannot redirect;
    # force a final collision in the exact held inode to exercise receipt truth.
    held = tmp_path / "agents-held" / "sandbox" / "sessions"
    held.mkdir(parents=True)
    (held / f"{sid}.json").write_text("existing")

    result = await value.archive(sid)

    assert result["archived"] is False
    assert result["transcript_persisted"] is False
    assert result["teardown_succeeded"] is False
    assert sid in tmux.live
    assert not any("kill-window" in call for call in tmux.calls)


@pytest.mark.asyncio
async def test_setup_rollback_kill_failure_exposes_unresolved_resource(tmp_path):
    value, tmux, _git_calls, repo = harness(tmp_path)

    def fail_pipe_and_kill(argv):
        if "pipe-pane" in argv:
            return CommandResult(tuple(argv), 1, "", "pipe refused")
        if "kill-window" in argv:
            return CommandResult(tuple(argv), 1, "", "kill refused")
        return tmux(argv)

    value._runner = fail_pipe_and_kill
    with pytest.raises(SpawnOwnershipError) as caught:
        await value.spawn(
            SessionDescriptor(repo=str(repo), branch="main", quality="sandbox"),
            prompt="x",
        )

    assert caught.value.receipt["teardown_succeeded"] is False
    assert caught.value.receipt["tracked"] is False
    assert tmux.live


@pytest.mark.asyncio
async def test_spawn_command_failure_is_honest_and_not_tracked(tmp_path):
    value, tmux, _git_calls, repo = harness(tmp_path)

    def fail_new_window(argv):
        if "new-window" in argv:
            return CommandResult(tuple(argv), 1, "", "no such executable")
        return tmux(argv)

    value._runner = fail_new_window
    with pytest.raises(SpawnRejected, match="tmux new-window failed"):
        await value.spawn(
            SessionDescriptor(repo=str(repo), branch="main", quality="sandbox"),
            prompt="x",
        )
    assert value._spawned_sids == set()


@pytest.mark.asyncio
async def test_capture_setup_failure_kills_window_and_is_not_tracked(tmp_path):
    value, tmux, _git_calls, repo = harness(tmp_path)

    def fail_pipe(argv):
        if "pipe-pane" in argv:
            return CommandResult(tuple(argv), 1, "", "pipe refused")
        return tmux(argv)

    value._runner = fail_pipe
    with pytest.raises(SpawnRejected, match="tmux pipe-pane failed"):
        await value.spawn(
            SessionDescriptor(repo=str(repo), branch="main", quality="sandbox"),
            prompt="x",
        )
    assert value._spawned_sids == set()
    assert tmux.live == set()


@pytest.mark.asyncio
async def test_liveness_failure_does_not_track_running_session(tmp_path):
    value, tmux, _git_calls, repo = harness(tmp_path)

    def fail_liveness(argv):
        if "display-message" in argv:
            return CommandResult(tuple(argv), 1, "", "window absent")
        return tmux(argv)

    value._runner = fail_liveness
    with pytest.raises(SpawnRejected, match="liveness check"):
        await value.spawn(
            SessionDescriptor(repo=str(repo), branch="main", quality="sandbox"),
            prompt="x",
        )
    assert value._spawned_sids == set()
    assert tmux.live == set()


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
async def test_archive_teardown_failure_is_partial_with_transcript_receipt(tmp_path):
    value, tmux, _git_calls, repo = harness(tmp_path)
    session = await value.spawn(
        SessionDescriptor(repo=str(repo), branch="main", quality="sandbox"), prompt="x"
    )

    def fail_kill(argv):
        if "kill-window" in argv:
            return CommandResult(tuple(argv), 1, "", "server unavailable")
        return tmux(argv)

    value._runner = fail_kill
    result = await value.archive(session.sid)

    assert result["archived"] is False
    assert result["partial"] is True
    assert result["transcript_persisted"] is True
    assert result["teardown_succeeded"] is False
    assert Path(result["transcript_path"]).is_file()
    assert session.sid in tmux.live


@pytest.mark.asyncio
async def test_sid_collision_retries_before_launch(tmp_path, monkeypatch):
    value, tmux, _git_calls, _repo = harness(tmp_path)
    colliding = "a" * 32
    replacement = "b" * 32
    value.reservation_root.mkdir(parents=True)
    (value.reservation_root / f"sandbox-{colliding}").mkdir()
    tokens = iter([colliding, replacement])
    monkeypatch.setattr(
        "skharness.harnesses.claude_code.secrets.token_hex",
        lambda _n: next(tokens),
    )

    session = await value.spawn(SessionDescriptor(quality="sandbox"), prompt="x")

    assert session.sid == f"sandbox-{replacement}"
    call = new_window(tmux.calls)
    assert call[call.index("-n") + 1] == session.sid


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
