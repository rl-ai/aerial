"""Lane D self-test: notification engine against spec §4 ACs N1–N7.

Run: cd /home/ai-eva/workspaces/build && python3 -m dashboard.notifications.selftest_lane_d
Exits 0 only if every check passes. Imports real Lane A (Queue) modules.
"""

from __future__ import annotations

import sys
import time

from dashboard.notifications.engine import (
    ADDITIVE_KINDS, BLOCKED_PING_AFTER_S, CONSECUTIVE_FAILURES,
    DEMOTABLE_ORDER, RECOVERY_WINDOW_S, NotificationEngine, Ping)
from dashboard.exception_queue.queue import Finding, Queue

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}: {name}" + (f" — {detail}" if detail else ""))


class Harness:
    def __init__(self, start_ts: float):
        self.ts = start_ts
        self.pings: list[Ping] = []
        self.brief: list[str] = []
        self.logs: list[str] = []
        self.queue = Queue(db_path=":memory:", clock=lambda: self.ts)
        self.engine = NotificationEngine(
            queue=self.queue, clock=lambda: self.ts,
            ping_sink=self.pings.append, brief_sink=self.brief.append,
            log_sink=self.logs.append)

    def advance(self, seconds: float) -> None:
        self.ts += seconds

    def ev(self, **kw) -> list[Ping]:
        kw.setdefault("ts", self.ts)
        return self.engine.handle_event(kw)


def make_queue_item(h: Harness, item_id: str, impact_class: str = "blocking",
                    age_hours: float = 0.0) -> str:
    h.queue._next_id = 100  # deterministic ids q-100, q-101, ...
    item = h.queue.admit(Finding(
        condition_key=f"proj-{item_id}:cond-{item_id}", claim=f"claim {item_id}",
        impact_class=impact_class, admission_score=0.9, eval_passed=True))
    if item and age_hours:
        # backdate created_at directly for SLA/expiry math
        h.queue.conn.execute("UPDATE queue_items SET created_at=? WHERE item_id=?",
                             (h.ts - age_hours * 3600, item.item_id))
        h.queue.conn.commit()
        return item.item_id
    return item.item_id if item else item_id


# ---------------------------------------------------------------- AC1 (N1)
def test_n1(h: Harness) -> None:
    # waiting-on-you Panel A/B pings immediately, from the event stream
    p = h.ev(type="item-created", panel="A", project="p1", item_id="i1",
             claim="c1")
    check("N1.A panel-A item-created pings immediately",
          len(p) == 1 and p[0].kind == "waiting-on-you")
    p = h.ev(type="job-run-completed", status="success", project="p1",
             job_id="j1", summary="stage pass")
    check("N1.B progress/stage-pass event never pings", p == [] and
          any("stage pass" in b for b in h.brief))
    # Panel C policy item: brief only
    p = h.ev(type="item-created", panel="C", project="p1", item_id="i2",
             claim="policy")
    check("N1.C Panel C policy item brief-only, no ping",
          p == [] and any("policy" in b for b in h.brief))
    # blocked > 4h pings (single), detected from event stream
    h.ev(type="job-run-completed", status="blocked", project="p2", job_id="j2")
    p = h.ev(type="job-run-completed", status="blocked", project="p2",
             job_id="j2")
    check("N1.D blocked<4h no ping yet", p == [])
    h.advance(BLOCKED_PING_AFTER_S + 60)
    p = h.ev(type="job-run-completed", status="blocked", project="p2",
             job_id="j2")
    check("N1.E blocked>4h pings once",
          len(p) == 1 and p[0].kind == "blocked-4h")
    p2 = h.ev(type="job-run-completed", status="blocked", project="p2",
              job_id="j2")
    check("N1.F blocked pings only once", p2 == [])


# ---------------------------------------------------------------- AC2 (N2)
def test_n2(h: Harness) -> None:
    h2 = Harness(h.ts)
    h2.ev(type="job-run-completed", status="failed", project="p3", job_id="j3")
    h2.advance(90)  # real incident self-recovered in 90 s
    h2.ev(type="job-run-completed", status="success", project="p3", job_id="j3")
    check("N2.A self-recovery within 15min -> no ping", h2.pings == [])
    check("N2.B self-recovery gets brief line",
          any("self-recovered" in b for b in h2.brief))
    # retry launching is NOT recovery: window expires without same-job success
    h3 = Harness(h.ts)
    h3.ev(type="job-run-completed", status="failed", project="p4", job_id="j4")
    h3.advance(RECOVERY_WINDOW_S + 1)
    pings = h3.engine.check_pending_recoveries()
    check("N2.C no recovery within 15min -> ping",
          len(pings) == 1 and pings[0].kind == "job-failure")
    # a retry (another failure) is not a success: still no recovery credit
    h4 = Harness(h.ts)
    h4.ev(type="job-run-completed", status="failed", project="p5", job_id="j5")
    h4.advance(60)
    h4.ev(type="job-run-completed", status="failed", project="p5", job_id="j5")
    check("N2.D retry launch is not recovery (no success event)",
          all(p.kind != "job-failure" for p in h4.pings))
    h4.advance(RECOVERY_WINDOW_S)
    h4.engine.check_pending_recoveries()
    check("N2.E unrecovered failure pings after window despite retry",
          any(p.kind == "job-failure" for p in h4.pings))


# ---------------------------------------------------------------- AC3 (N4)
def test_n4(h: Harness) -> None:
    h2 = Harness(h.ts)
    for _ in range(2):  # two consecutive failures, each individually recovered
        h2.ev(type="job-run-completed", status="failed", project="p6",
              job_id="j6")
        h2.advance(60)
        h2.ev(type="job-run-completed", status="success", project="p6",
              job_id="j6")
        h2.advance(60)
    check("N4.A two recovered failures: no ping yet", h2.pings == [])
    h2.ev(type="job-run-completed", status="failed", project="p6", job_id="j6")
    check("N4.B third consecutive failure pings once (overrides N2)",
          len(h2.pings) == 1 and h2.pings[0].kind == "job-failure"
          and "3 consecutive" in h2.pings[0].message)
    # streak resets after the ping
    h2.ev(type="job-run-completed", status="success", project="p6", job_id="j6")
    h2.ev(type="job-run-completed", status="failed", project="p6", job_id="j6")
    h2.advance(RECOVERY_WINDOW_S + 1)
    h2.engine.check_pending_recoveries()
    n_after = len(h2.pings)
    check("N4.C streak reset after N4 ping (next failure goes via N2)",
          n_after == 2)  # N4 ping + N2 window-expiry ping only


# ---------------------------------------------------------------- AC4 (N3)
def test_n3(h: Harness) -> None:
    h2 = Harness(h.ts)
    # 5 demotable pings fill the cap
    for i in range(1, 6):
        h2.ev(type="sla-breach", item_id=f"s{i}", project=f"pr{i % 3}",
              claim=f"sla {i}")
    check("N3.A cap 5 demotable pings emitted",
          sum(1 for p in h2.pings if not p.demoted and not p.coalesced) == 5)
    # 6th: same-project coalescing wins if a recent same-project ping exists
    h2.ev(type="job-failure", item_id="jf1", project="pr1")
    check("N3.B over cap: same-project coalesce into digest",
          h2.pings[-1].coalesced)
    # 7th: different project, no coalesce candidate -> degrade fixed order
    h2.ev(type="lower-admission", item_id="la1", project="pr9")
    check("N3.C lower-admission demoted first (fixed order)",
          h2.pings[-1].demoted and h2.pings[-1].kind == "lower-admission")
    demoted_kinds = [d["kind"] for d in
                     h2.engine.assemble_daily_brief()["brief"]["demotions"]]
    check("N3.D demotion logged for calibration loop", "lower-admission" in demoted_kinds)
    # SLA-breach is protected: when at cap it demotes lower-priority pings
    h3 = Harness(h.ts)
    for i in range(1, 6):
        h3.ev(type="lower-admission", item_id=f"l{i}", project=f"x{i}")
    h3.ev(type="sla-breach", item_id="s9", project="x0", claim="breach")
    check("N3.E SLA-breach survives cap by demoting lower-admission",
          h3.pings[-1].kind == "sla-breach" and not h3.pings[-1].demoted)
    # additive kinds unaffected by cap
    h4 = Harness(h.ts)
    for i in range(1, 8):
        h4.ev(type="item-created", panel="B", project=f"y{i}", item_id=f"w{i}",
              claim=f"c{i}")
    check("N3.F waiting-on-you pings additive on top of cap (7 emitted)",
          sum(1 for p in h4.pings if p.kind == "waiting-on-you"
              and not p.coalesced) == 7)
    # same-project A/B coalesce within one hour; cross-project never coalesce
    h5 = Harness(h.ts)
    h5.ev(type="item-created", panel="A", project="z1", item_id="a1", claim="c")
    h5.ev(type="item-created", panel="A", project="z1", item_id="a2", claim="c")
    h5.ev(type="item-created", panel="A", project="z2", item_id="a3", claim="c")
    check("N3.G same-project A/B coalesce, cross-project do not",
          h5.pings[1].coalesced and not h5.pings[2].coalesced)


# ---------------------------------------------------------------- AC5 (N5)
def test_n5(h: Harness) -> None:
    p1 = h.ev(type="mandate-expiry-near", mandate_id="m1", project="p1")
    p2 = h.ev(type="mandate-expiry-near", mandate_id="m1", project="p1")
    check("N5.A mandate <24h expiry single ping",
          len(p1) == 1 and p2 == [] and p1[0].kind == "mandate-expiry")
    p3 = h.ev(type="policy-blocking", item_id="pb1", project="p1",
              claim="blocking policy")
    p4 = h.ev(type="policy-blocking", item_id="pb1", project="p1")
    check("N5.B policy item actively blocking -> single ping",
          len(p3) == 1 and p4 == [])


# ---------------------------------------------------------------- AC6
def test_n6(h: Harness) -> None:
    # expiry never pings: Lane A sweep via engine.poll_queue
    h2 = Harness(h.ts)
    make_queue_item(h2, "e1", "routine", age_hours=73)  # past 72h SLA
    h2.engine.poll_queue()
    n_breach = len([p for p in h2.pings if p.kind == "sla-breach"])
    # advance past the 14-day routine expiry threshold
    h2.advance((336 - 73) * 3600)
    pings = h2.engine.poll_queue()
    n_after = len(h2.pings)
    check("N6.A expiry produces no ping",
          n_breach == 1 and pings == [] and n_after == 1)
    check("N6.B expiry gets brief entry",
          any("expired" in e for e in h2.queue.daily_brief_entries()))
    # SLA-breach once per item, no scheduled re-pings
    make_queue_item(h2, "s1", "blocking", age_hours=5)
    h2.engine.poll_queue()
    n1 = len([p for p in h2.pings if p.kind == "sla-breach"])
    h2.engine.poll_queue()
    h2.engine.poll_queue()
    n2 = len([p for p in h2.pings if p.kind == "sla-breach"])
    check("N6.C SLA-breach pings exactly once per item", n1 == 1 and n2 == 1)


# ---------------------------------------------------------------- AC7
def test_n7(h: Harness) -> None:
    h2 = Harness(h.ts)
    h2.ev(type="item-created", panel="A", project="p", item_id="i", claim="c")
    h2.ev(type="job-run-completed", status="success", project="p", job_id="j",
          summary="stage 2 passed")
    result = h2.engine.assemble_daily_brief()
    brief = result["brief"]
    check("N7.A brief is a single digest with all sections",
          brief is not None and "queue_entries" in brief
          and "pings_emitted" in brief and "demotions" in brief)
    again = h2.engine.assemble_daily_brief()
    check("N7.B only one daily brief per day", again["brief"] is None)


def main() -> int:
    start = time.time()
    h = Harness(start)
    for fn in (test_n1, test_n2, test_n4, test_n3, test_n5, test_n6, test_n7):
        try:
            fn(h)
        except Exception as exc:  # real exit code on any failure
            check(fn.__name__ + " crashed", False, repr(exc))
    failed = [c for c in CHECKS if not c[1]]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks pass")
    for name, _, detail in failed:
        print(f"  FAILED: {name} {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
