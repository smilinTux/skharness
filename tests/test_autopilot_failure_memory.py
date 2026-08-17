"""The forgetting renderer: meta.autopilot.attempts[] -> bounded prompt context.

Spec: docs/specs/2026-08-14-skharness-failure-memory.md (FM-3, forgetting policy
rules 1-5). Every rule below maps to one test. The point of the feature is that a
card REMEMBERS a terminal failure across runs; the point of these tests is that it
forgets helpfully, so a card can never bloat into an unreadable brief.
"""
from __future__ import annotations

from skharness.autocode.failure_memory import build_prior_feedback, distill_failure

from skharness.autocode.failure_memory import _HEADER as HEADER


def _payload(attempts, **extra):
    p = {"title": "t", "meta": {"autopilot": {"attempts": attempts}}}
    p.update(extra)
    return p


def _attempt(**over):
    a = {"run_id": "r1", "ts": "2026-08-14T00:00:00+00:00", "round": 1,
         "outcome": "ci_red", "tried": "an approach", "why_failed": "a cause",
         "replacement_hint": ""}
    a.update(over)
    return a


# -- rule 1: empty -> None (byte-identical to today's fresh start) ------------

def test_card_without_meta_returns_none():
    assert build_prior_feedback({"title": "thin card"}) is None


def test_card_with_no_attempts_returns_none():
    assert build_prior_feedback(_payload([])) is None


def test_tolerates_a_non_dict_payload():
    assert build_prior_feedback(None) is None


def test_tolerates_a_malformed_attempts_value():
    assert build_prior_feedback({"meta": {"autopilot": {"attempts": "nope"}}}) is None


def test_skips_malformed_entries_but_keeps_good_ones():
    out = build_prior_feedback(_payload(["garbage", _attempt(why_failed="real cause")]))
    assert "real cause" in out


# -- rule 4: distillation templates -------------------------------------------

def test_hint_present_renders_the_try_template():
    out = build_prior_feedback(_payload([
        _attempt(why_failed="test_x asserts 3, got 4", replacement_hint="fix the off-by-one")]))
    assert out.startswith(HEADER)
    assert "This failed for test_x asserts 3, got 4, try fix the off-by-one." in out


def test_hint_absent_renders_the_avoid_template():
    out = build_prior_feedback(_payload([_attempt(why_failed="empty diff", replacement_hint="")]))
    assert "This previously failed for empty diff; avoid repeating that approach." in out


def test_renders_the_journal_pointer_for_the_newest_run():
    out = build_prior_feedback(_payload([
        _attempt(run_id="old", ts="2026-08-14T00:00:00+00:00"),
        _attempt(run_id="newest", ts="2026-08-14T05:00:00+00:00", outcome="no_op")]))
    assert "Raw history: autopilot/runs/newest" in out


# -- rule 2: dedup on (outcome, why_failed casefolded), newest wins -----------

def test_identical_failures_collapse_to_one_line():
    out = build_prior_feedback(_payload([
        _attempt(run_id="r1", ts="2026-08-14T01:00:00+00:00", why_failed="CI red on test_a"),
        _attempt(run_id="r2", ts="2026-08-14T02:00:00+00:00", why_failed="ci red on TEST_A")]))
    assert out.count("This previously failed for") == 1


def test_dedup_keeps_the_newest_entrys_hint():
    out = build_prior_feedback(_payload([
        _attempt(run_id="r1", ts="2026-08-14T01:00:00+00:00",
                 why_failed="same cause", replacement_hint="stale hint"),
        _attempt(run_id="r2", ts="2026-08-14T02:00:00+00:00",
                 why_failed="same cause", replacement_hint="fresh hint")]))
    assert "fresh hint" in out and "stale hint" not in out


def test_same_cause_under_a_different_outcome_is_not_deduped():
    out = build_prior_feedback(_payload([
        _attempt(run_id="r1", ts="2026-08-14T01:00:00+00:00",
                 outcome="ci_red", why_failed="shared cause"),
        _attempt(run_id="r2", ts="2026-08-14T02:00:00+00:00",
                 outcome="no_op", why_failed="shared cause")]))
    assert out.count("shared cause") == 2


# -- rule 3: bound to the last 3 distinct failures ----------------------------

def test_bounds_to_three_distinct_failures_newest_first_by_ts():
    out = build_prior_feedback(_payload([
        _attempt(run_id=f"r{i}", ts=f"2026-08-14T0{i}:00:00+00:00",
                 why_failed=f"cause {i}") for i in range(1, 6)]))
    assert out.count("This previously failed for") == 3
    for kept in ("cause 3", "cause 4", "cause 5"):
        assert kept in out
    for dropped in ("cause 1", "cause 2"):
        assert dropped not in out


# -- rule 5: hard ceiling, oldest truncated first -----------------------------

def test_block_never_exceeds_600_chars_and_drops_oldest_first():
    out = build_prior_feedback(_payload([
        _attempt(run_id=f"r{i}", ts=f"2026-08-14T0{i}:00:00+00:00",
                 why_failed=f"cause {i} " + "x" * 260) for i in range(1, 4)]))
    assert len(out) <= 600
    assert "cause 3" in out          # newest survives
    assert "cause 1" not in out      # oldest truncated away


def test_a_single_oversized_failure_is_still_capped_at_600():
    out = build_prior_feedback(_payload([_attempt(why_failed="y" * 5000)]))
    assert len(out) <= 600


def test_never_inlines_a_raw_traceback_verbatim():
    """The renderer stores what the call site distilled; it must not grow the
    block just because a caller was sloppy. The ceiling is the backstop."""
    raw = "Traceback (most recent call last):\n" + "\n".join(
        f'  File "mod{i}.py", line {i}, in f' for i in range(60))
    out = build_prior_feedback(_payload([_attempt(why_failed=raw)]))
    assert len(out) <= 600


# -- the call-site distiller: grader notes -> one honest line -----------------

def test_distill_prefers_the_failing_test_id_line():
    notes = ("The implementation looks reasonable overall.\n"
             "FAILED tests/test_parse.py::test_empty_input - AssertionError\n"
             "Consider revisiting the empty branch.")
    out = distill_failure(notes)
    assert "tests/test_parse.py::test_empty_input" in out


def test_distill_keeps_the_assertion_detail():
    notes = ("Round summary follows.\n"
             "E       assert 3 == 4\n"
             "That is the whole story.")
    assert "assert 3 == 4" in distill_failure(notes)


def test_distill_falls_back_to_the_first_meaningful_line():
    assert distill_failure("\n\ncoverage 61% is below the 80% floor\n") == (
        "coverage 61% is below the 80% floor")


def test_distill_of_empty_notes_is_still_a_usable_line():
    out = distill_failure("")
    assert out and out.strip() == out


def test_distill_is_length_bounded_and_single_line():
    notes = "FAILED test_a.py::test_x - " + "detail " * 200
    out = distill_failure(notes)
    assert len(out) <= 180
    assert "\n" not in out


def test_distill_does_not_carry_a_traceback_block():
    notes = ("Traceback (most recent call last):\n"
             '  File "mod.py", line 3, in f\n'
             "    raise ValueError('boom')\n"
             "FAILED tests/test_m.py::test_f - ValueError: boom")
    out = distill_failure(notes)
    assert "tests/test_m.py::test_f" in out
    assert 'File "mod.py"' not in out


# -- S21 vector 2: the cross-attempt channel carries CLAIMS, not findings ------

def test_the_header_attributes_the_prior_as_an_unverified_report():
    """S21 (card 53b8c8be): `why_failed` is model-derived text distilled from one
    run's grader notes, and it is fed into the NEXT run's round one. The same
    mechanism was accepted as decisive against the exploration slice (card
    f81d8d2d): a weak model's misdiagnosis becomes the next worker's premise.

    The channel is already BOUNDED (3 distinct entries, 600 chars, one distilled
    line each, dedup) and the entries are already attributed to a run via the
    journal pointer. What was missing was epistemic status: the block read as
    established fact. It must announce itself as an unverified prior.
    """
    out = build_prior_feedback(_payload([_attempt(why_failed="a cause")]))
    assert "Prior attempts on this card" in out          # unchanged prefix
    assert "UNVERIFIED" in out
    first = out.splitlines()[0]
    assert "verify" in first.lower() or "check" in first.lower()


def test_the_attributed_header_still_fits_inside_the_char_ceiling():
    """Positive control: the longer header must not blow the 600-char bound, and
    the newest failure must still survive the oldest-first drop."""
    out = build_prior_feedback(_payload([
        _attempt(run_id=f"r{i}", ts=f"2026-08-14T0{i}:00:00+00:00",
                 why_failed=f"cause {i} " + "x" * 260) for i in range(1, 4)]))
    assert len(out) <= 600
    assert "cause 3" in out
