"""Lane-4 governance labels: computed sensitivity + entities in the digest.

Covers (acceptance a): public item, personal item, unknown sensitivity
(fail-closed skip), empty entity list in a digest item, and empty
selection. test_real_digest_labels_survive_ingest runs a real bridge run
and asserts the produced digest JSON itself (acceptance b).
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

BRIDGE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BRIDGE_DIR))
import bridge  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY, source_id TEXT, topic TEXT, guid TEXT,
    title TEXT, url TEXT, body TEXT, published TEXT,
    fetched_at TEXT, run_id TEXT, sensitivity TEXT);
"""

NOW = time.time()


def iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def item(i, title, body, sensitivity, guid=None):
    return {"source_id": f"src-{i}", "topic": f"topic-{i}",
            "guid": guid or f"guid-{i}", "title": title, "url":
            f"https://example.com/{i}", "body": body,
            "published": iso(NOW - 3600),
            "fetched_at": iso(NOW - 3600),
            "run_id": "r1", "sensitivity": sensitivity}


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


BASE_ITEMS = [
    item(1, "Public benchmark", "The benchmark covers gpu inference cost.",
         "public"),
    item(2, "Personal pricing note", "We cut cost by 20%.", "personal"),
    item(3, "Invalid-label leak", "A new llm model shipped.", "topsecret",
         guid="guid-invalid"),
    item(4, "Unclassified leak", "A new llm model shipped.", None,
         guid="guid-missing"),
]


@pytest.fixture()
def env_pair(tmp_path):
    db = tmp_path / "scrapes.db"
    gw = tmp_path / "gateway"
    gw.mkdir()
    make_db(db, BASE_ITEMS)
    return db, gw


def test_public_and_personal_labels_stamped(env_pair):
    db, gw = env_pair
    bridge.run(str(db), str(gw))
    notes = sorted((gw / "bridge" / "staging").glob("*.md"))
    texts = [n.read_text() for n in notes]
    assert any("sensitivity: public" in t for t in texts)
    assert any("sensitivity: personal" in t for t in texts)
    # sensitivity marker is the LAST non-empty line (G9)
    for t in texts:
        lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
        assert lines[-1].startswith("sensitivity: ")


def test_unknown_sensitivity_fails_closed(env_pair):
    db, gw = env_pair
    s = bridge.run(str(db), str(gw))
    # the invalid-label item never entered the note pipeline: no note and
    # no state entry for its guid (it is skipped, not relabeled)
    texts = [n.read_text() for n in (gw / "bridge" / "staging").glob("*.md")]
    assert not any("Invalid-label leak" in t for t in texts)
    state = json.loads((gw / "bridge" / "state.json").read_text())
    assert "guid-invalid" not in state
    # fail-closed marking: counted as an error with an explicit reason
    assert s["errors"] >= 1
    flat = json.dumps(s["error_details"])
    assert "ClassificationError" in flat


def test_missing_sensitivity_defaults_personal(env_pair):
    db, gw = env_pair
    s = bridge.run(str(db), str(gw))
    # missing label -> conservative default personal (vault design rule),
    # ingested not skipped
    texts = [n.read_text() for n in (gw / "bridge" / "staging").glob("*.md")]
    assert any("Unclassified leak" in t for t in texts)
    state = json.loads((gw / "bridge" / "state.json").read_text())
    assert "guid-missing" in state
    flat = json.dumps(s["error_details"])
    assert "guid-missing" not in flat
    # labeled items unchanged; invalid one still skipped
    assert any("sensitivity: personal" in t for t in texts)
    assert any("sensitivity: public" in t for t in texts)
    assert s["ingested"] == 3
    assert s["errors"] == 1


def test_digest_items_carry_sensitivity_and_entities(env_pair):
    db, gw = env_pair
    bridge.run(str(db), str(gw))
    digest = json.loads((gw / "bridge" / "digest-latest.json").read_text())
    entries = {e["title"]: e for e in digest["ingested"]}
    pub = entries["Public benchmark"]
    per = entries["Personal pricing note"]
    assert pub["sensitivity"] == "public"
    assert per["sensitivity"] == "personal"
    for e in (pub, per):
        assert isinstance(e["entities"], list) and e["entities"]
        assert e["entities"] == ["src-1", "topic-1"] or e is per, e
        assert all(not x.startswith("@") for x in e["entities"])


def test_digest_item_with_empty_entity_list(env_pair):
    """A candidate note without entity markers serializes entities: []."""
    db, gw = env_pair
    bridge.run(str(db), str(gw))
    from secondbrain import GrantBook, Gatekeeper, Workspace
    book = GrantBook(gw / "ledger.db")
    gatekeeper = Gatekeeper(Workspace(gw / "l1"), book)
    candidates = [("guid-x", "Bare title\n\nsensitivity: public\n",
                   {"title": "Bare title", "url": None,
                    "source_id": None, "topic": None})]
    digest = bridge.build_digest_summary(book, gatekeeper, candidates, 1,
                                         "2026-01-01T00:00:00Z")
    entry = digest["ingested"][0]
    assert entry["sensitivity"] == "public"
    assert entry["entities"] == []


def test_empty_selection_keeps_empty_digest(tmp_path):
    db = tmp_path / "scrapes.db"
    gw = tmp_path / "gateway"
    gw.mkdir()
    make_db(db, [item(9, "weather report", "sunny today", "public")])
    s = bridge.run(str(db), str(gw))
    assert s["selected"] == 0 and s["ingested"] == 0
    digest = json.loads((gw / "bridge" / "digest-latest.json").read_text())
    assert digest["ingested"] == []
    assert digest["ingested_count"] == 0


def test_real_digest_labels_survive_ingest(env_pair):
    """Acceptance (b): the digest JSON produced by a real run carries the
    labels parsed back from the actual ingested notes."""
    db, gw = env_pair
    summary = bridge.run(str(db), str(gw))
    assert summary["ingested"] == 3
    digest_path = Path(summary["digest_path"])
    digest = json.loads(digest_path.read_text())
    labels = {e["title"]: e["sensitivity"] for e in digest["ingested"]}
    assert labels == {"Public benchmark": "public",
                      "Personal pricing note": "personal",
                      "Unclassified leak": "personal"}
