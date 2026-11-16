"""Conformance verifier tests — hermetic (fixtures, mocked hub/clock;
docs/pipeline-checkpoints.md PC-1..PC-7 + runner poll-loop wiring)."""
import sys
import os
import sqlite3

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import conformance as cf                                 # noqa: E402
from dashboard.followup import runner as rn                             # noqa: E402
from dashboard.followup.classifier import (                             # noqa: E402
    classify, SEV_SECURITY, UNKNOWN)

T0 = 1763217600.0   # fixed clock


class Clock:
    def __call__(self):
        return T0


# ----------------------------------------------------------------- PC-1

def test_pc1_valid_or_defaulted_labels_pass():
    r = cf.check_pc1([
        {"id": "a", "sensitivity": "personal"},
        {"id": "b", "sensitivity": "sensitive"},
        {"id": "c", "sensitivity": None},          # missing → defaulted
        {"id": "d", "sensitivity": "  "},          # blank → defaulted
    ])
    assert r["ok"] is True and r["bypass"] is False and r["checked"] == 4


def test_pc1_non_lattice_label_is_a_bypass():
    r = cf.check_pc1([{"id": "x", "sensitivity": "topsecret"}])
    assert r["ok"] is False and r["bypass"] is True
    assert "topsecret" in r["detail"]


def test_pc1_empty_sample_not_checkable():
    assert cf.check_pc1([])["available"] is False


# ----------------------------------------------------------------- PC-2

def test_pc2_reconciles_when_seen_count_present():
    r = cf.check_pc2([{"id": "r1", "seen": 10, "ingested": 7, "skipped": 2,
                       "failed": 1}])
    assert r["ok"] is True and r["checked"] == 1


def test_pc2_silent_drop_fails():
    r = cf.check_pc2([{"id": "r1", "seen": 10, "ingested": 7, "skipped": 1,
                       "failed": 1}])
    assert r["ok"] is False and "9 != seen=10" in r["detail"]


def test_pc2_no_seen_counter_is_not_checkable_not_a_pass():
    r = cf.check_pc2([{"id": "r1", "ingested": 7}])
    assert r["ok"] is None and r["available"] is False and "G-3" in r["detail"]


# ----------------------------------------------------------------- PC-3

def test_pc3_suppressed_or_over_ceiling_fails():
    ok = cf.check_pc3([{"key": "k1", "suppressed": False, "admissions_today": 3}])
    assert ok["ok"] is True
    bad = cf.check_pc3([{"key": "k2", "suppressed": True, "admissions_today": 1},
                        {"key": "k3", "suppressed": False,
                         "admissions_today": cf.BURST_ABS_PER_DAY + 1}])
    assert bad["ok"] is False and len(bad["violations"]) == 2


# ----------------------------------------------------------------- PC-4

def test_pc4_three_evidence_items_with_links_pass():
    ev = [{"text": f"quote {i}", "link": f"http://x/{i}"} for i in range(3)]
    assert cf.check_pc4([{"key": "k", "evidence": ev}])["ok"] is True


def test_pc4_bare_claim_fails():
    r = cf.check_pc4([{"key": "k", "evidence": [{"text": "only one", "link": "l"}]}])
    assert r["ok"] is False and "<3" in r["detail"]


# ----------------------------------------------------------------- PC-5

def _fu_db(tmp_path, rows, transitions):
    path = str(tmp_path / "f.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE followups (finding_id TEXT PRIMARY KEY,"
                " fix_class TEXT, state TEXT, created_at REAL, updated_at REAL)")
    con.execute("CREATE TABLE transitions (finding_id TEXT, from_state TEXT,"
                " to_state TEXT, action TEXT, at REAL, detail TEXT)")
    con.executemany("INSERT INTO followups VALUES (?,?,?,?,?)", rows)
    con.executemany("INSERT INTO transitions VALUES (?,?,?,?,?,?)", transitions)
    con.commit()
    con.close()
    return path


def test_pc5_clean_trail_passes(tmp_path):
    db = _fu_db(tmp_path,
                [("f1", "mandate", "closed", T0, T0)],
                [("f1", "", "classified", "submit", T0, ""),
                 ("f1", "classified", "fix-attempted", "attempt", T0, ""),
                 ("f1", "fix-attempted", "verified", "verify", T0, ""),
                 ("f1", "verified", "closed", "close", T0, "")])
    fu, tr = cf.sample_followups(db)
    assert cf.check_pc5(fu, tr)["ok"] is True


def test_pc5_unknown_reaching_fix_attempted_fails(tmp_path):
    db = _fu_db(tmp_path,
                [("f1", "unknown", "escalated", T0, T0)],
                [("f1", "classified", "fix-attempted", "attempt", T0, "")])
    fu, tr = cf.sample_followups(db)
    r = cf.check_pc5(fu, tr)
    assert r["ok"] is False and "unknown class reached fix-attempted" in r["detail"]


def test_pc5_closed_without_verify_fails(tmp_path):
    db = _fu_db(tmp_path,
                [("f1", "mandate", "closed", T0, T0)],
                [("f1", "classified", "closed", "close", T0, "")])
    fu, tr = cf.sample_followups(db)
    r = cf.check_pc5(fu, tr)
    assert r["ok"] is False and "no verify transition" in r["detail"]


# ----------------------------------------------------------------- PC-6

def test_pc6_cited_ids_verified_and_caps_hold():
    html = ("<ul class=brief><li>a</li><li>b</li><li>c</li></ul>"
            "<p>see TK-123 and EM-9</p>")
    r = cf.check_pc6(html, lambda tid: tid in ("TK-123", "EM-9"))
    assert r["ok"] is True and r["checked"] >= 2


def test_pc6_unverified_ticket_id_fails():
    html = "<p>shipped TK-404</p>"
    r = cf.check_pc6(html, lambda tid: False)
    assert r["ok"] is False and "TK-404" in r["detail"]


def test_pc6_numbers_inside_unavailable_block_fail():
    html = "<p class=unavailable>hub unavailable, 42 runs</p>"
    r = cf.check_pc6(html, lambda tid: True)
    assert r["ok"] is False and "unavailable block contains numbers" in r["detail"]


def test_pc6_over_cap_headlines_fail():
    html = "<ul class=brief>" + "".join(f"<li>h{i}</li>" for i in range(5)) + "</ul>"
    r = cf.check_pc6(html, lambda tid: True)
    assert r["ok"] is False and "cap 3" in r["detail"]


# ----------------------------------------------------------------- PC-7

def _gt(days_ago=0.0, layers=None):
    return {"id": "TK-GAUNTLET", "created_at": T0 - days_ago * 86400,
            "layers": layers if layers is not None else {n: "PASS" for n in range(1, 6)}}


def test_pc7_recent_5_of_5_pass_record_passes():
    assert cf.check_pc7([_gt()], now=T0)["ok"] is True


def test_pc7_missing_ticket_not_checkable():
    assert cf.check_pc7([], now=T0)["available"] is False


def test_pc7_stale_run_fails():
    r = cf.check_pc7([_gt(days_ago=30)], now=T0)
    assert r["ok"] is False and "stale" in r["detail"]


def test_pc7_missing_layer_record_is_a_bypass():
    r = cf.check_pc7([_gt(layers={1: "PASS", 2: "PASS"})], now=T0)
    assert r["ok"] is False and r["bypass"] is True


def test_pc7_failed_layer_fails():
    layers = {n: "PASS" for n in range(1, 6)}
    layers[3] = "FAIL"
    r = cf.check_pc7([_gt(layers=layers)], now=T0)
    assert r["ok"] is False and "layer 3" in r["detail"]


# ----------------------------------------------------------------- classification

def test_bypass_finding_classifies_r1_security():
    res = {"gate": "PC-7", "ok": False, "bypass": True, "available": True,
           "detail": "trail shows 2/5 layer records"}
    f = cf.finding_for(res, Clock())
    assert f is not None
    assert classify(f) == SEV_SECURITY, "a gate bypass is R1 (immediate ping)"


def test_gate_failure_finding_classifies_r6_unknown():
    res = {"gate": "PC-4", "ok": False, "bypass": False, "available": True,
           "detail": "proposal k: 1 evidence items (<3)"}
    f = cf.finding_for(res, Clock())
    assert f is not None
    assert classify(f) == UNKNOWN, "a merely failing gate → R6 daily aggregate"


def test_passing_or_not_checkable_gates_produce_no_finding():
    assert cf.finding_for({"gate": "PC-1", "ok": True, "available": True},
                          Clock()) is None
    assert cf.finding_for({"gate": "PC-6", "ok": None, "available": False},
                          Clock()) is None


# ----------------------------------------------------------------- run_all

def test_run_all_hermetic_with_injected_samples():
    results = cf.run_all(
        now=T0,        samples={
            "ingests": [{"id": "a", "sensitivity": "personal"}],
            "reports": [{"id": "r1", "seen": 1, "ingested": 1, "skipped": 0,
                         "failed": 0}],
            "proposals": [{"key": "k", "evidence": [
                {"text": "t", "link": "l"} for _ in range(3)]}],
            "followups": ([{"finding_id": "f1", "fix_class": "mandate",
                            "state": "escalated"}],
                          [{"finding_id": "f1", "from_state": "classified",
                            "to_state": "classified", "action": "submit",
                            "at": T0, "detail": ""}]),
            "summary_html": "<ul class=brief><li>x</li></ul>",
            "gauntlet_tickets": [_gt()],
        },
        ticket_exists=lambda tid: True)
    assert [r["gate"] for r in results] == [f"PC-{i}" for i in range(1, 8)]
    assert all(r["ok"] is True for r in results)


def test_run_all_missing_sources_fail_closed_not_fake_pass(tmp_path):
    results = cf.run_all(
        now=T0,
        digest_path=str(tmp_path / "nope.json"),
        proposals_ledger=str(tmp_path / "nope.json"),
        followups_db=str(tmp_path / "nope.db"),
        summary_path=str(tmp_path / "nope.html"),
        ticket_exists=None, gauntlet_tickets=None)
    for r in results:
        assert r["available"] is False and r["ok"] is None


# ----------------------------------------------------------------- sampler

def test_sample_ingests_reads_digest(tmp_path):
    d = tmp_path / "digest.json"
    d.write_text('{"ingested": [{"guid": "g1", "sensitivity": "personal"},'
                 ' {"guid": "g2", "sensitivity": "intimate"}]}')
    out = cf.sample_ingests(str(d))
    assert [o["id"] for o in out] == ["g1", "g2"]


def test_sample_followups_missing_db_returns_none(tmp_path):
    assert cf.sample_followups(str(tmp_path / "x.db")) is None


# ----------------------------------------------------------------- runner wiring

def _mk_runner(tmp_path, conformance_fn):
    return rn.OpsRunner(
        followups_db=str(tmp_path / "f.db"),
        clock=Clock(),
        probe=lambda port, bearer=None: 200,
        hub_db=str(tmp_path / "hub.db"),
        health_path=str(tmp_path / "no-health.json"),
        crontab_fn=lambda: "",
        state_path=None,
        conformance_fn=conformance_fn)


def test_poll_once_runs_conformance_pass(tmp_path):
    results = [{"gate": "PC-4", "ok": False, "bypass": False, "available": True,
                "detail": "proposal k: 1 evidence items (<3)"}]
    r = _mk_runner(tmp_path, lambda: results)
    summary = r.poll_once()
    assert summary["findings"] >= 1
    assert any(f.get("finding_id", "").startswith("conformance:PC-4")
               for f in summary["results"])


def test_poll_once_conformance_crash_never_kills_poll(tmp_path):
    def boom():
        raise RuntimeError("probe exploded")
    r = _mk_runner(tmp_path, boom)
    s = r.poll_once()
    assert "findings" in s            # poll completed anyway


def test_default_runner_uses_conformance_run_all(tmp_path):
    r = _mk_runner(tmp_path, None)
    assert r.conformance_fn is cf.run_all
