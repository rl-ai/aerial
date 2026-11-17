"""RC1 lane 1 self-tests: template.py against spec §8 V1, V2, V3, V8, V10, V13."""

import re
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dashboard.console.template import (  # noqa: E402
    FALLBACK_NOTICE, PANEL_ORDER, READONLY_BANNER_TEXT, STALE_TOOLTIP,
    render_console_html,
)

NOW = 1794472800.0  # 2026-11-12 10:00 UTC-ish; timestamps are only rendered as HH:MM


def board(live=True):
    """A full, live board dict in the exact shape render_board() produces."""
    return {
        "generated_at": NOW,
        "runs_state": "live",
        "zone_a": [
            {"project": "proj-1", "item": "item-1", "status": "waiting-on-you",
             "pct_complete": 40.0, "eta": "2-4h", "eta_insufficient": False,
             "tokens": "12 calls / 3400 tok", "untagged_bucket": "3 calls / 900 tok",
             "evidence_artifact": "art-1", "expanded": True,
             "collapsed_line": "proj-1 — waiting-on-you — 40% — 2-4h",
             "detail": {"in_progress_milestones": ["m1"], "open_asks": ["ask-1"],
                        "blockers": []},
             "done_label": None},
            {"project": "proj-2", "item": "item-2", "status": "done-since-last-visit",
             "pct_complete": 100.0, "eta": "n/a (no open stages)",
             "eta_insufficient": False, "tokens": "1 calls / 200 tok",
             "untagged_bucket": None, "evidence_artifact": None, "expanded": False,
             "collapsed_line": "proj-2 — done-since-last-visit — 100%",
             "detail": {"in_progress_milestones": None, "open_asks": None,
                        "blockers": None},
             "done_label": "done since 2026-11-11 18:00"},
        ],
        "zone_b": [
            {"item_id": "q-1", "panel": "A", "claim": "stale tokens t1/t2",
             "evidence_label": "verified",
             "source": "curator-1 (role: curator)",
             "proposed_action": "rotate stale t1/t2 tokens",
             "mandate": {"mandate_id": "m-000043", "scope": "rotate tokens (t1,t2)",
                         "expiry_at": "2026-11-14 09:00"},
             "evidence_artifact": "diff:tokens-t1t2", "age": "waiting 26h",
             "admission_score": 0.81, "score": 0.81,
             "actions": ["approve", "dismiss", "escalate"],
             "impact_class": "degraded", "age_hours": 26.0},
            {"item_id": "q-2", "panel": "B", "claim": "brief: cost drift",
             "evidence_label": "inferred", "source": "watcher (role: watcher)",
             "proposed_action": "review spend", "mandate": None,
             "evidence_artifact": None, "age": "waiting 45m",
             "admission_score": 0.55, "score": 0.55,
             "actions": ["approve", "dismiss", "escalate"],
             "impact_class": "routine", "age_hours": 0.75},
            {"item_id": "q-3", "panel": "C", "claim": "policy: widen scope",
             "evidence_label": "stale", "source": "curator-2 (role: curator)",
             "proposed_action": "update policy scope", "mandate": None,
             "evidence_artifact": "diff:policy", "age": "waiting 80h",
             "admission_score": 0.44, "score": 0.44,
             "actions": ["approve", "dismiss", "escalate"],
             "impact_class": "blocking", "age_hours": 80.0},
        ],
        "zone_c": {"mandates_ledger": [
            {"mandate_id": "m-000043", "scope": "rotate tokens (t1,t2)",
             "expiry_at": "2026-11-14 09:00"}],
            "shadow_pulse": {"would_have_done_today": 2,
                             "divergence_note": "1 auto-eligible",
                             "daily_comparison_link": "compare.html"}},
        "brief": ["DONE: proj-2 — done since 2026-11-11 18:00",
                  "POLICY: policy: widen scope (waiting 80h)"],
    }


def live_board():
    b = board()
    b["exec_summary"] = {"headlines": ["h1", "h2", "h3"],
                         "numbers": ["4.2% fail", "$1.20 spend"],
                         "decisions": ["d1"], "risks": ["r1"]}
    b["themes"] = [{"name": "auth", "current": 7, "prev": 5},
                   {"name": "billing", "current": 3, "prev": 3}]
    b["stack_overlap"] = "12%"
    return b


# ---------------------------------------------------------------- V1: layout

def test_v1_panel_order():
    html = render_console_html(live_board(), interactive=True)
    pos = [html.index(f'id="{pid}"') for pid in PANEL_ORDER]
    assert pos == sorted(pos), "panel order must follow spec §1"


def test_v1_zone_b_left_borders_match_panel():
    html = render_console_html(live_board(), interactive=True)
    for panel in ("a", "b", "c"):
        assert f"queue-row panel-{panel}" in html
    # each row's border class must match its panel value
    rows = re.findall(r"queue-row panel-(\w)", html)
    assert rows == ["a", "b", "c"]


# ------------------------------------------------------------- V2: state chips

def test_v2_chips_live_with_read_timestamp():
    html = render_console_html(live_board(), interactive=True)
    for zone in "ABC":
        assert re.search(rf'chip chip-live" data-zone="{zone}">{zone} live · read \d\d:\d\d', html)


def test_v2_stale_zone_chip_banner_and_disabled_buttons():
    b = live_board()
    b["zone_b_state"] = "stale (read 2026-11-12 09:42)"
    html = render_console_html(b, interactive=True)
    assert re.search(r'chip chip-stale" data-zone="B">B stale · read 2026-11-12 09:42', html)
    assert "B: stale (read 2026-11-12 09:42)" in html  # in-panel banner still renders
    zone_b = html[html.index('id="zone-b"'):html.index('id="zone-a"')]
    assert zone_b.count('disabled title="stale snapshot — refresh before deciding"') == 9


def test_v2_unavailable_zone_never_fabricates():
    b = live_board()
    del b["zone_a"]
    html = render_console_html(b, interactive=True)
    assert re.search(r'chip chip-unavailable" data-zone="A">A unavailable', html)
    assert "unavailable — no last-known-good read" in html
    assert "proj-1" not in html  # nothing fabricated for the missing zone


# ------------------------------------------------------- V3: interactive diff

def test_v3_diff_limited_to_buttons_notice_footer():
    static = render_console_html(live_board(), interactive=False)
    hub = render_console_html(live_board(), interactive=True)
    s_lines, h_lines = static.split("\n"), hub.split("\n")
    assert len(s_lines) == len(h_lines)
    # normalize the three allowed differences, then the renders must be identical
    norm = static.replace(' disabled title="actions disabled"', "")
    norm = norm.replace(READONLY_BANNER_TEXT + " ", "")
    norm = norm.replace(FALLBACK_NOTICE + " · ", "")
    # the POST helper is wired only when interactive (V13) — strip it from the hub render
    hub_norm = re.sub(r"<script>.*</script>", "", hub)
    # (c) footer string: normalize both footers to a common token
    norm = re.sub(r"(<footer[^>]*>).*?(</footer>)", r"\1FOOTER\2", norm)
    hub_norm = re.sub(r"(<footer[^>]*>).*?(</footer>)", r"\1FOOTER\2", hub_norm)
    assert norm == hub_norm


# ------------------------------------------------------------- V8: badges/SLA

def test_v8_badge_styles():
    html = render_console_html(live_board(), interactive=True)
    assert "badge verified" in html
    assert "badge inferred" in html
    assert "badge stale" in html
    assert 'title="evidence older than 24h"' in html  # stale badge tooltip


def test_v8_age_amber_at_sla_threshold():
    thr = {"blocking": 4, "degraded": 24, "routine": 72}
    for cls, hours in thr.items():
        b = live_board()
        b["zone_b"] = [{"item_id": "q-1", "panel": "A", "claim": "c",
                        "evidence_label": "verified", "source": "s",
                        "proposed_action": "a", "mandate": None,
                        "evidence_artifact": None, "age": f"waiting {hours}h",
                        "admission_score": 0.5, "score": 0.5,
                        "actions": ["approve", "dismiss", "escalate"],
                        "impact_class": cls, "age_hours": float(hours)}]
        at = render_console_html(b, True)
        assert "age age-amber" in at, f"{cls} at {hours}h must be amber"
        b["zone_b"][0]["age_hours"] = float(hours) - 0.5
        below = render_console_html(b, True)
        assert "age age-amber" not in below, f"{cls} below {hours}h must not be amber"


# --------------------------------------------------------- V10: kept content

def test_v10_kept_panels_render_current_fields():
    html = render_console_html(live_board(), interactive=True)
    # exec summary fields
    for h in ("h1", "h2", "h3", "4.2% fail", "d1", "r1"):
        assert h in html
    # zone A columns and rows
    for col in ("Project", "Item", "Status", "ETA", "Tokens", "Evidence"):
        assert col in html
    assert "waiting-on-you" in html and "done since 2026-11-11 18:00" in html
    assert "12 calls / 3400 tok" in html
    assert "untagged model-calls bucket: 3 calls / 900 tok" in html
    # themes table with trend arrows + stack overlap
    assert "billing" in html and "→" in html
    assert "▲" in html  # auth went 5 -> 7
    assert "stack overlap: 12%" in html
    # raw stats content matches board fields
    assert "proj-1" in html and "Raw stats" in html


# ------------------------------------------------------------------ V13: JS

def test_v13_no_chart_lib_websocket_or_polling():
    for flag in (True, False):
        html = render_console_html(live_board(), interactive=flag)
        low = html.lower()
        for banned in ("chart.js", "d3.js", "highcharts", "websocket", "eventsource",
                       "setinterval", "polling"):
            assert banned not in low, banned


def test_v13_post_helper_only_when_interactive():
    static = render_console_html(live_board(), interactive=False)
    hub = render_console_html(live_board(), interactive=True)
    assert "<script" not in static
    assert hub.count("<script>") == 1
    assert "fetch(" in hub and "POST" in hub
    assert "<script>" not in static
