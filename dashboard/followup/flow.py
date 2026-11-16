"""Failure-followup state machine — decision D3.

States (first-match linear path with explicit failure branches):

    finding → classified → mandate-created → fix-attempted → verified
        → closed (silent) | escalated

Failure branches:
    - mandate creation fails      → mandate-failed → escalated
    - fix execution raises        → fix-failed     → escalated
    - verification says not fixed → escalated
    - classify returns unknown    → escalated (daily report, fail-closed)

Every transition is persisted (sqlite) with a timestamp, so the audit trail
shows exactly which branch a finding took. Closed findings are closed
SILENTLY: no ping, at most a daily-report line via the ping policy.

Mandate creation goes through the Lane A mandate-creation path (`_create`):
the mandate is scoped to the finding's proposed fix and expires (default 24h).
The creator is injectable so the flow is fully self-testable; the default
adapter wraps `exception_queue.mandate_flow.MandateFlow`.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any, Callable, Optional

try:  # Lane F imports (package-relative)
    from . import classifier as clf
    from . import ping_policy
except ImportError:  # running as a top-level module
    import classifier as clf            # type: ignore
    import ping_policy                  # type: ignore

MANDATE_EXPIRY_S = 24 * 3600.0

# Linear happy-path states, in order.
STATES = ("finding", "classified", "mandate-created", "fix-attempted",
          "verified", "closed")
TERMINAL_STATES = frozenset({"closed", "escalated"})

TRANSITIONS = {
    "submit":      ("finding", "classified"),
    "create":      ("classified", "mandate-created"),
    "attempt":     ("mandate-created", "fix-attempted"),
    "verify":      ("fix-attempted", "verified"),
    "close":       ("verified", "closed"),
    "escalate":    ("*", "escalated"),
    "fail-create": ("*", "mandate-failed"),
    "fail-fix":    ("*", "fix-failed"),
}


class InvalidTransition(Exception):
    pass


# ---------------------------------------------------------------------------
# Mandate creation via the Lane A mandate-creation path (_create)
# ---------------------------------------------------------------------------

class MandateFlowAdapter:
    """Creates scoped + expiring mandates through MandateFlow's internal
    mandate-creation transaction (the `_create` path)."""

    def __init__(self, mandate_flow, clock: Callable[[], float],
                 expiry_s: float = MANDATE_EXPIRY_S):
        self.mf = mandate_flow
        self.clock = clock
        self.expiry_s = expiry_s
        self._seq = 0

    def create(self, finding, scope: str) -> dict:
        """`finding` may be a CuratorFinding or any object carrying
        item_id / claim / proposed_action / evidence_label (e.g. a Lane A
        QueueItem)."""
        self._seq += 1
        mandate_id = f"mf-{finding.item_id}-{self._seq}"
        expiry_at = self.clock() + self.expiry_s
        # ONE hub transaction through Lane A's creation path; any exception
        # leaves nothing committed.
        self.mf._create_transaction(mandate_id, finding, scope, expiry_at,
                                    actor="followup")
        return {"mandate_id": mandate_id, "scope": scope, "expiry_at": expiry_at}


# ---------------------------------------------------------------------------
# Flow
# ---------------------------------------------------------------------------

class FollowupFlow:
    """State machine for the failure-followup workflow. External effects
    (mandate creation, fix execution, verification, ping routing) are all
    injected callables so the machine is fully hermetic under test."""

    def __init__(self, db_path: str = ":memory:",
                 clock: Callable[[], float] = lambda: 0.0,
                 classify_fn: Callable[[Any], str] = clf.classify,
                 mandate_creator: Optional[Any] = None,
                 fix_executor: Optional[Callable[[Any, dict], Any]] = None,
                 verifier: Optional[Callable[[Any], bool]] = None,
                 notify: Optional[Callable[[str, Any], Any]] = None):
        self.clock = clock
        self.classify_fn = classify_fn
        self.mandate_creator = mandate_creator
        self.fix_executor = fix_executor or (lambda finding, mandate: "fixed")
        self.verifier = verifier or (lambda finding: True)
        # notify(fix_class, finding) routes the ping per the policy.
        self.notify = notify or ping_policy.route
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(db_path, isolation_level=None)
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS followups (
                finding_id TEXT PRIMARY KEY, fix_class TEXT, state TEXT,
                mandate_id TEXT, opened_at REAL, updated_at REAL);
            CREATE TABLE IF NOT EXISTS transitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                finding_id TEXT, from_state TEXT, to_state TEXT,
                action TEXT, at REAL, detail TEXT);
            """
        )

    # -- persistence -------------------------------------------------------

    def state_of(self, finding_id: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT state FROM followups WHERE finding_id=?",
            (finding_id,)).fetchone()
        return row[0] if row else None

    def history(self, finding_id: str) -> list:
        return self.conn.execute(
            "SELECT from_state, to_state, action, at, detail FROM transitions "
            "WHERE finding_id=? ORDER BY id", (finding_id,)).fetchall()

    def _transition(self, finding_id: str, action: str, to_state: str,
                    detail: str = "", expect: Optional[str] = None) -> None:
        row = self.conn.execute(
            "SELECT state FROM followups WHERE finding_id=?",
            (finding_id,)).fetchone()
        current = row[0] if row else None
        if expect is not None and current != expect:
            raise InvalidTransition(
                f"{finding_id}: {action} requires state {expect!r}, "
                f"found {current!r}")
        now = self.clock()
        if row is None:
            self.conn.execute(
                "INSERT INTO followups VALUES (?,?,?,?,?,?)",
                (finding_id, "", to_state, None, now, now))
        else:
            self.conn.execute(
                "UPDATE followups SET state=?, updated_at=? WHERE finding_id=?",
                (to_state, now, finding_id))
        self.conn.execute(
            "INSERT INTO transitions (finding_id, from_state, to_state, "
            "action, at, detail) VALUES (?,?,?,?,?,?)",
            (finding_id, current, to_state, action, now, detail))

    def _set_class(self, finding_id: str, fix_class: str) -> None:
        self.conn.execute(
            "UPDATE followups SET fix_class=?, updated_at=? WHERE finding_id=?",
            (fix_class, self.clock(), finding_id))

    def _set_mandate(self, finding_id: str, mandate_id: str) -> None:
        self.conn.execute(
            "UPDATE followups SET mandate_id=?, updated_at=? WHERE finding_id=?",
            (mandate_id, self.clock(), finding_id))

    # -- the workflow --------------------------------------------------------

    def submit(self, finding) -> dict:
        """finding → classified. Persists the classification; routes
        immediate-ping classes right away; auto-fixable/mandate classes
        continue into the fix path; everything else escalates to the daily
        report. Unknown classes fail closed (escalate, never auto-fix)."""
        cls = self.classify_fn(finding)
        self._transition(finding.finding_id, "submit", "classified",
                         detail=cls, expect=None)
        self._set_class(finding.finding_id, cls)
        if cls in clf.IMMEDIATE_CLASSES:
            # SEV / security: immediate ping via the notification policy.
            self.notify(cls, finding)
        if cls == clf.AUTO_FIX:
            return self._fix_path(finding, cls, create_mandate=False)
        if cls == clf.MANDATE:
            return self._fix_path(finding, cls, create_mandate=True)
        # daily-report / unknown / sev classes: escalate (sev was pinged).
        self._escalate(finding, f"class={cls}")
        return {"finding_id": finding.finding_id, "fix_class": cls,
                "state": "escalated", "route": clf.route_for(cls)}

    def _fix_path(self, finding, cls: str, create_mandate: bool) -> dict:
        mandate = None
        if create_mandate:
            try:
                mandate = self._create_mandate(finding)
            except Exception as exc:
                self._transition(finding.finding_id, "fail-create",
                                 "mandate-failed", detail=repr(exc))
                self._escalate(finding, f"mandate creation failed: {exc!r}")
                return {"finding_id": finding.finding_id, "fix_class": cls,
                        "state": "escalated", "error": repr(exc)}
        try:
            self._transition(finding.finding_id, "attempt", "fix-attempted",
                             detail=repr(mandate), expect="mandate-created"
                             if create_mandate else "classified")
            result = self.fix_executor(finding, mandate)
        except Exception as exc:
            self._transition(finding.finding_id, "fail-fix", "fix-failed",
                             detail=repr(exc))
            self._escalate(finding, f"fix attempt failed: {exc!r}")
            return {"finding_id": finding.finding_id, "fix_class": cls,
                    "state": "escalated", "error": repr(exc)}
        return self._verify(finding, cls, result)

    def _create_mandate(self, finding) -> dict:
        if self.mandate_creator is None:
            raise RuntimeError("no mandate creator configured")
        scope = getattr(finding, "proposed_action", None) or finding.claim
        mandate = self.mandate_creator.create(finding, scope)
        self._transition(finding.finding_id, "create", "mandate-created",
                         detail=mandate["mandate_id"], expect="classified")
        self._set_mandate(finding.finding_id, mandate["mandate_id"])
        return mandate

    def _verify(self, finding, cls: str, result) -> dict:
        ok = bool(self.verifier(finding))
        self._transition(finding.finding_id, "verify", "verified",
                         detail=f"ok={ok}", expect="fix-attempted")
        if ok:
            # Closed SILENTLY — no ping, no immediate notification.
            self._transition(finding.finding_id, "close", "closed",
                             expect="verified")
            return {"finding_id": finding.finding_id, "fix_class": cls,
                    "state": "closed", "silent": True, "result": result}
        self._escalate(finding, "verification failed")
        return {"finding_id": finding.finding_id, "fix_class": cls,
                "state": "escalated", "reason": "verification failed"}

    def _escalate(self, finding, reason: str) -> None:
        state = self.state_of(finding.finding_id)
        if state not in TERMINAL_STATES:
            self._transition(finding.finding_id, "escalate", "escalated",
                             detail=reason)
        # Escalated non-SEV findings aggregate to the daily report via the
        # ping policy (never an immediate ping).
        if self.state_of(finding.finding_id) == "escalated":
            cls_row = self.conn.execute(
                "SELECT fix_class FROM followups WHERE finding_id=?",
                (finding.finding_id,)).fetchone()
            cls = cls_row[0] if cls_row else clf.UNKNOWN
            if cls not in clf.IMMEDIATE_CLASSES:
                self.notify(clf.DAILY_REPORT, finding)

    def close(self) -> None:
        self.conn.close()
