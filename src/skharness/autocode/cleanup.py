"""Post-run resource reclamation + spin-down for the autocode harness.

Each build leaves transient artifacts that pile up and eat disk/RAM: a git
worktree (a full repo checkout, hundreds of MB), a Docker sandbox container +
network, and coverage/pycache. After a run finishes, reclaim them.

Spin-down policy for the sandbox IMAGE (``mode``):
  ``cold`` (default) -- reclaim this run's worktrees + EXITED sandbox containers +
                        unused networks, but KEEP the sandbox image so the next
                        run starts immediately ("cold harness, ready to go").
  ``teardown``       -- also remove the sandbox images (reclaim ~1.5 GB); the next
                        run rebuilds the image. Only removes images when NO build
                        is still running (never pulls the rug on a live build).
  ``off``            -- reclaim nothing.

A running sandbox container (another concurrent build, or a dual-node peer) is
NEVER touched: only THIS run's worktrees and EXITED containers are removed, and
image teardown is skipped while any ``sbxrun-*`` container is up. Everything is
best-effort and never raises into the run.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger("skharness.autocode.cleanup")

_SANDBOX_IMAGES = ("sandbox-claude:1", "sandbox-proxy:1")


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True)


def _running_sandboxes() -> int:
    """Count currently-RUNNING sandbox build containers (never touch these)."""
    out = _run(["docker", "ps", "--filter", "name=sbxrun-", "-q"]).stdout
    return len([x for x in out.splitlines() if x.strip()])


def reclaim_worktrees(repo_paths, refs=()) -> int:
    """Remove the ``autopilot/<ref>`` worktree + local branch for each ref in
    THIS run, then prune dead registrations. Removing only this run's refs keeps
    a concurrent run's live worktrees intact. Returns the count removed."""
    removed = 0
    refs = list(refs)
    for repo in repo_paths:
        for ref in refs:
            wt = Path(repo).parent / f"{Path(repo).name}-wt" / ref
            if wt.exists():
                _run(["git", "-C", repo, "worktree", "remove", "--force", str(wt)])
                removed += 1
            _run(["git", "-C", repo, "branch", "-D", f"autopilot/{ref}"])
        _run(["git", "-C", repo, "worktree", "prune"])   # dead registrations only
    return removed


def reclaim_sandboxes() -> tuple[int, int]:
    """Remove EXITED sandbox containers + unused sandbox networks. Running
    containers (other live builds) are left alone. Returns (containers, networks)."""
    exited = _run(["docker", "ps", "-a", "--filter", "name=sbxrun-",
                   "--filter", "status=exited", "-q"]).stdout.split()
    exited += _run(["docker", "ps", "-a", "--filter", "name=sbxrun-",
                    "--filter", "status=created", "-q"]).stdout.split()
    for cid in exited:
        _run(["docker", "rm", "-f", cid])
    before = _run(["docker", "network", "ls", "--filter", "name=sbxnet-", "-q"]).stdout.split()
    _run(["docker", "network", "prune", "-f"])           # unused nets only
    after = _run(["docker", "network", "ls", "--filter", "name=sbxnet-", "-q"]).stdout.split()
    return len(exited), max(0, len(before) - len(after))


def remove_images() -> int:
    """Remove the sandbox images (teardown). Skipped while any build is running."""
    if _running_sandboxes():
        log.info("teardown skipped: %d sandbox build(s) still running", _running_sandboxes())
        return 0
    n = 0
    for img in _SANDBOX_IMAGES:
        if _run(["docker", "image", "inspect", img]).returncode == 0:
            _run(["docker", "rmi", "-f", img])
            n += 1
    return n


def _free_gb(path: str) -> float:
    try:
        return round(shutil.disk_usage(Path(path).expanduser()).free / 2**30, 1)
    except Exception:
        return -1.0


def reclaim(mode: str = "cold", *, repo_paths=(), refs=()) -> dict:
    """Reclaim transient build resources per ``mode``. Never raises."""
    mode = (mode or "cold").strip().lower()
    if mode == "off":
        return {"mode": "off"}
    probe = repo_paths[0] if repo_paths else "~"
    before = _free_gb(probe)
    result = {"mode": mode}
    try:
        result["worktrees"] = reclaim_worktrees(repo_paths, refs)
        c, net = reclaim_sandboxes()
        result["containers"] = c
        result["networks"] = net
        result["images_removed"] = remove_images() if mode == "teardown" else 0
        result["disk_freed_gb"] = round(_free_gb(probe) - before, 1) if before >= 0 else None
    except Exception as exc:                            # best-effort, never fail a run
        log.warning("cleanup failed (harmless): %s", exc)
        result["error"] = str(exc)[:120]
    return result
