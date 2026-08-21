from __future__ import annotations

import signal
import subprocess
import threading
import time
from datetime import datetime, timezone

import pytest

from skharness.arena.controller import ArenaController
from skharness.arena.models import ExperimentState
from skharness.arena.runner import (
    PiExperimentRunner,
    RunOutcome,
    SandboxProcessSupervisor,
    build_production_pi_runner,
    inspect_pi_tool_event,
    pi_launch_spec,
)
from skharness.arena.scheduler import AttemptRequest, LeaseScheduler, ResourceRequest
from skharness.arena.store import ArenaStore, CorruptEventLogError
from skharness.arena.trajectory import CardSize, PhaseBudget
from skharness.autocode.adapters.pi import PiAdapter
from skharness.autocode.sandbox import InspectionScope, LaunchSpec, Sandbox
from skharness.spawner import FakeSpawner


class ScriptedSupervisor:
    def __init__(self, *, exit_code=0, classification="exit", stdout=b"ok", stderr=b""):
        self.exit_code = exit_code
        self.classification = classification
        self.stdout = stdout
        self.stderr = stderr
        self.cancelled = False
        self.calls = 0
        self.last_spec = None

    def run(self, spec, attempt_dir, timeout_s):
        self.calls += 1
        self.last_spec = spec
        (attempt_dir / "stdout.log").write_bytes(self.stdout)
        (attempt_dir / "stderr.log").write_bytes(self.stderr)
        return self.exit_code, self.classification

    def cancel(self):
        self.cancelled = True


class BlockingSupervisor(ScriptedSupervisor):
    def __init__(self):
        super().__init__(exit_code=143)
        self.started = threading.Event()
        self.stopped = threading.Event()

    def run(self, spec, attempt_dir, timeout_s):
        self.started.set()
        self.stopped.wait(2)
        return 143, "exit"

    def cancel(self):
        self.cancelled = True
        self.stopped.set()


def _controller(tmp_path, scheduler=None):
    return ArenaController(
        ArenaStore(tmp_path / "store"),
        scheduler or LeaseScheduler(ResourceRequest(cpu=2, ram_gb=4, gateway_slots=2)),
        writer_id="runner",
        actor="agent",
        node="node",
        session_id="session",
        now=lambda: datetime(2026, 8, 20, tzinfo=timezone.utc),
    )


def _request():
    return AttemptRequest("challenge", "experiment", "1", "experiment:1")


def _spec(tmp_path):
    return LaunchSpec("pi", ["pi", "-p", "task"], "pi-image", str(tmp_path))


def test_success_runs_pi_attempt_and_persists_artifacts_before_terminal_event(tmp_path):
    controller = _controller(tmp_path)
    controller.propose("experiment")
    runner = PiExperimentRunner(
        controller, ScriptedSupervisor(stdout=b"trajectory"), tmp_path / "runs"
    )
    outcome = runner.execute(_request(), _spec(tmp_path), timeout_s=10)
    assert isinstance(outcome, RunOutcome) and outcome.successful
    assert controller.state("experiment") is ExperimentState.PROVISIONAL
    assert controller.store.get_artifact(outcome.stdout_digest) == b"trajectory"
    assert controller.scheduler.snapshot()["active_leases"] == 0


def test_admitted_resources_become_container_cpu_and_memory_limits(tmp_path):
    controller = _controller(tmp_path)
    controller.propose("experiment")
    supervisor = ScriptedSupervisor()
    runner = PiExperimentRunner(controller, supervisor, tmp_path / "runs")
    request = AttemptRequest(
        "challenge",
        "experiment",
        "1",
        "experiment:1",
        resources=ResourceRequest(cpu=1.5, ram_gb=3.25),
    )
    outcome = runner.execute(request, _spec(tmp_path))
    assert isinstance(outcome, RunOutcome) and outcome.successful
    assert supervisor.last_spec.cpu_limit == 1.5
    assert supervisor.last_spec.memory_gb_limit == 3.25


def test_timeout_and_oom_are_failed_with_durable_partial_evidence(tmp_path):
    for classification, code in (("timeout", 143), ("oom", 137)):
        root = tmp_path / classification
        controller = _controller(root)
        controller.propose("experiment")
        supervisor = ScriptedSupervisor(
            exit_code=code, classification=classification, stdout=b"partial-events"
        )
        outcome = PiExperimentRunner(controller, supervisor, root / "runs").execute(
            _request(), _spec(root), timeout_s=0.01
        )
        assert not outcome.successful and outcome.partial
        assert outcome.classification == classification
        assert controller.store.get_artifact(outcome.stdout_digest) == b"partial-events"
        event = controller.store.read_all_events()[-1]
        assert event.to_state is ExperimentState.FAILED
        assert event.payload["reason"] == classification


def test_gateway_outage_is_classified_and_retains_diagnostics(tmp_path):
    controller = _controller(tmp_path)
    controller.propose("experiment")
    supervisor = ScriptedSupervisor(
        exit_code=1,
        classification="exit",
        stderr=b"gateway unavailable: connection refused",
    )
    outcome = PiExperimentRunner(controller, supervisor, tmp_path / "runs").execute(
        _request(), _spec(tmp_path)
    )
    assert outcome.classification == "gateway_outage"
    assert outcome.partial and not outcome.successful
    assert controller.store.get_artifact(outcome.stderr_digest) == supervisor.stderr


def test_pi_structured_terminal_error_overrides_zero_process_exit(tmp_path):
    controller = _controller(tmp_path)
    controller.propose("experiment")
    stream = (
        b'{"type":"message_end","message":{"role":"assistant",'
        b'"stopReason":"error","errorMessage":"unexpected tokens remaining"}}\n'
    )
    outcome = PiExperimentRunner(
        controller, ScriptedSupervisor(exit_code=0, stdout=stream), tmp_path / "runs"
    ).execute(_request(), _spec(tmp_path))

    assert not outcome.successful and outcome.partial
    assert outcome.exit_code == 0
    assert outcome.classification == "pi_terminal_error"
    event = controller.store.read_all_events()[-1]
    assert event.to_state is ExperimentState.FAILED
    assert event.payload["terminal_error"] == "unexpected tokens remaining"


def test_normal_pi_terminal_event_with_zero_exit_remains_successful(tmp_path):
    controller = _controller(tmp_path)
    controller.propose("experiment")
    stream = (
        b'{"type":"message_end","message":{"role":"assistant",'
        b'"stopReason":"stop","content":[{"type":"text","text":"done"}]}}\n'
    )
    outcome = PiExperimentRunner(
        controller, ScriptedSupervisor(exit_code=0, stdout=stream), tmp_path / "runs"
    ).execute(_request(), _spec(tmp_path))
    assert outcome.successful
    assert outcome.classification == "exit"


def test_corrupt_committed_event_tail_stops_restart_recovery(tmp_path):
    controller = _controller(tmp_path)
    controller.propose("experiment")
    segment = controller.store.events_dir / "runner.jsonl"
    with segment.open("ab") as stream:
        stream.write(b"corrupt-committed-tail\n")
    runner = PiExperimentRunner(controller, ScriptedSupervisor(), tmp_path / "runs")
    with pytest.raises(CorruptEventLogError):
        runner.recover_incomplete()


def test_duplicate_delivery_does_not_spawn_a_second_pi_process(tmp_path):
    controller = _controller(tmp_path)
    controller.propose("experiment")
    supervisor = ScriptedSupervisor()
    # Model a delivery duplicated while the first lease/process is still running.
    assert controller.admit(_request()).admitted
    controller.running("experiment")
    runner = PiExperimentRunner(controller, supervisor, tmp_path / "runs")
    duplicate = runner.execute(_request(), _spec(tmp_path))
    assert duplicate.admitted and duplicate.duplicate
    assert supervisor.calls == 0


def test_long_running_attempt_heartbeats_its_lease(tmp_path):
    # Keep a clear heartbeat-vs-runtime relationship without relying on a
    # sub-100 ms host scheduling deadline (which is flaky under parallel CI/load).
    scheduler = LeaseScheduler(ResourceRequest(cpu=2, ram_gb=4, gateway_slots=2), lease_ttl_s=0.3)
    controller = _controller(tmp_path, scheduler)
    controller.propose("experiment")
    calls = []
    original = controller.heartbeat

    def observed(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    controller.heartbeat = observed

    class SlowSupervisor(ScriptedSupervisor):
        def run(self, spec, attempt_dir, timeout_s):
            time.sleep(0.45)
            return super().run(spec, attempt_dir, timeout_s)

    outcome = PiExperimentRunner(controller, SlowSupervisor(), tmp_path / "runs").execute(
        _request(), _spec(tmp_path)
    )
    assert outcome.successful
    assert len(calls) >= 2


def test_restart_recovery_terminalizes_running_attempt_and_captures_partial_logs(tmp_path):
    first = _controller(tmp_path)
    first.propose("experiment")
    first.admit(_request())
    first.running("experiment")
    recovery_runner = PiExperimentRunner(first, ScriptedSupervisor(), tmp_path / "runs")
    run_dir = recovery_runner._attempt_dir("experiment", 1)
    run_dir.mkdir(parents=True)
    (run_dir / "stdout.log").write_bytes(b"survived-crash")

    restarted = _controller(
        tmp_path, LeaseScheduler(ResourceRequest(cpu=2, ram_gb=4, gateway_slots=2))
    )
    runner = PiExperimentRunner(restarted, ScriptedSupervisor(), tmp_path / "runs")
    duplicate = runner.execute(_request(), _spec(tmp_path))
    assert not duplicate.admitted
    assert restarted.scheduler.snapshot()["active_leases"] == 0
    assert runner.recover_incomplete() == ["experiment"]
    event = restarted.store.read_all_events()[-1]
    assert event.to_state is ExperimentState.FAILED
    assert event.payload["classification"] == "restart_recovery"
    assert restarted.store.get_artifact(event.payload["stdout_digest"]) == b"survived-crash"
    assert runner.recover_incomplete() == []


def test_cancel_calls_real_supervisor_seam_before_durable_cancel(tmp_path):
    controller = _controller(tmp_path)
    controller.propose("experiment")
    controller.admit(_request())
    controller.running("experiment")
    supervisor = ScriptedSupervisor()
    runner = PiExperimentRunner(controller, supervisor, tmp_path / "runs")
    runner.cancel("experiment")
    assert supervisor.cancelled
    assert controller.state("experiment") is ExperimentState.CANCELLED


def test_cancel_racing_process_exit_does_not_overwrite_terminal_state(tmp_path):
    controller = _controller(tmp_path)
    controller.propose("experiment")
    supervisor = BlockingSupervisor()
    runner = PiExperimentRunner(controller, supervisor, tmp_path / "runs")
    outcomes = []
    thread = threading.Thread(
        target=lambda: outcomes.append(runner.execute(_request(), _spec(tmp_path)))
    )
    thread.start()
    assert supervisor.started.wait(1)
    runner.cancel("experiment")
    thread.join(2)
    assert not thread.is_alive()
    assert outcomes[0].classification == "cancelled"
    assert controller.state("experiment") is ExperimentState.CANCELLED


def test_pi_launch_spec_reuses_adapter_routing_profile_and_model_config(tmp_path):
    adapter = PiAdapter(
        Sandbox(),
        model="build",
        base_url="http://skgateway:18780/v1",
        capability_profile="arena-build",
        image="pi-pinned",
    )
    spec = pi_launch_spec(adapter, prompt="optimize", worktree=str(tmp_path))
    assert spec.image == "pi-pinned"
    assert spec.argv[:3] == ["pi", "-p", "optimize"]
    assert "skgw/build" in spec.argv
    assert "http://skgateway:18780/v1" in spec.config_files["/agent/models.json"]
    assert spec.required_commands == ["pytest"]
    assert spec.required_checks == [["/usr/local/bin/skharness-pi-python-test-preflight"]]


def test_pi_launch_spec_injects_explicit_phase_contract(tmp_path):
    adapter = PiAdapter(Sandbox(), model="build", base_url="http://gateway/v1")
    spec = pi_launch_spec(
        adapter,
        prompt="fix the card",
        worktree=str(tmp_path),
        card_size=CardSize.SMALL,
        phase_budget=PhaseBudget(1, 2, 3, 4),
    )
    assert "assess 1s, inspect 2s, build 3s, test 4s" in spec.argv[2]
    assert spec.argv[2].endswith("fix the card")


def _tool_start(tool, args):
    import json

    return json.dumps({"type": "tool_execution_start", "toolName": tool, "args": args}).encode()


@pytest.mark.parametrize(
    ("tool", "args", "reason"),
    [
        ("bash", {"command": "find / -name '*.py'"}, "inspection_path_outside_worktree"),
        ("bash", {"command": "grep -R token /home/cbrd21"}, "inspection_path_outside_worktree"),
        ("bash", {"command": "/usr/bin/find / -type f"}, "inspection_path_outside_worktree"),
        ("bash", {"command": "cd /work && find ../secret -type f"}, "inspection_parent_escape"),
        ("find", {"path": "/etc", "pattern": "*"}, "inspection_path_outside_worktree"),
        ("grep", {"path": "../", "pattern": "key"}, "inspection_parent_escape"),
    ],
)
def test_inspection_scope_denies_adversarial_filesystem_discovery(tool, args, reason):
    assert inspect_pi_tool_event(_tool_start(tool, args), InspectionScope()) == (reason, 1)


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("bash", {"command": "cd /work && find . -name '*.py' | head -50"}),
        ("bash", {"command": "find /work/src/skharness -type f | head -100"}),
        (
            "bash",
            {"command": "cd /work && grep -n token src/a.py; sed -n '/^def /p' src/b.py"},
        ),
        ("grep", {"path": "/work/src", "pattern": "Arena"}),
        ("find", {"path": ".", "pattern": "*.py"}),
    ],
)
def test_inspection_scope_allows_bounded_worktree_discovery(tool, args):
    assert inspect_pi_tool_event(_tool_start(tool, args), InspectionScope()) == (None, 1)


def test_arena_build_launch_enables_executable_inspection_scope(tmp_path):
    adapter = PiAdapter(
        Sandbox(), model="build", base_url="http://gateway/v1", capability_profile="arena-build"
    )
    spec = pi_launch_spec(adapter, prompt="fix", worktree=str(tmp_path))
    assert spec.inspection_scope == InspectionScope(root="/work", max_calls=24)


@pytest.mark.parametrize(
    ("size", "max_calls"),
    [(CardSize.SMALL, 24), (CardSize.MEDIUM, 48), (CardSize.LARGE, 80)],
)
def test_arena_build_inspection_budget_scales_with_card_size(tmp_path, size, max_calls):
    adapter = PiAdapter(
        Sandbox(), model="build", base_url="http://gateway/v1", capability_profile="arena-build"
    )
    spec = pi_launch_spec(adapter, prompt="fix", worktree=str(tmp_path), card_size=size)
    assert spec.inspection_scope == InspectionScope(root="/work", max_calls=max_calls)


def test_inspection_monitor_emits_structured_denial_and_cancels(tmp_path):
    path = tmp_path / "stdout.log"
    path.write_bytes(_tool_start("bash", {"command": "find / -type f"}) + b"\n")

    class Exited:
        @staticmethod
        def poll():
            return 0

    supervisor = SandboxProcessSupervisor(Sandbox(live_execution=True))
    cancelled = []
    supervisor.cancel = lambda: cancelled.append(True)
    denied = threading.Event()
    detail = {}
    supervisor._monitor_inspection(path, InspectionScope(), Exited(), denied, detail)
    assert cancelled == [True]
    assert denied.is_set()
    assert detail == {
        "type": "inspection_denial",
        "reason": "inspection_path_outside_worktree",
        "root": "/work",
        "observed_calls": 1,
        "max_calls": 24,
    }


def test_run_persists_bounded_routing_and_phase_metrics(tmp_path):
    controller = _controller(tmp_path)
    controller.propose("experiment")
    stream = (
        b'{"type":"tool_call","name":"edit","elapsed_s":12.5}\n'
        b'{"type":"message_end","message":{"role":"assistant",'
        b'"stopReason":"stop","responseModel":"served-qwen"}}\n'
    )
    outcome = PiExperimentRunner(
        controller, ScriptedSupervisor(stdout=stream), tmp_path / "runs"
    ).execute(
        _request(),
        _spec(tmp_path),
        card_size=CardSize.SMALL,
        requested_model="requested-qwen",
        phase_budget=PhaseBudget(1, 2, 3, 4),
    )
    assert outcome.metrics["time_to_first_edit_s"] == 12.5
    assert outcome.metrics["requested_model"] == "requested-qwen"
    assert outcome.metrics["served_model"] == "served-qwen"
    assert outcome.metrics["card_size"] == "S"
    assert controller.store.read_all_events()[-1].payload["metrics"] == outcome.metrics


def test_production_factory_can_only_construct_real_sandbox_supervisor(tmp_path):
    controller = _controller(tmp_path)
    runner = build_production_pi_runner(controller, artifact_root=tmp_path / "runs")
    assert isinstance(runner.supervisor, SandboxProcessSupervisor)
    assert not isinstance(runner.supervisor, FakeSpawner)
    assert runner.supervisor.sandbox.live_execution is True


def test_real_supervisor_cancel_kills_container_and_process_group(monkeypatch):
    supervisor = SandboxProcessSupervisor(Sandbox(live_execution=True))

    class Process:
        pid = 4242

        @staticmethod
        def poll():
            return None

        @staticmethod
        def wait(*, timeout):
            assert timeout == 10.0
            return 143

    supervisor._process = Process()
    supervisor._container_name = "arena-pi-live"
    docker_calls = []
    signals = []
    monkeypatch.setattr(
        "skharness.arena.runner.subprocess.run",
        lambda argv, **kwargs: docker_calls.append(argv),
    )
    monkeypatch.setattr(
        "skharness.arena.runner.os.killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )
    supervisor.cancel()
    assert docker_calls == [["docker", "rm", "-f", "arena-pi-live"]]
    assert signals == [(4242, signal.SIGTERM)]


def test_real_supervisor_shutdown_is_bounded_and_escalates_to_kill(monkeypatch):
    supervisor = SandboxProcessSupervisor(Sandbox(live_execution=True), shutdown_grace_s=0.25)

    class Process:
        pid = 4242

        @staticmethod
        def poll():
            return None

        @staticmethod
        def wait(*, timeout):
            assert timeout == 0.25
            raise subprocess.TimeoutExpired("pi", timeout)

    supervisor._process = Process()
    supervisor._container_name = "arena-pi-live"
    signals = []
    monkeypatch.setattr("skharness.arena.runner.subprocess.run", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "skharness.arena.runner.os.killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )
    supervisor.cancel()
    assert signals == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]
