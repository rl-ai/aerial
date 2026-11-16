"""Now-list tests — hermetic (fixtures, mocked clock; report-design-v2.md §6)."""
import sys
import os
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import now_list as nl                                    # noqa: E402
from dashboard.exception_queue.queue import Finding, Queue, QueueItem   # noqa: E402

T0 = 1763217600.0   # 2025-11-15 14:00:00 UTC (fixed mock clock)
DAY = T0 - (T0 % 86400)


class FakeItem:
    """QueueItem stand-in with the fields item_from_queue reads."""

    def __init__(self, item_id="q-1", claim="accept/reject first autonomously-closed incident",
                 admission_score=0.8, state="waiting-on-you", created_at=None,
                 ray_gated=False, impact_class="blocking", evidence_label="verified",
                 proposed_action="close as duplicate", source_agent="curator",
                 steps=(), resolved_at=None, actor=""):
        self.item_id = item_id
        self.claim = claim
        self.admission_score = admission_score
        self.state = state
        self.created_at = T0 - 6 * 3600 if created_at is None else created_at
        self.ray_gated = ray_gated
        self.impact_class = impact_class
        self.evidence_label = evidence_label
        self.proposed_action = proposed_action
        self.source_agent = source_agent
        self.steps = steps
        self.resolved_at = resolved_at
        self.actor = actor


@pytest.fixture
def now():
    return T0


# ----------------------------------------------------------------- sourcing

def test_sources_ranked_and_capped_at_3(now):
    decisions = ["Approve default actor kind for hub API reads",
                 "Second decision", "Third decision", "Fourth decision"]
    items = [FakeItem(item_id=f"q-{i}", admission_score=0.9) for i in range(4)]
    r = nl.build_now_list(decisions, items, now=now)
    pending = r["items"]
    assert len(pending) == 3, "hard cap 3 pending items, ever"
    # a 4th candidate is never rendered (it stays in Zone B)
    assert all(i.id != "q-3" for i in pending) or len(
        [i for i in pending if i.kind == "queue-gate"]) <= 2
    # rank order: decisions (rank 1) before queue items (rank 2)
    kinds = [i.kind for i in pending]
    assert kinds[0] == "decision"
    assert kinds.count("decision") == 3, "decisions outrank queue items"


def test_ray_gated_item_admitted_through_queue_source(now):
    it = FakeItem(claim="teardown post venue+go [ray-gated]", admission_score=0.45,
                  ray_gated=True)
    r = nl.build_now_list(queue_items=[it], now=now)
    assert [i.id for i in r["items"]] == ["q-1"]
    assert r["items"][0].ray_gated is True


def test_never_list_below_gate_and_agent_handled(now):
    below = FakeItem(item_id="low", admission_score=0.49)     # below 0.5 gate
    working = FakeItem(item_id="wk", admission_score=0.9, state="working")
    r = nl.build_now_list(queue_items=[below, working], now=now)
    assert r["items"] == []
    assert r["zero"] is True


def test_zero_state_exact_string(now):
    r = nl.build_now_list(now=now)
    assert r["zero"] is True
    assert nl.ZERO_STATE == "Nothing needs you right now — the agents have it."
    html_out = nl.now_list_html(r)
    assert "Nothing needs you right now — the agents have it." in html_out


def test_excluded_ids_never_render(now):
    r = nl.build_now_list(queue_items=[FakeItem(item_id="handled")], now=now,
                          excluded_ids=("handled",))
    assert r["items"] == []


# ----------------------------------------------------------------- effort ladder

def test_effort_static_table_and_override(now):
    d = nl.item_from_decision("pick LICENSE", 0)
    assert d.effort == "quick-win"
    q = nl.item_from_queue(FakeItem(), now)
    assert q.effort == "medium"
    ob = nl.item_from_onboarding({"id": "ob-1", "title": "clean-venv onboarding test (30 min)",
                                  "effort": "medium"})
    assert ob.effort == "medium"
    deep = nl.item_from_onboarding({"id": "ob-2", "title": "big migration",
                                    "effort": "deep"})
    assert deep.effort == "deep", "deep only via declared record override"


def test_deep_never_auto_assigned(now):
    r = nl.build_now_list(decisions=["a", "b"], queue_items=[FakeItem()], now=now)
    assert all(i.effort != "deep" for i in r["items"])


def test_display_order_quick_win_first_ties_by_rank(now):
    q1 = FakeItem(item_id="q-a", admission_score=0.6)
    q2 = FakeItem(item_id="q-b", admission_score=0.9, created_at=T0 - 3600)
    r = nl.build_now_list(decisions=["decide x"], queue_items=[q1, q2], now=now)
    efforts = [i.effort for i in r["items"]]
    assert efforts == ["quick-win", "medium", "medium"]
    # tie within medium: higher admission score first
    mids = [i.id for i in r["items"] if i.effort == "medium"]
    assert mids == ["q-b", "q-a"]


# ----------------------------------------------------------------- focus mode

def test_focus_on_deep_top_item(now):
    deep = nl.item_from_onboarding({"id": "deep-1", "title": "migration", "effort": "deep"})
    r = nl.build_now_list(decisions=["quick decision"], onboarding_steps=[deep], now=now)
    assert r["focused"] is True and r["focus_reason"] == "a deep focus block is pending"
    assert len(r["shown"]) == 1 and r["shown"][0].id == "deep-1"
    assert len(r["hidden"]) == 1
    assert "+1 more · 1 quick-win" in nl.now_list_html(r), \
        "hidden items still visible as +N more · <effort mix>"


def test_focus_when_in_progress(now):
    items = [FakeItem(item_id="a"), FakeItem(item_id="b", state="in-progress")]
    r = nl.build_now_list(queue_items=items, now=now)
    assert r["focused"] is True
    assert r["shown"] == [i for i in r["items"] if i.id == "b"][0:1] or \
        r["shown"][0].id == "b", "the in-progress item is the list"


def test_no_focus_by_default(now):
    r = nl.build_now_list(decisions=["a"], queue_items=[FakeItem()], now=now)
    assert r["focused"] is False and len(r["shown"]) == 2


def test_manual_toggle_outranks(now):
    r = nl.build_now_list(decisions=["a", "b"], now=now, focus_toggle=True)
    assert r["focused"] is True and len(r["shown"]) == 1


# ----------------------------------------------------------------- states + tally

def test_states_and_crossed_off_rendering(now):
    done_agent = FakeItem(item_id="x", state="approved",
                          created_at=DAY - 3600, resolved_at=T0 - 1800)
    r = nl.build_now_list(queue_items=[done_agent], now=now)
    assert not r["items"]                       # done: not a pending row
    assert len(r["done_today"]) == 1
    it = r["done_today"][0]
    assert it.state == "done-by-agent"
    html_out = nl.now_list_html(r)
    assert "crossed off for you — the agent resolved it" in html_out
    assert "✓ done" in html_out


def test_tally_header_counts(now):
    pend = [FakeItem(item_id=f"p{i}", created_at=T0 - i * 3600) for i in range(1, 3)]
    done = FakeItem(item_id="d", state="done", created_at=DAY,
                    resolved_at=T0 - 600)
    r = nl.build_now_list(queue_items=pend + [done], now=now)
    t = r["tally"]
    assert t["closed_today"] == 1 and t["new"] == 2
    assert t["header"] == "NOW — 1 of 3 done today"


def test_all_clear_header(now):
    done = FakeItem(item_id="d", state="done", created_at=DAY, resolved_at=T0 - 60)
    r = nl.build_now_list(queue_items=[done], now=now)
    assert r["tally"]["header"] == "All clear ✓"


def test_carried_counts_done_before_today(now):
    yesterday_done = FakeItem(item_id="y", state="done",
                              created_at=DAY - 90000, resolved_at=DAY - 3600)
    r = nl.build_now_list(queue_items=[yesterday_done], now=now)
    assert r["tally"]["carried"] == 1 and r["tally"]["closed_today"] == 0


def test_aged_item_decays_one_rank(now):
    fresh_q = FakeItem(item_id="fresh", admission_score=0.6, created_at=T0 - 3600)
    stale_q = FakeItem(item_id="stale", admission_score=0.9, created_at=T0 - 80 * 3600)
    r = nl.build_now_list(queue_items=[fresh_q, stale_q], now=now)
    # stale item (rank 2 + 1 decay) loses to the fresh item despite higher score
    assert r["items"][0].id == "fresh"


# ----------------------------------------------------------------- detail payloads

def test_detail_payloads_fail_closed(now):
    d = nl.item_from_decision("Approve actor kind", 0,
                              options=[{"label": "A", "consequence": "runner stops failing"}],
                              evidence=[{"label": "failure cluster: roles-runner ×12"}])
    f = nl.detail_fields(d)
    assert "A → runner stops failing" in f["options"]
    assert "roles-runner" in f["evidence"]
    q = nl.item_from_queue(FakeItem(), now)
    fq = nl.detail_fields(q)
    assert "0.80" in fq["admission_score"]
    assert "gate 0.5" in fq["why_admitted"]
    # missing field → 'unavailable', never blank
    ob = nl.item_from_onboarding({"id": "ob"})
    assert nl.detail_fields(ob)["command"] == "unavailable"


def test_queue_options_are_approve_reject_escalate(now):
    q = nl.item_from_queue(FakeItem(), now)
    labels = [o["label"] for o in q.options]
    assert labels == ["Approve", "Reject", "Escalate"]


# ----------------------------------------------------------------- rendering

def test_html_panel_structure(now):
    r = nl.build_now_list(decisions=["decide a"], queue_items=[FakeItem()], now=now)
    h = nl.now_list_html(r)
    assert h.startswith('<section id="now-list" class="panel">')
    assert "NOW —" in h and "⚡" in h and "⏱" in h


def test_text_template(now):
    r = nl.build_now_list(decisions=["decide a"], now=now)
    t = nl.now_list_text(r)
    assert t.splitlines()[0].startswith("NOW —")
    assert "quick-win" in t


# ----------------------------------------------------------------- queue integration

def test_real_queue_items_flow_through(tmp_path, now):
    q = Queue(db_path=":memory:", clock=lambda: now)
    item = q.admit(Finding(condition_key="c1", claim="approve the incident closure",
                           impact_class="blocking", admission_score=0.8,
                           eval_passed=True))
    assert item is not None
    r = nl.build_now_list(queue_items=q.ordered(now), now=now)
    assert [i.title for i in r["items"]] == ["approve the incident closure"]
    assert r["items"][0].kind == "queue-gate"
