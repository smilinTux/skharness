"""Tests for autocode build economics (SKJoule accounting).

These exercise the real skcapstone.skjoule wallet against a temp home when the
sibling is installed; the pure helpers run everywhere.
"""

from __future__ import annotations

import importlib.util

import pytest

from skharness.autocode.joules import (
    BuildUsage,
    _priority_bucket,
    _quality_bucket,
    settle,
)

_HAS_SKJOULE = importlib.util.find_spec("skcapstone") is not None
requires_skjoule = pytest.mark.skipif(
    not _HAS_SKJOULE, reason="skcapstone sibling (skjoule) not installed"
)


def test_priority_bucket_normalizes():
    assert _priority_bucket("HIGH") == "high"
    assert _priority_bucket(None) == "medium"
    assert _priority_bucket("bogus") == "medium"
    assert _priority_bucket("critical") == "critical"


def test_quality_bucket_maps_score():
    assert _quality_bucket(5) == "excellent"
    assert _quality_bucket(4) == "good"
    assert _quality_bucket(3) == "acceptable"
    assert _quality_bucket(2) == "acceptable"
    assert _quality_bucket(1) == "needs_improvement"
    assert _quality_bucket(None) == "needs_improvement"


def test_build_usage_add_and_tokens():
    u = BuildUsage()
    u.add(input_tokens=100, output_tokens=50, cost_usd=0.20, turns=3)
    u.add(input_tokens=10, output_tokens=5, cost_usd=0.05, turns=1, model="claude-code")
    assert u.input_tokens == 110
    assert u.output_tokens == 55
    assert u.tokens == 165
    assert round(u.cost_usd, 2) == 0.25
    assert u.turns == 4


def test_from_claude_json_parses_usage_and_cost():
    raw = {
        "result": "done",
        "total_cost_usd": 0.4213,
        "num_turns": 7,
        "model": "claude-sonnet-x",
        "usage": {
            "input_tokens": 1200,
            "output_tokens": 340,
            "cache_read_input_tokens": 800,
            "cache_creation_input_tokens": 200,
        },
    }
    u = BuildUsage.from_claude_json(raw)
    assert u.output_tokens == 340
    assert u.input_tokens == 1200 + 800 + 200  # base + cache read + cache create
    assert u.cost_usd == 0.4213
    assert u.turns == 7
    assert u.model == "claude-sonnet-x"


def test_from_claude_json_tolerates_missing_fields():
    u = BuildUsage.from_claude_json({"result": "x"})
    assert u.tokens == 0 and u.cost_usd == 0.0 and u.turns == 0


# ── S10: BuildUsage.model must name the adapter that ACTUALLY ran ────────────
# pi and opencode return an envelope with no `usage` key, so the accrual took a
# fallback branch that never set model, and every cost row and settlement for a
# pi build claimed to be claude-code.

class _FakeResult:
    """The HarnessResult shape the accrual reads (ok/tokens/cost_usd/raw)."""

    def __init__(self, tokens=0, cost_usd=0.0, raw=None):
        self.ok = True
        self.artifact = None
        self.tokens = tokens
        self.cost_usd = cost_usd
        self.raw = raw if raw is not None else {}


def test_from_harness_result_records_the_adapter_on_the_fallback_branch():
    """A pi envelope: no `usage` key, no `model` key. The recorded model must be
    the adapter that ran, never the claude-code default."""
    u = BuildUsage.from_harness_result(_FakeResult(tokens=42, cost_usd=0.07),
                                       adapter="pi")
    assert u.model == "pi"
    assert u.output_tokens == 42 and u.cost_usd == 0.07 and u.turns == 1


def test_from_harness_result_prefers_the_model_the_envelope_names():
    """A claude-code envelope names the real model id; the adapter name must not
    overwrite a more specific truth."""
    raw = {"model": "claude-sonnet-x", "total_cost_usd": 0.5, "num_turns": 2,
           "usage": {"input_tokens": 10, "output_tokens": 5}}
    u = BuildUsage.from_harness_result(_FakeResult(raw=raw), adapter="claude-code")
    assert u.model == "claude-sonnet-x"
    assert u.input_tokens == 10 and u.output_tokens == 5


def test_from_harness_result_falls_back_to_adapter_when_envelope_omits_model():
    """A usage block with no model id: the adapter is still the best truth
    available, and it must not silently read as claude-code."""
    u = BuildUsage.from_harness_result(
        _FakeResult(raw={"usage": {"output_tokens": 3}}), adapter="opencode")
    assert u.model == "opencode"


def test_build_usage_default_model_does_not_claim_claude_code():
    """An unfed BuildUsage has measured nothing, so it must not name a vendor.
    The old default made 'never recorded' and 'ran on claude-code' identical."""
    assert BuildUsage().model == "unknown"


@requires_skjoule
def test_settle_mints_and_spends_real_pnl(tmp_path):
    usage = BuildUsage(model="claude-code", input_tokens=1000, output_tokens=500, cost_usd=0.50)
    econ = settle(
        "test-agent-econ",
        "card-abc",
        priority="medium",
        score=5,
        usage=usage,
        commit_sha="deadbeef",
        joule_per_usd=50.0,
        home=tmp_path,
    )
    assert econ.recorded is True
    # task_complete base 25 x medium(1.0) x excellent(3.0) = 75 minted
    assert econ.minted == 75
    # 0.50 USD x 50 J/$ = 25 spent
    assert econ.spent_joules == 25
    assert econ.spent_joules_actual == 25  # fresh wallet had 75 after mint
    assert econ.net_joules == 50
    assert econ.balance_after == 50
    assert econ.joules_per_usd == pytest.approx(150.0)  # 75 J / $0.50
    assert "net +50" in econ.summary()


@requires_skjoule
def test_settle_caps_spend_at_balance_floor(tmp_path):
    # A costly build that mints little: spend intent exceeds post-mint balance.
    usage = BuildUsage(cost_usd=10.0)  # 10 x 50 = 500 J intended
    econ = settle(
        "test-agent-poor",
        "card-x",
        priority="low",
        score=5,
        usage=usage,
        joule_per_usd=50.0,
        home=tmp_path,
    )
    # low(0.5) x excellent(3.0) x 25 = max(1, 37) = 37 minted
    assert econ.minted == 37
    assert econ.spent_joules == 500          # intended (true P&L)
    assert econ.spent_joules_actual == 37    # capped at balance floor (0)
    assert econ.net_joules == 37 - 500       # deeply negative: inefficient build
    assert econ.balance_after == 0


def test_settle_never_raises_without_skjoule(monkeypatch):
    # Force the skjoule import to fail; settle must degrade to recorded=False.
    import builtins

    real_import = builtins.__import__

    def _blocked(name, *a, **k):
        if name.startswith("skcapstone"):
            raise ImportError("blocked for test")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    econ = settle("a", "ref", priority="high", score=5, usage=BuildUsage(cost_usd=1.0))
    assert econ.recorded is False
    assert econ.minted == 0
