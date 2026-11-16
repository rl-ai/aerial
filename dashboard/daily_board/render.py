"""Daily-board renderer (Lane C). Spec: operator-dashboard.md rev2, §1–3.

Zone A — project statuses (mirrors the tracker's project structure; no
live-watch pane, no activity feed, no run-tree trace — §6 non-features).
Zone B — exception/decision queue rows (Panel A/B/C), rendered from Lane A's
Queue.ordered() — scoring is NOT re-implemented here.
Zone C — right rail: mandates ledger + shadow-week pulse.

L2 collapse rule: ONLY waiting-on-you/blocked items render expanded; everything
else is one collapsed row. Done items show 'done since <last visit>' read from
the SAME dashboard-sessions store (Lane B SessionsStore) used by §1 and the
daily brief — one source, three surfaces.

Data access goes through Lane B: JobRunsDB, ModelCallsTailer, SessionsStore,
SnapshotStore. On degraded sources the renderer emits the snapshot-store
verdict ('stale' with read timestamp, or 'unavailable') rather than guessing.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from dashboard.data_layer.job_runs import JobRunsDB
from dashboard.data_layer.model_calls_tailer import ModelCallsTailer
from dashboard.data_layer.sessions import SessionsStore
from dashboard.data_layer.snapshot_store import SnapshotStore
from dashboard.exception_queue.queue import Queue

VALID_STATUSES = ("working", "waiting-on-you", "blocked", "done-since-last-visit")
EXPANDED_STATUSES = ("waiting-on-you", "blocked")
PANELS = ("A", "B", "C")
ACTIONS = ("approve", "dismiss", "escalate")  # exactly three, §2


# --------------------------------------------------------------------------- helpers

def _age_label(created_at: float, now: float) -> str:
    h = max(0.0, (now - created_at) / 3600.0)
    if h < 1:
        return f"waiting {h * 60:.0f}m"
    return f"waiting {h:.0f}h"


def _classify_panel(item: Any) -> str:

    if getattr(item, "steps", None):
        return "B"   # a multi-step proposal collapses to a brief (Panel B, §2)
    """Panel A/B/C per §2. An item may declare its panel (Panel B briefs,
    Panel C policy items); default is A (single gate decision)."""
    declared = getattr(item, "panel", None)
    if declared in PANELS:
        return declared
    action = (getattr(item, "proposed_action", "") or "").lower()
    if "mandate" in action or "policy" in action or "scope" in action:
        return "C"
    if getattr(item, "is_brief", False):
        return "B"
    return "A"



def _tokens_label(bucket: Optional[Dict[str, int]]) -> str:
    if not bucket:
        return "0 calls / 0 tok"
    return (f"{bucket.get('calls', 0)} calls / "
            f"{bucket.get('in_tokens', 0) + bucket.get('out_tokens', 0)} tok")


# ------------------------------------------------------------------- QA tab (C1)

def qa_tab(queue: Queue, sample_rate: float = 0.25, seed: int = 0,
           now: Optional[float] = None) -> Dict[str, Any]:
    """QA-sampling tab (§7 C1): a statistical slice (NOT the full backlog) of
    admitted findings offered for annotation. A reasoned dismissal of a
    sampled row goes through MandateFlow.decide(..., 'dismiss'), which feeds
    the gate's false-positive calibration metric."""
    from dashboard.exception_queue.queue import qa_sample
    now = now if now is not None else time.time()
    slice_rows = qa_sample(queue, sample_rate=sample_rate, seed=seed)
    return {
        "sample_size": len(slice_rows),
        "backlog_size": len([it for it in queue.ordered(now)
                             if it.state not in ("dismissed", "done")]),
        "sample_rate": sample_rate,
        "seed": seed,
        "rows": [{
            "item_id": it.item_id,
            "claim": it.claim,
            "evidence_label": it.evidence_label,
            "source": it.source_agent,
            "admission_score": round(it.admission_score, 3),
            "actions": ["dismiss-with-reason", "keep"],
        } for it in slice_rows],
    }


# --------------------------------------------------------------------------- brief + board



# --------------------------------------------------------------------------- Zone A

def zone_a_rows(project_items: List[Dict[str, Any]], runs: JobRunsDB,
                tailer: ModelCallsTailer, sessions: SessionsStore,
                now: Optional[float] = None) -> List[Dict[str, Any]]:
    """One row per project item, mirroring the tracker's structure.

    Each `project_items` entry comes from the tracker:
      {project, item, status, stages, done_stages, evidence_artifact, done_ts?}
    Status is normalized into the four valid states. 'done-since-last-visit'
    is decided against SessionsStore.last_visit() — the single source.
    """
    now = now if now is not None else time.time()
    tailer.tail()  # advance the persisted-offset tail before reading aggregates
    totals = tailer.totals_by_item()
    last_visit = sessions.last_visit()
    rows: List[Dict[str, Any]] = []
    for pi in project_items:
        raw_status = str(pi.get("status", "working")).lower()
        done_ts = pi.get("done_ts")
        if raw_status in ("done", "complete", "completed") or (
                done_ts is not None and done_ts <= (last_visit or float("inf"))):
            status = "done-since-last-visit"
        elif raw_status in ("waiting-on-you", "blocked"):
            status = raw_status
        else:
            status = "working"
        stages = list(pi.get("stages") or [])
        done_stages = list(pi.get("done_stages") or [])
        pct = round(100.0 * len(done_stages) / len(stages), 1) if stages else 0.0
        remaining = [s for s in stages if s not in done_stages]
        eta = runs.eta_range(pi["project"], remaining) if remaining else {"eta": "n/a (no open stages)"}
        bucket = totals.get(pi.get("item_tag") or pi.get("item") or pi["project"])
        untagged = tailer.untagged
        expanded = status in EXPANDED_STATUSES
        if status == "done-since-last-visit":
            done_label = sessions.done_since_label(done_ts)
        else:
            done_label = None
        rows.append({
            "project": pi["project"],
            "item": pi.get("item") or pi["project"],
            "status": status,                      # ∈ VALID_STATUSES
            "pct_complete": pct,
            "eta": eta.get("eta", "insufficient history"),
            "eta_insufficient": eta.get("eta") == "insufficient history",
            "tokens": _tokens_label(bucket),
            "untagged_bucket": _tokens_label(untagged) if untagged.get("calls") else None,
            "evidence_artifact": pi.get("evidence_artifact"),
            "expanded": expanded,
            # collapsed items carry exactly one row of content:
            "collapsed_line": f"{pi['project']} — {status} — {pct:.0f}% — {eta.get('eta')}",
            "detail": {
                "in_progress_milestones": pi.get("in_progress", []) if expanded else None,
                "open_asks": pi.get("open_asks", []) if expanded else None,
                "blockers": pi.get("blockers", []) if expanded else None,
            },
            "done_label": done_label,
            "done_since_last_visit_src": "dashboard-sessions.last_visit()",
        })
    return rows


# --------------------------------------------------------------------------- Zone B

def zone_b_rows(queue: Queue, now: Optional[float] = None,
                evidence_artifacts: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """Queue rows from Lane A's ordered() — one-line claim, evidence label,
    source agent+role, proposed action, mandate, evidence artifact for changes,
    age, admission score. Exactly three actions per row."""
    now = now if now is not None else time.time()
    evidence_artifacts = evidence_artifacts or {}
    flow = getattr(queue, "mandate_flow", None)
    rows = []
    for item in queue.ordered(now):
        mandate = None
        if flow is not None:
            m = flow.covering_mandate(item)
            if m:
                mandate = {"mandate_id": m["mandate_id"], "scope": m["scope"],
                           "expiry_at": m["expiry_at"]}
        rows.append({
            "item_id": item.item_id,
            "panel": _classify_panel(item),
            "claim": item.claim,
            "evidence_label": item.evidence_label,     # verified | inferred | stale
            "source": f"{item.source_agent} (role: {getattr(item, 'source_role', item.source_agent)})",
            "proposed_action": item.proposed_action,
            "mandate": mandate,
            "evidence_artifact": evidence_artifacts.get(item.item_id),
            "age": _age_label(item.created_at, now),
            "admission_score": round(item.admission_score, 3),
            "score": round(item.score, 3),
            "actions": list(ACTIONS),                  # exactly three
        })
    return rows


# --------------------------------------------------------------------------- Zone C

def zone_c(mandates_ledger: List[Dict[str, Any]], shadow_pulse: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "mandates_ledger": mandates_ledger,   # standing grants, scope + expiry
        "shadow_pulse": {
            "would_have_done_today": shadow_pulse.get("would_have_done_today", 0),
            "divergence_note": shadow_pulse.get("divergence_note", ""),
            "link": shadow_pulse.get("daily_comparison_link"),
        },
    }


# --------------------------------------------------------------------------- brief + board

def daily_brief(rows: List[Dict[str, Any]], queue_rows: List[Dict[str, Any]],
                sessions: SessionsStore) -> List[str]:
    """Daily-brief lines; 'done since' phrasing reads the SAME sessions store
    as Zone A and L2 (rev2 single-source requirement)."""
    lines = []
    for r in rows:
        if r["status"] == "done-since-last-visit":
            lines.append(f"DONE: {r['project']} — {r['done_label']}")
    for qr in queue_rows:
        if qr["panel"] == "C":
            lines.append(f"POLICY: {qr['claim']} ({qr['age']})")
    return lines


def render_board(project_items: List[Dict[str, Any]], runs: JobRunsDB,
                 tailer: ModelCallsTailer, sessions: SessionsStore,
                 queue: Queue, snapshot: Optional[SnapshotStore] = None,
                 mandates_ledger: Optional[List[Dict[str, Any]]] = None,
                 shadow_pulse: Optional[Dict[str, Any]] = None,
                 evidence_artifacts: Optional[Dict[str, str]] = None,
                 now: Optional[float] = None) -> Dict[str, Any]:
    now = now if now is not None else time.time()
    try:
        rows = zone_a_rows(project_items, runs, tailer, sessions, now)
        runs_state = "live"
    except Exception:  # degraded source -> last-known-good snapshot per D2
        runs_state = "unavailable"
        rows = []
        if snapshot is not None:
            stale = snapshot.get_stale("zone_a")
            if stale["state"] == "stale":
                rows, runs_state = stale["payload"], f"stale (read {time.strftime('%Y-%m-%d %H:%M', time.localtime(stale['read_ts']))})"
    queue_rows = zone_b_rows(queue, now, evidence_artifacts)
    board = {
        "zone_a": rows,
        "zone_b": queue_rows,
        "zone_c": zone_c(mandates_ledger or [], shadow_pulse or {}),
        "brief": daily_brief(rows, queue_rows, sessions),
        "runs_state": runs_state,
    }
    if snapshot is not None and rows and runs_state == "live":
        snapshot.save("zone_a", rows, now)
    return board


def render_text(board: Dict[str, Any]) -> str:
    out = ["=== DAILY BOARD ===", "-- Zone A: project statuses --"]
    for r in board["zone_a"]:
        out.append(r["collapsed_line"] if not r["expanded"] else
                   f"{r['project']} — {r['status']} — {r['pct_complete']}% — ETA {r['eta']} — "
                   f"{r['tokens']} — evidence: {r['evidence_artifact']}")
        if r["status"] == "done-since-last-visit":
            out.append(f"   ({r['done_label']})")
    if board["zone_a"]:
        untagged = [r["untagged_bucket"] for r in board["zone_a"] if r["untagged_bucket"]]
        if untagged:
            out.append(f"   untagged model-calls bucket: {untagged[0]}")
    out.append("-- Zone B: exception/decision queue --")
    for q in board["zone_b"]:
        out.append(f"[Panel {q['panel']}] {q['claim']} | {q['evidence_label']} | {q['source']} | "
                   f"action: {q['proposed_action']} | mandate: {q['mandate'] or 'none'} | "
                   f"artifact: {q['evidence_artifact'] or 'n/a'} | {q['age']} | score {q['score']}")
        out.append(f"   actions: {' / '.join(q['actions'])}")
    out.append("-- Zone C: right rail --")
    out.append(f"mandates ledger: {len(board['zone_c']['mandates_ledger'])} standing grants")
    sp = board["zone_c"]["shadow_pulse"]
    out.append(f"shadow pulse: {sp['would_have_done_today']} would-have-done today — {sp['divergence_note']}")
    out.append("-- daily brief --")
    out.extend(board["brief"] or ["(no brief entries)"])
    return "\n".join(out)
