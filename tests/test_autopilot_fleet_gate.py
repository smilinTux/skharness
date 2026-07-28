"""run_once fleet gate + claim-race guard (Card 2.2 acceptance)."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from skharness.autocode import fleet_dispatch as fd
from skharness.autocode import orchestrator as orch
from skharness.autocode.config import Caps
from skharness.autocode.types import ClaimRaced, GateResult, Verdict, WorkItem


@pytest.fixture(autouse=True)
def _hermetic_journal_and_gtd(monkeypatch, tmp_path):
    """These tests drive orch.run_once for real (dry_run=False), which writes
    the run journal and (with any decisions) the GTD digest. Keep both off
    the live ~/.skcapstone tree, mirroring test_autopilot_journal.py."""
    monkeypatch.setenv("SK_AUTOPILOT_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("SK_GTD_DIR", str(tmp_path / "gtd"))


def _write_task(d, tid, tags=None):
    t = {"id": tid, "title": tid, "description": "d",
         "tags": tags or ["repo:skos"], "acceptance_criteria": ["works"],
         "dependencies": [], "status": "open"}
    (d / f"{tid}.json").write_text(json.dumps(t))


def _board(unblocked):
    b = MagicMock()
    b.unblocked_task_ids.return_value = set(unblocked)
    return b


def _config(**kw):
    base = dict(enabled=True, dry_run=False, caps=Caps(), repo_map={"skos": object()},
                fleet_dispatch=True, cleanup_after_run="off")
    base.update(kw)
    return SimpleNamespace(**base)


class _RunExec:
    kind = "engineering"

    def __init__(self):
        self.ran = []

    def selectable(self, item):
        return True

    def run(self, item, harness):
        self.ran.append(item.ref)
        return GateResult(score=5, passed=True, notes="ok", artifact="pr#1")

    def finalize(self, item, result):
        pass

    def escalate(self, item, reason):
        raise AssertionError("escalate not expected in these tests")


def _placer_from_views(views):
    """A real scheduler query over synthetic views (no live fleet needed)."""
    from skcapstone.fleet import scheduler as fsched

    def placer(item):
        wl = fsched.Workload(kind="job", name=item.ref,
                             node_selector=fd.card_selector(
                                 item.payload.get("tags") or []))
        d = fsched.select(views, wl)
        return fd.DispatchDecision(ref=item.ref, node=d.node, reason=d.reason)

    return placer


@pytest.mark.needs_skcapstone
@pytest.mark.parametrize("phase,cordoned", [("Dead", False), ("Ready", True)])
def test_dead_or_cordoned_heavy_node_routes_builds_here(tmp_path, monkeypatch,
                                                        phase, cordoned):
    """Card 2.2 acceptance: with node-41 cordoned or Dead, a run places all
    schedulable builds on node-158 with no config edit; a heavy-build card
    (selector only matches node-41) is skipped, never run on the wrong node."""
    from skcapstone.fleet.node_controller import NodeView

    views = [
        NodeView(name="node-158", phase="Ready",
                 allocatable={"cores": 7, "ram_gb": 12.0, "disk_gb": 100.0}),
        NodeView(name="node-41", phase=phase, cordoned=cordoned,
                 labels={"heavy-build": "true"},
                 allocatable={"cores": 15, "ram_gb": 24.0, "disk_gb": 200.0}),
    ]
    _write_task(tmp_path, "t-plain")
    _write_task(tmp_path, "t-heavy", tags=["repo:skos", "node:heavy-build"])
    ex = _RunExec()
    board = _board(["t-plain", "t-heavy"])
    harness = SimpleNamespace(name="h",
                              assess=lambda b: Verdict(verdict="valid", reason=""))
    monkeypatch.setenv("SKFLEET_NODE", "node-158")
    out = orch.run_once(board=board, harness=harness, config=_config(),
                        tasks_dir=tmp_path, run_id="r1", dry_run=False,
                        executors={"engineering": ex},
                        placer=_placer_from_views(views))
    assert ex.ran == ["t-plain"]
    assert out["selected"] == ["t-plain"]
    assert [(o["ref"], o["node"]) for o in out["off_node"]] == [("t-heavy", None)]


@pytest.mark.needs_skcapstone
def test_heavy_build_card_lands_on_heavy_node(tmp_path, monkeypatch):
    """With both nodes Ready, the heavy-build selector card is placed on
    node-41 (filtering, not preference) and skipped by the node-158 run."""
    from skcapstone.fleet.node_controller import NodeView

    views = [
        NodeView(name="node-158", phase="Ready",
                 allocatable={"cores": 7, "ram_gb": 24.0, "disk_gb": 100.0}),
        NodeView(name="node-41", phase="Ready", labels={"heavy-build": "true"},
                 allocatable={"cores": 15, "ram_gb": 12.0, "disk_gb": 200.0}),
    ]
    _write_task(tmp_path, "t-plain")
    _write_task(tmp_path, "t-heavy", tags=["repo:skos", "node:heavy-build"])
    ex = _RunExec()
    board = _board(["t-plain", "t-heavy"])
    harness = SimpleNamespace(name="h",
                              assess=lambda b: Verdict(verdict="valid", reason=""))
    monkeypatch.setenv("SKFLEET_NODE", "node-158")
    out = orch.run_once(board=board, harness=harness, config=_config(),
                        tasks_dir=tmp_path, run_id="r1", dry_run=False,
                        executors={"engineering": ex},
                        placer=_placer_from_views(views))
    assert ex.ran == ["t-plain"]
    assert [(o["ref"], o["node"]) for o in out["off_node"]] == [("t-heavy", "node-41")]


def test_no_placer_and_no_fleet_keeps_current_behavior(tmp_path):
    """Gate inert (hermetic empty fleet root): everything runs locally."""
    _write_task(tmp_path, "t-1")
    ex = _RunExec()
    board = _board(["t-1"])
    harness = SimpleNamespace(name="h",
                              assess=lambda b: Verdict(verdict="valid", reason=""))
    out = orch.run_once(board=board, harness=harness, config=_config(),
                        tasks_dir=tmp_path, run_id="r1", dry_run=False,
                        executors={"engineering": ex})
    assert ex.ran == ["t-1"] and out["off_node"] == []


def test_lost_claim_raises_claim_raced():
    """The coord claim stays the execution gate: a card already claimed by
    another node's autopilot raises ClaimRaced instead of double-running."""
    from skharness.autocode.engineering import EngineeringExecutor

    board = MagicMock()
    board.claim_task.side_effect = ValueError(
        "Task t-1 already claimed by autopilot-node-41")
    ex = EngineeringExecutor(_config(), board, MagicMock(),
                             agent_name="autopilot-node-158")
    item = WorkItem(kind="engineering", ref="t-1", source="coord", repo="skos",
                    payload={"tags": ["repo:skos"]})
    with pytest.raises(ClaimRaced):
        ex.claim(item)
    board.claim_task.assert_called_once_with("autopilot-node-158", "t-1")


def test_swarm_records_claim_race_as_skip(monkeypatch):
    """A lost race is journaled as a skip: no escalation, no crash, no build."""
    class _Racing(_RunExec):
        def run(self, item, harness):
            raise ClaimRaced("t-1: already claimed by autopilot-node-41")

    monkeypatch.setattr(orch.journal, "write_run", lambda *a, **k: None)
    item = WorkItem(kind="engineering", ref="t-1", source="coord", repo="skos",
                    payload={"tags": []})
    decisions = []
    state = orch.phase2_swarm([(item, _Racing())], harness=MagicMock(),
                              board=MagicMock(), caps=Caps(max_concurrent=1),
                              ledger=orch.CapLedger(Caps()), decisions=decisions,
                              run_id="r1", enabled=True)
    assert state["t-1"]["state"] == "claim-raced"
    assert decisions == []
