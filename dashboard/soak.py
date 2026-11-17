"""dashboard/soak.py — Layer 6 SOAK: time-compressed durability detection.

Ray's gap class: accumulation bugs (fd leaks, memory growth, disk creep,
SQLite WAL bloat) scale with REQUEST VOLUME, not wall-clock — a week of
quiet production can hide a leak that no 5-minute smoke ever sees. The soak
layer compresses "days of load" into minutes by AMPLIFYING synthetic traffic
against an isolated stack and monitoring per-request resource slopes.

Time-compression math
---------------------
AERIAL_SOAK_REQUESTS (default 5000) requests == 1 simulated day of load
(hub writes + console reads + ops-agent-shaped polls + seat probes, mixed in
production-like proportions). At concurrency 8 that is roughly 60-120s of
wall-clock per simulated day; the wall-clock budget itself is capped by
gauntlet.sh (AERIAL_GAUNTLET_BUDGET), so the layer can never over-run.

Detection
---------
Every ``sample_every`` requests a sample of (n_requests, fds, rss_kb,
disk_kb, wal_kb) is taken. For each resource a least-squares slope of value
vs n_requests is computed and expressed per 1000 requests; the layer FAILS
if any slope exceeds its regression threshold from the config (see
DEFAULTS / soak_config.json), or if any resource blows an absolute ceiling
(e.g. fds > 90% of the soft limit).

Mutation discipline
-------------------
``run_mutation_check()`` drives a DELIBERATE fd-leak double through the same
sample->slope->threshold pipeline and asserts the detector FAILS it. If the
leak double passes, the detector is not armed and the layer fails — a soak
layer that cannot catch the bug class it exists for is worse than none.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

#: Time-compression + regression thresholds (config file may override).
DEFAULTS: Dict[str, Any] = {
    # 1 simulated day == this many amplified requests.
    "requests_per_day": 5000,
    # How many simulated days a full layer run covers (budget-capped).
    "days": 1,
    "concurrency": 8,
    "sample_every": 250,
    "request_timeout": 10.0,
    # Max tolerated growth PER 1000 REQUESTS (regression thresholds).
    "thresholds": {
        # ~2% of a 1024-fd limit per 1000 requests (today's incident class).
        "fds": 20.0,
        "rss_kb": 10240.0,   # 10 MB per 1000 requests
        "disk_kb": 512.0,    # state dir + logs (EXCESS over data growth)
        "wal_kb": 256.0,     # sqlite WAL growth (EXCESS over data growth)
    },
    # Intentional writes legitimately grow the db/WAL: disk/wal slopes are
    # evaluated on growth EXCESS over (writes x expected bytes per write).
    "expected_per_write_kb": {"disk_kb": 64.0, "wal_kb": 64.0},
    # Absolute ceilings regardless of slope.
    "ceilings": {"fds": 900, "rss_kb": 1_500_000},
}

CONFIG_NAME = "soak_config.json"


def load_config() -> Dict[str, Any]:
    """DEFAULTS overlaid by dashboard/soak_config.json (or
    $AERIAL_SOAK_CONFIG) if present."""
    cfg = json.loads(json.dumps(DEFAULTS))
    path = os.environ.get("AERIAL_SOAK_CONFIG") or os.path.join(HERE, CONFIG_NAME)
    try:
        with open(path) as f:
            overlay = json.load(f)
    except (OSError, ValueError):
        return cfg
    for k, v in overlay.items():
        if k == "thresholds" and isinstance(v, dict):
            cfg["thresholds"].update(v)
        elif k == "ceilings" and isinstance(v, dict):
            cfg["ceilings"].update(v)
        else:
            cfg[k] = v
    return cfg


# ----------------------------------------------------------------- detector

def slope_per_1000(xs: List[float], ys: List[float]) -> Optional[float]:
    """Least-squares slope of ys vs xs, expressed per 1000 units of x."""
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return (sxy / sxx) * 1000.0


def evaluate_series(samples: List[Dict[str, float]], key: str,
                    threshold: float, ceiling: Optional[float] = None,
                    min_samples: int = 4) -> Tuple[bool, Dict[str, Any]]:
    """One resource's regression check.

    samples: [{"n": int, key: float}, ...] in request order.
    Returns (ok, detail). Fails when slope_per_1000 > threshold or the last
    value exceeds `ceiling`; too-few samples => ok with a note (the run was
    too short to regress on — callers enforce min_samples upstream).
    """
    pts = [(s["n"], s[key]) for s in samples if key in s]
    detail: Dict[str, Any] = {"resource": key, "samples": len(pts),
                              "threshold_per_1000": threshold}
    if len(pts) < min_samples:
        detail["note"] = f"only {len(pts)} samples (<{min_samples}) — not judged"
        return True, detail
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    slope = slope_per_1000(xs, ys)
    detail["slope_per_1000"] = round(slope, 4) if slope is not None else None
    detail["first"] = ys[0]
    detail["last"] = ys[-1]
    ok = True
    if slope is not None and slope > threshold:
        ok = False
        detail["reason"] = (f"{key} grows {slope:.1f}/1000 requests "
                            f"(threshold {threshold})")
    if ceiling is not None and ys[-1] > ceiling:
        ok = False
        detail["reason"] = (detail.get("reason", "") +
                            f" {key} hit {ys[-1]} (ceiling {ceiling})").strip()
    return ok, detail


def evaluate_samples(samples: List[Dict[str, float]],
                     config: Dict[str, Any]) -> Tuple[bool, List[Dict[str, Any]]]:
    """All resources at once. min_samples (default 4) guards against
    judging tiny runs.

    disk/wal growth that is explained by intentional writes (writes x
    expected_per_write_kb) is subtracted before the slope is judged —
    data accumulation is not a leak.
    """
    min_samples = max(4, config.get("min_samples", 4))
    epw = config.get("expected_per_write_kb", {})
    adjusted: List[Dict[str, float]] = []
    for s in samples:
        row = dict(s)
        for key, per_write in epw.items():
            if key in row and "n_writes" in s:
                row[key] = row[key] - s["n_writes"] * per_write
        adjusted.append(row)
    samples = adjusted
    ok = True
    details = []
    for key, thr in config["thresholds"].items():
        res_ok, det = evaluate_series(samples, key, thr,
                                      config["ceilings"].get(key),
                                      min_samples=min_samples)
        ok = ok and res_ok
        details.append(det)
    return ok, details


# ------------------------------------------------------------- load targets

class LoadTarget:
    """What the soak load hits. Subclasses provide request() and monitor()."""

    name = "target"

    def request(self) -> None:
        raise NotImplementedError

    def sample(self) -> Dict[str, float]:
        """Resource readings attributable to this target."""
        raise NotImplementedError

    def close(self) -> None:
        pass


class FdLeakDouble(LoadTarget):
    """Mutation-test double: leaks exactly one fd per request.

    Used ONLY by run_mutation_check() — if the slope detector cannot fail
    this, Layer 6 is not armed.
    """
    name = "fd-leak-double"

    def __init__(self, leak_per_request: int = 1):
        self.leak = leak_per_request
        self._handles: List[Any] = []
        self._n = 0
        self._lock = threading.Lock()
        self._base = self._fd_count()

    @staticmethod
    def _fd_count() -> int:
        try:
            return len(os.listdir("/proc/self/fd"))
        except OSError:
            return 0

    def request(self) -> None:
        with self._lock:
            for _ in range(self.leak):
                try:
                    self._handles.append(open(os.devnull))
                except OSError:
                    pass
            self._n += 1

    def sample(self) -> Dict[str, float]:
        return {"fds": float(self._fd_count() - self._base),
                "rss_kb": 0.0, "disk_kb": 0.0, "wal_kb": 0.0}

    def close(self) -> None:
        for h in self._handles:
            try:
                h.close()
            except Exception:
                pass
        self._handles = []


def run_samples(target: LoadTarget, n_requests: int, sample_every: int,
                concurrency: int = 4) -> List[Dict[str, float]]:
    """Fire n_requests at target with a thread pool, sampling every
    sample_every requests (the same pipeline the real stack uses)."""
    samples: List[Dict[str, float]] = []
    lock = threading.Lock()
    counter = {"n": 0}
    next_sample = {"at": sample_every}

    def one() -> None:
        target.request()
        with lock:
            counter["n"] += 1

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        while counter["n"] < n_requests:
            batch = min(sample_every, n_requests - counter["n"])
            for _ in range(batch):
                ex.submit(one)
            # drain this batch
            deadline = time.time() + 60
            while counter["n"] < (samples[-1]["n"] if samples else 0) + batch:
                if time.time() > deadline:
                    break
                time.sleep(0.002)
            with lock:
                n = counter["n"]
            s = target.sample()
            s["n"] = float(n)
            samples.append(s)
    return samples


def run_mutation_check(config: Optional[Dict[str, Any]] = None
                       ) -> Tuple[bool, str]:
    """Assert the detector CATCHES a deliberate fd leak (mutation rule).

    Returns (caught, detail). The layer fails when caught is False.
    """
    cfg = config or load_config()
    mut_cfg = {"sample_every": 50, "thresholds": {"fds": 5.0},
               "ceilings": {"fds": 10_000}}
    double = FdLeakDouble()
    try:
        samples = run_samples(double, n_requests=300, sample_every=50,
                              concurrency=4)
        ok, details = evaluate_samples(samples, mut_cfg)
        caught = not ok
        detail = "; ".join(d.get("reason", d.get("note", "clean"))
                           for d in details)
    finally:
        double.close()
    if not caught:
        return False, ("MUTATION NOT CAUGHT: the fd-leak double passed the "
                       f"slope detector ({detail}) — Layer 6 not armed")
    return True, f"fd-leak double caught: {detail}"


# ------------------------------------------------------- isolated stack boot

class IsolatedStack:
    """A scratch hub (+ console) with its own state dir, ports and token.

    Same isolation pattern as the demo stack: isolated TICKET_HUB_HOME,
    scratch db, disposable token, nothing touches production stores.
    """

    def __init__(self, workdir: str, hub_port: int = 0, console_port: int = 0,
                 boot_console: bool = True):
        self.workdir = Path(workdir)
        self.state = self.workdir / "state"
        self.hub_home = self.workdir / "hub-home"
        for d in (self.state, self.hub_home, self.state / "logs"):
            d.mkdir(parents=True, exist_ok=True)
        self.hub_port = hub_port or _free_port()
        self.console_port = console_port or _free_port()
        self.db = self.hub_home / "hub.db"
        self.token = os.urandom(24).hex()
        self.token_file = self.state / "hub-token"
        self.token_file.write_text(self.token)
        os.chmod(self.token_file, 0o600)
        self.procs: List[subprocess.Popen] = []
        self.boot_console = boot_console
        self.console_pid: Optional[int] = None
        self.n_writes = 0

    @property
    def hub_url(self) -> str:
        return f"http://127.0.0.1:{self.hub_port}"

    @property
    def console_url(self) -> str:
        return f"http://127.0.0.1:{self.console_port}"

    def start(self, timeout: float = 30.0) -> None:
        env = dict(os.environ)
        env.update({
            "TICKET_HUB_PORT": str(self.hub_port),
            "TICKET_HUB_HOST": "127.0.0.1",
            "TICKET_HUB_HOME": str(self.hub_home),
            "TICKET_HUB_DB": str(self.db),
            "TICKET_HUB_AUTH": "off",        # loopback scratch stack
        })
        hub = subprocess.Popen(
            [sys.executable, "hub/api.py"], cwd=ROOT, env=env,
            stdout=open(self.state / "logs" / "hub.log", "ab"),
            stderr=subprocess.STDOUT)
        self.procs.append(hub)
        if self.boot_console:
            cenv = dict(os.environ)
            cenv.update({"HOME": str(self.workdir),
                         "PYTHONPATH": f"{ROOT}:{ROOT}/dashboard"})
            console = subprocess.Popen(
                [sys.executable, "dashboard/serve_console.py",
                 "--port", str(self.console_port), "--host", "127.0.0.1",
                 "--hub-url", self.hub_url,
                 "--hub-token-file", str(self.token_file)],
                cwd=ROOT, env=cenv,
                stdout=open(self.state / "logs" / "console.log", "ab"),
                stderr=subprocess.STDOUT)
            self.procs.append(console)
            self.console_pid = console.pid
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(self.hub_url + "/health",
                                            timeout=2) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(0.2)
        else:
            self.stop()
            raise RuntimeError("isolated stack did not come up")

    def stop(self) -> None:
        for p in self.procs:
            try:
                p.terminate()
            except Exception:
                pass
        for p in self.procs:
            try:
                p.wait(timeout=10)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        self.procs = []

    # ---- request mix -------------------------------------------------------
    def hub_write(self) -> int:
        body = json.dumps({
            "title": f"soak-load-{time.time_ns()}",
            "body": "amplified synthetic ticket (soak layer)",
            "tags": ["soak"], "actor": "soak"}).encode()
        req = urllib.request.Request(self.hub_url + "/tickets", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            self.n_writes += 1
            return r.status

    def console_get(self, path: str = "/dashboard/health") -> int:
        req = urllib.request.Request(
            self.console_url + path,
            headers={"Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def hub_read(self) -> int:
        with urllib.request.urlopen(self.hub_url + "/tickets?limit=5",
                                    timeout=10) as r:
            return r.status

    def seat_probe(self, port: int = 18199) -> Optional[int]:
        """Ops-agent-shaped seat probe against a dead seat (fails closed)."""
        req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/models",
                                     headers={"Authorization":
                                              f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=2) as r:
                return r.status
        except Exception:
            return None

    # ---- monitoring --------------------------------------------------------
    def pids(self) -> List[int]:
        return [p.pid for p in self.procs]

    def sample(self) -> Dict[str, float]:
        pids = self.pids()
        fds = 0
        rss = 0.0
        for pid in pids:
            try:
                fds += len(os.listdir(f"/proc/{pid}/fd"))
            except OSError:
                pass
            try:
                with open(f"/proc/{pid}/status") as f:
                    for ln in f:
                        if ln.startswith("VmRSS:"):
                            rss += float(ln.split()[1])
                            break
            except OSError:
                pass
        disk = _tree_bytes(self.state) + _tree_bytes(self.hub_home)
        wal = 0.0
        for suffix in ("-wal", "-shm"):
            p = Path(str(self.db) + suffix)
            if p.exists():
                wal += p.stat().st_size
        return {"fds": float(fds), "rss_kb": rss,
                "disk_kb": disk / 1024.0, "wal_kb": wal / 1024.0,
                "n_writes": float(self.n_writes)}


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _tree_bytes(path: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for fn in files:
            try:
                total += (Path(dirpath) / fn).stat().st_size
            except OSError:
                pass
    return total


# ----------------------------------------------------------------- the layer

def run_soak(workdir: str, config: Optional[Dict[str, Any]] = None,
             stack: Optional[IsolatedStack] = None) -> Dict[str, Any]:
    """Layer 6 body. Returns {"ok", "lines", "data"} (gauntlet wraps it in a
    LayerResult).

    1. mutation check (detector must be armed — a leak double must FAIL)
    2. boot isolated stack, amplify AERIAL_SOAK_REQUESTS x days of load
    3. sample every sample_every requests, evaluate slopes vs thresholds
    """
    cfg = config or load_config()
    lines: List[str] = []
    data: Dict[str, Any] = {"config": {k: cfg[k] for k in
                                       ("requests_per_day", "days",
                                        "concurrency", "sample_every")}}
    ok = True

    caught, mdetail = run_mutation_check(cfg)
    lines.append(f"- mutation check (fd-leak double): "
                 f"{'CAUGHT' if caught else 'NOT CAUGHT — detector not armed'}"
                 f" → {'PASS' if caught else 'FAIL'}")
    lines.append(f"    {mdetail}")
    ok = ok and caught

    total = int(cfg["requests_per_day"] * cfg["days"])
    own_stack = stack is None
    if own_stack:
        stack = IsolatedStack(workdir)
        try:
            stack.start()
        except Exception as exc:
            return {"ok": False, "lines": lines + [f"- stack boot failed: {exc}"],
                    "data": data}
    try:
        mix = _mix(stack)
        samples: List[Dict[str, float]] = []
        counter = {"n": 0}
        lock = threading.Lock()
        errors = {"n": 0}

        def one() -> None:
            try:
                mix()
            except Exception:
                with lock:
                    errors["n"] += 1
            with lock:
                counter["n"] += 1

        t0 = time.time()
        with ThreadPoolExecutor(max_workers=cfg["concurrency"]) as ex:
            next_at = cfg["sample_every"]
            while counter["n"] < total:
                batch = min(cfg["sample_every"], total - counter["n"])
                for _ in range(batch):
                    ex.submit(one)
                deadline = time.time() + 120
                while counter["n"] < next_at and time.time() < deadline:
                    time.sleep(0.005)
                s = stack.sample()
                with lock:
                    s["n"] = float(counter["n"])
                samples.append(s)
                next_at += cfg["sample_every"]
        wall = time.time() - t0

        data["requests"] = int(samples[-1]["n"]) if samples else 0
        data["wall_s"] = round(wall, 1)
        data["errors"] = errors["n"]
        data["samples"] = len(samples)
        sim_days = data["requests"] / cfg["requests_per_day"]
        lines.append(f"- amplified load: {data['requests']} requests "
                     f"(≈ {sim_days:.2f} simulated day(s) at "
                     f"{cfg['requests_per_day']}/day) in {wall:.0f}s "
                     f"wall-clock, concurrency {cfg['concurrency']}, "
                     f"{errors['n']} request errors")

        ok_eval, details = evaluate_samples(samples, cfg)
        for d in details:
            status = "PASS" if d.get("note") or d["slope_per_1000"] is None or \
                d["slope_per_1000"] <= d["threshold_per_1000"] else "FAIL"
            if "reason" in d:
                status = "FAIL"
            lines.append(f"- {d['resource']}: slope {d.get('slope_per_1000')} "
                         f"/1000 req (threshold {d['threshold_per_1000']}), "
                         f"{d['first']}→{d['last']} over {d['samples']} "
                         f"samples → {status}")
            if "reason" in d:
                lines.append(f"    {d['reason']}")
        ok = ok and ok_eval
        data["slopes"] = details
        data["verdict"] = "PASS" if ok else "FAIL"
    finally:
        if own_stack:
            stack.stop()
    return {"ok": ok, "lines": lines, "data": data}


def _mix(stack: IsolatedStack) -> Callable[[], None]:
    """Production-shaped request mix: hub writes, console reads, ops-agent
    polls (hub list reads), seat probes."""
    import random
    rng = random.Random(20261117)

    def req() -> None:
        r = rng.random()
        if r < 0.35:
            stack.hub_write()
        elif r < 0.70:
            stack.console_get("/dashboard/health")
        elif r < 0.80:
            stack.console_get("/dashboard")
        elif r < 0.95:
            stack.hub_read()
        else:
            stack.seat_probe()

    return req
