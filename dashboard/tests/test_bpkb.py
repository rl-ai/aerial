"""Hermetic tests for the BP-KB lane: source ingestion, labeling, gap
detection, decision-point mapping. No network, no filesystem outside tmp."""
import json
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from bpkb import ingest, label, gap, poll, schema  # noqa: E402


# ---- source ingestion ------------------------------------------------------

def test_sources_valid():
    sources = poll.load_bp_sources()
    assert len(sources) >= 6
    ids = [s["id"] for s in sources]
    assert "web_doc.aws-well-architected" in ids
    assert "web_doc.google-sre-workbook" in ids
    assert "web_doc.azure-caf" in ids
    assert "web_doc.cis-benchmarks" in ids
    assert "web_doc.owasp" in ids
    assert "web_doc.nist-csf" in ids


def test_sources_match_groups_nonempty():
    for s in poll.load_bp_sources():
        assert s["match"] and all(g for g in s["match"])


def test_sources_bad_kind_rejected(tmp_path):
    bad = {"sources": [{"id": "rss.foo", "kind": "rss", "topic": "t",
                        "target": "x", "every_h": 1, "match": [["a"]]}]}
    p = tmp_path / "s.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(ValueError):
        ingest.load_sources(str(p))


def test_sources_bad_id_rejected(tmp_path):
    bad = {"sources": [{"id": "notkind.slug", "kind": "web_doc", "topic": "t",
                        "target": "x", "every_h": 1, "match": [["a"]]}]}
    p = tmp_path / "s.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(ValueError):
        ingest.load_sources(str(p))


# ---- labeling --------------------------------------------------------------

BP_IDS = {"web_doc.aws-well-architected"}

def test_label_public_for_bp_source():
    rec = {"id": "bp.x.1", "source": "web_doc.aws-well-architected",
           "pillar": "security", "statement": "s", "status": "compliant",
           "evidence": "e", "decision_point": "D1"}
    out = label.ingest_practice(rec, "web_doc.aws-well-architected", BP_IDS)
    assert out["sensitivity"] == "public"


def test_label_fails_closed_for_unknown_source():
    rec = {"id": "bp.x.1", "source": "other", "pillar": "security",
           "statement": "s", "status": "compliant", "evidence": "e",
           "decision_point": "D1"}
    with pytest.raises(ValueError):
        label.ingest_practice(rec, "some.random.source", BP_IDS)


# ---- schema ----------------------------------------------------------------

def test_schema_rejects_bad_status():
    rec = {"id": "bp.x.1", "source": "s", "pillar": "security",
           "statement": "s", "status": "maybe", "evidence": "e",
           "decision_point": "D1"}
    with pytest.raises(schema.SchemaError):
        schema.validate_practice(rec)


def test_schema_rejects_bad_decision_point():
    rec = {"id": "bp.x.1", "source": "s", "pillar": "security",
           "statement": "s", "status": "gap", "evidence": "e",
           "decision_point": "D13"}
    with pytest.raises(schema.SchemaError):
        schema.validate_practice(rec)


def test_schema_rejects_duplicate_ids():
    rec = {"id": "bp.x.1", "source": "s", "pillar": "security",
           "statement": "s", "status": "gap", "evidence": "e",
           "decision_point": "D1"}
    with pytest.raises(schema.SchemaError):
        schema.validate_kb([rec, rec])


def test_seeds_all_valid():
    recs = schema.validate_kb(poll.load_seed_practices())
    assert len(recs) == 10


# ---- gap detection ---------------------------------------------------------

ALL_COMPLIANT = {
    "followup_classifier_present": True, "failure_flow_state_machine": True,
    "vram_demand_load": True, "handstop_forbidden": True, "audit_log": "x",
    "separate_accounts": True, "no_credential_handling": True,
    "injection_defense": "x", "daily_report": "x", "token_budgets": True,
}
EMPTY = {}


def test_gap_detected_when_state_missing():
    result = gap.run_gap_analysis(poll.load_seed_practices(), EMPTY)
    assert result["counts"]["gap"] >= 3   # postmortem gap + failure checks collapse
    assert result["findings"], "gaps must produce findings"


def test_findings_classed_r1_to_r6():
    result = gap.run_gap_analysis(poll.load_seed_practices(), EMPTY)
    for f in result["findings"]:
        assert f["class"] in {"R1", "R2", "R3", "R4", "R5", "R6"}


def test_security_gap_is_r1():
    p = {"id": "bp.t.sec", "source": "s", "pillar": "security",
         "statement": "s", "status": "gap", "evidence": "e",
         "decision_point": "D1"}
    f = gap.classify_finding(p)
    assert f["class"] == "R1"


def test_compliant_state_has_no_reliability_gap():
    result = gap.run_gap_analysis(poll.load_seed_practices(), ALL_COMPLIANT)
    rel_gaps = [x for x in result["practices"]
                if x["pillar"] == "reliability" and x["status"] == "gap"]
    # only the single-point-of-failure practice stays a gap
    assert len(rel_gaps) == 1


# ---- decision-point mapping ------------------------------------------------

def test_decision_point_mapping_covers_d1_to_d12():
    dpoints = poll.load_decision_points()
    result = gap.run_gap_analysis(poll.load_seed_practices(), ALL_COMPLIANT)
    mapping = gap.map_decision_points(result["practices"], dpoints)
    assert set(mapping) == {f"D{i}" for i in range(1, 13)}
    assert mapping["D1"]["practices"] >= 1
    assert all("name" in v for v in mapping.values())


def test_pending_decision_points_marked():
    dpoints = poll.load_decision_points()
    assert dpoints["D4"].get("status") == "pending"
    assert dpoints["D1"].get("status") != "pending"


# ---- poll loop / signal line -----------------------------------------------

def test_poll_signal_line(tmp_path):
    state_path = tmp_path / "kbstate.json"
    res = poll.sample_bpkb(str(state_path), state=dict(ALL_COMPLIANT))
    assert res["signal_line"].startswith("Best practices signal:")
    assert "compliant" in res["signal_line"]
    saved = json.loads(state_path.read_text())
    assert len(saved["practice_ids"]) == 10


def test_poll_new_practice_detection(tmp_path):
    state_path = tmp_path / "kbstate.json"
    first = poll.sample_bpkb(str(state_path), state=dict(ALL_COMPLIANT))
    assert len(first["new_practices"]) == 10
    second = poll.sample_bpkb(str(state_path), state=dict(ALL_COMPLIANT))
    assert second["new_practices"] == []


def test_poll_state_file_survives_bad_prev(tmp_path):
    state_path = tmp_path / "kbstate.json"
    state_path.write_text("not json{")
    res = poll.sample_bpkb(str(state_path), state=dict(ALL_COMPLIANT))
    assert len(res["new_practices"]) == 10


def test_poll_hermetic_fresh_root(tmp_path):
    state_path = tmp_path / "kbstate.json"
    res = poll.sample_bpkb(str(state_path), state=poll.gather_state(root=str(tmp_path)))
    assert res["counts"]["gap"] >= 1
