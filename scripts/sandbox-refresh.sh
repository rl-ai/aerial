#!/usr/bin/env bash
# sandbox-refresh.sh — refresh a fix/optimization session workspace to the
# current build before the runner dispatches the agent (runtime-optimization
# lane). Usage: sandbox-refresh.sh <session-workspace> [tree]...
#
# Default trees: ai-ops (prod) and second-brain-gateway, git-pulled into
# <workspace>/<name> so the dispatched agent acts on the current build.
# Read-only toward the live repos: it clones from the local origin paths
# and pulls in the workspace copies only. A failed refresh is logged and
# non-fatal (recorded in <workspace>/refresh.log).
set -u

WS="${1:?usage: sandbox-refresh.sh <session-workspace> [tree]...}"
shift || true
TREES=("$@")
if [ ${#TREES[@]} -eq 0 ]; then
    TREES=("ai-ops" "second-brain-gateway")
fi
# Known tree names → sources (local origin paths; a full "name:path" spec
# overrides). The workspace copy is cloned from / pulled toward these.
declare -A SRC=(
    [ai-ops]="/home/ai-eva/repos/ai-ops.git"
    [second-brain-gateway]="/home/ai-eva/workspaces/build/second-brain-gateway"
)

mkdir -p "$WS"
LOG="$WS/refresh.log"
echo "refresh $(date -u +%Y-%m-%dT%H:%M:%SZ) ws=$WS" >> "$LOG"

rc=0
for spec in "${TREES[@]}"; do
    name="${spec%%:*}"
    src="${spec#*:}"
    [ "$src" = "$spec" ] && src="${SRC[$name]:-$src}"
    dest="$WS/$name"
    if [ -d "$dest/.git" ]; then
        if (cd "$dest" && git fetch origin --quiet \
                && git reset --hard origin/HEAD --quiet 2>/dev/null \
                || git pull --ff-only --quiet) >> "$LOG" 2>&1; then
            echo "ok: pull $name" >> "$LOG"
        else
            echo "FAIL: pull $name (see above)" >> "$LOG"
            rc=1
        fi
    elif git clone --quiet "$src" "$dest" >> "$LOG" 2>&1; then
        echo "ok: clone $name from $src" >> "$LOG"
    else
        echo "FAIL: clone $name from $src" >> "$LOG"
        rc=1
    fi
done
exit $rc
