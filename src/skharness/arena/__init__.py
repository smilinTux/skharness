"""Domain primitives for the sovereign SKHarness Evolution Arena.

Domain contracts remain usable across process boundaries; the explicit runner module
composes them with the production Pi sandbox without weakening verifier isolation.
"""

from skharness.arena.access import (
    AccessDeniedError,
    AgentMessage,
    AttemptOwnership,
    CollaborationAccess,
)
from skharness.arena.collaboration import (
    CollaborationError,
    ExperimentCatalog,
    ExperimentMatch,
    NegativeKind,
    NegativeKnowledge,
    NegativeKnowledgeIndex,
    PositiveEvidence,
    RefinementEvent,
    RefinementJournal,
    RefinementProposal,
    RefinementScope,
    RefinementState,
    evidence_id,
)
from skharness.arena.lineage import LineageGraph
from skharness.arena.memory_adapter import (
    ExecutableRuntimeBackend,
    MemoryReceipt,
    RuntimeSKMemoryAdapter,
)
from skharness.arena.metrics import (
    MetricDirection,
    MetricObjective,
    MetricSummary,
    ParetoCandidate,
    VerifiedParetoCandidate,
    pareto_frontier,
    summarize,
    verified_pareto_frontier,
)
from skharness.arena.models import (
    ChallengeSpec,
    Experiment,
    ExperimentEvent,
    ExperimentState,
    Measurement,
    Result,
    VerificationState,
    canonical_digest,
)
from skharness.arena.operations import ArenaJobService
from skharness.arena.runner import (
    PiExperimentRunner,
    RunOutcome,
    SandboxProcessSupervisor,
    build_production_pi_runner,
    pi_launch_spec,
)
from skharness.arena.status import ArenaStatusService, BoundedArenaMetrics, ProbeResult
from skharness.arena.store import ArenaStore, CorruptEventLogError, EventConflictError
from skharness.arena.verifier import (
    ControlKind,
    IndependentVerifier,
    PrivateEvaluationHandle,
    ProvisionalSubmission,
    TrialEvidence,
    VerificationPolicy,
    VerificationStatus,
    VerificationVerdict,
)

__all__ = [
    "ArenaStore",
    "AccessDeniedError",
    "AgentMessage",
    "AttemptOwnership",
    "ArenaStatusService",
    "ArenaJobService",
    "BoundedArenaMetrics",
    "CollaborationError",
    "CollaborationAccess",
    "ChallengeSpec",
    "ControlKind",
    "CorruptEventLogError",
    "EventConflictError",
    "Experiment",
    "ExperimentCatalog",
    "ExperimentMatch",
    "ExperimentEvent",
    "ExperimentState",
    "ExecutableRuntimeBackend",
    "IndependentVerifier",
    "LineageGraph",
    "Measurement",
    "MetricDirection",
    "MetricObjective",
    "MetricSummary",
    "NegativeKind",
    "NegativeKnowledge",
    "NegativeKnowledgeIndex",
    "PositiveEvidence",
    "ParetoCandidate",
    "VerifiedParetoCandidate",
    "PrivateEvaluationHandle",
    "PiExperimentRunner",
    "ProbeResult",
    "ProvisionalSubmission",
    "Result",
    "RunOutcome",
    "MemoryReceipt",
    "RuntimeSKMemoryAdapter",
    "SandboxProcessSupervisor",
    "RefinementEvent",
    "RefinementJournal",
    "RefinementProposal",
    "RefinementScope",
    "RefinementState",
    "TrialEvidence",
    "VerificationPolicy",
    "VerificationStatus",
    "VerificationState",
    "VerificationVerdict",
    "build_production_pi_runner",
    "pareto_frontier",
    "pi_launch_spec",
    "canonical_digest",
    "evidence_id",
    "summarize",
    "verified_pareto_frontier",
]
