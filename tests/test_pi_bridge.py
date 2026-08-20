import json
from pathlib import Path

import pytest

from skharness.arena.pi_bridge import PI_PROFILES, BridgeDeniedError, ScopedPiBridge
from skharness.autocode.adapters.pi import PiAdapter
from skharness.autocode.sandbox import Sandbox


def test_profiles_are_fail_closed_and_operator_has_no_baked_authority():
    assert PI_PROFILES["operator"].sk_operations == frozenset()
    with pytest.raises(BridgeDeniedError, match="unknown"):
        ScopedPiBridge("typo", lambda operation, payload: {})


def test_build_profile_can_append_result_but_cannot_validate_or_promote_memory():
    seen = []
    bridge = ScopedPiBridge(
        "arena-build", lambda operation, payload: seen.append((operation, payload)) or {"ok": True})
    assert bridge.invoke("arena.result.append", {"experiment_id": "e1"}) == {"ok": True}
    assert seen == [("arena.result.append", {"experiment_id": "e1"})]
    assert bridge.invoke("arena.negative.search", {"reason": "oom"}) == {"ok": True}
    assert bridge.invoke("arena.experiment.reproduce", {"source_id": "e1"}) == {"ok": True}
    for denied in ("arena.verdict.append", "memory.proposal.append", "capstone.card.claim"):
        with pytest.raises(BridgeDeniedError, match="not granted"):
            bridge.invoke(denied, {})


def test_verifier_cannot_write_build_results_or_memory():
    bridge = ScopedPiBridge("arena-verify", lambda operation, payload: {"ok": True})
    assert bridge.invoke("arena.verdict.append", {"verdict": "valid"}) == {"ok": True}
    with pytest.raises(BridgeDeniedError):
        bridge.invoke("arena.result.append", {})
    with pytest.raises(BridgeDeniedError):
        bridge.invoke("memory.recall", {})


def test_payload_must_be_an_object_and_backend_must_return_one():
    bridge = ScopedPiBridge("arena-build", lambda operation, payload: [])
    with pytest.raises(BridgeDeniedError, match="JSON object"):
        bridge.invoke("memory.recall", ["not", "an", "object"])
    with pytest.raises(RuntimeError, match="non-object"):
        bridge.invoke("memory.recall", {})


def test_adapter_loads_only_pinned_extension_and_profile_tool_allowlist():
    adapter = PiAdapter(
        Sandbox(), model="bucket", base_url="http://gateway/v1",
        capability_profile="arena-verify")
    argv = adapter._argv("verify")
    assert argv[argv.index("--no-extensions") + 1:argv.index("--tools")] == [
        "-e", "/opt/skharness/pi/sk-bridge.ts"]
    tools = set(argv[argv.index("--tools") + 1].split(","))
    assert tools == set(PI_PROFILES["arena-verify"].pi_tools)
    assert "write" not in tools and "arena_result_append" not in tools
    assert adapter._auth_env()["SKHARNESS_PI_PROFILE"] == "arena-verify"


def test_image_manifest_pins_base_pi_and_two_targets():
    root = Path(__file__).parents[1]
    dockerfile = (root / "docker/sandbox/pi/Dockerfile").read_text()
    lock = json.loads((root / "docker/sandbox/pi/dependencies.lock.json").read_text())
    assert f"node:24-bookworm@{lock['base_image']['index_digest']}" in dockerfile
    assert f"ARG PI_VERSION={lock['npm']['version']}" in dockerfile
    assert "AS pi-core" in dockerfile and "AS pi-polyglot" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "@${PI_VERSION}" in dockerfile
    assert lock["npm"]["integrity"].startswith("sha512-")


def test_no_profile_preserves_existing_adapter_contract():
    adapter = PiAdapter(Sandbox(), model="m", base_url="http://gateway/v1")
    assert "--no-extensions" not in adapter._argv("plain")
    assert "SKHARNESS_PI_PROFILE" not in adapter._auth_env()
