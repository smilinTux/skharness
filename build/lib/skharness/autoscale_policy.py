"""Versioned lane 2 rolling autoscale policy for :class:`PoolController`.

The policy separates observation, proposal, and authorized execution. A normal
``evaluate`` call only observes the pool and returns a parked proposal. It never
claims or completes a card. ``execute`` is an explicit seam for a human or the
governed action dispatcher; it rechecks the observation before driving the
existing ``PoolController.scale`` primitive.

No spawn, drain, destroy, guard, or transcript implementation lives here. Scale
up and scale down both go through ``PoolController.scale``. Scale down also
requires a multi-check preflight and verifies that the primitive reported a
persisted transcript for every drained member before returning success.

Horizontal planning consumes the optional ``skcapstone.fleet`` scheduler. That
scheduler filters for Ready nodes and ranks live allocatable RAM and cores. If
the package, roster, current-node identity, or live Ready views are unavailable,
the policy returns an evidence-backed parked blocker rather than pretending
multi-node placement works.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Callable, Protocol

from skharness.autocode import autoscale, fleet_dispatch
from skharness.pool import PoolController

POLICY_SCHEMA = "skharness.autoscale-policy.v1"
DEFAULT_PARENT_CARD = "d9f8f889"


class AuthorizationCheck(Protocol):
    """Injected dispatcher or human authorization check."""

    def __call__(self, proposal: "ScaleProposal") -> bool: ...


EvidenceRecorder = Callable[["ScaleProposal", dict[str, Any]], Awaitable[None] | None]


@dataclass(frozen=True)
class RollingTarget:
    """Versioned target and hysteresis band for one host."""

    target: int = 10
    safe_low: int = 8
    safe_high: int = 12
    mode: str = "recommended"
    hard_ceiling: int = 12
    schema: str = POLICY_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != POLICY_SCHEMA:
            raise ValueError(f"unsupported autoscale policy schema: {self.schema}")
        if not 0 <= self.safe_low <= self.target <= self.safe_high:
            raise ValueError("safe band must contain the rolling target")
        if not 1 <= self.hard_ceiling <= autoscale._HARD_CEIL:
            raise ValueError(
                f"hard_ceiling must be within autoscale's ceiling of {autoscale._HARD_CEIL}"
            )


@dataclass(frozen=True)
class ScaleObservation:
    """One read of pool size and current autoscale.resolve headroom."""

    lane: str
    current: int
    live_headroom: int
    effective_target: int
    effective_low: int
    effective_high: int
    schema: str = POLICY_SCHEMA
    authority: str = "observation"


@dataclass(frozen=True)
class ScaleProposal:
    """A deterministic, parked request to drive ``PoolController.scale``."""

    proposal_id: str
    lane: str
    parent_card: str
    target: int
    direction: str
    reason: str
    observation: ScaleObservation
    schema: str = POLICY_SCHEMA
    state: str = "parked"
    authority: str = "proposal"


@dataclass(frozen=True)
class TeardownPreflight:
    """Facts that must be recorded before any scale-down reaches the primitive."""

    exact_lane: str
    exact_current: int
    exact_target: int
    host_healthy: bool
    panes_healthy: bool
    evidence_recorded: bool
    parent_card_linked: str
    evidence_ref: str

    def validate(self, proposal: ScaleProposal) -> None:
        if (
            self.exact_lane != proposal.lane
            or self.exact_current != proposal.observation.current
            or self.exact_target != proposal.target
        ):
            raise RuntimeError("teardown preflight does not name the exact observed target")
        if not self.host_healthy or not self.panes_healthy:
            raise RuntimeError("teardown preflight health checks did not pass")
        if not self.evidence_recorded or not self.evidence_ref.strip():
            raise RuntimeError("teardown preflight evidence was not recorded")
        if self.parent_card_linked != proposal.parent_card:
            raise RuntimeError("teardown preflight is not linked to the parent card")


@dataclass(frozen=True)
class FleetNode:
    """Bounded live headroom evidence copied from one fleet NodeView."""

    name: str
    phase: str
    heartbeat_age_s: float | None
    allocatable_cores: float
    allocatable_ram_gb: float


@dataclass(frozen=True)
class FleetObservation:
    """Truthful optional-fleet readiness finding."""

    package_available: bool
    functional: bool
    self_node: str
    nodes: tuple[FleetNode, ...]
    blocker: str = ""
    heartbeat_max_age_s: float = 120.0
    authority: str = "observation"


@dataclass(frozen=True)
class PlacementProposal:
    """Parked horizontal placement selected by the fleet scheduler."""

    ref: str
    node: str | None
    reason: str
    fleet: FleetObservation
    state: str = "parked"
    authority: str = "proposal"


def _proposal_id(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class AutoscalePolicy:
    """Observe, propose, and explicitly drive one lane through PoolController."""

    def __init__(
        self,
        pool: PoolController,
        *,
        rolling: RollingTarget = RollingTarget(),
        parent_card: str = DEFAULT_PARENT_CARD,
        resolve: Callable[[Any, int | None], int] = autoscale.resolve,
    ) -> None:
        if not parent_card.strip():
            raise ValueError("parent_card is required")
        self.pool = pool
        self.rolling = rolling
        self.parent_card = parent_card
        self._resolve = resolve

    def observe(self, lane: str) -> ScaleObservation:
        """Read current pool size and live per-host resource headroom."""
        current = sum(not member.drained for member in self.pool.members(lane))
        headroom = self._resolve(self.rolling.mode, self.rolling.hard_ceiling)
        target = min(self.rolling.target, headroom)
        low = min(self.rolling.safe_low, target)
        high = min(self.rolling.safe_high, headroom)
        return ScaleObservation(
            lane=lane,
            current=current,
            live_headroom=headroom,
            effective_target=target,
            effective_low=low,
            effective_high=high,
        )

    def evaluate(self, lane: str) -> tuple[ScaleObservation, ScaleProposal | None]:
        """Return no action inside the safe band, otherwise one parked proposal."""
        observed = self.observe(lane)
        if observed.effective_low <= observed.current <= observed.effective_high:
            return observed, None
        direction = "up" if observed.current < observed.effective_low else "down"
        reason = (
            f"current={observed.current} outside safe band "
            f"[{observed.effective_low},{observed.effective_high}]; "
            f"propose target={observed.effective_target} from live headroom="
            f"{observed.live_headroom}"
        )
        identity = {
            "schema": POLICY_SCHEMA,
            "lane": lane,
            "parent_card": self.parent_card,
            "target": observed.effective_target,
            "direction": direction,
            "observation": asdict(observed),
        }
        return observed, ScaleProposal(
            proposal_id=_proposal_id(identity),
            lane=lane,
            parent_card=self.parent_card,
            target=observed.effective_target,
            direction=direction,
            reason=reason,
            observation=observed,
        )

    async def execute(
        self,
        proposal: ScaleProposal,
        *,
        authorize: AuthorizationCheck,
        repo: str,
        branch: str,
        prompt: str,
        model: str = "",
        quality: str = "sandbox",
        mode: str = "direct",
        teardown: TeardownPreflight | None = None,
        record_evidence: EvidenceRecorder | None = None,
    ) -> list[Any]:
        """Drive the primitive only after authorization and a fresh exact recheck.

        This method has no default authorization and is never called by
        :meth:`evaluate`. Callers must deliberately supply the governed seam.
        """
        if proposal.schema != POLICY_SCHEMA or proposal.state != "parked":
            raise RuntimeError("only a parked current-schema proposal can execute")
        if proposal.parent_card != self.parent_card:
            raise RuntimeError("proposal parent card does not match this policy")
        if not authorize(proposal):
            raise PermissionError("autoscale proposal is not authorized")
        fresh = self.observe(proposal.lane)
        if fresh != proposal.observation:
            raise RuntimeError("autoscale observation changed; park and propose again")

        before = {member.sid for member in self.pool.members(proposal.lane) if not member.drained}
        if proposal.direction == "down":
            if teardown is None:
                raise RuntimeError("scale-down requires teardown preflight")
            teardown.validate(proposal)
            if record_evidence is None:
                raise RuntimeError("scale-down requires an evidence recorder")
            recorded = record_evidence(proposal, asdict(teardown))
            if recorded is not None:
                await recorded

        active = await self.pool.scale(
            lane=proposal.lane,
            repo=repo,
            branch=branch,
            prompt=prompt,
            target=proposal.target,
            model=model,
            quality=quality,
            mode=mode,
        )
        if proposal.direction == "down":
            after = {member.sid for member in active}
            drained = [
                member for member in self.pool.members(proposal.lane)
                if member.sid in before - after
            ]
            if len(after) != proposal.target:
                raise RuntimeError("pool primitive did not reach the exact teardown target")
            if any(
                not member.drain_result.get("archived")
                or not member.drain_result.get("transcript_path")
                for member in drained
            ):
                raise RuntimeError("pool primitive did not report transcript-first archive")
        return active


def observe_live_fleet(*, max_heartbeat_age_s: float = 120.0) -> FleetObservation:
    """Inspect the optional fleet package and its current synchronized views."""
    try:
        from skcapstone.fleet.node_controller import node_views
        from skcapstone.fleet.paths import default_paths, self_node_name
    except Exception as exc:
        return FleetObservation(
            package_available=False,
            functional=False,
            self_node="local",
            nodes=(),
            blocker=f"skcapstone.fleet unavailable: {type(exc).__name__}",
        )

    try:
        self_name = self_node_name()
        views = node_views(default_paths())
    except Exception as exc:
        return FleetObservation(
            package_available=True,
            functional=False,
            self_node="unknown",
            nodes=(),
            blocker=f"fleet views unavailable: {type(exc).__name__}",
        )

    nodes = tuple(
        FleetNode(
            name=view.name,
            phase=view.phase,
            heartbeat_age_s=view.heartbeat_age_s,
            allocatable_cores=float(view.allocatable.get("cores", 0)),
            allocatable_ram_gb=float(view.allocatable.get("ram_gb", 0.0)),
        )
        for view in views
    )
    roster = {node.name for node in nodes}
    ready = [
        node for node in nodes
        if node.phase == "Ready"
        and node.heartbeat_age_s is not None
        and node.heartbeat_age_s <= max_heartbeat_age_s
    ]
    blockers = []
    if self_name not in roster:
        blockers.append(f"self node {self_name!r} is not in roster {sorted(roster)!r}")
    if not ready:
        blockers.append(f"zero Ready nodes with heartbeat <= {max_heartbeat_age_s:g}s")
    return FleetObservation(
        package_available=True,
        functional=not blockers,
        self_node=self_name,
        nodes=nodes,
        blocker="; ".join(blockers),
        heartbeat_max_age_s=max_heartbeat_age_s,
    )


def propose_horizontal(
    ref: str,
    *,
    tags: list[str] | None = None,
    fleet: FleetObservation | None = None,
) -> PlacementProposal:
    """Ask the installed fleet scheduler for headroom placement, or park blocked."""
    observed = fleet or observe_live_fleet()
    if not observed.functional:
        return PlacementProposal(
            ref=ref,
            node=None,
            reason=f"horizontal placement blocked: {observed.blocker}",
            fleet=observed,
        )
    try:
        from skcapstone.fleet import scheduler
        from skcapstone.fleet.node_controller import node_views
        from skcapstone.fleet.paths import default_paths

        fresh_views = node_views(default_paths())
        live_names = {
            node.name for node in observed.nodes
            if node.phase == "Ready"
            and node.heartbeat_age_s is not None
            and node.heartbeat_age_s <= observed.heartbeat_max_age_s
        }
        workload = scheduler.Workload(
            kind="job",
            name=ref,
            node_selector=fleet_dispatch.card_selector(tags or []),
        )
        decision = scheduler.select(
            [view for view in fresh_views if view.name in live_names], workload
        )
    except Exception as exc:
        return PlacementProposal(
            ref=ref,
            node=None,
            reason=f"horizontal placement blocked: scheduler failed: {type(exc).__name__}",
            fleet=observed,
        )
    return PlacementProposal(
        ref=ref,
        node=decision.node,
        reason=decision.reason,
        fleet=observed,
    )


__all__ = [
    "AutoscalePolicy",
    "FleetNode",
    "FleetObservation",
    "PlacementProposal",
    "POLICY_SCHEMA",
    "RollingTarget",
    "ScaleObservation",
    "ScaleProposal",
    "TeardownPreflight",
    "observe_live_fleet",
    "propose_horizontal",
]
