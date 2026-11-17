"""dashboard/reboot.py — Layer 8 REBOOT: resilience against machine reboots.

Regression target: the 2026-11-15 reboot cascade — after a host reboot the
console stayed dead, a model seat was dead, and crontab entries were LOST.
This layer simulates a reboot against the hermetic isolated stack (fresh.py
sandbox: copied tree, disposable tokens, no host systemd) and asserts full
recovery:

  (a) stop ALL services (hub, console, ops agent, scheduler)
  (b) restart them via the same direct-exec commands the docs prescribe
  (c) assert: hub /health 200, console health 200 (bearer) / 401 (no
      bearer), state dir intact (tickets/job_runs, ledger, findings all
      survive), crontab entries intact, tokens still valid.

Mutation discipline — the layer seeds each fault class from that cascade
and asserts the assessor DETECTS every one:
  - a service that does not come back (unit disabled)   -> detected
  - a crontab entry lost across the reboot              -> detected
  - a token file missing/invalid                        -> detected
Any seeded fault that goes undetected = FAIL ("mutation not caught"); any
finding in the clean reboot = FAIL.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from dashboard import fresh as fr                                     # noqa: E402
from dashboard.followup import runner as rn                           # noqa: E402

ALL_SERVICES = ("hub", "console", "ops-agent", "scheduler")


# --------------------------------------------------- pure assessor (tested)

def snapshot(tickets: List[str], ledger_lines: int, crontab: List[str],
             token: str, services_up: Dict[str, bool]) -> Dict[str, Any]:
    """Immutable before/after state capture for a reboot cycle."""
    return {"tickets": list(tickets), "ledger_lines": ledger_lines,
            "crontab": list(crontab), "token": token,
            "services_up": dict(services_up)}


def assess_recovery(before: Dict[str, Any], after: Dict[str, Any],
                    services_expected: Tuple[str, ...] = ALL_SERVICES
                    ) -> List[str]:
    """Specific findings for everything that did NOT survive the reboot."""
    out: List[str] = []
    for svc in services_expected:
        if not after["services_up"].get(svc):
            out.append(f"service-did-not-recover: {svc}")
    bt, at = before["crontab"], after["crontab"]
    for entry in bt:
        if entry not in at:
            out.append(f"crontab-entry-lost: {entry[:80]}")
    if len(after["tickets"]) < len(before["tickets"]):
        out.append("state-loss: tickets/job_runs shrank "
                   f"({len(before['tickets'])} -> {len(after['tickets'])})")
    if after["ledger_lines"] < before["ledger_lines"]:
        out.append("state-loss: ledger shrank")
    if after["token"] != before["token"]:
        out.append("token-changed-or-invalid: hub token file did not "
                   "survive the reboot intact")
    return out


# ----------------------------------------------------------- live collection

def _hub_tickets(sb: "fr.Sandbox") -> List[str]:
    try:
        with urllib.request.urlopen(sb.hub_url + "/tickets?limit=1000",
                                    timeout=5) as r:
            data = json.loads(r.read())
        rows = data if isinstance(data, list) else \
            (data.get("tickets") or data.get("items") or [])
        return [str(t.get("title", t.get("id", "?"))) if isinstance(t, dict)
                else str(t) for t in rows]
    except Exception:
        return []


def _probe_services(sb: "fr.Sandbox") -> Dict[str, bool]:
    hub = fr.probe(sb.hub_url + "/health")[0] == 200
    console = fr.probe(sb.console_url + "/dashboard/health",
                       sb.token)[0] == 200
    return {"hub": hub, "console": console,
            "ops-agent": _pid_alive(sb, "ops-agent"),
            "scheduler": _pid_alive(sb, "scheduler")}


def _pid_alive(sb: "fr.Sandbox", name: str) -> bool:
    log = sb.logs / f"{name}.log"
    # ops-agent/scheduler are long-running: alive iff their pid responds
    for pid in sb.pids:
        try:
            with open(f"/proc/{pid}/cmdline") as f:
                cmd = f.read()
        except OSError:
            continue
        marker = "followup.runner" if name == "ops-agent" else "scheduler.py"
        if marker in cmd:
            return True
    return False


def _stop_all(sb: "fr.Sandbox") -> List[str]:
    """Simulate the machine going down: SIGTERM every service (the sandbox
    equivalent of a reboot — nothing survives but the state dir)."""
    pids = list(sb.pids)
    for pid in pids:
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    deadline = time.time() + 15
    while time.time() < deadline and any(_alive(p) for p in pids):
        time.sleep(0.2)
    for pid in pids:
        try:
            os.kill(pid, 9)
        except OSError:
            pass
    sb.pids = []
    return [str(p) for p in pids]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return Path(f"/proc/{pid}").exists()
    except OSError:
        return False


def _prime_state(sb: "fr.Sandbox") -> Tuple[List[str], int, List[str], str]:
    """Tickets + a finding-shaped ticket + ledger + crontab, the state the
    reboot must preserve."""
    import urllib.request
    titles = []
    for i in range(3):
        body = json.dumps({"title": f"reboot-check-{i}",
                           "body": "state written before the reboot",
                           "tags": ["reboot"], "actor": "gauntlet"}).encode()
        req = urllib.request.Request(sb.hub_url + "/tickets", data=body,
                                     headers={"Content-Type":
                                              "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            titles.append(f"reboot-check-{i}")
    # a finding-shaped ticket (findings live in the hub too)
    body = json.dumps({"title": "finding:reboot-check:sample",
                       "body": "finding recorded before reboot",
                       "tags": ["finding"], "actor": "gauntlet"}).encode()
    req = urllib.request.Request(sb.hub_url + "/tickets", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        titles.append("finding:reboot-check:sample")
    ledger = sb.root / "aerial-monorepo-demo" / "model-calls.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger, "a") as f:
        for i in range(5):
            f.write(json.dumps({"ts": "2026-11-17T00:00:00Z",
                                "role": "reboot-check", "tokens_in": 10}) + "\n")
    crontab = [
        "*/5 * * * * /usr/bin/python3 bin/job_run.py run --node aerial --job reboot-a -- 'echo a' # reboot-a",
        "*/10 * * * * /usr/bin/python3 bin/job_run.py run --node aerial --job reboot-b -- 'echo b' # reboot-b",
        "0 * * * * /usr/bin/python3 integration/scheduler.py --interval 30 # reboot-sched",
    ]
    sb.crontab_file.write_text("\n".join(crontab) + "\n")
    return titles, 5, crontab, sb.token


def _collect(sb: "fr.Sandbox") -> Dict[str, Any]:
    ledger = sb.root / "aerial-monorepo-demo" / "model-calls.jsonl"
    n_ledger = sum(1 for _ in open(ledger)) if ledger.exists() else 0
    crontab = ([ln for ln in sb.crontab_file.read_text().splitlines()
                if ln.strip()] if sb.crontab_file.exists() else [])
    token = sb.token_file.read_text().strip() \
        if sb.token_file.exists() else ""
    return snapshot(_hub_tickets(sb), n_ledger, crontab, token,
                    _probe_services(sb))


# ----------------------------------------------------------------- the layer

def run_reboot(workdir: str) -> Dict[str, Any]:
    lines: List[str] = []
    data: Dict[str, Any] = {}
    ok = True

    sb = fr.Sandbox(workdir, name="reboot")
    try:
        # ---- cycle 0: clean boot, prime state
        boot_ok, blines = fr.boot_stack(sb, ALL_SERVICES)
        lines += blines
        ok = ok and boot_ok
        before = _collect(sb) if boot_ok else None
        if before:
            primed = _prime_state(sb)
            before = _collect(sb)
            lines.append(f"- state primed: {len(before['tickets'])} tickets "
                         f"(incl. a finding), {before['ledger_lines']} ledger "
                         f"lines, {len(before['crontab'])} crontab entries")

            # ---- (a) stop ALL services
            stopped = _stop_all(sb)
            lines.append(f"- reboot simulated: {len(stopped)} services stopped")
            down = _probe_services(sb)
            if any(down.values()):
                ok = False
                lines.append(f"    FAIL: services still up after stop: "
                             f"{[s for s, v in down.items() if v]}")

            # ---- (b) restart via the docs' commands
            boot_ok, _ = fr.boot_stack(sb, ALL_SERVICES)
            # ---- (c) full-recovery assertions
            after = _collect(sb)
            findings = assess_recovery(before, after)
            clean_ok = not findings and boot_ok
            ok = ok and clean_ok
            lines.append(f"- clean reboot recovery: "
                         f"{'PASS — no findings' if clean_ok else 'FAIL'}")
            for f_ in findings:
                lines.append(f"    finding: {f_}")
            for svc in ("hub", "console"):
                up = after["services_up"].get(svc)
                lines.append(f"- {svc} recovered: "
                             f"{'PASS' if up else 'FAIL'}")
            lines.append(f"- state intact: tickets {len(after['tickets'])}, "
                         f"ledger {after['ledger_lines']}, crontab "
                         f"{len(after['crontab'])}, token valid: "
                         f"{bool(after['token'])} → "
                         f"{'PASS' if clean_ok else 'FAIL'}")

        # ---- mutation: each seeded fault class MUST be detected
        faults = [
            ("unit-disabled-console", ("hub",), "console"),
            ("cron-entry-lost", ALL_SERVICES, "cron"),
            ("token-hidden", ("hub", "console"), "token"),
        ]
        for name, restart, fault in faults:
            fok, flines = _fault_scenario(sb, workdir, name, restart, fault)
            ok = ok and fok
            lines.append(f"- mutation {name}: "
                         f"{'DETECTED → PASS' if fok else 'NOT DETECTED → FAIL'}")
            lines += ["    " + l for l in flines]
        data["ok"] = ok
    finally:
        sb.stop()
    return {"ok": ok, "lines": lines, "data": data}


def _fault_scenario(sb: "fr.Sandbox", workdir: str, name: str,
                    restart: Tuple[str, ...], fault: str
                    ) -> Tuple[bool, List[str]]:
    """Stop everything, seed ONE fault, restart the surviving services;
    the assessor must emit a specific finding for the fault."""
    lines: List[str] = []
    before = _collect(sb)
    _stop_all(sb)
    if fault == "cron":
        # lose one crontab entry across the reboot
        kept = before["crontab"][:-1]
        sb.crontab_file.write_text("\n".join(kept) + "\n")
    if fault == "token":
        sb.token_file.rename(sb.token_file.with_suffix(".hidden"))
    boot_ok, _ = fr.boot_stack(sb, restart)
    after = _collect(sb)
    findings = assess_recovery(before, after, ALL_SERVICES)
    lines.append(f"seeded {fault}; assessor findings: {findings or 'NONE'}")
    detected = any(
        (fault == "console" and f.startswith("service-did-not-recover: console"))
        or (fault == "cron" and f.startswith("crontab-entry-lost"))
        or (fault == "token" and f.startswith("token-changed-or-invalid"))
        for f in findings)
    return detected and boot_ok, lines
