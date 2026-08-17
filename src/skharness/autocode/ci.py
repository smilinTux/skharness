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

from . import health
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


def _unusable(reason: str, **detail) -> None:
    """Record WHY the coverage arm produced no signal.

    S21: a returned None used to be indistinguishable from every other None, and
    a fabricated ratio was indistinguishable from a measured one. This is the
    observation that separates "coverage measured and met" from "the instrument
    never ran", and it is the point of the card, not decoration.
    """
    health.record("coverage_unusable", reason=reason, **detail)


def diff_coverage(repo: RepoSpec, worktree: str, diff: str) -> float | None:
    """Changed-lines coverage ratio, or None when there is no usable measurement.

    Runs coverage_cmd in the worktree (emitting a Cobertura coverage.xml), then
    scores only the lines the diff added. Computed outside the harness.

    S21 (card 53b8c8be): this function must return a MEASUREMENT, never a CLAIM.
    `_stage_work` deliberately resets `coverage.xml` out of the staged index so
    CI byproducts do not pollute the diff, and the grader reads `git diff
    --cached`, so a `coverage.xml` written by the worker is structurally
    invisible to the LLM arm of the twin gate. That exclusion is CORRECT (it is
    what lets a legitimate TDD change, whose new test files are untracked, be
    gradeable at all), so the fix is here rather than there: this arm makes a
    planted file impossible to read as a measurement.

    Four checks, in order, each returning None (the "no coverage signal"
    contract, which the twin gate already treats conservatively: no hands-off
    merge, the item surfaces as a review PR):

      1. any pre-existing coverage.xml is DELETED before cov_cmd runs, so a
         planted report cannot survive into the parse;
      2. cov_cmd's returncode is checked, so a failed coverage run and a
         successful one are no longer indistinguishable (a hang is bounded by
         the same ceiling local CI uses and is likewise not a pass);
      3. the emitted file's mtime must postdate the run, so a cov_cmd that
         restores a stale report cannot pass it off as current;
      4. a non-test source line in the diff that the report does not mention at
         all yields None rather than the old perfect 1.0, which is what a diff
         adding its own `omit` rule produced.
    """
    if not repo.coverage_cmd:
        return None
    cov_path = Path(worktree) / "coverage.xml"
    # (1) The planted-file defence. Anything already on disk was not produced by
    # THIS run, so it can only mislead. Delete it before measuring.
    try:
        cov_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:               # cannot guarantee freshness -> no signal
        _unusable("stale_report_not_removable", error=str(exc)[:120])
        return None
    started = time.time()
    cov_cmd = _scoped_cmd(repo.coverage_cmd, repo, worktree, diff)
    try:
        proc = subprocess.run(cov_cmd, shell=True, cwd=worktree,
                              capture_output=True, text=True,
                              timeout=_LOCAL_CI_TIMEOUT)
    except subprocess.TimeoutExpired:
        _unusable("command_timeout", repo=repo.name)
        return None
    # (2) An unrun or failed coverage command is not a measurement.
    if proc.returncode != 0:
        _unusable("command_failed", repo=repo.name, returncode=proc.returncode)
        return None
    changed = _changed_lines(diff)
    if not cov_path.exists():
        # coverage_cmd exited 0 but emitted no coverage.xml (a missing
        # --cov-report=xml, a wrong --cov target). Exit 0 alone is not evidence.
        _unusable("no_report_emitted", repo=repo.name)
        return None
    # (3) A report older than the run is a leftover, not this diff's measurement.
    # The one-second slack absorbs coarse filesystem mtime granularity; the
    # unlink above is what actually carries this check, so the slack cannot open
    # a hole a planted file could fit through.
    try:
        if cov_path.stat().st_mtime < started - 1.0:
            _unusable("stale_report", repo=repo.name)
            return None
    except OSError:
        _unusable("report_unreadable", repo=repo.name)
        return None
    try:
        root = ET.parse(str(cov_path)).getroot()
    except ET.ParseError:
        _unusable("report_malformed", repo=repo.name)
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
    if total == 0:
        # (4) Nothing of this diff appears in the report. Two very different
        # causes used to collapse into the same perfect 1.0:
        #   * the diff has no measurable source line at all (docs, config, a new
        #     test file): honestly unmeasurable, and 1.0 is the right answer, or
        #     every documentation card would fail the gate;
        #   * the diff DOES change non-test source, and the instrument still did
        #     not see it. That is what an `omit` rule added by the diff itself
        #     produces, and reporting 1.0 for it is the strongest gaming path in
        #     the whole gate. Refuse to certify: None routes to human review.
        src = [p for p, lines in changed.items()
               if lines and p.endswith(".py") and not _is_test_file(p)]
        if src:
            _unusable("changed_source_absent_from_report", repo=repo.name,
                      files=sorted(src)[:10])
            return None
        return 1.0
    return covered / total
