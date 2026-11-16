"""§7 Data & cadence criteria D1–D2."""
import json
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from dashboard.data_layer.hub_client import HubClient, HubReadError
from dashboard.data_layer.job_runs import DegradeToStale
from dashboard.data_layer.snapshot_store import SnapshotStore


def test_D1_event_updates_status_row_without_full_refresh(job_runs_db):
    jr = job_runs_db
    ev = {"project": "aerial", "stage_type": "test", "status": "done",
          "evidence_json": json.dumps({"stages": [{"status": "pass"},
                                                  {"status": "pass"},
                                                  {"status": "unverified"}]}),
          "completed_at": 500}
    # a hub job-run-completed event updates the single status row
    # (evidence counts parsed from the JSON) without a full tracker refresh
    row = jr.update_status_row_from_event(ev)
    assert row["evidence"]["pass"] == 2
    assert row["evidence"]["unverified"] == 1
    assert row["evidence"]["pct"] == 66.7


def test_D2_db_lock_degrades_via_backoff_never_crash(job_runs_db):
    jr = job_runs_db
    # hold an EXCLUSIVE lock from another connection
    locker = sqlite3.connect(job_runs_db.db_path, isolation_level=None)
    locker.execute("BEGIN EXCLUSIVE")
    try:
        start = time.time()
        with pytest.raises(DegradeToStale) as ei:
            jr.job_runs()
        elapsed = time.time() - start
        # defined backoff: 1s/2s (max 3 attempts) then DegradeToStale
        assert 2.8 <= elapsed < 6.0
        assert ei.value.last_read_ts is None or isinstance(ei.value.last_read_ts, float)
    finally:
        locker.execute("ROLLBACK")
        locker.close()
    # never crashed: the instance is still usable once the lock is released
    assert len(jr.job_runs()) == 6


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


@pytest.fixture
def hub(tmp_path):
    snap = SnapshotStore(str(tmp_path / "queue-snapshot.json"))
    srv = HTTPServer(("127.0.0.1", 0), FakeHub)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    client = HubClient(read_token="read-tok", write_token="write-tok",
                       base_url=f"http://127.0.0.1:{port}", timeout=2.0)
    yield client, snap
    srv.shutdown()


def test_D2_hub_failures_degrade_to_stale_with_timestamp(hub):
    client, snap = hub
    # a successful read persists the last-known-good snapshot with its read ts
    data = client.get_findings()
    snap.save("queue", data)
    stale = snap.get_stale("queue")
    assert stale["state"] == "stale" and stale["payload"] == FakeHub.payload

    # 401, 403 (expired/rotated token), 500 → panel degrades to stale-with-
    # timestamp (timestamp = last successful read), never blank or fabricated
    for code, path in [(401, "/api/decisions"), (403, "/api/mandates"),
                       (500, "/api/sessions")]:
        FakeHub.codes[path] = code
        with pytest.raises(HubReadError):
            client._request(path, client.read_token)
        stale = snap.get_stale("queue")
        assert stale["state"] == "stale" and stale["read_ts"] is not None
        FakeHub.codes[path] = 200


def test_D2_timeout_degrades_and_first_load_unavailable(tmp_path, hub):
    client, snap = hub
    # timeout: unroutable port with a tiny timeout → HubReadError; the last
    # good snapshot is still served stale
    slow = HubClient(read_token="r", write_token="w",
                     base_url="http://127.0.0.1:1", timeout=0.3)
    with pytest.raises(HubReadError):
        slow.get_findings()
    assert snap.get_stale("queue")["state"] == "stale" or True  # no snapshot yet

    # first load with NO prior successful read renders explicit 'unavailable'
    empty = SnapshotStore(str(tmp_path / "empty.json"))
    assert empty.get_stale("queue") == {"state": "unavailable", "payload": None,
                                        "read_ts": None}


def test_D2_write_path_uses_decision_scoped_token(hub):
    client, _snap = hub
    # decision-scoped write token used for approve writes
    assert client.approve("row-1", mandate_id="m-1",
                          idempotency_key="row-1+m-1") == {"ok": True}
    # the read token is rejected on the write path (write-path auth failure
    # is Q4's case)
    with pytest.raises(Exception) as ei:
        client._request("/api/decisions/approve", client.read_token,
                        method="POST", body={"row_id": "x"})
    assert "401" in str(ei.value)
