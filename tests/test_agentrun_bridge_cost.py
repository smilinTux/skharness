"""Cost-tracking wiring tests for skharness.autocode.agentrun_bridge.

Covers the two safety-critical additions to execute_dispatch (P1.1):
1. a happy run appends a ledger row carrying the harness's real cost/tokens
   (captured by wrapping harness.run_task around AgentRunDirectExecutor.run).
2. a pre-existing day_total at/over the daily cap refuses BEFORE the engine
   ever runs, and fires the (deduped) daily-usd cap alert.

Mirrors test_agentrun_bridge.py's fixtures/helpers (see that file's header
for the full design-doc pointer); this file only adds what's specific to
cost tracking.

No em/en dashes anywhere (SKWorld hard rule).
"""
from __future__ import annotations

import types as _t
from datetime import datetime, timezone
from pathlib import Path

import pytest

from skharness.autocode import autopilot_cost
from skharness.autocode.agentrun_bridge import AgentRunDirectExecutor, execute_dispatch
from skharness.autocode.config import Caps, Config
from skharness.autocode.types import GateResult, HarnessResult, QualityMode, RepoSpec

# --------------------------------------------------------------------------- #
# Fixtures / helpers (mirrors test_agentrun_bridge.py)                        #
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _isolate_dirs(monkeypatch, tmp_path):
    """Real RunHandle + cost ledger backed by throwaway dirs: never touch the
    live ~/.skcapstone/coordination/autopilot/runs/ or ~/.skcapstone/autopilot-cost/."""
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


class _FakeHarness:
    """A harness stand-in with a real ``run_task`` the bridge can wrap."""

    name = "fake"

    def __init__(self, tokens: int = 555, cost_usd: float = 1.23) -> None:
        self._tokens = tokens
        self._cost_usd = cost_usd
        self.calls = 0

    def run_task(self, brief):
        self.calls += 1
        return HarnessResult(ok=True, artifact=None, tokens=self._tokens,
                             cost_usd=self._cost_usd, raw={})


def _wire_common(mocker, card, cfg, harness):
    mocker.patch("skcapstone.mcp_tools._helpers._shared_root",
                 return_value=Path("/nonexistent-agentrun-bridge-cost-test-home"))
    mocker.patch("skcoord.card_store.CardStore.fold", return_value=card)
    mocker.patch("skharness.autocode.agentrun_bridge.Config.load", return_value=cfg)
    mocker.patch("skharness.autocode.agentrun_bridge.build_harness", return_value=harness)


def _passing_run_calls_harness(self, item, harness):
    """A stand-in for AgentRunDirectExecutor.run that (unlike the mocks in
    test_agentrun_bridge.py) actually calls harness.run_task, so the bridge's
    cost-capturing wrap has something real to observe."""
    self.journal.set_worktree(item.ref, "/fake/worktree")
    harness.run_task(_t.SimpleNamespace())
    return GateResult(score=None, passed=True, notes="direct mode: UNGATED single run",
                      artifact="/fake/worktree", mode="direct")


def _passing_ratify(*a, **k):
    return GateResult(score=5, passed=True, notes="<promise>COMPLETE</promise>", artifact=None)


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


# --------------------------------------------------------------------------- #
# 1. Happy run appends a ledger row carrying the harness's real cost/tokens   #
# --------------------------------------------------------------------------- #


@pytest.mark.needs_skcapstone
def test_happy_run_appends_ledger_row_with_harness_cost(mocker):
    card = _card(["repo:skrender"])
    cfg = _cfg()
    harness = _FakeHarness(tokens=555, cost_usd=1.23)
    _wire_common(mocker, card, cfg, harness)

    def _finalize(self, item, result):
        self.pr_url = "https://github.com/acme/skrender/pull/42"

    mocker.patch.object(AgentRunDirectExecutor, "run", _passing_run_calls_harness)
    mocker.patch("skharness.autocode.agentrun_bridge.ratify", side_effect=_passing_ratify)
    mocker.patch.object(AgentRunDirectExecutor, "finalize", _finalize)

    result = execute_dispatch(_context(card_id=card.id))

    assert result["links"]["pr"] == "https://github.com/acme/skrender/pull/42"
    assert harness.calls == 1          # confirms the real run_task path was exercised

    rows = autopilot_cost._read_ledger()
    assert len(rows) == 1
    row = rows[0]
    assert row["card_id"] == card.id
    assert row["repo"] == "skrender"
    assert row["tokens"] == 555
    assert row["cost_usd"] == 1.23
    assert row["joules"] == round(1.23 * autopilot_cost.JOULE_PER_USD)
    assert row["passed"] is True
    assert row["pr"] == "https://github.com/acme/skrender/pull/42"
    assert row["date"] == _today()

    # The activity trail carries a human-readable cost line too.
    cost_lines = [a["text"] for a in result["activity"] if "run cost" in a.get("text", "")]
    assert len(cost_lines) == 1
    assert "$1.2300" in cost_lines[0]
    assert "555 tokens" in cost_lines[0]


@pytest.mark.needs_skcapstone
def test_non_passing_run_still_records_a_ledger_row_with_empty_pr(mocker):
    card = _card(["repo:skrender"])
    cfg = _cfg()
    harness = _FakeHarness(tokens=42, cost_usd=0.05)
    _wire_common(mocker, card, cfg, harness)

    def _failing_run(self, item, harness):
        self.journal.set_worktree(item.ref, "/fake/worktree")
        harness.run_task(_t.SimpleNamespace())
        return GateResult(score=None, passed=False, notes="diff was empty",
                          artifact="/fake/worktree", mode="direct")

    mocker.patch.object(AgentRunDirectExecutor, "run", _failing_run)
    mocker.patch("skharness.autocode.agentrun_bridge.ratify", side_effect=_passing_ratify)
    finalize_spy = mocker.patch.object(AgentRunDirectExecutor, "finalize")
    mocker.patch.object(AgentRunDirectExecutor, "prune_worktree")

    result = execute_dispatch(_context(card_id=card.id))

    assert result["summary"].startswith("execute refused (bridge):")
    finalize_spy.assert_not_called()          # never finalizes a non-passing run

    rows = autopilot_cost._read_ledger()
    assert len(rows) == 1
    assert rows[0]["passed"] is False
    assert rows[0]["pr"] == ""
    assert rows[0]["tokens"] == 42
    assert rows[0]["cost_usd"] == 0.05


# --------------------------------------------------------------------------- #
# 2. Pre-existing day_total over the cap refuses WITHOUT running the engine   #
# --------------------------------------------------------------------------- #


@pytest.mark.needs_skcapstone
def test_dispatch_refuses_without_running_engine_when_already_over_cap(mocker):
    today = _today()
    # Pre-populate the ledger with a prior run that already spent past the cap.
    autopilot_cost.record_run(card_id="earlier-task", repo="skrender", tokens=10,
                              cost_usd=2.0, passed=True, pr="https://x/pr/1",
                              ts=f"{today}T00:00:00+00:00")

    card = _card(["repo:skrender"])
    cfg = _cfg(max_usd_per_day=1.0)          # cap already exceeded by the row above
    harness = _FakeHarness()
    _wire_common(mocker, card, cfg, harness)

    run_spy = mocker.patch.object(AgentRunDirectExecutor, "run")
    ratify_spy = mocker.patch("skharness.autocode.agentrun_bridge.ratify")
    alert_spy = mocker.patch("skharness.autocode.autopilot_cost.alert", return_value=True)

    result = execute_dispatch(_context(card_id=card.id))

    assert result["links"] == {}
    assert result["summary"].startswith("execute refused (bridge):")
    assert "daily cost cap" in result["summary"]
    assert "$1.00" in result["summary"]
    assert "$2.00" in result["summary"]

    run_spy.assert_not_called()
    ratify_spy.assert_not_called()
    alert_spy.assert_called_once()
    assert alert_spy.call_args.args[1] == "chef-dm"      # cfg.digest_chat is None -> fallback

    # The refusal itself is not a "run": it must NOT add a second ledger row.
    rows = autopilot_cost._read_ledger()
    assert len(rows) == 1
    assert rows[0]["card_id"] == "earlier-task"


@pytest.mark.needs_skcapstone
def test_dispatch_proceeds_when_under_cap(mocker):
    today = _today()
    autopilot_cost.record_run(card_id="earlier-task", repo="skrender", tokens=10,
                              cost_usd=1.0, passed=True, pr="https://x/pr/1",
                              ts=f"{today}T00:00:00+00:00")

    card = _card(["repo:skrender"])
    cfg = _cfg(max_usd_per_day=25.0)          # well under the cap
    harness = _FakeHarness(tokens=10, cost_usd=0.10)
    _wire_common(mocker, card, cfg, harness)

    def _finalize(self, item, result):
        self.pr_url = "https://github.com/acme/skrender/pull/7"

    mocker.patch.object(AgentRunDirectExecutor, "run", _passing_run_calls_harness)
    mocker.patch("skharness.autocode.agentrun_bridge.ratify", side_effect=_passing_ratify)
    mocker.patch.object(AgentRunDirectExecutor, "finalize", _finalize)

    result = execute_dispatch(_context(card_id=card.id))

    assert result["links"]["pr"] == "https://github.com/acme/skrender/pull/7"
    assert harness.calls == 1
    rows = autopilot_cost._read_ledger()
    assert len(rows) == 2          # the pre-existing row + this run's row
