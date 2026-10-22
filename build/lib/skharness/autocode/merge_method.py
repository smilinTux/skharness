"""Fail-closed protected merge-method preflight for GitHub PR merges.

Local pure equivalent of the SKCapstone protected-method contract: repository
allow flags and protected-branch method policy must both be read explicitly,
then squash > rebase > merge is selected only from their intersection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_MERGE_METHODS = ("squash", "rebase", "merge")
_REPO_FLAG_TO_METHOD = {
    "allow_squash_merge": "squash",
    "allow_rebase_merge": "rebase",
    "allow_merge_commit": "merge",
}


@dataclass(frozen=True)
class ProtectedMergePolicy:
    """Explicit repository and protected-branch merge-method readback."""

    repository_allowed: tuple[str, ...]
    branch_allowed: tuple[str, ...]


def resolve_protected_merge_method(policy: ProtectedMergePolicy | None) -> str | None:
    """Select the preferred explicitly allowed method, or fail closed.

    Args:
        policy: Repository plus protected-branch allow lists, or None.

    Returns:
        One of squash/rebase/merge, or None when policy is missing/malformed
        or the intersection is empty.
    """
    if policy is None:
        return None
    repository = set(policy.repository_allowed)
    protected_branch = set(policy.branch_allowed)
    if not repository.issubset(_MERGE_METHODS) or not protected_branch.issubset(
        _MERGE_METHODS
    ):
        return None
    return next(
        (
            method
            for method in _MERGE_METHODS
            if method in repository and method in protected_branch
        ),
        None,
    )


def repository_allowed_from_repo_json(data: Any) -> tuple[str, ...] | None:
    """Parse repository allow_* flags into method names.

    Args:
        data: Decoded ``gh api repos/:owner/:repo`` JSON object.

    Returns:
        Allowed methods, or None when any required flag is absent/non-bool.
    """
    if not isinstance(data, dict):
        return None
    allowed: list[str] = []
    for flag, method in _REPO_FLAG_TO_METHOD.items():
        value = data.get(flag)
        if not isinstance(value, bool):
            return None
        if value:
            allowed.append(method)
    return tuple(allowed)


def branch_allowed_from_rules_json(rules: Any) -> tuple[str, ...] | None:
    """Parse ``rules/branches/{branch}`` into explicit branch-allowed methods.

    Args:
        rules: Decoded rules list. An empty list means the branch read succeeded
            and imposes no further method restriction (all known methods).

    Returns:
        Allowed methods, or None when the payload is malformed.
    """
    if not isinstance(rules, list):
        return None
    declared: list[set[str]] = []
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("type") != "pull_request":
            continue
        params = rule.get("parameters")
        if not isinstance(params, dict) or "allowed_merge_methods" not in params:
            continue
        methods = params.get("allowed_merge_methods")
        if not isinstance(methods, list) or not methods:
            return None
        normalized: set[str] = set()
        for item in methods:
            if not isinstance(item, str):
                return None
            name = item.strip().lower()
            if name not in _MERGE_METHODS:
                return None
            normalized.add(name)
        declared.append(normalized)
    if not declared:
        # Reason: a successful empty rules read means no branch-level method
        # restriction; repository flags still narrow the intersection.
        return _MERGE_METHODS
    shared = set.intersection(*declared)
    return tuple(method for method in _MERGE_METHODS if method in shared)


def merge_argv(pr_branch: str, method: str, head_sha: str) -> list[str]:
    """Build the exact ``gh pr merge`` argv for a selected method and head.

    Args:
        pr_branch: PR number, URL, or head branch name.
        method: Selected merge method (squash/rebase/merge).
        head_sha: Exact PR head commit to bind with ``--match-head-commit``.

    Returns:
        Argv list with delete-branch and exact-head match flags.
    """
    return [
        "gh",
        "pr",
        "merge",
        pr_branch,
        f"--{method}",
        "--delete-branch",
        "--match-head-commit",
        head_sha,
    ]
