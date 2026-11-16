"""Lane D: state-based notification engine + daily-brief assembly hook.

Implements operator-dashboard spec rev2 §4 (notification policy):

- N1: waiting-on-you (Panel A/B) and blocked>4h ping immediately. Pings are
  detected from the EVENT STREAM (hub `job-run-completed` / queue
  `item-created`), never from the 5-min status poll. Panel C policy items and
  all progress / stage-pass / shadow events are brief-only and never ping.
- N2: a single job failure that self-recovers (the SAME job succeeding) within
  15 min pings nothing — brief line only. A retry launching is not recovery.
- N4: three consecutive failures of the same job (each individually
  self-recovered) ping once on the third consecutive failure; this overrides
  N2 for that failure.
- N3: demotable/coalescable pings (SLA-breach, job-failure, mandate-expiry,
  lower-admission) are capped at ~5/day. Panel A/B waiting-on-you pings and
  blocked>4h pings are ADDITIVE on top of the cap. Over cap: same-project
  pings coalesce within one hour into a per-project digest; otherwise degrade
  in fixed order — SLA-breach (protected longest) → job-failure →
  mandate-expiry → lower-admission (demoted first). Every demotion is logged
  for the calibration loop.
- N5: mandate within 24h of expiry, or a policy item actively blocking work,
  pings exactly once.
- Expiry never pings (brief only). SLA-breach pings once per item — the cap
  may only demote that single ping, never schedule re-pings.
- Daily brief: the single digest; the dashboard emits at most immediate pings
  per the table + one daily brief.

Imports Lane A (exception_queue.queue) for queue state/policy and Lane B
(data_layer.job_runs) event shapes. All external effects go through injected
sinks so the engine is fully self-testable.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

try:  # Lane A import (policy classes / SLA thresholds / brief entries)
    from dashboard.exception_queue.queue import Queue, load_policy
    from dashboard import clock
except ImportError:  # running as a top-level module from build/
    from exception_queue.queue import Queue, load_policy  # type: ignore
    import sys as _sys
    import os as _os
    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(
        _os.path.abspath(__file__))))
    import dashboard.clock as clock  # type: ignore

# ---------------------------------------------------------------------------
# Constants from spec §4 / rev2 findings #6, #8, #10, #11
# ---------------------------------------------------------------------------

RECOVERY_WINDOW_S = 15 * 60          # N2: self-recovery window
CONSECUTIVE_FAILURES = 3             # N4
BLOCKED_PING_AFTER_S = 4 * 3600      # blocked > 4h
MANDATE_EXPIRY_WARN_S = 24 * 3600    # N5
COALESCE_WINDOW_S = 3600             # same-project coalescing, one hour
DAILY_CAP = 5                        # ~5 demotable pings/day

# Demotable kinds in DEGRADATION order: the FIRST entry is demoted LAST
# (protected longest); the LAST entry is demoted first.
DEMOTABLE_ORDER = ["sla-breach", "job-failure", "mandate-expiry", "lower-admission"]

# Kinds that are additive on top of the cap and never demoted/coalesced
# across projects (spec: cap applies ONLY to demotable/coalescable pings).
ADDITIVE_KINDS = {"waiting-on-you", "blocked-4h"}


@dataclass
class Ping:
    kind: str
    item_id: str
    project: str
    message: str
    ts: float
    demotable: bool
    demoted: bool = False
    coalesced: bool = False


@dataclass
class _JobState:
    streak: int = 0                     # consecutive failures (N4)
    last_failure_ts: Optional[float] = None
    recovered: bool = False             # success landed within window


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class NotificationEngine:
    """State-based notification engine. Feed it events from the hub event
    stream; it emits Pings via `ping_sink` and brief lines via `brief_sink`,
    maintains demotion log via `log_sink`, and assembles the single daily
    brief via `assemble_daily_brief()`."""

    def __init__(self, queue: Optional[Queue] = None, policy: Optional[dict] = None,
                 clock: Callable[[], float] = clock.now, db_path: str = ":memory:",
                 daily_cap: int = DAILY_CAP,
                 recovery_window_s: int = RECOVERY_WINDOW_S,
                 coalesce_window_s: int = COALESCE_WINDOW_S,
                 ping_sink: Callable[[Ping], None] = lambda p: None,
                 brief_sink: Callable[[str], None] = lambda s: None,
                 log_sink: Callable[[str], None] = lambda s: None) -> None:
        self.queue = queue
        self.policy = policy or (queue.policy if queue else load_policy())
        self.clock = clock
        self.daily_cap = daily_cap
        self.recovery_window_s = recovery_window_s
        self.coalesce_window_s = coalesce_window_s
        self.ping_sink = ping_sink
        self.brief_sink = brief_sink
        self.log_sink = log_sink
        self.conn = sqlite3.connect(db_path)
        self._init_schema()
        self.jobs: Dict[str, _JobState] = {}
        self.blocked_since: Dict[str, float] = {}
        self.brief_lines: List[str] = []
        self.last_brief_day: Optional[str] = None

    # -- schema --------------------------------------------------------------

    def _init_schema(self) -> None:
        c = self.conn
        c.execute("CREATE TABLE IF NOT EXISTS pings_emitted ("
                  "ts REAL, day TEXT, kind TEXT, item_id TEXT, project TEXT, "
                  "demoted INTEGER DEFAULT 0, coalesced INTEGER DEFAULT 0)")
        c.execute("CREATE TABLE IF NOT EXISTS demotions ("
                  "ts REAL, day TEXT, kind TEXT, item_id TEXT, reason TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS once_pings ("
                  "key TEXT PRIMARY KEY, kind TEXT, ts REAL)")
        c.execute("CREATE TABLE IF NOT EXISTS project_digests ("
                  "ts REAL, project TEXT, kinds TEXT)")
        c.commit()

    # -- helpers -------------------------------------------------------------

    def _day(self, ts: float) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(ts))

    def _demotable_count_today(self, day: str) -> int:
        r = self.conn.execute(
            "SELECT COUNT(*) FROM pings_emitted WHERE day=? AND demoted=0 "
            "AND coalesced=0 AND kind IN (?,?,?,?)",
            (day, *DEMOTABLE_ORDER)).fetchone()
        return int(r[0])

    def _once_key_used(self, key: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM once_pings WHERE key=?", (key,)).fetchone() is not None

    def _mark_once(self, key: str, kind: str, ts: float) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO once_pings VALUES (?,?,?)", (key, kind, ts))
        self.conn.commit()

    def _recent_same_project_demotable(self, project: str, ts: float) -> Optional[tuple]:
        r = self.conn.execute(
            "SELECT ts, kind FROM pings_emitted WHERE project=? AND demoted=0 "
            "AND coalesced=0 AND kind IN (?,?,?,?) AND ts>=? ORDER BY ts DESC",
            (project, *DEMOTABLE_ORDER, ts - self.coalesce_window_s)).fetchone()
        return (float(r[0]), r[1]) if r else None

    # -- core: emit a demotable ping with cap/coalesce/degrade (N3) ----------

    def _emit_demotable(self, kind: str, item_id: str, project: str,
                        message: str, ts: float) -> Optional[Ping]:
        day = self._day(ts)
        count = self._demotable_count_today(day)
        ping = Ping(kind, item_id, project, message, ts, demotable=True)
        if count < self.daily_cap:
            self._record_ping(ping, day)
            self.ping_sink(ping)
            return ping
        # Over cap, step 1: coalesce same-project demotable pings within an
        # hour into a per-project digest.
        recent = self._recent_same_project_demotable(project, ts)
        if recent is not None:
            ping.coalesced = True
            self.conn.execute(
                "INSERT INTO pings_emitted VALUES (?,?,?,?,?,0,1)",
                (ts, day, kind, item_id, project))
            self.conn.execute(
                "INSERT INTO project_digests VALUES (?,?,?)",
                (ts, project, f"{recent[1]}+{kind}"))
            self.conn.commit()
            self.log_sink(f"coalesced {kind} {item_id} into {project} digest")
            self.brief_sink(f"[{project} digest] {message}")
            self.ping_sink(ping)
            return ping
        # Step 2: degrade in fixed order. The candidate pool is the incoming
        # ping plus today's already-emitted demotable pings; demote the
        # lowest-priority kind present (lower-admission first, SLA-breach last).
        emitted_kinds = [r[0] for r in self.conn.execute(
            "SELECT kind FROM pings_emitted WHERE day=? AND demoted=0 AND "
            "coalesced=0 AND kind IN (?,?,?,?)",
            (day, *DEMOTABLE_ORDER)).fetchall()]
        pool = emitted_kinds + [kind]
        for victim in reversed(DEMOTABLE_ORDER):
            if victim in pool:
                break
        else:  # pragma: no cover - kind is always in DEMOTABLE_ORDER
            victim = kind
        if victim == kind:
            ping.demoted = True
            self._record_ping(ping, day)
            self.conn.execute(
                "INSERT INTO demotions VALUES (?,?,?,?,?)",
                (ts, day, kind, item_id,
                 f"demoted to brief (cap {self.daily_cap} reached, fixed "
                 f"degradation order)"))
            self.conn.commit()
            self.log_sink(f"demotion: {kind} {item_id} degraded to brief "
                          f"(cap {self.daily_cap} reached)")
            self.brief_sink(f"[demoted] {message}")
            self.ping_sink(ping)
        else:
            # Demote one already-emitted ping of the victim kind in favour of
            # the incoming (higher-priority) ping.
            row = self.conn.execute(
                "SELECT rowid FROM pings_emitted WHERE day=? AND kind=? AND "
                "demoted=0 AND coalesced=0 ORDER BY ts LIMIT 1",
                (day, victim)).fetchone()
            self.conn.execute(
                "UPDATE pings_emitted SET demoted=1 WHERE rowid=?", (row[0],))
            self.conn.execute(
                "INSERT INTO demotions VALUES (?,?,?,?,?)",
                (ts, day, victim, item_id,
                 f"demoted in favour of {kind} (fixed degradation order)"))
            self.conn.commit()
            self.log_sink(f"demotion: {victim} degraded to brief in favour of "
                          f"{kind} {item_id}")
            self._record_ping(ping, day)
            self.ping_sink(ping)
        return ping

    def _record_ping(self, ping: Ping, day: str) -> None:
        self.conn.execute(
            "INSERT INTO pings_emitted VALUES (?,?,?,?,?,?,?)",
            (ping.ts, day, ping.kind, ping.item_id, ping.project,
             1 if ping.demoted else 0, 1 if ping.coalesced else 0))
        self.conn.commit()

    def _emit_additive(self, kind: str, item_id: str, project: str,
                       message: str, ts: float, coalesce_key: str) -> Ping:
        """Additive ping: survives the cap, never demoted; same-project
        waiting-on-you pings coalesce within one hour into a per-project
        digest (never across projects)."""
        if kind == "waiting-on-you":
            recent = self.conn.execute(
                "SELECT ts FROM pings_emitted WHERE project=? AND kind=? AND "
                "coalesced=0 AND ts>=?",
                (project, kind, ts - self.coalesce_window_s)).fetchone()
            if recent is not None:
                ping = Ping(kind, item_id, project, message, ts,
                            demotable=False, coalesced=True)
                self.conn.execute(
                    "INSERT INTO pings_emitted VALUES (?,?,?,?,?,0,1)",
                    (ts, self._day(ts), kind, item_id, project))
                self.conn.commit()
                self.log_sink(f"coalesced {kind} {item_id} into {project} digest")
                self.brief_sink(f"[{project} digest] {message}")
                self.ping_sink(ping)
                return ping
        ping = Ping(kind, item_id, project, message, ts, demotable=False)
        self._record_ping(ping, self._day(ts))
        self.ping_sink(ping)
        return ping

    # -- event handling ------------------------------------------------------

    def handle_event(self, event: Dict[str, Any]) -> List[Ping]:
        """Process one event-stream event; returns pings emitted for it."""
        etype = event.get("type", "")
        ts = float(event.get("ts", self.clock()))
        handler = {
            "item-created": self._on_item_created,
            "job-run-completed": self._on_job_run_completed,
            "sla-breach": self._on_sla_breach,
            "mandate-expiry-near": self._on_mandate_expiry_near,
            "policy-blocking": self._on_policy_blocking,
            "lower-admission": self._on_lower_admission,
            "job-failure": self._on_job_failure_direct,
        }.get(etype)
        if handler is None:
            # Progress / stage-pass / shadow / done-since-last-visit / expiry
            # events are brief-only by policy — never ping.
            self.brief_sink(f"{etype or 'unknown'}: {event.get('summary', '')}"
                            .strip(": "))
            return []
        return handler(event, ts)

    # N1: waiting-on-you from the EVENT STREAM (item-created).
    def _on_item_created(self, event: Dict[str, Any], ts: float) -> List[Ping]:
        panel = event.get("panel", "C")
        project = event.get("project", "?")
        item_id = event.get("item_id", "?")
        if panel in ("A", "B"):
            ping = self._emit_additive(
                "waiting-on-you", item_id, project,
                f"waiting-on-you (Panel {panel}): {event.get('claim', item_id)}",
                ts, item_id)
            return [ping]
        # Panel C policy item: brief only (unless it actively blocks work —
        # that arrives as a separate `policy-blocking` event → N5 single ping).
        self.brief_sink(f"Panel C policy item: {event.get('claim', item_id)}")
        return []

    # N1/N2/N4: job-run-completed drives failure tracking from the stream.
    def _on_job_run_completed(self, event: Dict[str, Any], ts: float) -> List[Ping]:
        status = event.get("status", "done")
        project = event.get("project", "?")
        job = event.get("job_id", event.get("stage_type", "?"))
        key = f"{project}/{job}"
        if status == "failed":
            st = self.jobs.setdefault(key, _JobState())
            st.streak += 1
            st.last_failure_ts = ts
            st.recovered = False
            if st.streak >= CONSECUTIVE_FAILURES:
                # N4 overrides N2: ping once on the third consecutive failure.
                ping = self._emit_demotable(
                    "job-failure", key, project,
                    f"job {key} failed {CONSECUTIVE_FAILURES} consecutive times",
                    ts)
                self.jobs[key] = _JobState()  # streak resets after the ping
                return [ping]
            return []  # pending recovery window (N2); resolved below or by timeout
        if status == "success":
            st = self.jobs.get(key)
            if st and st.last_failure_ts is not None and not st.recovered:
                if ts - st.last_failure_ts <= self.recovery_window_s:
                    # N2: self-recovered — no ping, brief line only. The
                    # streak is NOT reset: three consecutive failures each
                    # individually recovered still trip N4.
                    st.recovered = True
                    self.brief_sink(
                        f"job {key} failed and self-recovered in "
                        f"{ts - st.last_failure_ts:.0f}s (no ping)")
                else:
                    self.brief_sink(f"job {key} recovered late")
                    self.jobs[key] = _JobState()  # fully healthy again
            else:
                self.jobs[key] = _JobState()
                self.brief_sink(f"{event.get('summary', 'done')}: {key}")
            return []
        if status == "blocked":
            return self._track_blocked(key, project, event, ts)
        # progress / stage-pass / shadow completions: brief only
        self.brief_sink(f"{event.get('summary', status)}: {key}")
        return []

    def check_pending_recoveries(self, now: Optional[float] = None) -> List[Ping]:
        """Time-driven N2 check: a failure whose recovery window expired
        without the SAME job succeeding pings (a retry launching is not
        recovery — that is exactly what this catches)."""
        now = self.clock() if now is None else now
        out = []
        for key, st in list(self.jobs.items()):
            if (st.last_failure_ts is not None and not st.recovered
                    and now - st.last_failure_ts >= self.recovery_window_s):
                project, job = key.split("/", 1)
                ping = self._emit_demotable(
                    "job-failure", key, project,
                    f"job {key} failed {self.recovery_window_s // 60} min ago "
                    f"without self-recovery", now)
                out.append(ping)
                self.jobs[key] = _JobState()
        return out

    # N1: blocked > 4h — tracked from the event stream, single ping.
    def _track_blocked(self, key: str, project: str,
                       event: Dict[str, Any], ts: float) -> List[Ping]:
        first = self.blocked_since.setdefault(key, ts)
        if ts - first >= BLOCKED_PING_AFTER_S and not self._once_key_used(
                f"blocked:{key}"):
            self._mark_once(f"blocked:{key}", "blocked-4h", ts)
            ping = self._emit_additive(
                "blocked-4h", key, project,
                f"blocked >4h: {event.get('summary', key)}", ts, key)
            return [ping]
        return []

    # SLA-breach: once per item (Lane A enforces once; engine also guards).
    def _on_sla_breach(self, event: Dict[str, Any], ts: float) -> List[Ping]:
        item_id = event.get("item_id", "?")
        if self._once_key_used(f"sla-breach:{item_id}"):
            return []
        self._mark_once(f"sla-breach:{item_id}", "sla-breach", ts)
        ping = self._emit_demotable(
            "sla-breach", item_id, event.get("project", "?"),
            f"SLA breach: {event.get('claim', item_id)}", ts)
        return [ping]

    # N5: mandate within 24h of expiry — single ping.
    def _on_mandate_expiry_near(self, event: Dict[str, Any], ts: float) -> List[Ping]:
        mandate_id = event.get("mandate_id", "?")
        if self._once_key_used(f"mandate-expiry:{mandate_id}"):
            return []
        self._mark_once(f"mandate-expiry:{mandate_id}", "mandate-expiry", ts)
        ping = self._emit_demotable(
            "mandate-expiry", mandate_id, event.get("project", "?"),
            f"mandate {mandate_id} expires within 24h", ts)
        return [ping]

    # N5: Panel C policy item actively blocking work — single ping.
    def _on_policy_blocking(self, event: Dict[str, Any], ts: float) -> List[Ping]:
        item_id = event.get("item_id", "?")
        if self._once_key_used(f"policy-blocking:{item_id}"):
            return []
        self._mark_once(f"policy-blocking:{item_id}", "policy-blocking", ts)
        # Not in the demotable list → additive single ping.
        ping = self._emit_additive(
            "policy-blocking", item_id, event.get("project", "?"),
            f"policy item actively blocking work: {event.get('claim', item_id)}",
            ts, item_id)
        return [ping]

    # lower-admission: a demotable ping kind (cap degradation order).
    def _on_lower_admission(self, event: Dict[str, Any], ts: float) -> List[Ping]:
        item_id = event.get("item_id", "?")
        ping = self._emit_demotable(
            "lower-admission", item_id, event.get("project", "?"),
            f"admitted to lower class: {event.get('claim', item_id)}", ts)
        return [ping]

    # A pre-resolved job-failure ping event (e.g. relayed by an upstream
    # watcher that already applied the N2/N4 rules).
    def _on_job_failure_direct(self, event: Dict[str, Any], ts: float) -> List[Ping]:
        item_id = event.get("job_id", event.get("item_id", "?"))
        ping = self._emit_demotable(
            "job-failure", item_id, event.get("project", "?"),
            f"job failure: {event.get('summary', item_id)}", ts)
        return [ping]

    # -- Lane A integration --------------------------------------------------

    def poll_queue(self, now: Optional[float] = None) -> List[Ping]:
        """Drive Lane A sweeps through the event path. SLA breaches come back
        as demotable once-per-item pings; expiry NEVER pings (brief only) —
        `sweep_expiry` itself writes brief entries and drops actions."""
        now = self.clock() if now is None else now
        out: List[Ping] = []
        if self.queue is None:
            return out
        for item_id in self.queue.check_sla_breach(now):
            item = self.queue.get(item_id)
            if item:
                out += self.handle_event({
                    "type": "sla-breach", "item_id": item_id,
                    "project": item.condition_key.split(":")[0],
                    "claim": item.claim, "ts": now})
        self.queue.sweep_expiry(now)  # brief-only by design, no ping path
        return out

    # -- daily brief (single digest) -----------------------------------------

    def assemble_daily_brief(self, now: Optional[float] = None) -> Dict[str, Any]:
        now = self.clock() if now is None else now
        day = self._day(now)
        if self.last_brief_day == day:
            return {"brief": None, "reason": "already assembled today"}
        self.last_brief_day = day
        queue_entries = (self.queue.daily_brief_entries()
                         if self.queue else [])
        pings_today = [dict(zip(
            ("ts", "kind", "item_id", "project", "demoted", "coalesced"), r))
            for r in self.conn.execute(
                "SELECT ts, kind, item_id, project, demoted, coalesced "
                "FROM pings_emitted WHERE day=? ORDER BY ts", (day,))]
        demotions = [dict(zip(("kind", "item_id", "reason"), r[1:]))
                     for r in self.conn.execute(
                         "SELECT ts, kind, item_id, reason FROM demotions "
                         "WHERE day=? ORDER BY ts", (day,))]
        digests = [dict(zip(("project", "kinds"), r[1:])) for r in
                   self.conn.execute(
                       "SELECT ts, project, kinds FROM project_digests "
                       "WHERE ts>=? ORDER BY ts",
                       (now - 24 * 3600,))]
        brief = {
            "day": day,
            "queue_entries": queue_entries,
            "brief_lines": list(self.brief_lines),
            "pings_emitted": pings_today,
            "demotions": demotions,
            "project_digests": digests,
        }
        self.brief_lines = []
        self.brief_sink(json.dumps({"daily_brief_assembled": day}))
        return {"brief": brief}

    def close(self) -> None:
        self.conn.close()
