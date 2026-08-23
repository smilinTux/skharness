"""The guard that the wiring detector is not itself inert (card bb536f68).

The detector (:mod:`skharness.wiring`) exists to catch a class: a load-bearing
mechanism that is unit-tested yet has no reachable caller on the path that runs.
Epic 935d4b61 shipped eight of them. This file is the negative control: it
points the detector at a miniature pre-fix tree that reproduces all eight shapes
and FAILS if the detector finds fewer than eight -- because a detector that
under-reports is the ninth built-but-unwired mechanism.

It also asserts the detector's OWN reachability, from both its static inventory
(``wiring_audit``) and its runtime CLI entry point, so it cannot quietly go
inert without this suite going red.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

# Audit the tree THIS TEST LIVES IN (the worktree/CI checkout), independent of
# where an editable `skharness` install happens to import from.
REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"


def _load_detector():
    """Load the detector from THIS checkout's source, not the editable install.

    Several skharness worktrees share one editable install on this box, so a bare
    ``import skharness.wiring`` resolves to whatever checkout the install points
    at -- not necessarily the branch under test. The detector module is
    self-contained (stdlib + yaml only), so we load it directly from the source
    tree the test lives in. This makes the guard test THIS branch's detector both
    locally and in CI, matching the file-path rooting used throughout below.
    """
    path = SRC_ROOT / "skharness" / "wiring.py"
    spec = importlib.util.spec_from_file_location("skharness_wiring_under_test", path)
    module = importlib.util.module_from_spec(spec)
    # Register before exec so dataclass() can resolve the module's __dict__.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_detector = _load_detector()
Analyzer = _detector.Analyzer
audit = _detector.audit
load_inventory = _detector.load_inventory
main = _detector.main
REAL_INVENTORY = SRC_ROOT / "skharness" / "wiring_inventory.yaml"
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "wiring_prefix"
FIXTURE_INVENTORY = FIXTURE_ROOT / "inventory.yaml"

# The eight instances of epic 935d4b61, by fixture id.
EXPECTED_EIGHT = {
    "instance_1_record_run",
    "instance_2_record_success",
    "instance_3_changed_paths_are_protected",
    "instance_4_cap_ledger_ceiling",
    "instance_5_escalation_reason",
    "instance_6_graded_dispatch",
    "instance_7_reverted_sensor",
    "instance_8_routing_guard_protected",
}


# --------------------------------------------------------------------------- #
# The core negative control: all eight, or the detector is inert.             #
# --------------------------------------------------------------------------- #


def test_detector_finds_all_eight_on_the_prefix_tree():
    """Acceptance criterion 3: fewer than eight means the detector is inert."""
    findings = audit(FIXTURE_ROOT, FIXTURE_INVENTORY)
    flagged = {f.mechanism_id for f in findings}
    missing = EXPECTED_EIGHT - flagged
    assert not missing, (
        f"detector is INERT: it missed {sorted(missing)}. A detector that "
        f"under-reports is the ninth built-but-unwired mechanism."
    )
    assert flagged == EXPECTED_EIGHT, f"unexpected extra findings: {flagged - EXPECTED_EIGHT}"


def test_each_prefix_failure_mode_is_exercised():
    """The eight are caught by genuinely different reachability arguments, not
    one over-broad rule firing eight times."""
    findings = audit(FIXTURE_ROOT, FIXTURE_INVENTORY)
    checks = {f.check for f in findings}
    assert {"reachable", "arg_constant", "present", "sensor_varies"} <= checks


# --------------------------------------------------------------------------- #
# Criterion 2: the detector FAILS on a mechanism with no REACHABLE caller,     #
# not merely no caller. Instance 3 has a caller -- behind a dead flag.         #
# --------------------------------------------------------------------------- #


def test_reachable_distinguishes_dead_flag_from_live_caller():
    an = Analyzer(FIXTURE_ROOT)
    # instance 3's carve-out HAS a call site...
    assert an.all_reachable_calls_to(["engineering:engineer"], "changed_paths_are_protected")
    # ...but none of it is live, because the only caller is under `if automerge:`.
    assert not an.live_calls_to(
        ["engineering:engineer"], "changed_paths_are_protected", frozenset({"automerge"})
    )


def test_detector_reacts_to_a_freshly_unwired_mechanism(tmp_path):
    """Pointed at the REAL tree (not just fixtures), the detector still fires for
    a mechanism that is present but unreachable -- proof it is not fixture-only."""
    inv = tmp_path / "inv.yaml"
    inv.write_text(
        yaml.safe_dump(
            {
                "mechanisms": [
                    {
                        "id": "orphan",
                        "description": "a real symbol with no reachable caller",
                        "checks": [
                            {
                                "type": "reachable",
                                "entrypoints": ["skharness.wiring:main"],
                                "callee": "record_run",
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    findings = audit(SRC_ROOT, inv)
    assert [f.mechanism_id for f in findings] == ["orphan"]


# --------------------------------------------------------------------------- #
# Criterion 4: the detector's own reachability is asserted.                    #
# --------------------------------------------------------------------------- #


def test_real_inventory_is_green():
    """Every mechanism the fleet promises is wired must be reachable today. If
    someone unwires one, this goes red."""
    findings = audit(SRC_ROOT, REAL_INVENTORY)
    assert findings == [], f"unwired mechanism(s) on the live tree: {findings}"


def test_detector_lists_itself_and_is_reachable():
    mechanisms = load_inventory(REAL_INVENTORY)
    by_id = {m["id"]: m for m in mechanisms}
    assert "wiring_audit" in by_id, "the detector must audit its own reachability"
    check = by_id["wiring_audit"]["checks"][0]
    # The static claim: main() reaches audit().
    an = Analyzer(SRC_ROOT)
    assert an.live_calls_to(check["entrypoints"], check["callee"], frozenset())


def test_cli_entry_point_runs_the_detector():
    """The live path: the public CLI entry point actually runs and reaches the
    audit, returning 0 clean and 1 with findings."""
    assert main(["--root", str(SRC_ROOT), "--inventory", str(REAL_INVENTORY)]) == 0
    assert (
        main(
            [
                "--root",
                str(FIXTURE_ROOT),
                "--inventory",
                str(FIXTURE_INVENTORY),
                "--format",
                "json",
            ]
        )
        == 1
    )


def test_console_script_is_declared():
    """The detector's binary must exist on the live path, wired in pyproject."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "skharness-wiring-audit = \"skharness.wiring:main\"" in pyproject


def test_unknown_check_type_is_a_hard_error(tmp_path):
    inv = tmp_path / "inv.yaml"
    inv.write_text(
        yaml.safe_dump(
            {"mechanisms": [{"id": "x", "checks": [{"type": "nonsense"}]}]}
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        audit(FIXTURE_ROOT, inv)
