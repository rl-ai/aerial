"""Hermetic tests for the Aerial shadow-week scheduler.

No network, no real subprocesses: subprocess and comment-posting are
monkeypatched module attributes; all state lives under pytest tmp_path.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

import pytest

import scheduler


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Point all scheduler paths at tmp_path; reset injectables per test."""
    shadow = tmp_path / "a5-harness" / "shadow"
    monkeypatch.setenv("SHADOW_DIR", str(shadow))
    monkeypatch.delenv("HUB_URL", raising=False)
    monkeypatch.delenv("HUB_TOKEN", raising=False)
    monkeypatch.delenv("HUB_TOKEN_FILE", raising=False)
    calls = {"subprocess": [], "posts": [], "sleeps": []}

    def fake_run(cmd, **kwargs):
        calls["subprocess"].append(list(cmd))
        return type("P", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()

    def fake_post(ticket_id, body):
        calls["posts"].append((ticket_id, body))
        return 200

    def fake_sleep(s):
        calls["sleeps"].append(s)

    monkeypatch.setattr(scheduler, "run_subprocess", fake_run)
    monkeypatch.setattr(scheduler, "post_comment_impl", fake_post)
    monkeypatch.setattr(scheduler, "sleep_impl", fake_sleep)
    return {"shadow": shadow, "calls": calls}


SCRIPT = "~/ai-ops/integration/run_shadow_hierarchy.py"


def read_log(shadow: Path) -> list[str]:
    lp = shadow / scheduler.LOG_NAME
    if not lp.exists():
        return []
    return lp.read_text().splitlines()


# --------------------------------------------------------------------------
# --once mode
# --------------------------------------------------------------------------

def test_once_mode_fires_script(env):
    scheduler.main(["--once", SCRIPT])
    runs = env["calls"]["subprocess"]
    assert len(runs) == 1
    cmd = runs[0]
    assert cmd[-2:] == [os.path.expanduser(SCRIPT), "--latest-failed"]
    assert any(e == "started" for e in [ln.split()[1] for ln in read_log(env["shadow"])])
    assert any(e == "finished" for e in [ln.split()[1] for ln in read_log(env["shadow"])])
    # lock released after the run
    assert not (env["shadow"] / scheduler.LOCK_NAME).exists()


def test_once_mode_default_script_path(env):
    scheduler.main(["--once"])
    assert env["calls"]["subprocess"][0][-2] == os.path.expanduser(
        scheduler.DEFAULT_SCRIPT)


# --------------------------------------------------------------------------
# No-overlap: skip when locked
# --------------------------------------------------------------------------

def test_interval_skips_when_locked(env, monkeypatch):
    # Simulate an in-flight run by holding the lock with our own PID.
    shadow = env["shadow"]
    shadow.mkdir(parents=True)
    (shadow / scheduler.LOCK_NAME).write_text(str(os.getpid()))
    # Interval loop with a single cycle: break after first sleep.
    real_loop = scheduler.loop

    def one_cycle(script, minutes):
        scheduler.run_cycle(script)

    monkeypatch.setattr(scheduler, "loop", one_cycle)
    scheduler.main(["--interval", "5", SCRIPT])
    assert env["calls"]["subprocess"] == []
    skipped = [ln for ln in read_log(shadow) if " skipped " in ln]
    assert skipped and "another run in flight" in skipped[0]
    # Our lock (held by a live PID) must not have been stolen.
    assert (shadow / scheduler.LOCK_NAME).read_text() == str(os.getpid())


# --------------------------------------------------------------------------
# Stale lock recovery
# --------------------------------------------------------------------------

def test_stale_lock_recovered(env, monkeypatch):
    shadow = env["shadow"]
    shadow.mkdir(parents=True)
    # PID 3999999 is almost certainly dead on this host; verify to be safe.
    dead_pid = max(p for p in (3999999, 3999998) if not scheduler._pid_alive(p))
    (shadow / scheduler.LOCK_NAME).write_text(str(dead_pid))
    scheduler.main(["--once", SCRIPT])
    # Run fired despite the stale lock...
    assert len(env["calls"]["subprocess"]) == 1
    # ...recovery was logged...
    assert any("stale-lock-recovered" in ln for ln in read_log(shadow))
    # ...and the lock now belongs to us (released afterwards by --once).
    assert not (shadow / scheduler.LOCK_NAME).exists()


def test_stale_lock_garbage_content_recovered(env):
    shadow = env["shadow"]
    shadow.mkdir(parents=True)
    (shadow / scheduler.LOCK_NAME).write_text("not-a-pid")
    assert scheduler.acquire_lock() is True
    scheduler.release_lock()


# --------------------------------------------------------------------------
# Daily report
# --------------------------------------------------------------------------

def _write_brief(shadow: Path, incident: str, mtime: float, job_id="J-123"):
    d = shadow / incident
    d.mkdir(parents=True, exist_ok=True)
    body = (f"# Incident {incident}\n"
            f"job id: {job_id}\n"
            f"# Run failed on latest-failed\n"
            f"failures: 3\nwarnings: 7\n")
    p = d / "brief.md"
    p.write_text(body)
    os.utime(p, (mtime, mtime))
    return p


def test_daily_report_groups_and_filters_24h(env):
    shadow = env["shadow"]
    now = time.time()
    _write_brief(shadow, "inc-2026-11-11-alpha", now - 3600, job_id="J-1")
    _write_brief(shadow, "inc-2026-11-11-beta", now - 7200, job_id="J-2")
    _write_brief(shadow, "inc-2026-11-01-old", now - 72 * 3600, job_id="J-3")

    scheduler.main(["--daily-report", "TCK-42"])
    assert len(env["calls"]["posts"]) == 1
    ticket, body = env["calls"]["posts"][0]
    assert ticket == "TCK-42"
    # Both fresh incidents present, grouped, with job ids and counts.
    assert "inc-2026-11-11-alpha (job id: J-1)" in body
    assert "inc-2026-11-11-beta (job id: J-2)" in body
    assert "failures: 3" in body and "warnings: 7" in body
    # Stale (outside 24h) incident filtered out.
    assert "inc-2026-11-01-old" not in body
    assert "J-3" not in body
    # Alphabetical grouping order.
    assert body.index("inc-2026-11-11-alpha") < body.index("inc-2026-11-11-beta")


def test_daily_report_window_boundary(env):
    shadow = env["shadow"]
    now = time.time()
    _write_brief(shadow, "just-inside", now - 23 * 3600)
    _write_brief(shadow, "just-outside", now - 25 * 3600)
    scheduler.main(["--daily-report", "TCK-1"])
    body = env["calls"]["posts"][0][1]
    assert "just-inside" in body
    assert "just-outside" not in body


# --------------------------------------------------------------------------
# Comment POST failure tolerated, retried next cycle
# --------------------------------------------------------------------------

def test_post_failure_tolerated_then_retry(env, monkeypatch):
    shadow = env["shadow"]
    now = time.time()
    _write_brief(shadow, "inc-x", now - 3600)

    state = {"fails_left": 1}

    def flaky_post(ticket_id, body):
        if state["fails_left"] > 0:
            state["fails_left"] -= 1
            raise OSError("hub unreachable")
        env["calls"]["posts"].append((ticket_id, body))
        return 200

    monkeypatch.setattr(scheduler, "post_comment_impl", flaky_post)

    assert scheduler.daily_report("TCK-9", sdir=shadow) is False
    assert env["calls"]["posts"] == []
    assert any("daily-report-post-failed" in ln for ln in read_log(shadow))

    # Next cycle: failure cleared, post succeeds.
    assert scheduler.daily_report("TCK-9", sdir=shadow) is True
    assert len(env["calls"]["posts"]) == 1
    assert env["calls"]["posts"][0][0] == "TCK-9"
    assert any("daily-report-posted" in ln for ln in read_log(shadow))


def test_default_post_comment_uses_env(monkeypatch, tmp_path):
    """Default implementation reads HUB_URL/HUB_TOKEN and hits the right URL."""
    captured = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        status = 201

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.headers)
        captured["data"] = req.data
        return FakeResp()

    token_file = tmp_path / "token"
    token_file.write_text("sekrit\n")
    monkeypatch.setenv("HUB_URL", "http://hub.local/api")
    monkeypatch.delenv("HUB_TOKEN", raising=False)
    monkeypatch.setenv("HUB_TOKEN_FILE", str(token_file))
    monkeypatch.setattr(scheduler.urllib.request, "urlopen", fake_urlopen)

    status = scheduler.default_post_comment("TCK-7", "hello")
    assert status == 201
    assert captured["url"] == "http://hub.local/api/tickets/TCK-7/events"
    assert captured["headers"]["Authorization"] == "Bearer sekrit"
    assert b"hello" in captured["data"]


# --------------------------------------------------------------------------
# Lock hygiene / misc
# --------------------------------------------------------------------------

def test_release_lock_does_not_remove_foreign_lock(env):
    shadow = env["shadow"]
    shadow.mkdir(parents=True)
    lp = shadow / scheduler.LOCK_NAME
    lp.write_text(str(os.getpid()) + " other-scheduler")
    scheduler.release_lock()
    assert lp.exists()  # not ours to remove


def test_log_entries_have_timestamps(env):
    scheduler.main(["--once", SCRIPT])
    for ln in read_log(env["shadow"]):
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z ", ln)


def test_interval_requires_positive(env, capsys):
    code = scheduler.main(["--interval", "0"])
    assert code == 2
    assert env["calls"]["subprocess"] == []


def test_collect_briefs_empty_dir(env):
    assert scheduler.collect_briefs(env["shadow"]) == []
