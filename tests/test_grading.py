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
# Vocabulary drift guard: literal assertions against the vendored copy, so a
# widened or renamed enum fails loudly here instead of silently disagreeing
# with the canonical skcapstone file it was copied from (mirrors skgateway's
# tests/grade-vocabulary.test.mjs).
# ---------------------------------------------------------------------------


def test_size_values_are_exactly_s_m_l_xl():
    assert list(VOCABULARY["size"]["values"]) == ["S", "M", "L", "XL"]


def test_risk_values_are_exactly_low_med_high_crit():
    assert list(VOCABULARY["risk"]["values"]) == ["low", "med", "high", "crit"]


def test_sensitivity_values_are_exactly_public_internal_secret():
    assert list(VOCABULARY["sensitivity"]["values"]) == ["public", "internal", "secret"]


def test_no_risk_value_collides_with_a_size_label_in_any_case_form():
    size_labels = {v.upper() for v in VOCABULARY["size"]["values"]}
    for risk_value in VOCABULARY["risk"]["values"]:
        assert risk_value.upper() not in size_labels, risk_value


def test_size_and_risk_ranks_both_span_0_to_3():
    # This is what makes max(size_rank, risk_rank) a valid index into CLASS.
    assert sorted(VOCABULARY["size"]["ranks"].values()) == [0, 1, 2, 3]
    assert sorted(VOCABULARY["risk"]["ranks"].values()) == [0, 1, 2, 3]


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


@pytest.mark.parametrize(
    "raw",
    [
        12345,
        3.14,
        {"size": "M"},
        {"size": "M", "risk": "high", "sensitivity": "internal"},
        ["size", "M"],
        b'{"size":"M","risk":"high","sensitivity":"internal"}',
        True,
    ],
    ids=["int", "float", "dict-partial", "dict-full", "list", "bytes", "bool"],
)
def test_parse_grade_is_total_for_non_string_input(raw):
    """A provider handing back a pre-parsed body must degrade one card to
    ungraded, never raise and take down the whole assess pass (finding 1)."""
    assert parse_grade(raw) is None


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
