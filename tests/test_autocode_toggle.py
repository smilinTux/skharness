"""T1 conformance: the hardcore-vs-simple quality TOGGLE.

Pins the toggle's safety and routing invariants:

  G1  DirectExecutor NEVER invokes `git merge` and NEVER grades (run + finalize
      driven under a recording fake subprocess/harness).
  G2  DirectExecutor.finalize refuses a gated result and never merges; its
      inherited merge helper is structurally refused.
  floor  a per-repo min_quality upgrades a direct request to gated (G6).
  route  QualityMode picks the executor kind (direct -> engineering-direct,
         everything else -> engineering).
  S20    a card `quality:` TAG is raise-only: it can strengthen review, never
         weaken it, so a card can no longer route itself to the gateless
         executor. Lowering quality is operator config only.
  gated-unchanged  the gated path still routes to EngineeringExecutor and still
         passes ONLY behind the twin gate.

No em/en dashes anywhere (SKWorld hard rule).
"""
import types as _t

import pytest

from skharness.autocode import orchestrator as orch
from skharness.autocode.direct import DirectExecutor
from skharness.autocode.engineering import EngineeringExecutor
from skharness.autocode.types import GateResult, HarnessResult, QualityMode, RepoSpec, WorkItem


def _spec(name="skrender", **over):
    base = dict(name=name, path=f"/repos/{name}", base_branch="main",
                integration_branch="develop", test_cmd="pytest", ci="none")
    base.update(over)
    return RepoSpec(**base)


def _cfg(repo_map=None, **over):
    base = dict(repo_map=repo_map or {"skrender": _spec()}, automerge_repos=[],
                default_quality=QualityMode.GATED)
    base.update(over)
    return _t.SimpleNamespace(**base)


def _item(**payload):
    payload.setdefault("tags", ["repo:skrender"])
    payload.setdefault("unblocked", True)
    payload.setdefault("verdict", "valid")
    payload.setdefault("description", "fix a typo")
    return WorkItem(kind="engineering-direct", ref="t1", source="coord",
                    repo=None, payload=payload)


# --------------------------------------------------------------------------- #
# GateResult.mode default (back-compat) + DirectExecutor produces mode=direct  #
# --------------------------------------------------------------------------- #

def test_gate_result_mode_defaults_to_gated():
    gr = GateResult(score=5, passed=True, notes="ok", artifact="pr")
    assert gr.mode == "gated"                     # every existing call site preserved


def test_direct_kind_is_engineering_direct():
    ex = DirectExecutor(_cfg(), board=object(), journal=object())
    assert ex.kind == "engineering-direct"


# --------------------------------------------------------------------------- #
# G1: DirectExecutor never merges and never grades                            #
# --------------------------------------------------------------------------- #

def _recording_run(mocker):
    """Patch engineering.subprocess.run to record every git argv; return the recorder."""
    calls = []

    def _fake(argv, *a, **k):
        calls.append(list(argv))
        return _t.SimpleNamespace(returncode=0, stdout="", stderr="")

    mocker.patch("skharness.autocode.engineering.subprocess.run", side_effect=_fake)
    return calls


def _assert_no_merge(calls, integration_branch="develop"):
    for argv in calls:
        assert "merge" not in argv, f"DirectExecutor invoked a merge: {argv}"
        # a merge is a checkout of the protected/integration branch then `git merge`;
        # DirectExecutor must never check out the integration branch either.
        if "checkout" in argv:
            assert integration_branch not in argv, f"checked out protected branch: {argv}"


def test_g1_direct_run_never_grades_and_never_merges(mocker):
    calls = _recording_run(mocker)
    ex = DirectExecutor(_cfg(), board=mocker.Mock(), journal=mocker.Mock())
    harness = mocker.Mock(name="harness")
    harness.run_task.return_value = HarnessResult(ok=True, artifact=None, tokens=1,
                                                  cost_usd=0.0, raw={})
    res = ex.run(_item(), harness)
    harness.grade.assert_not_called()             # NO independent grader in direct mode
    _assert_no_merge(calls)
    assert res.mode == "direct" and res.score is None


def test_g1_direct_finalize_never_merges(mocker):
    calls = _recording_run(mocker)
    ex = DirectExecutor(_cfg(), board=mocker.Mock(), journal=mocker.Mock(),
                        digest=mocker.Mock())
    ex.journal.worktree_for.return_value = "/wt/t1"
    result = GateResult(score=None, passed=True, notes="ungated", artifact="/wt/t1",
                        mode="direct")
    ex.finalize(_item(), result)
    _assert_no_merge(calls)
    ex.digest.queue_decision.assert_called_once()  # PR + review decision, the terminal state
    # the queued decision is flagged UNGATED so a reviewer can never mistake it
    assert "UNGATED" in ex.digest.queue_decision.call_args.kwargs["prompt"]


# --------------------------------------------------------------------------- #
# G2: finalize refuses a gated result; the inherited merge is refused          #
# --------------------------------------------------------------------------- #

def test_g2_finalize_refuses_gated_result(mocker):
    calls = _recording_run(mocker)
    ex = DirectExecutor(_cfg(), board=mocker.Mock(), journal=mocker.Mock(),
                        digest=mocker.Mock())
    ex.journal.worktree_for.return_value = "/wt/t1"
    gated = GateResult(score=5, passed=True, notes="", artifact="pr", mode="gated")
    with pytest.raises(RuntimeError):
        ex.finalize(_item(), gated)
    _assert_no_merge(calls)                        # refused BEFORE any git ran
    ex.digest.queue_decision.assert_not_called()


def test_g2_direct_merge_helper_is_structurally_refused():
    ex = DirectExecutor(_cfg(), board=object(), journal=object())
    with pytest.raises(RuntimeError):
        ex._merge(_spec(), "autopilot/t1")         # even if called directly, it refuses


def test_g2_gated_finalize_refuses_non_gated_result(mocker):
    """Inverse of the DirectExecutor guard: EngineeringExecutor (the GATED
    executor) must refuse a result whose mode is not 'gated', before any
    commit/merge/push, even though it never produces such a result itself."""
    calls = _recording_run(mocker)
    ex = EngineeringExecutor(_cfg(), board=mocker.Mock(), journal=mocker.Mock(),
                             digest=mocker.Mock())
    ex.journal.worktree_for.return_value = "/wt/t1"
    direct = GateResult(score=None, passed=True, notes="ungated", artifact="/wt/t1",
                        mode="direct")
    with pytest.raises(RuntimeError):
        ex.finalize(_item(), direct)
    assert calls == []                              # refused BEFORE any git ran (no commit)
    ex.digest.queue_decision.assert_not_called()


# --------------------------------------------------------------------------- #
# min_quality FLOOR (G6): a direct request is upgraded to gated on a floored repo #
# --------------------------------------------------------------------------- #

def test_floor_upgrades_direct_to_gated_on_floored_repo():
    cfg = _cfg(repo_map={"skchat": _spec("skchat", min_quality=QualityMode.GATED)})
    task = {"id": "t1", "tags": ["repo:skchat", "quality:direct"]}
    assert orch.resolve_quality(task, cfg) == QualityMode.GATED   # floor wins over the tag
    assert orch.classify_kind(task, cfg) == "engineering"          # so it routes gated


def test_floor_absent_keeps_direct():
    """With no floor, an OPERATOR-configured direct default survives untouched.

    Updated by S20 (card 0b7e3ac3): the direct request used to come from a
    `quality:direct` card TAG, which let a card switch off its own twin gate. The
    tag is now raise-only and the request comes from operator config instead. The
    property under test is unchanged: absent a floor, direct stays direct.
    """
    cfg = _cfg(repo_map={"skrender": _spec("skrender")},            # min_quality None
               default_quality=QualityMode.DIRECT)
    task = {"id": "t1", "tags": ["repo:skrender"]}
    assert orch.resolve_quality(task, cfg) == QualityMode.DIRECT
    assert orch.classify_kind(task, cfg) == "engineering-direct"


def test_floor_never_lowers_a_gated_request():
    # a floor can only strengthen; a gated request against a direct-floored repo stays gated
    cfg = _cfg(repo_map={"skrender": _spec("skrender", min_quality=QualityMode.DIRECT)})
    task = {"id": "t1", "tags": ["repo:skrender", "quality:gated"]}
    assert orch.resolve_quality(task, cfg) == QualityMode.GATED


# --------------------------------------------------------------------------- #
# QualityMode routes to the right executor kind                                #
# --------------------------------------------------------------------------- #

def test_quality_direct_routes_direct_when_the_OPERATOR_asks_for_it():
    """DIRECT still routes to the gateless executor. What changed in S20 is WHO
    may ask for it: an operator, through config, never a card through its tags.
    The card-tag half of this is pinned in tests/test_autocode_grader_pin.py."""
    cfg = _cfg(default_quality=QualityMode.DIRECT)
    task = {"id": "t1", "tags": ["repo:skrender"]}
    assert orch.classify_kind(task, cfg) == "engineering-direct"


def test_a_card_tag_can_no_longer_route_itself_to_the_gateless_executor():
    cfg = _cfg()                                      # operator baseline: gated
    task = {"id": "t1", "tags": ["repo:skrender", "quality:direct"]}
    assert orch.classify_kind(task, cfg) == "engineering"


def test_no_quality_tag_uses_config_default():
    task = {"id": "t1", "tags": ["repo:skrender"]}
    assert orch.classify_kind(task, _cfg(default_quality=QualityMode.DIRECT)) == \
        "engineering-direct"
    assert orch.classify_kind(task, _cfg(default_quality=QualityMode.GATED)) == "engineering"


def test_tag_beats_config_default():
    task = {"id": "t1", "tags": ["repo:skrender", "quality:gated"]}
    assert orch.resolve_quality(task, _cfg(default_quality=QualityMode.DIRECT)) == \
        QualityMode.GATED


def test_none_quality_fail_safe_routes_gated():
    # NONE is not an engine execution; on a repo-tagged board task it fail-safes to gated
    task = {"id": "t1", "tags": ["repo:skrender", "quality:none"]}
    assert orch.classify_kind(task, _cfg()) == "engineering"


def test_unknown_quality_tag_fails_closed_to_gated():
    task = {"id": "t1", "tags": ["repo:skrender", "quality:banana"]}
    assert orch.resolve_quality(task, _cfg()) == QualityMode.GATED


def test_to_workitem_normalizes_quality_into_payload():
    task = {"id": "t1", "tags": ["repo:skrender"], "acceptance_criteria": ["x"]}
    wi = orch._to_workitem(task, config=_cfg(default_quality=QualityMode.DIRECT))
    assert wi.payload["quality"] == "direct" and wi.kind == "engineering-direct"


# --------------------------------------------------------------------------- #
# build_executors registers BOTH kinds                                         #
# --------------------------------------------------------------------------- #

def test_build_executors_registers_direct_and_engineering():
    from skharness.autocode.config import Caps
    cfg = _t.SimpleNamespace(enabled=True, dry_run=True, caps=Caps(),
                             repo_map={"skrender": _spec()}, automerge_repos=[])
    table = orch.build_executors(cfg, object(), "rX")
    assert isinstance(table["engineering"], EngineeringExecutor)
    assert isinstance(table["engineering-direct"], DirectExecutor)


# --------------------------------------------------------------------------- #
# the gated path is UNCHANGED: EngineeringExecutor still gates on all four arms #
# --------------------------------------------------------------------------- #

def _drive_gated(mocker, grades, ci_status="green", cov=0.95):
    cfg = _cfg()
    ex = EngineeringExecutor(cfg, board=mocker.Mock(), journal=mocker.Mock())
    mocker.patch.object(ex, "make_worktree", return_value="/wt/t1")
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
    return ex.run(item, harness), harness


def test_gated_path_passes_only_behind_the_twin_gate(mocker):
    good = GateResult(score=5, passed=True, notes="ready <promise>COMPLETE</promise>",
                      artifact="pr")
    res, harness = _drive_gated(mocker, [good], ci_status="green", cov=0.95)
    assert res.passed is True and res.score == 5 and res.mode == "gated"
    harness.grade.assert_called()                 # gated mode DOES grade (twin gate intact)


def test_gated_path_still_blocks_on_ci_red(mocker):
    five = GateResult(score=5, passed=True, notes="<promise>COMPLETE</promise>",
                      artifact="pr")
    res, _ = _drive_gated(mocker, [five] * 6, ci_status="red", cov=0.95)
    assert res.passed is False                    # CI red overrides a 5/5, unchanged
