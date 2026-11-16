"""Lane 3 regressions: single UTC window reconciliation, exit-75 = skipped,
section dedup, honest `unavailable` markers (never 0 or n/a), dogfood path."""

from __future__ import annotations

import json
import os
import sqlite3
import time

import pytest

from dashboard import exec_summary
from dashboard.exec_summary import (
    classify_run, gather, gather_job_runs, numbers_strip, summary_html,
    yesterday_window, DEFAULT_DOGFOOD_LOG,
)


# ---------------------------------------------------------------- helpers

def _make_hub_db(path, rows):
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE job_runs (job_id TEXT, status TEXT, exit_code INTEGER,"
                 " started_at REAL, ended_at REAL, duration_s REAL, summary TEXT)")
    conn.executemany("INSERT INTO job_runs VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def _win(now):
    return yesterday_window(now)


# ---------------------------------------------------------------- window reconciliation

def test_classify_exit75_is_skipped_never_failed():
    assert classify_run({"status": "", "exit_code": 75}) == "skipped"
    assert classify_run({"status": "skipped", "exit_code": 75}) == "skipped"
    # a real failure status overrides even exit 75? No — exit-75 is the
    # curator-poll signature; but explicit failed status is a real failure.
    assert classify_run({"status": "failed", "exit_code": 75}) == "failed"


def test_classify_partitions_every_row():
    rows = [
        {"status": "done", "exit_code": 0},
        {"status": "failed", "exit_code": 1},
        {"status": "skipped", "exit_code": 75},
        {"status": "running", "exit_code": None},
        {"status": "", "exit_code": None},
    ]
    classes = [classify_run(r) for r in rows]
    assert classes == ["ok", "failed", "skipped", "running", "running"]
    # every row got exactly one class (no row counted twice, none dropped)


def test_window_reconciliation_mixed_counts_sum_to_total(tmp_path, now):
    start, end = _win(now)
    rows = [
        ("j-ok", "done", 0, start + 10, end, 60.0, "fine"),
        ("j-fail", "failed", 1, start + 20, end, 30.0, "boom"),
        ("j-skip75", "", 75, start + 30, end, 0.0, "curator poll skip"),
        ("j-run", "running", None, start + 40, None, None, "in flight"),
        ("j-ok2", "ok", 0, start + 50, end, 120.0, "also fine"),
    ]
    db = tmp_path / "hub.db"
    _make_hub_db(db, rows)
    out = gather_job_runs(str(db), (start, end))
    assert out["available"]
    assert out["runs"] == 5
    assert out["ok"] == 2
    assert out["failed"] == 1
    assert out["skipped"] == 1
    assert out["running"] == 1
    assert out["ok"] + out["failed"] + out["skipped"] + out["running"] == out["runs"]
    assert out["reconciled"] is True
    # failures list reconciles with failed count — the I2 6-vs-2 class is gone
    assert len(out["failures"]) == out["failed"] == 1
    # exit-75 row never counted as a failure
    assert all("j-skip75" not in json.dumps(f) for f in out["failures"])


def test_rows_outside_window_excluded(tmp_path, now):
    start, end = _win(now)
    rows = [
        ("in-win", "done", 0, start + 5, end, 10.0, ""),
        ("before", "done", 0, start - 5, start, 10.0, ""),
        ("after", "done", 0, end + 5, end + 6, 10.0, ""),
    ]
    db = tmp_path / "hub.db"
    _make_hub_db(db, rows)
    out = gather_job_runs(str(db), (start, end))
    assert out["runs"] == 1 and out["ok"] == 1


# ---------------------------------------------------------------- unavailable markers

def test_numbers_strip_unavailable_not_zero_or_na(now):
    data = {
        "job_runs": {"available": False, "detail": "hub.db not found"},
        "model_calls": {"available": False, "detail": "ledger not found"},
        "ingest": {"available": True, "items": None, "detail": "no counts"},
    }
    strip = numbers_strip(data)
    assert "unavailable" in strip
    assert "n/a" not in strip
    # never a bare fabricated 0 for a missing source
    assert "0 runs" not in strip
    assert "0 calls" not in strip


def test_numbers_strip_real_numbers_when_available(tmp_path, now):
    start, end = _win(now)
    db = tmp_path / "hub.db"
    _make_hub_db(db, [("a", "done", 0, start + 1, end, 3600.0, ""),
                      ("b", "skipped", 75, start + 2, end, 0.0, "")])
    data = {
        "job_runs": gather_job_runs(str(db), (start, end)),
        "model_calls": {"available": True, "calls": 7, "in_tokens": 100, "out_tokens": 50},
        "ingest": {"available": True, "items": 3},
    }
    strip = numbers_strip(data)
    assert "2 runs" in strip and "1 ok" in strip
    assert "0 failed" in strip and "1 skipped" in strip
    assert "3 items ingested" in strip
    assert "7 calls / 150 tokens" in strip
    assert "1.0 agent-hours" in strip
    assert "unavailable" not in strip and "n/a" not in strip


def test_window_totals_missing_ledger_unavailable(tmp_path):
    out = exec_summary.window_totals(str(tmp_path / "nope.jsonl"), (0.0, 1.0))
    assert out.get("available") is False


def test_gather_declares_one_window_and_derives_all(now):
    data = gather(now=now, hub_db="/nonexistent/hub.db",
                  ledger="/nonexistent/ledger.jsonl",
                  bridge_log="/nonexistent/bridge.log",
                  digest="/nonexistent/digest.json",
                  dogfood_log="/nonexistent/dogfood.md",
                  scheduler_log="/nonexistent/scheduler.log",
                  scrapes_db="/nonexistent/scrapes.db")
    win = data["window"]
    assert win["end"] - win["start"] == 86400.0
    # every unavailable source degrades explicitly, never fabricated
    for key in ("job_runs", "model_calls", "ingest", "dogfood", "shadow"):
        assert data[key]["available"] is False


# ---------------------------------------------------------------- section dedup

def _fake_ds_runner_structured(ask_path, out_path):
    text = (
        "### 3 headlines\n- one\n- two\n- three\n"
        "### Numbers strip\n- 5 runs · 1 failed\n"
        "### Decisions needed\n- none\n"
        "### Risks/watch\n- none flagged\n")
    with open(out_path, "w") as f:
        f.write(text)
    return 0


def test_structured_sections_render_exactly_once(now):
    start, end = _win(now)
    data = {"window": {"start": start, "end": end},
            "job_runs": {"available": True, "runs": 5, "ok": 4, "failed": 1,
                         "skipped": 0, "running": 0, "failures": [],
                         "agent_hours": 1.0},
            "model_calls": {"available": True, "calls": 1, "in_tokens": 1, "out_tokens": 1},
            "ingest": {"available": True, "items": 1},
            "scrape_intel": {"available": False}}
    data["numbers_strip"] = numbers_strip(data)
    _, ds = exec_summary.write_summary_section(data, _fake_ds_runner_structured)
    html = summary_html(data, ds)
    assert html.count("Numbers:") == 1          # local strip rendered once
    assert html.count("Headlines") == 1
    assert html.count("Decisions needed") == 1
    assert html.count("Risks/watch") == 1
    # the DS raw output (with its own Numbers strip) must not leak into HTML
    assert "5 runs · 1 failed" not in html


def test_raw_mode_drops_duplicate_numbers_section():
    raw = ("### 3 headlines\n- h1\n### Numbers strip\n- 9 runs · 2 failed\n"
           "### Risks/watch\n- watch the thing\n")
    data = {"numbers_strip": "5 runs · 1 ok", "scrape_intel": {"available": False}}
    ds = {"mode": "raw", "numbers_strip": "5 runs · 1 ok", "raw": raw}
    html = summary_html(data, ds)
    assert html.count("<b>Numbers:</b>") == 1
    assert "9 runs" not in html          # duplicate strip section dropped
    assert "watch the thing" in html     # rest of raw output preserved


def test_fallback_mode_single_strip():
    data = {"numbers_strip": "3 runs · 0 ok · 1 failed · 1 skipped · unavailable · "
                             "unavailable calls / unavailable tokens · 0.50 agent-hours",
            "scrape_intel": {"available": False}}
    ds = {"mode": "fallback", "numbers_strip": data["numbers_strip"],
          "detail": "model_call.py exit 7"}
    html = summary_html(data, ds)
    assert html.count("<b>Numbers:</b>") == 1
    assert "exec summary unavailable" in html


# ---------------------------------------------------------------- dogfood path

def test_dogfood_path_is_real_gateway_v1_path():
    assert DEFAULT_DOGFOOD_LOG == os.path.expanduser(
        "~/workspaces/build/second-brain-gateway/v1/dogfood/dogfood-log.md")
    assert "services/gateway" not in DEFAULT_DOGFOOD_LOG


def test_gather_dogfood_missing_source_unavailable(now):
    out = exec_summary.gather_dogfood("/nonexistent/dogfood.md", _win(now))
    assert out["available"] is False
    assert out["detail"]
