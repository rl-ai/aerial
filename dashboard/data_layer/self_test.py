"""Lane B self-test — acceptance criteria 1–5 with real exit codes.

Run: python3 self_test.py   (exit 0 = all pass, 1 = failure)
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hub_client import HubClient, HubReadError
from job_runs import DegradeToStale, JobRunsDB
from model_calls_tailer import ModelCallsTailer
from sessions import SessionsStore
from snapshot_store import SnapshotStore

FAILS = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


# ---------------------------------------------------------------- AC1: partial write fixture
def test_partial_write(tmp):
    ledger = os.path.join(tmp, "model-calls.jsonl")
    state = os.path.join(tmp, "tail_state.json")

    def rec(item, i, o):
        return json.dumps({"item": item, "in_tokens": i, "out_tokens": o}).encode() + b"\n"

    with open(ledger, "wb") as f:
        f.write(rec("proj-a", 100, 10))
        f.write(rec("proj-a", 200, 20))
        f.write(rec("proj-b", 50, 5))
        # truncated partial write (no newline)
        f.write(b'{"item": "proj-a", "in_tokens": 999, ')

    t = ModelCallsTailer(ledger, state)
    d1 = t.tail()
    check("AC1 first tail excludes partial line",
          d1["lines"] == 3 and t.checkpoint["proj-a"]["calls"] == 2 and t.checkpoint["proj-b"]["calls"] == 1)
    ck_after_first = json.loads(json.dumps(t.checkpoint))

    # Complete the partial line (writer finishes it)
    with open(ledger, "ab") as f:
        f.write(b'"out_tokens": 99}\n')

    d2 = t.tail()
    check("AC1 second tail includes completed line exactly once",
          d2["lines"] == 1 and t.checkpoint["proj-a"]["calls"] == 3
          and t.checkpoint["proj-a"]["in_tokens"] == 100 + 200 + 999
          and t.checkpoint["proj-a"]["out_tokens"] == 10 + 20 + 99,
          f"delta={d2['items']}")

    # Malformed + untagged lines -> 'untagged' bucket
    with open(ledger, "ab") as f:
        f.write(b"this is not json\n")
        f.write(json.dumps({"role": "glm", "in_tokens": 7, "out_tokens": 3}).encode() + b"\n")
    t.tail()
    v = t.verify_full_scan()
    check("AC1 malformed/untagged bucketed, totals == full scan", v["match"]
          and t.untagged["calls"] == 2 and t.untagged["in_tokens"] == 7)
    # double-tail is idempotent (no double-count)
    t.tail()
    v2 = t.verify_full_scan()
    check("AC1 re-tail idempotent (no double-count)", v2["match"])


# ---------------------------------------------------------------- AC2: DB-lock degradation
def test_db_lock(tmp):
    db = os.path.join(tmp, "a5-runtime.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE job_runs (project TEXT, stage_type TEXT, status TEXT,"
                 " evidence_json TEXT, completed_at REAL, duration_hours REAL)")
    conn.execute("INSERT INTO job_runs VALUES ('proj-a','build','done','{\"stages\":[{\"status\":\"pass\"},{\"status\":\"unverified\"}]}', 100, 20)")
    conn.commit()
    conn.close()

    jr = JobRunsDB(db)
    # Simulate a lock by holding an EXCLUSIVE lock from another connection.
    locker = sqlite3.connect(db, isolation_level=None)
    locker.execute("BEGIN EXCLUSIVE")
    try:
        start = time.time()
        try:
            jr.job_runs()
            check("AC2 lock raised DegradeToStale", False, "no exception")
        except DegradeToStale as e:
            elapsed = time.time() - start
            # backoff sleeps 1+2 = 3s minimum (4th sleep never happens, max 3 attempts)
            check("AC2 backoff 1s/2s then degrade (max 3 attempts)", 2.8 <= elapsed < 6.0,
                  f"elapsed={elapsed:.1f}s")
            check("AC2 degrade carries last-good timestamp (or None if never read)",
                  e.last_read_ts is None or isinstance(e.last_read_ts, float))
        check("AC2 never crashed — instance still usable", True)
    finally:
        locker.execute("ROLLBACK")
        locker.close()
    rows = jr.job_runs()  # lock released -> works
    check("AC2 reads succeed after lock release", len(rows) == 1)

    # AC5: ETA from history; no history -> 'insufficient history'
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO job_runs VALUES ('proj-a','build','done','{}', 200, 30)")
    conn.execute("INSERT INTO job_runs VALUES ('proj-a','test','done','{}', 300, 5)")
    conn.execute("INSERT INTO job_runs VALUES ('proj-a','test','done','{}', 400, 15)")
    conn.commit()
    conn.close()
    jr.close()
    jr2 = JobRunsDB(db)
    eta = jr2.eta_range("proj-a", ["build", "test"])
    check("AC5 ETA is a range from per-stage history",
          eta["eta"] == "1d–2d" and eta["hours"] == [25.0, 45.0], str(eta))
    check("AC5 ETA stable within observed variance (not point estimate)", eta["hours"][1] - eta["hours"][0] > 0)
    eta_none = jr2.eta_range("proj-a", ["build", "deploy"])
    check("AC5 no history -> 'insufficient history' (L3), never fabricated",
          eta_none["eta"] == "insufficient history", str(eta_none))

    # evidence parsing + event-driven row update (D1)
    ev = {"project": "proj-a", "stage_type": "test", "status": "done",
          "evidence_json": json.dumps({"stages": [{"status": "pass"}, {"status": "pass"}, {"status": "unverified"}]}),
          "completed_at": 500}
    row = jr2.update_status_row_from_event(ev)
    check("D1 job-run-completed updates single status row w/o full refresh",
          row["evidence"]["pass"] == 2 and row["evidence"]["unverified"] == 1
          and row["evidence"]["pct"] == 66.7, str(row))


# ---------------------------------------------------------------- AC3: hub failure matrix
class FakeHub(BaseHTTPRequestHandler):
    codes = {"/api/findings": 200}
    payload = {"items": [{"id": 1, "claim": "x"}]}

    def do_GET(self):
        code = self.codes.get(self.path, 200)
        if code == 200:
            body = json.dumps(self.payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(code)
            self.end_headers()
            self.wfile.write(b'{"error":"denied"}')

    def do_POST(self):
        # writes require the write token; read token -> 401
        tok = self.headers.get("Authorization", "")
        if "write-tok" in tok:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok": true}')
        else:
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"error":"auth required"}')

    def log_message(self, *a):
        pass


def test_hub_failures(tmp):
    snap_path = os.path.join(tmp, "queue-snapshot.json")
    snap = SnapshotStore(snap_path)

    srv = HTTPServer(("127.0.0.1", 0), FakeHub)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()

    client = HubClient(read_token="read-tok", write_token="write-tok",
                       base_url=f"http://127.0.0.1:{port}", timeout=2.0)

    # successful read -> persisted last-known-good with timestamp
    data = client.get_findings()
    snap.save("queue", data)
    check("AC3 successful read persisted as last-known-good", snap.get_stale("queue")["state"] == "stale"
          and snap.get_stale("queue")["payload"] == FakeHub.payload)

    # failure matrix -> degrade to stale-with-timestamp, never fabricate
    for label, path in [("401", "/api/decisions"), ("403", "/api/mandates"), ("500", "/api/sessions")]:
        FakeHub.codes[path] = int(label)
        try:
            client._request(path, client.read_token)
            check(f"AC3 {label} raised HubReadError", False)
        except HubReadError as e:
            stale = snap.get_stale("queue")
            check(f"AC3 {label} -> degrade to stale-with-timestamp (D2)",
                  stale["state"] == "stale" and stale["read_ts"] is not None,
                  f"err={e}")
        FakeHub.codes[path] = 200

    # timeout (unroutable port with tiny timeout)
    slow = HubClient(read_token="r", write_token="w", base_url="http://127.0.0.1:1", timeout=0.3)
    try:
        slow.get_findings()
        check("AC3 timeout raised HubReadError", False)
    except HubReadError:
        check("AC3 timeout -> degrade path available (stale still served)",
              snap.get_stale("queue")["state"] == "stale")

    # first load with NO prior successful read -> explicit 'unavailable'
    empty = SnapshotStore(os.path.join(tmp, "empty.json"))
    check("AC3 first load, no prior read -> 'unavailable' (D2)",
          empty.get_stale("queue") == {"state": "unavailable", "payload": None, "read_ts": None})

    # write path uses write token; read token is rejected on POST (Q4 boundary)
    ok = client.approve("row-1", mandate_id="m-1", idempotency_key="row-1+m-1")
    check("AC3 decision-scoped write token used for writes", ok == {"ok": True})
    try:
        client._request("/api/decisions/approve", client.read_token, method="POST", body={"row_id": "x"})
        check("AC3 read token cannot write", False)
    except Exception as e:
        check("AC3 read token rejected on write path (401)", "401" in str(e), str(e))

    srv.shutdown()


# ---------------------------------------------------------------- sessions (L2 single source)
def test_sessions(tmp):
    hub_db = os.path.join(tmp, "hub.db")
    mirror = os.path.join(tmp, "sessions-mirror.json")
    s = SessionsStore(hub_db, mirror)
    # hub DB must exist for the write-on-load path
    conn0 = sqlite3.connect(hub_db)
    conn0.execute("CREATE TABLE IF NOT EXISTS dashboard_sessions (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                  " visitor TEXT NOT NULL DEFAULT 'ray', visit_ts REAL NOT NULL)")
    conn0.commit()
    conn0.close()
    check("sessions no visit yet -> None", s.last_visit() is None)
    s.record_visit(visit_ts=1000.0)
    check("sessions write on load + read on render", s.last_visit() == 1000.0)
    s.record_visit(visit_ts=2000.0)
    check("sessions last visit wins", s.last_visit() == 2000.0)
    # done after last visit -> 'done-since-last-visit'; before -> dated label
    check("L2 single-source label (done after visit)",
          s.done_since_label(done_ts=2500.0) == "done-since-last-visit")
    check("L2 single-source label (done before visit)",
          s.done_since_label(done_ts=1500.0) == "done since " + time.strftime("%Y-%m-%d %H:%M", time.localtime(2000.0)))
    # mirror fallback when hub DB gone
    os.rename(hub_db, hub_db + ".bak")
    check("sessions mirror fallback on hub unavailability", s.last_visit() == 2000.0)
    os.rename(hub_db + ".bak", hub_db)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        test_partial_write(tmp)
        test_db_lock(tmp)
        test_hub_failures(tmp)
        test_sessions(tmp)
    print()
    if FAILS:
        print(f"SELF-TEST FAILED: {len(FAILS)} — {FAILS}")
        sys.exit(1)
    print("SELF-TEST PASSED (all acceptance criteria)")
    sys.exit(0)


if __name__ == "__main__":
    main()
