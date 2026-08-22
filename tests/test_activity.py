from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from skharness.activity import (
    ActivityContext,
    ActivityCorruptionError,
    ActivityJournal,
    ActivityKind,
)


def _context(**changes):
    values = {
        "session_id": "session-1",
        "run_id": "run-1",
        "agent_id": "pi-scout-1",
        "role": "scout",
        "phase": "inspect",
        "source": "swarm",
        "card_id": "card-1",
        "card_hash": "sha256:" + "1" * 64,
        "trajectory_id": "trajectory-1",
        "team_id": "team-1",
        "parent_agent_id": "orchestrator-1",
        "contract_id": "contract-1",
        "contract_hash": "sha256:" + "2" * 64,
        "plan_hash": "sha256:" + "3" * 64,
        "lease_id": "lease-1",
        "attempt_id": "1",
        "base_commit": "a" * 40,
        "evidence_id": "sha256:" + "4" * 64,
    }
    values.update(changes)
    return ActivityContext(**values)


def test_publish_assigns_durable_cursor_hash_and_observation_authority(tmp_path):
    journal = ActivityJournal(root=tmp_path, clock=lambda: 42.0)
    first = journal.publish(_context(), ActivityKind.STATUS, summary="started")
    second = journal.publish(_context(), ActivityKind.PHASE, summary="inspecting")

    assert (first.cursor, second.cursor) == (1, 2)
    assert first.event_id.startswith("sha256:")
    assert first.authority == "observation"
    assert first.published_at == 42.0
    restarted = ActivityJournal(root=tmp_path, clock=lambda: 43.0)
    assert restarted.publish(_context(), ActivityKind.STATUS).cursor == 3


def test_publish_redacts_credential_keys_and_bounds_nested_values(tmp_path):
    journal = ActivityJournal(root=tmp_path)
    event = journal.publish(
        _context(),
        ActivityKind.TOOL_CALL,
        data={
            "tool_name": "bash",
            "authorization": "Bearer do-not-store",
            "nested": {"api_key": "also-secret", "safe": "x" * 3_000},
            "total_tokens": 42,
            "access_token": "do-not-store",
            "detail": "upstream said Bearer do-not-store-inline",
        },
    )

    assert event.data["authorization"] == "[redacted]"
    assert event.data["nested"]["api_key"] == "[redacted]"
    assert event.data["total_tokens"] == 42
    assert event.data["access_token"] == "[redacted]"
    assert event.data["detail"] == "upstream said [redacted]"
    assert len(event.data["nested"]["safe"]) == 2_048
    assert "do-not-store" not in journal.path.read_text(encoding="utf-8")
    assert os.stat(journal.root).st_mode & 0o777 == 0o700
    assert os.stat(journal.path).st_mode & 0o777 == 0o600


def test_incomplete_tail_is_truncated_before_next_append(tmp_path):
    journal = ActivityJournal(root=tmp_path)
    journal.publish(_context(), ActivityKind.STATUS, summary="one")
    with journal.path.open("ab") as stream:
        stream.write(b'{"partial":')

    second = journal.publish(_context(), ActivityKind.STATUS, summary="two")

    assert second.cursor == 2
    assert [event.summary for event in journal.read_after()] == ["one", "two"]
    assert journal.path.read_bytes().endswith(b"\n")


def test_append_reads_only_the_bounded_tail_not_the_whole_journal(tmp_path, monkeypatch):
    journal = ActivityJournal(root=tmp_path)
    journal.publish(_context(), ActivityKind.STATUS, summary="first")

    def whole_file_read_forbidden(_path):
        raise AssertionError("append must not reread the whole activity journal")

    monkeypatch.setattr(type(journal.path), "read_bytes", whole_file_read_forbidden)
    assert journal.publish(_context(), ActivityKind.STATUS, summary="second").cursor == 2


def test_committed_corruption_fails_closed(tmp_path):
    journal = ActivityJournal(root=tmp_path)
    journal.publish(_context(), ActivityKind.STATUS, summary="one")
    with journal.path.open("ab") as stream:
        stream.write(b"not-json\n")

    with pytest.raises(ActivityCorruptionError):
        journal.publish(_context(), ActivityKind.STATUS, summary="two")
    with pytest.raises(ActivityCorruptionError):
        journal.read_after()


def test_content_hash_detects_committed_event_tampering(tmp_path):
    journal = ActivityJournal(root=tmp_path)
    journal.publish(_context(), ActivityKind.STATUS, summary="original")
    journal.path.write_text(
        journal.path.read_text(encoding="utf-8").replace("original", "tampered"),
        encoding="utf-8",
    )
    with pytest.raises(ActivityCorruptionError):
        journal.read_after()


def test_two_journal_instances_serialize_concurrent_writers(tmp_path):
    left = ActivityJournal(root=tmp_path)
    right = ActivityJournal(root=tmp_path)

    def publish(index):
        journal = left if index % 2 else right
        return journal.publish(
            _context(agent_id=f"agent-{index % 2}"),
            ActivityKind.STATUS,
            summary=str(index),
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(publish, range(40)))

    rows = left.read_after(limit=100)
    assert [row.cursor for row in rows] == list(range(1, 41))
    assert len({row.event_id for row in rows}) == 40


def test_replay_filters_and_reports_retention_window(tmp_path):
    journal = ActivityJournal(root=tmp_path, max_bytes=4_500, max_event_bytes=2_048)
    for index in range(20):
        journal.publish(
            _context(agent_id=f"agent-{index % 2}"),
            ActivityKind.PHASE if index % 2 else ActivityKind.STATUS,
            summary=f"event-{index}-" + "x" * 80,
        )

    window = journal.window()
    assert window["head_cursor"] == 20
    assert window["retained_from_cursor"] > 1
    assert window["retained_events"] < 20
    rows = journal.read_after(
        window["retained_from_cursor"] - 1,
        agent_id="agent-1",
        card_id="card-1",
        contract_id="contract-1",
        lease_id="lease-1",
        kind="phase",
        limit=500,
    )
    assert rows
    assert all(row.agent_id == "agent-1" and row.kind is ActivityKind.PHASE for row in rows)
    assert rows[0].parent_agent_id == "orchestrator-1"
    assert rows[0].base_commit == "a" * 40


def test_rows_reject_unknown_fields_and_invalid_context(tmp_path):
    with pytest.raises(ValueError):
        _context(agent_id="../../escape")
    with pytest.raises(ValueError, match="card_hash"):
        _context(card_hash="not-a-snapshot-hash")
    journal = ActivityJournal(root=tmp_path)
    journal.publish(_context(), ActivityKind.STATUS)
    row = json.loads(journal.path.read_text(encoding="utf-8"))
    row["future_unreviewed_field"] = True
    journal.path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(ActivityCorruptionError):
        journal.read_after()


def test_job_identity_is_first_class_and_filterable(tmp_path):
    journal = ActivityJournal(root=tmp_path)
    journal.publish(
        ActivityContext(session_id="control-job-1", job_id="job-1", source="scheduler"),
        ActivityKind.STATUS,
        summary="job running",
    )
    assert journal.read_after(job_id="job-1")[0].job_id == "job-1"
    assert journal.read_after(job_id="job-other") == []
