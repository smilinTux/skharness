"""Carve-out detector tests: the leash cannot be loosened autonomously."""
from __future__ import annotations

import json

from skharness.autocode.protected import (
    _ALWAYS_PROTECTED,
    _FAIL_CLOSED,
    is_protected,
    load_manifest,
)


def _write_manifest(root, data):
    obj = root / "objects"
    obj.mkdir(parents=True, exist_ok=True)
    (obj / "_protected.json").write_text(json.dumps(data))


def test_protected_hit_on_manifest_glob(tmp_path):
    _write_manifest(tmp_path, {"protected": ["*/skcapstone/fleet/signing.py"]})
    m = load_manifest(tmp_path)
    assert is_protected(["src/skcapstone/fleet/signing.py"], m) is True


def test_clean_miss(tmp_path):
    _write_manifest(tmp_path, {"protected": ["*/skcapstone/fleet/signing.py"]})
    m = load_manifest(tmp_path)
    assert is_protected(["src/skcapstone/fleet/cron.py"], m) is False


def test_missing_manifest_fails_closed(tmp_path):
    # No manifest at all -> protect everything.
    m = load_manifest(tmp_path)
    assert m == _FAIL_CLOSED
    assert is_protected(["any/random/file.py"], m) is True


def test_unreadable_manifest_fails_closed(tmp_path):
    obj = tmp_path / "objects"
    obj.mkdir(parents=True)
    (obj / "_protected.json").write_text("{ not json")
    m = load_manifest(tmp_path)
    assert is_protected(["any/file.py"], m) is True


def test_empty_protected_list_fails_closed(tmp_path):
    _write_manifest(tmp_path, {"protected": []})
    m = load_manifest(tmp_path)
    assert is_protected(["any/file.py"], m) is True


def test_failed_verification_fails_closed(tmp_path):
    _write_manifest(tmp_path, {"protected": ["*/x.py"], "sig": "bad"})
    m = load_manifest(tmp_path, verify=lambda d: False)
    assert is_protected(["src/skcapstone/fleet/cron.py"], m) is True


def test_passing_verification_keeps_manifest(tmp_path):
    _write_manifest(tmp_path, {"protected": ["*/x.py"], "sig": "ok"})
    m = load_manifest(tmp_path, verify=lambda d: True)
    assert m.get("_fail_closed") is None
    assert is_protected(["src/skcapstone/fleet/cron.py"], m) is False


def test_detector_self_protected_even_if_manifest_omits_it(tmp_path):
    # A valid manifest that lists only an unrelated file must NOT leave the
    # detector or the other core guardrails unprotected.
    _write_manifest(tmp_path, {"protected": ["*/unrelated.py"]})
    m = load_manifest(tmp_path)
    assert is_protected(["src/skharness/autocode/protected.py"], m) is True
    assert is_protected(["src/skharness/autocode/engineering.py"], m) is True
    assert is_protected(["src/skcapstone/itil.py"], m) is True
    assert is_protected(["objects/_freeze.json"], m) is True


def test_always_protected_covers_the_guardrail_core():
    assert any("protected.py" in g for g in _ALWAYS_PROTECTED)
    assert any("engineering.py" in g for g in _ALWAYS_PROTECTED)
    assert any("_freeze.json" in g for g in _ALWAYS_PROTECTED)


def test_changed_paths_gate_core_protected_without_manifest(tmp_path):
    from skharness.autocode.protected import changed_paths_are_protected
    # No manifest deployed: core guardrails still protected, normal work is not.
    assert changed_paths_are_protected(tmp_path, ["src/skcapstone/itil.py"]) is True
    assert changed_paths_are_protected(tmp_path, ["src/skcapstone/fleet/cron.py"]) is False


def test_changed_paths_gate_manifest_adds_extra(tmp_path):
    from skharness.autocode.protected import changed_paths_are_protected
    _write_manifest(tmp_path, {"protected": ["*/skcapstone/fleet/scheduler.py"]})
    assert changed_paths_are_protected(tmp_path, ["src/skcapstone/fleet/scheduler.py"]) is True
    assert changed_paths_are_protected(tmp_path, ["src/skcapstone/fleet/cron.py"]) is False
    # core still protected with a manifest present
    assert changed_paths_are_protected(tmp_path, ["src/skcapstone/itil.py"]) is True
