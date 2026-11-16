"""Two-phase approve (mandate creation / execution) — Lane A.

Implements operator-dashboard spec §2 Decision 1 / §7 Q3, Q4, Q7:
- Exactly three decisions: approve / dismiss / escalate.
- Approve without a covering mandate opens the mandate-creation flow with
  scope + expiry pre-filled from the row's context.
- Phase (a) *creation*: ONE hub transaction creates the mandate, persists the
  action intent, and writes the audit record. Any exception mid-transaction
  rolls everything back — mandate, intent, audit all uncommitted — and the row
  stays waiting-on-you.
- Phase (b) *execution*: a separate step consuming the mandate, bounded retry
  (max 3 attempts), idempotency key = row ID + mandate ID. Residual failure
  returns the row to waiting-on-you and the intent is NOT re-executed without
  a fresh decision.
- Write path goes through a decision-scoped token; a 401/403 leaves everything
  uncommitted (no partial write) and allows exactly one re-auth + retry.
- Dismiss feeds the gate calibration loop; a reason-less dismissal records a
  negative sample with reason 'unspecified'.
- Escalate routes curator → lead → Ray → JEV.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable, Optional

from .queue import Queue, QueueItem

ESCALATION_ROUTE = ["curator", "lead", "Ray", "JEV"]
MAX_EXECUTION_ATTEMPTS = 3


class WriteAuthError(Exception):
    """Decision-scoped write token rejected (401/403) — no partial write."""

    def __init__(self, status: int, message: str = ""):
        super().__init__(f"write-token {status}: {message}")
        self.status = status


class NoFreshDecision(Exception):
    """A pending (unexecuted) intent may not be re-executed without a fresh
    decision from the operator."""


class UnknownDecision(ValueError):
    pass


class WriteToken:
    """Decision-scoped write-token gate. `authenticate()` performs the single
    re-auth allowed after a 401/403 (token-rotation fixture path)."""

    def __init__(self, fail_times: int = 0, status: int = 403):
        self.fail_times = fail_times
        self.status = status
        self.auth_calls = 0

    def authorize(self) -> None:
        """Raise WriteAuthError while the token is stale; re-auths once."""
        if self.fail_times > 0:
            self.fail_times -= 1
            raise WriteAuthError(self.status, "token rotated/expired")

    def re_authenticate(self) -> None:
        self.auth_calls += 1


class MandateFlow:
    def __init__(self, queue: Queue, db_path: str = ":memory:",
                 executor: Optional[Callable[[QueueItem, dict], Any]] = None,
                 calibration: Optional[Callable[[QueueItem, str], Any]] = None,
                 write_token: Optional[WriteToken] = None,
                 clock: Callable[[], float] = lambda: 0.0):
        self.queue = queue
        self.clock = clock
        self.executor = executor or (lambda item, mandate: "executed")
        self.calibration = calibration or (lambda item, reason: None)
        self.token = write_token or WriteToken()
        self._mandate_seq = 0
        self.conn = sqlite3.connect(db_path, isolation_level=None)
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS mandates (
                mandate_id TEXT PRIMARY KEY, row_id TEXT, scope TEXT,
                expiry_at REAL, created_at REAL, status TEXT);
            CREATE TABLE IF NOT EXISTS action_intents (
                row_id TEXT PRIMARY KEY, mandate_id TEXT, action TEXT,
                status TEXT,        -- pending | executed | failed
                attempts INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS audit_records (
                audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision TEXT, row_id TEXT, mandate_id TEXT, actor TEXT,
                evidence TEXT, at REAL);
            CREATE TABLE IF NOT EXISTS execution_log (
                idem_key TEXT PRIMARY KEY, status TEXT, attempts INTEGER,
                finished_at REAL);
            """
        )

    # -- helpers ---------------------------------------------------------

    def covering_mandate(self, item: QueueItem) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT mandate_id, scope, expiry_at, status FROM mandates "
            "WHERE row_id=? AND status='active'", (item.item_id,)).fetchone()
        if not row:
            return None
        return {"mandate_id": row[0], "scope": row[1], "expiry_at": row[2]}

    @staticmethod
    def _prefill(item: QueueItem, queue: Queue) -> tuple[str, float]:
        """Scope + expiry pre-filled from the row's context."""
        scope = item.proposed_action or item.claim
        expiry_hours = float(queue.policy["classes"][item.impact_class]["expiry_hours"])
        return scope, queue.clock() + expiry_hours * 3600.0

    # -- the three decisions ----------------------------------------------

    def decide(self, item_id: str, decision: str, reason: Optional[str] = None,
               actor: str = "Ray") -> dict:
        item = self.queue.get(item_id)
        if item is None:
            raise KeyError(f"no queue row {item_id}")
        if decision == "approve":
            return self._approve(item, actor)
        if decision == "dismiss":
            return self._dismiss(item, reason)
        if decision == "escalate":
            return self._escalate(item, actor)
        raise UnknownDecision(decision)

    # 1) approve — two phases ---------------------------------------------

    def _approve(self, item: QueueItem, actor: str) -> dict:
        steps = list(getattr(item, "steps", ()) or ())
        # idempotency: an already-executed intent for this row is never
        # re-executed by a repeat approve — exactly-once per decision
        if steps:
            pending = self.conn.execute(
                "SELECT COUNT(*) FROM action_intents WHERE mandate_id IN "
                "(SELECT mandate_id FROM mandates WHERE row_id=?) "
                "AND status!='executed'", (item.item_id,)).fetchone()[0]
            total = self.conn.execute(
                "SELECT COUNT(*) FROM action_intents WHERE mandate_id IN "
                "(SELECT mandate_id FROM mandates WHERE row_id=?)",
                (item.item_id,)).fetchone()[0]
            if total and pending == 0:
                return {"phase": "execution", "ok": True,
                        "note": "already executed", "row_state": item.state,
                        "subtree": True}
        else:
            intent = self.conn.execute(
                "SELECT status FROM action_intents WHERE row_id=?",
                (item.item_id,)).fetchone()
            if intent and intent[0] == "executed":
                return {"phase": "execution", "ok": True,
                        "note": "already executed", "row_state": item.state}
        mandate = self.covering_mandate(item)
        if mandate is None:
            # mandate-creation flow: scope + expiry pre-filled from the row
            scope, expiry_at = self._prefill(item, self.queue)
            self._mandate_seq += 1
            mandate_id = f"m-{item.item_id}-{self._mandate_seq}"
            # token check BEFORE any write so a 401/403 commits nothing
            self._authorized()
            try:
                self._create_transaction(mandate_id, item, scope, expiry_at,
                                         actor, steps)
            except Exception as exc:            # any exception mid-transaction
                try:
                    self.conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass   # transaction already closed (e.g. failure before BEGIN)
                # mandate, action intent(s), audit record ALL uncommitted;
                # row stays waiting-on-you
                return {"phase": "creation", "ok": False, "error": repr(exc),
                        "row_state": self.queue.get(item.item_id).state}
            mandate = {"mandate_id": mandate_id, "scope": scope,
                       "expiry_at": expiry_at}
        # phase (b): execution — separate step, bounded retry, idempotent
        if steps:
            return self._execute_subtree(item, mandate, steps)
        return self._execute(item, mandate)

    def _create_transaction(self, mandate_id: str, item: QueueItem, scope: str,
                            expiry_at: float, actor: str,
                            steps: Optional[list] = None) -> None:
        """ONE hub transaction: mandate + action intent(s) + audit record.

        For a multi-step brief (§2 Panel B) the SAME single transaction
        persists one action intent PER STEP — approving the brief commits the
        whole subtree, never a per-step prompt."""
        self.conn.execute("BEGIN IMMEDIATE")
        self.conn.execute(
            "INSERT INTO mandates VALUES (?,?,?,?,?,?)",
            (mandate_id, item.item_id, scope, expiry_at, self.clock(), "active"))
        if steps:
            for i, step in enumerate(steps):
                self.conn.execute(
                    "INSERT INTO action_intents VALUES (?,?,?, 'pending', 0)",
                    (f"{item.item_id}#s{i}", mandate_id, str(step)))
        else:
            self.conn.execute(
                "INSERT INTO action_intents VALUES (?,?,?, 'pending', 0)",
                (item.item_id, mandate_id, item.proposed_action))
        self.conn.execute(
            "INSERT INTO audit_records (decision, row_id, mandate_id, actor,"
            " evidence, at) VALUES ('approve-create', ?, ?, ?, ?, ?)",
            (item.item_id, mandate_id, actor, item.evidence_label, self.clock()))
        self.conn.execute("COMMIT")

    def _execute_subtree(self, item: QueueItem, mandate: dict,
                         steps: list) -> dict:
        """Execute the WHOLE subtree of a multi-step brief under one mandate:
        every per-step intent runs, and the commit is all-or-nothing — if any
        step fails, NO step intent is marked executed and the row returns to
        waiting-on-you (the plan gate is the single decision, §2/§7 Q5)."""
        mid = mandate["mandate_id"]
        rows = self.conn.execute(
            "SELECT row_id, action FROM action_intents WHERE mandate_id=? "
            "ORDER BY row_id", (mid,)).fetchall()
        if not rows:
            rows = [(f"{item.item_id}#s{i}", str(s)) for i, s in enumerate(steps)]
        logs = dict(self.conn.execute(
            "SELECT idem_key, status FROM execution_log WHERE idem_key IN "
            f"({','.join('?' * len(rows))})",
            [f"{rid}:{mid}" for rid, _ in rows]).fetchall())
        if logs and all(s == "executed" for s in logs.values()) \
                and len(logs) == len(rows):
            return {"phase": "execution", "ok": True, "note": "already executed",
                    "subtree": True, "steps": len(rows),
                    "row_state": self.queue.get(item.item_id).state}
        if any(s == "failed" for s in logs.values()):
            failed_attempts = max(
                self.conn.execute(
                    "SELECT attempts FROM execution_log WHERE idem_key=?",
                    (f"{rid}:{mid}",)).fetchone()[0]
                for rid, _ in rows)
            if failed_attempts >= MAX_EXECUTION_ATTEMPTS:
                raise NoFreshDecision(
                    f"{item.item_id} subtree failed {failed_attempts} attempts;"
                    " fresh decision required")
        attempts = (max([r[0] for r in self.conn.execute(
            "SELECT attempts FROM execution_log WHERE idem_key=?",
            (f"{rows[0][0]}:{mid}",)).fetchall()] or [0]) + 1)
        self._authorized()
        try:
            for i, (rid, action) in enumerate(rows):
                step_mandate = {**mandate, "step": i, "action": action,
                                "row_id": rid}
                self.executor(item, step_mandate)
        except Exception as exc:
            # nothing commits: every step intent stays unexecuted
            for rid, _ in rows:
                self.conn.execute(
                    "INSERT INTO execution_log (idem_key, status, attempts,"
                    " finished_at) VALUES (?,?,?,?) ON CONFLICT(idem_key) DO"
                    " UPDATE SET status='failed', attempts=excluded.attempts,"
                    " finished_at=excluded.finished_at",
                    (f"{rid}:{mid}", "failed", attempts, self.clock()))
                self.conn.execute(
                    "UPDATE action_intents SET status='failed', attempts=?"
                    " WHERE row_id=?", (attempts, rid))
            self.queue.set_state(item.item_id, "waiting-on-you")
            return {"phase": "execution", "ok": False, "error": repr(exc),
                    "subtree": True, "steps": len(rows), "attempts": attempts,
                    "row_state": self.queue.get(item.item_id).state}
        self.conn.execute("BEGIN IMMEDIATE")
        for rid, _ in rows:
            self.conn.execute(
                "INSERT INTO execution_log (idem_key, status, attempts,"
                " finished_at) VALUES (?,?,?,?) ON CONFLICT(idem_key) DO UPDATE"
                " SET status='executed', attempts=excluded.attempts,"
                " finished_at=excluded.finished_at",
                (f"{rid}:{mid}", "executed", attempts, self.clock()))
            self.conn.execute(
                "UPDATE action_intents SET status='executed', attempts=?"
                " WHERE row_id=?", (attempts, rid))
        self.conn.execute(
            "UPDATE mandates SET status='consumed' WHERE mandate_id=?", (mid,))
        self.conn.execute("COMMIT")
        self.queue.set_state(item.item_id, "done")
        return {"phase": "execution", "ok": True, "subtree": True,
                "steps": len(rows), "attempts": attempts, "row_state": "done"}

    def _execute(self, item: QueueItem, mandate: dict) -> dict:
        idem_key = f"{item.item_id}:{mandate['mandate_id']}"
        row = self.conn.execute(
            "SELECT status, attempts FROM execution_log WHERE idem_key=?",
            (idem_key,)).fetchone()
        if row and row[0] == "executed":
            return {"phase": "execution", "ok": True, "note": "already executed",
                    "attempts": row[1]}
        if row and row[0] == "failed" and row[1] >= MAX_EXECUTION_ATTEMPTS:
            # residual failure already recorded: intent NOT re-executed without
            # a fresh decision
            raise NoFreshDecision(
                f"{idem_key} failed {row[1]} attempts; fresh decision required")
        attempts = (row[1] if row else 0) + 1
        self._authorized()
        try:
            self.executor(item, mandate)
        except Exception as exc:
            self.conn.execute(
                "INSERT INTO execution_log (idem_key, status, attempts,"
                " finished_at) VALUES (?,?,?,?) "
                "ON CONFLICT(idem_key) DO UPDATE SET status='failed',"
                " attempts=excluded.attempts, finished_at=excluded.finished_at",
                (idem_key, "failed", attempts, self.clock()))
            self.conn.execute(
                "UPDATE action_intents SET status='failed', attempts=? "
                "WHERE row_id=?", (attempts, item.item_id))
            # row back to waiting-on-you; intent stays unexecuted
            self.queue.set_state(item.item_id, "waiting-on-you")
            return {"phase": "execution", "ok": False, "error": repr(exc),
                    "attempts": attempts,
                    "row_state": self.queue.get(item.item_id).state}
        self.conn.execute(
            "INSERT INTO execution_log (idem_key, status, attempts, finished_at)"
            " VALUES (?,?,?,?) ON CONFLICT(idem_key) DO UPDATE SET"
            " status='executed', attempts=excluded.attempts,"
            " finished_at=excluded.finished_at",
            (idem_key, "executed", attempts, self.clock()))
        self.conn.execute(
            "UPDATE action_intents SET status='executed', attempts=? "
            "WHERE row_id=?", (attempts, item.item_id))
        self.conn.execute(
            "UPDATE mandates SET status='consumed' WHERE mandate_id=?",
            (mandate["mandate_id"],))
        self.queue.set_state(item.item_id, "done")
        return {"phase": "execution", "ok": True, "attempts": attempts,
                "row_state": "done"}

    def _authorized(self) -> None:
        """Write-token gate with exactly one re-auth + retry on 401/403.
        Everything upstream of a successful authorize() is uncommitted."""
        try:
            self.token.authorize()
        except WriteAuthError:
            if self.token.auth_calls >= 1:
                raise                      # single re-auth already used
            self.token.re_authenticate()
            self.token.authorize()         # may raise → no partial write

    def re_execute(self, item_id: str) -> dict:
        """Explicit fresh decision: re-runs a failed intent once more."""
        item = self.queue.get(item_id)
        if item is None:
            raise KeyError(item_id)
        mandate = self.covering_mandate(item)
        if mandate is None:
            raise NoFreshDecision("no active mandate to execute under")
        return self._execute(item, mandate)

    # 2) dismiss — calibration hook (Q7) ------------------------------------

    def _dismiss(self, item: QueueItem, reason: Optional[str]) -> dict:
        self.queue.set_state(item.item_id, "dismissed")
        sample_reason = reason if reason else "unspecified"
        self.calibration(item, sample_reason)
        return {"decision": "dismiss", "calibration_reason": sample_reason}

    # 3) escalate — curator → lead → Ray → JEV ------------------------------

    def _escalate(self, item: QueueItem, actor: str) -> dict:
        start = ESCALATION_ROUTE.index(actor) if actor in ESCALATION_ROUTE else 0
        route = ESCALATION_ROUTE[start:]
        self.queue.set_state(item.item_id, "escalated")
        return {"decision": "escalate", "route": route,
                "row_state": "escalated"}

    def close(self) -> None:
        self.conn.close()
