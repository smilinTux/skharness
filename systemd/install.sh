#!/usr/bin/env bash
# install.sh: install the skcode-hostd systemd user unit from this repo.
#
# Copies systemd/skcode-hostd.service into ~/.config/systemd/user and the env
# template into ~/.config/skcode-hostd/skcode-hostd.env.example, then runs
# `systemctl --user daemon-reload`. Idempotent: it writes only when content
# differs, NEVER clobbers a live-tuned skcode-hostd.env, and NEVER auto-enables
# or auto-starts the service. Enabling/starting is left to the operator: the
# script PRINTS the exact next-step commands instead of running them.
#
# WHY it does not start: skcode-hostd exposes a remote-control surface. It ships
# tailnet-only (serve.py refuses a wildcard bind) and deny-all (SKCODE_REAL_VERIFIER
# unset), but bringing it up is a deliberate operator decision, not an install
# side effect.
#
# Usage:
#   ./systemd/install.sh                 install + daemon-reload (no enable/start)
#   ./systemd/install.sh --dry-run       print planned actions, touch nothing
#   ./systemd/install.sh --diff          show drift: repo vs installed, touch nothing
#   ./systemd/install.sh --enable        also `systemctl --user enable` (no start)
#   ./systemd/install.sh --enable --start  also enable + start the unit
#
# Even with --enable/--start the deny-all + tailnet-only defaults are unchanged:
# those flags only toggle systemd enablement, never the daemon's security posture.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="${HOME}/.config/systemd/user"
CFG_DIR="${HOME}/.config/skcode-hostd"

UNIT="skcode-hostd.service"
ENV_EXAMPLE="skcode-hostd.env.example"
ENV_LIVE="skcode-hostd.env"

MODE="install"   # install | dry-run | diff
ENABLE=0
START=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) MODE="dry-run"; shift ;;
        --diff)    MODE="diff"; shift ;;
        --enable)  ENABLE=1; shift ;;
        --start)   START=1; shift ;;
        -h|--help) sed -n '2,30p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "error: unknown option: $1" >&2; exit 1 ;;
    esac
done

say()  { echo "$@"; }
info() { echo "  $@"; }

# copy_file SRC DST [MODE] --- honors MODE=dry-run/diff, reports status.
copy_file() {
    local src="$1" dst="$2" fmode="${3:-0644}"
    if [[ ! -f "$src" ]]; then
        info "[SKIP] $(basename "$dst") (source missing: $src)"
        return 0
    fi
    if [[ "$MODE" == "diff" ]]; then
        if [[ ! -f "$dst" ]]; then
            info "[NEW]  ${dst#"$HOME"/} (not installed)"
        elif ! cmp -s "$src" "$dst"; then
            info "[DRIFT] ${dst#"$HOME"/}"
            diff -u "$dst" "$src" | sed 's/^/    /' || true
        else
            info "[SAME] ${dst#"$HOME"/}"
        fi
        return 0
    fi
    if [[ "$MODE" == "dry-run" ]]; then
        if [[ ! -f "$dst" ]]; then
            info "[+NEW] ${dst#"$HOME"/}"
        elif ! cmp -s "$src" "$dst"; then
            info "[+UPD] ${dst#"$HOME"/}"
        else
            info "[=OK]  ${dst#"$HOME"/}"
        fi
        return 0
    fi
    # real install
    mkdir -p "$(dirname "$dst")"
    if [[ -f "$dst" ]] && cmp -s "$src" "$dst"; then
        info "[=OK]  ${dst#"$HOME"/}"
    else
        install -m "$fmode" "$src" "$dst"
        info "[WROTE] ${dst#"$HOME"/}"
    fi
}

say "skcode-hostd systemd install (mode: ${MODE})"
say "  unit dir: ${UNIT_DIR}"
say "  cfg dir:  ${CFG_DIR}"
say ""

# 1. Unit file
say "Unit:"
copy_file "${SCRIPT_DIR}/${UNIT}" "${UNIT_DIR}/${UNIT}"

# 2. Env template (never clobber a live skcode-hostd.env)
say ""
say "Env template:"
copy_file "${SCRIPT_DIR}/${ENV_EXAMPLE}" "${CFG_DIR}/${ENV_EXAMPLE}"

say ""
say "Live env preflight:"
if [[ -f "${CFG_DIR}/${ENV_LIVE}" ]]; then
    info "[OK]   ${CFG_DIR#"$HOME"/}/${ENV_LIVE} (present)"
else
    info "[MISS] ${CFG_DIR#"$HOME"/}/${ENV_LIVE}"
    info "       cp ${CFG_DIR#"$HOME"/}/${ENV_EXAMPLE} ${CFG_DIR#"$HOME"/}/${ENV_LIVE}"
    info "       then set SKCODE_HOSTD_TAILSCALE_IP + SKCODE_HOSTD_HOST_ID."
    info "       The unit fails closed until this exists (no bind IP)."
fi

if [[ "$MODE" == "diff" || "$MODE" == "dry-run" ]]; then
    say ""
    say "(${MODE}: nothing was written.)"
    exit 0
fi

# 3. Verify the rendered unit (read-only, safe) if the tool is available.
say ""
if command -v systemd-analyze >/dev/null 2>&1; then
    say "Verifying unit (systemd-analyze --user verify):"
    if systemd-analyze --user verify "${UNIT_DIR}/${UNIT}" 2>/dev/null; then
        info "[PASS] ${UNIT}"
    else
        info "[WARN] ${UNIT} (verify reported issues; review above)"
    fi
else
    info "systemd-analyze not available; skipped unit verify."
fi

# 4. daemon-reload (safe; does not start anything).
say ""
say "Reloading systemd user daemon..."
systemctl --user daemon-reload

# 5. Optional enable (idempotent). Never starts.
if [[ $ENABLE -eq 1 ]]; then
    say ""
    say "Enabling ${UNIT} (not starting):"
    systemctl --user enable "${UNIT}" >/dev/null 2>&1 && info "[EN] ${UNIT}" || info "[EN?] ${UNIT} (enable skipped/failed)"
fi

# 6. Optional start (only if requested and not already active; never restarts).
if [[ $START -eq 1 ]]; then
    say ""
    if systemctl --user is-active --quiet "${UNIT}"; then
        info "[RUN] ${UNIT} (already active, left as-is)"
    else
        systemctl --user start "${UNIT}" && info "[START] ${UNIT}" || info "[ERR] ${UNIT} failed to start"
    fi
fi

say ""
say "Done (mode: ${MODE}). Nothing was auto-started."
if [[ $ENABLE -eq 0 && $START -eq 0 ]]; then
    say ""
    say "Next steps (operator decides when to bring it up):"
    say "  1. Provision the env file (tailnet IP + host id):"
    say "       mkdir -p ${CFG_DIR}"
    say "       cp ${CFG_DIR}/${ENV_EXAMPLE} ${CFG_DIR}/${ENV_LIVE}"
    say "       \$EDITOR ${CFG_DIR}/${ENV_LIVE}"
    say "  2. Enable + start when ready:"
    say "       systemctl --user enable ${UNIT}"
    say "       systemctl --user start ${UNIT}"
    say "  3. Check it:"
    say "       systemctl --user status ${UNIT}"
    say "       journalctl --user -u ${UNIT} -f"
fi
