#!/usr/bin/env bash
# Install the daily ticket-hub maintenance job as a user launchd service.
#
# Renders deploy/com.ticket-hub.maintenance.plist.template into
#   ~/Library/LaunchAgents/com.ticket-hub.maintenance.plist
# (daily backup + quick hygiene + prune) and prints the EXACT launchctl
# command for the owner to run. This script never loads the service itself —
# service control belongs to the user. Same style as install_outbound.sh.
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${DEPLOY_DIR}/.." && pwd)"
TEMPLATE="${DEPLOY_DIR}/com.ticket-hub.maintenance.plist.template"
TARGET="${HOME}/Library/LaunchAgents/com.ticket-hub.maintenance.plist"

PYTHON="${TICKET_HUB_PYTHON:-$(command -v python3)}"
DB="${TICKET_HUB_DB:-${TICKET_HUB_HOME:-$HOME/.ticket-hub}/hub.db}"
HUB_URL="${TICKET_HUB_URL:-http://127.0.0.1:8777}"
DAILY_HOUR="${TICKET_HUB_MAINTENANCE_HOUR:-3}"
LOG_DIR="${TICKET_HUB_MAINTENANCE_LOG_DIR:-${TICKET_HUB_HOME:-$HOME/.ticket-hub}/logs}"

[ -f "${TEMPLATE}" ] || { echo "missing template: ${TEMPLATE}" >&2; exit 1; }
[ -x "${REPO_DIR}/scripts/com.ticket-hub.maintenance.sh" ] || { echo "maintenance script not executable: ${REPO_DIR}/scripts/com.ticket-hub.maintenance.sh" >&2; exit 1; }

mkdir -p "${HOME}/Library/LaunchAgents" "${LOG_DIR}"

sed \
  -e "s|__MAINTENANCE_SH__|${REPO_DIR}/scripts/com.ticket-hub.maintenance.sh|g" \
  -e "s|__DAILY_HOUR__|${DAILY_HOUR}|g" \
  -e "s|__LOG_DIR__|${LOG_DIR}|g" \
  -e "s|__DB__|${DB}|g" \
  -e "s|__HUB_URL__|${HUB_URL}|g" \
  -e "s|__PYTHON__|${PYTHON}|g" \
  "${TEMPLATE}" > "${TARGET}.tmp"

mv "${TARGET}.tmp" "${TARGET}"
plutil -lint "${TARGET}" >/dev/null

echo "Wrote: ${TARGET}"
echo "Daily at ${DAILY_HOUR}:00 — backup + quick hygiene + prune (keep 14)"
echo "DB: ${DB}  hub: ${HUB_URL}"
echo
echo "To start the service, run:"
echo "  launchctl load ${TARGET}"
echo "To stop it later:"
echo "  launchctl unload ${TARGET}"
echo "To run once right now without launchd:"
echo "  bash ${REPO_DIR}/scripts/com.ticket-hub.maintenance.sh"
