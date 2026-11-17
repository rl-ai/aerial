#!/usr/bin/env bash
# Install the ticket-hub outbound daemon as a user launchd service.
#
# Renders deploy/com.ticket-hub.outbound.plist.template into
#   ~/Library/LaunchAgents/com.ticket-hub.outbound.plist
# (python path, repo paths, interval) and prints the EXACT launchctl command
# for the owner to run. This script never loads the service itself — service
# control belongs to the user.
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${DEPLOY_DIR}/.." && pwd)"
TEMPLATE="${DEPLOY_DIR}/com.ticket-hub.outbound.plist.template"
TARGET="${HOME}/Library/LaunchAgents/com.ticket-hub.outbound.plist"

PYTHON="${TICKET_HUB_PYTHON:-$(command -v python3)}"
CONFIG="${TICKET_HUB_ADAPTERS_CONFIG:-${TICKET_HUB_HOME:-$HOME/.ticket-hub}/adapters.json}"
STATE="${TICKET_HUB_OUTBOUND_STATE:-${TICKET_HUB_HOME:-$HOME/.ticket-hub}/outbound_state.json}"
HUB_URL="${TICKET_HUB_URL:-http://127.0.0.1:8777}"
INTERVAL="${TICKET_HUB_OUTBOUND_INTERVAL:-60}"
LOG_DIR="${TICKET_HUB_OUTBOUND_LOG_DIR:-${TICKET_HUB_HOME:-$HOME/.ticket-hub}/logs}"

[ -f "${TEMPLATE}" ] || { echo "missing template: ${TEMPLATE}" >&2; exit 1; }
[ -x "${PYTHON}" ] || { echo "python not executable: ${PYTHON} (set TICKET_HUB_PYTHON)" >&2; exit 1; }

mkdir -p "${HOME}/Library/LaunchAgents" "${LOG_DIR}"

sed \
  -e "s|__PYTHON__|${PYTHON}|g" \
  -e "s|__OUTBOUND_PY__|${REPO_DIR}/outbound.py|g" \
  -e "s|__REPO_DIR__|${REPO_DIR}|g" \
  -e "s|__CONFIG__|${CONFIG}|g" \
  -e "s|__STATE__|${STATE}|g" \
  -e "s|__HUB_URL__|${HUB_URL}|g" \
  -e "s|__INTERVAL__|${INTERVAL}|g" \
  -e "s|__LOG_DIR__|${LOG_DIR}|g" \
  "${TEMPLATE}" > "${TARGET}.tmp"

mv "${TARGET}.tmp" "${TARGET}"
plutil -lint "${TARGET}" >/dev/null

echo "Wrote: ${TARGET}"
echo "Python: ${PYTHON}"
echo "Hub URL: ${HUB_URL}  interval: ${INTERVAL}s"
echo
echo "To start the service, run:"
echo "  launchctl load ${TARGET}"
echo "To stop it later:"
echo "  launchctl unload ${TARGET}"
