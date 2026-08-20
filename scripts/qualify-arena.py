#!/usr/bin/env python3
"""Generate a local, unsigned Evolution Arena qualification evidence bundle."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib import request


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def command(argv: list[str], *, truncate: bool = True) -> dict:
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    stdout = proc.stdout[-4000:] if truncate else proc.stdout
    stderr = proc.stderr[-4000:] if truncate else proc.stderr
    return {"argv": argv, "exit_code": proc.returncode,
            "stdout": stdout, "stderr": stderr}


def http_json(url: str, api_key: str, *, payload: dict | None = None,
              headers: dict[str, str] | None = None) -> dict:
    body = None if payload is None else json.dumps(payload).encode()
    req_headers = {"accept": "application/json", **(headers or {})}
    if api_key:
        req_headers["authorization"] = f"Bearer {api_key}"
    if body is not None:
        req_headers["content-type"] = "application/json"
    req = request.Request(url, data=body, headers=req_headers,
                          method="POST" if body is not None else "GET")
    with request.urlopen(req, timeout=20) as response:  # noqa: S310 - operator URL
        raw = response.read().decode()
        return {
            "status": response.status,
            "headers": {key.lower(): value for key, value in response.headers.items()},
            "body": json.loads(raw) if raw else None,
        }


def live_qualification(args: argparse.Namespace) -> dict:
    api_key = os.environ.get(args.gateway_api_key_env)
    if not api_key:
        raise ValueError(f"environment variable {args.gateway_api_key_env!r} is not set")
    gateway = args.live_gateway.rstrip("/")
    api_base = gateway if gateway.endswith("/v1") else gateway + "/v1"
    root = gateway[:-3].rstrip("/") if gateway.endswith("/v1") else gateway
    attribution = {"x-session-id": args.session_id, "x-sk-card-id": args.card_id}
    health = http_json(root + "/health", api_key)
    models = http_json(api_base + "/models", api_key)
    completion = http_json(
        api_base + "/chat/completions", api_key,
        payload={"model": args.gateway_model, "stream": False,
                 "messages": [{"role": "user", "content": args.prompt}]},
        headers=attribution,
    )
    response_headers = completion["headers"]
    observed_headers = {
        name: response_headers.get(name)
        for name in ("x-sk-req-id", "x-request-id", "x-sk-backend", "x-sk-model-served")
    }
    config = {"providers": {"skgw": {
        "baseUrl": api_base, "api": "openai-completions", "apiKey": "not-recorded",
        "headers": attribution, "compat": {"supportsDeveloperRole": False},
        "models": [{"id": args.gateway_model,
                    "limit": {"context": args.context_limit, "output": args.output_limit}}],
    }}}
    with tempfile.TemporaryDirectory(prefix="skharness-arena-") as temp:
        # tempfile directories are 0700 by default, but the worker runs as UID
        # 10001 and must be able to traverse the read-only bind mount.
        Path(temp).chmod(0o755)
        config_path = Path(temp) / "models.json"
        runtime_config = json.loads(json.dumps(config))
        runtime_config["providers"]["skgw"]["apiKey"] = api_key
        config_path.write_text(json.dumps(runtime_config))
        config_path.chmod(0o444)
        docker_argv = [
            "docker", "run", "--rm", "--network", "host",
            "-e", args.gateway_api_key_env,
            "-e", "PI_CODING_AGENT_DIR=/qualification", "-v",
            f"{temp}:/qualification:ro", args.image, "sh", "-c",
            f'exec pi -p "$1" --mode json --no-session --no-tools '
            f'--model skgw/{args.gateway_model} '
            f'--api-key "${args.gateway_api_key_env}"',
            "qualify-arena", args.prompt,
        ]
        # Pi's JSON stream may exceed the evidence truncation limit. Parse the
        # complete stream, then retain only the structured terminal evidence.
        pi = command(docker_argv, truncate=False)
    events = []
    for line in pi["stdout"].splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    assistant = [event["message"] for event in events
                 if event.get("type") == "message_end"
                 and event.get("message", {}).get("role") == "assistant"]
    message = assistant[-1] if assistant else {}
    output = "".join(part.get("text", "") for part in message.get("content", [])
                     if isinstance(part, dict))
    response_model = message.get("responseModel")
    if response_model is None:
        response_model = next((event.get("responseModel") for event in reversed(events)
                               if event.get("responseModel")), None)
    return {
        "gateway": gateway,
        "health": health,
        "models": models,
        "generated_models_json": config,
        "pi_request_headers": attribution,
        "attribution": observed_headers,
        "attribution_source": "direct_openai_compatible_probe",
        "completion_status": completion["status"],
        "completion_model": completion["body"].get("model")
        if isinstance(completion["body"], dict) else None,
        "pi": {"exit_code": pi["exit_code"], "stderr": pi["stderr"],
               "responseModel": response_model, "output": output},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image", default="skharness-pi-core:card-84710bd5")
    parser.add_argument("--live-gateway", help="opt in to live SKGateway qualification")
    parser.add_argument("--gateway-api-key-env", default="SKGATEWAY_API_KEY")
    parser.add_argument("--gateway-model", default="reference")
    parser.add_argument("--session-id", default="arena-qualification")
    parser.add_argument("--card-id", default="qualification")
    parser.add_argument("--prompt", default="Return exactly: qualified")
    parser.add_argument("--context-limit", type=int, default=4096)
    parser.add_argument("--output-limit", type=int, default=256)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    evidence_files = [
        root / "tests/data/arena-reference-challenge-v1.json",
        root / "docker/sandbox/pi/Dockerfile",
        root / "docker/sandbox/pi/dependencies.lock.json",
        root / "docker/sandbox/pi/apt-packages.lock",
        root / "docker/sandbox/pi/requirements.lock",
        root / "docker/sandbox/pi/test-requirements.lock",
        root / "docker/sandbox/pi/sk-bridge.ts",
    ]
    checks = [
        command(["python", "-m", "pytest", "tests/test_arena_qualification.py",
                 "tests/test_arena_sk_backend.py", "tests/test_pi_mock_gateway_it.py",
                 "tests/test_arena_verifier.py", "tests/test_arena_controller.py",
                 "tests/test_arena_scheduler.py", "-q"]),
        command(["docker", "image", "inspect", args.image, "--format", "{{json .}}"]),
        command(["docker", "run", "--rm", args.image, "pi", "--version"]),
        command(["docker", "run", "--rm", args.image, "pip", "freeze"]),
        command(["docker", "run", "--rm", args.image, "dpkg-query", "-W"]),
    ]
    bundle = {
        "schema": "skharness.arena.qualification.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "host": {"node": platform.node(), "platform": platform.platform()},
        "claims": {"signed": False, "sbom": False, "vulnerability_scan": False,
                   "live_gateway": False, "fleet_node": False},
        "files": {str(path.relative_to(root)): digest(path) for path in evidence_files},
        "checks": checks,
    }
    if args.live_gateway:
        try:
            bundle["live_gateway"] = live_qualification(args)
            live = bundle["live_gateway"]
            bundle["claims"]["live_gateway"] = (
                live["health"]["status"] == 200
                and live["models"]["status"] == 200
                and live["completion_status"] == 200
                and live["pi"]["exit_code"] == 0
                and bool(live["pi"]["responseModel"])
                and bool(live["pi"]["output"])
                and bool(live["attribution"]["x-sk-backend"])
                and bool(live["attribution"]["x-sk-model-served"])
                and bool(live["attribution"]["x-sk-req-id"]
                         or live["attribution"]["x-request-id"])
            )
        except Exception as exc:  # record a failed optional qualification truthfully
            bundle["live_gateway"] = {"error": f"{type(exc).__name__}: {exc}"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n")
    print(args.output)
    passed = all(check["exit_code"] == 0 for check in checks)
    if args.live_gateway:
        passed = passed and bundle["claims"]["live_gateway"]
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
