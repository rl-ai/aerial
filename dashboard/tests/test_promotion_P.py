"""§7 Promotion-gate criteria P1–P3 (Lane C promotion view)."""
import pytest

from conftest import T0
from dashboard.daily_board import promotion


def _draft():
    return {"scope": "relabel ingestion stages", "expiry": "2026-11-19T00:00:00Z"}


def _comparison_satisfied(now, draft):
    return {
        "role": "curator", "mode": "record-only-shadow",
        "shadow_actions": [
            {"ts": now - 86400 * 10, "role": "curator", "action": "relabel stage 3",
             "actual_outcome": "relabel done", "evidence_label": "verified",
             "divergence": False, "draft_mandate_spec": draft},
        ],
    }


def test_P1_gate_unavailable_until_all_criteria_pass(now):
    draft = _draft()
    view_ok = promotion.promotion_view(_comparison_satisfied(now, draft), now=now,
                                       observation_days=8.0,
                                       shadow_cost={"calls": 40, "tokens": 12000})
    # approve-promotion available only when all four gate criteria
    # (window, divergence rate, evidence coverage, cost projection) pass
    assert view_ok["approve_available"]
    assert all(c["satisfied"] for c in view_ok["gate_criteria"])

    # failing fixture: every criterion fails and is displayed with its
    # current value; approve stays unavailable
    comp_bad = {
        "role": "lead", "mode": "record-only-shadow",
        "shadow_actions": [
            {"ts": now - 86400 * 2, "role": "lead", "action": "a1",
             "actual_outcome": "x", "evidence_label": "stale", "divergence": True,
             "divergence_label": None, "draft_mandate_spec": None},
        ],
    }
    view_bad = promotion.promotion_view(comp_bad, now=now)
    assert view_bad["approve_available"] is False
    failing = [c for c in view_bad["gate_criteria"] if not c["satisfied"]]
    assert len(failing) == 4 and all("current" in c for c in failing)

    # criterion 3 asserts every shadow action carries a DRAFT mandate spec
    # (scope+expiry), not a live mandate
    assert view_ok["draft_mandates"] == [draft]
    # P1 stale rule: a stale-labelled action needs spec + staleness explanation
    assert "1 unexplained" in view_bad["gate_criteria"][2]["current"]
    comp_stale_ok = {
        "role": "lead", "mode": "record-only-shadow",
        "shadow_actions": [
            {"ts": now - 86400 * 2, "role": "lead", "action": "a1",
             "actual_outcome": "x", "evidence_label": "stale", "divergence": True,
             "divergence_label": "would-have-been-wrong",
             "draft_mandate_spec": draft,
             "staleness_explanation": "evidence file was mid-write"},
        ],
    }
    vs = promotion.promotion_view(comp_stale_ok, now=now, observation_days=8.0,
                                  shadow_cost={"calls": 10, "tokens": 100})
    assert vs["gate_criteria"][2]["satisfied"]

    # approving with an unsatisfied gate is refused
    with pytest.raises(ValueError):
        promotion.approve_promotion(view_bad)


def test_P2_approve_converts_drafts_to_real_mandates_ledger_and_demote(now):
    draft = _draft()
    view = promotion.promotion_view(_comparison_satisfied(now, draft), now=now,
                                    observation_days=8.0,
                                    shadow_cost={"calls": 40, "tokens": 12000})
    created, ledger = [], []
    res = promotion.approve_promotion(
        view, actor="Ray",
        mandate_creator=lambda spec: (created.append(
            {"mandate_id": f"m{len(created)+1}", **spec}) or created[-1]),
        ledger=ledger)
    # approving promotion converts the draft mandate specs into the role's
    # first real mandate(s)
    assert len(created) == 1 and created[0]["scope"] == draft["scope"]
    # the ledger records the evidence link (who/when/evidence)
    assert ledger and ledger[0]["who"] == "Ray" and "when" in ledger[0] \
        and "evidence" in ledger[0]
    # post-promotion, record-only shadow continues
    assert res["mode"] == "live" and res["record_only_shadow_continues"]
    # demote-to-shadow offered when divergence or false-positive metrics degrade
    dem = promotion.demote_check(
        {**_comparison_satisfied(now, draft), "mode": "live",
         "shadow_actions": [dict(_comparison_satisfied(now, draft)["shadow_actions"][0],
                                 divergence=True, divergence_label=None)]},
        false_positive_rate=0.5, observation_days=8.0,
        shadow_cost={"calls": 1, "tokens": 1})
    assert dem["demote_available"] and dem["false_positive_degraded"]
    dres = promotion.demote_to_shadow("curator", "divergence degraded", ledger=ledger)
    assert dres["mode"] == "record-only-shadow" and len(ledger) == 2


def test_P3_side_by_side_no_replay_ui(now):
    draft = _draft()
    view = promotion.promotion_view(_comparison_satisfied(now, draft), now=now,
                                    observation_days=8.0,
                                    shadow_cost={"calls": 40, "tokens": 12000})
    # shadow vs actual rendered side-by-side, aligned by ts
    row = view["side_by_side"][0]
    assert row["shadow_action"] == "relabel stage 3"
    assert row["actual_outcome"] == "relabel done"
    assert "ts" in row
    # no replay UI anywhere in the view
    text = str(view).lower()
    assert "replay" not in text and "timeline" not in text
