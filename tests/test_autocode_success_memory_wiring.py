"""S18: the success-memory call sites. Built is not wired.

S9 (card 506782a4) built `Board.record_success`, the `successes[]` sibling key,
`build_prior_success_feedback`, a separate renderer and 25 tests, and NOTHING in
production called any of it. A dormant success-memory module and an absent one
produce identical behaviour, and the tests are green either way, so every test
here drives the LIVE path (`EngineeringExecutor.run` / `.finalize`,
`DirectExecutor.run` / `.finalize`). A test that calls `record_success` directly
proves nothing S9's 25 tests did not already prove.

No test here can mint: `_settle_economics` is patched out on every executor.
"""
from __future__ import annotations

import types as _t

import pytest

from skharness.autocode.direct import DirectExecutor
from skharness.autocode.engineering import EngineeringExecutor
from skharness.autocode.types import (
    GATE_OUTCOMES, GateResult, HarnessResult, QualityMode, RepoSpec, WorkItem)

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
    return WorkItem(kind="engineering", ref="t1", source="coord", repo=None,
                    payload=p)


def _successes_payload(**over):
    entry = {"run_id": "r0", "ts": "2026-08-15T01:00:00+00:00", "round": 2,
             "outcome": "pass", "tried": "a scoped adapter shim",
             "why_succeeded": "the scoped shim kept the fixture stable",
             "approach_hint": "keep the shim scoped"}
    entry.update(over)
    return {"meta": {"autopilot": {"successes": [entry]}}}


def _build(mocker, cfg, executor=EngineeringExecutor, *, diff="DIFF", grades=(),
           ci_status="red", cov=0.95, board=None):
    """A REAL executor with only the outside world (git, CI, gh, wallet) mocked."""
    ex = executor(cfg, board=board or mocker.Mock(), journal=mocker.Mock(),
                  digest=mocker.Mock(), agent_name="autopilot")
    ex.journal.run_id = RUN_ID
    ex.journal.worktree_for.return_value = "/wt/t1"
    ex.board.clear_attempts.return_value = []
    mocker.patch("skharness.autocode.engineering.os.path.isdir", return_value=True)
    mocker.patch.object(ex, "make_worktree", return_value="/wt/t1")
    mocker.patch.object(ex, "_diff", return_value=diff)
    mocker.patch.object(ex, "_head_sha", return_value="sha1")
    mocker.patch.object(ex, "_salvage_to_review", return_value="http://pr/1")
    mocker.patch.object(ex, "_settle_economics")      # never touch the real wallet
    mocker.patch.object(ex, "_commit_and_push")
    mocker.patch.object(ex, "_open_pr", return_value="http://pr/1")
    mocker.patch.object(ex, "_changed_paths", return_value=["src/skr/foo.py"])
    mocker.patch.object(ex, "_fleet_root", return_value="/nonexistent-fleet-root")
    mocker.patch("skharness.autocode.engineering.external_ci_verdict",
                 return_value=ci_status)
    mocker.patch("skharness.autocode.engineering.diff_coverage", return_value=cov)
    harness = mocker.Mock(name="harness")
    harness.name = "claude-code"
    harness.run_task.return_value = HarnessResult(ok=True, artifact=None, tokens=1,
                                                  cost_usd=0.0, raw={})
    harness.grade.side_effect = list(grades)
    return ex, harness


def _pass_grade():
    return GateResult(score=5, passed=True, notes="done <promise>COMPLETE</promise>",
                      artifact="pr")


def _recorded(ex):
    """Kwargs of the single record_success call (asserts there was exactly one)."""
    assert ex.board.record_success.call_count == 1, (
        f"expected exactly one record_success, got "
        f"{ex.board.record_success.call_count}")
    return ex.board.record_success.call_args.kwargs


def _first_brief(harness):
    return harness.run_task.call_args_list[0].args[0]


# -- the write half, on the live path -----------------------------------------

def test_a_twin_gate_pass_records_a_success_through_run_and_finalize(mocker, cfg):
    """THE acceptance test. A real pass driven through run() then finalize()
    must write a success. Before this card the whole read/write path existed and
    no production call site reached any of it."""
    ex, harness = _build(mocker, cfg, ci_status="green", grades=[_pass_grade()])
    item = _item()
    res = ex.run(item, harness)
    assert res.passed is True and res.outcome == "pass"

    ex.finalize(item, res)

    kw = _recorded(ex)
    assert kw["run_id"] == RUN_ID
    assert kw["outcome"] == "pass"
    assert kw["tried"] and "add the parser" in kw["tried"]
    assert kw["why_succeeded"] and "\n" not in kw["why_succeeded"]


def test_a_terminal_failure_records_no_success(mocker, cfg):
    """Negative control. A build that never closed the gate must not write one."""
    grades = [GateResult(score=3, passed=False, notes="FAILED t.py::x", artifact=None)] * 4
    ex, harness = _build(mocker, cfg, grades=grades)
    item = _item()
    res = ex.run(item, harness)
    assert res.passed is False
    ex.finalize(item, res)
    assert ex.board.record_success.called is False


def test_the_salvage_path_records_no_success(mocker, cfg):
    """The salvage return has passed=False and opened a CI-green human-review PR.
    It is a success in the ordinary sense, and it must still NOT be written: the
    grade never said 5, so nothing verified the approach that is being
    remembered. Mirrors the existing 'must never record' contract on the failure
    side."""
    ex, harness = _build(mocker, cfg, ci_status="green",
                         grades=[GateResult(score=None, passed=False,
                                            notes="no verdict", artifact=None)])
    item = _item()
    res = ex.run(item, harness)
    assert res.passed is False and res.outcome == "salvage"
    ex.finalize(item, res)
    assert ex.board.record_success.called is False


def test_the_success_is_recorded_before_the_attempt_archive(mocker, cfg):
    """`clear_attempts` wipes attempts[] on every pass. It structurally cannot
    reach successes[], but ordering the write first means a future change to the
    clear cannot silently take the success with it."""
    ex, harness = _build(mocker, cfg, ci_status="green", grades=[_pass_grade()])
    item = _item()
    order = mocker.Mock()
    order.attach_mock(ex.board.record_success, "record")
    order.attach_mock(ex.board.clear_attempts, "clear")
    ex.finalize(item, ex.run(item, harness))
    seq = [c[0] for c in order.mock_calls]
    assert seq.index("record") < seq.index("clear")


def test_a_board_without_record_success_does_not_break_a_finalize(mocker, cfg):
    """Mid-rollout a node runs an older skcoord with no record_success at all.
    Success memory is an optimisation and must never be the reason a finalized
    PR dies. This is the live condition today: skcoord's record_success is on
    branch feat/s9-success-memory and is NOT yet on the installed package."""
    board = mocker.Mock(spec=["claim_task", "complete_task", "score_task",
                              "_write_task_raw", "clear_attempts", "record_attempt"])
    board.clear_attempts.return_value = []
    ex, harness = _build(mocker, cfg, ci_status="green", grades=[_pass_grade()],
                         board=board)
    item = _item()
    rec = mocker.patch("skharness.autocode.engineering.health.record")
    ex.finalize(item, ex.run(item, harness))          # must not raise
    kinds = [c.args[0] for c in rec.call_args_list if c.args]
    assert "record_success_error" in kinds


# -- the outcome-vocabulary decision -----------------------------------------

def test_a_success_outcome_outside_gate_outcomes_is_refused(mocker, cfg):
    """RECORDED DECISION (S18): success outcomes DO validate against
    `types.GATE_OUTCOMES`, rather than inheriting `record_attempt`'s looseness.

    The looseness was reasonable before S1, when no closed vocabulary existed.
    It is not now: `GateResult.__post_init__` already refuses an outcome outside
    the five-value set, the outcome rows S4 writes are keyed on the same
    vocabulary, and a success row carrying a sixth value could not be joined
    against either. An unvalidated field that only ever holds one value is
    indistinguishable from a validated one until the day it is not.
    """
    ex, _ = _build(mocker, cfg)
    rec = mocker.patch("skharness.autocode.engineering.health.record")
    ex._record_success(_item(), round=1, outcome="triumph", tried="t",
                       why_succeeded="w")
    assert ex.board.record_success.called is False
    kinds = [c.args[0] for c in rec.call_args_list if c.args]
    assert "record_success_invalid_outcome" in kinds


def test_every_gate_outcome_value_is_accepted_by_the_validator(mocker, cfg):
    """Positive control: the validator must not be a hardcoded == 'pass'."""
    ex, _ = _build(mocker, cfg)
    for outcome in sorted(GATE_OUTCOMES):
        ex.board.record_success.reset_mock()
        ex._record_success(_item(), round=1, outcome=outcome, tried="t",
                           why_succeeded="w")
        assert ex.board.record_success.call_count == 1, outcome


# -- the read half, on the live path -----------------------------------------

def test_prior_success_feedback_reaches_the_taskbrief_the_agent_receives(mocker, cfg):
    ex, harness = _build(mocker, cfg, diff="")
    ex.run(_item(_successes_payload()), harness)

    seeded = _first_brief(harness).prior_success_feedback
    assert seeded is not None
    assert "Prior successes on this card" in seeded
    assert "the scoped shim kept the fixture stable" in seeded


def test_a_card_with_no_success_memory_seeds_none(mocker, cfg):
    """Byte-identical to the behaviour before this card for every existing card."""
    ex, harness = _build(mocker, cfg, diff="")
    ex.run(_item(), harness)
    assert _first_brief(harness).prior_success_feedback is None


def test_the_success_seed_is_not_overwritten_by_in_run_grade_feedback(mocker, cfg):
    """`prior_feedback` is deliberately overwritten round to round by the live
    grade. The success seed is cross-RUN memory with no in-run equivalent, so it
    must persist across rounds rather than being clobbered by round one."""
    grades = [GateResult(score=3, passed=False, notes="fix X", artifact=None)] * 4
    ex, harness = _build(mocker, cfg, grades=grades)
    ex.run(_item(_successes_payload()), harness)

    round_two = harness.run_task.call_args_list[1].args[0]
    assert "Prior successes on this card" in round_two.prior_success_feedback
    assert "fix X" in round_two.prior_feedback          # unchanged behaviour


def test_the_failure_and_success_seeds_are_independent(mocker, cfg):
    """A card that has only ever succeeded must not grow a failure block, and
    vice versa. The two arrays are siblings, not one array with a flag."""
    ex, harness = _build(mocker, cfg, diff="")
    brief = None
    ex.run(_item(_successes_payload()), harness)
    brief = _first_brief(harness)
    assert brief.prior_feedback is None
    assert brief.prior_success_feedback is not None


# -- direct mode --------------------------------------------------------------

def test_direct_mode_success_records_a_success(mocker, cfg):
    """DirectExecutor's success path was untouched by S9 too. It records, and
    the entry says out loud that the run was UNGATED, so a future round cannot
    read an unverified pass as a verified one."""
    ex, harness = _build(mocker, cfg, DirectExecutor, diff="DIFF")
    item = _item()
    res = ex.run(item, harness)
    assert res.passed is True and res.mode == QualityMode.DIRECT.value

    ex.finalize(item, res)

    kw = _recorded(ex)
    assert kw["outcome"] == "pass"
    assert "UNGATED" in kw["why_succeeded"]


def test_direct_mode_failure_records_no_success(mocker, cfg):
    ex, harness = _build(mocker, cfg, DirectExecutor, diff="")
    item = _item()
    res = ex.run(item, harness)
    assert res.passed is False
    ex.finalize(item, res)
    assert ex.board.record_success.called is False


def test_direct_mode_seeds_prior_success_feedback(mocker, cfg):
    ex, harness = _build(mocker, cfg, DirectExecutor, diff="DIFF")
    ex.run(_item(_successes_payload()), harness)
    assert "Prior successes on this card" in \
        _first_brief(harness).prior_success_feedback
