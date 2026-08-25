#!/usr/bin/env bash
# pi-cockpit.sh -- tmux-driven manual pi harness cockpit.
#
# Chef picks the cards; each gets its own tmux pane running ONE card through
# the real pi harness -> skgateway -> claude-code-api route, end to end
# (isolated worktree -> Ralph rounds -> twin-gated grade -> commit/push/PR),
# via the SAME EngineeringExecutor the fleet uses. This exists because
# fleet_dispatch fails closed while the fleet is frozen (card P6 / 08963fbb):
# a human-driven singleton never reaches that placement gate, so Chef is the
# scheduler here, by hand. NOTHING this launches ever calls fleet_dispatch,
# touches _freeze.json/skoperator.*, or merges a PR.
#
# Usage:
#   scripts/pi-cockpit.sh CARD_ID [CARD_ID...]
#
# Env overrides:
#   SKOS_AUTOPILOT_CONFIG     autopilot yaml (default: the proven pi/claude-code-api
#                             route, ~/.skcapstone/config/autopilot-pi-claude.yaml)
#   PI_COCKPIT_SESSION        tmux session name (default: pi-cockpit-<HHMMSS>;
#                             refuses "swarm" and refuses an already-live name)
#   PI_COCKPIT_PYTHON         interpreter to run the panes with (default: ~/.skenv/bin/python)
#   PI_COCKPIT_AGENT_NAME     coord-board claim identity (default: pi-cockpit)
#   SKHARNESS_PI_SPAWN_STATE  authoritative spawn-control state file (required)
#   SKHARNESS_PI_SPAWN_ACTOR  attributable launcher identity (required)
#
# Example:
#   scripts/pi-cockpit.sh a7e3ca15 284a7d6e
#   tmux attach -t pi-cockpit-143022

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${SKOS_AUTOPILOT_CONFIG:-$HOME/.skcapstone/config/autopilot-pi-claude.yaml}"
SESSION="${PI_COCKPIT_SESSION:-pi-cockpit-$(date +%H%M%S)}"
PY="${PI_COCKPIT_PYTHON:-$HOME/.skenv/bin/python}"
AGENT_NAME="${PI_COCKPIT_AGENT_NAME:-pi-cockpit}"
SPAWN_STATE="${SKHARNESS_PI_SPAWN_STATE:?SKHARNESS_PI_SPAWN_STATE is required}"
SPAWN_ACTOR="${SKHARNESS_PI_SPAWN_ACTOR:?SKHARNESS_PI_SPAWN_ACTOR is required}"
PI_LAUNCH=(skharness-pi-launch --state "$SPAWN_STATE" --actor "$SPAWN_ACTOR" --scope pi:all)

if [ "$#" -eq 0 ]; then
  echo "usage: $0 CARD_ID [CARD_ID...]" >&2
  exit 1
fi

if [ "$SESSION" = "swarm" ]; then
  echo "refusing session name 'swarm' -- that session already exists and is not ours" >&2
  exit 1
fi
if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "tmux session '$SESSION' already exists; set PI_COCKPIT_SESSION to a fresh name" >&2
  exit 1
fi
if ! command -v tmux >/dev/null 2>&1; then
  echo "tmux is not on PATH" >&2
  exit 1
fi
if [ ! -f "$CONFIG" ]; then
  echo "autopilot config not found: $CONFIG" >&2
  exit 1
fi

RUN_ID="$(date +%Y%m%d-%H%M%S)-$$"
STATUS_DIR="/tmp/pi-cockpit/$RUN_ID"
mkdir -p "$STATUS_DIR"

echo "pi-cockpit run $RUN_ID"
echo "  session:    $SESSION"
echo "  config:     $CONFIG"
echo "  status dir: $STATUS_DIR"
echo "  cards:      $*"
echo "  NEVER merges. fleet_dispatch is not consulted."
echo

# Control pane (window "control", pane 0).
"${PI_LAUNCH[@]}" --worker-id "$RUN_ID-control" --kind tmux -- \
  tmux new-session -d -s "$SESSION" -n control \
  "$PY" "$HERE/scripts/pi_cockpit/control.py" \
    --status-dir "$STATUS_DIR" --config "$CONFIG" --cards "$@"

# One pane per card, tiled alongside the control pane in the same window.
for CARD in "$@"; do
  CMD="$(printf 'SKOS_AUTOPILOT_CONFIG=%q skharness-pi-launch --state %q --worker-id %q --actor %q --scope pi:all --kind process -- %q %q --card %q --status-dir %q --agent-name %q; ec=$?; echo; if [ $ec -eq 0 ]; then echo "[pane done: 0 -- press Enter to close]"; else echo "[pane done: $ec -- press Enter to close]"; fi; read _' \
    "$CONFIG" "$SPAWN_STATE" "$RUN_ID-$CARD" "$SPAWN_ACTOR" "$PY" "$HERE/scripts/pi_cockpit/run_card.py" "$CARD" "$STATUS_DIR" "$AGENT_NAME")"
  "${PI_LAUNCH[@]}" --worker-id "$RUN_ID-pane-$CARD" --kind tmux -- \
    tmux split-window -t "$SESSION:control" -h "$CMD"
  tmux select-layout -t "$SESSION:control" tiled
done

tmux select-pane -t "$SESSION:control.0"
echo "attach with: tmux attach -t $SESSION"
