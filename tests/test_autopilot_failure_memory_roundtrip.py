"""FM-7 integration: fail -> remember -> read back -> pass -> forget.

The unit tests pin each half in isolation. This one proves the loop actually
closes across the repo boundary, against a REAL skcoord Board on disk and a REAL
autopilot run journal. Only the outside world (git, CI, gh) is mocked.

The whole feature is worthless if any link is broken, and every link crosses a
boundary: skcoord writes the card, skharness reads it back into a prompt, and the
pass clears it. Nothing here reimplements the renderer or the writer.
"""
from __future__ import annotations

import json

import pytest

from skharness.autocode import journal as journal_mod
from skharness.autocode.engineering import EngineeringExecutor
from skharness.autocode.failure_memory import build_prior_feedback
from skharness.autocode.types import GateResult, HarnessResult, RepoSpec, WorkItem

pytestmark = pytest.mark.needs_skcapstone


@pytest.fixture
def board_cls():
    coordination = pytest.importorskip("skcapstone.coordination")
    if not hasattr(coordination.Board, "record_attempt"):
        pytest.skip("needs a skcoord carrying record_attempt/clear_attempts (FM-1, FM-2)")
    return coordination


def _payload_from_board(board, task_id: str) -> dict:
    """Rebuild the payload a FRESH run would hold, from the card on disk.

    Mirrors orchestrator._to_workitem: the raw task dict (so meta.autopilot
    survives) plus the phase-0 facts the executor's contract reads.
    """
    path = next(iter(sorted(board.tasks_dir.glob(f"{task_id}-*.json"))))
    task = json.loads(path.read_text(encoding="utf-8"))
    return {**task, "unblocked": True, "verdict": "valid",
            "acceptance": task.get("acceptance_criteria") or [],
            "tags": task.get("tags") or []}


def _card_attempts(board, task_id: str) -> list[dict]:
    task = next(t for t in board.load_tasks() if t.id == task_id)
    return ((task.meta or {}).get("autopilot", {})).get("attempts", [])


def _executor(mocker, board, run_id, *, diff, grades=()):
    cfg = mocker.Mock()
    cfg.repo_map = {"skrender": RepoSpec(name="skrender", path="/repos/skrender",
                                        base_branch="main", integration_branch="develop",
                                        test_cmd="pytest", ci="none")}
    cfg.automerge_repos = []
    ex = EngineeringExecutor(cfg, board=board, journal=journal_mod.handle(run_id),
                             digest=mocker.Mock(), agent_name="autopilot-test")
    mocker.patch.object(ex, "make_worktree", return_value="/wt/t1")
    mocker.patch.object(ex, "_diff", return_value=diff)
    mocker.patch.object(ex, "_head_sha", return_value="sha1")
    mocker.patch("skharness.autocode.engineering.external_ci_verdict",
                 return_value="green" if diff else "red")
    mocker.patch("skharness.autocode.engineering.diff_coverage", return_value=0.95)
    harness = mocker.Mock()
    harness.name = "claude-code"
    harness.run_task.return_value = HarnessResult(ok=True, artifact=None, tokens=1,
                                                  cost_usd=0.0, raw={})
    harness.grade.side_effect = list(grades)
    return ex, harness


def test_failure_is_remembered_read_back_then_forgotten_on_pass(
        mocker, tmp_path, monkeypatch, board_cls):
    monkeypatch.setenv("SK_AUTOPILOT_RUNS_DIR", str(tmp_path / "runs"))
    board = board_cls.Board(tmp_path / "coord")
    task = board_cls.Task(title="add the parser", priority="high",
                          tags=["repo:skrender"], acceptance_criteria=["parses input"])
    board.create_task(task)
    tid = task.id

    # -- 1. a terminal non-pass leaves exactly one distilled entry on the card --
    item = WorkItem(kind="engineering", ref=tid, source="coord", repo=None,
                    payload=_payload_from_board(board, tid))
    ex, harness = _executor(mocker, board, "run-A", diff="")     # no diff -> no_op bail
    first = ex.run(item, harness)

    assert first.passed is False
    entries = _card_attempts(board, tid)
    assert len(entries) == 1
    assert entries[0]["outcome"] == "no_op"
    assert entries[0]["run_id"] == "run-A"
    # the journal join key resolves: the run the entry points at is on disk
    assert tid in journal_mod.read_run("run-A")["items"]

    # -- 2. a FRESH run reads that memory off the board, not from run state ----
    fresh_payload = _payload_from_board(board, tid)
    assert build_prior_feedback(fresh_payload) is not None

    item2 = WorkItem(kind="engineering", ref=tid, source="coord", repo=None,
                     payload=fresh_payload)
    ex2, harness2 = _executor(mocker, board, "run-B", diff="DIFF", grades=[
        GateResult(score=5, passed=True, notes="done <promise>COMPLETE</promise>",
                   artifact="pr")])
    seeded = None

    def _capture(tb):
        nonlocal seeded
        seeded = tb.prior_feedback
        return HarnessResult(ok=True, artifact=None, tokens=1, cost_usd=0.0, raw={})

    harness2.run_task.side_effect = _capture
    second = ex2.run(item2, harness2)

    assert seeded is not None, "round 1 walked in blind"
    assert "Prior attempts on this card" in seeded
    assert "no diff in 2 rounds" in seeded
    assert len(seeded) <= 600
    assert second.passed is True

    # -- 3. the pass clears the card and archives the memory to that run -------
    mocker.patch.object(ex2, "_commit_and_push")
    mocker.patch.object(ex2, "_open_pr", return_value="http://pr/1")
    mocker.patch.object(ex2, "_settle_economics")
    ex2.journal.set_worktree(tid, str(tmp_path))     # a real dir for finalize's check
    ex2.finalize(item2, second)

    assert _card_attempts(board, tid) == []
    archived = journal_mod.read_run("run-B")["items"][tid]["cleared_attempts"]
    assert [e["outcome"] for e in archived] == ["no_op"]


def test_a_second_failure_on_the_same_card_does_not_duplicate_the_first(
        mocker, tmp_path, monkeypatch, board_cls):
    """Two runs, same failure mode: the card grows, but the PROMPT does not."""
    monkeypatch.setenv("SK_AUTOPILOT_RUNS_DIR", str(tmp_path / "runs"))
    board = board_cls.Board(tmp_path / "coord")
    task = board_cls.Task(title="add the parser", priority="high",
                          tags=["repo:skrender"], acceptance_criteria=["parses input"])
    board.create_task(task)
    tid = task.id

    for run_id in ("run-A", "run-B"):
        item = WorkItem(kind="engineering", ref=tid, source="coord", repo=None,
                        payload=_payload_from_board(board, tid))
        ex, harness = _executor(mocker, board, run_id, diff="")
        ex.run(item, harness)

    assert len(_card_attempts(board, tid)) == 2          # distinct runs, both stored
    rendered = build_prior_feedback(_payload_from_board(board, tid))
    assert rendered.count("This failed for") + rendered.count(
        "This previously failed for") == 1               # one CAUSE reaches the prompt
