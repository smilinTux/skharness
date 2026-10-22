"""Host-side backend for the narrow Pi SK bridge.

The worker invokes this executable through a runtime mount.  Imports of SK packages
remain host-side; no agent home or board is copied into the worker image.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Callable

from .access import AccessDeniedError
from .collaboration import CollaborationError


class BackendInputError(ValueError):
    """An operation payload failed its exact schema."""


BACKEND_OPERATIONS = frozenset(
    {
        "capstone.card.read",
        "capstone.card.claim",
        "capstone.progress.append",
        "arena.progress.append",
        "arena.result.append",
        "arena.verdict.append",
        "arena.experiment.search",
        "arena.experiment.reproduce",
        "arena.experiment.mutate",
        "arena.negative.search",
        "memory.recall",
        "memory.scratch.append",
        "memory.proposal.append",
    }
)


def _keys(payload: dict, required: set[str], optional: set[str] = set()) -> None:
    if set(payload) - required - optional or not required <= set(payload):
        raise BackendInputError(
            f"payload keys must be required={sorted(required)}, optional={sorted(optional)}"
        )


def _jsonable(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "__dict__"):
        return value.__dict__
    return value


class LocalSKBackend:
    def __init__(
        self,
        *,
        capstone_home: Path,
        event_dir: Path,
        agent: str,
        collaboration_access=None,
    ) -> None:
        self.capstone_home = capstone_home
        self.event_dir = event_dir
        self.agent = agent
        self.collaboration_access = collaboration_access

    def invoke(self, operation: str, payload: dict) -> dict:
        handlers: dict[str, Callable[[dict], dict]] = {
            "capstone.card.read": self.card_read,
            "capstone.card.claim": self.card_claim,
            "capstone.progress.append": self.progress_append,
            "arena.progress.append": self.arena_append,
            "arena.result.append": self.arena_append,
            "arena.verdict.append": self.arena_append,
            "arena.experiment.search": self.experiment_search,
            "arena.experiment.reproduce": self.experiment_reproduce,
            "arena.experiment.mutate": self.experiment_mutate,
            "arena.negative.search": self.negative_search,
            "memory.recall": self.memory_recall,
            "memory.scratch.append": self.memory_append,
            "memory.proposal.append": self.memory_append,
        }
        assert frozenset(handlers) == BACKEND_OPERATIONS
        try:
            handler = handlers[operation]
        except KeyError as exc:
            raise BackendInputError(f"unsupported backend operation: {operation}") from exc
        return handler(payload)

    def _board(self):
        from skcapstone.coordination import Board

        return Board(self.capstone_home)

    def card_read(self, payload: dict) -> dict:
        _keys(payload, {"card_id"})
        matches = [
            task
            for task in self._board().load_tasks(include_archived=True)
            if task.id == payload["card_id"]
        ]
        return {"found": bool(matches), "card": _jsonable(matches[0]) if matches else None}

    def card_claim(self, payload: dict) -> dict:
        _keys(payload, {"card_id"})
        agent = self._board().claim_task(self.agent, str(payload["card_id"]))
        return {"claimed": True, "agent": _jsonable(agent)}

    def progress_append(self, payload: dict) -> dict:
        _keys(
            payload,
            {"card_id", "run_id", "round", "outcome", "tried", "why_failed"},
            {"replacement_hint"},
        )
        path = self._board().record_attempt(
            str(payload["card_id"]),
            str(payload["run_id"]),
            int(payload["round"]),
            str(payload["outcome"]),
            str(payload["tried"]),
            str(payload["why_failed"]),
            str(payload.get("replacement_hint", "")),
        )
        return {"appended": True, "path": str(path)}

    def arena_append(self, payload: dict) -> dict:
        _keys(payload, {"idempotency_key", "record"})
        if not isinstance(payload["record"], dict):
            raise BackendInputError("record must be an object")
        self.event_dir.mkdir(parents=True, exist_ok=True)
        key = str(payload["idempotency_key"])
        if not key or any(
            c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
            for c in key
        ):
            raise BackendInputError("invalid idempotency_key")
        target = self.event_dir / f"{key}.json"
        encoded = json.dumps(payload["record"], sort_keys=True, separators=(",", ":")) + "\n"
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if target.read_text() != encoded:
                raise BackendInputError("idempotency key collision")
            return {"appended": True, "duplicate": True, "path": str(target)}
        with os.fdopen(fd, "w") as stream:
            stream.write(encoded)
        return {"appended": True, "duplicate": False, "path": str(target)}

    def memory_recall(self, payload: dict) -> dict:
        _keys(payload, {"query"}, {"limit"})
        from skmemory.store import MemoryStore

        rows = MemoryStore().search(
            str(payload["query"]), limit=min(int(payload.get("limit", 5)), 20)
        )
        return {"memories": [_jsonable(row) for row in rows]}

    def memory_append(self, payload: dict) -> dict:
        _keys(payload, {"idempotency_key", "record"})
        return self.arena_append(payload)

    def _arena_objects(self):
        from .collaboration import NegativeKnowledge
        from .models import Experiment, Result

        experiments, results, negatives = [], [], []
        if not self.event_dir.exists():
            return experiments, results, negatives
        types = {
            "arena.experiment.v1": (Experiment, experiments),
            "arena.result.v1": (Result, results),
            "arena.negative.v1": (NegativeKnowledge, negatives),
        }
        for path in sorted(self.event_dir.glob("*.json")):
            try:
                raw = json.loads(path.read_text())
                model, destination = types[raw.get("schema_version")]
                destination.append(model.model_validate(raw))
            except (KeyError, TypeError, ValueError):
                continue
        return experiments, results, negatives

    def _catalog(self):
        from .collaboration import ExperimentCatalog

        experiments, results, _ = self._arena_objects()
        return ExperimentCatalog(experiments, results)

    def experiment_search(self, payload: dict) -> dict:
        _keys(payload, set(), {"challenge_hash", "actor", "harness", "verification"})
        from .models import VerificationState

        verification = payload.get("verification")
        state = VerificationState(verification) if verification is not None else None
        matches = self._catalog().discover(
            challenge_hash=payload.get("challenge_hash"),
            actor=payload.get("actor"),
            harness=payload.get("harness"),
            verification=state,
        )
        return {
            "experiments": [
                {
                    "experiment": row.experiment.model_dump(mode="json"),
                    "result": row.result.model_dump(mode="json") if row.result else None,
                }
                for row in matches
            ]
        }

    def negative_search(self, payload: dict) -> dict:
        _keys(payload, set(), {"query", "challenge_hash", "kind", "changed_dimension"})
        from .collaboration import NegativeKind, NegativeKnowledgeIndex

        _, _, records = self._arena_objects()
        kind = NegativeKind(payload["kind"]) if payload.get("kind") else None
        matches = NegativeKnowledgeIndex(records).search(
            str(payload.get("query", "")),
            challenge_hash=payload.get("challenge_hash"),
            kind=kind,
            changed_dimension=payload.get("changed_dimension"),
        )
        return {"negative_evidence": [row.model_dump(mode="json") for row in matches]}

    def experiment_reproduce(self, payload: dict) -> dict:
        _keys(
            payload,
            {
                "immutable_evidence_id",
                "experiment_id",
                "attempt_id",
                "run_id",
                "created_at",
                "idempotency_key",
            },
        )
        access = self._require_collaboration_access()
        from datetime import datetime

        record = access.reproduce(
            self._catalog(),
            str(payload["immutable_evidence_id"]),
            experiment_id=str(payload["experiment_id"]),
            attempt_id=str(payload["attempt_id"]),
            actor=self.agent,
            run_id=str(payload["run_id"]),
            created_at=datetime.fromisoformat(payload["created_at"]),
        )
        return self.arena_append(
            {
                "idempotency_key": str(payload["idempotency_key"]),
                "record": record.model_dump(mode="json"),
            }
        ) | {"experiment_hash": record.content_hash}

    def experiment_mutate(self, payload: dict) -> dict:
        _keys(
            payload,
            {
                "parent_id",
                "experiment_id",
                "attempt_id",
                "run_id",
                "created_at",
                "changed_dimensions",
                "configuration",
                "idempotency_key",
            },
        )
        if not isinstance(payload["changed_dimensions"], list):
            raise BackendInputError("changed_dimensions must be an array")
        if not isinstance(payload["configuration"], dict):
            raise BackendInputError("configuration must be an object")
        access = self._require_collaboration_access()
        from datetime import datetime

        record = access.mutate(
            self._catalog(),
            str(payload["parent_id"]),
            experiment_id=str(payload["experiment_id"]),
            attempt_id=str(payload["attempt_id"]),
            actor=self.agent,
            run_id=str(payload["run_id"]),
            changed_dimensions=payload["changed_dimensions"],
            configuration=payload["configuration"],
            created_at=datetime.fromisoformat(payload["created_at"]),
        )
        return self.arena_append(
            {
                "idempotency_key": str(payload["idempotency_key"]),
                "record": record.model_dump(mode="json"),
            }
        ) | {"experiment_hash": record.content_hash}

    def _require_collaboration_access(self):
        if self.collaboration_access is None:
            raise BackendInputError(
                "collaboration mutation requires controller-injected lease authority"
            )
        return self.collaboration_access


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print("usage: skharness-sk-backend OPERATION", file=sys.stderr)
        return 2
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise BackendInputError("payload must be an object")
        home = Path(os.environ["SKCAPSTONE_HOME"])
        event_dir = Path(os.environ["SKHARNESS_ARENA_EVENT_DIR"])
        agent = os.environ["SKAGENT"]
        result = LocalSKBackend(capstone_home=home, event_dir=event_dir, agent=agent).invoke(
            args[0], payload
        )
    except (
        AccessDeniedError,
        BackendInputError,
        CollaborationError,
        KeyError,
        ValueError,
    ) as exc:
        print(f"skharness-sk-backend: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, default=str, sort_keys=True, separators=(",", ":")))
    return 0
