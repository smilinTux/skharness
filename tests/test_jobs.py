"""skharness.jobs: the read-only JobRun view over the cron ledger (spec section
8, card C-8). Never touches the real fleet ledger -- every test here writes its
own tmp_path fixture file, per the card's explicit instruction.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from skharness.jobs import (
    DEFAULT_STALE_WINDOW_S,
    JobRun,
    default_ledger_path,
    read_job_runs,
)


def _write(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _line(**kw) -> str:
    return json.dumps(kw)


# ---- basic shape ---------------------------------------------------------

def test_missing_ledger_returns_empty_list_not_an_error(tmp_path):
    missing = tmp_path / "does-not-exist.jsonl"
    assert read_job_runs(missing) == []


def test_empty_ledger_returns_empty_list(tmp_path):
    ledger = tmp_path / "cron-ledger.jsonl"
    ledger.write_text("", encoding="utf-8")
    assert read_job_runs(ledger) == []


def test_one_job_one_run_reports_unknown_cadence_fallback_window(tmp_path):
    ledger = tmp_path / "cron-ledger.jsonl"
    _write(ledger, [_line(job="drchiro-ingest", host="noroc2027",
                          start="2026-08-11T22:30:01-04:00", dur_s=1, exit=0,
                          ok=True, tail="0 new")])
    rows = read_job_runs(ledger, now=_iso_to_epoch("2026-08-11T22:35:01-04:00"))
    assert len(rows) == 1
    row = rows[0]
    assert row.job == "drchiro-ingest"
    assert row.status == "ok"
    assert row.dur_s == 1.0
    assert row.tail == "0 new"
    # A single run has no inferred cadence, so the fallback window applies.
    assert row.stale_threshold_s == DEFAULT_STALE_WINDOW_S
    assert row.stale is False  # only 5 minutes old, well under the 24h fallback


def _iso_to_epoch(iso: str) -> float:
    from datetime import datetime
    return datetime.fromisoformat(iso).timestamp()


# ---- multiple jobs, latest wins -------------------------------------------

def test_multiple_jobs_each_report_their_own_latest_run(tmp_path):
    ledger = tmp_path / "cron-ledger.jsonl"
    _write(ledger, [
        _line(job="a", host="h", start="2026-08-11T10:00:00+00:00", ok=True, tail="first"),
        _line(job="b", host="h", start="2026-08-11T10:05:00+00:00", ok=False, tail="broke"),
        _line(job="a", host="h", start="2026-08-11T11:00:00+00:00", ok=True, tail="second"),
    ])
    rows = {r.job: r for r in read_job_runs(ledger, now=_iso_to_epoch("2026-08-11T11:05:00+00:00"))}
    assert set(rows) == {"a", "b"}
    assert rows["a"].tail == "second"
    assert rows["a"].last_start == "2026-08-11T11:00:00+00:00"
    assert rows["b"].status == "failed"


def test_rows_are_sorted_by_job_name(tmp_path):
    ledger = tmp_path / "cron-ledger.jsonl"
    _write(ledger, [
        _line(job="zeta", start="2026-08-11T10:00:00+00:00", ok=True),
        _line(job="alpha", start="2026-08-11T10:00:00+00:00", ok=True),
    ])
    rows = read_job_runs(ledger)
    assert [r.job for r in rows] == ["alpha", "zeta"]


# ---- staleness from inferred cadence --------------------------------------

def test_staleness_is_flagged_once_past_the_jobs_own_inferred_cadence(tmp_path):
    ledger = tmp_path / "cron-ledger.jsonl"
    # Runs every 30 minutes historically.
    _write(ledger, [
        _line(job="ai-runner", start="2026-08-11T10:00:00+00:00", ok=True),
        _line(job="ai-runner", start="2026-08-11T10:30:00+00:00", ok=True),
        _line(job="ai-runner", start="2026-08-11T11:00:00+00:00", ok=True),
    ])
    # 2 hours after the last run: way past 3x the 30-minute cadence.
    stale_now = _iso_to_epoch("2026-08-11T13:00:00+00:00")
    row = read_job_runs(ledger, now=stale_now)[0]
    assert row.stale_threshold_s == pytest.approx(3 * 1800.0)
    assert row.staleness_s == pytest.approx(2 * 3600.0)
    assert row.stale is True


def test_freshness_within_cadence_is_not_stale(tmp_path):
    ledger = tmp_path / "cron-ledger.jsonl"
    _write(ledger, [
        _line(job="ai-runner", start="2026-08-11T10:00:00+00:00", ok=True),
        _line(job="ai-runner", start="2026-08-11T10:30:00+00:00", ok=True),
        _line(job="ai-runner", start="2026-08-11T11:00:00+00:00", ok=True),
    ])
    fresh_now = _iso_to_epoch("2026-08-11T11:10:00+00:00")
    row = read_job_runs(ledger, now=fresh_now)[0]
    assert row.stale is False


# ---- fail-safe on malformed / truncated / partial content -----------------

def test_blank_lines_are_skipped(tmp_path):
    ledger = tmp_path / "cron-ledger.jsonl"
    _write(ledger, [
        "",
        "   ",
        _line(job="a", start="2026-08-11T10:00:00+00:00", ok=True),
        "",
    ])
    rows = read_job_runs(ledger)
    assert [r.job for r in rows] == ["a"]


def test_malformed_final_line_does_not_raise_and_earlier_data_survives(tmp_path):
    """The load-bearing fail-safe test: the ledger is appended to by other
    processes while this reads it, so a half-written last line (this is what a
    concurrent writer's in-progress append looks like mid-flush) is normal, not
    exceptional. It must produce a clean partial response, never a 500."""
    ledger = tmp_path / "cron-ledger.jsonl"
    good_line = _line(job="drchiro-ingest", host="noroc2027",
                      start="2026-08-11T22:30:01-04:00", ok=True, tail="0 new")
    truncated_last_line = '{"job": "ingest-order", "host": "noroc2027", "sta'
    ledger.write_text(good_line + "\n" + truncated_last_line, encoding="utf-8")

    rows = read_job_runs(ledger)  # must not raise

    assert [r.job for r in rows] == ["drchiro-ingest"]


def test_line_with_non_string_or_empty_job_name_is_skipped(tmp_path):
    ledger = tmp_path / "cron-ledger.jsonl"
    _write(ledger, [
        _line(job=None, start="2026-08-11T10:00:00+00:00", ok=True),
        _line(job="", start="2026-08-11T10:00:00+00:00", ok=True),
        _line(job=123, start="2026-08-11T10:00:00+00:00", ok=True),
        _line(job="real", start="2026-08-11T10:00:00+00:00", ok=True),
    ])
    rows = read_job_runs(ledger)
    assert [r.job for r in rows] == ["real"]


def test_line_that_is_valid_json_but_not_an_object_is_skipped(tmp_path):
    ledger = tmp_path / "cron-ledger.jsonl"
    ledger.write_text('[1, 2, 3]\n"just a string"\n42\n' +
                      _line(job="real", start="2026-08-11T10:00:00+00:00", ok=True) + "\n",
                      encoding="utf-8")
    rows = read_job_runs(ledger)
    assert [r.job for r in rows] == ["real"]


def test_missing_start_field_reports_unknown_staleness_but_still_a_row(tmp_path):
    ledger = tmp_path / "cron-ledger.jsonl"
    _write(ledger, [_line(job="no-timestamp", ok=True, tail="ran somehow")])
    row = read_job_runs(ledger)[0]
    assert row.job == "no-timestamp"
    assert row.status == "ok"
    assert row.last_start is None
    assert row.staleness_s is None
    # Cannot claim freshness without a timestamp: fail-safe toward "stale"
    # (surfacing a possible problem) rather than silently hiding it as fresh.
    assert row.stale is True


def test_unparseable_start_string_degrades_gracefully(tmp_path):
    ledger = tmp_path / "cron-ledger.jsonl"
    _write(ledger, [_line(job="weird-clock", start="not-a-timestamp", ok=True)])
    row = read_job_runs(ledger)[0]
    assert row.job == "weird-clock"
    # last_start passes through the ledger's own raw text verbatim (nothing to
    # gain by hiding it), even though it could not be parsed into a datetime;
    # staleness math is what degrades, never this field.
    assert row.last_start == "not-a-timestamp"
    assert row.staleness_s is None
    assert row.stale is True


def test_missing_ok_field_reports_unknown_status(tmp_path):
    ledger = tmp_path / "cron-ledger.jsonl"
    _write(ledger, [_line(job="a", start="2026-08-11T10:00:00+00:00")])
    row = read_job_runs(ledger)[0]
    assert row.status == "unknown"


def test_non_numeric_dur_s_degrades_to_none(tmp_path):
    ledger = tmp_path / "cron-ledger.jsonl"
    _write(ledger, [_line(job="a", start="2026-08-11T10:00:00+00:00", ok=True,
                          dur_s="not-a-number")])
    row = read_job_runs(ledger)[0]
    assert row.dur_s is None


# ---- default path + env override ------------------------------------------

def test_default_ledger_path_honors_env_override(monkeypatch, tmp_path):
    fake = tmp_path / "custom-ledger.jsonl"
    monkeypatch.setenv("SKCODE_CRON_LEDGER_PATH", str(fake))
    assert default_ledger_path() == fake


def test_default_ledger_path_falls_back_to_skcapstone_logs(monkeypatch):
    monkeypatch.delenv("SKCODE_CRON_LEDGER_PATH", raising=False)
    path = default_ledger_path()
    assert path == Path.home() / ".skcapstone" / "logs" / "cron-ledger.jsonl"


# ---- JobRun.to_dict ---------------------------------------------------------

def test_jobrun_to_dict_round_trips_all_fields():
    row = JobRun(job="x", host="h", last_start="t", status="ok", dur_s=1.0,
                tail="tail", staleness_s=2.0, stale=False, stale_threshold_s=3.0)
    d = row.to_dict()
    assert d == {
        "job": "x", "host": "h", "last_start": "t", "status": "ok", "dur_s": 1.0,
        "tail": "tail", "staleness_s": 2.0, "stale": False, "stale_threshold_s": 3.0,
    }
