"""ticket_hub jobs — background job + agent run tracking (SPEC: Jobs/Data).

``JobStore`` lives in the same SQLite file as the hub Store (own connection,
WAL-safe, ``CREATE TABLE IF NOT EXISTS``). Timestamps are UTC ISO-8601 strings
with ``Z``; every method that needs "now" takes ``now: datetime | None`` (UTC
aware) so tests can inject time. Stdlib only. Python 3.12+.
"""
from __future__ import annotations

import functools
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone

FINAL_STATUSES = ("ok", "failed", "timeout", "skipped")
RUN_STATUSES = ("running",) + FINAL_STATUSES
JOB_FIELDS = ("name", "node", "kind", "schedule", "interval_s", "ticket_id",
              "command", "enabled")

SUMMARY_MAX = 500
LOG_TAIL_MAX = 4000
STUCK_FALLBACK_S = 6 * 3600      # stuck horizon when interval_s is NULL
OVERDUE_GRACE_S = 900            # slack on top of interval_s before overdue
DAY_S = 86400

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    node TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'cron',
    schedule TEXT NOT NULL DEFAULT '',
    interval_s INTEGER,
    ticket_id TEXT,
    command TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_runs (
    run_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    node TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL,
    exit_code INTEGER,
    duration_s REAL,
    summary TEXT NOT NULL DEFAULT '',
    log_tail TEXT NOT NULL DEFAULT '',
    meta TEXT NOT NULL DEFAULT '{}',
    pid INTEGER
);
CREATE INDEX IF NOT EXISTS idx_job_runs_job ON job_runs (job_id, started_at);
CREATE INDEX IF NOT EXISTS idx_job_runs_started ON job_runs (started_at);
"""


# -- time helpers -----------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _fmt(dt: datetime) -> str:
    """Aware/naive datetime -> canonical 'YYYY-MM-DDTHH:MM:SSZ' string."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value) -> datetime:
    """Canonical string (or datetime) -> aware UTC datetime."""
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _now(now: datetime | None) -> datetime:
    return _utcnow() if now is None else now


def _truncate(text: str, limit: int, keep_end: bool = False) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[-limit:] if keep_end else text[:limit]


# -- status rules (pure) ----------------------------------------------------

def job_status(job: dict, last_run: "dict | None", now: datetime) -> str:
    """Health of a job given its latest run. Pure function (SPEC 'Data')."""
    interval = job.get("interval_s")
    if last_run is not None and last_run["status"] == "running":
        horizon = max(2 * interval, STUCK_FALLBACK_S) if interval else STUCK_FALLBACK_S
        if (now - _parse(last_run["started_at"])).total_seconds() > horizon:
            return "stuck"
        return "running"
    if interval:
        grace = interval + OVERDUE_GRACE_S
        anchor = last_run["started_at"] if last_run is not None else job["created_at"]
        if (now - _parse(anchor)).total_seconds() > grace:
            return "overdue"
    if last_run is None:
        return "unknown"
    return "failed" if last_run["status"] in ("failed", "timeout") else "ok"


_STATUS_RANK = {"stuck": 5, "failed": 4, "overdue": 3, "running": 2, "ok": 1, "unknown": 0}


def _worst(statuses) -> str:
    best, worst = None, None
    for s in statuses:
        if worst is None or _STATUS_RANK[s] > _STATUS_RANK[worst]:
            worst, best = s, s
    return best or "unknown"


def _synced(fn):
    """Serialize all use of the shared connection under the store's RLock."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)
    return wrapper


def digest_markdown(d: dict) -> str:
    """Short Markdown rendering of a digest dict (SPEC 'Data'). Kept under
    4000 chars by dropping trailing project sections with '… N more'."""
    c, t = d["coverage"], d["totals"]
    lines = [
        f"# Jobs digest {d['generated_at']}",
        f"Coverage: {c['reporting']}/{c['registered']} reporting ({c['pct']}%). "
        f"Jobs {t['jobs']}, runs {t['runs']}, ok {t['ok']}, failed {t['failed']}, "
        f"overdue {t['overdue']}, stuck {t['stuck']}.",
        "",
    ]
    if d["problems"]:
        lines.append("Problems:")
        for p in d["problems"]:
            lines.append(f"- {p['job_id']} [{p['status']}] ({p['node']}): {p['last_summary']}")
    else:
        lines.append("No problems.")
    lines.append("")
    out = "\n".join(lines)
    TAIL_RESERVE = 64
    omitted = 0
    for pr in d["projects"]:
        sec = [f"## {pr['title']}"]
        for f in pr["features"]:
            n = len(f["jobs"])
            runs = sum(j["runs"] for j in f["jobs"])
            failed = sum(j["failed"] for j in f["jobs"])
            sec.append(f"- {f['title']}: {n} jobs, {runs} runs, {failed} failed [{f['status']}]")
        section = "\n".join(sec)
        if omitted == 0 and len(out) + len(section) + 2 + TAIL_RESERVE < 4000:
            out += section + "\n\n"
        else:
            omitted += 1
    if omitted:
        out += f"… {omitted} more\n"
    return out if len(out) < 4000 else out[:3999]


class JobStore:
    """Job + run tracking in the hub database (own connection).

    ``JobStore(path)`` opens the same file as ``Store``; ``":memory:"`` works
    for tests that do not need to share a file.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = str(db_path)
        self._lock = threading.RLock()   # guards every _conn execute/commit
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    @_synced
    def close(self) -> None:
        self._conn.close()

    # -- jobs ---------------------------------------------------------------

    @_synced
    def upsert_job(self, job_id: str, now: datetime | None = None, **fields) -> dict:
        """Create or update a job. Unknown field names raise ValueError;
        ``created_at`` is kept on update."""
        unknown = sorted(set(fields) - set(JOB_FIELDS))
        if unknown:
            raise ValueError(f"unknown job fields: {', '.join(unknown)}")
        ts = _fmt(_now(now))
        if "enabled" in fields:
            fields["enabled"] = 1 if fields["enabled"] else 0
        if self.get_job(job_id) is None:
            name = fields.get("name") or job_id  # name is NOT NULL; default to id
            self._conn.execute(
                """INSERT INTO jobs (job_id, name, node, kind, schedule, interval_s,
                                     ticket_id, command, enabled, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (job_id, name,
                 fields.get("node", ""), fields.get("kind", "cron"),
                 fields.get("schedule", ""), fields.get("interval_s"),
                 fields.get("ticket_id"), fields.get("command", ""),
                 fields.get("enabled", 1), ts, ts),
            )
        elif fields:
            items = sorted(fields.items())          # names and values in one order
            sets = ", ".join(f"{k} = ?" for k, _v in items)
            self._conn.execute(
                f"UPDATE jobs SET {sets}, updated_at = ? WHERE job_id = ?",
                (*[v for _k, v in items], ts, job_id),
            )
        self._conn.commit()
        return self.get_job(job_id)

    @_synced
    def get_job(self, job_id: str) -> "dict | None":
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    @_synced
    def list_jobs(self, node: str | None = None,
                  include_disabled: bool = False) -> "list[dict]":
        sql = ["SELECT * FROM jobs WHERE 1=1"]
        args: list = []
        if node is not None:
            sql.append("AND node = ?")
            args.append(node)
        if not include_disabled:
            sql.append("AND enabled = 1")
        sql.append("ORDER BY job_id ASC")
        return [dict(r) for r in self._conn.execute(" ".join(sql), args).fetchall()]

    # -- runs ---------------------------------------------------------------

    @_synced
    def start_run(self, job_id: str, node: str = "", pid: "int | None" = None,
                  meta: dict | None = None, now: datetime | None = None,
                  **job_fields) -> dict:
        """Register a run in progress; auto-registers the job if missing
        (``name`` defaults to the job id; *job_fields* go through upsert)."""
        if job_id not in {j["job_id"] for j in self.list_jobs(include_disabled=True)}:
            self.upsert_job(job_id, **{"node": node, "name": job_id, **job_fields})
        elif job_fields:
            self.upsert_job(job_id, **job_fields)
        return self._insert_run(job_id, "running", _now(now), node=node,
                                pid=pid, meta=meta)

    @_synced
    def end_run(self, run_id: str, status: str, exit_code: "int | None" = None,
                summary: str = "", log_tail: str = "", meta: dict | None = None,
                now: datetime | None = None) -> dict:
        """Finish a running run: sets ended_at/duration_s, merges meta."""
        if status not in FINAL_STATUSES:
            raise ValueError(f"bad end status: {status!r}")
        run = self._get_run(run_id)
        if run is None:
            raise KeyError(f"run not found: {run_id}")
        ended = _now(now)
        duration = (ended - _parse(run["started_at"])).total_seconds()
        merged = dict(run["meta"])
        merged.update(meta or {})
        self._conn.execute(
            """UPDATE job_runs SET status = ?, ended_at = ?, duration_s = ?,
                   exit_code = COALESCE(?, exit_code), summary = ?, log_tail = ?,
                   meta = ? WHERE run_id = ?""",
            (status, _fmt(ended), duration, exit_code,
             _truncate(summary, SUMMARY_MAX),
             _truncate(log_tail, LOG_TAIL_MAX, keep_end=True),
             json.dumps(merged), run_id),
        )
        self._conn.commit()
        return self._get_run(run_id)

    @_synced
    def record_run(self, job_id: str, status: str, started_at, ended_at=None,
                   node: str = "", pid: "int | None" = None,
                   meta: dict | None = None, exit_code: "int | None" = None,
                   summary: str = "", log_tail: str = "",
                   now: datetime | None = None, **job_fields) -> dict:
        """One-shot completed run (reporters that only report at the end).
        Repeats are folded by ``meta['dedupe_key']``: the existing run is
        returned instead of inserting a duplicate."""
        if status not in FINAL_STATUSES:
            raise ValueError(f"record_run needs a final status, got {status!r}")
        if meta and "dedupe_key" in meta:
            row = self._conn.execute(
                """SELECT * FROM job_runs WHERE job_id = ?
                   AND json_extract(meta, '$.dedupe_key') = ?
                   ORDER BY started_at ASC, rowid ASC LIMIT 1""",
                (job_id, str(meta["dedupe_key"]))).fetchone()
            if row is not None:
                return self._row_to_run(row)
        if job_id not in {j["job_id"] for j in self.list_jobs(include_disabled=True)}:
            self.upsert_job(job_id, **{"node": node, "name": job_id, **job_fields})
        elif job_fields:
            self.upsert_job(job_id, **job_fields)
        start = _parse(started_at) if started_at is not None else _now(now)
        end = _parse(ended_at) if ended_at is not None else _now(now)
        run = self._insert_run(job_id, status, start, node=node, pid=pid, meta=meta)
        return self.end_run(run["run_id"], status, exit_code=exit_code,
                            summary=summary, log_tail=log_tail, now=end)

    @_synced
    def runs(self, job_id: str | None = None, since=None,
             limit: int = 100) -> "list[dict]":
        """Runs newest first; optional job filter and ``since`` cutoff."""
        sql = ["SELECT * FROM job_runs WHERE 1=1"]
        args: list = []
        if job_id is not None:
            sql.append("AND job_id = ?")
            args.append(job_id)
        if since is not None:
            sql.append("AND started_at >= ?")
            args.append(_fmt(_parse(since)))
        sql.append("ORDER BY started_at DESC, rowid DESC LIMIT ?")
        args.append(limit)
        return [self._row_to_run(r)
                for r in self._conn.execute(" ".join(sql), args).fetchall()]

    def _insert_run(self, job_id: str, status: str, started: datetime, *,
                    node: str = "", pid: "int | None" = None,
                    meta: dict | None = None) -> dict:
        run_id = "run_" + uuid.uuid4().hex
        self._conn.execute(
            """INSERT INTO job_runs (run_id, job_id, node, started_at, status, meta, pid)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (run_id, job_id, node, _fmt(started), status,
             json.dumps(meta or {}), pid),
        )
        self._conn.commit()
        run = self._get_run(run_id)
        assert run is not None
        return run

    def _get_run(self, run_id: str) -> "dict | None":
        row = self._conn.execute(
            "SELECT * FROM job_runs WHERE run_id = ?", (run_id,)).fetchone()
        return self._row_to_run(row) if row else None

    def _last_run(self, job_id: str) -> "dict | None":
        row = self._conn.execute(
            "SELECT * FROM job_runs WHERE job_id = ? "
            "ORDER BY started_at DESC, rowid DESC LIMIT 1",
            (job_id,)).fetchone()
        return self._row_to_run(row) if row else None

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["meta"] = json.loads(d["meta"] or "{}")
        return d

    # -- views / rollups ------------------------------------------------------

    digest_markdown = staticmethod(digest_markdown)  # same function, both call styles

    @_synced
    def jobs_view(self, now: datetime | None = None,
                  node: str | None = None) -> "list[dict]":
        """Each enabled job plus last_run, status, next_due, runs_24h, fails_24h."""
        now_dt = _now(now)
        cutoff = _fmt(now_dt - timedelta(days=1))
        out = []
        for job in self.list_jobs(node=node):
            last = self._last_run(job["job_id"])
            recent = self._conn.execute(
                "SELECT status FROM job_runs WHERE job_id = ? AND started_at >= ?",
                (job["job_id"], cutoff)).fetchall()
            next_due = None
            if job["interval_s"] and last is not None:
                next_due = _fmt(_parse(last["started_at"])
                                + timedelta(seconds=job["interval_s"]))
            out.append({
                **job,
                "last_run": last,
                "status": job_status(job, last, now_dt),
                "next_due": next_due,
                "runs_24h": len(recent),
                "fails_24h": sum(1 for r in recent
                                 if r["status"] in ("failed", "timeout")),
            })
        return out

    def _window_runs(self, since_s: str) -> "list[dict]":
        rows = self._conn.execute(
            "SELECT * FROM job_runs WHERE started_at >= ? "
            "ORDER BY started_at ASC, rowid ASC", (since_s,)).fetchall()
        return [self._row_to_run(r) for r in rows]

    @staticmethod
    def _group_key(resolved: dict | None, level: str) -> "tuple":
        node = (resolved or {}).get(level)
        return (node["id"], node["title"]) if node else (None, "(no parent)")

    @_synced
    def rollup(self, level: str, resolve, since=None,
               now: datetime | None = None) -> "list[dict]":
        """Group jobs (and their window runs) by feature or project via the
        caller-supplied ``resolve(ticket_id)``; cache it per ticket per call."""
        if level not in ("feature", "project"):
            raise ValueError(f"rollup level must be feature|project: {level!r}")
        now_dt = _now(now)
        since_s = _fmt(_parse(since)) if since is not None \
            else _fmt(now_dt - timedelta(days=1))
        runs_by_job: dict = {}
        for r in self._window_runs(since_s):
            runs_by_job.setdefault(r["job_id"], []).append(r)
        cache: dict = {}
        groups: dict = {}
        for v in self.jobs_view(now=now_dt):
            resolved = None
            if v["ticket_id"]:
                if v["ticket_id"] not in cache:
                    cache[v["ticket_id"]] = resolve(v["ticket_id"])
                resolved = cache[v["ticket_id"]]
            gid, title = self._group_key(resolved, level)
            g = groups.setdefault(gid, {"id": gid, "title": title, "jobs": 0,
                                        "runs": 0, "ok": 0, "failed": 0,
                                        "overdue": 0, "stuck": 0,
                                        "_statuses": [], "_last_failure": None})
            g["jobs"] += 1
            g["_statuses"].append(v["status"])
            if v["status"] == "overdue":
                g["overdue"] += 1
            elif v["status"] == "stuck":
                g["stuck"] += 1
            for r in runs_by_job.get(v["job_id"], ()):
                g["runs"] += 1
                if r["status"] == "ok":
                    g["ok"] += 1
                elif r["status"] in ("failed", "timeout"):
                    g["failed"] += 1
                    stamp = r["ended_at"] or r["started_at"]
                    cur = g["_last_failure"]
                    if cur is None or stamp >= cur[1]:
                        g["_last_failure"] = ({"job_id": v["job_id"],
                                               "run_id": r["run_id"],
                                               "ended_at": stamp,
                                               "summary": r["summary"]}, stamp)
        out = []
        for g in groups.values():
            ok, failed = g["ok"], g["failed"]
            g["success_rate"] = ok / (ok + failed) if ok + failed else None
            g["status"] = _worst(g.pop("_statuses"))
            g["last_failure"] = (g.pop("_last_failure") or (None,))[0]
            out.append(g)
        out.sort(key=lambda g: (g["id"] is None, g["id"] or g["title"]))
        return out

    @_synced
    def digest(self, resolve, since=None,
               now: datetime | None = None) -> "dict":
        """Daily digest: coverage of scheduled jobs, totals, problems and the
        project -> feature -> jobs tree (SPEC 'Data')."""
        now_dt = _now(now)
        since_dt = _parse(since) if since is not None else now_dt - timedelta(days=1)
        since_s = _fmt(since_dt)
        runs_by_job: dict = {}
        window = self._window_runs(since_s)
        for r in window:
            runs_by_job.setdefault(r["job_id"], []).append(r)
        views = self.jobs_view(now=now_dt)

        scheduled = [v for v in views if v["interval_s"]]
        reporting = [v for v in scheduled if runs_by_job.get(v["job_id"])]
        coverage = {
            "registered": len(scheduled),
            "reporting": len(reporting),
            "pct": round(len(reporting) * 100 / len(scheduled), 1) if scheduled else 100.0,
        }
        totals = {
            "jobs": len(views),
            "runs": len(window),
            "ok": sum(1 for r in window if r["status"] == "ok"),
            "failed": sum(1 for r in window if r["status"] in ("failed", "timeout")),
            "overdue": sum(1 for v in views if v["status"] == "overdue"),
            "stuck": sum(1 for v in views if v["status"] == "stuck"),
        }
        problems = sorted(
            ({"job_id": v["job_id"], "name": v["name"], "node": v["node"],
              "status": v["status"], "ticket_id": v["ticket_id"],
              "last_summary": v["last_run"]["summary"] if v["last_run"] else ""}
             for v in views if v["status"] in ("stuck", "failed", "overdue")),
            key=lambda p: (-_STATUS_RANK[p["status"]], p["job_id"]))

        cache: dict = {}
        projects: dict = {}

        def resolve_cached(tid):
            if tid not in cache:
                cache[tid] = resolve(tid)
            return cache[tid]

        for v in views:
            resolved = resolve_cached(v["ticket_id"]) if v["ticket_id"] else None
            pid, ptitle = self._group_key(resolved, "project")
            fid, ftitle = self._group_key(resolved, "feature")
            job_runs = runs_by_job.get(v["job_id"], [])
            entry = {
                "job_id": v["job_id"], "name": v["name"], "node": v["node"],
                "status": v["status"], "runs": len(job_runs),
                "failed": sum(1 for r in job_runs
                              if r["status"] in ("failed", "timeout")),
                "last_run_at": job_runs[-1]["started_at"] if job_runs else None,
            }
            proj = projects.setdefault(pid, {"id": pid, "title": ptitle, "_f": {}})
            feat = proj["_f"].setdefault(fid, {"id": fid, "title": ftitle, "jobs": []})
            feat["jobs"].append(entry)

        proj_list = []
        for proj in projects.values():
            features = []
            for feat in proj["_f"].values():
                feat["status"] = _worst(j["status"] for j in feat["jobs"])
                features.append(feat)
            features.sort(key=lambda f: (f["id"] is None, f["id"] or f["title"]))
            proj_list.append({"id": proj["id"], "title": proj["title"],
                              "status": _worst(f["status"] for f in features),
                              "features": features})
        proj_list.sort(key=lambda p: (p["id"] is None, p["id"] or p["title"]))
        return {
            "generated_at": _fmt(now_dt),
            "since": since_s,
            "coverage": coverage,
            "totals": totals,
            "problems": problems,
            "projects": proj_list,
        }

    # -- maintenance ------------------------------------------------------------

    @_synced
    def prune(self, older_than_days: int = 30,
              now: datetime | None = None) -> int:
        """Delete runs started before the cutoff (running runs are kept)."""
        cutoff = _fmt(_now(now) - timedelta(days=older_than_days))
        cur = self._conn.execute(
            "DELETE FROM job_runs WHERE status != 'running' AND started_at < ?",
            (cutoff,))
        self._conn.commit()
        return cur.rowcount
