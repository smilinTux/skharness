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
import subprocess
from fnmatch import fnmatch
from pathlib import Path

import pytest

from skharness.autocode import autopilot_cost, protected
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


# ---------------------------------------------------------------------------
# AC4: nothing on the grading floor was modified.
#
# S25 (card 3f6719e4) rewrote the MEASUREMENT, not the rule. The rule is
# unchanged and unweakened: a file on the grading floor that differs from the
# branch's base is a violation.
#
# What changed is that the old form measured `origin/main...HEAD` and called the
# result "what this card did". On one card that is true. On a branch that
# composes nine cards it is not: card A touches no floor file, card B
# legitimately does, and A's guard then reports B's work as A's violation. Every
# card was individually compliant and the composition was red, for a structural
# reason that would recur on every future integration pass regardless of merit.
#
# The fix is an ALLOWANCE, and the shape of the allowance is the whole point:
#
#   * It is enumerated and human-authored, in tests/data/grading-floor-allowances.json.
#     There is no pattern, no "recent commits are fine", no environment variable.
#     An unlisted floor change is a violation, exactly as before.
#   * It is pinned to CONTENT (a git blob sha), not to a path. A path allowance
#     would be a permanent hole that every later edit rides through. A content
#     pin dies the moment the file changes again, so the next change gets its
#     own review. See the negative controls below, which prove that a
#     grading-semantic edit to the ALLOWED file is still red.
#   * The allowance list is itself on protected._ALWAYS_PROTECTED, so a diff that
#     adds an entry can never auto-merge. "Reviewed, not automatic" is enforced
#     by the same machinery this guard belongs to, not by convention.
#   * Every failure to MEASURE is a violation. No skip. A guard that passes
#     because it could not compute the diff is worse than no guard, and this
#     epic exists because ten mechanisms already failed that way.
# ---------------------------------------------------------------------------

#: The grading half of protected._ALWAYS_PROTECTED: the rubric, the deterministic
#: exposure rules, grade-to-trust-zone addressing, the vendored enums and the
#: calibration reference. AC4 forbids touching any of them.
_GRADING_FLOOR = ("*/skharness/autocode/grading.py", "*/skharness/autocode/sensitivity.py",
                  "*/skharness/autocode/buckets.py",
                  "*/autocode/data/joule-grade-vocabulary.json",
                  "*/tests/data/joule-economy-golden-set-*.json")

#: The reviewed exceptions. Repo-relative on purpose: it must be greppable and it
#: must appear in a diff.
_ALLOWANCES_REL = "tests/data/grading-floor-allowances.json"

#: Bases tried in order. The point is not convenience, it is that a shallow or
#: detached checkout must produce an ERROR rather than a pass.
_BASE_REFS = ("origin/main", "origin/HEAD", "main")


class FloorCheckError(RuntimeError):
    """The floor check could not be COMPUTED.

    Deliberately not a skip. If the base is missing, the diff fails, or the
    allowance list is unreadable, we do not know whether the floor was touched,
    and "we do not know" must read as red. A green that means "unmeasured" is
    indistinguishable from a green that means "clean", which is the exact defect
    this epic catalogued ten times over.
    """


def _repo_root() -> Path:
    return _SRC.parents[2]


def _git(repo, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)


def _resolve_base(repo) -> str:
    """First ref in _BASE_REFS that actually resolves to a commit.

    Raises FloorCheckError if none do (no git, not a repo, shallow clone with no
    remote-tracking ref, ...). Fail closed.
    """
    for ref in _BASE_REFS:
        proc = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        if proc.returncode == 0 and proc.stdout.strip():
            return ref
    raise FloorCheckError(
        f"no base ref among {_BASE_REFS} resolves in {repo}. The grading-floor "
        "check cannot be computed, so it reports a failure rather than passing: "
        "an unmeasured floor is not a clean floor.")


def _on_grading_floor(path: str) -> bool:
    """True if repo-relative *path* is on the grading floor."""
    return any(fnmatch("/" + path, g) for g in _GRADING_FLOOR)


def _changed_floor_paths(repo, base: str) -> list[str]:
    """Floor files that differ from *base*, in committed history OR the worktree.

    Both halves are kept from the original guard: `base...HEAD` covers the branch
    and its own base, `diff HEAD` covers uncommitted edits, so a floor change
    cannot hide by simply not being committed yet.
    """
    seen: list[str] = []
    for args in ([f"{base}...HEAD"], []):
        proc = _git(repo, "diff", "--name-only", *(args or ["HEAD"]))
        if proc.returncode != 0:
            raise FloorCheckError(
                f"git diff {args or ['HEAD']} failed in {repo}: "
                f"{proc.stderr.strip()[:200]}")
        seen += proc.stdout.split()
    return sorted({p for p in seen if _on_grading_floor(p)})


def _content_blob(repo, path: str) -> str:
    """git blob sha of the CURRENT content of *path*.

    A path that cannot be hashed (deleted, unreadable) returns a sentinel that no
    pinned sha can ever equal, so a floor file that was REMOVED is a violation
    rather than an absence of evidence.
    """
    proc = _git(repo, "hash-object", "--", path)
    sha = proc.stdout.strip()
    if proc.returncode != 0 or not sha:
        return f"<unhashable:{path}>"
    return sha


def _load_allowances(path) -> list[dict]:
    """Read the reviewed-exception list, failing closed to "allow nothing"."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = doc.get("allowances") if isinstance(doc, dict) else None
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def _floor_violations(changed_blobs: dict, allowances: list) -> list:
    """The whole decision, as a pure function so it can be negative-controlled.

    *changed_blobs* maps a repo-relative floor path to the blob sha of its
    current content. Returns a list of (path, why); empty means clean.
    """
    index: dict[str, dict] = {}
    problems: list = []
    for entry in allowances:
        # An entry without a path, a content pin, a card and a written reason is
        # not a review, it is a wildcard with paperwork. Refuse the whole list.
        missing = [f for f in ("path", "blob", "card", "reason") if not entry.get(f)]
        if missing:
            problems.append((_ALLOWANCES_REL,
                             f"allowance entry is missing {missing}: {entry!r}"))
            continue
        if not _on_grading_floor(entry["path"]):
            problems.append((_ALLOWANCES_REL,
                             f"allowance names {entry['path']!r}, which is not on "
                             "the grading floor; entries must be exact floor paths"))
            continue
        index[entry["path"]] = entry
    if problems:
        return problems

    for path in sorted(changed_blobs):
        entry = index.get(path)
        if entry is None:
            problems.append((path, "changed, is on the grading floor, and has no "
                                   f"reviewed allowance in {_ALLOWANCES_REL}"))
        elif entry["blob"] != changed_blobs[path]:
            problems.append((path, f"changed. The allowance (card {entry['card']}) "
                                   f"pins blob {entry['blob'][:12]}, the content is "
                                   f"{changed_blobs[path][:12]}. A reviewed "
                                   "exception covers ONE post-image; this is a "
                                   "different one and needs its own review."))
    return problems


def test_no_file_on_the_grading_floor_was_modified():
    """AC4, measured against the branch base and reconciled with the reviewed
    exception list. Green means: every floor file that differs from the base was
    read by a human, who wrote down why, and the content still matches what they
    read."""
    # Positive control 1: every glob asserted here really is on the hard floor, so
    # a future edit to protected.py cannot quietly shrink what this test guards.
    for g in _GRADING_FLOOR:
        assert g in protected._ALWAYS_PROTECTED, g

    # Positive control 2: the allowance list is ITSELF protected. If it were not,
    # the engine could grant itself permission to rewrite the rubric, and this
    # whole mechanism would be a mute button with extra steps.
    assert any(fnmatch("/" + _ALLOWANCES_REL, g) for g in protected._ALWAYS_PROTECTED), \
        f"{_ALLOWANCES_REL} must be on protected._ALWAYS_PROTECTED"

    # Positive control 3: the matcher demonstrably separates floor from not-floor.
    # This replaces the old "the diff must be non-empty" control, which certified
    # nothing on a branch whose diff is legitimately empty and would have failed
    # on main.
    assert _on_grading_floor("src/skharness/autocode/grading.py")
    assert not _on_grading_floor("src/skharness/autocode/orchestrator.py")

    repo = _repo_root()
    base = _resolve_base(repo)
    changed = {p: _content_blob(repo, p) for p in _changed_floor_paths(repo, base)}
    violations = _floor_violations(changed, _load_allowances(repo / _ALLOWANCES_REL))
    assert violations == [], violations


def test_the_floor_check_composes_over_a_single_card_and_an_integration_branch():
    """The defect this card exists to fix: the check must give the same verdict
    on one card's branch and on the branch that composes it with others."""
    repo = _repo_root()
    allowances = _load_allowances(repo / _ALLOWANCES_REL)
    buckets = "src/skharness/autocode/buckets.py"

    # SINGLE-CARD shape. S12's own branch touched no floor file, so the set of
    # changed floor files is empty and the verdict is clean.
    assert _floor_violations({}, allowances) == []

    # COMPOSED shape. The integration branch also carries S14's reviewed change
    # to buckets.py. Same verdict, because the composition is exactly the set of
    # reviewed exceptions and nothing else.
    composed = {buckets: _content_blob(repo, buckets)}
    assert _floor_violations(composed, allowances) == []

    # ... and composing is not a licence. A tenth card that lands an unreviewed
    # floor change on the same branch is still red, and the message names it.
    plus_unreviewed = dict(composed)
    plus_unreviewed["src/skharness/autocode/sensitivity.py"] = "0" * 40
    hits = _floor_violations(plus_unreviewed, allowances)
    assert [p for p, _ in hits] == ["src/skharness/autocode/sensitivity.py"], hits


def _blob_of(text: str) -> str:
    """Real git blob sha of *text*, so the negative controls hash real mutated
    source rather than asserting against a made-up string."""
    proc = subprocess.run(["git", "hash-object", "--stdin"], input=text,
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_negative_control_a_grading_change_to_an_allowed_file_is_still_red():
    """THE control that decides whether the allowance is a review or a mute.

    buckets.py IS allowed on this branch. Mutate model_class derivation inside
    it anyway (bucket_id is where a model_class becomes a routing address) and
    the guard must still fire, because the allowance is pinned to the exact
    reviewed content and this is not that content.
    """
    repo = _repo_root()
    buckets = "src/skharness/autocode/buckets.py"
    allowances = _load_allowances(repo / _ALLOWANCES_REL)
    assert any(a.get("path") == buckets for a in allowances), \
        "this control proves nothing unless buckets.py is actually allowed"
    assert _floor_violations({buckets: _content_blob(repo, buckets)}, allowances) == [], \
        "baseline must be green, or the red below is not caused by the mutation"

    src = (_SRC / "buckets.py").read_text(encoding="utf-8")
    anchor = "    cls = model_class.strip().lower()"
    assert anchor in src, "anchor moved; re-point this negative control at bucket_id"
    # Silently collapse the rubric's top class onto the one below it: an XL card
    # would route a rung down, forever, and nothing else in the file changes.
    mutated = src.replace(
        anchor,
        '    cls = model_class.strip().lower()\n'
        '    cls = "l" if cls == "xl" else cls  # NEGATIVE CONTROL, never ship')
    assert mutated != src

    hits = _floor_violations({buckets: _blob_of(mutated)}, allowances)
    assert [p for p, _ in hits] == [buckets], hits
    assert "needs its own review" in hits[0][1], hits


def test_negative_control_a_grading_change_to_an_unallowed_file_is_red():
    """The same mutation class one file over, in grading.py's model_class_for,
    which has no allowance at all."""
    repo = _repo_root()
    grading = "src/skharness/autocode/grading.py"
    allowances = _load_allowances(repo / _ALLOWANCES_REL)
    assert not any(a.get("path") == grading for a in allowances)

    src = (_SRC / "grading.py").read_text(encoding="utf-8")
    anchor = "def model_class_for(size: str, risk: str) -> str:"
    assert anchor in src, "anchor moved; re-point this negative control"
    mutated = src.replace(anchor, anchor + "\n    return CLASS[0]  # NEGATIVE CONTROL")
    assert mutated != src

    hits = _floor_violations({grading: _blob_of(mutated)}, allowances)
    assert [p for p, _ in hits] == [grading], hits
    assert "no reviewed allowance" in hits[0][1], hits


def test_a_deleted_floor_file_is_a_violation_not_an_absence_of_evidence():
    repo = _repo_root()
    buckets = "src/skharness/autocode/buckets.py"
    allowances = _load_allowances(repo / _ALLOWANCES_REL)
    hits = _floor_violations({buckets: f"<unhashable:{buckets}>"}, allowances)
    assert [p for p, _ in hits] == [buckets], hits
    assert _content_blob(repo, "src/skharness/autocode/does-not-exist.py").startswith(
        "<unhashable:")


def test_an_unresolvable_base_fails_closed_rather_than_skipping(tmp_path):
    """A shallow clone or a CI checkout with no origin/main must ERROR. The old
    form called pytest.skip here, which is a guard that reports success when it
    measured nothing."""
    with pytest.raises(FloorCheckError) as exc:
        _resolve_base(tmp_path)
    assert "cannot be computed" in str(exc.value)


def test_an_unreadable_or_tampered_allowance_list_allows_nothing(tmp_path):
    buckets = "src/skharness/autocode/buckets.py"
    real = _content_blob(_repo_root(), buckets)

    # Missing file, unparseable file, wrong shape: all read as "allow nothing".
    assert _load_allowances(tmp_path / "absent.json") == []
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    assert _load_allowances(tmp_path / "broken.json") == []
    (tmp_path / "wrong.json").write_text('{"allowances": "everything"}', encoding="utf-8")
    assert _load_allowances(tmp_path / "wrong.json") == []
    assert _floor_violations({buckets: real}, []) != []

    # An entry with no written reason, or one that names something off the floor,
    # invalidates the LIST rather than being ignored, so a half-filled entry
    # cannot quietly widen the exception.
    no_reason = [{"path": buckets, "blob": real, "card": "x", "reason": ""}]
    assert _floor_violations({buckets: real}, no_reason) != []
    off_floor = [{"path": "src/skharness/autocode/orchestrator.py", "blob": real,
                  "card": "x", "reason": "y"}]
    assert _floor_violations({}, off_floor) != []
