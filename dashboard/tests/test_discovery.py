"""Tests for dashboard.listening.discovery (P1b degree-expansion discovery).

The fixture ``scrapes.db`` is created in tmp_path via sqlite3 — the real
scrapes.db is never touched (the production path opens it read-only).
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

GATEWAY = Path("~/workspaces/build/second-brain-gateway/v1").expanduser()
if str(GATEWAY) not in sys.path:
    sys.path.insert(0, str(GATEWAY))

from dashboard.listening import discovery  # noqa: E402
from dashboard.exception_queue.queue import Queue  # noqa: E402

NOW = 1_800_000_000.0            # fixed epoch for determinism
DAY = 86400.0

SOURCES = [
    {"id": "reddit_sub.machinelearning", "kind": "reddit_sub",
     "topic": "ml", "target": "MachineLearning",
     "every_h": 24, "match": [["ml"]], "relation": "competitor",
     "added_via": "seed", "enabled": True},
    {"id": "github_repo.acme-toolkit", "kind": "github_repo",
     "topic": "ml", "target": "acme/toolkit",
     "every_h": 24, "match": [["ml"]], "relation": "dependency",
     "added_via": "seed", "enabled": True},
]


@pytest.fixture
def sources_file(tmp_path):
    p = tmp_path / "new_sources.json"
    p.write_text(json.dumps(SOURCES), encoding="utf-8")
    return p


@pytest.fixture
def scrapes_db(tmp_path):
    """Fixture scrapes.db with a small ingested corpus."""
    db = tmp_path / "scrapes.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE items(id INTEGER PRIMARY KEY, source_id TEXT NOT NULL,"
        " topic TEXT, guid TEXT NOT NULL, title TEXT, url TEXT, body TEXT,"
        " published TEXT, fetched_at TEXT NOT NULL, run_id TEXT NOT NULL,"
        " UNIQUE(source_id, guid))")
    rows = [
        # 8 items mention r/LocalLLaMA from 2 different ingested sources
        ("reddit_sub.machinelearning", "g1", "try r/LocalLLaMA for models",
         "https://reddit.com/r/MachineLearning/1", NOW - 1 * DAY),
        ("reddit_sub.machinelearning", "g2", "r/LocalLLaMA thread of the week",
         "https://reddit.com/r/MachineLearning/2", NOW - 2 * DAY),
        ("github_repo.acme-toolkit", "g3", "see the r/LocalLLaMA benchmarks",
         "https://github.com/acme/toolkit/releases", NOW - 3 * DAY),
        ("github_repo.acme-toolkit", "g4", "crossposted from r/LocalLLaMA",
         "https://github.com/acme/toolkit/issues", NOW - 4 * DAY),
        ("reddit_sub.machinelearning", "g9", "r/LocalLLaMA weekly roundup",
         None, NOW - 1 * DAY),
        ("reddit_sub.machinelearning", "g10", "ask r/LocalLLaMA anything",
         None, NOW - 2 * DAY),
        ("github_repo.acme-toolkit", "g11", "benchmarks mirror r/LocalLLaMA",
         None, NOW - 1 * DAY),
        ("github_repo.acme-toolkit", "g12", "r/LocalLLaMA favourite quants",
         None, NOW - 2 * DAY),
        # co-citation of github repo acme-labs/fastinfer by both sources
        ("reddit_sub.machinelearning", "g5",
         "speedups land in github.com/acme-labs/fastinfer",
         "https://reddit.com/r/MachineLearning/5", NOW - 5 * DAY),
        ("github_repo.acme-toolkit", "g6",
         "we depend on github.com/acme-labs/fastinfer now",
         "https://github.com/acme/toolkit/6", NOW - 6 * DAY),
        # candidate that IS a followed source (github.com/acme/toolkit) — dedup
        ("reddit_sub.machinelearning", "g7",
         "github.com/acme/toolkit is great", None, NOW - 7 * DAY),
        # weak candidate: 1 mention, 1 source, old
        ("reddit_sub.machinelearning", "g8",
         "check out r/ObscureSubOfTheDay",
         None, NOW - 29 * DAY),
    ]
    for sid, guid, title, url, ts in rows:
        conn.execute(
            "INSERT INTO items(source_id, topic, guid, title, url, body,"
            " published, fetched_at, run_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (sid, "ml", guid, title, url, "",
             time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
             time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)), "r1"))
    conn.commit()
    conn.close()
    return db


def _items(db, since_days=30):
    from secondbrain import proposals as sbp
    return sbp.fetch_scrape_items(str(db), since_days=since_days)


def _sources(sources_file):
    from secondbrain import listening as sbl
    return sbl.load_sources(str(sources_file))


# --- mention counting ---------------------------------------------------------

def test_mention_counting_per_item_dedup(scrapes_db, sources_file):
    recs = {c["target"]: c for c in
            discovery.count_mentions(_sources(sources_file),
                                     _items(scrapes_db), now=NOW)}
    localllama = recs["localllama"]
    assert localllama["kind"] == "reddit_sub"
    assert localllama["mentions"] == 8           # one per item, not per hit
    assert localllama["distinct_sources"] == 2
    refs = {r["ref"] for r in localllama["item_refs"]}
    assert "https://reddit.com/r/MachineLearning/1" in refs  # g1's url


def test_mention_recency_decay(scrapes_db, sources_file):
    recs = {c["target"]: c for c in
            discovery.count_mentions(_sources(sources_file),
                                     _items(scrapes_db), now=NOW)}
    # 8 items at ages 1,2,3,4,1,2,1,2 days: mean of exp(-age*ln2/14)
    import math
    ages = (1, 2, 3, 4, 1, 2, 1, 2)
    expect = sum(math.exp(-d * math.log(2) / 14.0) for d in ages) / len(ages)
    assert abs(recs["localllama"]["recency"] - expect) < 1e-6
    # the 29-day-old single mention is far colder
    assert recs["obscuresuboftheday"]["recency"] < 0.3


# --- scoring math -------------------------------------------------------------

def test_score_math():
    assert discovery.score_candidate(
        {"mentions": 10, "recency": 1.0, "distinct_sources": 3}) == 1.0
    assert discovery.score_candidate(
        {"mentions": 10, "recency": 0.5, "distinct_sources": 3}) == 0.5
    # frequency saturates at 10 mentions; distinct saturates at 3
    assert discovery.score_candidate(
        {"mentions": 100, "recency": 1.0, "distinct_sources": 99}) == 1.0
    assert discovery.score_candidate(
        {"mentions": 5, "recency": 1.0, "distinct_sources": 1}) \
        == pytest.approx(0.5 * (1 / 3), abs=1e-6)


def test_cocitation_flag_and_boost(scrapes_db, sources_file):
    ranked = discovery.rank_candidates(_sources(sources_file),
                                       _items(scrapes_db), now=NOW)
    by_target = {r["target"]: r for r in ranked}
    fast = by_target["acme-labs/fastinfer"]
    assert fast["cocitation"] is True              # 2 distinct followed sources
    base = discovery.score_candidate(fast)
    assert fast["score"] == pytest.approx(min(1.0, base * 1.5), abs=1e-6)
    assert by_target["obscuresuboftheday"]["cocitation"] is False


# --- dedup against existing sources --------------------------------------------

def test_dedup_against_followed(scrapes_db, sources_file):
    ranked = discovery.rank_candidates(_sources(sources_file),
                                       _items(scrapes_db), now=NOW)
    targets = {c["target"] for c in ranked}
    assert "acme/toolkit" not in targets          # followed github repo
    assert "machinelearning" not in targets       # followed sub itself
    deduped = discovery.dedup_against_followed(ranked, _sources(sources_file))
    assert all(c["target"] != "acme/toolkit" for c in deduped)


# --- domain extraction filters -------------------------------------------------

def test_extract_domains_filters_noise():
    assert "example.com" in discovery.extract_domains("see example.com now")
    # stoplist (with www. stripping), IPs, and repo-filename pseudo-domains
    text = ("visit www.reddit.com and preview.redd.it and raw.githubusercontent.com"
            " and 127.0.0.1 and t.co, plus readme.md settings.json install.sh"
            " and example.com")
    got = discovery.extract_domains(text)
    assert got == {"example.com"}


# --- queue proposals: shadow + idempotency --------------------------------------

def test_queue_idempotency(scrapes_db, sources_file, tmp_path):
    ledger_path = tmp_path / "ledger.json"
    queue = Queue(db_path=":memory:")
    ranked = discovery.dedup_against_followed(
        discovery.rank_candidates(_sources(sources_file), _items(scrapes_db),
                                  now=NOW), _sources(sources_file))
    first = discovery.propose_top_n(ranked, queue,
                                    discovery.load_proposal_ledger(ledger_path),
                                    ledger_path=ledger_path,
                                    top_n=5, now=NOW)
    assert first, "at least one proposal should fire on the fixture corpus"
    keys = [p["condition_key"] for p in first]
    assert len(keys) == len(set(keys))
    ledger = discovery.load_proposal_ledger(ledger_path)
    # second run with the SAME ranked list: zero new proposals
    second = discovery.propose_top_n(ranked, queue, ledger, top_n=5, now=NOW)
    assert second == []
    # and a re-run of the whole batch against a fresh Queue must also be
    # idempotent via the ledger (no double-add of the same condition_key)
    ledger2 = discovery.load_proposal_ledger(ledger_path)
    third = discovery.propose_top_n(ranked, Queue(db_path=":memory:"),
                                    ledger2, top_n=5, now=NOW)
    assert third == []


def test_proposals_are_shadow_routine_rows(scrapes_db, sources_file, tmp_path):
    queue = Queue(db_path=":memory:")
    ledger = {"proposed": {}}
    ranked = discovery.dedup_against_followed(
        discovery.rank_candidates(_sources(sources_file), _items(scrapes_db),
                                  now=NOW), _sources(sources_file))
    proposed = discovery.propose_top_n(ranked, queue, ledger, top_n=5, now=NOW)
    items = queue.ordered(now=NOW)
    assert items and len(items) == len(proposed)
    for it in items:
        assert it.impact_class == "routine"
        assert it.evidence_label == "inferred"
        assert it.source_agent == "listening-discovery"
        assert it.condition_key.startswith("source-proposal:")
        assert "new_sources.json ONLY" in it.proposed_action
    # shadow: the ledger records proposals, new_sources.json is untouched
    assert len(ledger["proposed"]) == len(proposed)


def test_run_discovery_end_to_end(scrapes_db, sources_file, tmp_path):
    ledger_path = tmp_path / "ledger.json"
    queue = Queue(db_path=":memory:")
    batch = discovery.run_discovery(str(sources_file), str(scrapes_db),
                                    queue=queue, ledger_path=ledger_path,
                                    now=NOW)
    assert batch["shadow"] is True
    assert batch["waits_for_you"] is False
    assert batch["items_scanned"] == 12
    assert batch["proposed"], "top candidate should be proposed"
    assert batch["proposed"][0]["target"] == "localllama"
    md = discovery.render_markdown(batch)
    assert "## Source discovery (degree expansion) — shadow" in md
    assert "acme-labs/fastinfer" in md or "localllama" in md
    assert "never automatically" in md


def test_fixture_db_opened_read_only(scrapes_db):
    # the production loader must open the db read-only (URI mode=ro)
    conn = sqlite3.connect(f"file:{scrapes_db}?mode=ro", uri=True)
    assert conn.execute("select count(*) from items").fetchone()[0] == 12
    conn.close()
