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


# ── Node partitioning (prb-7810b08e: per-writer files, writer == node) ────────


def test_two_nodes_sharing_a_base_never_write_the_same_file(tmp_path):
    """The replication-safety invariant: disjoint write sets per node.

    ~/.skcapstone is Syncthing-replicated. A shared events.jsonl + head.json
    + whole-file trim produced a conflict roughly every 40s and duplicated the
    entire 16MB journal each time. flock orders writers inside a node and does
    nothing across the mesh, so only the partition makes replication safe.
    """
    a = ActivityJournal(root=tmp_path, node="node-a")
    b = ActivityJournal(root=tmp_path, node="node-b")

    a.publish(_context(), ActivityKind.STATUS, summary="from a")
    b.publish(_context(), ActivityKind.STATUS, summary="from b")

    assert a.path != b.path
    assert a.root.parent == b.root.parent == tmp_path
    # No writable artifact is shared: events, head cursor and lock all differ.
    for name in ("events.jsonl", "head.json", ".activity.lock"):
        assert (a.root / name) != (b.root / name)
    assert "from a" in a.path.read_text(encoding="utf-8")
    assert "from a" not in b.path.read_text(encoding="utf-8")
    # Each node keeps its own monotonic cursor rather than racing a global one.
    assert a.publish(_context(), ActivityKind.PHASE).cursor == 2
    assert b.publish(_context(), ActivityKind.PHASE).cursor == 2


def test_read_fleet_merges_every_node_with_per_node_cursors(tmp_path):
    clock = {"t": 100.0}

    def tick():
        clock["t"] += 1.0
        return clock["t"]

    a = ActivityJournal(root=tmp_path, node="node-a", clock=tick)
    b = ActivityJournal(root=tmp_path, node="node-b", clock=tick)
    a.publish(_context(), ActivityKind.STATUS, summary="a1")
    b.publish(_context(), ActivityKind.STATUS, summary="b1")
    a.publish(_context(), ActivityKind.STATUS, summary="a2")

    assert a.iter_nodes() == ["node-a", "node-b"]

    rows, cursors = a.read_fleet()
    assert [summary for _, event in rows for summary in [event.summary]] == ["a1", "b1", "a2"]
    assert cursors == {"node-a": 2, "node-b": 1}

    # A resumed reader sees only what is new, per node.
    b.publish(_context(), ActivityKind.STATUS, summary="b2")
    rows, cursors = a.read_fleet(cursors)
    assert [event.summary for _, event in rows] == ["b2"]
    assert cursors == {"node-a": 2, "node-b": 2}


def test_read_fleet_ignores_a_node_partition_with_no_events(tmp_path):
    a = ActivityJournal(root=tmp_path, node="node-a")
    a.publish(_context(), ActivityKind.STATUS, summary="only")
    (tmp_path / "node-empty").mkdir()

    assert a.iter_nodes() == ["node-a"]
    rows, _ = a.read_fleet()
    assert [event.summary for _, event in rows] == ["only"]


@pytest.mark.parametrize("bad", ["", "../escape", "a/b", "node b"])
def test_journal_rejects_an_unsafe_node_segment(tmp_path, bad):
    with pytest.raises(ValueError, match="safe path segment"):
        ActivityJournal(root=tmp_path, node=bad)


def test_default_node_is_a_single_safe_segment():
    from skharness.activity import default_activity_node

    node = default_activity_node()
    assert node and "/" not in node
    assert ActivityJournal(node=node).node == node


def test_migrate_legacy_layout_moves_the_shared_journal_out_of_the_way(tmp_path):
    """The pre-partition files belong to every node, so to no node."""
    from skharness.activity import LEGACY_PARTITION, migrate_legacy_activity_layout

    legacy = ActivityJournal(root=tmp_path, node="node-a")
    legacy.publish(_context(), ActivityKind.STATUS, summary="historical")
    # Recreate the old shared layout: files sitting directly in the base.
    (tmp_path / "events.jsonl").write_bytes(legacy.path.read_bytes())
    (tmp_path / "head.json").write_text('{"head_cursor": 1}\n', encoding="utf-8")
    (tmp_path / ".activity.lock").touch()

    moved = migrate_legacy_activity_layout(tmp_path)

    assert set(moved) == {"events.jsonl", "head.json"}
    assert not (tmp_path / "events.jsonl").exists()
    assert not (tmp_path / "head.json").exists()
    assert not (tmp_path / ".activity.lock").exists()
    # History stays readable through the fleet view, owned by nobody.
    assert LEGACY_PARTITION in legacy.iter_nodes()
    rows, _ = legacy.read_fleet()
    assert any(node == LEGACY_PARTITION for node, _ in rows)
    # Idempotent.
    assert migrate_legacy_activity_layout(tmp_path) == {}
