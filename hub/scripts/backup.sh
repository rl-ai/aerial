#!/usr/bin/env bash
# backup.sh — hot-snapshot the hub DB with sqlite3 .backup (WAL-safe), verify
# the copy, keep the last 14 backups.
#
#   DB:   ${TICKET_HUB_DB:-${TICKET_HUB_HOME:-$HOME/.ticket-hub}/hub.db}  (source, read-only)
#   OUT:  ${TICKET_HUB_BACKUP_DIR:-${TICKET_HUB_HOME:-$HOME/.ticket-hub}/backups}/hub-YYYYmmdd-HHMMSS.db
set -euo pipefail

DB="${TICKET_HUB_DB:-${TICKET_HUB_HOME:-$HOME/.ticket-hub}/hub.db}"
BACKUP_DIR="${TICKET_HUB_BACKUP_DIR:-${TICKET_HUB_HOME:-$HOME/.ticket-hub}/backups}"
KEEP=14

command -v sqlite3 >/dev/null 2>&1 || { echo "FAIL  sqlite3 not found" >&2; exit 1; }
[ -f "${DB}" ] || { echo "FAIL  source db not found: ${DB}" >&2; exit 1; }

mkdir -p "${BACKUP_DIR}"
STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="${BACKUP_DIR}/hub-${STAMP}.db"

# .backup takes a consistent snapshot even while the server writes (WAL).
# NB: the source connection must NOT be mode=ro — sqlite opens the
# destination through it, and a read-only handle fails with error 14.
sqlite3 "${DB}" ".backup '${DEST}'" || { echo "FAIL  sqlite3 .backup failed for ${DB}" >&2; exit 1; }

# The copy inherits the WAL-mode header; converting it to rollback-journal
# mode checkpoints the snapshot into a single self-contained file (no
# stray -wal/-shm sidecars when later opened, no ro-URI open failures).
sqlite3 "${DEST}" 'PRAGMA journal_mode=delete;' >/dev/null
SRC_SIZE=$(stat -f%z "${DB}" 2>/dev/null || stat -c%s "${DB}")
DST_SIZE=$(stat -f%z "${DEST}")

# verify the copy
RES="$(sqlite3 "${DEST}" 'PRAGMA integrity_check;')"
if [ "${RES}" != "ok" ]; then
    echo "FAIL  backup failed integrity check: ${DEST}" >&2
    exit 1
fi

# prune: keep the newest $KEEP hub-*.db (pipe through process substitution so
# PRUNED survives the loop)
COUNT=$(ls -1t "${BACKUP_DIR}"/hub-*.db 2>/dev/null | wc -l | tr -d ' ')
PRUNED=0
if [ "${COUNT}" -gt "${KEEP}" ]; then
    while IFS= read -r old; do
        rm -f "${old}"
        PRUNED=$((PRUNED + 1))
    done < <(ls -1t "${BACKUP_DIR}"/hub-*.db | tail -n +$((KEEP + 1)))
fi

echo "backup: ${DB} -> ${DEST} (${DST_SIZE} bytes, source ${SRC_SIZE} bytes)"
echo "verify: PRAGMA integrity_check = ok"
echo "retention: kept newest ${KEEP}, pruned ${PRUNED} (total now: $(( COUNT > KEEP ? KEEP : COUNT )))"
