#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
default_skos="$HOME/clawd/skos"
if [[ ! -f "$default_skos/pyproject.toml" ]]; then
    default_skos="$(dirname "$repo_root")/skos"
fi
skos_root="${SKOS_REPO:-$default_skos}"
python_bin="${SKOPS_BOOTSTRAP_PYTHON:-$HOME/.pyenv/versions/3.12.3/bin/python}"
venv="${SKOPS_VENV:-$HOME/.venvs/skops}"

test -x "$python_bin"
test -f "$repo_root/pyproject.toml"
test -f "$skos_root/pyproject.toml"

"$python_bin" -m venv "$venv"
"$venv/bin/python" -m pip install --upgrade pip
"$venv/bin/python" -m pip install -e "$repo_root[service]" -e "$skos_root"
"$venv/bin/python" -m pip check
"$venv/bin/skcode-hostd" --help >/dev/null
"$venv/bin/skos" --help >/dev/null

printf 'skops runtime ready: %s\n' "$venv"
