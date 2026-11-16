#!/usr/bin/env python3
"""Operator dashboard — daily static HTML render (gap B1 + decision C-part-1).

Wires Lane B's data layer to the REAL production sources and renders the full
daily board (Lane C zones + Lane A exception queue + token stats) to a single
self-contained dashboard.html.

CLI:
  python3 render_daily.py --hub-db <path> --hub-url <url> \
      --hub-token-file <path> --ledger <path> --out <dashboard.html>

Defaults are the production paths:
  --hub-db          <state>/hub/hub.db
  --hub-url         http://127.0.0.1:8020
  --hub-token-file  ~/.config/ticket-hub/read-token
  --ledger          <state>/ledger/model-calls.jsonl

Fail-closed per spec §3.2: every source is wrapped so a failure degrades that
panel to stale-with-timestamp or 'unavailable' via Lane B's SnapshotStore —
the data_layer/degradation machinery is used, never bypassed. Nothing here
fabricates data.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import html
import json
import os
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional

def _norm_ts(v):
    """Hub timestamps arrive as ISO strings or epoch ints; normalize to a
    sortable epoch float so mixed-type rows never crash the sort/max."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    t = str(v).strip()
    try:
        return float(t)
    except ValueError:
        pass
    try:
        return _dt.datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


HERE = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.dirname(HERE)
for _p in (_PKG_ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from dashboard.data_layer.hub_client import HubClient, HubReadError          # noqa: E402
from dashboard.data_layer.job_runs import JobRunsDB                          # noqa: E402
from dashboard.data_layer.model_calls_tailer import ModelCallsTailer         # noqa: E402
from dashboard.data_layer.sessions import SessionsStore                      # noqa: E402
from dashboard.data_layer.snapshot_store import SnapshotStore                # noqa: E402
from dashboard.exception_queue import queue as qmod                          # noqa: E402
from dashboard.daily_board import render as board_render                     # noqa: E402

import exec_summary  # noqa: E402
import scrape_intel  # noqa: E402

from deployment_profile import pvalue as _pv
DEFAULT_HUB_DB = str(_pv("hub_db"))
DEFAULT_HUB_URL = "http://127.0.0.1:8020"
DEFAULT_TOKEN_FILE = os.path.expanduser("~/.config/ticket-hub/read-token")
DEFAULT_LEDGER = str(_pv("model_calls_ledger"))
DEFAULT_SNAPSHOT = os.path.expanduser("~/.hermes/dashboard/daily-snapshots.json")
DEFAULT_OUT = os.path.expanduser("~/.hermes/dashboard/dashboard.html")

DONE_STATUSES = ("done", "complete", "completed", "succeeded", "success")
FAILED_STATUSES = ("failed", "error", "errored", "failure")


# --------------------------------------------------------------------------- sources

class HubJobRuns(JobRunsDB):
    """JobRunsDB adapted to the hub.db job_runs schema.

    hub.db columns: run_id, job_id, node, started_at, ended_at, status,
    exit_code, duration_s, summary, log_tail, meta, pid. Mapped onto the
    data-layer row shape (project=job_id, stage_type=node, …) so Lane C's
    ETA logic and degradation path work unchanged.
    """

    def job_runs(self, project: Optional[str] = None,
                 stage_type: Optional[str] = None) -> List[Dict[str, Any]]:
        rows = self._execute_with_backoff("SELECT * FROM job_runs")
        out: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            if project is not None and d.get("job_id") != project:
                continue
            if stage_type is not None and d.get("node") != stage_type:
                continue
            ended = _norm_ts(d.get("ended_at") or d.get("started_at"))
            dur_s = d.get("duration_s")
            out.append({
                "project": d.get("job_id"),
                "stage_type": d.get("node"),
                "status": d.get("status"),
                "evidence_json": d.get("meta") or "{}",
                "completed_at": ended,
                "duration_hours": (float(dur_s) / 3600.0) if dur_s is not None else None,
            })
        return out

    def raw_rows(self) -> List[Dict[str, Any]]:
        return [dict(r) for r in self._execute_with_backoff("SELECT * FROM job_runs")]


def project_items_from_raw(raw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Derive Zone-A project items from raw hub job-run rows.

    One item per job_id; stage list = distinct nodes seen; a node counts done
    when its latest run succeeded (status done, exit 0/None). Grounded in the
    rows only — nothing invented.
    """
    by_job: Dict[str, List[Dict[str, Any]]] = {}
    for r in raw:
        job = r.get("job_id")
        if job is None:
            continue
        by_job.setdefault(str(job), []).append(r)
    items: List[Dict[str, Any]] = []
    for job in sorted(by_job):
        runs = by_job[job]
        nodes = sorted({str(r["node"]) for r in runs if r.get("node")})
        done_nodes: List[str] = []
        blocked = False
        for node in nodes:
            nruns = [r for r in runs if str(r.get("node")) == node]
            nruns.sort(key=lambda r: _norm_ts(r.get("ended_at") or r.get("started_at")) or 0)
            latest = nruns[-1]
            st = str(latest.get("status") or "").lower()
            ec = latest.get("exit_code")
            if st in FAILED_STATUSES or (ec is not None and ec != 0):
                blocked = True
            elif st in DONE_STATUSES:
                done_nodes.append(node)
        done_ts = max((_norm_ts(r.get("ended_at")) or 0) for r in runs) or None
        items.append({
            "project": job,
            "item": job,
            "item_tag": job,
            "status": "blocked" if blocked else ("done" if len(done_nodes) == len(nodes) and nodes else "working"),
            "stages": nodes,
            "done_stages": done_nodes,
            "done_ts": done_ts if (not blocked and len(done_nodes) == len(nodes) and nodes) else None,
            "evidence_artifact": None,
        })
    return items


def read_token(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            tok = f.read().strip()
        return tok or None
    except OSError:
        return None


def hub_findings_to_finding(rec: Dict[str, Any]) -> qmod.Finding:
    """Map a hub API finding record onto Lane A's Finding (tolerant of key aliases)."""
    steps = rec.get("steps") or ()
    return qmod.Finding(
        condition_key=str(rec.get("condition_key") or rec.get("id") or rec.get("finding_id") or ""),
        claim=str(rec.get("claim") or rec.get("title") or "(no claim)"),
        impact_class=str(rec.get("impact_class") or "routine"),
        admission_score=float(rec.get("admission_score") or 0.0),
        eval_passed=bool(rec.get("eval_passed", rec.get("eval_gate_passed", True))),
        evidence_label=str(rec.get("evidence_label") or "verified"),
        source_agent=str(rec.get("source_agent") or rec.get("source") or "hub"),
        proposed_action=str(rec.get("proposed_action") or ""),
        steps=tuple(str(s) for s in steps),
    )


def build_queue_from_hub(client: Optional[HubClient],
                         policy_path: str,
                         snapshot: SnapshotStore,
                         now: float):
    """Exception queue from the hub API with the REAL queue-policy.

    On API failure: degrade to snapshot (stale-with-timestamp) or mark the
    panel 'unavailable' — never fabricate rows.
    """
    q = qmod.Queue(db_path=":memory:", policy_path=policy_path, clock=lambda: now)
    if client is None:
        return q, {"state": "unavailable", "detail": "no hub credentials"}
    try:
        findings = client.get_findings()
    except HubReadError as e:
        return q, {"state": "unavailable", "detail": str(e)}
    for rec in findings if isinstance(findings, list) else []:
        try:
            q.admit(hub_findings_to_finding(rec))
        except (ValueError, TypeError, KeyError):
            continue
    return q, {"state": "live"}


def fetch_mandates(client: Optional[HubClient], snapshot: SnapshotStore):
    if client is None:
        return [], {"state": "unavailable", "detail": "no hub credentials"}
    try:
        mandates = client.get_mandates()
    except HubReadError as e:
        return [], {"state": "unavailable", "detail": str(e)}
    rows = mandates if isinstance(mandates, list) else (mandates or {}).get("mandates", [])
    ledger = [{
        "mandate_id": str(m.get("mandate_id") or m.get("id") or "?"),
        "scope": m.get("scope"),
        "expiry_at": m.get("expiry_at") or m.get("expiry"),
    } for m in rows if isinstance(m, dict)]
    return ledger, {"state": "live"}


# --------------------------------------------------------------------------- HTML

_CSS = """
body{font-family:-apple-system,'Segoe UI',sans-serif;margin:1.5rem;background:#0f1117;color:#e6e6e6}
h1{font-size:1.4rem} h2{font-size:1.1rem;margin-top:1.6rem;border-bottom:1px solid #2a2d3a;padding-bottom:.3rem}
table{border-collapse:collapse;width:100%;margin:.5rem 0}
th,td{border:1px solid #2a2d3a;padding:.35rem .55rem;text-align:left;font-size:.85rem}
th{background:#171a24}
tr.blocked td.status{color:#ff6b6b;font-weight:600}
tr.done-since-last-visit td.status{color:#7bd88f}
tr.waiting-on-you td.status,tr.working td.status{color:#ffd166}
.degraded{border:1px solid #b58900;background:#3a3000;padding:.35rem .6rem;border-radius:4px;font-size:.8rem;margin:.4rem 0;color:#ffd166}
.unavailable{border:1px solid #a33;background:#3a1010;padding:.35rem .6rem;border-radius:4px;font-size:.8rem;margin:.4rem 0;color:#ff9c9c}
.meta{color:#8a8fa3;font-size:.8rem}
.brief li{margin:.2rem 0}
.tag{display:inline-block;background:#1d2233;border-radius:3px;padding:0 .4rem;margin-right:.3rem}
"""


def _esc(v: Any) -> str:
    return html.escape("" if v is None else str(v))


def _degraded_banner(state: Dict[str, Any], panel: str) -> str:
    if state.get("state") == "live":
        return ""
    if state.get("state") == "stale":
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(state["read_ts"]))
        return (f'<div class="degraded" data-degraded="stale" data-panel="{panel}">'
                f'{panel}: stale (read {ts})</div>')
    return (f'<div class="unavailable" data-degraded="unavailable" data-panel="{panel}">'
            f'{panel}: unavailable ({_esc(state.get("detail", "source failure"))})</div>')


def _zone_a_html(board: Dict[str, Any], state: Dict[str, Any]) -> str:
    out = ["<h2>Zone A — project statuses</h2>", _degraded_banner(state, "Zone A")]
    rows = board["zone_a"]
    if not rows:
        return "".join(out) + "<p class=meta>No project rows.</p>"
    out.append("<table><tr><th>project</th><th>item</th><th>status</th><th>%</th>"
               "<th>ETA</th><th>tokens</th><th>detail</th></tr>")
    for r in rows:
        detail = ""
        if r["expanded"]:
            d = r.get("detail") or {}
            parts = []
            if d.get("in_progress_milestones"):
                parts.append("in progress: " + ", ".join(map(str, d["in_progress_milestones"])))
            if d.get("open_asks"):
                parts.append("asks: " + ", ".join(map(str, d["open_asks"])))
            if d.get("blockers"):
                parts.append("blockers: " + ", ".join(map(str, d["blockers"])))
            detail = _esc("; ".join(parts))
        out.append(
            f'<tr class="{_esc(r["status"])}">'
            f"<td>{_esc(r['project'])}</td><td>{_esc(r['item'])}</td>"
            f'<td class="status">{_esc(r["status"])}</td>'
            f'<td class="pct">{r["pct_complete"]:.0f}%</td>'
            f'<td class="eta">{_esc(r["eta"])}</td>'
            f"<td>{_esc(r['tokens'])}"
            + (f' <span class=meta>(untagged: {_esc(r["untagged_bucket"])})</span>' if r["untagged_bucket"] else "")
            + f"</td><td>{detail}</td></tr>")
    out.append("</table>")
    return "".join(out)


def _zone_b_html(board: Dict[str, Any], state: Dict[str, Any]) -> str:
    out = ["<h2>Zone B — exception / decision queue</h2>", _degraded_banner(state, "Zone B")]
    for q in board["zone_b"]:
        mandate = ""
        if q.get("mandate"):
            mandate = f' <span class=tag>mandate {_esc(q["mandate"]["mandate_id"])}</span>'
        out.append(
            f'<div class="queue-row panel-{_esc(q["panel"])}">'
            f'<span class=tag>Panel {_esc(q["panel"])}</span>'
            f"<b>{_esc(q['claim'])}</b> — {_esc(q['evidence_label'])} — "
            f"{_esc(q['source'])} — action: {_esc(q['proposed_action'])}{mandate} — "
            f"{_esc(q['age'])} — score {q['score']} — "
            f"actions: {_esc(' / '.join(q['actions']))}</div>")
    if not board["zone_b"]:
        out.append('<p class=meta>Queue empty.</p>')
    return "".join(out)


def _token_stats_html(tailer: ModelCallsTailer) -> str:
    totals = tailer.totals_by_item()
    out = ["<h2>Token stats (model-calls tailer)</h2>"]
    if not totals:
        out.append('<p class=meta>No token usage recorded.</p>')
        return "".join(out)
    out.append("<table><tr><th>item</th><th>calls</th><th>in tok</th><th>out tok</th><th>total tok</th></tr>")
    for item in sorted(totals):
        b = totals[item]
        out.append(f"<tr><td>{_esc(item)}</td><td>{b['calls']}</td><td>{b['in_tokens']}</td>"
                   f"<td>{b['out_tokens']}</td><td>{b['in_tokens'] + b['out_tokens']}</td></tr>")
    out.append("</table>")
    return "".join(out)


def render_html(board: Dict[str, Any], token_stats: str,
                zone_a_state: Dict[str, Any], zone_b_state: Dict[str, Any],
                zone_c_state: Dict[str, Any], generated_ts: float,
                exec_html: str = "") -> str:
    zone_c = board["zone_c"]
    brief = board.get("brief") or []
    qa = board.get("qa_tab") or {}
    brief_html = "".join(f"<li>{_esc(line)}</li>" for line in brief) or "<li class=meta>(no brief entries)</li>"
    mandates = zone_c.get("mandates_ledger") or []
    mand_rows = "".join(
        f"<tr><td>{_esc(m['mandate_id'])}</td><td>{_esc(json.dumps(m['scope']) if m.get('scope') is not None else '')}</td>"
        f"<td>{_esc(m.get('expiry_at'))}</td></tr>" for m in mandates)
    sp = zone_c.get("shadow_pulse") or {}
    qa_rows = "".join(
        f"<tr><td>{_esc(r['item_id'])}</td><td>{_esc(r['claim'])}</td>"
        f"<td>{_esc(r['evidence_label'])}</td><td>{_esc(r['source'])}</td></tr>"
        for r in qa.get("rows", []))
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Operator dashboard — daily board</title>
<style>{_CSS}</style></head><body>
<h1>Operator dashboard — daily board</h1>
<p class=meta>generated {time.strftime('%Y-%m-%d %H:%M', time.localtime(generated_ts))} ·
zone-a source: {_esc(zone_a_state.get('state'))} · zone-b source: {_esc(zone_b_state.get('state'))} ·
zone-c source: {_esc(zone_c_state.get('state'))}</p>
{exec_html}
{_zone_a_html(board, zone_a_state)}
{_zone_b_html(board, zone_b_state)}
<h2>Zone C — right rail</h2>{_degraded_banner(zone_c_state, "Zone C")}
<h3>Mandates ledger ({len(mandates)} standing grants)</h3>
<table><tr><th>mandate</th><th>scope</th><th>expiry</th></tr>{mand_rows}</table>
<h3>Shadow-week pulse</h3>
<p>{_esc(sp.get('would_have_done_today', 0))} would-have-done today — {_esc(sp.get('divergence_note', ''))}</p>
<h2>Daily brief</h2><ul class=brief>{brief_html}</ul>
<h2>QA sampling tab</h2>
<p class=meta>slice {qa.get('sample_size', 0)} of {qa.get('backlog_size', 0)} admitted findings
(rate {qa.get('sample_rate')}, seed {qa.get('seed')})</p>
<table><tr><th>item</th><th>claim</th><th>evidence</th><th>source</th></tr>{qa_rows}</table>
{token_stats}
</body></html>"""


# --------------------------------------------------------------------------- main

def build_board(args: argparse.Namespace, now: float) -> Dict[str, Any]:
    snapshot = SnapshotStore(args.snapshot)

    # --- Zone A: hub.db job runs (fail-closed via JobRunsDB backoff/degrade) ---
    zone_a_state: Dict[str, Any] = {"state": "live"}
    runs = HubJobRuns(args.hub_db)
    project_items: List[Dict[str, Any]] = []
    try:
        raw = runs.raw_rows()
        project_items = project_items_from_raw(raw)
    except (sqlite3.Error, LookupError, FileNotFoundError, OSError) as e:
        zone_a_state = {"state": "unavailable", "detail": f"hub.db: {e}"}
        runs = HubJobRuns(args.hub_db)  # fresh connection for retry inside render

    # --- sessions: same hub.db, single 'done since' source --------------------
    sessions = SessionsStore(hub_db_path=args.hub_db,
                             mirror_path=args.sessions_mirror)

    # --- token stats: ledger tailer -------------------------------------------
    tailer = ModelCallsTailer(ledger_path=args.ledger,
                              state_path=args.tail_state)

    # --- Zone B/C: hub API -----------------------------------------------------
    token = read_token(args.hub_token_file)
    client = None
    if token:
        client = HubClient(read_token=token, write_token=token, base_url=args.hub_url)
    queue, zone_b_state = build_queue_from_hub(client, args.queue_policy, snapshot, now)
    if zone_b_state["state"] == "unavailable":
        stale = snapshot.get_stale("zone_b")
        if stale["state"] == "stale":
            zone_b_state = {"state": "stale", "read_ts": stale["read_ts"]}
    mandates, zone_c_state = fetch_mandates(client, snapshot)
    if zone_c_state["state"] == "unavailable":
        stale = snapshot.get_stale("zone_c")
        if stale["state"] == "stale":
            zone_c_state = {"state": "stale", "read_ts": stale["read_ts"]}

    board = board_render.render_board(
        project_items, runs, tailer, sessions, queue, snapshot=snapshot,
        mandates_ledger=mandates, shadow_pulse={},
        evidence_artifacts={}, now=now)

    if zone_a_state["state"] == "live" and board["runs_state"] != "live":
        zone_a_state = {"state": board["runs_state"]}
    if zone_a_state["state"] == "live":
        snapshot.save("zone_a", board["zone_a"], now)
    elif zone_a_state["state"] == "unavailable":
        stale = snapshot.get_stale("zone_a")
        if stale["state"] == "stale" and stale.get("payload"):
            board["zone_a"] = stale["payload"]
            zone_a_state = {"state": "stale", "read_ts": stale["read_ts"]}
    if zone_b_state["state"] == "live" and board["zone_b"]:
        snapshot.save("zone_b", board["zone_b"], now)
    if zone_c_state["state"] == "live":
        snapshot.save("zone_c", mandates, now)
    board["zone_a_state"] = zone_a_state
    board["zone_b_state"] = zone_b_state
    board["zone_c_state"] = zone_c_state
    board["tailer"] = tailer
    return board


def _write_atomic(path: str, text: str) -> None:
    out_dir = os.path.dirname(os.path.abspath(path))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Render the operator daily board to static HTML.")
    p.add_argument("--hub-db", default=DEFAULT_HUB_DB)
    p.add_argument("--hub-url", default=DEFAULT_HUB_URL)
    p.add_argument("--hub-token-file", default=DEFAULT_TOKEN_FILE)
    p.add_argument("--ledger", default=DEFAULT_LEDGER)
    p.add_argument("--out", default=DEFAULT_OUT)
    p.add_argument("--queue-policy", default=qmod.POLICY_PATH)
    p.add_argument("--snapshot", default=DEFAULT_SNAPSHOT)
    p.add_argument("--sessions-mirror", default=os.path.expanduser("~/.hermes/dashboard/sessions-mirror.json"))
    p.add_argument("--tail-state", default=os.path.expanduser("~/.hermes/dashboard/model_calls_tail_state.json"))
    p.add_argument("--exec-summary", dest="exec_summary", action="store_true", default=True,
                   help="gather yesterday's activity and prepend the exec-summary section (default on)")
    p.add_argument("--no-exec-summary", dest="exec_summary", action="store_false",
                   help="skip the exec-summary layer entirely")
    p.add_argument("--exec-hub-db", default=None, help="override hub.db for the exec-summary gather")
    p.add_argument("--exec-ledger", default=None)
    p.add_argument("--exec-bridge-log", default=exec_summary.DEFAULT_BRIDGE_LOG)
    p.add_argument("--exec-digest", default=exec_summary.DEFAULT_DIGEST)
    p.add_argument("--exec-dogfood-log", default=exec_summary.DEFAULT_DOGFOOD_LOG)
    p.add_argument("--exec-scheduler-log", default=exec_summary.DEFAULT_SCHEDULER_LOG)
    p.add_argument("--exec-shadow-dir", default=exec_summary.DEFAULT_SHADOW_DIR,
                   help="shadow-harness root: scheduler.log + hierarchy run workspaces "
                        "(source for the agent-actions line)")
    p.add_argument("--window", choices=("rolling", "yesterday"), default="yesterday",
                   help="exec-summary window: 'yesterday' (daily board/email) or "
                        "'rolling' (last 24h from now, on-demand)")
    p.add_argument("--depth", choices=("brief", "full"), default="full",
                   help="exec-summary depth: 'brief' = the email 15-second read "
                        "(no themes prose/stack overlap/recommendations); "
                        "'full' = console exec-summary (default)")
    p.add_argument("--console-url", default=exec_summary.DEFAULT_CONSOLE_URL,
                   help="link put in the email footer (static text; no runtime "
                        "dependency on the console at send time)")
    p.add_argument("--email-text-out", default=None,
                   help="write the plain-text email part to this path (v2 email)")
    p.add_argument("--email-html-out", default=None,
                   help="write the email-safe inline-HTML email part to this path")
    p.add_argument("--scrapes-db", default=scrape_intel.DEFAULT_SCRAPES_DB,
                   help="scrape store for the scrape-intelligence section "
                        "(default: the profile scrapes_db)")
    p.add_argument("--project-keywords", default=scrape_intel.DEFAULT_REFERENCES_YAML,
                   help="yaml mapping project slug -> keywords for stack overlap "
                        "(default: docs/references.yaml in the repo; missing file "
                        "-> no stack-overlap rows, never fabricated)")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    now = time.time()
    board = build_board(args, now)

    exec_html = ""
    if args.exec_summary:
        project_keywords = scrape_intel.load_project_keywords(args.project_keywords)
        exec_data = exec_summary.gather(
            now=now,
            window_mode=args.window,
            hub_db=args.exec_hub_db or args.hub_db,
            ledger=args.exec_ledger or args.ledger,
            bridge_log=args.exec_bridge_log,
            digest=args.exec_digest,
            dogfood_log=args.exec_dogfood_log,
            scheduler_log=args.exec_scheduler_log,
            shadow_dir=args.exec_shadow_dir,
            scrapes_db=args.scrapes_db,
            project_keywords=project_keywords)
        _, ds_result = exec_summary.write_summary_section(exec_data)
        if args.depth == "brief":
            # Email/on-demand brief: drop the deep DS prose blocks from the
            # exec section (themes prose, stack overlap, recommendations).
            ds_result = dict(ds_result)
            blocks = dict(ds_result.get("blocks") or {})
            for k in ("scrape_themes", "stack_overlap", "recommendations"):
                blocks.pop(k, None)
            ds_result["blocks"] = blocks
        exec_html = exec_summary.summary_html(exec_data, ds_result)

        if args.email_text_out or args.email_html_out:
            if args.email_text_out:
                _write_atomic(args.email_text_out,
                              exec_summary.email_text(exec_data, ds_result,
                                                      args.console_url))
            if args.email_html_out:
                _write_atomic(args.email_html_out,
                              exec_summary.email_html(exec_data, ds_result,
                                                      args.console_url))

    html_out = render_html(board, _token_stats_html(board["tailer"]),
                           board["zone_a_state"], board["zone_b_state"],
                           board["zone_c_state"], now, exec_html)
    out_dir = os.path.dirname(os.path.abspath(args.out))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html_out)
    os.replace(tmp, args.out)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
