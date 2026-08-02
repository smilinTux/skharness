"""Tests for skharness.autocode.grounding — host-side repo grounding."""
import subprocess
import types

import pytest

from skharness.autocode import grounding as g


def _git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, check=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "executor.py").write_text("class OpsExecutor:\n    pass\n")
    (tmp_path / "src" / "util.py").write_text("def mask_ip(x):\n    return x\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    return tmp_path


def _brief(title="", acceptance=None, description=""):
    return types.SimpleNamespace(title=title, acceptance=acceptance or [], description=description)


def test_extract_anchors_finds_symbols_and_paths():
    a = g.extract_anchors("Add OpsExecutor to src/executor.py using mask_ip")
    assert "OpsExecutor" in a
    assert any("executor.py" in x for x in a)
    assert "mask_ip" in a


def test_ground_resolves_existing_symbol_high_concreteness(repo):
    br = _brief(title="Wire OpsExecutor", acceptance=["OpsExecutor calls mask_ip"])
    res = g.ground_card(br, str(repo), base_branch="main")
    assert res.grounded is True
    assert "OpsExecutor" in res.resolved and "mask_ip" in res.resolved
    assert res.concreteness == pytest.approx(1.0)
    assert "EXISTS" in res.context


def test_ground_vague_card_low_concreteness_not_net_new(repo):
    # references a symbol that does NOT exist, and is not create-shaped
    br = _brief(title="Improve the WidgetManager reliability",
                acceptance=["WidgetManager should be more robust"])
    res = g.ground_card(br, str(repo), base_branch="main")
    assert res.grounded is True
    assert res.resolved == []
    assert res.concreteness == pytest.approx(0.0)
    assert res.net_new is False        # not create-shaped -> vague, should decompose


def test_ground_greenfield_is_net_new(repo):
    br = _brief(title="Create a new BillingLedger module",
                acceptance=["add BillingLedger with post() and balance()"])
    res = g.ground_card(br, str(repo), base_branch="main")
    assert res.resolved == []
    assert res.net_new is True         # create-shaped + nothing resolves -> concrete-by-intent


def test_ground_no_repo_is_ungrounded():
    res = g.ground_card(_brief(title="itil cadence review"), None)
    assert res.grounded is False
    assert res.concreteness is None


def test_ground_refuses_on_dirty_tree(repo):
    (repo / "src" / "dirty.py").write_text("x=1\n")   # uncommitted -> dirty
    res = g.ground_card(_brief(title="OpsExecutor"), str(repo), base_branch="main")
    assert res.grounded is False       # falls back to text-only assess


def test_ground_refuses_on_unexpected_branch(repo):
    _git(repo, "checkout", "-q", "-b", "some-feature")
    res = g.ground_card(_brief(title="OpsExecutor"), str(repo), base_branch="main")
    assert res.grounded is False
