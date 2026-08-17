"""Carve-out detector tests: the leash cannot be loosened autonomously."""
from __future__ import annotations

import hashlib
import json

import pytest

from skharness.autocode.protected import (
    _ALWAYS_PROTECTED,
    _FAIL_CLOSED,
    changed_paths_are_protected,
    is_protected,
    load_manifest,
    matched_protected_paths,
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


# -- S21: the coverage instrument's own configuration -------------------------

def test_coverage_configuration_is_on_the_floor(tmp_path):
    """S21 (card 53b8c8be): the twin gate's third arm measures the diff with
    `pytest --cov`. The files below decide WHAT gets measured, so a diff that
    adds an `omit` rule blinds the instrument that grades it without touching
    CI. That is the same self-modification hazard as rewriting the rubric, so
    the coverage configuration sits on the hard-coded floor rather than on a
    manifest entry nobody would remember to add.
    """
    from skharness.autocode.protected import changed_paths_are_protected
    for path in (".coveragerc",
                 "pyproject.toml",
                 "pytest.ini",
                 "setup.cfg",
                 "tox.ini",
                 "conftest.py",
                 "tests/conftest.py",
                 "src/pkg/conftest.py"):
        assert changed_paths_are_protected(tmp_path, [path]) is True, path


def test_the_coverage_config_floor_does_not_swallow_ordinary_work(tmp_path):
    """Negative control for the entry above: the globs must not match every
    ordinary file, or the carve-out signal is worthless."""
    from skharness.autocode.protected import changed_paths_are_protected
    for path in ("src/pkg/module.py", "tests/test_module.py", "README.md",
                 "docs/design.md", "src/pkg/config.toml"):
        assert changed_paths_are_protected(tmp_path, [path]) is False, path


# ---------------------------------------------------------------------------
# Card P6 (coord `08963fbb`): the manifest's signature verification is now
# wired into the production call path (`_manifest_for`, reached by
# `matched_protected_paths` / `changed_paths_are_protected`, which is what
# `engineering.py` finalize actually calls). Gated by the same
# `SKFLEET_SIGNING` rollout flag Card 3.5 uses for writes; `off` (unset, the
# default) is asserted to be an EXACT no-op against every test above -- none
# of them set the env var, and they all still pass, which is the load-bearing
# fact that this section pins explicitly rather than leaving implicit.
# ---------------------------------------------------------------------------


def _fake_signer(data: bytes) -> str:
    return "sig:" + hashlib.sha256(data).hexdigest()


def _fake_verifier(data: bytes, sig: str) -> bool:
    return sig == "sig:" + hashlib.sha256(data).hexdigest()


def _write_signed_manifest(root, extra_protected, fleet_signing):
    obj = root / "objects"
    obj.mkdir(parents=True, exist_ok=True)
    payload = {
        "protected": extra_protected,
        "writer": {"identity": "capauth:chef@skworld.io", "role": "operator",
                   "signature": None},
    }
    payload["writer"]["signature"] = _fake_signer(fleet_signing.canonical_bytes(payload))
    (obj / "_protected.json").write_text(json.dumps(payload))
    return payload


@pytest.mark.needs_skcapstone
def test_wiring_off_by_default_matches_pre_p6_behavior(tmp_path, monkeypatch):
    from skcapstone.fleet import signing as fleet_signing
    monkeypatch.delenv(fleet_signing.SIGNING_ENV, raising=False)
    # An UNSIGNED manifest (no writer block at all) is still honored, exactly
    # as before this card: off means unchanged.
    _write_manifest(tmp_path, {"protected": ["*/skcapstone/fleet/scheduler.py"]})
    assert changed_paths_are_protected(tmp_path, ["src/skcapstone/fleet/scheduler.py"]) is True
    assert changed_paths_are_protected(tmp_path, ["src/skcapstone/fleet/cron.py"]) is False


@pytest.mark.needs_skcapstone
def test_wiring_enforce_rejects_unsigned_manifest(tmp_path, monkeypatch):
    from skcapstone.fleet import signing as fleet_signing
    monkeypatch.setenv(fleet_signing.SIGNING_ENV, "enforce")
    monkeypatch.setattr(fleet_signing, "capauth_verifier", lambda: _fake_verifier)
    _write_manifest(tmp_path, {"protected": ["*/skcapstone/fleet/scheduler.py"]})
    # Unsigned -> fails closed -> protects EVERYTHING, including the file the
    # manifest tried to narrowly add, not just the hard-coded floor.
    assert changed_paths_are_protected(tmp_path, ["src/skcapstone/fleet/cron.py"]) is True


@pytest.mark.needs_skcapstone
def test_wiring_enforce_rejects_tampered_manifest(tmp_path, monkeypatch):
    from skcapstone.fleet import signing as fleet_signing
    monkeypatch.setenv(fleet_signing.SIGNING_ENV, "enforce")
    monkeypatch.setattr(fleet_signing, "capauth_verifier", lambda: _fake_verifier)
    obj = tmp_path / "objects"
    obj.mkdir(parents=True)
    signed = _write_signed_manifest(tmp_path, ["*/skcapstone/fleet/scheduler.py"],
                                    fleet_signing)
    # Tamper post-signing: widen the allowed set without re-signing.
    signed["protected"] = ["*/skcapstone/fleet/scheduler.py", "*/skharness/autocode/protected.py"]
    (obj / "_protected.json").write_text(json.dumps(signed))
    assert changed_paths_are_protected(tmp_path, ["src/skcapstone/fleet/cron.py"]) is True


@pytest.mark.needs_skcapstone
def test_wiring_enforce_honors_a_validly_signed_manifest(tmp_path, monkeypatch):
    from skcapstone.fleet import signing as fleet_signing
    monkeypatch.setenv(fleet_signing.SIGNING_ENV, "enforce")
    monkeypatch.setattr(fleet_signing, "capauth_verifier", lambda: _fake_verifier)
    _write_signed_manifest(tmp_path, ["*/skcapstone/fleet/scheduler.py"], fleet_signing)
    assert changed_paths_are_protected(tmp_path, ["src/skcapstone/fleet/scheduler.py"]) is True
    assert changed_paths_are_protected(tmp_path, ["src/skcapstone/fleet/cron.py"]) is False


@pytest.mark.needs_skcapstone
def test_carveout_floor_holds_under_a_validly_signed_manifest_that_omits_it(tmp_path, monkeypatch):
    """Acceptance criterion 3, proven rather than asserted: even a manifest
    that VERIFIES -- signed by the trusted key, tamper-free -- cannot narrow
    protection off the hard-coded guardrail floor by simply not mentioning
    it. The constitutional carve-out (the operator may rewrite almost
    anything EXCEPT its own guardrails) survives the signing work; signing
    raises the bar on who can propose a manifest, it does not hand the
    manifest author the guardrails themselves.
    """
    from skcapstone.fleet import signing as fleet_signing
    monkeypatch.setenv(fleet_signing.SIGNING_ENV, "enforce")
    monkeypatch.setattr(fleet_signing, "capauth_verifier", lambda: _fake_verifier)
    _write_signed_manifest(tmp_path, ["*/some/unrelated/path.py"], fleet_signing)
    for path in ("src/skharness/autocode/protected.py",
                 "src/skharness/autocode/engineering.py",
                 "src/skcapstone/itil.py",
                 "src/skcapstone/fleet/store.py",
                 "objects/_freeze.json",
                 "objects/_protected.json"):
        assert changed_paths_are_protected(tmp_path, [path]) is True, path
    # Negative control: ordinary work covered by neither the manifest nor the
    # floor stays unprotected under the SAME signed, enforced manifest.
    assert changed_paths_are_protected(tmp_path, ["src/skharness/autocode/orchestrator.py"]) is False


@pytest.mark.needs_skcapstone
def test_matched_protected_paths_reports_which_paths_hit_under_enforce(tmp_path, monkeypatch):
    from skcapstone.fleet import signing as fleet_signing
    monkeypatch.setenv(fleet_signing.SIGNING_ENV, "enforce")
    monkeypatch.setattr(fleet_signing, "capauth_verifier", lambda: _fake_verifier)
    _write_signed_manifest(tmp_path, ["*/skcapstone/fleet/scheduler.py"], fleet_signing)
    hits = matched_protected_paths(
        tmp_path, ["src/skcapstone/fleet/scheduler.py", "src/skcapstone/fleet/cron.py"])
    assert hits == ["src/skcapstone/fleet/scheduler.py"]
