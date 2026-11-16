"""RC1 lane 3 — 7-day sparkline strip for Operator Console v0.9-rc1.

Spec: ~/workspaces/worker/lanes-research/console-rc-ui.md §5 (V9) and §8.

render_sparklines(series) -> str
    One row of three fixed-size inline SVG sparklines (~120x28 each, no JS
    chart library, no interactivity) for keys failure_rate, spend,
    theme_momentum. Fewer than 7 days of data renders what exists with an
    explicit `days: N` label — never padded with silent zeros.

collect_series(snapshot_dir) -> dict[str, list[float]]
    Builds the daily values from real data_layer aggregates:
      - failure_rate: job failures/total per day, from the a5-runtime.db
        job-runs table read through data_layer.job_runs.JobRunsDB (evidence
        unverified/total via parse_stage_evidence — the same aggregate the
        status rows use),
      - spend: daily token totals from the model-calls.jsonl ledger, using
        the same field conventions as data_layer.model_calls_tailer
        (in_tokens/input_tokens + out_tokens/output_tokens),
      - theme_momentum: daily counts of the top scrape theme, where themes
        are the per-topic groups scrape_intel._theme_stats builds (topic
        column of scrapes.db items).

Fail-closed: a missing/unreadable source yields an empty list for that key
(days: 0), never a fabricated number.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from typing import Any, Dict, List, Optional

DEFAULT_SNAPSHOT_DIR = os.path.expanduser("~/.hermes/dashboard/snapshots")

WIDTH, HEIGHT = 120, 28
ACCENT = {  # single accent color per metric (identity, not state)
    "failure_rate": "#b36b00",   # amber
    "spend": "#8a5fbf",          # violet
    "theme_momentum": "#1f6fb2",  # blue
}
LABELS = {
    "failure_rate": "failure rate",
    "spend": "spend",
    "theme_momentum": "theme momentum",
}
KEYS = ("failure_rate", "spend", "theme_momentum")

JOB_RUN_DB_NAMES = ("a5-runtime.db",)
LEDGER_NAME = "model-calls.jsonl"
SCRAPES_DB_NAMES = ("scrapes.db",)
DAY = 86400.0


def _esc(v: Any) -> str:
    return (str(v).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# ------------------------------------------------------------------ collect

def _day_index(ts: float) -> int:
    return int(ts // DAY)


def _failure_rate_daily(snapshot_dir: str) -> Dict[int, Dict[str, int]]:
    """Per-UTC-day {failures, total} from the job-runs table via JobRunsDB."""
    here = os.path.dirname(os.path.abspath(__file__))
    # data_layer modules import as top-level names (plain-path tree); include
    # our own tree first, then the canonical built package as fallback.
    dashboard_dir = os.path.dirname(here)
    for p in (os.path.join(dashboard_dir, "data_layer"),
              os.environ.get("AERIAL_DATA_LAYER_FALLBACK", ""),
              dashboard_dir):
        if p and p not in sys.path:
            sys.path.insert(0, p)
    db_path = None
    for name in JOB_RUN_DB_NAMES:
        cand = os.path.join(snapshot_dir, name)
        if os.path.exists(cand):
            db_path = cand
            break
    if db_path is None:
        return {}
    from job_runs import JobRunsDB
    try:
        db = JobRunsDB(db_path=db_path)
        runs = db.job_runs()
    except Exception:
        return {}
    out: Dict[int, Dict[str, int]] = {}
    for run in runs:
        ts = _norm_ts(run.get("completed_at"))
        if ts is None:
            continue
        ev = db.parse_stage_evidence(run.get("evidence_json") or "{}")
        bucket = out.setdefault(_day_index(ts), {"failures": 0, "total": 0})
        bucket["total"] += ev.get("total", 0)
        bucket["failures"] += ev.get("unverified", 0)
    return out


def _norm_ts(value: Any) -> Optional[float]:
    """Same normalization scrape_intel uses (epoch sec/ms or ISO string)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v <= 0:
            return None
        return v / 1000.0 if v > 1e12 else v
    s = str(value).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        pass
    from datetime import datetime, timezone
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def _spend_daily(snapshot_dir: str) -> Dict[int, int]:
    """Per-UTC-day token totals from the model-calls.jsonl ledger.

    Field conventions match data_layer.model_calls_tailer: in_tokens/
    input_tokens and out_tokens/output_tokens; malformed lines are skipped
    (the tailer's own verification covers the totals — this only buckets).
    """
    path = os.path.join(snapshot_dir, LEDGER_NAME)
    if not os.path.exists(path):
        return {}
    out: Dict[int, int] = {}
    with open(path, "rb") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(rec, dict):
                continue
            tin = int(rec.get("in_tokens") or rec.get("input_tokens") or 0)
            tout = int(rec.get("out_tokens") or rec.get("output_tokens") or 0)
            ts = _norm_ts(rec.get("ts") or rec.get("timestamp")
                          or rec.get("completed_at"))
            if ts is None:
                continue
            out[_day_index(ts)] = out.get(_day_index(ts), 0) + tin + tout
    return out


def _theme_daily(snapshot_dir: str) -> tuple[Dict[int, int], Optional[str]]:
    """Per-UTC-day counts of the top scrape theme (topic with most items)."""
    db_path = None
    for name in SCRAPES_DB_NAMES:
        cand = os.path.join(snapshot_dir, name)
        if os.path.exists(cand):
            db_path = cand
            break
    if db_path is None:
        return {}, None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.2)
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute("SELECT * FROM items")]
        conn.close()
    except (sqlite3.Error, OSError):
        return {}, None
    by_topic: Dict[str, Dict[int, int]] = {}
    for r in rows:
        topic = str(r.get("topic") or "untagged")  # scrape_intel naming
        ts = _item_ts(r)
        if ts is None:
            continue
        by_topic.setdefault(topic, {})
        d = _day_index(ts)
        by_topic[topic][d] = by_topic[topic].get(d, 0) + 1
    if not by_topic:
        return {}, None
    top = max(sorted(by_topic), key=lambda t: sum(by_topic[t].values()))
    return dict(by_topic[top]), top


def _item_ts(row: Dict[str, Any]) -> Optional[float]:
    for key in ("published", "fetched_at"):
        ts = _norm_ts(row.get(key))
        if ts is not None:
            return ts
    return None


def collect_series(snapshot_dir: str, now: Optional[float] = None
                   ) -> Dict[str, List[float]]:
    """Build {failure_rate, spend, theme_momentum} -> ascending daily values.

    Only days with real data appear (ascending); gaps are not padded with
    zeros — render_sparklines shows `days: N` for N < 7.
    """
    del now  # values are per calendar day; the window is the data's own span
    fr = _failure_rate_daily(snapshot_dir)
    failure_rate = [
        round(100.0 * v["failures"] / v["total"], 1)
        for d, v in sorted(fr.items()) if v["total"] > 0
    ]
    spend = [float(v) for d, v in sorted(_spend_daily(snapshot_dir).items())]
    theme_days, _top = _theme_daily(snapshot_dir)
    theme_momentum = [float(v) for d, v in sorted(theme_days.items())]
    return {
        "failure_rate": failure_rate,
        "spend": spend,
        "theme_momentum": theme_momentum,
    }


# ------------------------------------------------------------------- render

def _fmt_value(key: str, v: float) -> str:
    if key == "failure_rate":
        return f"{v:.1f}%"
    if key == "spend":
        return f"{v/1000.0:.1f}k" if v >= 1000 else f"{v:.0f}"
    return f"{v:.0f}"


def _delta(key: str, vals: List[float]) -> str:
    if len(vals) < 2:
        return ""
    d = vals[-1] - vals[0]
    if abs(d) < 1e-9:
        return "\u2500"  # flat
    arrow = "\u25b2" if d > 0 else "\u25bc"
    return f"{arrow}{abs(d):.1f}"


def _svg(vals: List[float], color: str) -> str:
    w, h = WIDTH, HEIGHT
    pad = 2
    if len(vals) >= 2 and max(vals) != min(vals):
        lo, hi = min(vals), max(vals)
        span = hi - lo or 1.0
        pts = []
        for i, v in enumerate(vals):
            x = pad + i * (w - 2 * pad) / (len(vals) - 1)
            y = h - pad - (v - lo) / span * (h - 2 * pad)
            pts.append(f"{x:.1f},{y:.1f}")
        poly = f'<polyline points="{" ".join(pts)}" fill="none" stroke="{color}" stroke-width="1.5"/>'
    else:
        y = h / 2.0
        poly = (f'<line x1="{pad}" y1="{y:.1f}" x2="{w-pad}" y2="{y:.1f}"'
                f' stroke="{color}" stroke-width="1.5"/>')
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}"'
            f' role="img" aria-hidden="true">{poly}</svg>')


def render_sparklines(series: Dict[str, List[float]], meta: Optional[Dict[str, str]] = None
                      ) -> str:
    """One row of three inline-SVG sparklines. Not clickable, no tooltips,
    no hover handlers, no drill-down (spec §5). meta may carry
    'theme_momentum_label' (the theme name) and 'delta_decimals'."""
    meta = meta or {}
    cells = []
    for key in KEYS:
        vals = [float(v) for v in (series.get(key) or [])]
        n = len(vals)
        label = LABELS[key]
        if key == "theme_momentum" and meta.get("theme_momentum_label"):
            label = f"theme: {meta['theme_momentum_label']}"
        days_tag = "" if n == 7 else f' <span class="spark-days">days: {n}</span>'
        delta = _delta(key, vals)
        value = _fmt_value(key, vals[-1]) if vals else "n/a"
        cells.append(
            f'<div class="sparkline spark-{_esc(key)}">'
            f'<span class="spark-label">{_esc(label)}</span>'
            f'{_svg(vals, ACCENT[key])}'
            f'<span class="spark-value">{_esc(value)}'
            + (f" {_esc(delta)}" if delta else "")
            + f"</span>{days_tag}</div>"
        )
    return ('<div class="sparkline-strip" id="sparkline-strip">'
            + "".join(cells) + "</div>")


# ------------------------------------------------------- V10 partial: board
# Integration (lane 6) places the fragment directly under exec summary:
#
#   board = {
#       "exec_summary_html": template.render_exec_summary(...),
#       "sparklines_html": sparklines.render_sparklines(series, meta),
#       ...zones...
#   }
#
# The template must render board["sparklines_html"] immediately after
# board["exec_summary_html"] (spec §1 panel order: exec summary → sparkline
# strip → Zone B → ...). This module supplies only the fragment.
