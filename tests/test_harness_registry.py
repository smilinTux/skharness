import pytest
from types import SimpleNamespace
from skharness.autocode.harness import build_harness, HARNESSES
import skharness.autocode.adapters   # noqa: F401  triggers adapter registration


def _cfg(**kw):
    base = dict(harness="claude-code", allowed_tools=["Read"], mcp_endpoints=[],
                live_execution=False, harness_model=None, sandbox_image=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_registry_has_all_harnesses():
    for n in ("stub", "claude-code", "pi", "opencode", "codex"):
        assert n in HARNESSES


def test_build_by_name_and_default():
    assert build_harness(_cfg(harness="stub")).name == "stub"
    assert build_harness(_cfg(harness="pi")).name == "pi"
    assert build_harness(_cfg(harness="opencode")).name == "opencode"
    assert build_harness(_cfg(harness="codex")).name == "codex"
    assert build_harness(_cfg()).name == "claude-code"        # default from config.harness


def test_pi_factory_passes_configured_session_and_card_identity():
    harness = build_harness(_cfg(
        harness="pi", harness_session_id="session-abc123", harness_card_id="a49d1c36"))
    assert harness.session_id == "session-abc123"
    assert harness.card_id == "a49d1c36"


def test_pi_factory_does_not_invent_identity_when_config_has_none():
    harness = build_harness(_cfg(
        harness="pi", harness_session_id=None, harness_card_id=None))
    assert harness.session_id is None
    assert harness.card_id is None


def test_unknown_harness_fails_closed():
    with pytest.raises(ValueError):
        build_harness(_cfg(harness="nope"))
