"""Hermetic tests for the v2 daily exec report (task t_387e1d9a).

Covers: the agent-actions gather (window, scheduler.log parsing, shadow
workspaces, hub done-items), headline/decision/risk caps, the numbers-strip
gap labels, the email/console content split, and the email-safe format rules.
All sources are tmp_path fixtures; the DS call is stubbed; no email is sent.
"""
import json
import os
import sqlite3
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
if PKG not in sys.path:
    sys.path.insert(0, PKG)

import exec_summary as es  # noqa: E402

# Fixed "now": 2026-11-12 12:00:00 UTC. Yesterday = 2026-11-11 00:00–24:00 UTC.
NOW = 1_794_484_800.0
W0, W1 = 1_794_355_200.0, 1_794_441_600.0


@pytest.fixture
def window():
    return (W0, W1)


def _iso(day, h=12, m=0):
    return f"2026-11-{day:02d}T{h:02d}:{m:02d}:00Z"


@pytest.fixture
def sched_log(tmp_path):
    log = tmp_path / "scheduler.log"
    log.write_text(
        # in window: started + finished ok
        f"{_iso(11, 7)} started /bin/run_shadow_hierarchy.py --latest-failed\n"
        f"{_iso(11, 8)} finished exit=0 out=ok\n"
        # in window: finished with error
        f"{_iso(11, 9)} started /bin/run_shadow_hierarchy.py --latest-failed\n"
        f"{_iso(11, 10)} finished exit=2 out=boom\n"
        # out of window entirely
        f"{_iso(10, 9)} started /bin/other.py\n"
        f"{_iso(12, 9)} finished exit=0 out=later\n",
        encoding="utf-8")
    return str(log)


@pytest.fixture
def shadow_dir(tmp_path):
    d = tmp_path / "shadow"
    d.mkdir()
    for name in ("run-a", "run-b"):
        p = d / name
        p.mkdir()
        os.utime(p, (W0 + 3600, W0 + 3600))  # in window
    old = d / "run-old"
    old.mkdir()
    os.utime(old, (W0 - 86400, W0 - 86400))  # out of window
    (d / "brief.md").write_text("# brief\n", encoding="utf-8")  # file, not a run
    return str(d)


@pytest.fixture
def hub_db(tmp_path):
    db = str(tmp_path / "hub.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE items (id INTEGER, title TEXT, status TEXT,"
                " closed_at TEXT)")
    con.executemany("INSERT INTO items VALUES (?,?,?,?)", [
        (1, "fix roles-runner", "done", _iso(11, 14)),      # in window
        (2, "retry ingest", "done", _iso(11, 18)),          # in window
        (3, "old work", "done", _iso(10, 9)),               # out of window
        (4, "open work", "open", _iso(11, 9)),              # not done
    ])
    con.execute("CREATE TABLE queue_items (id INTEGER, claim TEXT, state TEXT,"
                " updated_at TEXT)")
    con.executemany("INSERT INTO queue_items VALUES (?,?,?,?)", [
        (10, "queue A", "done", _iso(11, 15)),              # handled in window
        (11, "queue B", "done", _iso(12, 15)),              # out of window
        (12, "queue C", "open", _iso(11, 15)),              # not done
    ])
    con.commit()
    con.close()
    return db


# ---------------------------------------------------------------- agent actions

def test_agent_actions_parses_scheduler_log(sched_log, shadow_dir, hub_db, window):
    a = es.gather_agent_actions(sched_log, shadow_dir, hub_db, window)
    assert a["available"]
    s = a["sources"]["scheduler"]
    assert s["available"]
    assert s["started"] == 2          # the 2026-11-10 start is out of window
    assert s["finished_ok"] == 1
    assert s["finished_err"] == 1
    assert a["sources"]["hierarchy"]["runs"] == 2   # run-old and brief.md excluded
    assert a["sources"]["hub"]["done_items"] == 2
    assert a["sources"]["hub"]["queue_handled"] == 1


def test_agent_actions_line_counts(sched_log, shadow_dir, hub_db, window):
    a = es.gather_agent_actions(sched_log, shadow_dir, hub_db, window)
    line = es.agent_actions_line(a)
    assert "completed 2 items" in line
    assert "handled 1 queue entries" in line
    assert line.count(";") >= 1  # top-3 short phrases present


def test_agent_actions_zero_state(tmp_path, window):
    # hub available but nothing done in window; no scheduler log, no shadow dir
    db = tmp_path / "hub.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE items (id INTEGER, title TEXT, status TEXT,"
                " closed_at TEXT)")
    con.execute("CREATE TABLE queue_items (id INTEGER, claim TEXT, state TEXT,"
                " updated_at TEXT)")
    con.commit()
    con.close()
    a = es.gather_agent_actions(str(tmp_path / "none.log"), str(tmp_path),
                                str(db), window)
    line = es.agent_actions_line(a)
    assert line.startswith("Agents completed 0 items")


def test_agent_actions_fail_closed(tmp_path, window):
    a = es.gather_agent_actions(str(tmp_path / "none.log"), str(tmp_path / "no"),
                                str(tmp_path / "no.db"), window)
    assert not a["available"]
    assert "source gap" in es.agent_actions_line(a)
    assert a["detail"]  # per-source details recorded, nothing fabricated


def test_agent_actions_gather_wired_into_gather(sched_log, shadow_dir, hub_db, window):
    data = es.gather(now=W1, window_mode="yesterday",
                     hub_db=hub_db, ledger="/nonexistent.jsonl",
                     bridge_log="/nonexistent.log", digest="/nonexistent.json",
                     dogfood_log="/nonexistent.md", scheduler_log=sched_log,
                     shadow_dir=shadow_dir, scrapes_db="/nonexistent.db")
    assert data["agent_actions"]["sources"]["scheduler"]["started"] == 2


# ---------------------------------------------------------------- window modes

def test_window_modes():
    y = es.window_for("yesterday", NOW)
    r = es.window_for("rolling", NOW)
    assert y == (W0, W1)
    assert r == (NOW - 86400, NOW)
    assert es.WINDOW_LABEL["rolling"] == "last 24 h"
    assert es.WINDOW_LABEL["yesterday"] == "yesterday"


def test_gather_labels_window(sched_log, shadow_dir, hub_db):
    for mode, label in (("yesterday", "yesterday"), ("rolling", "last 24 h")):
        data = es.gather(now=NOW, window_mode=mode, hub_db=hub_db,
                         ledger="/nonexistent.jsonl",
                         bridge_log="/nonexistent.log", digest="/nonexistent.json",
                         dogfood_log="/nonexistent.md", scheduler_log=sched_log,
                         shadow_dir=shadow_dir, scrapes_db="/nonexistent.db")
        assert data["window"]["label"] == label


# ---------------------------------------------------------------- gap labels

def test_numbers_strip_token_accounting_gap():
    data = {
        "job_runs": {"available": True, "runs": 5, "failed": 0, "agent_hours": 1.0},
        "model_calls": {"available": True, "calls": 160, "in_tokens": 0, "out_tokens": 0},
        "ingest": {"available": True, "items": 10},
    }
    strip = es.numbers_strip(data)
    assert "0 tokens (accounting gap)" in strip   # constant-labeled, not silent
    assert "160 calls" in strip


def test_numbers_strip_source_gap_suffix_first_only():
    data = {
        "job_runs": {"available": False},
        "model_calls": {"available": False},
        "ingest": {"available": False},
        "dogfood": {"available": False},
    }
    strip = es.numbers_strip(data)
    assert strip.count("(source gap)") == 1       # first occurrence only
    assert strip.count("unavailable") >= 4


# ---------------------------------------------------------------- caps

def test_cap_headlines():
    lines = ["- " + f"word{i} " * 30 for i in range(5)]
    kept, overflow = es.cap_headlines(lines)
    assert len(kept) == 3
    assert overflow == 2
    for h in kept:
        assert len(h.split()) <= 20 + 1  # 20 words + trailing ellipsis marker
        assert h.rstrip("…").split() and len(h.rstrip("… ").split()) <= 20


def test_cap_decisions_overflow():
    lines = [f"decision {i}" for i in range(5)]
    kept, overflow = es.cap_decisions(lines)
    assert len(kept) == 4                      # 3 numbered + overflow line
    assert kept[-1] == "+2 more on the console"
    assert kept[0].startswith("1. ")


def test_cap_risks_overflow():
    lines = [f"risk {i}" for i in range(6)]
    kept, overflow = es.cap_risks(lines)
    assert len(kept) == 5                      # 4 + overflow line
    assert kept[-1] == "+2 more on the console"


def test_ask_prompt_carries_headline_cap():
    ask = es.build_ask_text({"window": {"label": "yesterday"}})
    assert "20 words" in ask
    assert "no inline code" in ask


# ---------------------------------------------------------------- email/console split

def _ds_structured():
    return {
        "mode": "structured",
        "numbers_strip": "5 runs · 0 failed · 10 items ingested · 3 calls / 1200 · 1.0 agent-hours",
        "blocks": {
            "headlines": "- short headline one\n- short headline two\n"
                         "- short headline three\n- overflow headline four",
            "decisions": "1. fix roles-runner\n2. retry ingest\n3. third\n4. fourth",
            "risks": "- r1\n- r2\n- r3\n- r4\n- r5",
            "scrape_themes": "themes prose table",
            "stack_overlap": "stack overlap prose",
            "recommendations": "rec prose",
        },
    }


@pytest.fixture
def gathered(sched_log, shadow_dir, hub_db):
    return es.gather(now=W1, window_mode="yesterday", hub_db=hub_db,
                     ledger="/nonexistent.jsonl", bridge_log="/nonexistent.log",
                     digest="/nonexistent.json", dogfood_log="/nonexistent.md",
                     scheduler_log=sched_log, shadow_dir=shadow_dir,
                     scrapes_db="/nonexistent.db")


FORBIDDEN_EMAIL_SNIPPETS = (
    "Scrape themes", "Stack overlap", "Recommendations",
    "Zone A", "Zone B", "Zone C", "QA sampling", "Token stats",
    "0f1117",  # dark board theme CSS never appears in the email
)


def test_email_excludes_console_only_sections(gathered):
    text = es.email_text(gathered, _ds_structured())
    html = es.email_html(gathered, _ds_structured())
    for part in (text, html):
        for bad in FORBIDDEN_EMAIL_SNIPPETS:
            assert bad not in part, bad


def test_email_sections_in_order(gathered):
    html = es.email_html(gathered, _ds_structured())
    order = ["Headlines", "Numbers", "Agent actions", "Decisions needed",
             "Risks/watch", "Intel pulse", "Full live board:"]
    pos = [html.index(s) for s in order]
    assert pos == sorted(pos)


def test_email_text_sections_in_order_and_caps(gathered):
    text = es.email_text(gathered, _ds_structured())
    for heading in ("HEADLINES", "NUMBERS", "AGENT ACTIONS", "DECISIONS NEEDED",
                    "RISKS/WATCH", "INTEL PULSE"):
        assert heading in text
    # caps enforced in the text part too
    assert "overflow headline four" not in text
    assert "4. fourth" not in text
    assert "+1 more on the console" in text
    assert "- r5" not in text
    assert text.rstrip().endswith("dashboard") or "Full live board:" in text


def test_email_sizes_and_console_link(gathered):
    text = es.email_text(gathered, _ds_structured())
    html = es.email_html(gathered, _ds_structured())
    assert len(text.encode("utf-8")) <= es.EMAIL_TEXT_MAX_BYTES
    assert len(html.encode("utf-8")) <= es.EMAIL_HTML_MAX_BYTES
    assert "http://127.0.0.1:8777/dashboard" in text
    assert "http://127.0.0.1:8777/dashboard" in html


def test_email_zero_states_written(gathered):
    ds = {"mode": "structured", "numbers_strip": "0 runs · 0 failed",
          "blocks": {"headlines": "", "decisions": "", "risks": ""}}
    text = es.email_text(gathered, ds)
    assert "None — nothing needs you today." in text
    assert "No risks flagged" in text


def test_email_fallback_when_ds_fails(gathered):
    # DS call failed: strip + pulse + footer still render (fail-closed).
    ds = {"mode": "fallback", "numbers_strip": gathered["numbers_strip"],
          "detail": "model_call.py exit 8"}
    html = es.email_html(gathered, ds)
    text = es.email_text(gathered, ds)
    assert gathered["numbers_strip"] in html
    assert "Listening:" in text
    assert "Full live board:" in html


def test_email_html_email_safe_format(gathered):
    html = es.email_html(gathered, _ds_structured())
    low = html.lower()
    # 1. no <style> block, no external assets, no classes for load-bearing style
    assert "<style" not in low
    assert "<img" not in low
    assert 'src=' not in low
    assert "stylesheet" not in low
    assert "<script" not in low
    # 2. light theme + inline styles + 640px single column
    assert "#ffffff" in low
    assert 'style="' in html
    assert "max-width:640px" in html
    # 3. escaped model output (no raw injection through the DS blocks)
    assert "<b>short headline one" not in html


def test_email_escapes_model_output():
    ds = {"mode": "structured", "numbers_strip": "s",
          "blocks": {"headlines": '- <script>alert(1)</script> headline',
                     "decisions": "", "risks": ""}}
    data = {"window": {"label": "yesterday"}, "agent_actions": {"available": False},
            "scrape_intel": {"available": False, "detail": "no store"},
            "numbers_strip": "s"}
    html = es.email_html(data, ds)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_intel_pulse_from_theme_stats():
    intel = {"available": True, "themes": [
        {"topic": "hardware", "count": 10, "prior_count": 6, "trend": "up",
         "ratio": 1.67},
        {"topic": "pricing", "count": 3, "prior_count": 9, "trend": "down",
         "ratio": 0.33},
    ]}
    pulse = es.intel_pulse(intel)
    assert pulse.startswith("Listening: 13 items (down 15")
    assert "biggest riser: hardware ×1.67" in pulse


def test_intel_pulse_fail_closed():
    pulse = es.intel_pulse({"available": False, "detail": "scrapes.db not found"})
    assert "unavailable" in pulse and "source gap" in pulse


# ---------------------------------------------------------------- console split

def test_console_keeps_deep_panels_and_adds_agent_actions(gathered):
    html = es.summary_html(gathered, _ds_structured())
    assert "Agent actions" in html
    # console keeps the deep DS prose the email drops
    assert "Scrape themes" in html
    assert "Stack overlap" in html
    assert "Recommendations" in html


def test_render_daily_email_parts_written(tmp_path, sched_log, shadow_dir,
                                          hub_db, monkeypatch):
    """End-to-end render: email parts written, board unchanged as attachment
    source; the DS writer is stubbed (no model, no email sent)."""
    import render_daily as rd
    board_html = tmp_path / "daily-board.html"
    text_out = tmp_path / "email.txt"
    html_out = tmp_path / "email.html"
    argv = [
        "--hub-db", hub_db,
        "--hub-token-file", str(tmp_path / "no-token"),
        "--ledger", str(tmp_path / "no.jsonl"),
        "--out", str(board_html),
        "--exec-hub-db", hub_db,
        "--exec-ledger", str(tmp_path / "no.jsonl"),
        "--exec-bridge-log", str(tmp_path / "no.log"),
        "--exec-digest", str(tmp_path / "no.json"),
        "--exec-dogfood-log", str(tmp_path / "no.md"),
        "--exec-scheduler-log", sched_log,
        "--exec-shadow-dir", shadow_dir,
        "--scrapes-db", str(tmp_path / "no-scrapes.db"),
        "--snapshot", str(tmp_path / "snapshots.json"),
        "--sessions-mirror", str(tmp_path / "sessions.json"),
        "--tail-state", str(tmp_path / "tail.json"),
        "--email-text-out", str(text_out),
        "--email-html-out", str(html_out),
        "--console-url", "http://cons:8777/dashboard",
    ]
    import exec_summary as es_mod
    monkeypatch.setattr(es_mod, "write_summary_section",
                        lambda data: ("", _ds_structured()))
    monkeypatch.setattr(rd.exec_summary, "write_summary_section",
                        lambda data: ("", _ds_structured()))
    rc = rd.main(argv)
    assert rc == 0
    assert board_html.exists()
    text = text_out.read_text(encoding="utf-8")
    html = html_out.read_text(encoding="utf-8")
    assert "AGENT ACTIONS" in text
    assert "cons:8777" in text and "cons:8777" in html
    assert "overflow headline four" not in text  # caps applied
    for bad in FORBIDDEN_EMAIL_SNIPPETS:
        assert bad not in html


def test_email_script_is_markdown_text_only():
    """Ray 2026-11-12: the daily email is text/markdown, not HTML. The
    script sends ONE plain-text part built by email_report.build_email_text
    via email_delivery.send_report — no multipart/alternative, no HTML.
    Assertions are on the script's content only; it never runs in tests."""
    script = os.path.join(PKG, "email-daily-report.sh")
    with open(script, encoding="utf-8") as f:
        body = f.read()
    # (a) does NOT build or send HTML
    assert "build_email_html" not in body
    assert "add_alternative" not in body       # no multipart/alternative
    assert "MIMEMultipart" not in body
    assert "--email-html-out" not in body
    # (b) DOES send plain text via email_delivery.send_report with the
    # markdown body from email_report.build_email_text
    assert "build_email_text" in body
    assert "email_delivery.send_report" in body
    assert "set -u" in body
