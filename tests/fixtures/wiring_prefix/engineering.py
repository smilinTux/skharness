"""The engineering executor (instances 3, 7, 8).

  * instance 3 (changed_paths_are_protected): the self-modification carve-out
    has exactly one call site, inside ``if automerge:``. ``automerge`` requires
    the repo to be in ``automerge_repos``, which is [] in every live config, so
    the carve-out never runs.
  * instance 8 (routing_guard in _ALWAYS_PROTECTED): the membership is consulted
    only by ``is_protected``, itself reached only behind the same dead flag, so
    the entry protects nothing.
  * instance 7 (meta.autopilot.reverted): assigned a constant ``False``, so the
    revert sensor is dead -- it reads exactly like good news.
"""
from __future__ import annotations

_ALWAYS_PROTECTED = ["src/skharness/autocode/routing_guard.py"]


def changed_paths_are_protected(paths: list[str]) -> bool:
    return any(p.startswith("src/skcapstone/itil") for p in paths)


def is_protected(path: str) -> bool:
    return path in _ALWAYS_PROTECTED


def _automerge_for(item: object, config: object) -> bool:
    return getattr(item, "repo", "") in getattr(config, "automerge_repos", [])


def _changed_paths(item: object) -> list[str]:
    return getattr(item, "paths", [])


def engineer(item: object, config: object) -> dict:
    meta = {"autopilot": {}}
    # instance 7: a constant sensor.
    meta["autopilot"]["reverted"] = False
    automerge = _automerge_for(item, config)
    if automerge:
        changed = _changed_paths(item)
        if changed_paths_are_protected(changed):
            meta["blocked"] = True
        for path in changed:
            if is_protected(path):
                meta["blocked"] = True
    return meta
