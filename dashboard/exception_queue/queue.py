"""Exception-queue backend: scoring, admission, and expiry (Lane A).

Implements operator-dashboard spec §2 / §7 Q1, Q2, Q6:
- score = 0.5*A + 0.3*U + 0.2*min(age_hours/48, 1.0), ties break oldest-first
- admission per queue-policy.yaml (below threshold or failing the Eval gate
  never enters the queue)
- expiry: past the class expiry threshold the item is removed, its proposed
  action is dropped+logged, it appears once in the daily-brief list, never
  pings; a recurrence of the same condition on a later gate cycle re-enters
  as a NEW item.

Policy is owned by queue-policy.yaml (editable); a minimal built-in YAML
subset parser is used only when PyYAML is unavailable.
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
POLICY_PATH = os.path.join(HERE, "queue-policy.yaml")


# ---------------------------------------------------------------- policy load

def _mini_yaml(text: str) -> dict:
    """Tiny loader for the flat nested-mapping subset used by queue-policy.yaml."""
    root: dict = {}
    stack = [(-1, root)]
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip() or line.strip() == "---":
            continue
        indent = len(line) - len(line.lstrip())
        key, _, val = line.strip().partition(":")
        val = val.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if val == "":
            node: dict = {}
            parent[key] = node
            stack.append((indent, node))
        else:
            parent[key] = _scalar(val)
    return root


def _scalar(v: str) -> Any:
    if v.startswith('"') and v.endswith('"'):
        return v[1:-1]
    try:
        return float(v) if "." in v else int(v)
    except ValueError:
        return v


def load_policy(path: str = POLICY_PATH) -> dict:
    try:
        import yaml  # type: ignore
        with open(path) as fh:
            return yaml.safe_load(fh)
    except ImportError:
        with open(path) as fh:
            return _mini_yaml(fh.read())


# ------------------------------------------------------------------- domain

@dataclass
class Finding:
    """A gate finding submitted for admission. `condition_key` identifies the
    underlying condition so a recurrence after expiry can be detected."""
    condition_key: str
    claim: str
    impact_class: str            # routine | degraded | blocking
    admission_score: float       # normalized 0-1 from the Eval gate
    eval_passed: bool            # Eval gate verdict
    evidence_label: str = "verified"   # verified | inferred | stale
    source_agent: str = "curator"
    proposed_action: str = ""
    steps: tuple = ()             # non-empty ⇒ multi-step brief (Panel B, §2)


@dataclass
class QueueItem:
    item_id: str
    condition_key: str
    claim: str
    impact_class: str
    admission_score: float
    evidence_label: str
    source_agent: str
    proposed_action: str
    created_at: float            # epoch seconds
    state: str = "waiting-on-you"
    breach_pinged: bool = False
    score: float = 0.0
    steps: tuple = ()            # per-step actions of a multi-step proposal


@dataclass
class ExpiryEvent:
    item: QueueItem
    dropped_action: str
    logged_at: float


def _to_hours(seconds: float) -> float:
    return seconds / 3600.0


def urgency_of(impact_class: str, policy: dict) -> float:
    return float(policy["urgency"][impact_class])


def score_item(item: QueueItem, now: float, policy: dict) -> float:
    """score = 0.5*A + 0.3*U + 0.2*min(age_hours/48, 1.0) (weights from policy)."""
    w = policy["scoring"]["weights"]
    age_hours = max(0.0, _to_hours(now - item.created_at))
    g = min(age_hours / float(policy["scoring"]["aging_cap_hours"]), 1.0)
    return (
        float(w["admission"]) * item.admission_score
        + float(w["urgency"]) * urgency_of(item.impact_class, policy)
        + float(w["aging"]) * g
    )


def _sla_breach_hours(item: QueueItem, policy: dict) -> float:
    return float(policy["classes"][item.impact_class]["sla_breach_hours"])


def _expiry_hours(item: QueueItem, policy: dict) -> float:
    return float(policy["classes"][item.impact_class]["expiry_hours"])


class Queue:
    """Queue store backed by SQLite (single-file, WAL-friendly). Owns
    admission, ordering, SLA-breach single-ping, and expiry."""

    def __init__(self, db_path: str = ":memory:", policy_path: str = POLICY_PATH,
                 clock: Callable[[], float] = time.time,
                 brief_sink: Optional[Callable[[str], None]] = None,
                 ping_sink: Optional[Callable[[str], None]] = None,
                 log_sink: Optional[Callable[[str], None]] = None):
        self.policy = load_policy(policy_path)
        self.clock = clock
        self.brief_sink = brief_sink or (lambda msg: None)
        self.ping_sink = ping_sink or (lambda msg: None)
        self.log_sink = log_sink or (lambda msg: None)
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()
        self._next_id = 1

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS queue_items (
                item_id TEXT PRIMARY KEY, condition_key TEXT, claim TEXT,
                impact_class TEXT, admission_score REAL, evidence_label TEXT,
                source_agent TEXT,                proposed_action TEXT, created_at REAL,
                state TEXT, breach_pinged INTEGER DEFAULT 0,
                steps TEXT NOT NULL DEFAULT '[]');
            CREATE TABLE IF NOT EXISTS expiry_log (
                log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id TEXT, condition_key TEXT, dropped_action TEXT,
                logged_at REAL);
            CREATE TABLE IF NOT EXISTS brief_entries (
                entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id TEXT, condition_key TEXT, entry TEXT, created_at REAL,
                UNIQUE(item_id));   -- appears once in the daily brief
            CREATE TABLE IF NOT EXISTS pings (
                item_id TEXT, kind TEXT, sent_at REAL,
                UNIQUE(item_id, kind));  -- one breach ping per item
            """
        )
        self.conn.commit()

    # -- admission (Q1) -----------------------------------------------------

    def admit(self, finding: Finding) -> Optional[QueueItem]:
        """A finding enters the queue only if it passed the Eval gate AND its
        normalized admission score is at/above the class threshold. Returns the
        new item, or None (finding never surfaces)."""
        cls = self.policy["classes"].get(finding.impact_class)
        if cls is None:
            raise ValueError(f"unknown impact class: {finding.impact_class}")
        if not finding.eval_passed:
            self.log_sink(f"rejected: eval-gate fail {finding.condition_key!r}")
            return None
        if finding.admission_score < float(cls["admission_threshold"]):
            self.log_sink(
                f"rejected: below admission threshold {finding.condition_key!r}")
            return None
        now = self.clock()
        item = QueueItem(
            item_id=f"q-{self._next_id}", condition_key=finding.condition_key,
            claim=finding.claim, impact_class=finding.impact_class,
            admission_score=finding.admission_score,
            evidence_label=finding.evidence_label,
            source_agent=finding.source_agent,
            proposed_action=finding.proposed_action, created_at=now)
        self._next_id += 1
        steps_json = json.dumps(list(finding.steps or ()))
        self.conn.execute(
            "INSERT INTO queue_items (item_id, condition_key, claim,"
            " impact_class, admission_score, evidence_label, source_agent,"
            " proposed_action, created_at, state, breach_pinged, steps)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,0,?)",
            (item.item_id, item.condition_key, item.claim, item.impact_class,
             item.admission_score, item.evidence_label, item.source_agent,
             item.proposed_action, item.created_at, item.state, steps_json))
        self.conn.commit()
        return item

    # -- ordering (Q2) ------------------------------------------------------

    def ordered(self, now: Optional[float] = None) -> list[QueueItem]:
        """Queue rows sorted by score desc; ties break oldest-first."""
        now = self.clock() if now is None else now
        rows = self.conn.execute(
            "SELECT * FROM queue_items ORDER BY created_at").fetchall()
        items = [self._row_to_item(r) for r in rows]
        for it in items:
            it.score = round(score_item(it, now, self.policy), 6)
        items.sort(key=lambda it: (-it.score, it.created_at))
        return items

    def _row_to_item(self, r) -> QueueItem:
        steps = tuple(json.loads(r[11])) if len(r) > 11 and r[11] else ()
        return QueueItem(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8],
                         r[9], breach_pinged=bool(r[10]), steps=steps)

    # -- SLA-breach single ping (§4) ---------------------------------------

    def check_sla_breach(self, now: Optional[float] = None) -> list[str]:
        """Ping exactly once per item when it crosses its class SLA-breach
        threshold; no scheduled re-pings."""
        now = self.clock() if now is None else now
        pinged = []
        for it in self.ordered(now):
            if it.breach_pinged:
                continue
            if _to_hours(now - it.created_at) >= _sla_breach_hours(it, self.policy):
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO pings VALUES (?,?,?)",
                    (it.item_id, "sla_breach", now))
                if cur.rowcount:
                    self.conn.execute(
                        "UPDATE queue_items SET breach_pinged=1 WHERE item_id=?",
                        (it.item_id,))
                    self.ping_sink(f"SLA breach: {it.item_id} {it.claim}")
                    pinged.append(it.item_id)
        self.conn.commit()
        return pinged

    # -- expiry (Q6) ---------------------------------------------------------

    def sweep_expiry(self, now: Optional[float] = None) -> list[ExpiryEvent]:
        """Remove items past their class expiry threshold: action dropped+logged,
        one brief entry, never a ping. The condition may recur later as a NEW
        item (recurrence is re-admitted via admit())."""
        now = self.clock() if now is None else now
        events = []
        for it in self.ordered(now):
            if _to_hours(now - it.created_at) < _expiry_hours(it, self.policy):
                continue
            self.conn.execute("DELETE FROM queue_items WHERE item_id=?",
                              (it.item_id,))
            self.conn.execute(
                "INSERT INTO expiry_log (item_id, condition_key, dropped_action,"
                " logged_at) VALUES (?,?,?,?)",
                (it.item_id, it.condition_key, it.proposed_action, now))
            self.conn.execute(
                "INSERT OR IGNORE INTO brief_entries (item_id, condition_key,"
                " entry, created_at) VALUES (?,?,?,?)",
                (it.item_id, it.condition_key,
                 f"expired: {it.claim} (action dropped)", now))
            self.log_sink(f"expired {it.item_id}: action dropped "
                          f"({it.proposed_action or 'no action'})")
            events.append(ExpiryEvent(it, it.proposed_action, now))
        self.conn.commit()
        for ev in events:
            self.brief_sink(f"expired: {ev.item.claim}")
        return events

    def daily_brief_entries(self) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT entry FROM brief_entries ORDER BY entry_id")]

    def get(self, item_id: str) -> Optional[QueueItem]:
        row = self.conn.execute(
            "SELECT * FROM queue_items WHERE item_id=?", (item_id,)).fetchone()
        return self._row_to_item(row) if row else None

    def set_state(self, item_id: str, state: str) -> None:
        self.conn.execute("UPDATE queue_items SET state=? WHERE item_id=?",
                          (state, item_id))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


# -------------------------------------------------------------- QA sampling (C1)

def qa_sample(queue: Queue, sample_rate: float = 0.25, seed: int = 0,
              items: Optional[Sequence[QueueItem]] = None) -> list[QueueItem]:
    """QA-sampling slice (spec §7 C1): a deterministic statistical slice of
    ADMITTED queue findings for annotation — never the full backlog while
    0 < sample_rate < 1 and more than one item is admitted.

    Sampling is seeded (reproducible slice across refresh cycles) and drawn
    from the queue's admission order. The sampled rows feed the reasoned-
    dismissal calibration half of C1 via MandateFlow.decide(..., 'dismiss').
    """
    if not 0.0 <= sample_rate <= 1.0:
        raise ValueError(f"sample_rate must be in [0,1], got {sample_rate}")
    if items is None:
        items = queue.ordered()
    admitted = [it for it in items if it.state not in ("dismissed", "done")]
    n = len(admitted)
    if n == 0:
        return []
    k = int(round(sample_rate * n))
    if 0 < sample_rate < 1:
        k = max(1, min(k, n - 1))   # a slice, never the whole backlog
    else:
        k = min(k, n)
    rng = random.Random(seed)
    return sorted(rng.sample(list(admitted), k),
                  key=lambda it: (-it.score, it.created_at))
