"""Reproducible continual-loop evaluation and slow-training promotion gates."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from statistics import mean
from typing import Iterable


class EvaluationError(ValueError):
    """The evaluation evidence is incomplete or crosses an isolation boundary."""


class Variant(StrEnum):
    BASELINE = "baseline"
    MEMORY = "memory-only"
    SKILLS = "skills-only"
    SUBAGENTS = "subagents-only"
    FULL = "full-loop"


class Plane(StrEnum):
    TRAIN = "train"
    EVAL = "eval"
    PROD = "prod"


@dataclass(frozen=True)
class Trial:
    variant: Variant
    seed: int
    repetition: int
    quality: float
    cost_usd: float
    tokens: int
    latency_ms: float
    recovered: bool
    reward_hacks: int = 0
    verifier_disagreements: int = 0


def evaluation_report(trials: Iterable[Trial], *, seeds: tuple[int, ...], repetitions: int) -> dict:
    """Aggregate a complete fixed-seed ablation matrix or refuse partial evidence."""
    rows = tuple(trials)
    if not seeds or repetitions < 1:
        raise EvaluationError("fixed seeds and a positive repetition count are required")
    expected = {
        (variant, seed, repetition)
        for variant in Variant
        for seed in seeds
        for repetition in range(repetitions)
    }
    observed = [(row.variant, row.seed, row.repetition) for row in rows]
    if len(observed) != len(set(observed)):
        raise EvaluationError("duplicate variant/seed/repetition trial")
    missing = expected - set(observed)
    extra = set(observed) - expected
    if missing or extra:
        raise EvaluationError(f"incomplete evaluation matrix: missing={len(missing)} extra={len(extra)}")

    variants = {}
    for variant in Variant:
        group = [row for row in rows if row.variant == variant]
        variants[variant.value] = {
            "trials": len(group),
            "quality_mean": mean(row.quality for row in group),
            "cost_usd_mean": mean(row.cost_usd for row in group),
            "tokens_mean": mean(row.tokens for row in group),
            "latency_ms_mean": mean(row.latency_ms for row in group),
            "recovery_rate": mean(int(row.recovered) for row in group),
            "reward_hacks": sum(row.reward_hacks for row in group),
            "verifier_disagreements": sum(row.verifier_disagreements for row in group),
        }
    return {"schema": "skharness.evaluation-matrix.v1", "seeds": list(seeds),
            "repetitions": repetitions, "variants": variants}


@dataclass(frozen=True)
class TraceStep:
    kind: str
    token_ids: tuple[int, ...]
    logprobs: tuple[float, ...]
    sampling: dict
    model: str


def export_rl_trace(steps: Iterable[TraceStep]) -> dict:
    """Curate exact traces; only assistant/action tokens are RL training targets."""
    records = []
    for step in steps:
        if step.kind not in {"assistant", "action", "observation", "tool-response"}:
            raise EvaluationError(f"unknown trace boundary: {step.kind}")
        if len(step.token_ids) != len(step.logprobs) or not step.model or not step.sampling:
            raise EvaluationError("trace requires aligned token IDs/logprobs, model, and sampling")
        record = asdict(step)
        record["training_use"] = "rl" if step.kind in {"assistant", "action"} else "world-model-sft"
        records.append(record)
    if not records:
        raise EvaluationError("empty trace export")
    return {"schema": "skharness.rl-trace.v1", "steps": records}


@dataclass(frozen=True)
class PlaneBinding:
    plane: Plane
    dataset_id: str
    credential_id: str


def authorize_training_promotion(
    bindings: Iterable[PlaneBinding], *, held_out_regression_passed: bool,
    canary_passed: bool, explicit_approval: str | None, reward_hacks: int,
) -> dict:
    """Fail closed unless data/credentials are disjoint and every slow gate passed."""
    rows = tuple(bindings)
    if {row.plane for row in rows} != set(Plane):
        raise EvaluationError("train, eval, and prod bindings are all required")
    if len({row.dataset_id for row in rows}) != len(rows):
        raise EvaluationError("train/eval/prod datasets must be disjoint")
    if len({row.credential_id for row in rows}) != len(rows):
        raise EvaluationError("train/eval/prod credentials must be disjoint")
    if reward_hacks:
        raise EvaluationError("reward-hack observations block promotion")
    if not held_out_regression_passed:
        raise EvaluationError("held-out regression must pass")
    if not canary_passed:
        raise EvaluationError("canary must pass")
    if not explicit_approval:
        raise EvaluationError("explicit promotion approval is required")
    return {"authorized": True, "approval": explicit_approval,
            "planes": {row.plane.value: {"dataset_id": row.dataset_id,
                                         "credential_id": row.credential_id} for row in rows}}
