"""Lane B — job-runs data layer.

Read-only SQLite connection (mode=ro) to the hub's a5-runtime.db in WAL mode.
Responsibilities:
  - stage evidence JSON parsing (pass/unverified counts),
  - ETA range derivation from job-run history per (project, stage type),
  - 'insufficient history' fallback — never a fabricated range (L3),
  - DB-lock handling: retry backoff 1s/2s/4s (max 3 attempts), then degrade to
    stale-with-timestamp (D1) — never a crash,
  - incremental status-row update on a job-run-completed event (D1) — no full
    tracker refresh.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

from deployment_profile import pvalue as _pv
DEFAULT_DB = str(_pv("a5_runtime_db"))
BACKOFFS = [1.0, 2.0, 4.0]  # seconds; max 3 attempts, then degrade


class DegradeToStale(Exception):
    """Raised after lock retries are exhausted; carries the last good read ts."""

    def __init__(self, last_read_ts: Optional[float], detail: str):
        super().__init__(detail)
        self.last_read_ts = last_read_ts
        self.detail = detail


class JobRunsDB:
    def __init__(self, db_path: str = DEFAULT_DB, last_good_ts: Optional[float] = None):
        self.db_path = db_path
        self.last_good_ts = last_good_ts
        self._conn: Optional[sqlite3.Connection] = None

    # ---- connection (read-only, WAL) -------------------------------------

    def _connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        if not os.path.exists(self.db_path):
            raise FileNotFoundError(self.db_path)
        # uri read-only: the dashboard can never write the runtime DB.
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=0.2)
        # WAL readers never block writers; keep the connection read-only anyway.
        conn.execute("PRAGMA query_only=ON")
        self._conn = conn
        return conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _execute_with_backoff(self, sql: str, params: Tuple = ()) -> List[sqlite3.Row]:
        """Retry on 'database is locked' with 1s/2s/4s backoff, max 3, then degrade."""
        conn = self._connect()
        conn.row_factory = sqlite3.Row
        last_err: Optional[Exception] = None
        for attempt in range(3):
            try:
                cur = conn.execute(sql, params)
                rows = cur.fetchall()
                self.last_good_ts = time.time()
                return rows
            except sqlite3.OperationalError as e:
                if "locked" not in str(e).lower() and "busy" not in str(e).lower():
                    raise
                last_err = e
                if attempt < 2:
                    time.sleep(BACKOFFS[attempt])
        raise DegradeToStale(self.last_good_ts, f"db locked after 3 attempts: {last_err}")

    # ---- schema (tolerates the real ai-ops/bin/job_run.py schema) ---------

    def _job_runs_table(self) -> str:
        rows = self._execute_with_backoff(
            "SELECT name FROM sqlite_master WHERE type='table'")
        names = {r[0] for r in rows}
        for cand in ("job_runs", "job_run", "a5_job_runs"):
            if cand in names:
                return cand
        raise LookupError(f"no job-runs table in {self.db_path}: {sorted(names)}")

    # ---- evidence parsing --------------------------------------------------

    def parse_stage_evidence(self, evidence_json: str) -> Dict[str, Any]:
        """Parse a stage evidence JSON blob into pass/unverified/total counts."""
        try:
            data = json.loads(evidence_json)
        except (json.JSONDecodeError, TypeError):
            return {"pass": 0, "unverified": 0, "total": 0, "error": "unparseable"}
        if isinstance(data, dict):
            stages = data.get("stages", data)
        else:
            stages = data
        passed = unverified = 0
        total = 0
        if isinstance(stages, dict):
            items = stages.values()
        elif isinstance(stages, list):
            items = stages
        else:
            items = []
        for s in items:
            total += 1
            status = str(s.get("status", s) if isinstance(s, dict) else s).lower()
            if status in ("pass", "passed", "ok", "verified"):
                passed += 1
            else:
                unverified += 1
        pct = round(100.0 * passed / total, 1) if total else 0.0
        return {"pass": passed, "unverified": unverified, "total": total, "pct": pct}

    # ---- reads -------------------------------------------------------------

    def job_runs(self, project: Optional[str] = None, stage_type: Optional[str] = None) -> List[Dict[str, Any]]:
        table = self._job_runs_table()
        sql = f"SELECT * FROM {table}"
        conds, params = [], []
        if project is not None:
            conds.append("project = ?")
            params.append(project)
        if stage_type is not None:
            conds.append("stage_type = ?")
            params.append(stage_type)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY completed_at"
        return [dict(r) for r in self._execute_with_backoff(sql, tuple(params))]

    def status_rows(self) -> List[Dict[str, Any]]:
        """Current per-project status rows (% complete from evidence JSONs)."""
        out = []
        for run in self.job_runs():
            out.append({
                "project": run.get("project"),
                "stage_type": run.get("stage_type"),
                "status": run.get("status", "working"),
                "evidence": self.parse_stage_evidence(run.get("evidence_json") or "{}"),
                "completed_at": run.get("completed_at"),
            })
        return out

    def update_status_row_from_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """D1: apply a single job-run-completed event — update one project's
        status row without a full tracker refresh."""
        run = {
            "project": event.get("project"),
            "stage_type": event.get("stage_type"),
            "status": event.get("status", "done"),
            "evidence_json": event.get("evidence_json", "{}"),
            "completed_at": event.get("completed_at"),
        }
        row = {
            "project": run["project"],
            "stage_type": run["stage_type"],
            "status": run["status"],
            "evidence": self.parse_stage_evidence(run["evidence_json"]),
            "completed_at": run["completed_at"],
        }
        # Event-sourced row update: replaces/updates just this row in the cache.
        self._event_rows = getattr(self, "_event_rows", {})
        self._event_rows[(row["project"], row["stage_type"])] = row
        return row

    # ---- ETA ---------------------------------------------------------------

    def eta_range(self, project: str, remaining_stage_types: List[str]) -> Dict[str, Any]:
        """ETA as a range from per-stage-type observed durations.

        Range = [sum(min observed), sum(max observed)] per remaining stage type;
        'insufficient history' when any remaining stage type has no prior runs.
        """
        lo = 0.0
        hi = 0.0
        per_stage = {}
        for st in remaining_stage_types:
            runs = [r for r in self.job_runs(project=project, stage_type=st)
                    if r.get("duration_hours") is not None]
            if not runs:
                return {"eta": "insufficient history", "stage_type": st}
            durs = sorted(float(r["duration_hours"]) for r in runs)
            per_stage[st] = {"min_h": durs[0], "max_h": durs[-1], "n": len(durs)}
            lo += durs[0]
            hi += durs[-1]
        def fmt(h: float) -> str:
            if h < 24:
                return f"{h:.0f}h"
            return f"{max(1, round(h / 24))}d"
        return {
            "eta": f"{fmt(lo)}–{fmt(hi)}" if hi > lo else fmt(lo),
            "per_stage": per_stage,
            "hours": [lo, hi],
        }
