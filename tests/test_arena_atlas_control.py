from __future__ import annotations

from datetime import datetime, timezone

from skharness.activity import ActivityJournal
from skharness.arena.atlas_control import SwarmAtlasControlOwner
from skharness.arena.swarm import (
    ExecutionBudget,
    SubagentContract,
    SwarmIdentity,
    SwarmPhaseSpec,
    SwarmPlan,
    SwarmRole,
    TeamBudget,
)
from skharness.arena.swarm_control import SwarmScheduler
from skharness.control import (
    ControlAction,
    ControlJournal,
    ControlStatus,
    ControlTargetKind,
)

COMMIT = "a" * 40
DIGEST = "sha256:" + "b" * 64


def _owner(tmp_path):
    identity = SwarmIdentity(
        card_id="card-1",
        card_hash=DIGEST,
        base_commit=COMMIT,
        evidence_id=DIGEST,
        trajectory_id="trajectory-1",
    )
    plan = SwarmPlan(
        plan_id="plan-1",
        identity=identity,
        phases=(
            SwarmPhaseSpec(
                phase_id="build",
                role=SwarmRole.BUILDER,
                contract_ids=("contract-1",),
            ),
        ),
        created_at=datetime.now(timezone.utc),
    )
    budget = ExecutionBudget(
        wall_seconds=60, token_limit=100, tool_call_limit=10, cost_limit=1.0
    )
    contract = SubagentContract(
        contract_id="contract-1",
        team_id="team-1",
        parent_agent_id="orchestrator-1",
        child_agent_id="builder-1",
        identity=identity,
        plan_hash=plan.content_hash,
        phase_id="build",
        role=SwarmRole.BUILDER,
        task="Implement the bounded change",
        readable_paths=("src",),
        writable_paths=("src",),
        protected_paths=("tests/hidden",),
        tool_allowlist=("read", "edit"),
        budget=budget,
        lease_id="lease-1",
        worktree_id="worktree-1",
        issued_at=datetime.now(timezone.utc),
    )
    scheduler = SwarmScheduler(
        TeamBudget(
            team_id="team-1",
            wall_seconds=120,
            token_limit=200,
            tool_call_limit=20,
            cost_limit=2.0,
            max_concurrency=1,
        ),
        identity=identity,
        orchestrator_id="orchestrator-1",
        state_path=tmp_path / "swarm.json",
    )
    scheduler.register_plan(plan)
    scheduler.register(contract)
    scheduler.admit(contract.contract_id, idempotency_key="delivery-1")
    controls = ControlJournal(tmp_path / "control", clock=lambda: 100.0)
    activity = ActivityJournal(root=tmp_path / "activity")
    stops = []
    owner = SwarmAtlasControlOwner(
        scheduler=scheduler,
        contracts=(contract,),
        stop_worker=lambda lease_id: stops.append(lease_id) or True,
        control_journal=controls,
        activity_journal=activity,
    )
    return owner, scheduler, controls, activity, stops


def _submit(controls, *, target_kind, target_id, action, expected_state=""):
    return controls.submit(
        actor="atlas",
        idempotency_key=f"key-{target_kind.value}-{action.value}",
        target_kind=target_kind,
        target_id=target_id,
        action=action,
        expected_state=expected_state,
        payload={},
    )[0]


def test_agent_cancel_uses_scheduler_lease_stop_and_emits_applied_receipt(tmp_path):
    owner, scheduler, controls, activity, stops = _owner(tmp_path)
    command = _submit(
        controls,
        target_kind=ControlTargetKind.AGENT,
        target_id="builder-1",
        action=ControlAction.CANCEL,
        expected_state="active",
    )

    assert owner.process_once() == 1
    assert stops == ["lease-1"]
    assert scheduler.lease("lease-1").state.value == "cancelled"
    assert scheduler.stop_requests() == ()
    receipt = controls.get(command.command_id)[1]
    assert receipt.status is ControlStatus.APPLIED
    assert receipt.activity_cursor == 1
    assert activity.read_after(agent_id="builder-1")[0].data["command_id"] == command.command_id


def test_run_cancel_cascades_and_requires_worker_quiescence(tmp_path):
    owner, scheduler, controls, _activity, _stops = _owner(tmp_path)
    owner.stop_worker = lambda _lease_id: False
    command = _submit(
        controls,
        target_kind=ControlTargetKind.RUN,
        target_id="trajectory-1",
        action=ControlAction.CANCEL,
        expected_state="running",
    )

    owner.process_once()

    receipt = controls.get(command.command_id)[1]
    assert receipt.status is ControlStatus.REJECTED
    assert scheduler.stop_requests() == ("lease-1",)


def test_message_and_state_mismatch_are_explicit_nonmutating_receipts(tmp_path):
    owner, scheduler, controls, _activity, stops = _owner(tmp_path)
    unsupported = _submit(
        controls,
        target_kind=ControlTargetKind.AGENT,
        target_id="builder-1",
        action=ControlAction.MESSAGE,
    )
    owner.process_once()
    assert controls.get(unsupported.command_id)[1].status is ControlStatus.UNSUPPORTED
    assert scheduler.lease("lease-1").state.value == "active"

    conflict = controls.submit(
        actor="atlas",
        idempotency_key="state-conflict",
        target_kind=ControlTargetKind.AGENT,
        target_id="builder-1",
        action=ControlAction.CANCEL,
        expected_state="finished",
        payload={},
    )[0]
    owner.process_once()
    assert controls.get(conflict.command_id)[1].status is ControlStatus.CONFLICT
    assert stops == []


def test_owner_ignores_commands_for_other_runs_and_agents(tmp_path):
    owner, _scheduler, controls, _activity, _stops = _owner(tmp_path)
    command = _submit(
        controls,
        target_kind=ControlTargetKind.RUN,
        target_id="other-run",
        action=ControlAction.CANCEL,
    )
    assert owner.process_once() == 0
    assert controls.get(command.command_id)[1].status is ControlStatus.QUEUED


def test_activity_failure_cannot_change_scheduler_control_outcome(tmp_path):
    owner, _scheduler, controls, _activity, _stops = _owner(tmp_path)

    class BrokenActivity:
        def publish(self, *_args, **_kwargs):
            raise OSError("activity disk unavailable")

    owner.activity_journal = BrokenActivity()
    command = _submit(
        controls,
        target_kind=ControlTargetKind.AGENT,
        target_id="builder-1",
        action=ControlAction.CANCEL,
    )
    owner.process_once()
    receipt = controls.get(command.command_id)[1]
    assert receipt.status is ControlStatus.APPLIED
    assert receipt.activity_cursor is None
