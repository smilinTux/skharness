"""resolver.answer - the single shared front-door body for numbered decisions.

Both doors (the `skos autopilot answer N` CLI now, the Telegram intercept in
v1.5) converge here. It reads the per-day digest manifest, finds (qid,
source_ref) for ordinal n, records the answer, transitions the GTD decision
item via the gtd_ingest port (upsert reconciles by (source, source_ref)), marks
the manifest answered.

Binding (spec `2026-08-13-unified-consent-plane-arch.md` section 3.2 + 4, card
`78409fc0`): the manifest renumbers on every rebuild, so a reply carrying only
`n` can silently resolve to a DIFFERENT item than the one actually shown --
the same defect class as applying a Terraform plan that drifted after it was
saved. The fix mirrors three external practices named in the card:

- **Terraform Cloud**: a plan that changed is stale and must be re-approved.
  Every call to `answer` must also pass the `generation` hash of the digest
  the human was looking at (`digest.generation_hash`); if the manifest has
  since been rebuilt into a different generation, the WHOLE digest is treated
  as stale (not just the one item) and the call is refused with
  `StaleGeneration`, never silently reinterpreted against today's `n`.
- **AWS Step Functions**: a task token is single-use; reusing it is an
  explicit ERROR, never a silent no-op. `~/.skcapstone` is Syncthing-synced,
  so two nodes could each believe they answered first -- a second use of an
  already-answered decision raises `AlreadyAnswered` instead of returning a
  success-shaped "idempotent" result.
- **CodePipeline**: approvals expire, and a timeout routes to an explicit
  state, never a silent drop. An answer against an expired decision raises
  `DecisionExpired`, and the decision is written to the store as explicitly
  EXPIRED (not left ambiguous between "pending" and "answered").

`n` remains in the API purely as a display convenience: once `generation` is
confirmed to match, the manifest's (n -> qid) mapping is guaranteed to be
exactly what was shown, because `generation` is a hash over that whole
mapping. The stable identity of a decision is always `qid`, which survives
every rebuild (`digest.queue_decision` derives it once at capture time).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from skos import gtd_ingest


class UnknownDecision(ValueError):
    """No manifest, or no decision numbered n in today's digest."""


class DecisionRefused(ValueError):
    """Base for an answer refused because its binding no longer holds.

    Distinct from `UnknownDecision` (which means "there was never such a
    decision"): these mean "there WAS such a decision, but this answer can no
    longer be safely applied to it," and are always refused explicitly rather
    than silently accepted or silently dropped.
    """


class StaleGeneration(DecisionRefused):
    """The digest was rebuilt since this decision was presented.

    Terraform-style: the whole digest the human looked at is what was
    "shown", not each line item independently, so any drift voids the lot.
    The caller must re-present a fresh digest before the human can answer.
    """


class AlreadyAnswered(DecisionRefused):
    """This decision was already resolved; a second use is refused, not
    reapplied (Step Functions task-token semantics)."""


class DecisionExpired(DecisionRefused):
    """This decision's approval window has closed (CodePipeline semantics).
    The store now carries an explicit EXPIRED state for it."""


def _manifest_path() -> Path:
    return gtd_ingest.gtd_dir() / "autopilot-digest.json"


def _load_manifest() -> dict:
    p = _manifest_path()
    if not p.exists():
        raise UnknownDecision("no autopilot-digest.json manifest present")
    return json.loads(p.read_text(encoding="utf-8"))


def _save_manifest(m: dict) -> None:
    _manifest_path().write_text(
        json.dumps(m, indent=2, ensure_ascii=False), encoding="utf-8")


def current_generation() -> str | None:
    """The generation hash of today's manifest as it stands right now.

    A presenter (digest send, CLI listing) reads this alongside `n` and hands
    both back to `answer`; a caller with no fresher copy has nothing valid to
    bind to and must not guess.
    """
    try:
        m = _load_manifest()
    except UnknownDecision:
        return None
    return m.get("generation")


def _is_expired(expires_at: str | None) -> bool:
    if not expires_at:
        return False
    try:
        dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return datetime.now(timezone.utc) >= dt


def _record_expired(qid: str, source_ref: str, prompt, options) -> None:
    """Transition the GTD decision item to an explicit EXPIRED state.

    Reuses the same upsert-to-done path `answer` uses for a real answer, so
    expiry is a first-class terminal outcome in the store, not an absence of
    one. `answered` stays False and `answer` stays None so an expired item is
    never mistaken for a resolved one in the archive.
    """
    cap = gtd_ingest.GtdCapture(
        text=prompt or "", source="autopilot", source_ref=source_ref,
        status="done", context="@decide", priority="high",
        meta={"decision": {"qid": qid, "prompt": prompt, "options": options,
                           "answered": False, "answer": None, "status": "expired",
                           "expired_at": datetime.now(timezone.utc).isoformat()}})
    gtd_ingest.upsert(cap)


def answer(n: int, generation: str, response: str | None = None) -> dict:
    """Resolve a numbered decision from the digest manifest.

    Args:
        n: The ordinal number from the digest. Presentation only: it is
            trusted to mean what it meant at presentation time ONLY because
            `generation` is checked first.
        generation: The manifest `generation` hash the human was shown
            alongside `n` (spec section 3.2). Required, not optional: an
            answer with no generation to bind to has nothing safe to resolve
            against and must not be accepted.
        response: The answer text (optional).

    Returns:
        A dict with keys: n, qid, source_ref, answer, answered, action_ref,
        gtd_action=(updated|completed).

    Raises:
        UnknownDecision: If manifest is missing or n not found.
        StaleGeneration: If the manifest has been rebuilt since presentation.
        AlreadyAnswered: If this decision was already resolved.
        DecisionExpired: If this decision's window has closed.
    """
    m = _load_manifest()

    live_generation = m.get("generation")
    if live_generation != generation:
        raise StaleGeneration(
            f"the digest shown for decision {n} is stale: it was generation "
            f"{generation!r}, today's digest is now {live_generation!r} "
            f"(digest_date={m.get('digest_date')!r}). The manifest was "
            "rebuilt since this decision was presented -- fetch a fresh "
            "digest and re-present before answering; the reply cannot be "
            "safely resolved against whatever now sits at that number.")

    entry = next((it for it in m.get("items", []) if it.get("n") == n), None)
    if entry is None:
        raise UnknownDecision(f"no decision numbered {n} in today's digest")

    qid = entry["qid"]
    source_ref = entry["source_ref"]

    if entry.get("answered"):
        raise AlreadyAnswered(
            f"decision {qid} (n={n}) was already answered "
            f"{'at ' + entry['answered_at'] + ' ' if entry.get('answered_at') else ''}"
            f"with {entry.get('answer')!r}; a second use is refused, not "
            "silently reapplied.")

    if _is_expired(entry.get("expires_at")):
        entry["status"] = "expired"
        _save_manifest(m)
        _record_expired(qid, source_ref, entry.get("prompt"), entry.get("options"))
        raise DecisionExpired(
            f"decision {qid} (n={n}) expired at {entry.get('expires_at')} and "
            "can no longer be answered; it has been recorded EXPIRED in the "
            "store. A new decision must be queued if it still needs a human.")

    result: dict = {"n": n, "qid": qid, "source_ref": source_ref,
                    "answer": response, "answered": True,
                    "action_ref": entry.get("action_ref")}

    answered_at = datetime.now(timezone.utc).isoformat()
    cap = gtd_ingest.GtdCapture(
        text=entry.get("prompt", ""), source="autopilot", source_ref=source_ref,
        status="done", context="@decide", priority="high",
        meta={"decision": {"qid": qid, "prompt": entry.get("prompt"),
                           "options": entry.get("options", {}),
                           "answered": True, "answer": response, "status": "answered",
                           "action_ref": entry.get("action_ref"),
                           "answered_at": answered_at}})
    _gid, action = gtd_ingest.upsert(cap)
    result["gtd_action"] = action

    entry["answered"] = True
    entry["answer"] = response
    entry["answered_at"] = answered_at
    entry["status"] = "answered"
    _save_manifest(m)
    return result
