"""Recurring-miss day-type + remediation-contract tests (Layer 4 extension).

The ops agent's loop must close a recurring schedule miss via a VERIFIED fix
or escalate it with before/after output — never silently close, never leave
it unclassified. Mutation: a loop that closes an UNPROVED fix must fail the
contract.
"""
import os
import sqlite3
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import chaos                                  # noqa: E402
from dashboard import recurring_miss as rm                   # noqa: E402


@pytest.fixture()
def chaos_env(monkeypatch):
    monkeypatch.setenv("AERIAL_TIME_ACCELERATION", "50")
    monkeypatch.setenv("AERIAL_CHAOS", "1")
    monkeypatch.setenv("AERIAL_VIRTUAL_EPOCH", "1763000000")
    yield
    monkeypatch.delenv("AERIAL_CHAOS", raising=False)
    monkeypatch.delenv("AERIAL_TIME_ACCELERATION", raising=False)


def test_day_type_registered():
    assert "recurring-miss" in chaos.DAY_TYPES


def test_inject_recurring_miss_day(tmp_path, chaos_env):
    hub_db = tmp_path / "hub.db"
    sched = tmp_path / "scheduler.log"
    con = sqlite3.connect(hub_db)
    con.execute("CREATE TABLE IF NOT EXISTS job_runs "
                "(run_id TEXT PRIMARY KEY, job_id TEXT, node TEXT,"
                " started_at TEXT, ended_at TEXT, status TEXT,"
                " exit_code INT, duration_s REAL, summary TEXT,"
                " log_tail TEXT, meta TEXT, pid INT)")
    con.commit()
    con.close()
    summary = chaos.inject_day(0, day_type="recurring-miss",
                               hub_db=str(hub_db), scheduler_log=str(sched),
                               ledger=str(tmp_path / "l.jsonl"),
                               flags_path=str(tmp_path / "f.json"),
                               bridge_log=str(tmp_path / "b.log"),
                               board_html=str(tmp_path / "board.html"),
                               shadow_comp=str(tmp_path / "s.jsonl"))
    assert summary["day_type"] == "recurring-miss"
    misses = summary["recurring_misses"]
    assert misses and misses[0]["misses"] >= 3
    # cron-shaped: misses at fixed intervals
    log = sched.read_text()
    assert log.count("exit=1 error=schedule-miss") >= 3
    # failed job_runs recorded in the hub db
    con = sqlite3.connect(hub_db)
    n = con.execute("SELECT COUNT(*) FROM job_runs WHERE status='failed'"
                    " AND job_id LIKE '%recurring%'").fetchone()[0]
    con.close()
    assert n >= 3


def test_finding_shape_matches_runner():
    f = rm.schedule_miss_finding("job-x", 3)
    assert f.finding_id == "schedule:job-x:3"
    assert f.check["kind"] == "schedule-miss"


def test_contract_fixable_closes_verified(tmp_path):
    marker = tmp_path / "marker.txt"
    rem, cmd = rm.schedule_miss_remediation(f"echo fixed > {marker}",
                                            str(marker))
    finding = rm.schedule_miss_finding("job-x", 1)
    out = rm.remediate_or_escalate(finding, rem)
    ok, detail = rm.assert_contract(out)
    assert ok, detail
    assert out["action"] == "closed-verified"
    assert marker.read_text().strip() == "fixed"
    assert out["classification"]


def test_contract_broken_payload_escalates(tmp_path):
    marker = tmp_path / "marker-broken.txt"
    rem, cmd = rm.schedule_miss_remediation(
        f"exit 3 > {marker}", str(marker))
    finding = rm.schedule_miss_finding("job-x", 1)
    out = rm.remediate_or_escalate(finding, rem)
    ok, detail = rm.assert_contract(out)
    assert ok, detail
    assert out["action"] == "escalated-open"
    assert out["result"].escalation
    assert "BEFORE" in out["result"].escalation


def test_mutation_silent_close_caught():
    """A loop that closes a finding WITHOUT a proved fix = the gap class."""
    fake_result = SimpleNamespace(proved=False, escalation=None)
    out = {"action": "closed-verified", "result": fake_result,
           "classification": "safe-fix"}
    ok, detail = rm.assert_contract(out)
    assert not ok
    assert "SILENT CLOSE" in detail


def test_mutation_unclassified_caught():
    fake_result = SimpleNamespace(proved=True, escalation=None)
    out = {"action": "closed-verified", "result": fake_result,
           "classification": None}
    ok, _ = rm.assert_contract(out)
    assert not ok


def test_full_scenario_layer(tmp_path):
    out = rm.scenario(str(tmp_path))
    assert out["ok"], "\n".join(out["lines"])
    assert out["data"]["fixable"] == "closed-verified"
    assert out["data"]["broken"] == "escalated-open"
