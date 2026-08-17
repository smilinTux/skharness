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


# ---------------------------------------------------------------------------
# Card P6 (coord `08963fbb`): `_freeze.json` gets the same signature check
# `_protected.json` got in `test_autocode_protected.py`, gated by the same
# `SKFLEET_SIGNING` flag. `off` (unset, the default) reproduces
# `test_default_placer_frozen_skips_everything` and its unfrozen counterpart
# unchanged; `enforce` cannot trust `frozen: false` written without a
# signature, and "cannot trust the kill switch says off" must mean the same
# thing `is_frozen` already means for an unreadable file: halt.
# ---------------------------------------------------------------------------

def _fake_signer(data: bytes) -> str:
    import hashlib
    return "sig:" + hashlib.sha256(data).hexdigest()


def _fake_verifier(data: bytes, sig: str) -> bool:
    import hashlib
    return sig == "sig:" + hashlib.sha256(data).hexdigest()


def _admit_one_node(tmp_path, monkeypatch, node="node-158"):
    from skcapstone.fleet import sknoded, store
    from skcapstone.fleet.paths import FleetPaths

    monkeypatch.setenv("SKFLEET_ROOT", str(tmp_path / "fleet"))
    monkeypatch.setenv("SKFLEET_NODE", node)
    monkeypatch.setattr("skcapstone.fleet.sknoded.node_capacity",
                        lambda: {"cores": 8, "ram_gb": 16.0, "disk_gb": 100.0,
                                 "gpu": None, "vram_gb": None})
    paths = FleetPaths(root=tmp_path / "fleet")
    operator = store.Writer(role="operator", node=node, identity="")
    sknoded.run_once(paths, node)
    store.write_spec(paths, "node", node, {"cordoned": False}, writer=operator)
    sknoded.run_once(paths, node)
    return paths, operator


@pytest.mark.needs_skcapstone
def test_freeze_trust_off_leaves_unsigned_unfrozen_file_working(monkeypatch, tmp_path):
    from skcapstone.fleet import signing as fleet_signing

    monkeypatch.delenv(fleet_signing.SIGNING_ENV, raising=False)
    _admit_one_node(tmp_path, monkeypatch)
    placer = fd.default_placer()
    decision = placer(_item("t-1"))
    assert decision.node == "node-158"          # unsigned "frozen: false" still honored


@pytest.mark.needs_skcapstone
def test_freeze_trust_enforce_halts_on_unsigned_freeze_file(monkeypatch, tmp_path):
    """The migration case: `_freeze.json` written before signing existed
    (`writer.signature: null`) must not silently become trusted once
    enforcement is on. Content says `frozen: false`; the dispatcher must
    still refuse, because it cannot confirm a human wrote that."""
    from skcapstone.fleet import signing as fleet_signing

    monkeypatch.setattr(fleet_signing, "capauth_verifier", lambda: _fake_verifier)
    _admit_one_node(tmp_path, monkeypatch)
    monkeypatch.setenv(fleet_signing.SIGNING_ENV, "enforce")   # flip AFTER admission
    placer = fd.default_placer()
    decision = placer(_item("t-1"))
    assert decision.node is None and "frozen" in decision.reason


@pytest.mark.needs_skcapstone
def test_freeze_trust_enforce_honors_a_validly_signed_unfrozen_file(monkeypatch, tmp_path):
    from skcapstone.fleet import signing as fleet_signing
    from skcapstone.fleet.paths import FleetPaths

    monkeypatch.setattr(fleet_signing, "capauth_verifier", lambda: _fake_verifier)
    paths, operator = _admit_one_node(tmp_path, monkeypatch)
    signed = {
        "frozen": False, "reason": "", "updatedAt": "2026-08-17T00:00:00Z",
        "writer": {"identity": "capauth:chef@skworld.io", "node": "cli",
                   "role": "operator", "signature": None},
    }
    signed["writer"]["signature"] = _fake_signer(fleet_signing.canonical_bytes(signed))
    FleetPaths(root=paths.root).freeze_path().write_text(__import__("json").dumps(signed))
    monkeypatch.setenv(fleet_signing.SIGNING_ENV, "enforce")
    placer = fd.default_placer()
    decision = placer(_item("t-1"))
    assert decision.node == "node-158"


@pytest.mark.needs_skcapstone
def test_freeze_trust_enforce_still_halts_on_an_actually_frozen_signed_file(monkeypatch, tmp_path):
    """A validly-signed freeze file that says `frozen: true` still halts:
    signing adds trust in the content, it does not flip the content."""
    from skcapstone.fleet import signing as fleet_signing
    from skcapstone.fleet.paths import FleetPaths

    monkeypatch.setattr(fleet_signing, "capauth_verifier", lambda: _fake_verifier)
    paths, operator = _admit_one_node(tmp_path, monkeypatch)
    signed = {
        "frozen": True, "reason": "chef says stop", "updatedAt": "2026-08-17T00:00:00Z",
        "writer": {"identity": "capauth:chef@skworld.io", "node": "cli",
                   "role": "operator", "signature": None},
    }
    signed["writer"]["signature"] = _fake_signer(fleet_signing.canonical_bytes(signed))
    FleetPaths(root=paths.root).freeze_path().write_text(__import__("json").dumps(signed))
    monkeypatch.setenv(fleet_signing.SIGNING_ENV, "enforce")
    placer = fd.default_placer()
    decision = placer(_item("t-1"))
    assert decision.node is None and "frozen" in decision.reason
