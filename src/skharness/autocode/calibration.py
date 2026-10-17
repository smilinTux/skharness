"""Read-only calibration reporting over existing retry evidence.

This is a reporting seam, never a control seam. It may read the three retry
sinks that already exist, combine their observations, and return review
candidates. It cannot dispatch work, construct a model address, change a gate,
or promote a golden-set entry. A human has exactly three dispositions for each
candidate: promote, dismiss, or defer.

Version 1 reports possible UNDERGRADING only. A retry is useful evidence that a
card may have been harder than its recorded grade. Version 1 deliberately does
not report possible overgrading. A first-round pass under a large model cannot
distinguish an easy card from a card that the large model made look easy. That
confound is unresolved, so treating a quick pass as an overgrade signal would
manufacture evidence.

Every ``meta.sensitivity_override`` is also a review candidate. The override is
a human verdict that deterministic rules needed correction, including when the
raw value is invalid. Candidate generation records that fact without importing
the sensitivity classifier or interpreting the override.

All functions that combine evidence are pure. The ``read_*`` helpers perform
best-effort, read-only loading for the existing digest caller. This
module opens no file for writing and imports only Python's standard library.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

CALIBRATION_CANDIDATE_SCHEMA_V1 = "skharness.calibration-candidate.v1"
UNDERGRADE = "undergrade"
SENSITIVITY_OVERRIDE = "sensitivity_override"
DISPOSITIONS = ("promote", "dismiss", "defer")
DEFAULT_SHOW_LIMIT = 3


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _candidate_id(kind: str, card_id: str) -> str:
    raw = _canonical({"schema": CALIBRATION_CANDIDATE_SCHEMA_V1,
                      "kind": kind, "card_id": card_id})
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _timestamp(value: object) -> datetime | None:
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        if isinstance(value, str) and value.strip():
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if parsed.utcoffset() is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
    except (OverflowError, TypeError, ValueError):
        pass
    return None


def _iso(value: object) -> str | None:
    parsed = _timestamp(value)
    return parsed.isoformat() if parsed is not None else None


def _grade(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    shape = {key: value.get(key) for key in
             ("size", "risk", "sensitivity", "model_class")}
    return shape if any(item is not None for item in shape.values()) else None


def _card_id(row: object) -> str | None:
    if not isinstance(row, dict):
        return None
    raw = row.get("card_id", row.get("task", row.get("id")))
    return raw.strip() if isinstance(raw, str) and raw.strip() else None


def _retry_observation(row: object, source: str) -> dict | None:
    if not isinstance(row, dict):
        return None
    card_id = _card_id(row)
    try:
        retries = int(row.get("retries", 0) or 0)
    except (TypeError, ValueError):
        return None
    if card_id is None or retries <= 0:
        return None

    work_grade = _grade(row.get("work_grade"))
    if work_grade is None:
        work_grade = _grade(row.get("task_shape"))
    return {
        "card_id": card_id,
        "retries": retries,
        "rounds_cap": row.get("rounds_cap"),
        "observed_at": _iso(row.get("ts", row.get("recorded_at"))),
        "work_grade": work_grade,
        "source": source,
    }


def undergrade_candidates(
    *,
    health_events: Iterable[dict] = (),
    outcome_rows: Iterable[dict] = (),
    run_records: Iterable[dict] = (),
    cards: Iterable[dict] = (),
) -> list[dict]:
    """Return one review-only undergrade candidate per card with retry evidence.

    Observations from all three sinks are merged. A candidate requires a grade
    from an observation or the current card, because retries without a recorded
    grade cannot disagree with that grade. No corrected grade is guessed.
    """
    card_grades = {}
    for card in cards:
        if not isinstance(card, dict) or (card_id := _card_id(card)) is None:
            continue
        meta = card.get("meta")
        grade = _grade(card.get("work_grade"))
        if grade is None and isinstance(meta, dict):
            grade = _grade(meta.get("grade"))
        card_grades[card_id] = grade
    grouped: dict[str, list[dict]] = {}
    sources = (
        (health_events, "health"),
        (outcome_rows, "outcome"),
        (run_records, "run_record"),
    )
    for rows, source in sources:
        for row in rows:
            if source == "health" and isinstance(row, dict):
                if row.get("kind") != "build_economics":
                    continue
            observation = _retry_observation(row, source)
            if observation is not None:
                grouped.setdefault(observation["card_id"], []).append(observation)

    candidates = []
    for card_id, observations in sorted(grouped.items()):
        grade = next((obs["work_grade"] for obs in observations
                      if obs["work_grade"] is not None), card_grades.get(card_id))
        if grade is None:
            continue
        observed = [obs["observed_at"] for obs in observations if obs["observed_at"]]
        candidates.append({
            "schema": CALIBRATION_CANDIDATE_SCHEMA_V1,
            "candidate_id": _candidate_id(UNDERGRADE, card_id),
            "kind": UNDERGRADE,
            "card_id": card_id,
            "observed_at": min(observed) if observed else None,
            "work_grade": grade,
            "retries": max(obs["retries"] for obs in observations),
            "rounds_cap": max(
                (cap for obs in observations
                 if isinstance((cap := obs["rounds_cap"]), int)),
                default=None,
            ),
            "evidence_sources": sorted({obs["source"] for obs in observations}),
            "dispositions": list(DISPOSITIONS),
            "authority": "report_only",
        })
    return candidates


def sensitivity_override_candidates(cards: Iterable[dict] = ()) -> list[dict]:
    """Capture every explicit sensitivity override as a review-only candidate."""
    candidates = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        card_id = _card_id(card)
        meta = card.get("meta")
        if card_id is None or not isinstance(meta, dict) or "sensitivity_override" not in meta:
            continue
        raw_override = meta.get("sensitivity_override")
        normalized = raw_override.strip().lower() if isinstance(raw_override, str) else None
        override = normalized if normalized in {"public", "internal", "secret"} else "invalid"
        candidates.append({
            "schema": CALIBRATION_CANDIDATE_SCHEMA_V1,
            "candidate_id": _candidate_id(SENSITIVITY_OVERRIDE, card_id),
            "kind": SENSITIVITY_OVERRIDE,
            "card_id": card_id,
            "observed_at": _iso(card.get("updated_at", card.get("created_at"))),
            # Never copy an arbitrary invalid value into the digest manifest. It
            # may itself be sensitive. The card id is enough for human review.
            "sensitivity_override": override,
            "dispositions": list(DISPOSITIONS),
            "authority": "report_only",
        })
    return sorted(candidates, key=lambda item: item["card_id"])


def build_report(
    *,
    health_events: Iterable[dict] = (),
    outcome_rows: Iterable[dict] = (),
    run_records: Iterable[dict] = (),
    cards: Iterable[dict] = (),
    now: datetime | None = None,
    show_limit: int = DEFAULT_SHOW_LIMIT,
) -> dict:
    """Build the bounded calibration report consumed by the numbered digest."""
    cards = list(cards)
    candidates = undergrade_candidates(
        health_events=health_events,
        outcome_rows=outcome_rows,
        run_records=run_records,
        cards=cards,
    ) + sensitivity_override_candidates(cards)
    candidates.sort(key=lambda item: (
        item.get("observed_at") is None,
        item.get("observed_at") or "",
        item["candidate_id"],
    ))
    limit = max(0, int(show_limit))
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    dated = [_timestamp(item.get("observed_at")) for item in candidates]
    dated = [value for value in dated if value is not None]
    oldest_days = max(0, (now - min(dated)).days) if dated else None
    return {
        "schema": "skharness.calibration-report.v1",
        "backlog_count": len(candidates),
        "shown_count": min(limit, len(candidates)),
        "oldest_days": oldest_days,
        "dispositions": list(DISPOSITIONS),
        "candidates": candidates[:limit],
        "authority": "report_only",
    }


def digest_line(report: dict) -> str:
    """Render the mandatory one-line backlog surface without candidate content."""
    total = max(0, int(report.get("backlog_count", 0) or 0))
    shown = max(0, min(total, int(report.get("shown_count", 0) or 0)))
    oldest = report.get("oldest_days")
    age = f"{int(oldest)} days" if isinstance(oldest, int) else "none"
    dispositions = report.get("dispositions") or list(DISPOSITIONS)
    return (
        f"Calibration candidates: showing {shown} of {total}, oldest {age} "
        f"[{'/'.join(str(value) for value in dispositions)}]."
    )


def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(value, dict):
                    rows.append(value)
    except OSError:
        pass
    return rows


def read_run_records(directory: Path) -> list[dict]:
    """Read existing RunRecords without mutating or repairing their journals."""
    records = []
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError:
        return records
    for path in paths:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for item in (document.get("items") or {}).values():
            if not isinstance(item, dict):
                continue
            records.extend(row for row in item.get("run_records", [])
                           if isinstance(row, dict))
    return records


def read_cards(directory: Path) -> list[dict]:
    """Read current task projections for grade and sensitivity-override facts."""
    cards = []
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError:
        return cards
    for path in paths:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(value, dict):
            cards.append(value)
    return cards


def default_report(*, now: datetime | None = None,
                   show_limit: int = DEFAULT_SHOW_LIMIT) -> dict:
    """Read the three established sinks and current cards, then report only."""
    home = Path(os.environ.get("SKCAPSTONE_HOME", "~/.skcapstone")).expanduser()
    health_path = Path(os.environ.get(
        "SKHARNESS_HEALTH_PATH", str(home / "autopilot" / "health.jsonl")))
    cost_dir = Path(os.environ.get(
        "SKAI_COST_DIR", str(home / "autopilot-cost"))).expanduser()
    runs_dir = Path(os.environ.get(
        "SK_AUTOPILOT_RUNS_DIR",
        str(home / "coordination" / "autopilot" / "runs"),
    )).expanduser()
    tasks_dir = home / "coordination" / "tasks"
    return build_report(
        health_events=_read_jsonl(health_path.expanduser()),
        outcome_rows=_read_jsonl(cost_dir / "ledger.jsonl"),
        run_records=read_run_records(runs_dir),
        cards=read_cards(tasks_dir),
        now=now,
        show_limit=show_limit,
    )


__all__ = [
    "CALIBRATION_CANDIDATE_SCHEMA_V1",
    "DISPOSITIONS",
    "SENSITIVITY_OVERRIDE",
    "UNDERGRADE",
    "build_report",
    "default_report",
    "digest_line",
    "read_cards",
    "read_run_records",
    "sensitivity_override_candidates",
    "undergrade_candidates",
]
