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
# Attribution vocabulary + normalisers (A1, card 8967bf22)                    #
# --------------------------------------------------------------------------- #

#: Closed vocabulary for ``fallback_reason``: why a run was NOT served by the
#: sovereign pi path. Closed on purpose. An open free-text field would let a
#: rate ("what fraction of runs left pi, and why") be computed over a set of
#: strings nobody can group, which is the same shape of unreadability the
#: ``outcome`` vocabulary (types.GATE_OUTCOMES) exists to prevent.
#:
#: ``pi`` is a member rather than an absence: "pi served it" and "nobody looked"
#: are different facts, and only the first should count as sovereign. None on
#: the row means nothing was observed, never that pi served it.
FALLBACK_REASONS: frozenset[str] = frozenset({
    "pi",                    # no fallback: the sovereign pi adapter served it
    "harness-configured",    # config selected a non-pi harness outright
    "capability-missing",    # pi lacks a capability this run needed
    "gateway-unreachable",   # the sovereign gateway did not answer
    "bucket-unavailable",    # the requested bucket had no sovereign backend
    "adapter-error",         # the pi adapter raised and another path ran
    "unknown",               # not pi, and the reason itself was not observed
})


def _ident(value: object) -> str | None:
    """Normalise one attribution string: a stripped str, or None.

    Empty and whitespace-only collapse to None because an empty string is the
    worst of both readings: it joins to nothing, yet it counts as present. A
    ledger reader that groups by ``gateway_req_id`` or ``session_id`` would
    build one large fake cohort out of every run that simply had no id. A null
    says "absent" and groups as absent, which is the honest answer.
    """
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _grade_axis(work_grade: object, key: str) -> str | None:
    """One axis of the nested ``work_grade`` dict, lifted out for querying.

    Reads the axis off the grade; it never re-derives one axis from another,
    and never invents one for a card that does not carry it. Case is folded
    down so ``"M"`` and ``"m"`` group as one value rather than two, matching
    what ``escalation.floor_class`` already does with ``model_class``. The
    nested dict itself is untouched and keeps whatever casing the card stored.

    None for an ungraded card, a non-dict grade, or a missing/blank axis, which
    all mean the same thing here: this row carries no value for that axis.
    """
    if not isinstance(work_grade, dict):
        return None
    axis = _ident(work_grade.get(key))
    return axis.lower() if axis else None


def _count(kind: str, **detail) -> None:
    """Record one health event. Never raises: health.record is already silent,
    and the import is guarded so a telemetry dependency can never turn a
    telemetry failure into a run failure."""
    try:
        from . import health
        health.record(kind, **detail)
    except Exception:  # noqa: BLE001 -- counting a failure never causes one
        pass


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
               agent: str | None = None, session_id: str | None = None,
               agent_var: str | None = None,
               session_id_var: str | None = None,
               node: str | None = None, gateway_url: str | None = None,
               bucket: str | None = None, backend_served: str | None = None,
               gateway_req_id: str | None = None,
               fallback_reason: str | None = None) -> None:
    """Append one run to the ledger. Never raises -- a cost-tracking bug must
    never turn a successful (or a well-formed failed) run into a crash. A
    failed write is COUNTED as a health event rather than only logged, see the
    bottom of this docstring.

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

    ``backend_served`` (A1, below) is held to that IDENTICAL discipline, for
    the identical reason one level down: it names the backend that actually
    answered, never the backend a bucket or a gateway config implies. Unknown
    and matched are different facts. Defaulting either field from the request
    would make every run in the ledger read as sovereign, and the resulting
    100% sovereign rate would be indistinguishable from a real one.

    ``work_grade`` carries the Joule Economy grade dict (``size``, ``risk``,
    ``sensitivity``, ``model_class``) exactly as stored on the card, or None
    when the card is ungraded; this module never re-derives it.

    ``work_grade`` STAYS NESTED. It is passed through as the dict it is, and
    is never flattened away, because ``escalation.py`` (S12, landing on its own
    branch with 31 tests) reads it with ``work_grade.get("model_class")`` at
    lines 126-128: flattening would break that call site on contact.
    The three ``grade_*`` keys on the row are DERIVED copies of the axes for
    querying, computed here from the same dict, exactly as the
    ``escalation_floor_class`` / ``escalation_served_class`` /
    ``escalation_state`` facts are computed rather than accepted. They are an
    addition beside the dict, never a replacement for it, and no caller can
    set them to something the dict disagrees with.

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

    ATTRIBUTION (A1, card 8967bf22). Ten optional fields, all defaulting to
    None, answering "who ran this, where, and what actually served it". They
    are the join keys that turn a ledger of anonymous rows into a ledger that
    can be attributed:

    ``agent``, ``session_id``, ``agent_var``, ``session_id_var``
      The resolved writer identity plus its PROVENANCE. ``(agent_var,
      session_id_var)`` is NOT redundant with ``agent``: the env vars that feed
      the resolver genuinely disagree in production. One ``skcomms.service``
      unit on the ``.41`` node sets ``SKAGENT=jarvis``,
      ``SKMEMORY_AGENT=lumina`` and ``SKCHAT_IDENTITY=capauth:opus@skworld.io``
      at once, so a row carrying only ``agent="jarvis"`` has silently discarded
      the fact that two other names were also on the table, and no later reader
      can recover which variable was read. ``agent_var`` names the variable the
      name came from; ``session_id_var`` is ``"SK_SESSION_ID"`` when the id was
      inherited across a re-exec or ``"minted"`` when this process created it,
      which is what separates one long session from several short ones.

    ``node``
      ``socket.gethostname()`` of the box that ran it. The fleet is multi-node
      and the ledger is Syncthing-synced into ONE tree, so without this every
      node's rows are already interleaved and unseparable.

    ``gateway_url``
      Which skgateway answered. REQUIRED semantically even though it is
      optional in the signature, because a bucket id is not self-describing: a
      bucket resolves to different backends depending on the gateway that
      resolved it, and ``autopilot-pi.yaml`` points at ``100.86.156.5:18780``
      while the other flags in this epic were verified against
      ``localhost:18780``. A bucket recorded without its gateway is
      indistinguishable from correct routing whichever way it actually went.

    ``bucket``
      The ``sk-<class>-<sensitivity>`` id, when one was actually DISPATCHED.
      Not the id a payload would have produced: an item that never reached a
      dispatch has no bucket, and recording the hypothetical one would report
      a dispatch that did not happen.

    ``backend_served``
      The backend that actually served the run, never derived. See the
      ``model_served`` discipline above, which this field extends.

    ``gateway_req_id``
      The skgateway request id: the join key that lets a ledger row be matched
      to the gateway's own record of the same call. It is the only field here
      that makes the two sides checkable against each other rather than merely
      consistent-looking.

    ``fallback_reason``
      A member of ``FALLBACK_REASONS`` (closed): why this run was not pi. An
      off-vocabulary value is still written verbatim (a row is never dropped or
      rewritten to fit a vocabulary) but is COUNTED as
      ``ledger_vocabulary_drift`` so the drift is readable instead of silent.

    FAILED WRITES ARE COUNTED. This function still never raises, but a write
    that fails now records a ``ledger_write_error`` health event as well as
    logging, matching what ``record_outcome_row`` already does with
    ``outcome_row_error``. A run record that fails to write used to be visible
    only as one line in a log nobody aggregates, which is exactly the
    invisible-absence failure this epic exists to remove: the missing rows were
    ABSENT rather than marked absent, so the ledger could not detect its own
    gaps. A count can be read, alerted on, and compared against the run journal.

    NO BACKFILL: rows written before this change carry none of these keys at
    all (not even as null), and nothing on the read path invents a value for
    them."""
    fallback_reason = _ident(fallback_reason)
    if fallback_reason is not None and fallback_reason not in FALLBACK_REASONS:
        # Written through verbatim, never coerced: silently normalising an
        # unknown value into "unknown" would erase the evidence that a caller
        # and this vocabulary have diverged.
        _count("ledger_vocabulary_drift", field="fallback_reason",
               value=fallback_reason[:80], card_id=card_id, run_id=run_id)

    row = {
        "ts": ts, "date": ts[:10], "card_id": card_id, "repo": repo,
        "tokens": tokens, "cost_usd": cost_usd, "joules": _joules(cost_usd),
        "passed": bool(passed), "pr": pr, "run_id": run_id,
        "outcome": outcome, "adapter": adapter,
        "model_requested": model_requested, "model_served": model_served,
        "score": score, "retries": retries, "quality_mode": quality_mode,
        "work_grade": work_grade, "terminal_state": terminal_state,
        # A1 attribution. Every id goes through _ident, so a caller that has
        # nothing to say writes a null rather than an empty string that would
        # group as a real (and enormous) cohort.
        "agent": _ident(agent), "session_id": _ident(session_id),
        "agent_var": _ident(agent_var),
        "session_id_var": _ident(session_id_var),
        "node": _ident(node), "gateway_url": _ident(gateway_url),
        "bucket": _ident(bucket),
        # NEVER defaulted from model_requested, model_served, bucket or
        # gateway_url. Only a caller that observed the answer can fill it.
        "backend_served": _ident(backend_served),
        "gateway_req_id": _ident(gateway_req_id),
        "fallback_reason": fallback_reason,
        # Derived FROM the nested work_grade dict above, which stays intact.
        "grade_size": _grade_axis(work_grade, "size"),
        "grade_risk": _grade_axis(work_grade, "risk"),
        "grade_sensitivity": _grade_axis(work_grade, "sensitivity"),
    }
    try:
        path = ledger_path()
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:  # noqa: BLE001 -- ledger writes are best-effort
        log.exception("autopilot_cost.record_run: failed to append ledger row")
        # Counted, not just logged: see FAILED WRITES ARE COUNTED above.
        _count("ledger_write_error", card_id=card_id, run_id=run_id,
               terminal_state=terminal_state, error=str(exc)[:120])


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
