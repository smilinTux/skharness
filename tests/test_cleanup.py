"""Tests for post-run resource reclamation / spin-down."""

from __future__ import annotations

from skharness.autocode import cleanup


def test_reclaim_off_is_noop():
    assert cleanup.reclaim("off") == {"mode": "off"}


def test_reclaim_cold_keeps_images(monkeypatch):
    monkeypatch.setattr(cleanup, "reclaim_worktrees", lambda repos, refs: 2)
    monkeypatch.setattr(cleanup, "reclaim_sandboxes", lambda: (3, 1))
    out = cleanup.reclaim("cold", repo_paths=["/nope"], refs=["a", "b"])
    assert out["mode"] == "cold"
    assert out["worktrees"] == 2 and out["containers"] == 3 and out["networks"] == 1
    assert out["images_removed"] == 0                 # cold KEEPS the image


def test_reclaim_teardown_removes_images(monkeypatch):
    monkeypatch.setattr(cleanup, "reclaim_worktrees", lambda repos, refs: 0)
    monkeypatch.setattr(cleanup, "reclaim_sandboxes", lambda: (0, 0))
    monkeypatch.setattr(cleanup, "remove_images", lambda: 2)
    out = cleanup.reclaim("teardown", repo_paths=["/nope"])
    assert out["images_removed"] == 2


def test_remove_images_skipped_while_a_build_is_running(monkeypatch):
    monkeypatch.setattr(cleanup, "_running_sandboxes", lambda: 2)   # 2 live builds
    assert cleanup.remove_images() == 0               # never rug-pull a running build


def test_reclaim_never_raises(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("docker down")
    monkeypatch.setattr(cleanup, "reclaim_worktrees", _boom)
    out = cleanup.reclaim("cold", repo_paths=["/nope"])
    assert out["mode"] == "cold" and "error" in out   # degraded, not raised


def test_reclaim_worktrees_only_touches_this_runs_refs(monkeypatch, tmp_path):
    # Only the given refs' worktrees + branches are removed; a concurrent run's
    # worktree is never enumerated/removed. We assert the exact git argv issued.
    calls = []

    class _CP:
        stdout = ""
        returncode = 0

    def _fake_run(argv):
        calls.append(argv)
        return _CP()

    monkeypatch.setattr(cleanup, "_run", _fake_run)
    repo = tmp_path / "skchat"
    repo.mkdir()
    (tmp_path / "skchat-wt" / "cardA").mkdir(parents=True)   # exists -> removed
    n = cleanup.reclaim_worktrees([str(repo)], refs=["cardA", "cardB"])
    assert n == 1                                     # only cardA's dir existed
    removed = [c for c in calls if c[3:6] == ["worktree", "remove", "--force"]]
    assert len(removed) == 1 and removed[0][-1].endswith("skchat-wt/cardA")
    # branches for BOTH refs are deleted; a final prune runs
    assert any(c[3:5] == ["branch", "-D"] and c[-1] == "autopilot/cardB" for c in calls)
    assert any(c[3:5] == ["worktree", "prune"] for c in calls)
