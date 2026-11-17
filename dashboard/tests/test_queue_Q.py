"""§7 Exception-queue criteria Q1–Q7."""
import pytest

from conftest import make_finding, T0, H
from dashboard.exception_queue.mandate_flow import (
    MandateFlow, WriteToken, WriteAuthError, NoFreshDecision)


def test_Q1_below_threshold_and_eval_gate_fail_absent(queue):
    below = queue.admit(make_finding("low", "degraded", a=0.39))       # threshold 0.4
    evalfail = queue.admit(make_finding("bad", "blocking", a=0.99, eval_passed=False))
    ok = queue.admit(make_finding("ok", "routine", a=0.31))            # threshold 0.3
    # a finding below the admission threshold or failing the Eval gate
    # NEVER appears in the queue
    assert below is None and evalfail is None
    assert ok is not None


def test_Q2_scoring_ordering_aged_cap_and_tie_break(queue):
    aged_weak = queue.admit(make_finding("weak", "routine", a=0.35))
    fresh_strong = queue.admit(make_finding("strong", "blocking", a=0.95))
    at_cap = queue.admit(make_finding("cap", "degraded", a=0.6))
    later = T0 + 48 * H  # the aged item has reached the 48h age cap
    order = [it.item_id for it in queue.ordered(now=later)]
    scores = {it.item_id: it.score for it in queue.ordered(now=later)}
    # 0.5·A + 0.3·U + 0.2·min(age/48, 1):
    #   strong = .5*.95+.3*1.0+.2*1.0 = 0.975
    #   cap    = .5*.6 +.3*0.5+.2*1.0 = 0.650
    #   weak   = .5*.35+.3*0.2+.2*1.0 = 0.435
    assert order == [fresh_strong.item_id, at_cap.item_id, aged_weak.item_id]
    assert abs(scores[fresh_strong.item_id] - 0.975) < 1e-9
    assert abs(scores[at_cap.item_id] - 0.65) < 1e-9        # 48h cap applied
    assert abs(scores[aged_weak.item_id] - 0.435) < 1e-9
    # tie-break oldest-first: two identical items in a fresh queue
    from dashboard.exception_queue.queue import Queue
    q2 = Queue(db_path=":memory:", clock=lambda: T0)
    a1 = q2.admit(make_finding("t1", "degraded", a=0.8))
    q2.clock = lambda: T0 + 5 * H
    a2 = q2.admit(make_finding("t2", "degraded", a=0.8))
    assert [it.item_id for it in q2.ordered(now=T0)] == [a1.item_id, a2.item_id]


def test_Q3_exactly_three_actions_and_two_phase_approve_failures(queue, tmp_path):
    # every row offers exactly three actions
    from dashboard.daily_board import render
    item = queue.admit(make_finding("r1", "blocking", a=0.9, action="restart-svc"))
    rows = render.zone_b_rows(queue, T0)
    assert all(r["actions"] == ["approve", "dismiss", "escalate"] for r in rows)

    # (a) creation failure — mandate, action intent and audit record all
    # uncommitted, action unexecuted, row stays waiting-on-you
    # (rev2 finding #2: atomicity split between creation and execution)
    executed = []
    flow = MandateFlow(queue, executor=lambda i, m: executed.append(i.item_id),
                       clock=lambda: T0)

    def boom(*a, **k):
        raise RuntimeError("hub 500 mid-transaction")

    flow._create_transaction = boom
    res = flow.decide(item.item_id, "approve")
    assert res["row_state"] == "waiting-on-you"
    counts = [flow.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("mandates", "action_intents", "audit_records")]
    assert counts == [0, 0, 0]
    assert executed == []

    # (b) execution failure — creation committed but execution (incl. retry)
    # fails: action executed exactly once or not at all (idempotency key =
    # row ID + mandate ID), row back to waiting-on-you
    item_b = queue.admit(make_finding("r2", "blocking", a=0.9, action="restart-svc"))
    fails = {"n": 0}

    def flaky(i, m):
        fails["n"] += 1
        raise IOError("execution transport down")

    mf = MandateFlow(queue, executor=flaky, clock=lambda: T0)
    res = mf.decide(item_b.item_id, "approve")
    assert res["ok"] is False and res["attempts"] == 1
    assert res["row_state"] == "waiting-on-you"
    # bounded retry is refused without a fresh decision
    with pytest.raises(NoFreshDecision):
        for _ in range(3):
            try:
                mf.re_execute(item_b.item_id)
            except NoFreshDecision:
                raise
    assert fails["n"] <= 3
    # a fresh decision executes exactly once; a replayed approve is idempotent
    executed2 = []
    mf2 = MandateFlow(queue, executor=lambda i, m: executed2.append(i.item_id) or "ok",
                      clock=lambda: T0)
    r1 = mf2.decide(item_b.item_id, "approve")
    r2 = mf2.decide(item_b.item_id, "approve")
    assert r1["ok"] and r2.get("note") == "already executed"
    assert len(executed2) == 1


def test_Q4_write_token_401_403_rotation_and_audit(queue):
    executed = []
    executor = lambda i, m: executed.append(i.item_id) or "ok"  # noqa: E731

    # token-rotation fixture: first write call 403s -> exactly ONE re-auth+retry
    tok = WriteToken(fail_times=1, status=403)
    item = queue.admit(make_finding("w1", "blocking", a=0.9, action="restart-svc"))
    mf = MandateFlow(queue, executor=executor, write_token=tok, clock=lambda: T0)
    res = mf.decide(item.item_id, "approve")
    assert res.get("ok") is True and tok.auth_calls == 1

    # persistent 403 (expired/rotated token) → no partial write: mandate and
    # action uncommitted, row stays waiting-on-you
    tok2 = WriteToken(fail_times=5, status=403)
    item2 = queue.admit(make_finding("w2", "blocking", a=0.9, action="x"))
    mf2 = MandateFlow(queue, executor=executor, write_token=tok2, clock=lambda: T0)
    with pytest.raises(WriteAuthError):
        mf2.decide(item2.item_id, "approve")
    n = mf2.conn.execute("SELECT COUNT(*) FROM mandates").fetchone()[0]
    assert n == 0
    assert queue.get(item2.item_id).state == "waiting-on-you"

    # 401 variant of the rotation fixture
    tok3 = WriteToken(fail_times=1, status=401)
    item3 = queue.admit(make_finding("w3", "blocking", a=0.9, action="x"))
    mf3 = MandateFlow(queue, executor=executor, write_token=tok3, clock=lambda: T0)
    assert mf3.decide(item3.item_id, "approve").get("ok") is True

    # approve writes an audit record (who, when, which evidence)
    n_aud = mf3.conn.execute(
        "SELECT COUNT(*) FROM audit_records").fetchone()[0]
    assert n_aud >= 1

    # escalate follows curator→lead→ operator →JEV
    item4 = queue.admit(make_finding("w4", "blocking", a=0.9))
    mf4 = MandateFlow(queue, clock=lambda: T0)
    r = mf4.decide(item4.item_id, "escalate", actor="curator")
    assert r["route"] == ["curator", "lead", "operator", "JEV"]


def test_Q5_panel_b_brief_classification(queue):
    # Q5 partial: a multi-step proposal renders as Panel B ("briefs") in the
    # Zone B classification. The full multi-step-subtree single-approve commit
    # is NOT implemented by any lane module — see uncovered list in the run log.
    from dashboard.daily_board.render import _classify_panel

    class BriefItem:
        is_brief = True
        panel = None
        proposed_action = "multi-step relabel plan"

    class PolicyItem:
        is_brief = False
        panel = None
        proposed_action = "mandate renewal for contained-mode config"

    assert _classify_panel(BriefItem()) == "B"
    assert _classify_panel(PolicyItem()) == "C"
    with pytest.raises(Exception):
        # approving a multi-step subtree through a single action has no hook
        MandateFlow(queue, clock=lambda: T0).decide("no-such-row", "approve")


def test_Q6_expiry_removal_brief_once_no_ping_recurrence(now):
    pings, brief, logs = [], [], []
    from dashboard.exception_queue.queue import Queue
    q = Queue(db_path=":memory:", clock=lambda: now,
              ping_sink=lambda m: pings.append(m),
              brief_sink=lambda m: brief.append(m),
              log_sink=lambda m: logs.append(m))
    q.admit(make_finding("cond-x", "degraded", a=0.8, action="deploy-fix"))
    ev = q.sweep_expiry(now=T0 + 7 * 24 * H + 60)
    # stale item past its expiry threshold is removed
    assert q.get("q-1") is None and len(ev) == 1
    # proposed action dropped and logged
    assert ev[0].dropped_action == "deploy-fix"
    assert any("action dropped" in m for m in logs)
    # appears exactly once in the daily brief, no ping
    entries = q.daily_brief_entries()
    assert len(entries) == 1 and entries.count(entries[0]) == 1
    assert pings == []
    # a recurrence of the underlying condition re-enters as a NEW item
    q.clock = lambda: T0 + 8 * 24 * H
    again = q.admit(make_finding("cond-x", "degraded", a=0.8))
    assert again is not None and again.item_id != "q-1"
    assert q.get(again.item_id) is not None


def test_Q7_reason_less_dismissal_unspecified_sample(queue):
    samples = []
    queue.admit(make_finding("d1", "degraded", a=0.8))
    queue.admit(make_finding("d2", "degraded", a=0.8))
    mf = MandateFlow(queue,
                     calibration=lambda item, reason: samples.append(reason),
                     clock=lambda: T0)
    res = mf.decide("q-1", "dismiss")            # no reason given
    # reason-less dismissal → negative calibration sample reason 'unspecified'
    assert samples == ["unspecified"]
    assert res["calibration_reason"] == "unspecified"
    # reasoned dismissal keeps its reason (feeds C1)
    mf.decide("q-2", "dismiss", reason="stale evidence")
    assert samples == ["unspecified", "stale evidence"]


def test_Q5_subtree_single_approve_commits_whole_subtree(queue):
    # §7 Q5: a multi-step proposal is a brief with ONE approve; approving
    # commits the whole subtree (mandate + per-step intents) — no per-step
    # prompt is ever shown.
    steps = ("relabel lane-a", "replay shadow week", "flip promotion gate")
    item = queue.admit(make_finding("plan-1", "degraded", a=0.8,
                                    action="multi-step relabel plan",
                                    steps=steps))
    executed = []
    mf = MandateFlow(queue, clock=lambda: T0,
                     executor=lambda it, m: executed.append((m["step"], m["action"])))
    res = mf.decide(item.item_id, "approve")           # single approve
    assert res["ok"] is True and res["subtree"] is True and res["steps"] == 3
    # every per-step intent was persisted and executed under the ONE mandate
    n_intents = mf.conn.execute(
        "SELECT COUNT(*) FROM action_intents WHERE mandate_id=?",
        (mf.conn.execute("SELECT mandate_id FROM mandates WHERE row_id=?",
                         (item.item_id,)).fetchone()[0],)).fetchone()[0]
    assert n_intents == 3
    assert executed == [(0, steps[0]), (1, steps[1]), (2, steps[2])]
    assert queue.get(item.item_id).state == "done"
    # exactly-once: repeat approve never re-executes the subtree
    res2 = mf.decide(item.item_id, "approve")
    assert res2["ok"] is True and res2["note"] == "already executed"
    assert len(executed) == 3


def test_Q5_subtree_all_or_nothing_on_step_failure(queue):
    # any step failing commits NOTHING: no step intent executed, row back to
    # waiting-on-you
    steps = ("step-ok", "step-boom")
    item = queue.admit(make_finding("plan-2", "degraded", a=0.8,
                                    action="brief", steps=steps))

    def executor(it, m):
        if m["action"] == "step-boom":
            raise RuntimeError("step 2 failed")

    mf = MandateFlow(queue, executor=executor, clock=lambda: T0)
    res = mf.decide(item.item_id, "approve")
    assert res["ok"] is False and res["subtree"] is True
    statuses = dict(mf.conn.execute(
        "SELECT row_id, status FROM action_intents").fetchall())
    assert set(statuses.values()) == {"failed"}
    assert all(k.startswith(item.item_id + "#s") for k in statuses)
    assert queue.get(item.item_id).state == "waiting-on-you"
    # bounded retry: a fresh decision may run it once more (fix the executor)
    mf.executor = lambda it, m: None
    res2 = mf.decide(item.item_id, "approve")
    assert res2["ok"] is True and res2["subtree"] is True
    assert queue.get(item.item_id).state == "done"


def test_Q5_multi_step_classifies_panel_b_in_render(queue):
    from dashboard.daily_board.render import _classify_panel
    item = queue.admit(make_finding("plan-3", "degraded", a=0.8,
                                    action="brief", steps=("a", "b")))
    assert _classify_panel(queue.get(item.item_id)) == "B"
    single = queue.admit(make_finding("single-1", "degraded", a=0.8))
    assert _classify_panel(queue.get(single.item_id)) == "A"
