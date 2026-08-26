import hashlib
import json
from pathlib import Path

import pytest

from skharness.autocode.golden_set import (
    DOCUMENT_SCHEMA_V2,
    ENTRY_SCHEMA_V2,
    GoldenSetSchemaError,
    create_candidate,
    gating_failures,
    load_v2_document,
    ratify_entry,
    validate_entry,
)

DATA = Path(__file__).parent / "data"


def _source_text() -> str:
    return json.dumps(
        {"id": "card-1", "title": "Exact source", "acceptance_criteria": ["works"]},
        sort_keys=True,
    )


def _candidate(*, model_class: str = "XL") -> dict:
    return create_candidate(
        source_text=_source_text(),
        size="S",
        risk="low",
        model_class=model_class,
        why="A deliberately reviewable proposal.",
    )


def _document(*entries: dict) -> dict:
    return {"schema": DOCUMENT_SCHEMA_V2, "entries": list(entries)}


def test_v2_fixture_starts_empty():
    assert load_v2_document(DATA / "joule-economy-golden-set-v2.json") == _document()


def test_candidate_embeds_and_hashes_source_at_creation():
    candidate = _candidate()
    assert candidate["schema"] == ENTRY_SCHEMA_V2
    assert candidate["source_text"] == _source_text()
    assert candidate["source_sha256"] == hashlib.sha256(_source_text().encode()).hexdigest()


@pytest.mark.parametrize("source_text", [None, "", "   "])
def test_missing_embedded_source_is_rejected_at_candidate_creation(source_text):
    with pytest.raises(GoldenSetSchemaError, match="source_text"):
        create_candidate(
            source_text=source_text,
            size="S",
            risk="low",
            model_class="S",
            why="No source means no candidate.",
        )


def test_candidate_cannot_turn_check_red_but_ratified_entry_can():
    candidate = _candidate(model_class="XL")
    assert gating_failures(_document(candidate)) == ()

    ratified = ratify_entry(candidate, human_signoff="APPROVED exact candidate")
    assert ratified["source_text"] == candidate["source_text"]
    assert ratified["source_sha256"] == candidate["source_sha256"]
    assert gating_failures(_document(ratified)) == ("entry 0: model_class 'XL', expected 'S'",)


def test_ratification_requires_human_signoff():
    with pytest.raises(GoldenSetSchemaError, match="human_signoff"):
        ratify_entry(_candidate(model_class="S"), human_signoff="")


def test_tampered_source_hash_is_rejected():
    candidate = _candidate(model_class="S")
    candidate["source_text"] += " changed"
    with pytest.raises(GoldenSetSchemaError, match="source_sha256"):
        validate_entry(candidate)


def test_v1_entry_cannot_be_promoted_into_v2():
    v1 = json.loads((DATA / "joule-economy-golden-set-v1.json").read_text())
    with pytest.raises(GoldenSetSchemaError, match="entry schema"):
        ratify_entry(v1["cards"][0], human_signoff="APPROVED")
