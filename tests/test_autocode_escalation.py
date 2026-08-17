"""S12 (card 9a7c0a86): escalation_reason, the ONE sanctioned feedback seam.

Joule Economy design 2026-08-14, decision D2: "Below the class is refused. Above
it is allowed but requires a written escalation_reason and the energy overage is
debited. ESCALATION REASONS BECOME THE TRAINING DATA THAT CORRECTS A BAD RUBRIC."
Section 3.3: "Floor is hard. Ceiling is soft."

Two things this file is built to prevent, and they are the whole point:

1. TWO STATES WOULD LIE. Today model_served is ALWAYS None (neither the
   orchestrator nor the bridge observes what skgateway served) and ZERO cards
   carry a grade, so "did the served model exceed the floor" is usually
   UNANSWERABLE. A two-state escalated/within_floor split would force every
   unanswerable row into one of them. Folded into within_floor it understates
   escalation and reads as good news, which is exactly the failure this epic
   exists to remove. So there are THREE states, and a rate computed over mostly
   unobserved rows must SAY SO and report observed_fraction alongside.

2. THIS IS A REPORTING SEAM, NOT A CONTROL SEAM. Nothing may read
   escalation_reason (or escalation_state) to make a routing decision. A human
   reads it and decides whether the rubric was wrong. Wiring it into dispatch
   would be the autotuner card 09573989 AC6 forbids. The last section of this
   file proves that both statically and behaviourally; it is a required
   deliverable, not a nicety.

No em/en dashes anywhere (SKWorld hard rule).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from skharness.autocode import autopilot_cost
from skharness.autocode import escalation as esc


# tests/conftest.py already points SKAI_COST_DIR at a throwaway dir for EVERY
# test. This fixture only asserts that guard is actually in force: a suite that
# appends to the operator's live, Syncthing-synced ledger is a standing hazard.
@pytest.fixture(autouse=True)
def _ledger_is_isolated(tmp_path_factory, monkeypatch):
    cd = tmp_path_factory.mktemp("s12-cost")
    monkeypatch.setenv("SKAI_COST_DIR", str(cd))
    assert autopilot_cost.cost_dir() == cd


def _grade(model_class="m", sensitivity="internal"):
    """A COMPLETE work grade, the only shape the contract allows besides None."""
    return {"size": "M", "risk": "medium", "sensitivity": sensitivity,
            "model_class": model_class}


def _rows():
    return [json.loads(ln) for ln in
            autopilot_cost.ledger_path().read_text(encoding="utf-8").splitlines() if ln.strip()]


# --------------------------------------------------------------------------- #
# 1. Three states, and the third one is load bearing                          #
# --------------------------------------------------------------------------- #


def test_served_class_above_the_floor_is_escalated():
    """The soft ceiling was used: an L model served an M-floor card."""
    out = esc.classify(_grade("m"), "sk-l-internal")
    assert out["escalation_state"] == esc.ESCALATED
    assert out["escalation_floor_class"] == "m"
    assert out["escalation_served_class"] == "l"


def test_served_class_at_the_floor_is_within_floor():
    out = esc.classify(_grade("m"), "sk-m-internal")
    assert out["escalation_state"] == esc.WITHIN_FLOOR


def test_served_class_below_the_floor_is_within_floor_not_escalated():
    """Below the floor is a REFUSAL question (floor is hard), handled upstream.
    It is emphatically not an escalation, and must never inflate the rate."""
    out = esc.classify(_grade("l"), "sk-s-internal")
    assert out["escalation_state"] == esc.WITHIN_FLOOR


def test_unobserved_served_model_is_its_own_state_not_within_floor():
    """THE load-bearing test. model_served is None on every row written today.
    Reading that as within_floor would report zero escalation forever and the
    number would look like good news."""
    out = esc.classify(_grade("m"), None)
    assert out["escalation_state"] == esc.UNOBSERVED
    assert out["escalation_state"] != esc.WITHIN_FLOOR
    assert out["escalation_served_class"] is None
    # The floor is still known and still recorded: only the served side is dark.
    assert out["escalation_floor_class"] == "m"


def test_a_raw_model_name_names_no_class_so_it_is_unobserved():
    """Only a validated skgateway bucket id provably names a class. A bare model
    id (what a static config sends) cannot be mapped to a class without inventing
    a table, so it is unobserved rather than guessed."""
    for served in ("qwen3.6-32b", "sk-default", "claude-opus-4", "sk-xl-secrets"):
        out = esc.classify(_grade("m"), served)
        assert out["escalation_state"] == esc.UNOBSERVED, served
        assert out["escalation_served_class"] is None, served


def test_an_ungraded_card_has_no_floor_so_nothing_can_exceed_it():
    """ZERO cards carry a grade today, so this is the branch every live row takes.
    No floor means no ceiling question, which is unobserved, not within_floor."""
    for grade in (None, {}, "not-a-dict", {"sensitivity": "internal"}):
        out = esc.classify(grade, "sk-xl-internal")
        assert out["escalation_state"] == esc.UNOBSERVED, grade
        assert out["escalation_floor_class"] is None, grade


def test_classify_never_raises_on_garbage():
    """Telemetry must never turn a real build into a crash."""
    for grade, served in ((object(), object()), ({"model_class": 7}, 7),
                          ({"model_class": "zz"}, "sk-q-internal")):
        assert esc.classify(grade, served)["escalation_state"] in esc.ESCALATION_STATES


def test_state_vocabulary_is_closed_at_exactly_three():
    assert esc.ESCALATION_STATES == {esc.ESCALATED, esc.WITHIN_FLOOR, esc.UNOBSERVED}


# --------------------------------------------------------------------------- #
# 2. The reason itself, written by a human, carried untouched                 #
# --------------------------------------------------------------------------- #


def test_a_written_reason_is_carried_verbatim():
    row = esc.escalation_row(_grade("m"), "sk-xl-internal",
                             reason="  crypto review, M floor was wrong  ")
    assert row["escalation_reason"] == "crypto review, M floor was wrong"
    assert row["escalation_state"] == esc.ESCALATED


def test_an_absent_reason_is_none_never_an_invented_string():
    """A machine-written reason would poison the exact corpus D2 designates as
    the training data that corrects a bad rubric."""
    row = esc.escalation_row(_grade("m"), "sk-xl-internal", reason=None)
    assert row["escalation_reason"] is None
    row = esc.escalation_row(_grade("m"), "sk-xl-internal", reason="   ")
    assert row["escalation_reason"] is None


def test_a_reason_survives_even_when_the_state_is_not_escalated():
    """What the human wrote is evidence about the rubric regardless of whether
    this particular run could observe the served class."""
    row = esc.escalation_row(_grade("m"), None, reason="needed the big one")
    assert row["escalation_state"] == esc.UNOBSERVED
    assert row["escalation_reason"] == "needed the big one"


def test_reason_from_payload_reads_the_operator_written_field():
    assert esc.reason_from_payload({"escalation_reason": "rubric under-graded it"}) \
        == "rubric under-graded it"
    assert esc.reason_from_payload({}) is None
    assert esc.reason_from_payload(None) is None
    assert esc.reason_from_payload({"escalation_reason": 42}) is None


# --------------------------------------------------------------------------- #
# 3. Every ledger row carries the verdict, computed not trusted               #
# --------------------------------------------------------------------------- #


def test_record_run_stamps_the_four_escalation_keys_on_every_row():
    autopilot_cost.record_run(card_id="c1", repo="skos", tokens=10, cost_usd=0.1,
                              passed=True, pr="", ts="2026-08-17T00:00:00Z",
                              work_grade=_grade("m"), model_served="sk-l-internal",
                              escalation_reason="the rubric under-graded this")
    row = _rows()[0]
    assert row["escalation_state"] == esc.ESCALATED
    assert row["escalation_floor_class"] == "m"
    assert row["escalation_served_class"] == "l"
    assert row["escalation_reason"] == "the rubric under-graded this"


def test_record_run_computes_the_state_and_cannot_be_told_a_false_one():
    """record_run derives the state from (work_grade, model_served), two facts it
    already receives. A caller cannot hand it a state, so no caller can forget to
    and none can lie about it."""
    autopilot_cost.record_run(card_id="c2", repo="skos", tokens=0, cost_usd=0.0,
                              passed=False, pr="", ts="2026-08-17T00:00:00Z",
                              work_grade=_grade("xl"), model_served="sk-s-public")
    assert _rows()[0]["escalation_state"] == esc.WITHIN_FLOOR
    with pytest.raises(TypeError):
        autopilot_cost.record_run(card_id="c3", repo="skos", tokens=0, cost_usd=0.0,
                                  passed=False, pr="", ts="2026-08-17T00:00:00Z",
                                  escalation_state=esc.WITHIN_FLOOR)


def test_the_live_shape_today_records_unobserved_on_every_row():
    """model_served is None and work_grade is None on every row the orchestrator
    and the bridge write right now. The row must say so out loud."""
    autopilot_cost.record_run(card_id="c4", repo="skos", tokens=5, cost_usd=0.05,
                              passed=True, pr="", ts="2026-08-17T00:00:00Z")
    row = _rows()[0]
    assert row["escalation_state"] == esc.UNOBSERVED
    assert row["escalation_reason"] is None
    assert row["escalation_floor_class"] is None
    assert row["escalation_served_class"] is None


def test_record_run_still_never_raises_when_escalation_math_explodes(monkeypatch):
    monkeypatch.setattr(esc, "escalation_row",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    autopilot_cost.record_run(card_id="c5", repo="skos", tokens=1, cost_usd=0.01,
                              passed=True, pr="", ts="2026-08-17T00:00:00Z",
                              work_grade=_grade("m"), model_served="sk-l-internal")
    # The row is still written, and the escalation fields degrade to unobserved
    # rather than vanishing: an absent key would read the same as an old row.
    row = _rows()[0]
    assert row["escalation_state"] == esc.UNOBSERVED
    assert row["card_id"] == "c5"


# --------------------------------------------------------------------------- #
# 4. The rate: stratified, and honest about how little it saw                 #
# --------------------------------------------------------------------------- #


def _r(floor, state, reason=None):
    return {"escalation_floor_class": floor, "escalation_state": state,
            "escalation_reason": reason}


def test_rates_are_stratified_per_class_never_a_single_blended_number():
    out = esc.escalation_rates([
        _r("m", esc.ESCALATED), _r("m", esc.WITHIN_FLOOR),
        _r("xl", esc.WITHIN_FLOOR), _r("xl", esc.WITHIN_FLOOR),
    ])
    assert set(out["by_class"]) == {"m", "xl"}
    assert out["by_class"]["m"]["escalation_rate"] == pytest.approx(0.5)
    assert out["by_class"]["xl"]["escalation_rate"] == pytest.approx(0.0)


def test_the_rate_denominator_is_observed_rows_only_and_says_what_it_saw():
    """One escalation in two OBSERVED rows is 50 percent, not 10 percent of the
    twenty rows in the ledger. The blended number would hide the escalation."""
    rows = [_r("m", esc.ESCALATED), _r("m", esc.WITHIN_FLOOR)]
    rows += [_r("m", esc.UNOBSERVED)] * 18
    cls = esc.escalation_rates(rows)["by_class"]["m"]
    assert cls["rows"] == 20
    assert cls["observed"] == 2
    assert cls["unobserved"] == 18
    assert cls["escalation_rate"] == pytest.approx(0.5)
    assert cls["observed_fraction"] == pytest.approx(0.1)


def test_a_wholly_unobserved_class_reports_none_not_a_reassuring_zero():
    """This is the shape of EVERY class today. Zero would read as "no escalation
    is happening"; None reads as "we cannot tell", which is the truth."""
    cls = esc.escalation_rates([_r("m", esc.UNOBSERVED)] * 5)["by_class"]["m"]
    assert cls["escalation_rate"] is None
    assert cls["escalation_rate"] != 0
    assert cls["observed_fraction"] == pytest.approx(0.0)
    assert cls["observed"] == 0


def test_ungraded_rows_are_counted_apart_and_never_pollute_a_class():
    out = esc.escalation_rates([_r(None, esc.UNOBSERVED)] * 3 + [_r("m", esc.ESCALATED)])
    assert out["ungraded_rows"] == 3
    assert set(out["by_class"]) == {"m"}
    assert out["totals"]["rows"] == 1


def test_an_escalation_with_no_written_reason_is_counted_as_a_gap():
    """D2 requires a WRITTEN reason. An escalation without one is a hole in the
    corpus, so it is surfaced rather than silently averaged in."""
    out = esc.escalation_rates([_r("m", esc.ESCALATED, "because"),
                                _r("m", esc.ESCALATED, None)])
    assert out["by_class"]["m"]["escalated"] == 2
    assert out["by_class"]["m"]["escalated_without_reason"] == 1
    assert out["totals"]["escalated_without_reason"] == 1


def test_totals_carry_the_same_honesty_as_the_strata():
    out = esc.escalation_rates([_r("s", esc.UNOBSERVED), _r("l", esc.UNOBSERVED)])
    assert out["totals"]["escalation_rate"] is None
    assert out["totals"]["observed"] == 0
    assert out["totals"]["rows"] == 2


def test_rates_over_an_empty_ledger_are_empty_not_zero():
    out = esc.escalation_rates([])
    assert out["by_class"] == {}
    assert out["totals"]["escalation_rate"] is None


def test_old_rows_predating_this_change_read_as_unobserved_not_backfilled():
    """NO BACKFILL: rows written before S12 carry none of these keys at all. They
    must not be invented into within_floor."""
    out = esc.escalation_rates([{"card_id": "old", "cost_usd": 1.0}])
    assert out["ungraded_rows"] == 1
    assert out["by_class"] == {}


def test_escalation_summary_reads_the_real_ledger():
    autopilot_cost.record_run(card_id="c6", repo="skos", tokens=1, cost_usd=0.01,
                              passed=True, pr="", ts="2026-08-17T00:00:00Z",
                              work_grade=_grade("m"), model_served="sk-xl-internal",
                              escalation_reason="deliberate, M floor too low")
    out = autopilot_cost.escalation_summary()
    assert out["by_class"]["m"]["escalated"] == 1
    assert out["by_class"]["m"]["escalation_rate"] == pytest.approx(1.0)
    assert out["by_class"]["m"]["escalated_without_reason"] == 0


# --------------------------------------------------------------------------- #
# 5. REQUIRED DELIVERABLE: nothing reads this to make a routing decision      #
# --------------------------------------------------------------------------- #

_SRC = Path(esc.__file__).resolve().parent

#: Every module that can influence WHICH model serves a card. If the escalation
#: vocabulary ever appears in one of these, the reporting seam has become a
#: control seam and this repo has grown the autotuner card 09573989 AC6 forbids.
_ROUTING_MODULES = ("buckets.py", "grading.py", "sensitivity.py", "engineering.py",
                    "resolver.py", "harness.py", "claude_code.py", "direct.py",
                    "fleet_dispatch.py", "sandbox_proxy.py", "autoscale.py")

_VOCAB = ("escalation_reason", "escalation_state", "escalation_floor_class",
          "escalation_served_class", "from .escalation", "from skharness.autocode.escalation",
          "import escalation")


def test_no_routing_module_mentions_the_escalation_vocabulary():
    offenders = []
    for name in _ROUTING_MODULES:
        p = _SRC / name
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8")
        for token in _VOCAB:
            if token in text:
                offenders.append(f"{name}: {token}")
    assert offenders == [], (
        "escalation is a REPORTING seam. These routing modules reference it, which "
        f"means something can route on it: {offenders}")


def test_the_routing_module_list_is_not_silently_empty():
    """Negative control for the test above: an assertion over files that do not
    exist passes vacuously and certifies nothing."""
    present = [n for n in _ROUTING_MODULES if (_SRC / n).exists()]
    assert len(present) >= 8, present
    # And positive control: the token IS findable by this method where it lives.
    assert "escalation_reason" in (_SRC / "escalation.py").read_text(encoding="utf-8")


def test_the_dispatch_decision_is_byte_identical_with_and_without_escalation():
    """The behavioural proof. EngineeringExecutor._dispatch_model is the single
    function that chooses a card's model. Feed it a payload carrying every
    escalation field, at every state, and it must return exactly what it returns
    for the same payload with none of them."""
    from skharness.autocode.engineering import EngineeringExecutor
    from skharness.autocode.types import WorkItem

    def _wi(payload):
        return WorkItem(kind="engineering", ref="t1", source="coord", repo="skos",
                        payload=payload)

    for grade in (None, _grade("s", "public"), _grade("xl", "secret")):
        base = {"id": "t1", "work_grade": grade}
        clean = EngineeringExecutor._dispatch_model(None, _wi(dict(base)))
        for state in esc.ESCALATION_STATES:
            loaded = dict(base, escalation_reason="please use the biggest model",
                          escalation_state=state, escalation_floor_class="s",
                          escalation_served_class="xl")
            assert EngineeringExecutor._dispatch_model(None, _wi(loaded)) == clean


def test_the_requested_model_the_orchestrator_records_ignores_escalation_too():
    """The orchestrator's other model-facing decision, held to the same rule."""
    from types import SimpleNamespace

    from skharness.autocode import orchestrator as orch
    from skharness.autocode.types import WorkItem

    harness = SimpleNamespace(name="claude-code", model="sk-default")
    for grade in (None, _grade("m")):
        base = {"id": "t1", "work_grade": grade}
        clean = orch._model_requested(
            WorkItem(kind="engineering", ref="t1", source="coord", repo="skos",
                     payload=dict(base)), harness)
        loaded = dict(base, escalation_reason="bigger please",
                      escalation_state=esc.ESCALATED, escalation_served_class="xl")
        assert orch._model_requested(
            WorkItem(kind="engineering", ref="t1", source="coord", repo="skos",
                     payload=loaded), harness) == clean


#: Everything in buckets.py that CONSTRUCTS a routing address. escalation.py may
#: read the bucket grammar (BUCKET_RE, BUCKET_CLASSES) to parse a class out of an
#: id, which is inert; calling one of these would make it a producer of routing.
_ADDRESS_PRODUCERS = {"bucket_for_payload", "bucket_for_grade", "bucket_id",
                      "attach_dispatch_model", "ungraded_floor_bucket",
                      "dispatch_model_of", "validate_bucket"}


def test_the_escalation_module_cannot_address_a_bucket():
    """Parsed as code, not grepped as text: a docstring naming a function is not
    a call to it, and a test that cannot tell them apart will eventually be
    silenced by someone deleting a comment."""
    import ast

    tree = ast.parse((_SRC / "escalation.py").read_text(encoding="utf-8"))

    called = set()
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            called.add(fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", ""))
        elif isinstance(node, ast.ImportFrom):
            imported |= {a.name for a in node.names}
    assert not (called & _ADDRESS_PRODUCERS), called & _ADDRESS_PRODUCERS
    assert not (imported & _ADDRESS_PRODUCERS), imported & _ADDRESS_PRODUCERS
    # Positive control: the AST walk really does see this module's calls.
    assert "match" in called and "escalation_rates" not in _ADDRESS_PRODUCERS


#: The grading half of protected._ALWAYS_PROTECTED (protected.py:47-58): the
#: rubric, the deterministic exposure rules, grade-to-trust-zone addressing, the
#: vendored enums and the calibration reference. AC4 forbids touching any of them.
_GRADING_FLOOR = ("*/skharness/autocode/grading.py", "*/skharness/autocode/sensitivity.py",
                  "*/skharness/autocode/buckets.py",
                  "*/autocode/data/joule-grade-vocabulary.json",
                  "*/tests/data/joule-economy-golden-set-*.json")


def test_no_file_on_the_grading_floor_was_modified():
    """AC4, checked against origin/main so it covers this branch AND its base."""
    import subprocess
    from fnmatch import fnmatch

    from skharness.autocode import protected

    # Positive control: every glob asserted here really is on the hard floor, so
    # a future edit to protected.py cannot quietly shrink what this test guards.
    for g in _GRADING_FLOOR:
        assert g in protected._ALWAYS_PROTECTED, g

    repo = _SRC.parents[2]
    changed: list[str] = []
    for args in (["diff", "--name-only", "origin/main...HEAD"],
                 ["diff", "--name-only", "HEAD"]):
        proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
        if proc.returncode != 0:
            pytest.skip(f"git unavailable or no origin/main: {proc.stderr.strip()[:80]}")
        changed += proc.stdout.split()
    assert changed, "empty diff means this assertion certifies nothing"
    hits = [p for p in changed for g in _GRADING_FLOOR if fnmatch("/" + p, g)]
    assert hits == [], hits
