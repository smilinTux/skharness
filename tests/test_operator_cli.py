"""Tests for the skcode-hostd operator facet CLI (spec 4.2, card R2.14).

Covers explain (shape matches the contract), observe (healthy + each condition
firing via an injected probe + default probe fails safe), and act (restart-hostd
calls the runner, archive-stale-session calls the archive path, kill/pause return
the escalate/not-enabled message, unknown action refuses cleanly). The runner,
harness, and probe are all injected, so no test touches real systemd, tmux, or
the network.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from skharness import operator_cli as op
from skharness.manifest import skcode_module_manifest

# --- test doubles ------------------------------------------------------------


@dataclass
class _FakeCompleted:
    returncode: int = 0


class _RecordingRunner:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> _FakeCompleted:
        self.calls.append(list(argv))
        return _FakeCompleted(returncode=self.returncode)


class _RecordingHarness:
    """A session-plane double whose async archive records the sid."""

    def __init__(self) -> None:
        self.archived: list[str] = []

    async def archive(self, sid: str) -> None:
        self.archived.append(sid)


class _UnimplementedHarness:
    """A harness whose archive body is still deferred (the real P0 state)."""

    async def archive(self, sid: str) -> None:
        raise NotImplementedError("session plane P1")


# --- explain -----------------------------------------------------------------


def test_explain_shape_matches_the_contract():
    c = op.operator_explain()
    assert c["kinds"] == ["hostd", "session", "registry", "dispatch"]
    assert c["conditions"] == [
        "HostdReady",
        "SessionsHealthy",
        "RegistryConsistent",
        "AuthEnforced",
    ]
    names = [a["name"] for a in c["actions"]]
    assert names == [
        "restart-hostd",
        "archive-stale-session",
        "kill-runaway-session",
        "pause-dispatch",
    ]
    for a in c["actions"]:
        assert set(a) == {
            "name",
            "standard",
            "reversible",
            "blast_radius",
            "runbook",
            "kedb_refs",
        }
        assert a["blast_radius"] in {"low", "normal", "high"}


def test_explain_action_metadata_semantics():
    by_name = {a["name"]: a for a in op.operator_explain()["actions"]}
    # the two reversible standard actions
    assert by_name["restart-hostd"]["standard"] is True
    assert by_name["restart-hostd"]["reversible"] is True
    assert by_name["archive-stale-session"]["standard"] is True
    assert by_name["archive-stale-session"]["reversible"] is True
    # kill escalates: NOT standard, irreversible (policy forces MAJOR)
    assert by_name["kill-runaway-session"]["standard"] is False
    assert by_name["kill-runaway-session"]["reversible"] is False
    # pause-dispatch: not standard, but reversible (a flag flip)
    assert by_name["pause-dispatch"]["standard"] is False
    assert by_name["pause-dispatch"]["reversible"] is True


def test_explain_standard_actions_match_the_manifest():
    # The manifest's proposedStandardActions must equal the CLI's STANDARD actions.
    manifest = skcode_module_manifest("http://host:9394/")
    proposed = manifest["operator"]["proposedStandardActions"]
    standard = [a["name"] for a in op.operator_explain()["actions"] if a["standard"]]
    assert standard == proposed == ["restart-hostd", "archive-stale-session"]


# --- observe -----------------------------------------------------------------


def _statuses(observed: dict) -> dict:
    return {c["type"]: c["status"] for c in observed["conditions"]}


def test_observe_healthy_when_probe_reports_all_true():
    healthy = {
        "hostd_ready": True,
        "sessions_healthy": True,
        "registry_consistent": True,
        "auth_enforced": True,
    }
    st = _statuses(op.operator_observe(probe=lambda: healthy))
    assert st == {
        "HostdReady": "True",
        "SessionsHealthy": "True",
        "RegistryConsistent": "True",
        "AuthEnforced": "True",
    }
    # every condition carries an object label
    objs = {c["type"]: c["object"] for c in op.operator_observe(probe=lambda: healthy)["conditions"]}
    assert objs == {
        "HostdReady": "skcode-hostd",
        "SessionsHealthy": "sessions",
        "RegistryConsistent": "registry",
        "AuthEnforced": "verifier",
    }


@pytest.mark.parametrize(
    "key,condition",
    [
        ("hostd_ready", "HostdReady"),
        ("sessions_healthy", "SessionsHealthy"),
        ("registry_consistent", "RegistryConsistent"),
        ("auth_enforced", "AuthEnforced"),
    ],
)
def test_observe_each_condition_fires_independently(key, condition):
    state = {
        "hostd_ready": True,
        "sessions_healthy": True,
        "registry_consistent": True,
        "auth_enforced": True,
    }
    state[key] = False
    st = _statuses(op.operator_observe(probe=lambda: state))
    assert st[condition] == "False"
    for other in ("HostdReady", "SessionsHealthy", "RegistryConsistent", "AuthEnforced"):
        if other != condition:
            assert st[other] == "True"


def test_observe_missing_keys_default_healthy():
    # A probe that omits keys must fail safe (report healthy).
    st = _statuses(op.operator_observe(probe=dict))
    assert set(st.values()) == {"True"}


def test_default_probe_fails_safe_when_hostd_unreachable(monkeypatch):
    # Point the probe at a closed port: connection refused -> all healthy.
    monkeypatch.setenv("SKCODE_HOSTD_HEALTH", "http://127.0.0.1:1/api/v1/hosts/self")
    state = op._default_probe()
    assert state == {
        "hostd_ready": True,
        "sessions_healthy": True,
        "registry_consistent": True,
        "auth_enforced": True,
    }


# --- pure probe logic --------------------------------------------------------


def test_sessions_healthy_running_stale_fires():
    stale = [{"state": "running", "last_event_age_s": op._SESSION_STALE_S + 1}]
    assert op._sessions_healthy(stale) is False


def test_sessions_healthy_non_running_and_unknown_age_are_safe():
    rows = [
        {"state": "idle", "last_event_age_s": 10**9},  # not running -> ignored
        {"state": "running", "last_event_age_s": None},  # unknown age -> safe
        {"state": "running", "last_event_age_s": 5},  # fresh
    ]
    assert op._sessions_healthy(rows) is True


def test_registry_consistent_orphan_fires():
    # A registered id with no live backing is an orphan.
    assert op._registry_consistent(["a", "b"], ["a"]) is False
    assert op._registry_consistent(["a"], ["a", "b"]) is True
    assert op._registry_consistent([], []) is True


# --- act: restart-hostd ------------------------------------------------------


def test_act_restart_hostd_calls_runner():
    runner = _RecordingRunner(returncode=0)
    result = op.operator_act("restart-hostd", runner=runner)
    assert runner.calls == [["systemctl", "--user", "restart", "skcode-hostd.service"]]
    assert result["performed"] is True
    assert result["action"] == "restart-hostd"
    assert result["unit"] == "skcode-hostd.service"


def test_act_restart_hostd_reports_failure():
    runner = _RecordingRunner(returncode=1)
    result = op.operator_act("restart-hostd", runner=runner)
    assert result["performed"] is False
    assert result["returncode"] == 1


# --- act: archive-stale-session ----------------------------------------------


def test_act_archive_calls_the_archive_path():
    harness = _RecordingHarness()
    result = op.operator_act("archive-stale-session", session="sess-1", harness=harness)
    assert harness.archived == ["sess-1"]
    assert result == {"performed": True, "action": "archive-stale-session", "session": "sess-1"}


def test_act_archive_requires_a_session():
    with pytest.raises(ValueError, match="requires --session"):
        op.operator_act("archive-stale-session", harness=_RecordingHarness())


def test_act_archive_not_implemented_is_a_clear_stub():
    result = op.operator_act(
        "archive-stale-session", session="sess-2", harness=_UnimplementedHarness()
    )
    assert result["performed"] is False
    assert result["session"] == "sess-2"
    assert "P1" in result["reason"]


def test_act_archive_actuates_the_real_claude_code_harness(tmp_path):
    """End-to-end: the real ClaudeCodeHarness archive body now actuates through
    operator_act (P1 closes the 'cannot actuate' gap), stopping + persisting the
    session over a fake tmux runner with no real tmux."""
    from skharness.harnesses.claude_code import ClaudeCodeHarness

    root = tmp_path / "agents"
    verbs: list[str] = []

    def runner(argv):
        if "list-windows" in argv:
            return "monitor\t1\nlumina-abc12345\t1700000100\n"
        if "capture-pane" in argv:
            verbs.append("capture")
            return "session output\n"
        if "kill-window" in argv:
            verbs.append("kill")
            return ""
        raise AssertionError(f"unexpected tmux call: {argv}")

    harness = ClaudeCodeHarness(runner=runner, sessions_root=root, host=".158")
    result = op.operator_act(
        "archive-stale-session", session="lumina-abc12345", harness=harness
    )
    assert result == {
        "performed": True,
        "action": "archive-stale-session",
        "session": "lumina-abc12345",
    }
    # stop + persist actually happened, persist before stop
    assert verbs == ["capture", "kill"]
    assert (root / "lumina" / "sessions" / "lumina-abc12345.json").exists()


# --- act: escalating / not-enabled actions -----------------------------------


def test_act_kill_runaway_escalates_without_acting():
    result = op.operator_act("kill-runaway-session", session="sess-3")
    assert result["performed"] is False
    assert result["escalated"] is True
    assert "MAJOR" in result["reason"]


def test_act_pause_dispatch_reports_not_enabled():
    result = op.operator_act("pause-dispatch")
    assert result["performed"] is False
    assert result["enabled"] is False
    assert "dispatch" in result["reason"].lower()


def test_act_unknown_action_refuses_cleanly():
    with pytest.raises(ValueError, match="unknown action"):
        op.operator_act("frobnicate")


# --- CLI entry (argv -> exit code + JSON) ------------------------------------


def test_cli_explain_prints_json_and_exits_zero(capsys):
    rc = op.main(["explain"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["conditions"] == op.CONDITIONS


def test_cli_observe_prints_json_and_exits_zero(capsys, monkeypatch):
    monkeypatch.setattr(
        op,
        "_default_probe",
        lambda: {
            "hostd_ready": True,
            "sessions_healthy": True,
            "registry_consistent": True,
            "auth_enforced": True,
        },
    )
    rc = op.main(["observe"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert {c["type"] for c in payload["conditions"]} == set(op.CONDITIONS)


def test_cli_act_kill_exits_nonzero_but_reports(capsys):
    rc = op.main(["act", "kill-runaway-session", "--session", "s"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["escalated"] is True


def test_cli_act_unknown_action_exits_two(capsys):
    rc = op.main(["act", "bogus"])
    assert rc == 2
    assert "unknown action" in capsys.readouterr().err


def test_cli_act_archive_exit_code_maps_to_performed(capsys):
    # A recording harness makes archive perform -> exit 0 (what the Atlas adapter
    # keys off: returncode == 0 means archived).
    harness = _RecordingHarness()

    def _fake_default_harness():
        return harness

    import skharness.operator_cli as mod

    orig = mod._default_harness
    mod._default_harness = _fake_default_harness
    try:
        rc = mod.main(["act", "archive-stale-session", "--session", "sess-9"])
    finally:
        mod._default_harness = orig
    assert rc == 0
    assert harness.archived == ["sess-9"]


# --- wiring: skcode-hostd routes the operator subcommand ---------------------


def test_serve_main_routes_operator_subcommand(capsys):
    from skharness.serve import main as serve_main

    rc = serve_main(["operator", "explain"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kinds"] == op.KINDS
