#!/usr/bin/env python3
"""idle-broker — fill measured-idle model seats with pre-approved backlog work.

Policy: interactive/production traffic ALWAYS preempts background backlog.
The broker never competes with live traffic — it only dispatches onto seats
that are measured idle (per the seat-usage.db time-series) and only while no
production gate is active.

Idle classification (per seat): the last 2 reachable samples in
seat-usage.db must both have running<=0 AND waiting<=0. A single sample of
activity disqualifies the seat for that pass.

Production gates (ALL must be clear to dispatch anything):
  (a) no seat has waiting>0 in its latest sample (queueing anywhere = pause)
  (b) env AERIAL_SATURATION_PAUSE is unset/empty
  (c) no command for that seat is already running (pidfile in
      ~/.aerial/run/idle-broker/<entry-id>.pid holding a live pid)

Backlog registry: backlog.yaml next to this script (human-editable). Entries:
  {id, seat_match, command, priority, max_concurrent, cooldown_h, enabled}
seat_match is a seat name or "*" for any seat. command may contain "{seat}"
(= the seat name) and "{pidfile}" (= the pidfile path for the entry).

Dispatch: subprocess.Popen(shell command), pid recorded in the pidfile and in
the run-log (~/.aerial/data/idle-broker-runs.jsonl). Cooldowns and last-run
times are derived from the run-log, so state survives restarts.

Usage: broker.py --once | --daemon [--interval S] [--dry-run]
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - yaml is expected on the host
    yaml = None

HERE = os.path.dirname(os.path.abspath(__file__))
RUN_LOG = os.path.expanduser("~/.aerial/data/idle-broker-runs.jsonl")
PID_DIR = os.path.expanduser("~/.aerial/run/idle-broker")
DEFAULT_DB = os.path.expanduser("~/.aerial/data/seat-usage.db")
SAMPLE_INTERVAL_S = 300  # sampler cadence; used to bound "latest" lookback


def usage_db_path():
    env = os.environ.get("SEAT_USAGE_DB")
    if env:
        return os.path.expanduser(env)
    try:
        sys.path.insert(0, os.path.dirname(HERE))  # repo root (monorepo)
        import deployment_profile  # type: ignore
        p = deployment_profile.value("paths.seat_usage_db")
        if p:
            return str(p)
    except Exception:
        pass
    return DEFAULT_DB


# ---------------------------------------------------------------------------
# seat-usage.db reading
# ---------------------------------------------------------------------------

def latest_samples(db_path, lookback_s=SAMPLE_INTERVAL_S * 4 + 60):
    """{seat: [most-recent, ..., oldest]} of reachable samples within lookback."""
    con = sqlite3.connect(db_path)
    try:
        since = time.time() - lookback_s
        rows = con.execute(
            "SELECT ts, seat, running, waiting FROM seat_usage"
            " WHERE reachable=1 AND ts>=? ORDER BY ts DESC", (since,)).fetchall()
    finally:
        con.close()
    out = {}
    for ts, seat, running, waiting in rows:
        out.setdefault(seat, []).append(
            {"ts": ts, "running": running or 0.0, "waiting": waiting or 0.0})
    return out


def seat_idle(samples, seat, n=2):
    """Idle = last n reachable samples all have running<=0 and waiting<=0."""
    rows = samples.get(seat) or []
    if len(rows) < n:
        return False
    return all(r["running"] <= 0 and r["waiting"] <= 0 for r in rows[:n])


def any_seat_waiting(samples):
    """Production gate (a): queue depth > 0 anywhere in the latest samples."""
    return any(r["waiting"] > 0 for rows in samples.values() for r in rows[:1])


# ---------------------------------------------------------------------------
# backlog registry
# ---------------------------------------------------------------------------

def load_backlog(path=None):
    path = path or os.path.join(HERE, "backlog.yaml")
    if yaml is not None:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    else:
        data = _mini_yaml(path)
    entries = []
    for e in data.get("entries", []):
        def _num(v, conv, default):
            if v is None:
                return default
            try:
                return conv(v)
            except (TypeError, ValueError):
                return default

        def _bool(v, default=True):
            if v is None:
                return default
            if isinstance(v, str):
                return v.strip().lower() not in ("false", "no", "0", "")
            return bool(v)
        entries.append({
            "id": str(e["id"]),
            "seat_match": str(e.get("seat_match", "*")),
            "command": str(e["command"]),
            "priority": _num(e.get("priority"), float, 50.0),
            "max_concurrent": _num(e.get("max_concurrent"), int, 1),
            "cooldown_h": _num(e.get("cooldown_h"), float, 0.0),
            "enabled": _bool(e.get("enabled"), True),
        })
    return entries


def _mini_yaml(path):
    """Fallback parser for the simple flat-list backlog.yaml shape."""
    entries, cur = [], None
    with open(path) as f:
        for raw in f:
            line = raw.rstrip()
            if not line.strip() or line.strip().startswith("#"):
                continue
            ind = len(line) - len(line.lstrip())
            s = line.strip()
            if ind == 0 and s.endswith(":"):
                continue
            if s.startswith("- "):
                cur = {}
                entries.append(cur)
                s = s[2:]
            elif entries:
                cur = entries[-1]
            if cur is not None and ":" in s:
                k, v = s.split(":", 1)
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                cur[k.strip()] = v
    return {"entries": entries}


# ---------------------------------------------------------------------------
# run-log / pidfile state
# ---------------------------------------------------------------------------

def load_run_log(path=RUN_LOG):
    """List of run records (newest last): {ts, entry, seat, pid, command}."""
    runs = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    runs.append(json.loads(line))
                except Exception:
                    pass
    except OSError:
        pass
    return runs


def append_run_log(record, path=RUN_LOG):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def pidfile_path(entry_id, dir_=PID_DIR):
    return os.path.join(dir_, f"{entry_id}.pid")


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def running_count(entry, dir_=PID_DIR):
    """Live dispatches for an entry (pidfiles whose pid is still alive)."""
    n = 0
    pf = pidfile_path(entry["id"], dir_)
    try:
        with open(pf) as f:
            pids = [p.strip() for p in f.read().split() if p.strip()]
    except OSError:
        return 0
    alive = []
    for p in pids:
        if pid_alive(p):
            alive.append(p)
            n += 1
    if not alive:
        try:
            os.remove(pf)  # stale pidfile, clean it
        except OSError:
            pass
    elif len(alive) != len(pids):
        with open(pf, "w") as f:
            f.write(" ".join(alive) + "\n")
    return n


def last_dispatch_ts(entry, runs):
    for r in reversed(runs):
        if r.get("entry") == entry["id"] and r.get("pid"):
            return r.get("ts")
    return None


# ---------------------------------------------------------------------------
# broker core
# ---------------------------------------------------------------------------

class Broker:
    def __init__(self, db_path=None, backlog_path=None, run_log=RUN_LOG,
                 pid_dir=PID_DIR, popen=None, clock=time.time):
        self.db_path = db_path or usage_db_path()
        self.backlog_path = backlog_path
        self.run_log = run_log
        self.pid_dir = pid_dir
        self.popen = popen or self._default_popen
        self.clock = clock

    @staticmethod
    def _default_popen(cmd):
        return subprocess.Popen(cmd, shell=True,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                start_new_session=True)

    def decide(self, seats=None, dry_run=False):
        """One pass. Returns (dispatched, decisions) — decisions for logging.

        Each decision: {seat, entry, action, reason}.
        """
        now = self.clock()
        samples = latest_samples(self.db_path)
        seats = seats or sorted(samples.keys())
        pause_env = bool(os.environ.get("AERIAL_SATURATION_PAUSE"))
        global_pause = pause_env or any_seat_waiting(samples)
        runs = load_run_log(self.run_log)
        entries = [e for e in load_backlog(self.backlog_path) if e["enabled"]]
        entries.sort(key=lambda e: -e["priority"])

        dispatched = []
        decisions = []
        for seat in seats:
            if not seat_idle(samples, seat):
                decisions.append({"seat": seat, "action": "skip",
                                  "reason": "busy (last 2 samples active)"})
                continue
            if global_pause:
                decisions.append({"seat": seat, "action": "skip",
                                  "reason": "production gate: "
                                            + ("AERIAL_SATURATION_PAUSE" if pause_env
                                               else "seat waiting>0")})
                continue
            picked = None
            for e in entries:
                if e["seat_match"] not in ("*", seat):
                    continue
                if running_count(e, self.pid_dir) >= e["max_concurrent"]:
                    continue
                last = last_dispatch_ts(e, runs)
                if last is not None and \
                        (now - last) < e["cooldown_h"] * 3600:
                    continue
                picked = e
                break
            if picked is None:
                decisions.append({"seat": seat, "action": "skip",
                                  "reason": "no eligible backlog entry"})
                continue
            cmd = picked["command"].replace("{seat}", seat).replace(
                "{pidfile}", pidfile_path(picked["id"], self.pid_dir))
            decisions.append({"seat": seat, "action": "dispatch",
                              "entry": picked["id"], "command": cmd})
            if not dry_run:
                os.makedirs(self.pid_dir, exist_ok=True)
                proc = self.popen(cmd)
                pid = getattr(proc, "pid", None) or 0
                with open(pidfile_path(picked["id"], self.pid_dir), "a") as f:
                    f.write(f"{pid}\n")
                append_run_log({
                    "ts": now, "entry": picked["id"], "seat": seat,
                    "pid": pid, "command": cmd,
                    "iso": datetime.now(timezone.utc).isoformat(),
                }, self.run_log)
                dispatched.append({"seat": seat, "entry": picked["id"],
                                   "pid": pid})
        return dispatched, decisions

    def once(self, dry_run=False):
        dispatched, decisions = self.decide(dry_run=dry_run)
        for d in decisions:
            print(json.dumps(d, sort_keys=True))
        if not dry_run and dispatched:
            print(f"dispatched {len(dispatched)} job(s)")
        return dispatched

    def daemon(self, interval_s=300, dry_run=False):
        while True:
            try:
                self.once(dry_run=dry_run)
            except Exception as exc:
                print(f"broker pass failed: {exc!r}", file=sys.stderr)
            time.sleep(interval_s)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="idle-work broker")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--once", action="store_true")
    g.add_argument("--daemon", action="store_true")
    ap.add_argument("--interval", type=float, default=300)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    b = Broker()
    if args.once:
        b.once(dry_run=args.dry_run)
    else:
        b.daemon(interval_s=args.interval, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
