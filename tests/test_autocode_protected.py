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


# ---------------------------------------------------------------------------
# The work-grade policy is a guardrail, not a feature. Once a grade selects the
# model, the rubric decides the capability floor and which trust zone sees the
# card's data, so an engine that can rewrite it can re-route itself. These
# assert the hard-coded floor holds even under a valid but incomplete manifest,
# which is the realistic case: nobody removes the entry, someone just never
# adds it.
# ---------------------------------------------------------------------------

_GRADE_POLICY_PATHS = (
    "src/skharness/autocode/grading.py",
    "src/skharness/autocode/sensitivity.py",
    "src/skharness/autocode/buckets.py",
    "src/skharness/autocode/data/joule-grade-vocabulary.json",
    "tests/data/joule-economy-golden-set-v1.json",
)


def test_grade_policy_protected_even_if_manifest_omits_it(tmp_path):
    # A signed, valid manifest that simply never mentions the rubric.
    _write_manifest(tmp_path, {"protected": ["*/some/unrelated/path.py"]})
    m = load_manifest(tmp_path)
    for path in _GRADE_POLICY_PATHS:
        assert is_protected([path], m) is True, path


def test_grade_policy_paths_are_on_the_hardcoded_floor():
    # NEGATIVE CONTROL: this is the assertion that fails if someone removes an
    # entry from _ALWAYS_PROTECTED. Without it the test above would still pass
    # via a future manifest and the floor could be quietly emptied.
    joined = "\n".join(_ALWAYS_PROTECTED)
    for needle in ("grading.py", "sensitivity.py", "buckets.py",
                   "joule-grade-vocabulary.json", "joule-economy-golden-set-"):
        assert needle in joined, needle


def test_a_golden_set_v2_is_protected_too():
    # The glob must cover future versions, or promoting the set to v2 silently
    # drops it off the floor at exactly the moment it starts being ground truth.
    assert is_protected(["tests/data/joule-economy-golden-set-v2.json"],
                        _FAIL_CLOSED) is True


def test_ordinary_autocode_files_are_still_editable(tmp_path):
    # NEGATIVE CONTROL the other way: if the new globs are too broad they would
    # freeze the whole autocode package and every card would escalate.
    _write_manifest(tmp_path, {"protected": ["*/some/unrelated/path.py"]})
    m = load_manifest(tmp_path)
    assert is_protected(["src/skharness/autocode/orchestrator.py"], m) is False
    assert is_protected(["src/skharness/autocode/adapters/pi.py"], m) is False
