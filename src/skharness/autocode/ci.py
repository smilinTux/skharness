"""External CI verdict + diff-coverage, computed OUTSIDE the harness. The
load-bearing merge control (spec section 5.5)."""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from .types import RepoSpec

_POLL_INTERVAL = 5  # seconds between gh polls; patched to a no-op sleep in tests
#: Hard ceiling for a local: CI command. A hung test must never hang finalize
#: (which runs this AFTER the branch is pushed); a timeout is treated as red.
_LOCAL_CI_TIMEOUT = 900
#: How long to wait for a github-actions run to FIRST APPEAR before concluding
#: the branch is unpushed (the pre-commit gate) and returning `pending` instead of
#: polling the full ci_poll_timeout. A run only exists after the branch is pushed,
#: so with zero runs the poll would otherwise deadlock the whole build round.
_CI_FIRST_APPEARANCE_GRACE = 45

_RED_CONCLUSIONS = {"failure", "cancelled", "timed_out",
                    "action_required", "startup_failure"}


def _is_test_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name.startswith("test_") and name.endswith(".py")


def scoped_test_targets(diff: str, worktree: str) -> list[str]:
    """Worktree-relative test files relevant to a unified diff.

    The pre-commit twin gate must green a CORRECT change without being held
    hostage to unrelated pre-existing suite red (which, run whole, never clears
    and burns every Ralph round). So map the diff to just the tests that matter:

      * a changed/added test file (``test_*.py``) is included directly;
      * a changed source module ``.../foo.py`` pulls in every ``test_foo*.py``
        found under the worktree (catches the module's OWN existing regressions).

    Returns sorted, de-duplicated, existing paths relative to the worktree.
    """
    root = Path(worktree)
    targets: set[str] = set()
    for path in _changed_lines(diff):
        if _is_test_file(path):
            if (root / path).exists():
                targets.add(path)
            continue
        if path.endswith(".py"):
            stem = path.rsplit("/", 1)[-1][:-3]
            for hit in root.rglob(f"test_{stem}*.py"):
                targets.add(hit.relative_to(root).as_posix())
    return sorted(targets)


def _scoped_cmd(cmd: str, repo: RepoSpec, worktree: str | None,
                diff: str | None) -> str:
    """Append changed test targets to a pytest-style cmd when ci_scope=changed.

    Falls back to the verbatim cmd when scope is off, no diff/worktree is
    available, or the diff maps to no test targets (an untested change still
    runs the full cmd rather than passing by default)."""
    if getattr(repo, "ci_scope", "full") != "changed" or not diff or not worktree:
        return cmd
    targets = scoped_test_targets(diff, worktree)
    if not targets:
        return cmd
    return cmd + " " + " ".join(shlex.quote(t) for t in targets)


def external_ci_verdict(repo: RepoSpec, pr_branch: str, head_sha: str,
                        worktree: str | None = None, diff: str | None = None) -> str:
    """Return green|red|pending|none for the repo's external CI over head_sha.

    github-actions: poll `gh run list` up to ci_poll_timeout, map the run whose
    headSha matches. success -> green; a failing/unknown conclusion -> red; a
    poll timeout -> red. NEVER green on an unknown conclusion or a timeout.
    local:<cmd> runs in the worktree (where the harness's edits live) so it gates
    the current work, not the base checkout.
    """
    if repo.ci == "none":
        return "none"
    if repo.ci == "github-actions":
        deadline = time.monotonic() + repo.ci_poll_timeout
        first_appear_deadline = time.monotonic() + _CI_FIRST_APPEARANCE_GRACE
        seen_run = False
        while True:
            proc = subprocess.run(
                ["gh", "run", "list", "--branch", pr_branch,
                 "--json", "headSha,databaseId,status,conclusion"],
                cwd=repo.path, capture_output=True, text=True)
            runs = json.loads(proc.stdout or "[]")
            if runs:
                seen_run = True
            match = next((r for r in runs if r.get("headSha") == head_sha), None)
            if match is not None and match.get("status") == "completed":
                return "green" if match.get("conclusion") == "success" else "red"
            now = time.monotonic()
            # No run has EVER appeared for this branch within the grace window: the
            # branch is unpushed (this is the pre-commit gate), so a run will never
            # appear and polling the full ci_poll_timeout is a pure deadlock. Return
            # `pending` (honest: CI hasn't started) so the twin gate falls through to
            # its local/coverage signals instead of hanging the whole build round.
            # NB: a repo that expects to CONVERGE pre-push must use a `local:` ci.
            if not seen_run and now >= first_appear_deadline:
                return "pending"
            if now >= deadline:
                return "red"        # a run existed but never completed: fail-safe
            time.sleep(_POLL_INTERVAL)
    if repo.ci.startswith("local:"):
        cmd = _scoped_cmd(repo.ci[len("local:"):], repo, worktree, diff)
        try:
            proc = subprocess.run(cmd, shell=True, cwd=(worktree or repo.path),
                                  capture_output=True, text=True,
                                  timeout=_LOCAL_CI_TIMEOUT)
        except subprocess.TimeoutExpired:
            return "red"                 # a hung CI command is not a pass
        return "green" if proc.returncode == 0 else "red"
    return "red"


def _changed_lines(diff: str) -> dict[str, set[int]]:
    """Map post-image path -> set of added line numbers from a unified diff."""
    changed: dict[str, set[int]] = {}
    cur: str | None = None
    newno = 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            cur = path[2:] if path.startswith("b/") else path
            changed.setdefault(cur, set())
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            newno = int(m.group(1)) if m else 0
        elif cur is not None:
            if line.startswith("+") and not line.startswith("+++"):
                changed[cur].add(newno)
                newno += 1
            elif line.startswith("-") and not line.startswith("---"):
                continue
            else:
                newno += 1
    return changed


def diff_coverage(repo: RepoSpec, worktree: str, diff: str) -> float | None:
    """Changed-lines coverage ratio, or None when the repo has no coverage_cmd.

    Runs coverage_cmd in the worktree (emitting a Cobertura coverage.xml), then
    scores only the lines the diff added. Computed outside the harness.
    """
    if not repo.coverage_cmd:
        return None
    cov_cmd = _scoped_cmd(repo.coverage_cmd, repo, worktree, diff)
    subprocess.run(cov_cmd, shell=True, cwd=worktree,
                   capture_output=True, text=True)
    changed = _changed_lines(diff)
    cov_path = Path(worktree) / "coverage.xml"
    if not cov_path.exists():
        # coverage_cmd ran but emitted no coverage.xml (missing pytest-cov, a
        # wrong --cov target, or the test command itself errored). Degrade to the
        # "no coverage signal" contract (None) instead of crashing the whole run;
        # the twin gate already treats None conservatively (no hands-off merge),
        # so the item surfaces as a review PR rather than killing the pass.
        return None
    try:
        root = ET.parse(str(cov_path)).getroot()
    except ET.ParseError:
        return None
    covered = missed = 0
    for cls in root.iter("class"):
        want = changed.get(cls.get("filename"))
        if not want:
            continue
        for ln in cls.iter("line"):
            n = int(ln.get("number"))
            if n in want:
                if int(ln.get("hits", "0")) > 0:
                    covered += 1
                else:
                    missed += 1
    total = covered + missed
    return 1.0 if total == 0 else covered / total
