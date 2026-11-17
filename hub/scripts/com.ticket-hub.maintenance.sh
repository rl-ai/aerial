#!/usr/bin/env bash
# maintenance.sh — daily always-on maintenance body for the launchd
# maintenance job (see com.ticket-hub.maintenance.plist.template).
# Order matters: snapshot first, then hygiene (quick), then prune is done
# inside backup.sh's retention policy.
#
# Non-interactive; one summary line per step; exit non-zero on any failure
# so the launchd log shows it.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FAILURES=0

echo "=== ticket-hub maintenance $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

if "${SCRIPT_DIR}/backup.sh"; then
    echo "MAINT: backup PASS"
else
    echo "MAINT: backup FAIL"
    FAILURES=$((FAILURES + 1))
fi

# quick hygiene: lint + secrets + db integrity, skip the full test suite
if "${SCRIPT_DIR}/hygiene_check.sh" --quick; then
    echo "MAINT: hygiene(quick) PASS"
else
    echo "MAINT: hygiene(quick) FAIL"
    FAILURES=$((FAILURES + 1))
fi

if "${SCRIPT_DIR}/healthcheck.sh"; then
    echo "MAINT: healthcheck PASS"
else
    echo "MAINT: healthcheck FAIL (server may be stopped — not fatal for maintenance)"
fi

# prune is owned by backup.sh (keep 14); nothing extra here.
# Machine-readable summary for GET /admin/status (Mission Control).
STATUS_FILE="${TICKET_HUB_MAINT_STATUS:-${TICKET_HUB_HOME:-$HOME/.ticket-hub}/maintenance-status.json}"
mkdir -p "$(dirname "${STATUS_FILE}")" 2>/dev/null || true
printf '%s' "{\"last_run\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\", \"summary\": \"$( [ "${FAILURES}" -eq 0 ] && echo 'PASS' || echo "FAIL (${FAILURES} step(s))" )\", \"failures\": ${FAILURES}}" \
    > "${STATUS_FILE}.tmp" 2>/dev/null && mv "${STATUS_FILE}.tmp" "${STATUS_FILE}" 2>/dev/null || true

if [ "${FAILURES}" -eq 0 ]; then
    echo "MAINT: PASS"
    exit 0
else
    echo "MAINT: FAIL (${FAILURES} step(s) failed)"
    exit 1
fi
