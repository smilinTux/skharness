"""Focused regressions for protected merge-method preflight in _gh_merge."""

from __future__ import annotations

import types as _t

import pytest

from skharness.autocode.engineering import EngineeringExecutor
from skharness.autocode.merge_method import (
    ProtectedMergePolicy,
    branch_allowed_from_rules_json,
    merge_argv,
    repository_allowed_from_repo_json,
    resolve_protected_merge_method,
)


def _spec(name: str = "skrender"):
    return _t.SimpleNamespace(
        name=name,
        path=f"/tmp/{name}",
        base_branch="main",
        integration_branch="main",
        test_cmd="pytest",
        ci="github",
        advisory_checks=[],
        automerge=True,
        min_diff_coverage=0.8,
    )


def _executor(mocker):
    return EngineeringExecutor(
        _t.SimpleNamespace(repo_map={}, automerge_repos=[]),
        board=mocker.Mock(),
        journal=mocker.Mock(),
    )


def test_resolve_prefers_squash_when_merge_commits_are_branch_rejected():
    """Merge-commit rejection with shared squash selects squash."""
    policy = ProtectedMergePolicy(
        repository_allowed=("merge", "squash"),
        branch_allowed=("squash",),
    )
    assert resolve_protected_merge_method(policy) == "squash"


@pytest.mark.parametrize(
    ("allowed", "expected"),
    [
        (("merge", "rebase", "squash"), "squash"),
        (("merge", "rebase"), "rebase"),
        (("merge",), "merge"),
    ],
)
def test_resolve_preference_is_deterministic(allowed, expected):
    policy = ProtectedMergePolicy(
        repository_allowed=allowed,
        branch_allowed=tuple(reversed(allowed)),
    )
    assert resolve_protected_merge_method(policy) == expected


@pytest.mark.parametrize(
    "policy",
    [
        None,
        ProtectedMergePolicy(repository_allowed=("merge",), branch_allowed=("squash",)),
        ProtectedMergePolicy(repository_allowed=("nope",), branch_allowed=("squash",)),
    ],
)
def test_resolve_fails_closed_without_shared_method(policy):
    assert resolve_protected_merge_method(policy) is None


def test_branch_rules_without_method_clause_allow_all_known_methods():
    assert branch_allowed_from_rules_json([]) == ("squash", "rebase", "merge")


def test_branch_rules_intersect_explicit_allowed_methods():
    rules = [
        {
            "type": "pull_request",
            "parameters": {"allowed_merge_methods": ["merge", "squash"]},
        },
        {
            "type": "pull_request",
            "parameters": {"allowed_merge_methods": ["squash", "rebase"]},
        },
    ]
    assert branch_allowed_from_rules_json(rules) == ("squash",)


def test_repository_flags_require_explicit_bools():
    assert repository_allowed_from_repo_json(
        {
            "allow_merge_commit": True,
            "allow_squash_merge": True,
            "allow_rebase_merge": False,
        }
    ) == ("squash", "merge")
    assert repository_allowed_from_repo_json({"allow_squash_merge": True}) is None


def test_gh_merge_argv_uses_squash_and_match_head(mocker):
    ex = _executor(mocker)
    repo = _spec()
    mocker.patch.object(
        ex,
        "_protected_merge_policy",
        return_value=ProtectedMergePolicy(
            repository_allowed=("merge", "squash"),
            branch_allowed=("squash",),
        ),
    )
    mocker.patch.object(ex, "_pr_head_sha", return_value="a" * 40)
    run = mocker.patch(
        "skharness.autocode.engineering.subprocess.run",
        return_value=_t.SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    assert ex._gh_merge(repo, "feature/x") is True
    argv = run.call_args.args[0]
    assert argv == merge_argv("feature/x", "squash", "a" * 40)
    assert "--merge" not in argv
    assert "--squash" in argv
    assert "--match-head-commit" in argv
    assert "--delete-branch" in argv


@pytest.mark.parametrize(
    "policy",
    [
        None,
        ProtectedMergePolicy(repository_allowed=("merge",), branch_allowed=("squash",)),
    ],
)
def test_gh_merge_issues_zero_merge_subprocess_when_policy_blocks(mocker, policy):
    ex = _executor(mocker)
    mocker.patch.object(ex, "_protected_merge_policy", return_value=policy)
    head = mocker.patch.object(ex, "_pr_head_sha", return_value="a" * 40)
    run = mocker.patch("skharness.autocode.engineering.subprocess.run")

    assert ex._gh_merge(_spec(), "feature/x") is False
    run.assert_not_called()
    head.assert_not_called()


def test_gh_merge_issues_zero_merge_subprocess_without_exact_head(mocker):
    ex = _executor(mocker)
    mocker.patch.object(
        ex,
        "_protected_merge_policy",
        return_value=ProtectedMergePolicy(
            repository_allowed=("squash",),
            branch_allowed=("squash",),
        ),
    )
    mocker.patch.object(ex, "_pr_head_sha", return_value="")
    run = mocker.patch("skharness.autocode.engineering.subprocess.run")

    assert ex._gh_merge(_spec(), "feature/x") is False
    run.assert_not_called()
