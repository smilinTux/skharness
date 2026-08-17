"""Autopilot config: ~/.skcapstone/config/autopilot.yaml -> typed Config.

Precedence: SKOS_AUTOPILOT_CONFIG (explicit) > <SKCAPSTONE_HOME>/config/autopilot.yaml
> ~/.skcapstone/config/autopilot.yaml. A missing file yields a disabled default so
a fresh box never auto-runs.

WHY AN UNKNOWN KEY RAISES, and it is the reason `ConfigError` exists
--------------------------------------------------------------------
Every block below is parsed by filtering the raw yaml against a known-key set.
Before 2026-08-16 an unrecognised key was simply dropped, so a key the operator
DELIBERATELY wrote had no effect and produced no output of any kind. Measured on
~/.skcapstone/config/autopilot-pi.yaml the same day: it carried `new_tasks_per_run: 1`
at the TOP level, while `Config.load` only ever reads that name out of the `caps:`
block. The operator believed the run was capped at one new task. The default of 10
applied instead, and nothing anywhere said so. A misplaced key and a correct key are
indistinguishable at runtime, which is exactly the class of failure this file must
not have.

So parsing fails closed, matching the house precedent: `types.coerce_quality` falls
to GATED rather than to a permissive mode on an unrecognised value, and
`buckets.BucketError` raises rather than returning None because returning None would
silently widen. Here, dropping a key silently widens a cap. A raise makes a typo or a
misplaced key a startup failure rather than a policy change nobody ordered.

The lint covers all three filtered levels (top level, `caps:`, and each `repo_map:`
entry) because each one uses the same drop-on-unknown filter and would otherwise be
the next place this happens.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .types import QualityMode, RepoSpec, coerce_quality

_REPO_KEYS = {
    "name", "path", "base_branch", "integration_branch", "test_cmd", "ci",
    "coverage_cmd", "ci_poll_timeout", "ci_scope", "advisory_checks", "automerge",
    "auto_revert", "min_diff_coverage", "sandbox_image", "min_quality", "deploy_cmd",
}


class ConfigError(ValueError):
    """An autopilot yaml carries a key this loader does not understand.

    Raised, never warned. See the module docstring: a dropped key is a policy
    change the operator did not order and cannot observe.
    """


def _reject_unknown(known: set[str], raw: dict, where: str, path: Path) -> None:
    """Raise ConfigError if `raw` carries a key outside `known`.

    `where` names the block for the message ("top level", "caps", "repo_map.skos")
    so the operator is told which line to move, not merely that something is wrong.
    """
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: {where} must be a mapping, got {type(raw).__name__}")
    unknown = sorted(set(raw) - known)
    if not unknown:
        return
    hint = ""
    # The measured case: a caps key written at the top level. Say so outright.
    misplaced = [k for k in unknown if where == "top level" and k in _CAPS_KEYS]
    if misplaced:
        hint = (f" ({', '.join(misplaced)} belongs under the `caps:` block; "
                "at the top level it is ignored and the default cap applies)")
    raise ConfigError(
        f"{path}: unknown key(s) in {where}: {', '.join(unknown)}{hint}. "
        "Autopilot refuses to load a config it would otherwise silently ignore.")


def config_path() -> Path:
    env = os.environ.get("SKOS_AUTOPILOT_CONFIG")
    if env:
        return Path(env).expanduser()
    home = Path(os.environ.get("SKCAPSTONE_HOME", str(Path.home() / ".skcapstone")))
    return home / "config" / "autopilot.yaml"


@dataclass
class Caps:
    max_concurrent: int = 3               # HARD ceiling; the autoscaler never exceeds it
    concurrency: str = "recommended"      # min | recommended | max | <int> (resource-scaled)
    new_tasks_per_run: int = 10
    max_tokens_per_run: int = 2_000_000
    max_usd_per_day: float = 25.0
    max_subtasks_per_card: int = 8        # a decompose can never explode the board
    max_decompose_children_per_run: int = 24  # per-RUN child ceiling across all epics (anti-flood)
    max_decompose_depth: int = 2          # children carry decomp_depth; ceiling -> needs_decision
    concreteness_floor: float = 0.34      # repo card below this (and not net_new) -> decompose


#: The accepted `caps:` keys. Derived from the dataclass so a new cap cannot be
#: rejected by a hand-maintained list that someone forgot to update.
_CAPS_KEYS = set(Caps.__dataclass_fields__)


@dataclass
class Config:
    enabled: bool = False
    harness: str = "claude-code"
    allowed_tools: list[str] = field(default_factory=list)
    repo_map: dict[str, RepoSpec] = field(default_factory=dict)
    automerge_repos: list[str] = field(default_factory=list)
    cleanup_after_run: str = "cold"       # spin-down: cold (keep image) | teardown | off
    caps: Caps = field(default_factory=Caps)
    digest_chat: str | None = None
    epic_id: str | None = None
    dry_run: bool = True
    dry_run_summary: bool = False
    harness_model: str | None = None
    harness_base_url: str | None = None
    harness_max_tokens: int | None = None
    live_execution: bool = False
    mcp_endpoints: list[str] = field(default_factory=list)
    sandbox_image: str | None = None
    default_quality: QualityMode = QualityMode.GATED   # per-interface default quality
    #   (toggle spec 2.2 step 3); board/CLI runs fall back to this, then to gated.
    fleet_dispatch: bool = True           # consult the fleet scheduler before swarm

    def repo(self, name: str) -> RepoSpec | None:
        return self.repo_map.get(name)

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        p = path or config_path()
        if not p.exists():
            return cls()                                  # disabled default
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        _reject_unknown(_TOP_KEYS, raw, "top level", p)
        repo_map: dict[str, RepoSpec] = {}
        for name, spec in (raw.get("repo_map") or {}).items():
            spec = dict(spec or {})
            spec.setdefault("name", name)                 # key is the canonical name
            _reject_unknown(_REPO_KEYS, spec, f"repo_map.{name}", p)
            if spec.get("min_quality") is not None:       # yaml str -> QualityMode floor
                spec["min_quality"] = coerce_quality(spec["min_quality"])
            repo_map[name] = RepoSpec(**{k: v for k, v in spec.items() if k in _REPO_KEYS})
        caps_raw = raw.get("caps") or {}
        _reject_unknown(_CAPS_KEYS, caps_raw, "caps", p)
        caps = Caps(**{k: v for k, v in caps_raw.items()
                       if k in _CAPS_KEYS})
        return cls(
            enabled=bool(raw.get("enabled", False)),
            harness=raw.get("harness", "claude-code"),
            allowed_tools=list(raw.get("allowed_tools") or []),
            repo_map=repo_map,
            automerge_repos=list(raw.get("automerge_repos") or []),
            cleanup_after_run=raw.get("cleanup_after_run", "cold"),
            caps=caps,
            digest_chat=raw.get("digest_chat"),
            epic_id=raw.get("epic_id"),
            dry_run=bool(raw.get("dry_run", True)),
            dry_run_summary=bool(raw.get("dry_run_summary", False)),
            harness_model=raw.get("harness_model"),
            harness_base_url=raw.get("harness_base_url"),
            harness_max_tokens=raw.get("harness_max_tokens"),
            live_execution=bool(raw.get("live_execution", False)),
            mcp_endpoints=list(raw.get("mcp_endpoints") or []),
            sandbox_image=raw.get("sandbox_image"),
            default_quality=coerce_quality(raw.get("default_quality")),
            fleet_dispatch=bool(raw.get("fleet_dispatch", True)),
        )


#: The accepted top-level yaml keys. Every Config field is settable from yaml and
#: `load` reads exactly these, so deriving the set from the dataclass keeps the lint
#: and the parser from ever disagreeing.
_TOP_KEYS = set(Config.__dataclass_fields__)


_AUTOPILOT_JOB_YAML = """\
autopilot-daily:
  schedule: "30 6 * * *"
  type: shell
  nodes: [noroc2027]
  command: >
    /usr/bin/flock -n /home/cbrd21/.skcapstone/scheduler/autopilot-daily.lock
    /home/cbrd21/clawd/skos/scripts/sk-cron-run.sh autopilot-daily
    /home/cbrd21/.skenv/bin/skos autopilot run --once
  timeout: 3600
  retries: 0
  jitter: 30
  notify: on_failure
  notify_level: warn
  catchup: false
  enabled: true
"""


def load(path: Path | None = None) -> Config:
    """Module-level convenience so callers can `from skharness.autocode import config;
    config.load()` without touching the classmethod."""
    return Config.load(path)


def render_autopilot_job_yaml() -> str:
    """Return the literal `autopilot-daily` scheduler block (spec section 13).

    Ready to paste under the top-level `jobs:` map in
    ~/.skcapstone/config/jobs.yaml. Kept as source-of-truth here so the block
    is testable even though the live synced config is edited by hand.
    """
    return _AUTOPILOT_JOB_YAML
