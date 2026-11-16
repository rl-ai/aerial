"""V9 acceptance tests for the RC1 sparkline strip (console-rc-ui.md §8).

Fixture store: a real 7-day snapshot dir built per test run — a5-runtime.db
with a job_runs table (the ai-ops job_run.py schema shape job_runs.py
tolerates), a model-calls.jsonl ledger with the tailer's field conventions,
and a scrapes.db with the scrape_intel items schema. Expected values are
recomputed independently with direct SQL — never invented numbers.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_DIR = os.path.dirname(HERE)  # .../build/dashboard
sys.path.insert(0, DASHBOARD_DIR)

from console import sparklines  # noqa: E402

DAY = 86400.0


def _day(ts: float) -> int:
    return int(ts // DAY)


def _make_snapshot_dir(tmp_path, now: float, n_days: int = 7):
    """Build a real fixture snapshot store; returns (dir, expected)."""
    d = tmp_path / "snapshot"
    d.mkdir()
    # a5-runtime.db: job_runs with evidence_json; days 0..n_days-1 ago
    db = d / "a5-runtime.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE job_runs (id INTEGER PRIMARY KEY, project TEXT,"
                 " stage_type TEXT, status TEXT, evidence_json TEXT,"
                 " completed_at REAL, duration_hours REAL)")
    expected_fr = {}
    for day in range(n_days):
        ts = now - (day + 0.5) * DAY
        n = 2 + day  # jobs per day
        fails = day % 3
        for j in range(n):
            passed = j >= fails
            stages = {"stages": {"s1": {"status": "pass" if passed else "fail"}}}
            conn.execute("INSERT INTO job_runs (project, stage_type, status,"
                         " evidence_json, completed_at) VALUES (?,?,?,?,?)",
                         ("proj", "build", "done", json.dumps(stages), ts))
        ev_total = n
        ev_fail = fails
        expected_fr[_day(ts)] = round(100.0 * ev_fail / ev_total, 1)
    conn.commit()
    conn.close()
    # model-calls.jsonl
    expected_spend = {}
    with open(d / "model-calls.jsonl", "wb") as f:
        for day in range(n_days):
            ts = now - (day + 0.3) * DAY
            recs = [{"item": "t1", "in_tokens": 100 * (day + 1),
                     "out_tokens": 50 * (day + 1), "ts": ts}]
            # one malformed line (skipped) and an extra record same day
            if day == 2:
                recs.append({"item": "t2", "in_tokens": 700, "out_tokens": 300,
                             "ts": ts})  # same bucket, same day guaranteed
            for rec in recs:
                f.write((json.dumps(rec) + "\n").encode())
            for rec in recs:
                k = _day(rec["ts"])
                expected_spend[k] = expected_spend.get(k, 0) + \
                    rec["in_tokens"] + rec["out_tokens"]
        f.write(b'{"item": "t3", "in_tok')  # partial trailing line
    # scrapes.db: two topics, 'agent-runtimes' wins overall
    sdb = d / "scrapes.db"
    conn = sqlite3.connect(sdb)
    conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, source_id TEXT,"
                 " topic TEXT, guid TEXT, title TEXT, url TEXT, body TEXT,"
                 " published REAL, fetched_at REAL, run_id TEXT)")
    expected_theme = {}
    for day in range(n_days):
        ts = now - (day + 0.1) * DAY
        for i in range(day + 1):  # agent-runtimes: 1..7/day
            conn.execute("INSERT INTO items (topic, title, body, published)"
                         " VALUES (?,?,?,?)",
                         ("agent-runtimes", f"t{day}-{i}", "body", ts))
        if day < 2:
            conn.execute("INSERT INTO items (topic, title, body, published)"
                         " VALUES (?,?,?,?)", ("other-topic", "x", "b", ts))
        expected_theme[_day(ts)] = expected_theme.get(_day(ts), 0) + day + 1
    conn.commit()
    conn.close()
    return str(d), {"failure_rate": [expected_fr[k] for k in sorted(expected_fr)],
                    "spend": [float(expected_spend[k]) for k in sorted(expected_spend)],
                    "theme_momentum": [float(expected_theme[k])
                                       for k in sorted(expected_theme)],
                    "theme_name": "agent-runtimes"}


@pytest.fixture(scope="module")
def snapshot(tmp_path_factory):
    now = time.time()
    d, expected = _make_snapshot_dir(tmp_path_factory.mktemp("snap"), now)
    return d, expected


def test_collect_series_matches_aggregates(snapshot):
    d, expected = snapshot
    series = sparklines.collect_series(d)
    assert list(series) == ["failure_rate", "spend", "theme_momentum"]
    for key in ("failure_rate", "spend", "theme_momentum"):
        assert len(series[key]) == 7, key
        assert series[key] == pytest.approx(expected[key]), key


def test_collect_series_empty_dir(tmp_path):
    series = sparklines.collect_series(str(tmp_path / "nothing"))
    assert series == {"failure_rate": [], "spend": [], "theme_momentum": []}


def test_render_sparklines_structure(snapshot):
    d, expected = snapshot
    series = sparklines.collect_series(d)
    html = sparklines.render_sparklines(
        series, meta={"theme_momentum_label": expected["theme_name"]})
    # three charts
    assert html.count("<svg") == 3
    for key in ("failure_rate", "spend", "theme_momentum"):
        assert f'spark-{key}' in html
    # label + current value + delta arrow present (first day != last day)
    assert "▲" in html or "▼" in html
    assert "spark-value" in html
    # theme label carries the theme name
    assert expected["theme_name"] in html
    # fixed size, no JS chart library
    assert 'width="120"' in html and 'height="28"' in html
    assert "<script" not in html and "chart" not in html.lower().replace("sparkline", "")


def test_render_no_interactivity(snapshot):
    """V9: not clickable, no tooltips, no hover/pointer handlers."""
    d, _ = snapshot
    html = sparklines.render_sparklines(sparklines.collect_series(d))
    for bad in ("onclick", "onmouseover", "onmouseenter", "onpointer",
                "pointer-events", "cursor:", "title=", "tooltip",
                "<a ", "<button", "drill", "data-href"):
        assert bad not in html.lower(), bad


def test_render_partial_days_label(tmp_path):
    """Fewer than 7 days: render what exists, explicit days: N, no padding."""
    now = time.time()
    d, expected = _make_snapshot_dir(tmp_path, now, n_days=3)
    series = sparklines.collect_series(d)
    for key in ("failure_rate", "spend", "theme_momentum"):
        assert len(series[key]) == len(expected[key])
    n = len(series["spend"])
    html = sparklines.render_sparklines(series)
    assert html.count(f"days: {n}") == 3
    assert "days: 7" not in html


def test_render_empty_series():
    html = sparklines.render_sparklines({"failure_rate": [], "spend": [],
                                         "theme_momentum": []})
    assert html.count("days: 0") == 3
    assert "n/a" in html


def test_v10_partial_board_placement(snapshot):
    """Fixture board dict demonstrating placement: sparkline strip directly
    under exec summary (integration into render_console_html is lane 6)."""
    d, expected = snapshot
    series = sparklines.collect_series(d)
    fragment = sparklines.render_sparklines(
        series, meta={"theme_momentum_label": expected["theme_name"]})
    board = {
        "exec_summary_html": "<section class=exec-summary>headlines…</section>",
        "sparklines_html": fragment,
        "zone_b_html": "<section class=zone-b></section>",
    }
    page = board["exec_summary_html"] + board["sparklines_html"] + board["zone_b_html"]
    exec_pos = page.index("exec-summary")
    strip_pos = page.index('id="sparkline-strip"')
    zone_pos = page.index("zone-b")
    assert exec_pos < strip_pos < zone_pos
