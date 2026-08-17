"""S4 (card 432b81b7): exactly one append-only outcome row per item per run,
written from the ORCHESTRATOR at every terminal state.

The load-bearing assertion in this file is the FIRST test: a FAILED build
produces a row. A test that only proves a pass writes a row asserts nothing,
because joules.settle() already covers the pass half and is contractually
pass-only. Recording at settle would yield a dataset of only passes, so
over-graded cards would appear constantly, under-graded ones never, class
floors would ratchet downward, and every version would measure as an
improvement (inherited verbatim from card 09573989).

Every test here therefore also asserts that settle never ran on the path it
exercises, or exercises a path where settle structurally cannot run.

No em/en dashes anywhere (SKWorld hard rule).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from skharness.autocode import autopilot_cost
from skharness.autocode import orchestrator as orch
from skharness.autocode.fleet_dispatch import DispatchDecision
from skharness.autocode.orchestrator import Caps, CapLedger
from skharness.autocode.types import (ClaimRaced, DecisionItem, GateResult,
                                      UNRECORDED, Verdict, WorkItem)


# tests/conftest.py already points SKAI_COST_DIR at a throwaway dir for EVERY
# test, so nothing here can append to the operator's live, Syncthing-synced
# ledger. This fixture only asserts that guard is actually in force: a suite
# that writes to the real fleet is a standing hazard in this repo.
@pytest.fixture(autouse=True)
def _ledger_is_isolated(tmp_path_factory, monkeypatch):
    cd = tmp_path_factory.mktemp("s4-cost")
    monkeypatch.setenv("SKAI_COST_DIR", str(cd))
    assert autopilot_cost.cost_dir() == cd


@pytest.fixture(autouse=True)
def _fake_journal(monkeypatch):
    monkeypatch.setattr(orch, "journal", SimpleNamespace(
        read_run=lambda rid: {}, write_run=lambda rid, d: None,
        handle=lambda rid: SimpleNamespace()))


def _wi(ref, repo="skos", **payload):
    p = {"id": ref, "tags": [f"repo:{repo}"]}
    p.update(payload)
    return WorkItem(kind="engineering", ref=ref, source="coord", repo=repo, payload=p)


class _RunExec:
    kind = "engineering"

    def __init__(self, result=None, run_exc=None):
        self.run = (MagicMock(side_effect=run_exc) if run_exc is not None
                    else MagicMock(return_value=result))
        self.finalize = MagicMock()
        self.escalate = MagicMock(return_value=DecisionItem(
            qid="e", prompt="stuck", options={}, action_ref="t", priority="high"))

    def selectable(self, item):
        return True


def _harness(name="claude-code", model="ornith-big"):
    return SimpleNamespace(name=name, model=model,
                           assess=lambda brief: Verdict(verdict="valid", reason=""))


def _swarm(pairs, *, run_id, ledger=None, enabled=True, harness=None, decisions=None):
    return orch.phase2_swarm(pairs, harness=harness or _harness(), board=MagicMock(),
                             caps=Caps(), ledger=ledger or CapLedger(Caps()),
                             decisions=decisions if decisions is not None else [],
                             run_id=run_id, workers=1)


def _rows():
    return autopilot_cost._read_ledger()


def _settled() -> bool:
    """True if the wallet settlement journal exists at all. settle() writes it
    only on a twin-gate pass, so this is the observation that distinguishes
    'recorded from the orchestrator' from 'recorded at settle'."""
    return autopilot_cost.settlements_path().exists()


# --------------------------------------------------------------------------- #
# THE LOAD-BEARING TEST: a FAILED build produces an outcome row.              #
# --------------------------------------------------------------------------- #


def test_a_failed_build_writes_exactly_one_outcome_row():
    """The assertion this whole card exists for. settle() is pass-only, so
    before this wiring a failed build's cost and terminal state were recorded
    in NO store at all: engineering.py took the usage off the books and the
    ledger never saw a row."""
    ex = _RunExec(GateResult(score=2, passed=False, notes="did not converge",
                             artifact=None, outcome="ci_red",
                             tokens=4321, cost_usd=0.75))
    state = _swarm([(_wi("t-fail"), ex)], run_id="r-fail")

    assert state["t-fail"]["state"] == "escalated"
    ex.finalize.assert_not_called()          # settle is unreachable on this path
    assert _settled() is False

    rows = _rows()
    assert len(rows) == 1, "a failed build must produce exactly one outcome row"
    row = rows[0]
    assert row["card_id"] == "t-fail"
    assert row["outcome"] == "ci_red"        # the real terminal state, not a null
    assert row["passed"] is False
    assert row["terminal_state"] == "escalated"
    assert row["tokens"] == 4321
    assert row["cost_usd"] == 0.75
    assert row["score"] == 2
    assert row["run_id"] == "r-fail"


def test_a_failed_build_row_carries_a_non_null_outcome():
    """A row with a null outcome is indistinguishable from a run that had no
    outcome; that ambiguity is the exact defect this epic removes."""
    ex = _RunExec(GateResult(score=None, passed=False, notes="no diff",
                             artifact=None, outcome="no_op", tokens=10,
                             cost_usd=0.01))
    _swarm([(_wi("t-noop"), ex)], run_id="r-noop")
    row = _rows()[0]
    assert row["outcome"] is not None
    assert row["outcome"] == "no_op"


# --------------------------------------------------------------------------- #
# The other two branches that funnel through the single guarded write site.   #
# --------------------------------------------------------------------------- #


def test_finalized_writes_a_row_from_the_orchestrator():
    ex = _RunExec(GateResult(score=5, passed=True, notes="ok", artifact="pr#1",
                             outcome="pass", tokens=900, cost_usd=0.2))
    state = _swarm([(_wi("t-ok"), ex)], run_id="r-ok")
    assert state["t-ok"]["state"] == "finalized"
    row = _rows()[0]
    assert row["terminal_state"] == "finalized"
    assert row["outcome"] == "pass" and row["passed"] is True
    assert row["tokens"] == 900


def test_finalize_failed_writes_a_row():
    ex = _RunExec(GateResult(score=5, passed=True, notes="ok", artifact="pr#1",
                             outcome="pass", tokens=7, cost_usd=0.01))
    ex.finalize = MagicMock(side_effect=RuntimeError("gh pr create exploded"))
    decisions: list = []
    state = _swarm([(_wi("t-ff"), ex)], run_id="r-ff", decisions=decisions)
    assert state["t-ff"]["state"] == "finalize-failed"
    row = _rows()[0]
    assert row["terminal_state"] == "finalize-failed"
    # The GATE passed; the finalize did not. Both facts survive, separately.
    assert row["outcome"] == "pass" and row["passed"] is True


# --------------------------------------------------------------------------- #
# The three bypasses. Without these the union is not a union.                 #
# --------------------------------------------------------------------------- #


def test_claim_raced_writes_a_row():
    ex = _RunExec(run_exc=ClaimRaced("claimed by autopilot-100"))
    state = _swarm([(_wi("t-race"), ex)], run_id="r-race")
    assert state["t-race"]["state"] == "claim-raced"
    rows = _rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["terminal_state"] == "claim-raced"
    # No build ran, so there is no GATE outcome. UNRECORDED says exactly that,
    # and it is deliberately not "pass" and deliberately not a bare null.
    assert row["outcome"] == UNRECORDED
    assert row["passed"] is False
    assert row["tokens"] == 0 and row["cost_usd"] == 0.0
    assert row["score"] is None
    assert _settled() is False


def test_kill_switch_writes_a_row_per_unstarted_item(monkeypatch):
    monkeypatch.setenv("SKOS_AUTOPILOT_OFF", "1")
    ex = _RunExec(GateResult(score=5, passed=True, notes="ok", artifact="pr"))
    state = _swarm([(_wi("t-k1"), ex), (_wi("t-k2"), ex)], run_id="r-kill")
    ex.run.assert_not_called()                      # nothing was built
    assert state == {}                              # and no state was invented
    rows = _rows()
    assert [r["card_id"] for r in rows] == ["t-k1", "t-k2"]
    assert all(r["terminal_state"] == "kill-switch" for r in rows)
    assert all(r["outcome"] == UNRECORDED for r in rows)


def test_budget_hit_writes_a_row_per_stopped_item():
    ledger = CapLedger(Caps(max_tokens_per_run=100))
    ledger.tokens = 200                              # already over the ceiling
    ex = _RunExec(GateResult(score=5, passed=True, notes="ok", artifact="pr"))
    decisions: list = []
    state = _swarm([(_wi("t-b1"), ex), (_wi("t-b2"), ex)], run_id="r-budget",
                   ledger=ledger, decisions=decisions)
    ex.run.assert_not_called()
    assert state == {}
    assert len(decisions) == 1                       # only the first item escalates
    rows = _rows()
    assert [r["card_id"] for r in rows] == ["t-b1", "t-b2"]   # but BOTH get a row
    assert all(r["terminal_state"] == "budget-hit" for r in rows)


def test_off_node_writes_a_row():
    """Off-node items never enter phase2_swarm, so the row has to be written in
    run_once where the placement decision is made."""
    item = _wi("t-remote")
    decision = DispatchDecision(ref=item.ref, node="node-100",
                                reason="repo affinity")
    state: dict = {}
    orch.record_off_node_rows([(item, decision)], state=state,
                              run_id="r-off", harness=_harness())
    rows = _rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["card_id"] == "t-remote"
    assert row["terminal_state"] == "off-node"
    assert row["outcome"] == UNRECORDED
    assert state["t-remote"] == {"state": "off-node", "node": "node-100",
                                 "reason": "repo affinity"}


def test_off_node_row_is_written_by_run_once(tmp_path, monkeypatch):
    """The wired path, end to end: run_once partitions off-node and the row
    appears. Asserting the helper alone would not prove run_once calls it."""
    import json

    (tmp_path / "t-remote.json").write_text(json.dumps({
        "id": "t-remote", "title": "t", "description": "", "tags": ["repo:skos"],
        "acceptance_criteria": ["works"], "dependencies": [], "status": "open"}))
    board = MagicMock()
    board.unblocked_task_ids.return_value = {"t-remote"}
    ex = _RunExec(GateResult(score=5, passed=True, notes="ok", artifact="pr"))
    cfg = SimpleNamespace(enabled=True, dry_run=False, caps=Caps(),
                          repo_map={"skos": object()}, fleet_dispatch=True,
                          cleanup_after_run="off")
    monkeypatch.setattr(orch, "phase3_report", lambda decisions, **kw: {})
    out = orch.run_once(board=board, harness=_harness(), config=cfg,
                        tasks_dir=tmp_path, run_id="r-off2",
                        executors={"engineering": ex},
                        placer=lambda it: DispatchDecision(
                            ref=it.ref, node="node-100", reason="repo affinity"))
    assert [d["ref"] for d in out["off_node"]] == ["t-remote"]
    ex.run.assert_not_called()
    rows = _rows()
    assert len(rows) == 1 and rows[0]["terminal_state"] == "off-node"


# --------------------------------------------------------------------------- #
# One row per item per run, and the S3 field contract.                        #
# --------------------------------------------------------------------------- #


def test_exactly_one_row_per_item_per_run():
    ex = _RunExec(GateResult(score=5, passed=True, notes="ok", artifact="pr",
                             outcome="pass", tokens=1, cost_usd=0.0))
    _swarm([(_wi("t-1"), ex), (_wi("t-2"), ex), (_wi("t-3"), ex)], run_id="r-many")
    rows = _rows()
    assert len(rows) == 3
    assert sorted(r["card_id"] for r in rows) == ["t-1", "t-2", "t-3"]


def test_row_carries_the_wired_adapter_model_quality_and_grade():
    """Every one of these comes off the LIVE path (the GateResult the executor
    returned, or the payload the orchestrator itself normalized). Nothing here
    is hand-injected by the test into the recorder."""
    grade = {"size": "M", "risk": "high", "sensitivity": "internal",
             "model_class": "sk-m"}
    item = _wi("t-graded", work_grade=grade, quality="gated")
    ex = _RunExec(GateResult(score=5, passed=True, notes="ok", artifact="pr",
                             mode="gated", outcome="pass", tokens=5, cost_usd=0.1))
    _swarm([(item, ex)], run_id="r-fields",
           harness=_harness(name="pi", model="ornith-big"))
    row = _rows()[0]
    assert row["adapter"] == "pi"
    assert row["model_requested"] == "ornith-big"
    assert row["quality_mode"] == "gated"
    assert row["work_grade"] == grade
    assert row["retries"] == 0


def test_model_served_is_never_defaulted_from_model_requested():
    """Negative control inherited from S3: the orchestrator does not observe
    what skgateway actually served, so it must write None rather than echo the
    request. Defaulting it would manufacture the exact fact the field exists to
    detect (the .100 outage silently served a cloud model for sk-default)."""
    ex = _RunExec(GateResult(score=1, passed=False, notes="x", artifact=None,
                             outcome="ci_red"))
    _swarm([(_wi("t-served"), ex)], run_id="r-served",
           harness=_harness(model="ornith-big"))
    row = _rows()[0]
    assert row["model_requested"] == "ornith-big"
    assert row["model_served"] is None


def test_a_recorder_bug_never_breaks_a_build(monkeypatch):
    """The recorder is telemetry. If it explodes, the build still finishes."""
    monkeypatch.setattr(autopilot_cost, "record_run",
                        MagicMock(side_effect=RuntimeError("ledger on fire")))
    ex = _RunExec(GateResult(score=5, passed=True, notes="ok", artifact="pr",
                             outcome="pass"))
    state = _swarm([(_wi("t-boom"), ex)], run_id="r-boom")
    assert state["t-boom"]["state"] == "finalized"
