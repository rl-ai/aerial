"""Hermetic tests for the scrape-intelligence layer (task t_a73af432).

Fixture scrapes.db in tmp_path, fixture keywords yaml in tmp_path; the DS
call is stubbed — tests never hit a model or a network.
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

import scrape_intel as si  # noqa: E402
import exec_summary as es  # noqa: E402

# Fixed "now": 2026-11-12 12:00:00 UTC.
NOW = 1_794_484_800.0
CUR0, CUR1 = NOW - 86400.0, NOW          # last 24h window
PRIOR0, PRIOR1 = NOW - 172800.0, NOW - 86400.0  # baseline window


def iso(epoch: float) -> str:
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY, source_id TEXT, topic TEXT, guid TEXT,
    title TEXT, url TEXT, body TEXT, published TEXT,
    fetched_at TEXT, run_id TEXT);
"""


def make_db(path, items):
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    for it in items:
        conn.execute(
            "INSERT INTO items (source_id, topic, guid, title, url, body,"
            " published, fetched_at, run_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (it.get("source_id"), it.get("topic"), it.get("guid"),
             it.get("title"), it.get("url"), it.get("body"),
             it.get("published"), it.get("fetched_at"), it.get("run_id")))
    conn.commit()
    conn.close()
    return str(path)


KW = {"alpha-agent": ["agent", "tooling"],
      "beta-infra": ["gpu", "inference"],
      "gamma-none": ["zebra"]}


@pytest.fixture
def scrapes_db(tmp_path):
    items = [
        # current window, topic llm: 3 items; term "benchmark" novel here
        dict(source_id="s1", topic="llm", guid="g1",
             title="New benchmark for agent tooling",
             body="agents and agent tooling benchmarked",
             published=iso(CUR0 + 100), fetched_at=iso(CUR0 + 100), run_id="r1"),
        dict(source_id="s1", topic="llm", guid="g2",
             title="Benchmark results followup",
             body="more benchmark numbers",
             published=iso(CUR0 + 200), fetched_at=iso(CUR0 + 200), run_id="r1"),
        dict(source_id="s2", topic="infra", guid="g3",
             title="GPU inference costs",
             body="gpu inference pricing dropped",
             published=iso(CUR0 + 300), fetched_at=iso(CUR0 + 300), run_id="r2"),
        # current window, topic llm, no keywords hit
        dict(source_id="s3", topic="llm", guid="g4",
             title="Unrelated chatter", body="zebra",
             published=iso(CUR0 + 400), fetched_at=iso(CUR0 + 400), run_id="r2"),
        # prior window only: topic infra had 2 items -> trend down for infra
        dict(source_id="s2", topic="infra", guid="g5",
             title="Old gpu item", body="gpu inference",
             published=iso(PRIOR0 + 100), fetched_at=iso(PRIOR0 + 100), run_id="r0"),
        dict(source_id="s2", topic="infra", guid="g6",
             title="Older gpu item", body="gpu again",
             published=iso(PRIOR0 + 200), fetched_at=iso(PRIOR0 + 200), run_id="r0"),
        # out of both windows -> excluded
        dict(source_id="s1", topic="llm", guid="g7",
             title="Ancient", body="old",
             published=iso(PRIOR0 - 100), fetched_at=iso(PRIOR0 - 100), run_id="r-1"),
        dict(source_id="s1", topic="llm", guid="g8",
             title="Future", body="later",
             published=iso(NOW + 100), fetched_at=iso(NOW + 100), run_id="r+1"),
        # in window but unparsable ts -> excluded, counted
        dict(source_id="s1", topic="llm", guid="g9",
             title="No ts", body="x", published="garbage", fetched_at="", run_id="r2"),
    ]
    return make_db(tmp_path / "scrapes.db", items)


# ------------------------------------------------------------------ fail-closed

def test_fail_closed_missing_db(tmp_path):
    r = si.gather(now=NOW, scrapes_db=str(tmp_path / "nope.db"), project_keywords=KW)
    assert r["available"] is False
    assert "not found" in r["detail"]


def test_fail_closed_bad_table(tmp_path):
    p = str(tmp_path / "empty.db")
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE other (x)")
    conn.commit()
    conn.close()
    r = si.gather(now=NOW, scrapes_db=p, project_keywords=KW)
    assert r["available"] is False
    assert "read failed" in r["detail"]


# --------------------------------------------------------------------- window

def test_window_filtering_and_counts(scrapes_db):
    r = si.gather(now=NOW, scrapes_db=scrapes_db, project_keywords=KW)
    assert r["available"] is True
    assert r["items"] == 4            # g1..g4
    assert r["prior_items"] == 2      # g5, g6
    assert r["no_ts_items"] == 1      # g9


def test_theme_counts_and_trend(scrapes_db):
    r = si.gather(now=NOW, scrapes_db=scrapes_db, project_keywords=KW)
    themes = {t["topic"]: t for t in r["themes"]}
    assert themes["llm"]["count"] == 3
    assert themes["infra"]["count"] == 1
    assert themes["llm"]["trend"] == "up" and themes["llm"]["ratio"] is None
    assert themes["infra"]["trend"] == "down" and themes["infra"]["ratio"] == 0.5
    # flat case: same count in both windows (seed a prior llm item)
    db2 = make_db(os.path.join(os.path.dirname(scrapes_db), "flat.db"), [
        dict(source_id="s", topic="flat", guid="a", title="T", body="b",
             published=iso(CUR0 + 10), fetched_at=iso(CUR0 + 10), run_id="r"),
        dict(source_id="s", topic="flat", guid="b", title="T2", body="b2",
             published=iso(PRIOR0 + 10), fetched_at=iso(PRIOR0 + 10), run_id="r"),
    ])
    r2 = si.gather(now=NOW, scrapes_db=db2, project_keywords=KW)
    t2 = {t["topic"]: t for t in r2["themes"]}["flat"]
    assert t2["trend"] == "flat" and t2["ratio"] == 1.0


def test_term_delta_vs_baseline(scrapes_db):
    # "benchmark" appears in cur llm (3x) and not in prior llm -> delta > 0.
    r = si.gather(now=NOW, scrapes_db=scrapes_db, project_keywords=KW)
    llm = {t["topic"]: t for t in r["themes"]}["llm"]
    assert "benchmark" in llm["top_terms"]
    # no baseline at all: raw-frequency fallback still yields terms
    assert llm["top_terms"]


def test_top_terms_cap(scrapes_db):
    r = si.gather(now=NOW, scrapes_db=scrapes_db, project_keywords=KW)
    for t in r["themes"]:
        assert len(t["top_terms"]) <= si.MAX_TERMS


# ----------------------------------------------------------------- overlap

def test_overlap_counts_and_titles(scrapes_db):
    r = si.gather(now=NOW, scrapes_db=scrapes_db, project_keywords=KW)
    ov = {o["project"]: o for o in r["stack_overlap"]}
    # alpha-agent: 2 items mention "agent"/"tooling" (g1 only? g1 title+body hit) —
    # g1 hits agent+tooling; g2/g3/g4 don't
    assert ov["alpha-agent"]["count"] == 1
    assert ov["alpha-agent"]["top_titles"] == ["New benchmark for agent tooling"]
    assert ov["beta-infra"]["count"] == 1   # g3 only (prior-window gpu items excluded)
    assert ov["gamma-none"]["count"] == 1   # g4 body contains "zebra"


def test_overlap_top_titles_max3(tmp_path):
    items = [dict(source_id="s", topic="t", guid=f"g{i}",
                  title=f"agent item {i}", body="",
                  published=iso(CUR0 + i), fetched_at=iso(CUR0 + i),
                  run_id="r") for i in range(5)]
    db = make_db(tmp_path / "many.db", items)
    r = si.gather(now=NOW, scrapes_db=db, project_keywords={"p": ["agent"]})
    ov = r["stack_overlap"][0]
    assert ov["count"] == 5
    assert len(ov["top_titles"]) == 3


def test_no_keywords_no_overlap(scrapes_db):
    r = si.gather(now=NOW, scrapes_db=scrapes_db, project_keywords=None)
    assert r["available"] and r["stack_overlap"] == []


# ------------------------------------------------------------- keywords yaml

def test_load_project_keywords_yaml(tmp_path):
    p = tmp_path / "references.yaml"
    p.write_text("projects:\n"
                 "  alpha-agent:\n"
                 "    title: Alpha Agent Tooling\n"
                 "  beta-infra:\n"
                 "    title: GPU Inference Infra\n")
    kw = si.load_project_keywords(str(p))
    assert "alpha-agent" in kw
    assert "alpha" in kw["alpha-agent"] and "agent" in kw["alpha-agent"]
    assert "gpu" in kw["beta-infra"]


def test_load_project_keywords_missing(tmp_path):
    assert si.load_project_keywords(str(tmp_path / "nope.yaml")) == {}
    assert si.load_project_keywords(None) == {}


# -------------------------------------------------------------- DS integration

def _ds_stub(text):
    def runner(ask_path, out_path):
        with open(out_path, "w") as f:
            f.write(text)
        return 0
    return runner


DS_OUT = """### 3 headlines
- one
- two
- three
### Numbers strip
- 4 runs
### Decisions needed
- none
### Risks/watch
- none flagged
### Scrape themes & trends
- LLM chatter up (x1.5), Infra down
### Stack overlap
- alpha-agent: 2 hits
### Recommendations
- Track the benchmark wave this week
"""

FULL_DATA = {
    "window": {"start": CUR0, "end": CUR1},
    "job_runs": {"available": False, "detail": "x"},
    "model_calls": {"available": False, "detail": "x"},
    "ingest": {"available": False, "detail": "x"},
    "dogfood": {"available": False, "detail": "x"},
    "shadow": {"available": False, "detail": "x"},
    "scrape_intel": si.gather(now=NOW, scrapes_db=os.path.join(
        os.path.dirname(__file__), "..", "..", "__nonexistent__"),
        project_keywords=KW),
    "numbers_strip": "n/a",
}


def test_build_ask_includes_scrape_sections_when_available(scrapes_db):
    data = dict(FULL_DATA)
    data["scrape_intel"] = si.gather(now=NOW, scrapes_db=scrapes_db, project_keywords=KW)
    ask = es.build_ask_text(data)
    assert "### Scrape themes & trends" in ask
    assert "### Stack overlap" in ask
    assert "### Recommendations" in ask


def test_build_ask_omits_scrape_sections_when_unavailable():
    ask = es.build_ask_text(FULL_DATA)  # scrape_intel unavailable (missing db)
    assert "Scrape themes" not in ask
    assert "Recommendations" not in ask


def test_parse_ds_output_new_sections():
    b = es.parse_ds_output(DS_OUT)
    assert "LLM chatter" in b["scrape_themes"]
    assert "alpha-agent" in b["stack_overlap"]
    assert "benchmark wave" in b["recommendations"]


def test_summary_html_renders_scrape_sections_and_table(scrapes_db):
    data = dict(FULL_DATA)
    data["scrape_intel"] = si.gather(now=NOW, scrapes_db=scrapes_db, project_keywords=KW)
    _, ds_result = es.write_summary_section(data, ds_runner=_ds_stub(DS_OUT))
    html = es.summary_html(data, ds_result)
    assert ds_result["mode"] == "structured"
    assert "Scrape themes" in html
    assert "Stack overlap" in html
    assert "Recommendations" in html
    assert "<table" in html and "llm" in html and "top terms" in html
    # numbers survive even if DS phrasing is vague
    assert "infra" in html and "1" in html


def test_summary_html_unavailable_scrape_intel_note():
    data = dict(FULL_DATA)
    _, ds_result = es.write_summary_section(data, ds_runner=_ds_stub(DS_OUT))
    html = es.summary_html(data, ds_result)
    assert "scrape intel unavailable" in html


# ------------------------------------------------------------------- gather()

def test_exec_summary_gather_includes_scrape_intel(scrapes_db, tmp_path):
    data = es.gather(now=NOW, hub_db=str(tmp_path / "hub.db"),
                     ledger=str(tmp_path / "ledger.jsonl"),
                     bridge_log=str(tmp_path / "bridge.log"),
                     digest=str(tmp_path / "digest.json"),
                     dogfood_log=str(tmp_path / "dogfood.log"),
                     scheduler_log=str(tmp_path / "sched.log"),
                     scrapes_db=scrapes_db, project_keywords=KW)
    assert data["scrape_intel"]["available"] is True
    assert data["scrape_intel"]["items"] == 4
