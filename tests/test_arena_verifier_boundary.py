"""Hermetic local proof of the worker/verifier container trust boundary."""

from __future__ import annotations

import hashlib
import json
import subprocess

import pytest

from skharness.arena.verifier_boundary import IsolatedVerifierBoundary


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_worker_cannot_read_or_modify_verifier_material_or_verdict(tmp_path):
    if not IsolatedVerifierBoundary.available():
        pytest.skip("Docker is required for the local isolation proof")
    image = "python:3.12-slim"
    if subprocess.run(
        ["docker", "image", "inspect", image], capture_output=True, check=False
    ).returncode:
        pytest.skip(f"local isolation image is absent: {image}")

    worker = tmp_path / "worker"
    private = tmp_path / "verifier-private"
    verdict = tmp_path / "verdict"
    for directory in (worker, private, verdict):
        directory.mkdir()
    worker.chmod(0o777)
    verdict.chmod(0o777)
    (worker / "candidate").write_text("qualified")
    protected = {
        "hidden": private / "hidden.expected",
        "rubric": private / "rubric",
        "controls": private / "controls.json",
        "verifier": private / "verifier.py",
    }
    protected["hidden"].write_text("qualified")
    protected["rubric"].write_text("byte-exact-v1")
    protected["controls"].write_text(json.dumps({"gold": True, "no_op": False}))
    protected["verifier"].write_text("# pinned verifier implementation v1\n")
    before = {name: _digest(path) for name, path in protected.items()}
    boundary = IsolatedVerifierBoundary(worker, private, verdict, image)

    attack = boundary.worker_probe(tuple(protected.values()))
    assert all(
        not outcome[permission]
        for outcome in attack.values()
        for permission in ("visible", "read", "write")
    )
    observed = boundary.verifier_control()
    assert observed["passed"] is True
    verdict_path = verdict / "verdict.json"
    verdict_hash = _digest(verdict_path)

    second_attack = boundary.worker_probe((*protected.values(), verdict_path))
    assert all(
        not outcome[permission]
        for outcome in second_attack.values()
        for permission in ("visible", "read", "write")
    )
    assert {name: _digest(path) for name, path in protected.items()} == before
    assert _digest(verdict_path) == verdict_hash
