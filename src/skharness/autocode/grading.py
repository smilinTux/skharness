"""Joule Economy work grading: rubric text and a pure, model-free parser (P1, shadow only).

This module builds an assess-prompt rubric from the canonical vocabulary and parses a
model's grading reply back into a validated dict. It does not call a model, does not
wire into phase0_assess, and does not change any routing behaviour. See spec sections
3.1 to 3.6:
docs/superpowers/specs/2026-08-14-joule-economy-design.md (skcapstone repo).

The vocabulary is vendored at ``data/joule-grade-vocabulary.json`` (a byte-for-byte
copy of the canonical skcapstone spec file) so this module has no cross-repo runtime
dependency. Consume the JSON rather than retyping the enum definitions in Python.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_DATA_PATH = Path(__file__).parent / "data" / "joule-grade-vocabulary.json"


def _load_vocabulary(path: Path = _DATA_PATH) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


VOCABULARY: dict[str, Any] = _load_vocabulary()

SIZE_VALUES: tuple[str, ...] = tuple(VOCABULARY["size"]["values"])
RISK_VALUES: tuple[str, ...] = tuple(VOCABULARY["risk"]["values"])
SENSITIVITY_VALUES: tuple[str, ...] = tuple(VOCABULARY["sensitivity"]["values"])

SIZE_RANK: dict[str, int] = dict(VOCABULARY["size"]["ranks"])
RISK_RANK: dict[str, int] = dict(VOCABULARY["risk"]["ranks"])

# model_class = CLASS[max(size_rank, risk_rank)]; ranks are 0..3, aligned S/M/L/XL.
CLASS: tuple[str, ...] = tuple(VOCABULARY["model_class"]["values"])


def _build_rubric(vocab: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(
        f"Joule Economy work grade rubric (rubric_version {vocab.get('version', 1)})."
    )
    lines.append("")
    lines.append("Score the work on three independent axes and reply with ONLY a JSON")
    lines.append('object with exactly the keys "size", "risk", and "sensitivity".')
    lines.append("")
    size = vocab["size"]
    lines.append(f"size, {size['meaning']}:")
    for value in size["values"]:
        lines.append(f"  {value}: {size['definitions'][value]}")
    lines.append("")
    risk = vocab["risk"]
    lines.append(f"risk, {risk['meaning']}:")
    for value in risk["values"]:
        lines.append(f"  {value}: {risk['definitions'][value]}")
    if "$warning" in risk:
        lines.append(f"  WARNING: {risk['$warning']}")
    lines.append("")
    sensitivity = vocab["sensitivity"]
    lines.append(f"sensitivity, {sensitivity['meaning']}:")
    for value in sensitivity["values"]:
        lines.append(f"  {value}: {sensitivity['definitions'][value]}")
    if "$warning" in sensitivity:
        lines.append(f"  NOTE: {sensitivity['$warning']}")
    lines.append("")
    lines.append("Worked examples, size and risk resolve to a model_class floor:")
    for ex in vocab["model_class"]["worked_examples"]:
        lines.append(
            f"  size={ex['size']}, risk={ex['risk']} -> class {ex['model_class']} "
            f"({ex['note']})"
        )
    lines.append("")
    lines.append(
        "Reply with ONLY a JSON object, for example: "
        '{"size": "M", "risk": "high", "sensitivity": "internal"}'
    )
    return "\n".join(lines)


GRADE_RUBRIC: str = _build_rubric(VOCABULARY)

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

_GRADE_KEYS = ("size", "risk", "sensitivity")


def _candidate_texts(raw: str) -> list[str]:
    """All substrings of ``raw`` worth trying as a JSON payload, most likely first."""
    candidates = list(_FENCE_RE.findall(raw))
    candidates.append(raw)
    return candidates


def _try_parse_object(text: str) -> Any:
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = _OBJECT_RE.search(text)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _select_grade_source(obj: Any) -> dict | None:
    """Return the dict that actually carries size/risk/sensitivity, unwrapping a
    single ``{"grade": {...}}`` nesting if the top level does not carry them."""
    if not isinstance(obj, dict):
        return None
    top_keys = {str(k).strip().lower() for k in obj}
    if set(_GRADE_KEYS) <= top_keys:
        return obj
    nested = obj.get("grade")
    if isinstance(nested, dict):
        nested_keys = {str(k).strip().lower() for k in nested}
        if set(_GRADE_KEYS) <= nested_keys:
            return nested
    return None


def parse_grade(raw: Any) -> dict | None:
    """Pull size, risk, sensitivity out of a model's grading reply.

    Tolerates markdown fences and surrounding prose. Validates every value against
    the canonical enums (case-insensitively) and returns None rather than guessing
    when the reply is unusable or any field is missing or invalid. risk NEVER
    accepts an S/M/L/XL size label; that is enforced here by validating against
    the risk enum only, which does not contain them.

    Total: this only ever reads a str. A provider that hands back a pre-parsed
    dict, a list, bytes, a number, or None is not special-cased here (it would
    need its own decode/validate path to do so safely), so any non-str input
    returns None outright rather than raising. Raising would stop assess for
    every card in the run; None degrades exactly the one card to ungraded.
    """
    if not isinstance(raw, str):
        return None
    if not raw.strip():
        return None
    for candidate in _candidate_texts(raw):
        obj = _try_parse_object(candidate)
        source = _select_grade_source(obj)
        if source is None:
            continue
        lowered = {str(k).strip().lower(): v for k, v in source.items()}
        size_raw, risk_raw, sensitivity_raw = (
            lowered.get("size"),
            lowered.get("risk"),
            lowered.get("sensitivity"),
        )
        if size_raw is None or risk_raw is None or sensitivity_raw is None:
            continue
        size = str(size_raw).strip().upper()
        risk = str(risk_raw).strip().lower()
        sensitivity = str(sensitivity_raw).strip().lower()
        if size not in SIZE_VALUES:
            continue
        if risk not in RISK_VALUES:
            continue
        if sensitivity not in SENSITIVITY_VALUES:
            continue
        return {"size": size, "risk": risk, "sensitivity": sensitivity}
    return None


def model_class_for(size: str, risk: str) -> str:
    """model_class = CLASS[max(size_rank, risk_rank)] (spec section 3.3).

    Raises ValueError on an unknown size, an unknown risk, or a risk value that
    reuses an S/M/L/XL size label (the exact collision the two-axis design exists
    to prevent, see 3.2.1). Callers are expected to have already validated their
    inputs via parse_grade; this is a second, defensive check.
    """
    size_key = str(size).strip().upper() if size is not None else ""
    risk_key = str(risk).strip().lower() if risk is not None else ""
    if risk_key.upper() in SIZE_VALUES:
        raise ValueError(f"risk must not reuse a size label: {risk!r}")
    if size_key not in SIZE_RANK:
        raise ValueError(f"unknown size: {size!r}")
    if risk_key not in RISK_RANK:
        raise ValueError(f"unknown risk: {risk!r}")
    rank = max(SIZE_RANK[size_key], RISK_RANK[risk_key])
    return CLASS[rank]
