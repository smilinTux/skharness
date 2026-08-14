"""Failure-memory call sites: what gets recorded, what must NOT, and read-back.

Spec: docs/specs/2026-08-14-skharness-failure-memory.md (FM-4, FM-5, FM-6).

Two of these tests exist because of specific double-write hazards found while
verifying the design against the real source, and they are the load-bearing ones:

  * `escalate()` is called by the orchestrator for EVERY non-passed result, so a
    record there would double-count every failure. It must record nothing.
  * the salvage return has `passed=False` but opened a CI-green human-review PR.
    That is a SUCCESS. Recording it would poison every future run of the card.
"""
from __future__ import annotations

import types as _t

import pytest

from skharness.autocode.direct import DirectExecutor
from skharness.autocode.engineering import EngineeringExecutor
from skharness.autocode.types import GateResult, HarnessResult, RepoSpec, WorkItem

RUN_ID = "run-1"


def _spec(name="skrender"):
    return RepoSpec(name=name, path=f"/repos/{name}", base_branch="main",
                    integration_branch="develop", test_cmd="pytest", ci="none")


@pytest.fixture
def cfg():
    return _t.SimpleNamespace(repo_map={"skrender": _spec()}, automerge_repos=[])


def _item(payload=None):
    p = {"tags": ["repo:skrender"], "title": "add the parser",
         "description": "d", "acceptance": ["a"]}
    p.update(payload or {})
    return WorkItem(kind="engineering", ref="t1", source="coord", repo=None, payload=p)


def _attempts_payload(**over):
    entry = {"run_id": "r0", "ts": "2026-08-14T01:00:00+00:00", "round": 2,
             "outcome": "ci_red", "tried": "rewrote the parser",
             "why_failed": "tests/test_p.py::test_empty asserts ValueError",
             "replacement_hint": ""}
    entry.update(over)
    return {"meta": {"autopilot": {"attempts": [entry]}}}


def _build(mocker, cfg, executor=EngineeringExecutor, *, diff="DIFF", grades=(),
           ci_status="red", cov=0.95, board=None):
    """Wire a REAL executor with only the outside world (git, CI, gh) mocked."""
    ex = executor(cfg, board=board or mocker.Mock(), journal=mocker.Mock(),
                  digest=mocker.Mock())
    ex.journal.run_id = RUN_ID
    mocker.patch.object(ex, "make_worktree", return_value="/wt/t1")
    mocker.patch.object(ex, "_diff", return_value=diff)
    mocker.patch.object(ex, "_head_sha", return_value="sha1")
    mocker.patch.object(ex, "_salvage_to_review", return_value="http://pr/1")
    mocker.patch("skharness.autocode.engineering.external_ci_verdict",
                 return_value=ci_status)
    mocker.patch("skharness.autocode.engineering.diff_coverage", return_value=cov)
    harness = mocker.Mock(name="harness")
    harness.name = "claude-code"
    harness.run_task.return_value = HarnessResult(ok=True, artifact=None, tokens=1,
                                                  cost_usd=0.0, raw={})
    harness.grade.side_effect = list(grades)
    return ex, harness


def _recorded(ex):
    """The kwargs of the single record_attempt call (asserts there was exactly one)."""
    assert ex.board.record_attempt.call_count == 1
    return ex.board.record_attempt.call_args.kwargs


def _grade(score, notes):
    return GateResult(score=score, passed=False, notes=notes, artifact=None)


# -- FM-4: the two engineering write sites ------------------------------------

def test_no_op_double_empty_bail_records_a_no_op_attempt(mocker, cfg):
    ex, harness = _build(mocker, cfg, diff="")          # no diff, ever
    res = ex.run(_item(), harness)

    assert res.passed is False and "no-op" in res.notes
    kw = _recorded(ex)
    assert kw["outcome"] == "no_op"
    assert kw["run_id"] == RUN_ID
    assert kw["round"] == 2
    assert kw["why_failed"] and "\n" not in kw["why_failed"]
    assert kw["replacement_hint"]                        # a no-op has an obvious next step


def test_did_not_converge_records_a_ci_red_attempt_with_the_test_id(mocker, cfg):
    notes = ("Overall the approach is close.\n"
             "FAILED tests/test_p.py::test_empty - AssertionError: assert None is not None")
    ex, harness = _build(mocker, cfg, grades=[_grade(3, notes)] * 4)
    res = ex.run(_item(), harness)

    assert res.passed is False and "did not converge" in res.notes
    kw = _recorded(ex)
    assert kw["outcome"] == "ci_red"
    assert "tests/test_p.py::test_empty" in kw["why_failed"]
    assert "Overall the approach is close" not in kw["why_failed"]   # distilled, not wholesale


def test_a_twin_gate_pass_records_no_failure(mocker, cfg):
    ex, harness = _build(mocker, cfg, ci_status="green", grades=[
        GateResult(score=5, passed=True, notes="done <promise>COMPLETE</promise>",
                   artifact="pr")])
    res = ex.run(_item(), harness)

    assert res.passed is True
    assert ex.board.record_attempt.called is False


# -- FM-4: the two things that must NEVER record (verified hazards) -----------

def test_escalate_records_nothing(mocker, cfg):
    """The orchestrator calls escalate() for EVERY non-passed result. A write here
    would double-record every failure AND fire on the salvage success."""
    ex, _ = _build(mocker, cfg)

    ex.escalate(_item(), "did not converge")

    assert ex.board.record_attempt.called is False


def test_salvage_to_human_review_records_nothing(mocker, cfg):
    """passed=False but CI green + coverage met + a PR opened for review: that is a
    success. Recording it would poison every future run of this card."""
    ex, harness = _build(mocker, cfg, ci_status="green",
                         grades=[_grade(None, "grader returned no verdict")])
    res = ex.run(_item(), harness)

    assert res.passed is False and res.artifact == "http://pr/1"    # salvage path taken
    assert ex.board.record_attempt.called is False


# -- FM-4: a recording failure must never break a build ----------------------

def test_a_board_that_cannot_record_does_not_break_the_run(mocker, cfg):
    board = mocker.Mock()
    board.record_attempt.side_effect = RuntimeError("task file vanished")
    ex, harness = _build(mocker, cfg, diff="", board=board)

    res = ex.run(_item(), harness)

    assert res.passed is False and "no-op" in res.notes      # the run still returned


def test_a_board_without_the_method_at_all_does_not_break_the_run(mocker, cfg):
    """Mid-rollout a node may run an older skcoord with no record_attempt."""
    board = mocker.Mock(spec=["claim_task", "complete_task", "score_task",
                              "_write_task_raw"])
    ex, harness = _build(mocker, cfg, diff="", board=board)

    assert ex.run(_item(), harness).passed is False


# -- FM-4: read-back at run() start ------------------------------------------

def _first_brief(harness):
    return harness.run_task.call_args_list[0].args[0]


def test_run_seeds_round_one_feedback_from_the_cards_attempts(mocker, cfg):
    ex, harness = _build(mocker, cfg, diff="")
    ex.run(_item(_attempts_payload()), harness)

    seeded = _first_brief(harness).prior_feedback
    assert seeded is not None
    assert "Prior attempts on this card" in seeded
    assert "tests/test_p.py::test_empty" in seeded


def test_run_on_a_card_with_no_memory_seeds_none_exactly_as_before(mocker, cfg):
    ex, harness = _build(mocker, cfg, diff="")
    ex.run(_item(), harness)

    assert _first_brief(harness).prior_feedback is None


def test_in_run_grade_feedback_still_overwrites_the_seed(mocker, cfg):
    """Read-back seeds round 1 only; the live grade must still drive later rounds."""
    ex, harness = _build(mocker, cfg, grades=[_grade(3, "round one says fix X")] * 4)
    ex.run(_item(_attempts_payload()), harness)

    round_two = harness.run_task.call_args_list[1].args[0].prior_feedback
    assert "round one says fix X" in round_two
    assert "Prior attempts on this card" not in round_two


# -- FM-5: direct mode -------------------------------------------------------

def test_direct_mode_failure_records_direct_fail(mocker, cfg):
    ex, harness = _build(mocker, cfg, DirectExecutor, diff="")
    res = ex.run(_item(), harness)

    assert res.passed is False
    kw = _recorded(ex)
    assert kw["outcome"] == "direct_fail"
    assert kw["run_id"] == RUN_ID


def test_direct_mode_success_records_nothing(mocker, cfg):
    ex, harness = _build(mocker, cfg, DirectExecutor, diff="DIFF")
    res = ex.run(_item(), harness)

    assert res.passed is True
    assert ex.board.record_attempt.called is False


def test_direct_mode_seeds_prior_feedback_from_the_card(mocker, cfg):
    ex, harness = _build(mocker, cfg, DirectExecutor, diff="DIFF")
    ex.run(_item(_attempts_payload()), harness)

    assert "Prior attempts on this card" in _first_brief(harness).prior_feedback


def test_direct_mode_with_no_memory_seeds_none(mocker, cfg):
    ex, harness = _build(mocker, cfg, DirectExecutor, diff="DIFF")
    ex.run(_item(), harness)

    assert _first_brief(harness).prior_feedback is None


# -- FM-6: clear-on-pass, archived to the run journal ------------------------

def _finalize(mocker, cfg, tmp_path, *, cleared, passed=True):
    ex, _ = _build(mocker, cfg)
    ex.board.clear_attempts.return_value = cleared
    ex.journal.worktree_for.return_value = str(tmp_path)
    mocker.patch.object(ex, "_commit_and_push")
    mocker.patch.object(ex, "_open_pr", return_value="http://pr/1")
    mocker.patch.object(ex, "_settle_economics")
    result = GateResult(score=5, passed=passed, notes="ok", artifact="pr")
    ex.finalize(_item(), result)
    return ex


def test_final_pass_clears_the_card_and_archives_to_the_run_journal(mocker, cfg, tmp_path):
    entries = [{"run_id": "r0", "outcome": "ci_red", "why_failed": "old cause"}]
    ex = _finalize(mocker, cfg, tmp_path, cleared=entries)

    ex.board.clear_attempts.assert_called_once_with("t1")
    ex.journal.archive_attempts.assert_called_once_with("t1", entries)


def test_nothing_is_archived_when_the_card_had_no_memory(mocker, cfg, tmp_path):
    ex = _finalize(mocker, cfg, tmp_path, cleared=[])

    assert ex.journal.archive_attempts.called is False


def test_a_clear_failure_never_breaks_finalize(mocker, cfg, tmp_path):
    ex, _ = _build(mocker, cfg)
    ex.board.clear_attempts.side_effect = RuntimeError("board unavailable")
    ex.journal.worktree_for.return_value = str(tmp_path)
    mocker.patch.object(ex, "_commit_and_push")
    opened = mocker.patch.object(ex, "_open_pr", return_value="http://pr/1")
    mocker.patch.object(ex, "_settle_economics")

    ex.finalize(_item(), GateResult(score=5, passed=True, notes="ok", artifact="pr"))

    assert opened.called is True        # finalize completed despite the clear failing
