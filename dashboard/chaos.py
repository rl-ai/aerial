"""dashboard/chaos.py — the day-type injector for accelerated validation.

Generates per-virtual-day event variety into the REAL stores so a 7-day
observation window can be validated in a fraction of real time:

  day types
    quiet         few clean runs, no failures
    failure-storm many failed job_runs, dead seats, health flags red
    mixed         some failures among successes
    recovery      green after a failure-storm (recovery note path)
    anomalous     month-end style spike (calls/tokens/runs ×10)

GUARD: inject_day() is a no-op-with-error unless BOTH
    AERIAL_TIME_ACCELERATION > 1  AND  AERIAL_CHAOS = 1
are set — it can never touch production stores from a normal run.

Deterministic: day d of seed S always generates the same events
(random.Random(S * 1000 + d)).
"""
from __future__ import annotations

import json
import os
import random
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from dashboard import clock

DAY_TYPES = ("quiet", "failure-storm", "mixed", "recovery", "anomalous")

#: Default 7-day sequence: every day type at least once, failure-storm late
#: enough for the trend math to have a 3-day baseline, ending green.
DEFAULT_SEQUENCE = ("quiet", "mixed", "quiet", "failure-storm",
                    "anomalous", "recovery", "mixed")

#: Real store defaults (validation writes here only under the guard).
from deployment_profile import pvalue as _pv
from deployment_profile import value as _val
DEFAULT_HUB_DB = str(_pv("hub_db"))
DEFAULT_SCHEDULER_LOG = str(_pv("scheduler_log"))
DEFAULT_LEDGER = str(_pv("model_calls_ledger"))
DEFAULT_FLAGS = str(_pv("validation_flags"))
DEFAULT_BRIDGE_LOG = str(_pv("bridge_log"))
DEFAULT_BOARD_HTML = str(_pv("board_html"))
DEFAULT_SHADOW_COMP = str(_pv("validation_shadow"))


class ChaosGuardError(RuntimeError):
    """Raised when the injector is invoked without the validation guards."""


def _guard() -> None:
    try:
        accel = float(os.environ.get("AERIAL_TIME_ACCELERATION", "1.0") or 1.0)
    except ValueError:
        accel = 1.0
    chaos = os.environ.get("AERIAL_CHAOS", "").strip() == "1"
    if not (accel > 1.0 and chaos):
        raise ChaosGuardError(
            "chaos injector requires AERIAL_TIME_ACCELERATION > 1 AND "
            "AERIAL_CHAOS=1 — refusing to touch stores")


def day_profile(day_type: str) -> Dict[str, int]:
    """Deterministic event counts per day type."""
    return {
        "quiet":         {"runs": 4,  "failed": 0,  "calls": 18,  "seats_dead": 0},
        "failure-storm": {"runs": 30, "failed": 25, "calls": 40,  "seats_dead": 2},
        "mixed":         {"runs": 14, "failed": 5,  "calls": 26,  "seats_dead": 0},
        "recovery":      {"runs": 10, "failed": 1,  "calls": 22,  "seats_dead": 0},
        "anomalous":     {"runs": 44, "failed": 9,  "calls": 210, "seats_dead": 0},
    }[day_type]


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _scheduler_line(ts: float, event: str, detail: str) -> str:
    return f"{_iso(ts)} {event} {detail}"


def inject_day(day: int, *, seed: int = 20261112,
               day_type: Optional[str] = None,
               hub_db: str = DEFAULT_HUB_DB,
               scheduler_log: str = DEFAULT_SCHEDULER_LOG,
               ledger: str = DEFAULT_LEDGER,
               flags_path: str = DEFAULT_FLAGS,
               bridge_log: str = DEFAULT_BRIDGE_LOG,
               board_html: str = DEFAULT_BOARD_HTML,
               shadow_comp: str = DEFAULT_SHADOW_COMP,
               ) -> Dict[str, Any]:
    """Generate one virtual day's events into the stores. Deterministic.

    Returns a summary dict of what was written (also used by validate_days
    for per-day reporting). Guarded — see module docstring.
    """
    _guard()
    dtype = day_type or DEFAULT_SEQUENCE[day % len(DEFAULT_SEQUENCE)]
    prof = day_profile(dtype)
    rng = random.Random(seed * 1000 + day)

    day0 = clock.day_start(day)
    # spread events across the virtual day (we write them up-front; the
    # virtual clock only needs to be *past* them when consumers run)
    slots = [day0 + 86400.0 * (i + 1) / (prof["runs"] + 1) for i in range(prof["runs"])]

    # --- hub.db job_runs (test rows, clearly marked) -----------------------
    rows = []
    for i, ts in enumerate(slots):
        failed = bool(prof["failed"] and i < prof["failed"])
        dur = round(rng.uniform(2.0, 90.0), 1)
        rows.append((f"chaos-d{day}-{i:03d}", "chaos-validation", "chaos-node",
                     _iso(ts), _iso(ts + dur), "failed" if failed else "ok",
                     1 if failed else 0, dur,
                     f"[chaos:{dtype}] " + ("boom: hub write wedge"
                                            if failed else "ok"),
                     "", "{}", None))
    con = sqlite3.connect(hub_db)
    try:
        con.executemany(
            "INSERT OR REPLACE INTO job_runs (run_id, job_id, node, started_at,"
            " ended_at, status, exit_code, duration_s, summary, log_tail, meta,"
            " pid) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        con.commit()
    finally:
        con.close()

    # --- scheduler.log lines ------------------------------------------------
    sched: List[str] = []
    for i, ts in enumerate(slots):
        cmd = " ".join([
            str(_val("paths.python_bin", "python3")),
            str((Path(__file__).resolve().parent.parent
                 / "integration" / "run_shadow_hierarchy.py")),
            f"--chaos-day={day}"])
        sched.append(_scheduler_line(ts, "started", cmd))
        if rows[i][5] == "failed":
            sched.append(_scheduler_line(ts + rows[i][7], "finished",
                                         "exit=1 error=chaos-injected-failure"))
        else:
            sched.append(_scheduler_line(ts + rows[i][7], "finished",
                                         "exit=0 out=DONE"))

    # --- ledger entries (model-calls.jsonl) ---------------------------------
    ledger_lines = []
    for i in range(prof["calls"]):
        ts = day0 + 86400.0 * (i + 1) / (prof["calls"] + 1)
        server = rng.choice(["glm", "deepseek", "kimi"])
        ledger_lines.append(json.dumps({
            "ts": _iso(ts), "role": f"chaos-{dtype}-{rng.randrange(4)}",
            "server": server, "slot": None, "effort": "low",
            "tokens_in": rng.randrange(300, 4000),
            "prompt_n": rng.randrange(1, 900),
            "cache_n": rng.randrange(0, 500),
            "prompt_ms": round(rng.uniform(10, 9000), 3),
            "predicted_n": rng.randrange(50, 2100),
            "predicted_ms": round(rng.uniform(50, 60000), 3),
            "finish": rng.choice(["stop", "length"]), "verdict": None,
            "ticket": None, "chaos_day": day, "chaos_type": dtype,
        }))

    # --- flags (health-status-shaped, validation-only file) -----------------
    flags: Dict[str, Any] = {"ok": True, "ts": _iso(day0 + 86400.0 - 60),
                             "day": day, "day_type": dtype,
                             "failed_checks": {}}
    if prof["seats_dead"]:
        for s in range(prof["seats_dead"]):
            flags["failed_checks"][f"model_seat_{s}"] = {
                "ok": False,
                "detail": f"seat dead: port {18102 + s} /v1/models refused"}
    if prof["failed"] >= 20:  # storm: hub wedge shows on the health surface
        flags["ok"] = False
        flags["failed_checks"]["hub_wedge"] = {
            "ok": False, "detail": "hub write wedge — job_runs stale 35min"}

    # --- bridge log + board render touch (freshness for the sre monitor) ---
    bridge_line = (f"{_iso(day0 + 86300)} ingest run: "
                   f"{prof['runs'] * 12} items\n")
    board_note = (f"<!-- chaos render d{day} {dtype} {_iso(day0 + 86340)} -->\n")

    os.makedirs(os.path.dirname(hub_db) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(scheduler_log) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(ledger) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(flags_path) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(bridge_log) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(board_html) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(shadow_comp) or ".", exist_ok=True)

    with open(scheduler_log, "a") as f:
        f.write("\n".join(sched) + "\n")
    with open(ledger, "a") as f:
        f.write("\n".join(ledger_lines) + ("\n" if ledger_lines else ""))
    with open(flags_path, "w") as f:
        json.dump(flags, f)
    with open(bridge_log, "a") as f:
        f.write(bridge_line)
    with open(board_html, "a") as f:
        f.write(board_note)

    # --- shadow comparison actions (promotion-gate evidence) ---------------
    # Would-have-done actions for the day; divergence/evidence profile by
    # day type (storms diverge, quiet days agree).
    shadow: List[Dict[str, Any]] = []
    n_shadow = max(prof["runs"] // 2, 2)
    for i in range(n_shadow):
        ts = day0 + 86400.0 * (i + 1) / (n_shadow + 1)
        diverge = dtype == "failure-storm" and i % 2 == 0 \
            or dtype == "anomalous" and i == 0
        act: Dict[str, Any] = {
            "ts": ts, "role": "curator",
            "action": f"chaos-d{day}-relabel-stage-{i}",
            "actual_outcome": "relabel done",
            "evidence_label": "verified",
            "divergence": bool(diverge),
            "draft_mandate_spec": {"scope": f"relabel chaos-d{day} stage {i}",
                                   "expiry": _iso(day0 + 7 * 86400)},
        }
        if diverge:
            act["divergence_label"] = "would-have-been-wrong"
        if dtype == "mixed" and i == 1:
            act["evidence_label"] = "inferred"
        shadow.append(act)
    with open(shadow_comp, "a") as f:
        f.write(json.dumps({"day": day, "day_type": dtype,
                            "role": "curator", "actions": shadow}) + "\n")

    return {"day": day, "day_type": dtype, "job_runs": len(rows),
            "failed": sum(1 for r in rows if r[5] == "failed"),
            "scheduler_lines": len(sched), "ledger_lines": len(ledger_lines),
            "flags": flags, "shadow_actions": len(shadow)}


def read_flags(path: str = DEFAULT_FLAGS) -> Dict[str, Any]:
    """Read the validation flags file (health-status shaped)."""
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}
