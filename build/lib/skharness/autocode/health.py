"""Harness health telemetry: the substrate for a self-healing, self-learning
autocode harness.

Every adapter/orchestrator step that hits a recoverable snag records a
structured event here (a retry, an inconclusive assess, an auth expiry, an
egress failure, a gate outcome). The events are the harness's memory of how it
is doing, and a learning layer reads them to ADAPT its own behaviour at
runtime -- e.g. raise the retry budget when the assess decline rate climbs,
flag a credential that keeps expiring, or notice an image that keeps failing to
spawn.

Design rules:
- Append-only JSONL, one event per line. Cheap to write, trivial to tail.
- Best-effort and TOTALLY silent: recording must never raise into a live run.
  A telemetry bug can never be allowed to break the thing it observes.
- Pure stdlib, no deps, no network. Local sovereign state.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

_DEFAULT_PATH = "~/.skcapstone/autopilot/health.jsonl"
#: How many trailing events the learning helpers consider a "recent" window.
_RECENT_DEFAULT = 400


def _path() -> Path:
    return Path(os.environ.get("SKHARNESS_HEALTH_PATH", _DEFAULT_PATH)).expanduser()


def record(kind: str, **detail) -> None:
    """Append one health event ``{ts, kind, **detail}``. Best-effort: any failure
    (unwritable dir, serialisation error) is swallowed -- telemetry must never
    break the run it observes."""
    try:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": round(time.time(), 3), "kind": str(kind)}
        for k, v in detail.items():
            try:
                json.dumps(v)          # keep only JSON-serialisable detail
                rec[k] = v
            except (TypeError, ValueError):
                rec[k] = repr(v)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:                  # noqa: BLE001 - telemetry is never fatal
        pass


def recent(kind: str | None = None, limit: int = _RECENT_DEFAULT) -> list[dict]:
    """The most recent events (newest last), optionally filtered by ``kind``.
    Returns [] when there is no log yet or it cannot be read."""
    try:
        lines = _path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in lines[-limit * 4:]:    # over-read; filtering may thin the set
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if kind is None or ev.get("kind") == kind:
            out.append(ev)
    return out[-limit:]


def rate(kind: str, over: tuple[str, ...], window: int = _RECENT_DEFAULT) -> float:
    """Fraction of recent events (across ``over`` kinds) that are ``kind`` -- the
    core learning signal. E.g. ``rate("assess_inconclusive",
    over=("assess_inconclusive", "assess_ok"))`` is the assess decline rate.
    0.0 when the denominator is empty (no data == no alarm)."""
    events = [e for e in recent(limit=window) if e.get("kind") in over]
    if not events:
        return 0.0
    hits = sum(1 for e in events if e.get("kind") == kind)
    return hits / len(events)
