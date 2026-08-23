"""Tiny atomic JSON status-file protocol between a run_card.py pane and the
control.py pane. One file per card: ``<status_dir>/<card_id>.json``.

Deliberately not a queue or a socket: tmux panes are independent processes
with nothing else in common, and a directory of small JSON files is the
simplest thing that is safe to poll from a second process (control.py) while
one writer (run_card.py) owns each file exclusively -- one card, one writer,
no lock needed.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


def write(status_dir: Path, card_id: str, **fields: Any) -> None:
    """Merge ``fields`` into ``<status_dir>/<card_id>.json`` and write it
    atomically (write-then-rename), so control.py never reads a half-written
    file. Always stamps ``updated`` with the current epoch time."""
    status_dir.mkdir(parents=True, exist_ok=True)
    path = status_dir / f"{card_id}.json"
    current: dict[str, Any] = {}
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    current.update(fields)
    current["card"] = card_id
    current["updated"] = time.time()
    current.setdefault("started", current["updated"])
    tmp = path.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(current), encoding="utf-8")
    tmp.replace(path)


def read_all(status_dir: Path) -> list[dict[str, Any]]:
    """Every card status file currently in ``status_dir``, oldest-started
    first. Best-effort: a file mid-write (caught between the writer's
    rename and this read, or simply absent) is skipped, not raised."""
    if not status_dir.exists():
        return []
    rows = []
    for f in sorted(status_dir.glob("*.json")):
        try:
            rows.append(json.loads(f.read_text(encoding="utf-8")))
        except (FileNotFoundError, json.JSONDecodeError):
            continue
    rows.sort(key=lambda r: r.get("started", 0))
    return rows
