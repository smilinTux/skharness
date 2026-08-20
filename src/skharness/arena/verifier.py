"""Independent-verification policy and trust-boundary contracts.

Workers create :class:`ProvisionalSubmission` objects.  Those objects intentionally
have no withheld-evaluation field.  A separately privileged backend is configured
with private material and exposes only operations taking an opaque
:class:`PrivateEvaluationHandle`; this domain service never sends hidden bytes or a
filesystem path to a worker-controlled callback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Callable, Mapping, Protocol, Sequence

from skharness.arena.metrics import MetricObjective, MetricSummary, summarize


class VerificationStatus(str, Enum):
    VALID = "valid"
    INVALID = "invalid"
    INCONCLUSIVE = "inconclusive"


class ControlKind(str, Enum):
    GOLD = "gold"
    NO_OP = "no_op"
    ADVERSARIAL = "adversarial"


@dataclass(frozen=True)
class PrivateEvaluationHandle:
    """Opaque capability resolved only inside the privileged verifier backend."""

    evaluation_id: str
    version: str
    _capability: str = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.evaluation_id.strip() or not self.version.strip():
            raise ValueError("private evaluation identity and version are required")
        if not self._capability:
            raise ValueError("private evaluation capability must not be empty")


@dataclass(frozen=True)
class ProvisionalSubmission:
    experiment_id: str
    challenge_hash: str
    artifact_digest: str
    requested_model_digest: str
    claimed_metrics: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        required = {
            "experiment_id": self.experiment_id,
            "challenge_hash": self.challenge_hash,
            "artifact_digest": self.artifact_digest,
            "requested_model_digest": self.requested_model_digest,
        }
        missing = [name for name, value in required.items() if not value.strip()]
        if missing:
            raise ValueError(f"submission fields must not be empty: {', '.join(missing)}")
        object.__setattr__(self, "claimed_metrics", MappingProxyType(dict(self.claimed_metrics)))


@dataclass(frozen=True)
class TrialEvidence:
    """One verifier-observed execution, never a worker's claimed summary."""

    metrics: Mapping[str, float]
    served_model_digest: str
    artifact_digest: str
    modalities_exercised: frozenset[str] = field(default_factory=frozenset)
    capabilities_exercised: frozenset[str] = field(default_factory=frozenset)
    completed_work: bool = True
    output_truncated: bool = False
    cache_disclosed: bool = True
    warm_cache_used: bool = False
    cache_key_digest: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))
        object.__setattr__(self, "modalities_exercised", frozenset(self.modalities_exercised))
        object.__setattr__(self, "capabilities_exercised", frozenset(self.capabilities_exercised))
        if self.cache_key_digest is not None and not self.cache_key_digest.strip():
            raise ValueError("cache key digest must not be blank")


class VerificationBackend(Protocol):
    """Privileged execution backend; it resolves the opaque private handle."""

    def run_trial(
        self,
        submission: ProvisionalSubmission,
        private_evaluation: PrivateEvaluationHandle,
        trial_index: int,
    ) -> TrialEvidence: ...

    def run_control(
        self,
        control: ControlKind,
        private_evaluation: PrivateEvaluationHandle,
    ) -> bool: ...


@dataclass(frozen=True)
class VerificationPolicy:
    challenge_hash: str
    expected_model_digest: str
    repetitions: int
    objectives: tuple[MetricObjective, ...]
    required_modalities: frozenset[str] = field(default_factory=frozenset)
    required_capabilities: frozenset[str] = field(default_factory=frozenset)
    confidence_z: float = 1.96
    require_controls: bool = True

    def __post_init__(self) -> None:
        if not self.challenge_hash.strip() or not self.expected_model_digest.strip():
            raise ValueError("challenge hash and expected model digest are required")
        if self.repetitions < 2:
            raise ValueError("independent verification requires at least two repetitions")
        if not self.objectives:
            raise ValueError("at least one metric objective is required")
        names = [objective.name for objective in self.objectives]
        if len(names) != len(set(names)):
            raise ValueError("metric objective names must be unique")
        object.__setattr__(self, "required_modalities", frozenset(self.required_modalities))
        object.__setattr__(self, "required_capabilities", frozenset(self.required_capabilities))


@dataclass(frozen=True)
class VerificationVerdict:
    experiment_id: str
    status: VerificationStatus
    reasons: tuple[str, ...]
    summaries: Mapping[str, MetricSummary] = field(default_factory=dict)
    private_evaluation_id: str = ""
    private_evaluation_version: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "summaries", MappingProxyType(dict(self.summaries)))


class IndependentVerifier:
    """Rerun provisional work and derive a verdict from observed evidence only."""

    def __init__(
        self,
        backend: VerificationBackend,
        *,
        observe_verdict: Callable[[VerificationVerdict], None] | None = None,
    ) -> None:
        self._backend = backend
        self._observe_verdict = observe_verdict

    def verify(
        self,
        submission: ProvisionalSubmission,
        policy: VerificationPolicy,
        private_evaluation: PrivateEvaluationHandle,
    ) -> VerificationVerdict:
        identity = {
            "private_evaluation_id": private_evaluation.evaluation_id,
            "private_evaluation_version": private_evaluation.version,
        }
        if submission.challenge_hash != policy.challenge_hash:
            return self._verdict(
                submission, VerificationStatus.INVALID, ("challenge_hash_mismatch",), identity
            )
        if submission.requested_model_digest != policy.expected_model_digest:
            return self._verdict(
                submission, VerificationStatus.INVALID, ("requested_model_mismatch",), identity
            )

        if policy.require_controls:
            try:
                controls = {
                    control: self._backend.run_control(control, private_evaluation)
                    for control in ControlKind
                }
            except Exception:
                return self._verdict(
                    submission,
                    VerificationStatus.INCONCLUSIVE,
                    ("control_infrastructure_error",),
                    identity,
                )
            if not controls[ControlKind.GOLD]:
                return self._verdict(
                    submission, VerificationStatus.INCONCLUSIVE, ("gold_control_failed",), identity
                )
            if controls[ControlKind.NO_OP]:
                return self._verdict(
                    submission,
                    VerificationStatus.INCONCLUSIVE,
                    ("no_op_control_passed",),
                    identity,
                )
            if controls[ControlKind.ADVERSARIAL]:
                return self._verdict(
                    submission,
                    VerificationStatus.INCONCLUSIVE,
                    ("adversarial_control_passed",),
                    identity,
                )

        trials: list[TrialEvidence] = []
        try:
            for index in range(policy.repetitions):
                trials.append(self._backend.run_trial(submission, private_evaluation, index))
        except Exception:
            return self._verdict(
                submission,
                VerificationStatus.INCONCLUSIVE,
                ("trial_infrastructure_error",),
                identity,
            )

        violations = self._gaming_violations(submission, policy, trials)
        if violations:
            return self._verdict(submission, VerificationStatus.INVALID, violations, identity)

        summaries: dict[str, MetricSummary] = {}
        try:
            for objective in policy.objectives:
                summaries[objective.name] = summarize(
                    (trial.metrics[objective.name] for trial in trials),
                    confidence_z=policy.confidence_z,
                )
        except (KeyError, ValueError):
            return self._verdict(
                submission, VerificationStatus.INVALID, ("invalid_metric_evidence",), identity
            )

        failed_constraints = tuple(
            f"constraint_failed:{objective.name}"
            for objective in policy.objectives
            if not objective.accepts(summaries[objective.name].mean)
        )
        if failed_constraints:
            return self._verdict(
                submission,
                VerificationStatus.INVALID,
                failed_constraints,
                identity,
                summaries,
            )
        return self._verdict(submission, VerificationStatus.VALID, (), identity, summaries)

    @staticmethod
    def _gaming_violations(
        submission: ProvisionalSubmission,
        policy: VerificationPolicy,
        trials: Sequence[TrialEvidence],
    ) -> tuple[str, ...]:
        violations: set[str] = set()
        for trial in trials:
            if trial.served_model_digest != policy.expected_model_digest:
                violations.add("served_model_mismatch")
            if trial.artifact_digest != submission.artifact_digest:
                violations.add("artifact_digest_mismatch")
            if not trial.completed_work:
                violations.add("skipped_work")
            if trial.output_truncated:
                violations.add("output_truncated")
            if not trial.cache_disclosed:
                violations.add("cache_undisclosed")
            if trial.warm_cache_used and (
                not trial.cache_disclosed or trial.cache_key_digest is None
            ):
                violations.add("warm_cache_concealed")
            if not policy.required_modalities.issubset(trial.modalities_exercised):
                violations.add("required_modality_missing")
            if not policy.required_capabilities.issubset(trial.capabilities_exercised):
                violations.add("required_capability_missing")
        return tuple(sorted(violations))

    def _verdict(
        self,
        submission: ProvisionalSubmission,
        status: VerificationStatus,
        reasons: tuple[str, ...],
        identity: Mapping[str, str],
        summaries: Mapping[str, MetricSummary] | None = None,
    ) -> VerificationVerdict:
        verdict = VerificationVerdict(
            experiment_id=submission.experiment_id,
            status=status,
            reasons=reasons,
            summaries=summaries or {},
            **identity,
        )
        if self._observe_verdict is not None:
            self._observe_verdict(verdict)
        return verdict
