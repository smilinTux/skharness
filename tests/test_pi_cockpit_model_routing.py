"""Unit tests for the pi-cockpit model routing policy (scripts/pi_cockpit).

Loaded via importlib.util like scripts/qualify-arena.py's test, per this
repo's existing convention for testing a standalone (non-packaged) script.
"""
import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "pi_cockpit" / "model_routing.py"
SPEC = importlib.util.spec_from_file_location("pi_cockpit_model_routing", SCRIPT)
ROUTING = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(ROUTING)


def test_small_doc_card_is_cheap():
    card = {"tags": ["repo:skharness", "size-S", "docs"], "priority": "high"}
    model, reason = ROUTING.choose_model(card)
    assert model == ROUTING.CHEAP_MODEL
    assert "cheap tier" in reason
    assert "docs" in reason


def test_small_testing_card_is_cheap():
    card = {"tags": ["repo:skharness", "size-S", "testing"], "priority": "medium"}
    model, reason = ROUTING.choose_model(card)
    assert model == ROUTING.CHEAP_MODEL


def test_small_core_logic_card_is_mid():
    card = {"tags": ["repo:skcoord", "size-S", "itil"], "priority": "medium"}
    model, reason = ROUTING.choose_model(card)
    assert model == ROUTING.MID_MODEL
    assert "mid tier" in reason


def test_security_tag_overrides_size_and_doc_shape():
    # Even a small, doc-tagged card gets bumped to strong when it also
    # carries a high-risk tag: the risk override must win over the doc/test
    # shortcut, not merely coexist with it.
    card = {"tags": ["repo:skcoord", "size-S", "docs", "security"],
            "priority": "medium"}
    model, reason = ROUTING.choose_model(card)
    assert model == ROUTING.STRONG_MODEL
    assert "security" in reason


def test_medium_size_is_strong():
    card = {"tags": ["repo:skharness", "size-M"], "priority": "high"}
    model, _ = ROUTING.choose_model(card)
    assert model == ROUTING.STRONG_MODEL


def test_untagged_size_defaults_strong_not_cheap():
    # An absent size tag must never be read as "small" -- an unknown size
    # widening to the cheap tier would under-resource an untriaged card.
    card = {"tags": ["repo:skharness"], "priority": "medium"}
    model, reason = ROUTING.choose_model(card)
    assert model == ROUTING.STRONG_MODEL
    assert "untagged" in reason


def test_critical_priority_is_strong_even_at_size_s():
    card = {"tags": ["repo:skharness", "size-S"], "priority": "critical"}
    model, reason = ROUTING.choose_model(card)
    assert model == ROUTING.STRONG_MODEL
    assert "priority=critical" in reason


def test_no_tags_no_priority_does_not_raise():
    model, reason = ROUTING.choose_model({})
    assert model == ROUTING.STRONG_MODEL
    assert isinstance(reason, str) and reason
