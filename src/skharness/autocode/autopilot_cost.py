"""Autopilot cost ledger: a persistent per-run cost/token record over time,
an overview aggregate, and a cap-hit alert (Chef's fleet cost tracker).

The agent-run execute bridge (agentrun_bridge.py) does not otherwise persist
per-run cost -- ``DirectExecutor.run`` discards the ``HarnessResult`` it gets
back from ``harness.run_task``. This module is the append-only ledger the
bridge writes to, plus the aggregates + alert primitive the overview CLI and
the daily-cap guard both need.

Ledger location: ``~/.skcapstone/autopilot-cost/`` (Syncthing-synced across
the fleet), overridable via ``SKAI_COST_DIR`` for tests. One JSON object per
line in ``ledger.jsonl``; every write is defensive -- a bug in cost tracking
must never turn a real run into a failure.

No em/en dashes anywhere (SKWorld hard rule).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from datetime import date as _date_cls
from datetime import timedelta
from pathlib import Path

log = logging.getLogger("skharness.autocode.autopilot_cost")

# Joules are the canonical SKWorld cost unit; USD is secondary/derived. Rate
# comes from the real skjoule knob (joules.DEFAULT_JOULE_PER_USD); the literal
# here is only a fallback so a broken/absent import can never break tracking.
JOULE_PER_USD = 50.0
try:
    from . import joules as _joules_mod
    JOULE_PER_USD = _joules_mod.DEFAULT_JOULE_PER_USD
except Exception:  # noqa: BLE001 -- fall back to the literal above, never raise
    pass


def _joules(cost_usd: float) -> int:
    """Convert a USD cost into joules at the current rate. This is a unit
    conversion ONLY -- it never touches JouleWallet/settle()/mint()/spend();
    wallet settlement is a separate decision being evaluated elsewhere."""
    return round(float(cost_usd or 0.0) * JOULE_PER_USD)


# --------------------------------------------------------------------------- #
# Paths                                                                       #
# --------------------------------------------------------------------------- #


def cost_dir() -> Path:
    """The cost ledger directory, created if missing. Overridable via
    ``SKAI_COST_DIR`` (tests never touch the live Syncthing-synced path)."""
    p = Path(os.environ.get("SKAI_COST_DIR", "~/.skcapstone/autopilot-cost")).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def ledger_path() -> Path:
    return cost_dir() / "ledger.jsonl"


def _today() -> str:
    """UTC today as an ISO date string. A convenience for callers that don't
    already have one; this module's own functions never call it internally
    -- every "now" is a value the caller passes in."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).date().isoformat()


# --------------------------------------------------------------------------- #
# Ledger read/write                                                           #
# --------------------------------------------------------------------------- #


def record_run(*, card_id: str, repo: str, tokens: int, cost_usd: float,
               passed: bool, pr: str, ts: str, run_id: str = "",
               outcome: str | None = None, adapter: str | None = None,
               model_requested: str | None = None,
               model_served: str | None = None, score: int | None = None,
               retries: int = 0, quality_mode: str | None = None,
               work_grade: dict | None = None,
               terminal_state: str | None = None,
               escalation_reason: str | None = None) -> None:
    """Append one run to the ledger. Never raises -- a cost-tracking bug must
    never turn a successful (or a well-formed failed) run into a crash.

    ``run_id`` (default "" for back-compat) is the bridge's own journal handle
    stamp (``airun-<card_id>-<YYYYmmddTHHMMSSZ>``), so ledger rows join cleanly
    against the run journal and the settlement journal (design doc section 6).

    The eight parameters below (S3, card 20710266) name the same event card
    8967bf22 (A1)'s RunRecord schema describes; field names are chosen to
    match it where both schemas cover the same fact (``adapter``,
    ``model_requested``, ``model_served``, ``quality_mode``, ``score``,
    ``outcome``), so the two do not diverge:

    ``model_served`` is deliberately Optional with NO default derived from
    ``model_requested``. Defaulting it would manufacture the exact fact this
    field exists to detect: during the .100 outage on 2026-08-16, skgateway
    silently served a cloud model for a sovereign ``sk-default`` request. A
    caller that does not know what actually served the run must pass None,
    not the request it made.

    ``work_grade`` carries the Joule Economy grade dict (``size``, ``risk``,
    ``sensitivity``, ``model_class``) exactly as stored on the card, or None
    when the card is ungraded; this module never re-derives it.

    ``terminal_state`` (S4, card 432b81b7) names the terminal disposition of
    the run as its OWN DISPATCHER saw it, which is a different fact from
    ``outcome`` and must not be folded into it. ``outcome`` is the closed
    five-value GATE vocabulary (types.GATE_OUTCOMES) plus the UNRECORDED
    sentinel: it answers "what did the gate decide". ``terminal_state``
    answers "how did this item end", including the paths where NO build ran
    and so no gate ever decided anything:

      orchestrator (orchestrator.TERMINAL_STATES): ``finalized``,
        ``finalize-failed``, ``escalated``, ``claim-raced``, ``off-node``,
        ``kill-switch``, ``budget-hit``
      agent-run bridge: ``agentrun-finalized``, ``agentrun-refused``

    Keeping them separate is what lets a bypass row stay honest. A claim-raced
    item has ``terminal_state="claim-raced"`` and ``outcome="unrecorded"``:
    the row says "this item ended, and no gate outcome exists", which is
    neither a pass nor a null.

    ``escalation_reason`` (S12, card 9a7c0a86) is the WRITTEN justification a
    human put on the card for using a model above its ``model_class`` floor;
    Joule Economy design D2 makes it the one sanctioned feedback channel, the
    corpus a human reads to decide a rubric was wrong. It is carried verbatim
    and never synthesised: a machine-written reason would poison the exact
    corpus it exists to fill.

    The three FACT keys beside it (``escalation_state``,
    ``escalation_floor_class``, ``escalation_served_class``) are COMPUTED here
    from ``work_grade`` and ``model_served``, two arguments this function
    already receives, rather than accepted from a caller. Deliberate on both
    sides: no caller can forget to stamp them, and no caller can stamp a state
    that disagrees with the row's own grade and served model. This does not
    re-derive the grade; ``escalation`` reads the precomputed ``model_class``.

    Because ``model_served`` is None on every row written today and no card
    carries a grade, ``escalation_state`` is ``unobserved`` on essentially
    every live row. That is the honest answer and the ledger says it out loud
    rather than defaulting to ``within_floor``, which would report zero
    escalation forever and read as good news.

    NOTHING READS THESE FIELDS TO ROUTE. They are reporting only. Feeding them
    back into dispatch would be the autotuner card 09573989 AC6 forbids.

    NO BACKFILL: rows written before this change carry none of these thirteen
    keys at all (not even as null), and nothing on the read path invents a
    value for them."""
    try:
        from . import escalation as _esc
        esc_fields = _esc.escalation_row(work_grade, model_served, escalation_reason)
    except Exception:  # noqa: BLE001 -- a verdict bug must never lose the row
        log.exception("autopilot_cost.record_run: escalation classification failed")
        try:
            from . import escalation as _esc2
            esc_fields = _esc2.unobserved_row(reason=escalation_reason)
        except Exception:  # noqa: BLE001 -- last resort, still never absent keys
            esc_fields = {"escalation_state": "unobserved",
                          "escalation_floor_class": None,
                          "escalation_served_class": None,
                          "escalation_reason": None}
    row = {
        "ts": ts, "date": ts[:10], "card_id": card_id, "repo": repo,
        "tokens": tokens, "cost_usd": cost_usd, "joules": _joules(cost_usd),
        "passed": bool(passed), "pr": pr, "run_id": run_id,
        "outcome": outcome, "adapter": adapter,
        "model_requested": model_requested, "model_served": model_served,
        "score": score, "retries": retries, "quality_mode": quality_mode,
        "work_grade": work_grade, "terminal_state": terminal_state,
        **esc_fields,
    }
    try:
        path = ledger_path()
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except Exception:  # noqa: BLE001 -- ledger writes are best-effort
        log.exception("autopilot_cost.record_run: failed to append ledger row")


def _read_ledger() -> list[dict]:
    """Read every row in the ledger, tolerating a missing file and skipping
    any malformed line rather than failing the whole read."""
    path = ledger_path()
    if not path.exists():
        return []
    rows: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        log.exception("autopilot_cost._read_ledger: failed to read ledger")
        return []
    return rows


# --------------------------------------------------------------------------- #
# Settlement journal (design doc section 6): the dedupe guard in front of the #
# JouleWallet. A distinct file from the ledger -- the ledger prices every run #
# (pass or fail), this journal records only wallet settlements (pass only).   #
# --------------------------------------------------------------------------- #


def settlements_path() -> Path:
    return cost_dir() / "settlements.jsonl"


def _read_settlements() -> list[dict]:
    """Read every row in the settlement journal, tolerating a missing file and
    skipping any malformed line rather than failing the whole read (mirrors
    ``_read_ledger``)."""
    path = settlements_path()
    if not path.exists():
        return []
    rows: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        log.exception("autopilot_cost._read_settlements: failed to read settlements")
        return []
    return rows


def already_settled(card_id: str) -> bool:
    """True if ANY settlement row already exists for *card_id*.

    This is the design doc's J1 hardening rule (section 6): a full
    re-dispatch of the same card produces a fresh worktree and a fresh commit
    sha, so the ``(card_id, commit_sha)`` key alone cannot catch a semantic
    duplicate. The cheap journal-local rule is "one settlement per card_id
    until an operator clears it" -- start with that, no ``gh`` call needed.

    Never raises: a corrupted/unreadable journal reads as "not settled", the
    same fail-open discipline as the rest of this module -- a broken journal
    must never turn an otherwise-legitimate run into a crash."""
    try:
        return any(r.get("card_id") == card_id for r in _read_settlements())
    except Exception:  # noqa: BLE001 -- the dedup guard must never raise
        log.exception("autopilot_cost.already_settled: failed to check settlements")
        return False


def record_settlement(*, card_id: str, commit_sha: str, agent: str, minted: int,
                      spent: int, net: int, balance_after: int | None,
                      ts: str) -> None:
    """Append one row to the settlement journal: one row per WALLET
    settlement, keyed (for the dedupe guard above) by ``card_id``. Never
    raises -- a journal-write bug must never turn a shipped, settled build
    into a crash."""
    row = {
        "ts": ts, "card_id": card_id, "commit_sha": commit_sha, "agent": agent,
        "minted": minted, "spent_joules": spent, "net_joules": net,
        "balance_after": balance_after, "state": "settled",
    }
    try:
        path = settlements_path()
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except Exception:  # noqa: BLE001 -- settlement-journal writes are best-effort
        log.exception("autopilot_cost.record_settlement: failed to append settlement row")


def _aggregate(rows: list[dict]) -> dict:
    return {
        "cost_usd": sum(float(r.get("cost_usd") or 0.0) for r in rows),
        "joules": sum(int(r.get("joules") or 0) for r in rows),
        "tokens": sum(int(r.get("tokens") or 0) for r in rows),
        "runs": len(rows),
    }


def day_total(date: str) -> dict:
    """Sum ledger rows for exactly one date (``row["date"] == date``).

    Returns ``{"cost_usd": float, "joules": int, "tokens": int, "runs": int}``.
    The caller
    supplies the date string (typically the bridge's own UTC-today or a value
    from a test fixture) -- this function never reads the clock itself.
    """
    rows = [r for r in _read_ledger() if r.get("date") == date]
    return _aggregate(rows)


def summary(*, today: str, cap_usd: float | None = None) -> dict:
    """Read the whole ledger once and return the overview aggregates: today,
    last 7/30 days, all-time, and an all-time per-repo breakdown.

    Deterministic: the only "now" in this function is the ``today`` string
    the caller supplies; the 7/30-day cutoffs are computed from it.
    """
    rows = _read_ledger()
    today_date = _date_cls.fromisoformat(today)
    cutoff_7 = (today_date - timedelta(days=7)).isoformat()
    cutoff_30 = (today_date - timedelta(days=30)).isoformat()

    today_rows = [r for r in rows if r.get("date") == today]
    last_7_rows = [r for r in rows if cutoff_7 <= (r.get("date") or "") <= today]
    last_30_rows = [r for r in rows if cutoff_30 <= (r.get("date") or "") <= today]

    by_repo: dict[str, dict] = {}
    for r in rows:
        repo = r.get("repo") or "unknown"
        agg = by_repo.setdefault(repo, {"cost_usd": 0.0, "joules": 0, "tokens": 0, "runs": 0})
        agg["cost_usd"] += float(r.get("cost_usd") or 0.0)
        agg["joules"] += int(r.get("joules") or 0)
        agg["tokens"] += int(r.get("tokens") or 0)
        agg["runs"] += 1

    today_agg = _aggregate(today_rows)
    today_pct_of_cap = (
        (today_agg["cost_usd"] / cap_usd * 100) if cap_usd else None
    )
    cap_joules = _joules(cap_usd) if cap_usd else None

    return {
        "today": today_agg,
        "last_7_days": _aggregate(last_7_rows),
        "last_30_days": _aggregate(last_30_rows),
        "all_time": _aggregate(rows),
        "by_repo": by_repo,
        "cap_usd": cap_usd,
        "cap_joules": cap_joules,
        "today_pct_of_cap": today_pct_of_cap,
    }


# --------------------------------------------------------------------------- #
# Dashboard read helpers (skdashboard's fleet-wide Economy view)              #
# --------------------------------------------------------------------------- #


def daily_series(*, today: str, days: int = 30) -> list[dict]:
    """One ``{date, cost_usd, joules, tokens, runs}`` row per calendar day for
    the ``days`` days ending at (and including) ``today``, oldest first.

    Dates with no ledger rows are zero-filled so a chart gets a continuous
    x-axis instead of gaps. Deterministic: the only "now" is the ``today``
    string the caller supplies. Never raises -- an empty list on any read
    error, the same fail-open discipline as the rest of this module.
    """
    try:
        rows = _read_ledger()
        today_date = _date_cls.fromisoformat(today)
        by_date: dict[str, list[dict]] = {}
        for r in rows:
            d = r.get("date")
            if d:
                by_date.setdefault(d, []).append(r)

        series: list[dict] = []
        for offset in range(days - 1, -1, -1):
            d = (today_date - timedelta(days=offset)).isoformat()
            agg = _aggregate(by_date.get(d, []))
            series.append({"date": d, **agg})
        return series
    except Exception:  # noqa: BLE001 -- a chart-read bug must never break the page
        log.exception("autopilot_cost.daily_series: failed")
        return []


def escalation_summary(*, since: str | None = None) -> dict:
    """Escalation rate PER MODEL CLASS over the ledger (design section 9).

    The phase 2 exit gate is "escalation rate per class is stable and
    EXPLAINABLE", explainable by a human. So this returns a per-class
    stratification and never a single blended number: an XL-floor card
    escalating means almost nothing (there is barely any ceiling above it),
    while an S-floor card escalating constantly says the S rubric is wrong, and
    averaged together those two facts cancel.

    Read this WITH ``observed_fraction``, which travels beside every rate. The
    rate's denominator is observed rows only, and ``escalation_rate`` is None
    rather than 0.0 when nothing was observed. Today that is the normal case:
    ``model_served`` is None on every row, so almost everything classifies as
    ``unobserved`` and a 0.0 would be a reassuring lie.

    ``since`` optionally restricts to rows on or after an ISO date. Never
    raises, matching the rest of this module's read helpers.
    """
    try:
        from . import escalation
        rows = _read_ledger()
        if since:
            rows = [r for r in rows if (r.get("date") or "") >= since]
        return escalation.escalation_rates(rows)
    except Exception:  # noqa: BLE001 -- a report bug must never break a caller
        log.exception("autopilot_cost.escalation_summary: failed")
        return {"by_class": {}, "totals": {}, "ungraded_rows": 0}


def recent_settlements(limit: int = 20) -> list[dict]:
    """The most recent ``limit`` settlement rows, newest-first.

    Reads ``settlements.jsonl`` (append-only, oldest-first on disk) and
    reverses it. Never raises -- an empty list on a missing/unreadable file
    or malformed lines (``_read_settlements`` already tolerates those).
    """
    try:
        rows = _read_settlements()
        rows.reverse()
        return rows[:limit]
    except Exception:  # noqa: BLE001 -- a dashboard-read bug must never raise
        log.exception("autopilot_cost.recent_settlements: failed")
        return []


# --------------------------------------------------------------------------- #
# Alerting                                                                    #
# --------------------------------------------------------------------------- #


def alert(text: str, chat: str) -> bool:
    """Best-effort sk-alert shell-out. Returns True only on a clean rc=0;
    swallows every failure (missing binary, timeout, nonzero rc) because an
    alert failure must never break a run."""
    path = shutil.which("sk-alert") or os.path.expanduser("~/.skenv/bin/sk-alert")
    try:
        proc = subprocess.run([path, "-c", chat, text], timeout=20,
                              capture_output=True, text=True)
        return proc.returncode == 0
    except Exception:  # noqa: BLE001 -- alerting is best-effort, never fatal
        log.exception("autopilot_cost.alert: sk-alert shell-out failed")
        return False


def _sentinel_path(kind: str, date: str) -> Path:
    return cost_dir() / f".alerted-{kind}-{date}"


def _sentinel_hit(kind: str, date: str) -> bool:
    return _sentinel_path(kind, date).exists()


def _mark_sentinel(kind: str, date: str) -> None:
    try:
        _sentinel_path(kind, date).touch()
    except OSError:
        log.exception("autopilot_cost._mark_sentinel: failed to write sentinel")


def check_and_alert_caps(*, cfg, today: str, day_cost: float,
                         this_run_tokens: int) -> list[str]:
    """Fire (and dedup, once per day per kind) the daily-cost-cap and
    per-run-token-cap alerts. Returns the alert kinds that fired this call.
    Never raises -- this is a post-run guard, not a gate."""
    fired: list[str] = []
    try:
        chat = cfg.digest_chat or "chef-dm"
        cap_usd = cfg.caps.max_usd_per_day
        cap_tokens = cfg.caps.max_tokens_per_run

        if day_cost >= cap_usd and not _sentinel_hit("daily-usd", today):
            day_joules = _joules(day_cost)
            cap_joules = _joules(cap_usd)
            alert(
                f"\U0001f6d1 Autopilot daily cost cap hit: {day_joules:,} J "
                f"(${day_cost:.2f}) today; cap {cap_joules:,} J (${cap_usd:.2f}). "
                "Raise max_usd_per_day in autopilot-live.yaml to continue (edit "
                f"takes effect next run; no restart; joules track at "
                f"{JOULE_PER_USD:.0f} J/$).",
                chat,
            )
            _mark_sentinel("daily-usd", today)
            fired.append("daily-usd")

        if this_run_tokens >= cap_tokens and not _sentinel_hit("run-tokens", today):
            alert(
                f"⚠️ Autopilot run hit the per-run token cap "
                f"({this_run_tokens} / {cap_tokens} tokens). Consider raising "
                "max_tokens_per_run.",
                chat,
            )
            _mark_sentinel("run-tokens", today)
            fired.append("run-tokens")
    except Exception:  # noqa: BLE001 -- a cap-alert bug must never break a run
        log.exception("autopilot_cost.check_and_alert_caps: failed")
    return fired
