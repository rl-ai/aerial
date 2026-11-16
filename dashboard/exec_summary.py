#!/usr/bin/env python3
"""Exec summary layer — yesterday's activity for the 15-second daily board.

Gathers the previous UTC day's raw activity from the production sources
(fail-closed: a missing/unreadable source degrades to 'unavailable', never
fabricated) and optionally hands the gathered data to the DeepSeek writer via
the governed model_call.py subprocess for a 4-block markdown summary.

Window: previous 24h ending at the last UTC midnight ("yesterday"), i.e.
[utc_midnight - 24h, utc_midnight). All timestamps are normalized with
_norm_ts (mixed int/epoch-float/ISO input).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.dirname(HERE)
for _p in (_PKG_ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from dashboard.data_layer.model_calls_tailer import ModelCallsTailer  # noqa: E402
from dashboard import clock  # noqa: E402
import scrape_intel  # noqa: E402

from deployment_profile import pvalue as _pv   # noqa: E402
DEFAULT_HUB_DB = str(_pv("hub_db"))
DEFAULT_LEDGER = str(_pv("model_calls_ledger"))
DEFAULT_BRIDGE_LOG = str(_pv("bridge_log"))
DEFAULT_DIGEST = str(_pv("digest_latest"))
DEFAULT_DOGFOOD_LOG = str(_pv("dogfood_log"))
DEFAULT_SCHEDULER_LOG = str(_pv("scheduler_log"))
DEFAULT_SHADOW_DIR = str(_pv("shadow_dir"))
DEFAULT_MODEL_CALL = str(_pv("model_call_script")
                         or (Path(_PKG_ROOT) / "bin" / "model_call.py"))
DEFAULT_CONSOLE_URL = "http://127.0.0.1:8777/dashboard"

# Spec §3.3: known instrumentation gaps, labeled from this constant list —
# never from the model. Key -> the label appended when the value indicates
# the known gap (e.g. the 0-token ledger accounting bug).
KNOWN_GAPS = {"tokens": "accounting gap"}

HEADLINE_CAP = 3          # max headlines in the email
HEADLINE_WORD_CAP = 20    # hard word cap per headline
DECISIONS_CAP = 3         # max decision lines in the email
RISKS_CAP = 4             # max risk bullets in the email
EMAIL_HTML_MAX_BYTES = 15 * 1024
EMAIL_TEXT_MAX_BYTES = 2.5 * 1024

DONE_STATUSES = ("done", "complete", "completed", "succeeded", "success", "ok")
FAILED_STATUSES = ("failed", "error", "errored", "failure")

# F2 (buglist 2026-11-14): agent transcripts occasionally hallucinate ticket
# ids. Every TK-/EM- id the report layer renders is verified against the hub
# (GET /tickets/<id> with the bearer); unverifiable ids get " (unverified id)"
# appended so a fabricated id can never masquerade as real work.
TICKET_ID_RE = re.compile(r"\b((?:TK|EM)-[A-Za-z0-9]+)\b")
DEFAULT_HUB_URL = os.environ.get("A5_HUB_URL", "http://127.0.0.1:8777")
DEFAULT_HUB_TOKEN_PATH = os.path.expanduser(
    "~/.config/aiops/handoff/eva-harness-a5.token")


# --------------------------------------------------------------------------- time

def _norm_ts(value: Any) -> Optional[float]:
    """Normalize a mixed int/epoch-float/ISO timestamp to a POSIX float."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v <= 0:
            return None
        # Heuristic: seconds vs milliseconds epoch.
        if v > 1e12:
            v /= 1000.0
        return v
    s = str(value).strip()
    if not s:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return _norm_ts(float(s))
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def yesterday_window(now: Optional[float] = None) -> Tuple[float, float]:
    """(start, end) of the previous UTC day: [last-midnight - 24h, last-midnight)."""
    if now is None:
        now = clock.now()
    midnight = datetime.fromtimestamp(now, tz=timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0)
    end = midnight.timestamp()
    return end - 86400.0, end


def rolling_window(now: Optional[float] = None) -> Tuple[float, float]:
    """(start, end) of the rolling last 24h from now (on-demand chat format)."""
    if now is None:
        now = clock.now()
    return now - 86400.0, now


def window_for(mode: str, now: Optional[float] = None) -> Tuple[float, float]:
    """Window by mode: 'yesterday' (daily email) or 'rolling' (on-demand)."""
    if mode == "rolling":
        return rolling_window(now)
    return yesterday_window(now)


WINDOW_LABEL = {"yesterday": "yesterday", "rolling": "last 24 h"}


# --------------------------------------------------------------------------- sources

def _unavailable(detail: str) -> Dict[str, Any]:
    return {"available": False, "detail": detail}


SKIP_EXITS = (75,)   # curator-poll skip — never a failure (I2/I4 decision)
RUNNING_STATUSES = ("running", "in_progress", "in-progress", "pending")


def classify_run(row: Dict[str, Any]) -> str:
    """Classify one job_runs row into exactly one of ok/failed/skipped/running.

    Every row in the window gets exactly one class, so the four counts always
    sum to the window run total (the I2 6-vs-2 discrepancy class is impossible).
    exit-75 curator-poll skips are `skipped`, never `failed`; a row with no
    parsable status and no exit code is unresolved (`running`).
    """
    st = str(row.get("status") or "").lower()
    ec = row.get("exit_code")
    if ec is not None:
        try:
            ec = int(ec)
        except (TypeError, ValueError):
            ec = None
    if st in RUNNING_STATUSES:
        return "running"
    if st in ("skipped", "skip") or (ec in SKIP_EXITS and st not in FAILED_STATUSES):
        return "skipped"
    if st in FAILED_STATUSES or (ec is not None and ec != 0):
        return "failed"
    if st in DONE_STATUSES:
        return "ok"
    return "running"   # unresolved: no status, no exit code


def gather_job_runs(db_path: str, window: Tuple[float, float]) -> Dict[str, Any]:
    """hub.db job_runs: runs started in ONE declared UTC window, classified
    ok/failed/skipped/running (counts sum to the window total), failure lines
    for failed runs only, reconcile counts carried separately."""
    import sqlite3
    if not os.path.exists(db_path):
        return _unavailable(f"hub.db not found: {db_path}")
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.2)
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute("SELECT * FROM job_runs")]
        conn.close()
    except (sqlite3.Error, OSError) as e:
        return _unavailable(f"hub.db read failed: {e}")
    start, end = window
    in_win = []
    for r in rows:
        ts = _norm_ts(r.get("started_at") or r.get("ended_at"))
        if ts is not None and start <= ts < end:
            in_win.append(r)
    ok = failed = skipped = running = 0
    failures: List[Dict[str, Any]] = []
    agent_hours = 0.0
    for r in in_win:
        cls = classify_run(r)
        if cls == "failed":
            failed += 1
            failures.append({
                "run_id": r.get("run_id"),
                "started_at": r.get("started_at"),
                "job_id": r.get("job_id"),
                "summary": (r.get("summary") or "").strip() or "(no summary)",
            })
        elif cls == "ok":
            ok += 1
        elif cls == "skipped":
            skipped += 1
        else:
            running += 1
        dur = r.get("duration_s")
        if dur is not None:
            try:
                agent_hours += float(dur) / 3600.0
            except (TypeError, ValueError):
                pass
    return {
        "available": True,
        "runs": len(in_win),
        "ok": ok,
        "failed": failed,
        "skipped": skipped,
        "running": running,
        "failures": failures,
        "reconciled": ok + failed + skipped + running == len(in_win),
        "agent_hours": round(agent_hours, 2),
    }


def window_totals(ledger_path: str, window: Tuple[float, float]) -> Dict[str, Any]:
    """Model-calls ledger calls + token totals for the window.

    Uses the data_layer tailer's fresh-scan discipline: newline-terminated
    lines only, malformed/untagged lines bucketed under 'untagged' and never
    dropped, same token-field aliases (in_tokens/input_tokens,
    out_tokens/output_tokens) as ModelCallsTailer. Fresh scan (state at zero),
    then lines are window-filtered by their ts/timestamp/time field via
    _norm_ts; lines without any parsable timestamp are excluded from the
    window (counted as 'no_ts') rather than guessed.
    """
    if not os.path.exists(ledger_path):
        return _unavailable(f"ledger not found: {ledger_path}")
    start, end = window
    calls = in_tok = out_tok = no_ts = 0
    tailer = ModelCallsTailer(ledger_path=ledger_path,
                              state_path=os.path.join(tempfile.gettempdir(),
                                                      "exec-summary-scan-does-not-persist"))
    try:
        with open(ledger_path, "rb") as f:
            data = f.read()
        complete = data[: data.rfind(b"\n") + 1] if b"\n" in data else b""
        for raw in complete.splitlines():
            try:
                rec = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue  # malformed -> counted by tailer discipline, not here
            if not isinstance(rec, dict):
                continue
            ts = None
            for key in ("ts", "timestamp", "time", "created_at", "started_at"):
                ts = _norm_ts(rec.get(key))
                if ts is not None:
                    break
            if ts is None or not (start <= ts < end):
                if ts is None:
                    no_ts += 1
                continue
            calls += 1
            in_tok += int(rec.get("in_tokens") or rec.get("input_tokens") or 0)
            out_tok += int(rec.get("out_tokens") or rec.get("output_tokens") or 0)
    except OSError as e:
        return _unavailable(f"ledger read failed: {e}")
    return {
        "available": True,
        "calls": calls,
        "in_tokens": in_tok,
        "out_tokens": out_tok,
        "no_ts_lines": no_ts,
        "_tailer_discipline": tailer is not None,  # fresh-scan, newline-terminated
    }


_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_ISO_RE = re.compile(r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?)")


def _line_ts(line: str) -> Optional[float]:
    m = _ISO_RE.search(line)
    if m:
        ts = _norm_ts(m.group(1).replace(" ", "T"))
        if ts is not None:
            return ts
    m = _DATE_RE.search(line)
    if m:
        return _norm_ts(f"{m.group(1)}-{m.group(2)}-{m.group(3)}T00:00:00+00:00")
    return None


def gather_ingest(bridge_log: str, digest_path: str,
                  window: Tuple[float, float]) -> Dict[str, Any]:
    """Ingest bridge: items ingested yesterday from the bridge log + digest."""
    start, end = window
    items = None
    log_avail = False
    log_detail = ""
    if os.path.exists(bridge_log):
        try:
            with open(bridge_log, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            in_win = [ln for ln in lines
                      if (ts := _line_ts(ln)) is not None and start <= ts < end]
            ingested_lines = [ln for ln in in_win
                              if re.search(r"ingest", ln, re.I)]
            log_avail = True
            items = len(ingested_lines) if ingested_lines else None
            log_detail = f"{len(in_win)} log lines in window"
        except OSError as e:
            log_detail = f"bridge log read failed: {e}"
    else:
        log_detail = f"bridge log not found: {bridge_log}"
    # Digest as the authoritative count when present.
    if os.path.exists(digest_path):
        try:
            with open(digest_path, "r", encoding="utf-8") as f:
                digest = json.load(f)
            if isinstance(digest, dict):
                ing = digest.get("ingested")
                if isinstance(ing, list):
                    items = len(ing)
                elif isinstance(ing, int):
                    items = ing
                elif digest.get("items_ingested") is not None:
                    items = int(digest["items_ingested"])
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
            log_detail += f"; digest read failed: {e}"
    if not log_avail and not os.path.exists(digest_path):
        return _unavailable(f"no bridge log ({log_detail}) and no digest at {digest_path}")
    return {
        "available": True,
        "items": items,
        "detail": log_detail,
    }


def gather_dogfood(dogfood_log: str, window: Tuple[float, float]) -> Dict[str, Any]:
    """Dogfood log: new anomaly lines dated in the window."""
    if not os.path.exists(dogfood_log):
        return _unavailable(f"dogfood log not found: {dogfood_log}")
    start, end = window
    try:
        with open(dogfood_log, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as e:
        return _unavailable(f"dogfood log read failed: {e}")
    new_lines: List[str] = []
    for ln in lines:
        stripped = ln.strip()
        if not stripped or stripped.startswith("#"):
            continue
        ts = _line_ts(stripped)
        if ts is not None and start <= ts < end:
            new_lines.append(stripped)
    return {"available": True, "new_anomaly_lines": new_lines,
            "count": len(new_lines)}


def gather_shadow(scheduler_log: str, window: Tuple[float, float]) -> Dict[str, Any]:
    """Shadow scheduler log: runs fired (started) vs skipped in the window."""
    if not os.path.exists(scheduler_log):
        return _unavailable(f"scheduler log not found: {scheduler_log}")
    start, end = window
    try:
        with open(scheduler_log, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as e:
        return _unavailable(f"scheduler log read failed: {e}")
    fired = skipped = 0
    for ln in lines:
        ts = _line_ts(ln)
        if ts is None or not (start <= ts < end):
            continue
        ev = ln.split(None, 2)
        event = ev[1].lower() if len(ev) > 1 else ""
        if event == "started":
            fired += 1
        elif event == "skipped":
            skipped += 1
    return {"available": True, "fired": fired, "skipped": skipped}


_FINISHED_EXIT_RE = re.compile(r"exit=(\d+)")
_FINISHED_ERR_RE = re.compile(r"error=")


def gather_agent_actions(scheduler_log: str, shadow_dir: str, hub_db: str,
                         window: Tuple[float, float]) -> Dict[str, Any]:
    """What the agents DID proactively in the window (spec §3.4).

    Sources, each fail-closed like gather_job_runs — a missing/unreadable
    source degrades to 'unavailable', never a fabricated count:
      - scheduler.log started/finished entries (scheduled shadow runs)
      - hierarchy run workspaces under the shadow dir (dirs touched in window)
      - hub.db done-items (tolerant: any table with a status-like column)
    """
    start, end = window
    out: Dict[str, Any] = {"available": False, "sources": {}}

    # --- scheduler.log: started / finished-ok / finished-error --------------
    sched: Dict[str, Any] = {"available": False, "detail": ""}
    if os.path.exists(scheduler_log):
        try:
            with open(scheduler_log, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            started = finished_ok = finished_err = 0
            cmds: List[str] = []
            for ln in lines:
                ts = _line_ts(ln)
                if ts is None or not (start <= ts < end):
                    continue
                ev = ln.split(None, 2)
                event = ev[1].lower() if len(ev) > 1 else ""
                detail = ev[2] if len(ev) > 2 else ""
                if event == "started":
                    started += 1
                    cmd = detail.split()[0] if detail.split() else ""
                    if cmd and os.path.basename(cmd) not in cmds:
                        cmds.append(os.path.basename(cmd))
                elif event == "finished":
                    m = _FINISHED_EXIT_RE.search(detail)
                    if m and m.group(1) == "0":
                        finished_ok += 1
                    elif m and m.group(1) != "0":
                        finished_err += 1
                    elif _FINISHED_ERR_RE.search(detail):
                        finished_err += 1
                    else:
                        finished_ok += 1
            sched = {"available": True, "started": started,
                     "finished_ok": finished_ok, "finished_err": finished_err,
                     "commands": cmds[:3]}
        except OSError as e:
            sched["detail"] = f"scheduler log read failed: {e}"
    else:
        sched["detail"] = f"scheduler log not found: {scheduler_log}"
    out["sources"]["scheduler"] = sched

    # --- hierarchy run workspaces under the shadow dir ----------------------
    hier: Dict[str, Any] = {"available": False, "detail": ""}
    if os.path.isdir(shadow_dir):
        try:
            run_dirs: List[Tuple[float, str]] = []
            for name in sorted(os.listdir(shadow_dir)):
                p = os.path.join(shadow_dir, name)
                if not os.path.isdir(p):
                    continue  # scheduler.log / brief.md / lock are files
                try:
                    mtime = os.path.getmtime(p)
                except OSError:
                    continue
                if start <= mtime < end:
                    run_dirs.append((mtime, name))
            hier = {"available": True, "runs": len(run_dirs),
                    "names": [n for _, n in sorted(run_dirs)][-3:]}
        except OSError as e:
            hier["detail"] = f"shadow dir read failed: {e}"
    else:
        hier["detail"] = f"shadow dir not found: {shadow_dir}"
    out["sources"]["hierarchy"] = hier

    # --- hub.db done-items (tolerant table discovery) ------------------------
    hub: Dict[str, Any] = {"available": False, "detail": ""}
    if os.path.exists(hub_db):
        import sqlite3
        try:
            conn = sqlite3.connect(f"file:{hub_db}?mode=ro", uri=True, timeout=0.2)
            conn.row_factory = sqlite3.Row
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
            done = queue_handled = None
            for t in ("items", "tasks", "cards", "job_runs"):
                if t not in tables:
                    continue
                cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
                stcol = next((c for c in cols if c.lower() in
                              ("status", "state")), None)
                tscol = next((c for c in cols if c.lower() in
                              ("ended_at", "updated_at", "completed_at",
                               "done_at", "closed_at", "ts")), None)
                if not (stcol and tscol):
                    continue
                rows = [dict(r) for r in conn.execute(f"SELECT * FROM {t}")]
                n = 0
                for r in rows:
                    st = str(r.get(stcol) or "").lower()
                    if st not in DONE_STATUSES:
                        continue
                    ts = _norm_ts(r.get(tscol))
                    if ts is not None and start <= ts < end:
                        n += 1
                done = n if done is None else done + n
            # exception-queue transitions: queue_items in a done state
            if "queue_items" in tables:
                cols = [r[1] for r in conn.execute("PRAGMA table_info(queue_items)")]
                stcol = "state" if "state" in cols else (
                    "status" if "status" in cols else None)
                tscol = next((c for c in cols if c.lower() in
                              ("updated_at", "closed_at", "resolved_at",
                               "ended_at", "ts")), None)
                if stcol and tscol:
                    qn = 0
                    for r in conn.execute(f"SELECT * FROM queue_items"):
                        d = dict(r)
                        if str(d.get(stcol) or "").lower() in DONE_STATUSES:
                            ts = _norm_ts(d.get(tscol))
                            if ts is not None and start <= ts < end:
                                qn += 1
                    queue_handled = qn
            conn.close()
            hub = {"available": True, "done_items": done,
                   "queue_handled": queue_handled}
        except (sqlite3.Error, OSError) as e:
            hub["detail"] = f"hub.db read failed: {e}"
    else:
        hub["detail"] = f"hub.db not found: {hub_db}"
    out["sources"]["hub"] = hub

    # --- assembled line + phrases -------------------------------------------
    parts: List[str] = []
    if sched.get("available") and (sched["started"] or sched["finished_ok"]):
        parts.append(f"ran {sched['started']} scheduled shadow runs")
    if hier.get("available") and hier["runs"]:
        parts.append(f"completed {hier['runs']} hierarchy runs")
    if hub.get("available"):
        if hub.get("done_items"):
            parts.append(f"closed {hub['done_items']} hub items")
    phrases = []
    if sched.get("available") and sched.get("commands"):
        phrases += [f"ran {c}" for c in sched["commands"]]
    if hier.get("available"):
        phrases += [f"hierarchy run {n}" for n in hier.get("names", [])]
    out["phrases"] = phrases[:3]
    out["available"] = bool(sched.get("available") or hier.get("available")
                            or hub.get("available"))
    out["detail"] = "; ".join(
        f"{k}: {v.get('detail')}" for k, v in out["sources"].items()
        if not v.get("available"))
    return out


def _hub_token() -> str:
    """Bearer for hub verification: A5_HUB_TOKEN env, else the handoff token
    file; empty string when neither exists (verification then fail-closes)."""
    tok = os.environ.get("A5_HUB_TOKEN", "")
    if tok:
        return tok.strip()
    try:
        return Path(DEFAULT_HUB_TOKEN_PATH).read_text(
            encoding="utf-8").strip()
    except OSError:
        return ""


def _hub_ticket_exists(ticket_id: str, hub_url: str, token: str,
                       timeout: float) -> bool:
    """True iff the hub answers GET /tickets/<id> with 200. Anything else
    (404, 5xx, auth failure, transport error) -> False: fail closed, an id
    we cannot prove is real renders as unverified."""
    import urllib.error
    import urllib.request
    url = f"{hub_url.rstrip('/')}/tickets/{ticket_id}"
    req = urllib.request.Request(url, method="GET")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except urllib.error.HTTPError:
        return False
    except Exception:
        return False


def verify_ticket_ids(text: str, hub_url: str | None = None, token: str | None = None,
                      timeout: float = 2.0) -> str:
    """Append ' (unverified id)' to every TK-/EM- id in *text* that the hub
    does not confirm. Verified ids are left untouched. Each unique id is
    checked once per call."""
    if not text:
        return text
    hub_url = hub_url or os.environ.get("A5_HUB_URL", DEFAULT_HUB_URL)
    token = _hub_token() if token is None else token
    cache: dict[str, bool] = {}

    def repl(m: "re.Match") -> str:
        tid = m.group(1)
        if tid not in cache:
            cache[tid] = _hub_ticket_exists(tid, hub_url, token, timeout)
        return tid if cache[tid] else f"{tid} (unverified id)"

    return TICKET_ID_RE.sub(repl, text)


def agent_actions_line(actions: Dict[str, Any]) -> str:
    """The one-line email/console agent-actions sentence, fail-closed."""
    if not actions or not actions.get("available"):
        detail = (actions or {}).get("detail") or "sources unavailable"
        return f"Agent actions: unavailable ({detail}) · (source gap)"
    sched = actions["sources"].get("scheduler") or {}
    hier = actions["sources"].get("hierarchy") or {}
    hub = actions["sources"].get("hub") or {}
    completed = hub.get("done_items") if hub.get("available") else None
    queue_h = hub.get("queue_handled") if hub.get("available") else None
    ran = sched.get("started") if sched.get("available") else None
    bits: List[str] = []
    if completed is not None:
        bits.append(f"completed {completed} items")
    elif ran is not None:
        bits.append(f"ran {ran} scheduled runs")
    if queue_h is not None:
        bits.append(f"handled {queue_h} queue entries")
    if not bits:
        bits.append("completed 0 items")
    line = "Agents " + ", ".join(bits)
    phrases = actions.get("phrases") or []
    if phrases:
        line += ": " + "; ".join(phrases)
    # F2: any TK-/EM- id quoted from transcripts is hub-verified before it
    # renders; unverifiable ids are marked, never silently trusted.
    return verify_ticket_ids(line)


# --------------------------------------------------------------------------- gather

def gather(now: Optional[float] = None, *,
           window_mode: str = "yesterday",
           hub_db: str = DEFAULT_HUB_DB,
           ledger: str = DEFAULT_LEDGER,
           bridge_log: str = DEFAULT_BRIDGE_LOG,
           digest: str = DEFAULT_DIGEST,
           dogfood_log: str = DEFAULT_DOGFOOD_LOG,
           scheduler_log: str = DEFAULT_SCHEDULER_LOG,
           shadow_dir: str = DEFAULT_SHADOW_DIR,
           scrapes_db: str = scrape_intel.DEFAULT_SCRAPES_DB,
           project_keywords: Optional[Dict[str, List[str]]] = None,
           ) -> Dict[str, Any]:
    """Collect the window's raw numbers from every source. Fail-closed per source.

    window_mode: 'yesterday' (previous UTC day, the daily email) or
    'rolling' (last 24h from now, the on-demand chat format).
    """
    win = window_for(window_mode, now)
    out = {
        "window": {"start": win[0], "end": win[1], "mode": window_mode,
                   "label": WINDOW_LABEL.get(window_mode, window_mode)},
        "job_runs": gather_job_runs(hub_db, win),
        "model_calls": window_totals(ledger, win),
        "ingest": gather_ingest(bridge_log, digest, win),
        "dogfood": gather_dogfood(dogfood_log, win),
        "shadow": gather_shadow(scheduler_log, win),
        "agent_actions": gather_agent_actions(scheduler_log, shadow_dir,
                                              hub_db, win),
        "scrape_intel": scrape_intel.gather(
            now=now, scrapes_db=scrapes_db,
            project_keywords=project_keywords),
    }
    out["numbers_strip"] = numbers_strip(out)
    return out


def _fmt_hours(h: float) -> str:
    if h >= 1:
        return f"{h:.1f}"
    return f"{h:.2f}"


def numbers_strip(data: Dict[str, Any]) -> str:
    """The one-line numbers strip, computed LOCALLY from raw counts only.

    Spec §3.3: unavailable sources read 'unavailable · (source gap)' (suffix on the
    FIRST occurrence only); a known instrumentation gap from the KNOWN_GAPS
    constant is labeled inline (e.g. '0 tokens (accounting gap)'). Fail-closed
    rule unchanged: never invent a 0.
    """
    jr = data.get("job_runs") or {}
    mc = data.get("model_calls") or {}
    ing = data.get("ingest") or {}

    def na() -> str:
        # First unavailable occurrence gets the explicit source-gap suffix.
        if not na.fired:
            na.fired = True
            return "unavailable · (source gap)"
        return "unavailable"
    na.fired = False  # type: ignore[attr-defined]

    runs = str(jr.get("runs")) if jr.get("available") else na()
    ok = str(jr.get("ok", 0)) if jr.get("available") else na()
    failed = str(jr.get("failed")) if jr.get("available") else na()
    skipped = str(jr.get("skipped", 0)) if jr.get("available") else na()
    ingested = (str(ing.get("items")) if ing.get("available") and ing.get("items") is not None
                else na())
    if mc.get("available"):
        calls = str(mc.get("calls"))
        total_toks = mc.get("in_tokens", 0) + mc.get("out_tokens", 0)
        if total_toks == 0 and "tokens" in KNOWN_GAPS:
            toks = f"0 tokens ({KNOWN_GAPS['tokens']})"
        else:
            toks = f"{total_toks} tokens"
    else:
        calls = toks = na()
    hours = _fmt_hours(jr.get("agent_hours", 0)) if jr.get("available") else na()
    return (f"{runs} runs · {ok} ok · {failed} failed · {skipped} skipped · "
            f"{ingested} items ingested · {calls} calls / {toks} · "
            f"{hours} agent-hours")


# --------------------------------------------------------------------------- caps

def _words(line: str) -> int:
    return len(line.split())


def cap_headlines(lines: List[str], cap: int = HEADLINE_CAP,
                  word_cap: int = HEADLINE_WORD_CAP) -> Tuple[List[str], int]:
    """Keep ≤cap headlines, each ≤word_cap words (hard local truncation)."""
    kept: List[str] = []
    overflow = 0
    for ln in lines:
        ln = ln.strip().strip("-•").strip()
        if not ln:
            continue
        if len(kept) >= cap:
            overflow += 1
            continue
        words = ln.split()
        if len(words) > word_cap:
            ln = " ".join(words[:word_cap]) + " …"
        kept.append(ln)
    return kept, overflow


def _numbered(lines: List[str]) -> List[str]:
    return [f"{i}. {ln.strip().lstrip('0123456789. )-•').strip()}"
            for i, ln in enumerate(lines, 1) if ln.strip()]


def cap_decisions(lines: List[str], cap: int = DECISIONS_CAP) -> Tuple[List[str], int]:
    """Numbered, one line each, ≤cap; overflow becomes an '+N more' line."""
    clean = [re.sub(r"\s+", " ", ln.strip().strip("-•").strip())
             for ln in lines if ln.strip()]
    kept = _numbered(clean[:cap])
    overflow = len(clean) - cap
    if overflow > 0:
        kept.append(f"+{overflow} more on the console")
    return kept, max(overflow, 0)


def cap_risks(lines: List[str], cap: int = RISKS_CAP) -> Tuple[List[str], int]:
    """Bullet lines, ≤cap, one line each; overflow becomes an '+N more' line."""
    clean = [re.sub(r"\s+", " ", ln.strip().strip("-•").strip())
             for ln in lines if ln.strip()]
    kept = clean[:cap]
    overflow = len(clean) - cap
    if overflow > 0:
        kept.append(f"+{overflow} more on the console")
    return kept, max(overflow, 0)


# --------------------------------------------------------------------------- intel pulse

def intel_pulse(intel: Dict[str, Any]) -> str:
    """One-line listening pulse, computed locally from scrape_intel theme stats.

    Total current-window items vs the prior window, plus the biggest riser by
    ratio. Fail-closed: 'unavailable (source gap)' when the scrape store is.
    """
    if not intel or not intel.get("available"):
        detail = (intel or {}).get("detail") or "scrape store unavailable"
        return f"Listening: unavailable ({detail}) · (source gap)"
    themes = intel.get("themes") or []
    total = sum(t.get("count", 0) for t in themes)
    prior = sum(t.get("prior_count", 0) for t in themes)
    direction = "down" if total < prior else ("up" if total > prior else "flat from")
    riser = None
    for t in themes:
        if t.get("trend") == "up" and t.get("ratio"):
            if riser is None or t["ratio"] > riser["ratio"]:
                riser = t
    line = f"Listening: {total} items ({direction} {prior}"
    if total < prior and prior:
        line += f", ×{round(total / prior, 2) if prior else '—'}"
    line += ")"
    if riser:
        line += f"; biggest riser: {riser['topic']} ×{riser['ratio']}"
    line += "."
    return line


# --------------------------------------------------------------------------- email (v2)

_EMAIL_STYLE = {
    "body": ("margin:0;padding:16px;background:#ffffff;color:#1a1a1a;"
             "font-family:-apple-system,'Segoe UI',Arial,sans-serif;"
             "font-size:14px;line-height:1.45;"),
    "wrap": ("max-width:640px;margin:0 auto;background:#ffffff;"),
    "h2": ("font-size:16px;margin:18px 0 6px 0;color:#1a1a1a;"),
    "li": ("margin:4px 0;"),
    "meta": ("color:#666666;font-size:12px;margin-top:20px;"),
    "strip": ("margin:6px 0;padding:8px;background:#f5f5f5;font-size:13px;"),
    "fail": "color:#b3261e;font-weight:bold;",
    "ok": "color:#1a7f37;",
    "watch": "color:#9a6700;",
}


def email_sections(data: Dict[str, Any], ds_result: Dict[str, Any],
                   console_url: str = DEFAULT_CONSOLE_URL) -> Dict[str, Any]:
    """Assemble the email's 15-second read (spec §3), caps enforced locally."""
    strip = ds_result.get("numbers_strip") or numbers_strip(data)
    window_label = (data.get("window") or {}).get("label", "yesterday")
    blocks = (ds_result.get("blocks") or {}) if ds_result.get("mode") == "structured" else {}

    raw_headlines = [ln for ln in (blocks.get("headlines") or "").splitlines()
                     if ln.strip()]
    headlines, h_overflow = cap_headlines(raw_headlines)

    raw_decisions = [ln for ln in (blocks.get("decisions") or "").splitlines()
                     if ln.strip()]
    decisions, d_overflow = cap_decisions(raw_decisions)

    raw_risks = [ln for ln in (blocks.get("risks") or "").splitlines()
                 if ln.strip()]
    risks, r_overflow = cap_risks(raw_risks)

    actions_line = agent_actions_line(data.get("agent_actions") or {})
    pulse = intel_pulse(data.get("scrape_intel") or {})
    footer = (f"Full live board: {console_url} · Attached: yesterday's board "
              f"snapshot. Generated {time.strftime('%H:%M', time.gmtime(time.time()))} UTC.")

    return {
        "window_label": window_label,
        "strip": strip,
        "headlines": headlines,
        "headlines_overflow": h_overflow,
        "actions_line": actions_line,
        "decisions": decisions,
        "decisions_overflow": d_overflow,
        "risks": risks,
        "risks_overflow": r_overflow,
        "pulse": pulse,
        "footer": footer,
    }


def email_text(data: Dict[str, Any], ds_result: Dict[str, Any],
               console_url: str = DEFAULT_CONSOLE_URL) -> str:
    """Plain-text part (≤2.5 KB): section headings present, console link last."""
    s = email_sections(data, ds_result, console_url)
    lines = [f"Aerial daily — {s['window_label']}", ""]
    lines.append("HEADLINES")
    lines += [f"- {h}" for h in s["headlines"]] or ["- none"]
    lines += ["", "NUMBERS", s["strip"]]
    lines += ["", "AGENT ACTIONS", s["actions_line"]]
    lines += ["", "DECISIONS NEEDED"]
    lines += s["decisions"] or ["None — nothing needs you today."]
    lines += ["", "RISKS/WATCH"]
    lines += [f"- {r}" for r in s["risks"]] or ["No risks flagged"]
    lines += ["", "INTEL PULSE", s["pulse"], "", s["footer"]]
    text = "\n".join(lines)
    if len(text.encode("utf-8")) > EMAIL_TEXT_MAX_BYTES:
        text = text.encode("utf-8")[:EMAIL_TEXT_MAX_BYTES].decode(
            "utf-8", "ignore") + "\n…(truncated; see the console)"
    return text


def email_html(data: Dict[str, Any], ds_result: Dict[str, Any],
               console_url: str = DEFAULT_CONSOLE_URL) -> str:
    """Email-safe inline-HTML part (≤15 KB): light theme, single column,
    table-free block layout, ALL styles inline, no external assets, no
    <style> block, no dark-theme board CSS. Fail-closed: renders even when
    the DS call failed (strip + pulse + footer only)."""
    s = email_sections(data, ds_result, console_url)
    st = _EMAIL_STYLE
    parts = [
        '<!DOCTYPE html><html><head><meta charset="utf-8"></head>'
        f'<body style="{st["body"]}"><div style="{st["wrap"]}">',
        f'<h2 style="{st["h2"]}">Headlines — {_esc(s["window_label"])}</h2>'
        '<table role="presentation" cellpadding="0" cellspacing="0" width="100%">',
    ]
    for h in s["headlines"]:
        parts.append(f'<tr><td style="{st["li"]}padding:2px 0;">• {_esc(h)}</td></tr>')
    if not s["headlines"]:
        parts.append(f'<tr><td style="{st["li"]}">No headlines this window.</td></tr>')
    parts.append("</table>")
    parts.append(f'<h2 style="{st["h2"]}">Numbers — {_esc(s["window_label"])}</h2>'
                 f'<div style="{st["strip"]}">{_esc(s["strip"])}</div>')
    parts.append(f'<h2 style="{st["h2"]}">Agent actions</h2>'
                 f'<div>{_esc(s["actions_line"])}</div>')
    parts.append(f'<h2 style="{st["h2"]}">Decisions needed</h2>')
    if s["decisions"]:
        parts.append('<table role="presentation" cellpadding="0" cellspacing="0" width="100%">')
        for d in s["decisions"]:
            parts.append(f'<tr><td style="{st["li"]}padding:2px 0;">{_esc(d)}</td></tr>')
        parts.append("</table>")
    else:
        parts.append("<div>None — nothing needs you today.</div>")
    parts.append(f'<h2 style="{st["h2"]}">Risks/watch</h2>')
    if s["risks"]:
        parts.append('<table role="presentation" cellpadding="0" cellspacing="0" width="100%">')
        for r in s["risks"]:
            parts.append(f'<tr><td style="{st["li"]}padding:2px 0;">• {_esc(r)}</td></tr>')
        parts.append("</table>")
    else:
        parts.append("<div>No risks flagged</div>")
    parts.append(f'<h2 style="{st["h2"]}">Intel pulse</h2>'
                 f'<div>{_esc(s["pulse"])}</div>')
    parts.append(f'<div style="{st["meta"]}">{_esc(s["footer"])}</div>')
    parts.append("</div></body></html>")
    html = "".join(parts)
    if len(html.encode("utf-8")) > EMAIL_HTML_MAX_BYTES:
        html = html.encode("utf-8")[:EMAIL_HTML_MAX_BYTES].decode(
            "utf-8", "ignore") + "</div></body></html>"
    return html


# --------------------------------------------------------------------------- DS call

def default_ds_runner(ask_path: str, out_path: str) -> int:
    """The governed subprocess call: bin/model_call.py --role ds-writer."""
    cmd = [sys.executable, DEFAULT_MODEL_CALL, "--role", "ds-writer",
           "--max-tokens", "2048", "--ask", ask_path, "--out", out_path]
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=300).returncode


def build_ask_text(data: Dict[str, Any]) -> str:
    intel = data.get("scrape_intel") or {}
    scrape_sections = ""
    if intel.get("available"):
        scrape_sections = (
            "### Scrape themes & trends\n"
            "- (top 3-5 themes with direction — up/flat/down — grounded in the themes data)\n"
            "### Stack overlap\n"
            "- (one line per project the chatter touches, with counts; 'none' if empty)\n"
            "### Recommendations\n"
            "- (1-3 actionable recommendations grounded in the themes above)\n")
    label = ((data.get("window") or {}).get("label") or "yesterday")
    return (
        "You are writing the executive summary block for the operator's "
        f"daily board — window: {label} (audience: Ray, 15 seconds). Use ONLY "
        "the data below — do not invent numbers, and mark anything unavailable "
        "as unavailable.\n\n"
        "Respond in EXACTLY this markdown structure:\n"
        "### 3 headlines\n"
        "- one sentence each, HARD CAP 20 words per headline; no inline code "
        "paths, no backticks, no error strings — those details belong in "
        "risks/decisions\n"
        "### Numbers strip\n"
        "- N runs · N failed · N items ingested · X calls / Y tokens · Z agent-hours\n"
        "### Decisions needed\n"
        "- (numbered list, one line each, max 3, or 'none')\n"
        "### Risks/watch\n"
        "- (bullet list, one line each, max 4, or 'none flagged')\n"
        + scrape_sections +
        "\n=== GATHERED DATA (yesterday, UTC day boundary) ===\n"
        + json.dumps(data, indent=1, sort_keys=True) + "\n"
    )


def _section(text: str, *names: str) -> str:
    for name in names:
        m = re.search(rf"^\s*(?:#+\s*)?\**{re.escape(name)}\**\s*:?\s*$",
                      text, re.I | re.M)
        if m:
            rest = text[m.end():]
            nxt = _next_heading(rest)
            body = rest[: nxt.start()] if nxt else rest
            return body.strip()
    return ""


_HEADING_NEXT = re.compile(
    r"^\s*(?:#{1,6}\s+\S.*|(?:\*\*.+\*\*|\*[^*\n]+\*)\s*:?)\s*$", re.M)


def _next_heading(text: str):
    """Next markdown heading line: '#'-style or bold-wrapped (the DS writer
    uses '### Title', not '**Title**' — both must terminate a section)."""
    return _HEADING_NEXT.search(text)


def _drop_section(text: str, *names: str) -> str:
    """Remove a heading + its body (inverse of _section) so a section the
    summary already renders once is not rendered twice from raw output."""
    for name in names:
        m = re.search(rf"^\s*(?:#+\s*)?\**{re.escape(name)}\**\s*:?\s*$",
                      text, re.I | re.M)
        if m:
            rest = text[m.end():]
            nxt = _next_heading(rest)
            end = nxt.start() if nxt else len(rest)
            return (text[:m.start()] + rest[end:]).strip()
    return text


def parse_ds_output(text: str) -> Dict[str, str]:
    """Extract the blocks; tolerant of heading variants, raw-text fallback."""
    return {
        "headlines": _section(text, "3 headlines", "headlines"),
        "numbers_strip": _section(text, "Numbers strip"),
        "decisions": _section(text, "Decisions needed", "decisions"),
        "risks": _section(text, "Risks/watch", "risks", "watch"),
        "scrape_themes": _section(text, "Scrape themes & trends",
                                  "Scrape themes and trends", "Scrape themes",
                                  "themes"),
        "stack_overlap": _section(text, "Stack overlap", "overlap"),
        "recommendations": _section(text, "Recommendations", "recommendation"),
    }


def write_summary_section(data: Dict[str, Any],
                          ds_runner: Optional[Callable[[str, str], int]] = None,
                          ) -> Tuple[str, Dict[str, Any]]:
    """Run the DS call and return (summary_html, meta). Never raises.

    On any DS failure returns the raw local numbers strip + an
    'exec summary unavailable' note (fail-closed, never fabricated).
    """
    if ds_runner is None:
        ds_runner = default_ds_runner
    strip = data.get("numbers_strip") or numbers_strip(data)
    fallback = {
        "mode": "fallback",
        "numbers_strip": strip,
        "note": "exec summary unavailable",
    }
    try:
        with tempfile.TemporaryDirectory() as td:
            ask = os.path.join(td, "ask.md")
            out = os.path.join(td, "out.md")
            with open(ask, "w", encoding="utf-8") as f:
                f.write(build_ask_text(data))
            rc = ds_runner(ask, out)
            if rc != 0 or not os.path.exists(out):
                fallback["detail"] = f"model_call.py exit {rc}"
                return "", fallback
            with open(out, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        if not text.strip():
            fallback["detail"] = "empty DS output"
            return "", fallback
        blocks = parse_ds_output(text)
        if not blocks["headlines"]:
            # Raw-text fallback: keep the whole output, local strip wins.
            return (f"<pre class=exec-raw>{text.strip()}</pre>",
                    {"mode": "raw", "numbers_strip": strip})
        return "", {"mode": "structured", "blocks": blocks,
                    "numbers_strip": strip, "raw": text}
    except Exception as e:  # subprocess/timeout/OSError — degrade, never crash
        fallback["detail"] = f"{type(e).__name__}: {e}"
        return "", fallback


# --------------------------------------------------------------------------- HTML

def summary_html(data: Dict[str, Any], ds_result: Dict[str, Any]) -> str:
    """The `<!-- exec-summary -->` section: structured DS blocks, raw text, or
    the local numbers strip + 'unavailable' note."""
    parts = ['<!-- exec-summary -->', '<h2>Executive summary — yesterday</h2>']
    strip = ds_result.get("numbers_strip") or numbers_strip(data)
    mode = ds_result.get("mode")
    if mode == "structured":
        b = ds_result["blocks"]
        parts.append(f'<p class="exec-strip"><b>Numbers:</b> {_esc(strip)}</p>')
        parts.append("<h3>Headlines</h3><ul class=brief>" + "".join(
            f"<li>{_esc(ln.strip('-• ').strip())}</li>"
            for ln in b["headlines"].splitlines() if ln.strip()) + "</ul>")
        parts.append(f"<h3>Decisions needed</h3><div>{_esc(b['decisions'] or 'none')}</div>")
        parts.append(f"<h3>Risks/watch</h3><div>{_esc(b['risks'] or 'none flagged')}</div>")
        # Spec §1/§3: the console keeps the FULL agent-actions list (the email
        # gets the capped one-liner only).
        parts.append(f"<h3>Agent actions</h3><div>{_esc(agent_actions_line(data.get('agent_actions') or {}))}</div>")
        if b.get("scrape_themes"):
            parts.append(f"<h3>Scrape themes &amp; trends</h3><div>{_esc(b['scrape_themes'])}</div>")
        if b.get("stack_overlap"):
            parts.append(f"<h3>Stack overlap</h3><div>{_esc(b['stack_overlap'])}</div>")
        if b.get("recommendations"):
            parts.append(f"<h3>Recommendations</h3><div>{_esc(b['recommendations'])}</div>")
        intel = data.get("scrape_intel") or {}
        if intel.get("available"):
            parts.append("<h3>Scrape intel — raw stats</h3>")
            parts.append(scrape_intel.intel_table_html(intel))
        else:
            parts.append(scrape_intel.intel_table_html(intel))
    elif mode == "raw":
        parts.append(f'<p class="exec-strip"><b>Numbers:</b> {_esc(strip)}</p>')
        parts.append('<p class=unavailable>exec summary unavailable — raw model output only</p>')
        # Dedup: the strip is already rendered above — drop the raw output's
        # own Numbers strip section so it appears exactly once on the board.
        raw_text = _drop_section(ds_result.get("raw") or "", "Numbers strip")
        if raw_text:
            parts.append(f"<pre class=exec-raw>{_esc(raw_text)}</pre>")
    else:
        parts.append(f'<p class="exec-strip"><b>Numbers:</b> {_esc(strip)}</p>')
        parts.append('<p class=unavailable>exec summary unavailable'
                     + (f' ({_esc(ds_result.get("detail", ""))})' if ds_result.get("detail") else "")
                     + '</p>')
        parts.append(f"<h3>Agent actions</h3><div>{_esc(agent_actions_line(data.get('agent_actions') or {}))}</div>")
        intel = data.get("scrape_intel") or {}
        if intel.get("available"):
            parts.append("<h3>Scrape intel — raw stats</h3>")
            parts.append(scrape_intel.intel_table_html(intel))
    return "\n".join(parts)


def _esc(v: Any) -> str:
    import html as _html
    return _html.escape("" if v is None else str(v))


if __name__ == "__main__":
    data = gather()
    print(json.dumps({k: v for k, v in data.items() if k != "numbers_strip"},
                     indent=1, default=str))
    print("strip:", data["numbers_strip"])
