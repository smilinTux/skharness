"""A6.3: the attribution join, proven over committed rows (card c7aea2e0).

Every assertion in this file runs with no gateway, no network and no live
database. The rows in ``tests/data/attribution-join-rows.json`` were captured
verbatim from the real skgateway store, so what CI checks is the same shape
production writes. The live half of the proof lives in
``test_attribution_join_live.py`` and skips loudly when no gateway answers.

The case that matters most here is ``control_no_headers``. It is a real call,
issued seconds after the attributed one, same model, same gateway, same prompt,
with the two attribution headers removed and nothing else changed. Its row is
identical to the attributed row in every column except the two this epic exists
to populate. That pairing is what turns "the join succeeded" into evidence: a
join that reports a match for a call which sent nothing has not measured
anything.
"""
from __future__ import annotations

import dataclasses
import json
import sqlite3
from pathlib import Path

import pytest

from skharness.autocode import attribution as attr

_ROWS = json.loads(
    (Path(__file__).parent / "data" / "attribution-join-rows.json").read_text())


def _case(name: str) -> attr.GatewayRows:
    return attr.GatewayRows.from_mapping(_ROWS["cases"][name])


def _attributed() -> attr.GatewayRows:
    return _case("attributed")


def _control() -> attr.GatewayRows:
    return _case("control_no_headers")


# The ids that were actually put on the wire for the `attributed` case. Written
# here as literals rather than read back out of the row, because reading them
# from the row would make the equality check compare the store against itself.
SENT_SESSION = "a63-probe-1786934730"
SENT_CARD = "c7aea2e0"
SENT_REQ = "6bd5bbf66f88"


# ---------------------------------------------------------------- the fixtures

def test_the_fixture_pair_differs_only_in_the_attribution_columns():
    """The control is a control, not a differently shaped call.

    If the two rows differed in model or backend as well, a passing join could
    be explained by that difference instead of by the headers. This pins the
    pairing itself, so a future fixture refresh cannot quietly break it.
    """
    a = _ROWS["cases"]["attributed"]["request_log"]
    c = _ROWS["cases"]["control_no_headers"]["request_log"]
    assert a["model"] == c["model"]
    assert a["backend"] == c["backend"]
    assert a["status_code"] == c["status_code"] == 200
    assert a["session_id"] == SENT_SESSION and c["session_id"] is None
    assert a["id"] != c["id"]


def test_agent_id_is_null_on_every_fixture_row():
    """`request_log.agent_id` has never once been populated, by two different
    mechanisms across its history. Nothing in the join may read it, and this
    pins the reason so a later reader does not mistake it for a usable column.
    """
    for case in _ROWS["cases"].values():
        assert case["request_log"]["agent_id"] is None
        for row in case["token_usage"] + case["energy_log"]:
            assert row["agent_id"] is None


# --------------------------------------------------------- the populated join

def test_the_three_way_join_holds_on_the_attributed_call():
    join = attr.join_rows(_attributed())
    assert join.found is True
    assert join.req_id == SENT_REQ                       # run <-> request_log.id
    assert join.session_id == SENT_SESSION               # what we SENT came back
    assert join.card_id == SENT_CARD
    assert join.backend_served == "reg:ornith"           # the backend is recoverable
    assert join.status_code == 200


def test_the_backend_names_which_tables_agreed():
    """A recovered backend that cannot say where it came from is a claim, not a
    measurement. On this call all three per-request tables agree, and the join
    records all three rather than picking one and staying quiet about it."""
    join = attr.join_rows(_attributed())
    assert join.backend_sources == ("cost_log", "request_log", "token_usage")
    assert join.backend_conflict is None


def test_verify_reports_a_match_on_the_attributed_call():
    verdict = attr.verify_join(
        attr.SentIds(session_id=SENT_SESSION, card_id=SENT_CARD, req_id=SENT_REQ),
        attr.join_rows(_attributed()))
    assert verdict.ok is True
    assert verdict.attributed is True
    assert verdict.session_id == attr.MATCH
    assert verdict.card_id == attr.MATCH
    assert verdict.backend == attr.RECOVERED
    assert verdict.problems == ()


# ------------------------------------------------------------------ the control

def test_the_headerless_call_produces_null_on_both_sides():
    join = attr.join_rows(_control())
    assert join.found is True
    assert join.session_id is None
    assert join.card_id is None
    # the call still happened and was still served: the nulls are about
    # attribution, not about the request failing.
    assert join.status_code == 200
    assert join.backend_served == "reg:ornith"


def test_the_control_verdict_is_a_different_word_from_a_match():
    """The load-bearing distinction of this card.

    A verdict that says "match" for a call which sent no session id has
    conflated "the store agrees with me" with "I had nothing to compare". The
    two outcomes must be separately nameable, or a green control proves nothing.
    """
    verdict = attr.verify_join(attr.SentIds(), attr.join_rows(_control()))
    assert verdict.ok is True                # consistent, nothing went wrong
    assert verdict.attributed is False       # and honestly says it is anonymous
    assert verdict.session_id == attr.ABSENT_AS_SENT
    assert verdict.card_id == attr.ABSENT_AS_SENT
    assert attr.ABSENT_AS_SENT != attr.MATCH
    assert verdict.problems == ()


def test_a_headerless_call_attributed_to_a_default_is_refused():
    """The negative control that gives the control its meaning.

    This is the failure the card was written to catch: nothing was sent, yet the
    store holds a session id anyway, because something downstream filled in a
    plausible default. The row looks healthy in isolation. The join must call it
    what it is rather than reporting a successful attribution.
    """
    rows = _ROWS["cases"]["control_no_headers"]
    poisoned = json.loads(json.dumps(rows))
    poisoned["request_log"]["session_id"] = "lumina"      # a default, invented
    join = attr.join_rows(attr.GatewayRows.from_mapping(poisoned))

    verdict = attr.verify_join(attr.SentIds(), join)
    assert verdict.ok is False
    assert verdict.session_id == attr.INVENTED
    assert any("invented" in p for p in verdict.problems)
    # and it must NOT be reported as an attributed run
    assert verdict.attributed is False


def test_the_control_would_go_red_against_a_defaulting_implementation():
    """The negative control on the CONTROL: does the check above have any power?

    A guard that has never been observed to fail is not known to be a guard.
    This builds the wrong implementation on purpose (a join that fills a NULL
    session id with the agent name, which is exactly the plausible mistake, and
    exactly what `request_log.agent_id` would look like if it had ever worked)
    and asserts that the control's assertions do NOT hold against it.

    The mutation is applied to the join OUTPUT rather than by monkeypatching
    module internals, so it stays valid if `join_rows` is refactored: any
    implementation that produces this output fails, whatever its shape.
    """
    honest = attr.join_rows(_control())
    defaulting = dataclasses.replace(honest, session_id="lumina")

    verdict = attr.verify_join(attr.SentIds(), defaulting)
    assert verdict.session_id != attr.ABSENT_AS_SENT     # the control's assertion
    assert verdict.ok is not True                        # the control's assertion
    assert verdict.attributed is not True                # the control's assertion


def test_a_lost_header_is_not_the_same_word_as_an_absent_one():
    """We sent a session id and the store has none: the header was dropped
    somewhere between the adapter and the gateway. That is a real defect, and it
    must not read as the (fine) anonymous case."""
    verdict = attr.verify_join(
        attr.SentIds(session_id="s-we-sent"), attr.join_rows(_control()))
    assert verdict.ok is False
    assert verdict.session_id == attr.MISSING
    assert verdict.session_id != attr.ABSENT_AS_SENT
    assert verdict.attributed is False


def test_a_different_session_in_the_store_is_a_mismatch():
    verdict = attr.verify_join(
        attr.SentIds(session_id="some-other-session", card_id=SENT_CARD),
        attr.join_rows(_attributed()))
    assert verdict.ok is False
    assert verdict.session_id == attr.MISMATCH
    assert verdict.card_id == attr.MATCH          # the two axes stay independent


def test_a_req_id_that_names_a_different_row_is_not_a_join():
    verdict = attr.verify_join(
        attr.SentIds(session_id=SENT_SESSION, req_id="ffffffffffff"),
        attr.join_rows(_attributed()))
    assert verdict.ok is False
    assert any("req_id" in p for p in verdict.problems)


def test_a_missing_request_row_is_a_failure_not_an_empty_success():
    join = attr.join_rows(attr.GatewayRows(req_id="deadbeefdead"))
    assert join.found is False
    verdict = attr.verify_join(attr.SentIds(session_id="s"), join)
    assert verdict.ok is False
    assert verdict.attributed is False
    assert any("no request_log row" in p for p in verdict.problems)


# --------------------------------------------------------- the served model

def test_model_served_is_unobservable_from_this_store_and_says_so():
    """No table in metrics.db has ever held a served model. `token_usage.model`
    never once disagrees with `request_log.model` across all 1,445 joined rows,
    because both are the REQUESTED id. Deriving one from the other would
    manufacture a fact, so the join records the absence with a reason."""
    join = attr.join_rows(_attributed())
    assert join.model_requested == "sk-default"
    assert join.model_served is None
    assert join.model_served_reason == attr.MODEL_SERVED_UNOBSERVED
    # the tempting derivation, refused explicitly
    assert join.model_served != join.model_requested


def test_agreeing_model_columns_do_not_become_a_served_model():
    """The two model columns agree on every fixture, which is exactly what makes
    the wrong implementation look right. Assert the agreement, then assert the
    join still refuses to promote it."""
    for name in ("attributed", "control_no_headers", "failover_chain"):
        case = _ROWS["cases"][name]
        assert case["token_usage"][0]["model"] == case["request_log"]["model"]
        assert attr.join_rows(_case(name)).model_served is None


def test_the_verdict_never_claims_a_served_model():
    verdict = attr.verify_join(
        attr.SentIds(session_id=SENT_SESSION, card_id=SENT_CARD, req_id=SENT_REQ),
        attr.join_rows(_attributed()))
    assert verdict.model_served == attr.UNOBSERVED


# ------------------------------------------------------------ the failover case

def test_the_failover_chain_is_kept_as_a_chain():
    """`energy_log` holds one row per ATTEMPT. Collapsing it to a scalar would
    erase the fact that this request touched two backends and burned 828 joules
    doing it."""
    join = attr.join_rows(_case("failover_chain"))
    assert [a.backend for a in join.backend_attempts] == ["reg:ornith", "nvidia"]
    assert [a.basis for a in join.backend_attempts] == ["measured_gpu", "imputed_cloud"]
    assert round(join.joules_total, 2) == round(2.976 + 825.36, 2)


def test_the_served_backend_on_a_failover_is_the_one_that_completed():
    """`request_log.backend` is NULL on this row (it predates the gateway fix),
    `token_usage` is written once per completed request, and `energy_log` names
    both attempts. The served backend is therefore the one the completion row
    names, and the join must not average, first-wins or last-wins over the
    attempt chain."""
    join = attr.join_rows(_case("failover_chain"))
    assert join.backend_served == "nvidia"
    # both completion tables name the second attempt, and neither names the
    # first, which is what makes "the one that completed" a reading of the store
    # rather than a rule imposed on it.
    assert join.backend_sources == ("cost_log", "token_usage")
    assert join.backend_conflict is None
    assert join.backend_attempts[0].backend == "reg:ornith"   # first attempt lost


def test_a_backend_disagreement_is_recorded_not_silently_resolved():
    """Two per-request tables naming different backends means the store does not
    know what served the call. Picking one by precedence would produce a
    confident wrong answer, so the join returns no backend and names the
    conflict."""
    poisoned = json.loads(json.dumps(_ROWS["cases"]["attributed"]))
    poisoned["token_usage"][0]["backend"] = "nvidia"     # request_log says reg:ornith
    join = attr.join_rows(attr.GatewayRows.from_mapping(poisoned))
    assert join.backend_served is None
    assert join.backend_sources == ()
    assert join.backend_conflict == {"cost_log": "reg:ornith",
                                     "request_log": "reg:ornith",
                                     "token_usage": "nvidia"}

    verdict = attr.verify_join(attr.SentIds(session_id=SENT_SESSION), join)
    assert verdict.backend == attr.CONFLICT
    assert verdict.ok is False


def test_an_unrecoverable_backend_is_honest_but_not_an_error():
    """A row whose backend columns are all NULL cannot say what served it. That
    is a gap in the store, not an inconsistency in the join, so the verdict
    stays ok while the backend axis says plainly that it is unrecoverable."""
    stripped = json.loads(json.dumps(_ROWS["cases"]["attributed"]))
    stripped["request_log"]["backend"] = None
    for row in stripped["token_usage"] + stripped["cost_log"] + stripped["energy_log"]:
        row["backend"] = None
    join = attr.join_rows(attr.GatewayRows.from_mapping(stripped))
    assert join.backend_served is None
    assert join.backend_conflict is None

    verdict = attr.verify_join(
        attr.SentIds(session_id=SENT_SESSION, card_id=SENT_CARD), join)
    assert verdict.backend == attr.UNRECOVERABLE
    assert verdict.ok is True
    assert verdict.attributed is True


# ------------------------------------------------------------------ the reader

@pytest.fixture
def fixture_db(tmp_path) -> Path:
    """A throwaway metrics.db built from the committed rows, so the SQL layer is
    exercised without touching the live gateway store."""
    path = tmp_path / "metrics.db"
    con = sqlite3.connect(path)
    con.executescript("""
      CREATE TABLE request_log (id TEXT PRIMARY KEY, agent_id TEXT, model TEXT,
        backend TEXT, session_id TEXT, started_at INTEGER NOT NULL,
        status_code INTEGER, first_byte_ms INTEGER, total_ms INTEGER,
        error_msg TEXT);
      CREATE TABLE token_usage (id INTEGER PRIMARY KEY, req_id TEXT NOT NULL,
        agent_id TEXT, model TEXT, backend TEXT, session_id TEXT,
        ts INTEGER NOT NULL, hour_bucket TEXT NOT NULL, day_bucket TEXT NOT NULL,
        input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
        cache_write_tokens INTEGER);
      CREATE TABLE cost_log (id INTEGER PRIMARY KEY, req_id TEXT NOT NULL,
        agent_id TEXT, model TEXT, backend TEXT, session_id TEXT,
        ts INTEGER NOT NULL, day_bucket TEXT NOT NULL, input_cost REAL,
        output_cost REAL, cache_read_cost REAL, cache_write_cost REAL);
      CREATE TABLE energy_log (id INTEGER PRIMARY KEY, req_id TEXT NOT NULL,
        agent_id TEXT, model TEXT, backend TEXT, card_id TEXT,
        ts INTEGER NOT NULL, day_bucket TEXT NOT NULL, joules REAL,
        basis TEXT NOT NULL, node TEXT, concurrency_n INTEGER);
    """)
    for case in _ROWS["cases"].values():
        for table in ("request_log", "token_usage", "cost_log", "energy_log"):
            rows = case[table]
            rows = [rows] if isinstance(rows, dict) else rows
            for row in rows:
                cols = [c for c in row if c != "total_cost"]   # generated column
                con.execute(
                    f"INSERT INTO {table} ({','.join(cols)}) VALUES "
                    f"({','.join('?' * len(cols))})", [row[c] for c in cols])
    con.commit()
    con.close()
    return path


def test_fetch_rows_reads_every_table_for_one_req_id(fixture_db):
    rows = attr.fetch_rows(SENT_REQ, db_path=fixture_db)
    assert rows.request_log["id"] == SENT_REQ
    assert len(rows.token_usage) == 1
    assert len(rows.energy_log) == 1
    assert attr.join_rows(rows).session_id == SENT_SESSION


def test_fetch_rows_on_an_unknown_req_id_is_not_found_not_an_error(fixture_db):
    rows = attr.fetch_rows("000000000000", db_path=fixture_db)
    assert attr.join_rows(rows).found is False


def test_find_req_ids_for_session_finds_only_that_session(fixture_db):
    assert attr.find_req_ids_for_session(SENT_SESSION, db_path=fixture_db) == (SENT_REQ,)
    assert attr.find_req_ids_for_session("nobody", db_path=fixture_db) == ()


def test_the_connection_is_read_only(fixture_db):
    """The live gateway owns metrics.db and writes to it continuously. This
    module reads a production store, so its handle must be incapable of writing
    rather than merely refraining from it."""
    with attr.open_store(fixture_db) as con:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            con.execute("DELETE FROM request_log")


def test_a_missing_store_names_the_path_it_wanted(tmp_path):
    missing = tmp_path / "nope" / "metrics.db"
    with pytest.raises(attr.GatewayStoreUnavailable, match=str(missing)):
        attr.fetch_rows(SENT_REQ, db_path=missing)


def test_the_default_store_path_is_overridable_by_env(monkeypatch, tmp_path):
    monkeypatch.setenv(attr.METRICS_DB_ENV, str(tmp_path / "elsewhere.db"))
    assert attr.metrics_db_path() == tmp_path / "elsewhere.db"
    monkeypatch.delenv(attr.METRICS_DB_ENV)
    assert attr.metrics_db_path() == Path(attr.DEFAULT_METRICS_DB).expanduser()
