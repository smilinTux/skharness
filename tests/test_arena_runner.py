from __future__ import annotations

import signal
import threading
import time
from datetime import datetime, timezone

from skharness.arena.controller import ArenaController
from skharness.arena.models import ExperimentState
from skharness.arena.runner import (
    PiExperimentRunner,
    RunOutcome,
    SandboxProcessSupervisor,
    build_production_pi_runner,
    pi_launch_spec,
)
from skharness.arena.scheduler import AttemptRequest, LeaseScheduler, ResourceRequest
from skharness.arena.store import ArenaStore
from skharness.autocode.adapters.pi import PiAdapter
from skharness.autocode.sandbox import LaunchSpec, Sandbox
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
        writer_id="runner", actor="agent", node="node", session_id="session",
        now=lambda: datetime(2026, 8, 20, tzinfo=timezone.utc),
    )


def _request():
    return AttemptRequest("challenge", "experiment", "1", "experiment:1")


def _spec(tmp_path):
    return LaunchSpec("pi", ["pi", "-p", "task"], "pi-image", str(tmp_path))


def test_success_runs_pi_attempt_and_persists_artifacts_before_terminal_event(tmp_path):
    controller = _controller(tmp_path)
    controller.propose("experiment")
    runner = PiExperimentRunner(controller, ScriptedSupervisor(stdout=b"trajectory"),
                                tmp_path / "runs")
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
        "challenge", "experiment", "1", "experiment:1",
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
    scheduler = LeaseScheduler(ResourceRequest(cpu=2, ram_gb=4, gateway_slots=2),
                               lease_ttl_s=0.09)
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
            time.sleep(0.12)
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

    restarted = _controller(tmp_path, LeaseScheduler(ResourceRequest(
        cpu=2, ram_gb=4, gateway_slots=2)))
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
        Sandbox(), model="build", base_url="http://skgateway:18780/v1",
        capability_profile="arena-build", image="pi-pinned",
    )
    spec = pi_launch_spec(adapter, prompt="optimize", worktree=str(tmp_path))
    assert spec.image == "pi-pinned"
    assert spec.argv[:3] == ["pi", "-p", "optimize"]
    assert "skgw/build" in spec.argv
    assert "http://skgateway:18780/v1" in spec.config_files["/agent/models.json"]


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
