from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from skharness.control import (
    ControlAction,
    ControlConflictError,
    ControlCorruptionError,
    ControlJournal,
    ControlStatus,
    ControlTargetKind,
)


def _submit(journal, *, key="atlas-1", text="continue"):
    return journal.submit(
        actor="atlas",
        idempotency_key=key,
        target_kind=ControlTargetKind.AGENT,
        target_id="scout-1",
        action=ControlAction.MESSAGE,
        payload={"text": text},
        ttl_s=60,
    )


def test_command_is_durable_idempotent_and_receipt_driven(tmp_path):
    journal = ControlJournal(tmp_path, clock=lambda: 100.0)
    command, queued, replayed = _submit(journal)
    assert not replayed
    assert queued.status is ControlStatus.QUEUED
    restarted = ControlJournal(tmp_path, clock=lambda: 101.0)
    same, same_receipt, replayed = _submit(restarted)
    assert replayed and same == command and same_receipt == queued
    applied = restarted.record(
        command.command_id,
        ControlStatus.APPLIED,
        controller="swarm-controller",
        activity_cursor=7,
    )
    assert restarted.get(command.command_id) == (command, applied)
    assert restarted.pending() == ()


def test_applying_command_cannot_move_back_to_queued(tmp_path):
    journal = ControlJournal(tmp_path, clock=lambda: 100.0)
    command, _, _ = _submit(journal)
    journal.record(command.command_id, ControlStatus.APPLYING, controller="owner")
    with pytest.raises(ControlConflictError, match="applying->queued"):
        journal.record(command.command_id, ControlStatus.QUEUED, controller="owner")


def test_receipt_detail_redacts_inline_credentials(tmp_path):
    journal = ControlJournal(tmp_path, clock=lambda: 100.0)
    command, _, _ = _submit(journal)
    receipt = journal.record(
        command.command_id,
        ControlStatus.REJECTED,
        controller="owner",
        detail="upstream said Bearer do-not-store",
    )
    assert receipt.detail == "upstream said [redacted]"
    assert b"do-not-store" not in journal.path.read_bytes()


def test_command_claim_is_atomic_single_owner_and_expiry_aware(tmp_path):
    now = [100.0]
    journal = ControlJournal(tmp_path, clock=lambda: now[0])
    command, _, _ = _submit(journal)
    claimed, won = journal.claim(command.command_id, controller="owner-a")
    assert won and claimed.status is ControlStatus.APPLYING
    same, won = journal.claim(command.command_id, controller="owner-b")
    assert not won and same == claimed

    expiring, _, _ = _submit(journal, key="expires")
    now[0] = 200.0
    expired, won = journal.claim(expiring.command_id, controller="owner-a")
    assert not won and expired.status is ControlStatus.EXPIRED


def test_two_process_style_owners_cannot_both_claim_one_command(tmp_path):
    left = ControlJournal(tmp_path, clock=lambda: 100.0)
    right = ControlJournal(tmp_path, clock=lambda: 100.0)
    command, _, _ = _submit(left)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                lambda pair: pair[0].claim(command.command_id, controller=pair[1]),
                ((left, "owner-a"), (right, "owner-b")),
            )
        )
    assert sum(claimed for _receipt, claimed in outcomes) == 1


def test_idempotency_key_reuse_with_different_bytes_fails_closed(tmp_path):
    journal = ControlJournal(tmp_path, clock=lambda: 100.0)
    _submit(journal)
    with pytest.raises(ControlConflictError):
        _submit(journal, text="different")


def test_expired_command_gets_terminal_receipt_and_is_not_delivered(tmp_path):
    now = [100.0]
    journal = ControlJournal(tmp_path, clock=lambda: now[0])
    command, _, _ = _submit(journal)
    now[0] = 200.0
    assert journal.pending() == ()
    assert journal.get(command.command_id)[1].status is ControlStatus.EXPIRED


def test_payload_is_exact_and_secret_keys_are_rejected_before_persistence(tmp_path):
    journal = ControlJournal(tmp_path, clock=lambda: 100.0)
    command, _, _ = _submit(journal, text="continue exactly")
    assert command.payload == {"text": "continue exactly"}

    budget, _, _ = journal.submit(
        actor="atlas",
        idempotency_key="atlas-budget",
        target_kind=ControlTargetKind.RUN,
        target_id="run-1",
        action=ControlAction.MESSAGE,
        payload={"total_tokens": 42},
    )
    assert budget.payload == {"total_tokens": 42}
    assert os.stat(journal.path).st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="credential-bearing"):
        journal.submit(
            actor="atlas",
            idempotency_key="atlas-secret",
            target_kind=ControlTargetKind.SESSION,
            target_id="session-1",
            action=ControlAction.MESSAGE,
            payload={"text": "continue", "api_key": "do-not-store"},
        )
    assert b"do-not-store" not in journal.path.read_bytes()


def test_committed_corruption_fails_closed(tmp_path):
    journal = ControlJournal(tmp_path, clock=lambda: 100.0)
    _submit(journal)
    with journal.path.open("ab") as stream:
        stream.write(b"corrupt\n")
    with pytest.raises(ControlCorruptionError):
        journal.pending()


def test_incomplete_tail_is_removed_before_next_command_append(tmp_path):
    journal = ControlJournal(tmp_path, clock=lambda: 100.0)
    _submit(journal, key="first")
    with journal.path.open("ab") as stream:
        stream.write(b'{"record_type":"command"')
    _submit(journal, key="second")
    assert len(journal.pending()) == 2


def test_record_hash_detects_committed_receipt_tampering(tmp_path):
    journal = ControlJournal(tmp_path, clock=lambda: 100.0)
    _submit(journal)
    journal.path.write_text(
        journal.path.read_text(encoding="utf-8").replace('"queued"', '"applied"'),
        encoding="utf-8",
    )
    with pytest.raises(ControlCorruptionError):
        journal.pending()
