"""Autopilot data contracts (spec section 10). Plain dataclasses, no I/O."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class QualityMode(str, Enum):
    """One quality axis, three values (toggle spec Decision 1).

    GATED  -> the hardcore loop: worktree + Ralph rounds + 1-5 grade + twin gate
              + merge policy (EngineeringExecutor, the crown jewel, verbatim).
    DIRECT -> simple/unattended: ONE sandboxed harness run in a worktree, branch
              + diff + PR produced, NO grade, NO gate, NEVER merges (DirectExecutor).
    NONE   -> no engine at all: a plain live session (skcode/telegram only).

    Ordered by strength (NONE < DIRECT < GATED); quality is only ever lowered by an
    explicit, attributable choice, never implicitly. GATED is the always-fallback.
    """
    GATED = "gated"
    DIRECT = "direct"
    NONE = "none"


# Strength ranking so a per-repo floor (RepoSpec.min_quality) can only raise, never
# lower, a requested mode. Higher rank = stronger (more review) = safer.
QUALITY_RANK: dict[QualityMode, int] = {
    QualityMode.NONE: 0,
    QualityMode.DIRECT: 1,
    QualityMode.GATED: 2,
}


def coerce_quality(value) -> QualityMode:
    """Normalize a str / QualityMode / None into a QualityMode (default GATED).

    Unknown strings fail closed to GATED: quality is never lowered by a typo.
    """
    if isinstance(value, QualityMode):
        return value
    if value is None:
        return QualityMode.GATED
    try:
        return QualityMode(str(value).strip().lower())
    except ValueError:
        return QualityMode.GATED


@dataclass
class WorkItem:
    kind: str
    ref: str
    source: str
    repo: str | None
    payload: dict


@dataclass
class RepoSpec:                       # one entry of repo_map (autopilot.yaml)
    name: str
    path: str
    base_branch: str
    integration_branch: str
    test_cmd: str
    ci: str                           # "github-actions" | "local:<cmd>" | "none"
    coverage_cmd: str | None = None   # emits Cobertura/lcov; None -> PR-only
    ci_poll_timeout: int = 1200       # seconds to poll github-actions before red
    ci_scope: str = "full"            # "full" (whole suite) | "changed" (tests for the diff)
    advisory_checks: list[str] = field(default_factory=list)   # CI checks that are
    #   non-blocking for auto-merge even on failure, e.g. ["lint"] for a repo whose
    #   GitHub lint job is `continue-on-error` (advisory). A name matched here is
    #   dropped from the auto-merge core gate; security checks are NEVER advisory.
    #   Empty (default) preserves strict behavior: every core check must pass.
    automerge: bool = False
    auto_revert: bool = False
    min_diff_coverage: float = 0.8
    sandbox_image: str | None = None
    min_quality: QualityMode | None = None   # per-repo quality FLOOR (toggle spec G6):
    #   e.g. min_quality=gated on a deployed-service repo upgrades any direct/none
    #   request against it to gated. None means no floor (current behavior preserved).
    deploy_cmd: str | None = None     # change-mgmt P3.2: optional per-repo deploy step run by
    #   change_deploy_bridge AFTER a successful merge, when publish-on-main alone is not enough
    #   (design doc 2026-08-13-change-management-cab-ai-arch.md section 5.2 step 5). None means
    #   merge == deploy (the common case).


@dataclass
class AssessBrief:                    # Phase 0 assess input
    task_id: str
    title: str
    description: str
    acceptance: list[str]
    tags: list[str]
    repo: str | None
    codebase_context: str


@dataclass
class TaskBrief:                      # implement input
    task_id: str
    repo: RepoSpec
    worktree: str
    title: str
    description: str
    acceptance: list[str]
    prior_feedback: str | None
    round: int


@dataclass
class GradeBrief:                     # grade input
    task_id: str
    repo: RepoSpec
    worktree: str
    diff: str
    acceptance: list[str]
    ci_status: str                    # green | red | pending | none
    diff_coverage: float | None       # changed-lines coverage ratio, or None


@dataclass
class GateResult:
    score: int | None
    passed: bool
    notes: str
    artifact: str | None
    mode: str = "gated"               # which QualityMode produced this result; default
    #   "gated" preserves every existing construction site (and the twin gate's byte-
    #   identical GateResult(...) returns) for full back-compat.


@dataclass
class Verdict:                        # Phase 0 assess output
    verdict: str                      # valid | stale | obsolete | needs_decision | decompose
    reason: str
    updated_description: str | None = None
    updated_acceptance: list[str] | None = None
    subtasks: list[dict] | None = None      # decompose payload: [{title, description, acceptance}]
    concreteness: float | None = None       # grounding score that drove the gate (audit)
    size: str | None = None  # grade axis: S|M|L|XL (shadow only, P1)
    risk: str | None = None  # grade axis: low|med|high|crit (shadow only, P1)
    sensitivity: str | None = None  # axis: public|internal|secret (shadow, P1)


@dataclass
class HarnessResult:
    ok: bool
    artifact: str | None
    tokens: int
    cost_usd: float
    raw: dict


@dataclass
class DecisionItem:
    qid: str
    prompt: str
    options: dict
    action_ref: str | None
    priority: str


class ClaimRaced(Exception):
    """The coord claim was won by another runtime (stale-placement guard).

    Raised instead of executing when claim_task reports the card claimed by a
    different agent name; the swarm records a skip, never a double-run.
    """
