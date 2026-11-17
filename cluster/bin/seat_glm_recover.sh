#!/usr/bin/env bash
# seat_glm_recover.sh — ops-host-side recovery driver for the GLM-5.3-Flash
# orchestrator seat (seat host:18110, forwarded to the ops host:18110).
#
# Recovery ladder (see docs/sre-runbook-glm-seat.md):
#   (a) probe the local seat  GET http://127.0.0.1:18110/v1/models with the
#       cluster bearer — HTTP 200 means up (f6: HTTP-ready, not TCP), exit 0.
#   (b) diagnostic only: raw TCP to seat host:18110 (f11: NEVER gates any
#       decision — diagnostics go to stderr).
#   (c) pinned-host-key forced-command recovery on the seat host (f12):
#       timeout 300 ssh -i $RECOVERY_KEY -o BatchMode=yes
#         -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$KNOWN_HOSTS
#         -o ServerAliveInterval=15 -o ServerAliveCountMax=3 (f7)
#         -o ConnectTimeout=5 $SEAT_RECOVERY_USER@$SEAT_HOST recover
#       (the sshd forced command runs /usr/local/sbin/seat-glm-recover;
#       ssh exit 0 = seat up, 1 = attempted+failed, 3 = throttled).
#       The driver REFUSES to run when the pinned known_hosts file is
#       missing or its fingerprint does not match (fail closed, exit 2).
#   (d) forwarder restart — decided ONLY by the local HTTP probe (f11), and
#       restricted to the GLM-specific unit $GLM_FORWARDER_UNIT
#       (f5: a shared worker-forward unit serves other seats and
#       is never restarted here). Skipped with a diagnostic when
#       `systemctl --user` is unavailable in this context (f9).
#   (e) final verdict: EXACTLY ONE JSON line on stdout (f10); all
#       diagnostics — TCP state, ssh output, restart messages — go to stderr.
#
# Exit codes (f3/f4):
#   0 = seat up (JSON state=up)
#   1 = recovery attempted / probe-only, seat still down (state=failed)
#   2 = refused to run: pinned host key missing/mismatched, bad flag (state=failed, via=host-key)
#   3 = the seat host reported throttled — still down, retry later; NO forwarder
#       restart, NO driver restart by the runner (state=throttled)
#   4 = lock contention — another driver instance owns the recovery
#       (state=skipped); the single JSON line is still emitted
#
# Flags:
#   --probe-only  (a) + (b) diagnostics only: no ssh, no restarts.
#   --force       run the full ladder even when the local probe answers 200.
#
# Concurrency (f4): flock -n on a file the driver creates inside
# ${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/seat-glm-recover/ (mode 700, owner
# verified, symlink-checked — never follows a planted symlink). Contention
# emits the JSON line (state=skipped) and exits 4 — never a silent 0.
#
# Required (your deployment's values; nothing is built in):
#   SEAT_HOST, SEAT_RECOVERY_USER, SEAT_HOST_KEY_FP (the seat host's
#   pinned SHA256 ed25519 fingerprint), CLUSTER_BEARER_PATH.
# Optional: GLM_FORWARDER_UNIT (no forwarder restart without it),
#   SEAT_RECOVERY_KEY (default ~/.ssh/seat_recovery_ed25519), SEAT_PORT,
#   GLM_SEAT_PORT, SEAT_KNOWN_HOSTS, SEAT_SSH_TIMEOUT.
set -uo pipefail

SEAT_HOST=${SEAT_HOST:-}
SEAT_PORT=${SEAT_PORT:-18110}
SEAT_PORT=${GLM_SEAT_PORT:-18110}
BEARER_PATH=${CLUSTER_BEARER_PATH:-}
RECOVERY_KEY=${SEAT_RECOVERY_KEY:-$HOME/.ssh/seat_recovery_ed25519}
RECOVERY_USER=${SEAT_RECOVERY_USER:-}
SSH_TIMEOUT=${SEAT_SSH_TIMEOUT:-300}
EXPECTED_FP=${SEAT_HOST_KEY_FP:-}
# f12: pinned known_hosts lives in the profile state dir; bootstrap once via
#   ssh-keyscan -t ed25519 "$SEAT_HOST" > "$KNOWN_HOSTS"   (see runbook)
KNOWN_HOSTS=${SEAT_KNOWN_HOSTS:-${XDG_STATE_HOME:-$HOME/.local/state}/seat-host/seat_known_hosts}
# f5: GLM-specific unit ONLY — a shared worker-forward unit serves other
# seats and must never be restarted by this driver.
GLM_FORWARDER=${GLM_FORWARDER_UNIT:-}

missing=""
for v in SEAT_HOST:"$SEAT_HOST" SEAT_RECOVERY_USER:"$RECOVERY_USER" \
         SEAT_HOST_KEY_FP:"$EXPECTED_FP" CLUSTER_BEARER_PATH:"$BEARER_PATH"; do
  [ -n "${v#*:}" ] || missing="$missing ${v%%:*}"
done
if [ -n "$missing" ]; then
  echo "seat_glm_recover: set the deployment values:$missing (see the header)" >&2
  printf '{"seat":"glm","state":"failed","via":"config","ssh_exit":-1}\n'
  exit 2
fi

PROBE_ONLY=0
FORCE=0
for arg in "$@"; do
  case "$arg" in
    --probe-only) PROBE_ONLY=1 ;;
    --force)      FORCE=1 ;;
    -h|--help)    sed -n '2,45p' "$0" >&2; exit 0 ;;
    *) echo "seat_glm_recover: unknown flag: $arg (try --help)" >&2; \
       printf '{"seat":"glm","state":"failed","via":"bad-flag","ssh_exit":-1}\n'; exit 2 ;;
  esac
done

# -- emit: the ONE stdout line (f10) ------------------------------------------
emit() {  # state via ssh_exit — exactly one JSON line on stdout
  printf '{"seat":"glm","state":"%s","via":"%s","ssh_exit":%s}\n' "$1" "$2" "$3"
}
log() { echo "seat_glm_recover: $*" >&2; }

# -- f4: safe lock (no /tmp, no symlink following) ----------------------------
LOCK_ROOT="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
LOCKDIR="$LOCK_ROOT/seat-glm-recover"
if [ -L "$LOCKDIR" ]; then
  log "lock dir $LOCKDIR is a symlink — refusing (f4)"
  emit skipped lock-symlink -1
  exit 4
fi
mkdir -p -m 700 "$LOCKDIR" 2>/dev/null || true
if [ ! -d "$LOCKDIR" ] || [ "$(stat -c %u "$LOCKDIR" 2>/dev/null)" != "$(id -u)" ]; then
  log "lock dir $LOCKDIR unusable/not owned by $(id -u) — refusing (f4)"
  emit skipped lock-dir -1
  exit 4
fi
LOCKFILE="$LOCKDIR/lock"
if [ -L "$LOCKFILE" ]; then
  log "lock file $LOCKFILE is a symlink — refusing (f4)"
  emit skipped lock-symlink -1
  exit 4
fi
exec 9>>"$LOCKFILE"
if ! flock -n 9; then
  log "another recovery is running (lock $LOCKFILE) — emitting skipped/4 (f4)"
  emit skipped lock-contention -1
  exit 4
fi

# -- helpers -------------------------------------------------------------------
probe() {
  # f6: HTTP status of GET /v1/models with the bearer; 000 when unreachable
  local code bearer
  bearer=$(cat "$BEARER_PATH" 2>/dev/null) || bearer=""
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
    -H "authorization: Bearer $bearer" \
    "http://127.0.0.1:${SEAT_PORT}/v1/models" 2>/dev/null) || code=000
  echo "$code"
}

seat_tcp() {
  # DIAGNOSTIC ONLY (f11) — never gates a restart decision
  timeout 3 bash -c 'exec 3<>/dev/tcp/"$1"/"$2"' _ "$SEAT_HOST" "$SEAT_PORT" 2>/dev/null
}

systemctl_user_ok() {
  # f9: verify the systemd user session is reachable in THIS context
  if [ -z "${XDG_RUNTIME_DIR:-}" ]; then
    log "XDG_RUNTIME_DIR unset — systemctl --user unavailable here"
    return 1
  fi
  local out rc
  out=$(systemctl --user is-system-running 2>/dev/null); rc=$?
  if [ "$rc" -eq 0 ]; then return 0; fi
  case "$out" in
    degraded|running|"" ) [ "$rc" -le 1 ] && return 0 || return 1 ;;
    *) log "systemctl --user reports '$out' (rc=$rc) — unavailable here"; return 1 ;;
  esac
}

# -- (a) local HTTP seat probe (f6) --------------------------------------------
code=$(probe)
if [ "$code" = "200" ] && [ "$FORCE" = "0" ]; then
  emit up already -1
  exit 0
fi

SSH_EXIT=-1
VIA=probe-only

# -- --probe-only short-circuit -------------------------------------------------
if [ "$PROBE_ONLY" = "1" ]; then
  if seat_tcp; then log "seat-host:${SEAT_PORT} tcp=open"; else log "seat-host:${SEAT_PORT} tcp=closed"; fi
  code=$(probe)
  if [ "$code" = "200" ]; then emit up already "$SSH_EXIT"; exit 0; fi
  emit failed probe-only "$SSH_EXIT"
  exit 1
fi

# -- (b) seat-host TCP pre-check — diagnostic to stderr (f10/f11) --------------------
if seat_tcp; then log "seat-host:${SEAT_PORT} tcp=open"; else log "seat-host:${SEAT_PORT} tcp=closed"; fi

# -- (c) pinned-host-key forced-command recovery on the seat host (f12/f7) ---------------
if [ ! -f "$KNOWN_HOSTS" ] || [ -L "$KNOWN_HOSTS" ]; then
  log "pinned known_hosts $KNOWN_HOSTS missing — refusing to run (f12, fail closed)"
  log "bootstrap once: ssh-keyscan -t ed25519 ${SEAT_HOST} > $KNOWN_HOSTS (see runbook)"
  emit failed host-key -1
  exit 2
fi
if ! ssh-keygen -F "$SEAT_HOST" -f "$KNOWN_HOSTS" >/dev/null 2>&1; then
  log "no pinned entry for $SEAT_HOST in $KNOWN_HOSTS — refusing (f12)"
  emit failed host-key -1
  exit 2
fi
if ! ssh-keygen -F "$SEAT_HOST" -f "$KNOWN_HOSTS" 2>/dev/null | grep -qF "$EXPECTED_FP"; then
  log "pinned key fingerprint mismatch (expected $EXPECTED_FP) — refusing (f12)"
  emit failed host-key -1
  exit 2
fi

log "running seat-host recovery via ssh (${RECOVERY_USER}@${SEAT_HOST}, key ${RECOVERY_KEY})"
ssh_out=$(timeout "$SSH_TIMEOUT" ssh -i "$RECOVERY_KEY" \
  -o BatchMode=yes \
  -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile="$KNOWN_HOSTS" \
  -o ServerAliveInterval=15 \
  -o ServerAliveCountMax=3 \
  -o ConnectTimeout=5 \
  "${RECOVERY_USER}@${SEAT_HOST}" recover 2>&1)
SSH_EXIT=$?
[ -n "$ssh_out" ] && echo "$ssh_out" >&2
VIA=seat-recover

# -- f3: throttled is NOT failed — no forwarder restart, retry later -----------
if [ "$SSH_EXIT" = "3" ]; then
  log "the seat host throttled the recovery (exit 3) — seat still down, retry later"
  emit throttled seat-recover 3
  exit 3
fi

# -- (d) forwarder restart: local HTTP probe ONLY (f11), GLM unit only (f5) -----
code=$(probe)
if [ "$code" != "200" ]; then
  if systemctl_user_ok; then
    log "local seat dead (HTTP probe ${code}) — restarting ${GLM_FORWARDER} ONLY (shared worker-forward untouched, f5)"
    if [ -z "$GLM_FORWARDER" ]; then
      log "GLM_FORWARDER_UNIT unset — forwarder restart skipped"
    elif systemctl --user restart "$GLM_FORWARDER" 2>&1; then
      log "restarted ${GLM_FORWARDER}"
    else
      log "systemctl --user restart ${GLM_FORWARDER} FAILED (f9: surfaced, not swallowed)"
    fi
    sleep "${SEAT_RESTART_WAIT:-5}"
    code=$(probe)
    VIA=forwarder-restart
  else
    log "skipping forwarder restart: systemctl --user unavailable in this context (f9)"
  fi
fi

# -- (e) final verdict ----------------------------------------------------------
if [ "$code" = "200" ]; then
  emit up "$VIA" "$SSH_EXIT"
  exit 0
else
  emit failed "$VIA" "$SSH_EXIT"
  exit 1
fi
