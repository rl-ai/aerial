#!/usr/bin/env python3
"""Aerial VRAM Manager — model-weight paging substrate.

Models run on VRAM by default and displace idle VRAM; swapping is built in.
This is platform substrate (OS-virtual-memory-for-model-weights), not an agent
decision: the agent only states demand (`vram-ctl load <model>`) or relies on
idle eviction. See docs/vram-manager.md in this workspace.

Pieces:
  * Registry   — vram_registry.json (seats) + vram_state.json (learned state)
  * Pager      --daemon: 5-min tick, flock guard, journal (job_run pattern)
  * vram-ctl   — CLI: list / status / load / displace

Hermetic by construction: every host touchpoint (nvidia-smi, systemctl,
socket probes, clock) goes through an injectable `Backend`. Tests swap in a
FakeBackend; the daemon runs the RealBackend unmodified.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Optional

FAST = "FAST"
SLOW = "SLOW"
TIER_ORDER = {"C": 0, "B": 1, "A": 2}  # lowest priority first

DEMAND_IDLE_S = 15 * 60        # no requests for 15 min -> demand-displaceable
IDLE_EVICT_S = 30 * 60         # no requests for 30 min -> pressure-evictable
TICK_S = 5 * 60                # daemon tick

ROOT = Path(__file__).resolve().parent
REGISTRY_PATH = ROOT / "vram_registry.json"
STATE_PATH = ROOT / "vram_state.json"
LOCK_PATH = ROOT / "vram_manager.lock"
JOURNAL_PATH = ROOT / "vram_journal.log"


# --------------------------------------------------------------------------
# Model registry
# --------------------------------------------------------------------------

@dataclass
class Seat:
    name: str
    port: int
    reload_class: str            # FAST | SLOW
    tier: str                    # A | B | C
    never_displace: bool = False
    service_unit: str = ""
    load_cmd: str = ""
    vram_gib: float = 0.0        # measured at load; 0 = not resident
    total_layers: int = 0        # llama.cpp seats: for -ngl math
    full_gib: float = 0.0        # full-weight footprint (llama.cpp partial offload)
    # learned state (state file)
    resident: bool = False
    last_request_ts: float = 0.0
    inflight: bool = False
    displaced_at: Optional[float] = None

    def idle_s(self, now: float) -> float:
        return max(0.0, now - self.last_request_ts)

    def is_idle_for_demand(self, now: float) -> bool:
        return not self.inflight and self.idle_s(now) >= DEMAND_IDLE_S

    def is_idle_for_evict(self, now: float) -> bool:
        return not self.inflight and self.idle_s(now) >= IDLE_EVICT_S

    def to_dict(self) -> dict:
        return asdict(self)


def load_registry(path: Path = REGISTRY_PATH) -> list[Seat]:
    cfg = json.loads(path.read_text())
    seats = [Seat(**s) for s in cfg["seats"]]
    state_path = Path(cfg.get("state_path", STATE_PATH))
    if state_path.exists():
        state = json.loads(state_path.read_text())
        for s in seats:
            st = state.get(s.name, {})
            for k in ("vram_gib", "resident", "last_request_ts",
                      "inflight", "displaced_at"):
                if k in st:
                    setattr(s, k, st[k])
    return seats


def save_state(seats: list[Seat],
               path: Path = None) -> Path:
    cfg = json.loads(REGISTRY_PATH.read_text())
    path = Path(cfg.get("state_path", STATE_PATH))
    path.write_text(json.dumps(
        {s.name: {k: s.__dict__[k] for k in
                  ("vram_gib", "resident", "last_request_ts",
                   "inflight", "displaced_at")} for s in seats},
        indent=2))
    return path


# --------------------------------------------------------------------------
# Backend — every host touchpoint, injectable for tests
# --------------------------------------------------------------------------

class RealBackend:
    @staticmethod
    def now() -> float:
        return time.time()

    def gpu_free_gib(self) -> float:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, check=True).stdout
        mibs = [float(x.strip()) for x in out.strip().splitlines() if x.strip()]
        return sum(mibs) / 1024.0

    def active_connections(self, port: int) -> int:
        """Requests are in flight if any TCP connection is established."""
        try:
            out = subprocess.run(["ss", "-tn", f"sport = :{port}"],
                                 capture_output=True, text=True).stdout
        except FileNotFoundError:
            return 0
        return sum(1 for l in out.splitlines() if "ESTAB" in l)

    def systemctl(self, action: str, unit: str) -> None:
        subprocess.run(["systemctl", "--user", action, unit], check=True)

    def run(self, cmd: str) -> None:
        subprocess.run(cmd, shell=True, check=True)


@dataclass
class FakeBackend:
    """Test double: dict-driven GPU + connection + service state."""
    free_gib: float = 40.0
    connections: dict = field(default_factory=dict)   # port -> n
    stopped: list = field(default_factory=list)
    started: list = field(default_factory=list)
    freed_on_stop: dict = field(default_factory=dict)  # unit -> gib released
    cost_on_start: dict = field(default_factory=dict)  # unit -> gib consumed
    clock: float = 1_000_000.0

    def now(self) -> float:
        return self.clock

    def gpu_free_gib(self) -> float:
        return self.free_gib

    def active_connections(self, port: int) -> int:
        return self.connections.get(port, 0)

    def systemctl(self, action: str, unit: str) -> None:
        if action == "stop":
            self.stopped.append(unit)
            self.free_gib += self.freed_on_stop.get(unit, 0.0)
        elif action == "start":
            self.started.append(unit)
            self.free_gib -= self.cost_on_start.get(unit, 0.0)

    def run(self, cmd: str) -> None:
        pass

    def advance(self, seconds: float) -> None:
        self.clock += seconds


# --------------------------------------------------------------------------
# Pager core
# --------------------------------------------------------------------------

class VramManager:
    def __init__(self, seats: list[Seat], backend, journal: Callable = print):
        self.seats = seats
        self.backend = backend
        self.journal = journal
        self.events: list[dict] = []

    # -- helpers -----------------------------------------------------------

    def by_name(self, name: str) -> Optional[Seat]:
        return next((s for s in self.seats if s.name == name), None)

    def refresh(self) -> None:
        """Reconcile live state: connections, residency."""
        now = self.backend.now()
        for s in self.seats:
            conns = self.backend.active_connections(s.port)
            s.inflight = conns > 0
            if s.inflight:
                s.last_request_ts = now
        save_state(self.seats)

    def free_gib(self) -> float:
        return self.backend.gpu_free_gib()

    def victims(self, exclude: Optional[str] = None) -> list[Seat]:
        """Demand-displaceable residents, best victim first.

        Order: FAST before SLOW, lowest tier first, largest footprint first.
        Skips never_displace, in-flight, and not-yet-idle (15 min) seats.
        """
        cands = [s for s in self.seats
                 if s.resident and not s.never_displace
                 and s.name != exclude and s.is_idle_for_demand(self.backend.now())]
        cands.sort(key=lambda s: (0 if s.reload_class == FAST else 1,
                                  TIER_ORDER[s.tier], -s.vram_gib))
        return cands

    def evictable(self) -> list[Seat]:
        """Pressure-evictable residents (30-min idle), same victim order."""
        cands = [s for s in self.seats
                 if s.resident and not s.never_displace
                 and s.is_idle_for_evict(self.backend.now())]
        cands.sort(key=lambda s: (0 if s.reload_class == FAST else 1,
                                  TIER_ORDER[s.tier], -s.vram_gib))
        return cands

    # -- actions -------------------------------------------------------------

    def displace(self, seat: Seat, reason: str) -> float:
        """Page a seat's weights out. Returns GiB freed."""
        if seat.never_displace:
            raise PermissionError(f"{seat.name} is never_displace")
        if seat.inflight:
            raise RuntimeError(f"{seat.name} has an in-flight request")
        freed = seat.vram_gib
        self.backend.systemctl("stop", seat.service_unit)
        seat.resident = False
        seat.displaced_at = self.backend.now()
        self._journal("displace", seat=seat.name, freed_gib=round(freed, 2),
                      reason=reason, unit=seat.service_unit)
        save_state(self.seats)
        return freed

    def compute_ngl(self, seat: Seat, avail_gib: float) -> int:
        """llama.cpp -ngl from available VRAM (proportional to full footprint)."""
        if seat.total_layers <= 0:
            return 0
        full = seat.full_gib or seat.vram_gib
        if full <= 0:
            return seat.total_layers
        frac = max(0.0, min(1.0, avail_gib / full))
        return int(seat.total_layers * frac)

    def load_need_gib(self, seat: Seat) -> float:
        """VRAM the demand-load must secure before starting the seat.

        FAST (llama.cpp) seats partially offload: they take whatever free VRAM
        exists (-ngl computed from it), so they need 0 displacement.
        SLOW (vLLM) seats need their full measured footprint resident.
        """
        if seat.reload_class == FAST and seat.total_layers > 0:
            return 0.0
        return seat.full_gib or seat.vram_gib

    def load(self, name: str, need_gib: Optional[float] = None) -> dict:
        """Demand-load: displace idle FAST-first victims until space frees,
        then start the seat. Never displaces Tier A standing / in-flight."""
        seat = self.by_name(name)
        if seat is None:
            raise KeyError(f"unknown model {name}")
        if seat.resident:
            return {"status": "resident", "model": name}
        need = need_gib if need_gib is not None else self.load_need_gib(seat)
        displaced: list[str] = []
        while need > 0 and self.free_gib() < need:
            v = self.victims(exclude=name)
            if not v:
                raise RuntimeError(
                    f"cannot free {need:.1f} GiB for {name}: no idle victims")
            freed = self.displace(v[0], reason=f"demand-load {name}")
            displaced.append(v[0].name)
        if seat.load_cmd:
            self.backend.run(seat.load_cmd)
        self.backend.systemctl("start", seat.service_unit)
        seat.resident = True
        seat.displaced_at = None
        seat.last_request_ts = self.backend.now()
        self._journal("load", model=name, displaced=displaced,
                      unit=seat.service_unit)
        save_state(self.seats)
        return {"status": "loaded", "model": name, "displaced": displaced}

    def tick(self, pressure_floor_gib: float = 8.0) -> list[str]:
        """One pager tick: idle eviction under VRAM pressure."""
        self.refresh()
        evicted = []
        if self.free_gib() < pressure_floor_gib:
            for s in self.evictable():
                if self.free_gib() >= pressure_floor_gib:
                    break
                self.displace(s, reason="idle-pressure")
                evicted.append(s.name)
        return evicted

    def _journal(self, kind: str, **fields) -> None:
        rec = {"ts": time.time(), "kind": kind, **fields}
        self.events.append(rec)
        self.journal(json.dumps(rec, sort_keys=True))


# --------------------------------------------------------------------------
# Daemon + CLI
# --------------------------------------------------------------------------

def run_daemon(tick_s: int = TICK_S, once: bool = False) -> None:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_fh = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("vram-manager: another pager holds the lock; exiting",
              file=sys.stderr)
        return 1

    def journal(line: str) -> None:
        with open(JOURNAL_PATH, "a") as fh:
            fh.write(line + "\n")

    cfg = json.loads(REGISTRY_PATH.read_text())
    while True:
        seats = load_registry()
        mgr = VramManager(seats, RealBackend(), journal=journal)
        evicted = mgr.tick(pressure_floor_gib=cfg.get("pressure_floor_gib", 8.0))
        if evicted:
            journal(json.dumps({"ts": time.time(), "kind": "tick",
                                "evicted": evicted}))
        if once:
            break
        time.sleep(tick_s)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="vram-ctl")
    ap.add_argument("cmd", choices=["list", "status", "load", "displace"])
    ap.add_argument("model", nargs="?")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    seats = load_registry()
    backend = RealBackend()
    mgr = VramManager(seats, backend)

    if args.cmd == "list":
        for s in seats:
            print(f"{s.name:8s} port={s.port:5d} {s.reload_class:4s} "
                  f"tier={s.tier} resident={s.resident} "
                  f"vram={s.vram_gib:.1f}GiB never={s.never_displace}")
        return 0

    if args.cmd == "status":
        print(f"free VRAM: {mgr.free_gib():.1f} GiB")
        for s in seats:
            idle = f"{s.idle_s(backend.now())/60:.0f}m" if s.last_request_ts else "n/a"
            print(f"{s.name:8s} resident={s.resident} inflight={s.inflight} "
                  f"idle={idle} displaced_at={s.displaced_at}")
        return 0

    if not args.model:
        ap.error(f"{args.cmd} requires a model name")
    if args.cmd == "displace":
        s = mgr.by_name(args.model)
        if s is None:
            print(f"unknown model {args.model}", file=sys.stderr)
            return 2
        if args.dry_run:
            print(f"[dry-run] would displace {s.name} "
                  f"({s.vram_gib:.1f} GiB, {s.reload_class})")
            return 0
        freed = mgr.displace(s, reason="manual vram-ctl displace")
        print(f"displaced {s.name}; freed {freed:.1f} GiB")
        return 0

    # load
    if args.dry_run:
        s = mgr.by_name(args.model)
        plan = [v.name for v in mgr.victims()]
        print(f"[dry-run] load {args.model}: victims in order = {plan}")
        return 0
    print(mgr.load(args.model))
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
