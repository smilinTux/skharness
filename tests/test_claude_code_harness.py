"""ClaudeCodeHarness read-only session plane, over a FAKE tmux runner.

No real tmux, no real claude: the injectable argv runner returns canned output
and a tmp historical dir stands in for ~/.skcapstone/agents/<agent>/sessions/.
"""
import json
import subprocess
from pathlib import Path

import pytest

from skharness.events import EventType
from skharness.harness import Harness, HarnessSession, SessionDescriptor, SpawnRejected
from skharness.harnesses.claude_code import (
    ClaudeCodeHarness,
    new_lines,
    parse_repo_allowlist,
    parse_windows,
    scan_historical,
)


def test_parse_windows_skips_monitor_and_blanks():
    out = "monitor\t1700000000\nlumina-abc12345\t1700000100\nopus-deadbeef\t1700000200\n\n"
    got = parse_windows(out, host=".158")
    ids = [s.sid for s in got]
    assert ids == ["lumina-abc12345", "opus-deadbeef"]
    assert got[0].harness == "claude-code"
    assert got[0].host == ".158"
    assert got[0].state == "running"
    assert got[0].last_activity == 1700000100.0
    assert isinstance(got[0], SessionDescriptor)


def test_parse_windows_handles_missing_activity_field():
    got = parse_windows("lumina-abc12345\n", host=".158")
    assert got[0].sid == "lumina-abc12345"
    assert got[0].last_activity == 0.0


def test_new_lines_returns_only_the_tail():
    assert new_lines("a\nb\n", "a\nb\nc\nd\n") == ["c", "d"]
    assert new_lines("", "x\ny\n") == ["x", "y"]
    assert new_lines("a\nb\n", "a\nb\n") == []


def test_scan_historical_reads_agent_session_dirs(tmp_path):
    root = tmp_path / "agents"
    sdir = root / "lumina" / "sessions"
    sdir.mkdir(parents=True)
    (sdir / "sess-001.json").write_text(json.dumps({"id": "sess-001"}))
    (sdir / "sess-002.json").write_text(json.dumps({"id": "sess-002"}))
    got = scan_historical(root, host=".158")
    assert {s.sid for s in got} == {"lumina/sess-001", "lumina/sess-002"}
    assert all(s.state == "ended" for s in got)
    assert all(s.harness == "claude-code" for s in got)


def test_scan_historical_missing_root_is_empty(tmp_path):
    assert scan_historical(tmp_path / "nope", host=".158") == []


def test_is_a_harness_and_declares_pty_session_plane():
    h = ClaudeCodeHarness(runner=lambda argv: "")
    assert isinstance(h, Harness)
    assert h.name == "claude-code"
    caps = h.capabilities()
    assert caps["session_plane"] is True
    assert caps["headless_api"] == "pty"
    assert caps["task_plane"] is False
    assert caps["hot_set_model"] is False


@pytest.mark.asyncio
async def test_list_sessions_merges_live_then_historical(tmp_path):
    root = tmp_path / "agents"
    sdir = root / "lumina" / "sessions"
    sdir.mkdir(parents=True)
    (sdir / "old.json").write_text("{}")

    def runner(argv):
        assert argv[0] == "tmux"
        assert "list-windows" in argv
        return "monitor\t1\nlumina-abc12345\t1700000100\n"

    h = ClaudeCodeHarness(runner=runner, sessions_root=root, host=".158")
    got = await h.list_sessions()
    ids = [s.sid for s in got]
    assert ids[0] == "lumina-abc12345"          # live first
    assert "lumina/old" in ids                   # historical after
    assert got[0].state == "running"


@pytest.mark.asyncio
async def test_stream_emits_status_then_new_capture_lines(tmp_path):
    captures = iter([
        "line1\nline2\n",          # first poll
        "line1\nline2\nline3\n",   # second poll adds line3
    ])

    def runner(argv):
        if "capture-pane" in argv:
            return next(captures)
        return ""

    h = ClaudeCodeHarness(runner=runner, sessions_root=tmp_path, host=".158",
                          poll_interval=0.0, max_polls=2)
    out = [e async for e in h.stream("lumina-abc12345")]
    assert out[0].type == EventType.STATUS
    texts = [e.text for e in out if e.type == EventType.ASSISTANT_TEXT]
    assert texts == ["line1", "line2", "line3"]


@pytest.mark.asyncio
async def test_stream_rejects_bad_sid_charset(tmp_path):
    h = ClaudeCodeHarness(runner=lambda argv: "", sessions_root=tmp_path,
                          max_polls=1)
    out = [e async for e in h.stream("bad;name$(x)")]
    assert len(out) == 1
    assert out[0].type == EventType.STATUS
    assert "invalid" in out[0].text.lower()


# --- archive: stop + persist (never a destructive kill) ----------------------


@pytest.mark.asyncio
async def test_archive_persists_transcript_before_stopping(tmp_path):
    root = tmp_path / "agents"
    sid = "lumina-abc12345"
    expected = root / "lumina" / "sessions" / f"{sid}.json"
    verbs: list[tuple[str, bool]] = []

    def runner(argv):
        if "list-windows" in argv:
            return "monitor\t1\nlumina-abc12345\t1700000100\n"
        if "capture-pane" in argv:
            # full scrollback capture must ask for the top of history
            assert "-S" in argv and "-" in argv
            verbs.append(("capture", expected.exists()))
            return "hello\nworld\n"
        if "kill-window" in argv:
            # record whether the transcript was already on disk at STOP time
            verbs.append(("kill", expected.exists()))
            return ""
        raise AssertionError(f"unexpected tmux call: {argv}")

    h = ClaudeCodeHarness(runner=runner, sessions_root=root, host=".158")
    result = await h.archive(sid)

    assert result["archived"] is True
    assert Path(result["transcript_path"]) == expected
    # persist happened, transcript preserved with archived state
    assert expected.exists()
    record = json.loads(expected.read_text())
    assert record["transcript"] == "hello\nworld\n"
    assert record["state"] == "archived"
    assert record["sid"] == sid
    # ordering is load-bearing: capture ran with no file yet, STOP ran AFTER the
    # transcript was durable on disk.
    assert verbs == [("capture", False), ("kill", True)]


@pytest.mark.asyncio
async def test_archive_unknown_sid_is_a_clean_noop(tmp_path):
    root = tmp_path / "agents"
    calls: list[list[str]] = []

    def runner(argv):
        calls.append(list(argv))
        if "list-windows" in argv:
            return "monitor\t1\nlumina-abc12345\t1700000100\n"
        raise AssertionError(f"must not touch tmux beyond list-windows: {argv}")

    h = ClaudeCodeHarness(runner=runner, sessions_root=root, host=".158")
    result = await h.archive("opus-deadbeef")  # not a live window

    assert result["archived"] is False
    assert "no live session" in result["reason"]
    # never captured, never killed, nothing persisted
    assert all("kill-window" not in c and "capture-pane" not in c for c in calls)
    assert not (root / "opus" / "sessions").exists()


@pytest.mark.asyncio
async def test_archive_invalid_sid_never_touches_tmux(tmp_path):
    def runner(argv):
        raise AssertionError(f"invalid sid must not reach tmux: {argv}")

    h = ClaudeCodeHarness(runner=runner, sessions_root=tmp_path, host=".158")
    result = await h.archive("bad;name$(x)")
    assert result["archived"] is False
    assert "invalid" in result["reason"].lower()


@pytest.mark.asyncio
async def test_archive_removes_session_from_running_set_and_keeps_record(tmp_path):
    root = tmp_path / "agents"
    sid = "lumina-abc12345"
    state = {"alive": True}

    def runner(argv):
        if "list-windows" in argv:
            if state["alive"]:
                return "monitor\t1\nlumina-abc12345\t1700000100\n"
            return "monitor\t1\n"
        if "capture-pane" in argv:
            return "final transcript\n"
        if "kill-window" in argv:
            state["alive"] = False
            return ""
        raise AssertionError(f"unexpected tmux call: {argv}")

    h = ClaudeCodeHarness(runner=runner, sessions_root=root, host=".158")
    await h.archive(sid)

    sessions = await h.list_sessions()
    running = [s.sid for s in sessions if s.state == "running"]
    ended = [s.sid for s in sessions if s.state == "ended"]
    # left the running set, survives as an ended historical record
    assert sid not in running
    assert "lumina/lumina-abc12345" in ended


@pytest.mark.asyncio
async def test_archive_is_idempotent(tmp_path):
    """A second archive of the same sid (now gone) is a clean no-op, not a raise."""
    root = tmp_path / "agents"
    sid = "lumina-abc12345"
    state = {"alive": True}

    def runner(argv):
        if "list-windows" in argv:
            return ("monitor\t1\nlumina-abc12345\t1700000100\n"
                    if state["alive"] else "monitor\t1\n")
        if "capture-pane" in argv:
            return "t\n"
        if "kill-window" in argv:
            state["alive"] = False
            return ""
        raise AssertionError(f"unexpected tmux call: {argv}")

    h = ClaudeCodeHarness(runner=runner, sessions_root=root, host=".158")
    first = await h.archive(sid)
    second = await h.archive(sid)
    assert first["archived"] is True
    assert second["archived"] is False


# --- inject: send operator text into a running session (P1 write surface) -----


@pytest.mark.asyncio
async def test_inject_sends_send_keys_to_the_window(tmp_path):
    sid = "lumina-abc12345"
    sent: list[list[str]] = []

    def runner(argv):
        if "list-windows" in argv:
            return "monitor\t1\nlumina-abc12345\t1700000100\n"
        if "send-keys" in argv:
            sent.append(list(argv))
            return ""
        raise AssertionError(f"unexpected tmux call: {argv}")

    h = ClaudeCodeHarness(runner=runner, sessions_root=tmp_path, host=".158")
    result = await h.inject(sid, "run the tests")

    assert result == {"sid": sid, "injected": True}
    # text then a separate Enter keypress, targeting the session's tmux window
    assert sent == [["tmux", "send-keys", "-t", "skchat-agents:lumina-abc12345",
                     "run the tests", "Enter"]]


@pytest.mark.asyncio
async def test_inject_unknown_sid_is_a_clean_noop(tmp_path):
    calls: list[list[str]] = []

    def runner(argv):
        calls.append(list(argv))
        if "list-windows" in argv:
            return "monitor\t1\nlumina-abc12345\t1700000100\n"
        raise AssertionError(f"must not send keys to an unknown sid: {argv}")

    h = ClaudeCodeHarness(runner=runner, sessions_root=tmp_path, host=".158")
    result = await h.inject("opus-deadbeef", "hi")  # not a live window

    assert result["injected"] is False
    assert "no live session" in result["reason"]
    # never sent keys, only ever consulted the live window set
    assert all("send-keys" not in c for c in calls)


@pytest.mark.asyncio
async def test_inject_invalid_sid_never_touches_tmux(tmp_path):
    def runner(argv):
        raise AssertionError(f"invalid sid must not reach tmux: {argv}")

    h = ClaudeCodeHarness(runner=runner, sessions_root=tmp_path, host=".158")
    result = await h.inject("bad;name$(x)", "hi")
    assert result["injected"] is False
    assert "invalid" in result["reason"].lower()


# --- parse_repo_allowlist ----------------------------------------------------


def test_parse_repo_allowlist_empty_is_deny_all():
    assert parse_repo_allowlist("") == []
    assert parse_repo_allowlist(None) == []
    assert parse_repo_allowlist("  ,  ") == []


def test_parse_repo_allowlist_realpaths_entries(tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    got = parse_repo_allowlist(f"{a}, , {a}/../a")
    # both entries canonicalize to the same realpath
    assert got == [str(a.resolve()), str(a.resolve())]


# --- spawn: the Dispatch unlock (RCE guards + profile-by-construction) --------


def _tmux_recorder():
    """A str tmux runner that records argv and returns '' (tmux new-window prints
    nothing). Never a real tmux."""
    calls: list[list[str]] = []

    def runner(argv):
        calls.append(list(argv))
        return ""

    return runner, calls


def _git_ok():
    """A git runner where check-ref-format and worktree add both succeed."""
    calls: list[list[str]] = []

    def git(argv):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    return git, calls


def _spawn_harness(repo_root: Path, *, runner, git_runner, profile_full_cfg=False):
    kw = dict(
        host=".158",
        runner=runner,
        git_runner=git_runner,
        dispatch_repos=[str(repo_root)],
        worktree_root=repo_root.parent / "wt",
        claude_bin="claude",
        child_path="/usr/bin:/bin",
    )
    if profile_full_cfg:
        kw.update(full_agent="lumina", full_home=repo_root.parent / "home",
                  mcp_config=str(repo_root.parent / "mcp.full.json"))
    return ClaudeCodeHarness(**kw)


def _new_window_argv(calls):
    for c in calls:
        if "new-window" in c:
            return c
    raise AssertionError(f"no tmux new-window call recorded: {calls}")


def _env_pairs(new_window_argv):
    """Extract the KEY=VAL env pairs the child is handed (between 'env -i' and the
    claude binary)."""
    i = new_window_argv.index("env")
    assert new_window_argv[i + 1] == "-i"
    pairs = {}
    for tok in new_window_argv[i + 2:]:
        if tok == "claude":
            break
        k, _, v = tok.partition("=")
        pairs[k] = v
    return pairs


@pytest.mark.asyncio
async def test_spawn_sandbox_creates_worktree_named_window_and_scoped_env(tmp_path):
    repo = tmp_path / "skharness"
    repo.mkdir()
    runner, tcalls = _tmux_recorder()
    git, gcalls = _git_ok()
    h = _spawn_harness(repo, runner=runner, git_runner=git)

    desc = SessionDescriptor(repo=str(repo), branch="feat/x", model="ornith-tiny",
                             quality="sandbox")
    session = await h.spawn(desc, prompt="do the thing")

    assert isinstance(session, HarnessSession)
    assert session.sid.startswith("sandbox-")   # sandbox NEVER uses the real agent
    assert session.status == "running"
    # git worktree add ran on the allowlisted repo, on the requested branch
    assert any(c[:4] == ["git", "-C", str(repo.resolve()), "worktree"] for c in gcalls)
    add = next(c for c in gcalls if "worktree" in c and "add" in c)
    assert add[-1] == "feat/x"
    # a tmux window named exactly the sid, cwd scoped to the worktree
    nw = _new_window_argv(tcalls)
    assert nw[:3] == ["tmux", "new-window", "-t"]
    assert "-n" in nw and nw[nw.index("-n") + 1] == session.sid
    worktree = str((repo.parent / "wt" / session.sid))
    assert nw[nw.index("-c") + 1] == worktree
    # the command is exec'd directly (there is a '--' separator, no sh -c)
    assert "--" in nw
    # SANDBOX env: NO SKAGENT, HOME scoped to the worktree, no --mcp-config anywhere
    env = _env_pairs(nw)
    assert "SKAGENT" not in env
    assert env["HOME"] == worktree
    assert "--mcp-config" not in nw
    # and nothing sk* leaked into the child env
    assert not any(k.startswith("SK") for k in env)


@pytest.mark.asyncio
async def test_spawn_full_profile_wires_skagent_and_mcp(tmp_path):
    repo = tmp_path / "skharness"
    repo.mkdir()
    runner, tcalls = _tmux_recorder()
    git, _ = _git_ok()
    h = _spawn_harness(repo, runner=runner, git_runner=git, profile_full_cfg=True)

    desc = SessionDescriptor(repo=str(repo), branch="main", quality="full")
    session = await h.spawn(desc, prompt="orchestrate")

    assert session.sid.startswith("lumina-")   # full uses the real operator agent
    nw = _new_window_argv(tcalls)
    env = _env_pairs(nw)
    # FULL env: SKAGENT wired, HOME is the real agent home (NOT the worktree)
    assert env["SKAGENT"] == "lumina"
    assert env["HOME"] == str(repo.parent / "home")
    # sk* MCP config wired for full only
    assert "--mcp-config" in nw
    assert nw[nw.index("--mcp-config") + 1] == str(repo.parent / "mcp.full.json")


@pytest.mark.asyncio
async def test_spawn_prompt_is_data_never_shell(tmp_path):
    repo = tmp_path / "skharness"
    repo.mkdir()
    runner, tcalls = _tmux_recorder()
    git, _ = _git_ok()
    h = _spawn_harness(repo, runner=runner, git_runner=git)

    evil = "rm -rf / ; $(whoami) `id` && echo pwned"
    await h.spawn(SessionDescriptor(repo=str(repo), branch="x", quality="sandbox"),
                  prompt=evil)
    nw = _new_window_argv(tcalls)
    # the prompt survives verbatim as ONE argv element (data), never split/expanded
    assert evil in nw
    assert nw.count(evil) == 1
    # no shell was ever invoked
    joined = " ".join(nw)
    assert "sh -c" not in joined and "/bin/sh" not in joined and "bash" not in joined


@pytest.mark.asyncio
async def test_spawn_rejects_repo_not_in_allowlist(tmp_path):
    repo = tmp_path / "allowed"
    repo.mkdir()
    other = tmp_path / "evil"
    other.mkdir()
    runner, tcalls = _tmux_recorder()
    git, gcalls = _git_ok()
    h = _spawn_harness(repo, runner=runner, git_runner=git)

    with pytest.raises(SpawnRejected, match="allowlist"):
        await h.spawn(SessionDescriptor(repo=str(other), branch="x", quality="sandbox"),
                      prompt="p")
    # nothing touched: no git, no tmux
    assert gcalls == []
    assert tcalls == []


@pytest.mark.asyncio
async def test_spawn_empty_allowlist_is_deny_all(tmp_path):
    repo = tmp_path / "skharness"
    repo.mkdir()
    runner, tcalls = _tmux_recorder()
    git, gcalls = _git_ok()
    h = ClaudeCodeHarness(host=".158", runner=runner, git_runner=git,
                          dispatch_repos=[], worktree_root=tmp_path / "wt")
    with pytest.raises(SpawnRejected, match="deny all"):
        await h.spawn(SessionDescriptor(repo=str(repo), branch="x", quality="sandbox"),
                      prompt="p")
    assert gcalls == [] and tcalls == []


@pytest.mark.asyncio
async def test_spawn_rejects_bad_branch_via_check_ref_format(tmp_path):
    repo = tmp_path / "skharness"
    repo.mkdir()
    runner, tcalls = _tmux_recorder()
    gcalls: list[list[str]] = []

    def git(argv):
        gcalls.append(list(argv))
        # check-ref-format fails; worktree add must never be reached
        if "check-ref-format" in argv:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="bad ref")
        raise AssertionError(f"worktree add must not run on a bad branch: {argv}")

    h = _spawn_harness(repo, runner=runner, git_runner=git)
    with pytest.raises(SpawnRejected, match="check-ref-format"):
        await h.spawn(SessionDescriptor(repo=str(repo), branch="--evil", quality="sandbox"),
                      prompt="p")
    # validated the branch, never added a worktree, never opened a window
    assert any("check-ref-format" in c for c in gcalls)
    assert all("worktree" not in c for c in gcalls)
    assert tcalls == []


@pytest.mark.asyncio
async def test_spawn_rejects_bad_charset_agent_name(tmp_path):
    repo = tmp_path / "skharness"
    repo.mkdir()
    runner, tcalls = _tmux_recorder()
    git, gcalls = _git_ok()
    # a full-profile agent name with shell/charset-breaking chars
    h = ClaudeCodeHarness(host=".158", runner=runner, git_runner=git,
                          dispatch_repos=[str(repo)], worktree_root=tmp_path / "wt",
                          full_agent="bad;name$(x)")
    with pytest.raises(SpawnRejected, match="charset"):
        await h.spawn(SessionDescriptor(repo=str(repo), branch="main", quality="full"),
                      prompt="p")
    assert tcalls == []


@pytest.mark.asyncio
async def test_spawn_rejects_invalid_profile(tmp_path):
    repo = tmp_path / "skharness"
    repo.mkdir()
    runner, tcalls = _tmux_recorder()
    git, gcalls = _git_ok()
    h = _spawn_harness(repo, runner=runner, git_runner=git)
    with pytest.raises(SpawnRejected, match="profile"):
        await h.spawn(SessionDescriptor(repo=str(repo), branch="x", quality="root"),
                      prompt="p")
    assert gcalls == [] and tcalls == []


@pytest.mark.asyncio
async def test_spawn_worktree_add_failure_is_rejected_no_window(tmp_path):
    repo = tmp_path / "skharness"
    repo.mkdir()
    runner, tcalls = _tmux_recorder()

    def git(argv):
        if "check-ref-format" in argv:
            return subprocess.CompletedProcess(argv, 0)
        if "worktree" in argv:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="fatal: exists")
        return subprocess.CompletedProcess(argv, 0)

    h = _spawn_harness(repo, runner=runner, git_runner=git)
    with pytest.raises(SpawnRejected, match="worktree add failed"):
        await h.spawn(SessionDescriptor(repo=str(repo), branch="x", quality="sandbox"),
                      prompt="p")
    # worktree add failed => NO tmux window opened
    assert tcalls == []
