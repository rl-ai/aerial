"""P1a SWOT layer tests: schema validation (evidence required, window
math), heuristic generation on fixture digest items, exception-queue
append idempotency, and the weekly cadence job shape."""
import json
import os
import sys

import pytest

BUILD = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BUILD not in sys.path:
    sys.path.insert(0, BUILD)

from dashboard.exception_queue.queue import Queue, load_policy  # noqa: E402
from dashboard.listening import swot as S                       # noqa: E402

T0 = 1_800_000_000.0            # fixed "now" (epoch)
DAY = 86400.0

ENTITIES = S.load_entities()
ENTITY = ENTITIES[0]            # local-inference-orchestration


def item(iid, title, summary="", source="src.a", ts=None, score=None):
    return {"item_id": iid, "title": title, "summary": summary or title,
            "source": source, "ts": ts, "score": score}


def fixture_items(now=T0):
    """One item per quadrant + an out-of-window item + an unrelated one."""
    return [
        item("i-strength", "vLLM released faster batching",
             "released improved throughput", ts=now - 2 * DAY),
        item("i-weakness", "llama.cpp crash on quantized model",
             "regression crash oom bug", ts=now - 3 * DAY),
        item("i-opportunity", "community request for ollama integration",
             "request integration proposal", ts=now - 1 * DAY),
        item("i-threat", "competitor alternative with better pricing",
             "competitor alternative migration", ts=now - 4 * DAY),
        item("i-stale", "old released note", "released", ts=now - 30 * DAY),
        item("i-noise", "unrelated gardening tips", "tomatoes", ts=now - 1 * DAY),
    ]


# ---------------------------------------------------------------- schema

def make_valid_swot():
    return {
        "entity": "local-inference-orchestration",
        "generated_at": S.now_iso(T0),
        "window_days": 7,
        "strengths": [{"text": "s", "item_ids": ["i1"]}],
        "weaknesses": [],
        "opportunities": [],
        "threats": [],
        "evidence": [{"item_id": "i1", "quote_or_summary": "quote",
                      "source": "src", "score": 0.8}],
    }


def test_valid_swot_passes():
    assert S.validate_swot(make_valid_swot())["window_days"] == 7


def test_missing_evidence_rejected():
    sw = make_valid_swot()
    sw["evidence"] = []
    with pytest.raises(S.SwotValidationError):
        S.validate_swot(sw)


def test_claim_without_item_ids_rejected():
    sw = make_valid_swot()
    sw["strengths"] = [{"text": "unevidenced claim"}]
    with pytest.raises(S.SwotValidationError, match="unevidenced"):
        S.validate_swot(sw)


def test_claim_citing_absent_evidence_rejected():
    sw = make_valid_swot()
    sw["strengths"] = [{"text": "s", "item_ids": ["ghost"]}]
    with pytest.raises(S.SwotValidationError, match="absent"):
        S.validate_swot(sw)


def test_evidence_outside_digest_window_rejected():
    sw = make_valid_swot()
    with pytest.raises(S.SwotValidationError, match="unknown items"):
        S.validate_swot(sw, known_ids={"other"})


def test_window_days_and_generated_at_validated():
    sw = make_valid_swot()
    sw["window_days"] = 0
    with pytest.raises(S.SwotValidationError):
        S.validate_swot(sw)
    sw = make_valid_swot()
    sw["generated_at"] = "not-a-date"
    with pytest.raises(S.SwotValidationError):
        S.validate_swot(sw)


# ------------------------------------------------------------ window math

def test_window_math_drops_stale_items():
    windowed = S.window_items(fixture_items(T0), 7, now=T0)
    ids = {it["item_id"] for it in windowed}
    assert "i-stale" not in ids          # 30d old, outside the 7d window
    assert "i-strength" in ids
    assert S.window_items(fixture_items(T0), 1, now=T0)  # tighter window keeps recent
    one_day = {it["item_id"] for it in S.window_items(fixture_items(T0), 1, now=T0)}
    assert "i-strength" not in one_day   # 2d old vs 1d window
    assert "i-opportunity" in one_day


def test_normalize_digest_shapes():
    digest = {"ingested": [{"title": "t1", "url": "u", "source": "s1",
                            "topic": "tp", "sensitivity": "personal",
                            "entities": []}],
              "ingested_count": 1}
    items = S.normalize_digest(digest)
    assert len(items) == 1 and items[0]["source"] == "s1"
    assert items[0]["item_id"]           # stable fallback id assigned


# ------------------------------------------------------ heuristic generation

def test_heuristic_generates_all_quadrants_with_evidence():
    items = [it for it in fixture_items(T0) if it["item_id"] != "i-stale"]
    swot = S.heuristic_swot(ENTITY, items, window_days=7)
    S.validate_swot(swot, known_ids={it["item_id"] for it in items})
    assert swot["strengths"] and swot["weaknesses"]
    assert swot["opportunities"] and swot["threats"]
    # every claim's item_ids resolve into the evidence list
    ev_ids = {e["item_id"] for e in swot["evidence"]}
    for quad in S.QUADRANTS:
        for claim in swot[quad]:
            assert set(claim["item_ids"]) <= ev_ids
    assert "i-noise" not in ev_ids       # unrelated item never cited


def test_entity_matching_uses_keywords():
    items = fixture_items(T0)
    matched = S.items_for_entity(items, ENTITY)
    ids = {it["item_id"] for it in matched}
    assert "i-noise" not in ids
    assert "i-strength" in ids           # vllm/quantization keyword hit


def test_generate_falls_back_to_heuristic_on_model_failure():
    def bad_runner(ask_path, files, out_path):
        raise RuntimeError("model unavailable")
    swot = S.generate_swot(ENTITY, fixture_items(T0), 7, now=T0,
                           runner=bad_runner, use_model=True)
    assert swot["strengths"]             # heuristic output, not model


def test_model_path_validated_and_used():
    def good_runner(ask_path, files, out_path):
        with open(out_path, "w") as fh:
            json.dump({
                "entity": ENTITY["entity"], "generated_at": S.now_iso(T0),
                "window_days": 7,
                "strengths": [{"text": "model claim", "item_ids": ["i-strength"]}],
                "weaknesses": [], "opportunities": [], "threats": [],
                "evidence": [{"item_id": "i-strength", "quote_or_summary": "q",
                              "source": "src", "score": 0.9}]}, fh)
        return json.load(open(out_path))
    swot = S.generate_swot(ENTITY, fixture_items(T0), 7, now=T0,
                           runner=good_runner, use_model=True)
    assert swot["strengths"][0]["text"] == "model claim"


def test_model_unevidenced_claim_falls_back_to_heuristic():
    def liar_runner(ask_path, files, out_path):
        with open(out_path, "w") as fh:
            json.dump({"strengths": [{"text": "invented", "item_ids": ["made-up"]}],
                       "evidence": []}, fh)
    swot = S.generate_swot(ENTITY, fixture_items(T0), 7, now=T0,
                           runner=liar_runner, use_model=True)
    assert swot["evidence"]              # heuristic fallback, not the model's


# ------------------------------------------------------------- queue

def test_queue_append_idempotent(tmp_path):
    from dashboard.exception_queue.queue import POLICY_PATH
    q = Queue(db_path=str(tmp_path / "q.db"), policy_path=POLICY_PATH)
    try:
        swot = S.heuristic_swot(ENTITY, fixture_items(T0), 7, now=T0)
        item1, created1 = S.submit_to_queue(swot, q, now=T0)
        assert created1 and item1 is not None
        item2, created2 = S.submit_to_queue(swot, q, now=T0)
        assert not created2 and item2.item_id == item1.item_id
        assert len(q.ordered(T0)) == 1          # one proposal per entity
    finally:
        q.close()


def test_queue_finding_admissible(tmp_path):
    q = Queue(db_path=":memory:")
    try:
        swot = S.heuristic_swot(ENTITY, fixture_items(T0), 7, now=T0)
        item, created = S.submit_to_queue(swot, q, now=T0)
        assert created
        assert item.impact_class == "routine"
        assert item.condition_key == f"swot-weekly:{ENTITY['entity']}"
    finally:
        q.close()


# ------------------------------------------------------------- cadence

def test_crontab_line_shape():
    line = S.weekly_crontab_line()
    entry = S.validate_job_entry(line)
    assert entry["schedule"] == S.JOB_SCHEDULE
    assert entry["name"] == S.JOB_NAME
    assert entry["payload"].endswith("--window-days 7")


def test_validate_job_entry_rejects_bad_shapes():
    with pytest.raises(ValueError):
        S.validate_job_entry("not a cron line")
    with pytest.raises(ValueError, match="dow=1"):
        S.validate_job_entry("0 6 * * 2 aerial x -- run something")
    with pytest.raises(ValueError, match="job_run.py"):
        S.validate_job_entry("0 6 * * 1 aerial plain-cmd -- run")
    with pytest.raises(ValueError, match="empty payload"):
        S.validate_job_entry("0 6 * * 1 aerial job_run.py --job n:x -- >> log")


def test_job_entry_parses_with_followup_runner_convention():
    """The emitted line must parse under dashboard/followup/runner's
    parse_crontab (the scheduler-side convention)."""
    from dashboard.followup.runner import parse_crontab
    entries = parse_crontab(S.weekly_crontab_line(ticket="TK-1") + "\n")
    assert len(entries) == 1
    e = entries[0]
    assert e.job_id.endswith(S.JOB_NAME)
    assert e.ticket == "TK-1"
    assert "--window-days 7" in e.cmd


def test_generate_all_and_render(tmp_path):
    digest = {"ingested": [
        {"item_id": it["item_id"], "title": it["title"], "summary": it["summary"],
         "source": it["source"],
         "ts": S.now_iso(it["ts"])} for it in fixture_items(T0) if it["ts"] is None or
        it["ts"] >= T0 - 7 * DAY]}
    results = S.generate_all(os.path.join(S.HERE, "swot_entities.json"),
                             digest, 7, now=T0, use_model=False)
    assert len(results) == 4             # meta + 3 entities
    known = set(results["meta"]["item_ids"])
    for key, swot in results.items():
        if key == "meta":
            continue
        S.validate_swot(swot, known_ids=known)
        md = S.render_markdown(swot)
        assert md.startswith(f"# SWOT — {key}")
