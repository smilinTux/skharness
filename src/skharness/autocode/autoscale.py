"""Resource-based autoscaler for on-host build concurrency.

Each concurrent autocode build costs roughly one core (the pytest CI leg), a few
GB of RAM (Docker sandbox + pytest), and a worktree of disk. Rather than a fixed
number, pick how many run at once from the host's ACTUAL capacity, in one of four
modes:

  ``min``          -> 1 (most conservative)
  ``recommended``  -> balanced; leaves the host headroom (DEFAULT, alias ``auto``)
  ``max``          -> aggressive; uses most of the host's capacity
  ``<int>``        -> exactly that many, clamped to [1, the resource max]

The result is ALWAYS clamped to the configured hard cap (``caps.max_concurrent``)
so the operator keeps a ceiling regardless of what the box could theoretically
run. This lets one config run correctly on a 4-core/16 GB box and a big laptop
alike: each scales to itself.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

# Absolute sanity ceiling: never propose more than this regardless of resources
# (claude-code + Docker fan-out has diminishing returns and shared-API limits).
_HARD_CEIL = 12
_REPO_ROOT = "~/clawd/skcapstone-repos"     # where per-build worktrees are created

# Per-build resource budgets (GB) used to derive the ceilings.
_RAM_PER_BUILD_REC = 3.0
_RAM_PER_BUILD_MAX = 2.0
_DISK_PER_BUILD_REC = 3.0
_DISK_PER_BUILD_MAX = 2.0


def _free_ram_gb() -> float:
    try:
        import psutil

        return psutil.virtual_memory().available / 2**30
    except Exception:
        pass
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 2**20   # kB -> GB
    except Exception:
        pass
    return 8.0                                        # optimistic fallback


def _free_disk_gb() -> float:
    try:
        return shutil.disk_usage(Path(_REPO_ROOT).expanduser()).free / 2**30
    except Exception:
        return 20.0


def resources() -> dict:
    """Current host capacity: cores + free RAM/disk (GB, rounded)."""
    return {
        "cores": os.cpu_count() or 2,
        "ram_gb": round(_free_ram_gb(), 1),
        "disk_gb": round(_free_disk_gb(), 1),
    }


def _ceiling(*, aggressive: bool) -> int:
    r = resources()
    if aggressive:                                    # "max": use most of the box
        cpu = r["cores"]
        ram_per, disk_per = _RAM_PER_BUILD_MAX, _DISK_PER_BUILD_MAX
    else:                                             # "recommended": leave headroom
        cpu = max(1, r["cores"] - 1)                  # keep a core for the host/daemon
        ram_per, disk_per = _RAM_PER_BUILD_REC, _DISK_PER_BUILD_REC
    return max(1, min(_HARD_CEIL, cpu,
                      int(r["ram_gb"] // ram_per),
                      int(r["disk_gb"] // disk_per)))


def recommended() -> int:
    """Balanced concurrency for this host (leaves headroom)."""
    return _ceiling(aggressive=False)


def maximum() -> int:
    """Aggressive concurrency for this host (uses most capacity)."""
    return _ceiling(aggressive=True)


def resolve(mode, hard_cap: int | None = None) -> int:
    """Resolve a concurrency mode to a worker count for THIS host.

    ``mode``: ``min`` | ``recommended`` | ``auto`` | ``max`` | an int (as int or
    string). Unknown -> recommended. Always clamped to [1, hard_cap].
    """
    m = str(mode if mode is not None else "recommended").strip().lower()
    if m == "min":
        n = 1
    elif m == "max":
        n = maximum()
    elif m in ("recommended", "auto", ""):
        n = recommended()
    else:
        try:
            n = max(1, min(int(m), maximum()))
        except ValueError:
            n = recommended()
    if hard_cap and hard_cap > 0:
        n = min(n, hard_cap)
    return max(1, n)


def describe(mode, hard_cap: int | None = None) -> str:
    """One-line human summary of the autoscaler decision (for logs/doctor)."""
    r = resources()
    return (f"concurrency={resolve(mode, hard_cap)} "
            f"(mode={mode}, recommended={recommended()}, max={maximum()}, "
            f"cap={hard_cap}) on {r['cores']} cores / {r['ram_gb']}GB RAM / "
            f"{r['disk_gb']}GB disk")
