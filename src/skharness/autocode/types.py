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
class RepoSpec:  # one entry of repo_map (autopilot.yaml)
    name: str
    path: str
    base_branch: str
    integration_branch: str
    test_cmd: str
    ci: str  # "github-actions" | "local:<cmd>" | "none"
    coverage_cmd: str | None = None  # emits Cobertura/lcov; None -> PR-only
    ci_poll_timeout: int = 1200  # seconds to poll github-actions before red
    ci_scope: str = "full"  # "full" (whole suite) | "changed" (tests for the diff)
    advisory_checks: list[str] = field(default_factory=list)  # CI checks that are
    #   non-blocking for auto-merge even on failure, e.g. ["lint"] for a repo whose
    #   GitHub lint job is `continue-on-error` (advisory). A name matched here is
    #   dropped from the auto-merge core gate; security checks are NEVER advisory.
    #   Empty (default) preserves strict behavior: every core check must pass.
    automerge: bool = False
    auto_revert: bool = False
    min_diff_coverage: float = 0.8
    sandbox_image: str | None = None
    min_quality: QualityMode | None = None  # per-repo quality FLOOR (toggle spec G6):
    #   e.g. min_quality=gated on a deployed-service repo upgrades any direct/none
    #   request against it to gated. None means no floor (current behavior preserved).
    deploy_cmd: str | None = None  # change-mgmt P3.2: optional per-repo deploy step run by
    #   change_deploy_bridge AFTER a successful merge, when publish-on-main alone is not enough
    #   (design doc 2026-08-13-change-management-cab-ai-arch.md section 5.2 step 5). None means
    #   merge == deploy (the common case).


@dataclass
class AssessBrief:  # Phase 0 assess input
    task_id: str
    title: str
    description: str
    acceptance: list[str]
    tags: list[str]
    repo: str | None
    codebase_context: str


@dataclass
class TaskBrief:  # implement input
    task_id: str
    repo: RepoSpec
    worktree: str
    title: str
    description: str
    acceptance: list[str]
    prior_feedback: str | None
    round: int
    prior_success_feedback: str | None = None  # S18: the sibling of prior_feedback,
    #   read from meta.autopilot.successes[] (failure_memory.build_prior_success_feedback).
    #   Defaulted so every existing construction site is unchanged, and so a card with no
    #   success memory is byte-identical to the behaviour before this field existed.
    #   Unlike prior_feedback it is NOT overwritten round to round: it is cross-RUN memory
    #   with no in-run equivalent, and the live grade has nothing to say about it.


@dataclass
class GradeBrief:  # grade input
    task_id: str
    repo: RepoSpec
    worktree: str
    diff: str
    acceptance: list[str]
    ci_status: str  # green | red | pending | none
    diff_coverage: float | None  # changed-lines coverage ratio, or None


# Closed terminal-state vocabulary (design doc section 4.1). Exactly five values.
# A sixth value requires a written reason and an update to this set, plus the test
# that asserts its size and membership (tests/test_autopilot_types.py).
GATE_OUTCOMES = frozenset({"pass", "ci_red", "no_op", "salvage", "direct_fail"})

# Sentinel meaning "no terminal state was recorded". Never "this succeeded". A
# construction site that omits outcome has not measured anything, and a default of
# "pass" would let that absence read as good news. UNRECORDED is deliberately kept
# out of GATE_OUTCOMES: it is not a sixth terminal state, it is the absence of one.
UNRECORDED = "unrecorded"


@dataclass
class GateResult:
    score: int | None
    passed: bool
    notes: str
    artifact: str | None
    mode: str = "gated"  # which QualityMode produced this result; default
    #   "gated" preserves every existing construction site (and the twin gate's byte-
    #   identical GateResult(...) returns) for full back-compat.
    outcome: str = UNRECORDED  # terminal-state vocabulary (design doc section 4.1);
    #   default UNRECORDED preserves every existing construction site, none of which
    #   pass this argument today, WITHOUT claiming any of them passed. A later card
    #   (S2) populates the real value at each of the five terminal return sites; this
    #   default is a placeholder, not a claim: an unset outcome must never read as a
    #   success, per "never let a grade widen access by being absent".
    tokens: int = 0  # accumulated token usage; repairs the CapLedger
    #   budget ceiling at orchestrator.py:800, which today always adds zero because
    #   GateResult never carried this field.
    cost_usd: float = 0.0  # accumulated dollar cost, same repair as tokens.
    mutation_report: dict | None = None  # S23 (card 33c50540): the RAW report of the
    #   shadow mutation probe (mutation.probe) over this build's changed lines, or None
    #   when no probe ran. SHADOW ONLY: twin_gate_passed does not read it, no policy and
    #   no dispatch decision may read it, and tests/test_autocode_mutation.py section 5
    #   proves that statically over the whole package and behaviourally over both the
    #   gate and the model choice. It is carried here purely so the orchestrator's
    #   outcome row can record a label the WORKER DID NOT AUTHOR beside the score the
    #   worker's own tests helped produce. The default None preserves every existing
    #   construction site and, crucially, classifies as `unobserved` rather than as a
    #   clean sweep: an unset shadow label must never read as good news.

    def __post_init__(self):
        if self.outcome not in GATE_OUTCOMES and self.outcome != UNRECORDED:
            raise ValueError(
                f"GateResult.outcome must be one of {sorted(GATE_OUTCOMES)} or "
                f"{UNRECORDED!r}, got {self.outcome!r}"
            )


@dataclass
class Verdict:  # Phase 0 assess output
    verdict: str  # valid | stale | obsolete | needs_decision | decompose
    reason: str
    updated_description: str | None = None
    updated_acceptance: list[str] | None = None
    subtasks: list[dict] | None = None  # decompose payload: [{title, description, acceptance}]
    concreteness: float | None = None  # grounding score that drove the gate (audit)
    size: str | None = None  # grade axis: S|M|L|XL (shadow only, P1)
    risk: str | None = None  # grade axis: low|med|high|crit (shadow only, P1)
    sensitivity: str | None = None  # axis: public|internal|secret (shadow, P1)


class HarnessProvenanceReason(str, Enum):
    """Closed reasons why one provider-observed result fact is absent.

    These values are controller-authored labels, never model text.  Keeping the
    vocabulary closed prevents an assistant reply from smuggling an attribution
    claim through a free-form explanation.
    """

    MODEL_SERVED_NOT_OBSERVED = "provider_event_missing_response_model"
    MODEL_SERVED_PARTIAL = "provider_events_partial_response_model"
    MODEL_SERVED_CONFLICT = "provider_events_conflicting_response_models"
    MODEL_SERVED_INCOMPLETE_STREAM = "provider_event_stream_malformed_or_incomplete"
    BACKEND_SERVED_NOT_OBSERVED = "provider_event_missing_gateway_backend"
    GATEWAY_REQ_ID_NOT_OBSERVED = "provider_event_missing_gateway_request_id"


@dataclass
class HarnessResult:
    ok: bool
    artifact: str | None
    tokens: int
    cost_usd: float
    raw: dict
    model_requested: str | None = None
    model_served: str | None = None
    backend_served: str | None = None
    gateway_req_id: str | None = None
    model_served_reason: HarnessProvenanceReason | None = None
    backend_served_reason: HarnessProvenanceReason | None = None
    gateway_req_id_reason: HarnessProvenanceReason | None = None

    def __post_init__(self):
        # A reason is meaningful only for its own absent fact.  Coerce the public
        # string form into the enum, but reject unknown/free-form values, reasons
        # attached to the wrong field, and a simultaneous observation + absence.
        expected = (
            (
                "model_served",
                "model_served_reason",
                frozenset(
                    {
                        HarnessProvenanceReason.MODEL_SERVED_NOT_OBSERVED,
                        HarnessProvenanceReason.MODEL_SERVED_PARTIAL,
                        HarnessProvenanceReason.MODEL_SERVED_CONFLICT,
                        HarnessProvenanceReason.MODEL_SERVED_INCOMPLETE_STREAM,
                    }
                ),
            ),
            (
                "backend_served",
                "backend_served_reason",
                frozenset({HarnessProvenanceReason.BACKEND_SERVED_NOT_OBSERVED}),
            ),
            (
                "gateway_req_id",
                "gateway_req_id_reason",
                frozenset({HarnessProvenanceReason.GATEWAY_REQ_ID_NOT_OBSERVED}),
            ),
        )
        for value_name, reason_name, allowed_reasons in expected:
            reason = getattr(self, reason_name)
            if reason is None:
                continue
            try:
                reason = HarnessProvenanceReason(reason)
            except ValueError as exc:
                raise ValueError(f"{reason_name} is not a recognized provenance reason") from exc
            if reason not in allowed_reasons:
                raise ValueError(f"{reason_name} cannot use {reason.value!r}")
            if getattr(self, value_name) is not None:
                raise ValueError(f"{reason_name} requires {value_name} to be None")
            setattr(self, reason_name, reason)


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
