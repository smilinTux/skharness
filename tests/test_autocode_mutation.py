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

#: The ONE live-path producer (S26, card 788425b8). engineering.py holds the
#: single grade-time site with a live worktree and the diff in hand, so it is
#: the only place the probe CAN run. That makes it a routing module that names
#: the label, which is exactly the shape this section exists to forbid, so the
#: exemption is drawn as narrowly as it can be drawn rather than by dropping the
#: module off the list:
#:
#:   * it may name the raw ``mutation_report``, and reach the module through its
#:     NAMESPACE (``from . import mutation``, then ``mutation.probe``);
#:   * it may NOT name ``mutation_state``, the three-value vocabulary, or any
#:     classifier entry point, and may not import names OUT of the module. It
#:     produces COUNTS and never a verdict, so the verdict stays derived by
#:     ``record_run``, which is what stops a producer stamping a state that
#:     disagrees with its own numbers;
#:   * it may not READ the label back.
#:
#: The last two are proved by AST rather than by grep, so the one module that
#: has to explain this seam stays free to explain it in prose. Every other
#: module on _ROUTING_MODULES stays banned outright and is scanned raw.
_SHADOW_WRITE_SITE = "engineering.py"

#: The ONLY files allowed to name the vocabulary: the module itself, the ledger
#: writer that derives the column, the one orchestrator function that forwards a
#: report to that writer, the dataclass that carries it, and (S26) the one
#: executor that produces it. All five are WRITE paths. Not one of them returns
#: a decision.
_ALLOWED = {"mutation.py", "autopilot_cost.py", "orchestrator.py", "types.py",
            _SHADOW_WRITE_SITE}


def test_no_routing_module_mentions_the_mutation_vocabulary():
    """Every routing module except the one producer, scanned RAW.

    Raw text is the strictest possible scan and it is kept for all of these,
    because none of them has any business even mentioning the label. The one
    producer is scanned by AST instead, in the two tests below: it has to be
    free to EXPLAIN the seam at length in prose, and this section's own rule
    (see test_the_mutation_module_cannot_address_a_bucket_or_merge_anything) is
    that a guard which cannot tell a docstring from an identifier ends up
    silenced by someone deleting a comment.
    """
    offenders = []
    for name in _ROUTING_MODULES:
        p = _SRC / name
        if not p.exists() or name == _SHADOW_WRITE_SITE:
            continue
        text = p.read_text(encoding="utf-8")
        offenders += [f"{name}: {t}" for t in _VOCAB if t in text]
    assert offenders == [], (
        "mutation is a SHADOW label. These routing modules reference it, which "
        f"means something can route on it: {offenders}")
    # The exempted module is not simply unguarded: it is guarded by AST below.
    assert _SHADOW_WRITE_SITE in _ROUTING_MODULES
    assert (_SRC / _SHADOW_WRITE_SITE).exists()


def _code_names(path) -> tuple[set, list]:
    """Every identifier, keyword-arg name and string constant that appears in
    the CODE of *path*, plus every `from ...mutation import x` it performs.

    Docstrings and comments are structurally out of reach: an `ast.Constant`
    that is a bare expression statement (a docstring) is skipped, and comments
    never reach the AST at all.
    """
    import ast

    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    docstrings = {id(n.value) for n in ast.walk(tree)
                  if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)}
    names: set = set()
    deep_imports: list = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.keyword) and node.arg:
            names.add(node.arg)
        elif isinstance(node, ast.alias):
            names.add(node.name.rsplit(".", 1)[-1])
            names.add(node.asname or "")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docstrings:
            names.add(node.value)
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("mutation"):
            deep_imports.append(f"line {node.lineno}: from .{node.module} import ...")
    return names, deep_imports


def test_the_shadow_write_site_is_still_forbidden_the_verdict_vocabulary():
    """Negative control for the exemption: the allowance must be a keyhole, not
    a door.

    The producer may name the raw REPORT. It must not be able to name a STATE,
    the three-value vocabulary, or any of the classifier entry points, because
    a producer that can compute a verdict is one line from branching on it and
    the "derived by the writer" guarantee becomes advisory. It must also not
    reach INTO the module (`from .mutation import ...`): going through the
    module namespace is what keeps `classify` and the state constants out of its
    local scope entirely.
    """
    names, deep = _code_names(_SRC / _SHADOW_WRITE_SITE)
    banned = {"mutation_state", "survived_clean", "mutants_survived",
              "MUTATION_STATES", "SURVIVED_CLEAN", "MUTANTS_SURVIVED", "UNOBSERVED",
              "classify", "mutation_row", "unobserved_row", "mutation_rates"}
    assert not (names & banned), names & banned
    assert deep == [], deep
    # Positive control: this walk really does see the tokens it is checking, so
    # it fails loudly rather than passing vacuously if the seam is refactored.
    assert "mutation_report" in names and "probe" in names


def test_the_shadow_write_site_writes_the_label_and_never_reads_it():
    """The structural proof that the ONE producer cannot become a consumer.

    ``mutation_report`` may appear ONLY as a keyword argument (constructing the
    GateResult that carries it outward). An attribute load, or the name used as
    a string key, would be a READ, and a read is the first half of a routing
    decision.
    """
    import ast

    tree = ast.parse((_SRC / _SHADOW_WRITE_SITE).read_text(encoding="utf-8"))
    writes, reads = 0, []
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "mutation_report":
            writes += 1
        elif isinstance(node, ast.Attribute) and node.attr == "mutation_report":
            reads.append(f"attribute access at line {node.lineno}")
        elif isinstance(node, ast.Constant) and node.value in (
                "mutation_report", "mutation_state"):
            reads.append(f"string key {node.value!r} at line {node.lineno}")
    assert reads == [], (
        f"{_SHADOW_WRITE_SITE} READS the shadow label: {reads}. It is a "
        "producer; a producer that reads is one refactor from a consumer.")
    assert writes >= 1, "the producer stopped stamping the label at all"


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


def test_exactly_one_module_invokes_the_probe_and_it_is_the_shadow_write_site():
    """The caller count is PINNED, not merely bounded below.

    This assertion used to read ``callers == []``, which was true and was the
    problem: S23 shipped the probe with no caller at all, so the epic's flagship
    artifact was inert (instance ELEVEN of the very failure class this epic
    catalogued, card bb536f68). S26 added the one legitimate caller, so the
    assertion is TIGHTENED rather than deleted: exactly one caller, and it is
    the shadow write site.

    Both halves matter. ``len == 1`` is what keeps the probe from becoming a
    cost paid on several paths; ``== [_SHADOW_WRITE_SITE]`` is what keeps it
    from moving to a path that decides something. A second consumer appearing
    anywhere in the package is still red, which is the property the original
    assertion was protecting.
    """
    import ast

    callers = []
    for p in sorted(Path(_SRC).parents[1].rglob("*.py")):
        if "__pycache__" in p.parts or p.name == "mutation.py":
            continue
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8", errors="replace"))):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "probe":
                callers.append(p.name)
    assert callers == [_SHADOW_WRITE_SITE], callers


def test_the_module_runs_standalone_so_a_human_can_produce_real_data():
    """The label is worth building only if somebody can actually run it. Since
    S26 the autopilot runs it too, but the standalone entry point stays: it is
    how a human produces a reading over an arbitrary branch without waiting for
    a gated build, and section 6 does not make it redundant."""
    proc = subprocess.run([sys.executable, "-m", "skharness.autocode.mutation", "--help"],
                          capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": str(Path(_SRC).parents[1])})
    assert proc.returncode == 0, proc.stderr
    assert "--worktree" in proc.stdout


# --------------------------------------------------------------------------- #
# 6. S26 (card 788425b8): the LIVE PATH                                       #
# --------------------------------------------------------------------------- #
#
# Section 2 already proves the probe works when called. That is NOT what was
# missing. What was missing is that nothing called it: `grep` across the package
# returned only docstring references, so 43 green tests and a merged module
# produced byte-identical behaviour to the module not existing at all. This epic
# catalogued that exact class ten times over (card bb536f68) and then formed an
# eleventh instance around its own flagship deliverable.
#
# So a test here that calls `probe()` directly would prove nothing S23's tests
# did not already prove. Every test in this section drives
# `EngineeringExecutor.run()`, over a REAL worktree with REAL source and REAL
# tests, and reads the result off the LEDGER ROW the orchestrator writes.
# ---------------------------------------------------------------------------


def _live_gated_build(tmp_path, monkeypatch, tests_source: str):
    """One REAL gated build through `run()`, then its outcome row.

    Only the outside world is stubbed (git worktree creation, the diff, the head
    sha, external CI, the coverage instrument, and the LLM). The executor, the
    twin gate, the probe and the ledger writer are all the real thing, and the
    worktree on disk really does hold the source the probe mutates and the tests
    that judge the mutants.

    Returns (GateResult, [ledger rows]).
    """
    import types as _t
    from unittest.mock import MagicMock

    from skharness.autocode import engineering as eng
    from skharness.autocode import orchestrator as orch
    from skharness.autocode.types import GateResult, HarnessResult, RepoSpec, WorkItem

    wt = _wt(tmp_path, _GOOD_SRC, tests_source)
    repo = RepoSpec(
        name="calc", path=str(tmp_path), base_branch="main",
        integration_branch="main", test_cmd="pytest",
        ci=f"local:{sys.executable} -m pytest -q -p no:cacheprovider")
    cfg = _t.SimpleNamespace(repo_map={"calc": repo}, automerge_repos=[])
    ex = eng.EngineeringExecutor(cfg, board=MagicMock(), journal=MagicMock(),
                                 digest=MagicMock(), agent_name="autopilot")
    ex.journal.run_id = "run-s26"
    monkeypatch.setattr(ex, "make_worktree", lambda item, repo: str(wt))
    monkeypatch.setattr(ex, "_diff", lambda repo, w: _DIFF)
    monkeypatch.setattr(ex, "_head_sha", lambda w: "sha1")
    monkeypatch.setattr(eng, "external_ci_verdict", lambda *a, **k: "green")
    monkeypatch.setattr(eng, "diff_coverage", lambda *a, **k: 0.95)

    harness = MagicMock(name="harness")
    harness.name = "claude-code"
    harness.run_task.return_value = HarnessResult(ok=True, artifact=None, tokens=1,
                                                  cost_usd=0.0, raw={})
    harness.grade.return_value = GateResult(
        score=5, passed=True, notes="done <promise>COMPLETE</promise>", artifact="pr")

    item = WorkItem(kind="engineering", ref="s26-live", source="coord", repo=None,
                    payload={"tags": ["repo:calc"], "title": "over()",
                             "description": "d", "acceptance": ["a"]})
    # The ledger goes to tmp_path. Nothing in this suite may write to the real
    # fleet ledger or the real wallet; run() never settles, and finalize (the one
    # path that mints) is deliberately not called.
    monkeypatch.setenv("SKAI_COST_DIR", str(tmp_path / "cost"))
    monkeypatch.setattr(orch, "_now_iso", lambda: "2026-08-17T00:00:00Z")

    result = ex.run(item, harness)
    orch.record_outcome_row(item, terminal_state="finalized", run_id="run-s26",
                            result=result)
    ledger = tmp_path / "cost" / "ledger.jsonl"
    rows = [json.loads(ln)
            for ln in ledger.read_text(encoding="utf-8").splitlines() if ln]
    return result, rows


def test_a_real_gated_build_produces_a_non_null_report_on_the_outcome_row(
        tmp_path, monkeypatch):
    """THE acceptance test for this card (AC1).

    A real gated build, driven through `run()`, must reach the ledger carrying a
    label the worker did not author. Before S26 this row read `unobserved` with
    reason `not_run` on every build that ever ran, forever, because no caller
    existed. Delete the probe call in `engineering.py` and this test goes red.
    """
    result, rows = _live_gated_build(tmp_path, monkeypatch, _GOOD_TESTS)

    assert result.passed is True and result.outcome == "pass"
    # The GateResult really carries a report, and the probe really ran.
    assert result.mutation_report is not None, "no probe ran on the live path"
    assert result.mutation_report["unobserved_reason"] is None, result.mutation_report
    assert result.mutation_report["mutants"] >= 1, result.mutation_report

    # And it survives the journey to the row a human actually reads.
    row = rows[0]
    assert row["card_id"] == "s26-live"
    assert row["mutation_state"] == mut.SURVIVED_CLEAN, row
    assert row["mutation_unobserved_reason"] is None, row
    assert row["mutation_killed"] >= 1 and row["mutation_survived"] == 0, row


def test_the_live_label_discriminates_between_two_builds_the_gate_cannot_tell_apart(
        tmp_path, monkeypatch):
    """The negative control, and the whole reason the label exists.

    Same source, same green CI, same coverage, same 5/5 grade, same twin-gate
    pass. The ONLY difference is the quality of the tests the worker wrote, and
    the twin gate is structurally blind to it because its CI and coverage arms
    are satisfied BY those tests. The mutation label is not: it says
    `mutants_survived` where the gate says pass.

    If this test ever agrees with the one above, the live path has stopped
    measuring anything and is only decorating rows.
    """
    result, rows = _live_gated_build(tmp_path, monkeypatch, _WEAK_TESTS)

    assert result.passed is True and result.outcome == "pass"   # the gate agrees
    assert rows[0]["mutation_state"] == mut.MUTANTS_SURVIVED, rows[0]
    assert rows[0]["mutation_survived"] >= 1, rows[0]


def test_a_build_the_probe_cannot_observe_records_unobserved_and_still_passes(
        tmp_path, monkeypatch):
    """Cost/robustness (AC4). A repo whose CI cannot serve the probe (nothing
    local to run) must still finish its build normally and write an HONEST
    `unobserved`, never a clean sweep and never an exception.
    """
    import types as _t
    from unittest.mock import MagicMock

    from skharness.autocode import engineering as eng
    from skharness.autocode import orchestrator as orch
    from skharness.autocode.types import GateResult, HarnessResult, RepoSpec, WorkItem

    wt = _wt(tmp_path, _GOOD_SRC, _GOOD_TESTS)
    repo = RepoSpec(name="calc", path=str(tmp_path), base_branch="main",
                    integration_branch="main", test_cmd="pytest", ci="none")
    cfg = _t.SimpleNamespace(repo_map={"calc": repo}, automerge_repos=[])
    ex = eng.EngineeringExecutor(cfg, board=MagicMock(), journal=MagicMock(),
                                 digest=MagicMock(), agent_name="autopilot")
    ex.journal.run_id = "run-s26"
    monkeypatch.setattr(ex, "make_worktree", lambda item, repo: str(wt))
    monkeypatch.setattr(ex, "_diff", lambda repo, w: _DIFF)
    monkeypatch.setattr(ex, "_head_sha", lambda w: "sha1")
    monkeypatch.setattr(eng, "external_ci_verdict", lambda *a, **k: "green")
    monkeypatch.setattr(eng, "diff_coverage", lambda *a, **k: 0.95)
    harness = MagicMock(name="harness")
    harness.name = "claude-code"
    harness.run_task.return_value = HarnessResult(ok=True, artifact=None, tokens=1,
                                                  cost_usd=0.0, raw={})
    harness.grade.return_value = GateResult(
        score=5, passed=True, notes="done <promise>COMPLETE</promise>", artifact="pr")
    item = WorkItem(kind="engineering", ref="s26-blind", source="coord", repo=None,
                    payload={"tags": ["repo:calc"], "title": "over()",
                             "description": "d", "acceptance": ["a"]})
    monkeypatch.setenv("SKAI_COST_DIR", str(tmp_path / "cost"))
    monkeypatch.setattr(orch, "_now_iso", lambda: "2026-08-17T00:00:00Z")

    result = ex.run(item, harness)
    assert result.passed is True                      # the build is unaffected
    orch.record_outcome_row(item, terminal_state="finalized", run_id="run-s26",
                            result=result)
    row = json.loads((tmp_path / "cost" / "ledger.jsonl")
                     .read_text(encoding="utf-8").splitlines()[0])
    assert row["mutation_state"] == mut.UNOBSERVED
    assert row["mutation_unobserved_reason"] == "no_test_command"


def test_a_build_the_gate_rejects_pays_nothing_for_the_probe(tmp_path, monkeypatch):
    """Cost bound (AC4), stated as an observation rather than a promise.

    The probe costs one scoped suite run per mutant, so it must run at most once
    per BUILD and only on a round that is about to end it with a green suite. A
    red-CI build never converges and never reaches a terminal green round, so it
    must pay ZERO probe runs. It also cannot produce a verdict even in
    principle: the probe requires a green baseline, so paying for it there would
    buy an `unobserved` row at the price of a full suite run per round.
    """
    import types as _t
    from unittest.mock import MagicMock

    from skharness.autocode import engineering as eng
    from skharness.autocode import mutation as live_mut
    from skharness.autocode.types import GateResult, HarnessResult, RepoSpec, WorkItem

    calls = []
    real_probe = live_mut.probe
    monkeypatch.setattr(live_mut, "probe",
                        lambda *a, **k: (calls.append(1), real_probe(*a, **k))[1])

    wt = _wt(tmp_path, _GOOD_SRC, _GOOD_TESTS)
    repo = RepoSpec(
        name="calc", path=str(tmp_path), base_branch="main",
        integration_branch="main", test_cmd="pytest",
        ci=f"local:{sys.executable} -m pytest -q -p no:cacheprovider")
    cfg = _t.SimpleNamespace(repo_map={"calc": repo}, automerge_repos=[])
    ex = eng.EngineeringExecutor(cfg, board=MagicMock(), journal=MagicMock(),
                                 digest=MagicMock(), agent_name="autopilot")
    ex.journal.run_id = "run-s26"
    monkeypatch.setattr(ex, "make_worktree", lambda item, repo: str(wt))
    monkeypatch.setattr(ex, "_diff", lambda repo, w: _DIFF)
    monkeypatch.setattr(ex, "_head_sha", lambda w: "sha1")
    monkeypatch.setattr(eng, "external_ci_verdict", lambda *a, **k: "red")
    monkeypatch.setattr(eng, "diff_coverage", lambda *a, **k: 0.95)
    harness = MagicMock(name="harness")
    harness.name = "claude-code"
    harness.run_task.return_value = HarnessResult(ok=True, artifact=None, tokens=1,
                                                  cost_usd=0.0, raw={})
    harness.grade.return_value = GateResult(score=3, passed=False, notes="nope",
                                            artifact=None)
    item = WorkItem(kind="engineering", ref="s26-red", source="coord", repo=None,
                    payload={"tags": ["repo:calc"], "title": "over()",
                             "description": "d", "acceptance": ["a"]})

    result = ex.run(item, harness)
    assert result.passed is False
    assert calls == [], f"the probe ran {len(calls)} times on a build that never passed"
    assert result.mutation_report is None


def test_the_probe_is_called_at_most_once_per_build(tmp_path, monkeypatch):
    """Positive control for the cost bound above: on a build that DOES pass,
    the probe runs exactly once, not once per round and not once per return."""
    from skharness.autocode import mutation as live_mut

    calls = []
    real_probe = live_mut.probe
    monkeypatch.setattr(live_mut, "probe",
                        lambda *a, **k: (calls.append(1), real_probe(*a, **k))[1])
    _live_gated_build(tmp_path, monkeypatch, _GOOD_TESTS)
    assert calls == [1], f"expected exactly one probe per build, got {len(calls)}"


def test_a_probe_that_explodes_never_breaks_the_build(tmp_path, monkeypatch):
    """The never-raises discipline `_record_attempt` and `record_run` already
    follow, extended to the probe. A shadow telemetry bug must never turn a
    passing build into a crash, and it must never leave a reassuring row."""
    from skharness.autocode import mutation as live_mut

    def boom(*a, **k):
        raise RuntimeError("probe bug")

    monkeypatch.setattr(live_mut, "probe", boom)
    result, rows = _live_gated_build(tmp_path, monkeypatch, _GOOD_TESTS)
    assert result.passed is True and result.outcome == "pass"
    assert rows[0]["mutation_state"] == mut.UNOBSERVED
    assert rows[0]["mutation_state"] != mut.SURVIVED_CLEAN
