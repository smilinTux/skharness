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
               passed: bool, pr: str, ts: str) -> None:
    """Append one run to the ledger. Never raises -- a cost-tracking bug must
    never turn a successful (or a well-formed failed) run into a crash."""
    row = {
        "ts": ts, "date": ts[:10], "card_id": card_id, "repo": repo,
        "tokens": tokens, "cost_usd": cost_usd, "joules": _joules(cost_usd),
        "passed": bool(passed), "pr": pr,
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
