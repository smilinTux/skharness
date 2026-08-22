from __future__ import annotations

import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

import pytest

from skharness.arena.controller import ArenaController
from skharness.arena.models import ExperimentState
from skharness.arena.runner import (
    DockerSupervisorError,
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

VALID_PI_STDOUT = b'{"type":"agent_end"}\n'


class ScriptedSupervisor:
    def __init__(
        self, *, exit_code=0, classification="exit", stdout=VALID_PI_STDOUT, stderr=b""
    ):
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
    runner = PiExperimentRunner(controller, ScriptedSupervisor(), tmp_path / "runs")
    outcome = runner.execute(_request(), _spec(tmp_path), timeout_s=10)
    assert isinstance(outcome, RunOutcome) and outcome.successful
    assert controller.state("experiment") is ExperimentState.PROVISIONAL
    assert controller.store.get_artifact(outcome.stdout_digest) == VALID_PI_STDOUT
    assert controller.scheduler.snapshot()["active_leases"] == 0


def test_admitted_resources_become_container_cpu_and_memory_limits(tmp_path):
    controller = _controller(tmp_path)
    controller.propose("experiment")

    class LeaseObservingSupervisor(ScriptedSupervisor):
        active_lease_id = None

        def run(self, spec, attempt_dir, timeout_s):
            self.active_lease_id = controller.scheduler.lease_records()[0]["lease_id"]
            return super().run(spec, attempt_dir, timeout_s)

    supervisor = LeaseObservingSupervisor()
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
    assert supervisor.last_spec.sandbox_run_id == supervisor.active_lease_id


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


def test_docker_control_plane_failure_has_explicit_terminal_classification(tmp_path):
    controller = _controller(tmp_path)
    controller.propose("experiment")

    class FailingDockerSupervisor(ScriptedSupervisor):
        def run(self, spec, attempt_dir, timeout_s):
            raise DockerSupervisorError("Docker OOM inspection timed out after 3s")

    outcome = PiExperimentRunner(
        controller,
        FailingDockerSupervisor(),
        tmp_path / "runs",
    ).execute(_request(), _spec(tmp_path))

    assert isinstance(outcome, RunOutcome)
    assert outcome.classification == "docker_supervisor_error"
    assert not outcome.successful
    assert b"OOM inspection timed out" in controller.store.get_artifact(
        outcome.stderr_digest
    )


def test_docker_launch_error_propagates_exit_125_and_primary_diagnostics(tmp_path):
    controller = _controller(tmp_path)
    controller.propose("experiment")
    supervisor = ScriptedSupervisor(
        exit_code=125,
        classification="docker_launch_error",
        stderr=b"docker: invalid mount config for type bind\n",
    )

    outcome = PiExperimentRunner(
        controller,
        supervisor,
        tmp_path / "runs",
    ).execute(_request(), _spec(tmp_path))

    assert not outcome.successful and outcome.partial
    assert outcome.exit_code == 125
    assert outcome.classification == "docker_launch_error"
    assert controller.store.get_artifact(outcome.stderr_digest) == supervisor.stderr
    event = controller.store.read_all_events()[-1]
    assert event.to_state is ExperimentState.FAILED
    assert event.payload["reason"] == "docker_launch_error"
    assert event.payload["exit_code"] == 125


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
        b'"stopReason":"error","errorMessage":"unexpected tokens remaining",'
        b'"content":[]}}\n'
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


def test_truncated_pi_tail_fails_run_even_after_actionable_scout_event(tmp_path):
    controller = _controller(tmp_path)
    controller.propose("experiment")
    stream = (
        b'{"type":"message_end","message":{"role":"assistant",'
        b'"responseModel":"model-a","content":[{"type":"text","text":'
        b'"SCOUT_ASSESSMENT: ACTIONABLE\\nSCOUT_FINDING: '
        b'src/skharness/arena/swarm.py:1 - exact path scoped finding"}]}}\n'
        b'{"type":"message_end","message":'
    )

    outcome = PiExperimentRunner(
        controller, ScriptedSupervisor(exit_code=0, stdout=stream), tmp_path / "runs"
    ).execute(_request(), _spec(tmp_path))

    assert not outcome.successful
    assert outcome.classification == "pi_event_stream_incomplete"
    assert outcome.scout_assessment == "actionable"
    assert outcome.metrics["served_model"] is None
    assert (
        outcome.metrics["served_model_reason"]
        == "provider_event_stream_malformed_or_incomplete"
    )
    assert outcome.metrics["pi_event_stream_complete"] is False


@pytest.mark.parametrize(
    ("stream", "classification"),
    (
        (b"", "pi_event_stream_missing"),
        (
            b'{"type":"tool_execution_start","toolName":"read","args":{}}\n',
            "pi_terminal_event_missing",
        ),
    ),
)
def test_zero_or_nonterminal_pi_event_stream_never_succeeds(
    tmp_path, stream, classification
):
    controller = _controller(tmp_path)
    controller.propose("experiment")

    outcome = PiExperimentRunner(
        controller, ScriptedSupervisor(exit_code=0, stdout=stream), tmp_path / "runs"
    ).execute(_request(), _spec(tmp_path))

    assert not outcome.successful
    assert outcome.classification == classification
    assert outcome.metrics["pi_terminal_event_observed"] is False


@pytest.mark.parametrize(
    ("tail", "expected_model", "expected_reason"),
    (
        (
            b'{"type":"message_end","message":{"role":"assistant",'
            b'"responseModel":"model-a","content":[]}}\n',
            "model-a",
            None,
        ),
        (
            b'{"type":"message_end","message":{"role":"assistant",'
            b'"content":[]}}\n',
            None,
            "provider_events_partial_response_model",
        ),
        (
            b'{"type":"message_end","message":{"role":"assistant",'
            b'"responseModel":"model-b","content":[]}}\n',
            None,
            "provider_events_conflicting_response_models",
        ),
    ),
)
def test_arena_aggregates_all_assistant_served_model_events(
    tmp_path, tail, expected_model, expected_reason
):
    controller = _controller(tmp_path)
    controller.propose("experiment")
    first = (
        b'{"type":"message_end","message":{"role":"assistant",'
        b'"responseModel":"model-a","content":[]}}\n'
    )

    outcome = PiExperimentRunner(
        controller, ScriptedSupervisor(stdout=first + tail), tmp_path / "runs"
    ).execute(_request(), _spec(tmp_path))

    assert outcome.successful
    assert outcome.metrics["served_model"] == expected_model
    assert outcome.metrics["served_model_reason"] == expected_reason
    assert outcome.metrics["pi_event_stream_complete"] is True


def test_arena_ignores_user_and_tool_served_model_noise(tmp_path):
    controller = _controller(tmp_path)
    controller.propose("experiment")
    stream = (
        b'{"type":"message_end","message":{"role":"user",'
        b'"responseModel":"user-forgery","content":"hello"}}\n'
        b'{"type":"message_end","message":{"role":"toolResult",'
        b'"responseModel":"tool-forgery","content":[]}}\n'
        b'{"type":"message_end","message":{"role":"assistant",'
        b'"responseModel":"actual-model","content":[]}}\n'
    )

    outcome = PiExperimentRunner(
        controller, ScriptedSupervisor(stdout=stream), tmp_path / "runs"
    ).execute(_request(), _spec(tmp_path))

    assert outcome.successful
    assert outcome.metrics["served_model"] == "actual-model"
    assert outcome.metrics["served_model_reason"] is None


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


def test_pi_blocked_assessment_cannot_be_successful_on_zero_exit(tmp_path):
    controller = _controller(tmp_path)
    controller.propose("experiment")
    stream = (
        b'{"type":"message_end","message":{"role":"assistant",'
        b'"stopReason":"stop","content":[{"type":"text",'
        b'"text":"## Assessment\\n\\n**Status: BLOCKED - acceptance not met.**"}]}}\n'
    )
    outcome = PiExperimentRunner(
        controller, ScriptedSupervisor(exit_code=0, stdout=stream), tmp_path / "runs"
    ).execute(_request(), _spec(tmp_path))

    assert not outcome.successful and outcome.partial
    assert outcome.classification == "blocked"
    assert outcome.disposition == "blocked"
    event = controller.store.read_all_events()[-1]
    assert event.to_state is ExperimentState.FAILED
    assert event.payload["disposition"] == "blocked"


def test_worker_text_can_only_ratchet_trust_down_not_claim_completion(tmp_path):
    controller = _controller(tmp_path)
    controller.propose("experiment")
    stream = (
        b'{"type":"message_end","message":{"role":"assistant",'
        b'"stopReason":"stop","content":[{"type":"text",'
        b'"text":"Status: COMPLETED by the worker"}]}}\n'
    )
    outcome = PiExperimentRunner(
        controller, ScriptedSupervisor(exit_code=0, stdout=stream), tmp_path / "runs"
    ).execute(_request(), _spec(tmp_path))

    assert outcome.successful
    assert outcome.disposition is None


def test_pi_usage_metrics_are_derived_from_structured_events(tmp_path):
    path = tmp_path / "stdout.log"
    path.write_bytes(
        b'{"type":"tool_execution_start","toolName":"read","args":{}}\n'
        b'{"type":"message_end","message":{"role":"assistant",'
        b'"usage":{"totalTokens":123,"cost":{"total":0.25}},"content":[]}}\n'
        b'{"type":"tool_execution_start","toolName":"bash","args":{}}\n'
    )
    assert PiExperimentRunner._usage_metrics(path) == {
        "tool_calls": 2,
        "tokens": 123,
        "cost": 0.25,
    }


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


def test_arena_verify_is_read_only_but_keeps_bounded_inspection(tmp_path):
    adapter = PiAdapter(
        Sandbox(), model="verify", base_url="http://gateway/v1", capability_profile="arena-verify"
    )
    spec = pi_launch_spec(
        adapter, prompt="verify", worktree=str(tmp_path), card_size=CardSize.MEDIUM
    )
    assert spec.inspection_scope == InspectionScope(root="/work", max_calls=48)
    tools = spec.argv[spec.argv.index("--tools") + 1].split(",")
    assert "edit" not in tools and "write" not in tools


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
        b'{"type":"tool_execution_start","toolName":"edit","elapsed_s":12.5}\n'
        b'{"type":"message_end","message":{"role":"assistant",'
        b'"stopReason":"stop","responseModel":"served-qwen","content":[]}}\n'
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


def test_production_supervisor_preserves_every_active_scheduler_lease(tmp_path):
    controller = _controller(tmp_path)
    controller.propose("experiment")
    admission = controller.admit(_request())
    runner = build_production_pi_runner(controller, artifact_root=tmp_path / "runs")

    assert admission.lease is not None
    assert tuple(runner.supervisor.active_run_ids()) == (admission.lease.lease_id,)


def test_real_supervisor_cleanup_attempts_every_resource_before_raising(monkeypatch):
    supervisor = SandboxProcessSupervisor(Sandbox(live_execution=True))
    calls = []

    def remove(argv, **_kwargs):
        calls.append(argv)
        if argv[1:3] == ["rm", "-f"] and argv[-1] == "worker":
            return subprocess.CompletedProcess(argv, 1, stderr="worker removal failed")
        if argv[1:3] == ["rm", "-f"] and argv[-1] == "proxy":
            raise OSError("daemon disconnected")
        return subprocess.CompletedProcess(argv, 2, stderr="network removal failed")

    monkeypatch.setattr("skharness.arena.runner.subprocess.run", remove)

    with pytest.raises(RuntimeError, match="worker removal failed") as raised:
        supervisor._cleanup_resources(
            docker="docker", worker="worker", proxy="proxy", network="network"
        )

    assert calls == [
        ["docker", "rm", "-f", "worker"],
        ["docker", "rm", "-f", "proxy"],
        ["docker", "network", "rm", "network"],
    ]
    assert "daemon disconnected" in str(raised.value)
    assert "network removal failed" in str(raised.value)


def test_real_supervisor_cleanup_is_idempotent_for_already_absent_resources(monkeypatch):
    supervisor = SandboxProcessSupervisor(Sandbox(live_execution=True))
    def absent(argv, **_kwargs):
        resource = "network" if argv[1:3] == ["network", "rm"] else "container"
        return subprocess.CompletedProcess(argv, 1, stderr=f"No such {resource}: {argv[-1]}")

    monkeypatch.setattr("skharness.arena.runner.subprocess.run", absent)

    supervisor._cleanup_resources(
        docker="docker", worker="worker", proxy="proxy", network="network"
    )


def test_real_supervisor_cleanup_timeouts_are_bounded_and_all_attempted(monkeypatch):
    supervisor = SandboxProcessSupervisor(
        Sandbox(live_execution=True), docker_timeout_s=2.5
    )
    calls = []

    def hangs(argv, **kwargs):
        calls.append((argv, kwargs["timeout"]))
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr("skharness.arena.runner.subprocess.run", hangs)

    with pytest.raises(DockerSupervisorError, match="worker: Docker removal timed out") as error:
        supervisor._cleanup_resources(
            docker="docker", worker="worker", proxy="proxy", network="network"
        )

    assert [item[0][-1] for item in calls] == ["worker", "proxy", "network"]
    assert [item[1] for item in calls] == [2.5, 2.5, 2.5]
    assert "proxy: Docker removal timed out" in str(error.value)
    assert "network: Docker removal timed out" in str(error.value)


def test_real_supervisor_startup_and_oom_inspection_time_out_fail_closed(monkeypatch):
    supervisor = SandboxProcessSupervisor(
        Sandbox(live_execution=True), docker_timeout_s=3, monotonic=lambda: 10
    )
    observed = []

    def hangs(argv, **kwargs):
        observed.append((argv, kwargs["timeout"]))
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr("skharness.arena.runner.subprocess.run", hangs)

    with pytest.raises(DockerSupervisorError, match="Docker startup timed out"):
        supervisor._run_checked(["docker", "network", "create", "net"], deadline=13)
    with pytest.raises(DockerSupervisorError, match="OOM inspection timed out"):
        supervisor._oom_killed("worker")

    assert [timeout for _argv, timeout in observed] == [3, 3]


def test_worker_docker_launch_error_retains_exit_125_without_oom_inspection(
    tmp_path, monkeypatch
):
    sandbox = Sandbox(live_execution=True)
    supervisor = SandboxProcessSupervisor(sandbox)
    monkeypatch.setattr(
        sandbox,
        "maybe_reconcile_orphans",
        lambda **_kwargs: {"outcome": "ok"},
    )
    monkeypatch.setattr(sandbox, "_ensure_capable", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(supervisor, "_run_checked", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(supervisor, "_cleanup_resources", lambda **_kwargs: None)
    monkeypatch.setattr(
        sandbox,
        "_docker_run_argv",
        lambda *_args, **_kwargs: [
            sys.executable,
            "--rm",
            "-c",
            "import sys; sys.stderr.write('docker: launch failed\\n'); raise SystemExit(125)",
        ],
    )
    monkeypatch.setattr(
        supervisor,
        "_oom_killed",
        lambda _name: pytest.fail("exit 125 must not inspect a nonexistent container"),
    )

    exit_code, classification = supervisor.run(
        LaunchSpec("pi", ["pi"], "image", str(tmp_path)), tmp_path / "attempt", 5
    )

    assert exit_code == 125
    assert classification == "docker_launch_error"
    assert (tmp_path / "attempt" / "stderr.log").read_text() == "docker: launch failed\n"


def test_real_supervisor_cancel_timeout_still_signals_and_reports_within_bound(monkeypatch):
    supervisor = SandboxProcessSupervisor(
        Sandbox(live_execution=True), docker_timeout_s=3, shutdown_grace_s=5
    )

    class Process:
        pid = 4242

        @staticmethod
        def poll():
            return None

        @staticmethod
        def wait(*, timeout):
            assert timeout == 5
            return 143

    supervisor._process = Process()
    supervisor._container_name = "arena-pi-live"
    signals = []

    def hangs(argv, **kwargs):
        assert kwargs["timeout"] == 3
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr("skharness.arena.runner.subprocess.run", hangs)
    monkeypatch.setattr(
        "skharness.arena.runner.os.killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )

    with pytest.raises(DockerSupervisorError, match="cancellation timed out"):
        supervisor.cancel()

    assert supervisor.cancel_bound_s == 8
    assert signals == [(4242, signal.SIGTERM)]


def test_real_supervisor_cancel_kills_container_and_process_group(monkeypatch):
    supervisor = SandboxProcessSupervisor(Sandbox(live_execution=True))

    class Process:
        pid = 4242

        @staticmethod
        def poll():
            return None

        @staticmethod
        def wait(*, timeout):
            assert timeout == 5.0
            return 143

    supervisor._process = Process()
    supervisor._container_name = "arena-pi-live"
    docker_calls = []
    signals = []
    monkeypatch.setattr(
        "skharness.arena.runner.subprocess.run",
        lambda argv, **kwargs: (
            docker_calls.append((argv, kwargs))
            or subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        ),
    )
    monkeypatch.setattr(
        "skharness.arena.runner.os.killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )
    supervisor.cancel()
    assert docker_calls == [
        (["docker", "rm", "-f", "arena-pi-live"], {
            "capture_output": True,
            "text": True,
            "timeout": 3.0,
        })
    ]
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
    monkeypatch.setattr(
        "skharness.arena.runner.subprocess.run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout="", stderr=""),
    )
    monkeypatch.setattr(
        "skharness.arena.runner.os.killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )
    supervisor.cancel()
    assert signals == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]
