#!/bin/bash
# backup_offsite.sh — mirror the hub DB (and optionally one immutable-bundle
# directory) to an offsite destination configured by environment.
# Inert-safe: skips (exit 0) when the destination is unset or absent,
# retries nothing.
#
#   HUB_BACKUP_DEST       destination directory (e.g. a mounted offsite/backup
#                         share). Unset -> SKIP; not a directory -> SKIP.
#   TICKET_HUB_DB         source DB (default $TICKET_HUB_HOME/hub.db).
#   HUB_BACKUP_EXTRA_DIR  optional directory of immutable files to mirror with
#                         rsync --ignore-existing into "$HUB_BACKUP_DEST/extra".
set -u
DEST="${HUB_BACKUP_DEST:-}"
HUB_DB="${TICKET_HUB_DB:-${TICKET_HUB_HOME:-$HOME/.ticket-hub}/hub.db}"
EXTRA_DIR="${HUB_BACKUP_EXTRA_DIR:-}"
LOG="${TICKET_HUB_HOME:-$HOME/.ticket-hub}/backup.log"

log() { mkdir -p "$(dirname "$LOG")" 2>/dev/null; echo "$(date '+%F %T') $*" >> "$LOG"; }

if [ -z "$DEST" ]; then
    log "SKIP: HUB_BACKUP_DEST not set"
    exit 0
fi
if [ ! -d "$DEST" ]; then
    log "SKIP: $DEST not a directory"
    exit 0
fi
mkdir -p "$DEST/ticket-hub"

# 1. hub DB: sqlite-consistent snapshot, keep 7 daily copies.
if [ -f "$HUB_DB" ]; then
    day=$(date +%Y%m%d)
    dest="$DEST/ticket-hub/hub-$day.db"
    if sqlite3 "$HUB_DB" ".backup '$dest'"; then
        log "OK hub db -> $dest ($(du -h "$dest" | cut -f1))"
        ls -1t "$DEST"/ticket-hub/hub-*.db 2>/dev/null | tail -n +8 \
            | xargs rm -f 2>/dev/null
    else
        log "FAIL hub db backup"
    fi
fi

# 2. optional immutable-file directory: rsync, --ignore-existing (files are
# immutable once written, so never overwrite at the destination).
if [ -n "$EXTRA_DIR" ] && [ -d "$EXTRA_DIR" ]; then
    mkdir -p "$DEST/extra"
    if rsync -a --ignore-existing "$EXTRA_DIR/" "$DEST/extra/" \
            --exclude '._*' 2>>"$LOG"; then
        find "$DEST/extra" -name '._*' -delete 2>/dev/null
        log "OK extra -> $DEST/extra"
    else
        log "FAIL extra rsync"
    fi
fi
exit 0
