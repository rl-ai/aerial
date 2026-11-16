"""Shared fixtures for the Lane E acceptance suite (spec §7, rev2).

Imports the four built lane modules from the build tree.
"""
import json
import os
import sqlite3
import sys
import time

import pytest

BUILD = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BUILD not in sys.path:
    sys.path.insert(0, BUILD)

T0 = 1_000_000.0
H = 3600.0

from dashboard.data_layer.job_runs import JobRunsDB            # noqa: E402
from dashboard.data_layer.model_calls_tailer import ModelCallsTailer  # noqa: E402
from dashboard.data_layer.sessions import SessionsStore        # noqa: E402
from dashboard.data_layer.snapshot_store import SnapshotStore  # noqa: E402
from dashboard.exception_queue import queue as qmod            # noqa: E402
from dashboard.exception_queue.mandate_flow import (           # noqa: E402
    MandateFlow, WriteToken, WriteAuthError, NoFreshDecision)
from dashboard.notifications.engine import (                   # noqa: E402
    NotificationEngine, Ping)
from dashboard.daily_board import promotion, render            # noqa: E402


def make_finding(ck="c1", cls="degraded", a=0.8, eval_passed=True, action="act-1", **kw):
    return qmod.Finding(condition_key=ck, claim=f"claim-{ck}", impact_class=cls,
                        admission_score=a, eval_passed=eval_passed,
                        proposed_action=action, **kw)


@pytest.fixture
def now():
    return T0


@pytest.fixture
def queue(tmp_path, now):
    policy = os.path.join(os.path.dirname(qmod.__file__), "queue-policy.yaml")
    return qmod.Queue(db_path=str(tmp_path / "q.db"), policy_path=policy,
                      clock=lambda: now)


@pytest.fixture
def job_runs_db(tmp_path, now):
    db = str(tmp_path / "a5-runtime.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE job_runs (project TEXT, stage_type TEXT, status TEXT,"
                " evidence_json TEXT, completed_at REAL, duration_hours REAL)")
    con.executemany("INSERT INTO job_runs VALUES (?,?,?,?,?,?)", [
        ("aerial", "ingest", "done",
         json.dumps({"stages": [{"status": "pass"}, {"status": "pass"},
                                {"status": "unverified"}]}),
         now - 86400 * 3, 20.0),
        ("aerial", "ingest", "done", json.dumps({"stages": []}), now - 86400 * 2, 30.0),
        ("aerial", "test", "done", json.dumps({"stages": []}), now - 86400, 5.0),
        ("aerial", "test", "done", json.dumps({"stages": []}), now, 15.0),
        ("aerial", "curate", "done", json.dumps({"stages": []}), now - 86400, 20.0),
        ("aerial", "digest", "done", json.dumps({"stages": []}), now - 86400, 30.0),
    ])
    con.commit()
    con.close()
    from dashboard.data_layer.job_runs import JobRunsDB
    yield JobRunsDB(db)


@pytest.fixture
def sessions(tmp_path):
    return SessionsStore(hub_db_path=str(tmp_path / "hub.db"),
                         mirror_path=str(tmp_path / "mirror.json"))


@pytest.fixture
def tailer(tmp_path):
    ledger = tmp_path / "model-calls.jsonl"
    with open(ledger, "w") as f:
        for _ in range(5):
            f.write(json.dumps({"item": "W6 shadow week", "in_tokens": 100,
                                "out_tokens": 50}) + "\n")
        for _ in range(2):
            f.write(json.dumps({"item": "M8.1 control-plane panel", "in_tokens": 10,
                                "out_tokens": 5}) + "\n")
        f.write(json.dumps({"note": "no item tag"}) + "\n")
    return ModelCallsTailer(ledger_path=str(ledger),
                            state_path=str(tmp_path / "tail-state.json"))


class NotifHarness:
    """Event-stream harness around the Lane D NotificationEngine."""

    def __init__(self, start_ts):
        self.ts = start_ts
        self.pings = []
        self.brief = []
        self.logs = []
        self.queue = qmod.Queue(db_path=":memory:", clock=lambda: self.ts)
        self.engine = NotificationEngine(
            queue=self.queue, clock=lambda: self.ts,
            ping_sink=self.pings.append, brief_sink=self.brief.append,
            log_sink=self.logs.append)

    def advance(self, seconds):
        self.ts += seconds

    def ev(self, **kw):
        kw.setdefault("ts", self.ts)
        return self.engine.handle_event(kw)


@pytest.fixture
def harness(now):
    return NotifHarness(now)
