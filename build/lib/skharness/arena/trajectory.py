"""Bounded Pi trajectories and evidence-driven model selection.

The policy is intentionally small and deterministic.  It is not an autonomous model
chooser: operators feed it fixed S/M/L trial evidence and it refuses to route when a
size has not been qualified.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class CardSize(str, Enum):
    SMALL = "S"
    MEDIUM = "M"
    LARGE = "L"


@dataclass(frozen=True)
class PhaseBudget:
    assess_s: int
    inspect_s: int
    build_s: int
    test_s: int

    def __post_init__(self) -> None:
        if min(self.assess_s, self.inspect_s, self.build_s, self.test_s) <= 0:
            raise ValueError("every Pi phase budget must be positive")

    @property
    def total_s(self) -> int:
        return self.assess_s + self.inspect_s + self.build_s + self.test_s

    def prompt_contract(self) -> str:
        return (
            "Bound this run by phase: "
            f"assess {self.assess_s}s, inspect {self.inspect_s}s, "
            f"build {self.build_s}s, test {self.test_s}s. "
            "Move on when a phase budget expires; report the unfinished phase honestly."
        )


DEFAULT_PHASE_BUDGETS = {
    CardSize.SMALL: PhaseBudget(30, 90, 240, 180),
    CardSize.MEDIUM: PhaseBudget(60, 180, 420, 240),
    CardSize.LARGE: PhaseBudget(90, 300, 720, 390),
}


@dataclass(frozen=True)
class ModelTrial:
    model: str
    card_size: CardSize
    successful: bool
    duration_s: float
    time_to_first_edit_s: float | None


class UnqualifiedRouteError(RuntimeError):
    """No fixed-trial evidence supports a safe route."""


class EvidenceModelRouter:
    """Choose the best qualified model from fixed S/M/L trials.

    A model is eligible only after at least one trial for every size.  For the target
    size, successful trials sort ahead of failures, then by completion duration and
    time-to-first-edit.  This keeps the policy auditable and prevents anecdotal runs
    from silently changing production routing.
    """

    def __init__(self, trials: Iterable[ModelTrial]) -> None:
        self.trials = tuple(trials)

    def route(self, card_size: CardSize) -> str:
        models = sorted({trial.model for trial in self.trials})
        qualified = [
            model
            for model in models
            if all(
                any(t.model == model and t.card_size is size for t in self.trials)
                for size in CardSize
            )
        ]
        candidates = [t for t in self.trials if t.model in qualified and t.card_size is card_size]
        successes = [t for t in candidates if t.successful]
        if not successes:
            raise UnqualifiedRouteError(f"no successful fixed {card_size.value} trial")
        winner = min(
            successes,
            key=lambda t: (
                t.duration_s,
                t.time_to_first_edit_s if t.time_to_first_edit_s is not None else float("inf"),
                t.model,
            ),
        )
        return winner.model


def compact_pi_events(content: bytes, *, max_event_bytes: int = 16_384) -> bytes:
    """Deduplicate Pi JSON envelopes and bound large tool payloads.

    Raw logs remain in the attempt directory for local diagnosis.  Only the durable,
    content-addressed evidence copy is compacted.  Non-JSON process output is retained
    (bounded per line) so supervisor failures do not disappear.
    """
    if max_event_bytes < 256:
        raise ValueError("max_event_bytes must be at least 256")
    result: list[bytes] = []
    saw_json = False
    previous: bytes | None = None
    duplicates = 0
    for raw in content.splitlines():
        line = raw
        try:
            event = json.loads(raw)
            saw_json = True
            line = json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
        if len(line) > max_event_bytes:
            digest = __import__("hashlib").sha256(line).hexdigest()
            line = json.dumps(
                {"type": "evidence_truncated", "bytes": len(line), "sha256": digest},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        if line == previous:
            duplicates += 1
            continue
        if duplicates:
            result.append(json.dumps({"type": "duplicate_events", "count": duplicates}).encode())
            duplicates = 0
        result.append(line)
        previous = line
    if duplicates:
        result.append(json.dumps({"type": "duplicate_events", "count": duplicates}).encode())
    if not saw_json and all(len(line) <= max_event_bytes for line in content.splitlines()):
        return content
    return b"\n".join(result) + (b"\n" if result else b"")
