"""Hardening gates G1–G6 (+G9) for the listening pipeline (task t_79b0ea96).

Acceptance criteria, live code path:
  (a) ingest a fixture batch twice -> identical artifact/note set (G1+G3)
  (b) edited changelog note -> supersedes edge, old version not a
      sibling in the cluster artifact (G2)
  (c) one malformed file -> {stored: n, skipped: 0, errors: [that one]}
      (G6)
  (d) notes carry published: stamps (G4)
Plus G1 normalized hashing and the G9 sensitivity-parse fix.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from secondbrain import Curator, L0Vault, Workspace  # noqa: E402
from secondbrain.curator import parse_note  # noqa: E402


def _env(tmp_path):
    (tmp_path / "notes").mkdir(parents=True, exist_ok=True)
    vault = L0Vault(tmp_path / "l0")
    ws = Workspace(tmp_path / "l1")
    return vault, ws, tmp_path / "notes"


def _node_counts(ws):
    from collections import Counter
    return Counter(n.type for n in ws.nodes.values())


# ---------------------------------------------------------------- G1

def test_g1_normalized_hashing_dedupes_re_renders(tmp_path):
    """A note re-rendered with different whitespace is the SAME original."""
    vault, _, _ = _env(tmp_path)
    a = vault.ingest("hello\nworld\n", source="a")
    b = vault.ingest("hello   world", source="b")      # re-render
    c = vault.ingest("hello\n\n  world\t\n", source="c")
    assert a == b == c
    assert len(vault.all_hashes()) == 1
    # genuinely different content still gets its own object
    assert vault.ingest("hello brave world", source="d") != a


# ---------------------------------------------------------------- G3

def test_g3_cluster_artifacts_idempotent(tmp_path):
    """Running curation twice must not multiply artifacts (hard blocker)."""
    vault, ws, notes = _env(tmp_path)
    (notes / "n1.md").write_text(
        "@entity:alpha\n\n# One\n\nbody one\n\nsensitivity: personal\n")
    (notes / "n2.md").write_text(
        "@entity:alpha\n@entity:beta\n\n# Two\n\nbody two\n\nsensitivity: personal\n")
    c = Curator(vault, ws)
    c.ingest_folder(notes)
    first = c.build_cluster_artifacts()
    ids1 = sorted(a.id for a in first)
    counts1 = _node_counts(ws)
    edges1 = sorted((e.src, e.dst, e.kind) for e in ws.edges)

    second = c.build_cluster_artifacts()
    assert sorted(a.id for a in second) == ids1
    assert _node_counts(ws) == counts1
    assert sorted((e.src, e.dst, e.kind) for e in ws.edges) == edges1


# ---------------------------------------------------------------- G2

def test_g2_supersedes_edge_and_old_version_not_a_sibling(tmp_path):
    """An edited changelog note supersedes the old one; the stale version
    no longer appears in the cluster artifact."""
    vault, ws, notes = _env(tmp_path)
    c = Curator(vault, ws)
    (notes / "v1.md").write_text(
        "@entity:proj\n\n# Changelog v1\n\nreleased 1.0\n\nsensitivity: personal\n")
    c.ingest_folder(notes)
    old_hash = next(n.refs[0] for n in ws.nodes.values() if n.type == "original")

    (notes / "v2.md").write_text(
        f"@entity:proj\n\n# Changelog v2\n\nreleased 1.1\n\n"
        f"- supersedes: {old_hash}\nsensitivity: personal\n")
    c.ingest_folder(notes)

    sup = [e for e in ws.edges if e.kind == "supersedes"]
    assert len(sup) == 1
    new_node = ws.nodes[sup[0].src]
    assert old_hash in ws.nodes[sup[0].dst].refs
    assert "1.1" in new_node.text

    artifacts = c.build_cluster_artifacts()
    art = next(a for a in artifacts if a.id == "artifact:entity:proj")
    assert "v2.md" in art.text and "v1.md" not in art.text  # old not a sibling
    # two originals exist in the graph, but only the live one is listed
    assert sum(1 for n in ws.nodes.values() if n.type == "original") == 2


# ---------------------------------------------------------------- G6

def test_g6_malformed_file_isolated_with_report(tmp_path):
    """{stored: n, skipped: 0, errors: [that one]} — the batch survives."""
    vault, ws, notes = _env(tmp_path)
    (notes / "good.md").write_text(
        "@entity:ok\n\n# Fine\n\nbody\n\nsensitivity: personal\n")
    (notes / "corrupt.md").write_bytes(b"\xff\xfe not utf-8 \x00\x01")
    report = Curator(vault, ws).ingest_folder(notes)
    assert set(report.keys()) == {"stored", "skipped", "errors"}
    assert len(report["stored"]) == 1
    assert report["skipped"] == []
    assert [e["file"] for e in report["errors"]] == ["corrupt.md"]


# ---------------------------------------------------------------- G4

def test_g4_notes_carry_published_stamps(tmp_path):
    """build_note stamps published: from the source timestamp, falling
    back to fetched_at."""
    import bridge
    note = bridge.build_note({"title": "t", "guid": "g", "source_id": "s",
                              "topic": "top", "body": "b",
                              "published": "2026-11-12T10:00:00Z",
                              "sensitivity": "personal"})
    assert "- published: 2026-11-12T10:00:00Z" in note
    fallback = bridge.build_note({"title": "t", "guid": "g",
                                  "source_id": "s", "topic": "top",
                                  "body": "b",
                                  "fetched_at": "2026-11-12T11:00:00Z",
                                  "sensitivity": "personal"})
    assert "- published: 2026-11-12T11:00:00Z" in fallback


# ---------------------------------------------------------------- G9

def test_g9_sensitivity_parsed_from_last_line_only():
    """Mid-body 'sensitivity:' phrases cannot re-label a note."""
    body = ("We discussed the sensitivity: public of this debate at length.\n"
            "sensitivity: personal\n")
    assert parse_note(body)["sensitivity"] == "personal"
    trick = ("sensitivity: personal\n\n"
             "The sensitivity: public hearing was adjourned.\n")
    assert parse_note(trick)["sensitivity"] == "personal"  # not last line
    assert parse_note("x\nsensitivity: public\n")["sensitivity"] == "public"
    assert parse_note("x\nsensitivity: secret\n")["sensitivity"] == "personal"
    assert parse_note("")["sensitivity"] == "personal"


# ------------------------------------------------- (a) end-to-end idempotence

def test_acceptance_a_batch_ingested_twice_identical(tmp_path):
    """Fixture batch ingested twice -> identical note/artifact set."""
    vault, ws, notes = _env(tmp_path)
    (notes / "a.md").write_text(
        "@entity:rel\n\n# Release 1.0\n\nnotes here\n\nsensitivity: personal\n")
    (notes / "b.md").write_text(
        "@entity:rel\n\n# Release 1.0\n\nnotes  here\n"  # whitespace variant
        "\nsensitivity: personal\n")
    c = Curator(vault, ws)
    r1 = c.ingest_folder(notes)
    a1 = c.build_cluster_artifacts()
    snap1 = (sorted(n.id for n in ws.nodes.values()),
             sorted((e.src, e.dst, e.kind) for e in ws.edges),
             len(vault.all_hashes()))
    r2 = c.ingest_folder(notes)
    a2 = c.build_cluster_artifacts()
    snap2 = (sorted(n.id for n in ws.nodes.values()),
             sorted((e.src, e.dst, e.kind) for e in ws.edges),
             len(vault.all_hashes()))
    assert snap1 == snap2
    assert [x.id for x in a1] == [x.id for x in a2]
    assert len(r1["stored"]) == len(r2["stored"]) == 2
    assert r2["errors"] == []


# ============================== bridge-level acceptance (live path) ====

import sqlite3
import time as _time

import bridge  # noqa: E402

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY, source_id TEXT, topic TEXT, guid TEXT,
    title TEXT, url TEXT, body TEXT, published TEXT,
    fetched_at TEXT, run_id TEXT, sensitivity TEXT);
"""
_NOW = _time.time()


def _iso(epoch):
    return _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(epoch))


def _make_db(path, items):
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
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


def _bridge_item(i, title, body, guid, sensitivity="personal"):
    return {"source_id": f"src-{i}", "topic": f"topic-{i}", "guid": guid,
            "title": title, "url": f"https://example.com/{i}", "body": body,
            "published": _iso(_NOW - 3600), "fetched_at": _iso(_NOW - 3600),
            "run_id": "r1", "sensitivity": sensitivity}


_MATCH = ("benchmark covering agent tooling and gpu inference cost "
          "for the new llm model")


def _bridge_env(tmp_path, items):
    db = tmp_path / "scrapes.db"
    gw = tmp_path / "gateway"
    gw.mkdir()
    _make_db(db, items)
    return db, gw


def test_acceptance_a_bridge_batch_twice_identical(tmp_path):
    """(a) via the live bridge: run twice -> nothing new the second time."""
    from secondbrain import L0Vault
    db, gw = _bridge_env(tmp_path, [
        _bridge_item(1, "New benchmark for llm agents", _MATCH, "g-1"),
        _bridge_item(2, "Second llm model benchmark", _MATCH, "g-2")])
    first = bridge.run(str(db), str(gw))
    assert first["ingested"] == 2 and first["errors"] == 0
    vault = L0Vault(gw / "l0")
    n_objects_1 = len(vault.all_hashes())
    l1_1 = (gw / "l1" / "l1.json").read_text()
    second = bridge.run(str(db), str(gw))
    assert second["ingested"] == 0
    assert second["skipped"] >= 2
    assert len(vault.all_hashes()) == n_objects_1
    assert (gw / "l1" / "l1.json").read_text() == l1_1


def test_acceptance_b_bridge_edited_changelog_supersedes(tmp_path):
    """(b) via the live bridge: edited note -> supersedes edge, old
    version not a sibling in the rebuilt cluster artifact."""
    from secondbrain import L0Vault, Workspace
    db, gw = _bridge_env(tmp_path, [
        _bridge_item(1, "changelog release 1.0", _MATCH, "cl-1")])
    bridge.run(str(db), str(gw))
    vault = L0Vault(gw / "l0")
    old_hash = next(h for h in vault.all_hashes()
                    if "release 1.0" in vault.get(h).text)

    # the source edits its changelog (same guid, new content)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE items SET body=?, title=? WHERE guid='cl-1'",
                 (_MATCH + " updated changelog", "changelog release 1.1"))
    conn.commit()
    conn.close()
    bridge.run(str(db), str(gw))

    ws = Workspace(gw / "l1")
    assert ws.load()
    sup = [e for e in ws.edges if e.kind == "supersedes"]
    assert len(sup) == 1, "supersedes edge must survive the second run"
    assert old_hash in ws.nodes[sup[0].dst].refs
    assert "release 1.1" in ws.nodes[sup[0].src].text
    arts = [n for n in ws.nodes.values() if n.type == "artifact"]
    assert arts and all("release 1.0" not in a.text for a in arts)


def test_acceptance_c_bridge_malformed_file_isolated(tmp_path):
    """(c) via the live bridge: one malformed item -> stored: n,
    skipped: 0 for that run, errors: [that one]; the rest stored."""
    db, gw = _bridge_env(tmp_path, [
        _bridge_item(1, "good one", _MATCH, "g-1"),
        _bridge_item(2, "good two", _MATCH, "g-2")])
    first = bridge.run(str(db), str(gw))
    assert first["ingested"] == 2
    # second window: one good new item + one whose note build fails
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO items (source_id, topic, guid, title, url, body,"
        " published, fetched_at, run_id, sensitivity)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("src-9", "topic-9", "g-bad", "bad item", "u",
         _MATCH, _iso(_NOW), _iso(_NOW), "r2", "personal"))
    conn.execute(
        "INSERT INTO items (source_id, topic, guid, title, url, body,"
        " published, fetched_at, run_id, sensitivity)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("src-10", "topic-10", "g-3", "third benchmark llm", "u3",
         _MATCH, _iso(_NOW), _iso(_NOW), "r2", "personal"))
    conn.commit()
    conn.close()

    real_build_note = bridge.build_note

    def broken_build_note(item, supersedes=None):
        if item.get("guid") == "g-bad":
            raise ValueError("malformed scrape result")
        return real_build_note(item, supersedes)

    bridge.build_note = broken_build_note
    try:
        s = bridge.run(str(db), str(gw), since_hours=1)
    finally:
        bridge.build_note = real_build_note
    assert s["ingested"] == 1          # the good new item stored
    assert s["skipped"] == 0
    assert s["errors"] == 1 and len(s["error_details"]) == 1
    assert s["error_details"][0]["guid"] == "g-bad"


def test_acceptance_d_bridge_notes_carry_published(tmp_path):
    """(d) via the live bridge: every staged note has a published: stamp,
    falling back to fetched_at when the source has none."""
    db, gw = _bridge_env(tmp_path, [
        {"source_id": "s", "topic": "t", "guid": "g-1",
         "title": "llm benchmark", "url": "u", "body": _MATCH,
         "published": None, "fetched_at": _iso(_NOW - 3600), "run_id": "r"}])
    bridge.run(str(db), str(gw))
    for note in (gw / "bridge" / "staging").glob("*.md"):
        assert "- published: " in note.read_text()
