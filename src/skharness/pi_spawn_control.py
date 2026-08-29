"""Authoritative, reversible control for new Pi worker creation.

The state file is the decision owner. Every supported launcher reserves its
worker identity here immediately before its final process or tmux mutation.
Existing reservations are never signalled or cancelled by this module.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Sequence

SCHEMA = "skharness.pi-spawn-control.v2"
MAX_TTL_SECONDS = 3600
CONTROL_SCOPE = "pi:all"
_TOKEN = re.compile(r"\A[A-Za-z0-9._:@/+-]{1,200}\Z")
_TIMESTAMP = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{6})?Z\Z")
_STATE_KEYS = {
    "schema",
    "mode",
    "fence",
    "owner",
    "reason",
    "scope",
    "created_at",
    "expires_at",
    "updated_at",
    "last_operation",
    "workers",
}
_WORKER_KEYS = {"worker_id", "actor", "scope", "kind", "token", "reserved_at"}


class SpawnControlError(RuntimeError):
    """Base class for sanitized control failures."""


class StateUnavailableError(SpawnControlError):
    """The authoritative decision cannot be read and spawning must stop."""


class ControlDeniedError(SpawnControlError):
    """A valid decision denied the requested control or spawn mutation."""


@dataclass(frozen=True)
class Reservation:
    worker_id: str
    token: str
    fence: int
    state_hash: str


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object, name: str) -> datetime:
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        raise StateUnavailableError(f"malformed {name}")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise StateUnavailableError(f"malformed {name}") from exc
    if _timestamp(parsed) != value:
        raise StateUnavailableError(f"malformed {name}")
    return parsed


def _checked_token(name: str, value: str) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise ValueError(f"{name} must be a nonempty bounded token")
    return value


def _checked_reason(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 200
        or value != value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError("reason must be nonempty, bounded, and contain no control characters")
    return value


def _checked_scope(value: str) -> str:
    if value != CONTROL_SCOPE:
        raise ValueError(f"scope must be {CONTROL_SCOPE}")
    return value


def _checked_fence(value: object, name: str = "fence") -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def _validate_operation(operation: object) -> None:
    if not isinstance(operation, dict) or not isinstance(operation.get("command"), str):
        raise StateUnavailableError("Pi spawn last operation is malformed")
    command = operation["command"]
    try:
        if command == "bootstrap" and set(operation) == {"command", "actor", "applied_at"}:
            _checked_token("actor", operation["actor"])
        elif command in {"pause", "drain"} and set(operation) == {
            "command", "owner", "reason", "scope", "ttl_seconds", "expected_fence",
            "applied_at",
        }:
            _checked_token("owner", operation["owner"])
            _checked_reason(operation["reason"])
            _checked_scope(operation["scope"])
            SpawnControl._ttl(operation["ttl_seconds"])
            _checked_fence(operation["expected_fence"], "expected_fence")
        elif command == "renew" and set(operation) == {
            "command", "owner", "fence", "ttl_seconds", "mode", "reason", "scope",
            "applied_at",
        }:
            _checked_token("owner", operation["owner"])
            _checked_fence(operation["fence"])
            SpawnControl._ttl(operation["ttl_seconds"])
            if operation["mode"] not in {"pause", "drain"}:
                raise ValueError("invalid mode")
            _checked_reason(operation["reason"])
            _checked_scope(operation["scope"])
        elif command == "resume" and set(operation) == {
            "command", "owner", "fence", "mode", "reason", "scope", "expires_at",
            "applied_at",
        }:
            _checked_token("owner", operation["owner"])
            _checked_fence(operation["fence"])
            if operation["mode"] not in {"pause", "drain"}:
                raise ValueError("invalid mode")
            _checked_reason(operation["reason"])
            _checked_scope(operation["scope"])
        else:
            raise ValueError("unsupported operation")
        _parse_timestamp(operation["applied_at"], "last_operation.applied_at")
        if command == "resume":
            _parse_timestamp(operation["expires_at"], "last_operation.expires_at")
    except ValueError as exc:
        raise StateUnavailableError("Pi spawn last operation is malformed") from exc


def _request_matches(operation: dict, request: dict) -> bool:
    return all(operation.get(key) == value for key, value in request.items())


def _state_bytes(state: dict) -> bytes:
    return (json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _state_hash(state: dict) -> str:
    return hashlib.sha256(_state_bytes(state)).hexdigest()


class SpawnControl:
    """Atomic file-backed Pi spawn decision and active-worker registry."""

    def __init__(self, path: str | Path, *, clock: Callable[[], datetime] = _utcnow):
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.clock = clock

    def _now(self) -> datetime:
        try:
            return _parse_timestamp(_timestamp(self.clock()), "clock")
        except (TypeError, ValueError, OverflowError, StateUnavailableError) as exc:
            raise StateUnavailableError("Pi spawn clock is invalid") from exc

    def _locked(self, *, bootstrap: bool = False):
        if bootstrap:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.lock_path, flags, 0o600)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o077
            ):
                os.close(descriptor)
                raise StateUnavailableError("Pi spawn decision lock is unsafe")
            handle = os.fdopen(descriptor, "r+", encoding="utf-8")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            return handle
        except (OSError, StateUnavailableError) as exc:
            raise StateUnavailableError("Pi spawn decision is unavailable") from exc

    def _load(self, *, now: datetime | None = None) -> dict:
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.path, flags)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o077
            ):
                os.close(descriptor)
                raise StateUnavailableError("Pi spawn decision file is unsafe")
            with os.fdopen(descriptor, encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise StateUnavailableError("Pi spawn decision is unavailable or malformed") from exc
        if not isinstance(raw, dict) or set(raw) != _STATE_KEYS:
            raise StateUnavailableError("Pi spawn decision has an unknown shape")
        if raw["schema"] != SCHEMA or raw["mode"] not in {"open", "pause", "drain"}:
            raise StateUnavailableError("Pi spawn decision has an unsupported schema or mode")
        try:
            _checked_fence(raw["fence"])
        except ValueError as exc:
            raise StateUnavailableError("Pi spawn decision has an invalid fence") from exc
        created_at = _parse_timestamp(raw["created_at"], "created_at")
        updated_at = _parse_timestamp(raw["updated_at"], "updated_at")
        now = self._now() if now is None else now
        if created_at > updated_at or updated_at > now:
            raise StateUnavailableError("Pi spawn decision has inconsistent timestamps")
        if raw["mode"] == "open":
            if any(raw[name] is not None for name in ("owner", "reason", "scope", "expires_at")):
                raise StateUnavailableError("open Pi spawn decision retains control authority")
        else:
            for name in ("owner", "reason", "scope"):
                try:
                    (
                        _checked_reason(raw[name])
                        if name == "reason"
                        else _checked_scope(raw[name])
                        if name == "scope"
                        else _checked_token(name, raw[name])
                    )
                except ValueError as exc:
                    raise StateUnavailableError(f"Pi spawn decision has invalid {name}") from exc
            _parse_timestamp(raw["expires_at"], "expires_at")
        if not isinstance(raw["workers"], dict):
            raise StateUnavailableError("Pi spawn worker registry is malformed")
        _validate_operation(raw["last_operation"])
        operation = raw["last_operation"]
        applied_at = _parse_timestamp(operation["applied_at"], "last_operation.applied_at")
        if applied_at < created_at or applied_at > updated_at or applied_at > now:
            raise StateUnavailableError("Pi spawn last operation has inconsistent time")
        command = operation["command"]
        if command == "bootstrap":
            valid = (
                raw["mode"] == "open"
                and raw["fence"] == 0
                and all(raw[name] is None for name in ("owner", "reason", "scope", "expires_at"))
                and applied_at == created_at
            )
        elif command in {"pause", "drain"}:
            valid = (
                raw["mode"] == command
                and raw["fence"] == operation["expected_fence"] + 1
                and raw["owner"] == operation["owner"]
                and raw["reason"] == operation["reason"]
                and raw["scope"] == operation["scope"]
                and _parse_timestamp(raw["expires_at"], "expires_at")
                == applied_at + timedelta(seconds=operation["ttl_seconds"])
            )
        elif command == "renew":
            valid = (
                raw["mode"] == operation["mode"]
                and raw["fence"] == operation["fence"] + 1
                and raw["owner"] == operation["owner"]
                and raw["reason"] == operation["reason"]
                and raw["scope"] == operation["scope"]
                and _parse_timestamp(raw["expires_at"], "expires_at")
                == applied_at + timedelta(seconds=operation["ttl_seconds"])
            )
        else:
            valid = (
                raw["mode"] == "open"
                and raw["fence"] == operation["fence"] + 1
                and all(raw[name] is None for name in ("owner", "reason", "scope", "expires_at"))
                and _parse_timestamp(operation["expires_at"], "last_operation.expires_at")
                > created_at
            )
        if not valid:
            raise StateUnavailableError("Pi spawn last operation is inconsistent with state")
        for worker_id, worker in raw["workers"].items():
            if not isinstance(worker, dict) or set(worker) != _WORKER_KEYS:
                raise StateUnavailableError("Pi spawn worker record is malformed")
            if worker_id != worker["worker_id"]:
                raise StateUnavailableError("Pi spawn worker identity is inconsistent")
            for name in ("worker_id", "actor", "scope", "kind", "token"):
                try:
                    (
                        _checked_scope(worker[name])
                        if name == "scope"
                        else _checked_token(name, worker[name])
                    )
                except ValueError as exc:
                    raise StateUnavailableError(f"Pi spawn worker has invalid {name}") from exc
            if worker["kind"] not in {"process", "tmux"}:
                raise StateUnavailableError("Pi spawn worker has invalid kind")
            reserved_at = _parse_timestamp(worker["reserved_at"], "reserved_at")
            if reserved_at < created_at or reserved_at > updated_at or reserved_at > now:
                raise StateUnavailableError("Pi spawn worker has inconsistent time")
        return raw

    def _write(self, state: dict) -> None:
        encoded = _state_bytes(state)
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.path.parent, delete=False) as temp:
                temp_name = temp.name
                os.fchmod(temp.fileno(), 0o600)
                temp.write(encoded)
                temp.flush()
                os.fsync(temp.fileno())
            os.replace(temp_name, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as exc:
            raise StateUnavailableError("Pi spawn decision could not be persisted") from exc
        finally:
            if temp_name:
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass

    def bootstrap(self, *, actor: str) -> dict:
        _checked_token("actor", actor)
        with self._locked(bootstrap=True):
            now = self._now()
            if self.path.exists():
                state = self._load(now=now)
                if not _request_matches(
                    state["last_operation"], {"command": "bootstrap", "actor": actor}
                ):
                    raise ControlDeniedError("Pi spawn bootstrap replay does not match state")
                return self._view(state, now=now)
            timestamp = _timestamp(now)
            state = {
                "schema": SCHEMA,
                "mode": "open",
                "fence": 0,
                "owner": None,
                "reason": None,
                "scope": None,
                "created_at": timestamp,
                "expires_at": None,
                "updated_at": timestamp,
                "last_operation": {
                    "command": "bootstrap", "actor": actor, "applied_at": timestamp
                },
                "workers": {},
            }
            self._write(state)
            return self._view(state, now=now)

    def _view(self, state: dict, *, now: datetime | None = None) -> dict:
        now = self._now() if now is None else now
        view = {
            key: value for key, value in state.items() if key not in {"workers", "last_operation"}
        }
        expired = state["mode"] != "open" and _parse_timestamp(
            state["expires_at"], "expires_at"
        ) <= now
        view["effective_mode"] = "expired" if expired else state["mode"]
        view["workers"] = sorted(
            ({key: value for key, value in worker.items() if key != "token"}
             for worker in state["workers"].values()),
            key=lambda item: item["worker_id"],
        )
        view["quiescent"] = not view["workers"]
        return view

    def status(self, *, _already_locked: bool = False) -> dict:
        now = self._now()
        if _already_locked:
            return self._view(self._load(now=now), now=now)
        with self._locked():
            return self._view(self._load(now=now), now=now)

    def check_open(self) -> dict:
        """Fail closed without registering a worker, for early launcher preflight."""
        with self._locked():
            now = self._now()
            state = self._load(now=now)
            if state["mode"] != "open":
                effective = self._view(state, now=now)["effective_mode"]
                raise ControlDeniedError(
                    f"new Pi worker creation denied: control mode is {effective}"
                )
            return self._view(state, now=now)

    @staticmethod
    def _ttl(ttl_seconds: int) -> int:
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
            raise ValueError("ttl_seconds must be an integer")
        if not 1 <= ttl_seconds <= MAX_TTL_SECONDS:
            raise ValueError(f"ttl_seconds must be between 1 and {MAX_TTL_SECONDS}")
        return ttl_seconds

    def pause(
        self,
        *,
        mode: str,
        owner: str,
        reason: str,
        scope: str,
        ttl_seconds: int,
        expected_fence: int,
    ) -> dict:
        if mode not in {"pause", "drain"}:
            raise ValueError("mode must be pause or drain")
        owner = _checked_token("owner", owner)
        _checked_fence(expected_fence, "expected_fence")
        reason = _checked_reason(reason)
        scope = _checked_scope(scope)
        ttl_seconds = self._ttl(ttl_seconds)
        operation = {
            "command": mode,
            "owner": owner,
            "reason": reason,
            "scope": scope,
            "ttl_seconds": ttl_seconds,
            "expected_fence": expected_fence,
        }
        with self._locked():
            now = self._now()
            state = self._load(now=now)
            if _request_matches(state["last_operation"], operation):
                return self._view(state, now=now)
            if state["mode"] != "open" or state["fence"] != expected_fence:
                raise ControlDeniedError("Pi spawn control is already owned or the fence is stale")
            operation["applied_at"] = _timestamp(now)
            state.update(
                mode=mode,
                fence=state["fence"] + 1,
                owner=owner,
                reason=reason,
                scope=scope,
                expires_at=_timestamp(now + timedelta(seconds=ttl_seconds)),
                updated_at=_timestamp(now),
                last_operation=operation,
            )
            self._write(state)
            return self._view(state, now=now)

    def renew(self, *, owner: str, fence: int, ttl_seconds: int) -> dict:
        owner = _checked_token("owner", owner)
        _checked_fence(fence)
        ttl_seconds = self._ttl(ttl_seconds)
        request = {
            "command": "renew",
            "owner": owner,
            "fence": fence,
            "ttl_seconds": ttl_seconds,
        }
        with self._locked():
            now = self._now()
            state = self._load(now=now)
            if _request_matches(state["last_operation"], request):
                return self._view(state, now=now)
            if state["mode"] == "open" or state["owner"] != owner or state["fence"] != fence:
                raise ControlDeniedError("Pi spawn renewal owner or fence is stale")
            if _parse_timestamp(state["expires_at"], "expires_at") <= now:
                raise ControlDeniedError("expired Pi spawn control cannot be renewed")
            operation = {
                **request,
                "mode": state["mode"],
                "reason": state["reason"],
                "scope": state["scope"],
                "applied_at": _timestamp(now),
            }
            state["fence"] += 1
            state["expires_at"] = _timestamp(now + timedelta(seconds=ttl_seconds))
            state["updated_at"] = _timestamp(now)
            state["last_operation"] = operation
            self._write(state)
            return self._view(state, now=now)

    def resume(self, *, owner: str, fence: int) -> dict:
        owner = _checked_token("owner", owner)
        _checked_fence(fence)
        request = {"command": "resume", "owner": owner, "fence": fence}
        with self._locked():
            now_value = self._now()
            state = self._load(now=now_value)
            if _request_matches(state["last_operation"], request):
                return self._view(state, now=now_value)
            if state["mode"] == "open" or state["owner"] != owner or state["fence"] != fence:
                raise ControlDeniedError("Pi spawn resume owner or fence is stale")
            now = _timestamp(now_value)
            operation = {
                **request,
                "mode": state["mode"],
                "reason": state["reason"],
                "scope": state["scope"],
                "expires_at": state["expires_at"],
                "applied_at": now,
            }
            state.update(
                mode="open",
                fence=state["fence"] + 1,
                owner=None,
                reason=None,
                scope=None,
                expires_at=None,
                updated_at=now,
                last_operation=operation,
            )
            self._write(state)
            return self._view(state, now=now_value)

    def reserve(self, worker_id: str, *, actor: str, scope: str, kind: str) -> Reservation:
        worker_id = _checked_token("worker_id", worker_id)
        actor = _checked_token("actor", actor)
        scope = _checked_scope(scope)
        if kind not in {"process", "tmux"}:
            raise ValueError("kind must be process or tmux")
        with self._locked():
            now = self._now()
            state = self._load(now=now)
            if state["mode"] != "open":
                effective = self._view(state, now=now)["effective_mode"]
                raise ControlDeniedError(
                    f"new Pi worker creation denied: control mode is {effective}"
                )
            if worker_id in state["workers"]:
                raise ControlDeniedError("Pi worker identity is already reserved")
            token = uuid.uuid4().hex
            state["workers"][worker_id] = {
                "worker_id": worker_id,
                "actor": actor,
                "scope": scope,
                "kind": kind,
                "token": token,
                "reserved_at": _timestamp(now),
            }
            state["updated_at"] = _timestamp(now)
            self._write(state)
            return Reservation(
                worker_id=worker_id,
                token=token,
                fence=state["fence"],
                state_hash=_state_hash(state),
            )

    def validate_reservation(self, reservation: Reservation) -> None:
        """Revalidate persisted state immediately before a process or tmux mutation."""
        with self._locked():
            now = self._now()
            state = self._load(now=now)
            worker = state["workers"].get(reservation.worker_id)
            if (
                worker is None
                or worker["token"] != reservation.token
                or _state_hash(state) != reservation.state_hash
            ):
                raise ControlDeniedError("Pi worker reservation is stale")

    def finish(self, worker_id: str, *, token: str) -> dict:
        worker_id = _checked_token("worker_id", worker_id)
        token = _checked_token("token", token)
        with self._locked():
            now = self._now()
            state = self._load(now=now)
            worker = state["workers"].get(worker_id)
            if worker is None:
                return self._view(state, now=now)
            if worker["token"] != token:
                raise ControlDeniedError("Pi worker completion token is stale")
            del state["workers"][worker_id]
            state["updated_at"] = _timestamp(now)
            self._write(state)
            return self._view(state, now=now)

    def guarded_run(
        self,
        argv: Sequence[str],
        *,
        worker_id: str,
        actor: str,
        scope: str,
        kind: str,
        runner: Callable | None = None,
        **kwargs,
    ):
        if not argv or not all(isinstance(item, str) and item for item in argv):
            raise ValueError("argv must contain nonempty strings")
        reservation = self.reserve(worker_id, actor=actor, scope=scope, kind=kind)
        try:
            self.validate_reservation(reservation)
            return (runner or subprocess.run)(list(argv), **kwargs)
        finally:
            self.finish(worker_id, token=reservation.token)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Control new Pi worker creation")
    parser.add_argument("--state", required=True, type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    bootstrap = sub.add_parser("bootstrap")
    bootstrap.add_argument("--actor", required=True)
    for command in ("pause", "drain"):
        item = sub.add_parser(command)
        item.add_argument("--owner", required=True)
        item.add_argument("--reason", required=True)
        item.add_argument("--scope", required=True)
        item.add_argument("--ttl-seconds", required=True, type=int)
        item.add_argument("--expected-fence", required=True, type=int)
    status = sub.add_parser("status")
    status.set_defaults(command="status")
    renew = sub.add_parser("renew")
    renew.add_argument("--owner", required=True)
    renew.add_argument("--fence", required=True, type=int)
    renew.add_argument("--ttl-seconds", required=True, type=int)
    resume = sub.add_parser("resume")
    resume.add_argument("--owner", required=True)
    resume.add_argument("--fence", required=True, type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    control = SpawnControl(args.state)
    try:
        if args.command == "bootstrap":
            result = control.bootstrap(actor=args.actor)
        elif args.command in {"pause", "drain"}:
            result = control.pause(
                mode=args.command,
                owner=args.owner,
                reason=args.reason,
                scope=args.scope,
                ttl_seconds=args.ttl_seconds,
                expected_fence=args.expected_fence,
            )
        elif args.command == "renew":
            result = control.renew(
                owner=args.owner, fence=args.fence, ttl_seconds=args.ttl_seconds
            )
        elif args.command == "resume":
            result = control.resume(owner=args.owner, fence=args.fence)
        else:
            result = control.status()
    except (SpawnControlError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps({"ok": True, "control": result}, sort_keys=True))
    return 0


def launch_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Launch one guarded Pi worker mutation")
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--scope", required=True)
    parser.add_argument("--kind", required=True, choices=("process", "tmux"))
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command is required after --")
    try:
        result = SpawnControl(args.state).guarded_run(
            command,
            worker_id=args.worker_id,
            actor=args.actor,
            scope=args.scope,
            kind=args.kind,
        )
    except (SpawnControlError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2
    return int(result.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
