"""S29 (card 60245d49): the cost-ledger fixture-contamination correction.

Shaped after tests/test_wallet_correction.py, because the module is shaped after
wallet_correction and the two corrections should be read together.
"""
import json

import pytest

from skharness.autocode import ledger_correction as lc


def _row(card_id="t-1", run_id="r1", tokens=0, cost_usd=0.0, joules=0,
         date="2026-08-17", ts="2026-08-17T02:58:18.617124+00:00", repo="skos"):
    return {"card_id": card_id, "run_id": run_id, "tokens": tokens,
            "cost_usd": cost_usd, "joules": joules, "date": date, "ts": ts,
            "repo": repo}


def _write(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# classifier                                                                   #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("card_id,run_id", [
    ("t-1", "r1"), ("t-2", "rc"), ("t-B", "rr"), ("keep", "r1"),
    ("t-0", "rp"), ("t-3", "rp"),
    ("task-abc", "airun-task-abc-20260813T105351Z"),
])
def test_every_observed_fixture_shape_is_classified(card_id, run_id):
    assert lc.is_fixture_row(_row(card_id=card_id, run_id=run_id)) is True


@pytest.mark.parametrize("card_id,run_id", [
    ("60245d49", "20260817T031234Z"),       # a real card, a real run
    ("t-1", "20260817T031234Z"),            # fixture-looking card, REAL run id
    ("60245d49", "r1"),                     # real card, fixture-looking run id
    ("task-abc", "20260817T031234Z"),       # bridge card without its prefix
])
def test_the_signature_is_conjunctive(card_id, run_id):
    """Both halves must match. A classifier that over-matches does not merely
    miss the correction, it corrupts it: every falsely flagged row is a real run
    deleted from the corrected totals, which understates what the fleet spent."""
    assert lc.is_fixture_row(_row(card_id=card_id, run_id=run_id)) is False


def test_a_missing_or_non_string_run_id_is_not_a_fixture_row():
    assert lc.is_fixture_row({"card_id": "t-1"}) is False
    assert lc.is_fixture_row({"card_id": "t-1", "run_id": 5}) is False


def test_repo_and_cost_are_deliberately_not_part_of_the_signature():
    """Identity is the card and the run. Matching on repo breaks the moment a
    fixture changes its tag; matching on a zero cost would quietly reclassify a
    future fixture that stamps a number."""
    assert lc.is_fixture_row(_row(repo="skrender", tokens=999, cost_usd=1.5)) is True


# --------------------------------------------------------------------------- #
# aggregates                                                                   #
# --------------------------------------------------------------------------- #

def test_correction_separates_fixture_rows_from_genuine_ones():
    rows = [
        _row(),                                                    # fixture
        _row(card_id="task-abc", run_id="airun-task-abc-20260813T105351Z"),
        _row(card_id="60245d49", run_id="20260817T031234Z",
             tokens=1000, cost_usd=2.0, joules=100),               # genuine
    ]
    c = lc.correct_ledger(rows)
    assert (c.total_rows, c.fixture_rows, c.genuine_rows) == (3, 2, 1)
    assert c.recorded_tokens == 1000 and c.corrected_tokens == 1000
    assert c.corrected_cost_usd == pytest.approx(2.0)
    assert c.corrected_joules == 100


def test_corrected_totals_drop_only_the_fixture_contribution():
    rows = [
        _row(tokens=50, cost_usd=1.0, joules=50),                  # fixture, priced
        _row(card_id="60245d49", run_id="20260817T031234Z",
             tokens=10, cost_usd=0.5, joules=25),                  # genuine
    ]
    c = lc.correct_ledger(rows)
    assert c.recorded_tokens == 60 and c.corrected_tokens == 10
    assert c.recorded_cost_usd == pytest.approx(1.5)
    assert c.corrected_cost_usd == pytest.approx(0.5)
    assert c.recorded_joules == 75 and c.corrected_joules == 25


def test_per_day_correction_is_reported_because_the_cap_is_per_day():
    rows = [
        _row(date="2026-08-16", ts="2026-08-16T01:00:00+00:00", tokens=7),
        _row(card_id="60245d49", run_id="20260817T031234Z",
             date="2026-08-17", tokens=3),
    ]
    c = lc.correct_ledger(rows)
    by_date = {d.date: d for d in c.days}
    assert by_date["2026-08-16"].recorded_runs == 1
    assert by_date["2026-08-16"].corrected_runs == 0
    assert by_date["2026-08-16"].corrected_tokens == 0
    assert by_date["2026-08-17"].corrected_runs == 1
    assert by_date["2026-08-17"].corrected_tokens == 3


def test_contaminated_range_and_card_census_are_recorded():
    rows = [_row(ts="2026-08-17T01:00:00+00:00"),
            _row(card_id="t-2", ts="2026-08-17T02:00:00+00:00")]
    c = lc.correct_ledger(rows)
    assert c.first_fixture_timestamp == "2026-08-17T01:00:00+00:00"
    assert c.last_fixture_timestamp == "2026-08-17T02:00:00+00:00"
    assert c.fixture_cards == {"t-1": 1, "t-2": 1}


def test_an_all_fixture_ledger_corrects_to_nothing():
    """The live ledger's actual state: 253 of 253 rows are pytest output, so the
    corrected ledger is empty and every corrected aggregate is zero."""
    c = lc.correct_ledger([_row(), _row(card_id="t-2"), _row(card_id="keep")])
    assert c.genuine_rows == 0
    assert c.corrected_tokens == 0 and c.corrected_joules == 0
    assert c.fraction_fixture == 1.0
    assert "never recorded a real run" in lc.summary_text(c)


def test_empty_ledger_is_not_a_division_by_zero():
    c = lc.correct_ledger([])
    assert c.fraction_fixture == 0.0
    assert c.summary()["total_rows"] == 0


# --------------------------------------------------------------------------- #
# counting, as the session guard uses it                                       #
# --------------------------------------------------------------------------- #

def test_count_fixture_rows_counts_only_fixture_rows(tmp_path):
    p = _write(tmp_path / "ledger.jsonl", [
        _row(), _row(card_id="t-2"),
        _row(card_id="60245d49", run_id="20260817T031234Z"),
    ])
    assert lc.count_fixture_rows(p) == 2


def test_count_fixture_rows_tolerates_a_missing_file(tmp_path):
    assert lc.count_fixture_rows(tmp_path / "nope.jsonl") == 0


def test_count_fixture_rows_skips_a_malformed_line(tmp_path):
    """The session guard reads a file other processes are appending to, so it can
    catch a partially written line. It must count what it can rather than
    exploding: a guard that breaks the suite it guards gets deleted."""
    p = tmp_path / "ledger.jsonl"
    p.write_text(json.dumps(_row()) + "\n{ truncated\n", encoding="utf-8")
    assert lc.count_fixture_rows(p) == 1


# --------------------------------------------------------------------------- #
# the sidecar never touches the ledger                                         #
# --------------------------------------------------------------------------- #

def test_sidecar_is_published_beside_the_ledger_and_leaves_it_byte_identical(tmp_path):
    p = _write(tmp_path / "ledger.jsonl", [_row(), _row(card_id="t-2")])
    before = p.read_bytes()
    target = lc.write_sidecar(p)
    assert target == tmp_path / lc.SIDECAR_NAME
    assert p.read_bytes() == before          # append-only store, untouched
    payload = json.loads(target.read_text())
    assert payload["summary"]["fixture_rows"] == 2
    assert payload["summary"]["genuine_rows"] == 0


def test_sidecar_refuses_to_write_into_the_ledger_itself(tmp_path):
    p = _write(tmp_path / "ledger.jsonl", [_row()])
    with pytest.raises(ValueError):
        lc.write_sidecar(p, sidecar=p)


def test_load_ledger_skips_malformed_lines(tmp_path):
    p = tmp_path / "ledger.jsonl"
    p.write_text(json.dumps(_row()) + "\nnot json\n" + json.dumps(_row()) + "\n",
                 encoding="utf-8")
    assert len(lc.load_ledger(p)) == 2


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #

def test_cli_is_read_only_by_default(tmp_path, capsys):
    p = _write(tmp_path / "ledger.jsonl", [_row()])
    assert lc.main(["--ledger", str(p)]) == 0
    assert not (tmp_path / lc.SIDECAR_NAME).exists()
    assert "fixture rows" in capsys.readouterr().out


def test_cli_write_sidecar_publishes(tmp_path, capsys):
    p = _write(tmp_path / "ledger.jsonl", [_row()])
    assert lc.main(["--ledger", str(p), "--write-sidecar", "--json"]) == 0
    assert (tmp_path / lc.SIDECAR_NAME).exists()
    assert json.loads(capsys.readouterr().out)["fixture_rows"] == 1


def test_cli_reports_a_missing_ledger(tmp_path, capsys):
    assert lc.main(["--ledger", str(tmp_path / "nope.jsonl")]) == 2
