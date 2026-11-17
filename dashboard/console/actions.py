"""Queue-action interaction layer (two-phase approve + POST handling) — RC1 lane 2.

Implements console-rc-ui.md §3/§4/§8 V4–V7 on top of the Lane A backend
(`exception_queue.mandate_flow`, `exception_queue.queue`):

- Two-phase approve: the Approve click returns confirmation-panel data
  (pre-filled scope/expiry, NO write); only the Confirm approve call touches
  the mandate flow. Cancel never reaches the write path.
- Dismiss with a blank reason records 'unspecified' (Q7).
- Escalate returns the route curator → lead → operator → JEV; the row's actions
  collapse to a pending-escalation line.
- Error treatments: write-token 401/403 → two-step inline error
  ("write token rejected — re-authenticated once, retrying…" then
  "not committed — nothing changed"); creation failure → the same
  "not committed" treatment; execution failure (NoFreshDecision) →
  "executed 0 times · intent NOT re-run — approve again for a fresh decision"
  with no auto-retry.

`ctx` carries the live objects: {'queue': Queue, 'mandate_flow': MandateFlow}.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from typing import Optional

# The exception_queue package lives in the built tree. Prefer a sibling copy,
# fall back to the canonical build tree.
import os as _os
for _root in (_file_root := __file__.rsplit("/console/", 1)[0],
              _os.environ.get("AERIAL_EXCEPTION_QUEUE_FALLBACK", "")):
    if _root and _os.path.isdir(_os.path.join(_root, "exception_queue")):
        sys.path.insert(0, _root)
        break

from exception_queue.mandate_flow import (          # noqa: E402
    ESCALATION_ROUTE,
    MandateFlow,
    NoFreshDecision,
    WriteAuthError,
)
from exception_queue.queue import Queue, QueueItem  # noqa: E402

REJECTED_STEPS = [
    "write token rejected — re-authenticated once, retrying…",
    "not committed — nothing changed",
]
NOT_RE_RUN = "executed 0 times · intent NOT re-run — approve again for a fresh decision"
NOT_COMMITTED = "not committed — nothing changed"
ESCALATION_ROUTE_LABEL = " → ".join(ESCALATION_ROUTE)  # curator → lead → operator → JEV


def _fmt_expiry(expiry_at: float) -> str:
    return datetime.fromtimestamp(expiry_at, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M")


def _base(attempts: int = 0, phase: str = "commit",
          prefill: Optional[dict] = None) -> dict:
    return {"status": None, "message": "", "attempts": attempts,
            "phase": phase, "prefill": prefill}


def get_confirmation(flow: MandateFlow, item_id: str) -> dict:
    """§4 confirmation-panel data for an Approve click — reads only, no write.

    Mirrors mandate_flow `_prefill`: scope/expiry come from the row's context.
    If a covering mandate exists it is shown; otherwise the NEW-mandate
    pre-fill (scope + policy expiry) is offered.
    """
    item = flow.queue.get(item_id)
    if item is None:
        raise KeyError(f"no queue row {item_id}")
    covering = flow.covering_mandate(item)
    scope, expiry_at = MandateFlow._prefill(item, flow.queue)
    panel = {
        "item_id": item_id,
        "action": item.proposed_action or item.claim,
        "claim": item.claim,
        "evidence": item.evidence_label,
        "covers": covering is not None,
        "scope": scope,
        "expiry_at": expiry_at,
        "expiry": _fmt_expiry(expiry_at),
        "audit": "1 mandate use · audit record written",
    }
    if covering is not None:
        panel["mandate_id"] = covering["mandate_id"]
        panel["mandate_scope"] = covering["scope"]
        panel["mandate_expiry_at"] = covering["expiry_at"]
        panel["mandate_expiry"] = _fmt_expiry(covering["expiry_at"])
    else:
        panel["new_mandate"] = True
    return panel


def handle_queue_action(ctx: dict, action: str, item_id: str,
                        payload: dict) -> dict:
    """POST handler for one queue action.

    ctx: {'queue': Queue, 'mandate_flow': MandateFlow}
    action: 'approve' | 'dismiss' | 'escalate' | 'cancel'
    payload: {'phase': 'confirm'|'commit', 'reason': str,
              'scope': str, 'expiry_at': float}  (scope/expiry editable
              pre-commit per §4; commit uses the edited values)

    Returns {'status': executed|dismissed|escalated|error, 'message': str,
             'attempts': int, 'phase': confirm|commit, 'prefill': dict|None}.
    """
    flow: MandateFlow = ctx["mandate_flow"]
    queue: Queue = ctx.get("queue", flow.queue)
    payload = payload or {}
    phase = payload.get("phase", "commit")

    out = _base(phase=phase)

    # ---- Approve, phase 1: confirmation data only (no write, ever) --------
    if action == "approve" and phase != "commit":
        out["status"] = "error"          # not executed yet — data only
        out["message"] = "confirm-panel data (no write performed)"
        out["prefill"] = get_confirmation(flow, item_id)
        out["attempts"] = 0
        return out

    # ---- Cancel: commits nothing, never touches the flow -------------------
    if action == "cancel":
        out["status"] = "dismissed"
        out["message"] = "cancelled — nothing was committed"
        out["prefill"] = None
        return out

    try:
        if action == "approve":
            return _approve_commit(flow, queue, item_id, payload, out)
        if action == "dismiss":
            return _dismiss(flow, item_id, payload, out)
        if action == "escalate":
            return _escalate(flow, item_id, payload, out)
        raise ValueError(f"unknown action: {action!r}")
    except KeyError:
        out["status"] = "error"
        out["message"] = f"unknown row: {item_id}"
        return out


# ---------------------------------------------------------------- phases ----

def _approve_commit(flow, queue, item_id, payload, out) -> dict:
    try:
        result = flow.decide(item_id, "approve")
    except WriteAuthError:
        # token gate already used its single re-auth inside the flow →
        # two-step inline error, row stays waiting-on-you, no partial write
        out["status"] = "error"
        out["message"] = REJECTED_STEPS[1]
        out["steps"] = list(REJECTED_STEPS)
        out["row_state"] = "waiting-on-you"
        out["actions_re_enabled"] = True
        return out
    except NoFreshDecision:
        out["status"] = "error"
        out["message"] = NOT_RE_RUN
        out["row_state"] = "waiting-on-you"
        out["auto_retry"] = False
        return out

    if result.get("phase") == "creation" and not result.get("ok"):
        # creation failure: everything rolled back — same treatment as 401
        out["status"] = "error"
        out["message"] = NOT_COMMITTED
        out["steps"] = list(REJECTED_STEPS)
        out["row_state"] = result.get("row_state", "waiting-on-you")
        out["actions_re_enabled"] = True
        return out

    if not result.get("ok", True) and result.get("phase") == "execution":
        # execution failure: intent NOT re-run without a fresh decision
        out["status"] = "error"
        out["message"] = NOT_RE_RUN
        out["attempts"] = int(result.get("attempts", 0))
        out["row_state"] = result.get("row_state", "waiting-on-you")
        out["auto_retry"] = False
        return out

    # success
    attempts = int(result.get("attempts", 0))
    out["status"] = "executed"
    out["message"] = f"executed ✓ (attempts {attempts})"
    out["attempts"] = attempts
    out["row_state"] = result.get("row_state", "done")
    return out


def _dismiss(flow, item_id, payload, out) -> dict:
    reason = (payload.get("reason") or "").strip()
    result = flow.decide(item_id, "dismiss", reason=reason or None)
    out["status"] = "dismissed"
    out["message"] = "dismissed · calibration sample recorded"
    out["calibration_reason"] = result.get("calibration_reason")
    out["row_state"] = "dismissed"
    return out


def _escalate(flow, item_id, payload, out) -> dict:
    # the console shows the full route curator → lead → operator → JEV; an actor
    # outside ESCALATION_ROUTE makes mandate_flow start at curator
    actor = payload.get("actor") or "console-operator"
    result = flow.decide(item_id, "escalate", actor=actor)
    route = result.get("route", ESCALATION_ROUTE)
    out["status"] = "escalated"
    out["message"] = "escalated → " + " → ".join(route)
    out["route"] = route
    out["row_state"] = "escalated"
    return out
