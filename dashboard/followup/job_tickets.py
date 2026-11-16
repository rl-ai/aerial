#!/usr/bin/env python3
"""Scheduled-job ticket registry: one recurring child ticket per scheduled job.

Enumerates the crontab's job_run.py entries and ensures each has a hub
ticket (child of the ops epic) titled "Scheduled: <name>". Failures on a
job are commented on its ticket and the ticket carries the ops assignee —
the per-job failure history Ray can open directly.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

_root = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if _root not in sys.path:
    sys.path.insert(0, _root)
from deployment_profile import value as _pv   # noqa: E402

HUB = str(_pv("cluster.hub_url") or "http://127.0.0.1:8020")
TOKEN_FILE = os.path.expanduser(str(_pv("cluster.hub_token_file")))
_WRITE_TF = str(_pv("cluster.hub_write_token_file") or "")
WRITE_TOKEN_FILE = os.path.expanduser(_WRITE_TF) if _WRITE_TF else TOKEN_FILE
EPIC = str(_pv("cluster.epic_id") or "")


def _token(method: str = "GET") -> str:
    """Reads use the read bearer; mutations use the write bearer (the hub's
    fail-closed write scoping rejects a read token on POST/PATCH/DELETE)."""
    f = WRITE_TOKEN_FILE if method.upper() not in ("GET", "HEAD", "OPTIONS") \
        else TOKEN_FILE
    return Path(f).read_text().strip()


def _api(path: str, method: str = "GET", body: dict | None = None) -> dict:
    req = urllib.request.Request(
        HUB + path, data=json.dumps(body).encode() if body else None,
        method=method,
        headers={"authorization": "Bearer " + _token(method),
                 "content-type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


def crontab_jobs() -> list[dict]:
    """Parse the crontab's job_run.py entries: name, schedule, ticket, cmd."""
    raw = subprocess.run(["crontab", "-l"], capture_output=True, text=True,
                         timeout=10).stdout
    jobs = []
    for line in raw.splitlines():
        if "job_run.py" not in line or line.strip().startswith("#"):
            continue
        m = re.search(r"--job\s+(\S+)", line)
        mt = re.search(r"--ticket\s+(\S+)", line)
        if not m:
            continue
        name = m.group(1).split(":", 1)[-1]  # <node>:xxx -> xxx
        sched = " ".join(line.split()[:5])
        comment = ""
        mc = re.search(r"#\s*(\S+)\s*$", line)
        if mc:
            comment = mc.group(1)
        jobs.append({"name": name, "schedule": sched,
                     "ticket": mt.group(1) if mt else None,
                     "comment": comment})
    return jobs


def ensure_ticket(job: dict, existing: dict | None) -> str:
    """Create the job's ticket if absent; return the ticket id."""
    if existing:
        return existing["id"]
    d = _api("/tickets", "POST", {
        "title": f"Scheduled: {job['name']}",
        "body": (f"Recurring scheduled job. Schedule: `{job['schedule']}` "
                 f"(cron). Failures on this job are commented here and "
                 f"assigned to ops for follow-up. Managed by the ops agent "
                 f"(schedule reconciliation)."),
        "tags": ["scheduled-job", "aerial", "ops"],
        "actor": "ops-agent",
        "parent_id": EPIC,
    })
    tid = d.get("id") or d.get("ticket_id")
    return tid


def sync() -> list[dict]:
    """Ensure every scheduled job has a ticket; return the registry."""
    jobs = crontab_jobs()
    # index existing tickets tagged scheduled-job
    existing = {}
    try:
        d = _api("/tickets?tag=scheduled-job&limit=100")
        for t in (d.get("tickets") or d.get("items") or d if isinstance(d, list) else []):
            if isinstance(t, dict) and t.get("title", "").startswith("Scheduled: "):
                existing[t["title"].replace("Scheduled: ", "")] = t
    except Exception:
        pass
    registry = []
    for job in jobs:
        tid = None
        ex = existing.get(job["name"])
        if ex:
            tid = ex["id"]
        elif job["ticket"] and job["ticket"].startswith("TK-"):
            # the job already points at a real ticket — reuse it
            try:
                _api(f"/tickets/{job['ticket']}")
                tid = job["ticket"]
            except Exception:
                tid = None
        try:
            tid = ensure_ticket(job, {"id": tid} if tid else None)
        except Exception as exc:
            registry.append({**job, "ticket_id": None, "error": str(exc)})
            continue
        registry.append({**job, "ticket_id": tid})
    return registry


if __name__ == "__main__":
    for entry in sync():
        status = entry.get("error") or entry.get("ticket_id")
        print(f"{entry['name']:28} {entry['schedule']:12} -> {status}")
