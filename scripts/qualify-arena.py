#!/usr/bin/env python3
"""Generate a local, unsigned Evolution Arena qualification evidence bundle."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import platform
import subprocess
import sys
import tempfile
import threading
from copy import deepcopy
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import parse, request


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def command(argv: list[str], *, truncate: bool = True) -> dict:
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    stdout = proc.stdout[-4000:] if truncate else proc.stdout
    stderr = proc.stderr[-4000:] if truncate else proc.stderr
    return {"argv": argv, "exit_code": proc.returncode, "stdout": stdout, "stderr": stderr}


def http_json(
    url: str, api_key: str, *, payload: dict | None = None, headers: dict[str, str] | None = None
) -> dict:
    body = None if payload is None else json.dumps(payload).encode()
    req_headers = {"accept": "application/json", **(headers or {})}
    if api_key:
        req_headers["authorization"] = f"Bearer {api_key}"
    if body is not None:
        req_headers["content-type"] = "application/json"
    req = request.Request(
        url, data=body, headers=req_headers, method="POST" if body is not None else "GET"
    )
    with request.urlopen(req, timeout=20) as response:  # noqa: S310 - operator URL
        raw = response.read().decode()
        return {
            "status": response.status,
            "headers": {key.lower(): value for key, value in response.headers.items()},
            "body": json.loads(raw) if raw else None,
        }


_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
_ATTRIBUTION_HEADERS = ("x-sk-req-id", "x-request-id", "x-sk-backend", "x-sk-model-served")


class PiGatewayRelay:
    """Loopback-only, transparent observer for Pi's gateway execution."""

    def __init__(self, upstream: str):
        parsed = parse.urlsplit(upstream.rstrip("/"))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("live gateway must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("live gateway URL must not contain credentials, query, or fragment")
        self._upstream = parsed
        self.captures: list[dict] = []
        self._lock = threading.Lock()
        relay = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
                length = self.headers.get("content-length")
                if length is None:
                    self.send_error(411, "content-length required")
                    return
                body = self.rfile.read(int(length))
                headers = {
                    key: value
                    for key, value in self.headers.items()
                    if key.lower() not in _HOP_BY_HOP | {"host", "content-length"}
                }
                path = relay._upstream.path.rstrip("/") + self.path
                connection_type = (
                    http.client.HTTPSConnection
                    if relay._upstream.scheme == "https"
                    else http.client.HTTPConnection
                )
                connection = connection_type(
                    relay._upstream.hostname, relay._upstream.port, timeout=20
                )
                captured = bytearray()
                try:
                    connection.request("POST", path, body=body, headers=headers)
                    upstream_response = connection.getresponse()
                    response_headers = {
                        key.lower(): value for key, value in upstream_response.getheaders()
                    }
                    self.send_response(upstream_response.status)
                    for key, value in upstream_response.getheaders():
                        if key.lower() not in _HOP_BY_HOP | {"content-length"}:
                            self.send_header(key, value)
                    self.send_header("connection", "close")
                    self.end_headers()
                    while True:
                        chunk = upstream_response.read1(16 * 1024)
                        if not chunk:
                            break
                        captured.extend(chunk)
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    with relay._lock:
                        relay.captures.append(
                            {
                                "method": "POST",
                                "path": self.path,
                                "status": upstream_response.status,
                                "headers": {
                                    name: response_headers.get(name)
                                    for name in _ATTRIBUTION_HEADERS
                                },
                                "body": bytes(captured),
                            }
                        )
                finally:
                    connection.close()
                    self.close_connection = True

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def api_base(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}/v1"

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()


def relay_response_model(capture: dict) -> str | None:
    """Extract model metadata from JSON or OpenAI-compatible SSE without guessing."""
    raw = capture["body"].decode("utf-8", errors="replace")
    candidates = [raw]
    candidates.extend(
        line[5:].strip()
        for line in raw.splitlines()
        if line.startswith("data:") and line[5:].strip() != "[DONE]"
    )
    for candidate in reversed(candidates):
        try:
            body = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(body, dict) and isinstance(body.get("model"), str):
            return body["model"]
    return None


def immutable_execution_records(args: argparse.Namespace, evidence: dict) -> list[dict]:
    """Materialize the two real executions without upgrading their trust state.

    The direct OpenAI-compatible probe and the Pi invocation are separate gateway
    executions.  Qualification preserves each as a frozen Experiment/Result pair;
    independent verification remains a later gate and therefore these Results are
    deliberately UNVERIFIED here.
    """
    from skharness.arena.models import (
        ArtifactRef,
        BudgetSpec,
        Experiment,
        Measurement,
        Observation,
        Result,
    )

    created_at = datetime.now(timezone.utc)
    challenge_hash = "sha256:" + hashlib.sha256(args.prompt.encode()).hexdigest()
    direct_body = evidence["completion_body"]
    direct_output = ""
    if isinstance(direct_body, dict):
        choices = direct_body.get("choices", [])
        if choices and isinstance(choices[0], dict):
            direct_output = choices[0].get("message", {}).get("content", "")
    executions = (
        (
            "direct",
            direct_output,
            evidence["completion_model"],
            evidence["attribution"].get("x-sk-req-id")
            or evidence["attribution"].get("x-request-id"),
            evidence["attribution"],
        ),
        (
            "pi",
            evidence["pi"]["output"],
            evidence["pi"]["responseModel"] or evidence["pi"]["served_model"],
            evidence["pi"]["attribution"].get("x-sk-req-id")
            or evidence["pi"]["attribution"].get("x-request-id"),
            evidence["pi"]["attribution"],
        ),
    )
    records = []
    for kind, output, served_model, request_id, execution_attribution in executions:
        raw = str(output).encode()
        artifact = ArtifactRef(
            digest="sha256:" + hashlib.sha256(raw).hexdigest(),
            media_type="text/plain",
            size=len(raw),
            role="assistant-output",
        )
        experiment = Experiment(
            id=f"{args.session_id}:{kind}",
            challenge_hash=challenge_hash,
            actor="service:arena-qualification",
            harness=kind,
            card_id=args.card_id,
            run_id=f"{args.session_id}:{kind}",
            repository_url="local://qualification-worktree",
            repository_base_sha="qualification-worktree-unresolved",
            image_digest=args.image if kind == "pi" else "gateway-managed",
            sbom_digest="not-observed",
            requested_route="skgateway/openai-compatible",
            requested_model=args.gateway_model,
            served_model=served_model,
            gateway_request_id=request_id,
            gateway_backend_id=execution_attribution.get("x-sk-backend"),
            configuration={
                "prompt_sha256": challenge_hash,
                "live": True,
                "gateway_header_served_model": execution_attribution.get("x-sk-model-served"),
            },
            budgets=BudgetSpec(wall_seconds=20),
            created_at=created_at,
            artifacts=(artifact,),
        )
        observation = Observation(value=float(bool(output)), recorded_at=created_at)
        result = Result(
            experiment_id=experiment.id,
            experiment_hash=experiment.content_hash,
            challenge_hash=challenge_hash,
            measurements=(
                Measurement(
                    metric="nonempty_output",
                    unit="boolean",
                    observations=(observation,),
                    mean=observation.value,
                    standard_deviation=0,
                ),
            ),
            artifacts=(artifact,),
            created_at=created_at,
        )
        records.append(
            {
                "experiment": experiment.model_dump(mode="json"),
                "experiment_hash": experiment.content_hash,
                "result": result.model_dump(mode="json"),
                "result_hash": result.content_hash,
                "assistant_output": str(output),
            }
        )
    return records


def planted_false_high_score(record: dict) -> dict:
    """Create an explicit adversarial fixture whose claimed score must be ignored."""
    from skharness.arena.models import (
        ArtifactRef,
        Experiment,
        Measurement,
        Observation,
        Result,
    )

    planted = deepcopy(record)
    raw = b"planted-false-output"
    artifact = ArtifactRef(
        digest="sha256:" + hashlib.sha256(raw).hexdigest(),
        media_type="text/plain",
        size=len(raw),
        role="assistant-output",
    )
    original_experiment = Experiment.model_validate(planted["experiment"])
    false_experiment = original_experiment.model_copy(update={"artifacts": (artifact,)})
    original = Result.model_validate(planted["result"])
    observation = Observation(value=999_999_999, recorded_at=original.created_at)
    false_result = original.model_copy(
        update={
            "measurements": (
                Measurement(
                    metric="claimed_score",
                    unit="ratio",
                    observations=(observation,),
                    mean=observation.value,
                    standard_deviation=0,
                ),
            ),
            "artifacts": (artifact,),
            "experiment_hash": false_experiment.content_hash,
        }
    )
    planted["experiment"] = false_experiment.model_dump(mode="json")
    planted["experiment_hash"] = false_experiment.content_hash
    planted["assistant_output"] = raw.decode()
    planted["result"] = false_result.model_dump(mode="json")
    planted["result_hash"] = false_result.content_hash
    return planted


def independent_workers_valid(workers: list[dict], required: int) -> bool:
    """Require distinct, attributed, independently verified Pi executions."""
    if len(workers) != required:
        return False
    request_ids = {worker.get("request_id") for worker in workers}
    experiment_hashes = {worker.get("record", {}).get("experiment_hash") for worker in workers}
    if None in request_ids or len(request_ids) != required:
        return False
    if None in experiment_hashes or len(experiment_hashes) != required:
        return False
    return all(
        worker.get("backend")
        and worker.get("served_model")
        and worker.get("verification", {}).get("status") == "valid"
        and worker.get("verification", {}).get("admitted") is True
        for worker in workers
    )


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
        api_base + "/chat/completions",
        api_key,
        payload={
            "model": args.gateway_model,
            "stream": False,
            "messages": [{"role": "user", "content": args.prompt}],
        },
        headers=attribution,
    )
    response_headers = completion["headers"]
    observed_headers = {
        name: response_headers.get(name)
        for name in ("x-sk-req-id", "x-request-id", "x-sk-backend", "x-sk-model-served")
    }
    config = {
        "providers": {
            "skgw": {
                "baseUrl": "ephemeral-loopback-relay",
                "api": "openai-completions",
                "apiKey": "not-recorded",
                "headers": attribution,
                "compat": {"supportsDeveloperRole": False},
                "models": [
                    {
                        "id": args.gateway_model,
                        "limit": {"context": args.context_limit, "output": args.output_limit},
                    }
                ],
            }
        }
    }
    with (
        PiGatewayRelay(root) as relay,
        tempfile.TemporaryDirectory(prefix="skharness-arena-") as temp,
    ):
        # tempfile directories are 0700 by default, but the worker runs as UID
        # 10001 and must be able to traverse the read-only bind mount.
        Path(temp).chmod(0o755)
        config_path = Path(temp) / "models.json"
        runtime_config = json.loads(json.dumps(config))
        runtime_config["providers"]["skgw"]["baseUrl"] = relay.api_base
        runtime_config["providers"]["skgw"]["apiKey"] = api_key
        config_path.write_text(json.dumps(runtime_config))
        config_path.chmod(0o444)
        docker_argv = [
            "docker",
            "run",
            "--rm",
            "--network",
            "host",
            "-e",
            args.gateway_api_key_env,
            "-e",
            "PI_CODING_AGENT_DIR=/qualification",
            "-v",
            f"{temp}:/qualification:ro",
            args.image,
            "sh",
            "-c",
            f'exec pi -p "$1" --mode json --no-session --no-tools '
            f"--model skgw/{args.gateway_model} "
            f'--api-key "${args.gateway_api_key_env}"',
            "qualify-arena",
            args.prompt,
        ]
        # Pi's JSON stream may exceed the evidence truncation limit. Parse the
        # complete stream, then retain only the structured terminal evidence.
        pi = command(docker_argv, truncate=False)
    pi_captures = [
        capture
        for capture in relay.captures
        if capture["path"].split("?", 1)[0] == "/v1/chat/completions"
    ]
    if len(pi_captures) != 1:
        raise RuntimeError(f"expected exactly one observed Pi completion, got {len(pi_captures)}")
    pi_capture = pi_captures[0]
    events = []
    for line in pi["stdout"].splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    assistant = [
        event["message"]
        for event in events
        if event.get("type") == "message_end"
        and event.get("message", {}).get("role") == "assistant"
    ]
    message = assistant[-1] if assistant else {}
    output = "".join(
        part.get("text", "") for part in message.get("content", []) if isinstance(part, dict)
    )
    response_model = message.get("responseModel")
    if response_model is None:
        response_model = next(
            (
                event.get("responseModel")
                for event in reversed(events)
                if event.get("responseModel")
            ),
            None,
        )
    pi_attribution = pi_capture["headers"]
    pi_served_model = pi_attribution.get("x-sk-model-served") or relay_response_model(pi_capture)
    return {
        "gateway": gateway,
        "health": health,
        "models": models,
        "generated_models_json": config,
        "pi_request_headers": attribution,
        "attribution": observed_headers,
        "attribution_source": "direct_openai_compatible_probe",
        "pi_relay": {
            "transport": "ephemeral_loopback_http_forwarder",
            "upstream": gateway,
            "captured_requests": 1,
            "preserves_streaming": True,
        },
        "completion_status": completion["status"],
        "completion_body": completion["body"],
        "completion_model": completion["body"].get("model")
        if isinstance(completion["body"], dict)
        else None,
        "pi": {
            "exit_code": pi["exit_code"],
            "stderr": pi["stderr"],
            # Preserve the provider event envelope as evidence. Pi stdout does
            # not contain generated models.json credentials or request headers.
            "raw_event_stream": pi["stdout"],
            "responseModel": response_model,
            "served_model": pi_served_model,
            "attribution": pi_attribution,
            "attribution_source": "ephemeral_loopback_relay_observation",
            "completion_status": pi_capture["status"],
            "output": output,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--arena-store", type=Path)
    parser.add_argument("--image", default="skharness-pi-core:card-84710bd5")
    parser.add_argument("--live-gateway", help="opt in to live SKGateway qualification")
    parser.add_argument("--gateway-api-key-env", default="SKGATEWAY_API_KEY")
    parser.add_argument("--gateway-model", default="reference")
    parser.add_argument("--session-id", default="arena-qualification")
    parser.add_argument(
        "--pi-workers",
        type=int,
        default=2,
        help="independent Dockerized Pi executions required for the live gate",
    )
    parser.add_argument("--card-id", default="qualification")
    parser.add_argument("--prompt", default="Return exactly: qualified")
    parser.add_argument(
        "--expected-output",
        default="qualified",
        help="frozen verifier-owned exact output (not a semantic rubric)",
    )
    parser.add_argument("--context-limit", type=int, default=4096)
    parser.add_argument("--output-limit", type=int, default=256)
    args = parser.parse_args()
    if args.pi_workers < 2:
        parser.error("--pi-workers must be at least 2 for Arena fleet qualification")
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
        command(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/test_arena_qualification.py",
                "tests/test_arena_sk_backend.py",
                "tests/test_arena_verifier.py",
                "tests/test_arena_controller.py",
                "tests/test_arena_scheduler.py",
                "-q",
            ]
        ),
        command(["docker", "image", "inspect", args.image, "--format", "{{json .}}"]),
        command(["docker", "run", "--rm", args.image, "pi", "--version"]),
        command(
            [
                "docker",
                "run",
                "--rm",
                args.image,
                "python",
                "-c",
                "import importlib.metadata as m,json; "
                "print(json.dumps(sorted((d.metadata['Name'],d.version) for d in m.distributions())))",
            ]
        ),
        command(
            [
                "docker",
                "run",
                "--rm",
                args.image,
                "sh",
                "-c",
                "if command -v apk >/dev/null; then apk info -vv; "
                "elif command -v dpkg-query >/dev/null; then dpkg-query -W; "
                "else echo 'no supported package inventory tool' >&2; exit 1; fi",
            ]
        ),
    ]
    bundle = {
        "schema": "skharness.arena.qualification.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "host": {"node": platform.node(), "platform": platform.platform()},
        "claims": {
            "signed": False,
            "sbom": False,
            "vulnerability_scan": False,
            "live_gateway": False,
            "fleet_node": False,
        },
        "files": {str(path.relative_to(root)): digest(path) for path in evidence_files},
        "checks": checks,
    }
    if args.live_gateway:
        try:
            bundle["live_gateway"] = live_qualification(args)
            live = bundle["live_gateway"]
            live["records"] = immutable_execution_records(args, live)
            from skharness.arena.qualification import qualify_execution_records
            from skharness.arena.store import ArenaStore

            store_root = args.arena_store or args.output.with_suffix(".arena-store")
            live["verification"] = qualify_execution_records(
                live["records"],
                ArenaStore(store_root / "candidate"),
                expected_output=args.expected_output,
            )
            live["independent_workers"] = [
                {
                    "session_id": args.session_id,
                    "record": live["records"][1],
                    "verification": live["verification"],
                    "request_id": live["pi"]["attribution"].get("x-sk-req-id")
                    or live["pi"]["attribution"].get("x-request-id"),
                    "backend": live["pi"]["attribution"].get("x-sk-backend"),
                    "served_model": live["pi"]["served_model"],
                }
            ]
            for worker_index in range(1, args.pi_workers):
                worker_args = deepcopy(args)
                worker_args.session_id = f"{args.session_id}:worker-{worker_index + 1}"
                worker_live = live_qualification(worker_args)
                worker_records = immutable_execution_records(worker_args, worker_live)
                worker_verification = qualify_execution_records(
                    worker_records,
                    ArenaStore(store_root / f"candidate-worker-{worker_index + 1}"),
                    expected_output=args.expected_output,
                )
                live["independent_workers"].append(
                    {
                        "session_id": worker_args.session_id,
                        "record": worker_records[1],
                        "verification": worker_verification,
                        "request_id": worker_live["pi"]["attribution"].get("x-sk-req-id")
                        or worker_live["pi"]["attribution"].get("x-request-id"),
                        "backend": worker_live["pi"]["attribution"].get("x-sk-backend"),
                        "served_model": worker_live["pi"]["served_model"],
                        "output": worker_live["pi"]["output"],
                    }
                )
            live["planted_false_high_score"] = qualify_execution_records(
                [planted_false_high_score(live["records"][1])],
                ArenaStore(store_root / "adversarial-control"),
                expected_output=args.expected_output,
            )
            bundle["claims"]["live_gateway"] = (
                live["health"]["status"] == 200
                and live["models"]["status"] == 200
                and live["completion_status"] == 200
                and live["pi"]["exit_code"] == 0
                # Pi does not emit responseModel for every OpenAI-compatible
                # response dialect. The relay-observed, gateway-authenticated
                # served-model header is the canonical fallback provenance.
                and bool(live["pi"]["responseModel"] or live["pi"]["served_model"])
                and live["pi"]["completion_status"] == 200
                and bool(live["pi"]["served_model"])
                and bool(live["pi"]["output"])
                and live["verification"]["status"] == "valid"
                and live["verification"]["admitted"]
                and live["planted_false_high_score"]["status"] == "invalid"
                and not live["planted_false_high_score"]["admitted"]
                and bool(live["attribution"]["x-sk-backend"])
                and bool(live["attribution"]["x-sk-model-served"])
                and bool(live["attribution"]["x-sk-req-id"] or live["attribution"]["x-request-id"])
                and bool(live["pi"]["attribution"]["x-sk-backend"])
                and bool(
                    live["pi"]["attribution"]["x-sk-req-id"]
                    or live["pi"]["attribution"]["x-request-id"]
                )
                and independent_workers_valid(live["independent_workers"], args.pi_workers)
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
