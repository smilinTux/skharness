from __future__ import annotations

import json
import subprocess
import threading
from datetime import datetime, timedelta, timezone

import pytest

from skharness.pi_spawn_control import (
    ControlDeniedError,
    SpawnControl,
    StateUnavailableError,
    launch_main,
    main,
)

NOW = datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc)


def control(tmp_path) -> SpawnControl:
    return SpawnControl(tmp_path / "pi-spawn.json", clock=lambda: NOW)


def test_missing_and_malformed_state_fail_closed(tmp_path):
    gate = control(tmp_path)
    with pytest.raises(StateUnavailableError):
        gate.reserve("worker-1", actor="controller", scope="pi:all", kind="process")
    gate.path.write_text("not json", encoding="utf-8")
    with pytest.raises(StateUnavailableError):
        gate.status()
    gate.path.unlink()
    gate.path.symlink_to(tmp_path / "missing-target")
    with pytest.raises(StateUnavailableError):
        gate.status()


def test_unknown_operation_shape_fails_closed(tmp_path):
    gate = control(tmp_path)
    gate.bootstrap(actor="installer")
    state = json.loads(gate.path.read_text(encoding="utf-8"))
    state["last_operation"]["credential"] = "must-not-pass"
    gate.path.write_text(json.dumps(state), encoding="utf-8")
    gate.path.chmod(0o600)
    with pytest.raises(StateUnavailableError):
        gate.status()


def test_bootstrap_pause_drain_status_renew_expiry_and_resume(tmp_path):
    gate = control(tmp_path)
    opened = gate.bootstrap(actor="installer")
    assert opened["mode"] == "open"
    assert gate.bootstrap(actor="installer") == opened

    paused = gate.pause(
        mode="pause",
        owner="operator-a",
        reason="reserve canary",
        scope="pi:all",
        ttl_seconds=300,
        expected_fence=0,
    )
    assert gate.pause(
        mode="pause",
        owner="operator-a",
        reason="reserve canary",
        scope="pi:all",
        ttl_seconds=300,
        expected_fence=0,
    ) == paused
    with pytest.raises(ControlDeniedError):
        gate.reserve("worker-2", actor="wave", scope="pi:all", kind="tmux")

    renewed = gate.renew(owner="operator-a", fence=1, ttl_seconds=600)
    assert renewed["fence"] == 2
    assert renewed["expires_at"] > paused["expires_at"]
    assert gate.renew(owner="operator-a", fence=1, ttl_seconds=600) == renewed
    with pytest.raises(ControlDeniedError):
        gate.renew(owner="operator-b", fence=2, ttl_seconds=60)
    with pytest.raises(ControlDeniedError):
        gate.resume(owner="operator-a", fence=1)

    gate.clock = lambda: NOW + timedelta(minutes=11)
    assert gate.status()["effective_mode"] == "expired"
    with pytest.raises(ControlDeniedError):
        gate.reserve("worker-3", actor="retry", scope="pi:all", kind="process")
    resumed = gate.resume(owner="operator-a", fence=2)
    assert resumed["mode"] == "open"
    assert resumed["fence"] == 3
    assert gate.resume(owner="operator-a", fence=2) == resumed


def test_drain_preserves_and_reports_exact_workers(tmp_path):
    gate = control(tmp_path)
    gate.bootstrap(actor="installer")
    first = gate.reserve("worker-a", actor="pool", scope="pi:all", kind="process")
    second = gate.reserve("worker-b", actor="repair", scope="pi:all", kind="tmux")
    drained = gate.pause(
        mode="drain",
        owner="operator-a",
        reason="canary",
        scope="pi:all",
        ttl_seconds=300,
        expected_fence=0,
    )
    assert [item["worker_id"] for item in drained["workers"]] == ["worker-a", "worker-b"]
    assert all("token" not in item for item in drained["workers"])
    assert drained["quiescent"] is False
    with pytest.raises(ControlDeniedError):
        gate.reserve("worker-c", actor="wave", scope="pi:all", kind="process")
    gate.finish("worker-a", token=first.token)
    assert [item["worker_id"] for item in gate.status()["workers"]] == ["worker-b"]
    gate.finish("worker-b", token=second.token)
    assert gate.status()["quiescent"] is True


def test_guarded_run_registers_before_spawn_and_cleans_after(tmp_path):
    gate = control(tmp_path)
    gate.bootstrap(actor="installer")
    seen = []

    def fake_run(argv, **kwargs):
        snapshot = json.loads(gate.path.read_text(encoding="utf-8"))
        seen.append((argv, kwargs, sorted(snapshot["workers"])))
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    result = gate.guarded_run(
        ["fake-pi", "--test"],
        worker_id="worker-a",
        actor="pool",
        scope="pi:all",
        kind="process",
        runner=fake_run,
        capture_output=True,
    )
    assert result.returncode == 0
    assert seen[0][2] == ["worker-a"]
    assert gate.status()["workers"] == []

    def fails(argv, **kwargs):
        raise RuntimeError("synthetic failure")

    with pytest.raises(RuntimeError, match="synthetic failure"):
        gate.guarded_run(
            ["fake-pi"],
            worker_id="worker-failed",
            actor="pool",
            scope="pi:all",
            kind="process",
            runner=fails,
        )
    assert gate.status()["workers"] == []


def test_pause_race_sees_reserved_worker_and_never_interrupts_it(tmp_path):
    gate = control(tmp_path)
    gate.bootstrap(actor="installer")
    entered = threading.Event()
    release = threading.Event()

    def fake_run(argv, **kwargs):
        entered.set()
        release.wait(timeout=2)
        return subprocess.CompletedProcess(argv, 0)

    thread = threading.Thread(
        target=lambda: gate.guarded_run(
            ["fake-pi"],
            worker_id="worker-a",
            actor="pool",
            scope="pi:all",
            kind="process",
            runner=fake_run,
        )
    )
    thread.start()
    assert entered.wait(timeout=2)
    paused = gate.pause(
        mode="drain",
        owner="operator-a",
        reason="canary",
        scope="pi:all",
        ttl_seconds=300,
        expected_fence=0,
    )
    assert [item["worker_id"] for item in paused["workers"]] == ["worker-a"]
    assert thread.is_alive()
    release.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert gate.status()["quiescent"] is True


def test_concurrent_pause_has_one_fenced_winner(tmp_path):
    gate = control(tmp_path)
    gate.bootstrap(actor="installer")
    outcomes = []

    def attempt(owner):
        try:
            outcomes.append(gate.pause(
                mode="pause",
                owner=owner,
                reason="test",
                scope="pi:all",
                ttl_seconds=60,
                expected_fence=0,
            )["owner"])
        except ControlDeniedError:
            outcomes.append("denied")

    threads = [threading.Thread(target=attempt, args=(owner,)) for owner in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) in (["a", "denied"], ["b", "denied"])


def test_concurrent_resume_and_pause_are_serialized(tmp_path):
    gate = control(tmp_path)
    gate.bootstrap(actor="installer")
    gate.pause(
        mode="pause",
        owner="owner",
        reason="test",
        scope="pi:all",
        ttl_seconds=60,
        expected_fence=0,
    )
    outcomes = []

    def resume():
        try:
            outcomes.append(gate.resume(owner="owner", fence=1)["mode"])
        except ControlDeniedError:
            outcomes.append("resume-denied")

    def pause():
        try:
            outcomes.append(gate.pause(
                mode="drain",
                owner="next-owner",
                reason="next",
                scope="pi:all",
                ttl_seconds=60,
                expected_fence=1,
            )["mode"])
        except ControlDeniedError:
            outcomes.append("pause-denied")

    threads = [threading.Thread(target=resume), threading.Thread(target=pause)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(outcomes) == 2
    assert gate.status()["mode"] in {"open", "drain"}


def test_bounds_and_validation(tmp_path):
    gate = control(tmp_path)
    gate.bootstrap(actor="installer")
    for ttl in (0, 3601):
        with pytest.raises(ValueError):
            gate.pause(
                mode="pause",
                owner="owner",
                reason="reason",
                scope="pi:all",
                ttl_seconds=ttl,
                expected_fence=0,
            )
    with pytest.raises(ValueError):
        gate.reserve("bad worker", actor="pool", scope="pi:all", kind="process")
    with pytest.raises(ValueError):
        gate.reserve("worker", actor="pool", scope="pi:subset", kind="process")


def test_cli_process_and_tmux_mutations_use_same_final_guard(tmp_path, monkeypatch, capsys):
    state = tmp_path / "control.json"
    assert main(["--state", str(state), "bootstrap", "--actor", "installer"]) == 0
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr("skharness.pi_spawn_control.subprocess.run", fake_run)
    for kind in ("process", "tmux"):
        assert launch_main([
            "--state", str(state),
            "--worker-id", f"worker-{kind}",
            "--actor", "wave",
            "--scope", "pi:all",
            "--kind", kind,
            "--", f"fake-{kind}", "--test",
        ]) == 0
    assert calls == [["fake-process", "--test"], ["fake-tmux", "--test"]]
    assert SpawnControl(state).status()["workers"] == []

    assert main([
        "--state", str(state), "drain",
        "--owner", "operator", "--reason", "canary window",
        "--scope", "pi:all", "--ttl-seconds", "60", "--expected-fence", "0",
    ]) == 0
    assert launch_main([
        "--state", str(state),
        "--worker-id", "worker-denied",
        "--actor", "retry", "--scope", "pi:all", "--kind", "tmux",
        "--", "fake-tmux",
    ]) == 2
    assert calls == [["fake-process", "--test"], ["fake-tmux", "--test"]]
    assert "control mode is drain" in capsys.readouterr().out
