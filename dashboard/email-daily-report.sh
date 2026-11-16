#!/usr/bin/env bash
# email-daily-report.sh — render the board, then email the exec summary.
# All paths via env overrides; generic defaults below.
#   AERIAL_REPO        repo root            (default: this script's repo)
#   AERIAL_PYTHON      interpreter          (default: python3)
#   AERIAL_STATE_DIR   scratch/state dir    (default: ~/.aerial/state)
set -u
REPO="${AERIAL_REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
VENV="${AERIAL_PYTHON:-python3}"
STATE_DIR="${AERIAL_STATE_DIR:-$HOME/.aerial/state}"
BOARD="${AERIAL_BOARD:-$STATE_DIR/daily-board.html}"
LOG="${AERIAL_BOARD_LOG:-$STATE_DIR/logs/daily-board.log}"
mkdir -p "$(dirname "$BOARD")" "$(dirname "$LOG")"

export PYTHONPATH="$REPO"
cd "$REPO"
"$VENV" dashboard/render_daily.py --out "$BOARD" >> "$LOG" 2>&1

cd "$REPO/dashboard"
"$VENV" - <<'PYEOF'
import email_delivery, email_report, os, pathlib, datetime
board = os.environ.get(
    "AERIAL_BOARD",
    os.path.expanduser("~/.aerial/state/daily-board.html"))
h = pathlib.Path(board).read_text()
date_str = datetime.date.today().strftime("%A, %B %d")
md_doc = email_report.build_email_text(h, date_str)
ok = email_delivery.send_report(
    "Aerial daily report — what happened, what it did, what needs you",
    md_doc)
print("email:", "sent" if ok else "FAILED")
PYEOF
