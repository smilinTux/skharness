from skharness.arena.worker_recovery import (
    AppendOnlyEventStore, TransportReceipt, WorkerProjection, WorkerRecovery,
)


def test_http_400_releases_exact_claim_and_records_separate_receipt(tmp_path):
    store = AppendOnlyEventStore(tmp_path / "events.jsonl")
    recovery = WorkerRecovery(store)
    released = []
    result = recovery.provider_failure(
        TransportReceipt("card", "owner", 7, "lane-a", "http_400", 400),
        WorkerProjection("card", "worker", "running"),
        release_claim=lambda card, rev: released.append((card, rev)) or True,
        candidate=b"immutable candidate",
    )
    assert released == [("card", 7)]
    assert result.retry_allowed
    events = store.read()
    assert [e["type"] for e in events] == ["transport_receipt", "worker_projection"]
    assert events[0]["card_id"] == "card"
    assert events[0]["owner"] == "owner"
    assert events[0]["claim_revision"] == 7
    assert events[0]["model_lane"] == "lane-a"
    assert events[0]["response_class"] == "http_400"
    assert events[1]["state"] == "terminal"


def test_retry_is_bounded_and_side_effectful_never_replayed(tmp_path):
    recovery = WorkerRecovery(AppendOnlyEventStore(tmp_path / "events.jsonl"))
    receipt = TransportReceipt("card", "owner", 1, "lane", "http_400", 400)
    projection = WorkerProjection("card", "worker", "running")
    release = lambda *_: True
    assert recovery.provider_failure(receipt, projection, release_claim=release).retry_allowed
    assert not recovery.provider_failure(receipt, projection, release_claim=release).retry_allowed
    assert not recovery.provider_failure(receipt, projection, release_claim=release, side_effectful=True).retry_allowed


def test_failed_review_preserves_candidate_digest_and_becomes_reviewable(tmp_path):
    store = AppendOnlyEventStore(tmp_path / "events.jsonl")
    projection = WorkerRecovery(store).review_failure("card", b"candidate", "reviewer")
    assert projection.state == "reviewable"
    assert projection.candidate_sha256
