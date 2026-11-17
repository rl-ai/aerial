"""Always-on ops agent runner — poll → classify → act → verify (decision: continuous loop).

Reuses the Lane F followup package (classifier R1–R6, flow state machine,
ping policy D5) and executes it continuously:

  a. POLL   failed job_runs from the hub DB since the last checkpoint,
            health-status.json anomalies, and model-seat liveness
            (kimi 18102, deepseek 18095, glm 18110 — GET /v1/models with the
            cluster bearer). Every dead seat is a finding.
  b. CLASSIFY each finding via followup.classifier (R1–R6).
  c. ACT    sev-security / sev-incident → immediate ping (ping_policy) AND a
            mandate-scoped fix session; auto-fix/mandate → scoped+expiring
            mandate then a Hermes one-shot fix session in a per-incident
            workspace (<ops-workspace>/<finding-id>/, TASK.md with finding +
            evidence + playbook + close-out.md instruction); mandate /
            daily-report / unknown → recorded for the daily aggregate only
            (unknown fails closed — never auto-fixed).
  d. VERIFY auto-fix runs re-check the exact symptom (seat re-probe, job
            re-gather, health re-read); verified → closed SILENT, failed →
            escalated to the daily aggregate.
  e. DEDUPE a finding_id never auto-fixes twice in 24 h — a recurring failure
            escalates to the daily aggregate instead (the sre-monitor cascade
            lesson).

Governance: SEV/security pings only per ping_policy; every fix session runs
under a scoped+expiring mandate; fail-closed on unknown classes; the daemon
never edits product code — it dispatches agent sessions that do.

All external effects (probe, fix execution, mandate creation, notification)
are injectable so the loop is fully hermetic under test.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

try:  # Lane F imports (package-relative)
    from . import classifier as clf
    from . import remediation as remd
    from .classifier import CuratorFinding
    from .flow import FollowupFlow
    from .. import clock
except ImportError:  # running as a top-level module / script
    import classifier as clf                     # type: ignore
    try:
        import remediation as remd               # type: ignore
    except ImportError:
        remd = None                              # type: ignore
    from classifier import CuratorFinding        # type: ignore
    from flow import FollowupFlow                # type: ignore
    import sys as _sys
    _sys.path.insert(0, os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    import dashboard.clock as clock              # type: ignore

try:
    from dashboard.exec_summary import gather_job_runs
except ImportError:
    import sys as _sys
    _sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from exec_summary import gather_job_runs     # type: ignore

log = logging.getLogger("ops-agent")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
try:
    from deployment_profile import value as _pv
except ImportError:  # standalone: repo root already on path via fallbacks
    def _pv(dotted_key: str, default=None):  # type: ignore[misc]
        return default

NODE_NAME = str(_pv("cluster.node_name") or "aerial")


def _job_run_script() -> str:
    """Path to bin/job_run.py: profile override, else repo-relative."""
    raw = str(_pv("paths.job_run_script") or "")
    if raw:
        return os.path.expanduser(raw)
    return os.path.join(_ROOT, "bin", "job_run.py")

DEFAULT_HUB_DB = str(_pv("hub_db"))
DEFAULT_HEALTH_STATUS = str(_pv("health_status"))
DEFAULT_BEARER = str(_pv("cluster_bearer"))
RUN_DIR = str(_pv("run_dir"))
STATE_PATH = os.path.join(RUN_DIR, "ops-agent-state.json")
DEFAULT_FOLLOWUPS_DB = os.path.join(RUN_DIR, "ops-agent-followups.db")
LOG_PATH = str(_pv("ops_log"))
WORKSPACE_ROOT = str(_pv("ops_workspace_root"))

#: Model seats: (name, port) or (name, port, recovery_script). A seat that
#: does not answer GET /v1/models is a finding — the 2026-11-11 host reboot
#: left Kimi (18102) dead for hours because nothing monitored model seats.
#:
#: The optional third element names a node-side RECOVERY DRIVER: the
#: auto-fix path executes [script] as the deterministic fix (guarded by
#: os.path.exists + executable + the f8 trust check) instead of dispatching
#: a generic fix session. The glm seat can be SERVED ON A REMOTE HOST and
#: is recovered via cluster/bin/seat_glm_recover.sh (ssh forced-command
#: ladder + forwarder restarts) — NEVER by local systemctl (2026-11-16 GLM
#: 3-h outage; see docs/sre-runbook-glm-seat.md).
#:
#: f8: the driver path is HARD-RESOLVED from the deployment profile
#: (cluster.seat_recovery_script, default cluster/bin/seat_glm_recover.sh
#: resolved relative to the repo root) — never from vram_registry.json.


def _seat_recovery_script() -> str:
    """Trusted recovery-driver path (f8): profile key
    cluster.seat_recovery_script, default cluster/bin/seat_glm_recover.sh,
    resolved relative to the repo root. Registry-provided paths are NEVER
    used — whoever can write vram_registry.json must not gain execution."""
    rel = str(_pv("cluster.seat_recovery_script")
              or "cluster/bin/seat_glm_recover.sh")
    return os.path.normpath(os.path.join(_ROOT, rel))


_SEAT_RECOVERY_SCRIPT = _seat_recovery_script()

MODEL_SEATS = (
    ("kimi", 18102),
    ("deepseek", 18095),
    ("glm", 18110, _SEAT_RECOVERY_SCRIPT),
)

#: Fallback recovery drivers by seat name — applies when a MODEL_SEATS entry
#: is a legacy 2-tuple (e.g. derived from a vram registry that predates the
#: recovery_script field). Values are always the profile-resolved trusted
#: path; seat_recovery_script() rejects anything else (f8).
SEAT_RECOVERY_SCRIPTS = {
    "glm": _SEAT_RECOVERY_SCRIPT,
}


def seat_recovery_script(seat) -> Optional[str]:
    """Recovery driver for one MODEL_SEATS entry (2- or 3-tuple), or None.

    f8 trust gate: ONLY the profile-resolved trusted driver path
    (_SEAT_RECOVERY_SCRIPT) may ever be returned — a registry-supplied or
    hand-set 3rd tuple element pointing anywhere else is rejected (logged,
    returns None → classic playbook). Only a script that exists and is
    executable counts as a driver (fail-safe: no driver → classic playbook).
    """
    script = str(seat[2]) if len(seat) > 2 and seat[2] else \
        SEAT_RECOVERY_SCRIPTS.get(seat[0])
    if not script:
        return None
    if os.path.realpath(script) != os.path.realpath(_SEAT_RECOVERY_SCRIPT):
        log.warning(
            "rejected recovery path %r — drivers are profile-resolved "
            "(cluster.seat_recovery_script), never registry-supplied (f8)",
            script)
        return None
    if os.path.isfile(script) and os.access(script, os.X_OK):
        return script
    return None


try:  # derive seat names/ports from the vram registry — 2-tuples ONLY (f8)
    import json as _json
    _reg_path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "vram_registry.json")
    with open(_reg_path, encoding="utf-8") as _fh:
        _reg = _json.load(_fh)
    if _reg.get("seats"):
        _rejected = [s["recovery_script"] for s in _reg["seats"]
                     if s.get("recovery_script")]
        MODEL_SEATS = tuple(
            (s["name"], int(s["port"]))
            for s in _reg["seats"] if "port" in s)
        if _rejected:
            log.warning(
                "vram_registry recovery_script values REJECTED (f8: "
                "recovery drivers come from the deployment profile, never "
                "from the registry): %r", _rejected)
except (OSError, ValueError, KeyError):
    pass  # registry absent/malformed -> keep the fallback tuple


def systemctl_user_available() -> bool:
    """f9: verify `systemctl --user` actually works in THIS execution
    context (XDG_RUNTIME_DIR set + the user session reachable). A runner
    started from cron / a system service / a detached worker has no user
    session, and `systemctl --user` would fail there. Never raises; the
    result is logged and the caller skips gracefully."""
    if not os.environ.get("XDG_RUNTIME_DIR"):
        return False
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "is-system-running"],
            timeout=10, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True)
    except Exception:
        return False
    if proc.returncode == 0:
        return True
    # "degraded"/"maintenance" (rc 1) still means the user session is
    # reachable; "offline"/"stopping"/"unknown" do not.
    return proc.returncode == 1 and proc.stdout.strip() in \
        ("degraded", "maintenance")

#: Poll window default (seconds) when no checkpoint exists.
DEFAULT_WINDOW_S = 15 * 60.0
DEDUPE_WINDOW_S = 24 * 3600.0
DEFAULT_INTERVAL_S = 300.0


def run_idle_broker(script: Optional[str] = None, timeout_s: float = 120.0) -> str:
    """Invoke the idle-work broker in --once mode as a subprocess.

    The broker lives at dashboard/idle_broker/broker.py (next to this
    package). It is a leaf of the poll cycle: failures here are logged and
    swallowed so they can never break ops polling, and the broker itself
    enforces the production pause gates (any seat waiting>0,
    AERIAL_SATURATION_PAUSE env, per-entry pidfiles) before dispatching.
    Returns a short human summary of the pass.
    """
    if script is None:
        script = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "idle_broker", "broker.py")
    if not os.path.exists(script):
        return "skipped (broker script not found)"
    proc = subprocess.run(
        [sys.executable, script, "--once"],
        capture_output=True, text=True, timeout=timeout_s)
    out = (proc.stdout or "").strip().splitlines()
    dispatched = [l for l in out if l.startswith("{") and '"dispatch"' in l]
    if proc.returncode != 0:
        return f"exit {proc.returncode}: {(proc.stderr or '')[:200]}"
    return f"{len(dispatched)} dispatched, {len(out)} decision(s)"

HERMES_BIN = os.path.expanduser(
    str(_pv("cluster.hermes_bin") or "~/.local/bin/hermes"))

#: Fix playbook embedded in every seat-liveness TASK.md: a dead seat is
#: restarted per the SRE runbook (systemd user service), never hand-edited.
SEAT_PLAYBOOK = """
## Fix playbook (model seat down)
1. Identify the seat's systemd user service: `systemctl --user list-units '<node>-*'`
   (the seat ports map to services named <node>-<model>-*.service).
2. Preserve evidence first: `journalctl --user -u <svc> --no-pager -n 50`
   saved into this workspace as journal.txt. Never delete evidence.
3. Restart the dead seat: `systemctl --user reset-failed <svc>;
   systemctl --user start <svc>` (see the SRE runbook).
4. Wait up to 60s, then confirm the seat answers
   `GET http://127.0.0.1:<port>/v1/models` with the cluster bearer.
5. If the restart fails twice, STOP: write what you tried to close-out.md and
   leave the seat for a human — do not loop-restart.
6. Remote seats (on the seat host) are recovered via cluster/bin/seat_glm_recover.sh,
   never by local systemctl.
"""


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------

@dataclass
class OpsFinding(CuratorFinding):
    """A live finding from the poll phase. `check` describes the symptom so
    verification re-runs exactly the check that failed; `verify` is the
    injected re-check callable."""
    proposed_action: str = ""
    evidence: Dict[str, Any] = field(default_factory=dict)
    check: Dict[str, Any] = field(default_factory=dict)
    verify: Callable[[], bool] = field(default=lambda: True)
    playbook: str = ""
    #: max auto-fixes within the 24h dedupe window (schedule findings may
    #: re-trigger up to 2 times before recurring-miss escalation).
    max_fixes: int = 1

    def task_text(self, mandate: Optional[dict] = None) -> str:
        parts = [
            "# Ops fix task\n",
            f"Finding: {self.finding_id}\n",
            f"Symptom: {self.claim}\n",
            f"Proposed action: {self.proposed_action or 'diagnose and fix'}\n",
            f"Evidence: {json.dumps(self.evidence, default=str)}\n",
        ]
        if mandate:
            parts.append(f"Mandate scope (do NOT act outside this scope): "
                         f"{mandate.get('scope')} (mandate {mandate.get('mandate_id')}, "
                         f"expires {mandate.get('expiry_at')})\n")
        if self.playbook:
            parts.append(self.playbook)
        parts.append(
            "\nDiagnose root cause and apply the safe fix per your mandate; "
            "verify the symptom is gone; write close-out.md in this workspace "
            "with what you found, what you changed, and the verification "
            "result.\n")
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Poll sources
# ---------------------------------------------------------------------------

def probe_seat(port: int, bearer_path: str = DEFAULT_BEARER,
               timeout: float = 5.0) -> Optional[int]:
    """GET /v1/models on a model seat → HTTP status, or None on error."""
    try:
        with open(bearer_path) as f:
            bearer = f.read().strip()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/models",
            headers={"Authorization": f"Bearer {bearer}"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return None


def seat_findings(clock: Callable[[], float], probe: Callable[..., Optional[int]],
                  bearer_path: str = DEFAULT_BEARER) -> List[OpsFinding]:
    """One finding per dead model seat (probe fails or non-200).

    A seat with a recovery driver (3-tuple MODEL_SEATS entry, or a name in
    SEAT_RECOVERY_SCRIPTS) gets the driver path in its proposed_action and in
    check['recovery_script'] — the auto-fix path then executes the driver
    instead of dispatching a generic fix session (remote seats on the seat host are
    recovered via cluster/bin/seat_glm_recover.sh, never by local systemctl).
    """
    day = time.strftime("%Y%m%d", time.gmtime(clock()))
    out = []
    for seat in MODEL_SEATS:
        name, port = seat[0], seat[1]
        script = seat_recovery_script(seat)
        status = probe(port, bearer_path)
        if status == 200:
            continue
        if script:
            action = (f"run the recovery driver {script} — remote seat on "
                      f"the seat host (ssh forced-command recovery + forwarder "
                      f"restart); verify GET /v1/models returns 200")
            check = {"kind": "seat", "name": name, "port": port,
                     "recovery_script": script}
        else:
            action = (f"restart the dead {name} model seat per the "
                      f"runbook; verify GET /v1/models returns 200")
            check = {"kind": "seat", "name": name, "port": port}
        out.append(OpsFinding(
            finding_id=f"seat:{name}:{day}",
            claim=f"model seat {name} (port {port}) is DOWN "
                  f"(probe status {status!r})",
            reversible=True, scope="internal", covered_by_mandate=False,
            tags=("seat-liveness", name),
            proposed_action=action,
            evidence={"port": port, "probe_status": status},
            check=check,
            playbook=SEAT_PLAYBOOK,
            verify=lambda p=port, b=bearer_path: probe(p, b) == 200))
    return out


def job_run_findings(db_path: str, window_start: float, window_end: float,
                     clock: Callable[[], float],
                     gather: Callable = gather_job_runs) -> List[OpsFinding]:
    """Findings from hub.db job_runs failures in the checkpoint window."""
    result = gather(db_path, (window_start, window_end))
    if not result.get("available"):
        return []
    day = time.strftime("%Y%m%d", time.gmtime(clock()))
    out = []
    for f in result.get("failures", []):
        job_id = f.get("job_id") or "unknown"
        out.append(OpsFinding(
            finding_id=f"job:{job_id}:{day}",
            claim=f"job run failed: {job_id} — {f.get('summary')}",
            reversible=True, scope="internal", covered_by_mandate=False,
            tags=("job-run", str(job_id)),
            proposed_action=f"diagnose why job {job_id} failed and apply the "
                            f"safe fix; re-run the job if safe",
            evidence={"job_id": job_id, "summary": f.get("summary"),
                      "window": [window_start, window_end]},
            check={"kind": "job", "job_id": job_id},
            verify=lambda j=job_id, db=db_path, g=gather, ws=window_end:
                not any(x.get("job_id") == j
                        for x in (g(db, (ws, ws + DEFAULT_WINDOW_S))
                                  .get("failures") or []))))
    return out


def health_findings(path: str = DEFAULT_HEALTH_STATUS,
                    clock: Callable[[], float] = clock.now) -> List[OpsFinding]:
    """Findings from health-status.json failed_checks (sre-health-monitor)."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    day = time.strftime("%Y%m%d", time.gmtime(clock()))
    out = []
    for check, info in (data.get("failed_checks") or {}).items():
        if not isinstance(info, dict) or info.get("ok") is not False:
            continue
        out.append(OpsFinding(
            finding_id=f"health:{check}:{day}",
            claim=f"health check {check} failing: "
                  f"{info.get('detail', '(no detail)')}",
            reversible=True, scope="internal", covered_by_mandate=False,
            tags=("health", check),
            proposed_action=f"diagnose health check {check} and apply the "
                            f"safe fix per the SRE runbook",
            evidence={"check": check, "detail": info.get("detail")},
            check={"kind": "health", "name": check, "path": path},
            playbook=("\n## Fix playbook (health check)\nSee "
                      "the SRE runbook. Container lifecycle "
                      "restarts are human-gated — do NOT restart containers; "
                      "document instead.\n"),
            verify=lambda c=check, p=path: not isinstance(
                (lambda d: d.get("failed_checks", {}).get(c))(_read_json(p)),
                dict)))
    return out


def _read_json(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------------------
# Schedule reconciliation (added per the operator): every job that was SUPPOSED to run
# actually ran. Parses the host crontab (job_run.py pattern: `--job
# job_run.py pattern: `--job <node>:<name> --ticket <tk>` + trailing
# `<name>` comment), evaluates
# whether each entry was due in the poll window (shapes: */N, M H, and the
# integer-field variants; @reboot entries are excluded from window due-ness),
# and compares against hub.db job_runs.
# ---------------------------------------------------------------------------

CRON_LINE_RE = re.compile(
    r"^(?P<spec>@\w+|\S+(?:\s+\S+){4})\s+(?P<cmd>.*?)\s*(?:#\s*(?P<name>\S+))?\s*$")
JOB_ARG_RE = re.compile(r"--job\s+(\S+)")
TICKET_ARG_RE = re.compile(r"--ticket\s+(\S+)")
SEV_ADJACENT_JOBS = frozenset({
    f"{NODE_NAME}:daily-board", "daily-board",
    f"{NODE_NAME}:sre-health-monitor", "sre-health-monitor"})


def _field_matches(field: str, value: int) -> bool:
    """One cron field: `*`, `*/N`, or an integer."""
    if field == "*":
        return True
    if field.startswith("*/"):
        try:
            return value % int(field[2:]) == 0
        except ValueError:
            return False
    try:
        return value == int(field)
    except ValueError:
        return False


def cron_due(spec: str, start: float, end: float) -> bool:
    """True if the schedule fires at least once in [start, end).

    Supported shapes (the ones in use on this host): `*/N * * * *`,
    `M H * * *` (incl. `*` and `*/N` fields), and `@reboot` (never due in a
    window — a reboot-spawned job is judged by staleness only). Evaluates
    minute-by-minute; poll windows are minutes wide, so this is cheap and
    exact for the supported shapes. Day-of-month/month/dow must be `*`.
    """
    parts = spec.split()
    if spec.startswith("@"):
        return False
    if len(parts) != 5:
        return False
    minute_f, hour_f, dom_f, mon_f, dow_f = parts
    if any(f != "*" for f in (dom_f, mon_f, dow_f)):
        return False
    t = int(start // 60 * 60)
    while t < end:
        tm = time.gmtime(t)
        if _field_matches(minute_f, tm.tm_min) and _field_matches(hour_f, tm.tm_hour):
            return True
        t += 60
    return False


@dataclass
class CronEntry:
    spec: str
    job_id: str
    ticket: str
    name: str
    cmd: str = ""          # payload after " -- " (re-trigger needs it)


def parse_crontab(text: str) -> List[CronEntry]:
    """Crontab lines following the job_run.py pattern → CronEntry list."""
    entries = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = CRON_LINE_RE.match(line)
        if not m:
            continue
        job = JOB_ARG_RE.search(m.group("cmd"))
        if not job:
            continue
        ticket_m = TICKET_ARG_RE.search(m.group("cmd"))
        entries.append(CronEntry(
            spec=m.group("spec"), job_id=job.group(1),
            ticket=ticket_m.group(1) if ticket_m else "",
            name=m.group("name") or job.group(1),
            cmd=_cron_payload(m.group("cmd"))))
    return entries


def _cron_payload(line_cmd: str) -> str:
    """The command payload after job_run.py's ' -- ' separator: everything
    after the LAST ' -- ' on the line, unquoted, trailing redirects
    stripped. The re-trigger must supply it — job_run.py run rejects a
    bare 'run' with no command (observed: re-trigger closed as no-op)."""
    parts = re.split(r"\s--\s", line_cmd)
    if len(parts) < 2:
        return ""
    payload = " -- ".join(parts[1:]).strip()
    # strip a trailing shell redirect that follows a closing quote:
    #   '...cmd...' >> log 2>&1
    m = re.match(r"^'(.*)'\s*(?:>>?|2>&1|\s*2\s*>&1).*$", payload) \
        or re.match(r"^\"(.*)\"\s*(?:>>?|2>&1|\s*2\s*>&1).*$", payload)
    if m:
        return m.group(1).strip()
    m = re.match(r"^(.*?)\s*>>?\s+\S.*$", payload)   # unquoted + redirect
    if m:
        return m.group(1).strip()
    return payload


def _runs_since(db_path: str, job_id: str, since: float,
                until: float) -> List[dict]:
    """job_runs rows for one job_id with started_at in [since, until)."""
    if not os.path.exists(db_path):
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.2)
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM job_runs WHERE job_id=?", (job_id,))]
        conn.close()
    except sqlite3.Error:
        return []
    out = []
    for r in rows:
        ts = _norm_started(r.get("started_at") or r.get("ended_at"))
        if ts is not None and since <= ts < until:
            out.append(r)
    return out


def _norm_started(value) -> Optional[float]:
    """ISO-8601 string or epoch → epoch seconds (UTC)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    try:
        return time.mktime(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")) - \
            time.timezone
    except ValueError:
        return None


def schedule_findings(entries: List[CronEntry], db_path: str,
                      win_start: float, win_end: float,
                      clock: Callable[[], float],
                      interval_s: float = DEFAULT_INTERVAL_S,
                      now: Optional[float] = None) -> List[OpsFinding]:
    """MISSED (due in window, no run), FAILED (ran, status failed), STALE
    (no run in 2× the poll interval) findings for the reconciled schedule."""
    now = now if now is not None else clock()
    stale_since = now - 2 * interval_s
    out = []
    for e in entries:
        if e.spec.startswith("@"):
            continue                      # reboot-spawned: staleness only
        runs_win = _runs_since(db_path, e.job_id, win_start, win_end)
        runs_stale = _runs_since(db_path, e.job_id, stale_since, now)
        failed = [r for r in runs_win
                  if str(r.get("status") or "").lower() in
                  ("failed", "error") or
                  (r.get("exit_code") not in (None, 0, "0")
                   and str(r.get("status") or "").lower() not in
                   ("ok", "done", "success"))]
        sev_label = e.name in SEV_ADJACENT_JOBS or e.job_id in SEV_ADJACENT_JOBS
        day = time.strftime("%Y%m%d", time.gmtime(clock()))
        tags = ("schedule-reconciliation", e.name)
        evidence = {"job_id": e.job_id, "spec": e.spec,
                    "window": [win_start, win_end],
                    "runs_in_window": len(runs_win)}
        check = {"kind": "schedule-miss", "job_id": e.job_id,
                 "ticket": e.ticket, "cmd": e.cmd, "created_ts": now}
        action = (f"re-trigger once: job_run.py run --node {NODE_NAME} "
                  f"--job {e.job_id} --ticket {e.ticket}")
        due_win = cron_due(e.spec, win_start, win_end)
        due_stale = cron_due(e.spec, stale_since, now)

        def mk(fid: str, claim: str, extra_tags: tuple, extra_evidence: dict,
               verify) -> OpsFinding:
            return OpsFinding(
                finding_id=fid, claim=claim, reversible=True, scope="internal",
                covered_by_mandate=False, tags=tags + extra_tags,
                proposed_action=action, evidence=dict(evidence, **extra_evidence),
                check=check, verify=verify, playbook="", max_fixes=2)

        if failed:
            fid = f"job:{e.job_id}:{day}"       # same id as job_run findings
            claim = (f"total outage of the {e.name} pipeline: scheduled job "
                     f"{e.job_id} FAILED (status failed) — report pipeline dead"
                     if sev_label else
                     f"scheduled job {e.job_id} FAILED (reconciliation)")
            out.append(mk(fid, claim, ("schedule-failed",),
                          {"failed_runs": len(failed)},
                          lambda j=e.job_id, db=db_path, ts=now:
                              not any(str(r.get("status") or "").lower()
                                      in ("failed", "error")
                                      for r in _runs_since(db, j, ts, ts + 3600))))
        elif due_stale and not runs_stale:
            fid = f"schedule:{e.job_id}:{day}"
            due = due_win
            kind = "MISSED (due in window, no run)" if due else \
                "STALE (no run in 2× interval)"
            claim = (f"total outage of the {e.name} pipeline: scheduled job "
                     f"{e.job_id} MISSED — due ({e.spec}) but no run in "
                     f"{2 * interval_s / 60:.0f} min; platform-degraded, "
                     f"report pipeline dead" if sev_label else
                     f"scheduled job {e.job_id} {kind} — spec {e.spec}")
            out.append(mk(fid, claim, ("schedule-missed",), {"due": due},
                          lambda j=e.job_id, db=db_path, ts=now:
                              any(str(r.get("status") or "").lower()
                                  not in ("failed", "error")
                                  for r in _runs_since(db, j, ts, ts + 3600))))
        elif due_win and not runs_win:
            # due in this window but the run landed just outside it — MISSED.
            fid = f"schedule:{e.job_id}:{day}"
            claim = (f"total outage of the {e.name} pipeline: scheduled job "
                     f"{e.job_id} MISSED — due ({e.spec}) in window but no "
                     f"run; report pipeline dead" if sev_label else
                     f"scheduled job {e.job_id} MISSED (due in window, no run)"
                     f" — spec {e.spec}")
            out.append(mk(fid, claim, ("schedule-missed",), {"due": True},
                          lambda j=e.job_id, db=db_path, ts=now:
                              any(str(r.get("status") or "").lower()
                                  not in ("failed", "error")
                                  for r in _runs_since(db, j, ts, ts + 3600))))
    return out


def read_crontab() -> str:
    try:
        proc = subprocess.run(["crontab", "-l"], timeout=10,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              text=True)
        return proc.stdout or ""
    except Exception:
        return ""


def retrigger_command(job_id: str, ticket: str,
                      command: str = "") -> List[str]:
    """Mandate-scoped single re-trigger of a missed/failed cron job.

    `command` is the crontab payload after job_run.py's ' -- ' separator —
    required: job_run.py run rejects a bare 'run' (observed in prod: the
    re-trigger exited 2 with 'run needs a command after --')."""
    cmd = [sys.executable, _job_run_script(),
           "run", "--node", NODE_NAME, "--job", job_id]
    if ticket:
        cmd += ["--ticket", ticket]
    if command:
        cmd += ["--", command]
    return cmd


# ---------------------------------------------------------------------------
# Mandate + dedupe stores
# ---------------------------------------------------------------------------

def _local_store_conn(db_path: str):
    """Open a local ops store: create the parent dir on first boot."""
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    return sqlite3.connect(db_path, isolation_level=None)


class MandateRecorder:
    """Scoped + expiring mandate records in the runner's own sqlite store.
    One hub-transaction-style insert: any exception leaves nothing committed.
    Injectable; the mandate id, scope and expiry are persisted so every fix
    session is auditable and time-boxed."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS mandates (
        mandate_id TEXT PRIMARY KEY, finding_id TEXT, scope TEXT,
        expiry_at REAL, created_at REAL, status TEXT);
    """

    def __init__(self, db_path: str, clock: Callable[[], float],
                 expiry_s: float = 24 * 3600.0):
        self.conn = _local_store_conn(db_path)
        self.clock = clock
        self.expiry_s = expiry_s
        self._seq = 0
        self.conn.executescript(self.SCHEMA)

    def create(self, finding, scope: str) -> dict:
        self._seq += 1
        mandate_id = f"mf-{finding.finding_id}-{self._seq}"
        expiry_at = self.clock() + self.expiry_s
        now = self.clock()
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO mandates VALUES (?,?,?,?,?,?)",
                (mandate_id, finding.finding_id, scope, expiry_at, now,
                 "active"))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"mandate_id": mandate_id, "scope": scope, "expiry_at": expiry_at}


class DedupeStore:
    """finding_id → last auto-fix time. A finding auto-fixed within the last
    24 h is recurring: it escalates to the daily aggregate instead of being
    re-fixed."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS dedupe (
        finding_id TEXT PRIMARY KEY, last_fixed_at REAL, fix_count INTEGER);
    """

    def __init__(self, db_path: str, clock: Callable[[], float],
                 window_s: float = DEDUPE_WINDOW_S):
        self.conn = _local_store_conn(db_path)
        self.clock = clock
        self.window_s = window_s
        self.conn.executescript(self.SCHEMA)

    def hit(self, finding_id: str, max_fixes: int = 1) -> bool:
        row = self.conn.execute(
            "SELECT last_fixed_at, fix_count FROM dedupe WHERE finding_id=?",
            (finding_id,)).fetchone()
        if not row:
            return False
        last_fixed_at, fix_count = row
        if self.clock() - last_fixed_at >= self.window_s:
            return False
        return fix_count >= max_fixes

    def record_fix(self, finding_id: str) -> None:
        now = self.clock()
        row = self.conn.execute(
            "SELECT fix_count FROM dedupe WHERE finding_id=?",
            (finding_id,)).fetchone()
        count = (row[0] + 1) if row else 1
        self.conn.execute(
            "INSERT INTO dedupe VALUES (?,?,?) "
            "ON CONFLICT(finding_id) DO UPDATE SET last_fixed_at=?, fix_count=?",
            (finding_id, now, count, now, count))


# ---------------------------------------------------------------------------
# Fix session dispatch (Hermes one-shot agent)
# ---------------------------------------------------------------------------

SANDBOX_REFRESH = os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "scripts", "sandbox-refresh.sh")

#: Trees refreshed into a fix-session workspace before dispatch (agent acts
#: on the current build, not the snapshot-time state).
REFRESH_TREES = ("aerial", "gateway")

#: Probe-first preamble in every fix-session TASK.md: the agent runs
#: `python3 -m dashboard.ops_context` (read-only), then decides within its
#: mandate per docs/runtime-optimization.md.
OPS_CONTEXT_PREAMBLE = """
## Probe first (runtime optimization)
Before acting, run the read-only runtime probe from this workspace's
ai-ops tree (refreshed to the current build — see refresh.log):

    python3 -m dashboard.ops_context

It reports: free VRAM per GPU, the seat map (listening model ports, model,
VRAM held, bare vs systemd), bare-process vs systemd user units, model
shard completeness, and the cron surface. The live context snapshot for
this finding is below. Decide ONLY per the rules in
docs/runtime-optimization.md (ngl-from-free-VRAM, seat placement,
bare→systemd conversion, shard completeness, monitor budget,
parallel/context split) and stay inside your mandate: never touch another
seat's processes; container lifecycle restarts and anything outside the
mandate are human-gated (document and block for the operator). SEV/security pings
immediately before any fix.
"""


def sandbox_refresh(ws: str, script: str = SANDBOX_REFRESH,
                    trees: tuple = REFRESH_TREES) -> dict:
    """Refresh the session workspace to the current build (git pull the
    ai-ops + gateway trees). Non-fatal on failure; logged in refresh.log."""
    try:
        proc = subprocess.run([script, ws, *trees], timeout=120,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True)
        return {"exit": proc.returncode, "output": (proc.stdout or "")[-500:]}
    except Exception as exc:
        log.warning("sandbox refresh failed: %r", exc)
        return {"exit": None, "error": repr(exc)}


def default_fix_command() -> List[str]:
    return [
        HERMES_BIN, "-p", "curator", "chat", "-Q", "--oneshot",
        "-t", "file,terminal",
        "--provider", str(_pv("cluster.provider") or "custom:aerial-local"),
        "-m", str(_pv("cluster.model") or "local-model"),
        "--max-turns", "25", "--reasoning", "low",
        "-q", "Do the task in TASK.md exactly. Work only inside this folder.",
    ]


def dispatch_fix_session(finding, mandate: Optional[dict],
                         workspace_root: str = WORKSPACE_ROOT,
                         command: Optional[List[str]] = None,
                         timeout_s: float = 1200.0,
                         refresh: Optional[Callable[[str], dict]] = None,
                         ops_context_fn: Optional[Callable[[], dict]] = None
                         ) -> dict:
    """Run a Hermes one-shot fix session in a per-incident workspace:
    sandbox refresh (git-pull the current build into the workspace), then
    TASK.md (probe-first preamble + live ops-context JSON + finding +
    evidence + playbook + close-out.md instruction), the agent command with
    cwd=workspace, output to agent.out. The daemon never edits product
    code — the dispatched session does."""
    ws = os.path.join(workspace_root, finding.finding_id.replace(":", "_"))
    os.makedirs(ws, exist_ok=True)
    refresh_result = (refresh or sandbox_refresh)(ws)
    context: Dict[str, Any] = {}
    if ops_context_fn is None:
        try:
            from ..ops_context import collect_context  # type: ignore
            ops_context_fn = collect_context
        except Exception:
            ops_context_fn = None
    if ops_context_fn is not None:
        try:
            context = ops_context_fn()
        except Exception as exc:
            log.warning("ops-context probe failed: %r", exc)
            context = {"error": repr(exc)}
    task_path = os.path.join(ws, "TASK.md")
    with open(task_path, "w") as f:
        f.write(OPS_CONTEXT_PREAMBLE)
        f.write("\n## Runtime context (probe snapshot)\n```json\n")
        f.write(json.dumps(context, indent=2, default=str))
        f.write("\n```\n\n")
        f.write(finding.task_text(mandate))
    cmd = command or default_fix_command()
    t0 = clock.now()
    try:
        proc = subprocess.run(cmd, cwd=ws, timeout=timeout_s,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True)
        out_path = os.path.join(ws, "agent.out")
        with open(out_path, "w") as f:
            f.write(proc.stdout or "")
        result = {"exit": proc.returncode, "duration_s": round(clock.now() - t0, 1),
                  "workspace": ws, "task_md": task_path,
                  "refresh": refresh_result}
    except subprocess.TimeoutExpired as e:
        result = {"exit": None, "timed_out": True,
                  "duration_s": round(clock.now() - t0, 1), "workspace": ws}
        with open(os.path.join(ws, "agent.out"), "w") as f:
            f.write(str(e.stdout or "") + "\n[timed out]\n")
    log.info("fix session %s -> %s", finding.finding_id, result)
    return result


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------

class OpsRunner:
    """One poll cycle = poll → classify → act → verify, with dedupe."""

    def __init__(self, followups_db: str = DEFAULT_FOLLOWUPS_DB,
                 mandate_db: Optional[str] = None,
                 dedupe_db: Optional[str] = None,
                 clock: Callable[[], float] = clock.now,
                 notify: Optional[Callable] = None,
                 mandate_creator: Optional[Any] = None,
                 fix_executor: Optional[Callable] = None,
                 probe: Callable[..., Optional[int]] = probe_seat,
                 hub_db: str = DEFAULT_HUB_DB,
                 health_path: str = DEFAULT_HEALTH_STATUS,
                 window_s: float = DEFAULT_WINDOW_S,
                 state_path: Optional[str] = STATE_PATH,
                 crontab_fn: Callable[[], str] = read_crontab,
                 conformance_fn: Optional[Callable[[], list]] = None):
        self.clock = clock
        self.probe = probe
        if conformance_fn is None:
            from dashboard import conformance as _cf_mod
            conformance_fn = _cf_mod.run_all
        self.conformance_fn = conformance_fn
        self.hub_db = hub_db
        self.health_path = health_path
        self.window_s = window_s
        self.state_path = state_path
        self.crontab_fn = crontab_fn
        self.dedupe = DedupeStore(dedupe_db or followups_db, clock)
        self.mandate_creator = mandate_creator or MandateRecorder(
            mandate_db or followups_db, clock)
        self.fix_executor = fix_executor
        self.notify = notify or ping_guard_stub()
        self.flow = FollowupFlow(
            db_path=followups_db, clock=clock, classify_fn=clf.classify,
            mandate_creator=self.mandate_creator,
            fix_executor=self._execute_fix,
            verifier=lambda f: f.verify(),
            notify=self.notify)

    # -- fix execution (mandate-scoped Hermes session) ---------------------

    def _execute_fix(self, finding, mandate) -> dict:
        if self.fix_executor is not None:
            # injection point: tests / alternate executors handle all kinds
            return self.fix_executor(finding, mandate)
        if finding.check.get("kind") == "schedule-miss":
            # Mandate-scoped single re-trigger via the job_run.py pattern
            # (the deterministic fix; no agent session needed).
            cmd = retrigger_command(finding.check["job_id"],
                                    finding.check.get("ticket", ""),
                                    finding.check.get("cmd", ""))
            log.info("re-trigger (mandate %s): %s",
                     mandate.get("mandate_id") if mandate else "-", cmd)
            try:
                proc = subprocess.run(cmd, timeout=900,
                                      stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, text=True)
                return {"exit": proc.returncode, "cmd": cmd,
                        "output_tail": (proc.stdout or "")[-2000:]}
            except subprocess.TimeoutExpired:
                return {"exit": None, "timed_out": True, "cmd": cmd}
        if finding.check.get("kind") == "seat":
            # Remote seats (on the seat host): the recovery driver IS the deterministic
            # fix — never local systemctl for a seat served on another host
            # (2026-11-16 GLM 3-h outage). Guards (f8): only when the driver
            # is the TRUSTED profile-resolved path and exists + executable;
            # otherwise fall through to the agent session. f9: verify the
            # systemd user session works here and log the result — a missing
            # session must never crash the runner.
            script = finding.check.get("recovery_script") or ""
            trusted = script and os.path.realpath(script) == \
                os.path.realpath(_SEAT_RECOVERY_SCRIPT)
            if not trusted and script:
                log.warning("seat fix: rejected non-trusted recovery path "
                            "%r (f8) — falling back to the agent session",
                            script)
            if trusted and os.path.isfile(script) and \
                    os.access(script, os.X_OK):
                if not systemctl_user_available():
                    log.warning(
                        "systemctl --user is NOT available in this execution "
                        "context (f9) — the recovery driver will skip its "
                        "forwarder restart; continuing anyway")
                cmd = [script]
                log.info("seat recovery driver (mandate %s): %s",
                         mandate.get("mandate_id") if mandate else "-", cmd)
                try:
                    proc = subprocess.run(cmd, timeout=900,
                                          stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT, text=True)
                    return {"exit": proc.returncode, "cmd": cmd,
                            "output_tail": (proc.stdout or "")[-2000:]}
                except subprocess.TimeoutExpired:
                    return {"exit": None, "timed_out": True, "cmd": cmd}
                except Exception as exc:      # f9: never crash the runner
                    log.error("seat recovery driver failed: %r", exc)
                    return {"exit": None, "error": repr(exc), "cmd": cmd}
        if self.fix_executor is not None:
            return self.fix_executor(finding, mandate)
        return dispatch_fix_session(finding, mandate)

    # -- one finding -------------------------------------------------------

    def _remediation_path(self, rem, finding: OpsFinding) -> dict:
        """A registered remediation matched this finding: run the
        remediation contract (probe → fix → re-probe → prove) instead of a
        generic fix session. The finding closes ONLY on proved=True;
        unproved → stays OPEN with an escalation note carrying the before/
        after outputs. requires_human remediations are never executed —
        a prepared copy-paste block is produced and the operator is pinged; the
        proof happens on the FOLLOWING poll's probe."""
        fid = finding.finding_id
        if not rem.requires_human and self.dedupe.hit(fid, rem.max_attempts):
            self.flow._transition(fid, "submit", "classified",
                                  detail=f"remediation:{rem.id}")
            self.flow._set_class(fid, clf.MANDATE)
            self.flow._escalate(finding, "remediation dedupe: unproved "
                                        "fix repeated within 24h — daily "
                                        "aggregate")
            return {"finding_id": fid, "state": "escalated",
                    "remediation": rem.id, "reason": "dedupe-24h"}
        self.flow._transition(fid, "submit", "classified",
                              detail=f"remediation:{rem.id}")
        self.flow._set_class(fid, clf.MANDATE)
        if remd is None:                      # pragma: no cover (import fallback)
            raise RuntimeError("remediation module unavailable")
        try:
            res = remd.run_remediation(rem, finding)
        except Exception as exc:
            self.flow._transition(fid, "fail-fix", "fix-failed",
                                  detail=repr(exc))
            self.flow._escalate(finding, f"remediation failed: {exc!r}")
            return {"finding_id": fid, "state": "escalated",
                    "remediation": rem.id, "error": repr(exc)}
        self.flow._transition(fid, "attempt", "fix-attempted", detail=rem.id)
        if res.proved:
            self.dedupe.record_fix(fid)
            self.flow._transition(fid, "verify", "verified",
                                  detail=json.dumps(res.after, default=str))
            self.flow._transition(fid, "close", "closed", expect="verified")
            log.info("remediation %s PROVED %s (after: %s)", rem.id, fid,
                     res.after.get("detail"))
            return {"finding_id": fid, "state": "closed",
                    "remediation": rem.id, "proved": True,
                    "result": res.to_dict()}
        # Unproved: finding stays OPEN — escalation with both outputs.
        self.dedupe.record_fix(fid)
        if res.prepared:
            self.notify(clf.MANDATE, finding)      # ping the operator with the block
        self.flow._escalate(finding, res.escalation or
                            "remediation not proved")
        log.warning("remediation %s NOT proved %s: before=%r after=%r",
                    rem.id, fid, res.before, res.after)
        return {"finding_id": fid, "state": "escalated",
                "remediation": rem.id, "proved": False,
                "prepared": res.prepared, "result": res.to_dict()}

    def handle(self, finding: OpsFinding) -> dict:
        fid = finding.finding_id
        # Registered remediation first: probe → fix → re-probe → prove.
        # (Runs BEFORE dedupe: a requires_human remediation must re-probe
        # every poll so the next poll can prove the human-applied fix.)
        rem = remd.find_remediation(finding) if remd is not None else None
        if rem is not None:
            return self._remediation_path(rem, finding)
        # e. dedupe: never auto-fix the same finding twice in 24 h.
        if self.dedupe.hit(fid, getattr(finding, "max_fixes", 1)):
            cls = clf.classify(finding)
            self.flow._transition(fid, "submit", "classified", detail=cls)
            self.flow._set_class(fid, cls)
            self.flow._escalate(finding, "dedupe: auto-fixed within 24h — "
                                        "recurring failure, daily aggregate")
            log.info("dedupe hit %s -> escalated", fid)
            return {"finding_id": fid, "fix_class": cls, "state": "escalated",
                    "reason": "dedupe-24h"}
        cls = clf.classify(finding)
        # per-job ticket: the failure history lives on the job's own ticket
        try:
            jt = job_tickets.sync()
            mine = [e for e in jt if e["name"] in (finding.finding_id, getattr(finding, "job_id", None))]
            if mine and mine[0].get("ticket_id"):
                job_tickets._api(f"/tickets/{mine[0]['ticket_id']}/events", "POST",
                                 {"kind": "comment", "payload": {"body":
                                  f"FAILED/MISSED at {finding.finding_id}: "
                                  f"{getattr(finding, 'detail', '')} — assigned to ops for follow-up."}})
        except Exception:
            pass  # ticket bookkeeping never blocks the fix path
        if cls in clf.IMMEDIATE_CLASSES:
            # SEV / security: immediate ping per policy AND a mandate-scoped
            # fix session (recorded through the same state machine).
            self.notify(cls, finding)
            self.flow._transition(fid, "submit", "classified", detail=cls)
            self.flow._set_class(fid, cls)
            result = self.flow._fix_path(finding, cls, create_mandate=True)
        else:
            result = self.flow.submit(finding)
        if cls in (clf.AUTO_FIX, clf.MANDATE) or cls in clf.IMMEDIATE_CLASSES:
            # a fix attempt happened (closed OR escalated): it counts toward
            # the 24h dedupe window, so a persistently failing fix is never
            # re-run forever — it escalates to the daily aggregate.
            self.dedupe.record_fix(fid)
        log.info("handled %s: %s", fid, result)
        return result

    # -- one poll cycle ----------------------------------------------------

    def poll_once(self) -> dict:
        now = self.clock()
        checkpoint = self._load_checkpoint()
        since = (checkpoint - self.window_s) if checkpoint else (now - self.window_s)
        findings: List[OpsFinding] = []
        findings += seat_findings(self.clock, self.probe)
        # IDLE-BROKER: fill measured-idle seats with pre-approved backlog.
        # Runs as a subprocess AFTER seat liveness so an ops-cycle dispatch
        # can never delay or break the poll; the broker enforces the same
        # pause gates itself (waiting>0, AERIAL_SATURATION_PAUSE, pidfiles).
        try:
            idle_broker_result = run_idle_broker()
            if idle_broker_result:
                log.info("idle-broker: %s", idle_broker_result)
        except Exception as exc:
            log.warning("idle-broker invocation failed: %r", exc)
        findings += job_run_findings(self.hub_db, since, now, self.clock)
        findings += health_findings(self.health_path, self.clock)
        # SCHEDULE-RECONCILIATION: every job that was SUPPOSED to run ran.
        entries = parse_crontab(self.crontab_fn())
        recon = schedule_findings(entries, self.hub_db, since, now, self.clock,
                                  interval_s=self.window_s, now=now)
        findings += recon
        log.info("reconciliation: %d entries checked, %d findings "
                 "(missed/failed/stale)", len(entries), len(recon))
        # CONFORMANCE PASS: PC-1..PC-7 sampled against live data. A crashing
        # probe must never kill the poll.
        try:
            from dashboard import conformance
            findings += conformance.conformance_findings(
                self.conformance_fn(), self.clock)
        except Exception as exc:
            log.warning("conformance pass failed: %r", exc)
        # dedupe within the cycle: same finding_id (e.g. a failed job seen by
        # both the job_runs poll and reconciliation) is handled once.
        seen: Dict[str, OpsFinding] = {}
        for f in findings:
            seen.setdefault(f.finding_id, f)
        findings = list(seen.values())
        results = [self.handle(f) for f in findings]
        self._save_checkpoint(now)
        summary = {"at": now, "window": [since, now], "findings": len(findings),
                   "results": results}
        log.info("poll: %s", json.dumps(
            {"at": now, "findings": len(findings),
             "states": [r.get("state") for r in results]}))
        return summary

    def loop(self, interval_s: float = DEFAULT_INTERVAL_S) -> None:
        log.info("ops-agent loop starting (interval %ss)", interval_s)
        while True:
            try:
                self.poll_once()
            except Exception as exc:        # never die on one bad cycle
                log.exception("poll cycle failed: %r", exc)
            time.sleep(interval_s)

    # -- checkpoint --------------------------------------------------------

    def _load_checkpoint(self) -> Optional[float]:
        if not self.state_path or not os.path.exists(self.state_path):
            return None
        try:
            with open(self.state_path) as f:
                return float(json.load(f).get("last_poll") or 0) or None
        except (OSError, ValueError):
            return None

    def _save_checkpoint(self, ts: float) -> None:
        if not self.state_path:
            return
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"last_poll": ts}, f)
        os.replace(tmp, self.state_path)


def ping_guard_stub():
    """Default notify: PingPolicyGuard if importable, else a no-op guard."""
    try:
        from .ping_policy import PingPolicyGuard
        return PingPolicyGuard()
    except ImportError:
        try:
            from ping_policy import PingPolicyGuard   # type: ignore
            return PingPolicyGuard()
        except ImportError:
            class _G:
                pings: list = []
                brief: list = []

                def __call__(self, cls, finding):
                    return {"routed": "unrouted"}
            return _G()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="always-on ops agent")
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S)
    ap.add_argument("--once", action="store_true",
                    help="run one poll cycle and exit")
    ap.add_argument("--log", default=LOG_PATH)
    args = ap.parse_args()
    os.makedirs(os.path.dirname(args.log), exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(args.log)])
    runner = OpsRunner()
    if args.once:
        summary = runner.poll_once()
        print(json.dumps(summary, default=str))
    else:
        runner.loop(args.interval)


if __name__ == "__main__":
    main()
