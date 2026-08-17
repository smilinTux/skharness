import pytest

from skharness.autocode.config import (DEFAULT_HARNESS, KNOWN_HARNESSES, Config,
                                       ConfigError, fits_pi, requires_pi)
from skharness.autocode.types import RepoSpec


def test_harness_fields_load_from_yaml(tmp_path):
    p = tmp_path / "autopilot.yaml"
    p.write_text(
        "enabled: true\n"
        "harness: pi\n"
        "harness_model: sk-default\n"
        "harness_base_url: http://localhost:18780/v1\n"
        "live_execution: true\n"
        "mcp_endpoints: [localhost, api.anthropic.com]\n"
        "sandbox_image: sandbox-pi:2\n")
    c = Config.load(p)
    assert c.harness == "pi"
    assert c.harness_model == "sk-default"
    assert c.harness_base_url == "http://localhost:18780/v1"
    assert c.live_execution is True
    assert c.mcp_endpoints == ["localhost", "api.anthropic.com"]
    assert c.sandbox_image == "sandbox-pi:2"


def test_harness_fields_default_safely():
    c = Config()
    assert c.live_execution is False           # posture stays off by default
    assert c.harness_model is None and c.harness_base_url is None
    assert c.mcp_endpoints == [] and c.sandbox_image is None


# --- the default is pi (card 1db15e43 / A4.2) ---

def test_default_harness_is_pi():
    """pi is the only adapter with supports_model_override(); a claude-code default
    RAISES ModelOverrideUnsupported on every graded card rather than building it."""
    assert DEFAULT_HARNESS == "pi"
    assert Config().harness == "pi"


def test_yaml_without_a_harness_key_gets_pi(tmp_path):
    p = tmp_path / "autopilot.yaml"
    p.write_text("enabled: true\n")
    assert Config.load(p).harness == "pi"


def test_explicit_claude_code_is_still_honoured(tmp_path):
    """The flip changes the DEFAULT, never an operator's explicit choice."""
    p = tmp_path / "autopilot.yaml"
    p.write_text("enabled: true\nharness: claude-code\n")
    assert Config.load(p).harness == "claude-code"


# --- an unknown harness name fails closed ---

def test_unknown_harness_in_yaml_raises_and_names_the_file(tmp_path):
    p = tmp_path / "autopilot.yaml"
    p.write_text("enabled: true\nharness: pie\n")           # typo for pi
    with pytest.raises(ConfigError) as e:
        Config.load(p)
    msg = str(e.value)
    assert "pie" in msg and str(p) in msg and "pi" in msg   # says which, where, and what is valid


def test_a_constructed_config_is_deliberately_not_validated():
    """The gate is `load`, not `__init__`, and that is a decision worth pinning.

    Validating in `__post_init__` would be the stronger invariant, but it makes
    `Config(harness="totally-bogus")` impossible, and that construction is how
    tests/test_agentrun_bridge.py proves the execute bridge fails closed on an
    unresolvable harness. Dropping a negative control to add a second guard over the
    same failure is a bad trade. `harness.build_harness` still raises on any unknown
    name that reaches it, so nothing silently falls back on this path either.
    """
    assert Config(harness="totally-bogus").harness == "totally-bogus"
    import skharness.autocode.adapters   # noqa: F401
    from skharness.autocode.harness import build_harness
    with pytest.raises(ValueError):
        build_harness(Config(harness="totally-bogus"))


def test_known_harnesses_matches_the_live_registry():
    """The literal set here must BE the adapter registry's keys.

    config.py deliberately does not import the registry (it sits below the adapters in
    the import graph), so this test is the only thing keeping the two from drifting: a
    new adapter that is registered but not listed here would be rejected at load with
    a message claiming it does not exist.
    """
    import skharness.autocode.adapters   # noqa: F401  registers the concrete adapters
    from skharness.autocode.harness import HARNESSES
    assert KNOWN_HARNESSES == frozenset(HARNESSES)


# --- "work that fits pi", the written definition, in code ---

def _cfg(**kw):
    base = dict(harness="pi", repo_map={
        "skos": RepoSpec("skos", "/tmp/skos", "main", "main", "pytest -q", "none"),
        "skworld-app": RepoSpec("skworld-app", "/tmp/app", "main", "main", "flutter test",
                                "none", sandbox_image="sandbox-claude-flutter:1"),
    })
    base.update(kw)
    return Config(**base)


def test_mapped_repo_with_no_image_pin_fits_pi():
    assert fits_pi("skos", _cfg()) is None


def test_unmapped_repo_does_not_fit_and_says_what_is_mapped():
    reason = fits_pi("skgateway", _cfg())
    assert reason and "skgateway" in reason and "skos" in reason


def test_missing_repo_tag_does_not_fit():
    assert "repo:" in (fits_pi(None, _cfg()) or "")


def test_repo_pinned_to_a_claude_image_does_not_fit_pi():
    """sandbox-claude-flutter:1 is built FROM sandbox-claude:1 and carries no `pi`
    binary (measured 2026-08-16), and a repo_map pin BEATS the adapter's own image."""
    reason = fits_pi("skworld-app", _cfg())
    assert reason and "sandbox-claude-flutter:1" in reason


def test_config_level_claude_image_does_not_fit_pi():
    reason = fits_pi("skos", _cfg(sandbox_image="sandbox-claude:1"))
    assert reason and "sandbox-claude:1" in reason


def test_pi_image_pins_still_fit():
    """Negative control: a rule that rejected every image pin would pass the two above."""
    cfg = _cfg(sandbox_image="sandbox-pi:1", repo_map={
        "skos": RepoSpec("skos", "/tmp/skos", "main", "main", "pytest -q", "none",
                         sandbox_image="sandbox-pi:2")})
    assert fits_pi("skos", cfg) is None


def test_session_plane_work_does_not_fit_pi():
    reason = fits_pi("skos", _cfg(), needs_session_plane=True)
    assert reason and "session" in reason


def test_graded_work_requires_pi_and_ungraded_does_not():
    assert requires_pi(graded=True) is True
    assert requires_pi(graded=False) is False


def test_repospec_sandbox_image_optional():
    from skharness.autocode.types import RepoSpec
    r = RepoSpec("n", "/p", "main", "ap", "true", "none")
    assert r.sandbox_image is None
    r2 = RepoSpec("n", "/p", "main", "ap", "true", "none", sandbox_image="img:1")
    assert r2.sandbox_image == "img:1"
