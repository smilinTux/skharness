"""Testable scheduled/on-demand Arena job execution with a durable run ledger."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - production is POSIX
    fcntl = None  # type: ignore[assignment]


class ArenaJobService:
    """Run the same named operation from a timer or on demand and ledger every exit.

    The caller owns scheduling and alert delivery. This boundary guarantees that a
    completed invocation has a durable JSONL row and that failures are also offered
    to the injected capture/alert hooks before the original exception is re-raised.
    """

    def __init__(
        self,
        ledger: str | Path,
        *,
        node: str,
        capture_failure: Callable[[dict[str, Any]], None] | None = None,
        alert_failure: Callable[[dict[str, Any]], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.ledger = Path(ledger)
        self.node = node
        self.capture_failure = capture_failure
        self.alert_failure = alert_failure
        self._clock = clock
        self._lock = threading.Lock()

    def run(self, job: str, operation: Callable[[], Any], *, trigger: str) -> Any:
        if not job.strip() or trigger not in {"scheduled", "on_demand"}:
            raise ValueError("job is required and trigger must be scheduled or on_demand")
        started = self._clock()
        record = {
            "schema": "skharness.arena.job-run.v1",
            "job": job,
            "run_id": uuid.uuid4().hex,
            "host": self.node,
            "trigger": trigger,
            "start": datetime.fromtimestamp(started, timezone.utc).isoformat(),
        }
        try:
            result = operation()
        except Exception as exc:
            record.update(
                {
                    "ok": False,
                    "status": "failed",
                    "failure_type": type(exc).__name__,
                    "failure": str(exc)[:1000],
                }
            )
            self._finish(record, started)
            for callback in (self.capture_failure, self.alert_failure):
                if callback is not None:
                    callback(dict(record))
            raise
        record.update({"ok": True, "status": "ok"})
        self._finish(record, started)
        return result

    def _finish(self, record: dict[str, Any], started: float) -> None:
        record["dur_s"] = max(0.0, self._clock() - started)
        payload = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.ledger.open("a", encoding="utf-8") as stream:
            if fcntl is not None:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())

    def status(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Read recent valid rows; a concurrent incomplete tail is ignored."""
        try:
            lines = self.ledger.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        rows = []
        for line in lines:
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(row, dict) and row.get("schema") == "skharness.arena.job-run.v1":
                rows.append(row)
        return rows[-max(1, min(limit, 500)) :]
