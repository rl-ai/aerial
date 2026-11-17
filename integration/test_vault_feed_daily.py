#!/usr/bin/env python3
"""Job-shape test for out/bin/vault-feed-daily.sh (gap C2).

Builds a fixture "real" dogfood vault (3 notes, via the real secondbrain
Curator — mirroring dogfood/setup_dogfood.py) and a fixture scrapes.db
with 30 matched items, then runs the real script with env overrides.
Verifies:

  1. exit 0; the feed copy (FEED_ROOT) gains bridged notes, CAPPED at
     FEED_TOP (20/day default — asserted with the default by passing 30
     items and FEED_TOP unset? no: explicit FEED_TOP=20 and count <= 20)
  2. Ray's real notes are NEVER touched: notes-src and the real
     notes-vault are byte-identical before/after (hash comparison)
  3. IDEMPOTENT: a second run ingests 0 new notes (bridge state.json in
     the persistent feed root skips them; l0 object count unchanged)
"""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

LANE = Path(__file__).resolve().parent.parent.parent
GW1 = Path(os.path.expanduser(
    "~/workspaces/build/second-brain-gateway/v1"))
SCRIPT = LANE / "out" / "bin" / "vault-feed-daily.sh"
sys.path.insert(0, str(GW1))

from secondbrain import GrantBook, L0Vault, Workspace  # noqa: E402
from secondbrain.curator import Curator  # noqa: E402

NOW = time.time()
N_ITEMS = 30
FEED_TOP = 20


def make_dogfood(root: Path) -> Path:
    """Mirror dogfood/setup_dogfood.py: a 3-note vault + grants."""
    vault = root / "dogfood" / "notes-vault"
    src = root / "dogfood" / "notes-src"
    (vault / "l0").mkdir(parents=True)
    (vault / "l1").mkdir(parents=True)
    src.mkdir(parents=True)
    notes = {
        "training-log.md": "@entity:training\nsensitivity: personal\n\n"
                           "Squat 5x5 at 100kg.\n",
        "planning-notes.md":
            "@entity:project-orchestra\nsensitivity: sensitive\n\n"
            "Q1 roadmap draft.\n",
        "private-journal.md": "@entity:journal\nsensitivity: intimate\n\n"
                              "Personal reflections.\n",
    }
    for name, text in notes.items():
        (src / name).write_text(text, encoding="utf-8")
    v = L0Vault(vault / "l0")
    ws = Workspace(vault / "l1")
    cur = Curator(v, ws)
    cur.ingest_folder(src)
    cur.build_cluster_artifacts()
    ws.publish("main")
    ws.save()
    book = GrantBook(vault / "grants.db")
    book.create(counterparty="Ray's planning agent", purpose="test",
                anchor_entities=["entity:project-orchestra"], alias="main",
                ttl_days=30, query_budget=200,
                disclosure={"generalize_location": "city",
                            "max_date_granularity": "month",
                            "forbid": [], "require_citation": False})
    return root / "dogfood"


def make_scrapes_db(path: Path, n: int) -> None:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE items (id INTEGER PRIMARY KEY, guid TEXT, title TEXT,"
        " body TEXT, url TEXT, source_id TEXT, topic TEXT,"
        " published TEXT, fetched_at REAL, sensitivity TEXT)")
    for i in range(n):
        con.execute(
            "INSERT INTO items (guid, title, body, url, source_id, topic,"
            " published, fetched_at, sensitivity) VALUES (?,?,?,?,?,?,?,?,?)",
            (f"feed-{i}", f"GPU inference cost {i}",
             f"agent tooling benchmark for llm model {i}",
             f"https://example.com/{i}", "hnews", "models",
             "2026-11-15T00:00:00Z", NOW - i * 60, "public"))
    con.commit()
    con.close()


def tree_digest(path: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(path.rglob("*")):
        h.update(str(p.relative_to(path)).encode())
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="vault-feed-test-"))
    try:
        dogfood = make_dogfood(tmp)
        real_vault = dogfood / "notes-vault"
        real_src = dogfood / "notes-src"
        vault_hash = tree_digest(real_vault)
        src_hash = tree_digest(real_src)

        db = tmp / "scrapes.db"
        make_scrapes_db(db, N_ITEMS)
        logs = tmp / "logs"
        env = dict(os.environ,
                   AERIAL_GATEWAY_V1=str(GW1),  # real repo: read-only use
                   DOGFOOD_VAULT=str(real_vault),
                   FEED_ROOT=str(tmp / "feed"),
                   SCRAPES_DB=str(db),
                   FEED_TOP=str(FEED_TOP),
                   TP_LOGS=str(logs))

        p = subprocess.run(["bash", str(SCRIPT)], env=env,
                           capture_output=True, text=True, timeout=300)
        print(f"[test] run1 exit={p.returncode} log:\n  "
              + (logs / "vault-feed-daily.log").read_text().replace(
                  "\n", "\n  "))
        if p.returncode != 0:
            print("[test] FAIL: run1 nonzero"); return 1
        feed_l0 = tmp / "feed" / "l0"
        base_objs = len(list((real_vault / "l0").rglob("*")))
        fed_objs = len(list(feed_l0.rglob("*")))
        added = fed_objs - base_objs
        if added < 1:
            print("[test] FAIL: feed root gained no notes"); return 1
        if added > FEED_TOP:
            print(f"[test] FAIL: cap exceeded — {added} > {FEED_TOP}")
            return 1

        # real notes untouched
        if tree_digest(real_vault) != vault_hash:
            print("[test] FAIL: real notes-vault was modified"); return 1
        if tree_digest(real_src) != src_hash:
            print("[test] FAIL: notes-src was modified"); return 1

        # idempotency: second run ingests nothing new
        p2 = subprocess.run(["bash", str(SCRIPT)], env=env,
                            capture_output=True, text=True, timeout=300)
        if p2.returncode != 0:
            print("[test] FAIL: run2 nonzero"); return 1
        fed_objs2 = len(list(feed_l0.rglob("*")))
        if fed_objs2 != fed_objs:
            print(f"[test] FAIL: not idempotent — l0 {fed_objs} -> "
                  f"{fed_objs2}"); return 1

        print(f"[test] PASS: fed {added} notes (cap {FEED_TOP}), run2 "
              f"ingested 0, real dogfood vault + notes-src untouched")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
