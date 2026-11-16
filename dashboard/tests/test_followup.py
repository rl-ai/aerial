"""Failure-followup workflow (Lane F): classifier R1-R6, state machine D3,
ping policy D5. Hermetic — no network, no real queue, all effects injected.
"""
import sys
import os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard.followup import classifier as clf                      # noqa: E402
from dashboard.followup import ping_policy                            # noqa: E402
from dashboard.followup.flow import (FollowupFlow, MandateFlowAdapter,  # noqa: E402
                                     InvalidTransition)
from dashboard.followup.classifier import CuratorFinding              # noqa: E402


def f(fid="f1", claim="stale cache", reversible=True, scope="internal",
      covered=False, tags=()):
    return CuratorFinding(finding_id=fid, claim=claim, reversible=reversible,
                          scope=scope, covered_by_mandate=covered, tags=tags)


T0 = 1_000_000.0


class FakeMandateCreator:
    """Records creation calls; scoped + expiring mandates."""
    def __init__(self, fail=False, clock=lambda: T0):
        self.calls = []
        self.fail = fail
        self.clock = clock
        self._seq = 0

    def create(self, finding, scope):
        self.calls.append((finding.finding_id, scope))
        if self.fail:
            raise RuntimeError("mandate creation exploded")
        self._seq += 1
        return {"mandate_id": f"m-{self._seq}", "scope": scope,
                "expiry_at": self.clock() + 24 * 3600}


class Harness:
    def __init__(self, verifier=None, fix_executor=None, creator=None):
        self.creator = creator or FakeMandateCreator()
        self.fix_calls = []
        self.pings = []
        self.brief = []
        self.flow = FollowupFlow(
            clock=lambda: T0,
            mandate_creator=self.creator,
            fix_executor=fix_executor or (
                lambda finding, mandate: self.fix_calls.append(
                    (finding.finding_id, mandate)) or "fixed"),
            verifier=verifier or (lambda finding: True),
            notify=lambda cls, finding: ping_policy.route(
                cls, finding, ping_sink=self.pings.append,
                brief_sink=self.brief.append))


# ---------------------------------------------------------------------------
# R1-R6 classifier
# ---------------------------------------------------------------------------

def test_R1_security_breach_immediate():
    assert clf.classify(f(claim="auth bypass in token check")) == clf.SEV_SECURITY
    assert clf.classify(f(claim="harmless", tags=("secret-leak",))) == clf.SEV_SECURITY
    assert clf.classify(f(claim="validator fails open")) == clf.SEV_SECURITY
    assert clf.route_for(clf.SEV_SECURITY) == "immediate-ping"


def test_R2_sev_incident_immediate():
    assert clf.classify(f(claim="platform fully dead")) == clf.SEV_INCIDENT
    assert clf.classify(f(claim="ingest bug caused data loss")) == clf.SEV_INCIDENT
    assert clf.route_for(clf.SEV_INCIDENT) == "immediate-ping"


def test_R3_auto_fix_under_covering_mandate():
    assert clf.classify(f(covered=True)) == clf.AUTO_FIX


def test_R4_reversible_internal_no_mandate():
    assert clf.classify(f()) == clf.MANDATE


def test_R5_irreversible_or_external_daily_report():
    assert clf.classify(f(reversible=False)) == clf.DAILY_REPORT
    assert clf.classify(f(scope="external")) == clf.DAILY_REPORT
    assert clf.route_for(clf.DAILY_REPORT) == "daily-report"


def test_R6_fail_closed_on_unknown():
    # unknown scope value
    assert clf.classify(f(scope="weird")) == clf.UNKNOWN
    # unknown reversibility (None)
    assert clf.classify(f(reversible=None)) == clf.UNKNOWN
    # missing required fields
    assert clf.classify(CuratorFinding(finding_id="", claim="x",
                                       reversible=True, scope="internal")) == clf.UNKNOWN
    assert clf.classify(CuratorFinding(finding_id="f", claim="",
                                       reversible=True, scope="internal")) == clf.UNKNOWN
    # unknown NEVER auto-fixes or mandates, even with a covering mandate
    assert clf.route_for(clf.UNKNOWN) == "daily-report"
    assert clf.classify(f(scope=None, covered=True)) == clf.UNKNOWN


def test_R1_beats_R3_security_not_auto_fixed_even_with_mandate():
    # a security breach must never be classified auto-fix, mandate or not
    assert clf.classify(f(claim="secret leak in logs", covered=True)) == clf.SEV_SECURITY


# ---------------------------------------------------------------------------
# Ping policy (D5)
# ---------------------------------------------------------------------------

def test_ping_policy_immediate_only_for_sev_and_security():
    g = ping_policy.PingPolicyGuard()
    for cls in (clf.SEV_SECURITY, clf.SEV_INCIDENT):
        g(cls, f(fid=f"fid-{cls}", claim="boom"))
    assert len(g.pings) == 2
    assert all(p["kind"] == ping_policy.SEV_PING_KIND for p in g.pings)
    assert g.brief == []


def test_ping_policy_no_non_sev_immediate_pings():
    g = ping_policy.PingPolicyGuard()
    for cls in (clf.AUTO_FIX, clf.MANDATE, clf.DAILY_REPORT, clf.UNKNOWN):
        g(cls, f(fid=f"fid-{cls}", claim="ordinary"))
    assert g.pings == []                       # zero immediate pings
    assert len(g.brief) == 4                   # all aggregated to the daily report


def test_ping_policy_integrates_with_engine_ping_path():
    from dashboard.notifications.engine import NotificationEngine
    from dashboard.exception_queue.queue import Queue
    pings, brief = [], []
    engine = NotificationEngine(queue=Queue(db_path=":memory:"),
                                clock=lambda: T0,
                                ping_sink=pings.append, brief_sink=brief.append)
    r = ping_policy.route(clf.SEV_INCIDENT, f(fid="sev1", claim="data loss"),
                          engine=engine)
    assert r["routed"] == "immediate-ping" and len(pings) == 1
    assert pings[0].demotable is False         # additive: never demoted by cap
    r2 = ping_policy.route(clf.MANDATE, f(fid="m1", claim="stale cache"),
                           engine=engine)
    assert r2["routed"] == "daily-report" and len(pings) == 1
    assert any("stale cache" in b for b in brief)


# ---------------------------------------------------------------------------
# State machine (D3) — happy paths
# ---------------------------------------------------------------------------

def test_happy_path_mandate_class_full_lifecycle():
    h = Harness()
    res = h.flow.submit(f(fid="f1"))
    assert res["state"] == "closed" and res["silent"] is True
    assert res["fix_class"] == clf.MANDATE
    # mandate was created scoped + expiring via the _create path
    assert h.creator.calls == [("f1", "stale cache")]
    states = [t[1] for t in h.flow.history("f1")]
    assert states == ["classified", "mandate-created", "fix-attempted",
                      "verified", "closed"]
    # closed SILENTLY: no ping, no brief line for the closure
    assert h.pings == []


def test_auto_fix_runs_under_existing_mandate_no_creation():
    h = Harness()
    res = h.flow.submit(f(fid="f2", covered=True))
    assert res["state"] == "closed" and res["fix_class"] == clf.AUTO_FIX
    assert h.creator.calls == []               # no new mandate created
    assert len(h.fix_calls) == 1


def test_daily_report_class_escalates_without_fix():
    h = Harness()
    res = h.flow.submit(f(fid="f3", reversible=False))
    assert res["state"] == "escalated" and res["fix_class"] == clf.DAILY_REPORT
    assert h.fix_calls == [] and h.creator.calls == []
    assert h.pings == [] and any("daily-report" in b for b in h.brief)


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------

def test_mandate_creation_failure_escalates():
    h = Harness(creator=FakeMandateCreator(fail=True))
    res = h.flow.submit(f(fid="f4"))
    assert res["state"] == "escalated" and "error" in res
    states = [t[1] for t in h.flow.history("f4")]
    assert states == ["classified", "mandate-failed", "escalated"]
    assert h.fix_calls == []
    assert h.pings == []                       # non-SEV failure: no immediate ping


def test_fix_execution_failure_escalates():
    def boom(finding, mandate):
        raise ValueError("fix exploded")
    h = Harness(fix_executor=boom)
    res = h.flow.submit(f(fid="f5"))
    assert res["state"] == "escalated" and "fix exploded" in res["error"]
    assert [t[1] for t in h.flow.history("f5")][-2:] == ["fix-failed", "escalated"]


def test_verification_failure_escalates_not_closed():
    h = Harness(verifier=lambda finding: False)
    res = h.flow.submit(f(fid="f6"))
    assert res["state"] == "escalated" and res["reason"] == "verification failed"
    states = [t[1] for t in h.flow.history("f6")]
    assert "closed" not in states and "verified" in states


def test_unknown_class_fails_closed_escalates_never_fixes():
    h = Harness()
    res = h.flow.submit(f(fid="f7", scope="mystery"))
    assert res["state"] == "escalated" and res["fix_class"] == clf.UNKNOWN
    assert h.fix_calls == [] and h.creator.calls == []
    assert h.pings == [] and any("daily-report" in b for b in h.brief)


def test_sev_class_pings_immediately_and_escalates():
    h = Harness()
    res = h.flow.submit(f(fid="f8", claim="platform fully dead"))
    assert res["state"] == "escalated"
    assert len(h.pings) == 1 and h.pings[0]["kind"] == ping_policy.SEV_PING_KIND
    assert h.fix_calls == []


# ---------------------------------------------------------------------------
# State-machine guards + mandate adapter
# ---------------------------------------------------------------------------

def test_invalid_transition_rejected():
    h = Harness()
    with pytest.raises(InvalidTransition):
        # 'attempt' out of order: state is still 'finding' (never submitted)
        h.flow._transition("ghost", "attempt", "fix-attempted",
                           expect="mandate-created")


def test_mandate_adapter_uses_lane_a_create_path():
    """The adapter routes creation through MandateFlow._create_transaction."""
    from dashboard.exception_queue.queue import Queue, Finding
    from dashboard.exception_queue.mandate_flow import MandateFlow
    q = Queue(db_path=":memory:", clock=lambda: T0)
    mf = MandateFlow(q, clock=lambda: T0)
    item = q.admit(Finding(condition_key="ck1", claim="stale index",
                           impact_class="degraded", admission_score=0.9,
                           eval_passed=True, proposed_action="rebuild index"))
    adapter = MandateFlowAdapter(mf, clock=lambda: T0)
    mandate = adapter.create(item, scope="rebuild index")
    assert mandate["mandate_id"].startswith("mf-")
    assert mandate["expiry_at"] == T0 + 24 * 3600.0
    row = mf.conn.execute(
        "SELECT scope, expiry_at, status FROM mandates WHERE mandate_id=?",
        (mandate["mandate_id"],)).fetchone()
    assert row == ("rebuild index", T0 + 24 * 3600.0, "active")
