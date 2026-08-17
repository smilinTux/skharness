"""S29 (card 60245d49): the cost ledger's writer-side production guard.

These tests pin the guard that stops a test run appending to the operator's
live, Syncthing-synced ``~/.skcapstone/autopilot-cost/`` tree. They are the unit
half; the aggregate half is the session guard in ``tests/conftest.py``, which
cannot be a unit test because the defect it catches only exists across a whole
session.

NOTE ON WHAT THESE TESTS DO NOT TOUCH. Nothing here writes to a real production
path. The guard is exercised by pointing ``SKAI_COST_DIR`` at a directory that
``_production_cost_dirs()`` has been told to treat as production, which is the
only way to test a refusal without needing the refusal to fail.
"""
import json

import pytest

from skharness.autocode import autopilot_cost, orchestrator


@pytest.fixture
def fake_production(tmp_path, monkeypatch):
    """A throwaway directory that the guard believes is production."""
    prod = tmp_path / "prod-cost"
    prod.mkdir()
    monkeypatch.setattr(autopilot_cost, "_production_cost_dirs",
                        lambda: {prod.resolve()})
    monkeypatch.setenv(autopilot_cost.COST_DIR_ENV, str(prod))
    return prod


def _record(**over):
    kw = dict(card_id="t-1", repo="skos", tokens=0, cost_usd=0.0, passed=True,
              pr="", ts="2026-08-17T00:00:00+00:00", run_id="r1")
    kw.update(over)
    autopilot_cost.record_run(**kw)


# --------------------------------------------------------------------------- #
# the refusal                                                                  #
# --------------------------------------------------------------------------- #

def test_record_run_refuses_to_append_to_a_production_ledger(fake_production):
    with pytest.raises(autopilot_cost.ProductionLedgerInTestError):
        _record()
    assert not (fake_production / "ledger.jsonl").exists()


def test_record_settlement_refuses_too(fake_production):
    """The settlement journal is guarded on the same terms. A fixture row here
    is worse than noise: already_settled() matches on card_id alone, so it would
    refuse a REAL settlement of the same card later."""
    with pytest.raises(autopilot_cost.ProductionLedgerInTestError):
        autopilot_cost.record_settlement(card_id="t-1", commit_sha="abc", agent="a",
                                         minted=1, spent=0, net=1, balance_after=1,
                                         ts="2026-08-17T00:00:00+00:00")
    assert not (fake_production / "settlements.jsonl").exists()


def test_the_refusal_is_not_swallowed_by_record_runs_catch_all(fake_production):
    """record_run swallows every other write failure on purpose, and must not
    swallow this one. A swallowed refusal is a silently dropped row and a green
    suite, which is exactly the shape that let the original leak survive."""
    with pytest.raises(autopilot_cost.ProductionLedgerInTestError):
        _record()


def test_the_refusal_is_not_swallowed_by_record_outcome_row_either(fake_production):
    """orchestrator.record_outcome_row wraps everything in `telemetry never
    breaks a build`. That catch-all is right for a full disk and wrong for this,
    because the consumer suite would stay green while production was corrupted."""
    item = type("I", (), {"ref": "t-1", "repo": "skos", "payload": {}})()
    with pytest.raises(autopilot_cost.ProductionLedgerInTestError):
        orchestrator.record_outcome_row(item, terminal_state="finalized", run_id="r1")


def test_the_error_names_the_env_var_that_fixes_it(fake_production):
    with pytest.raises(autopilot_cost.ProductionLedgerInTestError) as exc:
        _record()
    assert autopilot_cost.COST_DIR_ENV in str(exc.value)


# --------------------------------------------------------------------------- #
# the guard must not fire where it would be wrong                              #
# --------------------------------------------------------------------------- #

def test_an_isolated_dir_is_written_normally(tmp_path, monkeypatch):
    """The overwhelmingly common case: SKAI_COST_DIR points somewhere throwaway,
    so the guard is inert and the row lands."""
    monkeypatch.setenv(autopilot_cost.COST_DIR_ENV, str(tmp_path / "cost"))
    _record()
    rows = [json.loads(x) for x in
            (tmp_path / "cost" / "ledger.jsonl").read_text().splitlines() if x.strip()]
    assert len(rows) == 1 and rows[0]["card_id"] == "t-1"


def test_reads_and_aggregates_are_never_guarded(fake_production):
    """Resolving is not writing. `overview`, day_total and the cap alert must
    keep working against the real tree under pytest, or every read-path test
    would have to fake a directory to observe a directory."""
    assert autopilot_cost.cost_dir() == fake_production
    assert autopilot_cost.ledger_path() == fake_production / "ledger.jsonl"
    assert autopilot_cost._read_ledger() == []


def test_production_write_is_allowed_outside_a_test_run(fake_production, monkeypatch):
    """The guard keys on `pytest is driving this process`, not on the path. A
    real autopilot run resolves the same production path and must still write."""
    monkeypatch.setattr(autopilot_cost, "_in_test_run", lambda: False)
    _record()
    assert (fake_production / "ledger.jsonl").exists()


def test_the_escape_hatch_opens_it(fake_production, monkeypatch):
    monkeypatch.setenv(autopilot_cost.ALLOW_PRODUCTION_WRITE_ENV, "1")
    _record()
    assert (fake_production / "ledger.jsonl").exists()


# --------------------------------------------------------------------------- #
# the predicate itself                                                         #
# --------------------------------------------------------------------------- #

def test_in_test_run_is_true_here():
    """Negative control for the guard's own trigger: if this ever returns False
    inside pytest, every test above passes for the wrong reason."""
    assert autopilot_cost._in_test_run() is True


def test_in_test_run_survives_pytest_current_test_being_unset(monkeypatch):
    """PYTEST_CURRENT_TEST is unset between tests and during session teardown,
    which are exactly the windows an escaped write would land in. The
    sys.modules half is what has to carry it."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    assert autopilot_cost._in_test_run() is True


def test_production_roots_include_the_real_cost_dir():
    from pathlib import Path
    assert (Path.home() / ".skcapstone" / "autopilot-cost").resolve() \
        in autopilot_cost._production_cost_dirs()


def test_production_roots_follow_skcapstone_home(tmp_path, monkeypatch):
    """Some fleet nodes set SKCAPSTONE_HOME, so `production` is not only the
    literal ~/.skcapstone. A guard that missed those nodes would be a guard that
    protects the developer box and not the fleet."""
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path))
    assert (tmp_path / "autopilot-cost").resolve() \
        in autopilot_cost._production_cost_dirs()
