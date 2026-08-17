"""Cross-run failure memory: the READ half (helpful forgetting).

skcoord writes one distilled entry per terminal non-pass to
``meta.autopilot.attempts[]``; this module turns that array into the small block
of prior context a fresh harness session is allowed to see, or ``None``.

The forgetting is the feature. Remembering everything is what makes a card bloat
into an unreadable brief, and a stale flake would then haunt every future run. So
the reader dedupes, keeps only the last few DISTINCT failures, distills each to
one consequence line, and hard-caps the block. Raw detail (diffs, tracebacks, test
output) is never inlined; it stays in the run journal, which ``run_id`` points at.

Storage cap (skcoord, 10) is a corruption guard. The bound here is the policy.
"""
from __future__ import annotations

import re

# Forgetting policy knobs. Distinct failures reaching a prompt, and the hard
# ceiling on the rendered block (spec rules 3 and 5).
MAX_ATTEMPTS = 3
MAX_CHARS = 600

# S21 (card 53b8c8be), vector 2: every line under this header is model-derived
# text, distilled from ONE previous run's grader notes by `distill_failure`. It
# crosses attempts: a failing worker seeds the next worker's round one. That is
# the same mechanism the 2026-08-16 red-team accepted as decisive against the
# exploration slice (card f81d8d2d), where a weak model's misdiagnosis would have
# corrupted the control arm through the card's failure memory.
#
# The DECISION recorded here: keep the channel (a card that rebuilds into the
# same wall every run is the failure it exists to prevent), keep its existing
# bounds (3 distinct entries, 600 chars, one distilled line each, dedup on cause,
# and a journal pointer attributing the newest entry to its run), and fix the
# missing piece, which was epistemic status. The block used to read as an
# established finding. It now says what it is: an unverified report from a run
# that FAILED, to be checked rather than believed. The prefix is unchanged so the
# existing wiring tests keep matching on it.
_HEADER = ("Prior attempts on this card (distilled, UNVERIFIED: these are "
           "reports from earlier runs that FAILED, not findings, so verify each "
           "against the code before acting on it):")


# Longest a distilled cause may be. Well under the block ceiling so three of them
# plus the header still fit without truncation in the common case.
MAX_CAUSE_CHARS = 180

# A pytest-shaped failure line is worth far more to the next round than the prose
# around it: it names the exact test and the exact assertion that broke.
_SIGNAL = (
    re.compile(r"^\s*(?:FAILED|ERROR)\s+\S+", re.IGNORECASE),   # FAILED path::test - msg
    re.compile(r"\b\S*::\S*test\S*"),                           # a node id anywhere
    re.compile(r"^\s*E\s{2,}\S"),                               # pytest assertion detail
    re.compile(r"\bassert\b"),
)
# Traceback frames are the noise this is meant to strip: they are long, they are
# rarely actionable one round later, and the raw copy lives in the run journal.
_NOISE = (
    re.compile(r"^\s*File\s+\"", re.IGNORECASE),
    re.compile(r"^\s*Traceback\b", re.IGNORECASE),
)


def distill_failure(notes, limit: int = MAX_CAUSE_CHARS) -> str:
    """Reduce grader/CI notes to ONE bounded line naming the actual cause.

    Called at the write site, because only the call site knows what kind of
    failure it is holding. Prefers a pytest-shaped signal line (a node id, a
    FAILED line, an assertion) and falls back to the first meaningful line.
    Traceback frames are dropped: they are bulk, not cause, and the raw text
    stays in the run journal.
    """
    lines = [" ".join(ln.split()) for ln in str(notes or "").splitlines()]
    lines = [ln for ln in lines if ln and not any(n.search(ln) for n in _NOISE)]
    if not lines:
        return "no grader detail was returned"
    for pattern in _SIGNAL:
        for ln in lines:
            if pattern.search(ln):
                return ln[:limit].rstrip()
    return lines[0][:limit].rstrip()


def _attempts(payload) -> list[dict]:
    """Tolerant reader: any shape that is not a real attempts list reads as empty.

    The field is additive across a Syncthing fleet, so cards predating it (and
    thin payloads that never carried ``meta``) must behave exactly as before
    rather than raise.
    """
    if not isinstance(payload, dict):
        return []
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        return []
    ap = meta.get("autopilot")
    if not isinstance(ap, dict):
        return []
    raw = ap.get("attempts")
    if not isinstance(raw, list):
        return []
    return [e for e in raw if isinstance(e, dict)]


def _one_line(text) -> str:
    """Collapse any entry field to a single line. A call site is supposed to
    distill before writing; this keeps one sloppy caller from breaking the block
    shape (the char ceiling is the other backstop)."""
    return " ".join(str(text or "").split())


def _distinct_newest(attempts: list[dict]) -> list[dict]:
    """Dedup on (outcome, why_failed casefolded), keeping the NEWEST entry per
    key, then return the last MAX_ATTEMPTS distinct failures in ts order.

    Sorted by ``ts`` with the stored order as the tiebreak, so entries that share
    a timestamp stay in the order they were recorded.
    """
    ordered = sorted(enumerate(attempts),
                     key=lambda p: (str(p[1].get("ts") or ""), p[0]))
    seen: set[tuple[str, str]] = set()
    kept: list[tuple[int, dict]] = []
    for pos, entry in reversed(ordered):            # newest first: first seen wins
        key = (str(entry.get("outcome") or ""),
               _one_line(entry.get("why_failed")).strip().lower())
        if key in seen:
            continue
        seen.add(key)
        kept.append((pos, entry))
        if len(kept) >= MAX_ATTEMPTS:
            break
    kept.sort(key=lambda p: (str(p[1].get("ts") or ""), p[0]))   # back to oldest-first
    return [entry for _, entry in kept]


def _render(entry: dict) -> str:
    """One consequence line per failure. Phrased as what happened, never as an
    instruction: the agent decides what to do about it."""
    why = _one_line(entry.get("why_failed"))
    hint = _one_line(entry.get("replacement_hint"))
    if hint:
        return f"- This failed for {why}, try {hint}."
    return f"- This previously failed for {why}; avoid repeating that approach."


def build_prior_feedback(payload) -> str | None:
    """Render a card's failure memory as bounded prior context, or ``None``.

    ``payload`` is the card dict the executor already holds (``item.payload``), so
    no board re-read is needed. Returns ``None`` when there is nothing to
    remember, which is byte-identical to the fresh-start behaviour that preceded
    this feature.

    The returned block is ALWAYS at most ``MAX_CHARS``: oldest lines are dropped
    first, and a single oversized line is hard-truncated rather than allowed
    through.
    """
    attempts = _attempts(payload)
    if not attempts:
        return None
    kept = _distinct_newest(attempts)
    if not kept:
        return None

    lines = [_render(e) for e in kept]
    pointer = ""
    newest_run = _one_line(kept[-1].get("run_id"))
    if newest_run:
        pointer = f"Raw history: autopilot/runs/{newest_run}"

    # Drop OLDEST lines until the block fits; the newest failure is the one most
    # worth carrying, and the pointer stays so the raw detail remains reachable.
    while lines:
        block = "\n".join([_HEADER, *lines] + ([pointer] if pointer else []))
        if len(block) <= MAX_CHARS:
            return block
        if len(lines) == 1:
            break
        lines.pop(0)
    return block[:MAX_CHARS]
