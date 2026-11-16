"""§7 Layout criteria L1–L4 (Zone A + tailer checkpoint)."""
import json
import time

from dashboard.daily_board import render


def _project_items(now):
    return [
        {"project": "aerial", "item": "W6 shadow week", "item_tag": "W6 shadow week",
         "status": "working", "stages": ["ingest", "curate", "digest"],
         "done_stages": ["ingest"], "evidence_artifact": "stage-3.json"},
        {"project": "gateway", "item": "M8.1 control-plane panel",
         "item_tag": "M8.1 control-plane panel", "status": "waiting-on-you",
         "stages": [], "done_stages": [], "evidence_artifact": "panel-diff.patch"},
        {"project": "gateway", "item": "M7 ingestion cleanup", "status": "done",
         "done_ts": now - 1800, "stages": ["s"], "done_stages": ["s"]},
        {"project": "aerial", "item": "blocked ingest fix", "status": "blocked",
         "stages": ["ingest"], "done_stages": [], "evidence_artifact": "err.log"},
    ]


def test_L1_rows_status_pct_eta_token_counts(now, job_runs_db, tailer, sessions):
    sessions.record_visit(visit_ts=now - 3600, hub_available=False)
    rows = render.zone_a_rows(_project_items(now), job_runs_db, tailer, sessions, now)
    # every project item renders with status in the 4 valid states
    assert {r["status"] for r in rows} <= set(render.VALID_STATUSES)
    w6 = next(r for r in rows if r["item"] == "W6 shadow week")
    # % complete from stage evidence
    assert w6["pct_complete"] == round(100 / 3, 1)
    # ETA derived from per-stage history (curate 20h + digest 30h → '2d')
    assert w6["eta"] == "2d"
    # per-item token/call counts matching the model-calls.jsonl aggregation
    assert w6["tokens"] == "5 calls / 750 tok"
    # untagged bucket for malformed/untagged lines
    assert w6["untagged_bucket"] is not None and "1 calls" in w6["untagged_bucket"]


def test_L2_collapse_and_single_done_since_source(now, job_runs_db, tailer, sessions):
    sessions.record_visit(visit_ts=now - 3600, hub_available=False)
    items = _project_items(now)
    rows = render.zone_a_rows(items, job_runs_db, tailer, sessions, now)
    # only waiting-on-you/blocked render expanded
    assert all(r["expanded"] == (r["status"] in render.EXPANDED_STATUSES) for r in rows)
    done = next(r for r in rows if r["status"] == "done-since-last-visit")
    assert done["done_label"] == "done-since-last-visit"
    # fixture: change the timestamp ONCE -> Zone A, L2 label and daily brief all
    # update from the SAME dashboard-sessions store
    new_visit = now + 60
    sessions.record_visit(visit_ts=new_visit, hub_available=False)
    rows2 = render.zone_a_rows(items, job_runs_db, tailer, sessions, now)
    done2 = next(r for r in rows2 if r["status"] == "done-since-last-visit")
    assert done2["done_label"] == (
        "done since " + time.strftime("%Y-%m-%d %H:%M", time.localtime(new_visit)))
    brief1 = render.daily_brief(rows, [], sessions)
    brief2 = render.daily_brief(rows2, [], sessions)
    assert brief1 != brief2  # brief reads the same store


def test_L3_eta_range_variance_and_insufficient_history(job_runs_db):
    eta = job_runs_db.eta_range("aerial", ["ingest", "test"])
    # ETA is a range derived from per-stage history, not a point estimate
    assert eta["hours"] == [25.0, 45.0] and eta["eta"] == "1d–2d"
    assert eta["hours"][1] - eta["hours"][0] > 0
    # completing another job does not move the ETA beyond the observed variance:
    # a new 'test' run within min/max keeps the same per-stage bounds
    import sqlite3
    con = sqlite3.connect(job_runs_db.db_path)
    con.execute("INSERT INTO job_runs VALUES ('aerial','test','done','{}',?,14.0)",
                (1_100_000.0,))
    con.commit()
    con.close()
    eta2 = job_runs_db.eta_range("aerial", ["ingest", "test"])
    assert eta2["hours"] == eta["hours"]
    # a stage type / project with no prior job-runs renders 'insufficient history'
    # — never a fabricated range, no role-level default invented silently
    assert job_runs_db.eta_range("aerial", ["deploy", "test"])["eta"] == "insufficient history"
    assert job_runs_db.eta_range("no-such-project", ["test"])["eta"] == "insufficient history"


def test_L4_checkpoint_equals_full_scan_and_partial_write(tmp_path):
    from dashboard.data_layer.model_calls_tailer import ModelCallsTailer
    ledger = tmp_path / "model-calls.jsonl"
    state = tmp_path / "tail_state.json"

    def rec(item, i, o):
        return json.dumps({"item": item, "in_tokens": i, "out_tokens": o}).encode() + b"\n"

    with open(ledger, "wb") as f:
        f.write(rec("proj-a", 100, 10))
        f.write(rec("proj-a", 200, 20))
        f.write(rec("proj-b", 50, 5))
        # partial-write fixture: truncated final line, no newline
        f.write(b'{"item": "proj-a", "in_tokens": 999, ')

    t = ModelCallsTailer(str(ledger), str(state))
    d1 = t.tail()
    # first tail excludes the partial line; totals match newline-terminated lines only
    assert d1["lines"] == 3
    assert t.checkpoint["proj-a"]["calls"] == 2 and t.checkpoint["proj-b"]["calls"] == 1

    # writer completes the line
    with open(ledger, "ab") as f:
        f.write(b'"out_tokens": 99}\n')
    d2 = t.tail()
    # next tail includes it exactly once — no double-count (rev2 finding #4:
    # tailer offset + per-item aggregate checkpoint)
    assert d2["lines"] == 1
    assert t.checkpoint["proj-a"]["in_tokens"] == 100 + 200 + 999
    assert t.checkpoint["proj-a"]["out_tokens"] == 10 + 20 + 99
    # offset+aggregate checkpoint totals equal a full JSONL scan
    v = t.verify_full_scan()
    assert v["match"]
    # re-tail is idempotent
    t.tail()
    assert t.verify_full_scan()["match"]
