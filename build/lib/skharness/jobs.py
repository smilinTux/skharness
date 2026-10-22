"""JobRun: a read-only view over the cron run ledger (skcode Code-section card
C-8, spec 2026-08-11 section 8).

The Code section is a VIEW, never a store (spec section 8's one rule). Cron/
scheduler runs are NOT sessions and are never force-fitted into
``skharness.events.SessionEvent``: they are a different shape, ``JobRun``,
owned by the scheduler (the skos ai-runner / cron layer), not by hostd. This
module only READS the ledger the scheduler already appends to
(``~/.skcapstone/logs/cron-ledger.jsonl``, one JSON object per line, e.g.
``{"job": "drchiro-ingest", "host": "noroc2027", "start": "<iso8601>",
"dur_s": 0, "exit": 0, "ok": true, "tail": "..."}``) and reports on it; it
creates no new store and mutates nothing.

Freshness IS liveness (card C-8): there is no long-lived scheduler daemon, so
a ledger whose newest run for a job is older than that job's own expected
cadence reads as a stalled scheduler, not merely an old timestamp. Rather
than shipping raw timestamps and making the client re-derive "is this
stale", :func:`read_job_runs` computes ``stale`` / ``staleness_s`` here, once,
from the ledger's own history (the median interval between a job's recent
runs), so a fast job (minutes) and a slow job (weekly) are judged on their
own cadence instead of one global constant.

Fail-safe, always: the ledger is appended to by other processes while this
module reads it, so a half-written final line is the NORMAL case, not an
error. Every line is parsed independently inside its own try/except; a
missing file, an empty file, a blank line, a truncated/malformed JSON line,
or a line whose JSON is not a job record all get skipped rather than raised.
A caller can never get a 500 out of this module; at worst, an empty or
partial ``list[JobRun]``.
"""
from __future__ import annotations

import json
import os
import statistics
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

#: Default cron ledger path (or ``$SKCODE_CRON_LEDGER_PATH`` when set, mirroring
#: the ``SKCODE_STATE_DIR`` override convention elsewhere in this package so tests
#: never need to touch the real fleet ledger).
_LEDGER_ENV = "SKCODE_CRON_LEDGER_PATH"

#: How many of a job's most recent runs are used to infer its expected cadence
#: (the median gap between consecutive runs). Small on purpose: cadence can drift
#: (a schedule edit), and recent runs are the more honest signal.
_CADENCE_SAMPLE = 10

#: A job is "stale" once its last run is older than this multiple of its own
#: inferred cadence. 3x tolerates one missed run plus jitter without flapping.
STALE_MULTIPLIER = 3

#: Floor under the stale threshold so a very-frequent job (seconds/minutes
#: apart) does not flag stale on ordinary scheduling jitter.
STALE_FLOOR_S = 900.0  # 15 minutes

#: Fallback stale window used when a job has fewer than two parseable
#: timestamps to infer a cadence from (its first-ever ledger line, or every
#: prior line failed to parse). Generous, matching the widest ordinary
#: schedule (daily/weekly jobs) seen in jobs.yaml.
DEFAULT_STALE_WINDOW_S = 24.0 * 3600.0


def default_ledger_path() -> Path:
    """``~/.skcapstone/logs/cron-ledger.jsonl`` (or ``$SKCODE_CRON_LEDGER_PATH``).

    Never read directly by tests (card C-8): they point this at a tmp fixture
    file instead, so the suite never depends on live fleet cron state.
    """
    override = os.environ.get(_LEDGER_ENV)
    if override:
        return Path(override)
    return Path.home() / ".skcapstone" / "logs" / "cron-ledger.jsonl"


@dataclass
class JobRun:
    """One scheduler job's latest known state, as read from the cron ledger.

    Deliberately NOT a SessionEvent: a job is not a session, has no ``sid``,
    and the scheduler (not hostd) is its owner. ``tail`` is whatever short
    summary line the ledger already carries for the run; it is the log
    excerpt the card asks for ("view plus a link to logs only"), not a new
    log-serving surface of its own.
    """

    job: str
    host: str = ""
    last_start: str | None = None
    status: str = "unknown"  # "ok" | "failed" | "unknown"
    dur_s: float | None = None
    tail: str = ""
    staleness_s: float | None = None
    stale: bool = True
    stale_threshold_s: float = DEFAULT_STALE_WINDOW_S

    def to_dict(self) -> dict:
        return asdict(self)


def _parse_line(line: str) -> dict | None:
    """Parse one ledger line into a plain dict, or None on ANY problem.

    Blank lines, non-JSON lines (a truncated/half-written final append is
    exactly this), JSON that is not an object, and objects missing a
    non-empty ``job`` name are all treated the same way: skip, never raise.
    """
    line = line.strip()
    if not line:
        return None
    try:
        record = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    job = record.get("job")
    if not isinstance(job, str) or not job.strip():
        return None
    return record


def _parse_start(value: object) -> datetime | None:
    """Best-effort ISO-8601 parse of a ledger ``start`` field. None on failure,
    never raises: an unparseable timestamp degrades the job's staleness to
    "unknown", it never crashes the read."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _status_of(record: dict) -> str:
    ok = record.get("ok")
    if ok is True:
        return "ok"
    if ok is False:
        return "failed"
    return "unknown"


def _as_float(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def read_job_runs(path: Path | None = None, *, now: float | None = None) -> list[JobRun]:
    """Tail the cron ledger and return one :class:`JobRun` per job name.

    Fail-safe by construction (see module docstring): a missing file, an
    empty file, and any malformed/blank/truncated line all degrade to
    "nothing learned from that line", never an exception. Returns rows
    sorted by job name for a stable, testable response.

    ``now`` (epoch seconds) is injectable so staleness is deterministic in
    tests; it defaults to :func:`time.time`.
    """
    ledger = path if path is not None else default_ledger_path()
    now_s = time.time() if now is None else now

    # job -> ordered list of (parsed_dt_or_None, raw_record), file order == the
    # ledger's own append order (chronological, by construction of the writer).
    by_job: dict[str, list[tuple[datetime | None, dict]]] = {}
    try:
        with open(ledger, "r", encoding="utf-8") as fh:
            for raw_line in fh:
                record = _parse_line(raw_line)
                if record is None:
                    continue
                job = record["job"].strip()
                dt = _parse_start(record.get("start"))
                by_job.setdefault(job, []).append((dt, record))
    except OSError:
        # Missing ledger (fresh host, log not rotated in yet, permission hiccup):
        # an empty view, never a crash. The endpoint reports "no jobs known yet".
        return []

    rows: list[JobRun] = []
    for job, entries in by_job.items():
        latest_dt, latest_record = entries[-1]
        timestamps = [dt for dt, _ in entries if dt is not None]

        stale_threshold = DEFAULT_STALE_WINDOW_S
        if len(timestamps) >= 2:
            recent = timestamps[-_CADENCE_SAMPLE:]
            deltas = [
                (b - a).total_seconds()
                for a, b in zip(recent, recent[1:])
                if (b - a).total_seconds() > 0
            ]
            if deltas:
                stale_threshold = max(STALE_FLOOR_S, STALE_MULTIPLIER * statistics.median(deltas))

        staleness_s: float | None = None
        stale = True  # unknown timestamp => cannot claim freshness; report stale.
        if latest_dt is not None:
            staleness_s = now_s - latest_dt.timestamp()
            stale = staleness_s > stale_threshold

        rows.append(JobRun(
            job=job,
            host=str(latest_record.get("host", "") or ""),
            last_start=latest_record.get("start") if isinstance(latest_record.get("start"), str) else None,
            status=_status_of(latest_record),
            dur_s=_as_float(latest_record.get("dur_s")),
            tail=str(latest_record.get("tail", "") or ""),
            staleness_s=staleness_s,
            stale=stale,
            stale_threshold_s=stale_threshold,
        ))

    rows.sort(key=lambda r: r.job)
    return rows
