#!/usr/bin/env python3
"""Tests for the idle-work broker (dashboard/idle_broker/broker.py).

Covers: idle classification (2-sample rule), cooldown + concurrency
respect, production pause gates (waiting>0, AERIAL_SATURATION_PAUSE),
pidfile double-dispatch prevention, dry-run dispatching nothing, and busy
seats getting nothing. All fixtures are in-memory/tempdir: sqlite
seat-usage db + fake subprocess.Popen.
"""
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "idle_broker"))
import broker  # noqa: E402


def make_db(path, samples):
    """samples: {seat: [(ts, running, waiting, reachable), ...]}."""
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE seat_usage(ts REAL, seat TEXT, tokens_in REAL,"
        " tokens_out REAL, prompt_s REAL, gen_s REAL, requests REAL,"
        " reachable INT, running REAL, waiting REAL, cache_pct REAL,"
        " busy_slots REAL)")
    now = time.time()
    for seat, rows in samples.items():
        for ts, running, waiting, reachable in rows:
            con.execute(
                "INSERT INTO seat_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (now - ts, seat, 0, 0, 0, 0, 0, reachable,
                 running, waiting, 0, 0))
    con.commit()
    con.close()
    return path


class FakePopen:
    """Records dispatched commands; pretends pid 4242 always."""
    calls = []

    def __init__(self, cmd, **kw):
        self.pid = 4242
        FakePopen.calls.append(cmd)


class BrokerTestBase(unittest.TestCase):
    def setUp(self):
        FakePopen.calls = []
        self.tmp = tempfile.mkdtemp(prefix="idle-broker-test-")
        self.db = os.path.join(self.tmp, "seat-usage.db")
        self.run_log = os.path.join(self.tmp, "runs.jsonl")
        self.pid_dir = os.path.join(self.tmp, "pids")
        self.backlog = os.path.join(self.tmp, "backlog.yaml")
        self.old_env = os.environ.pop("AERIAL_SATURATION_PAUSE", None)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        if self.old_env is not None:
            os.environ["AERIAL_SATURATION_PAUSE"] = self.old_env
        else:
            os.environ.pop("AERIAL_SATURATION_PAUSE", None)

    def write_backlog(self, text):
        with open(self.backlog, "w") as f:
            f.write(text)

    def mk(self, samples, entries=""):
        make_db(self.db, samples)
        self.write_backlog("entries:\n" + entries)
        return broker.Broker(db_path=self.db, backlog_path=self.backlog,
                             run_log=self.run_log, pid_dir=self.pid_dir,
                             popen=FakePopen)

    # two idle samples 5 min apart, one busy seat
    IDLE = {"glm": [(600, 0, 0, 1), (300, 0, 0, 1), (0, 0, 0, 1)],
            "ds": [(600, 2, 0, 1), (300, 1, 0, 1), (0, 0, 0, 1)]}


class TestIdleClassification(BrokerTestBase):
    def test_two_idle_samples_is_idle(self):
        b = self.mk(self.IDLE, '- id: e1\n  seat_match: glm\n  command: echo {seat}\n')
        samples = broker.latest_samples(self.db)
        self.assertTrue(broker.seat_idle(samples, "glm"))
        self.assertFalse(broker.seat_idle(samples, "ds"))

    def test_one_active_sample_disqualifies(self):
        make_db(self.db, {"glm": [(600, 0, 0, 1), (300, 1, 0, 1)]})
        samples = broker.latest_samples(self.db)
        self.assertFalse(broker.seat_idle(samples, "glm"))

    def test_fewer_than_two_samples_not_idle(self):
        make_db(self.db, {"glm": [(300, 0, 0, 1)]})
        samples = broker.latest_samples(self.db)
        self.assertFalse(broker.seat_idle(samples, "glm"))

    def test_unreachable_samples_dont_count(self):
        make_db(self.db, {"glm": [(600, 0, 0, 0), (300, 0, 0, 0), (0, 0, 0, 1)]})
        samples = broker.latest_samples(self.db)
        self.assertFalse(broker.seat_idle(samples, "glm"))


class TestDispatch(BrokerTestBase):
    def test_idle_seat_gets_highest_priority_entry(self):
        b = self.mk(self.IDLE,
                    '- id: low\n  seat_match: glm\n  command: echo low-{seat}\n'
                    '  priority: 5\n'
                    '- id: high\n  seat_match: glm\n  command: echo high-{seat}\n'
                    '  priority: 50\n')
        dispatched, decisions = b.decide(seats=["glm"])
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0]["entry"], "high")
        self.assertIn("high-glm", FakePopen.calls[0])

    def test_busy_seat_gets_nothing(self):
        b = self.mk(self.IDLE, '- id: e1\n  seat_match: ds\n  command: echo x\n')
        dispatched, _ = b.decide(seats=["ds"])
        self.assertEqual(dispatched, [])
        self.assertEqual(FakePopen.calls, [])

    def test_seat_match_filters(self):
        b = self.mk(self.IDLE, '- id: e1\n  seat_match: qwen\n  command: echo x\n')
        dispatched, _ = b.decide(seats=["glm"])
        self.assertEqual(dispatched, [])

    def test_dry_run_dispatches_nothing(self):
        b = self.mk(self.IDLE, '- id: e1\n  seat_match: glm\n  command: echo x\n')
        dispatched, decisions = b.decide(seats=["glm"], dry_run=True)
        self.assertEqual(dispatched, [])
        self.assertEqual(FakePopen.calls, [])
        self.assertEqual(decisions[0]["action"], "dispatch")  # decided, not run

    def test_disabled_entry_skipped(self):
        b = self.mk(self.IDLE,
                    '- id: e1\n  seat_match: glm\n  command: echo x\n'
                    '  enabled: false\n')
        dispatched, decisions = b.decide(seats=["glm"])
        self.assertEqual(dispatched, [])
        self.assertEqual(decisions[0]["reason"], "no eligible backlog entry")


class TestCooldownAndConcurrency(BrokerTestBase):
    ENTRY = ('- id: e1\n  seat_match: "*"\n  command: echo {seat}\n'
             '  max_concurrent: 1\n  cooldown_h: 1\n')

    def test_cooldown_blocks(self):
        b = self.mk(self.IDLE, self.ENTRY)
        d1, _ = b.decide(seats=["glm"])
        self.assertEqual(len(d1), 1)
        # immediately again: cooldown active
        d2, _ = b.decide(seats=["glm"])
        self.assertEqual(d2, [])

    def test_cooldown_expires(self):
        b = self.mk(self.IDLE, self.ENTRY)
        b.decide(seats=["glm"])
        # backdate the run-log entry 2h
        runs = broker.load_run_log(self.run_log)
        runs[-1]["ts"] -= 2 * 3600
        with open(self.run_log, "w") as f:
            for r in runs:
                f.write(json.dumps(r) + "\n")
        d2, _ = b.decide(seats=["glm"])
        self.assertEqual(len(d2), 1)

    def test_max_concurrent_respected_via_pidfile(self):
        b = self.mk(self.IDLE, self.ENTRY)
        os.makedirs(self.pid_dir, exist_ok=True)
        with open(os.path.join(self.pid_dir, "e1.pid"), "w") as f:
            f.write(f"{os.getpid()}\n")  # this test process is alive
        dispatched, _ = b.decide(seats=["glm"])
        self.assertEqual(dispatched, [])

    def test_stale_pidfile_cleared(self):
        b = self.mk(self.IDLE, self.ENTRY)
        os.makedirs(self.pid_dir, exist_ok=True)
        with open(os.path.join(self.pid_dir, "e1.pid"), "w") as f:
            f.write("999999999\n")  # dead pid
        dispatched, _ = b.decide(seats=["glm"])
        self.assertEqual(len(dispatched), 1)


class TestPauseGates(BrokerTestBase):
    ENTRY = '- id: e1\n  seat_match: "*"\n  command: echo {seat}\n'

    def test_any_seat_waiting_blocks_all(self):
        samples = dict(self.IDLE)
        samples["kimi"] = [(600, 0, 0, 1), (300, 0, 0, 1), (0, 0, 3, 1)]
        b = self.mk(samples, self.ENTRY)
        dispatched, decisions = b.decide(seats=["glm"])
        self.assertEqual(dispatched, [])
        self.assertIn("waiting", decisions[0]["reason"])

    def test_env_pause_blocks_all(self):
        os.environ["AERIAL_SATURATION_PAUSE"] = "1"
        b = self.mk(self.IDLE, self.ENTRY)
        dispatched, decisions = b.decide(seats=["glm"])
        self.assertEqual(dispatched, [])
        self.assertIn("AERIAL_SATURATION_PAUSE", decisions[0]["reason"])


class TestMiniYaml(BrokerTestBase):
    def test_real_backlog_parses_without_pyyaml(self):
        # the shipped backlog.yaml (with the real entries) must parse
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "idle_broker", "backlog.yaml")
        entries = broker.load_backlog(path)
        ids = {e["id"] for e in entries}
        self.assertTrue({"kimi-codebase-review", "bpkb-source-refresh",
                         "listening-pipeline-refresh", "buglist-triage",
                         "tier-d-practice-extract", "backtest-sweep"} <= ids)
        kimi = [e for e in entries if e["id"] == "kimi-codebase-review"][0]
        self.assertFalse(kimi["enabled"])  # kimi ships disabled
        self.assertTrue(all(e["priority"] > 0 for e in entries))


if __name__ == "__main__":
    unittest.main()
