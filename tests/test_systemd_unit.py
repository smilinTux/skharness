"""Light guardrails on the shipped skcode-hostd systemd unit.

These are tooling-only checks (no daemon import): they assert the unit keeps the
two safety defaults the deploy card requires. If someone edits the unit to bind
a wildcard, drop the port, or bake in the real verifier, these fail.
"""

from __future__ import annotations

import configparser
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SYSTEMD = Path(__file__).resolve().parent.parent / "systemd"
_UNIT = _SYSTEMD / "skcode-hostd.service"
_ENV_EXAMPLE = _SYSTEMD / "skcode-hostd.env.example"


def _parse_unit() -> configparser.ConfigParser:
    # interpolation=None so systemd specifiers like %h do not trip configparser.
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.read(_UNIT, encoding="utf-8")
    return cp


def test_unit_is_valid_ini_with_expected_sections() -> None:
    cp = _parse_unit()
    for section in ("Unit", "Service", "Install"):
        assert cp.has_section(section), f"missing [{section}]"


def test_execstart_uses_port_9394_and_module_invocation() -> None:
    exec_start = _parse_unit()["Service"]["ExecStart"]
    assert exec_start.startswith("%h/.venvs/skops/bin/python ")
    assert "-m skharness" in exec_start
    assert "--port 9394" in exec_start
    assert "--host-id" in exec_start


def test_bind_host_is_a_placeholder_not_a_wildcard() -> None:
    exec_start = _parse_unit()["Service"]["ExecStart"]
    # Host comes from the env file placeholder, never a hardcoded wildcard/public.
    assert "${SKCODE_HOSTD_TAILSCALE_IP}" in exec_start
    assert "0.0.0.0" not in exec_start
    assert "--host ::" not in exec_start


def test_shipped_unit_does_not_set_real_verifier() -> None:
    # Deny-all is the default: the real verifier must not be turned on in the
    # shipped unit. Comments explaining it are fine; an active Environment= line
    # setting it is not.
    for line in _UNIT.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert "SKCODE_REAL_VERIFIER" not in stripped, (
            "shipped unit must not set SKCODE_REAL_VERIFIER (deny-all default)"
        )


def test_env_example_keeps_real_verifier_commented_out() -> None:
    for line in _ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if "SKCODE_REAL_VERIFIER" in stripped:
            assert stripped.startswith("#"), (
                "SKCODE_REAL_VERIFIER must ship commented out in the env example"
            )


def test_unit_carries_standard_hardening() -> None:
    service = _parse_unit()["Service"]
    assert service["NoNewPrivileges"] == "true"
    assert service["ProtectSystem"] == "strict"
    assert service["ProtectHome"] == "read-only"
    assert service["ReadWritePaths"].split() == ["%h/.skharness", "%h/.skcapstone"]
    assert service["Restart"] == "on-failure"


@pytest.mark.skipif(
    os.environ.get("SKHARNESS_RUN_SYSTEMD_SANDBOX_TEST") != "1",
    reason="requires a user systemd manager with transient-unit support",
)
def test_service_start_and_secure_publication_under_exact_filesystem_sandbox() -> None:
    state = Path.home() / ".skcapstone" / f"systemd-sandbox-test-{os.getpid()}"
    code = (
        "import os, shutil, uvicorn; from pathlib import Path; "
        "from skharness.securefs import SecureDir; "
        f"state=Path({str(state)!r}); root=SecureDir.anchor(state); "
        "fd=root.write_exclusive('started', b'ok'); os.close(fd); root.close(); "
        "uvicorn.run=lambda app,host,port: None; from skharness import serve; "
        "serve._serve(['--host', '127.0.0.2', '--host-id', 'hermetic']); "
        "assert (state/'started').read_bytes() == b'ok'; shutil.rmtree(state)"
    )
    command = [
        "systemd-run",
        "--user",
        "--wait",
        "--collect",
        "--quiet",
        f"--unit=skharness-securedir-{os.getpid()}",
        "--property=Type=oneshot",
        "--property=NoNewPrivileges=true",
        "--property=ProtectSystem=strict",
        "--property=ProtectHome=read-only",
        f"--property=ReadWritePaths={Path.home() / '.skharness'} {Path.home() / '.skcapstone'}",
        "--property=PrivateTmp=true",
        f"--setenv=PYTHONPATH={_SYSTEMD.parent / 'src'}",
        f"--setenv=SKCODE_STATE_DIR={state}",
        "--setenv=SKCODE_FORCE_DENY_ALL=1",
        sys.executable,
        "-c",
        code,
    ]
    completed = subprocess.run(command, text=True, capture_output=True, timeout=30)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert not state.exists()
