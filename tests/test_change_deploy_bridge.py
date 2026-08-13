"""P3.2 bridge tests: skharness.autocode.change_deploy_bridge.

Design doc: docs/specs/2026-08-13-change-management-cab-ai-arch.md (skcapstone
repo), section 5.2. This is the one and only merge authority in the change
management pipeline, so every guard is proven here with the ITILManager fold,
capauth, and every ``gh`` call mocked -- no test in this file ever invokes a
real ``gh`` subprocess or mutates a real repository.

No em/en dashes anywhere (SKWorld hard rule).
"""

from __future__ import annotations

import types as _t
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from skharness.autocode.change_deploy_bridge import build_deploy_dispatcher, deploy_dispatch

# --------------------------------------------------------------------------- #
# Fixtures / helpers                                                          #
# --------------------------------------------------------------------------- #

_PR_URL = "https://github.com/acme/skrender/pull/42"
_HEAD_SHA = "deadbeef00"


def _change(
    status="scheduled",
    prepared_pr="default",
    prepared_by="lumina",
    validation="default",
    scheduled_window="default",
    change_id="chg-abc12345",
):
    """Build a real ``skcoord.itil.Change`` (pydantic model), defaulting every
    field to a fully-green scheduled change so each test only overrides what
    it is exercising."""
    from skcoord.itil import Change, ChangeStatus, ChangeType, Risk

    if prepared_pr == "default":
        prepared_pr = {
            "url": _PR_URL,
            "branch": "autopilot/x",
            "run_id": "r1",
            "head_sha": _HEAD_SHA,
        }
    if validation == "default":
        validation = {
            "passed": True,
            "head_sha": _HEAD_SHA,
            "url": _PR_URL,
            "summary": "3/3 passed",
            "checks": [],
        }
    if scheduled_window == "default":
        now = datetime.now(timezone.utc)
        scheduled_window = {
            "window_start": (now - timedelta(hours=1)).isoformat(),
            "window_end": (now + timedelta(hours=1)).isoformat(),
            "asap": False,
            "deploy_mode": "confirm",
        }
    return Change(
        id=change_id,
        title="ship it",
        change_type=ChangeType.NORMAL,
        status=ChangeStatus(status),
        risk=Risk.MEDIUM,
        created_by="lumina",
        managed_by="lumina",
        prepared_pr=prepared_pr,
        prepared_by=prepared_by,
        validation=validation,
        scheduled_window=scheduled_window,
    )


class _FakeITILManager:
    """Stands in for ``skcoord.itil.ITILManager``: real ``cab_dir`` on disk
    (arm files are read with real ``Path.glob``/``read_text``), everything
    else (fold, event log) is captured in-memory so no test touches a real
    ``~/.skcapstone`` registry."""

    def __init__(self, home, *, chg, cab_dir: Path):
        self.home = home
        self.changes_dir = Path("/fake/itil/changes")
        self.cab_dir = cab_dir
        self._chg = chg
        self.events: list[tuple[str, str, str, dict]] = []

    def _resolve_id(self, directory, change_id):
        return change_id

    def _load_core(self, directory, rid):
        return {"id": rid}  # non-None -> "exists"

    def _fold_record(self, directory, rid, model_class):
        return self._chg

    def _append_event(self, directory, rid, agent, kind, **payload):
        self.events.append((rid, agent, kind, payload))


def _write_arm(cab_dir: Path, change_id: str, agent: str, armed: bool = True, note: str = ""):
    cab_dir.mkdir(parents=True, exist_ok=True)
    path = cab_dir / f"{change_id}-{agent}.arm.json"
    path.write_text(
        __import__("json").dumps(
            {
                "change_id": change_id,
                "agent": agent,
                "armed": armed,
                "armed_at": datetime.now(timezone.utc).isoformat(),
                "note": note,
            }
        )
    )
    return path


def _identity(fqid="lumina@chef.skworld", capauth_uri="capauth:lumina@skworld.io"):
    return _t.SimpleNamespace(fqid=fqid, capauth_uri=capauth_uri, agent="lumina")


def _allow_decide(*_a, **_k):
    return _t.SimpleNamespace(allow=True, reason="granted")


def _deny_decide(*_a, **_k):
    return _t.SimpleNamespace(allow=False, reason="unknown subject: no enrolled device")


def _wire(mocker, *, chg, cab_dir: Path, identity=None, decide_fn=None):
    """Patch every seam ``deploy_dispatch`` resolves through: the CardStore-
    equivalent fold (ITILManager), the runner identity resolver, the capauth
    PDP, and the shared-root lookup. Mirrors ``agentrun_bridge``'s own
    ``_wire_common`` helper."""
    fake_mgr = _FakeITILManager(Path("/fake/home"), chg=chg, cab_dir=cab_dir)
    mocker.patch("skcapstone.mcp_tools._helpers._shared_root", return_value=Path("/fake/home"))
    mocker.patch("skcoord.itil.ITILManager", return_value=fake_mgr)
    mocker.patch("capauth.resolve_agent_identity", return_value=identity or _identity())
    mocker.patch("capauth.authz.decide", side_effect=decide_fn or _allow_decide)
    mocker.patch("skharness.autocode.change_deploy_bridge._gh_pr_head_sha", return_value=_HEAD_SHA)
    return fake_mgr


def _fake_gh_ok(*_a, **_k):
    return _t.SimpleNamespace(returncode=0, stdout="", stderr="")


# --------------------------------------------------------------------------- #
# 1. build_deploy_dispatcher: fail-closed prerequisites                       #
# --------------------------------------------------------------------------- #


def test_factory_returns_none_when_skai_deploy_bridge_unset(monkeypatch):
    monkeypatch.delenv("SKAI_DEPLOY_BRIDGE", raising=False)
    assert build_deploy_dispatcher() is None


def test_factory_returns_none_when_gh_is_unavailable(monkeypatch, mocker):
    monkeypatch.setenv("SKAI_DEPLOY_BRIDGE", "1")
    mocker.patch("skharness.autocode.change_deploy_bridge.shutil.which", return_value=None)
    assert build_deploy_dispatcher() is None


def test_factory_returns_none_when_capauth_import_fails(monkeypatch, mocker):
    import sys

    monkeypatch.setenv("SKAI_DEPLOY_BRIDGE", "1")
    mocker.patch(
        "skharness.autocode.change_deploy_bridge.shutil.which", return_value="/usr/bin/gh"
    )
    monkeypatch.setitem(sys.modules, "capauth", None)
    assert build_deploy_dispatcher() is None


def test_factory_returns_the_dispatcher_when_prerequisites_are_met(monkeypatch, mocker):
    monkeypatch.setenv("SKAI_DEPLOY_BRIDGE", "1")
    mocker.patch(
        "skharness.autocode.change_deploy_bridge.shutil.which", return_value="/usr/bin/gh"
    )
    fn = build_deploy_dispatcher()
    assert fn is deploy_dispatch


def test_missing_change_id_refuses():
    result = deploy_dispatch({})
    assert result["refused"] is True
    assert "change_id" in result["reason"]


# --------------------------------------------------------------------------- #
# 2. Re-fold preconditions (step 1) and idempotency                          #
# --------------------------------------------------------------------------- #


def test_refuses_when_not_scheduled(mocker, tmp_path):
    chg = _change(status="approved")
    _wire(mocker, chg=chg, cab_dir=tmp_path / "cab")
    result = deploy_dispatch({"change_id": chg.id})
    assert result["refused"] is True
    assert "not scheduled" in result["reason"]


@pytest.mark.parametrize("status", ["implementing", "deployed"])
def test_refuses_double_deploy_when_already_mid_or_done(mocker, tmp_path, status):
    chg = _change(status=status)
    fake_mgr = _wire(mocker, chg=chg, cab_dir=tmp_path / "cab")
    result = deploy_dispatch({"change_id": chg.id})
    assert result["refused"] is True
    assert "already" in result["reason"]
    assert fake_mgr.events == []  # never touched: pure refusal, no new event


def test_refuses_when_window_has_not_arrived(mocker, tmp_path):
    now = datetime.now(timezone.utc)
    window = {
        "window_start": (now + timedelta(hours=1)).isoformat(),
        "window_end": (now + timedelta(hours=2)).isoformat(),
        "asap": False,
        "deploy_mode": "confirm",
    }
    chg = _change(scheduled_window=window)
    _wire(mocker, chg=chg, cab_dir=tmp_path / "cab")
    result = deploy_dispatch({"change_id": chg.id})
    assert result["refused"] is True
    assert "window has not arrived" in result["reason"]


def test_refuses_when_no_prepared_pr(mocker, tmp_path):
    chg = _change(prepared_pr=None)
    _wire(mocker, chg=chg, cab_dir=tmp_path / "cab")
    result = deploy_dispatch({"change_id": chg.id})
    assert result["refused"] is True
    assert "no prepared_pr" in result["reason"]


def test_refuses_when_validation_has_not_passed(mocker, tmp_path):
    chg = _change(
        validation={
            "passed": False,
            "head_sha": _HEAD_SHA,
            "url": _PR_URL,
            "summary": "1/3 passed",
            "checks": [],
        }
    )
    _wire(mocker, chg=chg, cab_dir=tmp_path / "cab")
    result = deploy_dispatch({"change_id": chg.id})
    assert result["refused"] is True
    assert "no passing validation" in result["reason"]


# --------------------------------------------------------------------------- #
# 3. capauth decide + no-self-approval (step 2, design doc layer 3)          #
# --------------------------------------------------------------------------- #


def test_refuses_when_capauth_denies(mocker, tmp_path):
    chg = _change()
    _wire(mocker, chg=chg, cab_dir=tmp_path / "cab", decide_fn=_deny_decide)
    result = deploy_dispatch({"change_id": chg.id})
    assert result["refused"] is True
    assert "capauth denied" in result["reason"]


def test_refuses_self_approval_when_runner_equals_prepared_by(mocker, tmp_path):
    chg = _change(prepared_by="lumina@chef.skworld")
    _wire(
        mocker, chg=chg, cab_dir=tmp_path / "cab", identity=_identity(fqid="lumina@chef.skworld")
    )
    result = deploy_dispatch({"change_id": chg.id})
    assert result["refused"] is True
    assert "no-self-approval" in result["reason"]


# --------------------------------------------------------------------------- #
# 4. Arm check (step 3)                                                      #
# --------------------------------------------------------------------------- #


def test_refuses_deploy_mode_auto_as_out_of_scope(mocker, tmp_path):
    now = datetime.now(timezone.utc)
    window = {
        "window_start": (now - timedelta(hours=1)).isoformat(),
        "window_end": (now + timedelta(hours=1)).isoformat(),
        "asap": False,
        "deploy_mode": "auto",
    }
    chg = _change(scheduled_window=window)
    _wire(mocker, chg=chg, cab_dir=tmp_path / "cab")
    result = deploy_dispatch({"change_id": chg.id})
    assert result["refused"] is True
    assert "Phase 3b" in result["reason"]


def test_refuses_when_no_arm_file_exists(mocker, tmp_path):
    chg = _change(prepared_by="lumina")
    _wire(mocker, chg=chg, cab_dir=tmp_path / "cab", identity=_identity(fqid="opus@chef.skworld"))
    result = deploy_dispatch({"change_id": chg.id})
    assert result["refused"] is True
    assert "no human arm found" in result["reason"] or "no valid human arm" in result["reason"]


def test_refuses_when_arm_subject_is_the_drafter(mocker, tmp_path):
    cab_dir = tmp_path / "cab"
    chg = _change(prepared_by="lumina")
    _write_arm(cab_dir, chg.id, "lumina")  # drafter tries to arm their own change
    _wire(mocker, chg=chg, cab_dir=cab_dir, identity=_identity(fqid="opus@chef.skworld"))
    result = deploy_dispatch({"change_id": chg.id})
    assert result["refused"] is True
    assert "no valid human arm found" in result["reason"]
    assert "drafter cannot arm their own change" in result["reason"]


def test_refuses_when_arm_subject_fails_capauth(mocker, tmp_path):
    cab_dir = tmp_path / "cab"
    chg = _change(prepared_by="lumina")
    _write_arm(cab_dir, chg.id, "chef")

    def _decide(subject, capability, **kw):
        if subject == "chef":
            return _t.SimpleNamespace(allow=False, reason="insufficient enrollment mode")
        return _t.SimpleNamespace(allow=True, reason="granted")

    _wire(
        mocker,
        chg=chg,
        cab_dir=cab_dir,
        identity=_identity(fqid="opus@chef.skworld"),
        decide_fn=_decide,
    )
    result = deploy_dispatch({"change_id": chg.id})
    assert result["refused"] is True
    assert "no valid human arm found" in result["reason"]
    assert "capauth denied change.deploy" in result["reason"]


# --------------------------------------------------------------------------- #
# 5. Freshness (step 4)                                                      #
# --------------------------------------------------------------------------- #


def test_refuses_stale_validation_head_sha(mocker, tmp_path):
    cab_dir = tmp_path / "cab"
    chg = _change(
        prepared_by="lumina",
        validation={
            "passed": True,
            "head_sha": "old-sha",
            "url": _PR_URL,
            "summary": "3/3",
            "checks": [],
        },
    )
    _write_arm(cab_dir, chg.id, "chef")
    _wire(mocker, chg=chg, cab_dir=cab_dir, identity=_identity(fqid="opus@chef.skworld"))
    result = deploy_dispatch({"change_id": chg.id})
    assert result["refused"] is True
    assert "stale validation" in result["reason"]


def test_refuses_when_head_sha_cannot_be_resolved(mocker, tmp_path):
    cab_dir = tmp_path / "cab"
    chg = _change(prepared_by="lumina")
    _write_arm(cab_dir, chg.id, "chef")
    _wire(mocker, chg=chg, cab_dir=cab_dir, identity=_identity(fqid="opus@chef.skworld"))
    mocker.patch("skharness.autocode.change_deploy_bridge._gh_pr_head_sha", return_value=None)
    result = deploy_dispatch({"change_id": chg.id})
    assert result["refused"] is True
    assert "could not resolve current head SHA" in result["reason"]


# --------------------------------------------------------------------------- #
# 6. Fully-green path merges (gh entirely mocked)                            #
# --------------------------------------------------------------------------- #


def test_all_green_path_merges_and_records_implementing_then_deployed(mocker, tmp_path):
    cab_dir = tmp_path / "cab"
    chg = _change(prepared_by="lumina")
    _write_arm(cab_dir, chg.id, "chef")
    fake_mgr = _wire(
        mocker, chg=chg, cab_dir=cab_dir, identity=_identity(fqid="opus@chef.skworld")
    )
    gh_calls: list[list[str]] = []

    def _fake_gh(argv, timeout):
        gh_calls.append(list(argv))
        return _t.SimpleNamespace(returncode=0, stdout="", stderr="")

    mocker.patch("skharness.autocode.change_deploy_bridge._gh", side_effect=_fake_gh)

    result = deploy_dispatch({"change_id": chg.id})

    assert result["refused"] is False
    assert result["status"] == "deployed"
    assert result["pr"] == _PR_URL
    kinds = [(e[2], e[3].get("to")) for e in fake_mgr.events]
    assert ("status", "implementing") in kinds
    assert ("status", "deployed") in kinds
    # implementing strictly before deployed
    to_values = [e[3].get("to") for e in fake_mgr.events if e[2] == "status"]
    assert to_values.index("implementing") < to_values.index("deployed")
    assert any("pr" in c and "ready" in c for c in gh_calls)
    assert any("merge" in c and "--squash" in c for c in gh_calls)
    assert all(c[0] == "gh" for c in gh_calls)  # every call is the gh CLI, nothing else


def test_gh_pr_ready_failure_appends_failed_never_partial(mocker, tmp_path):
    cab_dir = tmp_path / "cab"
    chg = _change(prepared_by="lumina")
    _write_arm(cab_dir, chg.id, "chef")
    fake_mgr = _wire(
        mocker, chg=chg, cab_dir=cab_dir, identity=_identity(fqid="opus@chef.skworld")
    )

    def _fake_gh(argv, timeout):
        if "ready" in argv:
            return _t.SimpleNamespace(returncode=1, stdout="", stderr="not authorized")
        raise AssertionError("merge must never be attempted after ready fails")

    mocker.patch("skharness.autocode.change_deploy_bridge._gh", side_effect=_fake_gh)

    result = deploy_dispatch({"change_id": chg.id})

    assert result["refused"] is True
    assert "gh pr ready failed" in result["reason"]
    to_values = [e[3].get("to") for e in fake_mgr.events if e[2] == "status"]
    assert to_values == ["implementing", "failed"]  # terminal, never left mid-flight


def test_gh_pr_merge_failure_appends_failed_never_partial(mocker, tmp_path):
    cab_dir = tmp_path / "cab"
    chg = _change(prepared_by="lumina")
    _write_arm(cab_dir, chg.id, "chef")
    fake_mgr = _wire(
        mocker, chg=chg, cab_dir=cab_dir, identity=_identity(fqid="opus@chef.skworld")
    )

    def _fake_gh(argv, timeout):
        if "ready" in argv:
            return _t.SimpleNamespace(returncode=0, stdout="", stderr="")
        if "merge" in argv:
            return _t.SimpleNamespace(returncode=1, stdout="", stderr="merge conflict")
        raise AssertionError(f"unexpected gh call: {argv}")

    mocker.patch("skharness.autocode.change_deploy_bridge._gh", side_effect=_fake_gh)

    result = deploy_dispatch({"change_id": chg.id})

    assert result["refused"] is True
    assert "gh pr merge failed" in result["reason"]
    to_values = [e[3].get("to") for e in fake_mgr.events if e[2] == "status"]
    assert to_values == ["implementing", "failed"]


def test_deploy_cmd_failure_after_merge_appends_failed(mocker, tmp_path):
    """A configured per-repo deploy_cmd that fails is a real deploy failure,
    even though the merge itself succeeded -- never reported as deployed."""
    from skharness.autocode.config import Config
    from skharness.autocode.types import RepoSpec

    cab_dir = tmp_path / "cab"
    chg = _change(prepared_by="lumina")
    _write_arm(cab_dir, chg.id, "chef")
    fake_mgr = _wire(
        mocker, chg=chg, cab_dir=cab_dir, identity=_identity(fqid="opus@chef.skworld")
    )
    mocker.patch("skharness.autocode.change_deploy_bridge._gh", side_effect=_fake_gh_ok)

    repo = RepoSpec(
        name="skrender",
        path="/repos/skrender",
        base_branch="main",
        integration_branch="develop",
        test_cmd="pytest",
        ci="none",
        deploy_cmd="/bin/false",
    )
    cfg = Config(repo_map={"skrender": repo})
    mocker.patch("skharness.autocode.change_deploy_bridge.Config.load", return_value=cfg)

    def _fake_subproc_run(cmd, **kw):
        assert cmd == "/bin/false"
        return _t.SimpleNamespace(returncode=1, stdout="", stderr="deploy script exploded")

    mocker.patch(
        "skharness.autocode.change_deploy_bridge.subprocess.run", side_effect=_fake_subproc_run
    )

    result = deploy_dispatch({"change_id": chg.id})

    assert result["refused"] is True
    assert "deploy_cmd failed" in result["reason"]
    to_values = [e[3].get("to") for e in fake_mgr.events if e[2] == "status"]
    assert to_values == ["implementing", "failed"]


# --------------------------------------------------------------------------- #
# 7. Direct unit coverage of small helpers                                   #
# --------------------------------------------------------------------------- #


def test_repo_name_from_pr_url():
    from skharness.autocode.change_deploy_bridge import _repo_name_from_pr_url

    assert _repo_name_from_pr_url(_PR_URL) == "skrender"
    assert _repo_name_from_pr_url("not-a-url") is None


def test_within_window():
    from skharness.autocode.change_deploy_bridge import _within_window

    now = datetime.now(timezone.utc)
    start = (now - timedelta(hours=1)).isoformat()
    end = (now + timedelta(hours=1)).isoformat()
    assert _within_window(now, start, end) is True
    assert _within_window(now, None, end) is False
    assert _within_window(now, "not-a-date", end) is False
