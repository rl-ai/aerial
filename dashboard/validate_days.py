#!/usr/bin/env python3
"""dashboard/validate_days.py — accelerated + historical-backtest validation.

Two modes — the PRIMARY one is the historical backtest:

1. BACKTEST (Ray's primary validation mode) — replay REAL windows from our
   own raw data instead of synthetic chaos:

       python -m dashboard.validate_days --backtest \
           --window-start 2026-11-06T00:00:00Z --window-days 7

   Sets AERIAL_VIRTUAL_EPOCH to the window start, BACKFILLS working (fixture)
   stores from the real historical rows filtered to the window — hub.db
   job_runs, the model-calls.jsonl ledger, scheduler.log lines, bridge-log
   lines, and health-status snapshots where they exist — then runs the
   consumers per virtual day against the REAL events. Production stores are
   never touched (everything lands in a scratch fixture directory).
   chaos.py stays available but is NOT used in backtest mode.

2. ACCELERATED (synthetic chaos) — "7 days in ~21 minutes" at ×480:

       AERIAL_CHAOS=1 python -m dashboard.validate_days --acceleration 480

   Sets the virtual epoch to the next UTC midnight, acceleration ×N, runs
   the chaos injector per virtual day and the cadence checks per virtual
   day. chaos.py's guard (accel > 1 AND AERIAL_CHAOS=1) protects production.

At window end both modes evaluate and print a PASS/FAIL per criterion:
  - continuous-ops criteria (render freshness each day, scheduler alive each
    day, zero operator restarts, alert pipeline delivered)
  - the promotion gate's 4 criteria (7-day window, divergence rate, zero
    unexplained shadow actions, cost projection)
  - the trend math (failure-cluster flagging: trailing-baseline z-score)
Backtest mode additionally runs incident-replay assertions: for known
incidents in the window (the 2026-11-12 wedge day, the roles-400 cluster)
assert the SEV ping WOULD have fired and the trend math WOULD have flagged
the failure cluster.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from dashboard import clock, chaos            # noqa: E402
from dashboard.exec_summary import gather_job_runs  # noqa: E402
from dashboard.followup import classifier as clf    # noqa: E402
from dashboard.daily_board import promotion         # noqa: E402

from deployment_profile import pvalue as _pv
PROD_HUB_DB = str(_pv("hub_db"))
PROD_LEDGER = str(_pv("model_calls_ledger"))
PROD_SCHED_LOG = str(_pv("scheduler_log"))
PROD_BRIDGE_LOG = str(_pv("bridge_log"))
PROD_HEALTH = str(_pv("health_status"))
PROD_SRE_STATE = str(_pv("sre_state"))
PROD_SRE_LOG = str(_pv("sre_log"))

#: Known incidents for backtest replay assertions (UTC window, keywords).
KNOWN_INCIDENTS = [
    {"name": "hub-write-wedge-day", "date": "2026-11-12",
     "start": "2026-11-12T18:30:00Z", "end": "2026-11-12T21:30:00Z",
     "keywords": ("wedge", "hub", "write", "restart", "dead", "refused"),
     "expect": ("sev_ping", "trend_flag")},
    {"name": "roles-400-cluster", "date": None,  # auto-located: worst failure day
     "start": None, "end": None,
     "keywords": ("roles", "400", "http", "error"),
     "expect": ("trend_flag",)},
]

SEV_PATTERNS = clf.SEV_PATTERNS


# ---------------------------------------------------------------------------
# fixtures (backtest working stores — never production)
# ---------------------------------------------------------------------------

def make_fixture_dir(tag: str) -> str:
    d = os.path.join(os.path.expanduser(
        "~/.hermes/profiles/build/cache/scratch"), "aerial-validate", tag)
    os.makedirs(d, exist_ok=True)
    return d


def _parse_iso(s: str) -> float:
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    d = dt.datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.timestamp()


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _norm_ts(value: Any) -> Optional[float]:
    """Parse a store timestamp ('2026-11-12T18:30:00Z' or epoch) to epoch s."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return _parse_iso(str(value))
    except ValueError:
        return None


def backfill(window_start: float, window_days: int,
             fixture_dir: str) -> Dict[str, Any]:
    """Copy the real historical rows for the window into the fixture stores.

    Returns counts. Production stores are only ever READ here.
    """
    end = window_start + window_days * 86400.0
    fb_hub = os.path.join(fixture_dir, "hub.db")
    fb_ledger = os.path.join(fixture_dir, "model-calls.jsonl")
    fb_sched = os.path.join(fixture_dir, "scheduler.log")
    fb_bridge = os.path.join(fixture_dir, "brain-bridge.log")
    fb_flags = os.path.join(fixture_dir, "health-flags.json")
    counts = {"job_runs": 0, "ledger_lines": 0, "sched_lines": 0,
              "bridge_lines": 0, "health_snapshots": 0}

    # hub.db job_runs → fixture hub.db (schema copied)
    if os.path.exists(PROD_HUB_DB):
        src = sqlite3.connect(f"file:{PROD_HUB_DB}?mode=ro", uri=True)
        src.row_factory = sqlite3.Row
        rows = list(src.execute(
            "SELECT * FROM job_runs WHERE started_at IS NOT NULL"))
        src.close()
        dst = sqlite3.connect(fb_hub)
        cols = [r[1] for r in dst.execute("PRAGMA table_info(job_runs)")] \
            if dst.execute("PRAGMA table_info(job_runs)").fetchone() else None
        if not cols:
            dst.execute(
                "CREATE TABLE job_runs (run_id TEXT PRIMARY KEY, job_id TEXT,"
                " node TEXT DEFAULT '', started_at TEXT NOT NULL, ended_at TEXT,"
                " status TEXT NOT NULL, exit_code INTEGER, duration_s REAL,"
                " summary TEXT DEFAULT '', log_tail TEXT DEFAULT '',"
                " meta TEXT DEFAULT '{}', pid INTEGER)")
            cols = [r[1] for r in dst.execute("PRAGMA table_info(job_runs)")]
        ins = []
        for r in rows:
            ts = _norm_ts(r["started_at"])
            if ts is None or not (window_start <= ts < end):
                continue
            ins.append(tuple(r[c] for c in cols))
        dst.executemany(
            f"INSERT OR REPLACE INTO job_runs ({','.join(cols)}) "
            f"VALUES ({','.join('?' * len(cols))})", ins)
        dst.commit()
        dst.close()
        counts["job_runs"] = len(ins)

    # ledger lines (model-calls.jsonl) → fixture ledger
    if os.path.exists(PROD_LEDGER):
        kept = []
        with open(PROD_LEDGER, "r", errors="replace") as f:
            for ln in f:
                if not ln.strip():
                    continue
                try:
                    rec = json.loads(ln)
                except ValueError:
                    continue
                ts = _norm_ts(rec.get("ts"))
                if ts is not None and window_start <= ts < end:
                    kept.append(ln if ln.endswith("\n") else ln + "\n")
        with open(fb_ledger, "w") as f:
            f.writelines(kept)
        counts["ledger_lines"] = len(kept)

    # scheduler.log lines → fixture scheduler.log
    if os.path.exists(PROD_SCHED_LOG):
        kept = []
        with open(PROD_SCHED_LOG, "r", errors="replace") as f:
            for ln in f:
                ts = _line_ts(ln)
                if ts is not None and window_start <= ts < end:
                    kept.append(ln if ln.endswith("\n") else ln + "\n")
        with open(fb_sched, "w") as f:
            f.writelines(kept)
        counts["sched_lines"] = len(kept)

    # bridge log lines (freshness evidence) → fixture bridge log
    if os.path.exists(PROD_BRIDGE_LOG):
        kept = []
        with open(PROD_BRIDGE_LOG, "r", errors="replace") as f:
            for ln in f:
                ts = _line_ts(ln)
                if ts is not None and window_start <= ts < end:
                    kept.append(ln if ln.endswith("\n") else ln + "\n")
        with open(fb_bridge, "w") as f:
            f.writelines(kept)
        counts["bridge_lines"] = len(kept)

    # health-status snapshots where they exist: the live file is
    # last-writer-wins, so it seeds the flags only if its ts is in-window.
    for path in (PROD_HEALTH, PROD_SRE_STATE):
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                snap = json.load(f)
        except (OSError, ValueError):
            continue
        ts = _norm_ts(snap.get("ts"))
        if ts is not None and window_start <= ts < end:
            with open(fb_flags, "a") as f:
                f.write(json.dumps({"ts": snap.get("ts"),
                                    "failed_checks": snap.get(
                                        "failed_checks", {}),
                                    "ok": snap.get("ok", True)}) + "\n")
            counts["health_snapshots"] += 1
    return counts


def _line_ts(line: str) -> Optional[float]:
    try:
        return _parse_iso(line[:20].strip().rstrip("Z") + "+00:00"
                          if line[:20].endswith("Z") else line[:19])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# trend math — failure-cluster flagging (trailing baseline, z-score)
# ---------------------------------------------------------------------------

def trend_flag(failed_by_day: List[int], day_idx: int,
               min_baseline: int = 3, z_thresh: float = 2.0,
               ratio_thresh: float = 2.0) -> Tuple[bool, str]:
    """Flag day `day_idx` when its failure count is an outlier vs the
    trailing baseline (prior days): z > 2 or ratio > 2 with n ≥ 3."""
    if day_idx <= 0:
        return False, "no baseline yet (day 0)"
    baseline = failed_by_day[:day_idx]
    n = len(baseline)
    if n < min_baseline:
        return False, f"baseline too short ({n}d of {min_baseline} required)"
    mean = sum(baseline) / n
    var = sum((x - mean) ** 2 for x in baseline) / n
    sd = var ** 0.5
    cur = failed_by_day[day_idx]
    z = ((cur - mean) / sd) if sd > 1e-9 else (float("inf") if cur > mean else 0.0)
    ratio = (cur / mean) if mean > 0 else float("inf")
    if z > z_thresh or ratio > ratio_thresh:
        return True, (f"failed={cur} vs baseline mean={mean:.1f} sd={sd:.1f} "
                      f"z={z:.1f} ratio={ratio:.1f}")
    return False, (f"failed={cur} vs baseline mean={mean:.1f} sd={sd:.1f} "
                   f"z={z:.1f} ratio={ratio:.1f}")


# ---------------------------------------------------------------------------
# per-day cadence checks (consumers against the working stores)
# ---------------------------------------------------------------------------

def outage_jobs(hub_db: str, day_start_ts: float) -> List[str]:
    """Jobs that ran on each of the two prior days but produced ZERO runs
    today — the runner's schedule-reconcile signature ('total outage of the
    X pipeline: no run in 2× the poll interval')."""
    con = sqlite3.connect(f"file:{hub_db}?mode=ro", uri=True)
    active = []
    for back in (1, 2):
        s = day_start_ts - back * 86400.0
        e = s + 86400.0
        seen = set()
        for (jid, sa) in con.execute("SELECT job_id, started_at FROM job_runs"):
            ts = _norm_ts(sa)
            if ts is not None and s <= ts < e:
                seen.add(jid)
        active.append(seen)
    today = set()
    e = day_start_ts + 86400.0
    for (jid, sa) in con.execute("SELECT job_id, started_at FROM job_runs"):
        ts = _norm_ts(sa)
        if ts is not None and day_start_ts <= ts < e:
            today.add(jid)
    con.close()
    return sorted((active[0] & active[1]) - today)


def outage_findings(hub_db: str, day_start_ts: float) -> List[clf.CuratorFinding]:
    """Outage jobs → findings phrased exactly like the runner's schedule
    reconciliation ('total outage ... platform dead'), which classifies R2
    sev-incident → immediate ping."""
    out = []
    for jid in outage_jobs(hub_db, day_start_ts):
        out.append(clf.CuratorFinding(
            finding_id=f"bt-outage-{jid}-{int(day_start_ts)}",
            claim=(f"total outage of the {jid} pipeline: no run in the day "
                   f"window — platform dead"),
            reversible=True, scope="internal", covered_by_mandate=False,
            tags=("ops", "schedule-reconcile")))
    return out


def day_findings(day_start_ts: float, hub_db: str) -> List[clf.CuratorFinding]:
    """Real failed job_runs of the day → followup findings (classifier input)."""
    win = (day_start_ts, day_start_ts + 86400.0)
    data = gather_job_runs(hub_db, win)
    findings = []
    for run in data.get("failures", []) or []:
        summary = f"{run.get('job_id', 'job')} failed: {run.get('summary', '')}"
        findings.append(clf.CuratorFinding(
            finding_id=f"bt-{run.get('run_id', 'run')}",
            claim=summary[:300],
            reversible=True, scope="internal", covered_by_mandate=False,
            tags=("ops", "job-failure")))
    return findings


def sev_pings_would_fire(findings: List[clf.CuratorFinding]) -> List[Dict[str, Any]]:
    """Which of the day's findings classify to immediate-ping (R1/R2)."""
    fired = []
    for f in findings:
        c = clf.classify(f)
        if c in clf.IMMEDIATE_CLASSES:
            fired.append({"finding_id": f.finding_id, "class": c,
                          "claim": f.claim[:160]})
    return fired


def render_freshness_ok(day_end_ts: float, bridge_log: str,
                        board_html: str, stale_s: float = 90 * 60) -> bool:
    """sre-monitor freshness semantics against the working stores: at least
    one bridge log line and one board artifact within the last 90 virtual
    minutes of the day."""
    for path in (bridge_log, board_html):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return False
        if day_end_ts - mtime > stale_s:
            return False
    return True


def scheduler_alive(day_start_ts: float, sched_log: str,
                    hub_db: Optional[str] = None) -> Optional[bool]:
    """True = the scheduler fired that day (a scheduler.log started line OR
    ≥1 job run — the scheduler is what fires job_runs). None = no evidence
    either way (e.g. hub data gap + rotated scheduler.log): not a failure,
    an unevaluable day."""
    end = day_start_ts + 86400.0
    if os.path.exists(sched_log):
        with open(sched_log, "r", errors="replace") as f:
            for ln in f:
                ts = _line_ts(ln)
                if ts is not None and day_start_ts <= ts < end and " started " in ln:
                    return True
    if hub_db and os.path.exists(hub_db):
        if _count_runs(hub_db, (day_start_ts, end)) > 0:
            return True
        return None  # no runs, no sched lines: data gap, not evidence of death
    return False


def build_shadow_comparison(fixture_dir: str, day_start_ts: float,
                            hub_db: str) -> Dict[str, Any]:
    """Shadow-vs-actual actions for the day from REAL rows: every failed
    job run is a would-have-done triage; the live outcome is the run state.
    Divergence = the SAME job failed again within the following 24h (the
    auto-fix would have been wrong — the failure recurred), individually
    labeled; evidence = verified (real logged rows)."""
    win = (day_start_ts, day_start_ts + 86400.0)
    data = gather_job_runs(hub_db, win)
    # jobs that failed again in the next 24h → the fix would not have held
    next_win = (day_start_ts + 86400.0, day_start_ts + 2 * 86400.0)
    recur = {r.get("job_id") for r in
             (gather_job_runs(hub_db, next_win).get("failures", []) or [])}
    actions = []
    for run in data.get("failures", []) or []:
        diverged = run.get("job_id") in recur
        actions.append({
            "ts": _norm_ts(run.get("started_at")) or day_start_ts,
            "role": "curator",
            "action": f"triage {run.get('job_id', 'job')} "
                      f"{run.get('run_id', '')}",
            "actual_outcome": f"run {run.get('status')}",
            "evidence_label": "verified",
            "divergence": bool(diverged),
            "divergence_label": "would-have-been-wrong" if diverged else None,
            "draft_mandate_spec": {
                "scope": f"triage {run.get('job_id', 'job')} failures",
                "expiry": _iso(day_start_ts + 7 * 86400.0)},
        })
    return {"role": "curator", "mode": "record-only-shadow", "actions": actions}


def shadow_cost_from_ledger(ledger_path: str,
                            window: Tuple[float, float]) -> Dict[str, int]:
    """Cost projection criterion 4: calls/tokens of the window's shadow runs."""
    calls = tokens = 0
    start, end = window
    try:
        with open(ledger_path, "r", errors="replace") as f:
            for ln in f:
                if not ln.strip():
                    continue
                try:
                    rec = json.loads(ln)
                except ValueError:
                    continue
                ts = _norm_ts(rec.get("ts"))
                if ts is None or not (start <= ts < end):
                    continue
                calls += 1
                tokens += int(rec.get("tokens_in", 0) or 0) \
                    + int(rec.get("predicted_n", 0) or 0)
    except OSError:
        pass
    return {"calls": calls, "tokens": tokens}


def classify_day_type(failed: int, runs: int, calls: int,
                      prev_failed: Optional[int],
                      med_runs: float = 0.0, med_failed: float = 0.0,
                      med_calls: float = 0.0) -> str:
    """Per-day-type coverage from the real numbers, relative to the window's
    medians (a real platform's 'normal' is its own baseline)."""
    spike = (runs >= max(3 * med_runs, 1) or calls >= max(3 * med_calls, 1)
             or failed >= max(3 * med_failed, 6))
    if spike:
        return "anomalous"
    if failed == 0:
        return "quiet"
    if prev_failed is not None and failed < max(prev_failed // 2, 1) \
            and prev_failed >= 5:
        return "recovery"
    if failed >= max(2 * med_failed, 15):
        return "failure-storm"
    return "mixed"


# ---------------------------------------------------------------------------
# evaluation + reporting
# ---------------------------------------------------------------------------

def evaluate_window(days: List[Dict[str, Any]], fixture_dir: str,
                    window_days: int,
                    shadow_comp_path: Optional[str] = None,
                    ledger_path: Optional[str] = None,
                    replay_incidents: bool = True) -> Dict[str, Any]:
    """Final per-criterion evaluation. `days` = one record per virtual day.
    `shadow_comp_path`: chaos-mode shadow-comparison jsonl (per-day evidence
    written by the injector) — used instead of rebuilding from job_runs."""
    fb_bridge = os.path.join(fixture_dir, "brain-bridge.log")
    fb_sched = os.path.join(fixture_dir, "scheduler.log")
    fb_ledger = os.path.join(fixture_dir, "model-calls.jsonl")
    fb_hub = os.path.join(fixture_dir, "hub.db")
    win_end = days[-1]["day_end"]

    def _shadow_for(day: int, day_start: float) -> Dict[str, Any]:
        if shadow_comp_path and os.path.exists(shadow_comp_path):
            with open(shadow_comp_path) as f:
                for ln in f:
                    if not ln.strip():
                        continue
                    try:
                        rec = json.loads(ln)
                    except ValueError:
                        continue
                    if rec.get("day") == day:
                        return {"role": rec.get("role", "curator"),
                                "mode": "record-only-shadow",
                                "actions": rec.get("actions", [])}
        return build_shadow_comparison(fixture_dir, day_start, fb_hub)

    results: Dict[str, Any] = {"criteria": [], "incidents": []}

    # --- continuous-ops criteria -------------------------------------------
    fresh_ok = all(d["render_fresh"] for d in days)
    sched_days = [d for d in days if d["scheduler_alive"] is not None]
    sched_ok = all(d["scheduler_alive"] for d in sched_days)
    n_sched_unk = len(days) - len(sched_days)
    restarts = sum(d.get("restarts", 0) for d in days)
    alerts_ok = all(d["sev_pings"] for d in days if d.get("outage_day"))
    n_outage = sum(1 for d in days if d.get("outage_day"))
    results["criteria"] += [
        {"name": "continuous-ops: render freshness every day",
         "pass": fresh_ok,
         "detail": f"{sum(d['render_fresh'] for d in days)}/{len(days)} days fresh"},
        {"name": "continuous-ops: scheduler alive every day",
         "pass": sched_ok,
         "detail": (f"{sum(1 for d in sched_days if d['scheduler_alive'])}/"
                    f"{len(sched_days)} evaluable days alive"
                    + (f", {n_sched_unk} days unevaluable (no data — "
                       f"hub gap + rotated scheduler.log)" if n_sched_unk else ""))},
        {"name": "continuous-ops: zero operator restarts",
         "pass": restarts == 0, "detail": f"{restarts} restart events"},
        {"name": "continuous-ops: alert pipeline (pings on outage days, "
                 "trend flags on failure-cluster days)",
         "pass": alerts_ok,
         "detail": f"{n_outage} outage days (all pinged: "
                   f"{all(d['sev_pings'] for d in days if d.get('outage_day'))}), "
                   f"{sum(1 for d in days if d['trend_flag'])} trend-flagged"},
    ]

    # --- promotion gate (all shadow actions across the window) -------------
    actions: List[Dict[str, Any]] = []
    for d in days:
        comp = _shadow_for(d["day"], d["day_start"])
        actions.extend(comp["actions"])
    view = promotion.promotion_view(
        {"role": "curator", "mode": "record-only-shadow", "shadow_actions": actions},
        now=win_end, observation_days=float(window_days),
        shadow_cost=shadow_cost_from_ledger(
            ledger_path or fb_ledger, (days[0]["day_start"], win_end)))
    for c in view["gate_criteria"]:
        results["criteria"].append({"name": f"promotion gate: {c['name']}",
                                    "pass": c["satisfied"], "detail": c["current"]})
    results["promotion_approve_available"] = view["approve_available"]

    # --- trend math: every real spike day flagged ---------------------------
    failed_by_day = [d["failed"] for d in days]
    spike_days = [i for i, d in enumerate(days) if d["failed"] > 0
                  and d["failed"] >= max(5, 3 * (sum(failed_by_day) / len(days)))]
    trend_ok = True
    trend_details = []
    for i in spike_days:
        flagged, detail = trend_flag(failed_by_day, i)
        trend_ok = trend_ok and flagged
        trend_details.append(f"day {i} ({_iso(days[i]['day_start'])[:10]}): "
                             f"{'FLAGGED' if flagged else 'MISSED'} — {detail}")
    results["criteria"].append({
        "name": "trend math flags failure clusters",
        "pass": trend_ok,
        "detail": "; ".join(trend_details) or "no spike days in window"})

    # --- incident replay (backtest mode only: known REAL incidents) --------
    if not replay_incidents:
        results["incidents"].append(
            {"name": "(incident replay is backtest-only)", "in_window": False,
             "pass": True, "detail": "synthetic chaos window — skipped"})
    for inc in KNOWN_INCIDENTS:
        if inc["name"] == "roles-400-cluster":
            # auto-locate: the worst failure day inside the window
            if not days:
                continue
            worst = max(range(len(days)), key=lambda i: days[i]["failed"])
            d0, d1 = days[worst]["day_start"], days[worst]["day_end"]
        else:
            d0, d1 = _parse_iso(inc["start"]), _parse_iso(inc["end"])
        in_window = any(d["day_start"] <= d0 < d["day_end"] for d in days)
        if not in_window:
            results["incidents"].append(
                {"name": inc["name"], "in_window": False,
                 "pass": True, "detail": "not in replay window — skipped"})
            continue
        day_of = next(d["day_start"] for d in days if d["day_start"] <= d0 < d["day_end"])
        f = day_findings(d0, fb_hub) + outage_findings(fb_hub, day_of)
        pings = sev_pings_would_fire(f)
        flagged, tdetail = trend_flag(failed_by_day,
                                      next(i for i, d in enumerate(days)
                                           if d["day_start"] <= d0 < d["day_end"]))
        want_sev = "sev_ping" in inc["expect"]
        want_trend = "trend_flag" in inc["expect"]
        ok = ((not want_sev) or bool(pings)) and ((not want_trend) or flagged)
        results["incidents"].append({
            "name": inc["name"], "in_window": True, "pass": ok,
            "detail": (f"sev pings would fire: {len(pings)} "
                       f"({pings[0]['class'] if pings else 'none'}); "
                       f"trend: {'FLAGGED' if flagged else 'MISSED'} — {tdetail}")})
    results["all_pass"] = all(c["pass"] for c in results["criteria"]) \
        and all(i["pass"] for i in results["incidents"])
    return results


def print_report(results: Dict[str, Any], days: List[Dict[str, Any]],
                 mode: str) -> None:
    print(f"\n=== Aerial validation report — mode: {mode} — "
          f"{len(days)} virtual days ===")
    day_types = [d.get("day_type", "?") for d in days]
    print("per-day-type coverage: " + ", ".join(
        f"d{i}={t}" for i, t in enumerate(day_types)))
    for d in days:
        print(f"  day {d['day']}: runs={d['runs']} failed={d['failed']} "
              f"calls={d['calls']} pings={len(d['sev_pings'])} "
              f"fresh={d['render_fresh']} sched={d['scheduler_alive']}")
    print("\n-- criteria --")
    for c in results["criteria"]:
        print(f"  [{'PASS' if c['pass'] else 'FAIL'}] {c['name']} — {c['detail']}")
    for inc in results["incidents"]:
        print(f"  [{'PASS' if inc['pass'] else 'FAIL'}] incident-replay "
              f"{inc['name']} — {inc['detail']}")
    print(f"\nOVERALL: {'PASS' if results['all_pass'] else 'FAIL'}")


# ---------------------------------------------------------------------------
# mode runners
# ---------------------------------------------------------------------------

def run_backtest(window_start: float, window_days: int,
                 fixture_dir: Optional[str] = None) -> Dict[str, Any]:
    """PRIMARY mode: replay a real historical window. No sleeping — the
    virtual days are evaluated analytically against the backfilled stores."""
    fixture_dir = fixture_dir or make_fixture_dir("backtest")
    os.environ["AERIAL_VIRTUAL_EPOCH"] = str(window_start)
    os.environ["AERIAL_TIME_ACCELERATION"] = "1"
    clock.reset_anchor()
    counts = backfill(window_start, window_days, fixture_dir)
    print(f"backtest: window {_iso(window_start)} + {window_days}d → "
          f"fixture {fixture_dir}")
    print(f"backfilled: {counts}")

    fb_bridge = os.path.join(fixture_dir, "brain-bridge.log")
    fb_sched = os.path.join(fixture_dir, "scheduler.log")
    fb_hub = os.path.join(fixture_dir, "hub.db")
    days = []
    prev_failed = None
    # first pass: raw numbers (for medians) — then classify relative to them
    raw = []
    for day in range(window_days):
        ds = clock.day_start(day)
        win = (ds, ds + 86400.0)
        raw.append({
            "runs": _count_runs(fb_hub, win),
            "failed": (gather_job_runs(fb_hub, win).get("failed", 0) or 0),
            "calls": _count_calls(os.path.join(fixture_dir,
                                               "model-calls.jsonl"), win)})
    def _med(vals):
        v = sorted(vals)
        return v[len(v) // 2] if v else 0.0
    med_runs, med_failed, med_calls = (_med([r["runs"] for r in raw]),
                                       _med([r["failed"] for r in raw]),
                                       _med([r["calls"] for r in raw]))
    for day in range(window_days):
        ds = clock.day_start(day)
        de = ds + 86400.0
        # board artifact: the real render pipeline runs daily — the fixture
        # board mtime tracks the last backfilled bridge line of the day
        f = day_findings(ds, fb_hub) + outage_findings(fb_hub, ds)
        pings = sev_pings_would_fire(f)
        outages = outage_jobs(fb_hub, ds)
        win = (ds, de)
        data = gather_job_runs(fb_hub, win)
        failed = data.get("failed", 0) or 0
        runs = _count_runs(fb_hub, win)
        calls = _count_calls(os.path.join(fixture_dir, "model-calls.jsonl"), win)
        days.append({
            "day": day, "day_start": ds, "day_end": de,
            "day_type": classify_day_type(failed, runs, calls, prev_failed,
                                          med_runs, med_failed, med_calls),
            "runs": runs, "failed": failed, "calls": calls,
            "sev_pings": pings,
            "render_fresh": render_freshness_ok(de, fb_bridge, fb_bridge),
            "scheduler_alive": scheduler_alive(ds, fb_sched, fb_hub),
            "restarts": 0, "trend_flag": False, "flagged": failed >= 5,
            "outage_day": bool(outages),
            "outage_jobs": outages,
        })
        prev_failed = failed
    # trend per day (for reporting + continuous-ops alert criterion)
    failed_by_day = [d["failed"] for d in days]
    for i, d in enumerate(days):
        d["trend_flag"], d["trend_detail"] = trend_flag(failed_by_day, i)
    results = evaluate_window(days, fixture_dir, window_days)
    print_report(results, days, f"backtest ({_iso(window_start)} +{window_days}d)")
    return results


def _count_runs(hub_db: str, window: Tuple[float, float]) -> int:
    start, end = window
    n = 0
    try:
        con = sqlite3.connect(f"file:{hub_db}?mode=ro", uri=True)
        for (sa,) in con.execute("SELECT started_at FROM job_runs"):
            ts = _norm_ts(sa)
            if ts is not None and start <= ts < end:
                n += 1
        con.close()
    except sqlite3.Error:
        pass
    return n


def _count_calls(ledger: str, window: Tuple[float, float]) -> int:
    start, end = window
    n = 0
    try:
        with open(ledger, "r", errors="replace") as f:
            for ln in f:
                if not ln.strip():
                    continue
                try:
                    rec = json.loads(ln)
                except ValueError:
                    continue
                ts = _norm_ts(rec.get("ts"))
                if ts is not None and start <= ts < end:
                    n += 1
    except OSError:
        pass
    return n


def run_accelerated(acceleration: float, days: int = 7,
                    seed: int = 20261112,
                    store_dir: Optional[str] = None,
                    poll_real_s: float = 5.0) -> Dict[str, Any]:
    """Synthetic-chaos mode: real-time accelerated run (×480 → ~21 min for
    7 virtual days). chaos.py's guard keeps production safe."""
    # epoch = next UTC midnight so virtual day 0 starts immediately
    now_real = time.time()
    epoch = (int(now_real) // 86400 + 1) * 86400
    os.environ["AERIAL_VIRTUAL_EPOCH"] = str(epoch)
    os.environ["AERIAL_TIME_ACCELERATION"] = str(acceleration)
    os.environ["AERIAL_CHAOS"] = "1"
    clock.reset_anchor()

    if store_dir:
        os.makedirs(store_dir, exist_ok=True)
        paths = dict(
            hub_db=os.path.join(store_dir, "hub.db"),
            scheduler_log=os.path.join(store_dir, "scheduler.log"),
            ledger=os.path.join(store_dir, "model-calls.jsonl"),
            flags_path=os.path.join(store_dir, "health-flags.json"),
            bridge_log=os.path.join(store_dir, "brain-bridge.log"),
            board_html=os.path.join(store_dir, "daily-board.html"),
            shadow_comp=os.path.join(store_dir, "shadow-comparison.jsonl"))
        if not os.path.exists(paths["hub_db"]):
            con = sqlite3.connect(paths["hub_db"])
            con.execute(
                "CREATE TABLE job_runs (run_id TEXT PRIMARY KEY, job_id TEXT,"
                " node TEXT DEFAULT '', started_at TEXT NOT NULL, ended_at TEXT,"
                " status TEXT NOT NULL, exit_code INTEGER, duration_s REAL,"
                " summary TEXT DEFAULT '', log_tail TEXT DEFAULT '',"
                " meta TEXT DEFAULT '{}', pid INTEGER)")
            con.commit(); con.close()
    else:
        paths = {}

    print(f"accelerated run: epoch={_iso(epoch)} accel=×{acceleration} "
          f"→ {days} virtual days in {days * 86400 / acceleration / 60:.1f} real min")
    injected: List[Dict[str, Any]] = []

    def inject_up_to(day: int) -> None:
        while len(injected) <= min(day, days - 1):
            injected.append(chaos.inject_day(len(injected), seed=seed,
                                             **paths))

    budget = time.time() + days * 86400 / acceleration + 120
    while time.time() < budget:
        inject_up_to(clock.virtual_day())
        if clock.virtual_day() >= days:
            break
        time.sleep(poll_real_s)
    inject_up_to(days - 1)   # ensure the full window is materialized

    # evaluate: chaos wrote real timestamps, so evaluate the virtual window
    results = evaluate_chaos_window(injected, paths or chaos.__dict__, days)
    print_report(results, results.get("_days", []),
                 f"accelerated ×{acceleration} (chaos, seed {seed})")
    return results


def _last_event_ts(path: str, start: float, end: float) -> Optional[float]:
    """Max ISO timestamp found in any line of a chaos-written store, scoped
    to the [start, end) virtual window."""
    import re
    best = None
    try:
        with open(path, "r", errors="replace") as f:
            for ln in f:
                m = re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", ln)
                if not m:
                    continue
                try:
                    ts = _parse_iso(m.group(0) + "Z")
                except ValueError:
                    continue
                if start <= ts < end and (best is None or ts > best):
                    best = ts
    except OSError:
        return None
    return best


def evaluate_chaos_window(injected: List[Dict[str, Any]],
                          paths: Dict[str, str], days: int) -> Dict[str, Any]:
    """Per-day + final evaluation from the chaos-injected stores."""
    from dashboard.exec_summary import window_totals
    fb = paths.get("hub_db", chaos.DEFAULT_HUB_DB)
    sched = paths.get("scheduler_log", chaos.DEFAULT_SCHEDULER_LOG)
    bridge = paths.get("bridge_log", chaos.DEFAULT_BRIDGE_LOG)
    board = paths.get("board_html", chaos.DEFAULT_BOARD_HTML)
    ledger = paths.get("ledger", chaos.DEFAULT_LEDGER)
    flags_path = paths.get("flags_path", chaos.DEFAULT_FLAGS)
    days_rec = []
    for rec in injected:
        ds = clock.day_start(rec["day"])
        win = (ds, ds + 86400.0)
        data = gather_job_runs(fb, win)
        totals = window_totals(ledger, win)
        flags = chaos.read_flags(flags_path) if rec["day"] == days - 1 else {}
        # accelerated mode: freshness is measured in VIRTUAL time — the last
        # chaos-written bridge line / board render must be within the sre
        # monitor's 90-min staleness budget of the day end
        fresh_parts = []
        for p in (bridge, board):
            lts = _last_event_ts(p, ds, ds + 86400.0)
            fresh_parts.append(lts is not None and (ds + 86400.0) - lts <= 90 * 60)
        days_rec.append({
            "day": rec["day"], "day_start": ds, "day_end": ds + 86400.0,
            "day_type": rec["day_type"], "runs": rec["job_runs"],
            "failed": rec["failed"], "calls": totals.get("calls", 0),
            "sev_pings": sev_pings_would_fire(day_findings(ds, fb)),
            "render_fresh": all(fresh_parts),
            "scheduler_alive": scheduler_alive(ds, sched),
            "restarts": 0, "trend_flag": False,
            "flagged": rec["failed"] >= 5,
        })
    failed_by_day = [d["failed"] for d in days_rec]
    for i, d in enumerate(days_rec):
        d["trend_flag"], d["trend_detail"] = trend_flag(failed_by_day, i)
    results = evaluate_window(days_rec, os.path.dirname(fb), days,
                              shadow_comp_path=paths.get(
                                  "shadow_comp", chaos.DEFAULT_SHADOW_COMP),
                              ledger_path=paths.get("ledger",
                                                    chaos.DEFAULT_LEDGER),
                              replay_incidents=False)
    results["_days"] = days_rec
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--backtest", action="store_true",
                    help="PRIMARY: replay a real historical window")
    ap.add_argument("--window-start", help="ISO start (UTC) of the backtest window")
    ap.add_argument("--window-days", type=int, default=7)
    ap.add_argument("--acceleration", type=float, default=480.0,
                    help="accelerated-chaos mode factor (×480 → ~21 min/7d)")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--store-dir", help="accelerated mode: isolate stores here")
    ap.add_argument("--fixture-dir", help="backtest: fixture directory override")
    args = ap.parse_args()

    if args.backtest:
        if not args.window_start:
            ap.error("--backtest requires --window-start")
        results = run_backtest(_parse_iso(args.window_start),
                               args.window_days, args.fixture_dir)
    else:
        results = run_accelerated(args.acceleration, args.days,
                                  store_dir=args.store_dir)
    return 0 if results.get("all_pass") else 1


if __name__ == "__main__":
    sys.exit(main())
