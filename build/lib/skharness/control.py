"""Durable, auditable Atlas control commands and receipts.

Observation and control are deliberately separate.  ActivityEvent is a view;
ControlCommand is an authenticated request to an owning controller.  A queued
command is not proof that an action happened: only an applied receipt says so.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1
MAX_CONTROL_BYTES = 16_384
_SAFE_ID = re.compile(r"[A-Za-z0-9._:@+-]{1,200}\Z")
_IDEMPOTENCY = re.compile(r"[A-Za-z0-9._:@+-]{1,128}\Z")
_SECRET_KEY = re.compile(
    r"(?:^|[_-])(?:authorization|cookie|credential|password|secret|token|"
    r"(?:access|refresh|session|bearer|auth)[_-]?token|api[_-]?key)(?:$|[_-])",
    re.I,
)
_SECRET_VALUE = re.compile(
    r"(?i)(?:bearer\s+|api[_-]?key\s*[:=]\s*|token\s*[:=]\s*)"
    r"[^\s,;\"']+"
)


def _safe_detail(value: object) -> str:
    return _SECRET_VALUE.sub("[redacted]", str(value).replace("\x00", ""))[:1_024]


class ControlError(RuntimeError):
    """Base control-plane error."""


class ControlConflictError(ControlError):
    """An idempotency key was reused for different command bytes."""


class ControlCorruptionError(ControlError):
    """A committed control-journal row failed validation."""


class ControlTargetKind(str, Enum):
    SESSION = "session"
    RUN = "run"
    AGENT = "agent"
    JOB = "job"


class ControlAction(str, Enum):
    MESSAGE = "message"
    NEEDS_INPUT_RESPONSE = "needs_input_response"
    CANCEL = "cancel"
    PAUSE = "pause"
    RESUME = "resume"
    RETRY = "retry"


class ControlStatus(str, Enum):
    QUEUED = "queued"
    APPLYING = "applying"
    APPLIED = "applied"
    REJECTED = "rejected"
    UNSUPPORTED = "unsupported"
    CONFLICT = "conflict"
    EXPIRED = "expired"


TERMINAL_CONTROL_STATUSES = frozenset(
    {
        ControlStatus.APPLIED,
        ControlStatus.REJECTED,
        ControlStatus.UNSUPPORTED,
        ControlStatus.CONFLICT,
        ControlStatus.EXPIRED,
    }
)


@dataclass(frozen=True)
class ControlCommand:
    schema_version: int
    command_id: str
    idempotency_key: str
    actor: str
    target_kind: ControlTargetKind
    target_id: str
    action: ControlAction
    expected_state: str
    payload: dict[str, Any]
    payload_digest: str
    submitted_at: float
    expires_at: float

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported control schema_version")
        for name in ("command_id", "actor", "target_id"):
            if not _SAFE_ID.fullmatch(getattr(self, name)):
                raise ValueError(f"invalid control {name}")
        if not _IDEMPOTENCY.fullmatch(self.idempotency_key):
            raise ValueError("invalid control idempotency_key")
        if self.expected_state and not _SAFE_ID.fullmatch(self.expected_state):
            raise ValueError("invalid control expected_state")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.payload_digest):
            raise ValueError("invalid control payload_digest")
        encoded = json.dumps(
            self.payload, sort_keys=True, separators=(",", ":")
        ).encode()
        expected = "sha256:" + hashlib.sha256(encoded).hexdigest()
        if self.payload_digest != expected:
            raise ValueError("control payload_digest does not match payload")
        if self.submitted_at <= 0 or self.expires_at <= self.submitted_at:
            raise ValueError("control expiry must be after submission")

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["target_kind"] = self.target_kind.value
        row["action"] = self.action.value
        return row

    def to_public_dict(self) -> dict[str, Any]:
        """Return controller metadata without replaying steering payload bytes."""

        row = self.to_dict()
        row.pop("payload")
        return row

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "ControlCommand":
        values = dict(row)
        values["target_kind"] = ControlTargetKind(values["target_kind"])
        values["action"] = ControlAction(values["action"])
        values["payload"] = dict(values.get("payload") or {})
        return cls(**values)


@dataclass(frozen=True)
class ControlReceipt:
    schema_version: int
    command_id: str
    receipt_id: str
    status: ControlStatus
    recorded_at: float
    controller: str
    detail: str = ""
    activity_cursor: int | None = None

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported receipt schema_version")
        for name in ("command_id", "receipt_id", "controller"):
            if not _SAFE_ID.fullmatch(getattr(self, name)):
                raise ValueError(f"invalid receipt {name}")
        if self.recorded_at <= 0:
            raise ValueError("receipt recorded_at must be positive")
        if len(self.detail) > 1_024:
            raise ValueError("receipt detail is too long")
        if self.detail != _safe_detail(self.detail):
            raise ValueError("receipt detail contains unsafe credential-shaped text")
        if self.activity_cursor is not None and self.activity_cursor < 1:
            raise ValueError("receipt activity_cursor must be positive")

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["status"] = self.status.value
        return row

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "ControlReceipt":
        values = dict(row)
        values["status"] = ControlStatus(values["status"])
        return cls(**values)


def default_control_root() -> Path:
    state = os.environ.get("SKCODE_STATE_DIR")
    base = Path(state) if state else Path.home() / ".skcapstone" / "skcode"
    return base / "control"


class ControlJournal:
    """Process-safe append-only command/receipt mailbox for controller owners."""

    def __init__(self, root: str | Path | None = None, *, clock=time.time) -> None:
        self.root = Path(root) if root is not None else default_control_root()
        self.path = self.root / "control.jsonl"
        self.lock_path = self.root / ".control.lock"
        self.clock = clock

    @contextmanager
    def _locked(self, *, exclusive: bool) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        with self.lock_path.open("a+b") as lock:
            try:
                os.chmod(self.lock_path, 0o600)
            except OSError:
                pass
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _payload(payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
        def validate(value: Any, depth: int = 0) -> Any:
            if depth > 4:
                raise ValueError("control payload nesting is too deep")
            if value is None or isinstance(value, (bool, int, float)):
                return value
            if isinstance(value, str):
                if "\x00" in value or len(value) > 4_096:
                    raise ValueError("control payload string is invalid or too long")
                return value
            if isinstance(value, list):
                if len(value) > 64:
                    raise ValueError("control payload list is too long")
                return [validate(item, depth + 1) for item in value]
            if isinstance(value, dict):
                if len(value) > 64:
                    raise ValueError("control payload object has too many keys")
                result = {}
                for key, item in value.items():
                    if not isinstance(key, str) or not key or len(key) > 100:
                        raise ValueError("control payload key is invalid")
                    if _SECRET_KEY.search(key):
                        raise ValueError("credential-bearing control payload keys are forbidden")
                    result[key] = validate(item, depth + 1)
                return result
            raise ValueError("control payload contains a non-JSON value")

        bounded = validate(payload)
        if not isinstance(bounded, dict):  # pragma: no cover - public input is typed dict
            raise ValueError("control payload must be an object")
        encoded = json.dumps(bounded, sort_keys=True, separators=(",", ":")).encode()
        if len(encoded) > MAX_CONTROL_BYTES // 2:
            raise ValueError("control payload is too large")
        return bounded, "sha256:" + hashlib.sha256(encoded).hexdigest()

    def submit(
        self,
        *,
        actor: str,
        idempotency_key: str,
        target_kind: ControlTargetKind,
        target_id: str,
        action: ControlAction,
        payload: dict[str, Any],
        expected_state: str = "",
        ttl_s: float = 300,
    ) -> tuple[ControlCommand, ControlReceipt, bool]:
        if not 1 <= ttl_s <= 3_600:
            raise ValueError("control ttl_s must be between 1 and 3600")
        bounded, digest = self._payload(payload)
        with self._locked(exclusive=True):
            self._recover_tail_unlocked()
            commands, receipts = self._read_unlocked()
            for existing in commands:
                if existing.actor == actor and existing.idempotency_key == idempotency_key:
                    if (
                        existing.target_kind is not target_kind
                        or existing.target_id != target_id
                        or existing.action is not action
                        or existing.expected_state != expected_state
                        or existing.payload_digest != digest
                    ):
                        raise ControlConflictError(
                            "control idempotency key was reused with different content"
                        )
                    latest = self._latest(existing.command_id, receipts)
                    return existing, latest, True
            now = float(self.clock())
            command = ControlCommand(
                schema_version=SCHEMA_VERSION,
                command_id="cmd-" + secrets.token_hex(16),
                idempotency_key=idempotency_key,
                actor=actor,
                target_kind=target_kind,
                target_id=target_id,
                action=action,
                expected_state=expected_state,
                payload=bounded,
                payload_digest=digest,
                submitted_at=now,
                expires_at=now + float(ttl_s),
            )
            receipt = ControlReceipt(
                schema_version=SCHEMA_VERSION,
                command_id=command.command_id,
                receipt_id="receipt-" + secrets.token_hex(16),
                status=ControlStatus.QUEUED,
                recorded_at=now,
                controller="skcode-hostd",
            )
            self._append_unlocked("command", command.to_dict())
            self._append_unlocked("receipt", receipt.to_dict())
            return command, receipt, False

    def record(
        self,
        command_id: str,
        status: ControlStatus,
        *,
        controller: str,
        detail: str = "",
        activity_cursor: int | None = None,
    ) -> ControlReceipt:
        with self._locked(exclusive=True):
            self._recover_tail_unlocked()
            commands, receipts = self._read_unlocked()
            if not any(item.command_id == command_id for item in commands):
                raise KeyError("unknown control command")
            latest = self._latest(command_id, receipts)
            if latest.status in TERMINAL_CONTROL_STATUSES:
                if latest.status is status:
                    return latest
                raise ControlConflictError("terminal control receipt cannot transition")
            allowed = {
                ControlStatus.QUEUED: {
                    ControlStatus.QUEUED,
                    ControlStatus.APPLYING,
                    *TERMINAL_CONTROL_STATUSES,
                },
                ControlStatus.APPLYING: {
                    ControlStatus.APPLYING,
                    *TERMINAL_CONTROL_STATUSES,
                },
            }
            if status not in allowed.get(latest.status, set()):
                raise ControlConflictError(
                    f"illegal control transition {latest.status.value}->{status.value}"
                )
            receipt = ControlReceipt(
                schema_version=SCHEMA_VERSION,
                command_id=command_id,
                receipt_id="receipt-" + secrets.token_hex(16),
                status=status,
                recorded_at=float(self.clock()),
                controller=controller,
                detail=_safe_detail(detail),
                activity_cursor=activity_cursor,
            )
            self._append_unlocked("receipt", receipt.to_dict())
            return receipt

    def claim(self, command_id: str, *, controller: str) -> tuple[ControlReceipt, bool]:
        """Atomically claim one queued command for exactly one owning controller."""

        with self._locked(exclusive=True):
            self._recover_tail_unlocked()
            commands, receipts = self._read_unlocked()
            command = next(
                (item for item in commands if item.command_id == command_id), None
            )
            if command is None:
                raise KeyError("unknown control command")
            latest = self._latest(command_id, receipts)
            if latest.status is not ControlStatus.QUEUED:
                return latest, False
            now = float(self.clock())
            status = (
                ControlStatus.EXPIRED if command.expires_at <= now else ControlStatus.APPLYING
            )
            receipt = ControlReceipt(
                schema_version=SCHEMA_VERSION,
                command_id=command.command_id,
                receipt_id="receipt-" + secrets.token_hex(16),
                status=status,
                recorded_at=now,
                controller=controller,
                detail=(
                    "command expired before owner claimed it"
                    if status is ControlStatus.EXPIRED
                    else ""
                ),
            )
            self._append_unlocked("receipt", receipt.to_dict())
            return receipt, status is ControlStatus.APPLYING

    def get(self, command_id: str) -> tuple[ControlCommand, ControlReceipt]:
        with self._locked(exclusive=False):
            commands, receipts = self._read_unlocked()
        command = next((item for item in commands if item.command_id == command_id), None)
        if command is None:
            raise KeyError("unknown control command")
        return command, self._latest(command_id, receipts)

    def pending(
        self, *, target_kind: ControlTargetKind | None = None, target_id: str = ""
    ) -> tuple[ControlCommand, ...]:
        with self._locked(exclusive=True):
            self._recover_tail_unlocked()
            commands, receipts = self._read_unlocked()
            now = float(self.clock())
            pending = []
            for command in commands:
                latest = self._latest(command.command_id, receipts)
                if latest.status is not ControlStatus.QUEUED:
                    continue
                if command.expires_at <= now:
                    expired = ControlReceipt(
                        schema_version=SCHEMA_VERSION,
                        command_id=command.command_id,
                        receipt_id="receipt-" + secrets.token_hex(16),
                        status=ControlStatus.EXPIRED,
                        recorded_at=now,
                        controller="control-journal",
                        detail="command expired before owner applied it",
                    )
                    self._append_unlocked("receipt", expired.to_dict())
                    receipts.append(expired)
                    continue
                if target_kind is not None and command.target_kind is not target_kind:
                    continue
                if target_id and command.target_id != target_id:
                    continue
                pending.append(command)
            return tuple(pending)

    @staticmethod
    def _latest(command_id: str, receipts: list[ControlReceipt]) -> ControlReceipt:
        matches = [item for item in receipts if item.command_id == command_id]
        if not matches:
            raise ControlCorruptionError("control command has no receipt")
        return matches[-1]

    def _append_unlocked(self, record_type: str, value: dict[str, Any]) -> None:
        unsigned = {"record_type": record_type, "value": value}
        record_sha256 = "sha256:" + hashlib.sha256(
            json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        encoded = (
            json.dumps(
                {**unsigned, "record_sha256": record_sha256}, sort_keys=True
            )
            + "\n"
        ).encode()
        if len(encoded) > MAX_CONTROL_BYTES:
            raise ValueError("control record is too large")
        fd = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, encoded)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            root_fd = os.open(
                self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                os.fsync(root_fd)
            finally:
                os.close(root_fd)
        except OSError:
            pass

    def _recover_tail_unlocked(self) -> None:
        if not self.path.exists():
            return
        raw = self.path.read_bytes()
        if not raw or raw.endswith(b"\n"):
            return
        newline = raw.rfind(b"\n")
        with self.path.open("r+b") as stream:
            stream.truncate(newline + 1 if newline >= 0 else 0)
            stream.flush()
            os.fsync(stream.fileno())

    def _read_unlocked(self) -> tuple[list[ControlCommand], list[ControlReceipt]]:
        if not self.path.exists():
            return [], []
        commands = []
        receipts = []
        raw = self.path.read_bytes()
        complete = raw[: raw.rfind(b"\n") + 1] if b"\n" in raw else b""
        for line_number, line in enumerate(complete.splitlines(), 1):
            try:
                row = json.loads(line)
                unsigned = {"record_type": row["record_type"], "value": row["value"]}
                expected = "sha256:" + hashlib.sha256(
                    json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                if row.get("record_sha256") != expected:
                    raise ValueError("control record hash mismatch")
                if row["record_type"] == "command":
                    commands.append(ControlCommand.from_dict(row["value"]))
                elif row["record_type"] == "receipt":
                    receipts.append(ControlReceipt.from_dict(row["value"]))
                else:
                    raise ValueError("unknown record type")
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ControlCorruptionError(
                    f"committed control row {line_number} is corrupt"
                ) from exc
        return commands, receipts


__all__ = [
    "ControlAction",
    "ControlCommand",
    "ControlConflictError",
    "ControlCorruptionError",
    "ControlJournal",
    "ControlReceipt",
    "ControlStatus",
    "ControlTargetKind",
    "TERMINAL_CONTROL_STATUSES",
    "default_control_root",
]
