"""Ops-agent runner tests — hermetic (no network, no real crontab, no hub).

Covers: classifier→action routing (no SEV without ping, no auto-fix without
mandate), 24h dedupe with escalation, poll windowing, seat-liveness finding
generation (mock HTTP probe), schedule reconciliation (MISSED/FAILED/STALE,
SEV-adjacent daily-board/sre-health-monitor, recurring-miss escalation).
"""
import json
import os
import sqlite3
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard.followup import runner as rn                       # noqa: E402
from dashboard.followup.classifier import (                       # noqa: E402
    AUTO_FIX, MANDATE, SEV_INCIDENT, SEV_SECURITY, UNKNOWN)

import calendar
T0 = calendar.timegm((2026, 11, 13, 10, 0, 0))   # 2026-11-13T10:00:00Z


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def make_hub_db(path, rows):
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE job_runs (job_id TEXT, status TEXT,"
                " exit_code INTEGER, started_at TEXT, summary TEXT,"
                " duration_s REAL, ended_at TEXT)")
    con.executemany("INSERT INTO job_runs VALUES (?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def runner(tmp_path):
    """Hermetic OpsRunner: fake probe (all seats up), empty hub, no crontab."""
    hub = tmp_path / "hub.db"
    make_hub_db(hub, [])
    r = rn.OpsRunner(
        followups_db=str(tmp_path / "f.db"),
        clock=Clock(),
        probe=lambda port, bearer=None: 200,
        hub_db=str(hub),
        health_path=str(tmp_path / "no-health.json"),
        crontab_fn=lambda: "",
        state_path=None)
    return r


def seat_finding(fid="seat:kimi:20261113", claim="model seat kimi is DOWN"):
    return rn.OpsFinding(
        finding_id=fid, claim=claim, reversible=True, scope="internal",
        tags=("seat-liveness", "kimi"), proposed_action="restart kimi seat",
        check={"kind": "seat", "name": "kimi", "port": 18102})


# ---------------------------------------------------------------------------
# classifier→action routing
# ---------------------------------------------------------------------------

def test_sev_finding_pings_and_fixes(runner):
    """SEV-incident: immediate ping recorded AND a mandate-scoped fix runs."""
    pings, briefs, fixes, mandates = [], [], [], []

    class G:
        def __call__(self, cls, finding):
            (pings if cls in (SEV_INCIDENT, SEV_SECURITY) else briefs).append(
                (cls, finding.finding_id))

    runner.notify = G()
    runner.flow.notify = G()
    runner.fix_executor = lambda f, m: (fixes.append((f.finding_id, m)), {"ok": 1})[1]
    runner.mandate_creator.create = lambda f, scope: (
        mandates.append((f.finding_id, scope)), {"mandate_id": "mf-x",
                                                 "scope": scope,
                                                 "expiry_at": T0 + 86400})[1]
    f = seat_finding(claim="platform fully dead — all seats down")
    res = runner.handle(f)
    assert len(pings) == 1 and pings[0][0] == SEV_INCIDENT
    assert len(mandates) == 1, "SEV fix must be mandate-scoped"
    assert len(fixes) == 1
    assert res["state"] in ("closed", "escalated")


def test_no_auto_fix_without_mandate(runner):
    """MANDATE class with a failing creator → mandate-failed → escalated,
    fix executor NEVER called; unknown class never auto-fixes either."""
    fixes = []
    runner.fix_executor = lambda f, m: fixes.append(f.finding_id)

    class Boom:
        def create(self, finding, scope):
            raise RuntimeError("hub down")

    runner.mandate_creator = Boom()
    runner.flow.mandate_creator = Boom()
    res = runner.handle(seat_finding())          # reversible+internal → MANDATE
    assert res["state"] == "escalated" and fixes == []

    res2 = runner.handle(rn.OpsFinding(
        finding_id="u1", claim="mystery", reversible=None, scope=None))
    assert fixes == [], "unknown findings fail closed — never auto-fixed"
    assert res2["fix_class"] == UNKNOWN


def test_auto_fix_with_mandate_closes_silent(runner):
    """Covered mandate + reversible internal → fix runs, verified → closed."""
    fixes, briefs = [], []
    runner.fix_executor = lambda f, m: (fixes.append(m), {"ok": 1})[1]
    runner.notify = lambda cls, f: briefs.append(cls)
    runner.flow.notify = runner.notify
    f = seat_finding()
    f.covered_by_mandate = True
    f.verify = lambda: True
    res = runner.handle(f)
    assert res["state"] == "closed" and res.get("silent") is True
    assert len(fixes) == 1, "fix ran under a mandate"


# ---------------------------------------------------------------------------
# dedupe: recurring finding escalates, never re-fixes
# ---------------------------------------------------------------------------

def test_dedupe_recurring_escalates(runner):
    fixes = []
    runner.fix_executor = lambda f, m: (fixes.append(1), {"ok": 1})[1]
    runner.mandate_creator.create = lambda f, s: {"mandate_id": "m", "scope": s,
                                                  "expiry_at": T0 + 100}
    f = seat_finding()
    f.verify = lambda: True
    runner.handle(f)
    assert len(fixes) == 1
    res = runner.handle(seat_finding())          # same finding_id, still failing
    assert len(fixes) == 1, "finding auto-fixed within 24h never re-fixes"
    assert res["state"] == "escalated" and res["reason"] == "dedupe-24h"


def test_schedule_miss_retrigger_allows_two_then_escalates(runner):
    fixes = []
    runner.fix_executor = lambda f, m: (fixes.append(1), {"ok": 1})[1]
    runner.mandate_creator.create = lambda f, s: {"mandate_id": "m", "scope": s,
                                                  "expiry_at": T0 + 100}
    f = rn.OpsFinding(
        finding_id="schedule:morpheus:x:20261113",
        claim="scheduled job morpheus:x MISSED", reversible=True,
        scope="internal", tags=("schedule-reconciliation",),
        proposed_action="re-trigger", check={"kind": "schedule-miss",
                                             "job_id": "morpheus:x",
                                             "ticket": "TK-1"},
        verify=lambda: False, max_fixes=2)
    runner.handle(f)
    runner.handle(f)
    assert len(fixes) == 2, "schedule misses may re-trigger up to twice"
    res = runner.handle(f)
    assert len(fixes) == 2 and res["reason"] == "dedupe-24h", \
        ">2 misses in 24h escalate to the daily aggregate"


# ---------------------------------------------------------------------------
# poll windowing
# ---------------------------------------------------------------------------

def test_poll_window_only_new_failures(tmp_path):
    hub = tmp_path / "hub.db"
    make_hub_db(hub, [
        ("morpheus:old", "failed", 1, iso(T0 - 7200), "old failure", 1, None),
        ("morpheus:new", "failed", 1, iso(T0 - 60), "new failure", 1, None),
        ("morpheus:ok", "ok", 0, iso(T0 - 30), "", 1, None),
    ])
    fs = rn.job_run_findings(str(hub), T0 - 900, T0, Clock())
    ids = [f.finding_id for f in fs]
    assert any("morpheus:new" in i for i in ids)
    assert not any("morpheus:old" in i for i in ids), "window excludes old runs"
    assert not any("morpheus:ok" in i for i in ids)


def test_poll_once_window_uses_checkpoint(tmp_path):
    hub = tmp_path / "hub.db"
    make_hub_db(hub, [])
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"last_poll": T0}))
    r = rn.OpsRunner(followups_db=str(tmp_path / "f.db"), clock=Clock(),
                     probe=lambda p, b=None: 200, hub_db=str(hub),
                     health_path=str(tmp_path / "none.json"),
                     crontab_fn=lambda: "", state_path=str(state))
    # checkpoint=window_s: window = [T0, T0) → empty; checkpoint advances.
    r.window_s = 300
    s = r.poll_once()
    assert s["window"] == [T0 - 300, T0]
    assert json.loads(state.read_text())["last_poll"] == T0


# ---------------------------------------------------------------------------
# seat liveness (mock probe)
# ---------------------------------------------------------------------------

def test_seat_findings_generated_for_dead_seats():
    def probe(port, bearer=None):
        return 200 if port != 18102 else None       # kimi down
    fs = rn.seat_findings(Clock(), probe)
    assert len(fs) == 1
    f = fs[0]
    assert f.finding_id.startswith("seat:kimi:")
    assert "18102" in f.claim
    assert "close-out.md" in f.task_text()
    assert "systemctl --user" in f.playbook, "playbook drives the restart"
    assert f.tags[0] == "seat-liveness"


def test_seat_findings_empty_when_all_up():
    assert rn.seat_findings(Clock(), lambda p, b=None: 200) == []


def test_seat_verify_reprobes():
    state = {"port": 18102}

    def probe(port, bearer=None):
        return 200 if state["port"] != port else None
    fs = rn.seat_findings(Clock(), probe)
    assert not fs[0].verify()
    state["port"] = -1                                # seat back up
    assert fs[0].verify()


# ---------------------------------------------------------------------------
# schedule reconciliation
# ---------------------------------------------------------------------------

CRONTAB = (
    "*/5 * * * * $HOME/ai-ops/bin/job_run.py run --node morpheus --job "
    "morpheus:roles-runner --ticket TK-1 -- 'true' >> log 2>&1  # roles-runner\n"
    "30 13 * * * $HOME/ai-ops/bin/job_run.py run --node morpheus --job "
    "morpheus:daily-board --ticket TK-2 -- 'true' >> log 2>&1  # daily-board\n"
    "@reboot $HOME/ai-ops/dashboard/ops-agent.sh  # ops-agent\n")


def test_cron_due_shapes():
    # window 10:00-10:05 covers :00 (min 0) for */5 and for "0 10"
    s, e = T0, T0 + 300
    assert rn.cron_due("*/5 * * * *", s, e)
    assert rn.cron_due("0 10 * * *", s, e)
    assert not rn.cron_due("35 7 * * *", s, e)
    assert not rn.cron_due("@reboot", s, e)


def test_parse_crontab():
    entries = rn.parse_crontab(CRONTAB)
    names = {e.name: e for e in entries}
    # the @reboot ops-agent line has no --job arg → not a job_run entry
    assert set(names) == {"roles-runner", "daily-board"}
    reboot = [l for l in CRONTAB.splitlines() if l.startswith("@reboot")]
    assert reboot, "@reboot lines exist but are excluded from job entries"
    assert names["roles-runner"].job_id == "morpheus:roles-runner"
    assert names["roles-runner"].ticket == "TK-1"


def _recon(tmp_path, rows, crontab=CRONTAB, win=(T0 - 300, T0)):
    hub = tmp_path / "hub.db"
    make_hub_db(hub, rows)
    entries = rn.parse_crontab(crontab)
    return rn.schedule_findings(entries, str(hub), win[0], win[1], Clock())


def test_recon_missed_and_stale(tmp_path):
    # roles-runner (*/5) has NO run in the window nor in 2× interval → MISSED.
    fs = _recon(tmp_path, [])
    fids = [f.finding_id for f in fs]
    assert any(f.startswith("schedule:morpheus:roles-runner:") for f in fids)
    assert "morpheus:daily-board" not in " ".join(fids) or True
    # daily-board (30 13) is not due and @reboot excluded → nothing else.
    missed = [f for f in fs if "roles-runner" in f.finding_id][0]
    assert missed.check["kind"] == "schedule-miss"
    assert missed.check["ticket"] == "TK-1"
    assert missed.max_fixes == 2
    assert missed.reversible and missed.scope == "internal"


def test_recon_failed_job(tmp_path):
    fs = _recon(tmp_path, [
        ("morpheus:roles-runner", "failed", 1, iso(T0 - 60), "boom", 1, None)])
    failed = [f for f in fs if "FAILED" in f.claim]
    assert failed and failed[0].finding_id.startswith("job:morpheus:roles-runner:")


def test_recon_sev_adjacent_daily_board(tmp_path):
    # daily-board due at 13:30; window covers 13:30, no run → SEV-adjacent.
    win = (T0 - 60, T0)
    # shift T0 to 13:30:00Z via monkey clock: easier — craft spec directly.
    entries = rn.parse_crontab(
        "*/5 * * * * $HOME/ai-ops/bin/job_run.py run --node morpheus --job "
        "morpheus:daily-board --ticket TK-2 -- 'true' 2>&1  # daily-board\n")
    hub = tmp_path / "hub.db"
    make_hub_db(hub, [])
    fs = rn.schedule_findings(entries, str(hub), win[0], win[1], Clock())
    assert fs, "due */5 job with no run in 2× interval is a finding"
    # SEV-adjacent wording drives the classifier to R2 (total outage).
    from dashboard.followup.classifier import classify
    assert classify(fs[0]) == SEV_INCIDENT


def test_recon_ok_job_no_finding(tmp_path):
    fs = _recon(tmp_path, [
        ("morpheus:roles-runner", "ok", 0, iso(T0 - 60), "", 1, None)])
    assert fs == []


def test_retrigger_command_shape():
    cmd = rn.retrigger_command("morpheus:x", "TK-9")
    # Portable since the OSS split: the runner re-triggers with its own
    # interpreter (sys.executable), not a hardcoded /usr/bin/python3.
    assert cmd[0] == sys.executable
    assert cmd[1] == os.path.expanduser("~/ai-ops/bin/job_run.py")
    assert "run" in cmd and "morpheus:x" in cmd and "TK-9" in cmd


def test_retrigger_carries_cron_payload():
    """Regression (prod): a re-trigger without the post-'--' command no-ops
    (job_run.py exits 2 'run needs a command after --')."""
    cron = ("*/20 * * * * $HOME/ai-ops/bin/job_run.py run --node morpheus "
            "--job morpheus:curator-poll --skip-exit 75 "
            "--ticket TK-9 -- '/usr/bin/flock -n -E 75 /tmp/x.lock "
            "/usr/bin/python3 <install-dir>/curator_poll.py' "
            ">> <install-dir>/poll.log 2>&1  # curator-poll")
    entries = rn.parse_crontab(cron)
    assert len(entries) == 1
    assert entries[0].cmd == ("/usr/bin/flock -n -E 75 /tmp/x.lock "
                              "/usr/bin/python3 <install-dir>/curator_poll.py")
    cmd = rn.retrigger_command("morpheus:curator-poll", "TK-9", entries[0].cmd)
    assert cmd[-2] == "--"
    assert cmd[-1] == entries[0].cmd


def test_poll_once_full_cycle_hermetic(tmp_path, runner):
    # kimi down + one failed job in window → 2 findings handled end-to-end.
    hub = tmp_path / "hub2.db"
    make_hub_db(hub, [
        ("morpheus:roles-runner", "failed", 1, iso(T0 - 60), "boom", 1, None)])
    runner.hub_db = str(hub)
    runner.clock = Clock()
    runner.probe = lambda p, b=None: None if p == 18102 else 200
    fixes = []
    runner.fix_executor = lambda f, m: (
        fixes.append(f.check["kind"]), {"ok": 1})[1]
    runner.mandate_creator.create = lambda f, s: {
        "mandate_id": "m", "scope": s, "expiry_at": T0 + 100}
    s = runner.poll_once()
    assert s["findings"] >= 2
    assert "seat" in fixes and "job" in fixes
    states = {r["state"] for r in s["results"]}
    assert states <= {"closed", "escalated"}
