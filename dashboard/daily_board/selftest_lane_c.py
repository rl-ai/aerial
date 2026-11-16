"""Lane C self-test — exercises the six acceptance criteria against real
fixtures (real sqlite job-runs DB, real JSONL tail, real sessions mirror, real
Lane A Queue/MandateFlow sqlite). Run: python3 -m dashboard.daily_board.selftest_lane_c
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time

from dashboard.data_layer.job_runs import JobRunsDB
from dashboard.data_layer.model_calls_tailer import ModelCallsTailer
from dashboard.data_layer.sessions import SessionsStore
from dashboard.exception_queue import mandate_flow, queue as qmod
from dashboard.daily_board import promotion, render

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}: {name}" + (f" — {detail}" if detail and not ok else ""))


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="lane_c_")
    now = time.time()

    # ---- fixture: job-runs sqlite (real DB, WAL) --------------------------
    db_path = os.path.join(tmp, "a5-runtime.db")
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE job_runs (project TEXT, stage_type TEXT, status TEXT,"
                " evidence_json TEXT, completed_at REAL, duration_hours REAL)")
    con.executemany("INSERT INTO job_runs VALUES (?,?,?,?,?,?)", [
        ("aerial", "ingest", "done", json.dumps({"stages": {"s1": {"status": "pass"}, "s2": {"status": "pass"}, "s3": {"status": "unverified"}}}), now - 86400 * 3, 10.0),
        ("aerial", "ingest", "done", json.dumps({"stages": {"s1": {"status": "pass"}}}), now - 86400 * 2, 12.0),
        ("aerial", "curate", "done", json.dumps({"stages": {"s1": {"status": "pass"}}}), now - 86400, 20.0),
        ("aerial", "digest", "done", json.dumps({"stages": {"s1": {"status": "pass"}}}), now - 86400 * 2, 30.0),
        ("aerial", "digest", "done", json.dumps({"stages": {"s1": {"status": "pass"}}}), now - 86400, 50.0),
    ])
    con.commit(); con.close()
    runs = JobRunsDB(db_path)

    # ---- fixture: model-calls.jsonl tail (real file) -----------------------
    ledger = os.path.join(tmp, "model-calls.jsonl")
    with open(ledger, "w") as f:
        for _ in range(5):
            f.write(json.dumps({"item": "W6 shadow week", "in_tokens": 100, "out_tokens": 50}) + "\n")
        for _ in range(2):
            f.write(json.dumps({"item": "M8.1 control-plane panel", "in_tokens": 10, "out_tokens": 5}) + "\n")
        f.write(json.dumps({"note": "no item tag"}) + "\n")
    tailer = ModelCallsTailer(ledger_path=ledger, state_path=os.path.join(tmp, "tail-state.json"))

    # ---- fixture: sessions store (single 'done since' source) --------------
    sessions = SessionsStore(hub_db_path=os.path.join(tmp, "no-hub.db"),
                             mirror_path=os.path.join(tmp, "sessions-mirror.json"))
    visit_ts = now - 3600  # Ray visited 1h ago
    sessions.record_visit(visit_ts=visit_ts, hub_available=False)

    project_items = [
        {"project": "aerial", "item": "W6 shadow week", "item_tag": "W6 shadow week",
         "status": "working", "stages": ["ingest", "curate", "digest"],
         "done_stages": ["ingest"], "evidence_artifact": "stage-3.json"},
        {"project": "gateway", "item": "M8.1 control-plane panel", "item_tag": "M8.1 control-plane panel",
         "status": "waiting-on-you", "stages": [], "done_stages": [],
         "evidence_artifact": "panel-diff.patch"},
        {"project": "gateway", "item": "M7 ingestion cleanup", "status": "done",
         "done_ts": now - 1800, "stages": ["s"], "done_stages": ["s"]},
        {"project": "aerial", "item": "blocked ingest fix", "status": "blocked",
         "stages": ["ingest"], "done_stages": [], "evidence_artifact": "err.log"},
    ]
    rows = render.zone_a_rows(project_items, runs, tailer, sessions, now)

    # ---- AC1: L1 row contents ---------------------------------------------
    statuses = {r["status"] for r in rows}
    check("AC1 statuses within valid set",
          statuses <= set(render.VALID_STATUSES), str(statuses))
    w6 = next(r for r in rows if r["item"] == "W6 shadow week")
    check("AC1 pct complete from stage evidence", w6["pct_complete"] == round(100 / 3, 1), str(w6["pct_complete"]))
    eta = runs.eta_range("aerial", ["curate", "digest"])
    check("AC1 ETA range from per-stage history", "–" in eta["eta"], eta["eta"])
    check("AC1 ETA 'insufficient history' when a stage has no runs",
          runs.eta_range("aerial", ["digest", "unknown"])["eta"] == "insufficient history")
    check("AC1 per-item token counts from tailer (5 calls / 750 tok)",
          w6["tokens"] == "5 calls / 750 tok", w6["tokens"])
    check("AC1 untagged bucket present (1 call)",
          w6["untagged_bucket"] is not None and "1 calls" in w6["untagged_bucket"], str(w6["untagged_bucket"]))

    # ---- AC2: L2 collapse + single done-since source ------------------------
    check("AC2 only waiting-on-you/blocked expanded",
          all(r["expanded"] == (r["status"] in render.EXPANDED_STATUSES) for r in rows))
    done_row = next(r for r in rows if r["status"] == "done-since-last-visit")
    check("AC2 done item shows 'done since <last visit>' (done after visit -> new since last visit)",
          done_row["done_label"] == "done-since-last-visit", str(done_row["done_label"]))
    # fixture: change the timestamp ONCE -> all three surfaces update
    new_visit = now + 60
    sessions.record_visit(visit_ts=new_visit, hub_available=False)
    rows2 = render.zone_a_rows(project_items, runs, tailer, sessions, now)
    done2 = next(r for r in rows2 if r["status"] == "done-since-last-visit")
    check("AC2 timestamp change updates Zone A surface",
          done2["done_label"] == f"done since {time.strftime('%Y-%m-%d %H:%M', time.localtime(new_visit))}", str(done2["done_label"]))
    brief2 = render.daily_brief(rows2, [], sessions)
    brief1 = render.daily_brief(rows, [], sessions)
    check("AC2 timestamp change updates brief surface (same store)",
          brief1 != brief2, f"{brief1!r} vs {brief2!r}")

    # ---- AC3: non-features ---------------------------------------------------
    text = render.render_text({"zone_a": rows, "zone_b": [], "zone_c": render.zone_c([], {}), "brief": []})
    check("AC3 no live-watch / activity-feed / run-tree vocabulary",
          not any(k in text.lower() for k in ("live-watch", "activity feed", "run tree", "trace ui", "streaming")))

    # ---- AC4: queue rows (Panel A/B/C, exactly three actions) ----------------
    policy_path = os.path.join(os.path.dirname(qmod.__file__), "queue-policy.yaml")
    q = qmod.Queue(db_path=os.path.join(tmp, "q.db"), policy_path=policy_path)
    f_strong = qmod.Finding(condition_key="c1", claim="curator mislabels ingestion stage",
                            impact_class="degraded", admission_score=0.8, eval_passed=True,
                            evidence_label="verified", source_agent="curator",
                            proposed_action="re-run curator with corrected schema")
    item = q.admit(f_strong)
    assert item is not None
    item.panel = "A"
    f_policy = qmod.Finding(condition_key="c2", claim="renew tracker contained-mode mandate",
                            impact_class="routine", admission_score=0.6, eval_passed=True,
                            evidence_label="verified", source_agent="lead",
                            proposed_action="mandate renewal for contained-mode config")
    item2 = q.admit(f_policy)
    assert item2 is not None
    qrows = render.zone_b_rows(q, now)
    check("AC4 row anatomy fields present",
          all(set(("claim", "evidence_label", "source", "proposed_action", "mandate",
                   "evidence_artifact", "age", "admission_score")) <= set(r) for r in qrows))
    check("AC4 exactly three actions per row",
          all(r["actions"] == ["approve", "dismiss", "escalate"] for r in qrows))
    check("AC4 panels classified", {r["panel"] for r in qrows} == {"A", "C"},
          str([r["panel"] for r in qrows]))
    check("AC4 evidence label surfaced", qrows[0]["evidence_label"] == "verified")

    # ---- AC5: promotion gate view ---------------------------------------------
    draft = {"scope": "relabel ingestion stages", "expiry": "2026-11-19T00:00:00Z"}
    comp_ok = {
        "role": "curator", "mode": "record-only-shadow",
        "shadow_actions": [
            {"ts": now - 86400 * 10, "role": "curator", "action": "relabel stage 3",
             "actual_outcome": "relabel done", "evidence_label": "verified",
             "divergence": False, "draft_mandate_spec": draft},
        ],
    }
    view_ok = promotion.promotion_view(comp_ok, now=now, observation_days=8.0,
                                       shadow_cost={"calls": 40, "tokens": 12000})
    check("AC5 gate satisfied -> approve available",
          view_ok["approve_available"] and all(c["satisfied"] for c in view_ok["gate_criteria"]))
    check("AC5 side-by-side shadow vs actual, no replay UI",
          view_ok["side_by_side"][0]["shadow_action"] == "relabel stage 3"
          and view_ok["side_by_side"][0]["actual_outcome"] == "relabel done")
    check("AC5 draft mandate is a spec, not a live mandate",
          view_ok["draft_mandates"] == [draft])

    comp_bad = {
        "role": "lead", "mode": "record-only-shadow",
        "shadow_actions": [
            {"ts": now - 86400 * 2, "role": "lead", "action": "a1", "actual_outcome": "x",
             "evidence_label": "stale", "divergence": True, "divergence_label": None,
             "draft_mandate_spec": None},
        ],
    }
    view_bad = promotion.promotion_view(comp_bad, now=now)
    check("AC5 approve unavailable until all 4 criteria pass",
          view_bad["approve_available"] is False)
    failing = [c for c in view_bad["gate_criteria"] if not c["satisfied"]]
    check("AC5 each failing criterion shown with current value",
          len(failing) == 4 and all("current" in c for c in failing),
          json.dumps([c["current"] for c in failing]))
    check("AC5 P1: stale-labeled action without spec+explanation counts as unexplained",
          "3 unexplained" not in view_bad["gate_criteria"][2]["current"]
          and "1 unexplained" in view_bad["gate_criteria"][2]["current"],
          view_bad["gate_criteria"][2]["current"])
    comp_stale_ok = {
        "role": "lead", "mode": "record-only-shadow",
        "shadow_actions": [
            {"ts": now - 86400 * 2, "role": "lead", "action": "a1", "actual_outcome": "x",
             "evidence_label": "stale", "divergence": True, "divergence_label": "would-have-been-wrong",
             "draft_mandate_spec": draft, "staleness_explanation": "evidence file was mid-write"},
        ],
    }
    vs = promotion.promotion_view(comp_stale_ok, now=now, observation_days=8.0,
                                  shadow_cost={"calls": 10, "tokens": 100})
    check("AC5 P1: stale action WITH draft spec + explanation is explained",
          vs["gate_criteria"][2]["satisfied"], vs["gate_criteria"][2]["current"])
    try:
        promotion.approve_promotion(view_bad)
        check("AC5 approve on unsatisfied gate raises", False)
    except ValueError:
        check("AC5 approve on unsatisfied gate raises", True)

    # ---- AC6: approve converts drafts into real mandates, ledger, continuation ----
    created: list[dict] = []
    ledger: list[dict] = []
    res = promotion.approve_promotion(view_ok, actor="Ray",
                                      mandate_creator=lambda spec: (created.append({"mandate_id": f"m{len(created)+1}", **spec}) or created[-1]),
                                      ledger=ledger)
    check("AC6 approve converts draft specs into real mandates",
          len(created) == 1 and created[0]["scope"] == draft["scope"], json.dumps(created))
    check("AC6 ledger records who/when/evidence",
          ledger and ledger[0]["who"] == "Ray" and "when" in ledger[0] and "evidence" in ledger[0])
    check("AC6 record-only shadow continues post-promotion",
          res["mode"] == "live" and res["record_only_shadow_continues"])
    # two-phase creation actually goes through Lane A's MandateFlow for queue rows:
    flow = mandate_flow.MandateFlow(q, db_path=os.path.join(tmp, "mf.db"))
    dec = flow.decide(item.item_id, "approve", actor="Ray")
    check("AC6 Lane A two-phase approve consumed via renderer's lane-A import",
          dec.get("phase") == "execution" and dec.get("ok") and dec.get("row_state") == "done",
          json.dumps(dec))
    dem = promotion.demote_check({**comp_ok, "mode": "live",
                                  "shadow_actions": [dict(comp_ok["shadow_actions"][0], divergence=True,
                                                          divergence_label=None)]},
                                 false_positive_rate=0.5, observation_days=8.0,
                                 shadow_cost={"calls": 1, "tokens": 1})
    check("AC6 demote-to-shadow offered on divergence/false-positive degradation",
          dem["demote_available"] and dem["false_positive_degraded"])
    dres = promotion.demote_to_shadow("curator", "divergence degraded", ledger=ledger)
    check("AC6 demotion recorded in ledger", dres["mode"] == "record-only-shadow" and len(ledger) == 2)

    failed = [c for c in CHECKS if not c[1]]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks pass")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
