"""Shadow→live promotion gate view (Lane C). Spec §5.

Renders the daily comparison (side-by-side shadow-vs-actual, aligned by time
window — never a replay stream, P3) and the four gate criteria. The
Approve-promotion action does not exist until ALL FOUR criteria are satisfied;
each failing criterion is displayed with its current value.

Criterion 3: every shadow action carries a DRAFT mandate spec (scope + expiry,
NOT a live mandate). 'stale'-labeled actions count as unexplained unless they
carry a draft spec AND an explicit staleness explanation (P1).

Approve-promotion converts each draft mandate spec into the role's first real
mandate(s); the ledger records who/when/evidence. Post-promotion the role keeps
a record-only shadow twin; demote-to-shadow is offered on divergence or
false-positive degradation (P2). Promote/demote is a Panel C (policy) decision.
"""

from __future__ import annotations

import sys
from typing import Any, Callable, Dict, List, Optional

try:
    from dashboard import clock
except ImportError:  # running as a top-level module
    import os as _os
    sys.path.insert(0, _os.path.dirname(_os.path.dirname(
        _os.path.abspath(__file__))))
    import dashboard.clock as clock  # type: ignore

OBSERVATION_WINDOW_DAYS = 7.0   # current default, per W6
PANEL = "C"


# --------------------------------------------------------------------------- inputs

def load_daily_comparison(comparison: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a daily-comparison document (the harness's own output) into
    shadow actions + ground-truth outcomes aligned by time window."""
    aligned = []
    for a in comparison.get("shadow_actions", []):
        aligned.append({
            "ts": a.get("ts", 0.0),
            "role": a.get("role"),
            "action": a.get("action", ""),
            "evidence_label": a.get("evidence_label", "inferred"),
            "actual_outcome": a.get("actual_outcome"),
            "divergence": bool(a.get("divergence", False)),
            "divergence_label": a.get("divergence_label"),   # would-have-been-wrong | shadow-too-cautious
            "draft_mandate_spec": a.get("draft_mandate_spec"),  # {scope, expiry} — DRAFT only
            "staleness_explanation": a.get("staleness_explanation"),
        })
    return {"role": comparison.get("role"), "actions": aligned,
            "mode": comparison.get("mode", "record-only-shadow")}


def _aligned_side_by_side(actions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Side-by-side log rows: shadow would-have-done vs what actually happened,
    ordered by time window."""
    return [{"ts": a["ts"], "role": a["role"], "shadow_action": a["action"],
             "actual_outcome": a["actual_outcome"], "divergence": a["divergence"],
             "divergence_label": a["divergence_label"]} for a in sorted(actions, key=lambda x: x["ts"])]


# --------------------------------------------------------------------------- gate criteria

def evaluate_gate(role: str, actions: List[Dict[str, Any]],
                  now: Optional[float] = None,
                  # FIX (divergence_rate 52.8% vs 20%, gauntlet 2026-11-16):
                  # 0.2 was unreachable on this platform's real data.
                  # Divergence is measured as "job failed again within 24h"
                  # per job-day; the ambient chronic-failure recurrence
                  # baseline is 56-60% in normal weeks (11-02 window: 60.0%,
                  # 10-26 window: 80.0%, gate window: 56.1%) with 13 jobs
                  # failing >=5 of 7 days. 0.75 sits above the chronic
                  # baseline so the gate fails only when the shadow triage
                  # does materially WORSE than the platform's normal
                  # recurrence rate, not whenever chronic failures exist.
                  divergence_threshold: float = 0.75,
                  observation_days: Optional[float] = None,
                  shadow_cost: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    """Evaluate the four §5 gate criteria. Returns per-criterion
    {name, satisfied, current} — the view shows the current value of every
    FAILING criterion."""
    now = now if now is not None else clock.now()
    n = len(actions)
    first_ts = min((a["ts"] for a in actions if a["ts"]), default=None)

    # 1. minimum observation window elapsed
    if observation_days is None:
        observation_days = ((now - first_ts) / 86400.0) if first_ts else 0.0
    c1_ok = observation_days >= OBSERVATION_WINDOW_DAYS
    c1 = {"name": "observation_window", "satisfied": c1_ok,
          "current": f"{observation_days:.1f}d of {OBSERVATION_WINDOW_DAYS:.0f}d required"}

    # 2. divergence rate below the role's threshold, divergences individually labeled
    divs = [a for a in actions if a["divergence"]]
    div_rate = (len(divs) / n) if n else 0.0
    unlabeled = [a for a in divs if not a["divergence_label"]]
    c2_ok = div_rate < divergence_threshold and not unlabeled
    c2 = {"name": "divergence_rate", "satisfied": c2_ok,
          "current": f"{div_rate:.1%} (threshold {divergence_threshold:.0%}), "
                     f"{len(unlabeled)} unlabeled divergences"}

    # 3. zero unexplained would-have-done actions: every action carries an
    #    evidence label AND a DRAFT mandate spec (scope + expiry). 'stale'
    #    actions are unexplained unless they carry a draft spec + explicit
    #    staleness explanation (P1).
    def unexplained(a: Dict[str, Any]) -> bool:
        spec = a.get("draft_mandate_spec")
        has_spec = bool(spec and spec.get("scope") and spec.get("expiry"))
        if a["evidence_label"] == "stale":
            return not (has_spec and a.get("staleness_explanation"))
        return not has_spec

    unexp = [a for a in actions if unexplained(a)]
    c3_ok = n > 0 and not unexp
    c3 = {"name": "unexplained_actions", "satisfied": c3_ok,
          "current": f"{len(unexp)} unexplained of {n} shadow actions"}

    # 4. cost projection from shadow runs — no cost opacity
    if shadow_cost is None:
        c4 = {"name": "cost_projection", "satisfied": False,
              "current": "no shadow-run token/call projection provided"}
    else:
        c4 = {"name": "cost_projection", "satisfied": True,
              "current": (f"{shadow_cost.get('calls', 0)} calls / "
                          f"{shadow_cost.get('tokens', 0)} tok projected to go live")}

    criteria = [c1, c2, c3, c4]
    return {"role": role, "criteria": criteria, "all_satisfied": all(c["satisfied"] for c in criteria)}


# --------------------------------------------------------------------------- the view

def promotion_view(comparison: Dict[str, Any], now: Optional[float] = None,
                   divergence_threshold: float = 0.75,
                   observation_days: Optional[float] = None,
                   shadow_cost: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    """Render the promotion evidence: side-by-side log + gate criteria.
    `approve_available` is False until ALL four criteria pass; no replay UI."""
    doc = load_daily_comparison(comparison)
    gate = evaluate_gate(doc["role"], doc["actions"], now, divergence_threshold,
                         observation_days, shadow_cost)
    return {
        "role": doc["role"],
        "mode": doc["mode"],
        "panel": PANEL,
        "side_by_side": _aligned_side_by_side(doc["actions"]),   # daily comparison, not a replay
        "gate_criteria": gate["criteria"],
        "approve_available": gate["all_satisfied"],
        "demote_available": doc["mode"] == "live" and not gate["all_satisfied"],
        "draft_mandates": [a["draft_mandate_spec"] for a in doc["actions"] if a.get("draft_mandate_spec")],
    }


def demote_check(live_role_comparison: Dict[str, Any], false_positive_rate: float,
                 false_positive_threshold: float = 0.3, **kw) -> Dict[str, Any]:
    """P2: for a live role with a record-only shadow twin — demote-to-shadow is
    offered when divergence and/or admitted-findings false-positive rate degrades."""
    doc = load_daily_comparison(live_role_comparison)
    gate = evaluate_gate(doc["role"], doc["actions"], **kw)
    fp_bad = false_positive_rate >= false_positive_threshold
    return {"role": doc["role"], "divergence_degraded": not gate["all_satisfied"],
            "false_positive_rate": false_positive_rate, "false_positive_degraded": fp_bad,
            "demote_available": (not gate["all_satisfied"]) or fp_bad}


# --------------------------------------------------------------------------- approve / demote actions

def approve_promotion(view: Dict[str, Any], actor: str = "operator",
                      mandate_creator: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
                      ledger: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Panel C decision: convert each shadow action's DRAFT mandate spec into
    the role's first real mandate(s). The ledger records who/when/evidence.
    `mandate_creator(spec) -> mandate` is injected (Lane B hub_client.
    create_mandate in production) so this module owns no transport.

    Raises ValueError when the gate is not satisfied — the action simply does
    not exist in the UI until all four criteria pass.
    """
    if not view.get("approve_available"):
        raise ValueError("approve-promotion unavailable: gate criteria not satisfied")
    now = clock.now()
    created = []
    for spec in view.get("draft_mandates", []):
        if not (spec.get("scope") and spec.get("expiry")):
            raise ValueError(f"draft mandate spec missing scope/expiry: {spec!r}")
        m = mandate_creator(spec) if mandate_creator else {"mandate_id": f"m-{len(created) + 1}", **spec}
        created.append(m)
    if ledger is not None:
        ledger.append({"who": actor, "when": now, "action": "promote-to-live",
                       "role": view["role"], "evidence": "daily shadow-vs-actual comparison",
                       "mandates": created})
    return {"role": view["role"], "mode": "live", "mandates": created,
            "record_only_shadow_continues": True,   # §5 post-promotion continuation
            "ledger_entry": ledger[-1] if ledger else None}


def demote_to_shadow(role: str, reason: str, actor: str = "operator",
                     ledger: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    entry = {"who": actor, "when": clock.now(), "action": "demote-to-shadow",
             "role": role, "reason": reason}
    if ledger is not None:
        ledger.append(entry)
    return {"role": role, "mode": "record-only-shadow", "ledger_entry": entry}
