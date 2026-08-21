import importlib.util
import json
import threading
from argparse import Namespace
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import request

from skharness.arena.models import Experiment, Result, VerificationState

SCRIPT = Path(__file__).parents[1] / "scripts" / "qualify-arena.py"
SPEC = importlib.util.spec_from_file_location("qualify_arena", SCRIPT)
QUALIFY = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(QUALIFY)


class Gateway(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *_args):
        pass

    def do_GET(self):
        type(self).requests.append((self.command, self.path, dict(self.headers), None))
        body = (
            {"status": "ok"}
            if self.path == "/health"
            else {"object": "list", "data": [{"id": "arena-model"}]}
        )
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def do_POST(self):
        length = int(self.headers["content-length"])
        body = json.loads(self.rfile.read(length))
        type(self).requests.append((self.command, self.path, dict(self.headers), body))
        request_number = len([item for item in type(self).requests if item[0] == "POST"])
        response = {
            "id": f"completion-{request_number}",
            "model": "served-model",
            "choices": [{"message": {"content": "qualified"}}],
        }
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("x-sk-req-id", f"request-{request_number}")
        self.send_header("x-sk-backend", f"reg:ornith-{request_number}")
        self.send_header("x-sk-model-served", "served-model")
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())


def test_live_qualification_records_gateway_and_pi_evidence_without_secret(tmp_path, monkeypatch):
    Gateway.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    secret = "extremely-secret-key"
    monkeypatch.setenv("TEST_GATEWAY_KEY", secret)

    def fake_command(argv, *, truncate=True):
        assert truncate is False
        assert secret not in argv
        config_dir = Path(argv[argv.index("-v") + 1].split(":", 1)[0])
        config = json.loads((config_dir / "models.json").read_text())
        assert config["providers"]["skgw"]["apiKey"] == secret
        relay_request = request.Request(
            config["providers"]["skgw"]["baseUrl"] + "/chat/completions",
            data=json.dumps(
                {
                    "model": "arena-model",
                    "stream": True,
                    "messages": [{"role": "user", "content": "qualify"}],
                }
            ).encode(),
            headers={
                "authorization": f"Bearer {secret}",
                "content-type": "application/json",
                "x-session-id": "session-1",
                "x-sk-card-id": "card-1",
            },
            method="POST",
        )
        with request.urlopen(relay_request, timeout=5) as response:
            assert json.loads(response.read())["model"] == "served-model"
        assert config_dir.stat().st_mode & 0o777 == 0o755
        assert (config_dir / "models.json").stat().st_mode & 0o777 == 0o444
        message = {
            "role": "assistant",
            "responseModel": "served-model-variant",
            "content": [{"type": "text", "text": "qualified"}],
        }
        return {
            "argv": argv,
            "exit_code": 0,
            "stdout": json.dumps({"type": "message_end", "message": message}),
            "stderr": "",
        }

    monkeypatch.setattr(QUALIFY, "command", fake_command)
    args = Namespace(
        live_gateway=f"http://127.0.0.1:{server.server_port}",
        gateway_api_key_env="TEST_GATEWAY_KEY",
        gateway_model="arena-model",
        session_id="session-1",
        card_id="card-1",
        prompt="qualify",
        context_limit=4096,
        output_limit=256,
        image="pi:test",
    )
    try:
        evidence = QUALIFY.live_qualification(args)
    finally:
        server.shutdown()
        thread.join()

    assert evidence["health"]["status"] == 200
    assert evidence["models"]["body"]["data"][0]["id"] == "arena-model"
    assert evidence["completion_status"] == 200
    records = QUALIFY.immutable_execution_records(args, evidence)
    assert [record["experiment"]["harness"] for record in records] == ["direct", "pi"]
    assert len({record["experiment_hash"] for record in records}) == 2
    assert len({record["result_hash"] for record in records}) == 2
    for record in records:
        experiment = Experiment.model_validate(record["experiment"])
        result = Result.model_validate(record["result"])
        assert experiment.content_hash == record["experiment_hash"]
        assert result.content_hash == record["result_hash"]
        assert result.experiment_hash == experiment.content_hash
        assert result.verification is VerificationState.UNVERIFIED
    assert evidence["attribution"] == {
        "x-sk-req-id": "request-1",
        "x-request-id": None,
        "x-sk-backend": "reg:ornith-1",
        "x-sk-model-served": "served-model",
    }
    assert evidence["attribution_source"] == "direct_openai_compatible_probe"
    assert evidence["pi_request_headers"] == {
        "x-session-id": "session-1",
        "x-sk-card-id": "card-1",
    }
    assert evidence["pi"] == {
        "exit_code": 0,
        "stderr": "",
        "raw_event_stream": json.dumps({
            "type": "message_end",
            "message": {
                "role": "assistant",
                "responseModel": "served-model-variant",
                "content": [{"type": "text", "text": "qualified"}],
            },
        }),
        "responseModel": "served-model-variant",
        "served_model": "served-model",
        "completion_status": 200,
        "attribution": {
            "x-sk-req-id": "request-2",
            "x-request-id": None,
            "x-sk-backend": "reg:ornith-2",
            "x-sk-model-served": "served-model",
        },
        "attribution_source": "ephemeral_loopback_relay_observation",
        "output": "qualified",
    }
    assert evidence["pi_relay"]["captured_requests"] == 1
    assert secret not in json.dumps(evidence)
    assert json.loads(evidence["pi"]["raw_event_stream"])["message"]["responseModel"] == (
        "served-model-variant"
    )
    post = next(item for item in Gateway.requests if item[0] == "POST")
    assert post[1] == "/v1/chat/completions"
    assert {key.lower(): value for key, value in post[2].items()}["x-sk-card-id"] == "card-1"
    assert records[0]["experiment"]["gateway_request_id"] == "request-1"
    assert records[0]["experiment"]["gateway_backend_id"] == "reg:ornith-1"
    assert records[1]["experiment"]["gateway_request_id"] == "request-2"
    assert records[1]["experiment"]["gateway_backend_id"] == "reg:ornith-2"
    assert records[1]["experiment"]["served_model"] == "served-model-variant"
    assert (
        records[1]["experiment"]["configuration"]["gateway_header_served_model"] == "served-model"
    )
    planted = QUALIFY.planted_false_high_score(records[1])
    assert planted["assistant_output"] == "planted-false-output"
    assert planted["result"]["measurements"][0]["mean"] == 999_999_999
    assert planted["result"]["experiment_hash"] == planted["experiment_hash"]


def test_live_qualification_requires_named_secret(monkeypatch):
    monkeypatch.delenv("ABSENT_GATEWAY_KEY", raising=False)
    args = Namespace(gateway_api_key_env="ABSENT_GATEWAY_KEY")
    try:
        QUALIFY.live_qualification(args)
    except ValueError as exc:
        assert "ABSENT_GATEWAY_KEY" in str(exc)
    else:
        raise AssertionError("missing key should fail before network or Docker access")


def test_independent_worker_gate_requires_distinct_attributed_verified_runs():
    workers = [
        {
            "request_id": f"request-{index}",
            "backend": "chiap08",
            "served_model": "qwen3.8-27b",
            "record": {"experiment_hash": f"sha256:experiment-{index}"},
            "verification": {"status": "valid", "admitted": True},
        }
        for index in (1, 2)
    ]

    assert QUALIFY.independent_workers_valid(workers, 2)
    for field in ("request_id", "backend", "served_model"):
        broken = deepcopy(workers)
        broken[1][field] = None
        assert not QUALIFY.independent_workers_valid(broken, 2)
    duplicate = deepcopy(workers)
    duplicate[1]["request_id"] = duplicate[0]["request_id"]
    assert not QUALIFY.independent_workers_valid(duplicate, 2)
    duplicate = deepcopy(workers)
    duplicate[1]["record"] = duplicate[0]["record"]
    assert not QUALIFY.independent_workers_valid(duplicate, 2)
    invalid = deepcopy(workers)
    invalid[1]["verification"] = {"status": "invalid", "admitted": False}
    assert not QUALIFY.independent_workers_valid(invalid, 2)


def test_relay_extracts_served_model_from_stream_body():
    capture = {
        "body": (
            b'data: {"id":"one","model":"served-stream"}\n\n'
            b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
            b"data: [DONE]\n\n"
        )
    }
    assert QUALIFY.relay_response_model(capture) == "served-stream"


def test_relay_rejects_gateway_url_credentials():
    try:
        QUALIFY.PiGatewayRelay("https://secret@example.invalid")
    except ValueError as exc:
        assert "credentials" in str(exc)
    else:
        raise AssertionError("credential-bearing URL must fail before provenance capture")
