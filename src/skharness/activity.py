"""Replayable, process-safe activity journal for live SKHarness observation.

The activity plane is a VIEW.  Worker-authored text is retained as bounded
observation data and never becomes completion, scheduling, or authorization
evidence.  Producers append without needing a connected viewer; skcode-hostd
reads the same journal for cursor replay and a bounded WebSocket tail.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1
DEFAULT_MAX_BYTES = 16_000_000
DEFAULT_MAX_EVENT_BYTES = 16_384
DEFAULT_REPLAY_LIMIT = 200
MAX_REPLAY_LIMIT = 500
MAX_SUMMARY_CHARS = 4_096

_EVENTS_FILENAME = "events.jsonl"
_HEAD_FILENAME = "head.json"
_LOCK_FILENAME = ".activity.lock"
_SAFE_ID = re.compile(r"[A-Za-z0-9._:@+-]{1,200}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_SECRET_KEY = re.compile(
    r"(?:^|[_-])(?:authorization|cookie|credential|password|secret|token|"
    r"(?:access|refresh|session|bearer|auth)[_-]?token|api[_-]?key)(?:$|[_-])",
    re.I,
)
_SECRET_VALUE = re.compile(
    r"(?i)(?:bearer\s+|api[_-]?key\s*[:=]\s*|token\s*[:=]\s*)"
    r"[^\s,;\"']+"
)


class ActivityError(RuntimeError):
    """Base class for activity-plane failures."""


class ActivityCorruptionError(ActivityError):
    """A newline-terminated (therefore committed) journal row is corrupt."""


class ActivityKind(str, Enum):
    STATUS = "status"
    PHASE = "phase"
    ASSISTANT_TEXT = "assistant_text"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    FILE_CHANGE = "file_change"
    TEST = "test"
    BUDGET = "budget"
    DISPOSITION = "disposition"
    ERROR = "error"


@dataclass(frozen=True)
class ActivityContext:
    """Controller-owned identity attached to every observed event."""

    session_id: str
    run_id: str = ""
    agent_id: str = ""
    job_id: str = ""
    role: str = ""
    phase: str = ""
    source: str = "arena"
    card_id: str = ""
    card_hash: str = ""
    trajectory_id: str = ""
    team_id: str = ""
    parent_agent_id: str = ""
    contract_id: str = ""
    contract_hash: str = ""
    plan_hash: str = ""
    lease_id: str = ""
    attempt_id: str = ""
    base_commit: str = ""
    evidence_id: str = ""

    def __post_init__(self) -> None:
        for name in (
            "session_id",
            "run_id",
            "agent_id",
            "job_id",
            "role",
            "phase",
            "source",
            "card_id",
            "trajectory_id",
            "team_id",
            "parent_agent_id",
            "contract_id",
            "lease_id",
            "attempt_id",
        ):
            value = getattr(self, name)
            if value and not _SAFE_ID.fullmatch(value):
                raise ValueError(f"activity {name} must be a bounded opaque id")
        if not self.session_id:
            raise ValueError("activity session_id is required")
        if not self.source:
            raise ValueError("activity source is required")
        for name in ("card_hash", "contract_hash", "plan_hash", "evidence_id"):
            value = getattr(self, name)
            if value and not _DIGEST.fullmatch(value):
                raise ValueError(f"activity {name} must be a sha256 digest")
        if self.base_commit and not _COMMIT.fullmatch(self.base_commit):
            raise ValueError("activity base_commit must be a full lowercase commit")


@dataclass(frozen=True)
class ActivityEvent:
    """One immutable observation in global publication order."""

    schema_version: int
    cursor: int
    event_id: str
    published_at: float
    session_id: str
    run_id: str
    agent_id: str
    job_id: str
    role: str
    phase: str
    source: str
    card_id: str
    card_hash: str
    trajectory_id: str
    team_id: str
    parent_agent_id: str
    contract_id: str
    contract_hash: str
    plan_hash: str
    lease_id: str
    attempt_id: str
    base_commit: str
    evidence_id: str
    kind: ActivityKind
    summary: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    artifact_refs: tuple[str, ...] = ()
    authority: str = "observation"

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported activity schema_version")
        if self.cursor < 1:
            raise ValueError("activity cursor must be positive")
        if not _DIGEST.fullmatch(self.event_id):
            raise ValueError("activity event_id must be a sha256 digest")
        if not isinstance(self.published_at, (int, float)) or self.published_at <= 0:
            raise ValueError("activity published_at must be a positive timestamp")
        ActivityContext(
            session_id=self.session_id,
            run_id=self.run_id,
            agent_id=self.agent_id,
            job_id=self.job_id,
            role=self.role,
            phase=self.phase,
            source=self.source,
            card_id=self.card_id,
            card_hash=self.card_hash,
            trajectory_id=self.trajectory_id,
            team_id=self.team_id,
            parent_agent_id=self.parent_agent_id,
            contract_id=self.contract_id,
            contract_hash=self.contract_hash,
            plan_hash=self.plan_hash,
            lease_id=self.lease_id,
            attempt_id=self.attempt_id,
            base_commit=self.base_commit,
            evidence_id=self.evidence_id,
        )
        if self.authority != "observation":
            raise ValueError("activity events never carry control authority")
        if not isinstance(self.summary, str) or len(self.summary) > MAX_SUMMARY_CHARS:
            raise ValueError("activity summary is not bounded")
        if "\x00" in self.summary:
            raise ValueError("activity summary contains NUL")
        for ref in self.artifact_refs:
            if not _DIGEST.fullmatch(ref):
                raise ValueError("activity artifact refs must be sha256 digests")

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["kind"] = self.kind.value
        row["artifact_refs"] = list(self.artifact_refs)
        return row

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "ActivityEvent":
        allowed = {
            "schema_version",
            "cursor",
            "event_id",
            "published_at",
            "session_id",
            "run_id",
            "agent_id",
            "job_id",
            "role",
            "phase",
            "source",
            "card_id",
            "card_hash",
            "trajectory_id",
            "team_id",
            "parent_agent_id",
            "contract_id",
            "contract_hash",
            "plan_hash",
            "lease_id",
            "attempt_id",
            "base_commit",
            "evidence_id",
            "kind",
            "summary",
            "data",
            "artifact_refs",
            "authority",
        }
        if set(row) - allowed:
            raise ValueError("activity row contains unknown fields")
        values = dict(row)
        values["kind"] = ActivityKind(values["kind"])
        values["artifact_refs"] = tuple(values.get("artifact_refs") or ())
        values["data"] = dict(values.get("data") or {})
        event = cls(**values)
        unsigned = event.to_dict()
        unsigned.pop("event_id")
        expected = "sha256:" + hashlib.sha256(
            json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if event.event_id != expected:
            raise ValueError("activity event_id does not match event content")
        return event


def default_activity_root() -> Path:
    state = os.environ.get("SKCODE_STATE_DIR")
    base = Path(state) if state else Path.home() / ".skcapstone" / "skcode"
    return base / "activity"


def _bounded_text(value: object, *, limit: int) -> str:
    text = str(value).replace("\x00", "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def sanitize_activity_text(value: object, *, limit: int = MAX_SUMMARY_CHARS) -> str:
    """Bound text and redact common inline credential forms before publication."""

    return _SECRET_VALUE.sub("[redacted]", _bounded_text(value, limit=limit))


def sanitize_activity_data(value: object, *, _depth: int = 0) -> Any:
    """Bound nested metadata and redact common credential-bearing keys.

    This is defense in depth, not a claim that arbitrary stdout is secret-free.
    Producers therefore pass operational metadata only; raw logs remain protected
    artifacts outside this journal.
    """

    if _depth > 4:
        return "[depth-limited]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return sanitize_activity_text(value, limit=2_048)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for raw_key in list(value)[:64]:
            key = _bounded_text(raw_key, limit=100)
            out[key] = (
                "[redacted]"
                if _SECRET_KEY.search(key)
                else sanitize_activity_data(value[raw_key], _depth=_depth + 1)
            )
        return out
    if isinstance(value, (list, tuple)):
        return [sanitize_activity_data(item, _depth=_depth + 1) for item in value[:64]]
    return _bounded_text(value, limit=2_048)


class ActivityJournal:
    """Bounded hash-addressed JSONL journal with a global durable cursor."""

    def __init__(
        self,
        *,
        root: Path | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_event_bytes: int = DEFAULT_MAX_EVENT_BYTES,
        clock=time.time,
    ) -> None:
        if max_bytes < max_event_bytes * 2:
            raise ValueError("activity max_bytes must retain at least two events")
        if max_event_bytes < 1_024:
            raise ValueError("activity max_event_bytes is too small")
        self.root = Path(root) if root is not None else default_activity_root()
        self.max_bytes = int(max_bytes)
        self.max_event_bytes = int(max_event_bytes)
        self.clock = clock

    @property
    def path(self) -> Path:
        return self.root / _EVENTS_FILENAME

    @contextmanager
    def _locked(self, *, exclusive: bool) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        lock_path = self.root / _LOCK_FILENAME
        with lock_path.open("a+b") as lock:
            try:
                os.chmod(lock_path, 0o600)
            except OSError:
                pass
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def publish(
        self,
        context: ActivityContext,
        kind: ActivityKind,
        *,
        summary: str = "",
        data: dict[str, Any] | None = None,
        artifact_refs: tuple[str, ...] = (),
    ) -> ActivityEvent:
        summary = sanitize_activity_text(summary)
        sanitized = sanitize_activity_data(data or {})
        if not isinstance(sanitized, dict):  # pragma: no cover - data is typed dict
            raise TypeError("activity data must sanitize to a mapping")
        with self._locked(exclusive=True):
            last = self._last_committed_unlocked(recover_tail=True)
            head = self._read_head_unlocked()
            cursor = max(head, last.cursor if last is not None else 0) + 1
            published_at = float(self.clock())
            unsigned = {
                "schema_version": SCHEMA_VERSION,
                "cursor": cursor,
                "published_at": published_at,
                **asdict(context),
                "kind": kind.value,
                "summary": summary,
                "data": sanitized,
                "artifact_refs": list(artifact_refs),
                "authority": "observation",
            }
            digest = "sha256:" + hashlib.sha256(
                json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            event = ActivityEvent.from_dict({"event_id": digest, **unsigned})
            encoded = (
                json.dumps(event.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"
            ).encode()
            if len(encoded) > self.max_event_bytes:
                raise ValueError("activity event exceeds max_event_bytes")
            fd = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                os.write(fd, encoded)
                os.fsync(fd)
            finally:
                os.close(fd)
            self._fsync_root()
            self._write_head_unlocked(cursor)
            self._trim_unlocked()
            return event

    def read_after(
        self,
        after_cursor: int = 0,
        *,
        limit: int = DEFAULT_REPLAY_LIMIT,
        session_id: str = "",
        run_id: str = "",
        agent_id: str = "",
        job_id: str = "",
        card_id: str = "",
        contract_id: str = "",
        lease_id: str = "",
        role: str = "",
        kind: str = "",
    ) -> list[ActivityEvent]:
        if after_cursor < 0:
            raise ValueError("after_cursor must not be negative")
        limit = max(1, min(int(limit), MAX_REPLAY_LIMIT))
        filters = {
            "session_id": session_id,
            "run_id": run_id,
            "agent_id": agent_id,
            "job_id": job_id,
            "card_id": card_id,
            "contract_id": contract_id,
            "lease_id": lease_id,
            "role": role,
        }
        for name, value in filters.items():
            if value and not _SAFE_ID.fullmatch(value):
                raise ValueError(f"invalid activity {name} filter")
        if kind:
            ActivityKind(kind)
        with self._locked(exclusive=False):
            rows = self._read_committed_unlocked()
        result = []
        for event in rows:
            if event.cursor <= after_cursor:
                continue
            if any(value and getattr(event, name) != value for name, value in filters.items()):
                continue
            if kind and event.kind.value != kind:
                continue
            result.append(event)
            if len(result) >= limit:
                break
        return result

    def window(self) -> dict[str, int]:
        with self._locked(exclusive=False):
            rows = self._read_committed_unlocked()
            head = max(self._read_head_unlocked(), rows[-1].cursor if rows else 0)
        return {
            "retained_from_cursor": rows[0].cursor if rows else head + 1,
            "head_cursor": head,
            "retained_events": len(rows),
        }

    def _read_committed_unlocked(self) -> list[ActivityEvent]:
        if not self.path.exists():
            return []
        raw = self.path.read_bytes()
        complete = raw[: raw.rfind(b"\n") + 1] if b"\n" in raw else b""
        rows = []
        for number, line in enumerate(complete.splitlines(), 1):
            if not line:
                continue
            try:
                rows.append(ActivityEvent.from_dict(json.loads(line)))
            except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
                raise ActivityCorruptionError(
                    f"committed activity row {number} is corrupt"
                ) from exc
        if any(right.cursor <= left.cursor for left, right in zip(rows, rows[1:])):
            raise ActivityCorruptionError("activity cursors are not strictly increasing")
        return rows

    def _last_committed_unlocked(self, *, recover_tail: bool) -> ActivityEvent | None:
        if not self.path.exists():
            return None
        mode = "r+b" if recover_tail else "rb"
        with self.path.open(mode) as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            if not size:
                return None
            stream.seek(size - 1)
            complete_size = size
            if stream.read(1) != b"\n":
                if not recover_tail:
                    newline = self._find_last_newline(stream, size)
                    complete_size = newline + 1 if newline >= 0 else 0
                else:
                    newline = self._find_last_newline(stream, size)
                    complete_size = newline + 1 if newline >= 0 else 0
                    stream.truncate(complete_size)
                    stream.flush()
                    os.fsync(stream.fileno())
            if complete_size == 0:
                return None
            read_size = min(complete_size, self.max_event_bytes + 1)
            stream.seek(complete_size - read_size)
            tail = stream.read(read_size).rstrip(b"\n")
        line = tail.rsplit(b"\n", 1)[-1]
        try:
            return ActivityEvent.from_dict(json.loads(line))
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise ActivityCorruptionError("last committed activity row is corrupt") from exc

    @staticmethod
    def _find_last_newline(stream, size: int) -> int:
        """Return the final newline offset without rereading the whole journal."""

        end = size
        while end:
            start = max(0, end - 65_536)
            stream.seek(start)
            chunk = stream.read(end - start)
            found = chunk.rfind(b"\n")
            if found >= 0:
                return start + found
            end = start
        return -1

    def _read_head_unlocked(self) -> int:
        path = self.root / _HEAD_FILENAME
        if not path.exists():
            return 0
        try:
            value = json.loads(path.read_text(encoding="utf-8"))["head_cursor"]
            return int(value) if int(value) >= 0 else 0
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
            return 0

    def _write_head_unlocked(self, cursor: int) -> None:
        fd, name = tempfile.mkstemp(prefix=".head-", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"head_cursor": cursor}, stream, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.root / _HEAD_FILENAME)
            self._fsync_root()
        finally:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass

    def _trim_unlocked(self) -> None:
        if self.path.stat().st_size <= self.max_bytes:
            return
        lines = self.path.read_bytes().splitlines(keepends=True)
        total = sum(len(line) for line in lines)
        while len(lines) > 1 and total > self.max_bytes:
            total -= len(lines.pop(0))
        fd, name = tempfile.mkstemp(prefix=".events-", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.writelines(lines)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            self._fsync_root()
        finally:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass

    def _fsync_root(self) -> None:
        try:
            fd = os.open(self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            # File data remains fsynced; some filesystems do not support directory fsync.
            pass


__all__ = [
    "ActivityContext",
    "ActivityCorruptionError",
    "ActivityError",
    "ActivityEvent",
    "ActivityJournal",
    "ActivityKind",
    "DEFAULT_REPLAY_LIMIT",
    "MAX_REPLAY_LIMIT",
    "default_activity_root",
    "sanitize_activity_data",
    "sanitize_activity_text",
]
