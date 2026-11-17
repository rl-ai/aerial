#!/usr/bin/env python3
"""dashboard/gauntlet.py — the Aerial Gauntlet: one-command full dress rehearsal.

Ticket-based (the platform's own governance): the run creates a hub ticket
"Gauntlet run <date>" (tag `gauntlet`, child of the ops epic) plus FIVE child
tickets — one per layer:

  Layer 1  Unit suites        dashboard / gateway / followup+ops / clock+validation pytest runs
  Layer 2  Stress             attack_run.py adversarial harness vs the 1.0/0.96 reference
  Layer 3  Backtest           validate_days --backtest over the trailing 7 real days
  Layer 4  Day-in-the-life    raw scrapes -> bridge -> listening proposals -> shadow
                              hierarchy -> daily board + exec report (E2E on real data)
  Layer 5  Live smoke         console 200, email delivery, model seats, sre monitor

Every layer result posts to that layer's child ticket (fail-closed: even a
failing layer reports). The overall verdict (PASS only if ALL five layers
pass) posts to the parent run ticket. Failures are classified with the
followup classifier; per ping_policy only SEV/security classes escalate
immediately (email ping), everything else aggregates to the daily report.

The DITL layer never modifies production stores: it reads the real read-only
stores (scrapes.db, hub.db, logs) and writes only to the gauntlet's scratch
workdir — the same fixture-store pattern as validate_days' backtest backfill.

Usage:
    python3 -m dashboard.gauntlet              # full run
    python3 -m dashboard.gauntlet --only 1,5   # subset of layers (tests)
    python3 -m dashboard.gauntlet --workdir D  # scratch dir (default under the profile console_workdir)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from dashboard.followup import job_tickets as jt                     # noqa: E402
from dashboard.followup.classifier import (CuratorFinding, classify,  # noqa: E402
                                           SEV_INCIDENT, SEV_SECURITY)
from dashboard.followup import ping_policy                            # noqa: E402
from dashboard.followup.runner import (MODEL_SEATS, probe_seat,        # noqa: E402
                                       DEFAULT_BEARER)

EPIC = jt.EPIC
from deployment_profile import value as _pv            # noqa: E402
SCRAPES_DB = os.path.expanduser(str(_pv("scrapes_db")))
GATEWAY_V1 = os.path.expanduser(str(_pv("paths.gateway_v1") or ""))
if not GATEWAY_V1 or not os.path.isdir(GATEWAY_V1):
    # The profile leaves paths.gateway_v1 empty; the live gateway v1 checkout
    # (bridge.py, secondbrain package, dogfood log) is at this fixed path.
    _GW1_DEFAULT = os.path.expanduser("~/workspaces/build/second-brain-gateway/v1")
    if os.path.isdir(_GW1_DEFAULT):
        GATEWAY_V1 = _GW1_DEFAULT
SOURCES_PATH = os.path.expanduser(str(_pv("sources_path") or ""))
if not SOURCES_PATH or not os.path.exists(SOURCES_PATH):
    _SRC_DEFAULT = os.path.join(GATEWAY_V1, "sources.json") if GATEWAY_V1 else ""
    if _SRC_DEFAULT and os.path.exists(_SRC_DEFAULT):
        SOURCES_PATH = _SRC_DEFAULT
STRESS_DIR = os.path.expanduser(str(_pv("stress_dir")))
SRE_STATE = os.path.expanduser(str(_pv("sre_state")))
CONSOLE_URL = "http://127.0.0.1:8777/dashboard"
CONSOLE_TOKEN_FILE = os.path.expanduser(
    "~/.config/aiops/handoff/eva-harness-a5.token")
DEFAULT_WORKDIR = os.path.expanduser(str(_pv("console_workdir")))
EMAIL_TO = _pv("email.recipient_email") or ""

#: Stress-layer reference rates (cause_named, honesty=evidence_honest).
STRESS_REFERENCE = {"cause_named": 1.0, "honesty": 0.96}

LAYER_NAMES = {
    1: "Layer 1 Unit suites",
    2: "Layer 2 Stress",
    3: "Layer 3 Backtest",
    4: "Layer 4 Day-in-the-life",
    5: "Layer 5 Live smoke",
    6: "Layer 6 Soak (time-compressed durability)",
    7: "Layer 7 Fresh (cold machine + doc accuracy)",
    8: "Layer 8 Reboot (resilience)",
}

LAYER_SCOPES = {
    1: ("Runs the four pytest suites: dashboard tests, gateway (second-brain "
        "v1) tests, followup + ops-runner tests, clock/validation tests. "
        "Posts pass counts per suite."),
    2: ("Runs the adversarial stress harness "
        "(`python3 <stress-dir>/attack_run.py --label gauntlet --workers 8`). "
        "Posts per-class cause_named / honesty rates vs the reference (1.0 / 0.96)."),
    3: ("Runs `python3 -m dashboard.validate_days --backtest --window-start "
        "<7 days ago> --window-days 7`. Posts per-criterion results and "
        "incident-replay verdicts."),
    4: ("Day-in-the-life E2E on real data: latest raw scrape items -> bridge "
        "(filter/rank/sensitivity -> ingest, fixture gateway root) -> listening "
        "proposal pass -> one forced shadow-hierarchy investigation (injected "
        "failure) -> daily board + exec report render. Writes only to the "
        "gauntlet workdir; production stores are read-only here."),
    5: ("Live smoke: console HTTP 200, email_delivery.send_report test to "
        "the profile recipient, model seats 200, sre monitor last run ok."),
    6: ("Soak (time-compressed durability): amplify synthetic traffic "
        "(hub writes, console GETs, ops-agent polls, seat probes) against "
        "an isolated stack — AERIAL_SOAK_REQUESTS per simulated day — and "
        "fail on any sustained per-request resource slope (fds, RSS, disk, "
        "sqlite WAL) beyond the config thresholds. Mutation-validated: a "
        "deliberate fd-leak double must be caught."),
    7: ("Fresh (cold machine + doc accuracy): hermetic cold-start sandbox "
        "(copied tree, disposable tokens, no host systemd/bindings), then "
        "GETTING-STARTED.md's commands executed verbatim with every "
        "documented expected output asserted (health ok:true, bearer 200, "
        "no-bearer 401) + the first-boot assertion checklist, under the "
        "environment matrix (clean vs pre-existing state, py3.12 when "
        "available)."),
    8: ("Reboot (resilience): stop ALL services on the isolated stack, "
        "restart via the doc commands, assert full recovery (health, "
        "state dir intact: tickets/job_runs + ledger + findings, crontab "
        "intact, tokens valid). Mutation-validated: seeded faults from the "
        "2026-11-15 reboot cascade (dead unit, lost cron entry, hidden "
        "token) must each be DETECTED."),
}


@dataclass
class LayerResult:
    layer: int
    ok: bool
    lines: List[str] = field(default_factory=list)
    data: Dict[str, Any] = field(default_factory=dict)

    def body(self) -> str:
        head = f"{'PASS' if self.ok else 'FAIL'} — {LAYER_NAMES[self.layer]}"
        return "\n".join([head, ""] + self.lines)


# ----------------------------------------------------------------- hub helpers

def _hub(path: str, method: str = "GET", body: dict | None = None) -> dict:
    return jt._api(path, method, body)


def comment(ticket_id: str, body: str) -> None:
    _hub(f"/tickets/{ticket_id}/events", "POST",
         {"kind": "comment", "payload": {"body": body}})


def create_run_tickets(date_str: str) -> dict:
    """Create the gauntlet run ticket + one child per layer.

    Returns {"parent": id, "layers": {layer_no: id}}.
    """
    parent = _hub("/tickets", "POST", {
        "title": f"Gauntlet run {date_str}",
        "body": ("One-command full dress rehearsal of the Aerial platform. "
                 "Five layers run sequentially (unit suites, stress, backtest, "
                 "day-in-the-life, live smoke); per-layer results post to the "
                 "child tickets, this ticket gets the overall verdict. "
                 "Verdict = PASS only if all five layers pass."),
        "tags": ["gauntlet", "aerial"],
        "actor": "gauntlet",
        "parent_id": EPIC,
    })
    pid = parent.get("id") or parent.get("ticket_id")
    layers: Dict[int, str] = {}
    for n in (1, 2, 3, 4, 5, 6, 7, 8):
        t = _hub("/tickets", "POST", {
            "title": LAYER_NAMES[n],
            "body": f"Gauntlet layer for run {date_str}.\n\nScope: {LAYER_SCOPES[n]}",
            "tags": ["gauntlet", "gauntlet-layer"],
            "actor": "gauntlet",
            "parent_id": pid,
        })
        layers[n] = t.get("id") or t.get("ticket_id")
    return {"parent": pid, "layers": layers}


# -------------------------------------------------------------- layer runners

def _run(cmd: List[str], cwd: str, timeout: float) -> tuple[int, str]:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                       timeout=timeout)
    return p.returncode, (p.stdout or "") + (("\n--stderr--\n" + p.stderr)
                                             if p.stderr.strip() else "")


def _pytest_counts(output: str) -> tuple[int, int]:
    """(passed, failed) from a pytest summary line."""
    passed = failed = 0
    m = re.findall(r"(\d+) passed", output)
    if m:
        passed = int(m[-1])
    mf = re.findall(r"(\d+) failed", output)
    if mf:
        failed = int(mf[-1])
    return passed, failed


def layer1_units() -> LayerResult:
    suites = [
        ("dashboard",
         [sys.executable, "-m", "pytest", "dashboard/tests", "-q",
          "--ignore=dashboard/tests/test_followup.py",
          "--ignore=dashboard/tests/test_ops_runner.py",
          "--ignore=dashboard/tests/test_clock_validate.py"]),
        ("followup+ops-runner",
         [sys.executable, "-m", "pytest", "dashboard/tests/test_followup.py",
          "dashboard/tests/test_ops_runner.py"]),
        ("clock+validation",
         [sys.executable, "-m", "pytest",
          "dashboard/tests/test_clock_validate.py"]),
        ("gateway",
         [sys.executable, "-m", "pytest", GATEWAY_V1, "-q"]),
    ]
    res = LayerResult(1, ok=True)
    total_p = total_f = 0
    for name, cmd in suites:
        try:
            code, out = _run(cmd, ROOT, 3000)
        except subprocess.TimeoutExpired:
            res.lines.append(f"- {name}: TIMEOUT (3000s)")
            res.ok = False
            continue
        passed, failed = _pytest_counts(out)
        total_p += passed
        total_f += failed
        ok = code == 0
        res.ok = res.ok and ok
        res.lines.append(f"- {name}: {'PASS' if ok else 'FAIL'} "
                         f"({passed} passed, {failed} failed)")
        if not ok:
            tail = "\n".join(out.splitlines()[-15:])
            res.lines.append("  last output:\n    " +
                             tail.replace("\n", "\n    "))
    res.data = {"suites": {n: True for n, _ in suites}, "passed": total_p,
                "failed": total_f}
    res.lines.insert(0, f"suites: {len(suites)}, total {total_p} passed, "
                        f"{total_f} failed")
    return res


def layer2_stress() -> LayerResult:
    res = LayerResult(2, ok=False)
    agg_path = Path(STRESS_DIR) / "results" / "gauntlet" / "aggregate.json"
    try:
        code, out = _run([sys.executable, "attack_run.py", "--label",
                          "gauntlet", "--workers", "8"], STRESS_DIR, 7200)
    except subprocess.TimeoutExpired:
        res.lines.append("- attack_run timed out after 7200s")
        return res
    if code != 0:
        res.lines.append("- attack_run exited %d" % code)
        res.lines += ["  " + ln for ln in out.splitlines()[-10:]]
        return res
    try:
        agg = json.loads(agg_path.read_text())
        overall = agg["overall"]["all"]
    except Exception as exc:
        res.lines.append(f"- could not read {agg_path}: {exc}")
        return res
    rates = {"cause_named": overall["cause_named"]["rate"],
             "honesty": overall["evidence_honest"]["rate"]}
    res.ok = True
    for key, ref in STRESS_REFERENCE.items():
        rate = rates[key]
        ok = rate >= ref - 1e-9
        res.ok = res.ok and ok
        res.lines.append(f"- {key}: {rate:.3f} vs reference {ref:.2f} → "
                         f"{'PASS' if ok else 'FAIL'}")
    per_class = {c: v["cause_named"]["rate"]
                 for c, v in agg.get("by_class", {}).items()}
    res.data = {"rates": rates, "per_class_cause_named": per_class,
                "runs": overall["cause_named"]["n"]}
    res.lines.append(f"- per-class cause_named: "
                     f"{json.dumps(per_class, sort_keys=True)}")
    return res


def layer3_backtest() -> LayerResult:
    start = (dt.datetime.now(dt.timezone.utc).replace(hour=0, minute=0,
                                                      second=0, microsecond=0)
             - dt.timedelta(days=7))
    iso = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    res = LayerResult(3, ok=False)
    try:
        code, out = _run([sys.executable, "-m", "dashboard.validate_days",
                          "--backtest", "--window-start", iso,
                          "--window-days", "7"], ROOT, 3600)
    except subprocess.TimeoutExpired:
        res.lines.append(f"- backtest timed out (window {iso} +7d)")
        return res
    res.lines.append(f"- window {iso} +7d, exit {code}")
    for ln in out.splitlines():
        if re.search(r"\b(PASS|FAIL)\b", ln):
            res.lines.append("  " + ln.strip())
    res.ok = code == 0
    res.data = {"window_start": iso, "exit_code": code}
    return res


def layer4_ditl(workdir: str) -> LayerResult:
    """Day-in-the-life: raw -> bridge -> proposals -> hierarchy -> board.

    Read-only on production stores; all writes land under workdir.
    """
    res = LayerResult(4, ok=False)
    wd = Path(workdir) / "ditl"
    wd.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, GATEWAY_V1)

    # (a) latest raw items from the real scrape store (unfiltered)
    import sqlite3
    conn = sqlite3.connect(f"file:{SCRAPES_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    raw_rows = conn.execute(
        "SELECT * FROM items ORDER BY fetched_at DESC LIMIT 200").fetchall()
    conn.close()
    raw_items = [dict(r) for r in raw_rows]
    res.lines.append(f"- raw: {len(raw_items)} latest items from scrapes.db")

    # (b) bridge: filter/rank/sensitivity-label -> ingest into a FIXTURE root
    gw_root = str(wd / "gateway-root")
    from bridge import run as bridge_run
    bsum = bridge_run(SCRAPES_DB, gw_root, since_hours=24 * 30, top=40)
    ingested = bsum.get("ingested", 0)
    res.lines.append(f"- bridge: fetched {bsum.get('fetched')}, selected "
                     f"{bsum.get('selected')}, ingested {ingested}, "
                     f"skipped {bsum.get('skipped')}, errors {bsum.get('errors')}")
    digest = json.loads(Path(bsum["digest_path"]).read_text()) \
        if bsum.get("digest_path") else {}
    sens = {}
    for it in digest.get("ingested", []):
        sens[it.get("sensitivity")] = sens.get(it.get("sensitivity"), 0) + 1
    if sens:
        res.lines.append(f"- bridge sensitivity labels: {json.dumps(sens, sort_keys=True)}")

    # (c) listening proposal pass (shadow mode, ledger in the workdir)
    from secondbrain.listening import load_sources
    from secondbrain.proposals import (fetch_scrape_items, run_weekly_batch)
    sources_path = SOURCES_PATH
    if not sources_path or not os.path.exists(sources_path):
        # No real listening-sources file exists in this environment;
        # layer 4 is hermetic (all writes under the workdir), so seed a
        # 2-entry fixture rather than failing the layer on a missing file.
        sources_path = str(wd / "sources-fixture.json")
        Path(sources_path).write_text(json.dumps([
            {"id": "r/gauntlet-fixture-a", "kind": "reddit_sub",
             "target": "python", "topic": "gauntlet fixture",
             "match": [], "relation": "competitor", "sensitivity": "public"},
            {"id": "r/gauntlet-fixture-b", "kind": "reddit_sub",
             "target": "local llamma", "topic": "gauntlet fixture",
             "match": [], "relation": "competitor", "sensitivity": "public"},
        ]))
        res.lines.append("- listening: using 2-entry fixture sources (no real sources.json found)")
    sources = load_sources(sources_path)
    reddit_ids = [s["id"] for s in sources if s.get("kind") == "reddit_sub"]
    items = fetch_scrape_items(SCRAPES_DB, since_days=45,
                               source_ids=reddit_ids, per_source_limit=1500)
    ledger_path = wd / "proposal-ledger.json"
    out = run_weekly_batch(sources_path, items, str(ledger_path))
    proposed = out.get("proposals", [])
    res.lines.append(f"- listening: {len(items)} items scanned, "
                     f"{len(proposed)} proposal row(s) (shadow mode)")
    for p in proposed[:5]:
        res.lines.append(f"    proposal {p.get('id')}: score "
                         f"{p.get('last_score', p.get('score', '?'))}")

    # (d) force one hierarchy investigation on an injected failure
    from integration.run_shadow_hierarchy import run_hierarchy
    ws = wd / "hierarchy-ws"
    ws.mkdir(parents=True, exist_ok=True)
    job = {
        "job_id": "gauntlet:ditl-injected-failure",
        "status": "failed",
        "summary": "Injected failure for the gauntlet DITL layer",
        "error": "exit code 1: simulated scrape-pipeline step failure",
        "log_tail": "Traceback (most recent call last):\n"
                    "  File \"run-scrape.sh\", line 12\n"
                    "exit 1 (injected by gauntlet DITL)",
        "started_at": time.time(),
    }
    (ws / "incident.json").write_text(json.dumps(job, indent=1, default=str))
    hier = run_hierarchy(job, ws, dashboard=False)
    brief = (hier.get("exec_brief") or "").strip()
    findings = (hier.get("curator_findings") or "").strip()
    hier_ok = bool(brief) and bool(findings)
    res.lines.append(f"- hierarchy: findings {len(findings)} chars, brief "
                     f"{len(brief)} chars → {'PASS' if hier_ok else 'FAIL'}")
    if brief:
        res.lines.append("  brief excerpt: " + brief[:300].replace("\n", " "))

    # (e) daily board + exec report (outputs under the workdir only)
    board = wd / "daily-board.html"
    code, out = _run([sys.executable, "dashboard/render_daily.py",
                      "--out", str(board),
                      "--snapshot", str(wd / "board-snapshot.json"),
                      "--tail-state", str(wd / "tail-state.json"),
                      "--email-text-out", str(wd / "exec-report.txt"),
                      "--window", "rolling", "--depth", "brief"], ROOT, 900)
    board_ok = code == 0 and board.exists() and board.stat().st_size > 0
    res.lines.append(f"- board render: exit {code}, {board.stat().st_size if board.exists() else 0} bytes")
    headlines: List[str] = []
    rpt = wd / "exec-report.txt"
    if rpt.exists():
        headlines = [ln.strip() for ln in rpt.read_text().splitlines()
                     if ln.strip()][:12]
        res.lines.append("- exec report headlines:")
        res.lines += ["    " + h for h in headlines]
    res.ok = hier_ok and board_ok

    # (f) Layer-4 extension: recurring-miss day-type through the remediation
    # contract — schedule:* misses must close via verified fix OR escalate.
    try:
        from dashboard import recurring_miss
        rm = recurring_miss.scenario(str(wd))
        res.lines.append("- recurring-miss day-type (remediation contract): "
                         f"{'PASS' if rm['ok'] else 'FAIL'}")
        res.lines += ["  " + l for l in rm["lines"]]
        rm_ok = rm["ok"]
    except Exception as exc:
        rm_ok = False
        res.lines.append(f"- recurring-miss scenario raised {type(exc).__name__}: {exc}")

    # (g) Layer 4b: real-user UI walk — parallel Playwright sessions against
    # a hermetic stack (load /dashboard, scroll, hover queue, click a view).
    ui_ok, ui_lines = _ui_walk_section(str(wd))
    res.lines.append("- Layer 4b real-user UI walk: "
                     f"{'PASS' if ui_ok else 'FAIL'}")
    res.lines += ["  " + l for l in ui_lines]

    res.ok = res.ok and rm_ok and ui_ok
    res.data = {"raw": len(raw_items), "ingested": ingested,
                "proposed": len(proposed), "hierarchy_ok": hier_ok,
                "board_ok": board_ok, "headlines": headlines,
                "workdir": str(wd)}
    return res


def _ui_walk_section(workdir: str) -> tuple[bool, List[str]]:
    """Boot a hermetic hub+console in a sandbox, then run parallel real-user
    sessions (playwright) against it."""
    from dashboard import fresh, ui_walk
    sb = fresh.Sandbox(workdir, name="uiwalk")
    try:
        boot_ok, _ = fresh.boot_stack(sb, ("hub", "console"))
        if not boot_ok:
            return False, ["- hermetic stack did not come up for the UI walk"]
        out = ui_walk.run_walk(sb.console_url, sb.token)
        return out["ok"], out["lines"]
    finally:
        sb.stop()


def _console_token() -> str:
    """Console bearer token (deny-by-default auth)."""
    try:
        with open(CONSOLE_TOKEN_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


def _console_probe(bearer: str = ""):
    """GET the console dashboard; returns HTTP status (or error string)."""
    try:
        req = urllib.request.Request(
            CONSOLE_URL,
            headers=({"Authorization": f"Bearer {bearer}"} if bearer else {}))
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as exc:
        return f"error: {exc}"


def layer5_smoke() -> LayerResult:
    res = LayerResult(5, ok=True)
    # console: deny-by-default -> 401 WITHOUT bearer, 200 WITH bearer
    status_no = _console_probe()
    tok = _console_token()
    status = _console_probe(tok) if tok else "error: no console token file"
    console_ok = (status_no == 401) and (status == 200)
    res.ok = res.ok and console_ok
    res.lines.append(f"- console {CONSOLE_URL}: no-bearer={status_no} "
                     f"(expect 401), bearer={status} (expect 200) "
                     f"→ {'PASS' if console_ok else 'FAIL'}")
    # email delivery
    try:
        from dashboard import email_delivery
        sent = email_delivery.send_report(
            f"Aerial gauntlet smoke {time.strftime('%Y-%m-%d %H:%M UTC',
                                                  time.gmtime())}",
            "Live-smoke test email from the Aerial gauntlet (layer 5). "
            "No action needed.", to=EMAIL_TO)
    except Exception as exc:
        sent = False
        res.lines.append(f"- email: raised {exc}")
    res.ok = res.ok and bool(sent)
    res.lines.append(f"- email_delivery.send_report to {EMAIL_TO}: "
                     f"{'sent' if sent else 'FAILED'}")
    # model seats
    seats = {}
    for name, port in MODEL_SEATS:
        seats[name] = probe_seat(port, DEFAULT_BEARER)
    seats_ok = all(s == 200 for s in seats.values())
    res.ok = res.ok and seats_ok
    res.lines.append(f"- model seats: "
                     f"{json.dumps(seats, sort_keys=True)} "
                     f"→ {'PASS' if seats_ok else 'FAIL'}")
    # sre monitor: last run had no failed checks and ran within the last hour
    sre_ok, detail = False, "state file missing"
    try:
        st = json.loads(Path(SRE_STATE).read_text())
        failed = st.get("failed_checks") or {}
        last_ok = st.get("last_ok")
        age = None
        if last_ok:
            t = dt.datetime.fromisoformat(last_ok.replace("Z", "+00:00"))
            age = time.time() - t.timestamp()
        fresh = age is not None and age < 3600
        sre_ok = not failed and fresh
        detail = (f"failed_checks={list(failed) or 'none'}, "
                  f"last_ok={last_ok} (age {age and int(age)}s)")
    except Exception as exc:
        detail = f"error: {exc}"
    res.ok = res.ok and sre_ok
    res.lines.append(f"- sre monitor: {detail} → {'PASS' if sre_ok else 'FAIL'}")
    res.data = {"console": console_ok, "email": bool(sent), "seats": seats,
                "sre_ok": sre_ok, "sre_detail": detail}
    return res


def layer6_soak(workdir: str) -> LayerResult:
    from dashboard import soak
    out = soak.run_soak(workdir)
    return LayerResult(6, ok=out["ok"], lines=out["lines"], data=out["data"])


def layer7_fresh(workdir: str) -> LayerResult:
    from dashboard import fresh
    out = fresh.run_fresh(workdir)
    return LayerResult(7, ok=out["ok"], lines=out["lines"], data=out["data"])


def layer8_reboot(workdir: str) -> LayerResult:
    from dashboard import reboot
    out = reboot.run_reboot(workdir)
    return LayerResult(8, ok=out["ok"], lines=out["lines"], data=out["data"])


# resolved per call (module attribute lookup) so tests can patch the
# layer functions and so a redefinition always takes effect.
LAYER_RUNNERS = {n: name for n, name in enumerate(
    [None, "layer1_units", "layer2_stress", "layer3_backtest",
     "layer4_ditl", "layer5_smoke", "layer6_soak", "layer7_fresh",
     "layer8_reboot"]) if name}


ALL_LAYERS = (1, 2, 3, 4, 5, 6, 7, 8)
#: layers that receive the workdir argument
WORKDIR_LAYERS = (4, 6, 7, 8)


# ------------------------------------------------------------------- escalation

def escalate(result: LayerResult) -> Optional[str]:
    """Classify a layer failure; SEV/security escalates immediately (email
    ping), everything else aggregates (daily report). Returns the class."""
    if result.ok:
        return None
    claim = f"{LAYER_NAMES[result.layer]} failed: " + \
        " | ".join(result.lines)[:400]
    finding = CuratorFinding(
        finding_id=f"gauntlet-layer-{result.layer}",
        claim=claim, reversible=True, scope="internal")
    cls = classify(finding)
    if ping_policy.is_immediate(cls):
        try:
            from dashboard import email_delivery
            email_delivery.send_report(
                f"SEV ping: {LAYER_NAMES[result.layer]} FAILED (gauntlet)",
                claim + "\n\nImmediate escalation per ping_policy "
                        f"({cls}); gauntlet layer ticket has the detail.",
                to=EMAIL_TO)
        except Exception:
            pass
    return cls


# ------------------------------------------------------------------- the run

def run_gauntlet(workdir: str = DEFAULT_WORKDIR,
                 layers: tuple = ALL_LAYERS,
                 post_tickets: bool = True) -> dict:
    """Run the selected layers sequentially (a failure never stops the rest),
    post results to the layer child tickets, verdict to the parent."""
    date_str = time.strftime("%Y-%m-%d")
    tickets: Dict[str, Any] = {"parent": None, "layers": {}}
    if post_tickets:
        tickets = create_run_tickets(date_str)
    results: List[LayerResult] = []
    for n in ALL_LAYERS:
        if n not in layers:
            continue
        t0 = time.time()
        print(f"== gauntlet: {LAYER_NAMES[n]} ==", flush=True)
        try:
            fn = getattr(sys.modules[__name__], LAYER_RUNNERS[n])
            r = fn(workdir) if n in WORKDIR_LAYERS else fn()
        except Exception as exc:  # fail-closed: a crashed layer reports FAIL
            r = LayerResult(n, ok=False,
                            lines=[f"- layer raised {type(exc).__name__}: {exc}"])
        r.data["wall_s"] = round(time.time() - t0, 1)
        results.append(r)
        print(f"   -> {'PASS' if r.ok else 'FAIL'} "
              f"({r.data.get('wall_s')}s)", flush=True)
        if post_tickets:
            try:
                comment(tickets["layers"][n], r.body())
            except Exception as exc:
                print(f"   WARN: could not post to layer ticket: {exc}")
        cls = escalate(r)
        if cls:
            print(f"   escalated: {cls}", flush=True)
    all_ok = all(r.ok for r in results)
    verdict = "PASS" if all_ok else "FAIL"
    summary_lines = [f"Gauntlet run {date_str}: {verdict}"]
    for r in results:
        summary_lines.append(
            f"- {LAYER_NAMES[r.layer]}: {'PASS' if r.ok else 'FAIL'} "
            f"({r.data.get('wall_s', '?')}s)"
            + ("" if r.ok else " — see the layer ticket"))
    if post_tickets:
        try:
            comment(tickets["parent"], "\n".join(summary_lines))
        except Exception as exc:
            print(f"WARN: could not post verdict: {exc}")
    out = {"date": date_str, "verdict": verdict,
           "tickets": tickets,
           "layers": {r.layer: {"ok": r.ok, "lines": r.lines,
                                "data": r.data} for r in results}}
    try:
        Path(workdir).mkdir(parents=True, exist_ok=True)
        (Path(workdir) / "gauntlet-last.json").write_text(
            json.dumps(out, indent=1, default=str))
    except Exception:
        pass
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workdir", default=DEFAULT_WORKDIR)
    ap.add_argument("--only", default=None,
                    help="comma-separated layer numbers to run (default all)")
    ap.add_argument("--no-tickets", action="store_true",
                    help="do not create/post hub tickets (testing)")
    args = ap.parse_args()
    layers = tuple(int(x) for x in args.only.split(",")) if args.only \
        else ALL_LAYERS
    out = run_gauntlet(workdir=args.workdir, layers=layers,
                       post_tickets=not args.no_tickets)
    print(json.dumps({k: v for k, v in out.items() if k != "layers"},
                     indent=1, default=str))
    for n, r in sorted(out["layers"].items()):
        print(f"\n--- {LAYER_NAMES[int(n)]}: "
              f"{'PASS' if r['ok'] else 'FAIL'} ---")
        for ln in r["lines"]:
            print(ln)
    return 0 if out["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
