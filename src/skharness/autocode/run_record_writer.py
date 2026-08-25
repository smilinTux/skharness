"""The RunRecord writer at the twin-gate verdict boundary (coord card c672529e).

``twin_gate_passed`` (engineering.py:63-74) is the ONE predicate both coding
lanes call by import: the Ralph loop (``EngineeringExecutor.run``) and the
one-shot ``ratify()``. This module is the ONE writer both lanes call by
import, immediately after that predicate resolves a verdict, so every job
that reaches a verdict leaves behind a schema-valid, content-hashed
``RunRecord`` -- the provenance ``run_record.py`` specified but that, by its
own docstring, nothing wrote.

This module does not edit the frozen schema in ``run_record.py``. Where the
twin-gate boundary genuinely cannot observe a field the schema wants, the
record says so honestly (an ``ABSENT``/``PARTIAL`` evidence state, or
``GateState.ABSENT``) rather than inventing a plausible-looking value. Three
such gaps, found while wiring this writer, are recorded here rather than
patched around silently:

1. ``RunRecord`` has no first-class field for CI status, diff coverage, or
   free-text grader notes at all -- none of the three exist as typed fields
   anywhere on the schema. They are folded into the sha256-hashed evidence
   blob this writer addresses through ``record_sources``, so the fact is not
   silently discarded, but two things follow from that: a reader of the
   typed fields alone cannot recover WHY a verdict landed where it did
   (only THAT it did, via ``score``/``passed``/``gate_state``/``outcome``);
   and, as of this writer, the evidence blob itself is not persisted
   anywhere retrievable -- only its digest is. ``record_sources`` today is
   therefore an ATTESTATION ("this record's claims were computed from one
   real, hashable snapshot of boundary data, not invented after the fact"),
   not yet a dereferenceable pointer a later reader can fetch and re-hash.
   Persisting the evidence blob alongside the record (so the pointer
   resolves) is a natural next step, deliberately left out of this card's
   scope rather than folded in silently.
2. A LIVE record's ``usage_scope``/``cost_scope``/``energy_scope`` are
   pinned to ``ORDERED_GATEWAY_REQUESTS``, which requires genuine per-request
   evidence (``GatewayRequestProvenance``, digest-addressed, one entry per
   gateway call). Neither ``EngineeringExecutor.run`` nor ``ratify()`` holds
   that at the verdict boundary today -- ``joules.BuildUsage`` is a build-wide
   aggregate with no per-request ordered trail. Writing fabricated per-request
   rows to satisfy the aggregate reconciliation would be worse than omitting
   them, so this writer reports usage/cost/energy as ``AggregateState.ABSENT``
   with no ``gateway_requests``, honestly declaring "not observed at this
   boundary" rather than manufacturing evidence. Wiring the real join lives in
   ``attribution.py`` (card c7aea2e0) and is a separate, larger project.
3. When the LLM grader returns no parseable score (``gr.score is None`` --
   the "grade-resilience" / salvage path), ``twin_gate_passed`` still returns
   a definite boolean (always ``False`` in that case). The frozen
   ``GateState`` vocabulary has no state for "the pass/fail boolean is known
   but the score axis is not": ``GateState.OBSERVED`` requires both score and
   passed present, and ``GateState.ABSENT`` forbids claiming either. This
   writer resolves that by using ``GateState.ABSENT`` (score=None,
   passed=None) in that case, which is honest about the schema's fields but
   drops the independently-known pass/fail fact from the typed fields; the
   fact still survives in ``outcome`` and ``notes``.
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone

from . import health, identity
from .journal import handle as _journal_handle
from .run_record import (
    AggregateScope,
    AggregateState,
    AttributionState,
    GateState,
    RecordOrigin,
    RUN_RECORD_SCHEMA_VERSION,
    RunRecord,
    SourcePointer,
    TaskShape,
)

#: Serializes the read-modify-write append into one run's journal file. A
#: RunJournal.save() rewrites the WHOLE file, so two verdicts landing in the
#: same run_id at once (two items of one orchestrator run reaching a gate
#: within milliseconds of each other) would otherwise race exactly like the
#: board file _BOARD_LOCK already guards in engineering.py.
_JOURNAL_LOCK = threading.Lock()


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest_source(namespace: str, evidence: dict) -> str:
    """A real, reproducible sha256-addressed source over data genuinely
    present at the verdict boundary. The digest is of the exact evidence
    dict this record's fields are drawn from below -- never a fabricated
    pointer at a source that does not exist."""
    payload = _canonical_json(evidence)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{namespace}#sha256:{digest}"


def build_run_record(
    *,
    run_id: str,
    card_id: str,
    repository: str,
    round: int,
    adapter: str | None,
    model_requested: str | None,
    grader_model: str | None,
    effort_tier: str,
    work_grade: dict | None,
    score: int | None,
    passed: bool,
    notes: str,
    outcome: str,
    ci_status: str,
    diff_coverage: float | None,
    min_diff_coverage: float,
    pr: str | None,
    retries: int,
    payload: dict | None,
    started_at: datetime,
    finished_at: datetime,
    source_namespace: str,
    recorded_at: datetime | None = None,
) -> RunRecord:
    """Assemble a schema-valid, content-hashable LIVE RunRecord for one twin
    gate verdict. Pure: no I/O, so it is trivially testable and the caller
    decides whether/how to persist the result.

    Takes the raw card ``payload`` rather than a pre-resolved
    ``escalation_reason`` on purpose: ``escalation.py``'s own vocabulary is a
    REPORTING seam (tests/test_autocode_escalation.py enforces this by AST
    scan) that ROUTING modules -- ``engineering.py`` among them -- must never
    reference, so nothing there can route on a human's escalation note. This
    writer is reporting, not routing, so it is the correct place to resolve
    it; ``engineering.py`` and ``ratify.py`` pass the payload through
    untouched rather than importing ``escalation`` themselves.
    """
    from .escalation import reason_from_payload

    ident = identity.resolve_identity()
    recorded_at = recorded_at or datetime.now(timezone.utc)
    escalation_reason = reason_from_payload(payload)

    # honest fallbacks for schema-required scalars this boundary cannot
    # always name (see module docstring gap 3 for the analogous score case)
    adapter = adapter or "unknown-adapter"
    model_requested = model_requested or "unspecified"

    evidence: dict = {
        "run_id": run_id,
        "card_id": card_id,
        "repository": repository,
        "round": round,
        "adapter": adapter,
        "model_requested": model_requested,
        "grader_model": grader_model,
        "effort_tier": effort_tier,
        "score": score,
        "passed": passed,
        "outcome": outcome,
        "ci_status": ci_status,
        "diff_coverage": diff_coverage,
        "min_diff_coverage": min_diff_coverage,
        "notes": notes,
        "agent": ident.agent,
        "session_id": ident.session_id,
        "node": ident.node,
    }
    if work_grade:
        evidence["work_grade"] = work_grade
    record_source = _digest_source(source_namespace, evidence)

    # Gap 3 (module docstring): OBSERVED requires score AND passed present.
    gate_state = GateState.OBSERVED if score is not None else GateState.ABSENT
    record_score = score if gate_state is GateState.OBSERVED else None
    record_passed = passed if gate_state is GateState.OBSERVED else None

    task_shape = None
    if work_grade:
        size = work_grade.get("size")
        risk = work_grade.get("risk")
        sensitivity = work_grade.get("sensitivity")
        model_class = work_grade.get("model_class")
        if any((size, risk, sensitivity, model_class)):
            task_shape = TaskShape(
                size=size, risk=risk, sensitivity=sensitivity, model_class=model_class,
                source=SourcePointer(record_source=record_source, json_pointer="/work_grade"),
            )

    # Gap 1 (module docstring): notes/ci_status/diff_coverage have no typed
    # field anywhere on RunRecord. The evidence dict above is the only place
    # they survive; there is deliberately no attempt below to smuggle them
    # into a field that means something else (e.g. terminal_state is the
    # existing terminal vocabulary, not a notes field).
    return RunRecord(
        schema_version=RUN_RECORD_SCHEMA_VERSION,
        origin=RecordOrigin.LIVE,
        record_sources=(record_source,),
        run_id=run_id,
        card_id=card_id,
        repository=repository,
        round=round,
        agent=ident.agent,
        session_id=ident.session_id,
        node=ident.node,
        agent_source=ident.agent_var,
        session_id_source=ident.session_id_var,
        node_source="socket.gethostname",
        attribution_state=AttributionState.OBSERVED,
        adapter=adapter,
        model_requested=model_requested,
        model_served=None,
        model_served_state=AggregateState.ABSENT,
        effort_tier=effort_tier,
        task_shape=task_shape,
        # Gap 2 (module docstring): no ordered per-request evidence exists at
        # this boundary yet, so usage/cost/energy are honestly ABSENT rather
        # than backed by BuildUsage's build-wide aggregate.
        tokens=None,
        usage_scope=AggregateScope.ORDERED_GATEWAY_REQUESTS,
        usage_state=AggregateState.ABSENT,
        usage_sources=(),
        cost_usd=None,
        cost_scope=AggregateScope.ORDERED_GATEWAY_REQUESTS,
        cost_state=AggregateState.ABSENT,
        cost_sources=(),
        energy_joules=None,
        energy_scope=AggregateScope.ORDERED_GATEWAY_REQUESTS,
        energy_state=AggregateState.ABSENT,
        energy_sources=(),
        score=record_score,
        passed=record_passed,
        gate_state=gate_state,
        outcome=outcome,
        terminal_state=outcome,
        quality_mode="gated",
        grader_model=grader_model,
        retries=retries,
        pr=pr,
        escalation_reason=escalation_reason,
        started_at=started_at,
        finished_at=finished_at,
        recorded_at=recorded_at,
        gateway_requests=(),
    )


def persist_run_record(record: RunRecord) -> None:
    """Append one validated RunRecord under the journal field
    ``RUN_RECORD_JOURNAL_FIELD`` (``items.<card_id>.run_records[]``) of the
    journal named by ``RUN_RECORD_JOURNAL_TEMPLATE``
    (``~/.skcapstone/coordination/autopilot/runs/<run_id>.json``), the exact
    home run_record.py's own docstring names as this schema's future writer
    target. Reuses ``journal.RunHandle`` -- the same object
    ``EngineeringExecutor.run`` already holds as ``self.journal`` for
    ``set_worktree``/``worktree_for``/``archive_attempts`` -- rather than
    building a second journal abstraction.
    """
    with _JOURNAL_LOCK:
        _journal_handle(record.run_id).add_run_record(
            record.card_id, record.model_dump(mode="json"))


def write_verdict_run_record(**kwargs) -> RunRecord | None:
    """Build + persist a RunRecord for one twin-gate verdict, best-effort.

    Mirrors the rest of the package's telemetry philosophy (``_accrue_usage``,
    ``record_outcome_row``): a broken provenance writer must never turn a real
    twin-gate verdict into a crash, so failures are caught and recorded via
    ``health.record`` rather than raised into the build/ratify path. Returns
    the written record, or None on failure (health event already recorded).
    """
    try:
        record = build_run_record(**kwargs)
        persist_run_record(record)
        return record
    except Exception as exc:  # noqa: BLE001 - provenance must never break a verdict
        health.record(
            "run_record_write_error",
            task=kwargs.get("card_id", ""),
            round=kwargs.get("round"),
            error=str(exc)[:200],
        )
        return None


__all__ = [
    "build_run_record",
    "persist_run_record",
    "write_verdict_run_record",
]
