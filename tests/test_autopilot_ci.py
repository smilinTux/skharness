import json
import subprocess
import pytest
from skharness.autocode import ci
from skharness.autocode.types import RepoSpec


def _gha_repo(**kw):
    ci_poll_timeout = kw.pop('ci_poll_timeout', 1200)
    return RepoSpec(name="skrender", path="/repos/skrender", base_branch="main",
                    integration_branch="develop", test_cmd="pytest",
                    ci="github-actions", ci_poll_timeout=ci_poll_timeout, **kw)


def _runs_json(*runs):
    return subprocess.CompletedProcess(args=[], returncode=0,
                                       stdout=json.dumps(list(runs)), stderr="")


def test_gha_success_is_green_and_builds_expected_argv(mocker):
    repo = _gha_repo()
    run = mocker.patch("skharness.autocode.ci.subprocess.run", return_value=_runs_json(
        {"headSha": "abc123", "databaseId": 9, "status": "completed", "conclusion": "success"}))
    mocker.patch("skharness.autocode.ci.time.sleep")
    assert ci.external_ci_verdict(repo, "autopilot/t1", "abc123") == "green"
    argv = run.call_args.args[0]
    assert argv == ["gh", "run", "list", "--branch", "autopilot/t1",
                    "--json", "headSha,databaseId,status,conclusion"]
    assert run.call_args.kwargs["cwd"] == "/repos/skrender"


@pytest.mark.parametrize("concl", ["failure", "cancelled", "timed_out"])
def test_gha_failure_conclusions_are_red(mocker, concl):
    mocker.patch("skharness.autocode.ci.time.sleep")
    mocker.patch("skharness.autocode.ci.subprocess.run", return_value=_runs_json(
        {"headSha": "abc123", "status": "completed", "conclusion": concl}))
    assert ci.external_ci_verdict(_gha_repo(), "b", "abc123") == "red"


def test_gha_unknown_conclusion_never_green(mocker):
    mocker.patch("skharness.autocode.ci.time.sleep")
    mocker.patch("skharness.autocode.ci.subprocess.run", return_value=_runs_json(
        {"headSha": "abc123", "status": "completed", "conclusion": "neutral"}))
    assert ci.external_ci_verdict(_gha_repo(), "b", "abc123") == "red"


def test_gha_no_run_ever_appears_returns_pending_not_deadlock(mocker):
    # THE FINALIZE-STALL FIX: pre-push, the branch has NO runs, so polling the full
    # ci_poll_timeout was a pure deadlock (0 CPU for the whole build round). With no
    # run appearing within the first-appearance grace, return `pending` fast.
    mocker.patch("skharness.autocode.ci.time.sleep")
    mocker.patch("skharness.autocode.ci.subprocess.run", return_value=_runs_json())
    # monotonic: deadline base, grace base, then a value past the grace window
    mocker.patch("skharness.autocode.ci.time.monotonic", side_effect=[0.0, 0.0, 100.0])
    assert ci.external_ci_verdict(_gha_repo(ci_poll_timeout=1200), "b", "abc123") == "pending"


def test_gha_run_exists_but_never_completes_is_red(mocker):
    # A run DID appear for the branch (seen_run=True) but never completes -> the
    # fail-safe still applies: timeout is red, never green, and it does NOT return
    # the fast `pending` (that path is only for a branch with zero runs).
    mocker.patch("skharness.autocode.ci.time.sleep")
    mocker.patch("skharness.autocode.ci.subprocess.run", return_value=_runs_json(
        {"headSha": "abc123", "status": "in_progress", "conclusion": None}))
    mocker.patch("skharness.autocode.ci.time.monotonic", side_effect=[0.0, 0.0, 9999.0])
    assert ci.external_ci_verdict(_gha_repo(ci_poll_timeout=1), "b", "abc123") == "red"


def test_ci_none_returns_sentinel(mocker):
    run = mocker.patch("skharness.autocode.ci.subprocess.run")
    repo = RepoSpec(name="x", path="/x", base_branch="main", integration_branch="develop",
                    test_cmd="pytest", ci="none")
    assert ci.external_ci_verdict(repo, "b", "sha") == "none"
    run.assert_not_called()


def _local_repo(cmd):
    return RepoSpec(name="skr", path="/repos/skr", base_branch="main",
                    integration_branch="develop", test_cmd="pytest", ci=f"local:{cmd}")


def test_local_exit0_is_green_runs_cmd_in_repo(mocker):
    run = mocker.patch("skharness.autocode.ci.subprocess.run",
                       return_value=subprocess.CompletedProcess(args=[], returncode=0))
    assert ci.external_ci_verdict(_local_repo("make ci"), "b", "sha") == "green"
    assert run.call_args.args[0] == "make ci"
    assert run.call_args.kwargs["shell"] is True
    assert run.call_args.kwargs["cwd"] == "/repos/skr"


def test_local_nonzero_is_red(mocker):
    mocker.patch("skharness.autocode.ci.subprocess.run",
                 return_value=subprocess.CompletedProcess(args=[], returncode=1))
    assert ci.external_ci_verdict(_local_repo("make ci"), "b", "sha") == "red"


_COBERTURA = """<?xml version="1.0" ?>
<coverage><packages><package><classes>
<class filename="src/skr/foo.py"><lines>
<line number="10" hits="1"/><line number="11" hits="0"/>
<line number="12" hits="1"/><line number="99" hits="0"/>
</lines></class></classes></package></packages></coverage>"""

_DIFF = """diff --git a/src/skr/foo.py b/src/skr/foo.py
--- a/src/skr/foo.py
+++ b/src/skr/foo.py
@@ -9,3 +10,3 @@ def foo():
+    added_a = 1
+    added_b = 2
+    added_c = 3
"""


def _cov_repo(tmp, cmd="pytest --cov --cov-report=xml", **kw):
    return RepoSpec(name="skr", path=str(tmp), base_branch="main",
                    integration_branch="develop", test_cmd="pytest",
                    ci="none", coverage_cmd=cmd, **kw)


def _emitting_run(mocker, tmp_path, body=None, returncode=0):
    """Patch subprocess.run so the mocked coverage command behaves like a REAL
    one: it EMITS coverage.xml as a side effect.

    S21: writing the file in the test body and mocking the command to do nothing
    is precisely the shape of the gaming path this card closed (a report on disk
    that the coverage command did not produce). `diff_coverage` now deletes any
    pre-existing report before running, so a fixture that plants one is no longer
    a test of the measurement path at all.
    """
    def _side(*a, **kw):
        if body is not None:
            (tmp_path / "coverage.xml").write_text(body, encoding="utf-8")
        return subprocess.CompletedProcess(args=a[0] if a else [],
                                           returncode=returncode)
    return mocker.patch("skharness.autocode.ci.subprocess.run", side_effect=_side)


def test_diff_coverage_ratio_over_changed_lines_only(mocker, tmp_path):
    run = _emitting_run(mocker, tmp_path, _COBERTURA)
    repo = _cov_repo(tmp_path, min_diff_coverage=0.8)
    # changed lines 10,11,12 -> covered {10,12}, missed {11}; line 99 not in the diff
    ratio = ci.diff_coverage(repo, str(tmp_path), _DIFF)
    assert ratio == pytest.approx(2 / 3)
    assert ratio < repo.min_diff_coverage        # threshold compare the caller makes
    assert run.call_args.kwargs["cwd"] == str(tmp_path)


def test_diff_coverage_none_when_no_coverage_cmd(mocker, tmp_path):
    run = mocker.patch("skharness.autocode.ci.subprocess.run")
    repo = _cov_repo(tmp_path)
    repo.coverage_cmd = None
    assert ci.diff_coverage(repo, str(tmp_path), _DIFF) is None
    run.assert_not_called()


def test_diff_coverage_none_when_coverage_xml_missing(mocker, tmp_path):
    # coverage_cmd ran but produced no coverage.xml (e.g. pytest-cov absent or
    # the test cmd errored). Must degrade to None, NOT crash the whole run.
    mocker.patch("skharness.autocode.ci.subprocess.run",
                 return_value=subprocess.CompletedProcess(args=[], returncode=1))
    repo = _cov_repo(tmp_path)  # coverage_cmd set, but no coverage.xml on disk
    assert not (tmp_path / "coverage.xml").exists()
    assert ci.diff_coverage(repo, str(tmp_path), _DIFF) is None


def test_diff_coverage_none_when_coverage_xml_malformed(mocker, tmp_path):
    # The coverage command really emits a report; the report is unparseable.
    _emitting_run(mocker, tmp_path, "<not-valid-xml")
    repo = _cov_repo(tmp_path)
    assert ci.diff_coverage(repo, str(tmp_path), _DIFF) is None


# ── changed-scope CI: gate a correct change without being hostage to unrelated
# pre-existing suite red (the pre-commit Ralph-loop convergence fix) ──────────

def _scope_tree(tmp_path):
    """A worktree with src modules + their test files, mirroring skchat layout."""
    (tmp_path / "src" / "skchat").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    for p in ["src/skchat/transport.py", "src/skchat/crypto.py", "src/skchat/daemon.py"]:
        (tmp_path / p).write_text("x = 1\n", encoding="utf-8")
    for p in ["tests/test_transport.py", "tests/test_transport_fail_closed.py",
              "tests/test_crypto_signing_degraded.py", "tests/test_unrelated.py"]:
        (tmp_path / p).write_text("def test_x():\n    assert True\n", encoding="utf-8")
    return tmp_path


_SCOPE_DIFF = (
    "diff --git a/src/skchat/transport.py b/src/skchat/transport.py\n"
    "--- a/src/skchat/transport.py\n+++ b/src/skchat/transport.py\n"
    "@@ -1,1 +1,2 @@\n+added = 1\n"
    "diff --git a/src/skchat/crypto.py b/src/skchat/crypto.py\n"
    "--- a/src/skchat/crypto.py\n+++ b/src/skchat/crypto.py\n"
    "@@ -1,1 +1,2 @@\n+added = 2\n"
    "diff --git a/tests/test_transport_fail_closed.py b/tests/test_transport_fail_closed.py\n"
    "--- /dev/null\n+++ b/tests/test_transport_fail_closed.py\n"
    "@@ -0,0 +1,2 @@\n+def test_new():\n+    assert True\n"
    "diff --git a/tests/test_crypto_signing_degraded.py b/tests/test_crypto_signing_degraded.py\n"
    "--- /dev/null\n+++ b/tests/test_crypto_signing_degraded.py\n"
    "@@ -0,0 +1,2 @@\n+def test_new2():\n+    assert True\n"
)


def test_scoped_test_targets_maps_changed_sources_and_includes_changed_tests(tmp_path):
    _scope_tree(tmp_path)
    targets = ci.scoped_test_targets(_SCOPE_DIFF, str(tmp_path))
    # changed source transport.py -> its existing + new test files;
    # changed source crypto.py -> its degraded test; changed test files included directly.
    assert set(targets) == {
        "tests/test_transport.py",
        "tests/test_transport_fail_closed.py",
        "tests/test_crypto_signing_degraded.py",
    }
    # an unrelated test file is NOT pulled in (that's the whole point)
    assert "tests/test_unrelated.py" not in targets


def test_scoped_test_targets_empty_when_no_test_mapping(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "orphan.py").write_text("x=1\n", encoding="utf-8")
    diff = ("diff --git a/src/orphan.py b/src/orphan.py\n"
            "--- a/src/orphan.py\n+++ b/src/orphan.py\n@@ -1 +1,2 @@\n+y=2\n")
    assert ci.scoped_test_targets(diff, str(tmp_path)) == []


def test_local_changed_scope_runs_only_changed_targets(mocker, tmp_path):
    _scope_tree(tmp_path)
    run = mocker.patch("skharness.autocode.ci.subprocess.run",
                       return_value=subprocess.CompletedProcess(args=[], returncode=0))
    repo = RepoSpec(name="skchat", path=str(tmp_path), base_branch="main",
                    integration_branch="develop", test_cmd="pytest",
                    ci="local:pytest -q -m 'not integration'", ci_scope="changed")
    assert ci.external_ci_verdict(repo, "b", "sha", worktree=str(tmp_path),
                                  diff=_SCOPE_DIFF) == "green"
    cmd = run.call_args.args[0]
    assert cmd.startswith("pytest -q -m 'not integration'")
    assert "tests/test_transport.py" in cmd
    assert "tests/test_crypto_signing_degraded.py" in cmd
    assert "tests/test_unrelated.py" not in cmd      # unrelated red can't block us


def test_local_changed_scope_falls_back_to_full_when_no_targets(mocker, tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "orphan.py").write_text("x=1\n", encoding="utf-8")
    diff = ("diff --git a/src/orphan.py b/src/orphan.py\n"
            "--- a/src/orphan.py\n+++ b/src/orphan.py\n@@ -1 +1,2 @@\n+y=2\n")
    run = mocker.patch("skharness.autocode.ci.subprocess.run",
                       return_value=subprocess.CompletedProcess(args=[], returncode=0))
    repo = RepoSpec(name="skchat", path=str(tmp_path), base_branch="main",
                    integration_branch="develop", test_cmd="pytest",
                    ci="local:pytest -q", ci_scope="changed")
    ci.external_ci_verdict(repo, "b", "sha", worktree=str(tmp_path), diff=diff)
    assert run.call_args.args[0] == "pytest -q"      # no targets -> run full cmd


def test_local_full_scope_is_unchanged_default(mocker, tmp_path):
    _scope_tree(tmp_path)
    run = mocker.patch("skharness.autocode.ci.subprocess.run",
                       return_value=subprocess.CompletedProcess(args=[], returncode=0))
    # default ci_scope="full": the diff is ignored, the whole cmd runs verbatim
    repo = RepoSpec(name="skchat", path=str(tmp_path), base_branch="main",
                    integration_branch="develop", test_cmd="pytest", ci="local:pytest -q")
    ci.external_ci_verdict(repo, "b", "sha", worktree=str(tmp_path), diff=_SCOPE_DIFF)
    assert run.call_args.args[0] == "pytest -q"


def test_diff_coverage_scopes_cmd_to_changed_targets(mocker, tmp_path):
    _scope_tree(tmp_path)
    run = _emitting_run(mocker, tmp_path, _COBERTURA)
    repo = RepoSpec(name="skchat", path=str(tmp_path), base_branch="main",
                    integration_branch="develop", test_cmd="pytest",
                    ci="none", coverage_cmd="pytest --cov --cov-report=xml",
                    ci_scope="changed")
    ci.diff_coverage(repo, str(tmp_path), _SCOPE_DIFF)
    cmd = run.call_args.args[0]
    assert cmd.startswith("pytest --cov --cov-report=xml")
    assert "tests/test_transport.py" in cmd


def test_local_ci_timeout_is_red_not_a_hang(mocker):
    """A hung local CI command must not hang finalize forever (it runs AFTER the
    branch is pushed). A timeout is red, and the call is bounded by a timeout."""
    run = mocker.patch("skharness.autocode.ci.subprocess.run",
                       side_effect=subprocess.TimeoutExpired(cmd="make ci", timeout=1))
    assert ci.external_ci_verdict(_local_repo("make ci"), "b", "sha") == "red"
    assert run.call_args.kwargs.get("timeout") == ci._LOCAL_CI_TIMEOUT
