#!/usr/bin/env python3
"""Aerial shadow-week scheduler.

Modes:
  --once                 fire a single shadow-hierarchy run (manual trigger)
  --interval MINUTES     loop, firing every MINUTES
  --daily-report         compose a 24h comparison report from exec briefs and
                         POST it as a comment to a hub ticket

No-overlap: a PID lock file guards against concurrent firing; stale locks
(pointing at dead PIDs) are recovered. All side effects (subprocess, comment
POST, sleep) are injectable for hermetic tests.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_SCRIPT = str(__import__("pathlib").Path(__file__).resolve().parent
                     / "run_shadow_hierarchy.py")
from deployment_profile import pvalue as _pv
DEFAULT_SHUTDOWN_DIR = str(_pv("shadow_dir"))
LOCK_NAME = "scheduler.lock"
LOG_NAME = "scheduler.log"
BRIEF_NAME = "brief.md"


# --------------------------------------------------------------------------
# Injectable I/O (tests monkeypatch these module attributes)
# --------------------------------------------------------------------------

run_subprocess = subprocess.run  # (cmd, **kwargs) -> CompletedProcess
post_comment_impl = None  # default built from env; see post_comment()
sleep_impl = time.sleep


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

def _expand(p: str | Path) -> Path:
    return Path(os.path.expanduser(str(p)))


def shutdown_dir() -> Path:
    return _expand(os.environ.get("SHADOW_DIR", DEFAULT_SHUTDOWN_DIR))


def lock_path() -> Path:
    return shutdown_dir() / LOCK_NAME


def log_path() -> Path:
    return shutdown_dir() / LOG_NAME


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

def log(event: str, detail: str = "") -> None:
    d = shutdown_dir()
    d.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{ts} {event}"
    if detail:
        line += f" {detail}"
    with open(log_path(), "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


# --------------------------------------------------------------------------
# Lock file / no-overlap
# --------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def acquire_lock() -> bool:
    """Return True if we hold the lock; False if another run is in flight."""
    d = shutdown_dir()
    d.mkdir(parents=True, exist_ok=True)
    lp = lock_path()
    # Atomic create: O_EXCL prevents two schedulers both claiming the lock.
    try:
        fd = os.open(lp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return _try_stale_recovery(lp)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))
    return True


def _try_stale_recovery(lp: Path) -> bool:
    try:
        raw = lp.read_text(encoding="utf-8").strip()
        pid = int(raw.split()[0]) if raw else -1
    except (ValueError, OSError):
        pid = -1
    if pid > 0 and _pid_alive(pid):
        return False  # genuinely in flight
    # Stale lock (dead PID or unparsable) — recover it.
    try:
        log("stale-lock-recovered", f"pid={pid}")
        lp.unlink(missing_ok=True)
    except OSError:
        return False
    try:
        fd = os.open(lp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))
    return True


def release_lock() -> None:
    try:
        lp = lock_path()
        raw = lp.read_text(encoding="utf-8").strip() if lp.exists() else ""
        if raw == str(os.getpid()):
            lp.unlink(missing_ok=True)
    except OSError:
        pass


# --------------------------------------------------------------------------
# Firing a run
# --------------------------------------------------------------------------

def fire_once(script_path: str) -> int:
    """Run the shadow hierarchy script. Returns exit code."""
    cmd = [sys.executable, str(_expand(script_path)), "--latest-failed"]
    log("started", " ".join(cmd))
    try:
        proc = run_subprocess(cmd, capture_output=True, text=True, timeout=3600)
        code = proc.returncode
        tail = (proc.stdout or "").strip().splitlines()[-1:] if proc.stdout else []
        log("finished", f"exit={code}" + (f" out={tail[0][:200]}" if tail else ""))
        return code
    except Exception as exc:  # timeout, OSError, ...
        log("finished", f"error={exc!r}")
        return 1


# --------------------------------------------------------------------------
# Daily report
# --------------------------------------------------------------------------

HEADLINE_SPLIT = re.compile(r"^#\s+", re.MULTILINE)


def parse_brief(path: Path) -> dict:
    """Extract job id, headline lines, and counts from a brief.md file."""
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = [ln.rstrip() for ln in text.splitlines()]
    headlines: list[str] = []
    for ln in lines:
        if ln.startswith("#"):
            headlines.append(ln.lstrip("#").strip())
    # job id: look for `job id: X` / `job_id: X` / `Job ID: X` in the brief,
    # else fall back to the workspace directory name.
    job_id = None
    m = re.search(r"(?i)job[_ -]?id[:=]\s*(\S+)", text)
    if m:
        job_id = m.group(1).strip("`*_")
    incident_id = path.parent.name
    counts = {}
    for ln in lines:
        cm = re.match(r"(?i)\s*[-*]?\s*([a-z_ ]+?)\s*[:=]\s*(\d+)\s*$", ln)
        if cm:
            key = cm.group(1).strip().lower().replace(" ", "_")
            counts[key] = int(cm.group(2))
    return {
        "incident": incident_id,
        "job_id": job_id or incident_id,
        "headlines": headlines,
        "counts": counts,
        "mtime": path.stat().st_mtime,
        "path": str(path),
    }


def collect_briefs(sdir: Path, window_hours: float = 24.0,
                   now: float | None = None) -> list[dict]:
    """Briefs under sdir/*/brief.md modified within the last `window_hours`."""
    if now is None:
        now = time.time()
    cutoff = now - window_hours * 3600
    out = []
    if not sdir.is_dir():
        return out
    for brief in sorted(sdir.glob(f"*/{BRIEF_NAME}")):
        try:
            mtime = brief.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            continue
        try:
            out.append(parse_brief(brief))
        except OSError:
            continue
    return out


def compose_report(briefs: list[dict], now: datetime | None = None) -> str:
    """Group briefs per incident and compose the comparison report body."""
    if now is None:
        now = datetime.now(timezone.utc)
    lines = [f"Shadow-week daily report — {now.strftime('%Y-%m-%d %H:%M UTC')}",
             f"Incidents: {len(briefs)}", ""]
    for b in sorted(briefs, key=lambda x: x["incident"]):
        lines.append(f"## {b['incident']} (job id: {b['job_id']})")
        for h in b["headlines"]:
            lines.append(f"- {h}")
        if b["counts"]:
            counts = ", ".join(f"{k}: {v}" for k, v in sorted(b["counts"].items()))
            lines.append(f"- counts: {counts}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def default_post_comment(ticket_id: str, body: str) -> int:
    """Default hub POST via urllib. Returns HTTP status or raises."""
    hub_url = os.environ.get("HUB_URL", "")
    token_path = os.environ.get("HUB_TOKEN_FILE", "")
    token_env = os.environ.get("HUB_TOKEN", "")
    if not hub_url:
        raise RuntimeError("HUB_URL not set")
    token = token_env
    if not token and token_path:
        token = Path(os.path.expanduser(token_path)).read_text(
            encoding="utf-8").strip()
    req = urllib.request.Request(
        f"{hub_url.rstrip('/')}/tickets/{ticket_id}/events",
        data=json.dumps({"kind": "comment", "payload": {"body": body}}).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status


def post_comment(ticket_id: str, body: str) -> int:
    impl = post_comment_impl or default_post_comment
    return impl(ticket_id, body)


def daily_report(ticket_id: str, sdir: Path | None = None) -> bool:
    """Compose + POST the daily report. Returns True on success."""
    sdir = sdir or shutdown_dir()
    briefs = collect_briefs(sdir)
    body = compose_report(briefs)
    log("daily-report", f"incidents={len(briefs)} ticket={ticket_id}")
    try:
        status = post_comment(ticket_id, body)
        log("daily-report-posted", f"status={status}")
        return True
    except Exception as exc:
        log("daily-report-post-failed", f"error={exc!r}")
        return False


# --------------------------------------------------------------------------
# Modes / CLI
# --------------------------------------------------------------------------

def run_cycle(script_path: str) -> None:
    if acquire_lock():
        try:
            fire_once(script_path)
        finally:
            release_lock()
    else:
        log("skipped", "another run in flight")


def loop(script_path: str, interval_minutes: float) -> None:
    interval_s = interval_minutes * 60
    while True:
        run_cycle(script_path)
        sleep_impl(interval_s)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Aerial shadow-week scheduler")
    p.add_argument("script", nargs="?", default=DEFAULT_SCRIPT,
                   help="path to run_shadow_hierarchy.py")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--interval", type=float, metavar="MINUTES")
    mode.add_argument("--daily-report", metavar="TICKET_ID")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.once:
        run_cycle(args.script)
        return 0
    if args.interval is not None:
        if args.interval <= 0:
            print("--interval must be positive", file=sys.stderr)
            return 2
        loop(args.script, args.interval)
        return 0
    # --daily-report TICKET_ID
    ok = daily_report(args.daily_report)
    return 0 if ok else 1


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    sys.exit(main())
