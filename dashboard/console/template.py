"""Operator Console v0.9-rc1 — single-template renderer (RC1 lane 1).

Spec: console-rc-ui spec §1, §2, §3, §6.

Pinned interface (do not change):
    render_console_html(board: dict, interactive: bool) -> str

`board` is the dict produced by daily_board.render.render_board():
    zone_a: rows (project, item, status, pct_complete, eta, eta_insufficient,
            tokens, untagged_bucket, evidence_artifact, expanded, collapsed_line,
            detail{in_progress_milestones, open_asks, blockers}, done_label)
    zone_b: queue rows (item_id, panel, claim, evidence_label, source,
            proposed_action, mandate|None, evidence_artifact, age,
            admission_score, score, actions)   [+ optional impact_class, age_hours]
    zone_c: {mandates_ledger, shadow_pulse{would_have_done_today,
            divergence_note, link}}
    brief:  list[str]
    runs_state: "live" | "stale (read YYYY-MM-DD HH:MM)" | "unavailable"

Optional board keys consumed when present (integration lane may add them):
    generated_at (epoch), exec_summary {headlines, numbers, decisions, risks},
    themes [{name, current, prev}], stack_overlap, sparklines_html,
    zone_a_state / zone_b_state / zone_c_state (override per-zone chip state),
    raw_stats (dict of extra stat rows).

Rule: if a key is missing, the panel renders the §3 "unavailable" state —
never fabricated. One template, two renders: the ONLY branch is `interactive`
(V3: diff is limited to enabled/disabled buttons, the fallback notice line,
and the footer string).
"""

from __future__ import annotations

import html
import re
import time
from typing import Any, Dict, List, Optional, Tuple

RC_VERSION = "v0.9-rc1"
PANEL_ORDER = ("exec-summary", "sparkline-strip", "zone-b", "zone-a",
               "themes", "zone-c", "raw-stats")
STALE_TOOLTIP = "stale snapshot — refresh before deciding"
FALLBACK_NOTICE = "actions disabled — open the hub /dashboard to decide"
#: §7 read-only banner: shown whenever the console renders non-interactive
#: (no write token configured). serve.py re-exports this constant.
READONLY_BANNER_TEXT = ("You're viewing the operator console "
                        "(read-only until your write token is granted).")
EVIDENCE_BADGES = ("verified", "inferred", "stale")

# Fallback SLA-breach thresholds (hours) per impact class; the authoritative
# source is exception_queue/queue-policy.yaml (loaded below when importable).
_DEFAULT_SLA = {"blocking": 4.0, "degraded": 24.0, "routine": 72.0}


def _sla_thresholds() -> Dict[str, float]:
    """SLA-breach hours per impact class from queue-policy.yaml (4h/24h/72h)."""
    try:
        from dashboard.exception_queue.queue import load_policy  # type: ignore
        policy = load_policy()
        return {k: float(v["sla_breach_hours"]) for k, v in policy["classes"].items()}
    except Exception:
        pass
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "..", "exception_queue", "queue-policy.yaml"),
                 os.path.join(here, "queue-policy.yaml")):
        try:
            with open(cand) as fh:
                text = fh.read()
            out: Dict[str, float] = {}
            cur = None
            for line in text.splitlines():
                line = line.split("#", 1)[0].rstrip()
                m = re.match(r"^\s{2}(\w+):\s*$", line)
                if m:
                    cur = m.group(1)
                m = re.match(r"^\s+sla_breach_hours:\s*([\d.]+)", line)
                if m and cur:
                    out[cur] = float(m.group(1))
            if out:
                return out
        except OSError:
            continue
    return dict(_DEFAULT_SLA)


_SLA = _sla_thresholds()

# --------------------------------------------------------------------- helpers

def esc(v: Any) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def _hhmm(epoch: Optional[float]) -> str:
    if epoch is None:
        return ""
    return time.strftime("%H:%M", time.localtime(epoch))


def _zone_state(board: Dict[str, Any], state_key: str, rows_key: str) -> Tuple[str, str]:
    """Resolve one zone's source state -> (word, read_ts_str).

    Precedence: explicit board[f"{zone}_state"] > runs_state (zone A only)
    > 'live' when the zone's rows key exists > 'unavailable' (missing key —
    §3: never fabricate). A missing rows key is always unavailable, even if
    runs_state still says live. state strings may carry a read timestamp, e.g.
    'stale (read 2026-11-12 09:42)'; a live zone's read ts is generated_at.
    """
    raw = board.get(state_key)
    if raw is None and rows_key == "zone_a":
        raw = board.get("runs_state")
    if rows_key not in board:
        raw = "unavailable"
    elif raw is None:
        raw = "live"
    text = str(raw)
    m = re.search(r"read\s+([^)\]]+)", text)
    read_ts = m.group(1).strip() if m else ""
    word = "stale" if text.startswith("stale") else \
           "unavailable" if text.startswith("unavailable") else "live"
    if word == "live" and not read_ts:
        generated = board.get("generated_at")
        read_ts = _hhmm(float(generated)) if generated is not None else ""
    return word, read_ts


def _chip(zone: str, word: str, read_ts: str) -> str:
    cls = {"live": "chip-live", "stale": "chip-stale", "unavailable": "chip-unavailable"}[word]
    ts = f" · read {esc(read_ts)}" if read_ts else ""
    return f'<span class="chip {cls}" data-zone="{zone}">{zone} {esc(word)}{ts}</span>'


def _unavailable_panel(panel_id: str, title: str, extra: str = "") -> str:
    return (f'<section id="{panel_id}" class="panel degraded">'
            f'<h2>{esc(title)}</h2>'
            f'<div class="banner">unavailable — no last-known-good read</div>'
            f'{extra}</section>')


def _age_hours(row: Dict[str, Any]) -> Optional[float]:
    """Age in hours: explicit row['age_hours'] if numeric, else parsed from the
    board's age label ('waiting 26h' / 'waiting 45m')."""
    v = row.get("age_hours")
    if isinstance(v, (int, float)):
        return float(v)
    m = re.match(r"waiting\s+([\d.]+)\s*h", str(row.get("age", "")))
    if m:
        return float(m.group(1))
    m = re.match(r"waiting\s+([\d.]+)\s*m", str(row.get("age", "")))
    if m:
        return float(m.group(1)) / 60.0
    return None


def _age_class(row: Dict[str, Any]) -> str:
    """Amber at the item class's SLA-breach threshold (V8). Rows without an
    impact_class cannot be classified — no amber is asserted."""
    hrs = _age_hours(row)
    cls = row.get("impact_class")
    if hrs is None or cls is None or cls not in _SLA:
        return "age-gray"
    return "age-amber" if hrs >= _SLA[cls] else "age-gray"


# --------------------------------------------------------------------- panels

def _exec_summary(board: Dict[str, Any]) -> str:
    """Exec summary: headlines (3) · numbers strip · decisions needed · risks.
    Falls back to the board's daily-brief lines when exec_summary is absent
    (real data from the same board — never fabricated)."""
    es = board.get("exec_summary")
    body = ""
    if isinstance(es, dict):
        headlines = [str(h) for h in (es.get("headlines") or [])][:3]
        if headlines:
            body += "<ol class='headlines'>" + "".join(f"<li>{esc(h)}</li>" for h in headlines) + "</ol>"
        numbers = es.get("numbers") or []
        if numbers:
            body += ("<div class='numbers-strip'>" +
                     "".join(f"<span class='num'>{esc(n)}</span>" for n in numbers) +
                     "</div>")
        for key, label in (("decisions", "Decisions needed"), ("risks", "Risks")):
            items = es.get(key) or []
            if items:
                body += f"<h3>{esc(label)}</h3><ul>" + "".join(f"<li>{esc(i)}</li>" for i in items) + "</ul>"
    else:
        brief = board.get("brief")
        if brief:
            body = "<ul class='headlines'>" + "".join(f"<li>{esc(b)}</li>" for b in brief[:3]) + "</ul>"
    if not body:
        return _unavailable_panel("exec-summary", "Executive summary")
    return f'<section id="exec-summary" class="panel"><h2>Executive summary</h2>{body}</section>'


def _sparkline_strip(board: Dict[str, Any]) -> str:
    """Fixed-height strip of 3 mini-charts (sparklines lane supplies the HTML;
    no JS chart lib — V13). Missing data renders the §3 unavailable state."""
    html_str = board.get("sparklines_html")
    if not html_str:
        return _unavailable_panel(
            "sparkline-strip", "Sparkline strip",
            "<div class='muted'>failure rate · spend · theme momentum</div>")
    return (f'<section id="sparkline-strip" class="panel quiet">'
            f'<h2>Sparkline strip</h2>{html_str}</section>')


def _zone_b_buttons(row: Dict[str, Any], interactive: bool, zone_word: str) -> str:
    """Exactly three action buttons (§2): approve primary, dismiss neutral,
    escalate quiet. Disabled when static OR the zone is stale (with the stale
    tooltip). The POST helper is wired only when interactive (V13)."""
    item_id = esc(row.get("item_id", ""))
    buttons = []
    for action, style in (("approve", "btn-primary"), ("dismiss", "btn-neutral"),
                          ("escalate", "btn-quiet")):
        attrs = f'data-item-id="{item_id}" data-action="{action}"'
        if interactive and zone_word != "stale":
            buttons.append(f'<button class="btn {style}" {attrs}>{action.capitalize()}</button>')
        else:
            tip = STALE_TOOLTIP if zone_word == "stale" else "actions disabled"
            buttons.append(f'<button class="btn {style}" {attrs} disabled title="{esc(tip)}">'
                           f'{action.capitalize()}</button>')
    return "<span class='actions'>" + "".join(buttons) + "</span>"


def _zone_b(board: Dict[str, Any], interactive: bool) -> str:
    word, read_ts = _zone_state(board, "zone_b_state", "zone_b")
    rows = board.get("zone_b")
    chip = _chip("B", word, read_ts)
    if rows is None:
        return _unavailable_panel("zone-b", f"Zone B — Needs a person {chip}")
    body = ""
    if word == "stale":
        body += f"<div class='banner'>B: stale{(' (read ' + esc(read_ts) + ')') if read_ts else ''}</div>"
    if not rows:
        body += "<div class='muted'>queue empty</div>"
    for r in rows:
        panel = str(r.get("panel", "A")).upper()
        if panel not in ("A", "B", "C"):
            panel = "A"
        badge = esc(r.get("evidence_label", "inferred"))
        badge_cls = badge if badge in EVIDENCE_BADGES else "inferred"
        badge_title = ' title="evidence older than 24h"' if badge_cls == "stale" else ""
        artifact = r.get("evidence_artifact")
        link_cls = "artifact muted" if badge_cls == "stale" else "artifact"
        mandate = r.get("mandate")
        mandate_line = (f"mandate: {esc(mandate.get('mandate_id', ''))}"
                        if isinstance(mandate, dict) and mandate
                        else "no mandate — will create")
        age_cls = _age_class(r)
        brief_line = ""
        if panel == "B":
            brief_line = f"<div class='brief'>{esc(r.get('claim', ''))}</div>"
        body += (
            f"<div class='queue-row panel-{panel.lower()}'>"
            f"<div class='row-top'><span class='panel-tag'>Panel {panel}</span> "
            f"<span class='claim'>{esc(r.get('claim', ''))}</span> "
            f"<span class='age {age_cls}'>{esc(r.get('age', ''))}</span> "
            f"<span class='score'>score {esc(r.get('score', ''))}</span></div>"
            f"<div class='row-meta'>evidence: <span class='badge {badge_cls}'{badge_title}>"
            f"{badge}</span> · source: {esc(r.get('source', ''))}</div>"
            f"<div class='row-action'>action: {esc(r.get('proposed_action', ''))} · {mandate_line}</div>"
            + (f"<div class='row-artifact'><a class='{link_cls}' href='#'>{esc(artifact)} ▸</a></div>"
               if artifact else "")
            + brief_line + _zone_b_buttons(r, interactive, word) + "</div>")
    return (f'<section id="zone-b" class="panel"><h2>Zone B — Needs a person {chip}</h2>'
            f"{body}</section>")


def _zone_a(board: Dict[str, Any]) -> str:
    word, read_ts = _zone_state(board, "zone_a_state", "zone_a")
    rows = board.get("zone_a")
    chip = _chip("A", word, read_ts)
    if rows is None:
        return _unavailable_panel("zone-a", f"Zone A — Project statuses {chip}")
    body = ""
    if word == "stale":
        body += f"<div class='banner'>A: stale{(' (read ' + esc(read_ts) + ')') if read_ts else ''}</div>"
    if not rows:
        body += "<div class='muted'>no project items</div>"
    body += ("<table class='zone-a-table'><thead><tr><th>Project</th><th>Item</th>"
             "<th>Status</th><th>%</th><th>ETA</th><th>Tokens</th><th>Evidence</th></tr></thead><tbody>")
    for r in rows:
        status = esc(r.get("status", ""))
        done = f" <span class='muted'>({esc(r['done_label'])})</span>" if r.get("done_label") else ""
        body += (f"<tr class='{'expanded' if r.get('expanded') else 'collapsed'}'>"
                 f"<td>{esc(r.get('project', ''))}</td><td>{esc(r.get('item', ''))}</td>"
                 f"<td>{status}{done}</td><td>{esc(r.get('pct_complete', ''))}%</td>"
                 f"<td>{esc(r.get('eta', ''))}</td><td>{esc(r.get('tokens', ''))}</td>"
                 f"<td>{esc(r.get('evidence_artifact') or 'n/a')}</td></tr>")
        if r.get("expanded"):
            d = r.get("detail") or {}
            for key, label in (("in_progress_milestones", "In progress"),
                               ("open_asks", "Open asks"), ("blockers", "Blockers")):
                vals = d.get(key) or []
                if vals:
                    body += (f"<tr class='detail-row'><td colspan='7'><span class='muted'>{label}:</span> "
                             f"{esc(', '.join(str(v) for v in vals))}</td></tr>")
    body += "</tbody></table>"
    untagged = [r.get("untagged_bucket") for r in rows if r.get("untagged_bucket")]
    if untagged:
        body += f"<div class='muted'>untagged model-calls bucket: {esc(untagged[0])}</div>"
    return (f'<section id="zone-a" class="panel"><h2>Zone A — Project statuses {chip}</h2>'
            f"{body}</section>")


def _trend_arrow(current: Any, prev: Any) -> str:
    try:
        delta = float(current) - float(prev)
    except (TypeError, ValueError):
        return "→"
    return "▲" if delta > 0 else "▼" if delta < 0 else "→"


def _themes(board: Dict[str, Any]) -> str:
    themes = board.get("themes")
    if themes is None:
        return _unavailable_panel("themes", "Themes & trends")
    body = ("<table class='themes-table'><thead><tr><th>Theme</th><th>Current</th>"
            "<th>Prev</th><th>Trend</th></tr></thead><tbody>")
    for t in themes:
        cur, prev = t.get("current"), t.get("prev")
        body += (f"<tr><td>{esc(t.get('name', ''))}</td><td>{esc(cur)}</td>"
                 f"<td>{esc(prev)}</td><td class='trend'>{_trend_arrow(cur, prev)}</td></tr>")
    body += "</tbody></table>"
    overlap = board.get("stack_overlap")
    if overlap is not None:
        body += f"<div class='stack-overlap'>stack overlap: {esc(overlap)}</div>"
    else:
        body += "<div class='muted'>stack overlap: unavailable</div>"
    return f'<section id="themes" class="panel"><h2>Themes &amp; trends</h2>{body}</section>'


def _zone_c(board: Dict[str, Any]) -> str:
    word, read_ts = _zone_state(board, "zone_c_state", "zone_c")
    zc = board.get("zone_c")
    chip = _chip("C", word, read_ts)
    if zc is None or not isinstance(zc, dict):
        return _unavailable_panel("zone-c", f"Zone C {chip}")
    body = "<h3>Mandates ledger</h3>"
    ledger = zc.get("mandates_ledger") or []
    if ledger:
        body += "<ul>" + "".join(
            f"<li>{esc(m.get('mandate_id', ''))} · scope: {esc(m.get('scope', ''))} · "
            f"expiry: {esc(m.get('expiry_at', ''))}</li>" for m in ledger) + "</ul>"
    else:
        body += "<div class='muted'>no standing grants</div>"
    sp = zc.get("shadow_pulse") or {}
    body += ("<h3>Shadow pulse</h3>"
             f"<div class='muted'>{esc(sp.get('would_have_done_today', 0))} would-have-done today — "
             f"{esc(sp.get('divergence_note', ''))}</div>")
    if sp.get("link"):
        body += f"<div><a href='{esc(sp['link'])}'>daily comparison</a></div>"
    return f'<section id="zone-c" class="panel quiet"><h2>Zone C {chip}</h2>{body}</section>'


def _raw_stats(board: Dict[str, Any]) -> str:
    """Token stats table + untagged bucket — same fields render.py emits
    (per-row tokens label, untagged model-calls bucket) plus any extra
    raw_stats the pipeline attaches. Missing both -> unavailable."""
    rows = board.get("zone_a")
    extras = board.get("raw_stats")
    if rows is None and extras is None:
        return _unavailable_panel("raw-stats", "Raw stats")
    body = ""
    if rows:
        body += ("<table class='raw-stats-table'><thead><tr><th>Project</th>"
                 "<th>Tokens</th></tr></thead><tbody>" +
                 "".join(f"<tr><td>{esc(r.get('project', ''))}</td>"
                         f"<td>{esc(r.get('tokens', ''))}</td></tr>" for r in rows) +
                 "</tbody></table>")
        untagged = [r.get("untagged_bucket") for r in rows if r.get("untagged_bucket")]
        if untagged:
            body += f"<div class='muted'>untagged bucket: {esc(untagged[0])}</div>"
    if extras:
        body += "<table class='raw-stats-extra'><tbody>" + "".join(
            f"<tr><td>{esc(k)}</td><td>{esc(v)}</td></tr>" for k, v in sorted(extras.items())) \
            + "</tbody></table>"
    return f'<section id="raw-stats" class="panel quiet"><h2>Raw stats</h2>{body}</section>'


# ----------------------------------------------------------------- page shell

_POST_HELPER = ("function qa(itemId, action, payload) { fetch('/dashboard/queue/' + "
                "encodeURIComponent(action), "
                "{method: 'POST', headers: {'Content-Type': 'application/json'}, "
                "body: JSON.stringify({item_id: itemId, payload: payload || {}})})"
                ".then(r => r.json()).then(() => location.reload()); }")


def render_console_html(board: Dict[str, Any], interactive: bool) -> str:
    """Full HTML page. One template, two renders (spec §6): the only branch is
    `interactive` — (a) wires the POST helper, (b) enables action buttons,
    plus the fallback notice line and footer string (V3)."""
    generated = board.get("generated_at")
    generated = float(generated) if generated is not None else None
    gen_str = _hhmm(generated) or _hhmm(time.time())
    chips = " ".join(_chip(z, *_zone_state(board, f"zone_{z.lower()}_state",
                                           f"zone_{z.lower()}")) for z in ("A", "B", "C"))
    notice = (f"{READONLY_BANNER_TEXT} {esc(FALLBACK_NOTICE)} · "
              if not interactive else "")
    script = f"<script>{_POST_HELPER}</script>" if interactive else ""
    footer = (f"served from hub · {RC_VERSION}" if interactive
              else f"static snapshot {gen_str}")
    parts = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'>",
        f"<title>Operator Console {RC_VERSION}</title>",
        "<style>"
        "*{box-sizing:border-box}"
        "body{margin:0;background:#0d1117;color:#e6edf3;"
        "font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif}"
        "header{position:sticky;top:0;z-index:10;display:flex;align-items:center;gap:12px;"
        "padding:10px 24px;background:#0d1117ee;border-bottom:1px solid #30363d;backdrop-filter:blur(6px)}"
        "header h1{font-size:15px;font-weight:600;margin:0;letter-spacing:.2px}"
        "header .generated{color:#8b949e;font-size:12px}"
        "header .chips{margin-left:auto;display:flex;gap:6px}"
        ".panel{max-width:1180px;margin:16px auto;padding:16px 20px;background:#161b22;"
        "border:1px solid #30363d;border-radius:10px}"
        ".panel h2{font-size:11px;font-weight:600;letter-spacing:.12em;text-transform:uppercase;"
        "color:#8b949e;margin:0 0 12px;display:flex;align-items:center;gap:8px}"
        ".panel h3{font-size:12px;color:#8b949e;margin:14px 0 6px;text-transform:uppercase;letter-spacing:.08em}"
        ".queue-row{border-left:3px solid #30363d;background:#0d1117;border-radius:6px;"
        "padding:10px 12px;margin:8px 0}"
        ".queue-row.panel-a{border-left-color:#d29922}.queue-row.panel-b{border-left-color:#58a6ff}"
        ".queue-row.panel-c{border-left-color:#a371f7}"
        ".row-top{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}"
        ".panel-tag{font-size:11px;font-weight:600;color:#8b949e;text-transform:uppercase;letter-spacing:.08em}"
        ".claim{color:#e6edf3;font-weight:500}"
        ".age{font-size:12px;font-variant-numeric:tabular-nums}"
        ".score{margin-left:auto;font-size:12px;color:#8b949e;font-variant-numeric:tabular-nums}"
        ".row-meta,.row-action{font-size:12px;color:#8b949e;margin-top:4px}"
        ".row-artifact{margin-top:4px;font-size:12px}"
        ".badge{padding:1px 8px;border-radius:10px;font-size:11px;font-weight:500}"
        ".badge.verified{background:#12351f;color:#3fb950;border:1px solid #1f6f37}"
        ".badge.inferred{background:#161b22;color:#8b949e;border:1px solid #30363d}"
        ".badge.stale{color:#d29922;border:1px dashed #d29922;background:transparent}"
        ".age-amber{color:#d29922;font-weight:600}.age-gray{color:#8b949e}"
        ".chip{padding:2px 10px;border-radius:10px;font-size:11px;font-weight:600}"
        ".chip-live{background:#12351f;color:#3fb950;border:1px solid #1f6f37}"
        ".chip-stale{background:#2a1f0d;color:#d29922;border:1px solid #6e4f13}"
        ".chip-unavailable{background:#161b22;color:#8b949e;border:1px solid #30363d}"
        ".banner{background:#2a1f0d;color:#d29922;border:1px solid #6e4f13;"
        "border-radius:6px;padding:6px 10px;font-size:12px;margin-bottom:8px}"
        ".btn{padding:4px 12px;border-radius:6px;border:1px solid #30363d;background:#21262d;"
        "color:#e6edf3;font-size:12px;font-weight:500;cursor:pointer}"
        ".btn:hover:not([disabled]){border-color:#8b949e}"
        ".btn-primary{background:#1f6f37;border-color:#1f6f37;color:#fff}"
        ".btn-primary:hover:not([disabled]){background:#239a43;border-color:#239a43}"
        ".btn-quiet{background:transparent;color:#8b949e}"
        ".btn[disabled]{opacity:.4;cursor:not-allowed}"
        "table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}"
        "th{text-align:left;font-size:11px;font-weight:600;color:#8b949e;"
        "text-transform:uppercase;letter-spacing:.08em;padding:6px 8px;border-bottom:1px solid #30363d}"
        "td{padding:7px 8px;border-bottom:1px solid #21262d;font-size:13px}"
        "tr:hover td{background:#1c2128}"
        ".detail-row td{background:#0d1117;color:#8b949e;font-size:12px}"
        ".quiet{color:#8b949e;font-size:12px}.muted{color:#8b949e}"
        ".banner,.fallback-notice{font-size:12px}"
        ".fallback-notice{color:#8b949e;text-align:center;padding:16px}"
        "a{color:#58a6ff;text-decoration:none}a:hover{text-decoration:underline}"
        ".exec-headline{font-size:14px;margin:6px 0}"
        ".exec-num{font-size:20px;font-weight:600;font-variant-numeric:tabular-nums}"
        "</style></head><body>",
        f"<header><h1>Operator Console</h1><span class='generated'>generated {esc(gen_str)}</span>"
        f"<span class='chips'>{chips}</span></header>",
        _exec_summary(board), _sparkline_strip(board),
        _zone_b(board, interactive), _zone_a(board), _themes(board),
        _zone_c(board), _raw_stats(board),
        f"{notice}<footer class='fallback-notice'>{esc(footer)} · generated {esc(gen_str)}</footer>",
        script + "</body></html>",
    ]
    return "\n".join(parts)
