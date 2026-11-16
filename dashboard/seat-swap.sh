#!/usr/bin/env bash
# seat-swap — the Kimi/Qwen swap pair on the Pro 6000 (D2).
#   seat-swap.sh kimi-on   : stop the Qwen container, restart Kimi with GPU offload
#   seat-swap.sh qwen-on   : stop Kimi, restart the Qwen container
#   seat-swap.sh status    : which model holds the seat
# The ops agent's D2 decision calls this; the swap-cost guard (≥30 min
# expected run) is enforced by the caller.
set -u
KIMI_UNIT="${AERIAL_KIMI_SERVICE_UNIT:-aerial-kimi.service}"
QWEN_CONTAINER="${AERIAL_QWEN_CONTAINER:-aerial-qwen}"
KIMI_MODEL="${AERIAL_KIMI_MODEL_PATH:-}"
VENV="${AERIAL_SWAP_VENV:-python3}"
# the swap-pair seat is the Pro 6000 = GPU 1 (GPU 0 holds DS on the Pro 5000)
FREE_GIB=$(nvidia-smi --query-gpu=index,memory.free --format=csv,noheader,nounits \
           | awk -F', ' '$1 == 1 {print int($2/1024)}')

kimi_on() {
  echo "[$(date -Is)] swapping: Qwen out first (frees the VRAM), then measure"
  docker stop "$QWEN_CONTAINER" 2>/dev/null || true
  sleep 3  # the GPU releases the displaced model's VRAM
  # measure AFTER displacement: the freed VRAM is what Kimi can offload into
  FREE_GIB=$(nvidia-smi --query-gpu=index,memory.free --format=csv,noheader,nounits \
             | awk -F', ' '$1 == 1 {print int($2/1024)}')
  # ngl: max layers that fit the freed VRAM (karlex ~662GB total, ~80 layers,
  # ~8.3GB/layer — cap at what the GPU holds with 10% margin)
  NGL=$(( FREE_GIB * 9 / 83 )); [ "$NGL" -gt 40 ] && NGL=40; [ "$NGL" -lt 0 ] && NGL=0
  echo "  freed ${FREE_GIB} GiB -> kimi -ngl $NGL"
  systemctl --user set-environment KIMI_NGL="$NGL"
  systemctl --user restart "$KIMI_UNIT"
  sleep 3
  systemctl --user is-active "$KIMI_UNIT"
}

qwen_on() {
  echo "[$(date -Is)] swapping: Kimi out (CPU-only standby), Qwen onto GPU"
  systemctl --user stop "$KIMI_UNIT" 2>/dev/null || true
  docker start "$QWEN_CONTAINER"
  sleep 3
  docker inspect "$QWEN_CONTAINER" --format '{{.State.Status}}'
}

status() {
  if curl -s -m 3 -o /dev/null http://127.0.0.1:18084/v1/models 2>/dev/null; then
    echo "seat: QWEN (qwen38-fp4 on 18084)"
  elif systemctl --user is-active "$KIMI_UNIT" >/dev/null 2>&1; then
    echo "seat: KIMI ($KIMI_UNIT, ngl ${KIMI_NGL:-0})"
  else
    echo "seat: EMPTY"
  fi
}

case "${1:-status}" in
  kimi-on) kimi_on ;;
  qwen-on) qwen_on ;;
  status)  status ;;
  *) echo "usage: seat-swap.sh {kimi-on|qwen-on|status}"; exit 2 ;;
esac
