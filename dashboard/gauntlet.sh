#!/usr/bin/env bash
# gauntlet.sh — one-command wrapper for the Aerial Gauntlet.
#
#   flock guard (one gauntlet at a time), runs dashboard/gauntlet.py,
#   logs to $AERIAL_STATE_DIR/logs/gauntlet.log (default ~/.aerial/state).
#
# Budget: the full 8-layer gauntlet (incl. soak/fresh/reboot) gets
# AERIAL_GAUNTLET_BUDGET seconds (default 10800 = 3h; was 7200) before a
# hard TERM/KILL — a layer can never over-run the wrapper's budget.
#
# Usage: gauntlet.sh [--only 1,3] [--no-tickets]
set -euo pipefail
REPO="${AERIAL_REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
LOG_DIR="${AERIAL_STATE_DIR:-$HOME/.aerial/state}/logs"
LOG="$LOG_DIR/gauntlet.log"
BUDGET="${AERIAL_GAUNTLET_BUDGET:-10800}"
mkdir -p "$LOG_DIR"

exec 9>"$LOG_DIR/.gauntlet.lock"
flock -n 9 || { echo "[gauntlet] another gauntlet run is active; refusing" >&2; exit 3; }

echo "[gauntlet] start $(date -u +%Y-%m-%dT%H:%M:%SZ) args: $* budget=${BUDGET}s" >> "$LOG"
cd "$REPO"
timeout --signal=TERM --kill-after=60 "$BUDGET" \
  python3 -m dashboard.gauntlet "$@" 2>&1 | tee -a "$LOG"
rc=${PIPESTATUS[0]}
echo "[gauntlet] end $(date -u +%Y-%m-%dT%H:%M:%SZ) rc=$rc" >> "$LOG"
exit "$rc"
