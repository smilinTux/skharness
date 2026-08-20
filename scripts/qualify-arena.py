#!/usr/bin/env python3
"""Generate a local, unsigned Evolution Arena qualification evidence bundle."""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def command(argv: list[str]) -> dict:
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    return {"argv": argv, "exit_code": proc.returncode,
            "stdout": proc.stdout[-4000:], "stderr": proc.stderr[-4000:]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image", default="skharness-pi-core:card-84710bd5")
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
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n")
    print(args.output)
    return 0 if all(check["exit_code"] == 0 for check in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
