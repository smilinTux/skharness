"""Local Pi -> OpenAI-compatible SKGateway contract test (no external network)."""
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Gateway(BaseHTTPRequestHandler):
    request = None

    def log_message(self, *_args):
        pass

    def do_POST(self):
        length = int(self.headers["content-length"])
        type(self).request = {
            "path": self.path,
            "headers": dict(self.headers),
            "body": json.loads(self.rfile.read(length)),
        }
        chunks = [
            {"id": "mock-1", "object": "chat.completion.chunk", "choices": [
                {"index": 0, "delta": {"role": "assistant", "content": "{\"qualified\":true}"},
                 "finish_reason": None}]},
            {"id": "mock-1", "object": "chat.completion.chunk", "choices": [
                {"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]
        data = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        self.wfile.write(data.encode())


def test_real_pi_routes_to_local_mock_skgateway_with_attribution(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = {"providers": {"skgw": {
            "baseUrl": f"http://127.0.0.1:{server.server_port}/v1",
            "api": "openai-completions", "apiKey": "sk-local",
            "headers": {"x-session-id": "qualification-1", "x-sk-card-id": "0c79fa63"},
            "compat": {"supportsDeveloperRole": False},
            "models": [{"id": "reference", "limit": {"context": 4096, "output": 256}}],
        }}}
        (tmp_path / "models.json").write_text(json.dumps(config))
        env = {**os.environ, "PI_CODING_AGENT_DIR": str(tmp_path),
               "HOME": str(tmp_path / "home"), "NO_PROXY": "127.0.0.1"}
        proc = subprocess.run(
            ["pi", "-p", "Return JSON", "--mode", "json", "--no-session",
             "--no-tools", "--model", "skgw/reference", "--api-key", "sk-local"],
            env=env, capture_output=True, text=True, timeout=30, check=False,
        )
    finally:
        server.shutdown()
        thread.join()
    assert proc.returncode == 0, proc.stderr
    events = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    assistant = [event for event in events if event.get("type") == "message_end"
                 and event.get("message", {}).get("role") == "assistant"]
    assert assistant
    text = "".join(part.get("text", "") for part in assistant[-1]["message"]["content"])
    assert json.loads(text) == {"qualified": True}
    assert Gateway.request["path"] == "/v1/chat/completions"
    assert Gateway.request["body"]["model"] == "reference"
    headers = {key.lower(): value for key, value in Gateway.request["headers"].items()}
    assert headers["x-session-id"] == "qualification-1"
    assert headers["x-sk-card-id"] == "0c79fa63"
