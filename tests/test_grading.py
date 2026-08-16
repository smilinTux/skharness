"""Joule Economy grading: rubric, parser, and model_class rule (P1, shadow only).

Pure and model-free: nothing here calls a model, wires phase0_assess, or changes
behaviour. See docs/superpowers/specs/2026-08-14-joule-economy-design.md sections
3.1 to 3.6 (skcapstone repo).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skharness.autocode.grading import (
    GRADE_RUBRIC,
    VOCABULARY,
    model_class_for,
    parse_grade,
)

GOLDEN_SET_PATH = Path(__file__).parent / "data" / "joule-economy-golden-set-v1.json"


def _load_golden_set() -> list[dict]:
    with GOLDEN_SET_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)["cards"]


# ---------------------------------------------------------------------------
# GRADE_RUBRIC
# ---------------------------------------------------------------------------


def test_rubric_is_built_from_vocabulary_not_retyped():
    for value, definition in VOCABULARY["size"]["definitions"].items():
        assert value in GRADE_RUBRIC
        assert definition in GRADE_RUBRIC
    for value, definition in VOCABULARY["risk"]["definitions"].items():
        assert value in GRADE_RUBRIC
        assert definition in GRADE_RUBRIC
    for value, definition in VOCABULARY["sensitivity"]["definitions"].items():
        assert value in GRADE_RUBRIC
        assert definition in GRADE_RUBRIC


def test_rubric_has_no_em_or_en_dashes():
    assert "—" not in GRADE_RUBRIC
    assert "–" not in GRADE_RUBRIC


# ---------------------------------------------------------------------------
# model_class_for: worked examples from the vocabulary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("example", VOCABULARY["model_class"]["worked_examples"])
def test_worked_examples_resolve_to_the_right_class(example):
    got = model_class_for(example["size"], example["risk"])
    assert got == example["model_class"], example["note"]


def test_model_class_for_rejects_risk_reusing_a_size_label():
    with pytest.raises(ValueError):
        model_class_for("M", "XL")


def test_model_class_for_rejects_unknown_size():
    with pytest.raises(ValueError):
        model_class_for("huge", "low")


def test_model_class_for_rejects_unknown_risk():
    with pytest.raises(ValueError):
        model_class_for("M", "extreme")


def test_model_class_for_is_case_insensitive():
    assert model_class_for("m", "CRIT") == "XL"
    assert model_class_for("s", "Low") == "S"


# ---------------------------------------------------------------------------
# parse_grade
# ---------------------------------------------------------------------------


def test_parse_grade_clean_json():
    raw = '{"size": "M", "risk": "high", "sensitivity": "internal"}'
    assert parse_grade(raw) == {"size": "M", "risk": "high", "sensitivity": "internal"}


def test_parse_grade_fenced_json():
    raw = '```json\n{"size": "L", "risk": "med", "sensitivity": "public"}\n```'
    assert parse_grade(raw) == {"size": "L", "risk": "med", "sensitivity": "public"}


def test_parse_grade_fenced_without_language_tag():
    raw = '```\n{"size": "S", "risk": "low", "sensitivity": "public"}\n```'
    assert parse_grade(raw) == {"size": "S", "risk": "low", "sensitivity": "public"}


def test_parse_grade_with_surrounding_prose():
    raw = (
        "Looking at this card, here is my grade:\n"
        '{"size": "XL", "risk": "crit", "sensitivity": "secret"}\n'
        "Let me know if you want a second opinion."
    )
    assert parse_grade(raw) == {"size": "XL", "risk": "crit", "sensitivity": "secret"}


def test_parse_grade_case_insensitive():
    raw = '{"size": "m", "risk": "HIGH", "sensitivity": "Internal"}'
    assert parse_grade(raw) == {"size": "M", "risk": "high", "sensitivity": "internal"}


def test_parse_grade_rejects_risk_using_a_size_label():
    raw = '{"size": "M", "risk": "XL", "sensitivity": "internal"}'
    assert parse_grade(raw) is None


def test_parse_grade_returns_none_for_junk():
    assert parse_grade("I refuse to answer in JSON today.") is None
    assert parse_grade("") is None
    assert parse_grade(None) is None
    assert parse_grade("{not even valid json") is None


def test_parse_grade_returns_none_when_a_field_is_missing():
    raw = '{"size": "M", "risk": "high"}'
    assert parse_grade(raw) is None


def test_parse_grade_returns_none_when_a_value_is_invalid():
    raw = '{"size": "huge", "risk": "high", "sensitivity": "internal"}'
    assert parse_grade(raw) is None


def test_parse_grade_unwraps_a_nested_grade_key():
    raw = '{"reasoning": "...", "grade": {"size": "M", "risk": "low", "sensitivity": "public"}}'
    assert parse_grade(raw) == {"size": "M", "risk": "low", "sensitivity": "public"}


# ---------------------------------------------------------------------------
# Golden set scoring harness: proves the rule agrees with hand grading before
# any model is involved.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "card", _load_golden_set(), ids=lambda c: f"{c['n']:02d}-{c['id']}"
)
def test_golden_set_class_matches_hand_grading(card):
    raw = json.dumps(
        {"size": card["size"], "risk": card["risk"].lower(), "sensitivity": "internal"}
    )
    grade = parse_grade(raw)
    assert grade is not None, f"card {card['n']} ({card['id']}) did not parse: {raw!r}"
    got = model_class_for(grade["size"], grade["risk"])
    assert got == card["class"], f"card {card['n']} ({card['id']}): {card['why']}"


def test_golden_set_has_42_cards():
    assert len(_load_golden_set()) == 42
