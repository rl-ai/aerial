#!/usr/bin/env bash
# ops-agent start script — nohup daemon with flock guard + pidfile.
# Usage: bash dashboard/ops-agent.sh [--once]
# Only one daemon instance ever runs: it holds flock on
# $AERIAL_STATE_DIR/run/ops-agent.lock for its lifetime; a second start
# (or the flock-guarded cron supervisor line) no-ops while it's alive.
set -u
REPO="${AERIAL_REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
STATE_DIR="${AERIAL_STATE_DIR:-$HOME/.aerial/state}"
RUN_DIR="$STATE_DIR/run"
LOG="${AERIAL_OPS_LOG:-$STATE_DIR/logs/ops-agent.log}"
PIDFILE="$RUN_DIR/ops-agent.pid"
LOCK="$RUN_DIR/ops-agent.lock"
VENV="${AERIAL_PYTHON:-python3}"
export PYTHONPATH="$REPO"
mkdir -p "$RUN_DIR" "$(dirname "$LOG")"

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date -Is) ops-agent already running (pid $(cat "$PIDFILE" 2>/dev/null))" >> "$LOG"
  exit 0
fi

if [ "${1:-}" = "--once" ]; then
  cd "$REPO" && "$VENV" dashboard/followup/runner.py --once >> "$LOG" 2>&1
  exit $?
fi

nohup "$VENV" "$REPO/dashboard/followup/runner.py" --interval 300 \
  >> "$LOG" 2>&1 &
PID=$!
echo "$PID" > "$PIDFILE"
echo "$(date -Is) ops-agent started pid=$PID" >> "$LOG"
echo "ops-agent started pid=$PID log=$LOG"
