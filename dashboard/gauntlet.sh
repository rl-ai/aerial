#!/usr/bin/env bash
# gauntlet.sh — one-command wrapper for the Aerial Gauntlet.
#
#   flock guard (one gauntlet at a time), runs dashboard/gauntlet.py,
#   logs to $AERIAL_STATE_DIR/logs/gauntlet.log (default ~/.aerial/state).
#
# Usage: gauntlet.sh [--only 1,3] [--no-tickets]
set -euo pipefail
REPO="${AERIAL_REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
LOG_DIR="${AERIAL_STATE_DIR:-$HOME/.aerial/state}/logs"
LOG="$LOG_DIR/gauntlet.log"
mkdir -p "$LOG_DIR"

exec 9>"$LOG_DIR/.gauntlet.lock"
flock -n 9 || { echo "[gauntlet] another gauntlet run is active; refusing" >&2; exit 3; }

echo "[gauntlet] start $(date -u +%Y-%m-%dT%H:%M:%SZ) args: $*" >> "$LOG"
cd "$REPO"
python3 -m dashboard.gauntlet "$@" 2>&1 | tee -a "$LOG"
rc=${PIPESTATUS[0]}
echo "[gauntlet] end $(date -u +%Y-%m-%dT%H:%M:%SZ) rc=$rc" >> "$LOG"
exit "$rc"
