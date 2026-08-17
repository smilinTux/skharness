"""Tests for skharness.autocode.autopilot_cost: the persistent per-run cost
ledger, its today/7d/30d/all-time/by-repo aggregates, and the dedup'd
daily-cost / per-run-token cap alerts.

No em/en dashes anywhere (SKWorld hard rule).
"""
from __future__ import annotations

import json
import types as _t

import pytest

from skharness.autocode import autopilot_cost


@pytest.fixture(autouse=True)
def _isolate_cost_dir(monkeypatch, tmp_path):
    """Never touch the live ~/.skcapstone/autopilot-cost/."""
    monkeypatch.setenv("SKAI_COST_DIR", str(tmp_path / "autopilot-cost"))


def _cfg(max_usd_per_day=25.0, max_tokens_per_run=2_000_000, digest_chat=None):
    caps = _t.SimpleNamespace(max_usd_per_day=max_usd_per_day,
                              max_tokens_per_run=max_tokens_per_run)
    return _t.SimpleNamespace(caps=caps, digest_chat=digest_chat)


def _write_rows(rows: list[dict]) -> None:
    path = autopilot_cost.ledger_path()
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


# --------------------------------------------------------------------------- #
# cost_dir / ledger_path                                                      #
# --------------------------------------------------------------------------- #


def test_cost_dir_created_and_env_overridable(tmp_path, monkeypatch):
    override = tmp_path / "custom-cost-dir"
    monkeypatch.setenv("SKAI_COST_DIR", str(override))
    d = autopilot_cost.cost_dir()
    assert d == override
    assert d.is_dir()


def test_ledger_path_is_under_cost_dir():
    assert autopilot_cost.ledger_path() == autopilot_cost.cost_dir() / "ledger.jsonl"


# --------------------------------------------------------------------------- #
# record_run                                                                  #
# --------------------------------------------------------------------------- #


def test_record_run_appends_correct_row():
    autopilot_cost.record_run(card_id="task-1", repo="skrender", tokens=1234,
                              cost_usd=0.5, passed=True, pr="https://x/pr/1",
                              ts="2026-08-13T10:00:00+00:00")
    rows = autopilot_cost._read_ledger()
    assert len(rows) == 1
    row = rows[0]
    assert row["card_id"] == "task-1"
    assert row["repo"] == "skrender"
    assert row["tokens"] == 1234
    assert row["cost_usd"] == 0.5
    assert row["passed"] is True
    assert row["pr"] == "https://x/pr/1"
    assert row["ts"] == "2026-08-13T10:00:00+00:00"
    assert row["date"] == "2026-08-13"
    assert row["joules"] == round(0.5 * autopilot_cost.JOULE_PER_USD)


def test_record_run_joules_is_a_unit_conversion_at_the_real_rate():
    # Joules are the canonical SKWorld cost unit; this asserts the actual
    # conversion (round(cost_usd * JOULE_PER_USD)), not a hardcoded literal,
    # so the test still holds if the knob (joules.DEFAULT_JOULE_PER_USD) moves.
    autopilot_cost.record_run(card_id="j1", repo="skrender", tokens=1,
                              cost_usd=3.5, passed=True, pr="",
                              ts="2026-08-13T00:00:00+00:00")
    row = autopilot_cost._read_ledger()[0]
    assert row["joules"] == round(3.5 * autopilot_cost.JOULE_PER_USD)
    assert autopilot_cost.JOULE_PER_USD == pytest.approx(50.0)   # skjoule's real knob


def test_record_run_appends_multiple_rows_in_order():
    autopilot_cost.record_run(card_id="a", repo="r", tokens=1, cost_usd=0.1,
                              passed=True, pr="", ts="2026-08-13T00:00:00+00:00")
    autopilot_cost.record_run(card_id="b", repo="r", tokens=2, cost_usd=0.2,
                              passed=False, pr="", ts="2026-08-13T01:00:00+00:00")
    rows = autopilot_cost._read_ledger()
    assert [r["card_id"] for r in rows] == ["a", "b"]


def test_record_run_never_raises_on_unwritable_dir(monkeypatch, tmp_path):
    # Point SKAI_COST_DIR at a path that cannot be created (a file, not a dir).
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir")
    monkeypatch.setenv("SKAI_COST_DIR", str(blocker / "nested"))
    # Must not raise.
    autopilot_cost.record_run(card_id="x", repo="r", tokens=1, cost_usd=0.1,
                              passed=True, pr="", ts="2026-08-13T00:00:00+00:00")


# --------------------------------------------------------------------------- #
# record_run: outcome fields (S3, card 20710266)                              #
# --------------------------------------------------------------------------- #


def test_record_run_writes_the_new_outcome_fields():
    autopilot_cost.record_run(
        card_id="s3-1", repo="skharness", tokens=500, cost_usd=0.25,
        passed=True, pr="https://x/pr/9", ts="2026-08-17T00:00:00+00:00",
        run_id="airun-s3-1-20260817T000000Z",
        outcome="merged", adapter="pi", model_requested="sk-default",
        model_served="qwen3.6-32b", score=4, retries=1,
        quality_mode="gated", work_grade={"size": "M", "risk": "med",
                                          "sensitivity": "internal",
                                          "model_class": "mid"})
    row = autopilot_cost._read_ledger()[0]
    assert row["outcome"] == "merged"
    assert row["adapter"] == "pi"
    assert row["model_requested"] == "sk-default"
    assert row["model_served"] == "qwen3.6-32b"
    assert row["score"] == 4
    assert row["retries"] == 1
    assert row["quality_mode"] == "gated"
    assert row["work_grade"] == {"size": "M", "risk": "med",
                                 "sensitivity": "internal", "model_class": "mid"}


def test_record_run_model_served_is_optional_and_never_defaults_to_model_requested():
    # This is the negative control the .100 outage demanded: skgateway can
    # silently serve a cloud model for a sovereign request. If model_served
    # defaulted to model_requested, that exact divergence would be erased at
    # the point of record, which is the one fact this field exists to keep.
    autopilot_cost.record_run(
        card_id="s3-2", repo="skharness", tokens=10, cost_usd=0.01,
        passed=True, pr="", ts="2026-08-17T00:00:00+00:00",
        model_requested="sk-default")
    row = autopilot_cost._read_ledger()[0]
    assert row["model_requested"] == "sk-default"
    assert row["model_served"] is None
    assert row["model_served"] != row["model_requested"]


def test_record_run_new_fields_default_to_none_when_omitted():
    # None of the 8 new callers exist yet at the bridge's call site, so every
    # field must have a safe, non-inventing default.
    autopilot_cost.record_run(card_id="s3-3", repo="r", tokens=1, cost_usd=0.1,
                              passed=True, pr="", ts="2026-08-13T00:00:00+00:00")
    row = autopilot_cost._read_ledger()[0]
    for key in ("outcome", "adapter", "model_requested", "model_served",
                "score", "quality_mode", "work_grade"):
        assert row[key] is None, f"{key} should default to None, got {row[key]!r}"
    assert row["retries"] == 0


def test_read_ledger_parses_a_pre_change_fixture_row_with_no_outcome_fields():
    # A row exactly as record_run wrote it BEFORE this change: none of the 8
    # new keys present at all. NO BACKFILL means this must still read cleanly,
    # and nothing on the read path may invent a value for the missing keys.
    _write_rows([{
        "ts": "2026-08-16T12:00:00+00:00", "date": "2026-08-16",
        "card_id": "pre-change-1", "repo": "skharness", "tokens": 999,
        "cost_usd": 1.23, "joules": 62, "passed": True, "pr": "",
        "run_id": "airun-pre-change-1-20260816T120000Z",
    }])
    row = autopilot_cost._read_ledger()[0]
    assert row["card_id"] == "pre-change-1"
    assert row["passed"] is True
    for key in ("outcome", "adapter", "model_requested", "model_served",
                "score", "retries", "quality_mode", "work_grade"):
        assert key not in row


# --------------------------------------------------------------------------- #
# day_total                                                                   #
# --------------------------------------------------------------------------- #


def test_day_total_sums_only_matching_date():
    _write_rows([
        {"ts": "2026-08-13T01:00:00+00:00", "date": "2026-08-13", "card_id": "a",
         "repo": "r1", "tokens": 100, "cost_usd": 1.0, "passed": True, "pr": ""},
        {"ts": "2026-08-13T02:00:00+00:00", "date": "2026-08-13", "card_id": "b",
         "repo": "r2", "tokens": 200, "cost_usd": 2.5, "passed": True, "pr": ""},
        {"ts": "2026-08-12T02:00:00+00:00", "date": "2026-08-12", "card_id": "c",
         "repo": "r1", "tokens": 50, "cost_usd": 5.0, "passed": True, "pr": ""},
    ])
    total = autopilot_cost.day_total("2026-08-13")
    assert total == {"cost_usd": 3.5, "joules": 0, "tokens": 300, "runs": 2}


def test_day_total_zero_for_unseen_date():
    total = autopilot_cost.day_total("2099-01-01")
    assert total == {"cost_usd": 0.0, "joules": 0, "tokens": 0, "runs": 0}


def test_day_total_zero_for_empty_ledger():
    assert autopilot_cost.day_total("2026-08-13") == {
        "cost_usd": 0.0, "joules": 0, "tokens": 0, "runs": 0}


# --------------------------------------------------------------------------- #
# summary                                                                     #
# --------------------------------------------------------------------------- #


def _row(date, repo, cost, tokens, card_id="c"):
    # Include joules the way record_run would (round(cost * JOULE_PER_USD)),
    # so summary()'s aggregation is exercised end-to-end for the real unit,
    # not just the pre-migration "no joules key" backward-compat path.
    return {"ts": f"{date}T00:00:00+00:00", "date": date, "card_id": card_id,
            "repo": repo, "tokens": tokens, "cost_usd": cost,
            "joules": round(cost * autopilot_cost.JOULE_PER_USD),
            "passed": True, "pr": ""}


def test_summary_today_7d_30d_all_time_and_by_repo():
    today = "2026-08-13"
    _write_rows([
        _row(today, "skrender", 1.0, 100),               # today
        _row("2026-08-10", "skrender", 2.0, 200),          # 3 days ago -> in 7d, 30d
        _row("2026-08-05", "skchat", 3.0, 300),             # 8 days ago -> in 30d only
        _row("2026-07-01", "skchat", 4.0, 400),             # 43 days ago -> all_time only
    ])
    s = autopilot_cost.summary(today=today, cap_usd=10.0)

    assert s["today"] == {"cost_usd": 1.0, "joules": 50, "tokens": 100, "runs": 1}
    assert s["last_7_days"] == {"cost_usd": 3.0, "joules": 150, "tokens": 300, "runs": 2}
    assert s["last_30_days"] == {"cost_usd": 6.0, "joules": 300, "tokens": 600, "runs": 3}
    assert s["all_time"] == {"cost_usd": 10.0, "joules": 500, "tokens": 1000, "runs": 4}
    assert s["by_repo"] == {
        "skrender": {"cost_usd": 3.0, "joules": 150, "tokens": 300, "runs": 2},
        "skchat": {"cost_usd": 7.0, "joules": 350, "tokens": 700, "runs": 2},
    }
    assert s["cap_usd"] == 10.0
    assert s["cap_joules"] == 500
    assert s["today_pct_of_cap"] == pytest.approx(10.0)


def test_summary_pct_of_cap_none_when_no_cap():
    today = "2026-08-13"
    _write_rows([_row(today, "skrender", 1.0, 100)])
    s = autopilot_cost.summary(today=today, cap_usd=None)
    assert s["today_pct_of_cap"] is None
    assert s["cap_joules"] is None


def test_summary_empty_ledger_is_all_zero():
    s = autopilot_cost.summary(today="2026-08-13", cap_usd=25.0)
    for key in ("today", "last_7_days", "last_30_days", "all_time"):
        assert s[key] == {"cost_usd": 0.0, "joules": 0, "tokens": 0, "runs": 0}
    assert s["by_repo"] == {}
    assert s["today_pct_of_cap"] == 0.0
    assert s["cap_joules"] == round(25.0 * autopilot_cost.JOULE_PER_USD)


# --------------------------------------------------------------------------- #
# daily_series                                                                #
# --------------------------------------------------------------------------- #


def test_daily_series_length_matches_days_and_is_continuous():
    today = "2026-08-13"
    series = autopilot_cost.daily_series(today=today, days=30)
    assert len(series) == 30
    dates = [row["date"] for row in series]
    assert dates == sorted(dates)               # oldest first
    assert dates[-1] == today                    # today is the last entry
    assert dates[0] == "2026-07-15"               # 29 days before today


def test_daily_series_zero_fills_days_with_no_runs():
    today = "2026-08-13"
    series = autopilot_cost.daily_series(today=today, days=5)
    assert len(series) == 5
    for row in series:
        assert row == {"date": row["date"], "cost_usd": 0.0, "joules": 0,
                        "tokens": 0, "runs": 0}


def test_daily_series_sums_per_day_and_zero_fills_the_rest():
    today = "2026-08-13"
    _write_rows([
        _row(today, "skrender", 1.0, 100, card_id="a"),
        _row(today, "skchat", 2.0, 200, card_id="b"),         # same day, second repo
        _row("2026-08-11", "skrender", 5.0, 500, card_id="c"),  # 2 days ago
        _row("2026-07-01", "skchat", 9.0, 900, card_id="d"),    # outside the 5-day window
    ])
    series = autopilot_cost.daily_series(today=today, days=5)
    by_date = {row["date"]: row for row in series}

    assert by_date[today] == {"date": today, "cost_usd": 3.0, "joules": 150,
                               "tokens": 300, "runs": 2}
    assert by_date["2026-08-11"] == {"date": "2026-08-11", "cost_usd": 5.0,
                                      "joules": 250, "tokens": 500, "runs": 1}
    assert by_date["2026-08-12"] == {"date": "2026-08-12", "cost_usd": 0.0,
                                      "joules": 0, "tokens": 0, "runs": 0}
    assert "2026-07-01" not in by_date            # outside the requested window


def test_daily_series_empty_ledger_is_all_zero():
    series = autopilot_cost.daily_series(today="2026-08-13", days=7)
    assert len(series) == 7
    assert all(row["cost_usd"] == 0.0 and row["joules"] == 0 for row in series)


def test_daily_series_never_raises_on_malformed_today(monkeypatch):
    # A bad "today" string must degrade to [] rather than propagate ValueError.
    assert autopilot_cost.daily_series(today="not-a-date", days=30) == []


# --------------------------------------------------------------------------- #
# recent_settlements                                                          #
# --------------------------------------------------------------------------- #


def test_recent_settlements_empty_on_missing_file():
    assert autopilot_cost.recent_settlements(limit=20) == []


def test_recent_settlements_newest_first():
    autopilot_cost.record_settlement(
        card_id="task-1", commit_sha="aaa", agent="lumina",
        minted=100, spent=0, net=100, balance_after=100,
        ts="2026-08-11T00:00:00+00:00")
    autopilot_cost.record_settlement(
        card_id="task-2", commit_sha="bbb", agent="opus",
        minted=50, spent=10, net=40, balance_after=140,
        ts="2026-08-12T00:00:00+00:00")
    autopilot_cost.record_settlement(
        card_id="task-3", commit_sha="ccc", agent="lumina",
        minted=25, spent=0, net=25, balance_after=165,
        ts="2026-08-13T00:00:00+00:00")

    rows = autopilot_cost.recent_settlements(limit=20)
    assert [r["card_id"] for r in rows] == ["task-3", "task-2", "task-1"]


def test_recent_settlements_respects_limit():
    for i in range(5):
        autopilot_cost.record_settlement(
            card_id=f"task-{i}", commit_sha="x", agent="lumina",
            minted=10, spent=0, net=10, balance_after=10 * (i + 1),
            ts=f"2026-08-{10 + i:02d}T00:00:00+00:00")

    rows = autopilot_cost.recent_settlements(limit=2)
    assert len(rows) == 2
    assert [r["card_id"] for r in rows] == ["task-4", "task-3"]


def test_recent_settlements_row_shape():
    autopilot_cost.record_settlement(
        card_id="task-1", commit_sha="deadbeef", agent="lumina",
        minted=100, spent=25, net=75, balance_after=575,
        ts="2026-08-13T00:00:00+00:00")
    rows = autopilot_cost.recent_settlements(limit=20)
    assert len(rows) == 1
    row = rows[0]
    for key in ("ts", "card_id", "commit_sha", "agent", "minted",
                "spent_joules", "net_joules", "balance_after"):
        assert key in row
    assert row["card_id"] == "task-1"
    assert row["minted"] == 100
    assert row["spent_joules"] == 25
    assert row["net_joules"] == 75
    assert row["balance_after"] == 575


def test_recent_settlements_never_raises_on_malformed_journal_line(tmp_path):
    path = autopilot_cost.settlements_path()
    with path.open("a", encoding="utf-8") as fh:
        fh.write("not valid json\n")
    assert autopilot_cost.recent_settlements(limit=20) == []


# --------------------------------------------------------------------------- #
# alert (never call the real sk-alert)                                       #
# --------------------------------------------------------------------------- #


def test_alert_true_on_rc_zero(mocker):
    mocker.patch("shutil.which", return_value="/usr/bin/sk-alert")
    mocker.patch("skharness.autocode.autopilot_cost.subprocess.run",
                 return_value=_t.SimpleNamespace(returncode=0, stdout="", stderr=""))
    assert autopilot_cost.alert("hello", "chef-dm") is True


def test_alert_false_on_nonzero_rc(mocker):
    mocker.patch("shutil.which", return_value="/usr/bin/sk-alert")
    mocker.patch("skharness.autocode.autopilot_cost.subprocess.run",
                 return_value=_t.SimpleNamespace(returncode=1, stdout="", stderr="boom"))
    assert autopilot_cost.alert("hello", "chef-dm") is False


def test_alert_false_on_subprocess_exception(mocker):
    mocker.patch("shutil.which", return_value=None)
    mocker.patch("skharness.autocode.autopilot_cost.subprocess.run",
                 side_effect=OSError("no such binary"))
    assert autopilot_cost.alert("hello", "chef-dm") is False


# --------------------------------------------------------------------------- #
# sentinel dedup                                                              #
# --------------------------------------------------------------------------- #


def test_sentinel_hit_false_then_true_after_mark():
    assert autopilot_cost._sentinel_hit("daily-usd", "2026-08-13") is False
    autopilot_cost._mark_sentinel("daily-usd", "2026-08-13")
    assert autopilot_cost._sentinel_hit("daily-usd", "2026-08-13") is True


def test_sentinel_scoped_by_kind_and_date():
    autopilot_cost._mark_sentinel("daily-usd", "2026-08-13")
    assert autopilot_cost._sentinel_hit("daily-usd", "2026-08-14") is False
    assert autopilot_cost._sentinel_hit("run-tokens", "2026-08-13") is False


# --------------------------------------------------------------------------- #
# check_and_alert_caps                                                        #
# --------------------------------------------------------------------------- #


def test_check_and_alert_caps_fires_daily_usd_once_per_day(mocker):
    spy = mocker.patch("skharness.autocode.autopilot_cost.alert", return_value=True)
    cfg = _cfg(max_usd_per_day=10.0, max_tokens_per_run=1_000_000)

    fired1 = autopilot_cost.check_and_alert_caps(
        cfg=cfg, today="2026-08-13", day_cost=10.0, this_run_tokens=0)
    assert fired1 == ["daily-usd"]
    assert spy.call_count == 1

    fired2 = autopilot_cost.check_and_alert_caps(
        cfg=cfg, today="2026-08-13", day_cost=15.0, this_run_tokens=0)
    assert fired2 == []
    assert spy.call_count == 1          # not fired again the same day


def test_check_and_alert_caps_daily_usd_message_carries_joule_figure(mocker):
    # Joules are the canonical SKWorld cost unit; the daily-cap alert must
    # lead a human reviewer to the joule number, not just the USD one.
    spy = mocker.patch("skharness.autocode.autopilot_cost.alert", return_value=True)
    cfg = _cfg(max_usd_per_day=20.0, max_tokens_per_run=1_000_000)

    autopilot_cost.check_and_alert_caps(
        cfg=cfg, today="2026-08-13", day_cost=20.0, this_run_tokens=0)

    text = spy.call_args.args[0]
    day_joules = round(20.0 * autopilot_cost.JOULE_PER_USD)
    cap_joules = round(20.0 * autopilot_cost.JOULE_PER_USD)
    assert f"{day_joules:,} J" in text
    assert f"{cap_joules:,} J" in text
    assert "$20.00" in text
    assert "J/$" in text


def test_check_and_alert_caps_fires_run_tokens_once_per_day(mocker):
    spy = mocker.patch("skharness.autocode.autopilot_cost.alert", return_value=True)
    cfg = _cfg(max_usd_per_day=1000.0, max_tokens_per_run=500)

    fired1 = autopilot_cost.check_and_alert_caps(
        cfg=cfg, today="2026-08-13", day_cost=0.0, this_run_tokens=500)
    assert fired1 == ["run-tokens"]
    assert spy.call_count == 1

    fired2 = autopilot_cost.check_and_alert_caps(
        cfg=cfg, today="2026-08-13", day_cost=0.0, this_run_tokens=600)
    assert fired2 == []
    assert spy.call_count == 1


def test_check_and_alert_caps_fires_neither_when_under_both_caps(mocker):
    spy = mocker.patch("skharness.autocode.autopilot_cost.alert", return_value=True)
    cfg = _cfg(max_usd_per_day=10.0, max_tokens_per_run=1000)

    fired = autopilot_cost.check_and_alert_caps(
        cfg=cfg, today="2026-08-13", day_cost=1.0, this_run_tokens=10)
    assert fired == []
    spy.assert_not_called()


def test_check_and_alert_caps_can_fire_both_in_one_call(mocker):
    spy = mocker.patch("skharness.autocode.autopilot_cost.alert", return_value=True)
    cfg = _cfg(max_usd_per_day=10.0, max_tokens_per_run=1000)

    fired = autopilot_cost.check_and_alert_caps(
        cfg=cfg, today="2026-08-13", day_cost=10.0, this_run_tokens=1000)
    assert set(fired) == {"daily-usd", "run-tokens"}
    assert spy.call_count == 2


def test_check_and_alert_caps_uses_digest_chat_or_chef_dm_fallback(mocker):
    spy = mocker.patch("skharness.autocode.autopilot_cost.alert", return_value=True)
    cfg = _cfg(max_usd_per_day=5.0, digest_chat="ops-room")

    autopilot_cost.check_and_alert_caps(
        cfg=cfg, today="2026-08-13", day_cost=5.0, this_run_tokens=0)
    assert spy.call_args.args[1] == "ops-room"


def test_check_and_alert_caps_falls_back_to_chef_dm(mocker):
    spy = mocker.patch("skharness.autocode.autopilot_cost.alert", return_value=True)
    cfg = _cfg(max_usd_per_day=5.0, digest_chat=None)

    autopilot_cost.check_and_alert_caps(
        cfg=cfg, today="2026-08-13", day_cost=5.0, this_run_tokens=0)
    assert spy.call_args.args[1] == "chef-dm"


def test_check_and_alert_caps_never_raises_when_alert_blows_up(mocker):
    mocker.patch("skharness.autocode.autopilot_cost.alert",
                 side_effect=RuntimeError("boom"))
    cfg = _cfg(max_usd_per_day=5.0)
    fired = autopilot_cost.check_and_alert_caps(
        cfg=cfg, today="2026-08-13", day_cost=5.0, this_run_tokens=0)
    assert fired == []          # swallowed, never raised


# --------------------------------------------------------------------------- #
# record_run: run_id back-compat field                                        #
# --------------------------------------------------------------------------- #


def test_record_run_defaults_run_id_to_empty_string():
    autopilot_cost.record_run(card_id="task-1", repo="skrender", tokens=1,
                              cost_usd=0.1, passed=True, pr="",
                              ts="2026-08-13T00:00:00+00:00")
    row = autopilot_cost._read_ledger()[0]
    assert row["run_id"] == ""


def test_record_run_carries_run_id_when_given():
    autopilot_cost.record_run(card_id="task-1", repo="skrender", tokens=1,
                              cost_usd=0.1, passed=True, pr="",
                              ts="2026-08-13T00:00:00+00:00",
                              run_id="airun-task-1-20260813T000000Z")
    row = autopilot_cost._read_ledger()[0]
    assert row["run_id"] == "airun-task-1-20260813T000000Z"


# --------------------------------------------------------------------------- #
# settlements.jsonl: already_settled / record_settlement                      #
# --------------------------------------------------------------------------- #


def test_settlements_path_is_under_cost_dir():
    assert autopilot_cost.settlements_path() == autopilot_cost.cost_dir() / "settlements.jsonl"


def test_already_settled_false_when_no_settlements_file():
    assert autopilot_cost.already_settled("task-1") is False


def test_already_settled_true_after_record_settlement():
    assert autopilot_cost.already_settled("task-1") is False
    autopilot_cost.record_settlement(
        card_id="task-1", commit_sha="deadbeef", agent="lumina",
        minted=100, spent=25, net=75, balance_after=575,
        ts="2026-08-13T00:00:00+00:00")
    assert autopilot_cost.already_settled("task-1") is True


def test_already_settled_scoped_by_card_id():
    autopilot_cost.record_settlement(
        card_id="task-1", commit_sha="deadbeef", agent="lumina",
        minted=100, spent=25, net=75, balance_after=575,
        ts="2026-08-13T00:00:00+00:00")
    assert autopilot_cost.already_settled("task-2") is False


def test_already_settled_true_even_for_a_different_commit_sha():
    # J1 hardening rule (design doc section 6): dedupe is by card_id ALONE, not
    # (card_id, commit_sha) -- a re-dispatch produces a fresh commit sha, and the
    # rule is "one settlement per card_id until an operator clears it".
    autopilot_cost.record_settlement(
        card_id="task-1", commit_sha="aaaa111", agent="lumina",
        minted=100, spent=25, net=75, balance_after=575,
        ts="2026-08-13T00:00:00+00:00")
    assert autopilot_cost.already_settled("task-1") is True


def test_record_settlement_row_shape():
    autopilot_cost.record_settlement(
        card_id="task-1", commit_sha="deadbeef", agent="lumina",
        minted=100, spent=25, net=75, balance_after=575,
        ts="2026-08-13T00:00:00+00:00")
    rows = autopilot_cost._read_settlements()
    assert len(rows) == 1
    row = rows[0]
    assert row == {
        "ts": "2026-08-13T00:00:00+00:00", "card_id": "task-1",
        "commit_sha": "deadbeef", "agent": "lumina", "minted": 100,
        "spent_joules": 25, "net_joules": 75, "balance_after": 575,
        "state": "settled",
    }


def test_record_settlement_never_raises_on_unwritable_dir(monkeypatch, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir")
    monkeypatch.setenv("SKAI_COST_DIR", str(blocker / "nested"))
    # Must not raise.
    autopilot_cost.record_settlement(
        card_id="task-1", commit_sha="deadbeef", agent="lumina",
        minted=100, spent=25, net=75, balance_after=575,
        ts="2026-08-13T00:00:00+00:00")


def test_already_settled_never_raises_on_malformed_journal_line(tmp_path):
    path = autopilot_cost.settlements_path()
    with path.open("a", encoding="utf-8") as fh:
        fh.write("not valid json\n")
    # Malformed line is skipped, not fatal -- reads as "not settled".
    assert autopilot_cost.already_settled("task-1") is False
