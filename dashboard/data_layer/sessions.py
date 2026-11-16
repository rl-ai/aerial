"""Lane B — dashboard-sessions store.

Single source of "done since last visit" (rev2 #5): one row per Ray visit,
written on dashboard load, read on render. Zone A (§1), L2, and the daily brief
all read the SAME last_visit() value.

Backed by the hub's dashboard-sessions table when reachable; falls back to a
local mirror file (read-only degradation, D2) when the hub is unavailable.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, Dict, Optional

from deployment_profile import pvalue as _pv
DEFAULT_HUB_DB = str(_pv("hub_db"))
DEFAULT_MIRROR = os.path.expanduser("~/.hermes/dashboard/sessions-mirror.json")
TABLE = "dashboard_sessions"


class SessionsStore:
    def __init__(self, hub_db_path: str = DEFAULT_HUB_DB, mirror_path: str = DEFAULT_MIRROR):
        self.hub_db_path = hub_db_path
        self.mirror_path = mirror_path

    # ---- local mirror (degradation path; also used in tests) ------------------

    def _read_mirror(self) -> Dict[str, Any]:
        if not os.path.exists(self.mirror_path):
            return {"last_visit": None}
        try:
            with open(self.mirror_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {"last_visit": None}

    def _write_mirror(self, visit_ts: float) -> None:
        d = os.path.dirname(self.mirror_path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = self.mirror_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"last_visit": visit_ts}, f)
        os.replace(tmp, self.mirror_path)

    # ---- schema ----------------------------------------------------------------

    def _ensure_table(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {TABLE} ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  visitor TEXT NOT NULL DEFAULT 'ray',"
            "  visit_ts REAL NOT NULL)"
        )
        conn.commit()

    # ---- write on dashboard load --------------------------------------------------

    def record_visit(self, visitor: str = "ray", visit_ts: Optional[float] = None,
                     hub_available: bool = True) -> Dict[str, Any]:
        ts = visit_ts if visit_ts is not None else time.time()
        if hub_available and os.path.exists(self.hub_db_path):
            try:
                conn = sqlite3.connect(self.hub_db_path, timeout=30.0)
                try:
                    self._ensure_table(conn)
                    conn.execute(f"INSERT INTO {TABLE} (visitor, visit_ts) VALUES (?, ?)", (visitor, ts))
                    conn.commit()
                finally:
                    conn.close()
            except sqlite3.OperationalError:
                pass  # lock/busy -> mirror still records the visit
        self._write_mirror(ts)
        return {"visitor": visitor, "visit_ts": ts}

    # ---- read on render (single source) ----------------------------------------------

    def last_visit(self) -> Optional[float]:
        """Ray's last dashboard visit timestamp — the single 'done since' source."""
        if os.path.exists(self.hub_db_path):
            try:
                conn = sqlite3.connect(f"file:{self.hub_db_path}?mode=ro", uri=True, timeout=30.0)
                try:
                    row = conn.execute(
                        f"SELECT MAX(visit_ts) FROM {TABLE} WHERE visitor = 'ray'").fetchone()
                    if row and row[0] is not None:
                        return float(row[0])
                finally:
                    conn.close()
            except sqlite3.OperationalError:
                pass
        return self._read_mirror().get("last_visit")

    def done_since_label(self, done_ts: Optional[float]) -> str:
        """Format 'done since <last visit>' — one phrasing for all three surfaces."""
        lv = self.last_visit()
        if done_ts is None:
            return "not done"
        if lv is None:
            return "done since first visit (no visit recorded)"
        if done_ts > lv:
            return "done-since-last-visit"
        return f"done since {time.strftime('%Y-%m-%d %H:%M', time.localtime(lv))}"
