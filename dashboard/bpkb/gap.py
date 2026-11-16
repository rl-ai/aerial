"""Gap analysis: measure Aerial's compliance per practice against the external
best practices, produce findings for gaps, and map findings to decision points.

Findings follow the followup-classifier classes R1-R6 (per D1). The mapping
here is the BP lane's contribution: a gap finding carries severity -> class,
so the ops agent's existing followup machinery can pick it up unchanged.
"""
from __future__ import annotations

from . import checks as checks_mod
from .schema import validate_practice, VALID_STATUS

# gap severity -> followup classifier class (R1-R6, per D1)
SEVERITY_TO_CLASS = {
    "high": "R1",      # reliability/security gap on a live decision point -> immediate
    "medium": "R2",    # process gap -> schedule
    "low": "R3",       # hygiene -> batch
    "info": "R4",      # informational -> report only
}

# pillar-level default severity when the practice doesn't override
PILLAR_SEVERITY = {
    "reliability": "high",
    "security": "high",
    "cost": "medium",
    "operational-excellence": "medium",
    "sre": "medium",
}


def evaluate_practice(practice: dict, state: dict, check_id: str | None = None) -> dict:
    """Run the practice's check against state; set status + evidence."""
    practice = dict(practice)
    cid = check_id or _check_id_for(practice)
    status, evidence = checks_mod.run_check(cid, state)
    if status in VALID_STATUS:
        practice["status"] = status
        practice["evidence"] = evidence
    return practice


def _check_id_for(practice: dict) -> str:
    """The check the practice maps to. Explicit 'check' field wins; else the
    first check of its decision point in decision_points.json; else none."""
    if practice.get("check"):
        return practice["check"]
    return practice.get("_check", "")


def classify_finding(practice: dict, severity: str | None = None) -> dict:
    """A gap becomes a followup finding: class R1-R6, mapped to the decision point."""
    sev = severity or practice.get("severity") or PILLAR_SEVERITY.get(practice.get("pillar"), "medium")
    return {
        "finding_id": f"bp-gap:{practice['id']}",
        "practice_id": practice["id"],
        "class": SEVERITY_TO_CLASS.get(sev, "R2"),
        "severity": sev,
        "decision_point": practice.get("decision_point", "none"),
        "statement": practice["statement"],
        "evidence": practice.get("evidence", ""),
    }


def run_gap_analysis(practices: list[dict], state: dict) -> dict:
    """Evaluate all practices, then produce findings for the gaps.
    Returns {practices, findings, counts}."""
    evaluated, findings = [], []
    for practice in practices:
        p = validate_practice(practice)
        p = evaluate_practice(p, state)
        evaluated.append(p)
        if p["status"] == "gap":
            findings.append(classify_finding(p))
    counts = {s: sum(1 for p in evaluated if p["status"] == s) for s in sorted(VALID_STATUS)}
    return {"practices": evaluated, "findings": findings, "counts": counts}


def map_decision_points(practices: list[dict], decision_points: dict) -> dict:
    """Per decision point D1-D12: which practices map to it and their compliance."""
    out = {}
    for dp_id in ["D%d" % i for i in range(1, 13)]:
        mapped = [p for p in practices if p.get("decision_point") == dp_id]
        out[dp_id] = {
            "name": decision_points.get(dp_id, {}).get("name", "unknown"),
            "status": decision_points.get(dp_id, {}).get("status", "active"),
            "practices": len(mapped),
            "gaps": sum(1 for p in mapped if p["status"] == "gap"),
        }
    return out
