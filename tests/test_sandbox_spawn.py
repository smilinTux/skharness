import json
import subprocess
from pathlib import Path
import pytest
from skharness.autocode.sandbox import Sandbox, LaunchSpec
from skharness.autocode.claude_code import HarnessUnavailable
from skharness.autocode.sandbox_lifecycle import (
    RESOURCE_ROLE_LABEL,
    RUN_ID_LABEL,
    SandboxOwnership,
)


def _spec():
    return LaunchSpec(name="pi", argv=["pi", "-p", "x", "--mode", "json"],
                      image="sandbox-pi:1", worktree="/tmp/wt",
                      egress_hosts=["gw.local"])


def _linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    common = tmp_path / "repo" / ".git"
    admin = common / "worktrees" / "ticket"
    admin.mkdir(parents=True)
    (admin / "commondir").write_text("../..\n", encoding="utf-8")
    worktree = tmp_path / "ticket"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
    return worktree, common


def test_linked_worktree_common_git_dir_is_mounted_readonly_at_original_path(tmp_path):
    worktree, common = _linked_worktree(tmp_path)
    spec = LaunchSpec("pi", ["pi"], "pi-image", str(worktree))

    argv = Sandbox()._docker_run_argv(spec, "net", "proxy")

    mount = f"type=bind,src={common.resolve()},dst={common.resolve()},readonly"
    assert mount in argv
    assert f"type=bind,src={worktree.resolve()},dst=/work" in argv


def test_normal_checkout_does_not_gain_an_external_git_metadata_mount(tmp_path):
    worktree = tmp_path / "checkout"
    (worktree / ".git").mkdir(parents=True)
    spec = LaunchSpec("pi", ["pi"], "pi-image", str(worktree))

    argv = Sandbox()._docker_run_argv(spec, "net", "proxy")

    assert not any(
        value.endswith("readonly") and "/.git,dst=" in value for value in argv
    )


def test_malformed_linked_worktree_pointer_fails_before_docker_preflight(
    tmp_path, monkeypatch
):
    worktree = tmp_path / "ticket"
    worktree.mkdir()
    (worktree / ".git").write_text("not a gitdir pointer\n", encoding="utf-8")
    docker_calls = []
    monkeypatch.setattr("skharness.autocode.sandbox.shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(
        "skharness.autocode.sandbox.subprocess.run",
        lambda argv, **kwargs: docker_calls.append(argv),
    )

    with pytest.raises(HarnessUnavailable, match="malformed linked-worktree"):
        Sandbox()._ensure_capable(LaunchSpec("pi", ["pi"], "pi-image", str(worktree)))

    assert docker_calls == []


def test_linked_worktree_admin_dir_must_be_inside_common_dir(tmp_path):
    worktree = tmp_path / "ticket"
    worktree.mkdir()
    admin = tmp_path / "admin"
    admin.mkdir()
    common = tmp_path / "common"
    common.mkdir()
    (admin / "commondir").write_text(str(common), encoding="utf-8")
    (worktree / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")

    with pytest.raises(HarnessUnavailable, match="escapes its Git common directory"):
        Sandbox()._docker_run_argv(
            LaunchSpec("pi", ["pi"], "pi-image", str(worktree)), "net", "proxy"
        )


def test_spawn_disabled_raises_when_not_live():
    with pytest.raises(HarnessUnavailable):
        Sandbox(live_execution=False).spawn(_spec(), repo_remote_host="github.com", ci_host=None)


def test_image_preflight_fails_clearly_when_required_test_command_is_absent(monkeypatch):
    def fake_run(argv, **kwargs):
        class Result:
            returncode = 1 if "command -v pytest" in argv else 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr("skharness.autocode.sandbox.shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr("skharness.autocode.sandbox.subprocess.run", fake_run)
    spec = _spec()
    spec.required_commands = ["pytest"]
    with pytest.raises(HarnessUnavailable, match="test-capable image"):
        Sandbox()._ensure_capable(spec)


def test_image_preflight_executes_required_behavior_check_without_network(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr("skharness.autocode.sandbox.shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr("skharness.autocode.sandbox.subprocess.run", fake_run)
    spec = _spec()
    spec.required_checks = [["/usr/local/bin/project-preflight", "--quick"]]

    Sandbox()._ensure_capable(spec)

    assert [
        "docker", "run", "--rm", "--network", "none", "--read-only",
        "--entrypoint", "/usr/local/bin/project-preflight", spec.image, "--quick",
    ] in calls


def test_image_preflight_fails_closed_when_behavior_check_fails(monkeypatch):
    def fake_run(argv, **kwargs):
        class Result:
            returncode = 7 if "--entrypoint" in argv else 0
            stdout = ""
            stderr = "pytest cannot import project"

        return Result()

    monkeypatch.setattr("skharness.autocode.sandbox.shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr("skharness.autocode.sandbox.subprocess.run", fake_run)
    spec = _spec()
    spec.required_checks = [["/usr/local/bin/project-preflight"]]

    with pytest.raises(HarnessUnavailable, match="pytest cannot import project"):
        Sandbox()._ensure_capable(spec)


def test_supervisor_scoped_image_preflight_timeout_fails_closed(monkeypatch):
    observed = []

    def hangs(argv, **kwargs):
        observed.append(kwargs["timeout"])
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    sandbox = Sandbox(docker_command_timeout_s=3, monotonic=lambda: 10)
    monkeypatch.setattr("skharness.autocode.sandbox.shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr("skharness.autocode.sandbox.subprocess.run", hangs)

    with pytest.raises(HarnessUnavailable, match="Docker preflight timed out"):
        sandbox._ensure_capable(_spec(), deadline=13)

    assert observed == [3]


def test_supervisor_preflight_uses_aggregate_deadline_and_checks_cancel_between_probes(
    monkeypatch,
):
    calls = []
    ticks = iter((0.0, 2.0, 4.0, 6.0, 8.0))
    sandbox = Sandbox(docker_command_timeout_s=3, monotonic=lambda: next(ticks))
    spec = _spec()
    spec.required_commands = ["python", "pytest"]
    spec.required_checks = [["preflight"], ["preflight", "--deep"]]

    def succeeds(argv, **kwargs):
        calls.append((argv, kwargs["timeout"]))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr("skharness.autocode.sandbox.shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr("skharness.autocode.sandbox.subprocess.run", succeeds)

    sandbox._ensure_capable(
        spec,
        deadline=30,
        cancelled=lambda: False,
        ownership=SandboxOwnership("lease-preflight", authority="lease"),
        container_name="arena-pi-preflight",
    )

    assert len(calls) == 5
    assert [timeout for _argv, timeout in calls] == [3, 3, 3, 3, 3]
    for argv, _timeout in calls[1:]:
        assert argv[argv.index("--name") + 1] == "arena-pi-preflight"
        assert f"{RUN_ID_LABEL}=lease-preflight" in argv
        assert f"{RESOURCE_ROLE_LABEL}=worker" in argv

    calls.clear()
    cancelled = iter((False, False, True))
    sandbox = Sandbox(docker_command_timeout_s=3, monotonic=lambda: 0)
    with pytest.raises(HarnessUnavailable, match="preflight cancelled"):
        sandbox._ensure_capable(spec, deadline=30, cancelled=lambda: next(cancelled))

    assert len(calls) == 1


def test_proxy_readiness_retries_until_listening(monkeypatch):
    results = iter([1, 1, 0])

    def fake_run(argv, **kw):
        class P:
            returncode = next(results)
            stdout = ""
            stderr = ""
        return P()

    monkeypatch.setattr("skharness.autocode.sandbox.subprocess.run", fake_run)
    Sandbox()._wait_for_proxy("proxy", attempts=3)


def test_proxy_readiness_fails_closed_with_logs(monkeypatch):
    def fake_run(argv, **kw):
        class P:
            returncode = 1
            stdout = ""
            stderr = "startup failed" if "logs" in argv else ""
        return P()

    monkeypatch.setattr("skharness.autocode.sandbox.subprocess.run", fake_run)
    with pytest.raises(HarnessUnavailable, match="startup failed"):
        Sandbox()._wait_for_proxy("proxy", attempts=2)


def test_spawn_runs_container_and_tears_down(monkeypatch):
    calls = []
    def fake_run(argv, **kw):
        calls.append(argv)
        class P:
            returncode = 0
            stdout = json.dumps({"result": {"ok": True}}) if argv[:2] == ["docker", "run"] else ""
            stderr = ""
        return P()
    sb = Sandbox(live_execution=True)
    monkeypatch.setattr(sb, "_ensure_capable", lambda spec: None)
    monkeypatch.setattr("skharness.autocode.sandbox.subprocess.run", fake_run)
    spec = _spec()
    spec.sandbox_run_id = "lease-from-controller"
    out = sb.spawn(spec, repo_remote_host="github.com", ci_host="ci.local")
    assert out == {"result": {"ok": True}}
    kinds = [c[1] for c in calls if c and c[0] == "docker"]
    assert "network" in kinds and "run" in kinds          # created a network and ran
    assert any(c[0] == "docker" and "network" in c and "rm" in c for c in calls)  # net teardown
    # the harness container is torn down by name (rm -f), not just the network/proxy
    assert any(c[0] == "docker" and c[1] == "rm" and "-f" in c and
              any(str(a).startswith("sbxrun-") for a in c) for c in calls)
    # the proxy was started with the assembled allowlist (repo + ci + egress hosts)
    proxy_start = next(c for c in calls if c[0] == "docker" and "run" in c and "-d" in c)
    for host in ("github.com", "ci.local", "gw.local"):
        assert host in proxy_start
    network_create = next(c for c in calls if c[1:3] == ["network", "create"])
    worker_start = next(c for c in calls if c[:2] == ["docker", "run"] and "-d" not in c)
    for command, role in (
        (network_create, "network"),
        (proxy_start, "proxy"),
        (worker_start, "worker"),
    ):
        labels = {
            value.split("=", 1)[0]: value.split("=", 1)[1]
            for index, value in enumerate(command)
            if index and command[index - 1] == "--label"
        }
        assert labels[RUN_ID_LABEL] == "lease-from-controller"
        assert labels[RESOURCE_ROLE_LABEL] == role


def test_spawn_passes_stdin_to_container_subprocess(monkeypatch):
    seen_kwargs = []
    def fake_run(argv, **kw):
        if "cwd" in kw:                         # only the harness container run sets this
            seen_kwargs.append(kw)
        class P:
            returncode = 0
            stdout = json.dumps({"result": {"ok": True}}) if argv[:2] == ["docker", "run"] else ""
            stderr = ""
        return P()
    sb = Sandbox(live_execution=True)
    monkeypatch.setattr(sb, "_ensure_capable", lambda spec: None)
    monkeypatch.setattr("skharness.autocode.sandbox.subprocess.run", fake_run)
    spec = LaunchSpec(name="opencode", argv=["opencode", "run", "--format", "json"],
                      image="sandbox-opencode:1", worktree="/tmp/wt",
                      egress_hosts=["gw.local"], stdin="PROMPT")
    sb.spawn(spec, repo_remote_host="github.com", ci_host="ci.local")
    assert len(seen_kwargs) == 1
    assert seen_kwargs[0]["input"] == "PROMPT"


def test_spawn_omits_input_when_stdin_is_none(monkeypatch):
    seen_kwargs = []
    def fake_run(argv, **kw):
        if "cwd" in kw:                         # only the harness container run sets this
            seen_kwargs.append(kw)
        class P:
            returncode = 0
            stdout = json.dumps({"result": {"ok": True}}) if argv[:2] == ["docker", "run"] else ""
            stderr = ""
        return P()
    sb = Sandbox(live_execution=True)
    monkeypatch.setattr(sb, "_ensure_capable", lambda spec: None)
    monkeypatch.setattr("skharness.autocode.sandbox.subprocess.run", fake_run)
    sb.spawn(_spec(), repo_remote_host="github.com", ci_host="ci.local")
    assert len(seen_kwargs) == 1
    assert "input" not in seen_kwargs[0]


def test_spawn_preserves_partial_stdout_on_timeout(monkeypatch):
    import subprocess
    # the harness container run (the one with `timeout=`) over-runs and is killed;
    # subprocess.run raises TimeoutExpired carrying the partial captured stdout.
    partial = ('{"type":"text","part":{"type":"text",'
               '"text":"{\\"verdict\\":\\"valid\\"}"}}\n'
               '{"type":"text","part":{"type":"text","text":"## rambling..."}}\n')

    def fake_run(argv, **kw):
        if "timeout" in kw:
            raise subprocess.TimeoutExpired(argv, kw["timeout"], output=partial)
        class P:
            returncode = 0
            stdout = ""
            stderr = ""
        return P()
    sb = Sandbox(live_execution=True)
    monkeypatch.setattr(sb, "_ensure_capable", lambda spec: None)
    monkeypatch.setattr("skharness.autocode.sandbox.subprocess.run", fake_run)
    out = sb.spawn(_spec(), repo_remote_host="github.com", ci_host=None)
    assert out["timeout"] is True and out["exit_code"] == 124
    # the partial stream survives so the adapter's _parse can still recover the answer
    assert out["result"] == partial
