"""Tests for the joule wallet fixture-contamination correction.

NOTHING HERE MAY READ OR WRITE THE LIVE WALLET. Every test builds its own
ledger in tmp_path. The bug being corrected was caused by test code writing to
production economic state, so a test suite that reached for the real ledger to
verify the correction would be a second instance of the same mistake.
"""

import json

import pytest

from skharness.autocode import wallet_correction as wc


def _row(kind, amount, balance_after, description, timestamp):
    return {
        "kind": kind,
        "amount": amount,
        "counterparty": "economy",
        "description": description,
        "proof_hash": "0" * 64,
        "timestamp": timestamp,
        "balance_after": balance_after,
    }


def _fixture_row(balance_after, timestamp):
    return _row("mint", 75, balance_after, wc.FIXTURE_DESCRIPTION, timestamp)


def _write(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# classifier                                                                   #
# --------------------------------------------------------------------------- #

def test_classifier_matches_the_full_fixture_signature():
    assert wc.is_fabricated(_fixture_row(100, "2026-08-01T00:00:00+00:00"))


def test_classifier_rejects_a_genuine_row():
    genuine = _row(
        "mint", 75, 100,
        "[a43cac2e] Task completed: S24 the grader sovereignty gate",
        "2026-08-01T00:00:00+00:00",
    )
    assert not wc.is_fabricated(genuine)


@pytest.mark.parametrize(
    "field,value",
    [
        ("kind", "spend"),
        ("amount", 50),
        ("counterparty", "someone-else"),
    ],
)
def test_classifier_requires_every_signature_field(field, value):
    """The signature is conjunctive. A row carrying the marker STRING but a
    different kind/amount/counterparty is not the known fixture and must not be
    silently swept up: over-matching would corrupt the correction it computes."""
    row = _fixture_row(100, "2026-08-01T00:00:00+00:00")
    row[field] = value
    assert not wc.is_fabricated(row)


def test_classifier_bounds_the_marker_by_the_cutoff():
    """The leak is fixed, so a row carrying the marker AFTER the last known
    fixture row cannot have come from the fixture. It is either a genuine card
    literally named t1 or a regression, and either way the correction must not
    claim it."""
    after = _fixture_row(100, "2026-08-18T00:00:00+00:00")
    assert not wc.is_fabricated(after)


def test_classifier_accepts_the_boundary_row_itself():
    """The last fixture row is inclusive: it IS fixture output."""
    boundary = _fixture_row(100, wc.LAST_FIXTURE_TIMESTAMP)
    assert wc.is_fabricated(boundary)


def test_classifier_tolerates_a_missing_description():
    assert not wc.is_fabricated({"kind": "mint", "amount": 75})


# --------------------------------------------------------------------------- #
# correction series                                                            #
# --------------------------------------------------------------------------- #

def test_clean_ledger_is_a_no_op(tmp_path):
    rows = [
        _row("mint", 100, 100, "[a1] Task completed: real", "2026-07-01T00:00:00+00:00"),
        _row("mint", 50, 150, "[a2] Task completed: real", "2026-07-02T00:00:00+00:00"),
        _row("spend", 20, 130, "autocode llm-cost a2", "2026-07-03T00:00:00+00:00"),
    ]
    result = wc.correct_ledger(rows)
    assert result.fabricated_count == 0
    assert result.fabricated_joules == 0
    assert result.recorded_balance == 130
    assert result.corrected_balance == 130
    for corrected in result.rows:
        assert corrected.corrected_balance == corrected.recorded_balance
        assert corrected.cumulative_fabricated == 0


def test_correction_applies_to_every_row_after_a_fake_mint_not_only_the_fake_rows(tmp_path):
    """The whole point of a per-row series. balance_after is a running total, so
    a fixture mint at row 1 poisons rows 1..N, not row 1 alone."""
    rows = [
        _row("mint", 100, 100, "[a1] Task completed: real", "2026-07-01T00:00:00+00:00"),
        _fixture_row(175, "2026-07-02T00:00:00+00:00"),
        _row("mint", 25, 200, "[a2] Task completed: real", "2026-07-03T00:00:00+00:00"),
        _fixture_row(275, "2026-07-04T00:00:00+00:00"),
        _row("spend", 50, 225, "autocode llm-cost a2", "2026-07-05T00:00:00+00:00"),
    ]
    result = wc.correct_ledger(rows)

    assert result.fabricated_count == 2
    assert result.fabricated_joules == 150
    # row 0 is before any contamination and must be untouched
    assert result.rows[0].corrected_balance == 100
    assert result.rows[0].cumulative_fabricated == 0
    # the fixture row itself falls back to the pre-mint balance
    assert result.rows[1].corrected_balance == 100
    assert result.rows[1].cumulative_fabricated == 75
    # a GENUINE row after a fixture mint is corrected too. This is the assertion
    # a scalar correction factor would get wrong.
    assert result.rows[2].corrected_balance == 125
    assert result.rows[2].cumulative_fabricated == 75
    assert result.rows[3].corrected_balance == 125
    assert result.rows[3].cumulative_fabricated == 150
    assert result.rows[4].corrected_balance == 75
    assert result.rows[4].cumulative_fabricated == 150

    assert result.recorded_balance == 225
    assert result.corrected_balance == 75


def test_rows_before_the_first_fabricated_mint_are_untouched():
    rows = [
        _row("mint", 10, 10, "[a1] real", "2026-07-01T00:00:00+00:00"),
        _row("mint", 10, 20, "[a2] real", "2026-07-02T00:00:00+00:00"),
        _fixture_row(95, "2026-07-03T00:00:00+00:00"),
    ]
    result = wc.correct_ledger(rows)
    assert result.first_fabricated_index == 2
    assert [r.corrected_balance for r in result.rows] == [10, 20, 20]


def test_boundary_a_marker_row_after_the_cutoff_is_left_alone():
    """Boundary case at the last fixture row: the row AT the cutoff is corrected,
    a marker row after it is not, and the correction stops accumulating."""
    rows = [
        _fixture_row(75, wc.LAST_FIXTURE_TIMESTAMP),
        _row("mint", 100, 175, "[a1] Task completed: real", "2026-08-17T02:00:00+00:00"),
        _fixture_row(250, "2026-08-17T03:00:00+00:00"),
    ]
    result = wc.correct_ledger(rows)
    assert result.fabricated_count == 1
    assert result.fabricated_joules == 75
    assert [r.corrected_balance for r in result.rows] == [0, 100, 175]


def test_continuity_anomalies_are_reported_not_repaired():
    """The recorded balance is NOT a pure running total: concurrent settles have
    dropped genuine credits. The correction removes fixture contamination ONLY,
    so it must surface those anomalies rather than quietly absorb them."""
    rows = [
        _row("mint", 100, 100, "[a1] real", "2026-07-01T00:00:00+00:00"),
        # lost update: two concurrent settles read the same balance and both
        # wrote it back, so this mint's 25 J was never credited
        _row("mint", 25, 100, "[a2] real", "2026-07-01T00:00:00+00:00"),
        _row("mint", 50, 150, "[a3] real", "2026-07-02T00:00:00+00:00"),
    ]
    result = wc.correct_ledger(rows)
    assert len(result.continuity_anomalies) == 1
    anomaly = result.continuity_anomalies[0]
    assert anomaly.index == 1
    assert anomaly.recorded_balance == 100
    assert anomaly.expected_balance == 125
    # unrepaired: the corrected series still tracks the recorded balance
    assert [r.corrected_balance for r in result.rows] == [100, 100, 150]


def test_no_spend_was_enabled_by_fabricated_joules_is_computed_not_assumed():
    rows = [
        _fixture_row(75, "2026-07-01T00:00:00+00:00"),
        _row("spend", 50, 25, "autocode llm-cost a1", "2026-07-02T00:00:00+00:00"),
    ]
    result = wc.correct_ledger(rows)
    # corrected balance after the spend is negative => that spend could not have
    # happened without the fabricated joules, and the tool must say so.
    assert result.min_corrected_balance == -50
    assert result.spends_requiring_fabricated_joules == 1


# --------------------------------------------------------------------------- #
# sidecar + loading                                                            #
# --------------------------------------------------------------------------- #

def test_sidecar_is_written_beside_the_wallet_and_never_into_it(tmp_path):
    wallet = _write(tmp_path / "transactions.jsonl", [
        _row("mint", 100, 100, "[a1] real", "2026-07-01T00:00:00+00:00"),
        _fixture_row(175, "2026-07-02T00:00:00+00:00"),
    ])
    before = wallet.read_bytes()

    sidecar = wc.write_sidecar(wallet)

    assert wallet.read_bytes() == before, "the wallet itself must never be rewritten"
    assert sidecar != wallet
    assert sidecar.parent == wallet.parent
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["summary"]["fabricated_count"] == 1
    assert payload["summary"]["fabricated_joules"] == 75
    assert payload["summary"]["corrected_balance"] == 100
    assert len(payload["rows"]) == 2
    assert payload["rows"][1]["corrected_balance"] == 100
    assert payload["rows"][1]["recorded_balance"] == 175


def test_load_ledger_skips_blank_lines(tmp_path):
    path = tmp_path / "transactions.jsonl"
    path.write_text(
        json.dumps(_row("mint", 5, 5, "[a1] real", "2026-07-01T00:00:00+00:00")) + "\n\n",
        encoding="utf-8",
    )
    assert len(wc.load_ledger(path)) == 1


def test_summary_text_reports_the_fraction_of_the_ledger_affected():
    rows = [
        _row("mint", 100, 100, "[a1] real", "2026-07-01T00:00:00+00:00"),
        _fixture_row(175, "2026-07-02T00:00:00+00:00"),
    ]
    text = wc.summary_text(wc.correct_ledger(rows))
    assert "50.0%" in text
    assert "75" in text
    assert "—" not in text and "–" not in text


def test_cli_reports_without_touching_the_wallet(tmp_path, capsys):
    wallet = _write(tmp_path / "transactions.jsonl", [
        _row("mint", 100, 100, "[a1] real", "2026-07-01T00:00:00+00:00"),
        _fixture_row(175, "2026-07-02T00:00:00+00:00"),
    ])
    before = wallet.read_bytes()

    rc = wc.main(["--wallet", str(wallet)])

    assert rc == 0
    assert wallet.read_bytes() == before
    out = capsys.readouterr().out
    assert "fabricated mints" in out
    assert "corrected balance" in out


def test_cli_json_mode_emits_the_summary(tmp_path, capsys):
    wallet = _write(tmp_path / "transactions.jsonl", [
        _fixture_row(75, "2026-07-02T00:00:00+00:00"),
    ])
    rc = wc.main(["--wallet", str(wallet), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["fabricated_count"] == 1
    assert payload["corrected_balance"] == 0


def test_cli_write_mode_creates_the_sidecar(tmp_path, capsys):
    wallet = _write(tmp_path / "transactions.jsonl", [
        _fixture_row(75, "2026-07-02T00:00:00+00:00"),
    ])
    before = wallet.read_bytes()
    rc = wc.main(["--wallet", str(wallet), "--write-sidecar"])
    assert rc == 0
    assert wallet.read_bytes() == before
    assert (tmp_path / wc.SIDECAR_NAME).exists()
