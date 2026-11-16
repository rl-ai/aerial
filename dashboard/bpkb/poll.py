"""Ops-agent poll-loop entry point.

The ops agent's poll loop calls sample_bpkb() each tick. It gathers Aerial's
state, runs the gap analysis over the BP-KB, and emits the "Best practices
signal" line for the daily report:

    Best practices signal: N new external practices, M compliant / K gaps
    (top gap: <statement> -> D<p>)

New-practice detection: a practice id not in the previous state file counts as
new this cycle. State file is JSON; pass the path, keep it hermetic via the
`state` inject for tests.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import ingest as ingest_mod
from .gap import run_gap_analysis, map_decision_points

BASE = Path(__file__).resolve().parent.parent


def load_seed_practices(seed_path: str | None = None) -> list[dict]:
    path = Path(seed_path or BASE / "seeds" / "practices.json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)["practices"]


def load_decision_points(path: str | None = None) -> dict:
    path = Path(path or BASE / "decision_points.json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)["decision_points"]


def load_bp_sources(path: str | None = None) -> list[dict]:
    path = Path(path or BASE / "bp_sources.json")
    return ingest_mod.load_sources(str(path))


def gather_state(root: str | None = None) -> dict:
    """Gather Aerial state for the checks. Probes well-known markers; every
    probe is tolerate-missing so a fresh root yields gaps, not crashes."""
    import os as _os
    root = Path(root or _os.path.expanduser(
        str(__import__("deployment_profile").value("paths.state_dir"))))
    state = {
        "followup_classifier_present": False,
        "failure_flow_state_machine": False,
        "vram_demand_load": False,
        "handstop_forbidden": True,          # D2 rule: hand-stop is forbidden by policy
        "audit_log": "model-call log + kanban events",
        "separate_accounts": True,           # service agents vs admin accounts separate
        "no_credential_handling": True,      # orchestration rules: never handle credentials
        "injection_defense": "prompt-injection terms in scrape match groups; sandboxed attack runs",
        "daily_report": "daily exec report (06:45 cadence)",
        "token_budgets": True,               # need = input + max_tokens + 5% margin
    }
    # live markers, tolerate-missing
    if (root / "workspaces/worker/lanes-report-v2/dashboard/exception_queue/mandate_flow.py").exists():
        state["failure_flow_state_machine"] = True
        state["followup_classifier_present"] = True
    if (root / "workspaces/devfix-20261114/vram-manager/dashboard/vram_manager.py").exists():
        state["vram_demand_load"] = True
    return state


def sample_bpkb(kb_state_path: str, seed_path: str | None = None,
                decision_points_path: str | None = None,
                state: dict | None = None) -> dict:
    """One poll tick. Returns the BP signal dict; writes the state file."""
    state = state or gather_state()
    practices = load_seed_practices(seed_path)
    dpoints = load_decision_points(decision_points_path)
    result = run_gap_analysis(practices, state)
    result["decision_points"] = map_decision_points(result["practices"], dpoints)

    prev_ids = set()
    p = Path(kb_state_path)
    if p.exists():
        try:
            prev_ids = set(json.loads(p.read_text(encoding="utf-8")).get("practice_ids", []))
        except Exception:
            prev_ids = set()
    new_ids = [x["id"] for x in result["practices"] if x["id"] not in prev_ids]
    result["new_practices"] = new_ids

    gaps = [x for x in result["practices"] if x["status"] == "gap"]
    counts = result["counts"]
    top = gaps[0]["statement"][:80] if gaps else "none"
    result["signal_line"] = (
        f"Best practices signal: {len(new_ids)} new external practices, "
        f"{counts['compliant']} compliant / {counts['gap']} gaps "
        f"(top gap: {top})"
    )
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "practice_ids": [x["id"] for x in result["practices"]],
        "findings": result["findings"],
        "signal_line": result["signal_line"],
    }, indent=1), encoding="utf-8")
    return result
