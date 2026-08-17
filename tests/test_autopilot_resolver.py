"""Tests for skharness.autocode.resolver.answer (stable-id decision resolver).

Card 78409fc0 / spec `2026-08-13-unified-consent-plane-arch.md` section 3.2:
`answer` binds to (n, generation) rather than n alone, so a reply against a
manifest that has since been rebuilt is refused rather than silently
resolving to whatever now sits at that position.
"""
import json

import pytest

# skos is an optional sibling, not a declared skharness dependency (same policy as
# the needs_skcapstone hook in conftest.py). Its absence used to be a COLLECTION
# error, which took the WHOLE suite down in any clean environment. Skip instead.
#
# This guard must precede the resolver import: skharness.autocode.resolver itself
# imports skos at module level, so importing it first reintroduces the error.
gtd_ingest = pytest.importorskip("skos.gtd_ingest")

from skharness.autocode import digest, resolver  # noqa: E402  (must follow the guard above)


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("SK_GTD_DIR", str(tmp_path / "gtd"))
    yield


def _seed(prompt="Merge PR #123 for skrender task?", answered=False,
          qid="q1", expires_at="2099-01-01T00:00:00+00:00"):
    # the decision as it lives in the GTD store (source="autopilot")
    gtd_ingest.capture(gtd_ingest.GtdCapture(
        text=prompt, source="autopilot", source_ref=f"autopilot:{qid}",
        status="waiting", context="@decide", priority="high",
        meta={"decision": {"qid": qid, "prompt": prompt, "options": {},
                           "answered": answered, "answer": None, "action_ref": None,
                           "expires_at": expires_at, "status": "pending"}}))
    # the per-day manifest that assigns the ordinal, built the same way the real
    # digest is (so generation is computed correctly rather than hand-faked)
    item = {"n": 1, "qid": qid, "id": "x", "source_ref": f"autopilot:{qid}",
            "prompt": prompt, "options": {},
            "content_hash": digest.content_hash(qid, prompt, {}),
            "expires_at": expires_at, "answered": answered}
    manifest = {"digest_date": "2026-07-12", "sent_at": None,
                "generation": digest.generation_hash([item]), "items": [item]}
    (gtd_ingest.gtd_dir() / "autopilot-digest.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    return manifest["generation"]


def test_answer_resolves_and_transitions():
    gen = _seed()
    out = resolver.answer(1, gen, "yes")
    assert out["n"] == 1 and out["qid"] == "q1" and out["answer"] == "yes"
    assert out["answered"] is True and out["gtd_action"] in ("updated", "completed")
    # decision item moved to done -> archive.json, marked answered in its meta
    arch = json.loads((gtd_ingest.gtd_dir() / "archive.json").read_text())
    assert len(arch) == 1 and arch[0]["decision"]["answered"] is True
    assert arch[0]["decision"]["answer"] == "yes"
    # manifest entry marked answered
    m = json.loads((gtd_ingest.gtd_dir() / "autopilot-digest.json").read_text())
    assert m["items"][0]["answered"] is True


def test_second_use_is_an_explicit_error_not_a_silent_noop():
    gen = _seed()
    resolver.answer(1, gen, "yes")
    with pytest.raises(resolver.AlreadyAnswered):
        resolver.answer(1, gen, "yes")               # same reply again: refused
    arch = json.loads((gtd_ingest.gtd_dir() / "archive.json").read_text())
    assert len(arch) == 1                             # not duplicated


def test_unknown_n_raises():
    gen = _seed()
    with pytest.raises(resolver.UnknownDecision):
        resolver.answer(99, gen)


def test_missing_manifest_raises():
    with pytest.raises(resolver.UnknownDecision):
        resolver.answer(1, "any-generation")


def test_stale_generation_is_refused_even_though_n_still_resolves():
    """A wrong generation is refused outright, independent of whether n
    happens to still exist in the current manifest."""
    gen = _seed()
    with pytest.raises(resolver.StaleGeneration):
        resolver.answer(1, gen + "-drifted", "yes")
    # nothing was written: the decision is still open
    m = json.loads((gtd_ingest.gtd_dir() / "autopilot-digest.json").read_text())
    assert m["items"][0]["answered"] is False


def test_renumbered_manifest_refuses_stale_answer_instead_of_wrong_item():
    """The acceptance test for card 78409fc0's central defect.

    A human is shown a digest where n=1 is "Merge PR #123" (qid q1). Before
    they reply, the manifest is rebuilt: q1 is now unanswered but sorts
    SECOND, and an unrelated higher-priority decision (qid q2, "Approve
    refund?") takes position n=1 instead. A stale reply of "1" carrying the
    ORIGINAL generation must never be silently applied to q2 -- it must be
    refused, and q2 (never shown, never consented to) must remain untouched.
    """
    original_prompt = "Merge PR #123 for skrender task?"
    gen_shown = _seed(prompt=original_prompt, qid="q1")

    # Rebuild the manifest as a real rebuild would: q1 still open (medium
    # priority) but a new critical-priority item q2 now outranks it, so q1
    # is bumped to n=2 and q2 lands at n=1 -- the exact renumbering defect.
    store_items = [
        {"id": "y", "source": "autopilot", "source_ref": "autopilot:q2",
         "priority": "critical", "created_at": "2026-07-12T00:00:00Z",
         "decision": {"qid": "q2", "prompt": "Approve refund?", "options": {},
                     "answered": False, "expires_at": "2099-01-01T00:00:00+00:00"}},
        {"id": "x", "source": "autopilot", "source_ref": "autopilot:q1",
         "priority": "medium", "created_at": "2026-07-12T01:00:00Z",
         "decision": {"qid": "q1", "prompt": original_prompt, "options": {},
                     "answered": False, "expires_at": "2099-01-01T00:00:00+00:00"}},
    ]
    rebuilt = digest.build_manifest(store_items, digest_date="2026-07-12")
    digest.write_manifest(rebuilt)
    assert [(it["n"], it["qid"]) for it in rebuilt["items"]] == [(1, "q2"), (2, "q1")]
    assert rebuilt["generation"] != gen_shown          # composition changed -> new plan

    # The stale reply: "1" against the OLD generation. Position 1 now means
    # q2 ("Approve refund?"), not the q1 PR merge the human actually saw.
    with pytest.raises(resolver.StaleGeneration):
        resolver.answer(1, gen_shown, "yes")

    # Prove nothing resolved to the wrong item: q2 was never touched.
    m = json.loads((gtd_ingest.gtd_dir() / "autopilot-digest.json").read_text())
    q2_entry = next(it for it in m["items"] if it["qid"] == "q2")
    assert q2_entry["answered"] is False
    assert not (gtd_ingest.gtd_dir() / "archive.json").exists()

    # The correct fix is to re-present the CURRENT generation and answer by
    # the id/position it actually holds now (q1 is at n=2 in the fresh plan).
    out = resolver.answer(2, rebuilt["generation"], "yes")
    assert out["qid"] == "q1"                          # resolves to the RIGHT item
    arch = json.loads((gtd_ingest.gtd_dir() / "archive.json").read_text())
    assert len(arch) == 1 and arch[0]["decision"]["qid"] == "q1"


def test_expired_decision_is_refused_and_recorded_explicitly():
    past = "2000-01-01T00:00:00+00:00"
    gen = _seed(expires_at=past)
    with pytest.raises(resolver.DecisionExpired):
        resolver.answer(1, gen, "yes")
    # explicit EXPIRED state, not a silent drop: recorded in the store...
    arch = json.loads((gtd_ingest.gtd_dir() / "archive.json").read_text())
    assert len(arch) == 1
    assert arch[0]["decision"]["status"] == "expired"
    assert arch[0]["decision"]["answered"] is False    # expired != answered
    # ...and reflected on the manifest entry too.
    m = json.loads((gtd_ingest.gtd_dir() / "autopilot-digest.json").read_text())
    assert m["items"][0]["status"] == "expired"


def test_expired_item_does_not_recirculate_into_the_next_digest():
    past = "2000-01-01T00:00:00+00:00"
    gen = _seed(expires_at=past)
    with pytest.raises(resolver.DecisionExpired):
        resolver.answer(1, gen, "yes")
    rebuilt = digest.rebuild_manifest()
    assert rebuilt["items"] == []                      # not silently re-offered forever
