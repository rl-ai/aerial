"""Lane A self-test: acceptance criteria 1-6 from task t_c7185693.

Run: python3 -m dashboard.exception_queue.selftest_lane_a   (from build/)
"""

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, BUILD)

from dashboard.exception_queue.queue import (          # noqa: E402
    Finding, Queue, score_item, load_policy)
from dashboard.exception_queue.mandate_flow import (   # noqa: E402
    MandateFlow, WriteToken, WriteAuthError, NoFreshDecision)

T0 = 1_000_000.0          # fixed epoch start
H = 3600.0
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))


def make_queue(now=T0, **kw):
    return Queue(":memory:", clock=lambda: now, **kw)


def finding(ck="c1", cls="degraded", a=0.8, eval_passed=True, action="act-1"):
    return Finding(condition_key=ck, claim=f"claim-{ck}", impact_class=cls,
                   admission_score=a, eval_passed=eval_passed, proposed_action=action)


# ---- Criterion 1: scoring + ordering (Q2) --------------------------------
def test_c1():
    now = T0
    q = make_queue(now=now)
    aged_weak = q.admit(finding("weak", "routine", a=0.35))       # enters (>=0.3)
    fresh_strong = q.admit(finding("strong", "blocking", a=0.95))
    at_cap = q.admit(finding("cap", "degraded", a=0.6))
    later = T0 + 48 * H                                           # cap reached
    order = [it.item_id for it in q.ordered(now=later)]
    scores = {it.item_id: it.score for it in q.ordered(now=later)}
    # expected: strong = .5*.95+.3*1.0+.2*1.0 = 0.975
    #           weak   = .5*.35+.3*0.2+.2*1.0 = 0.435
    #           cap    = .5*.6 +.3*0.5+.2*1.0 = 0.650
    check("C1 ordering strong > cap > weak", order == ["q-2", "q-3", "q-1"],
          str(order))
    check("C1 strong score 0.975", abs(scores["q-2"] - 0.975) < 1e-9,
          str(scores))
    check("C1 cap score 0.65 (48h cap)", abs(scores["q-3"] - 0.65) < 1e-9)
    check("C1 weak score 0.435", abs(scores["q-1"] - 0.435) < 1e-9)
    # tie-break oldest-first: two identical items
    q2 = make_queue(now=now)
    a1 = q2.admit(finding("t1", "degraded", a=0.8))
    q2.clock = lambda: T0 + 5 * H
    a2 = q2.admit(finding("t2", "degraded", a=0.8))
    # force equal scores by comparing at a moment where aging differs but cap not:
    # scores differ by aging; instead check tie at creation instant
    order2 = [it.item_id for it in q2.ordered(now=T0)]
    check("C2 tie-break oldest-first", order2 == [a1.item_id, a2.item_id],
          str(order2))


# ---- Criterion 2: admission classes (Q1) ----------------------------------
def test_c2():
    q = make_queue()
    pol = load_policy()
    check("C2 blocking thresholds", pol["classes"]["blocking"]["sla_breach_hours"] == 4
          and pol["classes"]["blocking"]["expiry_hours"] == 48)
    check("C2 degraded thresholds", pol["classes"]["degraded"]["sla_breach_hours"] == 24
          and pol["classes"]["degraded"]["expiry_hours"] == 168)
    check("C2 routine thresholds", pol["classes"]["routine"]["sla_breach_hours"] == 72
          and pol["classes"]["routine"]["expiry_hours"] == 336)
    below = q.admit(finding("low", "degraded", a=0.39))     # threshold 0.4
    evalfail = q.admit(finding("bad", "blocking", a=0.99, eval_passed=False))
    ok = q.admit(finding("ok", "routine", a=0.31))          # threshold 0.3
    check("C2 below-threshold absent", below is None)
    check("C2 eval-gate-fail absent", evalfail is None)
    check("C2 at-threshold admitted", ok is not None)


# ---- Criterion 3: expiry (Q6) ----------------------------------------------
def test_c3():
    pings, brief, logs = [], [], []
    q = make_queue(now=T0, ping_sink=lambda m: pings.append(m),
                   brief_sink=lambda m: brief.append(m),
                   log_sink=lambda m: logs.append(m))
    q.admit(finding("cond-x", "degraded", a=0.8, action="deploy-fix"))
    ev = q.sweep_expiry(now=T0 + 7 * 24 * H + 60)          # 7d expiry + 1min
    check("C3 removed from queue", q.get("q-1") is None and len(ev) == 1)
    check("C3 action dropped+logged",
          ev[0].dropped_action == "deploy-fix"
          and any("action dropped" in m for m in logs))
    check("C3 brief exactly once",
          q.daily_brief_entries().count(
              q.daily_brief_entries()[0]) == 1 and len(brief) == 1, str(brief))
    check("C3 no ping on expiry", pings == [], str(pings))
    # recurrence on a later gate cycle re-enters as a NEW item
    q.clock = lambda: T0 + 8 * 24 * H
    again = q.admit(finding("cond-x", "degraded", a=0.8))
    check("C3 recurrence re-enters as new item",
          again is not None and again.item_id != "q-1"
          and q.get(again.item_id) is not None)


# ---- Criterion 4: two-phase approve (Q3) ------------------------------------
def test_c4():
    executed = []

    def executor(item, mandate):
        executed.append((item.item_id, mandate["mandate_id"]))
        return "ok"

    # (a) creation failure → nothing committed, row stays waiting-on-you
    q = make_queue()
    q.admit(finding("c-a", "blocking", a=0.9, action="restart-svc"))
    fl = MandateFlow(q, clock=lambda: T0)

    def boom(*a, **k):
        raise RuntimeError("hub 500 mid-transaction")

    fl._create_transaction = boom
    res = fl.decide("q-1", "approve")
    check("C4a creation-fail row waiting-on-you",
          res["row_state"] == "waiting-on-you", str(res))
    n_mand = fl.conn.execute("SELECT COUNT(*) FROM mandates").fetchone()[0]
    n_int = fl.conn.execute("SELECT COUNT(*) FROM action_intents").fetchone()[0]
    n_aud = fl.conn.execute(
        "SELECT COUNT(*) FROM audit_records").fetchone()[0]
    check("C4a mandate/intent/audit all uncommitted",
          (n_mand, n_int, n_aud) == (0, 0, 0), f"{n_mand},{n_int},{n_aud}")
    check("C4a action unexecuted", executed == [])

    # (b) execution failure after creation commits
    q2 = make_queue()
    item_b = q2.admit(finding("c-b", "blocking", a=0.9, action="restart-svc"))
    bid = item_b.item_id
    fails = {"n": 0}

    def flaky(item, mandate):
        fails["n"] += 1
        raise IOError("execution transport down")

    mf = MandateFlow(q2, executor=flaky, clock=lambda: T0)
    res = mf.decide(bid, "approve")
    check("C4b exec-fail attempts=1 row waiting-on-you",
          res["ok"] is False and res["attempts"] == 1
          and res["row_state"] == "waiting-on-you", str(res))
    # bounded retry without fresh decision must be refused after max attempts
    blocked = None
    for _ in range(MAX_RETRY := 3):
        try:
            mf.re_execute(bid)
        except NoFreshDecision as e:
            blocked = e
            break
    check("C4b bounded retry then blocked without fresh decision",
          blocked is not None and fails["n"] <= 3, f"execs={fails['n']}")
    # fresh decision executes exactly once (same mandate row, new flow instance
    # sharing the same hub DB is out of scope here; a fresh decision re-approves)
    mf2 = MandateFlow(q2, executor=executor, clock=lambda: T0)
    r1 = mf2.decide(bid, "approve")
    r2 = mf2.decide(bid, "approve")       # replay of same approve is idempotent
    execs_b = [e for e in executed if e[0] == bid]
    check("C4b executed exactly once",
          r1["ok"] and r2.get("note") == "already executed"
          and len(execs_b) == 1, f"{execs_b} {r1} {r2}")

    # write-token 401/403: single re-auth, no partial write
    q3 = make_queue()
    item_t = q3.admit(finding("c-t", "blocking", a=0.9, action="restart-svc"))
    tid = item_t.item_id
    tok = WriteToken(fail_times=1, status=403)
    mf3 = MandateFlow(q3, executor=executor, write_token=tok, clock=lambda: T0)
    res = mf3.decide(tid, "approve")
    check("C4 token: one re-auth then success",
          res.get("ok") is True and tok.auth_calls == 1, str(res))
    tok2 = WriteToken(fail_times=5, status=403)
    q4 = make_queue()
    item_t2 = q4.admit(finding("c-t2", "blocking", a=0.9, action="x"))
    tid2 = item_t2.item_id
    mf4 = MandateFlow(q4, executor=executor, write_token=tok2, clock=lambda: T0)
    try:
        mf4.decide(tid2, "approve")
        raised = False
    except WriteAuthError:
        raised = True
    n_m = mf4.conn.execute("SELECT COUNT(*) FROM mandates").fetchone()[0]
    check("C4 token: persistent 403 → no partial write",
          raised and n_m == 0 and q4.get(tid2).state == "waiting-on-you")


# ---- Criterion 5: reason-less dismiss (Q7) ---------------------------------
def test_c5():
    samples = []
    q = make_queue()
    q.admit(finding("c-d", "degraded", a=0.8))
    mf = MandateFlow(q, calibration=lambda item, reason: samples.append(reason),
                     clock=lambda: T0)
    res = mf.decide("q-1", "dismiss")           # no reason
    check("C5 reason-less dismiss → 'unspecified'",
          samples == ["unspecified"] and res["calibration_reason"] == "unspecified",
          str(samples))
    q.admit(finding("c-d2", "degraded", a=0.8))
    mf.decide("q-2", "dismiss", reason="stale evidence")
    check("C5 reasoned dismiss keeps reason",
          samples == ["unspecified", "stale evidence"], str(samples))


# ---- Criterion 6: escalation route ------------------------------------------
def test_c6():
    q = make_queue()
    q.admit(finding("c-e", "blocking", a=0.9))
    mf = MandateFlow(q, clock=lambda: T0)
    res = mf.decide("q-1", "escalate", actor="curator")
    check("C6 route curator→lead→ operator →JEV",
          res["route"] == ["curator", "lead", "operator", "JEV"], str(res["route"]))
    res2 = mf.decide("q-1", "escalate", actor="operator")   # already escalated
    check("C6 route resumes from actor",
          res2["route"] == ["operator", "JEV"], str(res2["route"]))


if __name__ == "__main__":
    for fn in (test_c1, test_c2, test_c3, test_c4, test_c5, test_c6):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            check(f"{fn.__name__} crashed", False, repr(exc))
    failed = [(n, d) for n, ok, d in results if not ok]
    for n, ok, d in results:
        print(f"{'PASS' if ok else 'FAIL'}  {n}" + (f"  [{d}]" if d and not ok else ""))
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    sys.exit(1 if failed else 0)
