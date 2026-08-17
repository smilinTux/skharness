"""The forgetting renderer for SUCCESSES: meta.autopilot.successes[] -> bounded
prompt context.

Sibling of test_autopilot_failure_memory.py. Closes the memory asymmetry named
in coord card 506782a4: failure_memory.py used to read only terminal
non-passes, so a policy or a human reading it learned what not to do and never
what worked. Every forgetting rule below is the SAME discipline as the failure
side (dedup, 3-entry bound, 600-char ceiling, oldest-dropped-first, hard
truncation) applied to successes[] instead of attempts[], through a renderer
that is its own function, not the failure renderer behind a flag.
"""
from __future__ import annotations

from skharness.autocode.failure_memory import (
    build_prior_feedback,
    build_prior_success_feedback,
    _render,
    _render_success,
)

HEADER = "Prior successes on this card (distilled):"


def _payload(successes, **extra):
    p = {"title": "t", "meta": {"autopilot": {"successes": successes}}}
    p.update(extra)
    return p


def _success(**over):
    s = {"run_id": "r1", "ts": "2026-08-14T00:00:00+00:00", "round": 2,
         "outcome": "pass", "tried": "an approach", "why_succeeded": "twin gate closed",
         "approach_hint": ""}
    s.update(over)
    return s


# -- rule 1: empty -> None (byte-identical to a card with no success memory) --

def test_card_without_meta_returns_none():
    assert build_prior_success_feedback({"title": "thin card"}) is None


def test_card_with_no_successes_returns_none():
    assert build_prior_success_feedback(_payload([])) is None


def test_tolerates_a_non_dict_payload():
    assert build_prior_success_feedback(None) is None


def test_tolerates_a_malformed_successes_value():
    assert build_prior_success_feedback(
        {"meta": {"autopilot": {"successes": "nope"}}}) is None


def test_skips_malformed_entries_but_keeps_good_ones():
    out = build_prior_success_feedback(
        _payload(["garbage", _success(why_succeeded="real reason")]))
    assert "real reason" in out


def test_a_card_with_only_a_failure_has_no_success_feedback():
    """The two arrays are independent: failure memory must not leak into the
    success reader (and this also proves the sibling-key trap is respected)."""
    payload = {"title": "t", "meta": {"autopilot": {
        "attempts": [{"run_id": "r0", "ts": "2026-08-14T00:00:00+00:00",
                      "outcome": "ci_red", "why_failed": "a cause"}]}}}
    assert build_prior_success_feedback(payload) is None
    assert build_prior_feedback(payload) is not None    # sanity: failure side sees it


# -- rule 4: distillation templates, the SUCCESS renderer is its own ----------

def test_hint_present_renders_the_using_template():
    out = build_prior_success_feedback(_payload([
        _success(why_succeeded="twin gate closed on round 2", approach_hint="small diffs")]))
    assert out.startswith(HEADER)
    assert "This succeeded via twin gate closed on round 2, using small diffs." in out


def test_hint_absent_renders_the_plain_template():
    out = build_prior_success_feedback(_payload([
        _success(why_succeeded="CI green, coverage 0.95", approach_hint="")]))
    assert "This previously succeeded via CI green, coverage 0.95." in out


def test_renders_the_journal_pointer_for_the_newest_run():
    out = build_prior_success_feedback(_payload([
        _success(run_id="old", ts="2026-08-14T00:00:00+00:00"),
        _success(run_id="newest", ts="2026-08-14T05:00:00+00:00")]))
    assert "Raw history: autopilot/runs/newest" in out


def test_success_renderer_is_not_the_failure_renderer_behind_a_flag():
    """TRAP 2, directly: _render is failure-phrased ('This failed...'/'This
    previously failed...'); _render_success must be a DIFFERENT function with
    DIFFERENT literal text, not the same function toggled by a bool."""
    assert _render is not _render_success
    entry = _success(why_succeeded="it worked", approach_hint="")
    rendered = _render_success(entry)
    assert "failed" not in rendered
    assert "succeeded" in rendered


# -- rule 2: dedup on (outcome, why_succeeded casefolded), newest wins --------

def test_identical_successes_collapse_to_one_line():
    out = build_prior_success_feedback(_payload([
        _success(run_id="r1", ts="2026-08-14T01:00:00+00:00", why_succeeded="CI green"),
        _success(run_id="r2", ts="2026-08-14T02:00:00+00:00", why_succeeded="ci GREEN")]))
    assert out.count("This previously succeeded via") == 1


def test_dedup_keeps_the_newest_entrys_hint():
    out = build_prior_success_feedback(_payload([
        _success(run_id="r1", ts="2026-08-14T01:00:00+00:00",
                 why_succeeded="same reason", approach_hint="stale hint"),
        _success(run_id="r2", ts="2026-08-14T02:00:00+00:00",
                 why_succeeded="same reason", approach_hint="fresh hint")]))
    assert "fresh hint" in out and "stale hint" not in out


def test_same_reason_under_a_different_outcome_is_not_deduped():
    out = build_prior_success_feedback(_payload([
        _success(run_id="r1", ts="2026-08-14T01:00:00+00:00",
                 outcome="pass", why_succeeded="shared reason"),
        _success(run_id="r2", ts="2026-08-14T02:00:00+00:00",
                 outcome="salvaged", why_succeeded="shared reason")]))
    assert out.count("shared reason") == 2


# -- rule 3: bound to the last 3 distinct successes ---------------------------

def test_bounds_to_three_distinct_successes_newest_first_by_ts():
    out = build_prior_success_feedback(_payload([
        _success(run_id=f"r{i}", ts=f"2026-08-14T0{i}:00:00+00:00",
                 why_succeeded=f"reason {i}") for i in range(1, 6)]))
    assert out.count("This previously succeeded via") == 3
    for kept in ("reason 3", "reason 4", "reason 5"):
        assert kept in out
    for dropped in ("reason 1", "reason 2"):
        assert dropped not in out


# -- rule 5: hard ceiling, oldest truncated first -----------------------------

def test_block_never_exceeds_600_chars_and_drops_oldest_first():
    out = build_prior_success_feedback(_payload([
        _success(run_id=f"r{i}", ts=f"2026-08-14T0{i}:00:00+00:00",
                 why_succeeded=f"reason {i} " + "x" * 260) for i in range(1, 4)]))
    assert len(out) <= 600
    assert "reason 3" in out          # newest survives
    assert "reason 1" not in out      # oldest truncated away


def test_a_single_oversized_success_is_still_capped_at_600():
    out = build_prior_success_feedback(_payload([_success(why_succeeded="y" * 5000)]))
    assert len(out) <= 600
