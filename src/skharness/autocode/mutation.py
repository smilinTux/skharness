"""mutation: the WORKER-INDEPENDENT outcome label (S23, card 33c50540).

WHY THIS EXISTS, and it is not the usual reason
------------------------------------------------
This epic REFUSED to close a learning loop over the harness's own grades. The
load-bearing ground for that refusal, quoted in substance from the S13 verdict
(dab87c81) section 4.6:

    the twin gate's CI and coverage arms are satisfied by tests THE WORKER
    ITSELF AUTHORED, so a passing score partly measures the worker's ability to
    write a passing test, and any learner trained on it ratchets downward.

The same document names what would DEFEAT that ground: an outcome label THE
WORKER DID NOT AUTHOR. Section 3.4 records that none exists. So the refusal was
never a permanent judgement, it was a DEFERRAL with a named reopen condition.
And a deferral whose reopen condition has no owner is a permanent refusal
wearing a provisional label, which is strictly worse than either an honest
permanent refusal or an honest attempt: it stops anyone re-examining the
question while looking like it invites re-examination.

This module is that reopen condition, built. Mutation testing over changed
lines. The property that matters, and the ONLY one that matters:

    THE WORKER CANNOT AUTHOR THE MUTANTS.

The operators live here, in the harness, fixed. A worker cannot write a test
that satisfies them the way it can write a test that satisfies coverage, and it
cannot weaken them the way a diff can add an `omit` rule to a coverage config.
A mutant that survives is a changed line the worker's own tests did not notice,
and that fact is not something the worker produced.

SUCCESS IS NOT "THE LOOP IS CLOSED"
-----------------------------------
Success is that the refusal becomes FALSIFIABLE: after this, someone can point
at real data and argue the reopen condition is met, or point at it and argue it
is not. Before this, neither was possible.

SHADOW ONLY. THIS GATES NOTHING
--------------------------------
It records a second, independent label BESIDE ``GateResult.score``. No policy,
no gate and no dispatch decision may consume it. Making it a gate would recreate
the reward-gaming problem one level up (a worker that must satisfy a mutation
score writes tests aimed at mutants, and the label stops being independent of
the worker the moment the worker is graded on it), and card 09573989 acceptance
criterion 6 still forbids an autotuner.
``tests/test_autocode_mutation.py`` section 5 proves the seam stays shut,
statically over the WHOLE package and behaviourally over both the twin gate and
the dispatch decision.

WHY THREE STATES AND NOT TWO, and this is the load-bearing decision
--------------------------------------------------------------------
This epic invented the three-state discipline (S12 reports ``escalation_rate:
null`` beside ``observed_fraction: 0.0`` rather than "0 percent escalation",
because a two-state design would have been indistinguishable from a healthy
fleet). An epic that invented the rule and then shipped a two-state mutation
label would have learned nothing from itself. So:

  ``survived_clean``    every mutant generated over the changed lines was
                        killed, and every site was tried
  ``mutants_survived``  at least one mutant was NOT killed
  ``unobserved``        the probe did not or could not produce that verdict

THE ASYMMETRY, and it is deliberate. A survivor is POSITIVE evidence: one
mutant the worker's tests did not notice is a fact about the diff whether or not
every other site was tried. Absence of a survivor is not evidence of absence: a
capped or timed-out run that found nothing has shown the SAMPLE was small, not
that the diff is clean. So an incomplete run with no survivor is ``unobserved``,
never ``survived_clean``. An optimistic partial run reported as clean is exactly
the failure this epic exists to remove, and "a sampled label reported as
universal" is the same failure with a bigger denominator.

COST IS BOUNDED EXPLICITLY, AND THE BOUND IS RECORDED
------------------------------------------------------
Mutation testing is expensive: one full (scoped) test run per mutant. Three
bounds, all recorded on the row so a reader always knows what the number cost:

  * changed lines only, mirroring the ``diff_coverage`` discipline, so a diff of
    ten lines never triggers a repo-wide run;
  * ``max_mutants`` (a hard cap on executed mutants);
  * ``timeout_s`` (a wall-clock budget) plus ``per_mutant_timeout``.

Hitting any of them sets ``complete=False``, and the classifier refuses to read
an incomplete run as clean. That is the whole discipline: an honest
``unobserved`` is worth far more than an optimistic partial run.

No em/en dashes anywhere (SKWorld hard rule).
"""
from __future__ import annotations

import io
import logging
import os
import subprocess
import time
import tokenize
from dataclasses import dataclass

log = logging.getLogger("skharness.autocode.mutation")

#: The closed three-value vocabulary. Deliberately NOT merged into
#: types.GATE_OUTCOMES, orchestrator.TERMINAL_STATES or
#: escalation.ESCALATION_STATES: those answer "what did the gate decide", "how
#: did this item end" and "was the ceiling used". This answers a fourth
#: question, "did anything the worker did not author notice this diff", and a
#: row carries all four independently.
SURVIVED_CLEAN = "survived_clean"
MUTANTS_SURVIVED = "mutants_survived"
UNOBSERVED = "unobserved"

MUTATION_STATES: frozenset[str] = frozenset(
    {SURVIVED_CLEAN, MUTANTS_SURVIVED, UNOBSERVED})

#: Defaults, chosen for a bound rather than for completeness. A card-sized diff
#: typically yields a handful of mutable changed lines; 20 executed mutants at a
#: scoped test run each is the ceiling one card is allowed to cost. See the
#: module docstring: exceeding either bound is recorded, never absorbed.
DEFAULT_MAX_MUTANTS = 20
DEFAULT_TIMEOUT_S = 900
DEFAULT_PER_MUTANT_TIMEOUT = 120


# --------------------------------------------------------------------------- #
# The operator table. THIS is the worker-independent part.                    #
# --------------------------------------------------------------------------- #

#: Operator-token mutations. Kept small, fixed and boring on purpose: every
#: entry flips a decision a test could plausibly assert on. A bigger table buys
#: little and costs a test run per extra mutant.
_OP_MUTATIONS: dict[str, str] = {
    "==": "!=", "!=": "==",
    "<": "<=", "<=": "<", ">": ">=", ">=": ">",
    "+": "-", "-": "+", "*": "/", "//": "/", "%": "*",
    "+=": "-=", "-=": "+=",
}

#: Keyword/constant mutations. ``not`` is deleted rather than replaced, which
#: also covers ``not in`` and ``is not`` without needing multi-token matching:
#: the emitted source is validated by ``compile`` below, so a deletion that does
#: not parse is discarded rather than counted.
_NAME_MUTATIONS: dict[str, str] = {
    "True": "False", "False": "True",
    "and": "or", "or": "and",
    "not": "",
    "break": "continue", "continue": "break",
}


@dataclass(frozen=True)
class Site:
    """One mutable token on one CHANGED line. ``line`` is 1-based, ``col`` and
    ``end_col`` are 0-based character offsets into that line."""
    path: str
    line: int
    col: int
    end_col: int
    original: str
    replacement: str

    @property
    def label(self) -> str:
        return f"{self.path}:{self.line} {self.original!r}->{self.replacement!r}"


def _is_test_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name.startswith("test_") and name.endswith(".py")


def mutation_sites(path: str, source: str, changed: set[int]) -> list[Site]:
    """Every mutable token of *source* that sits on a line in *changed*.

    Sites come from ``tokenize``, never from a text search. A textual search
    would mutate the ``==`` inside a docstring or a comment and produce a mutant
    that changes no behaviour at all, which then reads as a SURVIVOR and
    slanders the tests. STRING and COMMENT tokens are structurally out of reach
    here, so that class of false survivor cannot occur.

    Returns sites in deterministic (line, col) order. Never raises: a file that
    does not tokenize yields no sites, which the caller reports honestly rather
    than treating as a clean diff.
    """
    sites: list[Site] = []
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError, ValueError):
        return []
    for tok in toks:
        line, col = tok.start
        if line not in changed or tok.start[0] != tok.end[0]:
            continue
        if tok.type == tokenize.OP:
            table = _OP_MUTATIONS
        elif tok.type == tokenize.NAME:
            table = _NAME_MUTATIONS
        else:
            continue
        rep = table.get(tok.string)
        if rep is None:
            continue
        sites.append(Site(path=path, line=line, col=col, end_col=tok.end[1],
                          original=tok.string, replacement=rep))
    sites.sort(key=lambda s: (s.line, s.col))
    return sites


def apply_site(source: str, site: Site) -> str | None:
    """The mutated source, or None when the mutation does not compile.

    Discarding a non-compiling mutant is not leniency: a mutant that cannot be
    imported is killed by the interpreter rather than by the tests, so counting
    it would inflate the kill count with something the worker's tests never did.
    """
    lines = source.splitlines(keepends=True)
    idx = site.line - 1
    if idx < 0 or idx >= len(lines):
        return None
    line = lines[idx]
    if line[site.col:site.end_col] != site.original:
        return None
    lines[idx] = line[:site.col] + site.replacement + line[site.end_col:]
    mutated = "".join(lines)
    if mutated == source:
        return None
    try:
        compile(mutated, site.path, "exec")
    except (SyntaxError, ValueError):
        return None
    return mutated


# --------------------------------------------------------------------------- #
# The classifier. PURE: it reads facts it is handed and returns a verdict.    #
# --------------------------------------------------------------------------- #


def unobserved_row(*, reason: str | None = None, report: object = None) -> dict:
    """The seven-key row for "we could not tell", used as the safe degradation.

    Exists so a failure inside the classifier still writes the keys with an
    HONEST value. An ABSENT key would read identically to a row written before
    this module existed, and the NO BACKFILL rule means those rows must stay
    distinguishable from new ones.
    """
    def _n(key):
        return report.get(key) if isinstance(report, dict) else None
    return {"mutation_state": UNOBSERVED,
            "mutation_mutants": _n("mutants"), "mutation_killed": _n("killed"),
            "mutation_survived": _n("survived"), "mutation_sites": _n("sites"),
            "mutation_complete": _n("complete"),
            "mutation_unobserved_reason": reason}


def _ints(report: dict, *keys) -> list[int] | None:
    out: list[int] = []
    for k in keys:
        v = report.get(k, 0)
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            return None
        out.append(v)
    return out


def classify(report: object) -> dict:
    """The three-state verdict over one probe report, plus its raw counts.

    The order of these checks IS the discipline, so it is written out:

      1. no report at all is ``unobserved`` with reason ``not_run``, never a
         pass. Almost every live row is this today and it says so;
      2. a reason the probe recorded itself wins over the counts: the probe
         knows why it stopped better than arithmetic does;
      3. counts that do not add up are ``unobserved``, never repaired by a
         guess. An inconsistent instrument has not measured anything;
      4. a SURVIVOR is decisive, even on an incomplete run (see the module
         docstring's asymmetry);
      5. zero executed mutants is ``unobserved``, NOT a clean sweep. A docs card
         and a diff whose tests killed everything must not write the same row;
      6. an incomplete run with no survivor is ``unobserved``. This is the rule
         that stops a sampled label being reported as universal;
      7. a mutant nobody could judge (a per-mutant timeout) blocks a clean
         verdict. An unknown must never resolve toward the good direction;
      8. only then, ``survived_clean``.

    Total: never raises. Any garbage degrades to ``unobserved``, which is the
    honest reading of "this input told us nothing".
    """
    try:
        if not isinstance(report, dict) or not report:
            return unobserved_row(reason="not_run")
        reason = report.get("unobserved_reason")
        if isinstance(reason, str) and reason.strip():
            return unobserved_row(reason=reason.strip(), report=report)
        counts = _ints(report, "mutants", "killed", "survived", "unobserved_mutants")
        if counts is None:
            return unobserved_row(reason="incoherent_counts", report=report)
        mutants, killed, survived, unjudged = counts
        if killed + survived + unjudged != mutants:
            return unobserved_row(reason="incoherent_counts", report=report)
        row = {"mutation_state": SURVIVED_CLEAN,
               "mutation_mutants": mutants, "mutation_killed": killed,
               "mutation_survived": survived,
               "mutation_sites": report.get("sites"),
               "mutation_complete": bool(report.get("complete")),
               "mutation_unobserved_reason": None}
        if survived > 0:
            row["mutation_state"] = MUTANTS_SURVIVED
            return row
        if mutants == 0:
            return unobserved_row(reason="no_mutants_executed", report=report)
        if not report.get("complete"):
            return unobserved_row(reason="incomplete", report=report)
        if unjudged > 0:
            return unobserved_row(reason="mutants_unobserved", report=report)
        return row
    except Exception:      # noqa: BLE001 - a telemetry verdict never breaks a build
        return unobserved_row(reason="classifier_error")


def mutation_row(report: object) -> dict:
    """The complete seven-key mutation record for one ledger row."""
    return classify(report)


# --------------------------------------------------------------------------- #
# The probe. The only part that does I/O.                                     #
# --------------------------------------------------------------------------- #


def _blank_report(reason: str | None, *, sites: int = 0, cap: int = 0,
                  timeout_s: int = 0, seconds: float = 0.0,
                  complete: bool = False) -> dict:
    return {"mutants": 0, "killed": 0, "survived": 0, "unobserved_mutants": 0,
            "sites": sites, "complete": complete, "unobserved_reason": reason,
            "seconds": round(seconds, 3), "cap": cap, "timeout_s": timeout_s}


def _resolve_cmd(repo, worktree: str, diff: str, test_cmd: str | None) -> str | None:
    """The command whose exit status decides killed vs survived.

    An explicit *test_cmd* is used verbatim (the caller already chose the
    scope). Otherwise the repo's ``local:`` CI command is used and SCOPED to the
    diff's own test targets, reusing ``ci.scoped_test_targets`` rather than a
    second implementation, so the probe measures the same tests the twin gate
    ran and costs a bounded fraction of a full suite.

    ``github-actions`` CI cannot serve here: a mutant is never pushed, so there
    is nothing for a remote run to observe. That is a ``no_test_command``
    (honest) and not a silent fallback to something else.
    """
    if isinstance(test_cmd, str) and test_cmd.strip():
        return test_cmd.strip()
    ci = getattr(repo, "ci", "") or ""
    if not ci.startswith("local:"):
        return None
    cmd = ci[len("local:"):].strip()
    if not cmd:
        return None
    try:
        from .ci import scoped_test_targets
        import shlex
        targets = scoped_test_targets(diff, worktree)
        if targets:
            cmd = cmd + " " + " ".join(shlex.quote(t) for t in targets)
    except Exception:      # noqa: BLE001 - an unscoped command is slower, not wrong
        log.exception("mutation._resolve_cmd: scoping failed, running unscoped")
    return cmd


def _run(cmd: str, cwd: str, timeout: int) -> str:
    """"green" | "red" | "unjudged". A timeout is ``unjudged``, NOT a kill.

    Counting a hung mutant as killed would fold an unknown toward the good
    direction, and a suite that hangs for an environmental reason would then
    report as a clean sweep. That is precisely the two-state failure this module
    was built to avoid, one level down.
    """
    try:
        proc = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "unjudged"
    except OSError:
        return "unjudged"
    return "green" if proc.returncode == 0 else "red"


def probe(repo, worktree: str, diff: str, *, test_cmd: str | None = None,
          max_mutants: int = DEFAULT_MAX_MUTANTS,
          timeout_s: int = DEFAULT_TIMEOUT_S,
          per_mutant_timeout: int = DEFAULT_PER_MUTANT_TIMEOUT) -> dict:
    """Run mutation testing over the CHANGED LINES of *diff* inside *worktree*.

    Returns a RAW report of counts and bounds. It deliberately does NOT return a
    state: the state is derived by whoever writes the row (see
    ``autopilot_cost.record_run``), so no caller can stamp a verdict that
    disagrees with its own counts. That is the house pattern S12 established for
    ``escalation_state`` and PR #45 for ``grade_size``/``grade_risk``.

    Never raises. Every failure path returns a report whose
    ``unobserved_reason`` says WHICH instrument did not produce a reading, which
    is the observation that separates "measured and clean" from "never ran".
    """
    started = time.monotonic()
    cap = max(0, int(max_mutants or 0))
    budget = max(0, int(timeout_s or 0))

    def _elapsed() -> float:
        return time.monotonic() - started

    try:
        from .ci import _changed_lines
        changed = _changed_lines(diff or "")
        sources = {p: lines for p, lines in changed.items()
                   if lines and p.endswith(".py") and not _is_test_file(p)
                   and os.path.isfile(os.path.join(worktree, p))}
        if not sources:
            return _blank_report("no_changed_source", cap=cap, timeout_s=budget,
                                 seconds=_elapsed())

        cmd = _resolve_cmd(repo, worktree, diff, test_cmd)
        if not cmd:
            return _blank_report("no_test_command", cap=cap, timeout_s=budget,
                                 seconds=_elapsed())

        originals: dict[str, str] = {}
        sites: list[Site] = []
        for path in sorted(sources):
            full = os.path.join(worktree, path)
            try:
                src = open(full, encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                continue
            originals[path] = src
            sites += mutation_sites(path, src, set(sources[path]))
        total_sites = len(sites)
        if not sites:
            return _blank_report("no_mutable_site", sites=0, cap=cap,
                                 timeout_s=budget, seconds=_elapsed())

        if budget <= 0 or _elapsed() >= budget:
            return _blank_report("budget_exhausted", sites=total_sites, cap=cap,
                                 timeout_s=budget, seconds=_elapsed())
        if cap <= 0:
            return _blank_report("cap_is_zero", sites=total_sites, cap=cap,
                                 timeout_s=budget, seconds=_elapsed())

        # BASELINE FIRST. If the suite is already red, killed and survived mean
        # nothing: every mutant "dies" for a reason that has nothing to do with
        # the mutation, and a red baseline would read as a perfect kill rate.
        base = _run(cmd, worktree, min(per_mutant_timeout, budget))
        if base != "green":
            return _blank_report(
                "baseline_red" if base == "red" else "baseline_timeout",
                sites=total_sites, cap=cap, timeout_s=budget, seconds=_elapsed())

        # Sampling, when there are more sites than the cap allows: an EVEN
        # spread across the diff rather than the first N, so a capped run at
        # least looks at the whole change instead of only its first file. The
        # run is still marked incomplete, and the classifier still refuses to
        # read it as clean. Sampling is recorded, never silent.
        selected = sites
        if total_sites > cap:
            step = total_sites / cap
            selected = [sites[int(i * step)] for i in range(cap)]

        killed = survived = unjudged = executed = 0
        complete = True
        for site in selected:
            if _elapsed() >= budget:
                complete = False
                break
            mutated = apply_site(originals[site.path], site)
            if mutated is None:
                continue        # not a mutant: never counted, in either column
            full = os.path.join(worktree, site.path)
            try:
                with open(full, "w", encoding="utf-8") as fh:
                    fh.write(mutated)
                remaining = int(max(1, budget - _elapsed()))
                verdict = _run(cmd, worktree, min(per_mutant_timeout, remaining))
            finally:
                # Restore ALWAYS, including on an exception. A probe that leaves
                # a mutant on disk would corrupt the very build it observes.
                try:
                    with open(full, "w", encoding="utf-8") as fh:
                        fh.write(originals[site.path])
                except OSError:
                    log.exception("mutation.probe: could not restore %s", full)
            executed += 1
            if verdict == "red":
                killed += 1
            elif verdict == "green":
                survived += 1
            else:
                unjudged += 1
        if total_sites > cap:
            complete = False

        return {"mutants": executed, "killed": killed, "survived": survived,
                "unobserved_mutants": unjudged, "sites": total_sites,
                "complete": complete, "unobserved_reason": None,
                "seconds": round(_elapsed(), 3), "cap": cap, "timeout_s": budget}
    except Exception:      # noqa: BLE001 - a shadow probe never breaks a build
        log.exception("mutation.probe: failed")
        return _blank_report("probe_error", cap=cap, timeout_s=budget,
                             seconds=_elapsed())


# --------------------------------------------------------------------------- #
# Reporting: refuse to invent a rate out of an empty denominator.             #
# --------------------------------------------------------------------------- #


def mutation_rates(rows) -> dict:
    """State distribution over an iterable of ledger rows, plus a rate.

    Returns ``{rows, survived_clean, mutants_survived, unobserved, observed,
    observed_fraction, survival_rate}``.

    ``survival_rate`` is survivors over OBSERVED rows, and it is None (NOT 0.0)
    when nothing was observed. Zero is a measurement; None is the absence of
    one, and the two must not print the same. ``observed_fraction`` travels
    beside it so the number always carries the size of the window it was
    computed through. Today that fraction is 0.0 fleet-wide, and saying so is
    the entire point.

    A row predating this module carries none of these keys and reads as
    ``unobserved``. NO BACKFILL: nothing here invents a value for it.

    Never raises: a malformed row is skipped, not fatal.
    """
    out = {"rows": 0, SURVIVED_CLEAN: 0, MUTANTS_SURVIVED: 0, UNOBSERVED: 0}
    try:
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            state = row.get("mutation_state")
            if state not in MUTATION_STATES:
                state = UNOBSERVED
            out["rows"] += 1
            out[state] += 1
    except Exception:      # noqa: BLE001 - a report bug must never break a caller
        log.exception("mutation.mutation_rates: failed")
    observed = out[SURVIVED_CLEAN] + out[MUTANTS_SURVIVED]
    out["observed"] = observed
    out["observed_fraction"] = (observed / out["rows"]) if out["rows"] else 0.0
    out["survival_rate"] = (out[MUTANTS_SURVIVED] / observed) if observed else None
    return out


# --------------------------------------------------------------------------- #
# Standalone entry point.                                                     #
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    """``python -m skharness.autocode.mutation --worktree ... --repo ...``

    The label is only worth building if somebody can actually run it today. The
    orchestrator cannot: the ONE site that holds a live worktree at grade time
    is ``engineering.py``, which is on the protected floor and outside this
    card's scope to modify. So the module carries its own entry point, and a
    human (or a follow-up card wiring the executor) can produce real data over
    any branch without waiting on that.

    Prints the raw report AND the derived state as JSON. It writes no ledger
    row: recording is the caller's decision, and a probe that wrote rows by
    itself would be a second store.
    """
    import argparse
    import json

    ap = argparse.ArgumentParser(
        prog="skharness.autocode.mutation",
        description="Shadow mutation probe over the changed lines of a diff. "
                    "Gates nothing; prints a report.")
    ap.add_argument("--worktree", required=True, help="checkout to mutate in place")
    ap.add_argument("--base", default="origin/main", help="diff base ref")
    ap.add_argument("--test-cmd", default=None,
                    help="command whose exit status decides killed vs survived")
    ap.add_argument("--max-mutants", type=int, default=DEFAULT_MAX_MUTANTS)
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
    ap.add_argument("--per-mutant-timeout", type=int, default=DEFAULT_PER_MUTANT_TIMEOUT)
    args = ap.parse_args(argv)

    diff = subprocess.run(["git", "-C", args.worktree, "diff", f"{args.base}...HEAD"],
                          capture_output=True, text=True).stdout
    if not diff.strip():
        diff = subprocess.run(["git", "-C", args.worktree, "diff", "HEAD"],
                              capture_output=True, text=True).stdout

    class _Repo:                      # minimal RepoSpec-shaped stand-in
        name = "cli"
        path = args.worktree
        ci = "none"

    report = probe(_Repo(), args.worktree, diff, test_cmd=args.test_cmd,
                   max_mutants=args.max_mutants, timeout_s=args.timeout,
                   per_mutant_timeout=args.per_mutant_timeout)
    print(json.dumps({"report": report, "row": mutation_row(report)}, indent=2))
    return 0


if __name__ == "__main__":       # pragma: no cover - exercised via subprocess
    raise SystemExit(main())
