"""Runtime-context probe for Aerial ops agents — READ-ONLY.

`python3 -m dashboard.ops_context` prints one JSON object describing the
live runtime so a dispatched fix/optimization agent can PROBE FIRST, then
decide within its mandate (docs/runtime-optimization.md):

  gpus          free/total VRAM per GPU (nvidia-smi)
  seats         listening llama-server / vLLM ports: model alias, VRAM held,
                owning PID (from `ss` + /proc)
  processes     bare model/service processes vs systemd user units
                (pgrep + `systemctl --user list-units`)
  model_shards  per model dir: shard count vs the `-of-NNN` in the filename
                (the 2026-11-13 Kimi outage: 2/17 shards = unusable)
  cron          the crontab surface (entries the schedule reconciler checks)

Everything runs through injectable `_run` / file-reading helpers so tests
are fully hermetic (mocked nvidia-smi / ss / systemctl / pgrep output).
The probe NEVER mutates state: it only reads and queries.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any, Callable, Dict, List, Optional

from deployment_profile import pvalue as _pv
DEFAULT_MODELS_ROOT = str(_pv("models_root"))
DEFAULT_CRONTAB_CMD = "crontab -l"
DEFAULT_SYSTEMCTL_CMD = ["systemctl", "--user", "list-units",
                         "--type=service", "--all", "--no-legend"]
DEFAULT_SS_CMD = ["ss", "-tlnp"]
DEFAULT_NVIDIA_SMI_CMD = ["nvidia-smi",
                          "--query-gpu=index,name,memory.used,memory.total",
                          "--format=csv,noheader,nounits"]
DEFAULT_PGREP_CMD = ["pgrep", "-a", "-f",
                     "llama-server|vllm|llama.cpp|python.*serve"]


def _run(cmd: List[str], timeout: float = 10.0) -> str:
    """Run a read-only command, return stdout ('' on any failure)."""
    try:
        proc = subprocess.run(cmd, timeout=timeout,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              text=True)
        return proc.stdout or ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# GPUs
# ---------------------------------------------------------------------------

def parse_nvidia_smi(out: str) -> List[Dict]:
    """`index,name,memory.used,memory.total` CSV → per-GPU dicts."""
    gpus = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        try:
            gpus.append({
                "index": int(parts[0]),
                "name": parts[1],
                "vram_used_mib": int(parts[2]),
                "vram_total_mib": int(parts[3]),
                "vram_free_mib": int(parts[3]) - int(parts[2]),
            })
        except ValueError:
            continue
    return gpus


def probe_gpus(cmd: List[str] = DEFAULT_NVIDIA_SMI_CMD,
               run: Callable = _run) -> List[Dict]:
    return parse_nvidia_smi(run(cmd))


# ---------------------------------------------------------------------------
# Seat map: listening ports -> model -> VRAM held -> owner
# ---------------------------------------------------------------------------

_SS_PORT_RE = re.compile(r":(\d+)\s+.*?users:\(\(\"([^\"]+)\".*?pid=(\d+)")
_CMDLINE_VRAM_RE = re.compile(r"--?(?:n-gpu-layers|ngl)\s+(\d+)")
_CTX_RE = re.compile(r"(?:--ctx-size|-c)\s+(\d+)")
_PARALLEL_RE = re.compile(r"--parallel\s+(\d+)")
_ALIAS_RE = re.compile(r"--alias\s+(\S+)")
_MODEL_PATH_RE = re.compile(r"(?:--model|-m)\s+(\S+)")


def parse_ss(out: str) -> List[Dict]:
    """`ss -tlnp` → listeners with process name+pid where visible."""
    listeners = []
    for line in out.splitlines():
        m = _SS_PORT_RE.search(line)
        if m:
            listeners.append({"port": int(m.group(1)),
                              "process": m.group(2), "pid": int(m.group(3))})
    return listeners


def read_proc_cmdline(pid: int, reader: Callable[[str], bytes]) -> Optional[str]:
    try:
        raw = reader(f"/proc/{pid}/cmdline")
        return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except Exception:
        return None


def _nvidia_proc_vram(pid: int, run: Callable = _run) -> int:
    """VRAM (MiB) held by pid, via `nvidia-smi --query-compute-apps`."""
    out = run(["nvidia-smi", "--query-compute-apps=pid,used_memory",
               "--format=csv,noheader,nounits"])
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 2 and parts[0] == str(pid):
            try:
                return int(parts[1])
            except ValueError:
                pass
    return 0


def _read_cgroup(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cgroup") as f:
            return f.read()
    except Exception:
        return ""


def probe_seats(ss_cmd: List[str] = DEFAULT_SS_CMD,
                run: Callable = _run,
                cmdline_reader: Callable[[int], Optional[str]] =
                lambda pid: read_proc_cmdline(pid, _read_proc_file),
                vram_for: Callable[[int], int] = _nvidia_proc_vram,
                cgroup_reader: Callable[[int], str] = _read_cgroup,
                models_root: str = DEFAULT_MODELS_ROOT) -> List[Dict]:
    """Listening model-server seats: port, process, pid, cmdline facts,
    VRAM held. Only llama-server / vLLM-ish ports are reported."""
    seats = []
    for l in parse_ss(run(ss_cmd)):
        if l["process"] not in ("llama-server", "vllm", "llama"):
            if not (18000 <= l["port"] <= 18999):
                continue
        cmd = cmdline_reader(l["pid"]) or ""
        def _find(rx, cast: Callable[[str], Any] = int):
            m = rx.search(cmd)
            return cast(m.group(1)) if m else None

        seat = {
            "port": l["port"],
            "process": l["process"],
            "pid": l["pid"],
            "alias": _find(_ALIAS_RE, str),
            "model_path": _find(_MODEL_PATH_RE, str),
            "ngl": _find(_CMDLINE_VRAM_RE),
            "ctx": _find(_CTX_RE),
            "parallel": _find(_PARALLEL_RE),
            "vram_used_mib": vram_for(l["pid"]),
            "bare_process": not _pid_has_unit_cgroup(cgroup_reader(l["pid"])),
        }
        seats.append(seat)
    return seats


def _read_proc_file(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _pid_has_unit_cgroup(cgroup: str) -> bool:
    return bool(re.search(r"/user@\d+\.service/.*\.service", cgroup))


def _pid_in_systemd_unit(pid: int, run: Callable = _run) -> bool:
    """A pid belongs to a systemd user unit iff its cgroup contains
    `/user.slice/user-*.slice/user@*.service/app.slice/...<unit>.service`."""
    return _pid_has_unit_cgroup(_read_cgroup(pid))


# ---------------------------------------------------------------------------
# Bare processes vs systemd user units
# ---------------------------------------------------------------------------

def probe_process_units(pgrep_cmd: List[str] = DEFAULT_PGREP_CMD,
                        systemctl_cmd: List[str] = DEFAULT_SYSTEMCTL_CMD,
                        run: Callable = _run,
                        cgroup_reader: Callable[[int], str] =
                        lambda pid: _read_cgroup(pid)) -> Dict:
    """Long-lived processes split into `bare` (no systemd unit — the
    reboot-casualty pattern) and `unit` (managed)."""
    procs = []
    for line in run(pgrep_cmd).splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            procs.append((int(parts[0]), parts[1]))
    units_out = run(systemctl_cmd)
    active_units = set()
    for line in units_out.splitlines():
        parts = line.split()
        # `systemctl --user list-units --no-legend`:
        # UNIT LOAD ACTIVE SUB DESCRIPTION
        if len(parts) >= 4 and parts[2] == "active" and parts[3] in (
                "running", "exited", "listening"):
            active_units.add(parts[0])
    bare, unit = [], []
    for pid, cmd in procs:
        cg = cgroup_reader(pid)
        entry = {"pid": pid, "cmdline": cmd[:300],
                 "bare": not bool(re.search(r"\.service", cg))}
        (bare if entry["bare"] else unit).append(entry)
    return {"bare": bare, "unit": unit, "active_units": sorted(active_units)}


# ---------------------------------------------------------------------------
# Model shard completeness
# ---------------------------------------------------------------------------

_SHARD_RE = re.compile(r"-(\d{5})-of-(\d{5})\.gguf$", re.IGNORECASE)


def parse_shard_state(filenames: List[str]) -> Dict:
    """Given *.gguf filenames in ONE model dir, decide completeness.

    Two shapes: a single monolithic file (no -of- suffix → complete, 1/1)
    or sharded `-NNNNN-of-NNNNN.gguf` files (complete iff the set of seen
    indexes == the declared total).
    """
    sharded = []
    mono = []
    for name in filenames:
        m = _SHARD_RE.search(name)
        if m:
            sharded.append((int(m.group(1)), int(m.group(2)), name))
        elif name.lower().endswith(".gguf"):
            mono.append(name)
    if sharded:
        total = sharded[0][1]
        seen = {i for i, _t, _n in sharded}
        return {"sharded": True, "shards_present": len(seen),
                "shards_total": total,
                "complete": seen == set(range(1, total + 1)),
                "missing": sorted(set(range(1, total + 1)) - seen)[:5]}
    if mono:
        return {"sharded": False, "shards_present": 1, "shards_total": 1,
                "complete": True, "missing": []}
    return {"sharded": None, "shards_present": 0, "shards_total": 0,
            "complete": False, "missing": []}


def probe_model_shards(models_root: str = DEFAULT_MODELS_ROOT,
                       listdir: Callable[[str], List[str]] = os.listdir,
                       isdir: Callable[[str], bool] = os.path.isdir) -> List[Dict]:
    """Per model dir (and one level of variant subdirs): shard completeness."""
    out = []
    try:
        names = sorted(listdir(models_root))
    except OSError:
        return out
    for name in names:
        path = os.path.join(models_root, name)
        if not isdir(path):
            continue
        subdirs = []
        try:
            subdirs = [d for d in sorted(listdir(path))
                       if isdir(os.path.join(path, d))]
        except OSError:
            pass
        variants = {"": parse_shard_state(_ggufs(path, listdir))}
        for d in subdirs:
            variants[d] = parse_shard_state(_ggufs(os.path.join(path, d),
                                                   listdir))
        complete = all(v["complete"] for v in variants.values()
                       if v["shards_present"] > 0) and any(
            v["shards_present"] > 0 for v in variants.values())
        out.append({"model": name, "path": path, "variants": variants,
                    "complete": complete})
    return out


def _ggufs(path: str, listdir: Callable[[str], List[str]]) -> List[str]:
    try:
        return [f for f in listdir(path) if f.lower().endswith(".gguf")]
    except OSError:
        return []


# ---------------------------------------------------------------------------
# Cron surface
# ---------------------------------------------------------------------------

def probe_cron(cmd: str = DEFAULT_CRONTAB_CMD,
               run_shell: Callable[[str], str] =
               lambda c: _run(["/bin/sh", "-c", c])) -> Dict:
    out = run_shell(cmd)
    entries = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        entries.append(line)
    return {"entries": entries, "count": len(entries)}


# ---------------------------------------------------------------------------
# Pure decision rules (docs/runtime-optimization.md)
# ---------------------------------------------------------------------------

SAFETY_MARGIN = 0.9   # use at most 90% of free VRAM


def ngl_for_free_vram(vram_free_mib: int, bytes_per_layer: float,
                      margin: float = SAFETY_MARGIN) -> int:
    """Kimi-lesson rule: ngl = floor(free VRAM * margin / weights-per-layer).

    `bytes_per_layer` = model weight bytes per layer (from the shard sizes:
    total_weights_bytes / n_layers). 0 free VRAM or unknown layer size → 0
    (CPU-only, the 2026-11-13 degraded mode).
    """
    if bytes_per_layer <= 0 or vram_free_mib <= 0:
        return 0
    return int((vram_free_mib * 1024 * 1024 * margin) // bytes_per_layer)


def pick_seat(gpus: List[Dict], seats: List[Dict], need_mib: int) -> Dict:
    """Seat placement: put a model needing `need_mib` on the GPU with the
    most free VRAM that fits; None if no GPU fits (do NOT evict other
    seats — never touch other seats' processes)."""
    occupied = {s.get("vram_used_mib", 0) for s in seats}
    candidates = []
    for g in gpus:
        free = g["vram_free_mib"]
        if free >= need_mib:
            candidates.append((free, g["index"]))
    if not candidates:
        return {"gpu": None, "reason": "no GPU has %d MiB free" % need_mib,
                "occupied_mib_seen": sorted(occupied)}
    free, idx = max(candidates)
    return {"gpu": idx, "free_mib": free, "reason": "most-free-GPU that fits"}


def context_split(total_ctx: int, parallel: int, input_tokens: int,
                  output_tokens: int) -> Dict:
    """parallel/context split math: per_slot = total/parallel;
    input+output must fit per slot."""
    if parallel < 1:
        return {"ok": False, "reason": "parallel must be >= 1"}
    per_slot = total_ctx // parallel
    fits = (input_tokens + output_tokens) <= per_slot
    return {"parallel": parallel, "total_ctx": total_ctx,
            "per_slot": per_slot, "input_tokens": input_tokens,
            "output_tokens": output_tokens, "fits": fits,
            "reason": ("" if fits else
                       "input+output %d exceeds per-slot ctx %d — raise "
                       "total_ctx or lower parallel"
                       % (input_tokens + output_tokens, per_slot))}


def systemd_conversion_candidates(proc_units: Dict) -> List[Dict]:
    """Bare-process rule: every long-lived bare process should ship a
    systemd user unit (the 2026-11-13/14 reboot-casualty incidents).
    Never propose touching a process that already has a unit."""
    return [{"pid": p["pid"], "cmdline": p["cmdline"],
             "action": "create systemd user unit + enable it, then migrate"}
            for p in proc_units.get("bare", [])]


def monitor_budget_checks(failed_checks: int, total_checks: int,
                          budget_per_cycle: int = 5) -> Dict:
    """Monitor budget calibration: if more than `budget_per_cycle` checks
    are failing, the monitor is spending its whole cycle re-alerting —
    calibrate (group checks, raise budget) instead of pinging per-check."""
    over = failed_checks > budget_per_cycle
    return {"failed": failed_checks, "total": total_checks,
            "budget": budget_per_cycle, "over_budget": over,
            "reason": ("" if not over else
                       "%d failing checks exceeds budget %d — group alerts"
                       % (failed_checks, budget_per_cycle))}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def collect_context(models_root: str = DEFAULT_MODELS_ROOT) -> Dict:
    import time
    ctx = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "gpus": probe_gpus(),
        "seats": probe_seats(models_root=models_root),
        "process_units": probe_process_units(),
        "model_shards": probe_model_shards(models_root=models_root),
        "cron": probe_cron(),
    }
    # attach pure-rule pre-computations the agent can sanity-check
    free = sum(g["vram_free_mib"] for g in ctx["gpus"])
    ctx["summary"] = {
        "vram_free_mib_total": free,
        "bare_processes": len(ctx["process_units"]["bare"]),
        "incomplete_models": [m["model"] for m in ctx["model_shards"]
                              if not m["complete"]],
    }
    return ctx


def main() -> None:
    print(json.dumps(collect_context(), indent=2, default=str))


if __name__ == "__main__":
    main()
