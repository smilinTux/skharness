"""A1 (card 8967bf22): the attribution fields on an autopilot ledger row, and
the counted (not merely logged) ledger write failure.

The load-bearing tests in this file are the NEGATIVE controls, not the
round-trips. A test that only proves "I passed backend_served=X and got X back"
would pass just as happily against an implementation that defaults
backend_served from model_requested, and that defaulted implementation is the
one that reports a 100% sovereign fleet forever. So the file asserts what the
row must NOT contain:

  * backend_served stays None when every plausible donor field (model_requested,
    model_served, adapter, bucket, gateway_url) is populated,
  * an id that was never supplied is null, never the empty string,
  * the nested work_grade dict survives intact, with the derived axes beside it
    rather than in place of it.

No em/en dashes anywhere (SKWorld hard rule).
"""
from __future__ import annotations

import json

import pytest

from skharness.autocode import autopilot_cost


@pytest.fixture(autouse=True)
def _isolate_cost_dir(monkeypatch, tmp_path):
    """Never touch the live ~/.skcapstone/autopilot-cost/. conftest.py already
    does this for every test; asserting it here too keeps this file honest on
    its own, because a suite that writes the real fleet ledger is a standing
    hazard in this repo."""
    cd = tmp_path / "autopilot-cost"
    monkeypatch.setenv("SKAI_COST_DIR", str(cd))
    assert autopilot_cost.cost_dir() == cd


@pytest.fixture(autouse=True)
def _isolate_health(monkeypatch, tmp_path):
    """Point health telemetry at a throwaway file so the counted failures this
    file provokes never land in the live health log (the adaptive retry budget
    reads that log)."""
    hp = tmp_path / "health.jsonl"
    monkeypatch.setenv("SKHARNESS_HEALTH_PATH", str(hp))
    return hp


def _rows() -> list[dict]:
    path = autopilot_cost.ledger_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _health(path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _record(**kw) -> dict:
    """record_run with the required fields filled in, returning the one row."""
    base = dict(card_id="card-1", repo="skharness", tokens=10, cost_usd=0.1,
                passed=True, pr="", ts="2026-08-16T12:00:00+00:00",
                run_id="airun-card-1-20260816T120000Z")
    base.update(kw)
    autopilot_cost.record_run(**base)
    rows = _rows()
    assert len(rows) == 1, rows
    return rows[0]


# --------------------------------------------------------------------------- #
# The ten fields round-trip, and default to null (present, not absent)        #
# --------------------------------------------------------------------------- #

ATTRIBUTION_FIELDS = ("agent", "session_id", "agent_var", "session_id_var",
                      "node", "gateway_url", "bucket", "backend_served",
                      "gateway_req_id", "fallback_reason")


def test_all_ten_attribution_fields_round_trip():
    row = _record(agent="jarvis", session_id="s-abc123", agent_var="SKAGENT",
                  session_id_var="SK_SESSION_ID", node="chiap04",
                  gateway_url="http://100.86.156.5:18780/v1",
                  bucket="sk-m-internal", backend_served="ornith-big",
                  gateway_req_id="req-9f2c", fallback_reason="pi")
    assert row["agent"] == "jarvis"
    assert row["session_id"] == "s-abc123"
    assert row["agent_var"] == "SKAGENT"
    assert row["session_id_var"] == "SK_SESSION_ID"
    assert row["node"] == "chiap04"
    assert row["gateway_url"] == "http://100.86.156.5:18780/v1"
    assert row["bucket"] == "sk-m-internal"
    assert row["backend_served"] == "ornith-big"
    assert row["gateway_req_id"] == "req-9f2c"
    assert row["fallback_reason"] == "pi"


def test_attribution_fields_default_to_null_and_are_present():
    """Omitted fields are null, and the KEY is present. Present-and-null means
    "this row could have said, and did not"; a missing key means "written by a
    build that had no such field". The two must stay distinguishable, so the
    keys are always emitted."""
    row = _record()
    for field in ATTRIBUTION_FIELDS:
        assert field in row, f"{field} key must be emitted even when unknown"
        assert row[field] is None, f"{field} must default to None, got {row[field]!r}"


def test_agent_var_and_session_id_var_are_recorded_independently_of_agent():
    """The .41 skcomms.service case: SKAGENT=jarvis, SKMEMORY_AGENT=lumina and
    SKCHAT_IDENTITY set in ONE unit. Recording only the resolved name destroys
    which variable produced it, and nothing can reconstruct that later."""
    row = _record(agent="jarvis", agent_var="SKAGENT",
                  session_id="s-1", session_id_var="minted")
    assert row["agent"] == "jarvis"
    assert row["agent_var"] == "SKAGENT"
    # The provenance field is its own fact, not a restatement of the name.
    assert row["agent_var"] != row["agent"]
    assert row["session_id_var"] == "minted"
    assert row["session_id_var"] != row["session_id"]


# --------------------------------------------------------------------------- #
# NEGATIVE CONTROLS                                                           #
# --------------------------------------------------------------------------- #


def test_backend_served_is_never_defaulted_from_anything():
    """The load-bearing negative control. Every plausible donor is populated;
    backend_served must still be None. Collapsing unknown into matched makes
    every run in the ledger read as sovereign."""
    row = _record(model_requested="sk-m-internal", model_served="ornith-big",
                  adapter="pi", bucket="sk-m-internal",
                  gateway_url="http://localhost:18780/v1")
    assert row["backend_served"] is None
    for donor in ("model_requested", "model_served", "adapter", "bucket",
                  "gateway_url"):
        assert row["backend_served"] != row[donor], (
            f"backend_served was populated from {donor}")


def test_model_served_is_still_never_defaulted_from_model_requested():
    """The pre-existing discipline this change extends, re-asserted so a later
    edit cannot quietly relax it."""
    row = _record(model_requested="sk-default")
    assert row["model_served"] is None


def test_missing_ids_are_null_not_empty_strings():
    """A row with no ids nulls them. An empty string joins to nothing yet reads
    as present, so a reader grouping by gateway_req_id would build one huge
    fake cohort out of every run that simply had no id."""
    row = _record(agent="", session_id="   ", agent_var="", session_id_var="",
                  node="", gateway_url="", bucket="", backend_served="",
                  gateway_req_id="", fallback_reason="")
    for field in ATTRIBUTION_FIELDS:
        assert row[field] is None, f"{field} was {row[field]!r}, expected None"
    # No attribution field may be an empty string. (``pr`` legitimately is one
    # on every orchestrator row, so this is scoped to the A1 fields.)
    assert "" not in [row[f] for f in ATTRIBUTION_FIELDS]


def test_ids_are_stripped():
    row = _record(session_id="  s-abc  ", node=" chiap04 ")
    assert row["session_id"] == "s-abc"
    assert row["node"] == "chiap04"


# --------------------------------------------------------------------------- #
# work_grade stays NESTED, derived axes sit beside it                         #
# --------------------------------------------------------------------------- #


def test_work_grade_stays_a_nested_dict_and_is_not_flattened_away():
    """escalation.py does work_grade.get("model_class") on a dict. Flattening
    would break that call site on contact."""
    grade = {"size": "M", "risk": "low", "sensitivity": "internal",
             "model_class": "m"}
    row = _record(work_grade=grade)
    assert isinstance(row["work_grade"], dict)
    assert row["work_grade"] == grade
    assert row["work_grade"].get("model_class") == "m"


def test_derived_grade_axes_are_added_beside_the_dict_not_instead_of_it():
    grade = {"size": "M", "risk": "Low", "sensitivity": "internal",
             "model_class": "m"}
    row = _record(work_grade=grade)
    assert row["grade_size"] == "m"
    assert row["grade_risk"] == "low"
    assert row["grade_sensitivity"] == "internal"
    # and the dict is STILL there, untouched
    assert row["work_grade"] == grade


def test_derived_grade_axes_are_null_when_there_is_no_grade():
    row = _record(work_grade=None)
    assert row["work_grade"] is None
    for k in ("grade_size", "grade_risk", "grade_sensitivity"):
        assert row[k] is None


def test_derived_grade_axes_tolerate_a_non_dict_grade():
    row = _record(work_grade="m")
    for k in ("grade_size", "grade_risk", "grade_sensitivity"):
        assert row[k] is None


def test_derived_axes_are_not_accepted_from_a_caller():
    """They are computed, not passed, so no caller can stamp an axis that
    disagrees with the row's own work_grade dict."""
    with pytest.raises(TypeError):
        autopilot_cost.record_run(
            card_id="c", repo="r", tokens=1, cost_usd=0.0, passed=True, pr="",
            ts="2026-08-16T12:00:00+00:00", grade_size="xl")


# --------------------------------------------------------------------------- #
# fallback_reason: closed vocabulary, drift counted not coerced               #
# --------------------------------------------------------------------------- #


#: Spelled out here rather than read off the module, so this file states the
#: vocabulary it expects instead of agreeing with whatever the module happens
#: to hold. A test that parametrizes over the implementation's own frozenset
#: passes for any vocabulary, including an empty one.
EXPECTED_FALLBACK_REASONS = ("adapter-error", "bucket-unavailable",
                             "capability-missing", "gateway-unreachable",
                             "harness-configured", "pi", "unknown")


def test_fallback_reason_vocabulary_is_closed_and_names_pi_explicitly():
    """``pi`` is a MEMBER, not an absence: "pi served it" and "nobody looked"
    are different facts, and only the first is evidence of sovereignty."""
    assert isinstance(autopilot_cost.FALLBACK_REASONS, frozenset)
    assert autopilot_cost.FALLBACK_REASONS == frozenset(EXPECTED_FALLBACK_REASONS)


@pytest.mark.parametrize("reason", EXPECTED_FALLBACK_REASONS)
def test_in_vocabulary_fallback_reason_is_recorded_without_a_drift_event(
        reason, _isolate_health):
    row = _record(fallback_reason=reason)
    assert row["fallback_reason"] == reason
    assert not [e for e in _health(_isolate_health)
                if e.get("kind") == "ledger_vocabulary_drift"]


def test_off_vocabulary_fallback_reason_is_kept_verbatim_and_counted(
        _isolate_health):
    """Never coerced into "unknown": normalising it would erase the evidence
    that a caller and this vocabulary have diverged."""
    row = _record(fallback_reason="because-the-gpu-was-busy")
    assert row["fallback_reason"] == "because-the-gpu-was-busy"
    drift = [e for e in _health(_isolate_health)
             if e.get("kind") == "ledger_vocabulary_drift"]
    assert len(drift) == 1
    assert drift[0]["field"] == "fallback_reason"
    assert drift[0]["value"] == "because-the-gpu-was-busy"


# --------------------------------------------------------------------------- #
# A failed ledger write is COUNTED, and still never raises                    #
# --------------------------------------------------------------------------- #


def test_failed_ledger_write_is_counted_as_a_health_event(monkeypatch,
                                                          _isolate_health):
    """The whole point: a run record that fails to write used to be visible
    only as one log line nobody aggregates. Now it is a countable event."""
    def _boom():
        raise OSError("read-only file system")
    monkeypatch.setattr(autopilot_cost, "ledger_path", _boom)

    autopilot_cost.record_run(card_id="card-9", repo="skharness", tokens=1,
                              cost_usd=0.0, passed=False, pr="",
                              ts="2026-08-16T12:00:00+00:00", run_id="airun-9",
                              terminal_state="finalize-failed")

    errs = [e for e in _health(_isolate_health)
            if e.get("kind") == "ledger_write_error"]
    assert len(errs) == 1
    assert errs[0]["card_id"] == "card-9"
    assert errs[0]["run_id"] == "airun-9"
    assert errs[0]["terminal_state"] == "finalize-failed"
    assert "read-only file system" in errs[0]["error"]


def test_record_run_still_never_raises_on_a_write_failure(monkeypatch):
    def _boom():
        raise OSError("nope")
    monkeypatch.setattr(autopilot_cost, "ledger_path", _boom)
    # No pytest.raises: the contract is that this returns normally.
    assert autopilot_cost.record_run(
        card_id="c", repo="r", tokens=0, cost_usd=0.0, passed=False, pr="",
        ts="2026-08-16T12:00:00+00:00") is None


def test_counting_a_failure_never_causes_one(monkeypatch):
    """Even a broken health module must not turn a ledger failure into a raise."""
    from skharness.autocode import health as _health_mod

    def _boom(*a, **k):
        raise RuntimeError("health is down")
    monkeypatch.setattr(_health_mod, "record", _boom)
    monkeypatch.setattr(autopilot_cost, "ledger_path",
                        lambda: (_ for _ in ()).throw(OSError("nope")))
    assert autopilot_cost.record_run(
        card_id="c", repo="r", tokens=0, cost_usd=0.0, passed=False, pr="",
        ts="2026-08-16T12:00:00+00:00") is None


# --------------------------------------------------------------------------- #
# Orchestrator wiring: node populated, the rest explicitly None               #
# --------------------------------------------------------------------------- #


def test_orchestrator_outcome_row_stamps_node_and_leaves_the_rest_none():
    import socket
    from types import SimpleNamespace

    from skharness.autocode import orchestrator as orch

    item = SimpleNamespace(ref="card-42", repo="skharness", payload={})
    orch.record_outcome_row(item, terminal_state="claim-raced",
                            run_id="airun-card-42", result=None, harness=None)

    rows = _rows()
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["node"] == socket.gethostname()
    # Pending PR #42 (feat/a21-session-identity): NOT reimplemented here, a
    # second resolver would mint a second session id for one session.
    for field in ("agent", "session_id", "agent_var", "session_id_var"):
        assert row[field] is None, f"{field} must stay None until PR #42 lands"
    # The orchestrator never talks to skgateway, so it cannot observe these.
    for field in ("gateway_url", "bucket", "backend_served", "gateway_req_id",
                  "fallback_reason"):
        assert row[field] is None, f"{field} must not be invented here"


def test_orchestrator_does_not_stamp_bucket_from_the_payload():
    """_model_requested resolves a bucket from the payload, which is what the
    item WOULD address. A claim-raced item never dispatched anything, so the
    bucket column must stay null even when model_requested carries a bucket
    id."""
    from types import SimpleNamespace

    from skharness.autocode import orchestrator as orch

    item = SimpleNamespace(ref="card-43", repo="skharness",
                           payload={"work_grade": {"size": "m", "risk": "low",
                                                   "sensitivity": "internal",
                                                   "model_class": "m"}})
    harness = SimpleNamespace(name="pi", model="ornith-big")
    orch.record_outcome_row(item, terminal_state="off-node",
                            run_id="airun-card-43", result=None,
                            harness=harness)
    row = _rows()[0]
    assert row["bucket"] is None
    assert row["backend_served"] is None
    # the nested grade survived the trip through the orchestrator
    assert row["work_grade"]["model_class"] == "m"
    assert row["grade_size"] == "m"
