"""Hermetic pytest for the ingest bridge (A1).

Fixtures only: a temp scrapes.db and a temp gateway root. The real
secondbrain package is used (sys.path insertion, same as bridge users).
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import sys
from pathlib import Path

import pytest

BRIDGE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BRIDGE_DIR))
import bridge  # noqa: E402

GATEWAY_SRC = Path(os.environ.get(
    "AERIAL_GATEWAY_SRC", str(Path(__file__).resolve().parent)))
sys.path.insert(0, str(GATEWAY_SRC))

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY, source_id TEXT, topic TEXT, guid TEXT,
    title TEXT, url TEXT, body TEXT, published TEXT,
    fetched_at TEXT, run_id TEXT, sensitivity TEXT);
"""

NOW = time.time()


def iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def make_db(path: Path, items: list[dict]) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    for it in items:
        conn.execute(
            "INSERT INTO items (source_id, topic, guid, title, url, body,"
            " published, fetched_at, run_id, sensitivity)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (it.get("source_id"), it.get("topic"), it.get("guid"),
             it.get("title"), it.get("url"), it.get("body"),
             it.get("published"), it.get("fetched_at"), it.get("run_id"),
             it.get("sensitivity")))
    conn.commit()
    conn.close()


def item(i, title, body, hours_ago, guid=None, sensitivity="personal"):
    return {"source_id": f"src-{i}", "topic": f"topic-{i}",
            "guid": guid or f"guid-{i}", "title": title, "url":
            f"https://example.com/{i}", "body": body,
            "published": iso(NOW - hours_ago * 3600),
            "fetched_at": iso(NOW - hours_ago * 3600),
            "run_id": "r1", "sensitivity": sensitivity}


DOZEN = [
    # 3 groups hit — top scorer
    item(1, "New benchmark for llm agents", "The benchmark covers agent "
         "tooling and gpu inference cost.", 1),
    # 1 group hit
    item(2, "pricing change", "We cut cost by 20%.", 2),
    # 0 groups — never selected
    item(3, "weather report", "Sunny today.", 3),
    # 1 group hit
    item(4, "model release notes", "A new llm model shipped.", 4),
    # old item outside the 24h window even though it matches
    item(5, "old benchmark", "benchmark results from last year", 72),
    # 2 groups
    item(6, "gpu inference benchmark", "benchmark run on gpu.", 5),
    item(7, "agent tooling roundup", "agent tooling links.", 6),
    item(8, "cost analysis", "cost of serving.", 7),
    item(9, "plain news", "nothing matched here at all", 8),
    item(10, "llm tooling", "llm tooling update", 9),
    item(11, "pricing update", "pricing and cost notes", 10),
    # corrupt body (not valid text for the note pipeline)
    item(12, "corrupt", "benchmark \x00 garbage", 11),
]


@pytest.fixture()
def env(tmp_path):
    db = tmp_path / "scrapes.db"
    gw = tmp_path / "gateway"
    gw.mkdir()
    make_db(db, DOZEN)
    return db, gw


def run_bridge(db, gw, **kw):
    return bridge.run(str(db), str(gw), **kw)


def test_selection_window_and_topn(env):
    db, gw = env
    s = run_bridge(db, gw, since_hours=24, top=3)
    assert s["fetched"] == 11          # item 5 (72h old) excluded
    assert s["selected"] == 3          # top-3 of the matched items
    assert s["errors"] == 0


def test_notes_have_entities_and_personal(env):
    db, gw = env
    run_bridge(db, gw)
    staging = gw / "bridge" / "staging"
    notes = sorted(staging.glob("*.md"))
    assert notes
    text = notes[0].read_text()
    assert "@entity:" in text
    assert "sensitivity: personal" in text
    assert "sensitivity: public" not in text


def test_idempotent_rerun(env):
    db, gw = env
    first = run_bridge(db, gw)
    second = run_bridge(db, gw)
    assert first["ingested"] > 0
    assert second["ingested"] == 0     # nothing new
    assert second["skipped"] >= first["ingested"]  # guids skipped


def test_corrupt_body_does_not_crash(env):
    db, gw = env
    s = run_bridge(db, gw)
    # the corrupt item must not abort the batch: others still ingested
    assert s["ingested"] > 0
    # (G6: any per-item failure is counted, not fatal)
    assert isinstance(s["errors"], int)


def test_digest_includes_new_items(env):
    db, gw = env
    run_bridge(db, gw)
    digest = json.loads((gw / "bridge" / "digest-latest.json").read_text())
    titles = [e["title"] for e in digest["ingested"]]
    assert "New benchmark for llm agents" in titles
    assert digest["ingested_count"] > 0


def test_dry_run_touches_nothing(env):
    db, gw = env
    s = run_bridge(db, gw, dry_run=True)
    assert s["selected"] > 0
    assert not (gw / "bridge").exists()
