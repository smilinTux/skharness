"""Lane 2 rolling-target policy over the existing PoolController primitive."""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from skharness.card_router import SessionAssignment
from skharness.autoscale_policy import (
    AutoscalePolicy,
    FleetNode,
    FleetObservation,
    RollingTarget,
    TeardownPreflight,
    observe_live_fleet,
    propose_horizontal,
)


@dataclass
class _Member:
    sid: str
    drained: bool = False
    drain_result: dict = field(default_factory=dict)


class _FakePool:
    """Fake the primitive, not spawn/drain internals owned by PoolController."""

    def __init__(self, count: int) -> None:
        self._members = [_Member(f"pane-{number}") for number in range(count)]
        self.scale_calls = []
        self.events = []

    def members(self, lane=None):
        return list(self._members)

    async def scale_assignments(self, **kwargs):
        self.events.append("scale_assignments")
        self.scale_calls.append(kwargs)
        for assignment in kwargs["assignments"]:
            self._members.append(_Member(assignment.session_id))
        return [member for member in self._members if not member.drained]

    async def scale(self, **kwargs):
        self.events.append("scale")
        self.scale_calls.append(kwargs)
        active = [member for member in self._members if not member.drained]
        target = kwargs["target"]
        if len(active) < target:
            for number in range(len(self._members), len(self._members) + target - len(active)):
                self._members.append(_Member(f"pane-{number}"))
        elif len(active) > target:
            for member in active[: len(active) - target]:
                member.drain_result = {
                    "archived": True,
                    "transcript_path": f"/evidence/{member.sid}.json",
                }
                member.drained = True
        return [member for member in self._members if not member.drained]


def _assignments(count):
    return [
        SessionAssignment(
            card_id=f"card-{number}", repo="/repo", base_branch="main", prompt="work",
            agent_id=f"agent-{number}", session_id=f"agent-{number}-session",
            worktree=f"/wt/agent-{number}-session",
            branch=f"skcode/agent-{number}-session", claim_event_id=f"event-{number}",
        )
        for number in range(count)
    ]


def _policy(pool, headroom=12):
    calls = []

    def resolve(mode, cap):
        calls.append((mode, cap))
        return headroom

    return AutoscalePolicy(
        pool,
        rolling=RollingTarget(target=10, safe_low=8, safe_high=12, hard_ceiling=12),
        resolve=resolve,
    ), calls


@pytest.mark.asyncio
async def test_outside_band_proposes_then_authorized_execution_converges_fake_pool():
    pool = _FakePool(3)
    policy, resolve_calls = _policy(pool, headroom=11)

    observed, proposal = policy.evaluate("coding")

    assert observed.current == 3
    assert observed.live_headroom == 11
    assert observed.effective_target == 10
    assert proposal is not None
    assert proposal.direction == "up"
    assert proposal.state == "parked"
    assert proposal.authority == "proposal"
    assert pool.scale_calls == []

    active = await policy.execute(
        proposal,
        authorize=lambda candidate: candidate.proposal_id == proposal.proposal_id,
        assignments=_assignments(7),
    )

    assert len(active) == 10
    assert len(pool.scale_calls[0]["assignments"]) == 7
    assert resolve_calls == [("recommended", 12), ("recommended", 12)]


def test_inside_safe_band_observes_without_proposing_or_acting():
    pool = _FakePool(9)
    policy, _ = _policy(pool)

    observed, proposal = policy.evaluate("coding")

    assert observed.effective_low == 8
    assert observed.effective_high == 12
    assert proposal is None
    assert pool.scale_calls == []


def test_live_headroom_caps_target_and_safe_band_through_autoscale_resolve():
    pool = _FakePool(9)
    policy, calls = _policy(pool, headroom=4)

    observed, proposal = policy.evaluate("coding")

    assert calls == [("recommended", 12)]
    assert (observed.effective_low, observed.effective_target, observed.effective_high) == (
        4,
        4,
        4,
    )
    assert proposal is not None and proposal.direction == "down" and proposal.target == 4


def test_policy_cannot_raise_autoscale_absolute_ceiling():
    with pytest.raises(ValueError, match="autoscale's ceiling of 12"):
        RollingTarget(hard_ceiling=13)


@pytest.mark.asyncio
async def test_evaluate_parks_and_execute_refuses_without_authorization():
    pool = _FakePool(1)
    policy, _ = _policy(pool)
    _, proposal = policy.evaluate("coding")

    with pytest.raises(PermissionError, match="not authorized"):
        await policy.execute(
            proposal,
            authorize=lambda _proposal: False,
            repo="/repo",
            branch="main",
            prompt="work",
        )

    assert pool.scale_calls == []


@pytest.mark.asyncio
async def test_changed_observation_requires_a_new_proposal():
    pool = _FakePool(1)
    policy, _ = _policy(pool)
    _, proposal = policy.evaluate("coding")
    pool._members.append(_Member("arrived-after-observation"))

    with pytest.raises(RuntimeError, match="observation changed"):
        await policy.execute(
            proposal,
            authorize=lambda _proposal: True,
            repo="/repo",
            branch="main",
            prompt="work",
        )

    assert pool.scale_calls == []


@pytest.mark.asyncio
async def test_scale_down_requires_exact_preflight_records_evidence_before_primitive():
    pool = _FakePool(13)
    policy, _ = _policy(pool)
    _, proposal = policy.evaluate("coding")
    assert proposal is not None and proposal.direction == "down"
    preflight = TeardownPreflight(
        exact_lane="coding",
        exact_current=13,
        exact_target=10,
        host_healthy=True,
        panes_healthy=True,
        evidence_recorded=True,
        parent_card_linked="d9f8f889",
        evidence_ref="sha256:" + "a" * 64,
    )

    def record(candidate, evidence):
        assert candidate is proposal
        assert evidence["parent_card_linked"] == "d9f8f889"
        pool.events.append("evidence")

    active = await policy.execute(
        proposal,
        authorize=lambda _proposal: True,
        repo="/repo",
        branch="main",
        prompt="work",
        teardown=preflight,
        record_evidence=record,
    )

    assert len(active) == 10
    assert pool.events[:2] == ["evidence", "scale"]
    drained = [member for member in pool.members() if member.drained]
    assert len(drained) == 3
    assert all(member.drain_result["transcript_path"] for member in drained)


@pytest.mark.asyncio
async def test_scale_down_refuses_missing_or_wrong_preflight_before_primitive():
    pool = _FakePool(13)
    policy, _ = _policy(pool)
    _, proposal = policy.evaluate("coding")

    with pytest.raises(RuntimeError, match="teardown preflight"):
        await policy.execute(
            proposal,
            authorize=lambda _proposal: True,
            repo="/repo",
            branch="main",
            prompt="work",
        )

    wrong = TeardownPreflight(
        exact_lane="different-lane",
        exact_current=13,
        exact_target=10,
        host_healthy=True,
        panes_healthy=True,
        evidence_recorded=True,
        parent_card_linked="d9f8f889",
        evidence_ref="sha256:" + "a" * 64,
    )
    with pytest.raises(RuntimeError, match="exact observed target"):
        await policy.execute(
            proposal,
            authorize=lambda _proposal: True,
            repo="/repo",
            branch="main",
            prompt="work",
            teardown=wrong,
            record_evidence=lambda *_args: None,
        )

    assert pool.scale_calls == []


@pytest.mark.needs_skcapstone
def test_live_fleet_finding_is_an_evidence_backed_blocker(monkeypatch):
    from skcapstone.fleet import node_controller
    from skcapstone.fleet import paths as fleet_paths
    from skcapstone.fleet.node_controller import NodeView

    monkeypatch.setattr(fleet_paths, "self_node_name", lambda: "node-chiap01")
    monkeypatch.setattr(
        node_controller,
        "node_views",
        lambda _paths: [
            NodeView(
                name="chiap01",
                phase="Dead",
                heartbeat_age_s=183_475.0,
                allocatable={"cores": 31, "ram_gb": 34.2},
            ),
            NodeView(
                name="chiap02",
                phase="Dead",
                heartbeat_age_s=183_475.0,
                allocatable={"cores": 23, "ram_gb": 15.3},
            ),
        ],
    )

    finding = observe_live_fleet()
    proposal = propose_horizontal("card-1", fleet=finding)

    assert finding.package_available is True
    assert finding.functional is False
    assert "not in roster" in finding.blocker
    assert "zero Ready nodes" in finding.blocker
    assert proposal.node is None
    assert proposal.state == "parked"
    assert finding.nodes[0].heartbeat_age_s == 183_475.0


@pytest.mark.needs_skcapstone
def test_functional_fleet_uses_scheduler_live_headroom_not_round_robin(monkeypatch, tmp_path):
    from skcapstone.fleet import node_controller
    from skcapstone.fleet import paths as fleet_paths
    from skcapstone.fleet.node_controller import NodeView

    views = [
        NodeView(
            name="chiap01",
            phase="Ready",
            heartbeat_age_s=5.0,
            allocatable={"cores": 12, "ram_gb": 10.0},
        ),
        NodeView(
            name="chiap02",
            phase="Ready",
            heartbeat_age_s=4.0,
            allocatable={"cores": 8, "ram_gb": 28.0},
        ),
    ]
    monkeypatch.setattr(fleet_paths, "self_node_name", lambda: "chiap01")
    monkeypatch.setattr(fleet_paths, "default_paths", lambda: tmp_path)
    monkeypatch.setattr(node_controller, "node_views", lambda _paths: views)

    finding = observe_live_fleet()
    first = propose_horizontal("card-1", fleet=finding)
    second = propose_horizontal("card-2", fleet=finding)

    assert finding.functional is True
    assert first.node == second.node == "chiap02"
    assert "allocatable ram=28.0GB" in first.reason
    assert first.authority == "proposal"


def test_fleet_observation_can_record_exact_live_blocker_without_importing_runtime():
    finding = FleetObservation(
        package_available=True,
        functional=False,
        self_node="node-chiap01",
        nodes=(
            FleetNode("chiap01", "Dead", 183_475.0, 31, 34.2),
            FleetNode("chiap02", "Dead", 183_475.0, 23, 15.3),
        ),
        blocker="zero Ready nodes with heartbeat <= 120s",
    )
    proposal = propose_horizontal("card-1", fleet=finding)

    assert proposal.node is None
    assert "zero Ready nodes" in proposal.reason
