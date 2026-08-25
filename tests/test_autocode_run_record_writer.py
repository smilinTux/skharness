"""The RunRecord writer at the twin-gate verdict boundary (coord card c672529e).

Three things this file proves, matching the card's acceptance criteria:

1. A verdict reaching the boundary (a lane 2 dry run through
   ``EngineeringExecutor.run``, and a lane 1 one-shot through ``ratify()``)
   writes a schema-valid RunRecord whose content hash verifies on re-read.
2. ``ratify()`` and the Ralph loop resolve to the SAME ``twin_gate_passed``
   function object -- an identity assertion, not just an import convention --
   demonstrated to actually fail when one path is shadowed with a
   reimplementation, then shown to pass again once restored.
3. A verdict that reaches the boundary with NO RunRecord written fails the
   drill: the negative control a writer failure must be caught by, not
   silently pass.
"""
from __future__ import annotations

import types as _t
from datetime import datetime, timedelta, timezone

import pytest

from skharness.autocode import run_record_writer
from skharness.autocode.engineering import EngineeringExecutor
from skharness.autocode.engineering import twin_gate_passed as loop_twin_gate_passed
from skharness.autocode.journal import read_run
from skharness.autocode.ratify import ratify
from skharness.autocode.run_record import validate_run_record
from skharness.autocode.types import GateResult, HarnessResult, RepoSpec, WorkItem


@pytest.fixture(autouse=True)
def _isolate_run_journal(tmp_path, monkeypatch):
    """Point the autopilot run journal (RunRecord's persistence target) at a
    throwaway dir for every test in this file. Without this, a real
    RunRecord write lands under the operator's live, Syncthing-synced
    ~/.skcapstone/coordination/autopilot/runs -- the exact hazard class
    _isolate_cost_dir / _isolate_joule_wallet already close for the cost
    ledger and the joule wallet, now closed here for the run journal too.
    """
    monkeypatch.setenv("SK_AUTOPILOT_RUNS_DIR", str(tmp_path / "runs"))


def _spec(name="skrender"):
    return RepoSpec(name=name, path=f"/repos/{name}", base_branch="main",
                    integration_branch="develop", test_cmd="pytest", ci="none")


class _FakeHarness:
    name = "fake"

    def __init__(self, grade_result):
        self._gr = grade_result

    def grade(self, brief):
        return self._gr


def _five_complete():
    return GateResult(score=5, passed=True,
                      notes="ready <promise>COMPLETE</promise>", artifact="pr")


# --------------------------------------------------------------------------- #
# 1a. build_run_record: pure construction, round-trips with a stable hash     #
# --------------------------------------------------------------------------- #

def test_build_run_record_round_trips_with_verifiable_content_hash():
    started = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    finished = started + timedelta(seconds=30)
    recorded = finished + timedelta(seconds=1)
    record = run_record_writer.build_run_record(
        run_id="airun-card-1-20260825T120000Z", card_id="card-1",
        repository="skharness", round=1, adapter="claude-code",
        model_requested="sk-creative", grader_model="sk-creative",
        effort_tier="gated",
        work_grade={"size": "M", "risk": "low", "sensitivity": "internal"},
        score=5, passed=True, notes="ready COMPLETE", outcome="pass",
        ci_status="green", diff_coverage=0.93, min_diff_coverage=0.8,
        pr=None, retries=0, payload=None,
        started_at=started, finished_at=finished, recorded_at=recorded,
        source_namespace="skharness.autocode.engineering.run",
    )
    assert record.content_hash.startswith("sha256:")
    assert record.task_shape is not None and record.task_shape.size == "M"

    # round-trip: dump to JSON-mode dict (as the journal stores it), reload,
    # and the hash must verify byte-for-byte against the original object.
    payload = record.model_dump(mode="json")
    restored = validate_run_record(payload)
    assert restored.content_hash == record.content_hash


def test_build_run_record_honestly_reports_inconclusive_score_as_gate_absent():
    """Gap 3 (module docstring): the frozen GateState vocabulary has no state
    for "passed is known but score is not". This proves the writer resolves
    that honestly (ABSENT, dropping neither field silently) rather than
    inventing a score or claiming OBSERVED with a null field."""
    started = datetime.now(timezone.utc)
    record = run_record_writer.build_run_record(
        run_id="airun-card-2-x", card_id="card-2", repository="skharness",
        round=3, adapter="claude-code", model_requested="sk-creative",
        grader_model="sk-creative", effort_tier="gated", work_grade=None,
        score=None, passed=False, notes="grade inconclusive", outcome="salvage",
        ci_status="green", diff_coverage=0.9, min_diff_coverage=0.8,
        pr="https://example/pr/2", retries=2, payload=None,
        started_at=started, finished_at=started,
        source_namespace="skharness.autocode.engineering.run",
    )
    assert record.score is None and record.passed is None
    assert record.gate_state.value == "absent"
    # the real, known outcome fact survives in `outcome` even though the
    # typed score/passed fields cannot carry it under GateState.ABSENT.
    # `notes` itself has no typed field anywhere on RunRecord (see the
    # module docstring's Gap 1): it lives only in the hashed evidence this
    # record's record_sources digest addresses, never on the record itself.
    assert record.outcome == "salvage"


# --------------------------------------------------------------------------- #
# 1b. write_verdict_run_record: persists to the journal, verifies on re-read  #
# --------------------------------------------------------------------------- #

def test_write_verdict_run_record_persists_and_verifies_on_reread():
    started = datetime.now(timezone.utc)
    record = run_record_writer.write_verdict_run_record(
        run_id="airun-card-3-drill", card_id="card-3", repository="skharness",
        round=1, adapter="claude-code", model_requested="sk-creative",
        grader_model="sk-creative", effort_tier="gated", work_grade=None,
        score=5, passed=True, notes="ready", outcome="pass",
        ci_status="green", diff_coverage=0.9, min_diff_coverage=0.8,
        pr="https://example/pr/3", retries=0, payload=None,
        started_at=started, finished_at=started,
        source_namespace="skharness.autocode.engineering.run",
    )
    assert record is not None

    data = read_run("airun-card-3-drill")
    stored = data["items"]["card-3"]["run_records"][0]
    restored = validate_run_record(stored)
    assert restored.content_hash == record.content_hash


# --------------------------------------------------------------------------- #
# 1c. end-to-end: a lane 2 dry run (Ralph loop) and a lane 1 ratify() call    #
# --------------------------------------------------------------------------- #

def _run_ex(mocker, cfg, grades, ci_status="green", cov=0.95):
    """Same shape as test_autopilot_engineering.py's _run_ex, except the
    journal is a REAL RunHandle (isolated by _isolate_run_journal above)
    instead of a Mock, so the RunRecord write actually lands and can be
    verified end-to-end."""
    from skharness.autocode.journal import handle as journal_handle

    ex = EngineeringExecutor(cfg, board=mocker.Mock(),
                             journal=journal_handle("airun-loop-drill"),
                             agent_name="autopilot")
    mocker.patch.object(ex, "make_worktree", return_value="/wt/t1")
    mocker.patch.object(ex, "prune_worktree")
    mocker.patch.object(ex, "_diff", return_value="DIFF")
    mocker.patch.object(ex, "_head_sha", return_value="sha1")
    mocker.patch("skharness.autocode.engineering.external_ci_verdict", return_value=ci_status)
    mocker.patch("skharness.autocode.engineering.diff_coverage", return_value=cov)
    harness = mocker.Mock(name="harness")
    harness.name = "claude-code"
    harness.run_task.return_value = HarnessResult(ok=True, artifact=None, tokens=1,
                                                  cost_usd=0.0, raw={})
    harness.grade.side_effect = grades
    item = WorkItem(kind="engineering", ref="t1", source="coord", repo=None,
                    payload={"tags": ["repo:skrender"], "title": "t",
                             "description": "d", "acceptance": ["a"]})
    return ex, harness, item


def test_lane2_dry_run_pass_writes_a_verifiable_run_record(mocker):
    cfg = _t.SimpleNamespace(repo_map={"skrender": _spec("skrender")},
                             automerge_repos=[])
    grades = [_five_complete()]
    ex, harness, item = _run_ex(mocker, cfg, grades)

    res = ex.run(item, harness)
    assert res.passed is True

    data = read_run("airun-loop-drill")
    records = data["items"]["t1"]["run_records"]
    assert len(records) == 1
    restored = validate_run_record(records[0])
    assert restored.content_hash.startswith("sha256:")
    assert restored.outcome == "pass" and restored.card_id == "t1"
    assert restored.round == 1


def test_lane2_dry_run_ci_red_terminal_also_writes_a_run_record(mocker):
    """The gate never closes (grade stays below 5 for every round): the
    ci_red terminal fallback is still a verdict reaching the boundary, per
    the card's acceptance criterion, and must still write a record."""
    cfg = _t.SimpleNamespace(repo_map={"skrender": _spec("skrender")},
                             automerge_repos=[])
    grades = [GateResult(score=3, passed=False, notes="thin tests", artifact=None)] * 4
    ex, harness, item = _run_ex(mocker, cfg, grades)

    res = ex.run(item, harness)
    assert res.passed is False and res.outcome == "ci_red"

    data = read_run("airun-loop-drill")
    records = data["items"]["t1"]["run_records"]
    assert len(records) == 1
    restored = validate_run_record(records[0])
    assert restored.outcome == "ci_red" and restored.gate_state.value == "observed"
    assert restored.score == 3 and restored.passed is False


def test_lane1_ratify_writes_a_verifiable_run_record(mocker):
    mocker.patch("skharness.autocode.engineering.subprocess.run",
                 return_value=_t.SimpleNamespace(stdout="", stderr="", returncode=0))
    mocker.patch("skharness.autocode.ratify.subprocess.run",
                 return_value=_t.SimpleNamespace(stdout="", stderr="", returncode=0))
    mocker.patch("skharness.autocode.ratify.external_ci_verdict", return_value="green")
    mocker.patch("skharness.autocode.ratify.diff_coverage", return_value=0.95)
    harness = _FakeHarness(_five_complete())

    res = ratify(_spec(), "/wt/ratify-drill", ["a"], harness)
    assert res.passed is True

    # ratify() mints its own run_id (airun-ratify-<card_id>-<ts>); find it by
    # scanning the isolated journal dir rather than guessing the timestamp.
    from skharness.autocode.journal import runs_dir
    candidates = list(runs_dir().glob("airun-ratify-ratify-drill-*.json"))
    assert len(candidates) == 1, f"expected exactly one ratify run journal, found {candidates}"

    data = read_run(candidates[0].stem)
    records = data["items"]["ratify-drill"]["run_records"]
    assert len(records) == 1
    restored = validate_run_record(records[0])
    assert restored.content_hash.startswith("sha256:")
    assert restored.outcome == "pass" and restored.card_id == "ratify-drill"


# --------------------------------------------------------------------------- #
# 2. identity assertion: ratify() and the Ralph loop share ONE gate object    #
# --------------------------------------------------------------------------- #

def _assert_same_twin_gate() -> None:
    # `from skharness.autocode.ratify import twin_gate_passed`, NOT
    # `import skharness.autocode.ratify` / `from skharness.autocode import
    # ratify` -- either of THOSE resolves `skharness.autocode.ratify` by
    # attribute traversal off the `skharness.autocode` PACKAGE object, and
    # that package's __init__.py runs `from .ratify import ratify`, which
    # permanently rebinds the package attribute `skharness.autocode.ratify`
    # to the FUNCTION, shadowing the submodule for every later dotted
    # lookup. `from skharness.autocode.ratify import <name>` instead goes
    # straight through `sys.modules['skharness.autocode.ratify']`, which is
    # never shadowed, and reads the real module's own namespace.
    from skharness.autocode.engineering import twin_gate_passed as loop_gate
    from skharness.autocode.ratify import twin_gate_passed as ratify_gate

    assert ratify_gate is loop_gate, (
        "ratify() and the Ralph loop must resolve to the SAME twin_gate_passed "
        "function object (coord card c672529e, R1): two merge paths that can "
        "drift apart from each other are two different merge bars."
    )


def test_ratify_and_ralph_loop_share_the_same_twin_gate_function_object():
    _assert_same_twin_gate()


def test_identity_assertion_fails_if_ratify_reimplements_the_gate(mocker):
    """Induced-failure demonstration: shadow ratify's twin_gate_passed with a
    SECOND function object of IDENTICAL behaviour (the drift the identity
    check exists to catch even when the logic still agrees), prove the
    assertion fails, then restore the real import and prove it passes again.
    """
    from skharness.autocode.engineering import is_complete

    def _reimplemented_twin_gate_passed(gr, ci_status, cov, repo):
        cov_ok = cov is not None and cov >= repo.min_diff_coverage
        return (gr.score == 5 and is_complete(gr.notes)
                and ci_status == "green" and cov_ok)

    assert _reimplemented_twin_gate_passed is not loop_twin_gate_passed  # a distinct object

    # String-target patch: resolves "skharness.autocode.ratify" straight
    # through sys.modules (see _assert_same_twin_gate's comment on why the
    # package's __init__.py makes attribute-traversal forms unsafe here).
    mocker.patch("skharness.autocode.ratify.twin_gate_passed",
                 _reimplemented_twin_gate_passed)
    with pytest.raises(AssertionError):
        _assert_same_twin_gate()

    mocker.stopall()  # restore the real import
    _assert_same_twin_gate()  # passes again: back to the one shared object


# --------------------------------------------------------------------------- #
# 3. negative control: a verdict with no RunRecord fails the drill            #
# --------------------------------------------------------------------------- #

def test_verdict_with_no_run_record_written_fails_the_drill(mocker):
    """If the writer fails, write_verdict_run_record swallows the exception
    (best-effort, like every other telemetry call in this package) and
    returns None -- but a DRILL asserting a RunRecord exists for a verdict
    that reached the boundary must then fail, not silently pass. This is the
    negative test the card's acceptance criteria names explicitly."""
    mocker.patch("skharness.autocode.run_record_writer.persist_run_record",
                 side_effect=RuntimeError("simulated journal outage"))
    started = datetime.now(timezone.utc)

    record = run_record_writer.write_verdict_run_record(
        run_id="airun-card-4-drill", card_id="card-4", repository="skharness",
        round=1, adapter="claude-code", model_requested="sk-creative",
        grader_model="sk-creative", effort_tier="gated", work_grade=None,
        score=5, passed=True, notes="ready", outcome="pass",
        ci_status="green", diff_coverage=0.9, min_diff_coverage=0.8,
        pr=None, retries=0, payload=None,
        started_at=started, finished_at=started,
        source_namespace="skharness.autocode.engineering.run",
    )
    assert record is None  # the writer failed, best-effort, and said so by returning None

    data = read_run("airun-card-4-drill")
    stored = (data.get("items", {}).get("card-4", {}) or {}).get("run_records") or []

    def _drill_assert_run_record_exists():
        assert stored, "expected a RunRecord for a verdict that reached the boundary"

    with pytest.raises(AssertionError):
        _drill_assert_run_record_exists()
