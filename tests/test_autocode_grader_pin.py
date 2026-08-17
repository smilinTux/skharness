"""S20 (card 0b7e3ac3): the card being graded must not choose its own grader.

THE DEFECT this file pins closed. A card's work grade produced `model_class`,
which became a bucket id, which was attached to the GradeBrief, which
adapters/base.py handed to the twin-gate grader as its `model`. So a card graded
S/low routed its OWN quality gate to the weakest class in the fleet. Grade the
work easy, get an easy grader.

THE HALF THAT WAS RIGHT AND MUST SURVIVE. The grader reads the DIFF, which
carries the card's content, so it genuinely sits in the same sensitivity zone as
the build and MUST obey the same trust-zone ceiling. Sensitivity is inherited.
Capability is not, because capability is what decides whether the grader is
competent to catch a bad diff.

WHY THIS NEEDED A TEST RATHER THAN A CODE READ. No observation distinguished
"graded by a competent model" from "this card downgraded its own grader": both
produce a well formed GateResult with score 5. The failure mode was identical to
success. The assertions here, plus `grader_model` on the outcome row, ARE that
observation.

Every test is negative-controlled: each one is paired with an assertion that
would fail if the property it claims did not hold.

No em/en dashes anywhere (SKWorld hard rule).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from skharness.autocode import autopilot_cost
from skharness.autocode import orchestrator as orch
from skharness.autocode.adapters.base import BaseCliAdapter
from skharness.autocode.buckets import (BUCKET_CLASSES, BUCKET_SENSITIVITIES,
                                        attach_dispatch_model, bucket_id,
                                        is_wider_than, validate_bucket)
from skharness.autocode.grader_pin import GRADER_CAPABILITY_CLASS, grader_bucket
from skharness.autocode.orchestrator import Caps, CapLedger
from skharness.autocode.sandbox import AuthMount, Sandbox
from skharness.autocode.types import (GateResult, GradeBrief, RepoSpec, Verdict,
                                      WorkItem)


class _Fake(BaseCliAdapter):
    name = "fake"

    def _argv(self, prompt, light=False):
        return ["fake", prompt]

    def _image(self):
        return "sandbox-fake:1"

    def _auth_mounts(self):
        return [AuthMount("/h/.cred", "/c/.cred")]

    def _auth_env(self):
        return {"BASE_URL": "http://gw.local"}

    def _parse(self, raw):
        return raw.get("result", raw)

    def capabilities(self):
        return {"session_resume": False, "structured_output": "json",
                "sandbox": True, "tool_restrictions": True}


def _repo(**kw):
    base = dict(name="r", path="/tmp/r", base_branch="main", integration_branch="int",
                test_cmd="pytest", ci="none")
    base.update(kw)
    return RepoSpec(**base)


def _grade_brief():
    return GradeBrief(task_id="t1", repo=_repo(), worktree="/tmp/wt",
                      diff="--- a\n+++ b\n+x", acceptance=["a"], ci_status="green",
                      diff_coverage=1.0)


def _model_the_grader_addressed(build_bucket, monkeypatch):
    """Run the REAL adapter.grade() and return the `model` it sent.

    Goes through the live seam rather than asserting on a helper, so a fix that
    only corrects a helper and forgets the call site still reads RED here.
    """
    seen = {}
    a = _Fake(Sandbox(live_execution=False), egress_hosts=["gw.local"])

    def _capture(instruction, data, *, worktree, repo, light=False, model=None):
        seen["model"] = model
        return {"score": 5, "passed": True, "notes": "<promise>COMPLETE</promise>"}

    monkeypatch.setattr(a, "_run", _capture)
    brief = _grade_brief()
    attach_dispatch_model(brief, build_bucket)
    result = a.grade(brief)
    assert isinstance(result, GateResult)   # the shape is unchanged by the pin
    return seen["model"]


def _class_of(bucket: str) -> str:
    return validate_bucket(bucket).split("-")[1]


def _zone_of(bucket: str) -> str:
    return validate_bucket(bucket).split("-")[2]


# --------------------------------------------------------------------------- #
# TEST 1: a card graded S does NOT receive an S-class grader.                  #
# --------------------------------------------------------------------------- #


def test_a_card_graded_s_does_not_get_an_s_class_grader(monkeypatch):
    """The load-bearing assertion of this card.

    `sk-s-internal` is exactly what a card graded size=S / risk=low / internal
    produces through work_grade_for -> model_class_for -> bucket_for_grade. Under
    the defect the grader was handed that same id and the weakest class in the
    fleet decided whether the diff was good enough to merge.
    """
    sent = _model_the_grader_addressed("sk-s-internal", monkeypatch)
    assert _class_of(sent) != "s", (
        "the twin-gate grader took the CARD's own capability class; a card "
        "graded S just chose the weakest grader in the fleet")
    assert _class_of(sent) == GRADER_CAPABILITY_CLASS


def test_no_card_grade_can_move_the_graders_capability_class(monkeypatch):
    """Negative control for test 1, and the general form of it.

    Sweeping all four classes proves the pin is a CONSTANT rather than a floor
    that a card could still push around from above: an XL card does not get an
    XL grader either. If the class ever tracked the card, this loop fails on the
    first id whose class is not the pin.
    """
    classes = {_class_of(_model_the_grader_addressed(bucket_id(c, "internal"),
                                                    monkeypatch))
               for c in BUCKET_CLASSES}
    assert classes == {GRADER_CAPABILITY_CLASS}


# --------------------------------------------------------------------------- #
# TEST 2: the SENSITIVITY leg is still inherited. Do not break this half.      #
# --------------------------------------------------------------------------- #


def test_a_secret_card_cannot_get_a_grader_in_a_looser_zone(monkeypatch):
    """The half of the old justification that was CORRECT.

    The grader reads the diff, so it must stay inside the build's trust zone. A
    fix that pinned the whole bucket (class AND zone) would hand a secret card's
    diff to a public-zone model, which is a sovereignty regression far worse than
    the defect being fixed.
    """
    sent = _model_the_grader_addressed("sk-xl-secret", monkeypatch)
    assert _zone_of(sent) == "secret"
    assert is_wider_than(sent, "sk-xl-secret") is False


def test_the_grader_never_widens_the_zone_for_any_of_the_twelve_buckets(monkeypatch):
    for cls in BUCKET_CLASSES:
        for zone in BUCKET_SENSITIVITIES:
            build = bucket_id(cls, zone)
            assert is_wider_than(grader_bucket(build), build) is False, build
            assert _zone_of(grader_bucket(build)) == zone


def test_is_wider_than_actually_detects_a_widening():
    """Negative control for the two tests above: their `is False` assertions are
    only worth something if `is_wider_than` returns True when a zone IS loosened.
    Without this, a helper stubbed to `return False` would make them both pass."""
    assert is_wider_than("sk-m-public", "sk-m-secret") is True
    assert is_wider_than("sk-m-internal", "sk-m-secret") is True


def test_an_ungraded_card_still_sends_no_override(monkeypatch):
    """Today's ONLY live branch, and it must not change. Zero cards carry a
    grade, so every build attaches None and the adapter runs on its statically
    configured sovereign model. The pin must not invent a bucket here: doing so
    would give an ungraded card a zone it could not otherwise address."""
    assert grader_bucket(None) is None
    assert _model_the_grader_addressed(None, monkeypatch) is None


def test_a_bucket_the_gateway_would_not_route_is_refused():
    """The pin must not become a laundering path for an unvalidated id.
    skgateway does NOT reject a malformed sk-* id; it falls through to sk-auto
    with no sensitivity ceiling, so the refusal has to happen locally."""
    with pytest.raises(Exception):
        grader_bucket("sk-s-secrets")
    with pytest.raises(Exception):
        grader_bucket("SK-S-SECRET")


# --------------------------------------------------------------------------- #
# TEST 3: grader_model is on the outcome row, so a downgrade is visible later. #
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _ledger_is_isolated(tmp_path_factory, monkeypatch):
    cd = tmp_path_factory.mktemp("s20-cost")
    monkeypatch.setenv("SKAI_COST_DIR", str(cd))
    assert autopilot_cost.cost_dir() == cd


@pytest.fixture(autouse=True)
def _fake_journal(monkeypatch):
    monkeypatch.setattr(orch, "journal", SimpleNamespace(
        read_run=lambda rid: {}, write_run=lambda rid, d: None,
        handle=lambda rid: SimpleNamespace()))


def _wi(ref, **payload):
    p = {"id": ref, "tags": ["repo:skos"]}
    p.update(payload)
    return WorkItem(kind="engineering", ref=ref, source="coord", repo="skos", payload=p)


def _harness(name="claude-code", model="ornith-big"):
    return SimpleNamespace(name=name, model=model,
                           assess=lambda brief: Verdict(verdict="valid", reason=""))


def _rows():
    return autopilot_cost._read_ledger()


def test_the_outcome_row_records_the_grader_separately_from_the_builder():
    """Creates the observation that did not exist.

    Before this, both a competent grade and a self-downgraded one wrote the same
    row. `model_requested` names the BUILD's bucket; a reader had no field at all
    for what graded it, so a downgraded grader left no trace. These two fields
    differing in CLASS while agreeing on ZONE is the whole fix, visible after the
    fact from the ledger alone.
    """
    grade = {"size": "S", "risk": "low", "sensitivity": "internal",
             "model_class": "s"}
    item = _wi("t-graded", work_grade=grade, quality="gated")
    orch.record_outcome_row(item, terminal_state="finalized", run_id="r-s20",
                            harness=_harness(),
                            result=GateResult(score=5, passed=True, notes="ok",
                                              artifact="pr", outcome="pass"))
    row = _rows()[0]
    assert row["model_requested"] == "sk-s-internal"     # the BUILD's bucket
    assert row["grader_model"] == "sk-m-internal"        # the PINNED grader
    assert row["grader_model"] != row["model_requested"], (
        "a row where the grader is indistinguishable from the builder is the "
        "unobservable state this card exists to remove")


def test_an_ungraded_card_records_the_static_grader():
    """Negative control for the row test: the field must be POPULATED on the
    ungraded path too, otherwise a null would read the same as a field nobody
    threaded, and today every card is ungraded."""
    orch.record_outcome_row(_wi("t-plain"), terminal_state="escalated",
                            run_id="r-s20b", harness=_harness(model="ornith-big"))
    row = _rows()[0]
    assert row["grader_model"] == "ornith-big"


def test_a_recorder_bug_never_breaks_the_row(monkeypatch):
    """The grader stamp is telemetry. A corrupt grade must not stop the row."""
    item = _wi("t-corrupt", work_grade={"size": "S", "risk": "low",
                                        "sensitivity": "internal",
                                        "model_class": "sk-s"})   # unmappable
    orch.record_outcome_row(item, terminal_state="finalized", run_id="r-s20c",
                            harness=_harness(model="ornith-big"))
    row = _rows()[0]
    assert row["card_id"] == "t-corrupt"
    assert row["grader_model"] == "ornith-big"


# --------------------------------------------------------------------------- #
# SECOND DEFECT: a card tag must not be able to switch the gate OFF.          #
# --------------------------------------------------------------------------- #


def _spec(name="skrender", **over):
    base = dict(name=name, path=f"/repos/{name}", base_branch="main",
                integration_branch="develop", test_cmd="pytest", ci="none")
    base.update(over)
    return RepoSpec(**base)


def _cfg(repo_map=None, **over):
    from skharness.autocode.types import QualityMode
    base = dict(repo_map=repo_map or {"skrender": _spec()}, automerge_repos=[],
                default_quality=QualityMode.GATED)
    base.update(over)
    return SimpleNamespace(**base)


def test_a_card_tag_cannot_lower_quality_below_the_operator_baseline():
    """`quality:direct` routes to engineering-direct, which types.py documents as
    NO grade, NO gate. That made the twin gate itself card-selectable. The tag is
    now RAISE-ONLY against the operator baseline (config default, raised by the
    repo floor), neither of which a card can write."""
    from skharness.autocode.types import QualityMode
    cfg = _cfg()                                    # default_quality=GATED
    task = {"id": "t1", "tags": ["repo:skrender", "quality:direct"]}
    assert orch.resolve_quality(task, cfg) == QualityMode.GATED
    assert orch.classify_kind(task, cfg) == "engineering"


def test_the_operator_can_still_choose_direct_and_the_tag_can_still_raise():
    """Negative control: the rule must be RAISE-ONLY, not "always gated". If the
    fix simply hardcoded GATED, both assertions here fail and the direct mode the
    operator configures for skcode/telegram would be silently dead."""
    from skharness.autocode.types import QualityMode
    direct_cfg = _cfg(default_quality=QualityMode.DIRECT)
    plain = {"id": "t1", "tags": ["repo:skrender"]}
    assert orch.resolve_quality(plain, direct_cfg) == QualityMode.DIRECT
    assert orch.classify_kind(plain, direct_cfg) == "engineering-direct"

    raised = {"id": "t1", "tags": ["repo:skrender", "quality:gated"]}
    assert orch.resolve_quality(raised, direct_cfg) == QualityMode.GATED
    assert orch.classify_kind(raised, direct_cfg) == "engineering"


def test_a_repo_floor_still_beats_an_operator_default_and_a_tag():
    from skharness.autocode.types import QualityMode
    cfg = _cfg(repo_map={"skchat": _spec("skchat", min_quality=QualityMode.GATED)},
               default_quality=QualityMode.DIRECT)
    task = {"id": "t1", "tags": ["repo:skchat", "quality:direct"]}
    assert orch.resolve_quality(task, cfg) == QualityMode.GATED
    assert orch.classify_kind(task, cfg) == "engineering"


def test_the_downgrade_refusal_is_recorded_not_silent(monkeypatch):
    """A card that TRIED to switch off its own gate must leave a trace. A silent
    coercion is indistinguishable from a card that never asked."""
    from skharness.autocode.types import QualityMode
    seen = []
    monkeypatch.setattr(orch.health, "record",
                        lambda event, **kw: seen.append((event, kw)))
    task = {"id": "t1", "tags": ["repo:skrender", "quality:direct"]}
    assert orch.resolve_quality(task, _cfg()) == QualityMode.GATED
    assert any(e == "quality_downgrade_refused" for e, _ in seen), seen
