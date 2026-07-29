"""Fleet-aware dispatch seam: card selectors, partition, unmanaged fallback."""
import pytest

from skharness.autocode import fleet_dispatch as fd
from skharness.autocode.types import WorkItem


def _item(ref, tags=None):
    return WorkItem(kind="engineering", ref=ref, source="coord", repo="skos",
                    payload={"tags": tags or []})


def test_card_selector_from_node_tags():
    assert fd.card_selector([]) == {}
    assert fd.card_selector(["repo:skos", "node:heavy-build"]) == {"heavy-build": "true"}
    assert fd.card_selector(["node:tier=core", "node:gpu"]) == {"tier": "core",
                                                                "gpu": "true"}


def test_partition_local_none_placer_keeps_everything():
    selected = [(_item("t-1"), object()), (_item("t-2"), object())]
    kept, skipped = fd.partition_local(selected, placer=None, self_node="node-158")
    assert kept == selected and skipped == []


def test_partition_local_splits_by_placement():
    selected = [(_item("t-1"), object()), (_item("t-2", ["node:heavy-build"]), object())]

    def placer(item):
        node = "node-41" if "node:heavy-build" in item.payload["tags"] else "node-158"
        return fd.DispatchDecision(ref=item.ref, node=node, reason="least-loaded")

    kept, skipped = fd.partition_local(selected, placer=placer, self_node="node-158")
    assert [it.ref for it, _ in kept] == ["t-1"]
    assert [(it.ref, d.node) for it, d in skipped] == [("t-2", "node-41")]


def test_partition_local_unschedulable_is_skipped_with_reason():
    selected = [(_item("t-1"), object())]

    def placer(item):
        return fd.DispatchDecision(ref=item.ref, node=None,
                                   reason="unschedulable (all filtered)")

    kept, skipped = fd.partition_local(selected, placer=placer, self_node="node-158")
    assert kept == [] and skipped[0][1].reason.startswith("unschedulable")


@pytest.mark.needs_skcapstone
def test_default_placer_unmanaged_tree_returns_none(monkeypatch, tmp_path):
    monkeypatch.setenv("SKFLEET_ROOT", str(tmp_path / "fleet"))
    monkeypatch.setenv("SKFLEET_NODE", "node-test")
    assert fd.default_placer() is None          # no admitted nodes: run local (3.6)


@pytest.mark.needs_skcapstone
def test_default_placer_places_and_persists_on_control_plane(monkeypatch, tmp_path):
    from skcapstone.fleet import events, sknoded, store
    from skcapstone.fleet.paths import FleetPaths

    events.reset_dedupe()
    monkeypatch.setenv("SKFLEET_ROOT", str(tmp_path / "fleet"))
    monkeypatch.setenv("SKFLEET_NODE", "node-158")
    monkeypatch.setattr("skcapstone.fleet.sknoded.node_capacity",
                        lambda: {"cores": 8, "ram_gb": 16.0, "disk_gb": 100.0,
                                 "gpu": None, "vram_gb": None})
    paths = FleetPaths(root=tmp_path / "fleet")
    operator = store.Writer(role="operator", node="node-158", identity="")
    sknoded.run_once(paths, "node-158")
    store.write_spec(paths, "node", "node-158", {"cordoned": False, "taints": []},
                     writer=operator, labels={"control-plane": "true"})
    sknoded.run_once(paths, "node-158")         # observe the admission
    placer = fd.default_placer()
    assert placer is not None
    decision = placer(_item("t-1"))
    assert decision.node == "node-158"
    assert decision.reason.startswith("least-loaded: node-158")
    # control-plane runs persist the audit record; others would only query
    assert store.read_placement(paths, "job", "t-1")["node"] == "node-158"
    events.reset_dedupe()


@pytest.mark.needs_skcapstone
def test_default_placer_unrecognized_self_builds_local(monkeypatch, tmp_path, capsys):
    # A node whose computed name is not in the roster (SKFLEET_NODE unset -> hostname
    # fallback) must NOT route every card off-node and strand all work. It falls back
    # to unmanaged (local build) and warns loudly.
    from skcapstone.fleet import sknoded, store
    from skcapstone.fleet.paths import FleetPaths

    monkeypatch.setenv("SKFLEET_ROOT", str(tmp_path / "fleet"))
    monkeypatch.setattr("skcapstone.fleet.sknoded.node_capacity",
                        lambda: {"cores": 8, "ram_gb": 16.0, "disk_gb": 100.0,
                                 "gpu": None, "vram_gb": None})
    paths = FleetPaths(root=tmp_path / "fleet")
    operator = store.Writer(role="operator", node="node-158", identity="")
    monkeypatch.setenv("SKFLEET_NODE", "node-158")
    sknoded.run_once(paths, "node-158")
    store.write_spec(paths, "node", "node-158", {"cordoned": False}, writer=operator)
    sknoded.run_once(paths, "node-158")
    monkeypatch.setenv("SKFLEET_NODE", "node-not-enrolled")   # self is not in the roster
    assert fd.default_placer() is None                        # unmanaged -> build local
    assert "not in the fleet roster" in capsys.readouterr().err


@pytest.mark.needs_skcapstone
def test_default_placer_frozen_skips_everything(monkeypatch, tmp_path):
    from skcapstone.fleet import sknoded, store
    from skcapstone.fleet.paths import FleetPaths

    monkeypatch.setenv("SKFLEET_ROOT", str(tmp_path / "fleet"))
    monkeypatch.setenv("SKFLEET_NODE", "node-158")
    monkeypatch.setattr("skcapstone.fleet.sknoded.node_capacity",
                        lambda: {"cores": 8, "ram_gb": 16.0, "disk_gb": 100.0,
                                 "gpu": None, "vram_gb": None})
    paths = FleetPaths(root=tmp_path / "fleet")
    operator = store.Writer(role="operator", node="node-158", identity="")
    sknoded.run_once(paths, "node-158")
    store.write_spec(paths, "node", "node-158", {"cordoned": False}, writer=operator)
    store.set_frozen(paths, True, writer=operator, reason="drill")
    placer = fd.default_placer()
    decision = placer(_item("t-1"))
    assert decision.node is None and "frozen" in decision.reason
