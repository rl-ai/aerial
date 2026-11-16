#!/usr/bin/env python3
"""Run the full Aerial agent hierarchy against a failed job.

Chains three Hermes agent sessions (curator -> lead-main -> exec-ray),
each with file+terminal tool access, in a shared per-incident workspace.
The exec brief is posted on the hub as a new ticket; a summary comment
goes on the epic.

Usage:
    python3 run_shadow_hierarchy.py --latest-failed
"""

import argparse
import json
import os
import sqlite3
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from deployment_profile import value as _dpv   # noqa: E402

HUB_URL = os.environ.get("A5_HUB_URL", "http://127.0.0.1:8020")
HUB_TOKEN_FILE = os.environ.get("A5_HUB_TOKEN") or os.environ.get(
    "AERIAL_HUB_WRITE_TOKEN_FILE") or str(
        _dpv("cluster.hub_write_token_file") or _dpv("cluster.hub_token_file"))
HUB_DB = os.environ.get(
    "A5_HUB_DB", str(_dpv("paths.hub_db")))
HERMES_BIN = os.environ.get("HERMES_BIN") or os.path.expanduser("~/.local/bin/hermes")
SHADOW_BASE = str(_dpv("paths.shadow_dir"))
PROVIDER = str(_dpv("cluster.provider") or "custom:aerial-local")
MODEL = str(_dpv("cluster.model") or "local-model")
EPIC = str(_dpv("cluster.epic_id") or "")

TIER_TASKS = {
    "curator": """You are the Ops Analyst for the Aerial platform.

A job has failed. Investigate it.

1. Read incident.json — the failed job's data (error, log_tail, summary).
2. The source code that failed may be reachable via the paths in the
   stack trace. Read it if the files exist and are accessible.
3. Run diagnostic commands with the terminal tool:
   - docker logs on the ticket-hub container if the failure is hub-side
   - Check filesystem state on any database files mentioned
   - curl http://127.0.0.1:8020/health for hub liveness
   - Look for recent changes that might explain the failure

Write your findings in findings.md:
## What happened
## Root cause (with evidence labels: verified/inferred/missing)
## Hypotheses (ranked)
## Recommended fix

Every claim must cite the evidence you gathered. Claims without
evidence must be labeled "inferred" or "missing". Do not guess.
""",
    "lead-main": """You are the Lead (supervisor) for the Aerial platform.

The Ops Analyst has investigated a failed job and written findings
in findings.md. Read the findings AND the evidence they cite.

For each hypothesis: accept or reject with a reason.
Decide which recommended fix to pursue first.

Write your decisions in review.md:
## Hypothesis review
## Decision
## Next steps
""",
    "exec-ray": """You are the Exec for the Aerial platform.

The Lead has reviewed the Ops Analyst's findings and written decisions
in review.md. Read the decisions.

Compose a concise brief for Ray in brief.md:
- What happened (1 sentence)
- What was decided (1-2 sentences)
- What Ray needs to do (specific, actionable)

Be concise. Ray reads this in the hub ticket.
""",
}


def _hub_token() -> str:
    with open(os.path.expanduser(HUB_TOKEN_FILE)) as fh:
        return fh.read().strip()


def _hub_post(path: str, body: dict) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        HUB_URL.rstrip("/") + path, data=data, method="POST",
        headers={"content-type": "application/json",
                 "authorization": "Bearer " + _hub_token()})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _find_latest_failed_job() -> dict:
    conn = sqlite3.connect(HUB_DB)
    conn.row_factory = sqlite3.Row
    r = conn.execute(
        "SELECT * FROM job_runs WHERE status != 'ok' "
        "AND job_id NOT LIKE '%qwen-listen%' "
        "AND job_id NOT LIKE '%curator-poll%' "
        "ORDER BY rowid DESC LIMIT 1").fetchone()
    conn.close()
    if r is None:
        raise SystemExit("no failed job found")
    return dict(r)


def _run_session(profile: str, ws: Path, max_turns: int = 30) -> None:
    """Run one Hermes agent session with cwd=workspace; outputs are files."""
    q = ("Do the task in TASK.md exactly. Work only inside this folder. "
         "incident.json has the failed job's data.")
    cmd = [str(HERMES_BIN), "-p", profile, "chat", "-Q", "--oneshot",
           "-t", "file,terminal",
           "--provider", PROVIDER, "-m", MODEL,
           "--max-turns", str(max_turns),
           "--reasoning", "low",
           "-q", q]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                          cwd=str(ws))
    if proc.returncode != 0:
        print(f"  WARN: {profile} session exited {proc.returncode}")
        if proc.stderr.strip():
            print("  stderr:", proc.stderr.strip()[:200])


def _read(ws: Path, name: str) -> str:
    p = ws / name
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _run_exec_brief(ws: Path) -> None:
    """Exec tier: a tool-less one-shot DeepSeek call (ds-writer role) that
    synthesizes the brief from the curator findings + lead review, which are
    inlined by model_call. Brief text lands in brief.md."""
    ask = ("Compose the executive brief for Ray (the operator) in plain "
           "markdown: '- What happened (1 sentence)\\n- What was decided "
           "(1-2 sentences)\\n- What Ray needs to do (specific, actionable "
           "numbered items)'. Be concise — Ray reads this in a hub ticket. "
           "Output ONLY the brief text, nothing else.")
    cmd = ["python3", str(_dpv("paths.model_call_script") or (Path(__file__).resolve().parent.parent / "bin" / "model_call.py")),
           "--role", "ds-writer", "--max-tokens", "4096",
           "--files", str(ws / "findings.md"), str(ws / "review.md"),
           "--ask", "/tmp/exec-brief-ask.txt",
           "--out", str(ws / "brief-call.json")]
    (ws / "ask.txt").write_text(ask, encoding="utf-8")
    import shutil
    shutil.copy(ws / "ask.txt", "/tmp/exec-brief-ask.txt")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        print(f"  WARN: exec brief call exited {proc.returncode}")
        if proc.stderr.strip():
            print("  stderr:", proc.stderr.strip()[:200])
        return
    try:
        raw = (ws / "brief-call.json").read_text(encoding="utf-8")
        try:
            data = json.loads(raw)
            text = data.get("text") or data.get("content") or ""
            if not text and isinstance(data.get("choices"), list):
                text = data["choices"][0].get("message", {}).get("content", "")
        except (json.JSONDecodeError, KeyError, IndexError, TypeError):
            text = raw  # model_call --out writes the raw text when unstructured
        if text.strip():
            (ws / "brief.md").write_text(text, encoding="utf-8")
    except Exception as exc:
        print(f"  WARN: could not read the brief call output: {exc}")


def _run_dashboard_build(ws: Path) -> None:
    """Exec directive hand-off: a GLM tool session builds the rendered
    deliverable (dashboard.html) from the brief + findings + review.
    Exec decides; GLM workers own tool use. The worker profile is
    sandboxed, so the workspace is staged under ~/workspaces/worker/
    (the only sandbox-visible mount) and the result copied back."""
    (ws / "TASK.md").write_text(
        "Build dashboard.html in this folder — the rendered executive "
        "dashboard for the incident in this workspace.\n\n"
        "Inputs (read them): brief.md (the exec's brief — this is the "
        "message to the operator), findings.md (evidence-labeled findings), "
        "review.md (the lead's decisions), incident.json (raw job data).\n\n"
        "Requirements:\n"
        "- Self-contained single HTML file, no external assets\n"
        "- Sections: What happened / What was decided / What Ray needs to do "
        "(from brief.md), evidence table with verified/inferred/missing "
        "labels (from findings.md), hypothesis decisions (from review.md)\n"
        "- Clean, minimal, readable; the operator reads this instead of the "
        "raw files\n"
        "- Every claim in the HTML must trace to its source file\n",
        encoding="utf-8")
    import shutil
    stage = Path.home() / "workspaces/worker/lanes-exec-dashboard" / ws.name
    if stage.exists():
        shutil.rmtree(stage)
    shutil.copytree(ws, stage)
    _run_session("worker", stage, max_turns=20)
    built = stage / "dashboard.html"
    if built.exists():
        shutil.copy(built, ws / "dashboard.html")
        print(f"  dashboard.html: {built.stat().st_size} bytes (copied back)")
    else:
        print("  WARN: the worker session produced no dashboard.html")
        for f in sorted(stage.iterdir()):
            print("   stage has:", f.name)


def run_hierarchy(job: dict, ws: Path, dashboard: bool = False) -> dict:
    results = {}
    tiers = list(TIER_TASKS.items())
    for i, (profile, task) in enumerate(tiers, 1):
        if profile == "exec-ray":
            continue  # exec tier runs as a tool-less DeepSeek one-shot below
        print(f"  [{i}/3] {profile}: working...")
        (ws / "TASK.md").write_text(task, encoding="utf-8")
        _run_session(profile, ws)
    print("  [3/3] exec (ds-writer, tool-less): composing brief...")
    _run_exec_brief(ws)
    if dashboard:
        print("  [3b] exec directive: delegating the dashboard build to a GLM tool session...")
        _run_dashboard_build(ws)
    results["curator_findings"] = _read(ws, "findings.md")
    results["lead_review"] = _read(ws, "review.md")
    results["exec_brief"] = _read(ws, "brief.md")
    print(f"  findings {len(results['curator_findings'])} chars, "
          f"review {len(results['lead_review'])} chars, "
          f"brief {len(results['exec_brief'])} chars")
    return results


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run the Aerial agent hierarchy")
    ap.add_argument("--latest-failed", action="store_true",
                    help="use the latest failed job from the hub DB")
    ap.add_argument("--dashboard", action="store_true",
                    help="after the exec brief, delegate building a rendered "
                         "dashboard.html to a GLM tool session")
    ap.add_argument("--dedupe-hours", type=float, default=1.0,
                    help="skip if the same job was already run within this "
                         "many hours (duplicate-run guard)")
    ap.add_argument("--force", action="store_true",
                    help="override the duplicate-run guard")
    args = ap.parse_args(argv)
    if not args.latest_failed:
        ap.error("specify --latest-failed")

    job = _find_latest_failed_job()
    print(f"Failed job: {job['job_id']}")
    print(f"Error: {(job.get('summary') or '')[:100]}")

    # Duplicate-run guard (tuning cycle 1): one incident must not be
    # processed by concurrent invocations (scheduler + manual runs).
    # A run is a duplicate when a workspace for the same job_id already
    # exists younger than the dedupe window, unless --force.
    if not args.force:
        recent = sorted(Path(SHADOW_BASE).glob(
            f"{job['job_id'].split(':')[-1]}-*"))
        if recent:
            age_h = (time.time() - recent[-1].stat().st_mtime) / 3600.0
            if age_h < args.dedupe_hours:
                print(f"DUPLICATE: {recent[-1].name} ran {age_h:.1f}h ago "
                      f"(< {args.dedupe_hours}h). Use --force to override.")
                return None

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    incident_id = f"{job['job_id'].split(':')[-1]}-{ts}"
    ws = Path(SHADOW_BASE) / incident_id
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "incident.json").write_text(
        json.dumps(job, indent=1, default=str), encoding="utf-8")
    print(f"Workspace: {ws}")

    results = run_hierarchy(job, ws, dashboard=args.dashboard)

    brief = results.get("exec_brief", "")
    try:
        resp = _hub_post("/tickets",
                         {"title": f"Shadow report: {job['job_id']}",
                          "body": brief or "(no brief produced)",
                          "tags": ["a5-qualification", "shadow"],
                          "actor": "test-harness"})
        ticket_id = resp.get("id", resp.get("ticket_id"))
        print(f"Report posted: {ticket_id}")
    except Exception as exc:
        print(f"WARN: could not post report on the hub: {exc}")
        ticket_id = None

    try:
        summary = (f"Shadow hierarchy run for {job['job_id']}: "
                   f"curator {len(results.get('curator_findings', ''))} chars, "
                   f"lead {len(results.get('lead_review', ''))} chars, "
                   f"exec {len(results.get('exec_brief', ''))} chars. "
                   f"Workspace: {ws}. Report ticket: {ticket_id or 'n/a'}")
        _hub_post(f"/tickets/{EPIC}/events",
                  {"kind": "comment", "payload": {"body": summary}})
        print("Epic updated")
    except Exception as exc:
        print(f"WARN: could not update the epic: {exc}")

    print("DONE")
    return results


if __name__ == "__main__":
    main()
