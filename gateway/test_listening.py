"""Listening pipeline lane 2: github_repo adapter + ingest plumbing.

Covers the lane's deliverable contract (match semantics finding 20,
sensitivity finding 10, high-water mark finding 3, failure state machine
findings 12-13, approve-path validation finding 9, digest windows
finding 18) and the acceptance criteria (a), (a2), (a3), (f) — plus one
LIVE integration test against a real GitHub repo with a known recent
release (network required; skipped only if api.github.com is reachable
but the repo call fails after retries, which would be an infra fault,
so it is reported, not silently skipped).
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from secondbrain import (Curator, L0Vault, Registry, Workspace)
from secondbrain.listening import (
    DIGEST_FAILURES, NOTE_CAP_PER_CYCLE, ROUTINE_FAILURES,
    GithubRepoAdapter, GithubRepoIngestAdapter, SourceState,
    append_source, item_matches, load_sources, register_listening_adapters,
    run_cycle, validate_source_entry)

NOW = 1_800_000_000.0  # fixed clock for deterministic tests


def source(**kw):
    base = {"id": "github.demo-repo", "kind": "github_repo",
            "topic": "gateway-competitors", "target": "demo/repo",
            "every_h": 24, "watch": ["releases"],
            "match": [["cost"], ["quota"]], "relation": "competitor",
            "added_via": "seed"}
    base.update(kw)
    return base


def rel(tag, ts, body="", name=None):
    return {"tag_name": tag, "name": name or f"Release {tag}",
            "body": body, "published_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
            "html_url": f"https://github.com/demo/repo/releases/tag/{tag}"}


def commit(msg, ts):
    return {"commit": {"message": msg,
                       "committer": {"date": time.strftime(
                           "%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))}},
            "html_url": "https://github.com/demo/repo/commit/abc"}


def fake_fetch(events_by_url):
    """events_by_url: substring of url -> (status, json)."""
    def fetch(url, timeout=30.0):
        for frag, (status, data) in events_by_url.items():
            if frag in url:
                return status, data
        return 200, []
    return fetch


# --- match semantics (finding 20) --------------------------------------------

def test_match_empty_list_matches_nothing():
    assert item_matches([], "cost quota pricing") is False


def test_match_empty_group_matches_all():
    assert item_matches([[]], "anything at all") is True


def test_match_group_requires_all_terms_of_one_group():
    assert item_matches([["cost", "quota"]], "cost only") is False
    assert item_matches([["cost", "quota"]], "cost and quota up") is True
    assert item_matches([["cost"], ["auth"]], "auth flow") is True


# --- approve-path validation (finding 9) ---------------------------------------

def test_validate_slugifies_and_allowlists_id():
    norm = validate_source_entry(source(id="github.Demo Repo!!"))
    assert norm["id"] == "github.demo-repo"


def test_validate_rejects_unknown_kind_and_bad_fields():
    with pytest.raises(Exception):
        validate_source_entry(source(kind="twitter"))
    with pytest.raises(Exception):
        validate_source_entry(source(target=""))
    with pytest.raises(Exception):
        validate_source_entry(source(match="cost"))
    with pytest.raises(Exception):
        validate_source_entry(source(relation="rival"))


# --- S3: target length + charset caps (buglist 2026-11-14) ---------------------

def test_validate_rejects_oversized_target():
    with pytest.raises(Exception, match="target too long"):
        validate_source_entry(source(target="a" * 2049))


def test_validate_accepts_target_at_limit():
    norm = validate_source_entry(source(target="https://example.com/" + "a" * 2000))
    assert norm["target"].startswith("https://example.com/")


@pytest.mark.parametrize("bad", [
    "owner/repo; rm -rf /",          # semicolon
    "owner/repo | cat /etc/passwd",  # pipe
    "owner/repo & echo hi",          # ampersand
    "$(curl evil)",                  # command substitution
    "`id`",                          # backtick substitution
    "owner/repo\nrm -rf /",          # newline / control char
    "owner/repo\x1b[31m",            # escape / control char
    "owner/repo > /etc/passwd",      # redirect
])
def test_validate_rejects_shell_metachar_targets(bad):
    with pytest.raises(Exception, match="forbidden characters"):
        validate_source_entry(source(target=bad))


def test_validate_accepts_long_but_safe_target():
    # a long, legit URL with slashes, colons, dots, dashes — no metachars
    long_url = ("https://sub.example.com/very/long/path/"
                "segment-with-dashes_and_underscores" * 20)[:2048]
    assert len(long_url) <= 2048
    norm = validate_source_entry(source(target=long_url))
    assert norm["target"] == long_url


def test_github_pushes_requires_nonempty_match():
    with pytest.raises(Exception):
        validate_source_entry(source(kind="github_pushes", match=[],
                                     watch=["pushes"]))


def test_append_source_rejects_duplicate_id_and_pair(tmp_path):
    p = tmp_path / "new_sources.json"
    p.write_text(json.dumps([source()]))
    with pytest.raises(Exception):
        append_source(p, source())                       # same id
    with pytest.raises(Exception):
        append_source(p, source(id="github.other"))      # same (kind,target)
    added = append_source(p, source(id="github.second", target="demo/two"))
    assert added["id"] == "github.second"
    assert len(json.loads(p.read_text())) == 2


def test_load_sources_validates_whole_file(tmp_path):
    p = tmp_path / "new_sources.json"
    p.write_text(json.dumps([source(), source(id="github.bad", kind="nope")]))
    with pytest.raises(Exception):
        load_sources(p)
    p.write_text(json.dumps([source(), source(id="github.two", target="x/y")]))
    assert len(load_sources(p)) == 2


# --- state: high-water mark + failure machine (finding 3, 12-13) ---------------

def test_state_last_seen_init_and_backfill():
    st = SourceState(tmp_path_state := Path("/tmp") / "unused")
    st.data = {}
    st.init_source(source(), NOW)
    assert st.get("github.demo-repo", "last_seen_ts") == NOW
    st2 = SourceState(Path("/tmp") / "unused2"); st2.data = {}
    st2.init_source(source(backfill_days=7), NOW)
    assert st2.get("github.demo-repo", "last_seen_ts") == NOW - 7 * 86400


def test_state_backoff_and_failure_levels():
    st = SourceState(Path("/tmp") / "unused3"); st.data = {}
    assert st.record_failure("s", "transient", "HTTP 429", NOW) == "logged"
    assert st.record_failure("s", "transient", "HTTP 502", NOW) == "digest"
    assert st.record_failure("s", "transient", "HTTP 503", NOW) == "digest"
    assert st.record_failure("s", "transient", "HTTP 500", NOW) == "routine"
    assert st.get("s", "backoff_until") > NOW
    st.record_success("s", None)
    assert st.get("s", "consecutive_failures") == 0
    assert st.get("s", "backoff_until") is None


def test_state_permanent_failure_auto_disables():
    st = SourceState(Path("/tmp") / "unused4"); st.data = {}
    level = st.record_failure("s", "permanent", "HTTP 404", NOW)
    assert level == "disabled"
    assert st.get("s", "disabled") is True


def test_state_classifies_transient_vs_permanent():
    st = SourceState(Path("/tmp") / "unused5"); st.data = {}
    assert st.classify_failure(404) == "permanent"
    assert st.classify_failure(410) == "permanent"
    assert st.classify_failure(429) == "transient"
    assert st.classify_failure(503) == "transient"
    assert st.classify_failure(None, ValueError("schema")) == "permanent"
    assert st.classify_failure(None, TimeoutError()) == "transient"


def test_state_transcript_budget_20_per_day():
    st = SourceState(Path("/tmp") / "unused6"); st.data = {}
    for _ in range(20):
        assert st.spend_transcript("s", NOW)
    assert not st.spend_transcript("s", NOW)
    assert st.spend_transcript("s", NOW + 86400)  # next day resets


# --- the adapter: notes, caps, suppression, windows -----------------------------

def make_env(tmp_path, events_by_url, **src_kw):
    root = tmp_path / "gateway"
    state = SourceState(root / "listening" / "state.json")
    # add-time is one tick before the fixture events (last_seen init rule)
    state.init_source(source(**src_kw), NOW - 1000)
    adapter = GithubRepoAdapter(state, fetch=fake_fetch(events_by_url), now=NOW)
    return root, state, adapter


def test_note_shape_entities_tag_sensitivity_published_meta(tmp_path):
    _, _, adapter = make_env(tmp_path, {})
    src = source(relation="own")
    note = adapter.build_note(src, {"event": "release", "ts": NOW - 60,
                                    "title": "v1", "body": "cost change",
                                    "url": "u"}, "github-demo-repo")
    assert "@entity:github-demo-repo" in note
    assert "@entity:demo-repo" in note
    assert "#gateway-competitors" in note
    assert "sensitivity: private" in note           # relation: own -> private
    assert f"published: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(NOW-60))}" in note
    assert '"source_kind": "github_repo"' in note
    assert '"weight": 0.8' in note
    assert '"ingest_ts"' in note
    # never hardcoded public (finding 10)
    src2 = source(sensitivity="personal")
    assert "sensitivity: personal" in adapter.build_note(
        src2, {"event": "release", "ts": NOW, "title": "t", "body": "",
               "url": ""}, "e")


def test_poll_release_match_and_dedupe_on_rerun(tmp_path):
    events = {"releases": (200, [rel("v1.0", NOW - 100, body="new cost model")])}
    root, state, adapter = make_env(tmp_path, events)
    staging = tmp_path / "staging"
    r1 = adapter.poll(source(), staging)
    assert r1["stored"] == 1
    ws = Workspace(root / "l1"); vault = L0Vault(root / "l0")
    Curator(vault, ws).ingest_folder(staging)
    # re-run: last_seen advanced -> no new events; and even a forced re-poll
    # of identical events is a content-hash no-op in the vault (G1)
    r2 = adapter.poll(source(), staging)
    assert r2["stored"] == 0
    ws2 = Workspace(root / "l1"); ws2.load()
    before = len(list((root / "l0" / "objects").glob("*.json")))
    Curator(vault, ws2).ingest_folder(staging)
    after = len(list((root / "l0" / "objects").glob("*.json")))
    assert before == after


def test_poll_unmatched_events_emit_nothing(tmp_path):
    events = {"releases": (200, [rel("v1.0", NOW - 100, body="renamed a file")])}
    _, _, adapter = make_env(tmp_path, events)
    r = adapter.poll(source(), tmp_path / "staging")
    assert r["stored"] == 0


def test_poll_respects_note_cap_200(tmp_path):
    rels = [rel(f"v{i}", NOW - 100 + i, body="cost") for i in range(250)]
    _, _, adapter = make_env(tmp_path, {"releases": (200, rels)})
    r = adapter.poll(source(), tmp_path / "staging")
    assert r["stored"] == NOTE_CAP_PER_CYCLE
    assert r["capped"] == 50


def test_poll_suppresses_stale_published(tmp_path):
    old = rel("v0.9", NOW - 90 * 86400, body="cost")     # 90 days old
    fresh = rel("v1.0", NOW - 100, body="cost")
    # backfill window reaches the stale release; the age gate then
    # suppresses it (finding 18) while the fresh one is stored
    _, _, adapter = make_env(tmp_path, {"releases": (200, [old, fresh])},
                             backfill_days=100)
    r = adapter.poll(source(), tmp_path / "staging")
    assert r["suppressed"] == 1
    assert r["stored"] == 1


def test_poll_permanent_404_one_row_then_disable(tmp_path):
    _, state, adapter = make_env(tmp_path, {"releases": (404, None)})
    staging = tmp_path / "staging"
    r1 = adapter.poll(source(), staging)
    assert r1["level"] == "disabled"
    assert state.get("github.demo-repo", "disabled") is True
    # disabled source emits nothing on later cycles (a3)
    r2 = adapter.poll(source(), staging)
    assert r2["level"] == "disabled"
    assert r2["stored"] == 0


def test_failed_cycle_does_not_advance_last_seen_a2(tmp_path):
    # cycle 1: transient 500 -> nothing ingested, mark not advanced
    env_events = {"releases": (500, None)}
    root, state, adapter = make_env(tmp_path, env_events)
    staging = tmp_path / "staging"
    r1 = adapter.poll(source(), staging)
    assert r1["level"] == "logged"     # single transient failure: logged only
    # second consecutive failure -> digest line
    state.set("github.demo-repo", "backoff_until", None)
    r1b = adapter.poll(source(), staging)
    assert r1b["level"] == "digest"
    mark_after_failures = state.get("github.demo-repo", "last_seen_ts")
    # cycle 2: success -> events since the ORIGINAL mark are ingested
    adapter.fetch = fake_fetch({"releases": (200, [rel("v1.0", NOW - 50, body="cost")])})
    adapter.now = NOW + 10
    state.set("github.demo-repo", "backoff_until", None)  # backoff elapsed
    r2 = adapter.poll(source(), staging)
    assert r2["stored"] == 1           # not re-ingested-and-lost, not skipped
    assert state.get("github.demo-repo", "last_seen_ts") >= NOW - 50


def test_push_window_match_since_last_seen(tmp_path):
    src = source(id="github.hot", kind="github_pushes", watch=["pushes"],
                 match=[["cost"]], every_h=6)
    state = SourceState(tmp_path / "s.json"); state.data = {}
    state.init_source(src, NOW - 3600)
    adapter = GithubRepoAdapter(
        state, fetch=fake_fetch({"commits": (200, [
            commit("cut cost of tokens", NOW - 10),
            commit("refactor unrelated", NOW - 20)])}), now=NOW)
    r = adapter.poll(src, tmp_path / "staging")
    assert r["stored"] == 1            # only the matching push
    text = sorted((tmp_path / "staging").rglob("*.md"))[0].read_text()
    assert "[push]" in text


# --- digest (findings 12-13, 18) -------------------------------------------------

def test_digest_windows_on_ingest_ts_and_failure_rows(tmp_path):
    src = source()
    st = SourceState(tmp_path / "s.json"); st.data = {}
    st.init_source(src, NOW)
    # 2 consecutive failures -> digest line
    st.record_failure(src["id"], "transient", "HTTP 500", NOW)
    st.record_failure(src["id"], "transient", "HTTP 500", NOW)
    d1 = run_cycle.__globals__["build_listening_digest"](
        st, [src], [{"source": src["id"], "stored": 1, "suppressed": 0,
                     "capped": 0, "errors": [], "level": None}],
        now=NOW)
    assert len(d1["failures"]) == 1 and d1["waits_for_you"]
    assert d1["window"] == "ingest_ts"
    # 4 -> routine queue row with error evidence
    st.set(src["id"], "backoff_until", None)
    st.record_failure(src["id"], "transient", "HTTP 500", NOW)
    st.record_failure(src["id"], "transient", "HTTP 500", NOW)
    d2 = run_cycle.__globals__["build_listening_digest"](st, [src], [], now=NOW)
    assert len(d2["routine_queue"]) == 1
    assert d2["routine_queue"][0]["evidence"]["error"] == "HTTP 500"
    # permanent -> disabled row
    st.record_failure("github.other", "permanent", "HTTP 404", NOW)
    d3 = run_cycle.__globals__["build_listening_digest"](
        st, [src, source(id="github.other", target="a/b")], [], now=NOW)
    assert len(d3["disabled"]) == 1


# --- registry registration (registration-only by design) ------------------------

def test_registry_registration_and_ingest(tmp_path):
    root = tmp_path / "gw"
    vault = L0Vault(root / "l0")
    ws = Workspace(root / "l1"); ws.load()
    curator = Curator(vault, ws)
    state = SourceState(root / "listening" / "state.json")
    state.init_source(source(), NOW - 1000)
    reg = Registry()
    adapter = GithubRepoIngestAdapter(
        vault, curator, state,
        fetch=fake_fetch({"releases": (200, [rel("v1", NOW - 10, body="cost")])}),
        now=NOW)
    reg.register(adapter)
    assert "github_repo" in reg.kinds()
    hashes = reg.ingest("github_repo", source())
    assert len(hashes) == 1
    register_listening_adapters(Registry(), vault, curator, state)  # smoke


# --- (f) one malformed result in a batch does not lose the others ---------------

def test_malformed_file_isolated_in_folder_ingest(tmp_path):
    root = tmp_path / "gw"
    vault = L0Vault(root / "l0")
    ws = Workspace(root / "l1"); ws.load()
    staging = tmp_path / "staging"; staging.mkdir()
    good = staging / "001-good.md"
    good.write_text("@entity:demo-repo\n\n# note\ncost\n\nsensitivity: personal\n")
    bad = staging / "002-bad.md"
    bad.write_bytes(b"\xff\xfe\x00broken")
    report = Curator(vault, ws).ingest_folder(staging)
    assert len(report["stored"]) == 1
    assert len(report["errors"]) == 1
    assert list(report["skipped"]) == []


# --- full plumbing: run_cycle end-to-end with fake fetch --------------------------

def test_run_cycle_end_to_end(tmp_path):
    sp = tmp_path / "new_sources.json"
    sp.write_text(json.dumps([source(backfill_days=1)]))
    events = {"releases": (200, [rel("v1.0", NOW - 100, body="cost quota")])}
    out = run_cycle(sp, tmp_path / "gw", fetch=fake_fetch(events), now=NOW)
    assert out["ingested"] == 1
    d = json.loads(Path(out["digest_path"]).read_text())
    assert d["window"] == "ingest_ts"
    # idempotent second cycle
    out2 = run_cycle(sp, tmp_path / "gw", fetch=fake_fetch(events),
                     now=NOW + 60)
    assert out2["ingested"] == 0


# --- LIVE integration (acceptance a): real repo, known recent release ----------

LIVE_REPO = "modelcontextprotocol/servers"


def test_live_real_repo_release_produces_note_once(tmp_path):
    """Acceptance (a) live: a real repo with a known recent release
    produces a matched, deduplicated, freshness-stamped note; re-running
    ingest does not duplicate it."""
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{LIVE_REPO}/releases?per_page=5",
            headers={"User-Agent": "aerial-listening-test"})
        live = json.load(urllib.request.urlopen(req, timeout=30))
    except Exception as exc:  # pragma: no cover - infra fault, reported loudly
        pytest.fail(f"live GitHub fetch failed (network/infra): {exc}")
    assert live, "expected at least one release on the live repo"
    latest = live[0]
    assert latest.get("published_at"), "live release missing published_at"

    sp = tmp_path / "new_sources.json"
    sp.write_text(json.dumps([source(
        id="github.mcp-servers", target=LIVE_REPO,
        match=[[]],   # empty group = match all: the live release is unknown text
        backfill_days=30)]))
    # live fetcher (production path), fixed clock = release publish time + 1s
    ts = time.mktime(time.strptime(latest["published_at"],
                                   "%Y-%m-%dT%H:%M:%SZ")) + 1
    out = run_cycle(sp, tmp_path / "gw", now=ts)
    assert out["ingested"] >= 1
    notes = list((tmp_path / "gw" / "listening" / "staging").rglob("*.md"))
    assert notes
    text = notes[0].read_text()
    assert "@entity:" in text and "sensitivity:" in text
    assert "published:" in text
    assert latest["published_at"][:10] in text
    # re-run: no duplicate (G1 hash + advanced high-water mark)
    out2 = run_cycle(sp, tmp_path / "gw", now=ts + 600)
    assert out2["ingested"] == 0
    vault = L0Vault(tmp_path / "gw" / "l0")
    assert len(vault.all_hashes()) == out["ingested"]
