"""dashboard/recurring_miss.py — Layer 4 extension: recurring-failure day-type.

The operator's finding: `schedule:*` misses were the weakest fix path — findings could
end up silently closed or left unclassified. This layer makes the correct
behavior a TESTED contract, using the remediation contract
(dashboard/followup/remediation.py) as the gate:

  A scheduled job that misses repeatedly across a virtual day
  (chaos.py day-type "recurring-miss": N misses at cron-shaped intervals)
  must, through the ops agent's remediation loop, end in exactly ONE of:

    - closed via a VERIFIED fix  (run_remediation proved=True: the re-trigger
      re-ran the payload and the AFTER probe shows the symptom gone), or
    - ESCALATED with before/after output (proved=False, escalation text
      attached, finding stays OPEN).

  Anything else — silently closed, or unclassified — FAILS the layer.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from dashboard.followup import remediation as remd                    # noqa: E402
from dashboard.followup import runner as rn                           # noqa: E402
from dashboard.followup.classifier import classify                    # noqa: E402
from dashboard.followup.remediation import (Remediation,              # noqa: E402
                                            RemediationResult,
                                            run_remediation)

DEMO_JOB = "gauntlet-recurring-demo"


# --------------------------------------------------------------- the contract

def schedule_miss_finding(job_id: str, day: int, spec: str = "*/5 * * * *"
                          ) -> rn.OpsFinding:
    """A finding shaped exactly like runner.schedule_findings() produces for
    a missed scheduled job (fid schedule:<job>:<day>, kind schedule-miss)."""
    return rn.OpsFinding(
        finding_id=f"schedule:{job_id}:{day}",
        claim=(f"scheduled job {job_id} MISSED repeatedly (recurring-miss "
               f"day) — spec {spec}"),
        reversible=True, scope="internal",
        tags=("schedule-reconciliation", job_id, "schedule-missed"),
        proposed_action="re-trigger the missed job with its full payload",
        check={"kind": "schedule-miss", "job_id": job_id, "due": True})


def schedule_miss_remediation(payload_cmd: str, marker_path: str,
                              job_id: str = DEMO_JOB, ticket: str = "",
                              ) -> Tuple[Remediation, List[str]]:
    """The schedule-miss fix path: re-trigger the job with its FULL payload
    (runner.retrigger_command); the symptom probe is the payload's own
    observable effect (its marker file)."""
    cmd = rn.retrigger_command(job_id, ticket, payload_cmd)

    def symptom() -> Tuple[bool, str]:
        p = Path(marker_path)
        if not p.exists():
            return (False, f"payload marker missing: {marker_path}")
        body = p.read_text(errors="replace").strip()
        return (bool(body), f"marker: {body[:200]!r}")

    def fix(_: Remediation) -> Dict[str, Any]:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=120)
            return {"exit": proc.returncode,
                    "output_tail": ((proc.stdout or "") +
                                    (proc.stderr or ""))[-2000:]}
        except Exception as exc:
            return {"exit": None, "output_tail": f"[fix error: {exc!r}]"}

    return Remediation(
        id=f"schedule-miss:{job_id}",
        finding_match=("schedule-miss", f"schedule:{job_id}"),
        symptom_check=symptom,
        fix_cmd=" ".join(cmd),
        verify_check=symptom,
        max_attempts=2,
        requires_human=False,
    ), cmd


def remediate_or_escalate(finding, rem: Remediation,
                          fix_executor=None) -> Dict[str, Any]:
    """The ops-agent loop policy for a schedule miss, encoded and enforced.

    Returns {"action": "closed-verified"|"escalated-open", "result": ...}.
    The invariant under test: the finding is NEVER silently closed (closed
    without a proved AFTER probe) and NEVER left unclassified.
    """
    result: RemediationResult = run_remediation(rem, finding,
                                                fix_executor=fix_executor)
    if result.proved:
        action = "closed-verified"
    else:
        action = "escalated-open"
        assert result.escalation, "unproved fix must carry escalation text"
    cls = classify(finding)
    return {"action": action, "result": result, "classification": cls}


def assert_contract(outcome: Dict[str, Any]) -> Tuple[bool, str]:
    """The layer assertion: closed-verified OR escalated-open, and the
    finding classified either way. Anything else = the gap class."""
    action = outcome["action"]
    if action not in ("closed-verified", "escalated-open"):
        return False, f"unclassified outcome: {action}"
    if action == "closed-verified":
        if not getattr(outcome["result"], "proved", False):
            return False, ("SILENT CLOSE: finding closed without a proved "
                           "AFTER probe (the 2026-11-17 gap class)")
    if action == "escalated-open" and not outcome["result"].escalation:
        return False, "escalated without before/after output"
    if not outcome.get("classification"):
        return False, "finding unclassified"
    return True, f"{action} (class {outcome['classification']})"


# ------------------------------------------------------------------ scenario

def scenario(workdir: str, day: int = 0, seed: int = 20261117
             ) -> Dict[str, Any]:
    """Inject a recurring-miss virtual day into fixture stores and run the
    remediation contract twice:

      1. healthy payload -> the loop must close the miss via a VERIFIED fix
      2. broken payload  -> the loop must ESCALATE (finding stays OPEN)

    plus the mutation inversion: a loop that closes the broken case is the
    2026-11-17 gap (silently closed schedule misses) and FAILS the layer.
    """
    from dashboard import chaos
    lines: List[str] = []
    ok = True

    wd = Path(workdir) / "recurring-miss"
    wd.mkdir(parents=True, exist_ok=True)
    hub_db = str(wd / "hub.db")
    sched_log = str(wd / "scheduler.log")

    # fixture hub db needs the job_runs schema (chaos writes into a real
    # store's schema; here we stand it up)
    import sqlite3 as _sq
    con = _sq.connect(hub_db)
    con.execute("CREATE TABLE IF NOT EXISTS job_runs "
                "(run_id TEXT PRIMARY KEY, job_id TEXT, node TEXT,"
                " started_at TEXT, ended_at TEXT, status TEXT,"
                " exit_code INT, duration_s REAL, summary TEXT,"
                " log_tail TEXT, meta TEXT, pid INT)")
    con.commit()
    con.close()

    # chaos guard: validation-only stores under the workdir; the virtual
    # clock needs an epoch for day_start()
    os.environ["AERIAL_TIME_ACCELERATION"] = "50"
    os.environ["AERIAL_CHAOS"] = "1"
    os.environ.setdefault("AERIAL_VIRTUAL_EPOCH", "1763000000")
    try:
        summary = chaos.inject_day(day, seed=seed, day_type="recurring-miss",
                                   hub_db=hub_db, scheduler_log=sched_log,
                                   ledger=str(wd / "model-calls.jsonl"),
                                   flags_path=str(wd / "flags.json"),
                                   bridge_log=str(wd / "bridge.log"),
                                   board_html=str(wd / "board.html"),
                                   shadow_comp=str(wd / "shadow.jsonl"))
    finally:
        os.environ.pop("AERIAL_CHAOS", None)
        os.environ.pop("AERIAL_TIME_ACCELERATION", None)
    n_misses = (summary.get("recurring_misses") or [{}])[0].get("misses", 0)
    lines.append(f"- injected recurring-miss day d{day}: "
                 f"{summary['job_runs']} job_runs, {n_misses} scheduled "
                 f"misses of {chaos_seed_job(day)} at 300s intervals")
    ok = ok and n_misses >= 3
    if n_misses < 3:
        lines.append("    FAIL: day-type did not produce a recurring miss pattern")

    finding = schedule_miss_finding(chaos_seed_job(day), day)

    # case 1: fixable — retrigger runs the payload, marker appears
    marker1 = wd / "marker-fixed.txt"
    payload1 = f"echo fixed-by-retrigger > {marker1}"
    rem1, cmd1 = schedule_miss_remediation(payload1, str(marker1))
    out1 = remediate_or_escalate(finding, rem1)
    ok1, d1 = assert_contract(out1)
    lines.append(f"- fixable payload: {out1['action']} ({d1}) → "
                 f"{'PASS' if ok1 else 'FAIL'}")
    if out1["action"] == "closed-verified":
        lines.append(f"    proof: AFTER probe {out1['result'].after}")
    ok = ok and ok1 and out1["action"] == "closed-verified"

    # case 2: broken payload — fix cannot prove; must ESCALATE, stay OPEN
    marker2 = wd / "marker-broken.txt"
    payload2 = f"exit 3  # payload is broken (nonzero exit, no marker) > {marker2}"
    rem2, _cmd2 = schedule_miss_remediation(payload2, str(marker2))
    out2 = remediate_or_escalate(finding, rem2)
    ok2, d2 = assert_contract(out2)
    escalated = out2["action"] == "escalated-open" and \
        bool(out2["result"].escalation)
    lines.append(f"- broken payload: {out2['action']} ({d2}) → "
                 f"{'PASS' if ok2 and escalated else 'FAIL'}")
    lines.append(f"    escalation: {out2['result'].escalation[:200] if out2['result'].escalation else 'NONE'}")
    ok = ok and ok2 and escalated
    if not escalated:
        lines.append("    FAIL: broken fix did NOT escalate — the silent-close gap")

    return {"ok": ok, "lines": lines,
            "data": {"day": day, "misses": n_misses,
                     "fixable": out1["action"], "broken": out2["action"],
                     "hub_db": hub_db}}


def chaos_seed_job(day: int) -> str:
    return f"{DEMO_JOB}-d{day}"
