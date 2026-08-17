import glob
import textwrap

import pytest

from skharness.autocode.config import Config, ConfigError, config_path

SAMPLE = """
enabled: true
harness: claude-code
allowed_tools: [Read, Edit, Write, Bash, mcp__skcapstone__coord_score]
automerge_repos: [skos]
digest_chat: "chef-dm"
epic_id: "1b4ab47a"
caps:
  max_concurrent: 2
  new_tasks_per_run: 5
repo_map:
  skos:
    path: /home/cbrd21/clawd/skos
    base_branch: main
    integration_branch: autopilot/integration
    test_cmd: "pytest -q"
    ci: github-actions
    coverage_cmd: "pytest --cov --cov-report=xml"
    automerge: true
  skcapstone:
    path: /home/cbrd21/clawd/skcapstone-repos/skcapstone
    base_branch: main
    integration_branch: autopilot/integration
    test_cmd: "pytest -q"
    ci: none
"""


def test_missing_file_returns_disabled_default(tmp_path, monkeypatch):
    monkeypatch.delenv("SKOS_AUTOPILOT_CONFIG", raising=False)
    cfg = Config.load(tmp_path / "nope.yaml")
    assert cfg.enabled is False
    assert cfg.harness == "claude-code"
    assert cfg.repo_map == {}
    assert cfg.caps.max_concurrent == 3
    assert cfg.repo("anything") is None


def test_parse_and_repo_resolution(tmp_path, monkeypatch):
    p = tmp_path / "autopilot.yaml"
    p.write_text(SAMPLE)
    monkeypatch.setenv("SKOS_AUTOPILOT_CONFIG", str(p))
    assert config_path() == p
    cfg = Config.load()
    assert cfg.enabled is True and cfg.automerge_repos == ["skos"]
    assert cfg.digest_chat == "chef-dm" and cfg.epic_id == "1b4ab47a"
    assert cfg.caps.max_concurrent == 2 and cfg.caps.new_tasks_per_run == 5
    assert cfg.caps.max_usd_per_day == 25.0          # untouched default preserved
    skos = cfg.repo("skos")
    assert skos is not None and skos.name == "skos"  # name injected from the key
    assert skos.ci == "github-actions" and skos.automerge is True
    assert skos.min_diff_coverage == 0.8             # RepoSpec default, not in yaml
    assert cfg.repo("skcapstone").ci == "none"
    assert cfg.repo("unknown") is None


# --- the lint: a key this loader does not understand must RAISE, not be dropped ---
#
# The measured failure (autopilot-pi.yaml, 2026-08-16) is the first test below:
# `new_tasks_per_run` written at the top level instead of under `caps:`. Before the
# lint, the key was dropped and the DEFAULT cap of 10 applied, so an operator who
# wrote 1 got 10 with no output distinguishing the two.

def _write(tmp_path, body: str):
    p = tmp_path / "autopilot.yaml"
    p.write_text(textwrap.dedent(body))
    return p


def test_caps_key_written_at_top_level_raises(tmp_path):
    p = _write(tmp_path, """
        enabled: true
        new_tasks_per_run: 1
        caps:
          max_concurrent: 1
        """)
    with pytest.raises(ConfigError) as e:
        Config.load(p)
    msg = str(e.value)
    assert "new_tasks_per_run" in msg
    assert "caps" in msg                       # tells the operator where to move it
    assert str(p) in msg                       # and which file


def test_unknown_top_level_key_raises_and_does_not_silently_default(tmp_path):
    p = _write(tmp_path, """
        enabled: true
        new_tasks_pr_run: 4
        """)
    with pytest.raises(ConfigError, match="new_tasks_pr_run"):
        Config.load(p)


def test_unknown_caps_key_raises(tmp_path):
    p = _write(tmp_path, """
        enabled: true
        caps:
          max_concurent: 2
        """)
    with pytest.raises(ConfigError, match="max_concurent"):
        Config.load(p)


def test_unknown_repo_map_key_raises_and_names_the_repo(tmp_path):
    p = _write(tmp_path, """
        enabled: true
        repo_map:
          skos:
            path: /tmp/skos
            automerg: true
        """)
    with pytest.raises(ConfigError) as e:
        Config.load(p)
    assert "automerg" in str(e.value) and "repo_map.skos" in str(e.value)


def test_every_known_key_still_loads(tmp_path):
    """Negative control: the lint must not reject a config using the full surface.

    Without this, a lint that rejected EVERYTHING would pass the tests above.
    """
    p = _write(tmp_path, """
        enabled: true
        harness: pi
        allowed_tools: [Read]
        automerge_repos: [skos]
        cleanup_after_run: teardown
        digest_chat: chef-dm
        epic_id: abc
        dry_run: false
        dry_run_summary: true
        harness_model: ornith-1.0-35b
        harness_base_url: http://localhost:18780/v1
        harness_max_tokens: 131072
        live_execution: true
        mcp_endpoints: [127.0.0.1]
        sandbox_image: sandbox-pi:1
        default_quality: gated
        fleet_dispatch: false
        caps:
          max_concurrent: 1
          concurrency: 1
          new_tasks_per_run: 1
          max_tokens_per_run: 10
          max_usd_per_day: 1.0
          max_subtasks_per_card: 2
          max_decompose_children_per_run: 3
          max_decompose_depth: 1
          concreteness_floor: 0.5
        repo_map:
          skos:
            path: /tmp/skos
            base_branch: main
            integration_branch: main
            test_cmd: pytest -q
            ci: none
            coverage_cmd: pytest --cov
            ci_poll_timeout: 60
            ci_scope: changed
            advisory_checks: [lint]
            automerge: true
            auto_revert: true
            min_diff_coverage: 0.5
            sandbox_image: img:1
            min_quality: gated
            deploy_cmd: echo ok
        """)
    cfg = Config.load(p)
    assert cfg.caps.new_tasks_per_run == 1 and cfg.repo("skos").deploy_cmd == "echo ok"


def test_live_fleet_autopilot_configs_all_load(monkeypatch):
    """Every autopilot yaml on this box must survive the lint.

    A stray key in a live config is a finding to fix in that config, not a reason
    to weaken the lint. Skips where the fleet configs are not present (CI).
    """
    monkeypatch.delenv("SKOS_AUTOPILOT_CONFIG", raising=False)
    from pathlib import Path
    found = sorted(glob.glob(str(Path.home() / ".skcapstone/config/autopilot*.yaml")))
    if not found:
        pytest.skip("no fleet autopilot configs on this box")
    for f in found:
        Config.load(Path(f))         # ConfigError here = that file has a stray key
