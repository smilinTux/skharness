"""S23 (card 33c50540): the WORKER-INDEPENDENT outcome label.

The epic refused to close a learning loop over its own grades. The load-bearing
ground for that refusal is that the twin gate's CI and coverage arms are
satisfied by tests THE WORKER ITSELF AUTHORED, so a passing score partly
measures the worker's ability to write a passing test. The verdict document
(S13, dab87c81) names what would DEFEAT that ground: an outcome label the
worker did not author. This suite is the proof that the label built here has
that property and ONLY that property, that it stays honest about what it could
not see, and that nothing anywhere routes on it.

Five sections, matching the card's acceptance criteria:

  1. the three-state vocabulary and the classifier that produces it
  2. the probe: changed lines only, capped, timed out, and honest when it stops
  3. the ledger row (AC1, AC3): the state rides the EXISTING outcome row and is
     DERIVED by the writer, so no caller can stamp one that disagrees
  4. reporting, which must refuse to invent a rate out of an empty denominator
  5. AC4: nothing consumes it, proved statically AND behaviourally

No em/en dashes anywhere (SKWorld hard rule).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from skharness.autocode import mutation as mut

_SRC = Path(mut.__file__).parent


# --------------------------------------------------------------------------- #
# 1. The vocabulary and the classifier                                        #
# --------------------------------------------------------------------------- #


def _report(**kw) -> dict:
    """A complete, coherent probe report; override one fact per test."""
    base = {"mutants": 4, "killed": 4, "survived": 0, "unobserved_mutants": 0,
            "sites": 4, "complete": True, "unobserved_reason": None,
            "seconds": 1.0, "cap": 20, "timeout_s": 600}
    base.update(kw)
    return base


def test_state_vocabulary_is_closed_at_exactly_three():
    assert mut.MUTATION_STATES == frozenset(
        {"survived_clean", "mutants_survived", "unobserved"})
    assert len(mut.MUTATION_STATES) == 3


def test_a_complete_run_that_killed_every_mutant_is_clean():
    row = mut.mutation_row(_report())
    assert row["mutation_state"] == mut.SURVIVED_CLEAN
    assert row["mutation_killed"] == 4
    assert row["mutation_unobserved_reason"] is None


def test_one_survivor_is_mutants_survived():
    row = mut.mutation_row(_report(killed=3, survived=1))
    assert row["mutation_state"] == mut.MUTANTS_SURVIVED
    assert row["mutation_survived"] == 1


def test_a_survivor_counts_even_when_the_run_was_incomplete():
    """The asymmetry, and it is deliberate. A survivor is POSITIVE evidence:
    one mutant that the worker's own tests did not notice is a fact about the
    diff whether or not every other site was tried. Absence is not."""
    row = mut.mutation_row(_report(mutants=2, killed=1, survived=1, sites=40,
                                   complete=False))
    assert row["mutation_state"] == mut.MUTANTS_SURVIVED


def test_an_incomplete_run_with_no_survivor_is_unobserved_not_clean():
    """The other half of the asymmetry, and the whole point of the card. A
    sample that found nothing has not shown the diff is clean, it has shown
    that the sample was small. Reporting that as clean is exactly the
    optimistic partial run the card forbids."""
    row = mut.mutation_row(_report(mutants=2, killed=2, survived=0, sites=40,
                                   complete=False))
    assert row["mutation_state"] == mut.UNOBSERVED
    assert row["mutation_unobserved_reason"] == "incomplete"


def test_zero_mutants_is_unobserved_not_a_clean_sweep():
    """A diff with no mutable changed line produced no measurement at all.
    Folding it into survived_clean would make an unmeasured docs card
    indistinguishable from a diff whose tests killed everything."""
    row = mut.mutation_row(_report(mutants=0, killed=0, survived=0, sites=0))
    assert row["mutation_state"] == mut.UNOBSERVED
    assert row["mutation_unobserved_reason"] == "no_mutants_executed"


def test_a_mutant_that_could_not_be_judged_blocks_a_clean_verdict():
    """A per-mutant timeout is an UNKNOWN, and an unknown must never resolve
    toward the good direction. Counting a hung mutant as killed would let a
    hanging suite report as a clean sweep."""
    row = mut.mutation_row(_report(mutants=4, killed=3, survived=0,
                                   unobserved_mutants=1))
    assert row["mutation_state"] == mut.UNOBSERVED
    assert row["mutation_unobserved_reason"] == "mutants_unobserved"


def test_an_explicit_unobserved_reason_wins_over_the_counts():
    row = mut.mutation_row(_report(unobserved_reason="baseline_red"))
    assert row["mutation_state"] == mut.UNOBSERVED
    assert row["mutation_unobserved_reason"] == "baseline_red"


def test_counts_that_do_not_add_up_are_unobserved_never_guessed():
    row = mut.mutation_row(_report(mutants=4, killed=1, survived=0))
    assert row["mutation_state"] == mut.UNOBSERVED
    assert row["mutation_unobserved_reason"] == "incoherent_counts"


def test_no_report_at_all_is_unobserved_with_a_reason_that_says_so():
    for absent in (None, "", 7, [], {}):
        row = mut.mutation_row(absent)
        assert row["mutation_state"] == mut.UNOBSERVED, absent
        assert row["mutation_unobserved_reason"] == "not_run", absent


def test_classify_never_raises_on_garbage():
    for junk in (object(), {"mutants": "four"}, {"killed": None}, [1, 2]):
        row = mut.mutation_row(junk)
        assert row["mutation_state"] in mut.MUTATION_STATES


def test_every_row_carries_the_same_seven_keys_whatever_the_state():
    keys = {"mutation_state", "mutation_mutants", "mutation_killed",
            "mutation_survived", "mutation_sites", "mutation_complete",
            "mutation_unobserved_reason"}
    for rep in (None, _report(), _report(survived=1, killed=3),
                _report(unobserved_reason="no_test_command")):
        assert set(mut.mutation_row(rep)) == keys


# --------------------------------------------------------------------------- #
# 2. The probe: changed lines only, bounded, honest when it stops             #
# --------------------------------------------------------------------------- #


_DIFF = textwrap.dedent("""\
    diff --git a/calc.py b/calc.py
    --- a/calc.py
    +++ b/calc.py
    @@ -0,0 +1,4 @@
    +def over(n):
    +    if n > 10:
    +        return True
    +    return False
    """)


def _wt(tmp_path: Path, source: str, test_source: str) -> Path:
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "calc.py").write_text(source, encoding="utf-8")
    (wt / "test_calc.py").write_text(test_source, encoding="utf-8")
    return wt


_GOOD_SRC = "def over(n):\n    if n > 10:\n        return True\n    return False\n"
_GOOD_TESTS = ("from calc import over\n\n\n"
               "def test_over():\n"
               "    assert over(11) is True\n"
               "    assert over(10) is False\n"
               "    assert over(0) is False\n")
#: A test that exercises the function but asserts nothing that any mutant can
#: violate. This is the worker-authored passing test the whole card is about.
_WEAK_TESTS = ("from calc import over\n\n\n"
               "def test_over():\n"
               "    over(11)\n"
               "    over(0)\n")


def _repo(tmp_path: Path, ci: str = "local:true"):
    from skharness.autocode.types import RepoSpec
    return RepoSpec(name="calc", path=str(tmp_path), base_branch="main",
                    integration_branch="main", test_cmd="pytest", ci=ci)


def _cmd() -> str:
    return f"{sys.executable} -m pytest -q -p no:cacheprovider test_calc.py"


def test_sites_are_found_only_on_changed_lines():
    src = "x = 1 == 2\ny = 3 == 4\n"
    both = mut.mutation_sites("f.py", src, {1, 2})
    only_one = mut.mutation_sites("f.py", src, {1})
    assert len(both) == 2
    assert len(only_one) == 1
    assert only_one[0].line == 1


def test_no_site_is_ever_taken_from_a_string_or_a_comment():
    """A textual search would mutate the '==' inside a docstring and produce a
    mutant that changes nothing, which reads as a survivor and slanders the
    tests. Sites come from the tokenizer, so literals are structurally out."""
    src = 's = "a == b and c"  # x == y and z\nflag = True\n'
    sites = mut.mutation_sites("f.py", src, {1, 2})
    assert [s.line for s in sites] == [2]
    assert sites[0].original == "True"


def test_a_mutant_the_tests_kill_is_recorded_killed(tmp_path):
    wt = _wt(tmp_path, _GOOD_SRC, _GOOD_TESTS)
    rep = mut.probe(_repo(tmp_path), str(wt), _DIFF, test_cmd=_cmd(),
                    max_mutants=6, timeout_s=300)
    assert rep["unobserved_reason"] is None, rep
    assert rep["mutants"] >= 1 and rep["survived"] == 0, rep
    assert mut.mutation_row(rep)["mutation_state"] == mut.SURVIVED_CLEAN


def test_a_weak_test_suite_lets_a_mutant_survive_and_the_label_says_so(tmp_path):
    """The negative control for the test above, and the label's whole reason to
    exist: the same green suite, the same green CI, a DIFFERENT verdict."""
    wt = _wt(tmp_path, _GOOD_SRC, _WEAK_TESTS)
    rep = mut.probe(_repo(tmp_path), str(wt), _DIFF, test_cmd=_cmd(),
                    max_mutants=6, timeout_s=300)
    assert rep["survived"] >= 1, rep
    assert mut.mutation_row(rep)["mutation_state"] == mut.MUTANTS_SURVIVED


def test_the_probe_restores_every_file_it_touched(tmp_path):
    wt = _wt(tmp_path, _GOOD_SRC, _GOOD_TESTS)
    before = (wt / "calc.py").read_text(encoding="utf-8")
    mut.probe(_repo(tmp_path), str(wt), _DIFF, test_cmd=_cmd(), max_mutants=3,
              timeout_s=300)
    assert (wt / "calc.py").read_text(encoding="utf-8") == before


def test_a_red_baseline_is_unobserved_never_a_verdict(tmp_path):
    """If the suite is already failing, killed and survived mean nothing: every
    mutant 'dies' for a reason that has nothing to do with the mutation."""
    wt = _wt(tmp_path, _GOOD_SRC, "def test_broken():\n    assert False\n")
    rep = mut.probe(_repo(tmp_path), str(wt), _DIFF, test_cmd=_cmd(),
                    max_mutants=3, timeout_s=300)
    assert rep["unobserved_reason"] == "baseline_red"
    assert mut.mutation_row(rep)["mutation_state"] == mut.UNOBSERVED


def test_no_test_command_is_unobserved_not_clean(tmp_path):
    wt = _wt(tmp_path, _GOOD_SRC, _GOOD_TESTS)
    rep = mut.probe(_repo(tmp_path, ci="none"), str(wt), _DIFF)
    assert rep["unobserved_reason"] == "no_test_command"
    assert mut.mutation_row(rep)["mutation_state"] == mut.UNOBSERVED


def test_a_diff_with_no_changed_source_is_unobserved(tmp_path):
    wt = _wt(tmp_path, _GOOD_SRC, _GOOD_TESTS)
    docs_diff = ("--- a/README.md\n+++ b/README.md\n@@ -0,0 +1,1 @@\n+hello\n")
    rep = mut.probe(_repo(tmp_path), str(wt), docs_diff, test_cmd=_cmd())
    assert rep["unobserved_reason"] == "no_changed_source"
    assert mut.mutation_row(rep)["mutation_state"] == mut.UNOBSERVED


def test_the_mutant_cap_is_honoured_and_recorded_as_incomplete(tmp_path):
    """Cost is bounded by a hard cap. A capped run says complete=False, which
    the classifier refuses to read as clean. The card's rule: do not silently
    sample."""
    wt = _wt(tmp_path, _GOOD_SRC, _GOOD_TESTS)
    rep = mut.probe(_repo(tmp_path), str(wt), _DIFF, test_cmd=_cmd(),
                    max_mutants=1, timeout_s=300)
    assert rep["mutants"] == 1
    assert rep["sites"] > 1, rep
    assert rep["complete"] is False
    assert mut.mutation_row(rep)["mutation_state"] == mut.UNOBSERVED


def test_the_wall_clock_budget_stops_the_run_and_says_it_was_incomplete(tmp_path):
    """Negative control for the cap: the OTHER bound must also mark the run
    incomplete rather than reporting whatever it managed as the whole story."""
    wt = _wt(tmp_path, _GOOD_SRC, _GOOD_TESTS)
    rep = mut.probe(_repo(tmp_path), str(wt), _DIFF, test_cmd=_cmd(),
                    max_mutants=20, timeout_s=0)
    assert rep["complete"] is False
    assert rep["mutants"] == 0
    assert mut.mutation_row(rep)["mutation_state"] == mut.UNOBSERVED


def test_the_probe_never_raises_on_a_worktree_that_is_not_there(tmp_path):
    rep = mut.probe(_repo(tmp_path), str(tmp_path / "gone"), _DIFF,
                    test_cmd=_cmd())
    assert mut.mutation_row(rep)["mutation_state"] == mut.UNOBSERVED


# --------------------------------------------------------------------------- #
# 3. The ledger row (AC1, AC3): it RIDES the existing row, and it is DERIVED  #
# --------------------------------------------------------------------------- #


def _rows(tmp_path) -> list[dict]:
    p = Path(os.environ["SKAI_COST_DIR"]) / "ledger.jsonl"
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln]


def test_record_run_stamps_the_seven_mutation_keys_on_every_row(tmp_path, monkeypatch):
    from skharness.autocode import autopilot_cost as ac
    monkeypatch.setenv("SKAI_COST_DIR", str(tmp_path))
    ac.record_run(card_id="c1", repo="skos", tokens=1, cost_usd=0.1, passed=True,
                  pr="", ts="2026-08-16T00:00:00Z")
    row = _rows(tmp_path)[0]
    for k in ("mutation_state", "mutation_mutants", "mutation_killed",
              "mutation_survived", "mutation_sites", "mutation_complete",
              "mutation_unobserved_reason"):
        assert k in row, k


def test_no_second_store_is_created(tmp_path, monkeypatch):
    """AC3. The label rides the append-only outcome row that already exists.
    The cost dir must hold exactly the two files it held before this card."""
    from skharness.autocode import autopilot_cost as ac
    monkeypatch.setenv("SKAI_COST_DIR", str(tmp_path))
    ac.record_run(card_id="c1", repo="skos", tokens=1, cost_usd=0.1, passed=True,
                  pr="", ts="2026-08-16T00:00:00Z",
                  mutation_report=_report(killed=3, survived=1))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["ledger.jsonl"]
    assert len(_rows(tmp_path)) == 1


def test_record_run_derives_the_state_and_cannot_be_told_a_false_one(tmp_path, monkeypatch):
    """The house pattern (S12 escalation_state, PR #45 grade_*): a column that
    CAN be derived from what the writer already holds is derived by the writer.
    A caller handing in a report with a survivor cannot get a clean row."""
    from skharness.autocode import autopilot_cost as ac
    monkeypatch.setenv("SKAI_COST_DIR", str(tmp_path))
    liar = _report(killed=3, survived=1)
    liar["mutation_state"] = mut.SURVIVED_CLEAN      # ignored: not an input
    ac.record_run(card_id="c1", repo="skos", tokens=1, cost_usd=0.1, passed=True,
                  pr="", ts="2026-08-16T00:00:00Z", mutation_report=liar)
    assert _rows(tmp_path)[0]["mutation_state"] == mut.MUTANTS_SURVIVED


def test_record_run_takes_no_mutation_state_parameter():
    """Static form of the test above: if the signature accepted the state, a
    caller could stamp one, and the derived column would be advisory."""
    import inspect

    from skharness.autocode import autopilot_cost as ac
    params = set(inspect.signature(ac.record_run).parameters)
    assert "mutation_report" in params
    assert not (params & {"mutation_state", "mutation_killed", "mutation_survived"})


def test_the_live_shape_today_records_unobserved_on_every_row(tmp_path, monkeypatch):
    """The honest statement of where this stands. No producer stamps a report
    on a GateResult yet, so every row the orchestrator writes today is
    unobserved with reason not_run. That is the measurement, and the ledger
    says it out loud rather than defaulting to a reassuring clean."""
    from types import SimpleNamespace

    from skharness.autocode import orchestrator as orch
    monkeypatch.setenv("SKAI_COST_DIR", str(tmp_path))
    monkeypatch.setattr(orch, "_now_iso", lambda: "2026-08-16T00:00:00Z")
    item = SimpleNamespace(ref="c1", repo="skos", payload={"id": "c1"})
    orch.record_outcome_row(item, terminal_state="finalized", run_id="r1")
    row = _rows(tmp_path)[0]
    assert row["mutation_state"] == mut.UNOBSERVED
    assert row["mutation_unobserved_reason"] == "not_run"


def test_the_orchestrator_forwards_a_report_the_moment_one_exists(tmp_path, monkeypatch):
    """Negative control for the test above: the seam is wired, not merely
    absent. A GateResult carrying a report produces a non-unobserved row."""
    from types import SimpleNamespace

    from skharness.autocode import orchestrator as orch
    from skharness.autocode.types import GateResult
    monkeypatch.setenv("SKAI_COST_DIR", str(tmp_path))
    monkeypatch.setattr(orch, "_now_iso", lambda: "2026-08-16T00:00:00Z")
    item = SimpleNamespace(ref="c1", repo="skos", payload={"id": "c1"})
    gr = GateResult(score=5, passed=True, notes="", artifact=None, outcome="pass",
                    mutation_report=_report(killed=3, survived=1))
    orch.record_outcome_row(item, terminal_state="finalized", run_id="r1", result=gr)
    assert _rows(tmp_path)[0]["mutation_state"] == mut.MUTANTS_SURVIVED


def test_record_run_still_never_raises_when_the_mutation_math_explodes(tmp_path, monkeypatch):
    from skharness.autocode import autopilot_cost as ac
    monkeypatch.setenv("SKAI_COST_DIR", str(tmp_path))

    def boom(_report):
        raise RuntimeError("classifier bug")

    monkeypatch.setattr(mut, "mutation_row", boom)
    ac.record_run(card_id="c1", repo="skos", tokens=1, cost_usd=0.1, passed=True,
                  pr="", ts="2026-08-16T00:00:00Z", mutation_report=_report())
    row = _rows(tmp_path)[0]
    assert row["mutation_state"] == mut.UNOBSERVED
    assert row["card_id"] == "c1"       # the row survived the telemetry bug


# --------------------------------------------------------------------------- #
# 4. Reporting: no rate out of an empty denominator                          #
# --------------------------------------------------------------------------- #


def test_rates_report_what_was_observed_and_refuse_to_invent_the_rest():
    rates = mut.mutation_rates([
        {"mutation_state": "survived_clean"},
        {"mutation_state": "survived_clean"},
        {"mutation_state": "mutants_survived"},
        {"mutation_state": "unobserved"},
    ])
    assert rates["rows"] == 4
    assert rates["observed"] == 3
    assert rates["observed_fraction"] == pytest.approx(0.75)
    assert rates["survival_rate"] == pytest.approx(1 / 3)


def test_a_wholly_unobserved_corpus_reports_none_not_a_reassuring_zero():
    rates = mut.mutation_rates([{"mutation_state": "unobserved"}] * 9)
    assert rates["survival_rate"] is None
    assert rates["observed_fraction"] == 0.0


def test_rates_over_an_empty_corpus_are_empty_not_zero():
    rates = mut.mutation_rates([])
    assert rates["rows"] == 0 and rates["survival_rate"] is None


def test_old_rows_predating_this_change_read_as_unobserved_never_backfilled():
    rates = mut.mutation_rates([{"card_id": "old"}, {"card_id": "older"}])
    assert rates["unobserved"] == 2 and rates["survival_rate"] is None


def test_mutation_summary_reads_the_real_ledger(tmp_path, monkeypatch):
    from skharness.autocode import autopilot_cost as ac
    monkeypatch.setenv("SKAI_COST_DIR", str(tmp_path))
    ac.record_run(card_id="a", repo="skos", tokens=1, cost_usd=0.0, passed=True,
                  pr="", ts="2026-08-16T00:00:00Z", mutation_report=_report())
    ac.record_run(card_id="b", repo="skos", tokens=1, cost_usd=0.0, passed=True,
                  pr="", ts="2026-08-16T00:00:00Z",
                  mutation_report=_report(killed=3, survived=1))
    s = ac.mutation_summary()
    assert s["rows"] == 2 and s["observed"] == 2
    assert s["survival_rate"] == pytest.approx(0.5)


# --------------------------------------------------------------------------- #
# 5. AC4: nothing consumes it                                                 #
# --------------------------------------------------------------------------- #

#: Every module that can CHOOSE something: a model, a merge, a dispatch target,
#: a gate verdict. If any of them so much as names the mutation vocabulary, this
#: is a control seam and card 09573989 acceptance criterion 6 has been broken.
_ROUTING_MODULES = ("buckets.py", "grading.py", "sensitivity.py", "engineering.py",
                    "resolver.py", "harness.py", "direct.py", "ratify.py",
                    "fleet_dispatch.py", "sandbox_proxy.py", "autoscale.py",
                    "routing_guard.py", "remediation.py", "escalation.py",
                    "change_deploy_bridge.py")

_VOCAB = ("mutation_state", "mutation_report", "survived_clean", "mutants_survived",
          "from .mutation", "from skharness.autocode.mutation", "import mutation")

#: The ONLY files allowed to name the vocabulary: the module itself, the ledger
#: writer that derives the column, the one orchestrator function that forwards a
#: report to that writer, and the dataclass that carries it. All four are WRITE
#: paths. Not one of them returns a decision.
_ALLOWED = {"mutation.py", "autopilot_cost.py", "orchestrator.py", "types.py"}


def test_no_routing_module_mentions_the_mutation_vocabulary():
    offenders = []
    for name in _ROUTING_MODULES:
        p = _SRC / name
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8")
        offenders += [f"{name}: {t}" for t in _VOCAB if t in text]
    assert offenders == [], (
        "mutation is a SHADOW label. These routing modules reference it, which "
        f"means something can route on it: {offenders}")


def test_the_routing_module_list_is_not_silently_empty():
    """Negative control: an assertion over files that do not exist passes
    vacuously and certifies nothing."""
    present = [n for n in _ROUTING_MODULES if (_SRC / n).exists()]
    assert len(present) >= 12, present
    # Positive control: the token IS findable by this method where it lives.
    assert "mutation_state" in (_SRC / "mutation.py").read_text(encoding="utf-8")


def test_only_write_paths_anywhere_in_the_tree_name_the_vocabulary():
    """Broader than the routing list: sweep the WHOLE package, so a future
    module cannot become a consumer just by not being on a list."""
    hits = {}
    for p in sorted(Path(_SRC).parents[1].rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        if any(t in text for t in ("mutation_state", "mutation_report")):
            hits[p.name] = True
    assert set(hits) <= _ALLOWED, set(hits) - _ALLOWED
    assert "mutation.py" in hits and "autopilot_cost.py" in hits   # positive control


def test_the_twin_gate_verdict_is_byte_identical_with_and_without_the_label():
    """The behavioural proof for the gate arm. twin_gate_passed is the ONE
    pinned merge predicate; it must not be reachable from the label."""
    import inspect

    from skharness.autocode.engineering import twin_gate_passed
    from skharness.autocode.types import GateResult, RepoSpec

    repo = RepoSpec(name="skos", path="/tmp/x", base_branch="main",
                    integration_branch="main", test_cmd="pytest",
                    ci="local:true", coverage_cmd="c")
    assert "mutation" not in inspect.getsource(twin_gate_passed)
    for state in sorted(mut.MUTATION_STATES):
        rep = _report(killed=0, survived=4) if state == mut.MUTANTS_SURVIVED else _report()
        clean = GateResult(score=5, passed=True, notes="", artifact=None)
        loaded = GateResult(score=5, passed=True, notes="", artifact=None,
                            mutation_report=rep)
        assert (twin_gate_passed(loaded, "green", 1.0, repo)
                == twin_gate_passed(clean, "green", 1.0, repo)), state


def test_the_dispatch_decision_is_byte_identical_with_and_without_the_label():
    """The behavioural proof for the routing arm, mirroring the escalation
    suite: the single function that chooses a card's model must return exactly
    the same answer for a payload carrying every mutation field."""
    from skharness.autocode.engineering import EngineeringExecutor
    from skharness.autocode.types import WorkItem

    def _wi(payload):
        return WorkItem(kind="engineering", ref="t1", source="coord", repo="skos",
                        payload=payload)

    base = {"id": "t1"}
    clean = EngineeringExecutor._dispatch_model(None, _wi(dict(base)))
    for state in sorted(mut.MUTATION_STATES):
        loaded = dict(base, mutation_state=state, mutation_survived=9,
                      mutation_report=_report(killed=0, survived=9))
        assert EngineeringExecutor._dispatch_model(None, _wi(loaded)) == clean


def test_the_mutation_module_cannot_address_a_bucket_or_merge_anything():
    """Parsed as code, not grepped as text: a docstring naming a function is
    not a call to it, and a test that cannot tell them apart will eventually be
    silenced by someone deleting a comment."""
    import ast

    producers = {"bucket_for_payload", "bucket_for_grade", "bucket_id",
                 "attach_dispatch_model", "ungraded_floor_bucket",
                 "dispatch_model_of", "validate_bucket", "twin_gate_passed",
                 "finalize", "record_run", "settle", "mint", "spend"}
    tree = ast.parse((_SRC / "mutation.py").read_text(encoding="utf-8"))
    called, imported = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            called.add(fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", ""))
        elif isinstance(node, ast.ImportFrom):
            imported |= {a.name for a in node.names}
    assert not (called & producers), called & producers
    assert not (imported & producers), imported & producers
    assert "mutation_row" in called       # positive control: the walk sees calls


def test_the_probe_is_never_invoked_from_any_gate_or_dispatch_path():
    """A shadow label that the gate RUNS is still a gate cost and one refactor
    away from being a gate input. No module outside the label's own tests may
    call probe()."""
    import ast

    callers = []
    for p in sorted(Path(_SRC).parents[1].rglob("*.py")):
        if "__pycache__" in p.parts or p.name == "mutation.py":
            continue
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8", errors="replace"))):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "probe":
                callers.append(p.name)
    assert callers == [], callers


def test_the_module_runs_standalone_so_a_human_can_produce_real_data():
    """The label is only worth building if somebody can actually run it today.
    The orchestrator cannot (the one site holding a worktree is on the
    protected floor), so the module carries its own entry point."""
    proc = subprocess.run([sys.executable, "-m", "skharness.autocode.mutation", "--help"],
                          capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": str(Path(_SRC).parents[1])})
    assert proc.returncode == 0, proc.stderr
    assert "--worktree" in proc.stdout
