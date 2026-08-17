"""resolve_identity(): who/where a run record came from (card A2.1).

Every session on this box wrote to the coordination board as ``lumina``, so
concurrent sessions were indistinguishable after the fact. These tests pin the
three things that has to stop happening: the precedence is the documented one,
the provenance of the name survives into the result, and one process has
exactly one session id, shared with the session descriptor.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from skharness.autocode.identity import (
    AGENT_ENV_VARS,
    AGENT_VAR_DEFAULT,
    DEFAULT_AGENT,
    SESSION_ID_ENV_VAR,
    SESSION_ID_VAR_MINTED,
    Identity,
    reset_identity_cache,
    resolve_identity,
)
from skharness.autocode.sessions import AutocodeSessionRegistry


@pytest.fixture(autouse=True)
def _clean_identity_env(monkeypatch):
    """No ambient agent/session vars leak in, and no memo leaks out.

    The suite itself often runs under a real ``SKAGENT``; without this the
    precedence tests would assert against the developer's shell rather than
    against the code.
    """
    for var in (*AGENT_ENV_VARS, SESSION_ID_ENV_VAR):
        monkeypatch.delenv(var, raising=False)
    reset_identity_cache()
    yield
    reset_identity_cache()


# --------------------------------------------------------------------------
# Agent precedence: SKAGENT > SKCAPSTONE_AGENT > SKMEMORY_AGENT > "lumina"
# --------------------------------------------------------------------------

def test_precedence_order_is_the_documented_one():
    """The order itself, read off the module rather than assumed."""
    assert AGENT_ENV_VARS == ("SKAGENT", "SKCAPSTONE_AGENT", "SKMEMORY_AGENT")


def test_no_var_set_falls_back_to_lumina():
    ident = resolve_identity()
    assert ident.agent == DEFAULT_AGENT == "lumina"
    assert ident.agent_var == AGENT_VAR_DEFAULT


@pytest.mark.parametrize("var,expected", [
    ("SKAGENT", "skagent-value"),
    ("SKCAPSTONE_AGENT", "skcapstone-value"),
    ("SKMEMORY_AGENT", "skmemory-value"),
])
def test_each_var_alone_is_honoured(monkeypatch, var, expected):
    monkeypatch.setenv(var, expected)
    ident = resolve_identity()
    assert ident.agent == expected
    assert ident.agent_var == var


def test_all_three_set_and_disagreeing_picks_skagent(monkeypatch):
    """The production case, not a hypothetical.

    On .41 a single ``skcomms.service`` unit sets SKAGENT=jarvis and
    SKMEMORY_AGENT=lumina at once, so the variables really do disagree while
    the process runs. SKAGENT wins, and the result says so.
    """
    monkeypatch.setenv("SKAGENT", "jarvis")
    monkeypatch.setenv("SKCAPSTONE_AGENT", "opus")
    monkeypatch.setenv("SKMEMORY_AGENT", "lumina")
    ident = resolve_identity()
    assert ident.agent == "jarvis"
    assert ident.agent_var == "SKAGENT"


def test_skcapstone_agent_wins_over_skmemory_agent(monkeypatch):
    monkeypatch.setenv("SKCAPSTONE_AGENT", "opus")
    monkeypatch.setenv("SKMEMORY_AGENT", "lumina")
    ident = resolve_identity()
    assert ident.agent == "opus"
    assert ident.agent_var == "SKCAPSTONE_AGENT"


def test_empty_higher_precedence_var_falls_through(monkeypatch):
    """``Environment=SKAGENT=`` in a unit must not pin the agent to "".

    A bare or whitespace-only assignment is a systemd/shell artefact, not an
    operator choosing the empty agent, so it falls through to the next name.
    """
    monkeypatch.setenv("SKAGENT", "   ")
    monkeypatch.setenv("SKMEMORY_AGENT", "lumina")
    ident = resolve_identity()
    assert ident.agent == "lumina"
    assert ident.agent_var == "SKMEMORY_AGENT"


def test_agent_value_is_stripped(monkeypatch):
    monkeypatch.setenv("SKAGENT", "  jarvis \n")
    assert resolve_identity().agent == "jarvis"


def test_the_variable_that_was_read_is_machine_readable(monkeypatch):
    """The provenance must survive serialization, not live only in a log line.

    Two of the three names are discarded by the precedence rule. Which one won
    cannot be recovered later from unit names either (skchat-daemon runs as
    opus while skchat-daemon-jarvis runs as jarvis), so if agent_var is not
    stored the record is ambiguous forever.
    """
    monkeypatch.setenv("SKMEMORY_AGENT", "lumina")
    payload = json.loads(json.dumps(resolve_identity().to_dict()))
    assert payload["agent"] == "lumina"
    assert payload["agent_var"] == "SKMEMORY_AGENT"
    assert set(payload) == {
        "agent", "session_id", "node", "agent_var", "session_id_var"}


# --------------------------------------------------------------------------
# Shape and stability
# --------------------------------------------------------------------------

def test_returns_a_stable_triple_within_a_process(monkeypatch):
    monkeypatch.setenv("SKAGENT", "jarvis")
    first = resolve_identity()
    second = resolve_identity()
    assert first.triple == second.triple
    assert first is second


def test_node_is_the_hostname():
    assert resolve_identity().node == socket.gethostname()


def test_triple_is_agent_session_node():
    ident = resolve_identity()
    assert ident.triple == (ident.agent, ident.session_id, ident.node)
    assert isinstance(ident, Identity)
    assert tuple(ident)[:3] == ident.triple


def test_memoized_result_ignores_a_later_env_mutation(monkeypatch):
    """A library that sets SKAGENT mid-run must not split one run in two."""
    monkeypatch.setenv("SKAGENT", "jarvis")
    first = resolve_identity()
    monkeypatch.setenv("SKAGENT", "opus")
    assert resolve_identity().agent == "jarvis"
    assert resolve_identity().session_id == first.session_id


def test_session_id_is_uuid4_hex_when_minted():
    ident = resolve_identity()
    assert ident.session_id_var == SESSION_ID_VAR_MINTED
    assert len(ident.session_id) == 32
    int(ident.session_id, 16)          # hex, raises if not


def test_sk_session_id_is_honoured_so_a_resumed_run_keeps_its_id(monkeypatch):
    monkeypatch.setenv(SESSION_ID_ENV_VAR, "resumed-session-1")
    ident = resolve_identity()
    assert ident.session_id == "resumed-session-1"
    assert ident.session_id_var == SESSION_ID_ENV_VAR


def test_blank_sk_session_id_is_treated_as_unset(monkeypatch):
    monkeypatch.setenv(SESSION_ID_ENV_VAR, "  ")
    ident = resolve_identity()
    assert ident.session_id_var == SESSION_ID_VAR_MINTED
    assert ident.session_id


def _resolve_in_a_child_process() -> dict:
    """Run resolve_identity() in a genuinely separate interpreter."""
    src = Path(__file__).resolve().parents[1] / "src"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(src)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    for var in (*AGENT_ENV_VARS, SESSION_ID_ENV_VAR):
        env.pop(var, None)
    code = (
        "import json;"
        "from skharness.autocode.identity import resolve_identity;"
        "print(json.dumps(resolve_identity().to_dict()))"
    )
    out = subprocess.run([sys.executable, "-c", code], env=env,
                         capture_output=True, text=True, timeout=120, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_two_processes_get_different_session_ids():
    """The whole point: two concurrent sessions must be distinguishable."""
    a = _resolve_in_a_child_process()
    b = _resolve_in_a_child_process()
    assert a["session_id"] != b["session_id"]
    assert a["node"] == b["node"] == socket.gethostname()
    assert a["agent"] == b["agent"] == DEFAULT_AGENT


# --------------------------------------------------------------------------
# The registry and the resolver must agree on the sid
# --------------------------------------------------------------------------

def test_registry_descriptor_sid_equals_the_resolved_session_id(tmp_path):
    """Proven by reading the persisted descriptor, not by inspection.

    Two ids for one session would rebuild exactly the ambiguity this card
    removes, so the assertion is on the bytes on disk.
    """
    reg = AutocodeSessionRegistry(root=tmp_path)
    desc = reg.register(repo="skharness")
    ident = resolve_identity()
    assert desc.sid == ident.session_id
    on_disk = json.loads(
        (tmp_path / ident.session_id / "session.json").read_text(encoding="utf-8"))
    assert on_disk["sid"] == ident.session_id
    assert reg.get(ident.session_id).sid == ident.session_id


def test_registry_defaults_host_to_the_resolved_node(tmp_path):
    desc = AutocodeSessionRegistry(root=tmp_path).register()
    assert desc.host == resolve_identity().node


def test_registry_still_honours_an_explicit_sid_and_host(tmp_path):
    """Existing callers pass their own sid; the default must not override it."""
    reg = AutocodeSessionRegistry(root=tmp_path)
    desc = reg.register(sid="autocode-r1-t1", host=".158", repo="skharness")
    assert desc.sid == "autocode-r1-t1"
    assert desc.host == ".158"
    assert desc.sid != resolve_identity().session_id
