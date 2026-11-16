"""Lane 3 tests: degree expansion + proposal rows in shadow mode.

Covers the lane's acceptance criteria (c), (d), (e/g) and the bound
decisions: snooze math, scoring guards (findings 2/6/7/17), queue-load cap
(finding 8), candidate-ledger suppression (finding 5), raw-text discovery
(findings 4/16), burst detection (finding 11). The final test runs the
weekly batch against the REAL reddit sources + scrape corpus (acceptance c).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from secondbrain.listening import load_sources
from secondbrain.proposals import (
    MAX_OUTSTANDING_PROPOSALS, ROUTINE_EXPIRY_DAYS, ROUTINE_SLA_HOURS,
    SNOOZE_HOLD_DAYS, SUPPRESS_AFTER_DISMISS_DAYS,
    build_evidence, density_score, detect_bursts, discover_candidates,
    expire_proposals, extract_entities, ledger_key, load_ledger,
    outstanding_proposals, record_decision, run_weekly_batch, save_ledger,
    score_candidate, suppressed,
)

NOW = 1_800_000_000.0
DAY = 86400


def make_source(sid, target, match=None, added_via="seed", **kw):
    return {"id": sid, "kind": "reddit_sub", "topic": "ai-tools",
            "target": target, "every_h": 6,
            "match": match or [["alpha"], ["beta"]],
            "added_via": added_via, "enabled": True, **kw}


def make_item(source_id, title, body="", ts=None, url=None):
    return {"source_id": source_id, "title": title, "body": body, "url": url,
            "_ts": ts if ts is not None else NOW - DAY,
            "guid": f"{source_id}:{title}",
            "_text": f"{title}\n{body}"}


# --- entity extraction (findings 4, 16) ----------------------------------------

def test_entities_from_raw_text_not_markers():
    ents = extract_entities("see r/LocalLLaMA and r/vllm for benchmarks")
    assert ("reddit_sub", "localllama") in ents
    assert ("reddit_sub", "vllm") in ents


def test_entities_github_urls_only_and_ignore_markers():
    # github.com URLs yield repo entities; other URLs do not;
    # @entity: markers are never read (finding 4)
    text = ("post https://github.com/owner/repo/wiki/page and "
            "https://example.com/foo/bar about @entity:fakeent and r/realone")
    ents = extract_entities(text)
    assert ("github_repo", "owner/repo") in ents
    assert ("reddit_sub", "realone") in ents
    assert ("reddit_sub", "fakeent") not in ents
    assert ("reddit_sub", "foo") not in ents
    # github site paths are never repo owners
    assert ("github_repo", "features/copilot") not in extract_entities(
        "https://github.com/features/copilot")


# --- discovery floors (finding 16) ----------------------------------------------

def _candidates(items, sources):
    return {(c["kind"], c["target"]): c for c in discover_candidates(sources, items)}


def test_discovery_requires_three_items_two_seeds():
    sources = [make_source("reddit.a", "asub"), make_source("reddit.b", "bsub")]
    def item(sid, t, ts): return make_item(sid, f"talk about r/target alpha {t}", ts=ts)
    # 2 items only -> rejected
    items = [item("reddit.a", "x", NOW - DAY), item("reddit.b", "y", NOW - DAY)]
    assert ("reddit_sub", "target") not in _candidates(items, sources)
    # 3 items but 1 seed -> rejected
    items = [item("reddit.a", f"m{i}", NOW - DAY) for i in range(3)]
    assert ("reddit_sub", "target") not in _candidates(items, sources)
    # 3 items from 2 seeds -> qualifies
    items = [item("reddit.a", "m1", NOW - DAY), item("reddit.a", "m2", NOW - DAY),
             item("reddit.b", "m3", NOW - DAY)]
    assert ("reddit_sub", "target") in _candidates(items, sources)


def test_discovery_excludes_seed_own_target():
    sources = [make_source("reddit.a", "asub"), make_source("reddit.b", "bsub")]
    items = [make_item("reddit.a", f"r/asub alpha post {i}", ts=NOW - DAY) for i in range(4)]
    assert ("reddit_sub", "asub") not in _candidates(items, sources)


def test_second_degree_only_from_approved_first_degree():
    sources = [make_source("reddit.p1", "p1sub", added_via="proposal:abc"),
               make_source("reddit.p2", "p2sub", added_via="proposal:abc")]
    items = [make_item("reddit.p1", f"r/child alpha mention {i}", ts=NOW - DAY) for i in range(2)]
    items += [make_item("reddit.p2", f"r/child alpha again {i}", ts=NOW - DAY) for i in range(2)]
    cands = _candidates(items, sources)
    assert cands[("reddit_sub", "child")]["degree"] == 2
    # same co-mentions from seed-added sources are 1st degree
    for s in sources:
        s["added_via"] = "seed"
    assert _candidates(items, sources)[("reddit_sub", "child")]["degree"] == 1


# --- scoring guards (findings 2, 6, 7, 17) ---------------------------------------

def _candidate_for(seed_ids=("reddit.a", "reddit.b"), n=6, target="targetsub"):
    items = [make_item(sid, f"note r/{target} alpha beta #{i}", ts=NOW - DAY)
             for sid in seed_ids for i in range(n // len(seed_ids) + 1)][:n]
    return {"kind": "reddit_sub", "target": target, "degree": 1,
            "seed_ids": list(seed_ids), "items": items,
            "distinct_items": len(items)}


def test_density_fixed_absolute_scale():
    assert density_score(0) == 0.0
    assert density_score(40) == 1.0
    assert density_score(400) == 1.0          # capped at 40/wk
    assert 0.0 < density_score(2) < density_score(10) < 1.0


def test_score_sample_floor_and_term_group_floor():
    sources = [make_source("reddit.a", "asub"), make_source("reddit.b", "bsub")]
    # sample floor: <5 matched items cannot score (finding 17)
    small = _candidate_for(n=4)
    assert score_candidate(small, sources, {}) is None
    # term-group floor: a single seed whose corpus hits only ONE of its
    # two term-groups cannot score (overlap requires >=2 groups)
    single = [make_source("reddit.a", "asub")]
    one_group = _candidate_for(seed_ids=("reddit.a",), n=6)
    for it in one_group["items"]:
        it["_text"] = it["_text"].replace("beta", "")
    assert score_candidate(one_group, single, {}) is None
    ok = _candidate_for(n=6)
    scores = score_candidate(ok, sources, {})
    assert scores is not None and {"A", "U", "G"} <= set(scores)


def test_score_seed_rate_zero_guard():
    sources = [make_source("reddit.a", "asub"), make_source("reddit.b", "bsub")]
    for s in sources:
        s["_rate_per_week"] = 0.0
    assert score_candidate(_candidate_for(), sources, {}) is None


def test_g_independent_seed_math():
    sources = [make_source(f"reddit.{c}", f"{c}sub") for c in "abc"]
    one = score_candidate(_candidate_for(seed_ids=("reddit.a",)), sources, {})
    two = score_candidate(_candidate_for(seed_ids=("reddit.a", "reddit.b")), sources, {})
    three = score_candidate(_candidate_for(seed_ids=tuple(f"reddit.{c}" for c in "abc")), sources, {})
    assert one["G"] == pytest.approx(1 / 3, abs=0.01)
    assert two["G"] == pytest.approx(2 / 3, abs=0.01)
    assert three["G"] == 1.0


# --- U term + calibration (acceptance d) ------------------------------------------

def test_u_cold_start_and_redundancy():
    sources = [make_source("reddit.a", "asub"), make_source("reddit.b", "bsub")]
    fresh = score_candidate(_candidate_for(target="freshsub"), sources, {})
    assert fresh["U"] == pytest.approx(0.5)          # cold start 0.5
    dup = score_candidate(_candidate_for(target="asub-clone"), sources, {})
    assert dup["U"] < fresh["U"]                     # duplicates penalized


def test_dismissing_duplicate_lowers_future_u(tmp_path):
    sources = [make_source("reddit.a", "asub"), make_source("reddit.b", "bsub")]
    cand = _candidate_for(target="dupedge")
    before = score_candidate(cand, sources, {})["U"]
    ledger = load_ledger(tmp_path / "l.json")
    # <5 dismissals: NO learning effect (bound decision)
    for _ in range(4):
        record_decision(ledger, "reddit_sub:dupedge", "dismiss",
                        reason="duplicate of reddit.claudeai")
    mid = score_candidate(cand, sources, ledger)["U"]
    assert mid == pytest.approx(before)
    # 5th dismissal activates the U learning path
    record_decision(ledger, "reddit_sub:dupedge", "dismiss",
                    reason="duplicate of reddit.claudeai")
    after = score_candidate(cand, sources, ledger)["U"]
    assert after < before, "duplicate dismissals must lower a similar candidate's U"


def test_duplicate_reason_lowers_similar_target_u(tmp_path):
    sources = [make_source("reddit.a", "asub"), make_source("reddit.b", "bsub")]
    ledger = load_ledger(tmp_path / "l.json")
    for _ in range(5):
        record_decision(ledger, "reddit_sub:dupedge", "dismiss",
                        reason="duplicate of reddit.a")
    similar = score_candidate(_candidate_for(target="dupedge-two"), sources, ledger)
    unrelated = score_candidate(_candidate_for(target="zzz-unrelated"), sources, ledger)
    assert similar["U"] < unrelated["U"]


# --- candidate ledger suppression (finding 5) ---------------------------------------

def test_ledger_suppression_8_weeks_and_permanent(tmp_path):
    ledger = load_ledger(tmp_path / "l.json")
    record_decision(ledger, "reddit_sub:spamsite", "dismiss", reason="noise", now=NOW)
    assert suppressed(ledger, "reddit_sub", "spamsite", NOW + DAY)
    assert not suppressed(ledger, "reddit_sub", "spamsite",
                          NOW + (SUPPRESS_AFTER_DISMISS_DAYS + 1) * DAY)
    for _ in range(2):
        record_decision(ledger, "reddit_sub:spamsite", "dismiss", reason="noise", now=NOW)
    assert ledger["candidates"]["reddit_sub:spamsite"]["dismiss_count"] == 3
    assert suppressed(ledger, "reddit_sub", "spamsite", NOW + 400 * DAY)  # permanent


# --- snooze / SLA / expiry math (acceptance e/g, finding 1) --------------------------

def test_snooze_expiry_math_and_cap(tmp_path):
    ledger = load_ledger(tmp_path / "l.json")
    rec = record_decision(ledger, "reddit_sub:holdme", "snooze", now=NOW)
    # snooze holds 14 days PAST the routine expiry
    assert rec["expires_at"] == time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(
            NOW + ROUTINE_EXPIRY_DAYS * DAY + SNOOZE_HOLD_DAYS * DAY))
    assert rec["snooze_count"] == 1
    assert rec["state"] == "snoozed"
    record_decision(ledger, "reddit_sub:holdme", "snooze", now=NOW)
    assert ledger["candidates"]["reddit_sub:holdme"]["snooze_count"] == 2
    # third snooze exceeds the re-propose cap -> auto-dismiss (finding 1)
    rec = record_decision(ledger, "reddit_sub:holdme", "snooze", now=NOW)
    assert rec["state"] == "dismissed"
    assert rec["dismiss_count"] == 1


def test_sla_breach_once_and_expiry(tmp_path):
    ledger = {"candidates": {ledger_key("reddit_sub", "slow"): {
        "state": "proposed", "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW)),
        "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                    time.gmtime(NOW + ROUTINE_EXPIRY_DAYS * DAY)),
        "kind": "reddit_sub", "target": "slow"}}}
    # before 72h: no breach
    assert expire_proposals(ledger, NOW + 71 * 3600) == []
    # past 72h: exactly ONE breach ping
    ev = expire_proposals(ledger, NOW + ROUTINE_SLA_HOURS * 3600 + 60)
    assert [e["event"] for e in ev] == ["sla_breach"]
    assert expire_proposals(ledger, NOW + 4 * DAY) == []     # not again
    # past 14 days: expired, logged once, dropped
    ev = expire_proposals(ledger, NOW + (ROUTINE_EXPIRY_DAYS + 1) * DAY)
    assert [e["event"] for e in ev] == ["expired"]
    assert ledger["candidates"]["reddit_sub:slow"]["state"] == "expired"
    assert expire_proposals(ledger, NOW + 20 * DAY) == []


# --- weekly batch: shadow mode + queue load (findings 8, 1) --------------------------

def _batch_env(tmp_path, n_outstanding=0):
    sources_path = tmp_path / "sources.json"
    sources_path.write_text(json.dumps([
        {"id": "reddit.a", "kind": "reddit_sub", "topic": "ai-tools",
         "target": "asub", "every_h": 6, "match": [["alpha"], ["beta"]]},
        {"id": "reddit.b", "kind": "reddit_sub", "topic": "ai-tools",
         "target": "bsub", "every_h": 6, "match": [["alpha"], ["beta"]]}]))
    items = [make_item("reddit.a", f"r/childsub alpha beta note {i}", ts=NOW - DAY)
             for i in range(4)]
    items += [make_item("reddit.b", f"r/childsub alpha beta reply {i}", ts=NOW - DAY)
              for i in range(3)]
    ledger_path = tmp_path / "ledger.json"
    ledger = load_ledger(ledger_path)
    for i in range(n_outstanding):
        record_decision(ledger, f"reddit_sub:filled{i}", "snooze", now=NOW)
        ledger["candidates"][f"reddit_sub:filled{i}"]["state"] = "proposed"
        ledger["candidates"][f"reddit_sub:filled{i}"]["expires_at"] = \
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW + 10 * DAY))
    save_ledger(ledger_path, ledger)
    return sources_path, items, ledger_path


def test_batch_shadow_never_surfaces_and_caps_outstanding(tmp_path):
    sources_path, items, ledger_path = _batch_env(tmp_path, n_outstanding=3)
    out = run_weekly_batch(sources_path, items, ledger_path, now=NOW)
    assert out["shadow"] is True
    assert out["waits_for_you"] is False          # finding 8 + shadow mode
    # outstanding cap: 3 already outstanding -> only 2 new slots (finding 8)
    assert len(out["proposals"]) <= MAX_OUTSTANDING_PROPOSALS - 3
    ledger = load_ledger(ledger_path)
    for row in out["proposals"]:
        assert row["class"] == "routine"
        assert row["status"] == "shadow"          # logged, NOT surfaced
        assert len(row["evidence"]) >= 3          # ≥3 verbatim evidence items
        assert {"A", "U", "G"} <= set(row)
        rec = ledger["candidates"][row["id"]]
        assert rec["state"] == "proposed"
        assert rec["expires_at"] == time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW + ROUTINE_EXPIRY_DAYS * DAY))


def test_batch_respects_suppressed_candidates(tmp_path):
    sources_path, items, ledger_path = _batch_env(tmp_path)
    ledger = load_ledger(ledger_path)
    record_decision(ledger, "reddit_sub:childsub", "dismiss", reason="noise", now=NOW)
    save_ledger(ledger_path, ledger)
    out = run_weekly_batch(sources_path, items, ledger_path, now=NOW)
    assert all(p["target"] != "childsub" for p in out["proposals"])
    assert "reddit_sub:childsub" not in load_ledger(ledger_path).get("shadow_scores", {})


# --- burst detection (finding 11) -----------------------------------------------------

def test_burst_absolute_never_suppressed_and_relative_floor():
    sources = [make_source("reddit.a", "asub", match=[["alpha"]])]
    items = [make_item("reddit.a", f"alpha flood {i}", ts=NOW) for i in range(60)]
    alarms = detect_bursts(sources, items, now=NOW)
    absolute = [a for a in alarms if a["kind"] == "absolute"]
    assert absolute and absolute[0]["today_rate"] >= 50   # never suppressed
    # relative: quiet baseline, 5x spike vs trailing 7-day median (floor 3/day)
    items = [make_item("reddit.a", f"alpha base {i}", ts=NOW - 3 * DAY) for i in range(4)]
    items += [make_item("reddit.a", f"alpha spike {i}", ts=NOW) for i in range(20)]
    alarms = detect_bursts(sources, items, now=NOW)
    assert any(a["kind"] == "relative" and a["today_rate"] >= 5 * max(a["median_7d"], 3)
               for a in alarms)


# --- acceptance (c): the REAL reddit sources + REAL scrape corpus ---------------------

REAL_SOURCES = os.environ.get("AERIAL_SOURCES_PATH", "")
REAL_DB = os.environ.get("AERIAL_SCRAPES_DB", "")


@pytest.mark.skipif(not (os.path.exists(REAL_SOURCES) and os.path.exists(REAL_DB)),
                    reason="real scrape corpus not present on this host")
def test_real_weekly_batch_produces_scored_candidate(tmp_path):
    sources = load_sources(REAL_SOURCES)
    reddit_sources = [s for s in sources if s["kind"] == "reddit_sub"]
    assert reddit_sources
    from secondbrain.proposals import fetch_scrape_items
    items = fetch_scrape_items(REAL_DB, since_days=45,
                               source_ids=[s["id"] for s in reddit_sources],
                               per_source_limit=1500)
    assert items
    out = run_weekly_batch(REAL_SOURCES, items, tmp_path / "ledger.json", now=NOW)
    ledger = load_ledger(tmp_path / "ledger.json")
    scored = {**ledger.get("shadow_scores", {}),
              **{k: v for k, v in ledger.get("candidates", {}).items()
                 if v.get("state") == "proposed"}}
    first_degree = [c for c in scored.values()
                    if c.get("degree", 1) == 1 and {"A", "U", "G"} <= set(c)]
    assert first_degree, "expected >=1 scored 1st-degree candidate from real sources"
    with_evidence = [c for c in first_degree if len(c.get("evidence", [])) >= 3]
    assert with_evidence, "candidate must carry >=3 verbatim evidence items"
    # 2nd-degree only where the parent was approved (shadow: none approved yet)
    assert all(c.get("degree", 1) == 1 for c in scored.values())
