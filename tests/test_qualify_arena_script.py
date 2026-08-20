import importlib.util
import json
import threading
from argparse import Namespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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
        body = {"status": "ok"} if self.path == "/health" else {
            "object": "list", "data": [{"id": "arena-model"}]}
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def do_POST(self):
        length = int(self.headers["content-length"])
        body = json.loads(self.rfile.read(length))
        type(self).requests.append((self.command, self.path, dict(self.headers), body))
        response = {"id": "completion-1", "model": "served-model",
                    "choices": [{"message": {"content": "qualified"}}]}
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("x-sk-req-id", "request-1")
        self.send_header("x-sk-backend", "reg:ornith")
        self.send_header("x-sk-model-served", "served-model")
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())


def test_live_qualification_records_gateway_and_pi_evidence_without_secret(
        tmp_path, monkeypatch):
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
        assert config_dir.stat().st_mode & 0o777 == 0o755
        assert (config_dir / "models.json").stat().st_mode & 0o777 == 0o444
        message = {"role": "assistant", "responseModel": "served-model",
                   "content": [{"type": "text", "text": "qualified"}]}
        return {"argv": argv, "exit_code": 0,
                "stdout": json.dumps({"type": "message_end", "message": message}),
                "stderr": ""}

    monkeypatch.setattr(QUALIFY, "command", fake_command)
    args = Namespace(
        live_gateway=f"http://127.0.0.1:{server.server_port}",
        gateway_api_key_env="TEST_GATEWAY_KEY", gateway_model="arena-model",
        session_id="session-1", card_id="card-1", prompt="qualify", context_limit=4096,
        output_limit=256, image="pi:test",
    )
    try:
        evidence = QUALIFY.live_qualification(args)
    finally:
        server.shutdown()
        thread.join()

    assert evidence["health"]["status"] == 200
    assert evidence["models"]["body"]["data"][0]["id"] == "arena-model"
    assert evidence["completion_status"] == 200
    assert evidence["attribution"] == {
        "x-sk-req-id": "request-1", "x-request-id": None,
        "x-sk-backend": "reg:ornith", "x-sk-model-served": "served-model",
    }
    assert evidence["attribution_source"] == "direct_openai_compatible_probe"
    assert evidence["pi_request_headers"] == {
        "x-session-id": "session-1", "x-sk-card-id": "card-1"}
    assert evidence["pi"] == {"exit_code": 0, "stderr": "",
                              "responseModel": "served-model", "output": "qualified"}
    assert secret not in json.dumps(evidence)
    post = next(item for item in Gateway.requests if item[0] == "POST")
    assert post[1] == "/v1/chat/completions"
    assert {key.lower(): value for key, value in post[2].items()}["x-sk-card-id"] == "card-1"


def test_live_qualification_requires_named_secret(monkeypatch):
    monkeypatch.delenv("ABSENT_GATEWAY_KEY", raising=False)
    args = Namespace(gateway_api_key_env="ABSENT_GATEWAY_KEY")
    try:
        QUALIFY.live_qualification(args)
    except ValueError as exc:
        assert "ABSENT_GATEWAY_KEY" in str(exc)
    else:
        raise AssertionError("missing key should fail before network or Docker access")
