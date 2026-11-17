"""RC1 lane 2 acceptance tests — console-rc-ui.md §8 V4–V7 against the REAL
mandate_flow / queue backend (no faked transport on the happy path)."""

from pathlib import Path
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
# The REAL backend lives in the built tree (canonical import target); the
# local workspace copy of console/ is what we test.
BUILD_DASH = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, BUILD_DASH)          # .../build/dashboard (exception_queue)
LOCAL_DASH = os.path.abspath(os.path.join(HERE, ".."))   # local console copy
sys.path.insert(0, LOCAL_DASH)

from exception_queue.mandate_flow import (   # noqa: E402
    MandateFlow, NoFreshDecision, WriteAuthError, WriteToken)
from exception_queue.queue import Finding, Queue  # noqa: E402
from console import actions  # noqa: E402


class Clock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


def make_fixture(token=None, executor=None, calibration=None):
    clock = Clock()
    queue = Queue(":memory:", clock=clock)
    flow = MandateFlow(queue, ":memory:", executor=executor,
                       calibration=calibration, write_token=token,
                       clock=clock)
    item = queue.admit(Finding(
        condition_key="stale-tokens", claim="t1/t2 tokens stale",
        impact_class="degraded", admission_score=0.8, eval_passed=True,
        evidence_label="verified", source_agent="curator-1",
        proposed_action="rotate stale t1/t2 tokens"))
    ctx = {"queue": queue, "mandate_flow": flow}
    return queue, flow, item, ctx


# ---------------------------------------------------------------- V4 -------

def test_v4_confirm_panel_prefills_scope_and_expiry():
    queue, flow, item, ctx = make_fixture()
    panel = actions.get_confirmation(flow, item.item_id)
    assert panel["scope"] == "rotate stale t1/t2 tokens"
    # expiry = now + degraded class expiry_hours from queue-policy.yaml
    expiry_hours = float(queue.policy["classes"]["degraded"]["expiry_hours"])
    assert panel["expiry_at"] == pytest.approx(flow.clock() + expiry_hours * 3600)
    assert panel["new_mandate"] is True
    assert "audit record written" in panel["audit"]


def test_v4_approve_click_is_read_only_then_confirm_writes():
    """The confirm phase performs NO write; the commit phase is the only
    write path — spy counts mandate_flow writes."""
    queue, flow, item, ctx = make_fixture()
    executed = []
    flow.executor = lambda it, mandate: executed.append(it.item_id) or "executed"
    calls = []

    real_decide = flow.decide
    def spy_decide(item_id, decision, reason=None, actor="operator"):
        calls.append(decision)
        return real_decide(item_id, decision, reason=reason, actor=actor)
    flow.decide = spy_decide

    # phase 1: Approve click → confirmation data, zero writes
    r1 = actions.handle_queue_action(ctx, "approve", item.item_id,
                                     {"phase": "confirm"})
    assert r1["phase"] == "confirm"
    assert r1["prefill"]["scope"] == "rotate stale t1/t2 tokens"
    assert calls == [] and executed == []      # no write, no decide call

    # cancel → still zero writes
    r2 = actions.handle_queue_action(ctx, "cancel", item.item_id, {})
    assert r2["message"].startswith("cancelled")
    assert calls == [] and executed == []      # V4 spy: zero POSTs/writes on cancel

    # phase 2: Confirm approve → the single write
    r3 = actions.handle_queue_action(ctx, "approve", item.item_id,
                                     {"phase": "commit"})
    assert r3["status"] == "executed"
    assert calls == ["approve"] and executed == [item.item_id]
    assert queue.get(item.item_id).state == "done"


# ---------------------------------------------------------------- V5 -------

def test_v5_approve_success_message_and_row_transition():
    queue, flow, item, ctx = make_fixture(
        executor=lambda it, m: "executed")
    r = actions.handle_queue_action(ctx, "approve", item.item_id,
                                    {"phase": "commit"})
    assert r["status"] == "executed"
    assert r["message"] == "executed ✓ (attempts 1)"
    assert r["attempts"] == 1
    assert queue.get(item.item_id).state == "done"


def test_v5_write_token_401_two_step_error_row_stays_waiting():
    queue, flow, item, ctx = make_fixture(token=WriteToken(fail_times=2))
    r = actions.handle_queue_action(ctx, "approve", item.item_id,
                                    {"phase": "commit"})
    assert r["status"] == "error"
    assert r["steps"][0] == "write token rejected — re-authenticated once, retrying…"
    assert r["message"] == "not committed — nothing changed"
    # row remains waiting-on-you, no partial write
    assert queue.get(item.item_id).state == "waiting-on-you"
    assert flow.covering_mandate(item) is None
    assert r["actions_re_enabled"] is True
    # buttons re-enabled: a later successful approve works
    flow.token.fail_times = 0
    r2 = actions.handle_queue_action(ctx, "approve", item.item_id,
                                     {"phase": "commit"})
    assert r2["status"] == "executed"


# ---------------------------------------------------------------- V6 -------

def test_v6_execution_failure_intent_not_re_run_no_auto_retry():
    queue, flow, item, ctx = make_fixture()

    def bad_executor(it, mandate):
        raise RuntimeError("target refused")

    flow.executor = bad_executor
    r1 = actions.handle_queue_action(ctx, "approve", item.item_id,
                                     {"phase": "commit"})
    assert r1["status"] == "error"
    assert r1["message"] == ("executed 0 times · intent NOT re-run — "
                             "approve again for a fresh decision")
    assert r1["auto_retry"] is False
    assert queue.get(item.item_id).state == "waiting-on-you"

    # drain the bounded retries to reach residual failure → NoFreshDecision
    for _ in range(2):
        actions.handle_queue_action(ctx, "approve", item.item_id,
                                    {"phase": "commit"})
    r4 = actions.handle_queue_action(ctx, "approve", item.item_id,
                                     {"phase": "commit"})
    assert r4["status"] == "error"
    assert "intent NOT re-run" in r4["message"]
    assert r4["auto_retry"] is False


def test_v6_nofreshdecision_raised_directly():
    queue, flow, item, ctx = make_fixture()
    flow.decide = lambda *a, **k: (_ for _ in ()).throw(NoFreshDecision("x"))
    r = actions.handle_queue_action(ctx, "approve", item.item_id,
                                    {"phase": "commit"})
    assert r["status"] == "error" and "intent NOT re-run" in r["message"]
    assert r["auto_retry"] is False


# ---------------------------------------------------------------- V7 -------

def test_v7_blank_dismiss_reason_records_unspecified():
    reasons = []
    queue, flow, item, ctx = make_fixture(
        calibration=lambda it, reason: reasons.append(reason))
    r = actions.handle_queue_action(ctx, "dismiss", item.item_id, {})
    assert r["status"] == "dismissed"
    assert r["calibration_reason"] == "unspecified"
    assert reasons == ["unspecified"]
    # explicit whitespace-only reason also counts as unspecified
    r2 = actions.handle_queue_action(ctx, "dismiss", item.item_id,
                                     {"reason": "   "})
    assert r2["calibration_reason"] == "unspecified"
    # a real reason passes through
    r3 = actions.handle_queue_action(ctx, "dismiss", item.item_id,
                                     {"reason": "duplicate of q-1"})
    assert r3["calibration_reason"] == "duplicate of q-1"


def test_v7_escalate_shows_route_and_collapses_actions():
    queue, flow, item, ctx = make_fixture()
    r = actions.handle_queue_action(ctx, "escalate", item.item_id, {})
    assert r["status"] == "escalated"
    assert r["route"] == ["curator", "lead", "operator", "JEV"]
    assert r["message"] == "escalated → curator → lead → operator → JEV"
    assert queue.get(item.item_id).state == "escalated"   # actions collapsed
