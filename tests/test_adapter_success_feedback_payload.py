"""The success memory must reach the MODEL, not merely the TaskBrief (card 0f61b5f7).

S9 built the success-memory reader, S18 wired `record_success` and seeded
`TaskBrief.prior_success_feedback` on the pass path, and 25+ tests passed. Every
one of those tests asserted on the BRIEF. `adapters/base.py.run_task` built its
JSON payload field by field and listed only `prior_feedback`, so the string was
recorded, read back, seeded onto the brief, and never sent. A feature that is
complete, tested and inert is indistinguishable at runtime from one that was
never written: this epic filed card bb536f68 for eight prior instances of exactly
that, and then formed a ninth inside itself.

So these tests deliberately do NOT assert `brief.prior_success_feedback == x`,
which is what S18 already proves and what would have passed throughout the bug.
They assert against the LaunchSpec handed to `Sandbox.spawn`, which is the last
object the harness controls before a process is launched. If the string is in
there, it reached the model; nothing between that point and the CLI can drop it.
"""
import json

import pytest

from skharness.autocode.adapters.base import BaseCliAdapter
from skharness.autocode.claude_code import DATA_BEGIN, DATA_END
from skharness.autocode.sandbox import AuthMount, Sandbox
from skharness.autocode.types import RepoSpec, TaskBrief


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


def _brief(**kw):
    base = dict(task_id="t1", repo=_repo(), worktree="/tmp/wt", title="t",
                description="d", acceptance=[], prior_feedback=None, round=0)
    base.update(kw)
    return TaskBrief(**base)


def _spawned_payload(brief) -> dict:
    """Run the adapter for real and return the JSON payload it actually sent.

    Intercepts at `Sandbox.spawn`, so everything upstream (run_task's payload
    construction, `frame`, `_argv`) executes as it does in production. The
    payload is recovered by parsing it back out of the framed prompt, which is
    the same bytes the CLI process would receive.
    """
    seen = {}
    sb = Sandbox(live_execution=True)

    def _spawn(spec, **kw):
        seen["spec"] = spec
        return {"result": {"ok": True}, "exit_code": 0}

    sb.spawn = _spawn
    _Fake(sb, egress_hosts=[]).run_task(brief)

    prompt = seen["spec"].argv[1]
    assert DATA_BEGIN in prompt and DATA_END in prompt, "prompt lost its data frame"
    body = prompt.split(DATA_BEGIN, 1)[1].split(DATA_END, 1)[0]
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise AssertionError(f"no JSON payload found inside the data frame: {body!r}")


def test_success_feedback_reaches_the_prompt_the_adapter_sends():
    """THE regression test for card 0f61b5f7. Removing the
    `prior_success_feedback` line from run_task's json.dumps turns this red."""
    memory = "SUCCESS-MEMORY-SENTINEL: the approach a grader already scored 5"
    payload = _spawned_payload(_brief(prior_success_feedback=memory))

    assert payload["prior_success_feedback"] == memory


def test_both_memory_channels_are_sent_and_stay_distinct():
    """The two channels carry different epistemic status: prior_feedback is a
    report from a run that FAILED, prior_success_feedback is an approach that
    was graded 5. Sending one under the other's key would tell the model to
    distrust a verified approach, or to trust an unverified one."""
    payload = _spawned_payload(_brief(prior_feedback="FAILURE-SIDE",
                                      prior_success_feedback="SUCCESS-SIDE"))

    assert payload["prior_feedback"] == "FAILURE-SIDE"
    assert payload["prior_success_feedback"] == "SUCCESS-SIDE"


def test_absent_success_memory_sends_the_key_as_null():
    """The common path: no prior success. The key is still present and null
    rather than absent, so the model sees one stable payload shape and the
    field's absence cannot be confused with a payload built by older code."""
    payload = _spawned_payload(_brief())

    assert "prior_success_feedback" in payload
    assert payload["prior_success_feedback"] is None


@pytest.mark.parametrize("field", ["task_id", "title", "description",
                                   "acceptance", "prior_feedback", "round"])
def test_the_fix_did_not_drop_a_pre_existing_field(field):
    """Negative control on the edit itself: this test would have passed before
    card 0f61b5f7 and must still pass after. Adding a key to a hand-built dict
    literal is exactly the kind of edit that quietly loses a neighbouring one."""
    payload = _spawned_payload(_brief(prior_feedback="f"))

    assert field in payload
