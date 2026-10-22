"""Promotion-only Joule Economy golden-set v2 boundary."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .grading import CLASS, RISK_VALUES, SIZE_VALUES, model_class_for

DOCUMENT_SCHEMA_V2 = "skharness.joule-economy-golden-set.v2"
ENTRY_SCHEMA_V2 = "skharness.joule-economy-golden-entry.v2"
CANDIDATE = "candidate"
RATIFIED = "ratified"

_BASE_KEYS = {
    "schema",
    "state",
    "source_text",
    "source_sha256",
    "size",
    "risk",
    "model_class",
    "why",
}


class GoldenSetSchemaError(ValueError):
    """A v2 document or entry is incomplete, tampered, or from another schema."""


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GoldenSetSchemaError(f"{field} must be non-empty text")
    return value


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_entry(entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise GoldenSetSchemaError("entry must be an object")
    if entry.get("schema") != ENTRY_SCHEMA_V2:
        raise GoldenSetSchemaError(f"entry schema must be {ENTRY_SCHEMA_V2!r}")

    state = entry.get("state")
    if state not in {CANDIDATE, RATIFIED}:
        raise GoldenSetSchemaError("entry state must be candidate or ratified")
    expected_keys = _BASE_KEYS | (
        {"human_signoff", "human_signoff_sha256"} if state == RATIFIED else set()
    )
    if set(entry) != expected_keys:
        raise GoldenSetSchemaError(f"{state} entry keys must be exactly {sorted(expected_keys)!r}")

    source_text = _required_text(entry.get("source_text"), "source_text")
    if entry.get("source_sha256") != _digest(source_text):
        raise GoldenSetSchemaError("source_sha256 does not match source_text")
    if entry.get("size") not in SIZE_VALUES:
        raise GoldenSetSchemaError("size is not canonical")
    if entry.get("risk") not in RISK_VALUES:
        raise GoldenSetSchemaError("risk is not canonical")
    if entry.get("model_class") not in CLASS:
        raise GoldenSetSchemaError("model_class is not canonical")
    _required_text(entry.get("why"), "why")

    if state == RATIFIED:
        signoff = _required_text(entry.get("human_signoff"), "human_signoff")
        if entry.get("human_signoff_sha256") != _digest(signoff):
            raise GoldenSetSchemaError("human_signoff_sha256 does not match human_signoff")
    return entry


def validate_document(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict) or set(document) != {"schema", "entries"}:
        raise GoldenSetSchemaError("v2 document keys must be exactly schema and entries")
    if document.get("schema") != DOCUMENT_SCHEMA_V2:
        raise GoldenSetSchemaError(f"document schema must be {DOCUMENT_SCHEMA_V2!r}")
    entries = document.get("entries")
    if not isinstance(entries, list):
        raise GoldenSetSchemaError("entries must be a list")
    for entry in entries:
        validate_entry(entry)
    return document


def load_v2_document(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return validate_document(json.load(handle))


def create_candidate(
    *,
    source_text: str | None,
    size: str,
    risk: str,
    model_class: str,
    why: str,
) -> dict[str, Any]:
    """Create a valid proposal with source bytes embedded and hashed immediately."""
    source = _required_text(source_text, "source_text")
    entry = {
        "schema": ENTRY_SCHEMA_V2,
        "state": CANDIDATE,
        "source_text": source,
        "source_sha256": _digest(source),
        "size": size,
        "risk": risk,
        "model_class": model_class,
        "why": why,
    }
    return validate_entry(entry)


def ratify_entry(entry: Any, *, human_signoff: str) -> dict[str, Any]:
    """Promote only a complete v2 candidate and bind the exact human signoff."""
    validated = validate_entry(entry)
    if validated["state"] != CANDIDATE:
        raise GoldenSetSchemaError("only a candidate may be ratified")
    signoff = _required_text(human_signoff, "human_signoff")
    ratified = {
        **validated,
        "state": RATIFIED,
        "human_signoff": signoff,
        "human_signoff_sha256": _digest(signoff),
    }
    return validate_entry(ratified)


def gating_failures(document: Any) -> tuple[str, ...]:
    """Return class-derivation failures from ratified entries only."""
    entries = validate_document(document)["entries"]
    failures = []
    for index, entry in enumerate(entries):
        if entry["state"] != RATIFIED:
            continue
        expected = model_class_for(entry["size"], entry["risk"])
        if entry["model_class"] != expected:
            failures.append(
                f"entry {index}: model_class {entry['model_class']!r}, expected {expected!r}"
            )
    return tuple(failures)
