"""P1 bridge tests: skharness.autocode.agentrun_bridge.

Design doc: docs/specs/2026-08-13-skharness-execute-bridge-arch.md (skcapstone
repo). A live sandbox run is unverifiable here (docker image, oauth token, gh
auth); this file proves the wiring and every fail-closed edge with the engine
mocked (DirectExecutor.run / ratify / finalize / build_harness / Config.load
and the CardStore fold), per the design doc's test strategy (section 7).

No em/en dashes anywhere (SKWorld hard rule).
"""
from __future__ import annotations

import types as _t
from pathlib import Path

import pytest

from skharness.autocode.agentrun_bridge import (
    AgentRunDirectExecutor,
    build_execute_dispatcher,
    execute_dispatch,
)
from skharness.autocode.config import Config
from skharness.autocode.executor import EXECUTORS
from skharness.autocode.types import GateResult, QualityMode, RepoSpec, WorkItem

# --------------------------------------------------------------------------- #
# Fixtures / helpers                                                          #
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _isolate_runs_dir(monkeypatch, tmp_path):
    """Real RunHandle backed by a throwaway dir: never touch the live
    ~/.skcapstone/coordination/autopilot/runs/."""
    monkeypatch.setenv("SK_AUTOPILOT_RUNS_DIR", str(tmp_path / "runs"))


def _spec(name="skrender", **over):
    # min_quality=DIRECT: an explicit non-gated floor, since coerce_quality(None)
    # normalizes an UNSET floor to GATED (types.py) and the bridge refuses gated
    # floors in P1 -- a repo must opt in to direct-mode bridge dispatch.
    base = dict(name=name, path=f"/repos/{name}", base_branch="main",
                integration_branch="develop", test_cmd="pytest", ci="none",
                min_quality=QualityMode.DIRECT)
    base.update(over)
    return RepoSpec(**base)


def _cfg(repo_map=None, **over):
    base = dict(repo_map=(repo_map if repo_map is not None else {"skrender": _spec()}),
                automerge_repos=[], live_execution=True, harness="claude-code",
                default_quality=QualityMode.GATED)
    base.update(over)
    return Config(**base)


def _context(card_id="task-abc", **over):
    base = dict(card_id=card_id, kind="task", title="Fix flaky retry",
                instruction="add retry with backoff", agent="lumina", mode="execute")
    base.update(over)
    return base


def _card(labels, description="", meta=None, title="Fix flaky retry", card_id="task-abc"):
    from skcoord.card import Card, Column, Kind
    return Card(id=card_id, kind=Kind.TASK, title=title, description=description,
               status=Column.REVIEW, swimlane="feature", labels=list(labels),
               meta=meta or {})


def _wire_common(mocker, card, cfg):
    """Patch the CardStore fold, the fresh Config.load, and build_harness --
    the seams execute_dispatch's own imports resolve through (design doc
    section 1 steps 1, 2, 5)."""
    mocker.patch("skcapstone.mcp_tools._helpers._shared_root",
                 return_value=Path("/nonexistent-agentrun-bridge-test-home"))
    mocker.patch("skcoord.card_store.CardStore.fold", return_value=card)
    mocker.patch("skharness.autocode.agentrun_bridge.Config.load", return_value=cfg)
    mocker.patch("skharness.autocode.agentrun_bridge.build_harness",
                 return_value=_t.SimpleNamespace(name="fake"))


class _FakeJournal:
    def __init__(self) -> None:
        self.claims: list[tuple[str, str]] = []
        self._wt: dict[str, str] = {}

    def record_claim(self, ref, claimed_at):
        self.claims.append((ref, claimed_at))

    def set_worktree(self, ref, path):
        self._wt[ref] = path

    def worktree_for(self, ref):
        return self._wt.get(ref)


class _FakeDigest:
    def __init__(self) -> None:
        self.decisions: list[dict] = []

    def queue_decision(self, **kw):
        self.decisions.append(kw)


def _passing_run(self, item, harness):
    self.journal.set_worktree(item.ref, "/fake/worktree")
    return GateResult(score=None, passed=True, notes="direct mode: UNGATED single run",
                      artifact="/fake/worktree", mode="direct")


def _passing_ratify(*a, **k):
    return GateResult(score=5, passed=True, notes="<promise>COMPLETE</promise>", artifact=None)


# --------------------------------------------------------------------------- #
# 1. Happy path: run -> ratify -> finalize in order, links = {pr, branch}     #
# --------------------------------------------------------------------------- #


@pytest.mark.needs_skcapstone
def test_happy_path_calls_run_ratify_finalize_in_order(mocker):
    order: list[str] = []
    card = _card(["repo:skrender"])
    cfg = _cfg()
    _wire_common(mocker, card, cfg)

    def _run(self, item, harness):
        order.append("run")
        assert item.ref == f"airun-{card.id}"
        assert item.repo == "skrender"
        return _passing_run(self, item, harness)

    def _ratify(*a, **k):
        order.append("ratify")
        return _passing_ratify(*a, **k)

    def _finalize(self, item, result):
        order.append("finalize")
        assert result.mode == "direct"           # never the gated ratify result (G2)
        self.pr_url = "https://github.com/acme/skrender/pull/42"

    mocker.patch.object(AgentRunDirectExecutor, "run", _run)
    mocker.patch("skharness.autocode.agentrun_bridge.ratify", side_effect=_ratify)
    mocker.patch.object(AgentRunDirectExecutor, "finalize", _finalize)

    result = execute_dispatch(_context(card_id=card.id))

    assert order == ["run", "ratify", "finalize"]
    assert result["links"] == {"pr": "https://github.com/acme/skrender/pull/42",
                               "branch": f"autopilot/airun-{card.id}"}
    assert "draft PR" in result["summary"]
    assert "twin gate PASS" in result["summary"]
    assert "human review required" in result["summary"]


# --------------------------------------------------------------------------- #
# 2 & 3. Repo-label resolution: 0 or >1 labels -> refusal, engine NEVER called #
# --------------------------------------------------------------------------- #


@pytest.mark.needs_skcapstone
def test_zero_repo_labels_refuses_without_calling_the_engine(mocker):
    card = _card([])
    cfg = _cfg()
    _wire_common(mocker, card, cfg)
    run_spy = mocker.patch.object(AgentRunDirectExecutor, "run")
    ratify_spy = mocker.patch("skharness.autocode.agentrun_bridge.ratify")
    finalize_spy = mocker.patch.object(AgentRunDirectExecutor, "finalize")

    result = execute_dispatch(_context(card_id=card.id))

    assert result["links"] == {}
    assert result["summary"] == (
        "execute refused (bridge): no target repo on the card; add a repo:<name> label")
    run_spy.assert_not_called()
    ratify_spy.assert_not_called()
    finalize_spy.assert_not_called()


@pytest.mark.needs_skcapstone
def test_two_repo_labels_refuses_ambiguous_without_calling_the_engine(mocker):
    card = _card(["repo:skrender", "repo:skchat"])
    cfg = _cfg(repo_map={"skrender": _spec("skrender"), "skchat": _spec("skchat")})
    _wire_common(mocker, card, cfg)
    run_spy = mocker.patch.object(AgentRunDirectExecutor, "run")

    result = execute_dispatch(_context(card_id=card.id))

    assert result["links"] == {}
    assert "ambiguous target (repo:skrender, repo:skchat)" in result["summary"]
    run_spy.assert_not_called()


# --------------------------------------------------------------------------- #
# 4 & 5. Static factory prerequisites: missing config / unresolvable harness  #
# --------------------------------------------------------------------------- #


def test_factory_returns_none_when_repo_map_is_empty(mocker):
    mocker.patch("skharness.autocode.agentrun_bridge.Config.load",
                 return_value=Config())          # disabled default, empty repo_map
    assert build_execute_dispatcher() is None


def test_factory_returns_none_when_harness_is_unresolvable(mocker):
    cfg = _cfg(harness="totally-bogus-harness-name")
    mocker.patch("skharness.autocode.agentrun_bridge.Config.load", return_value=cfg)
    # build_harness is NOT mocked here: the real HARNESSES lookup raises ValueError.
    assert build_execute_dispatcher() is None


def test_factory_returns_the_dispatcher_when_prerequisites_are_met(mocker):
    cfg = _cfg()
    mocker.patch("skharness.autocode.agentrun_bridge.Config.load", return_value=cfg)
    fn = build_execute_dispatcher()
    assert fn is execute_dispatch


# --------------------------------------------------------------------------- #
# 6. Swallowed `gh pr create` failure -> refusal-with-branch, never success    #
# --------------------------------------------------------------------------- #


@pytest.mark.needs_skcapstone
def test_pr_open_failure_after_push_is_refusal_with_branch(mocker):
    card = _card(["repo:skrender"])
    cfg = _cfg()
    _wire_common(mocker, card, cfg)

    def _finalize(self, item, result):
        self.pr_url = ""                          # gh pr create failed: empty stdout

    mocker.patch.object(AgentRunDirectExecutor, "run", _passing_run)
    mocker.patch("skharness.autocode.agentrun_bridge.ratify", side_effect=_passing_ratify)
    mocker.patch.object(AgentRunDirectExecutor, "finalize", _finalize)

    result = execute_dispatch(_context(card_id=card.id))

    assert result["links"] == {"branch": f"autopilot/airun-{card.id}"}
    assert "pr" not in result["links"]
    assert result["summary"].startswith("execute refused (bridge):")
    assert "PR could not be opened" in result["summary"]


# --------------------------------------------------------------------------- #
# 7. Draft-only is structural: the dispatcher never calls a merge path        #
# --------------------------------------------------------------------------- #


@pytest.mark.needs_skcapstone
def test_dispatcher_never_calls_merge(mocker):
    card = _card(["repo:skrender"])
    cfg = _cfg()
    _wire_common(mocker, card, cfg)

    merge_spy = mocker.patch.object(
        AgentRunDirectExecutor, "_merge",
        side_effect=AssertionError("draft-only bridge must never merge"))

    def _finalize(self, item, result):
        self.pr_url = "https://github.com/acme/skrender/pull/1"

    mocker.patch.object(AgentRunDirectExecutor, "run", _passing_run)
    mocker.patch("skharness.autocode.agentrun_bridge.ratify", side_effect=_passing_ratify)
    mocker.patch.object(AgentRunDirectExecutor, "finalize", _finalize)

    result = execute_dispatch(_context(card_id=card.id))

    merge_spy.assert_not_called()
    assert result["links"]["pr"] == "https://github.com/acme/skrender/pull/1"


def test_merge_is_structurally_refused_on_the_bridge_executor():
    """The inherited G1 guardrail (DirectExecutor._merge raises unconditionally)
    is not overridden or weakened by the bridge subclass."""
    ex = AgentRunDirectExecutor(_cfg(), board=None, journal=_FakeJournal(), digest=_FakeDigest())
    with pytest.raises(RuntimeError, match="must never merge"):
        ex._merge(_spec(), "autopilot/airun-x")


# --------------------------------------------------------------------------- #
# 8. An exception mid-run -> a safe refusal dict, never an exception out      #
# --------------------------------------------------------------------------- #


@pytest.mark.needs_skcapstone
def test_exception_mid_run_returns_safe_refusal_not_an_exception(mocker):
    card = _card(["repo:skrender"])
    cfg = _cfg()
    _wire_common(mocker, card, cfg)

    def _boom(self, item, harness):
        raise RuntimeError("sandbox blew up")

    mocker.patch.object(AgentRunDirectExecutor, "run", _boom)
    ratify_spy = mocker.patch("skharness.autocode.agentrun_bridge.ratify")

    result = execute_dispatch(_context(card_id=card.id))       # must not raise

    assert result["links"] == {}
    assert "sandbox blew up" in result["summary"]
    assert result["activity"][-1]["atype"] == "error"
    ratify_spy.assert_not_called()


# --------------------------------------------------------------------------- #
# Direct unit tests of the three overrides                                    #
# --------------------------------------------------------------------------- #


def test_claim_is_journal_only_and_never_touches_the_board():
    journal = _FakeJournal()
    ex = AgentRunDirectExecutor(_cfg(), board=None, journal=journal, digest=_FakeDigest())
    item = WorkItem(kind="agentrun-direct", ref="airun-x", source="agent-run",
                    repo="skrender", payload={"tags": ["repo:skrender"]})
    ex.claim(item)          # board=None: a board touch here would AttributeError
    assert len(journal.claims) == 1
    assert journal.claims[0][0] == "airun-x"


def test_settle_economics_is_a_noop_in_p1():
    ex = AgentRunDirectExecutor(_cfg(), board=None, journal=_FakeJournal(), digest=_FakeDigest())
    item = WorkItem(kind="agentrun-direct", ref="airun-x", source="agent-run",
                    repo="skrender", payload={})
    assert ex._settle_economics(item, "deadbeef") is None


def test_open_pr_adds_draft_flag_and_records_the_url_on_self(mocker):
    ex = AgentRunDirectExecutor(_cfg(), board=None, journal=_FakeJournal(), digest=_FakeDigest())
    mocker.patch.object(ex, "_pr_base", return_value="develop")
    calls: list[list[str]] = []

    def _fake_run(argv, **kw):
        calls.append(list(argv))
        return _t.SimpleNamespace(returncode=0,
                                  stdout="https://github.com/acme/skrender/pull/7\n",
                                  stderr="")

    mocker.patch("skharness.autocode.agentrun_bridge.subprocess.run", side_effect=_fake_run)
    repo = _spec()
    item = WorkItem(kind="agentrun-direct", ref="airun-x", source="agent-run",
                    repo=repo.name, payload={"title": "Fix flaky retry"})

    url = ex._open_pr(repo, "autopilot/airun-x", item)

    assert url == "https://github.com/acme/skrender/pull/7"
    assert ex.pr_url == url
    assert "--draft" in calls[0]


def test_open_pr_records_empty_url_on_gh_failure(mocker):
    ex = AgentRunDirectExecutor(_cfg(), board=None, journal=_FakeJournal(), digest=_FakeDigest())
    mocker.patch.object(ex, "_pr_base", return_value="develop")

    def _fake_run(argv, **kw):
        return _t.SimpleNamespace(returncode=1, stdout="", stderr="not authenticated")

    mocker.patch("skharness.autocode.agentrun_bridge.subprocess.run", side_effect=_fake_run)
    repo = _spec()
    item = WorkItem(kind="agentrun-direct", ref="airun-x", source="agent-run",
                    repo=repo.name, payload={"title": "Fix flaky retry"})

    url = ex._open_pr(repo, "autopilot/airun-x", item)

    assert url == ""
    assert ex.pr_url == ""


# --------------------------------------------------------------------------- #
# Isolation pin: the bridge never registers into the shared EXECUTORS registry #
# --------------------------------------------------------------------------- #


def test_bridge_never_registers_into_executors():
    before = dict(EXECUTORS)
    AgentRunDirectExecutor(_cfg(), board=None, journal=_FakeJournal(), digest=_FakeDigest())
    assert dict(EXECUTORS) == before
    assert "agentrun-direct" not in EXECUTORS
