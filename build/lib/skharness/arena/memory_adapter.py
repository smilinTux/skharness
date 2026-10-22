"""Narrow runtime SKMemory adapter for authorized promotion and rollback."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .collaboration import (
    CollaborationError,
    RefinementJournal,
    RefinementState,
)
from .models import FrozenModel, Provenance

RuntimeBackend = Callable[[str, dict[str, Any]], dict[str, Any]]


class ExecutableRuntimeBackend:
    """Invoke a controller-mounted SKMemory PEP executable without a shell."""

    operations = frozenset({"memory.read", "memory.compare_and_set", "memory.restore"})

    def __init__(self, executable: str | Path, *, timeout_s: float = 30) -> None:
        self.executable = Path(executable)
        self.timeout_s = timeout_s
        if not self.executable.is_absolute():
            raise ValueError("runtime SKMemory backend path must be absolute")

    def __call__(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        if operation not in self.operations:
            raise CollaborationError("unsupported runtime SKMemory operation")
        process = subprocess.run(
            [os.fspath(self.executable), operation],
            input=json.dumps(payload, sort_keys=True, separators=(",", ":")),
            text=True,
            capture_output=True,
            timeout=self.timeout_s,
            check=False,
        )
        if process.returncode:
            raise CollaborationError(
                f"runtime SKMemory backend failed with exit {process.returncode}: "
                f"{process.stderr[:500]}"
            )
        try:
            response = json.loads(process.stdout)
        except json.JSONDecodeError as exc:
            raise CollaborationError("runtime SKMemory backend returned invalid JSON") from exc
        if not isinstance(response, dict):
            raise CollaborationError("runtime SKMemory backend returned a non-object")
        return response


class MemoryReceipt(FrozenModel):
    schema_version: str = "arena.memory-receipt.v1"
    proposal_id: str
    operation: str
    target: str
    idempotency_key: str
    backend_receipt: str
    prior_content_hash: str
    resulting_content_hash: str


class RuntimeSKMemoryAdapter:
    """Apply only journal-authorized compare-and-set operations.

    The mounted runtime backend implements three fixed operations: ``memory.read``,
    ``memory.compare_and_set`` and ``memory.restore``. It receives no arbitrary MCP
    method or shell input. Backend idempotency plus journal state makes crash retries
    safe: an already recorded operation returns its immutable receipt.
    """

    def __init__(self, journal: RefinementJournal, backend: RuntimeBackend) -> None:
        self.journal = journal
        self.backend = backend

    def promote(self, proposal_id: str, provenance: Provenance) -> MemoryReceipt:
        existing = self._recorded(proposal_id, RefinementState.PROMOTED)
        if existing is not None:
            return existing
        if self.journal.state(proposal_id) is not RefinementState.PROMOTION_AUTHORIZED:
            raise CollaborationError("promotion has not been explicitly authorized")
        proposal = self.journal.proposals()[proposal_id]
        prior = self._read(proposal.target)
        key = f"arena:{proposal.id}:promote"
        response = self.backend(
            "memory.compare_and_set",
            {
                "target": proposal.target,
                "expected_content_hash": prior,
                "content": proposal.proposed_content,
                "idempotency_key": key,
            },
        )
        receipt = self._receipt(
            proposal_id=proposal.id,
            operation="promote",
            target=proposal.target,
            idempotency_key=key,
            prior=prior,
            response=response,
        )
        authorization = self._last(proposal_id, RefinementState.PROMOTION_AUTHORIZED)
        try:
            self.journal.record_promoted(
                proposal_id,
                provenance,
                evidence_ids=authorization.evidence_ids,
                receipt=receipt.model_dump_json(),
            )
            return receipt
        except CollaborationError:
            recorded = self._recorded(proposal_id, RefinementState.PROMOTED)
            if recorded == receipt:
                return recorded
            raise

    def rollback(self, proposal_id: str, provenance: Provenance) -> MemoryReceipt:
        existing = self._recorded(proposal_id, RefinementState.ROLLED_BACK)
        if existing is not None:
            return existing
        if self.journal.state(proposal_id) is not RefinementState.ROLLBACK_AUTHORIZED:
            raise CollaborationError("rollback has not been explicitly authorized")
        proposal = self.journal.proposals()[proposal_id]
        promoted = self._recorded(proposal_id, RefinementState.PROMOTED)
        if promoted is None:
            raise CollaborationError("promotion receipt is missing")
        key = f"arena:{proposal.id}:rollback"
        response = self.backend(
            "memory.restore",
            {
                "target": proposal.target,
                "expected_content_hash": promoted.resulting_content_hash,
                "restore_content_hash": promoted.prior_content_hash,
                "idempotency_key": key,
            },
        )
        receipt = self._receipt(
            proposal_id=proposal.id,
            operation="rollback",
            target=proposal.target,
            idempotency_key=key,
            prior=promoted.resulting_content_hash,
            response=response,
        )
        if receipt.resulting_content_hash != promoted.prior_content_hash:
            raise CollaborationError("SKMemory did not restore the prior content hash")
        authorization = self._last(proposal_id, RefinementState.ROLLBACK_AUTHORIZED)
        try:
            self.journal.record_rolled_back(
                proposal_id,
                provenance,
                evidence_ids=authorization.evidence_ids,
                receipt=receipt.model_dump_json(),
            )
            return receipt
        except CollaborationError:
            recorded = self._recorded(proposal_id, RefinementState.ROLLED_BACK)
            if recorded == receipt:
                return recorded
            raise

    def _read(self, target: str) -> str:
        response = self.backend("memory.read", {"target": target})
        content_hash = response.get("content_hash")
        if not isinstance(content_hash, str) or not content_hash.strip():
            raise CollaborationError("SKMemory read omitted content_hash")
        return content_hash

    @staticmethod
    def _receipt(
        *,
        proposal_id: str,
        operation: str,
        target: str,
        idempotency_key: str,
        prior: str,
        response: dict[str, Any],
    ) -> MemoryReceipt:
        backend_receipt = response.get("receipt")
        resulting_hash = response.get("content_hash")
        if not isinstance(backend_receipt, str) or not backend_receipt.strip():
            raise CollaborationError("SKMemory mutation omitted receipt")
        if not isinstance(resulting_hash, str) or not resulting_hash.strip():
            raise CollaborationError("SKMemory mutation omitted content_hash")
        recorded_prior = response.get("prior_content_hash", prior)
        if not isinstance(recorded_prior, str) or not recorded_prior.strip():
            raise CollaborationError("SKMemory mutation omitted prior content hash")
        return MemoryReceipt(
            proposal_id=proposal_id,
            operation=operation,
            target=target,
            idempotency_key=idempotency_key,
            backend_receipt=backend_receipt,
            prior_content_hash=recorded_prior,
            resulting_content_hash=resulting_hash,
        )

    def _last(self, proposal_id: str, state: RefinementState):
        matches = [
            event
            for event in self.journal.events()
            if event.proposal_id == proposal_id and event.to_state is state
        ]
        if not matches:
            raise CollaborationError(f"missing refinement event: {state.value}")
        return matches[-1]

    def _recorded(self, proposal_id: str, state: RefinementState) -> MemoryReceipt | None:
        matches = [
            event
            for event in self.journal.events()
            if event.proposal_id == proposal_id and event.to_state is state
        ]
        if not matches:
            return None
        try:
            return MemoryReceipt.model_validate_json(matches[-1].receipt or "")
        except Exception as exc:
            raise CollaborationError("recorded SKMemory receipt is invalid") from exc
