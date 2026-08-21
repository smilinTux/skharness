"""Confined Pi-container -> mock SKGateway integration proof.

This is deliberately opt-in because it creates Docker resources and requires an
already-pulled, immutable Pi image reference.  The gateway and worker use that
same image on a unique ``--internal`` network; no auxiliary mutable image is
introduced.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from skharness.autocode.adapters.pi import PiAdapter

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_PI_GATEWAY_CONTAINER_IT") != "1" or not shutil.which("docker"),
    reason=(
        "integration: set RUN_PI_GATEWAY_CONTAINER_IT=1 and provide "
        "PI_GATEWAY_TEST_IMAGE=<registry>@sha256:<digest>"
    ),
)

_IMMUTABLE_IMAGE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")

_GATEWAY = r'''
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

capture = Path("/qualification/request.json")
ready = Path("/qualification/gateway-ready")

class Gateway(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            return
        self.send_error(404)

    def do_POST(self):
        length = int(self.headers["content-length"])
        request = {
            "path": self.path,
            "headers": dict(self.headers),
            "body": json.loads(self.rfile.read(length)),
        }
        capture.write_text(json.dumps(request))
        chunks = [
            {
                "id": "confined-mock-1",
                "object": "chat.completion.chunk",
                "model": "served-reference",
                "choices": [{
                    "index": 0,
                    "delta": {"role": "assistant", "content": "{\"qualified\":true}"},
                    "finish_reason": None,
                }],
            },
            {
                "id": "confined-mock-1",
                "object": "chat.completion.chunk",
                "model": "served-reference",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
        ]
        body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        body += "data: [DONE]\n\n"
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("x-sk-req-id", "confined-mock-request-1")
        self.send_header("x-sk-backend", "confined-mock")
        self.send_header("x-sk-model-served", "served-reference")
        self.end_headers()
        self.wfile.write(body.encode())

server = ThreadingHTTPServer(("0.0.0.0", 8080), Gateway)
ready.write_text("ready\n")
server.serve_forever()
'''

_DIRECT_EGRESS_PROBE = r'''
import json
import socket
import sys
from pathlib import Path

result = {"target": "1.1.1.1:443", "direct_public_egress": "blocked"}
try:
    connection = socket.create_connection(("1.1.1.1", 443), timeout=3)
except OSError as exc:
    result["error_type"] = type(exc).__name__
else:
    connection.close()
    result["direct_public_egress"] = "REACHED"
Path("/qualification/egress.json").write_text(json.dumps(result))
if result["direct_public_egress"] != "blocked":
    sys.exit(23)
'''


def _run(argv: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _remove_container(name: str) -> None:
    _run(["docker", "rm", "--force", name])
    if _run(["docker", "container", "inspect", name]).returncode == 0:
        raise RuntimeError(f"failed to remove integration container {name}")


def _remove_network(name: str) -> None:
    # Container removal can be asynchronous in some Docker versions.
    for _attempt in range(3):
        if _run(["docker", "network", "rm", name]).returncode == 0:
            return
        time.sleep(0.2)
    if _run(["docker", "network", "inspect", name]).returncode == 0:
        raise RuntimeError(f"failed to remove integration network {name}")


def _cleanup_resources(*, worker: str, gateway: str, network: str) -> None:
    """Attempt every cleanup action, then report the complete failure set."""
    errors = []
    removals = (
        ("worker container", worker, _remove_container),
        ("gateway container", gateway, _remove_container),
        ("network", network, _remove_network),
    )
    for kind, name, remove in removals:
        try:
            remove(name)
        except Exception as exc:  # cleanup must continue after an individual failure
            errors.append(f"{kind} {name}: {type(exc).__name__}: {exc}")
    if errors:
        raise RuntimeError("integration cleanup failed:\n- " + "\n- ".join(errors))


def _write_fixture(root: Path) -> None:
    root.chmod(0o777)
    (root / "gateway.py").write_text(_GATEWAY)
    (root / "egress_probe.py").write_text(_DIRECT_EGRESS_PROBE)
    adapter = PiAdapter(
        model="reference",
        base_url="http://skgw:8080/v1",
        session_id="c0c28bbe-container-it",
        card_id="c0c28bbe",
    )
    config = adapter._config_files()["/agent/models.json"]
    (root / "models.json").write_text(config)


def _wait_for_gateway(root: Path, container: str) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if (root / "gateway-ready").is_file():
            return
        state = _run(["docker", "inspect", "--format", "{{.State.Running}}", container])
        if state.returncode != 0 or state.stdout.strip() != "true":
            logs = _run(["docker", "logs", container]).stdout
            pytest.fail(f"mock gateway exited before readiness: {logs}")
        time.sleep(0.1)
    logs = _run(["docker", "logs", container]).stdout
    pytest.fail(f"mock gateway did not become ready: {logs}")


def test_pi_container_routes_to_mock_gateway_without_direct_egress(tmp_path: Path) -> None:
    image = os.environ.get("PI_GATEWAY_TEST_IMAGE", "")
    assert _IMMUTABLE_IMAGE.fullmatch(image), (
        "PI_GATEWAY_TEST_IMAGE must be a registry reference pinned with "
        "@sha256:<64 lowercase hex characters>"
    )
    inspected = _run(["docker", "image", "inspect", image])
    assert inspected.returncode == 0, (
        f"immutable image is not present locally; pull it first: {image}\n{inspected.stderr}"
    )

    _write_fixture(tmp_path)
    suffix = uuid.uuid4().hex[:12]
    network = f"skh-pi-gw-{suffix}"
    gateway = f"skh-pi-gateway-{suffix}"
    worker = f"skh-pi-worker-{suffix}"
    mounted = f"{tmp_path}:/qualification:rw"

    created = _run(["docker", "network", "create", "--internal", network])
    assert created.returncode == 0, created.stderr
    try:
        started = _run(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                gateway,
                "--network",
                network,
                "--network-alias",
                "skgw",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                "64",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,nodev,size=16m",
                "--volume",
                mounted,
                image,
                "python3",
                "/qualification/gateway.py",
            ]
        )
        assert started.returncode == 0, started.stderr
        _wait_for_gateway(tmp_path, gateway)

        result = _run(
            [
                "docker",
                "run",
                "--rm",
                "--name",
                worker,
                "--network",
                network,
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                "128",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,nodev,size=32m",
                "--tmpfs",
                "/home/sbx:rw,noexec,nosuid,nodev,size=16m",
                "--volume",
                mounted,
                "--env",
                "PI_CODING_AGENT_DIR=/qualification",
                image,
                "sh",
                "-c",
                (
                    "python3 /qualification/egress_probe.py && "
                    "exec pi -p \"$1\" --mode json --no-session --no-tools "
                    "--model skgw/reference --api-key sk-local"
                ),
                "c0c28bbe-container-it",
                "Return JSON",
            ],
            timeout=60,
        )
        assert result.returncode == 0, (
            f"confined Pi worker failed ({result.returncode})\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

        events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        assistant = [
            event
            for event in events
            if event.get("type") == "message_end"
            and event.get("message", {}).get("role") == "assistant"
        ]
        assert assistant
        message = assistant[-1]["message"]
        assert message["responseModel"] == "served-reference"
        output = "".join(part.get("text", "") for part in message["content"])
        assert json.loads(output) == {"qualified": True}

        request = json.loads((tmp_path / "request.json").read_text())
        assert request["path"] == "/v1/chat/completions"
        assert request["body"]["model"] == "reference"
        headers = {key.lower(): value for key, value in request["headers"].items()}
        assert headers["x-session-id"] == "c0c28bbe-container-it"
        assert headers["x-sk-card-id"] == "c0c28bbe"

        egress = json.loads((tmp_path / "egress.json").read_text())
        assert egress["direct_public_egress"] == "blocked"
    finally:
        _cleanup_resources(worker=worker, gateway=gateway, network=network)


def test_cleanup_attempts_every_resource_before_reporting(monkeypatch) -> None:
    attempts = []

    def fail_container(name: str) -> None:
        attempts.append(("container", name))
        raise RuntimeError(f"cannot remove {name}")

    def fail_network(name: str) -> None:
        attempts.append(("network", name))
        raise RuntimeError(f"cannot remove {name}")

    monkeypatch.setitem(globals(), "_remove_container", fail_container)
    monkeypatch.setitem(globals(), "_remove_network", fail_network)

    with pytest.raises(RuntimeError) as raised:
        _cleanup_resources(worker="worker-1", gateway="gateway-1", network="network-1")

    assert attempts == [
        ("container", "worker-1"),
        ("container", "gateway-1"),
        ("network", "network-1"),
    ]
    message = str(raised.value)
    assert "worker container worker-1" in message
    assert "gateway container gateway-1" in message
    assert "network network-1" in message
