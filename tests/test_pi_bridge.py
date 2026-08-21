import json
import re
import subprocess
from pathlib import Path

import pytest

from skharness.arena.pi_bridge import (
    ARENA_BUILD_PROHIBITIONS,
    PI_PROFILES,
    BridgeDeniedError,
    ScopedPiBridge,
    _exec_backend,
)
from skharness.arena.sk_backend import BACKEND_OPERATIONS
from skharness.autocode.adapters.pi import PiAdapter
from skharness.autocode.sandbox import Sandbox


def test_profiles_are_fail_closed_and_operator_has_no_baked_authority():
    assert PI_PROFILES["operator"].sk_operations == frozenset()
    with pytest.raises(BridgeDeniedError, match="unknown"):
        ScopedPiBridge("typo", lambda operation, payload: {})


def test_build_profile_can_append_result_but_cannot_validate_or_promote_memory():
    seen = []
    bridge = ScopedPiBridge(
        "arena-build", lambda operation, payload: seen.append((operation, payload)) or {"ok": True}
    )
    assert bridge.invoke("arena.result.append", {"experiment_id": "e1"}) == {"ok": True}
    assert seen == [("arena.result.append", {"experiment_id": "e1"})]
    assert bridge.invoke("arena.negative.search", {"reason": "oom"}) == {"ok": True}
    assert bridge.invoke("arena.experiment.reproduce", {"source_id": "e1"}) == {"ok": True}
    for denied in ("arena.verdict.append", "memory.proposal.append", "capstone.card.claim"):
        with pytest.raises(BridgeDeniedError, match="not granted"):
            bridge.invoke(denied, {})


def test_arena_build_cannot_complete_cards_read_hidden_tests_promote_or_change_gateway():
    bridge = ScopedPiBridge("arena-build", lambda operation, payload: {"ok": True})
    assert ARENA_BUILD_PROHIBITIONS.isdisjoint(PI_PROFILES["arena-build"].sk_operations)
    for operation in ARENA_BUILD_PROHIBITIONS:
        with pytest.raises(BridgeDeniedError, match="not granted"):
            bridge.invoke(operation, {})


def test_all_four_profile_allowlists_are_explicit_subsets_of_backend_schema():
    assert set(PI_PROFILES) == {"arena-build", "arena-verify", "project-full", "operator"}
    for profile in PI_PROFILES.values():
        assert profile.sk_operations <= BACKEND_OPERATIONS
    assert PI_PROFILES["operator"].builtin_tools == ()
    assert PI_PROFILES["operator"].sk_operations == frozenset()


def test_extension_profile_and_backend_operation_names_have_schema_parity():
    source = (Path(__file__).parents[1] / "docker/sandbox/pi/sk-bridge.ts").read_text()
    block = re.search(r"const operations = \[(.*?)\] as const;", source, re.DOTALL)
    assert block is not None
    extension_operations = frozenset(re.findall(r'"([^"]+)"', block.group(1)))
    granted = frozenset().union(*(profile.sk_operations for profile in PI_PROFILES.values()))
    assert extension_operations == BACKEND_OPERATIONS == granted


def test_backend_startup_failure_and_authorization_denial_fail_closed(monkeypatch):
    monkeypatch.delenv("SKHARNESS_SK_BRIDGE_BACKEND", raising=False)
    with pytest.raises(RuntimeError, match="absolute mounted executable"):
        _exec_backend("memory.recall", {})
    monkeypatch.setenv("SKHARNESS_SK_BRIDGE_BACKEND", "/does/not/exist")
    with pytest.raises(RuntimeError, match="could not start: FileNotFoundError"):
        _exec_backend("memory.recall", {})

    def denied(_operation, _payload):
        raise PermissionError("capauth scope missing")

    bridge = ScopedPiBridge("arena-build", denied)
    with pytest.raises(BridgeDeniedError, match="authorization denied"):
        bridge.invoke("memory.recall", {"query": "safe"})


def test_backend_auth_failure_does_not_reflect_secret_bearing_stderr(monkeypatch):
    monkeypatch.setenv("SKHARNESS_SK_BRIDGE_BACKEND", "/mounted/backend")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=77, stdout="", stderr="Bearer super-secret"
        ),
    )
    with pytest.raises(RuntimeError, match="failed with exit 77") as exc:
        _exec_backend("memory.recall", {"query": "x"})
    assert "super-secret" not in str(exc.value)


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
        Sandbox(), model="bucket", base_url="http://gateway/v1", capability_profile="arena-verify"
    )
    argv = adapter._argv("verify")
    assert argv[argv.index("--no-extensions") + 1 : argv.index("--tools")] == [
        "-e",
        "/opt/skharness/pi/sk-bridge.ts",
    ]
    tools = set(argv[argv.index("--tools") + 1].split(","))
    assert tools == set(PI_PROFILES["arena-verify"].pi_tools)
    assert "write" not in tools and "arena_result_append" not in tools
    assert adapter._auth_env()["SKHARNESS_PI_PROFILE"] == "arena-verify"


def test_malicious_extension_and_ambient_pi_config_cannot_join_profile_launch():
    adapter = PiAdapter(
        Sandbox(), model="bucket", base_url="http://gateway/v1", capability_profile="arena-build"
    )
    argv = adapter._argv("build")
    assert argv.count("-e") == 1
    assert argv[argv.index("-e") + 1] == "/opt/skharness/pi/sk-bridge.ts"
    assert "--no-extensions" in argv
    assert not any("evil" in argument or "PI_PACKAGE" in argument for argument in argv)


def test_image_manifest_pins_base_pi_and_two_targets():
    root = Path(__file__).parents[1]
    dockerfile = (root / "docker/sandbox/pi/Dockerfile").read_text()
    lock = json.loads((root / "docker/sandbox/pi/dependencies.lock.json").read_text())
    assert f"{lock['base_image']['reference']}@{lock['base_image']['index_digest']}" in dockerfile
    assert f"ARG PI_VERSION={lock['npm']['version']}" in dockerfile
    assert "AS pi-core" in dockerfile and "AS pi-polyglot" in dockerfile
    assert "AS pi-python-test" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "package-lock.json" in dockerfile
    assert "ghcr.io/astral-sh/uv:0.9.27@sha256:" in dockerfile
    assert "COPY --from=uv-tools /uv /uvx /usr/local/bin/" in dockerfile
    polyglot = dockerfile.split("FROM pi-base AS pi-polyglot", 1)[1]
    assert "npm-12=12.0.2-r2" in polyglot
    python_test = dockerfile.split("FROM pi-polyglot AS pi-python-test", 1)[1]
    assert "pi-python-test-builder" in python_test
    assert "/opt/skharness/venv" in python_test
    assert "skharness-pi-python-test-preflight" in python_test
    assert '].version")" = "${PI_VERSION}"' in dockerfile
    assert lock["npm"]["integrity"].startswith("sha512-")


def test_core_image_keeps_build_and_package_managers_out_of_runtime():
    root = Path(__file__).parents[1]
    dockerfile = (root / "docker/sandbox/pi/Dockerfile").read_text()
    package_lock = json.loads((root / "docker/sandbox/pi/package-lock.json").read_text())
    runtime = dockerfile.split("FROM ${WOLFI_IMAGE} AS pi-base", 1)[1].split(
        "FROM pi-base AS pi-core", 1
    )[0]

    assert "COPY --from=pi-node-builder" in runtime
    assert "COPY --from=pi-python-runtime" in runtime
    assert "build-base" not in runtime
    assert "py3.13-pip" not in runtime
    assert "test-requirements.lock" not in runtime
    assert "npm-12" not in runtime
    pi_package = package_lock["packages"]["node_modules/@earendil-works/pi-coding-agent"]
    assert pi_package["version"] == "0.84.2"
    assert (
        pi_package["integrity"]
        == json.loads((root / "docker/sandbox/pi/dependencies.lock.json").read_text())["npm"][
            "integrity"
        ]
    )


def test_python_test_target_is_project_qualified_and_published_like_other_images():
    root = Path(__file__).parents[1]
    lock = (root / "docker/sandbox/pi/test-requirements.lock").read_text()
    for distribution in (
        "capauth",
        "httpx",
        "jsonschema",
        "pytest-asyncio",
        "pytest-mock",
        "skcapstone",
        "skcoord",
        "skmemory",
    ):
        assert f"{distribution}==" in lock
    assert "--hash=sha256:" in lock

    workflow = (root / ".github/workflows/pi-image.yml").read_text()
    assert workflow.count("target: [pi-core, pi-polyglot, pi-python-test]") == 2
    assert "skharness-pi-python-test-preflight" in workflow
    assert workflow.index("Qualify Python test capability") < workflow.index(
        "Keyless-sign immutable image digest"
    )


def test_no_profile_preserves_existing_adapter_contract():
    adapter = PiAdapter(Sandbox(), model="m", base_url="http://gateway/v1")
    assert "--no-extensions" not in adapter._argv("plain")
    assert "SKHARNESS_PI_PROFILE" not in adapter._auth_env()
