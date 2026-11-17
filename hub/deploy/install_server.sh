#!/usr/bin/env bash
# Install the ticket-hub server as a user launchd service (LaunchAgent).
#
# Renders deploy/com.ticket-hub.server.plist.template into
#   ~/Library/LaunchAgents/com.ticket-hub.server.plist
# (python path, repo paths, DB, port) and prints the EXACT launchctl command
# for the owner to run. This script never loads the service itself — service
# control belongs to the user. Same style as install_outbound.sh.
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${DEPLOY_DIR}/.." && pwd)"
TEMPLATE="${DEPLOY_DIR}/com.ticket-hub.server.plist.template"
TARGET="${HOME}/Library/LaunchAgents/com.ticket-hub.server.plist"

PYTHON="${TICKET_HUB_PYTHON:-$(command -v python3)}"
DB="${TICKET_HUB_DB:-${TICKET_HUB_HOME:-$HOME/.ticket-hub}/hub.db}"
PORT="${TICKET_HUB_PORT:-8777}"
LOG_DIR="${TICKET_HUB_SERVER_LOG_DIR:-${TICKET_HUB_HOME:-$HOME/.ticket-hub}/logs}"

[ -f "${TEMPLATE}" ] || { echo "missing template: ${TEMPLATE}" >&2; exit 1; }
[ -x "${PYTHON}" ] || { echo "python not executable: ${PYTHON} (set TICKET_HUB_PYTHON)" >&2; exit 1; }

mkdir -p "${HOME}/Library/LaunchAgents" "${LOG_DIR}"

sed \
  -e "s|__PYTHON__|${PYTHON}|g" \
  -e "s|__API_PY__|${REPO_DIR}/api.py|g" \
  -e "s|__REPO_DIR__|${REPO_DIR}|g" \
  -e "s|__DB__|${DB}|g" \
  -e "s|__PORT__|${PORT}|g" \
  -e "s|__LOG_DIR__|${LOG_DIR}|g" \
  "${TEMPLATE}" > "${TARGET}.tmp"

mv "${TARGET}.tmp" "${TARGET}"
plutil -lint "${TARGET}" >/dev/null

echo "Wrote: ${TARGET}"
echo "Python: ${PYTHON}"
echo "DB: ${DB}  port: ${PORT}"
echo
echo "To start the service, run:"
echo "  launchctl load ${TARGET}"
echo "To stop it later:"
echo "  launchctl unload ${TARGET}"
