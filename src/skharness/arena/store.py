"""Append-only event segments and a content-addressed artifact store."""

from __future__ import annotations

import os
import re
import secrets
from pathlib import Path

from .models import Experiment, ExperimentEvent, Result

try:
    import fcntl
except ImportError:  # pragma: no cover - the production images are POSIX
    fcntl = None  # type: ignore[assignment]


class CorruptEventLogError(RuntimeError):
    """A committed event or hash link failed validation."""


class EventConflictError(RuntimeError):
    """An append did not extend the writer's observed head."""


# Compatibility names used by the first arena integration branch. New callers
# should prefer the PEP-8-compliant ``*Error`` forms.
CorruptEventLog = CorruptEventLogError
EventConflict = EventConflictError


_SAFE_WRITER = re.compile(r"^[A-Za-z0-9_.-]+$")


class ArenaStore:
    """Filesystem store with one locked append-only segment per writer."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.events_dir = self.root / "events"
        self.artifacts_dir = self.root / "artifacts" / "sha256"
        self.specs_dir = self.root / "specs"
        self.experiments_dir = self.root / "experiments"
        self.results_dir = self.root / "results"
        for directory in (
            self.events_dir,
            self.artifacts_dir,
            self.specs_dir,
            self.experiments_dir,
            self.results_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def _segment(self, writer_id: str) -> Path:
        if not _SAFE_WRITER.fullmatch(writer_id):
            raise ValueError("writer_id contains unsafe path characters")
        return self.events_dir / f"{writer_id}.jsonl"

    def read_segment(self, writer_id: str) -> list[ExperimentEvent]:
        return self._read_path(self._segment(writer_id))

    def read_all_events(self) -> list[ExperimentEvent]:
        events: list[ExperimentEvent] = []
        for path in sorted(self.events_dir.glob("*.jsonl")):
            events.extend(self._read_path(path))
        return sorted(events, key=lambda event: (event.timestamp, event.writer_id, event.sequence))

    def _read_path(self, path: Path) -> list[ExperimentEvent]:
        if not path.exists():
            return []
        data = path.read_bytes()
        raw_lines = data.splitlines(keepends=True)
        events: list[ExperimentEvent] = []
        prior_hash: str | None = None
        for index, raw in enumerate(raw_lines):
            final_incomplete = index == len(raw_lines) - 1 and not raw.endswith(b"\n")
            try:
                event = ExperimentEvent.model_validate_json(raw)
            except Exception as exc:
                if final_incomplete:
                    break
                raise CorruptEventLogError(f"invalid event at {path}:{index + 1}") from exc
            if event.sequence != len(events) + 1:
                raise CorruptEventLogError(f"non-contiguous sequence at {path}:{index + 1}")
            if event.prior_event_hash != prior_hash:
                raise CorruptEventLogError(f"broken hash link at {path}:{index + 1}")
            if event.event_hash != event.calculated_hash():
                raise CorruptEventLogError(f"invalid event hash at {path}:{index + 1}")
            events.append(event)
            prior_hash = event.event_hash
        return events

    def append_event(self, event: ExperimentEvent) -> ExperimentEvent:
        path = self._segment(event.writer_id)
        with path.open("a+b") as stream:
            if fcntl is not None:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            stream.seek(0)
            existing = self._read_path(path)
            # A killed writer may leave one partial final record. It was never a
            # committed event, so remove exactly that tail before extending the log.
            stream.seek(0)
            raw = stream.read()
            if raw and not raw.endswith(b"\n"):
                last_complete = raw.rfind(b"\n") + 1
                stream.seek(last_complete)
                stream.truncate()
            expected_sequence = len(existing) + 1
            expected_prior = existing[-1].event_hash if existing else None
            if event.sequence != expected_sequence or event.prior_event_hash != expected_prior:
                raise EventConflictError(
                    f"expected sequence={expected_sequence} prior={expected_prior!r}; "
                    f"received sequence={event.sequence} prior={event.prior_event_hash!r}"
                )
            sealed = event.sealed()
            stream.seek(0, os.SEEK_END)
            stream.write(sealed.model_dump_json(exclude_none=True).encode() + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
            return sealed

    def put_artifact(self, content: bytes) -> str:
        # Artifact identity is the conventional digest of the bytes, not JSON.
        import hashlib

        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        hex_digest = digest.removeprefix("sha256:")
        path = self.artifacts_dir / hex_digest[:2] / hex_digest[2:]
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_bytes() != content:
                raise CorruptEventLogError(f"artifact digest collision at {path}")
            return digest
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != content:
                    raise CorruptEventLogError(f"artifact digest collision at {path}")
        finally:
            temporary.unlink(missing_ok=True)
        return digest

    def get_artifact(self, digest: str) -> bytes:
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError("artifact digest must be sha256:<64 lowercase hex characters>")
        value = digest.removeprefix("sha256:")
        path = self.artifacts_dir / value[:2] / value[2:]
        content = path.read_bytes()
        import hashlib

        if hashlib.sha256(content).hexdigest() != value:
            raise CorruptEventLogError(f"artifact content does not match digest {digest}")
        return content

    def put_spec(self, spec) -> str:
        digest = spec.content_hash
        path = self.specs_dir / f"{digest.removeprefix('sha256:')}.json"
        payload = spec.model_dump_json(exclude_none=True, indent=2).encode() + b"\n"
        if path.exists() and path.read_bytes() != payload:
            raise CorruptEventLogError(f"spec content does not match existing hash {digest}")
        if not path.exists():
            path.write_bytes(payload)
        return digest

    def _put_record(self, directory: Path, record) -> str:
        """Persist an immutable model under its canonical content hash."""
        digest = record.content_hash
        path = directory / f"{digest.removeprefix('sha256:')}.json"
        payload = record.model_dump_json(exclude_none=True, indent=2).encode() + b"\n"
        if path.exists() and path.read_bytes() != payload:
            raise CorruptEventLogError(f"record content does not match existing hash {digest}")
        if not path.exists():
            temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
            try:
                with temporary.open("xb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(temporary, path)
                    directory_fd = os.open(directory, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                except FileExistsError:
                    if path.read_bytes() != payload:
                        raise CorruptEventLogError(
                            f"record content does not match existing hash {digest}"
                        )
            finally:
                temporary.unlink(missing_ok=True)
        return digest

    def put_experiment(self, experiment: Experiment) -> str:
        """Persist one content-addressed immutable experiment."""
        return self._put_record(self.experiments_dir, experiment)

    def put_result(self, result: Result) -> str:
        """Persist one content-addressed immutable result."""
        return self._put_record(self.results_dir, result)

    def read_experiments(self) -> list[Experiment]:
        """Read and validate every persisted experiment."""
        return self._read_records(self.experiments_dir, Experiment)

    def read_results(self) -> list[Result]:
        """Read and validate every persisted result."""
        return self._read_records(self.results_dir, Result)

    @staticmethod
    def _read_records(directory: Path, model_type) -> list:
        """Validate both content and content-addressed filename on every read."""
        records = []
        for path in sorted(directory.glob("*.json")):
            try:
                record = model_type.model_validate_json(path.read_bytes())
            except Exception as exc:
                raise CorruptEventLogError(f"invalid immutable record at {path}") from exc
            if path.stem != record.content_hash.removeprefix("sha256:"):
                raise CorruptEventLogError(f"immutable record hash mismatch at {path}")
            records.append(record)
        return records
