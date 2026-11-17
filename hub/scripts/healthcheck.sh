#!/usr/bin/env bash
# healthcheck.sh — one-line ok/FAIL for launchd/cron monitors.
# Checks: GET /dashboard responds within timeout and the board has >0 tickets.
#   URL: ${TICKET_HUB_URL:-http://127.0.0.1:8777}
#   Auth: ${TICKET_HUB_TOKEN_FILE} (optional bearer file; needed once hub auth is on)
# Side effect: writes last-known health to
#   ${TICKET_HUB_HEALTH_STATUS:-$TICKET_HUB_HOME/health-status.json}
#   so the hub's /health endpoint and any monitor can surface failures
#   between checks (GLM review requirement: a FAIL must be visible).
# Exit: 0 ok, 1 FAIL.
set -uo pipefail

URL="${TICKET_HUB_URL:-http://127.0.0.1:8777}"
TIMEOUT="${TICKET_HUB_HEALTHCHECK_TIMEOUT:-5}"
STATUS_FILE="${TICKET_HUB_HEALTH_STATUS:-${TICKET_HUB_HOME:-$HOME/.ticket-hub}/health-status.json}"
TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

write_status() {  # $1 ok|fail  $2 tickets  $3 error(json or null)
    mkdir -p "$(dirname "${STATUS_FILE}")" 2>/dev/null || true
    printf '%s' "{\"ok\": $1, \"ts\": \"${TS}\", \"tickets\": $2, \"url\": \"${URL}\", \"last_error\": $3}" \
        > "${STATUS_FILE}.tmp" 2>/dev/null && mv "${STATUS_FILE}.tmp" "${STATUS_FILE}" 2>/dev/null || true
}

# With hub auth on, /board needs a bearer: TICKET_HUB_TOKEN_FILE names a file holding it.
# The token goes to curl on stdin (a shell builtin printf), so it never appears in process arguments.
TOKEN_FILE="${TICKET_HUB_TOKEN_FILE:-}"
fetch_board() {
    if [ -n "${TOKEN_FILE}" ] && [ -r "${TOKEN_FILE}" ]; then
        printf 'header = "Authorization: Bearer %s"\n' "$(tr -d '\r\n' < "${TOKEN_FILE}")" \
            | curl -fsS --max-time "${TIMEOUT}" -K - "${URL}/dashboard/health"
    else
        curl -fsS --max-time "${TIMEOUT}" "${URL}/dashboard/health"
    fi
}

BODY="$(fetch_board 2>/dev/null)" || {
    echo "FAIL  hub healthcheck: no response from ${URL}/dashboard/health within ${TIMEOUT}s"
    write_status false -1 '"no response"'
    exit 1
}

# The board goes in on stdin: a large board passed as an argument exceeds the argument-size limit.
COUNT="$(printf '%s' "${BODY}" | python3 -c '
import json, sys
try:
    board = json.load(sys.stdin)
    if isinstance(board, dict) and board.get("ok"):
        print(int(board.get("tickets", -1)))
    else:
        print(-1)
except Exception:
    print(-1)
' 2>/dev/null)"

if [ "${COUNT:-x}" != "x" ] && [ "${COUNT}" -gt 0 ] 2>/dev/null; then
    echo "ok  hub ${URL} — ${COUNT} tickets on board"
    write_status true "${COUNT}" null
    exit 0
else
    echo "FAIL  hub healthcheck: ${URL}/dashboard/health unhealthy (tickets=${COUNT:-unparseable})"
    write_status false "${COUNT:--1}" '"unhealthy board"'
    exit 1
fi
