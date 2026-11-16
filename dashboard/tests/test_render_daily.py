"""Hermetic acceptance test for render_daily.py (gap B1).

Builds fixture copies of hub.db (real job_runs schema) + a small ledger JSONL
+ a stub hub API server; points render_daily's CLI at the fixtures. Never
touches the real production sources.
"""
import http.server
import json
import os
import sqlite3
import sys
import threading
import time

import pytest

LANE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if LANE not in sys.path:
    sys.path.insert(0, LANE)

T0 = 1_000_000.0
H = 3600.0
TOKEN = "test-read-token"

HUB_SCHEMA = """
CREATE TABLE job_runs (
    run_id TEXT PRIMARY KEY, job_id TEXT, node TEXT,
    started_at REAL, ended_at REAL, status TEXT, exit_code INTEGER,
    duration_s REAL, summary TEXT, log_tail TEXT, meta TEXT, pid INTEGER);
"""


def make_hub_db(path, rows=None):
    if rows is None:
        rows = [
            ("r1", "ingest-bridge", "fetch", T0 - 86400 * 3, T0 - 86400 * 3 + 3600,
             "done", 0, 3600.0, "ok", "", "{}", 100),
            ("r2", "ingest-bridge", "load", T0 - 86400 * 2, T0 - 86400 * 2 + 7200,
             "done", 0, 7200.0, "ok", "", "{}", 101),
            ("r3", "curator", "curate", T0 - 86400, T0 - 86400 + 1800,
             "done", 0, 1800.0, "ok", "", "{}", 102),
            ("r4", "curator", "review", T0 - 86400 * 5, T0 - 86400 * 5 + 3600,
             "done", 0, 3600.0, "ok", "", "{}", 103),
            ("r5", "curator", "review", T0 - 86400 * 4, T0 - 86400 * 4 + 7200,
             "done", 0, 7200.0, "ok", "", "{}", 104),
            ("r6", "curator", "review", T0 - 7200, None,
             "running", None, None, "in progress", "", "{}", 105),
        ]
    con = sqlite3.connect(path)
    con.execute(HUB_SCHEMA)
    con.executemany("INSERT INTO job_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return path


def make_ledger(path):
    with open(path, "w") as f:
        for _ in range(4):
            f.write(json.dumps({"item": "ingest-bridge", "in_tokens": 100,
                                "out_tokens": 50}) + "\n")
        f.write(json.dumps({"item": "curator", "in_tokens": 10,
                            "out_tokens": 5}) + "\n")
        f.write(json.dumps({"note": "untagged line"}) + "\n")
    return path


FINDINGS = [
    {"condition_key": "f1", "claim": "ledger drift detected", "impact_class": "degraded",
     "admission_score": 0.8, "eval_passed": True, "evidence_label": "verified",
     "source_agent": "curator", "proposed_action": "reconcile ledger"},
    {"condition_key": "f2", "claim": "policy: widen mandate scope", "impact_class": "routine",
     "admission_score": 0.6, "eval_passed": True, "evidence_label": "inferred",
     "source_agent": "reviewer", "proposed_action": "update mandate policy"},
]
MANDATES = [{"mandate_id": "m-1", "scope": {"lane": "ingest"}, "expiry_at": "2026-12-01T00:00:00Z"}]


class _Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._send({"error": "unauthorized"}, 401)
            return
        if self.path == "/api/findings":
            self._send(FINDINGS)
        elif self.path == "/api/mandates":
            self._send(MANDATES)
        elif self.path == "/api/decisions":
            self._send([])
        else:
            self._send({"error": "not found"}, 404)

    def log_message(self, *a):  # silence
        pass


@pytest.fixture
def hub_server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def tokens(tmp_path):
    tf = tmp_path / "read-token"
    tf.write_text(TOKEN + "\n")
    return str(tf)


def run_render(tmp_path, hub_db, hub_url, token_file, ledger, monkeypatch):
    out = str(tmp_path / "dashboard.html")
    from dashboard.render_daily import main
    rc = main(["--hub-db", hub_db, "--hub-url", hub_url,
               "--hub-token-file", token_file, "--ledger", ledger,
               "--out", out,
               "--snapshot", str(tmp_path / "snap.json"),
               "--sessions-mirror", str(tmp_path / "mirror.json"),
               "--tail-state", str(tmp_path / "tail-state.json")])
    assert rc == 0
    with open(out) as f:
        return f.read()


def test_render_daily_full_sources(tmp_path, hub_server, tokens, monkeypatch):
    hub_db = make_hub_db(str(tmp_path / "hub.db"))
    ledger = make_ledger(str(tmp_path / "model-calls.jsonl"))
    html = run_render(tmp_path, hub_db, hub_server, tokens, ledger, monkeypatch)

    # Zone A: real rendered rows, one per job_id, with status/pct/ETA
    assert "ingest-bridge" in html and "curator" in html
    assert 'class="done-since-last-visit"' in html
    assert 'class="working"' in html or "working" in html
    assert "100%" in html           # ingest-bridge: 2 of 2 nodes done
    assert "50%" in html            # curator: curate done, review in progress
    assert "1h–2h" in html          # ETA from observed review-node durations
    # Token stats from the tailer (real aggregate: 4 calls ingest-bridge)
    assert "400" in html and "200" in html
    assert "untagged" in html
    # Zone B: hub findings admitted through the real queue-policy
    assert "ledger drift detected" in html
    assert "Panel A" in html
    assert "approve / dismiss / escalate" in html
    # Zone C: mandates from the hub API
    assert "m-1" in html and "2026-12-01T00:00:00Z" in html
    # no degraded banners when everything is live
    assert 'data-degraded' not in html
    # self-contained single file
    assert html.startswith("<!DOCTYPE html>") and "</html>" in html
    assert "<script" not in html  # static, self-contained


def test_render_daily_degraded_sources(tmp_path, tokens, monkeypatch):
    # hub.db missing -> Zone A unavailable; hub API on a closed port -> Zone B/C unavailable
    ledger = make_ledger(str(tmp_path / "model-calls.jsonl"))
    html = run_render(tmp_path, str(tmp_path / "missing.db"),
                      "http://127.0.0.1:1", tokens, ledger, monkeypatch)
    assert 'data-degraded="unavailable"' in html
    assert 'data-panel="Zone A"' in html
    assert 'data-panel="Zone B"' in html
    assert 'data-panel="Zone C"' in html
    assert "unavailable" in html
    # token stats still render from the ledger (independent source)
    assert "Token stats" in html and "ingest-bridge" in html


def test_render_daily_stale_snapshot(tmp_path, hub_server, tokens, monkeypatch):
    """A prior good read then a broken source -> stale-with-timestamp marker."""
    hub_db = make_hub_db(str(tmp_path / "hub.db"))
    ledger = make_ledger(str(tmp_path / "model-calls.jsonl"))
    snap = str(tmp_path / "snap.json")
    from dashboard.render_daily import main
    common = ["--hub-db", hub_db, "--hub-url", hub_server, "--hub-token-file", tokens,
              "--ledger", ledger, "--snapshot", snap,
              "--sessions-mirror", str(tmp_path / "mirror.json"),
              "--tail-state", str(tmp_path / "tail-state.json")]
    assert main(common + ["--out", str(tmp_path / "a.html")]) == 0
    os.remove(hub_db)  # break Zone A only
    html = main(common + ["--out", str(tmp_path / "b.html")]) and None
    with open(str(tmp_path / "b.html")) as f:
        html = f.read()
    assert 'data-degraded="stale"' in html or 'data-degraded="unavailable"' in html
    assert "Zone A" in html
    # the previously-good Zone A rows come back from the snapshot when stale
    if 'data-degraded="stale"' in html:
        assert "ingest-bridge" in html


def test_render_daily_bad_token_degrades(tmp_path, hub_server, tmp_tokenfile=None):
    hub_db = make_hub_db(str(tmp_path / "hub.db"))
    ledger = make_ledger(str(tmp_path / "model-calls.jsonl"))
    tf = tmp_path / "bad-token"
    tf.write_text("wrong-token\n")
    html = run_render(tmp_path, hub_db, hub_server, str(tf), ledger, None)
    # Zone A fine (local db), but hub API auth fails -> Zone B unavailable
    assert "ingest-bridge" in html
    assert 'data-panel="Zone B"' in html
    assert 'data-degraded="unavailable"' in html
