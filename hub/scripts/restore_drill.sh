#!/usr/bin/env bash
# restore_drill.sh — quarterly (or on-demand) proof that backups restore.
# A backup that has never been restored is an unverified backup.
#
# What it does (read-only against production data):
#   1. Picks the newest snapshot in BACKUP_DIR (or $1).
#   2. Copies it into a scratch temp DB.
#   3. PRAGMA integrity_check on the restored copy.
#   4. Counts tickets/events on the restored copy and prints them.
#   5. Removes the scratch copy.
#
# Exit: 0 DRILL PASS, 1 DRILL FAIL. Never writes to the live hub.db.
set -uo pipefail

DB_DIR="${TICKET_HUB_DIR:-${TICKET_HUB_HOME:-$HOME/.ticket-hub}}"
BACKUP_DIR="${BACKUP_DIR:-${DB_DIR}/backups}"
SNAP="${1:-$(ls -1t "${BACKUP_DIR}"/hub-*.db 2>/dev/null | head -1)}"

fail() { echo "DRILL: FAIL — $1"; exit 1; }
[ -n "${SNAP}" ] && [ -f "${SNAP}" ] || fail "no backup snapshot found in ${BACKUP_DIR}"

TMP="$(mktemp -d "${TMPDIR:-/tmp}/hub-restore-drill.XXXXXX")" || fail "mktemp failed"
trap 'rm -rf "${TMP}"' EXIT
COPY="${TMP}/restored.db"

cp "${SNAP}" "${COPY}" || fail "copy failed"

INTEGRITY="$(sqlite3 "${COPY}" 'PRAGMA integrity_check;' 2>&1)" || fail "integrity_check errored"
[ "${INTEGRITY}" = "ok" ] || fail "integrity_check: ${INTEGRITY}"

TICKETS="$(sqlite3 "${COPY}" 'SELECT COUNT(*) FROM tickets;' 2>/dev/null)" || fail "ticket count failed"
EVENTS="$(sqlite3 "${COPY}" 'SELECT COUNT(*) FROM events;' 2>/dev/null)" || fail "event count failed"
[ "${TICKETS:-0}" -gt 0 ] || fail "restored copy has no tickets"

echo "DRILL: PASS — ${SNAP##*/}: integrity ok, ${TICKETS} tickets, ${EVENTS} events"
