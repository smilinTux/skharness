"""Regression contracts for the owned SKOps runtime installer and tagged SOP path."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "systemd" / "install-skops-runtime.sh"
SOP = ROOT / "SOP.md"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def test_installer_runs_owned_editable_sequence_without_disabling_build_isolation(
    tmp_path: Path,
) -> None:
    log = tmp_path / "calls.log"
    bootstrap = tmp_path / "bootstrap-python"
    venv = tmp_path / "skops-venv"
    skos = tmp_path / "skos"
    (venv / "bin").mkdir(parents=True)
    skos.mkdir()
    (skos / "pyproject.toml").write_text("[project]\nname='skos-fixture'\n", encoding="utf-8")

    _write_executable(
        bootstrap,
        '#!/bin/sh\nprintf "bootstrap|%s\\n" "$*" >> "$INSTALL_LOG"\n',
    )
    for name in ("python", "skcode-hostd", "skos"):
        _write_executable(
            venv / "bin" / name,
            f'#!/bin/sh\nprintf "{name}|%s\\n" "$*" >> "$INSTALL_LOG"\n',
        )

    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(tmp_path / "home"),
            "INSTALL_LOG": str(log),
            "SKOPS_BOOTSTRAP_PYTHON": str(bootstrap),
            "SKOPS_VENV": str(venv),
            "SKOS_REPO": str(skos),
        }
    )
    result = subprocess.run(
        [str(INSTALLER)],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.stdout == f"skops runtime ready: {venv}\n"
    assert log.read_text(encoding="utf-8").splitlines() == [
        f"bootstrap|-m venv {venv}",
        "python|-m pip install --upgrade pip",
        f"python|-m pip install -e {ROOT}[service] -e {skos}",
        "python|-m pip check",
        "skcode-hostd|--help",
        "skos|--help",
    ]


def test_installer_and_tagged_sop_keep_one_controlled_install_contract() -> None:
    installer = INSTALLER.read_text(encoding="utf-8")
    sop = SOP.read_text(encoding="utf-8")
    start = sop.index("For an editable tagged deployment")
    end = sop.index("\nThe two current qualification targets", start)
    tagged = sop[start:end]
    commands_start = tagged.index("```bash")
    commands_end = tagged.index("```", commands_start + len("```bash"))
    tagged_commands = tagged[commands_start:commands_end]

    assert 'pip install -e "$repo_root[service]" -e "$skos_root"' in installer
    assert '"$venv/bin/python" -m pip check' in installer
    assert '"$venv/bin/skcode-hostd" --help' in installer
    assert '"$venv/bin/skos" --help' in installer
    assert "--no-build-isolation" not in installer
    assert "--no-deps" not in installer

    assert 'SKOPS_VENV="$skharness_venv"' in tagged
    assert '"$skharness_repo/systemd/install-skops-runtime.sh"' in tagged
    assert '"$skharness_venv_python" -m pip check' in tagged
    assert "skharness_tag=v0.3.39" in tagged
    assert 'rev-parse "${skharness_tag}^{commit}"' in tagged
    assert 'rev-parse HEAD)" = "$skharness_tag_commit"' in tagged
    assert 'version("skharness") == expected' in tagged
    assert "module_path.is_relative_to(repo_src)" in tagged
    assert "--no-build-isolation" not in tagged_commands
    assert "--no-deps" not in tagged_commands
    assert 'pip install -e "$skharness_repo"' not in tagged_commands


def test_installer_is_valid_bash() -> None:
    subprocess.run(["bash", "-n", str(INSTALLER)], check=True)
