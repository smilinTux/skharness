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


def rewrite_state(gate: SpawnControl, mutate) -> None:
    state = json.loads(gate.path.read_text(encoding="utf-8"))
    mutate(state)
    gate.path.write_text(json.dumps(state), encoding="utf-8")


def future_worker_state(gate: SpawnControl, reserved_at: object) -> None:
    gate.bootstrap(actor="installer")
    gate.reserve("existing", actor="pool", scope="pi:all", kind="process")
    rewrite_state(
        gate,
        lambda state: state["workers"]["existing"].update(reserved_at=reserved_at),
    )


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


@pytest.mark.parametrize(
    "reserved_at",
    [
        "2026-08-25T14:59:59.999999Z",
        "2026-08-25T15:00:00.000001Z",
        "2026-08-25T16:00:00Z",
        "2026-08-25T10:00:00-05:00",
        "2026-08-25T15:00:00.1Z",
        "2026-08-25T15:00:00.0000001Z",
        "not-a-time",
        float("inf"),
    ],
)
def test_invalid_worker_time_fails_before_status_reservation_or_process(tmp_path, reserved_at):
    gate = control(tmp_path)
    future_worker_state(gate, reserved_at)
    before = gate.path.read_bytes()
    calls = []

    with pytest.raises(StateUnavailableError):
        gate.status()
    with pytest.raises(StateUnavailableError):
        gate.reserve("second", actor="pool", scope="pi:all", kind="process")
    with pytest.raises(StateUnavailableError):
        gate.guarded_run(
            ["fake-pi"], worker_id="third", actor="pool", scope="pi:all",
            kind="process", runner=lambda argv: calls.append(argv),
        )

    assert calls == []
    assert gate.path.read_bytes() == before


def test_mixed_worker_times_and_duplicate_identity_fail_closed(tmp_path):
    gate = control(tmp_path)
    gate.bootstrap(actor="installer")
    gate.reserve("valid", actor="pool", scope="pi:all", kind="process")
    gate.reserve("invalid", actor="pool", scope="pi:all", kind="tmux")
    rewrite_state(
        gate,
        lambda state: state["workers"]["invalid"].update(
            reserved_at="2026-08-25T15:00:00.000001Z"
        ),
    )
    with pytest.raises(StateUnavailableError):
        gate.status()

    rewrite_state(
        gate,
        lambda state: state["workers"]["invalid"].update(
            reserved_at="2026-08-25T15:00:00Z", worker_id="valid"
        ),
    )
    with pytest.raises(StateUnavailableError):
        gate.status()


def test_equal_worker_time_and_canonical_microseconds_are_valid(tmp_path):
    precise_now = NOW.replace(microsecond=123456)
    gate = SpawnControl(tmp_path / "pi-spawn.json", clock=lambda: precise_now)
    gate.bootstrap(actor="installer")
    gate.reserve("worker", actor="pool", scope="pi:all", kind="process")

    status = gate.status()

    assert status["workers"][0]["reserved_at"] == "2026-08-25T15:00:00.123456Z"
    assert status["updated_at"] == status["created_at"]


def test_clock_regression_fails_closed_without_state_write(tmp_path):
    current = [NOW]
    gate = SpawnControl(tmp_path / "pi-spawn.json", clock=lambda: current[0])
    gate.bootstrap(actor="installer")
    before = gate.path.read_bytes()
    current[0] = NOW - timedelta(microseconds=1)

    with pytest.raises(StateUnavailableError):
        gate.reserve("worker", actor="pool", scope="pi:all", kind="process")

    assert gate.path.read_bytes() == before


def test_each_boundary_uses_one_explicit_clock_sample(tmp_path):
    calls = []

    def clock():
        calls.append(None)
        if len(calls) > 1:
            raise AssertionError("clock sampled more than once")
        return NOW

    gate = SpawnControl(tmp_path / "pi-spawn.json", clock=clock)
    gate.bootstrap(actor="installer")
    for action in (
        gate.status,
        lambda: gate.reserve("worker", actor="pool", scope="pi:all", kind="process"),
    ):
        calls.clear()
        action()
        assert len(calls) == 1


def test_concurrent_duplicate_reservation_has_one_winner(tmp_path):
    gate = control(tmp_path)
    gate.bootstrap(actor="installer")
    outcomes = []

    def attempt(actor):
        try:
            outcomes.append(gate.reserve(
                "same-worker", actor=actor, scope="pi:all", kind="process"
            ).worker_id)
        except ControlDeniedError:
            outcomes.append("denied")

    threads = [threading.Thread(target=attempt, args=(actor,)) for actor in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["denied", "same-worker"]
    assert [worker["worker_id"] for worker in gate.status()["workers"]] == ["same-worker"]


@pytest.mark.parametrize("kind", ["process", "tmux"])
def test_snapshot_replacement_before_final_mutation_fails_with_zero_calls(
    tmp_path, monkeypatch, kind
):
    gate = control(tmp_path)
    gate.bootstrap(actor="installer")
    original_reserve = gate.reserve
    calls = []

    def reserve_then_replace(*args, **kwargs):
        reservation = original_reserve(*args, **kwargs)
        original_reserve(
            "interloper", actor="other-pool", scope="pi:all", kind="process"
        )
        return reservation

    monkeypatch.setattr(gate, "reserve", reserve_then_replace)
    with pytest.raises(ControlDeniedError):
        gate.guarded_run(
            [f"fake-{kind}"], worker_id=f"worker-{kind}", actor="pool",
            scope="pi:all", kind=kind, runner=lambda argv: calls.append(argv),
        )
    assert calls == []


def test_unknown_operation_shape_fails_closed(tmp_path):
    gate = control(tmp_path)
    gate.bootstrap(actor="installer")
    state = json.loads(gate.path.read_text(encoding="utf-8"))
    state["last_operation"]["credential"] = "must-not-pass"
    gate.path.write_text(json.dumps(state), encoding="utf-8")
    gate.path.chmod(0o600)
    with pytest.raises(StateUnavailableError):
        gate.status()


def test_pause_replay_rejects_open_state_and_cannot_reserve(tmp_path):
    gate = control(tmp_path)
    gate.bootstrap(actor="installer")
    state = json.loads(gate.path.read_text(encoding="utf-8"))
    state["last_operation"] = {
        "command": "pause",
        "owner": "operator-a",
        "reason": "reserve canary",
        "scope": "pi:all",
        "ttl_seconds": 300,
        "expected_fence": 0,
    }
    gate.path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(StateUnavailableError):
        gate.pause(
            mode="pause",
            owner="operator-a",
            reason="reserve canary",
            scope="pi:all",
            ttl_seconds=300,
            expected_fence=0,
        )
    with pytest.raises(StateUnavailableError):
        gate.reserve("worker-bypass", actor="wave", scope="pi:all", kind="process")


def test_status_rejects_resume_record_with_mismatched_fence(tmp_path):
    gate = control(tmp_path)
    gate.bootstrap(actor="installer")
    state = json.loads(gate.path.read_text(encoding="utf-8"))
    state["last_operation"] = {"command": "resume", "owner": "operator-a", "fence": 99}
    gate.path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(StateUnavailableError):
        gate.status()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda state: state.update(mode="open", owner=None, reason=None, scope=None, expires_at=None),
        lambda state: state.update(fence=0),
        lambda state: state.update(fence=2),
        lambda state: state.update(owner="other-owner"),
        lambda state: state.update(reason="other reason"),
        lambda state: state.update(scope="pi:other"),
        lambda state: state.update(expires_at="2026-08-25T15:05:01Z"),
        lambda state: state["last_operation"].update(applied_at="2026-08-25T15:00:01Z"),
    ],
)
def test_pause_history_mismatch_fails_closed(tmp_path, mutate):
    gate = control(tmp_path)
    gate.bootstrap(actor="installer")
    gate.pause(
        mode="pause",
        owner="operator-a",
        reason="reserve canary",
        scope="pi:all",
        ttl_seconds=300,
        expected_fence=0,
    )
    rewrite_state(gate, mutate)

    with pytest.raises(StateUnavailableError):
        gate.status()
    with pytest.raises(StateUnavailableError):
        gate.reserve("worker-bypass", actor="wave", scope="pi:all", kind="process")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda state: state.update(mode="drain"),
        lambda state: state.update(fence=1),
        lambda state: state.update(fence=3),
        lambda state: state.update(owner="other-owner"),
        lambda state: state.update(reason="other reason"),
        lambda state: state.update(expires_at="2026-08-25T15:10:01Z"),
        lambda state: state["last_operation"].update(mode="drain"),
        lambda state: state["last_operation"].update(scope="pi:other"),
    ],
)
def test_renew_history_mismatch_fails_closed(tmp_path, mutate):
    gate = control(tmp_path)
    gate.bootstrap(actor="installer")
    gate.pause(
        mode="pause",
        owner="operator-a",
        reason="reserve canary",
        scope="pi:all",
        ttl_seconds=300,
        expected_fence=0,
    )
    gate.renew(owner="operator-a", fence=1, ttl_seconds=600)
    rewrite_state(gate, mutate)

    with pytest.raises(StateUnavailableError):
        gate.status()


def test_strict_replay_rejects_changed_bootstrap_renew_and_resume(tmp_path):
    gate = control(tmp_path)
    gate.bootstrap(actor="installer")
    with pytest.raises(ControlDeniedError):
        gate.bootstrap(actor="other-installer")

    gate.pause(
        mode="drain",
        owner="operator-a",
        reason="reserve canary",
        scope="pi:all",
        ttl_seconds=300,
        expected_fence=0,
    )
    gate.renew(owner="operator-a", fence=1, ttl_seconds=600)
    before = gate.path.read_bytes()
    with pytest.raises(ControlDeniedError):
        gate.renew(owner="operator-a", fence=1, ttl_seconds=601)
    assert gate.path.read_bytes() == before

    gate.resume(owner="operator-a", fence=2)
    before = gate.path.read_bytes()
    with pytest.raises(ControlDeniedError):
        gate.resume(owner="other-owner", fence=2)
    with pytest.raises(ControlDeniedError):
        gate.resume(owner="operator-a", fence=1)
    assert gate.path.read_bytes() == before


def test_valid_replay_survives_worker_registry_updates(tmp_path):
    gate = control(tmp_path)
    gate.bootstrap(actor="installer")
    reservation = gate.reserve("worker-a", actor="pool", scope="pi:all", kind="process")
    assert gate.bootstrap(actor="installer")["workers"][0]["worker_id"] == "worker-a"
    paused = gate.pause(
        mode="drain",
        owner="operator-a",
        reason="reserve canary",
        scope="pi:all",
        ttl_seconds=300,
        expected_fence=0,
    )
    gate.finish("worker-a", token=reservation.token)
    replay = gate.pause(
        mode="drain",
        owner="operator-a",
        reason="reserve canary",
        scope="pi:all",
        ttl_seconds=300,
        expected_fence=0,
    )
    assert replay["fence"] == paused["fence"]
    assert replay["workers"] == []


def test_legacy_partial_history_requires_fresh_bootstrap_state(tmp_path):
    gate = control(tmp_path)
    gate.bootstrap(actor="installer")
    rewrite_state(gate, lambda state: state["last_operation"].pop("applied_at"))
    with pytest.raises(StateUnavailableError):
        gate.status()

    replacement = control(tmp_path / "replacement")
    assert replacement.bootstrap(actor="installer")["mode"] == "open"


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
