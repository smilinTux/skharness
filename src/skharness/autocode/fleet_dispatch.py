"""Fleet-aware dispatch gate for autopilot (fleet design spec, Card 2.2).

Replaces the static single-node-by-convention dispatch (jobs.yaml node
pinning plus hand-assigned per-node tags): before the swarm phase, each
selected card is placed by the fleet scheduler v1 (filter + least-loaded)
and only cards placed on THIS node proceed. The coord claim remains the
authoritative execution gate, placement is advisory routing, so a stale
placement can never double-run a card (the claim is atomic).

Soft dependency: skcapstone is an optional sibling. When it is not
importable, or the fleet tree has no admitted nodes, the gate is inert and
everything runs locally, preserving one-box behavior (spec 3.6).

Single-writer discipline: select() is pure and every node computes the same
answer from the same synced views, only a run on the control-plane node
(label control-plane=true) PERSISTS placement records. Other nodes query.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .types import WorkItem

NODE_TAG_PREFIX = "node:"


@dataclass(frozen=True)
class DispatchDecision:
    """Where one card should run, and why."""

    ref: str
    node: str | None
    reason: str


def card_selector(tags: list[str]) -> dict:
    """Map ``node:<key>[=<value>]`` card tags to a fleet nodeSelector.

    Same exact-match AND semantics as autopilot ``--tag`` selection; a bare
    ``node:<key>`` means ``<key>=true`` (e.g. ``node:heavy-build``).
    """
    selector: dict = {}
    for tag in tags:
        if not tag.startswith(NODE_TAG_PREFIX):
            continue
        body = tag[len(NODE_TAG_PREFIX):]
        key, _, value = body.partition("=")
        if key:
            selector[key] = value or "true"
    return selector


def self_node() -> str:
    """This machine's fleet node name, or "local" without the fleet package."""
    try:
        from skcapstone.fleet.paths import self_node_name

        return self_node_name()
    except Exception:
        return "local"


def claim_agent_name() -> str:
    """Node-scoped claim identity so the coord claim gate distinguishes nodes.

    With cross-node dispatch, two nodes claiming as the same agent name
    would not conflict (claim_task allows re-claim by the same name), so the
    claimer must be per-node: autopilot-<node>. Falls back to the legacy
    "autopilot" on a box without the fleet package.
    """
    try:
        from skcapstone.fleet.paths import self_node_name

        return f"autopilot-{self_node_name()}"
    except Exception:
        return "autopilot"


def default_placer() -> Callable[[WorkItem], DispatchDecision] | None:
    """Build the live placer from the fleet tree, or None when unmanaged.

    None means "no fleet": the caller keeps every card local (the exact
    pre-Phase-2 behavior, and the spec 3.6 one-box invariant).
    """
    try:
        from skcapstone.fleet import scheduler as fsched
        from skcapstone.fleet import store
        from skcapstone.fleet.node_controller import node_views
        from skcapstone.fleet.paths import default_paths, self_node_name
    except Exception:
        return None                                  # no fleet substrate installed
    paths = default_paths()
    views = [v for v in node_views(paths) if v.phase != "Pending"]
    if not views:
        return None                                  # unmanaged tree: run local
    me = self_node_name()
    if not any(v.name == me for v in views):
        # This node's computed name is not in the roster (almost always SKFLEET_NODE
        # is unset, so the name fell back to the hostname while the node enrolled
        # under a friendly name). With the gate active every card would route
        # off-node and NOTHING would ever build here: the run silently strands all
        # work. Treat an unrecognized self as unmanaged (build local) and warn
        # loudly so the misconfiguration is visible rather than eating cards.
        import sys
        print(f"autopilot: WARNING self node {me!r} is not in the fleet roster "
              f"({sorted(v.name for v in views)}); building locally. "
              f"Set SKFLEET_NODE to this node's roster name to enable placement.",
              file=sys.stderr)
        return None
    is_control_plane = any(v.name == me and v.labels.get("control-plane") == "true"
                           for v in views)
    frozen = store.is_frozen(paths)
    writer = store.Writer(role="scheduler", node=me,
                          identity=store.writer_identity())

    def _place(item: WorkItem) -> DispatchDecision:
        if frozen:
            return DispatchDecision(ref=item.ref, node=None,
                                    reason="fleet frozen: no new placements")
        workload = fsched.Workload(
            kind="job", name=item.ref,
            node_selector=card_selector(item.payload.get("tags") or []))
        decision = fsched.select(views, workload)
        if decision.node is not None and is_control_plane:
            fsched.place(paths, workload, writer=writer, views=views)  # audit record
        return DispatchDecision(ref=item.ref, node=decision.node,
                                reason=decision.reason)

    return _place


def partition_local(selected, *, placer, self_node: str) -> tuple[list, list[tuple]]:
    """Split (item, executor) pairs into (run here, skipped elsewhere).

    A None placer keeps everything (gate inert). Skipped entries carry the
    full DispatchDecision so the run journal records where and why.
    """
    if placer is None:
        return list(selected), []
    kept: list = []
    skipped: list[tuple] = []
    for item, ex in selected:
        decision = placer(item)
        if decision.node == self_node:
            kept.append((item, ex))
        else:
            skipped.append((item, decision))
    return kept, skipped
