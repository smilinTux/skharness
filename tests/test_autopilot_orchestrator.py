"""Autopilot orchestrator: helpers, phases, run_once, dry-run, kill switch, caps, resume."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from skharness.autocode import orchestrator as orch
from skharness.autocode.executor import EXECUTORS
from skharness.autocode.orchestrator import Caps, CapLedger, kill_switch_active, stable_qid
from skharness.autocode.types import Verdict, WorkItem, GateResult, DecisionItem
from skharness.autocode.grounding import Grounding as _Grounding


def test_kill_switch_env(monkeypatch):
    monkeypatch.setenv("SKOS_AUTOPILOT_OFF", "1")
    assert kill_switch_active(enabled=True) is True


def test_kill_switch_disabled_flag(monkeypatch):
    monkeypatch.delenv("SKOS_AUTOPILOT_OFF", raising=False)
    assert kill_switch_active(enabled=False) is True
    assert kill_switch_active(enabled=True) is False


def test_cap_ledger_exceeded_on_tokens():
    led = CapLedger(Caps(max_tokens_per_run=100, max_usd_per_day=10.0))
    led.add(tokens=60, usd=1.0)
    assert led.exceeded() is False
    led.add(tokens=60, usd=1.0)
    assert led.exceeded() is True


def test_cap_ledger_exceeded_on_usd():
    led = CapLedger(Caps(max_tokens_per_run=10_000, max_usd_per_day=2.0))
    led.add(tokens=1, usd=2.5)
    assert led.exceeded() is True


def test_stable_qid_deterministic():
    a = stable_qid("Merge PR #12 for task X?", "task-x")
    b = stable_qid("Merge PR #12 for task X?", "task-x")
    c = stable_qid("Merge PR #12 for task X?", "task-y")
    assert a == b and a != c and len(a) == 12


def _write_task(d, tid, **fields):
    t = {"id": tid, "title": tid, "description": "", "tags": [],
         "acceptance_criteria": [], "dependencies": [], "status": "open"}
    t.update(fields)
    (d / f"{tid}-x.json").write_text(json.dumps(t))
    return t


def _board(unblocked):
    b = MagicMock()
    b.unblocked_task_ids.return_value = set(unblocked)
    return b


def test_phase0_reclaims_then_computes_unblocked(tmp_path):
    _write_task(tmp_path, "t-1", tags=["repo:skos"], acceptance_criteria=["works"])
    _write_task(tmp_path, "t-2", tags=["repo:skos"])
    board = _board(["t-1"])
    harness = MagicMock()
    harness.assess.return_value = Verdict(verdict="valid", reason="")
    cands, decisions = orch.phase0_assess(board=board, harness=harness, tasks_dir=tmp_path,
                                          caps=Caps(), run_id="r1")
    calls = [c.args for c in board.release_stale_claims.call_args_list]
    assert ("autopilot", 3600) in calls          # legacy name still reclaimed
    assert [c.ref for c in cands] == ["t-1"]          # only unblocked assessed
    assert decisions == []


def test_phase0_skips_obsolete_marked_cards(tmp_path):
    # A card closed via close_task_obsolete carries meta.autopilot.obsolete (task
    # files have no status field). phase0 must skip it, so neither a stale-sweep
    # nor the engine's own obsolete closures get re-assessed every run.
    _write_task(tmp_path, "t-live", tags=["repo:skos"], acceptance_criteria=["w"])
    _write_task(tmp_path, "t-dead", tags=["repo:skos"], acceptance_criteria=["w"],
                meta={"autopilot": {"obsolete": {"reason": "already on main", "ts": "x"}}})
    board = _board(["t-live", "t-dead"])
    harness = MagicMock()
    harness.assess.return_value = Verdict(verdict="valid", reason="")
    cands, _ = orch.phase0_assess(board=board, harness=harness, tasks_dir=tmp_path,
                                  caps=Caps(), run_id="r")
    assert [c.ref for c in cands] == ["t-live"]       # obsolete card skipped
    assert harness.assess.call_count == 1             # t-dead never even assessed


def _decompose_board(tmp_path, tid="t-vague", **taskkw):
    _write_task(tmp_path, tid, tags=["repo:skos"], acceptance_criteria=["do the thing"],
                **taskkw)
    board = _board([tid])
    created = []
    board.create_task.side_effect = lambda task: created.append(task)
    return board, created


def test_phase0_decompose_verdict_creates_children_and_parks_parent(tmp_path, mocker):
    board, created = _decompose_board(tmp_path)
    harness = MagicMock()
    harness.assess.return_value = Verdict(verdict="decompose", reason="too coarse")
    harness.decompose.return_value = [
        {"title": "sub A", "description": "", "acceptance": ["a.py has f"]},
        {"title": "sub B", "description": "", "acceptance": ["b.py has g"]},
    ]
    # avoid real git grounding: force ungrounded so the model verdict stands
    mocker.patch("skharness.autocode.orchestrator._ground_card",
                 return_value=_Grounding(grounded=False))
    cands, decisions = orch.phase0_assess(board=board, harness=harness, tasks_dir=tmp_path,
                                          caps=Caps(), run_id="r", only_ids=["t-vague"])
    assert [c.ref for c in cands] == []            # parent is NOT built
    assert len(created) == 2                        # two children created
    for child in created:
        assert "autopilot-untriaged" in child.tags  # human-release gate
        assert "parent:t-vague" in child.tags
    board.mark_decomposed.assert_called_once()      # parent parked


def test_phase0_decompose_incoherent_children_escalate_not_created(tmp_path, mocker):
    # COHERENCE GATE: a decompose that emits foreign-language subtasks (Go in a
    # Python repo) means the model misread the repo -> route to a human, create NO
    # garbage children.
    board, created = _decompose_board(tmp_path)
    harness = MagicMock()
    harness.assess.return_value = Verdict(verdict="decompose", reason="coarse")
    harness.decompose.return_value = [
        {"title": "Add OpsExecutor struct", "acceptance": ["ops_executor.go exists"]},
        {"title": "fine one", "acceptance": ["src/x.py has f"]},
    ]
    mocker.patch("skharness.autocode.orchestrator._ground_card",
                 return_value=_Grounding(grounded=False))
    # profile says python -> the .go/struct child is incoherent
    mocker.patch("skharness.autocode.grounding.repo_profile",
                 return_value={"language": "python", "ext": ".py",
                               "foreign_ext": [".go"], "foreign_terms": [r"\bstruct\b"]})
    cfg = SimpleNamespace(repo=lambda n: SimpleNamespace(path="/x", base_branch="main"))
    cands, decisions = orch.phase0_assess(board=board, harness=harness, tasks_dir=tmp_path,
                                          caps=Caps(), run_id="r", config=cfg,
                                          only_ids=["t-vague"])
    assert created == []                              # no garbage children created
    assert len(decisions) == 1                        # escalated to human
    board.mark_decomposed.assert_not_called()


def test_phase0_decompose_empty_escalates_not_drops(tmp_path, mocker):
    board, created = _decompose_board(tmp_path)
    harness = MagicMock()
    harness.assess.return_value = Verdict(verdict="decompose", reason="too coarse")
    harness.decompose.return_value = []             # inconclusive
    mocker.patch("skharness.autocode.orchestrator._ground_card",
                 return_value=_Grounding(grounded=False))
    cands, decisions = orch.phase0_assess(board=board, harness=harness, tasks_dir=tmp_path,
                                          caps=Caps(), run_id="r", only_ids=["t-vague"])
    assert created == []
    assert len(decisions) == 1                       # escalated to human, not dropped
    board.mark_decomposed.assert_not_called()


def test_phase0_decompose_depth_ceiling_escalates(tmp_path, mocker):
    board, created = _decompose_board(
        tmp_path, meta={"autopilot": {"decomp_depth": 2}})
    harness = MagicMock()
    harness.assess.return_value = Verdict(verdict="decompose", reason="too coarse")
    mocker.patch("skharness.autocode.orchestrator._ground_card",
                 return_value=_Grounding(grounded=False))
    _cands, decisions = orch.phase0_assess(board=board, harness=harness, tasks_dir=tmp_path,
                                           caps=Caps(max_decompose_depth=2), run_id="r",
                                           only_ids=["t-vague"])
    harness.decompose.assert_not_called()            # at ceiling: never split again
    assert len(decisions) == 1


def test_phase0_concreteness_gate_downgrades_valid_to_decompose(tmp_path, mocker):
    board, created = _decompose_board(tmp_path)
    harness = MagicMock()
    harness.assess.return_value = Verdict(verdict="valid", reason="looks fine")  # model says valid
    harness.decompose.return_value = [{"title": "sub", "acceptance": ["x.py"]}]
    # grounded, low concreteness, not net_new -> the gate must downgrade valid->decompose
    mocker.patch("skharness.autocode.orchestrator._ground_card",
                 return_value=_Grounding(grounded=True, concreteness=0.0, net_new=False,
                                         context="REPO FACTS"))
    cands, _ = orch.phase0_assess(board=board, harness=harness, tasks_dir=tmp_path,
                                  caps=Caps(), run_id="r", only_ids=["t-vague"])
    assert [c.ref for c in cands] == []             # NOT built as valid
    harness.decompose.assert_called_once()           # routed to decompose instead
    board.mark_decomposed.assert_called_once()


# -- decompose create-or-skip guard (regression for the 2026-08-03 mass pass that
#    re-decomposed already-decomposed epics and created exact-title duplicates) ----

def test_phase0_decompose_skips_epic_that_already_has_children(tmp_path, mocker):
    # Epic already hand-carded children (tagged parent:<epic>) but carries NO
    # meta.autopilot.decomposed flag. Re-decomposing it must be a no-op: no new
    # children, decompose() never even runs, parent never re-parked.
    board, created = _decompose_board(tmp_path)
    _write_task(tmp_path, "child-1", tags=["parent:t-vague", "autopilot"], title="sub A")
    harness = MagicMock()
    harness.assess.return_value = Verdict(verdict="decompose", reason="too coarse")
    harness.decompose.return_value = [
        {"title": "sub A", "acceptance": ["a.py has f"]},
        {"title": "sub B", "acceptance": ["b.py has g"]},
    ]
    mocker.patch("skharness.autocode.orchestrator._ground_card",
                 return_value=_Grounding(grounded=False))
    cands, decisions = orch.phase0_assess(board=board, harness=harness, tasks_dir=tmp_path,
                                          caps=Caps(), run_id="r", only_ids=["t-vague"])
    assert created == []                             # no duplicate children created
    harness.decompose.assert_not_called()            # short-circuited before splitting
    board.mark_decomposed.assert_not_called()        # epic not re-parked


def test_phase0_decompose_child_skips_duplicate_title(tmp_path, mocker):
    # A human hand-carded ONE of the subtasks as a top-level (unlinked) card. The
    # epic has no parent:<epic> children, so the epic-level guard passes, but the
    # per-child create-or-skip must skip the colliding title (case/space
    # insensitive) and still create the genuinely-new sibling.
    board, created = _decompose_board(tmp_path)
    _write_task(tmp_path, "hand-a", tags=["repo:skos"], title="Sub  A")   # unlinked dup
    harness = MagicMock()
    harness.assess.return_value = Verdict(verdict="decompose", reason="too coarse")
    harness.decompose.return_value = [
        {"title": "sub a", "description": "", "acceptance": ["a.py has f"]},   # dup -> skip
        {"title": "sub B", "description": "", "acceptance": ["b.py has g"]},   # new -> create
    ]
    mocker.patch("skharness.autocode.orchestrator._ground_card",
                 return_value=_Grounding(grounded=False))
    cands, decisions = orch.phase0_assess(board=board, harness=harness, tasks_dir=tmp_path,
                                          caps=Caps(), run_id="r", only_ids=["t-vague"])
    assert [c.title for c in created] == ["sub B"]   # only the non-duplicate created
    board.mark_decomposed.assert_called_once()
    parked_children = board.mark_decomposed.call_args.args[1]
    assert len(parked_children) == 1                 # parent parked with just the new child


def test_phase0_decompose_new_epic_still_creates_all_children(tmp_path, mocker):
    # Genuinely-new epic (no existing children, no title collisions): the guard is
    # inert and every child is created + the parent parked.
    board, created = _decompose_board(tmp_path)
    harness = MagicMock()
    harness.assess.return_value = Verdict(verdict="decompose", reason="too coarse")
    harness.decompose.return_value = [
        {"title": "fresh A", "acceptance": ["a.py has f"]},
        {"title": "fresh B", "acceptance": ["b.py has g"]},
    ]
    mocker.patch("skharness.autocode.orchestrator._ground_card",
                 return_value=_Grounding(grounded=False))
    cands, decisions = orch.phase0_assess(board=board, harness=harness, tasks_dir=tmp_path,
                                          caps=Caps(), run_id="r", only_ids=["t-vague"])
    assert sorted(c.title for c in created) == ["fresh A", "fresh B"]
    for child in created:
        assert "parent:t-vague" in child.tags


# -- decompose flood guard (regression for the 2026-08-06/07 mass pass that emitted
#    ~821 untriaged children board-wide, 164 of them with no repo) -----------------

def test_phase0_unscoped_decompose_queues_scope_decision_not_children(tmp_path, mocker):
    # FIX C1: an UNSCOPED (daily/board-wide) run must NEVER carpet-split. A decompose
    # verdict becomes a "scope it" decision, no children, so the bare triage that
    # flooded the board can no longer emit anything.
    board, created = _decompose_board(tmp_path)
    harness = MagicMock()
    harness.assess.return_value = Verdict(verdict="decompose", reason="too coarse")
    harness.decompose.return_value = [{"title": "sub A", "acceptance": ["a.py has f"]}]
    mocker.patch("skharness.autocode.orchestrator._ground_card",
                 return_value=_Grounding(grounded=False))
    cands, decisions = orch.phase0_assess(board=board, harness=harness, tasks_dir=tmp_path,
                                          caps=Caps(), run_id="r")   # UNSCOPED
    assert created == []                              # nothing emitted board-wide
    harness.decompose.assert_not_called()             # never even split
    assert [d.action_ref for d in decisions] == ["t-vague"]   # queued for a scoped run
    board.mark_decomposed.assert_not_called()


def test_phase0_decompose_norepo_epic_queues_decision_not_children(tmp_path, mocker):
    # FIX A: an epic with no single repo:<name> tag cannot be routed to a codebase.
    # It must become a decision, never no-repo orphan children (the 164-orphan bug),
    # even when the run is properly scoped to it.
    _write_task(tmp_path, "t-norepo", tags=[], acceptance_criteria=["do it"])  # NO repo tag
    board = _board(["t-norepo"])
    created = []
    board.create_task.side_effect = lambda task: created.append(task)
    harness = MagicMock()
    harness.assess.return_value = Verdict(verdict="decompose", reason="too coarse")
    harness.decompose.return_value = [{"title": "sub", "acceptance": ["x"]}]
    mocker.patch("skharness.autocode.orchestrator._ground_card",
                 return_value=_Grounding(grounded=False))
    cands, decisions = orch.phase0_assess(board=board, harness=harness, tasks_dir=tmp_path,
                                          caps=Caps(), run_id="r", only_ids=["t-norepo"])
    assert created == []                              # no no-repo orphans created
    harness.decompose.assert_not_called()             # a no-repo epic is never split
    assert len(decisions) == 1 and "repo" in decisions[0].prompt.lower()


def test_phase0_decompose_run_budget_defers_epic_over_budget(tmp_path, mocker):
    # FIX C2: even a scoped --tag run spanning many epics cannot exceed the per-run
    # child budget. With budget=1 and an epic needing 2 children, the WHOLE epic is
    # deferred to a decision (never a partial split that would park the parent).
    _write_task(tmp_path, "e1", tags=["repo:skos", "big"], acceptance_criteria=["w"])
    board = _board(["e1"])
    created = []
    board.create_task.side_effect = lambda task: created.append(task)
    harness = MagicMock()
    harness.assess.return_value = Verdict(verdict="decompose", reason="coarse")
    harness.decompose.return_value = [
        {"title": "s1", "acceptance": ["a.py"]},
        {"title": "s2", "acceptance": ["b.py"]},
    ]
    mocker.patch("skharness.autocode.orchestrator._ground_card",
                 return_value=_Grounding(grounded=False))
    cands, decisions = orch.phase0_assess(
        board=board, harness=harness, tasks_dir=tmp_path,
        caps=Caps(max_decompose_children_per_run=1), run_id="r", only_tag="big")
    assert created == []                              # not partially split
    board.mark_decomposed.assert_not_called()         # parent not parked
    assert any("budget" in d.prompt.lower() for d in decisions)


def test_phase0_decompose_budget_allows_epic_that_fits(tmp_path, mocker):
    # FIX C2 (positive): an epic whose full child set fits the remaining budget is
    # split normally; the budget only defers epics that would overflow it.
    board, created = _decompose_board(tmp_path)
    harness = MagicMock()
    harness.assess.return_value = Verdict(verdict="decompose", reason="coarse")
    harness.decompose.return_value = [
        {"title": "sub A", "acceptance": ["a.py has f"]},
        {"title": "sub B", "acceptance": ["b.py has g"]},
    ]
    mocker.patch("skharness.autocode.orchestrator._ground_card",
                 return_value=_Grounding(grounded=False))
    cands, decisions = orch.phase0_assess(
        board=board, harness=harness, tasks_dir=tmp_path,
        caps=Caps(max_decompose_children_per_run=8), run_id="r", only_ids=["t-vague"])
    assert len(created) == 2                           # fits the budget: split proceeds
    board.mark_decomposed.assert_called_once()


# -- B2 staged "Proposed" lane: decomposed children are born staged (hidden from
#    OPEN/build) until `skos autopilot release <epic>` strips the stage --------------

def test_phase0_scoped_decompose_children_born_staged(tmp_path, mocker):
    board, created = _decompose_board(tmp_path)
    harness = MagicMock()
    harness.assess.return_value = Verdict(verdict="decompose", reason="coarse")
    harness.decompose.return_value = [{"title": "sub A", "acceptance": ["a.py has f"]}]
    mocker.patch("skharness.autocode.orchestrator._ground_card",
                 return_value=_Grounding(grounded=False))
    orch.phase0_assess(board=board, harness=harness, tasks_dir=tmp_path,
                       caps=Caps(), run_id="r", only_ids=["t-vague"])
    assert created                                       # split happened
    for c in created:
        assert "autopilot-staged" in c.tags              # born into the Proposed lane
        assert c.meta["autopilot"]["staged"] is True


def test_release_epic_strips_stage_from_children(tmp_path):
    board = MagicMock()
    tasks = [
        {"id": "c1", "tags": ["repo:skos", "parent:e1", "autopilot-staged",
                              "autopilot-untriaged"]},
        {"id": "c2", "tags": ["repo:skos", "parent:e1", "autopilot-staged"]},
        {"id": "c3", "tags": ["repo:skos", "parent:e1"]},              # already released
        {"id": "x9", "tags": ["parent:other", "autopilot-staged"]},   # different epic
    ]
    released = orch.release_epic("e1", board=board, tasks=tasks)
    assert released == ["c1", "c2"]                       # only staged children of e1
    calls = {c.args[0]: c.kwargs for c in board.update_task.call_args_list}
    assert set(calls) == {"c1", "c2"}
    for kw in calls.values():
        assert "autopilot-staged" in kw["remove_tags"]
        assert "autopilot-untriaged" in kw["remove_tags"]


def test_phase0_net_new_card_still_builds(tmp_path, mocker):
    board, created = _decompose_board(tmp_path)
    harness = MagicMock()
    harness.assess.return_value = Verdict(verdict="valid", reason="greenfield")
    mocker.patch("skharness.autocode.orchestrator._ground_card",
                 return_value=_Grounding(grounded=True, concreteness=0.0, net_new=True,
                                         context="REPO FACTS"))
    cands, _ = orch.phase0_assess(board=board, harness=harness, tasks_dir=tmp_path,
                                  caps=Caps(), run_id="r")
    assert [c.ref for c in cands] == ["t-vague"]     # net_new is concrete-by-intent -> builds
    harness.decompose.assert_not_called()


def test_run_once_triage_only_stops_before_build(tmp_path, mocker):
    # triage_only assesses/decomposes/closes but NEVER selects anything to build.
    _write_task(tmp_path, "t-1", tags=["repo:skos"], acceptance_criteria=["w"])
    board = _board(["t-1"])
    board.create_task.side_effect = lambda task: None
    harness = MagicMock()
    harness.assess.return_value = Verdict(verdict="valid", reason="ok")
    mocker.patch("skharness.autocode.orchestrator._ground_card",
                 return_value=_Grounding(grounded=False))
    swarm = mocker.patch("skharness.autocode.orchestrator.phase2_swarm")
    mocker.patch("skharness.autocode.orchestrator.phase3_report", return_value={})
    mocker.patch("skharness.autocode.orchestrator.journal.write_run")
    cfg = SimpleNamespace(enabled=True, repo_map={}, dry_run=False,
                          fleet_dispatch=False, caps=Caps())
    out = orch.run_once(board=board, harness=harness, config=cfg, tasks_dir=tmp_path,
                        run_id="r", dry_run=False, triage_only=True)
    assert out["triage_only"] is True
    assert out["candidates"] == 1                    # it DID assess
    swarm.assert_not_called()                         # but never built


def test_phase0_only_ids_scopes_to_the_batch(tmp_path):
    # A batch (--tasks / only_ids) assesses EXACTLY those ids, in the given order,
    # never the rest of the unblocked board.
    for i in "abc":
        _write_task(tmp_path, f"t-{i}", tags=["repo:skos"], acceptance_criteria=["works"])
    board = _board(["t-a", "t-b", "t-c"])
    harness = MagicMock()
    harness.assess.return_value = Verdict(verdict="valid", reason="")
    cands, _ = orch.phase0_assess(board=board, harness=harness, tasks_dir=tmp_path,
                                  caps=Caps(), run_id="r", only_ids=["t-c", "t-a"])
    assert [c.ref for c in cands] == ["t-c", "t-a"]   # exactly the batch, in order
    assert harness.assess.call_count == 2             # t-b never assessed


def test_phase0_only_tag_filters_unblocked(tmp_path):
    _write_task(tmp_path, "t-1", tags=["repo:skos", "autopilot"], acceptance_criteria=["w"])
    _write_task(tmp_path, "t-2", tags=["repo:skos"], acceptance_criteria=["w"])
    board = _board(["t-1", "t-2"])
    harness = MagicMock()
    harness.assess.return_value = Verdict(verdict="valid", reason="")
    cands, _ = orch.phase0_assess(board=board, harness=harness, tasks_dir=tmp_path,
                                  caps=Caps(), run_id="r", only_tag="autopilot")
    assert [c.ref for c in cands] == ["t-1"]          # only the tagged card


def test_phase0_applies_verdicts(tmp_path):
    _write_task(tmp_path, "stale", tags=["repo:skos"])
    _write_task(tmp_path, "dead", tags=["repo:skos"])
    _write_task(tmp_path, "ask", tags=["repo:skos"])
    board = _board(["stale", "dead", "ask"])
    harness = MagicMock()
    harness.assess.side_effect = [
        Verdict(verdict="needs_decision", reason="which repo?"),
        Verdict(verdict="obsolete", reason="superseded"),
        Verdict(verdict="stale", reason="drifted", updated_description="new",
                updated_acceptance=["a"]),
    ]
    cands, decisions = orch.phase0_assess(board=board, harness=harness, tasks_dir=tmp_path,
                                          caps=Caps(), run_id="r1")
    board.update_task.assert_called_once_with("stale", description="new",
                                              acceptance_criteria=["a"], run_id="r1")
    board.close_task_obsolete.assert_called_once_with("dead", "superseded", run_id="r1")
    assert {c.ref for c in cands} == {"stale"}         # stale rewritten stays actionable
    assert len(decisions) == 1 and decisions[0].action_ref == "ask"


def test_phase0_dry_run_writes_nothing(tmp_path):
    _write_task(tmp_path, "stale", tags=["repo:skos"])
    board = _board(["stale"])
    harness = MagicMock()
    harness.assess.return_value = Verdict(verdict="stale", reason="d", updated_description="n")
    orch.phase0_assess(board=board, harness=harness, tasks_dir=tmp_path,
                       caps=Caps(), run_id="r1", dry_run=True)
    board.update_task.assert_not_called()
    board.close_task_obsolete.assert_not_called()


def test_deepdive_spawn_caps_and_tags(tmp_path):
    board = MagicMock()
    props = [{"title": "a"}, {"title": "b"}, {"title": "c"}]
    made = orch.deepdive_spawn(board, props, caps=Caps(new_tasks_per_run=2), run_id="r1")
    assert len(made) == 2                               # capped at 2
    # create_task is called with a real Task object (not kwargs); each is untriaged
    calls = board.create_task.call_args_list
    assert len(calls) == 2
    for call in calls:
        task = call.args[0]
        assert "autopilot-untriaged" in task.tags
        assert task.id in made


def test_deepdive_spawn_dry_run_no_writes():
    board = MagicMock()
    orch.deepdive_spawn(board, [{"title": "a"}], caps=Caps(), run_id="r1", dry_run=True)
    board.create_task.assert_not_called()


class _Eng:
    kind = "engineering"
    def __init__(self, sel):
        self._sel = sel
        self.escalate = MagicMock()
    def selectable(self, item): return self._sel
    def run(self, item, harness): return GateResult(5, True, "", None)
    def finalize(self, item, result): pass


@pytest.fixture
def clean_execs():
    saved = dict(EXECUTORS)
    EXECUTORS.clear()
    yield
    EXECUTORS.clear()
    EXECUTORS.update(saved)


def _wi(ref, repo="skos", tags=None):
    return WorkItem(kind="engineering", ref=ref, source="coord", repo=repo,
                    payload={"id": ref, "tags": tags if tags is not None else [f"repo:{repo}"]})


def test_phase1_selects_only_selectable_in_scope(clean_execs):
    ex = _Eng(sel=True)
    EXECUTORS["engineering"] = ex
    decisions = []
    selected = orch.phase1_triage([_wi("t-1")], MagicMock(),
                                  repo_map={"skos": object()}, decisions=decisions)
    assert [i.ref for i, _ in selected] == ["t-1"] and decisions == []


def test_phase1_untriaged_never_selected(clean_execs):
    ex = _Eng(sel=True)
    EXECUTORS["engineering"] = ex
    decisions = []
    item = _wi("t-u", tags=["repo:skos", "autopilot-untriaged"])
    selected = orch.phase1_triage([item], MagicMock(),
                                  repo_map={"skos": object()}, decisions=decisions)
    assert selected == [] and decisions == []          # promoted by operator, not queued here


def test_phase1_unselectable_queues_without_escalate(clean_execs):
    ex = _Eng(sel=False)
    EXECUTORS["engineering"] = ex
    decisions = []
    selected = orch.phase1_triage([_wi("t-2")], MagicMock(),
                                  repo_map={"skos": object()}, decisions=decisions)
    assert selected == [] and len(decisions) == 1 and decisions[0].action_ref == "t-2"
    ex.escalate.assert_not_called()                     # escalate is only for mid-run gate fail


def test_phase1_unknown_repo_queues_decision(clean_execs):
    ex = _Eng(sel=True)
    EXECUTORS["engineering"] = ex
    decisions = []
    selected = orch.phase1_triage([_wi("t-3", repo="ghost")], MagicMock(),
                                  repo_map={"skos": object()}, decisions=decisions)
    assert selected == [] and len(decisions) == 1


def test_phase1_no_executor_queues_decision(clean_execs):
    decisions = []
    item = WorkItem(kind="research", ref="t-4", source="email", repo=None, payload={"id": "t-4", "tags": []})
    selected = orch.phase1_triage([item], MagicMock(), repo_map={}, decisions=decisions)
    assert selected == [] and len(decisions) == 1


@pytest.fixture(autouse=True)
def fake_journal(monkeypatch):
    writes = []
    ns = SimpleNamespace(read_run=lambda rid: {}, write_run=lambda rid, d: writes.append((rid, d)))
    monkeypatch.setattr(orch, "journal", ns)
    return writes


class _RunExec:
    kind = "engineering"
    def __init__(self, result):
        self._result = result
        self.run = MagicMock(return_value=result)
        self.finalize = MagicMock()
        self.escalate = MagicMock(return_value=DecisionItem(qid="e", prompt="stuck",
                                  options={}, action_ref="t", priority="high"))
    def selectable(self, item): return True


def test_phase2_finalizes_and_scores_on_pass():
    ex = _RunExec(GateResult(score=5, passed=True, notes="ok", artifact="pr#1"))
    board = MagicMock()
    harness = SimpleNamespace(name="claude-code")
    decisions = []
    state = orch.phase2_swarm([(_wi("t-1"), ex)], harness=harness, board=board,
                              caps=Caps(), ledger=CapLedger(Caps()), decisions=decisions,
                              run_id="r1")
    ex.run.assert_called_once()
    ex.finalize.assert_called_once()
    board.score_task.assert_not_called()
    assert state["t-1"]["state"] == "finalized" and decisions == []


def test_phase2_surfaces_finalize_failure_as_decision():
    # A gate-PASSED item whose finalize raises (CI re-check / PR open / merge) must
    # not vanish: the branch may already be pushed. It becomes an operator decision.
    ex = _RunExec(GateResult(score=5, passed=True, notes="ok", artifact="pr#1"))
    ex.finalize = MagicMock(side_effect=RuntimeError("gh pr create exploded"))
    board = MagicMock()
    decisions = []
    state = orch.phase2_swarm([(_wi("t-9"), ex)], harness=SimpleNamespace(name="h"),
                              board=board, caps=Caps(), ledger=CapLedger(Caps()),
                              decisions=decisions, run_id="r1")
    assert state["t-9"]["state"] == "finalize-failed"
    assert len(decisions) == 1
    assert "finalize failed" in decisions[0].prompt.lower()
    assert "gh pr create exploded" in decisions[0].prompt


def test_phase2_escalates_on_non_convergence():
    ex = _RunExec(GateResult(score=4, passed=False, notes="thin tests", artifact=None))
    board = MagicMock()
    decisions = []
    state = orch.phase2_swarm([(_wi("t-2"), ex)], harness=SimpleNamespace(name="h"),
                              board=board, caps=Caps(), ledger=CapLedger(Caps()),
                              decisions=decisions, run_id="r1")
    ex.finalize.assert_not_called()
    ex.escalate.assert_called_once()
    assert state["t-2"]["state"] == "escalated" and len(decisions) == 1


def _decision(qid="q1", prio="high"):
    return DecisionItem(qid=qid, prompt=f"Merge PR for {qid}?", options={"yes": "y", "no": "n"},
                        action_ref="task-x", priority=prio)


def test_write_decision_captures_source_autopilot(monkeypatch, tmp_path):
    monkeypatch.setenv("SK_GTD_DIR", str(tmp_path / "gtd"))
    import skos.gtd_ingest as gi
    orch.write_decision(_decision("q1"))
    items = json.loads((gi.gtd_dir() / "waiting-for.json").read_text())
    assert items[0]["source"] == "autopilot" and items[0]["source_ref"] == "autopilot:q1"
    assert items[0]["decision"]["qid"] == "q1" and items[0]["decision"]["answered"] is False


def test_write_decision_falls_back_to_upsert_on_dup(monkeypatch):
    cap = MagicMock(return_value=None)              # simulate duplicate
    ups = MagicMock(return_value=("id1", "unchanged"))
    monkeypatch.setattr("skos.gtd_ingest.capture", cap)
    monkeypatch.setattr("skos.gtd_ingest.upsert", ups)
    gid = orch.write_decision(_decision("q2"))
    cap.assert_called_once()
    ups.assert_called_once()                        # None -> upsert guarantees presence
    assert gid == "id1"


def test_phase3_dry_run_no_gtd_writes(monkeypatch):
    cap = MagicMock()
    monkeypatch.setattr("skos.gtd_ingest.capture", cap)
    out = orch.phase3_report([_decision("q3")], dry_run=True, digest_date="2026-07-12")
    cap.assert_not_called()
    assert out["dry_run"] is True and "digest_preview" in out


def test_phase3_writes_and_builds_manifest(monkeypatch, tmp_path):
    monkeypatch.setenv("SK_GTD_DIR", str(tmp_path / "gtd"))
    out = orch.phase3_report([_decision("q4")], dry_run=False, digest_date="2026-07-12")
    from skos.gtd_ingest import gtd_dir
    assert (gtd_dir() / "autopilot-digest.json").exists()
    assert out["manifest"]["items"][0]["qid"] == "q4"


def _config(**kw):
    base = dict(enabled=True, dry_run=False, caps=Caps(), repo_map={"skos": object()})
    base.update(kw)
    return SimpleNamespace(**base)


def test_run_once_full_pipeline(tmp_path, clean_execs):
    _write_task(tmp_path, "t-1", tags=["repo:skos"], acceptance_criteria=["works"])
    ex = _RunExec(GateResult(5, True, "ok", "pr#1"))
    EXECUTORS["engineering"] = ex
    board = _board(["t-1"])
    harness = SimpleNamespace(name="claude-code",
                              assess=lambda brief: Verdict(verdict="valid", reason=""))
    out = orch.run_once(board=board, harness=harness, config=_config(),
                        tasks_dir=tmp_path, run_id="r1", executors={"engineering": ex})
    ex.run.assert_called_once()
    ex.finalize.assert_called_once()
    assert out["selected"] == ["t-1"] and out["run_id"] == "r1"
    assert out["report"]["dry_run"] is False


def test_dry_run_is_read_only(tmp_path, monkeypatch, clean_execs, fake_journal):
    _write_task(tmp_path, "stale", tags=["repo:skos"], acceptance_criteria=["x"])
    ex = _RunExec(GateResult(5, True, "ok", "pr#1"))
    EXECUTORS["engineering"] = ex
    board = _board(["stale"])
    board.create_task = MagicMock()
    harness = SimpleNamespace(name="h",
        assess=lambda brief: Verdict(verdict="stale", reason="d", updated_description="n"))
    cap = MagicMock()
    monkeypatch.setattr("skos.gtd_ingest.capture", cap)

    out = orch.run_once(board=board, harness=harness, config=_config(dry_run=True),
                        tasks_dir=tmp_path, run_id="rdry",
                        deepdive_proposals=[{"title": "new"}], executors={"engineering": ex})

    board.update_task.assert_not_called()           # no coord mutation
    board.close_task_obsolete.assert_not_called()
    board.create_task.assert_not_called()
    board.score_task.assert_not_called()
    ex.run.assert_not_called()                       # Phase 2 skipped
    cap.assert_not_called()                          # no GTD write
    assert out["dry_run"] is True
    assert out["report"]["dry_run"] is True and "digest_preview" in out["report"]
    assert any(rid == "rdry" for rid, _ in fake_journal)  # journal entry written


def test_kill_switch_stops_before_swarm(tmp_path, monkeypatch, clean_execs, fake_journal):
    monkeypatch.setenv("SKOS_AUTOPILOT_OFF", "1")
    _write_task(tmp_path, "t-1", tags=["repo:skos"], acceptance_criteria=["x"])
    ex = _RunExec(GateResult(5, True, "ok", "pr"))
    EXECUTORS["engineering"] = ex
    board = _board(["t-1"])
    harness = SimpleNamespace(name="h", assess=lambda b: Verdict(verdict="valid", reason=""))
    out = orch.run_once(board=board, harness=harness, config=_config(),
                        tasks_dir=tmp_path, run_id="rk", executors={"engineering": ex})
    ex.run.assert_not_called()                       # never entered Phase 2
    assert out["stopped"] == "kill_switch"


def test_caps_stop_and_escalate_between_items(clean_execs, fake_journal):
    ex = _RunExec(GateResult(5, True, "ok", "pr"))
    EXECUTORS["engineering"] = ex
    board = MagicMock()
    ledger = CapLedger(Caps(max_tokens_per_run=100))
    ledger.tokens = 200  # already over
    decisions = []
    state = orch.phase2_swarm([(_wi("t-1"), ex), (_wi("t-2"), ex)],
                              harness=SimpleNamespace(name="h"), board=board,
                              caps=Caps(), ledger=ledger, decisions=decisions, run_id="rc")
    ex.run.assert_not_called()                       # stopped before any run
    assert state == {}
    assert len(decisions) == 1 and "budget" in decisions[0].prompt.lower()


def test_resume_skips_finalized(tmp_path, monkeypatch, clean_execs):
    _write_task(tmp_path, "t-A", tags=["repo:skos"], acceptance_criteria=["x"])
    _write_task(tmp_path, "t-B", tags=["repo:skos"], acceptance_criteria=["x"])
    ex = _RunExec(GateResult(5, True, "ok", "pr"))
    EXECUTORS["engineering"] = ex
    board = _board(["t-A", "t-B"])
    harness = SimpleNamespace(name="h", assess=lambda b: Verdict(verdict="valid", reason=""))
    prior = {"run_id": "rr", "items": {"t-A": {"state": "finalized", "round": 1, "score": 5}}}
    monkeypatch.setattr(orch, "journal", SimpleNamespace(
        read_run=lambda rid: prior, write_run=lambda rid, d: None))

    orch.run_once(board=board, harness=harness, config=_config(),
                  tasks_dir=tmp_path, run_id="rr", executors={"engineering": ex})

    # t-A already finalized -> not re-run; only t-B runs
    ran = [c.args[0].ref for c in ex.run.call_args_list]
    assert ran == ["t-B"]


@pytest.mark.needs_skcapstone
def test_run_cli_dry_run_uses_stub(monkeypatch):
    import skharness.autocode.orchestrator as o
    seen = {}
    monkeypatch.setattr(o.Config, "load", classmethod(lambda cls, *a, **k: _config(live_execution=False)))
    monkeypatch.setattr(o, "run_once", lambda **kw: seen.update(kw) or {"ok": True})
    monkeypatch.setattr("skcapstone.coordination.Board", lambda *a, **k: object())
    monkeypatch.setattr("skcapstone.mcp_tools._helpers._shared_root", lambda: "/tmp")
    o.run_cli(dry_run=True)
    assert seen["harness"].name == "stub" and seen["dry_run"] is True


@pytest.mark.needs_skcapstone
def test_run_cli_canary_disabled_when_live_execution_off(monkeypatch):
    import skharness.autocode.orchestrator as o
    monkeypatch.setattr(o.Config, "load", classmethod(lambda cls, *a, **k: _config(live_execution=False)))
    monkeypatch.setattr("skcapstone.coordination.Board", lambda *a, **k: object())
    monkeypatch.setattr("skcapstone.mcp_tools._helpers._shared_root", lambda: "/tmp")
    out = o.run_cli(dry_run=False, canary=True, task="t-1", harness="pi")
    assert "disabled" in out                     # live_execution off -> no spawn


@pytest.mark.needs_skcapstone
def test_run_cli_live_builds_real_harness(monkeypatch):
    import skharness.autocode.orchestrator as o
    from types import SimpleNamespace
    seen = {}
    monkeypatch.setattr(o.Config, "load", classmethod(lambda cls, *a, **k: _config(live_execution=True)))
    monkeypatch.setattr(o, "build_harness", lambda config, name=None: SimpleNamespace(name=name or "pi"))
    monkeypatch.setattr(o, "run_once", lambda **kw: seen.update(kw) or {"ok": True})
    monkeypatch.setattr("skcapstone.coordination.Board", lambda *a, **k: object())
    monkeypatch.setattr("skcapstone.mcp_tools._helpers._shared_root", lambda: "/tmp")
    o.run_cli(dry_run=False, canary=True, task="t-1", harness="pi")
    assert seen["harness"].name == "pi" and seen["dry_run"] is False and seen["task"] == "t-1"


def test_run_once_task_filter(tmp_path, clean_execs):
    import skharness.autocode.orchestrator as orch
    _write_task(tmp_path, "keep", tags=["repo:skos"], acceptance_criteria=["x"])
    _write_task(tmp_path, "drop", tags=["repo:skos"], acceptance_criteria=["x"])
    ex = _RunExec(GateResult(5, True, "ok", "pr#1"))
    EXECUTORS["engineering"] = ex
    board = _board(["keep", "drop"])
    harness = SimpleNamespace(name="h", assess=lambda b: Verdict(verdict="valid", reason=""))
    out = orch.run_once(board=board, harness=harness, config=_config(dry_run=False),
                        tasks_dir=tmp_path, run_id="r1", task="keep",
                        executors={"engineering": ex})
    assert out["selected"] == ["keep"]           # only the targeted task ran


def test_phase0_only_scopes_to_single_task(tmp_path):
    import skharness.autocode.orchestrator as orch
    from unittest.mock import MagicMock
    _write_task(tmp_path, "target", tags=["repo:skos"], acceptance_criteria=["x"])
    _write_task(tmp_path, "other", tags=["repo:skos"], acceptance_criteria=["y"])
    board = _board(["target", "other"])          # both unblocked
    harness = MagicMock()
    harness.assess.return_value = Verdict(verdict="valid", reason="")
    cands, _ = orch.phase0_assess(board=board, harness=harness, tasks_dir=tmp_path,
                                  caps=Caps(), run_id="r1", dry_run=True, only="target")
    assert harness.assess.call_count == 1        # only the one task assessed, not the board
    assert [c.ref for c in cands] == ["target"]


def test_swarm_runs_items_concurrently_when_max_concurrent_gt_1(clean_execs, fake_journal):
    """max_concurrent>1 runs items in parallel: N items each sleeping T finish in
    ~T, not ~N*T, and all get finalized. Proves the worker pool + locking."""
    import threading as _th
    import time as _t
    from types import SimpleNamespace

    active = {"n": 0, "max": 0}
    _l = _th.Lock()

    class _SlowExec:
        kind = "engineering"
        def __init__(self):
            self.name = "slow"
        def run(self, item, harness):
            with _l:
                active["n"] += 1
                active["max"] = max(active["max"], active["n"])
            _t.sleep(0.3)
            with _l:
                active["n"] -= 1
            return GateResult(5, True, "ok", "pr")
        def finalize(self, item, result):  # no-op finalize
            pass
        def escalate(self, item, notes):
            return DecisionItem(qid="q", prompt="p", options={}, action_ref=item.ref, priority="low")

    ex = _SlowExec()
    EXECUTORS["engineering"] = ex
    items = [(_wi(f"t-{i}"), ex) for i in range(4)]
    ledger = CapLedger(Caps())
    decisions = []
    t0 = _t.monotonic()
    # Pin the pool size explicitly so this asserts the CONCURRENCY LOGIC, not the
    # host's core/RAM/disk capacity (the resource autoscaler would clamp to 1 on a
    # small box and make this test machine-dependent). Production leaves `workers`
    # None and still scales to the host.
    state = orch.phase2_swarm(items, harness=SimpleNamespace(name="h"),
                              board=MagicMock(), caps=Caps(max_concurrent=4),
                              ledger=ledger, decisions=decisions, run_id="rp",
                              workers=4)
    elapsed = _t.monotonic() - t0
    assert len(state) == 4 and all(v["state"] == "finalized" for v in state.values())
    assert active["max"] >= 2, "no real concurrency observed"
    assert elapsed < 0.9, f"ran serially ({elapsed:.2f}s for 4x0.3s)"
