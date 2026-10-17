"""Governed card-to-session routing through the authoritative claim gate.

The router converts distinct card requests into distinct session assignments.
It does not discover work from a board projection and it does not implement a
launcher. Callers supply cards selected from the authoritative eligibility fold,
an SKCoord ``Board`` claim gate, and a launcher. Production launchers can wrap a
session-plane harness. Tests use a fake launcher and never touch tmux, git, or a
live pool.

Claims happen for the complete batch before the first launch. A rejected claim
releases earlier claims from that batch and launches nothing. Once launching has
started, claims are retained on failure because the router cannot safely assume
that a failed launcher left no process or worktree behind.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Protocol, Sequence


class ClaimGate(Protocol):
    """The SKCoord mutation surface used by the router."""

    def claim_task(self, agent_name: str, task_id: str, force: bool = False): ...

    def release_claim(self, owner: str, task_id: str, actor: str = "") -> bool: ...


@dataclass(frozen=True)
class CardRoute:
    """One already-selected card and the immutable inputs its session needs."""

    card_id: str
    repo: str
    base_branch: str
    prompt: str


@dataclass(frozen=True)
class SessionAssignment:
    """Router-owned identity, branch, and worktree reservation for one card."""

    card_id: str
    repo: str
    base_branch: str
    prompt: str
    agent_identity: str
    worktree: str
    branch: str


@dataclass(frozen=True)
class LaunchReceipt:
    """Exact facts returned by an injected launcher after a successful launch."""

    session_id: str
    card_id: str
    agent_identity: str
    worktree: str
    branch: str


@dataclass(frozen=True)
class RoutedSession:
    """A claimed assignment joined to its launched session identity."""

    assignment: SessionAssignment
    receipt: LaunchReceipt


class SessionLauncher(Protocol):
    """Injected launch seam. Implementations must honor every reservation."""

    def __call__(self, assignment: SessionAssignment) -> Awaitable[LaunchReceipt]: ...


class CardRouteError(RuntimeError):
    """Base error for a refused or inconsistent routing batch."""


class ClaimRejectedError(CardRouteError):
    """The authoritative claim gate refused at least one card."""


class LaunchInvariantError(CardRouteError):
    """A launcher returned facts that differ from the governed assignment."""


_AGENT_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,126}[A-Za-z0-9])?")


class CardSessionRouter:
    """Claim a batch and launch one isolated session for each claimed card."""

    def __init__(
        self,
        claim_gate: ClaimGate,
        launcher: SessionLauncher,
        *,
        worktree_root: Path,
        agent_prefix: str = "pi-card",
        identity_token: Callable[[], str] = lambda: uuid.uuid4().hex[:12],
    ) -> None:
        """Bind one claim gate and launcher without performing any action."""
        if not _AGENT_RE.fullmatch(agent_prefix):
            raise ValueError("agent_prefix must be a canonical agent name")
        self.claim_gate = claim_gate
        self.launcher = launcher
        self.worktree_root = Path(worktree_root)
        self.agent_prefix = agent_prefix
        self._identity_token = identity_token

    @classmethod
    def from_cardstore(
        cls,
        home: Path,
        launcher: SessionLauncher,
        *,
        worktree_root: Path,
        agent_prefix: str = "pi-card",
        identity_token: Callable[[], str] = lambda: uuid.uuid4().hex[:12],
    ) -> "CardSessionRouter":
        """Build the production router over SKCoord's authoritative claim gate.

        SKCoord is an optional sibling, so importing it is delayed until this
        explicit production constructor is called. ``Board.claim_task`` folds
        CardStore state under its mutation locks and mirrors the accepted claim
        back into CardStore. Display projections are never accepted here.
        """
        from skcoord.coordination import Board

        return cls(
            Board(Path(home)),
            launcher,
            worktree_root=worktree_root,
            agent_prefix=agent_prefix,
            identity_token=identity_token,
        )

    def _assignment(self, route: CardRoute) -> SessionAssignment:
        """Derive one collision-resistant assignment without mutating state."""
        card_id = route.card_id.strip()
        if not card_id or not _AGENT_RE.fullmatch(card_id):
            raise ValueError(f"card_id {route.card_id!r} is not a canonical identifier")
        token = self._identity_token().strip()
        if not token or not re.fullmatch(r"[A-Za-z0-9]+", token):
            raise ValueError("identity token must contain only ASCII letters and digits")
        identity = f"{self.agent_prefix}-{card_id}-{token}"
        if not _AGENT_RE.fullmatch(identity):
            raise ValueError("derived agent identity exceeds the canonical name contract")
        return SessionAssignment(
            card_id=card_id,
            repo=route.repo,
            base_branch=route.base_branch,
            prompt=route.prompt,
            agent_identity=identity,
            worktree=str(self.worktree_root / identity),
            branch=f"skcode/{identity}",
        )

    @staticmethod
    def _require_distinct(assignments: Sequence[SessionAssignment]) -> None:
        """Reject any duplicate reserved dimension before the first claim."""
        dimensions = {
            "card": [item.card_id for item in assignments],
            "agent identity": [item.agent_identity for item in assignments],
            "worktree": [item.worktree for item in assignments],
            "branch": [item.branch for item in assignments],
        }
        duplicates = [
            name for name, values in dimensions.items() if len(values) != len(set(values))
        ]
        if duplicates:
            raise CardRouteError("routing batch has duplicate " + ", ".join(duplicates))

    @staticmethod
    def _validate_receipt(
        assignment: SessionAssignment,
        receipt: LaunchReceipt,
        seen_session_ids: set[str],
    ) -> None:
        """Reject a launcher that substitutes or reuses an assignment fact."""
        expected = (
            assignment.card_id,
            assignment.agent_identity,
            assignment.worktree,
            assignment.branch,
        )
        actual = (
            receipt.card_id,
            receipt.agent_identity,
            receipt.worktree,
            receipt.branch,
        )
        if actual != expected:
            raise LaunchInvariantError("launcher did not honor the governed assignment")
        if not receipt.session_id or receipt.session_id in seen_session_ids:
            raise LaunchInvariantError("launcher returned an empty or duplicate session identity")

    async def route(self, routes: Sequence[CardRoute]) -> list[RoutedSession]:
        """Claim all cards through SKCoord, then launch one session per claim.

        ``routes`` must come from an authoritative eligibility read. This method
        deliberately accepts no board display payload. Eligibility, ownership,
        dependency, review, and human gates are enforced again by ``claim_task``.
        """
        assignments = [self._assignment(route) for route in routes]
        self._require_distinct(assignments)
        claimed: list[SessionAssignment] = []
        failed_assignment: SessionAssignment | None = None
        try:
            for assignment in assignments:
                failed_assignment = assignment
                self.claim_gate.claim_task(
                    assignment.agent_identity,
                    assignment.card_id,
                )
                claimed.append(assignment)
        except Exception as exc:
            for assignment in reversed(claimed):
                self.claim_gate.release_claim(
                    assignment.agent_identity,
                    assignment.card_id,
                    actor=assignment.agent_identity,
                )
            raise ClaimRejectedError(
                "authoritative claim gate refused card "
                f"{failed_assignment.card_id if failed_assignment else 'unknown'}"
            ) from exc

        routed: list[RoutedSession] = []
        seen_session_ids: set[str] = set()
        for assignment in assignments:
            receipt = await self.launcher(assignment)
            self._validate_receipt(assignment, receipt, seen_session_ids)
            seen_session_ids.add(receipt.session_id)
            routed.append(RoutedSession(assignment=assignment, receipt=receipt))
        return routed


__all__ = [
    "CardRoute",
    "CardRouteError",
    "CardSessionRouter",
    "ClaimRejectedError",
    "LaunchInvariantError",
    "LaunchReceipt",
    "RoutedSession",
    "SessionAssignment",
]
