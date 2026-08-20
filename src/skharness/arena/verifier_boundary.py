"""Production-representative container boundary for isolated verification.

This module constructs two mutually asymmetric Docker sandboxes.  The worker sees
only its writable submission directory.  The verifier alone receives read-only
hidden/rubric/control material and a dedicated writable verdict directory.  It is a
local boundary proof, not evidence that any fleet node deployed these settings.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class IsolatedVerifierBoundary:
    """Run worker and verifier probes with disjoint least-authority mount sets."""

    worker_dir: Path
    verifier_dir: Path
    verdict_dir: Path
    image: str = "python:3.12-slim"

    def _base(self) -> list[str]:
        return [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "64",
            "--user",
            "65534:65534",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=16m",
        ]

    def worker_probe(self, forbidden_host_paths: tuple[Path, ...]) -> dict:
        """Attempt worker reads/writes against every verifier-owned surface."""
        probe = """
import json, os
paths = json.loads(os.environ['FORBIDDEN'])
out = {}
for path in paths + ['/verifier', '/verdict', '/var/run/docker.sock']:
    key = path
    out[key] = {'visible': os.path.exists(path), 'read': False, 'write': False}
    try:
        open(path, 'rb').read(1); out[key]['read'] = True
    except OSError: pass
    try:
        open(path, 'ab').write(b'x'); out[key]['write'] = True
    except OSError: pass
print(json.dumps(out, sort_keys=True))
"""
        command = self._base() + [
            "-e",
            "FORBIDDEN=" + json.dumps([str(path) for path in forbidden_host_paths]),
            "--mount",
            f"type=bind,src={self.worker_dir},dst=/work",
            "--workdir",
            "/work",
            self.image,
            "python",
            "-c",
            probe,
        ]
        return self._json_run(command)

    def verifier_control(self) -> dict:
        """Read private controls and emit a verdict in verifier-only storage."""
        probe = """
import hashlib, json, pathlib
root = pathlib.Path('/verifier')
expected = (root / 'hidden.expected').read_bytes()
candidate = pathlib.Path('/work/candidate').read_bytes()
controls = json.loads((root / 'controls.json').read_text())
passed = candidate == expected and controls == {'gold': True, 'no_op': False}
verdict = {'passed': passed, 'rubric_hash': hashlib.sha256((root/'rubric').read_bytes()).hexdigest()}
verdict['verifier_hash'] = hashlib.sha256((root/'verifier.py').read_bytes()).hexdigest()
pathlib.Path('/verdict/verdict.json').write_text(json.dumps(verdict, sort_keys=True))
print(json.dumps(verdict, sort_keys=True))
"""
        command = self._base() + [
            "--mount",
            f"type=bind,src={self.worker_dir},dst=/work,readonly",
            "--mount",
            f"type=bind,src={self.verifier_dir},dst=/verifier,readonly",
            "--mount",
            f"type=bind,src={self.verdict_dir},dst=/verdict",
            self.image,
            "python",
            "-c",
            probe,
        ]
        return self._json_run(command)

    @staticmethod
    def available() -> bool:
        """Return whether the local Docker boundary can execute."""
        return shutil.which("docker") is not None

    @staticmethod
    def _json_run(command: list[str]) -> dict:
        completed = subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
        return json.loads(completed.stdout)
