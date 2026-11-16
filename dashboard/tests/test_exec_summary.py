"""Hermetic tests for the exec-summary layer (task t_f74c3f97).

All sources are fixtures in tmp_path; the DS call is injectable and stubbed —
tests never hit a model. Mixed int/ISO timestamps exercise _norm_ts.
"""
import json
import os
import sqlite3
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
if PKG not in sys.path:
    sys.path.insert(0, PKG)

import exec_summary as es  # noqa: E402

# Fixed "now": 2026-11-12 12:00:00 UTC. Yesterday = 2026-11-11 00:00–24:00 UTC.
NOW = 1_794_484_800.0  # 2026-11-12 12:00:00 UTC
W0, W1 = 1_794_355_200.0, 1_794_441_600.0  # 2026-11-11T00:00:00Z .. 2026-11-12T00:00:00Z


@pytest.fixture
def window():
    return (W0, W1)


def _iso(day, h=12, m=0):
    return f"2026-11-{day:02d}T{h:02d}:{m:02d}:00Z"


@pytest.fixture
def hub_db(tmp_path):
    db = str(tmp_path / "hub.db")
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE job_runs (run_id INTEGER, job_id TEXT, node TEXT,"
        " started_at, ended_at, status TEXT, exit_code INTEGER,"
        " duration_s REAL, summary TEXT, log_tail TEXT, meta TEXT, pid INTEGER)")
    rows = [
        # in-window (started 2026-11-11): ok, ok, failed, failed
        (1, "bridge", "ingest", W0 + 3600, W0 + 7200, "done", 0, 3600, "fine", "", "{}", 1),
        (2, "bridge", "digest", _iso(11, 3), W0 + 5 * 3600, "done", 0, 7200, "", "", "{}", 1),
        (3, "render", "render", _iso(11, 9), None, "failed", 2, 60, "exit 2: missing token", "", "{}", 1),
        # in-window but non-ok exit code with "done" status -> failed
        (4, "sched", "fire", _iso(11, 15), None, "done", 1, 30, "boom", "", "{}", 1),
        # out of window (2026-11-12 05:00 and 2026-11-10)
        (5, "later", "x", _iso(12, 5), None, "done", 0, 100, "", "", "{}", 1),
        (6, "older", "x", _iso(10, 5), None, "done", 0, 100, "", "", "{}", 1),
        # in-window but missing started_at -> excluded
        (7, "nots", "x", None, None, "done", 0, 10, "", "", "{}", 1),
    ]
    con.executemany("INSERT INTO job_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return db


def test_norm_ts_mixed():
    assert es._norm_ts(W0) == W0
    assert es._norm_ts(W0 * 1000) == W0               # ms epoch
    assert es._norm_ts("2026-11-11T00:00:00Z") == W0
    assert es._norm_ts("2026-11-11T00:00:00+00:00") == W0
    assert es._norm_ts("2026-11-11 00:00:00") == W0   # naive -> UTC
    assert es._norm_ts(None) is None
    assert es._norm_ts("garbage") is None


def test_yesterday_window_utc_boundary():
    s, e = es.yesterday_window(NOW)
    assert (s, e) == (W0, W1)


def test_gather_job_runs(hub_db, window):
    r = es.gather_job_runs(hub_db, window)
    assert r["available"] and r["runs"] == 4
    assert r["ok"] == 2 and r["failed"] == 2
    assert {f["job_id"] for f in r["failures"]} == {"render", "sched"}
    assert any("missing token" in f["summary"] for f in r["failures"])
    # agent-hours: 3600+7200+60+30 s = 10890 s = 3.025 h
    assert abs(r["agent_hours"] - 3.03) < 0.01


def test_gather_job_runs_missing_db(tmp_path, window):
    r = es.gather_job_runs(str(tmp_path / "nope.db"), window)
    assert not r["available"]


def test_window_totals_ledger(tmp_path, window):
    led = tmp_path / "model-calls.jsonl"
    lines = [
        {"item": "a", "in_tokens": 100, "out_tokens": 50, "ts": W0 + 10},       # in
        {"item": "b", "in_tokens": 10, "out_tokens": 5, "ts": _iso(11, 5)},     # in (ISO)
        {"item": "c", "in_tokens": 999, "out_tokens": 999, "ts": _iso(12, 1)},  # out
        {"item": "d", "in_tokens": 1, "out_tokens": 1},                          # no ts
        "not json at all\n",                                                     # malformed
    ]
    with open(led, "w") as f:
        for ln in lines:
            f.write(json.dumps(ln) if not isinstance(ln, str) else ln)
            f.write("\n")
    r = es.window_totals(str(led), window)
    assert r["available"]
    assert r["calls"] == 2
    assert r["in_tokens"] == 110 and r["out_tokens"] == 55
    assert r["no_ts_lines"] == 1


def test_window_totals_missing(tmp_path, window):
    assert not es.window_totals(str(tmp_path / "x.jsonl"), window)["available"]


def test_gather_ingest_digest_and_log(tmp_path, window):
    blog = tmp_path / "brain-bridge.log"
    blog.write_text(
        f"2026-11-11T02:00:00Z ingested 12 items\n"
        f"2026-11-11T03:00:00Z ingest ok\n"
        f"2026-11-12T01:00:00Z ingested 5 items\n"
        f"2026-11-10T02:00:00Z ingested 99 items\n")
    dg = tmp_path / "digest-latest.json"
    dg.write_text(json.dumps({"ingested": ["a", "b", "c"]}))
    r = es.gather_ingest(str(blog), str(dg), window)
    assert r["available"] and r["items"] == 3  # digest is authoritative


def test_gather_ingest_log_only(tmp_path, window):
    blog = tmp_path / "brain-bridge.log"
    blog.write_text("2026-11-11T02:00:00Z ingested 12 items\n"
                    "2026-11-12T01:00:00Z ingested 5 items\n")
    r = es.gather_ingest(str(blog), str(tmp_path / "no-digest.json"), window)
    assert r["available"] and r["items"] == 1


def test_gather_ingest_unavailable(tmp_path, window):
    r = es.gather_ingest(str(tmp_path / "a.log"), str(tmp_path / "b.json"), window)
    assert not r["available"]


def test_gather_dogfood(tmp_path, window):
    dl = tmp_path / "dogfood-log.md"
    dl.write_text(
        "# dogfood log\n"
        "| 2026-11-11 | eva | tool | anomaly | ok |\n"
        "| 2026-11-12 | eva | tool | later | ok |\n"
        "| 2026-11-10 | eva | tool | old | ok |\n")
    r = es.gather_dogfood(str(dl), window)
    assert r["available"] and r["count"] == 1
    assert "anomaly" in r["new_anomaly_lines"][0]


def test_gather_dogfood_unavailable(tmp_path, window):
    assert not es.gather_dogfood(str(tmp_path / "x.md"), window)["available"]


def test_gather_shadow(tmp_path, window):
    sl = tmp_path / "scheduler.log"
    sl.write_text(
        "2026-11-11T01:00:00Z started cmd\n"
        "2026-11-11T01:05:00Z finished exit=0\n"
        "2026-11-11T05:00:00Z skipped another run in flight\n"
        "2026-11-12T01:00:00Z started cmd\n"
        "2026-11-10T01:00:00Z started cmd\n")
    r = es.gather_shadow(str(sl), window)
    assert r["available"] and r["fired"] == 1 and r["skipped"] == 1


def test_gather_shadow_unavailable(tmp_path, window):
    assert not es.gather_shadow(str(tmp_path / "x.log"), window)["available"]


def test_numbers_strip_local_and_unavailable(hub_db, tmp_path, window):
    data = {
        "job_runs": es.gather_job_runs(hub_db, window),
        "model_calls": es.window_totals(str(tmp_path / "no.jsonl"), window),
        "ingest": es.gather_ingest(str(tmp_path / "n.log"), str(tmp_path / "n.json"), window),
    }
    strip = es.numbers_strip(data)
    # v2 spec §3.3: the FIRST unavailable source gets the '(source gap)' suffix.
    assert strip.startswith("4 runs · 2 ok · 2 failed · 0 skipped · unavailable · (source gap) items ingested · unavailable calls")
    assert "3.0 agent-hours" in strip  # 10890 s = 3.025 h -> 3.0


def test_gather_end_to_end_fixture(hub_db, tmp_path):
    data = es.gather(now=NOW, hub_db=hub_db,
                     ledger=str(tmp_path / "n.jsonl"),
                     bridge_log=str(tmp_path / "n.log"),
                     digest=str(tmp_path / "n.json"),
                     dogfood_log=str(tmp_path / "n.md"),
                     scheduler_log=str(tmp_path / "n.log"))
    assert data["job_runs"]["runs"] == 4
    assert not data["model_calls"]["available"]
    assert not data["shadow"]["available"]
    assert "unavailable" in data["numbers_strip"]


def test_ds_stub_structured(hub_db, tmp_path):
    data = es.gather(now=NOW, hub_db=hub_db, ledger=str(tmp_path / "n.jsonl"),
                     bridge_log=str(tmp_path / "n.log"), digest=str(tmp_path / "n.json"),
                     dogfood_log=str(tmp_path / "n.md"), scheduler_log=str(tmp_path / "n.log"))

    def fake_runner(ask_path, out_path):
        assert os.path.exists(ask_path)
        with open(out_path, "w") as f:
            f.write("### 3 headlines\n- Bridge ingested items.\n- Render failed.\n- Scheduler fired.\n"
                    "### Numbers strip\n- 4 runs · 2 failed · 3 items ingested · 2 calls / 165 tokens · 3.03 agent-hours\n"
                    "### Decisions needed\n- none\n"
                    "### Risks/watch\n- none flagged\n")
        return 0

    html, meta = es.write_summary_section(data, ds_runner=fake_runner)
    assert meta["mode"] == "structured"
    section = es.summary_html(data, meta)
    assert "<!-- exec-summary -->" in section
    assert "Headlines" in section and "Bridge ingested items." in section
    assert "none flagged" in section


def test_ds_failure_fallback_raw_strip(hub_db, tmp_path):
    data = es.gather(now=NOW, hub_db=hub_db, ledger=str(tmp_path / "n.jsonl"),
                     bridge_log=str(tmp_path / "n.log"), digest=str(tmp_path / "n.json"),
                     dogfood_log=str(tmp_path / "n.md"), scheduler_log=str(tmp_path / "n.log"))

    def failing_runner(ask_path, out_path):
        return 1  # model_call.py failure

    html, meta = es.write_summary_section(data, ds_runner=failing_runner)
    assert meta["mode"] == "fallback"
    assert meta["note"] == "exec summary unavailable"
    section = es.summary_html(data, meta)
    assert "<!-- exec-summary -->" in section
    assert "exec summary unavailable" in section
    assert "4 runs · 2 ok · 2 failed" in section  # local, never fabricated


def test_parse_ds_output_tolerant():
    text = "**3 headlines**\n- one\n**Numbers strip**\n- 1 runs\n**Decisions needed**\n1. do X\n**Risks/watch**\n- watch out\n"
    b = es.parse_ds_output(text)
    assert "one" in b["headlines"] and "do X" in b["decisions"] and "watch out" in b["risks"]


def test_render_daily_exec_summary_on_and_off(hub_db, tmp_path, monkeypatch):
    """End-to-end render: section present with DS stub; absent with --no-exec-summary."""
    import render_daily as rd
    from dashboard.exception_queue import queue as rd_q
    out = str(tmp_path / "board.html")
    argv = ["--hub-db", hub_db, "--hub-url", "http://127.0.0.1:1",
            "--hub-token-file", str(tmp_path / "no-token"),
            "--ledger", str(tmp_path / "no.jsonl"),
            "--queue-policy", os.path.join(os.path.dirname(rd_q.__file__), "queue-policy.yaml"),
            "--snapshot", str(tmp_path / "snap.json"),
            "--sessions-mirror", str(tmp_path / "sm.json"),
            "--tail-state", str(tmp_path / "ts.json"),
            "--exec-hub-db", hub_db, "--exec-ledger", str(tmp_path / "no.jsonl"),
            "--exec-bridge-log", str(tmp_path / "n.log"), "--exec-digest", str(tmp_path / "n.json"),
            "--exec-dogfood-log", str(tmp_path / "n.md"),
            "--exec-scheduler-log", str(tmp_path / "n.log"),
            "--out", out]

    def stub_runner(ask_path, out_path):
        with open(out_path, "w") as f:
            f.write("### 3 headlines\n- One.\n- Two.\n- Three.\n"
                    "### Numbers strip\n- strip\n### Decisions needed\n- none\n"
                    "### Risks/watch\n- none flagged\n")
        return 0

    monkeypatch.setattr(es, "default_ds_runner", stub_runner)
    rc = rd.main(argv)
    assert rc == 0
    body = open(out).read()
    assert "<!-- exec-summary -->" in body
    assert "Executive summary" in body and "One." in body

    rc = rd.main(argv + ["--no-exec-summary"])
    assert rc == 0
    body2 = open(out).read()
    assert "<!-- exec-summary -->" not in body2
