"""S17: the self-modification floor must EXECUTE on the path that actually runs.

THE FACT this file exists to fix (card 53e9190c, verified in code):
`command grep -rn 'changed_paths_are_protected|is_protected' src/` returned
EXACTLY ONE call site, `engineering.py`, inside `if automerge:`. `automerge`
requires `repo.name in self.config.automerge_repos`, and that list is `[]` in all
four live configs. So the carve-out never ran, while three documents (the
CHANGELOG, card 09573989 and the epic 935d4b61 design spec) cited it as the
protection against self-modification.

The real protection today is that auto-merge is globally off, so every build
reaches a human PR. That is genuine, but it is one config flag away from
disappearing, and the backstop meant to catch that flag was the inert part.

THE NEGATIVE CONTROL, which is the whole point: before this card no observation
distinguished "the carve-out is protecting us" from "the carve-out never ran".
Both look like a quiet log. The tests below are that observation.

None of these tests touch the joule wallet: `_settle_economics` is patched out on
every executor, so no finalize here can mint.
"""
from __future__ import annotations

import types as _t

import pytest

from skharness.autocode import protected
from skharness.autocode.engineering import EngineeringExecutor
from skharness.autocode.types import GateResult, RepoSpec, WorkItem

# A guardrail file on the hard-coded floor, and an ordinary one that is not.
GUARDRAIL = "src/skcapstone/itil.py"
ORDINARY = "src/skcapstone/fleet/cron.py"


def _spec(name="skrender", *, automerge=False, ci="none"):
    s = RepoSpec(name=name, path=f"/repos/{name}", base_branch="main",
                 integration_branch="develop", test_cmd="pytest", ci=ci)
    s.automerge = automerge
    return s


def _ex(mocker, *, automerge_repos, changed, repo_automerge=False,
        checks="green"):
    spec = _spec(automerge=repo_automerge, ci=("github" if repo_automerge else "none"))
    cfg = _t.SimpleNamespace(repo_map={"skrender": spec},
                             automerge_repos=list(automerge_repos))
    ex = EngineeringExecutor(cfg, board=mocker.Mock(), journal=mocker.Mock(),
                             digest=mocker.Mock(), agent_name="autopilot")
    ex.journal.worktree_for.return_value = "/wt/t1"
    ex.board.clear_attempts.return_value = []
    mocker.patch("skharness.autocode.engineering.os.path.isdir", return_value=True)
    mocker.patch.object(ex, "_settle_economics")      # never touch the real wallet
    mocker.patch.object(ex, "_head_sha", return_value="sha1")
    mocker.patch.object(ex, "_commit_and_push")
    mocker.patch.object(ex, "prune_worktree")
    mocker.patch.object(ex, "_gh_merge", return_value=True)
    mocker.patch.object(ex, "_github_checks_verdict", return_value=checks)
    mocker.patch.object(ex, "_open_pr", return_value="https://gh/pr/1")
    mocker.patch.object(ex, "_fleet_root", return_value="/nonexistent-fleet-root")
    mocker.patch.object(ex, "_changed_paths", return_value=list(changed))
    item = WorkItem(kind="engineering", ref="t1", source="coord", repo=None,
                    payload={"tags": ["repo:skrender"], "title": "t"})
    return ex, item


def _passed():
    return GateResult(score=5, passed=True, notes="", artifact="pr")


def _events(rec, kind):
    return [c.kwargs for c in rec.call_args_list
            if c.args and c.args[0] == kind]


# -- the observation that did not exist -------------------------------------

@pytest.mark.parametrize("automerge_repos, repo_automerge", [
    ([], False),                       # the live configuration of all four nodes
    (["skrender"], True),              # the one config flag away
])
def test_the_carveout_is_consulted_on_every_finalize(mocker, automerge_repos,
                                                     repo_automerge):
    """THE load-bearing test. The floor must be evaluated on the path that
    actually runs, not only inside `if automerge:`.

    Parametrised over both worlds on purpose, so this single test FAILS if
    auto-merge is ever enabled while the carve-out path is unreachable, AND
    fails if someone re-guards the evaluation behind the auto-merge branch.
    """
    ex, item = _ex(mocker, automerge_repos=automerge_repos,
                   repo_automerge=repo_automerge, changed=[GUARDRAIL])
    spy = mocker.spy(protected, "matched_protected_paths")
    inner = mocker.spy(protected, "is_protected")
    rec = mocker.patch("skharness.autocode.engineering.health.record")
    ex.finalize(item, _passed())
    assert spy.call_count == 1                       # consulted, exactly once
    assert inner.called                              # and it really reaches the floor
    ev = _events(rec, "carveout_evaluated")
    assert len(ev) == 1
    assert ev[0]["protected"] is True
    assert ev[0]["paths"] == [GUARDRAIL]
    assert ev[0]["automerge"] is bool(automerge_repos and repo_automerge)


def test_an_ordinary_diff_records_the_carveout_as_evaluated_and_clean(mocker):
    """The other half of the observation. A silent log used to mean either "no
    guardrail was touched" or "nothing ever looked". Now a clean evaluation is
    written down as a clean evaluation, so absence of a hold is evidence."""
    ex, item = _ex(mocker, automerge_repos=[], changed=[ORDINARY])
    rec = mocker.patch("skharness.autocode.engineering.health.record")
    ex.finalize(item, _passed())
    ev = _events(rec, "carveout_evaluated")
    assert len(ev) == 1 and ev[0]["protected"] is False and ev[0]["paths"] == []


def test_the_pr_only_review_prompt_names_the_guardrail_files(mocker):
    """PR-only work still writes a diff and still asks a human to merge it, so
    the floor's verdict must reach the human making that decision. Before this
    card the prompt was the same sentence whether or not the diff rewrote the
    twin gate."""
    ex, item = _ex(mocker, automerge_repos=[], changed=[GUARDRAIL, ORDINARY])
    ex.finalize(item, _passed())
    prompt = ex.digest.queue_decision.call_args.kwargs["prompt"]
    assert "PROTECTED GUARDRAIL" in prompt
    assert GUARDRAIL in prompt
    assert ORDINARY not in prompt          # only the matches, not the whole diff


def test_the_pr_only_prompt_is_unchanged_for_ordinary_work(mocker):
    """Negative control for the annotation: if every PR were flagged the flag
    would carry no information."""
    ex, item = _ex(mocker, automerge_repos=[], changed=[ORDINARY])
    ex.finalize(item, _passed())
    prompt = ex.digest.queue_decision.call_args.kwargs["prompt"]
    assert "PROTECTED GUARDRAIL" not in prompt
    assert prompt.startswith("Merge PR")


def test_a_failed_build_still_evaluates_the_carveout(mocker):
    """finalize runs for non-passing results too (they still commit, push and
    open a PR a human may merge). The floor is about what the diff TOUCHES, not
    about whether the gate liked it."""
    ex, item = _ex(mocker, automerge_repos=[], changed=[GUARDRAIL])
    rec = mocker.patch("skharness.autocode.engineering.health.record")
    ex.finalize(item, GateResult(score=2, passed=False, notes="", artifact=None))
    ev = _events(rec, "carveout_evaluated")
    assert len(ev) == 1 and ev[0]["protected"] is True


# -- the existing auto-merge hold must be preserved exactly -------------------

def test_the_carveout_still_holds_an_automerge_of_a_guardrail_diff(mocker):
    """Positive control on the behaviour that already existed: making the floor
    reachable must not have weakened the one place it already worked."""
    ex, item = _ex(mocker, automerge_repos=["skrender"], repo_automerge=True,
                   changed=[GUARDRAIL])
    ex.finalize(item, _passed())
    ex._gh_merge.assert_not_called()
    ex._github_checks_verdict.assert_not_called()   # short-circuits before the poll
    prompt = ex.digest.queue_decision.call_args.kwargs["prompt"]
    assert "CARVE-OUT HELD" in prompt
    assert GUARDRAIL in prompt                      # names WHAT it held on


def test_ordinary_work_still_automerges(mocker):
    ex, item = _ex(mocker, automerge_repos=["skrender"], repo_automerge=True,
                   changed=[ORDINARY])
    ex.finalize(item, _passed())
    ex._gh_merge.assert_called_once()


# -- the manifest half is unsigned, so only the tuple is unforgeable ----------

def test_the_hard_coded_floor_holds_without_any_manifest(tmp_path):
    """Card note: the manifest half is UNSIGNED (`"signature": null`) and no
    caller passes `verify`, so the only unforgeable half is `_ALWAYS_PROTECTED`.
    That is why this card's fix routes through the tuple and does not depend on
    the manifest: with no manifest at all, the core guardrails are still held.
    """
    assert protected.changed_paths_are_protected(tmp_path, [GUARDRAIL]) is True
    assert protected.matched_protected_paths(tmp_path, [GUARDRAIL]) == [GUARDRAIL]
    assert protected.matched_protected_paths(tmp_path, [ORDINARY]) == []


def test_matched_paths_are_the_matches_not_the_whole_change_set(tmp_path):
    """The gate answered a bare bool, so a hold could not say WHAT it held on.
    An operator cannot review a decision whose reason is not in it."""
    got = protected.matched_protected_paths(
        tmp_path, [ORDINARY, GUARDRAIL, "src/skharness/autocode/grading.py"])
    assert got == [GUARDRAIL, "src/skharness/autocode/grading.py"]
