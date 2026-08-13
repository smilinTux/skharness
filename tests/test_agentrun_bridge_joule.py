"""Wallet-settlement wiring tests for skharness.autocode.agentrun_bridge (J1,
docs/specs/2026-08-13-bridge-joule-economics.md, skcapstone repo).

Covers the safety-critical addition: execute_dispatch settles to the
JouleWallet ONLY on the REAL ratify twin-gate verdict (rr.passed), never on
the ungated direct-mode gr.passed, deduped by card_id, and never lets a
wallet failure turn a shipped draft PR into a refusal.

Mirrors test_agentrun_bridge_cost.py's fixtures/helpers (see that file's
header); this file only adds what's specific to wallet settlement. Every
``joules.settle`` reference is patched via the module attribute
(``skharness.autocode.joules.settle``), never a bound name, because
agentrun_bridge.py calls it as ``joules.settle(...)`` through a module import
(``from . import autopilot_cost, joules``) -- exactly so this seam is
patchable without reaching into the bridge's own namespace.

No em/en dashes anywhere (SKWorld hard rule).
"""
from __future__ import annotations

import types as _t
from pathlib import Path

import pytest

from skharness.autocode import autopilot_cost
from skharness.autocode.agentrun_bridge import AgentRunDirectExecutor, execute_dispatch
from skharness.autocode.config import Caps, Config
from skharness.autocode.joules import Economics
from skharness.autocode.types import GateResult, HarnessResult, QualityMode, RepoSpec

# --------------------------------------------------------------------------- #
# Fixtures / helpers (mirrors test_agentrun_bridge_cost.py)                   #
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _isolate_dirs(monkeypatch, tmp_path):
    """Real RunHandle + cost ledger + settlement journal backed by throwaway
    dirs: never touch the live ~/.skcapstone/coordination/autopilot/runs/ or
    ~/.skcapstone/autopilot-cost/."""
    monkeypatch.setenv("SK_AUTOPILOT_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("SKAI_COST_DIR", str(tmp_path / "autopilot-cost"))


def _spec(name="skrender", **over):
    base = dict(name=name, path=f"/repos/{name}", base_branch="main",
                integration_branch="develop", test_cmd="pytest", ci="none",
                min_quality=QualityMode.DIRECT)
    base.update(over)
    return RepoSpec(**base)


def _cfg(repo_map=None, max_usd_per_day=25.0, **over):
    base = dict(repo_map=(repo_map if repo_map is not None else {"skrender": _spec()}),
                automerge_repos=[], live_execution=True, harness="claude-code",
                default_quality=QualityMode.GATED,
                caps=Caps(max_usd_per_day=max_usd_per_day))
    base.update(over)
    return Config(**base)


def _context(card_id="task-abc", agent="lumina", **over):
    base = dict(card_id=card_id, kind="task", title="Fix flaky retry",
                instruction="add retry with backoff", agent=agent, mode="execute")
    base.update(over)
    return base


def _card(labels, description="", meta=None, title="Fix flaky retry",
          card_id="task-abc", priority="high"):
    from skcoord.card import Card, Column, Kind
    return Card(id=card_id, kind=Kind.TASK, title=title, description=description,
               status=Column.REVIEW, swimlane="feature", labels=list(labels),
               meta=meta or {}, priority=priority)


class _FakeHarness:
    """A harness stand-in with real ``run_task``/``grade`` the bridge wraps."""

    name = "fake"

    def __init__(self, run_cost: float = 1.0, run_tokens: int = 500,
                 run_raw: dict | None = None, grade_cost: float = 0.25,
                 grade_tokens: int = 50) -> None:
        self._run_cost = run_cost
        self._run_tokens = run_tokens
        self._run_raw = run_raw or {}
        self._grade_cost = grade_cost
        self._grade_tokens = grade_tokens
        self.run_calls = 0
        self.grade_calls = 0

    def run_task(self, brief):
        self.run_calls += 1
        return HarnessResult(ok=True, artifact=None, tokens=self._run_tokens,
                             cost_usd=self._run_cost, raw=self._run_raw)

    def grade(self, brief):
        # types.GateResult carries no cost fields; the bridge's grade-wrap is
        # getattr-based/defensive so it tolerates that today AND picks up cost
        # if a richer adapter ever attaches it, which this fake simulates.
        self.grade_calls += 1
        return _t.SimpleNamespace(cost_usd=self._grade_cost, tokens=self._grade_tokens,
                                  raw=None)


def _wire_common(mocker, card, cfg, harness):
    mocker.patch("skcapstone.mcp_tools._helpers._shared_root",
                 return_value=Path("/nonexistent-agentrun-bridge-joule-test-home"))
    mocker.patch("skcoord.card_store.CardStore.fold", return_value=card)
    mocker.patch("skharness.autocode.agentrun_bridge.Config.load", return_value=cfg)
    mocker.patch("skharness.autocode.agentrun_bridge.build_harness", return_value=harness)


def _passing_run_calls_harness(self, item, harness):
    self.journal.set_worktree(item.ref, "/fake/worktree")
    harness.run_task(_t.SimpleNamespace())
    return GateResult(score=None, passed=True, notes="direct mode: UNGATED single run",
                      artifact="/fake/worktree", mode="direct")


def _make_ratify(passed=True, score=5):
    """A ratify stand-in that also calls harness.grade (exercising the bridge's
    grade-cost wrap), then returns the twin-gate verdict under test."""
    def _ratify(repo, worktree, acceptance, harness):
        harness.grade(_t.SimpleNamespace())
        notes = "<promise>COMPLETE</promise>" if passed else "grade found issues"
        return GateResult(score=score, passed=passed, notes=notes, artifact=None)
    return _ratify


def _wire_pass(mocker, pr_url="https://github.com/acme/skrender/pull/42",
              passed=True, score=5):
    mocker.patch.object(AgentRunDirectExecutor, "run", _passing_run_calls_harness)
    mocker.patch("skharness.autocode.agentrun_bridge.ratify",
                 side_effect=_make_ratify(passed=passed, score=score))

    def _finalize(self, item, result):
        self.pr_url = pr_url
    mocker.patch.object(AgentRunDirectExecutor, "finalize", _finalize)


def _econ(minted=100, spent=63, net=37, balance_after=537):
    return Economics(agent="lumina", task_ref="airun-task-abc", minted=minted,
                     spent_joules=spent, net_joules=net, balance_after=balance_after,
                     recorded=True)


# --------------------------------------------------------------------------- #
# (a) rr.passed True -> settle called ONCE with the right args, row recorded  #
# --------------------------------------------------------------------------- #


@pytest.mark.needs_skcapstone
def test_settle_called_once_on_ratify_pass_with_correct_args(mocker):
    card = _card(["repo:skrender"], priority="critical")
    cfg = _cfg()
    harness = _FakeHarness(run_cost=1.0, run_tokens=500, grade_cost=0.25, grade_tokens=50)
    _wire_common(mocker, card, cfg, harness)
    _wire_pass(mocker, passed=True, score=5)

    settle_spy = mocker.patch("skharness.autocode.joules.settle", return_value=_econ())

    result = execute_dispatch(_context(card_id=card.id, agent="lumina"))

    assert result["links"]["pr"] == "https://github.com/acme/skrender/pull/42"
    settle_spy.assert_called_once()
    kwargs = settle_spy.call_args.kwargs
    assert kwargs["agent"] == "lumina"          # context["agent"], never the env agent
    assert kwargs["task_ref"] == f"airun-{card.id}"
    assert kwargs["priority"] == "critical"     # card.priority, never a literal
    assert kwargs["score"] == 5                 # rr.score, never a hardcoded 5
    # The settled usage.cost_usd MUST equal the run's captured cost: run_task's
    # cost PLUS the grade's cost (design doc J1 step 2, "close the grade-cost gap").
    assert kwargs["usage"].cost_usd == pytest.approx(1.0 + 0.25)

    # A settlement journal row was written from the returned Economics.
    rows = autopilot_cost._read_settlements()
    assert len(rows) == 1
    assert rows[0]["card_id"] == card.id
    assert rows[0]["agent"] == "lumina"
    assert rows[0]["minted"] == 100
    assert rows[0]["spent_joules"] == 63
    assert rows[0]["net_joules"] == 37
    assert rows[0]["balance_after"] == 537

    # Activity trail carries a human-readable settlement line.
    settle_lines = [a["text"] for a in result["activity"] if "joules settled" in a.get("text", "")]
    assert len(settle_lines) == 1
    assert "+100" in settle_lines[0]
    assert "-63" in settle_lines[0]
    assert "net 37" in settle_lines[0]
    assert "537" in settle_lines[0]


@pytest.mark.needs_skcapstone
def test_settle_charges_the_requesting_agent_not_the_env_agent(mocker):
    card = _card(["repo:skrender"])
    cfg = _cfg()
    harness = _FakeHarness()
    _wire_common(mocker, card, cfg, harness)
    _wire_pass(mocker, passed=True, score=5)
    settle_spy = mocker.patch("skharness.autocode.joules.settle", return_value=_econ())

    execute_dispatch(_context(card_id=card.id, agent="jarvis"))

    assert settle_spy.call_args.kwargs["agent"] == "jarvis"


# --------------------------------------------------------------------------- #
# (b) rr.passed False -> settle NEVER called, no wallet effect                #
# --------------------------------------------------------------------------- #


@pytest.mark.needs_skcapstone
def test_settle_never_called_when_ratify_does_not_pass(mocker):
    card = _card(["repo:skrender"])
    cfg = _cfg()
    harness = _FakeHarness()
    _wire_common(mocker, card, cfg, harness)
    _wire_pass(mocker, passed=False, score=2)
    settle_spy = mocker.patch("skharness.autocode.joules.settle")

    result = execute_dispatch(_context(card_id=card.id))

    # The bridge's contract: a draft PR still opens either way.
    assert result["links"]["pr"] == "https://github.com/acme/skrender/pull/42"
    settle_spy.assert_not_called()
    assert autopilot_cost._read_settlements() == []


# --------------------------------------------------------------------------- #
# (c) a second dispatch on the same card_id (already_settled) skips settle    #
# --------------------------------------------------------------------------- #


@pytest.mark.needs_skcapstone
def test_second_dispatch_same_card_does_not_settle_again(mocker):
    card = _card(["repo:skrender"])
    cfg = _cfg()
    harness = _FakeHarness()
    _wire_common(mocker, card, cfg, harness)
    _wire_pass(mocker, passed=True, score=5)
    settle_spy = mocker.patch("skharness.autocode.joules.settle", return_value=_econ())

    result1 = execute_dispatch(_context(card_id=card.id))
    result2 = execute_dispatch(_context(card_id=card.id))

    settle_spy.assert_called_once()
    assert len(autopilot_cost._read_settlements()) == 1

    skip_lines = [a["text"] for a in result2["activity"]
                  if "already settled" in a.get("text", "")]
    assert len(skip_lines) == 1
    # The second dispatch still ships its own draft PR; the guard is wallet-only.
    assert result1["links"]["pr"] == "https://github.com/acme/skrender/pull/42"
    assert result2["links"]["pr"] == "https://github.com/acme/skrender/pull/42"


# --------------------------------------------------------------------------- #
# (d) settle() raising never breaks the dispatch (still a draft-PR result)    #
# --------------------------------------------------------------------------- #


@pytest.mark.needs_skcapstone
def test_settle_exception_does_not_break_the_dispatch(mocker):
    card = _card(["repo:skrender"])
    cfg = _cfg()
    harness = _FakeHarness()
    _wire_common(mocker, card, cfg, harness)
    _wire_pass(mocker, passed=True, score=5)
    mocker.patch("skharness.autocode.joules.settle",
                 side_effect=RuntimeError("wallet backend unreachable"))

    result = execute_dispatch(_context(card_id=card.id))       # must not raise

    assert result["links"]["pr"] == "https://github.com/acme/skrender/pull/42"
    assert "draft PR" in result["summary"]
    fail_lines = [a["text"] for a in result["activity"]
                  if "joule settlement failed" in a.get("text", "")]
    assert len(fail_lines) == 1
    assert "wallet backend unreachable" in fail_lines[0]
    # No settlement row on a failed settle -- nothing to dedupe against.
    assert autopilot_cost._read_settlements() == []


@pytest.mark.needs_skcapstone
def test_record_settlement_exception_does_not_break_the_dispatch(mocker):
    """A settlement-JOURNAL write failure (after a successful settle()) must
    also never turn a shipped draft PR into a refusal -- it's inside the same
    try/except as the settle() call itself."""
    card = _card(["repo:skrender"])
    cfg = _cfg()
    harness = _FakeHarness()
    _wire_common(mocker, card, cfg, harness)
    _wire_pass(mocker, passed=True, score=5)
    mocker.patch("skharness.autocode.joules.settle", return_value=_econ())
    mocker.patch("skharness.autocode.autopilot_cost.record_settlement",
                 side_effect=RuntimeError("disk full"))

    result = execute_dispatch(_context(card_id=card.id))       # must not raise

    assert result["links"]["pr"] == "https://github.com/acme/skrender/pull/42"
