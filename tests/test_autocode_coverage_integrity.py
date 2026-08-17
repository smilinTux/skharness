"""S21: the coverage arm must be able to tell a MEASUREMENT from a CLAIM.

The gaming path this file exists to close (card 53b8c8be, verified in code):

  * `_stage_work` (engineering.py) deliberately `git reset`s `coverage.xml` out
    of the index so CI byproducts never pollute the diff, and `_diff` then runs
    `git diff --cached`. So the diff the LLM grader reads can NEVER contain
    `coverage.xml`.
  * `diff_coverage` separately parsed whatever `coverage.xml` happened to be on
    disk, with no returncode check on `cov_cmd`.

A worker could therefore write a fabricated `coverage.xml` into the worktree:
the grader is structurally incapable of seeing it, and the coverage arm reported
whatever the file claimed. Two of three gate arms satisfied, the third unable to
observe why.

Every test here uses REAL subprocess calls (no `subprocess.run` mock) precisely
because the mock is the same shape as the attack: "a coverage file is on disk and
the coverage command did not produce it".
"""
from __future__ import annotations

import types as _t

import pytest

from skharness.autocode import ci
from skharness.autocode.engineering import EngineeringExecutor
from skharness.autocode.types import GateResult, HarnessResult, RepoSpec, WorkItem

# A Cobertura report claiming EVERY changed line is covered: the fabrication.
_PERFECT = """<?xml version="1.0" ?>
<coverage><packages><package><classes>
<class filename="src/skr/foo.py"><lines>
<line number="10" hits="1"/><line number="11" hits="1"/><line number="12" hits="1"/>
</lines></class></classes></package></packages></coverage>"""

# The same report told honestly: line 11 was never executed (2/3 = 0.66).
_HONEST = """<?xml version="1.0" ?>
<coverage><packages><package><classes>
<class filename="src/skr/foo.py"><lines>
<line number="10" hits="1"/><line number="11" hits="0"/><line number="12" hits="1"/>
</lines></class></classes></package></packages></coverage>"""

# A report that simply does not mention the changed file at all: what an `omit`
# rule added by the diff itself produces.
_OMITTED = """<?xml version="1.0" ?>
<coverage><packages><package><classes>
<class filename="src/skr/other.py"><lines>
<line number="1" hits="1"/>
</lines></class></classes></package></packages></coverage>"""

_DIFF = """diff --git a/src/skr/foo.py b/src/skr/foo.py
--- a/src/skr/foo.py
+++ b/src/skr/foo.py
@@ -9,3 +10,3 @@ def foo():
+    added_a = 1
+    added_b = 2
+    added_c = 3
"""

_DOC_DIFF = """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,1 +1,2 @@
+a documentation line
"""


def _repo(tmp, cmd, **kw):
    return RepoSpec(name="skr", path=str(tmp), base_branch="main",
                    integration_branch="develop", test_cmd="pytest",
                    ci="none", coverage_cmd=cmd, **kw)


def _plant(tmp, body=_PERFECT):
    """What a gaming worker does: write a coverage report it did not measure."""
    (tmp / "coverage.xml").write_text(body, encoding="utf-8")


def _emit(tmp, body: str) -> str:
    """A coverage_cmd that really does emit a FRESH coverage.xml, the way a real
    `pytest --cov --cov-report=xml` does."""
    (tmp / "expected.xml").write_text(body, encoding="utf-8")
    return "cp expected.xml coverage.xml"


# -- the unit-level negative controls ------------------------------------------

def test_a_planted_coverage_xml_is_not_read_as_a_measurement(tmp_path):
    """cov_cmd succeeds but emits nothing (the worker never wired --cov-report).
    The planted file must not be what the gate reads."""
    _plant(tmp_path)
    assert ci.diff_coverage(_repo(tmp_path, "true"), str(tmp_path), _DIFF) is None


def test_a_failed_coverage_command_is_not_a_coverage_pass(tmp_path):
    """The card's second half: cov_cmd had NO returncode check, so a failed
    coverage run and a successful one were indistinguishable."""
    _plant(tmp_path)
    assert ci.diff_coverage(_repo(tmp_path, "false"), str(tmp_path), _DIFF) is None


def test_a_stale_coverage_xml_is_not_read_as_current(tmp_path):
    """A cov_cmd that exits 0 while restoring an OLD report (mtime predating the
    run) is not a measurement of this diff."""
    (tmp_path / "saved.xml").write_text(_PERFECT, encoding="utf-8")
    cmd = "cp saved.xml coverage.xml && touch -d '2020-01-01T00:00:00' coverage.xml"
    assert ci.diff_coverage(_repo(tmp_path, cmd), str(tmp_path), _DIFF) is None


def test_a_real_coverage_run_still_reports_its_ratio(tmp_path):
    """Positive control: the fix must not close the arm on honest work."""
    ratio = ci.diff_coverage(_repo(tmp_path, _emit(tmp_path, _HONEST)), str(tmp_path), _DIFF)
    assert ratio == pytest.approx(2 / 3)


def test_a_diff_that_omits_itself_from_measurement_cannot_report_full_coverage(tmp_path):
    """The active form of vector 1: move the instrument. An `omit` rule added by
    the diff makes the changed source file vanish from the report, which used to
    reach `total == 0` and return a PERFECT 1.0."""
    cov = ci.diff_coverage(_repo(tmp_path, _emit(tmp_path, _OMITTED)), str(tmp_path), _DIFF)
    assert cov is None


def test_a_docs_only_diff_still_reads_as_nothing_to_measure(tmp_path):
    """Positive control for the rule above: a change with no measurable source
    line is honestly unmeasurable, not evasion, and must stay at 1.0."""
    cov = ci.diff_coverage(_repo(tmp_path, _emit(tmp_path, _OMITTED)), str(tmp_path), _DOC_DIFF)
    assert cov == 1.0


def test_the_run_leaves_an_observation_naming_why_coverage_was_unusable(tmp_path):
    """Acceptance criterion 3: an observation must exist that distinguishes a
    real coverage pass from a self-blinded one. Before this card, both were the
    same float with no event either way."""
    seen = []
    import skharness.autocode.health as health
    orig = health.record
    health.record = lambda kind, **d: seen.append((kind, d))
    try:
        _plant(tmp_path)
        ci.diff_coverage(_repo(tmp_path, "false"), str(tmp_path), _DIFF)
    finally:
        health.record = orig
    kinds = [k for k, _ in seen]
    assert "coverage_unusable" in kinds
    reasons = [d.get("reason") for k, d in seen if k == "coverage_unusable"]
    assert reasons == ["command_failed"]


# -- the gate-level negative control the card asks for -------------------------

def _gate_ex(mocker, tmp_path, cov_cmd):
    spec = _repo(tmp_path, cov_cmd)
    cfg = _t.SimpleNamespace(repo_map={"skr": spec}, automerge_repos=[])
    ex = EngineeringExecutor(cfg, board=mocker.Mock(), journal=mocker.Mock(),
                             digest=mocker.Mock())
    ex.journal.run_id = "run-1"
    mocker.patch.object(ex, "claim")
    mocker.patch.object(ex, "make_worktree", return_value=str(tmp_path))
    mocker.patch.object(ex, "_diff", return_value=_DIFF)
    mocker.patch.object(ex, "_head_sha", return_value="sha1")
    mocker.patch.object(ex, "_salvage_to_review", return_value="http://pr/1")
    mocker.patch("skharness.autocode.engineering.external_ci_verdict",
                 return_value="green")
    harness = mocker.Mock(name="harness")
    harness.name = "claude-code"
    harness.run_task.return_value = HarnessResult(ok=True, artifact=None, tokens=1,
                                                  cost_usd=0.0, raw={})
    harness.grade.return_value = GateResult(
        score=5, passed=False, notes="done <promise>COMPLETE</promise>", artifact=None)
    item = WorkItem(kind="engineering", ref="t1", source="coord", repo=None,
                    payload={"tags": ["repo:skr"], "title": "t", "description": "d",
                             "acceptance": ["a"]})
    return ex, harness, item


def test_a_planted_coverage_report_does_not_pass_the_twin_gate(mocker, tmp_path):
    """THE negative control (card 53b8c8be): plant a coverage.xml claiming 100
    percent, run the gate with a score-5 grade, a COMPLETE promise and green CI,
    and prove the build does NOT pass. The grader cannot see the planted file
    (it is reset out of the staged diff by design), so this arm is the only one
    that can catch it."""
    _plant(tmp_path)                       # 100 percent, fabricated
    ex, harness, item = _gate_ex(mocker, tmp_path, "true")   # cov_cmd emits nothing
    res = ex.run(item, harness)
    assert res.passed is False
    assert res.outcome == "ci_red"


def test_a_measured_coverage_report_still_passes_the_twin_gate(mocker, tmp_path):
    """Positive control: an honest coverage run at/above the floor still passes,
    so the fix above did not simply close the gate on everything."""
    ex, harness, item = _gate_ex(mocker, tmp_path, _emit(tmp_path, _PERFECT))
    res = ex.run(item, harness)
    assert res.passed is True and res.outcome == "pass"
