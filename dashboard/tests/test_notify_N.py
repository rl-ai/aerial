"""§7 Notification criteria N1–N5 (Lane D engine, event-stream fixtures)."""
from conftest import NotifHarness, T0
from dashboard.exception_queue.queue import Finding
from dashboard.notifications.engine import (
    BLOCKED_PING_AFTER_S, RECOVERY_WINDOW_S)


def test_N1_waiting_on_you_blocked_ping_progress_brief_only(harness):
    h = harness
    # waiting-on-you Panel A/B ping immediately from the event stream
    p = h.ev(type="item-created", panel="A", project="p1", item_id="i1", claim="c1")
    assert len(p) == 1 and p[0].kind == "waiting-on-you"
    # progress / stage-pass events never ping — they appear in the brief
    p = h.ev(type="job-run-completed", status="success", project="p1",
             job_id="j1", summary="stage pass")
    assert p == [] and any("stage pass" in b for b in h.brief)
    # Panel C policy items: brief only, no ping
    p = h.ev(type="item-created", panel="C", project="p1", item_id="i2",
             claim="policy")
    assert p == [] and any("policy" in b for b in h.brief)
    # blocked > 4h pings once, tracked from the event stream
    h.ev(type="job-run-completed", status="blocked", project="p2", job_id="j2")
    assert h.ev(type="job-run-completed", status="blocked", project="p2",
                job_id="j2") == []          # blocked < 4h: no ping yet
    h.advance(BLOCKED_PING_AFTER_S + 60)
    p = h.ev(type="job-run-completed", status="blocked", project="p2", job_id="j2")
    assert len(p) == 1 and p[0].kind == "blocked-4h"
    assert h.ev(type="job-run-completed", status="blocked", project="p2",
                job_id="j2") == []          # only once


def test_N2_single_failure_self_recovery_brief_line(harness):
    # a single job failure that self-recovers within 15 min produces no ping,
    # only a brief line (regression-lane vs. the real 05:15 incident)
    h = NotifHarness(harness.ts)
    h.ev(type="job-run-completed", status="failed", project="p3", job_id="j3")
    h.advance(90)
    h.ev(type="job-run-completed", status="success", project="p3", job_id="j3")
    assert h.pings == []
    assert any("self-recovered" in b for b in h.brief)
    # window expires without same-job success → ping
    h2 = NotifHarness(harness.ts)
    h2.ev(type="job-run-completed", status="failed", project="p4", job_id="j4")
    h2.advance(RECOVERY_WINDOW_S + 1)
    pings = h2.engine.check_pending_recoveries()
    assert len(pings) == 1 and pings[0].kind == "job-failure"
    # a retry launch (another failure) is NOT a recovery
    h3 = NotifHarness(harness.ts)
    h3.ev(type="job-run-completed", status="failed", project="p5", job_id="j5")
    h3.advance(60)
    h3.ev(type="job-run-completed", status="failed", project="p5", job_id="j5")
    h3.advance(RECOVERY_WINDOW_S)
    h3.engine.check_pending_recoveries()
    assert any(p.kind == "job-failure" for p in h3.pings)


def test_N4_three_consecutive_failures_ping_once_overrides_N2(harness):
    h = NotifHarness(harness.ts)
    # three consecutive failures of the same job, each individually "recovered"
    for _ in range(2):
        h.ev(type="job-run-completed", status="failed", project="p6", job_id="j6")
        h.advance(60)
        h.ev(type="job-run-completed", status="success", project="p6", job_id="j6")
        h.advance(60)
    assert h.pings == []          # two recovered failures: no ping yet
    h.ev(type="job-run-completed", status="failed", project="p6", job_id="j6")
    # ping ONCE on the third consecutive failure (overrides N2)
    assert len(h.pings) == 1 and h.pings[0].kind == "job-failure"
    assert "3 consecutive" in h.pings[0].message
    # streak resets after the ping: the next failure goes through N2 again
    h.ev(type="job-run-completed", status="success", project="p6", job_id="j6")
    h.ev(type="job-run-completed", status="failed", project="p6", job_id="j6")
    h.advance(RECOVERY_WINDOW_S + 1)
    h.engine.check_pending_recoveries()
    assert len(h.pings) == 2


def test_N3_noisy_day_cap_and_additive_AB_pings_survive(harness):
    # noisy-day fixture: 5 demotable pings fill the cap (~5/day)
    h = NotifHarness(harness.ts)
    for i in range(1, 6):
        h.ev(type="sla-breach", item_id=f"s{i}", project=f"pr{i % 3}",
             claim=f"sla {i}")
    assert sum(1 for p in h.pings if not p.demoted and not p.coalesced) == 5
    # 6th ping: same-project coalescing into a digest within the hour
    h.ev(type="job-failure", item_id="jf1", project="pr1")
    assert h.pings[-1].coalesced
    # 7th: no coalesce candidate → degrade in the documented fixed order
    # (lower-admission first, SLA-breach protected last), logged for calibration
    h.ev(type="lower-admission", item_id="la1", project="pr9")
    assert h.pings[-1].demoted and h.pings[-1].kind == "lower-admission"
    demoted = [d["kind"] for d in h.engine.assemble_daily_brief()["brief"]["demotions"]]
    assert "lower-admission" in demoted
    # SLA-breach survives the cap by demoting lower-admission
    h3 = NotifHarness(harness.ts)
    for i in range(1, 6):
        h3.ev(type="lower-admission", item_id=f"l{i}", project=f"x{i}")
    h3.ev(type="sla-breach", item_id="s9", project="x0", claim="breach")
    assert h3.pings[-1].kind == "sla-breach" and not h3.pings[-1].demoted
    # Panel A/B waiting-on-you pings are ADDITIVE on top of the cap and
    # survive all fixtures (7/7 emitted on a noisy day)
    h4 = NotifHarness(harness.ts)
    for i in range(1, 8):
        h4.ev(type="item-created", panel="B", project=f"y{i}",
              item_id=f"w{i}", claim=f"c{i}")
    assert sum(1 for p in h4.pings if p.kind == "waiting-on-you"
               and not p.coalesced) == 7
    # same-project A/B pings coalesce within an hour; cross-project never do
    h5 = NotifHarness(harness.ts)
    h5.ev(type="item-created", panel="A", project="z1", item_id="a1", claim="c")
    h5.ev(type="item-created", panel="A", project="z1", item_id="a2", claim="c")
    h5.ev(type="item-created", panel="A", project="z2", item_id="a3", claim="c")
    assert h5.pings[1].coalesced and not h5.pings[2].coalesced


def test_N5_mandate_expiry_and_policy_blocking_single_ping(harness):
    # a mandate within 24h of expiry pings exactly once (once-key guard)
    p1 = harness.ev(type="mandate-expiry-near", mandate_id="m1", project="p1")
    p2 = harness.ev(type="mandate-expiry-near", mandate_id="m1", project="p1")
    assert len(p1) == 1 and p1[0].kind == "mandate-expiry" and p2 == []
    # a policy item actively blocking work pings once
    p3 = harness.ev(type="policy-blocking", item_id="pb1", project="p1",
                    claim="blocking policy")
    p4 = harness.ev(type="policy-blocking", item_id="pb1", project="p1")
    assert len(p3) == 1 and p4 == []
