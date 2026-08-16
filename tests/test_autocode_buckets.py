"""Graded model selection, sending side: grade -> skgateway bucket id.

These are deliberately NEGATIVE-CONTROL heavy. Every gate in this epic that looked
enforced and enforced nothing was caught by a test asserting something is BLOCKED,
never by a happy path. So the load-bearing assertions here are refusals: a typo'd
bucket is not sent, an unsupported adapter refuses rather than silently downgrading,
and an ungraded card cannot obtain a bucket at all.
"""
from __future__ import annotations

import itertools
import json

import pytest

from skharness.autocode.adapters.base import ModelOverrideUnsupported
from skharness.autocode.adapters.opencode import OpenCodeAdapter
from skharness.autocode.adapters.pi import PiAdapter
from skharness.autocode.buckets import (
    BUCKET_CLASSES,
    BUCKET_RE,
    BUCKET_SENSITIVITIES,
    BucketError,
    attach_dispatch_model,
    bucket_for_grade,
    bucket_for_payload,
    bucket_id,
    dispatch_model_of,
    is_wider_than,
    ungraded_floor_bucket,
    validate_bucket,
    work_grade,
)
from skharness.autocode.engineering import EngineeringExecutor

GATEWAY_URL = "http://localhost:18780/v1"

#: Every legal bucket the gateway grammar admits. Twelve, no more.
ALL_BUCKETS = [f"sk-{c}-{s}"
               for c, s in itertools.product(BUCKET_CLASSES, BUCKET_SENSITIVITIES)]


class RecordingSandbox:
    """Stands in for the Docker sandbox: records every spawn, never runs anything.

    Its whole job is to let a test assert that a refused dispatch was NEVER SENT,
    which is the only assertion that distinguishes a real local gate from one that
    merely logs and proceeds.
    """

    run_timeout = 1800

    def __init__(self):
        self.specs = []

    def spawn(self, spec, repo_remote_host=None, ci_host=None):
        self.specs.append(spec)
        return {"result": "{}"}


def _pi(sandbox=None, **kw):
    kw.setdefault("model", "ornith-big")
    kw.setdefault("base_url", GATEWAY_URL)
    return PiAdapter(sandbox or RecordingSandbox(), **kw)


def _grade(model_class="L", sensitivity="internal", size="M", risk="high"):
    """A COMPLETE grade, matching the agreed contract shape exactly."""
    return {"size": size, "risk": risk, "sensitivity": sensitivity,
            "model_class": model_class}


# -- the mapping itself ----------------------------------------------------------

def test_grade_maps_mechanically_onto_the_gateway_bucket_grammar():
    assert bucket_for_grade(_grade("L", "internal")) == "sk-l-internal"
    assert bucket_for_grade(_grade("XL", "secret")) == "sk-xl-secret"
    assert bucket_for_grade(_grade("S", "public")) == "sk-s-public"


def test_every_class_sensitivity_pair_produces_a_legal_lowercase_bucket():
    seen = set()
    for cls, zone in itertools.product(("S", "M", "L", "XL"), BUCKET_SENSITIVITIES):
        b = bucket_for_grade(_grade(cls, zone))
        assert b == b.lower()
        assert BUCKET_RE.match(b), b
        seen.add(b)
    assert seen == set(ALL_BUCKETS)          # exactly twelve addresses, no more


def test_model_class_is_consumed_never_re_derived():
    # size/risk say XL, but model_class says S. The bucket follows model_class,
    # because the class is derived ONCE upstream and this layer only consumes it.
    g = _grade(model_class="S", sensitivity="internal", size="XL", risk="crit")
    assert bucket_for_grade(g) == "sk-s-internal"


# -- NEGATIVE CONTROL: a secret card can never address a looser zone --------------

def _assert_secret_never_widens(mapper):
    """The property under test, factored out so it can be run against a PERMISSIVE
    STUB as a negative control on the test itself."""
    for cls in ("S", "M", "L", "XL"):
        b = mapper(_grade(cls, "secret"))
        assert BUCKET_RE.match(b), f"{b} is not even a bucket"
        assert b.endswith("-secret"), f"secret card routed to {b}"
        for other in ALL_BUCKETS:
            if other.endswith("-secret"):
                continue
            assert is_wider_than(other, b), "sanity: non-secret buckets are wider"
            assert not is_wider_than(b, other), f"{b} is wider than {other}"


def test_secret_graded_card_never_reaches_a_looser_bucket():
    _assert_secret_never_widens(bucket_for_grade)


def test_the_secret_property_test_fails_against_a_permissive_stub():
    # Negative control on the assertion above: replace the mapping with a
    # permissive stub that hands everything the widest public bucket, and the
    # property MUST fail. If this passes, the test above is proving nothing.
    def permissive(_grade_dict):
        return "sk-xl-public"

    with pytest.raises(AssertionError):
        _assert_secret_never_widens(permissive)


# -- NEGATIVE CONTROL: a typo is refused locally ---------------------------------

@pytest.mark.parametrize("bad", [
    "sk-xl-secrets",        # THE hazard: plural typo, gateway answers 200 from sk-auto
    "sk-xxl-secret",        # not a class
    "sk-l-confidential",    # not a zone
    "sk-l",                 # truncated
    "sk-l-internal ",       # trailing space
    "sk-auto",              # the difficulty classifier a typo silently lands on
    "gpt-4o",               # a third-party model id
    "SK-L-INTERNAL",        # gateway accepts case-insensitively; we do not emit it
    "",
])
def test_invalid_bucket_ids_are_refused(bad):
    with pytest.raises(BucketError):
        validate_bucket(bad)


def test_typod_bucket_is_never_sent_to_the_sandbox():
    # The assertion that matters: not merely that it raised, but that NOTHING was
    # spawned. A gate that raises after sending is not a gate.
    sb = RecordingSandbox()
    a = _pi(sb)
    with pytest.raises(BucketError):
        a._run_raw("i", "d", worktree="/tmp", repo=None, model="sk-xl-secrets")
    assert sb.specs == []


def test_bucket_id_refuses_unknown_class_or_sensitivity():
    with pytest.raises(BucketError):
        bucket_id("XXL", "secret")
    with pytest.raises(BucketError):
        bucket_id("L", "confidential")
    with pytest.raises(BucketError):
        bucket_id(None, "secret")


def test_corrupt_grade_refuses_rather_than_degrading_to_no_bucket():
    # A partial grade is off-contract. It must RAISE, not return None: returning
    # None would fall back to the static model and silently drop the ceiling.
    with pytest.raises(BucketError):
        bucket_for_grade({"size": "M", "risk": "high"})
    with pytest.raises(BucketError):
        bucket_for_payload({"work_grade": {"model_class": "L"}})


# -- NEGATIVE CONTROL: an ungraded card gets no bucket at all --------------------

def test_ungraded_card_obtains_no_bucket():
    assert work_grade({}) is None
    assert bucket_for_payload({}) is None
    assert bucket_for_payload({"work_grade": None}) is None
    assert bucket_for_payload({"work_grade": "L/internal"}) is None
    assert bucket_for_payload(None) is None


def test_ungraded_floor_is_not_wider_than_any_graded_bucket():
    floor = ungraded_floor_bucket()
    assert floor == "sk-s-secret"
    for b in ALL_BUCKETS:
        assert not is_wider_than(floor, b), f"ungraded floor {floor} wider than {b}"


def test_ungraded_executor_dispatch_sends_no_override():
    ex = EngineeringExecutor(None, None, None, agent_name="test")

    class Item:
        ref = "card-1"
        payload = {"title": "t"}

    assert ex._dispatch_model(Item()) is None


def test_graded_executor_dispatch_resolves_the_bucket():
    ex = EngineeringExecutor(None, None, None, agent_name="test")

    class Item:
        ref = "card-2"
        payload = {"title": "t", "work_grade": _grade("XL", "secret")}

    assert ex._dispatch_model(Item()) == "sk-xl-secret"


# -- the per-call seam -----------------------------------------------------------

def test_per_call_override_reaches_argv_and_models_json_together():
    sb = RecordingSandbox()
    a = _pi(sb)
    a._run_raw("i", "d", worktree="/tmp", repo=None, model="sk-l-secret")
    spec = sb.specs[0]
    assert "skgw/sk-l-secret" in spec.argv
    body = json.loads(spec.config_files["/agent/models.json"])
    assert body["providers"]["skgw"]["models"][0]["id"] == "sk-l-secret"
    # the statically configured model must NOT survive anywhere in the request
    assert "ornith-big" not in " ".join(spec.argv)
    assert "ornith-big" not in spec.config_files["/agent/models.json"]


def test_override_does_not_mutate_the_adapter():
    # The orchestrator shares ONE harness object across concurrent build threads,
    # so a per-call override that stuck to the instance would leak between cards.
    sb = RecordingSandbox()
    a = _pi(sb)
    a._run_raw("i", "d", worktree="/tmp", repo=None, model="sk-s-public")
    assert a.model == "ornith-big"
    a._run_raw("i", "d", worktree="/tmp", repo=None)
    assert "skgw/ornith-big" in sb.specs[1].argv


def test_adapter_without_support_refuses_the_override():
    # Refusal, not a silent downgrade to the static model: dropping the override
    # would run the card with its sensitivity ceiling discarded and no signal.
    sb = RecordingSandbox()
    a = OpenCodeAdapter(sb, model="x", base_url=GATEWAY_URL)
    assert a.supports_model_override() is False
    with pytest.raises(ModelOverrideUnsupported):
        a._run_raw("i", "d", worktree="/tmp", repo=None, model="sk-l-secret")
    assert sb.specs == []


def test_brief_carries_the_validated_bucket_to_the_adapter():
    class Brief:
        pass

    b = Brief()
    assert dispatch_model_of(b) is None
    attach_dispatch_model(b, "sk-m-internal")
    assert dispatch_model_of(b) == "sk-m-internal"
    attach_dispatch_model(b, None)
    assert dispatch_model_of(b) is None
    with pytest.raises(BucketError):
        attach_dispatch_model(b, "sk-m-internals")
