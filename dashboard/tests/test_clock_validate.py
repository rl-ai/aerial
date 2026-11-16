"""Hermetic tests for the virtual clock, chaos injector, and accelerated
validation (7 days of evidence in a fraction of the time).

Everything here is hermetic: chaos/validation stores are temp dirs; the
guarded injector refuses to run without the validation env; the default
(unset-env) behavior is asserted byte-identical to real time.
"""
import json
import os
import shutil
import sqlite3
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from dashboard import clock, chaos, validate_days as vd   # noqa: E402
from dashboard.exec_summary import yesterday_window       # noqa: E402
from dashboard.notifications.engine import NotificationEngine  # noqa: E402
from dashboard.daily_board import promotion               # noqa: E402

EPOCH = 1_764_000_000.0   # arbitrary virtual epoch


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("AERIAL_VIRTUAL_EPOCH", "AERIAL_TIME_ACCELERATION",
              "AERIAL_CHAOS"):
        monkeypatch.delenv(k, raising=False)
    clock.reset_anchor()
    yield
    clock.reset_anchor()


# ---------------------------------------------------------------- clock math

def test_unset_env_is_real_time():
    """Byte-identical default: no env → now() IS time.time()."""
    t0 = time.time()
    n = clock.now()
    assert 0 <= n - t0 < 1.0
    assert clock.acceleration() == 1.0
    assert not clock.validation_mode()


def test_acceleration_and_epoch_offset(monkeypatch):
    monkeypatch.setenv("AERIAL_VIRTUAL_EPOCH", str(EPOCH))
    monkeypatch.setenv("AERIAL_TIME_ACCELERATION", "48")
    clock.reset_anchor()
    real0 = time.time()
    v0 = clock.now()
    assert v0 == pytest.approx(EPOCH, abs=1.0)
    time.sleep(0.2)
    elapsed_real = time.time() - real0
    gained = clock.now() - v0
    # ×48: virtual advance ≈ 48× real advance (loose bounds for scheduler slop)
    assert gained > elapsed_real * 20
    assert gained < elapsed_real * 200 + 1


def test_virtual_day_boundaries(monkeypatch):
    monkeypatch.setenv("AERIAL_VIRTUAL_EPOCH", str(EPOCH))
    clock.reset_anchor()
    d0 = clock.day_start(0)      # UTC midnight aligned
    assert d0 <= EPOCH
    assert clock.virtual_day(d0) == 0
    assert clock.virtual_day(d0 + 86399.9) == 0
    assert clock.virtual_day(d0 + 86400.0) == 1
    assert clock.day_start(7) == d0 + 7 * 86400.0


def test_virtual_day_requires_epoch(monkeypatch):
    with pytest.raises(RuntimeError):
        clock.virtual_day()


def test_consumer_reads_clock(monkeypatch):
    """One wiring point per consumer: the engine's default clock, the
    exec-summary window default, and the promotion gate default now() all
    read clock.now() — under a virtual epoch they see virtual time."""
    monkeypatch.setenv("AERIAL_VIRTUAL_EPOCH", str(EPOCH))
    monkeypatch.setenv("AERIAL_TIME_ACCELERATION", "10")
    clock.reset_anchor()
    eng = NotificationEngine(db_path=":memory:")
    assert abs(eng.clock() - clock.now()) < 1.0
    win = yesterday_window()
    # yesterday = the virtual previous UTC day, not the real one
    virtual_midnight = clock.day_start(0)
    assert win[1] == pytest.approx(virtual_midnight, abs=2.0)
    gate = promotion.evaluate_gate("curator", [
        {"ts": EPOCH - 86400 * 8, "evidence_label": "verified",
         "draft_mandate_spec": {"scope": "s", "expiry": "2027-01-01T00:00:00Z"},
         "divergence": False}], now=None)
    assert gate["criteria"][0]["satisfied"]


# ---------------------------------------------------------------- chaos

_STORE_SEQ = [0]


def _store(tmp_path):
    # each call makes its OWN store dir (tests use two independent stores)
    _STORE_SEQ[0] += 1
    d = tmp_path / f"chaos-store-{_STORE_SEQ[0]}"
    d.mkdir()
    con = sqlite3.connect(str(d / "hub.db"))
    con.execute(
        "CREATE TABLE job_runs (run_id TEXT PRIMARY KEY, job_id TEXT,"
        " node TEXT DEFAULT '', started_at TEXT NOT NULL, ended_at TEXT,"
        " status TEXT NOT NULL, exit_code INTEGER, duration_s REAL,"
        " summary TEXT DEFAULT '', log_tail TEXT DEFAULT '',"
        " meta TEXT DEFAULT '{}', pid INTEGER)")
    con.commit()
    con.close()
    return {k: str(d / n) for k, n in (
        ("hub_db", "hub.db"), ("scheduler_log", "s.log"),
        ("ledger", "l.jsonl"), ("flags_path", "flags.json"),
        ("bridge_log", "b.log"), ("board_html", "board.html"),
        ("shadow_comp", "sc.jsonl"))}


def test_chaos_guard_refuses_without_env(tmp_path):
    with pytest.raises(chaos.ChaosGuardError):
        chaos.inject_day(0, hub_db=str(tmp_path / "hub.db"))
    assert not (tmp_path / "hub.db").exists()


def test_chaos_deterministic(tmp_path, monkeypatch):
    monkeypatch.setenv("AERIAL_TIME_ACCELERATION", "10")
    monkeypatch.setenv("AERIAL_CHAOS", "1")
    monkeypatch.setenv("AERIAL_VIRTUAL_EPOCH", str(EPOCH))
    clock.reset_anchor()
    p1, p2 = _store(tmp_path), _store(tmp_path)
    a = chaos.inject_day(3, **p1)
    b = chaos.inject_day(3, **p2)
    assert a == b                       # same seed+day → same events
    assert a["day_type"] == "failure-storm"  # DEFAULT_SEQUENCE[3]
    assert a["failed"] == 25
    # storm day diverges from quiet day
    c = chaos.inject_day(0, **p1)
    assert c["day_type"] == "quiet" and c["failed"] == 0
    # stores really written (the ledger is append-mode: both runs' lines)
    with open(p1["ledger"]) as f:
        assert a["ledger_lines"] + c["ledger_lines"] == \
            sum(1 for ln in f if ln.strip())
    rows = sqlite3.connect(p1["hub_db"]).execute(
        "SELECT COUNT(*) FROM job_runs WHERE job_id='chaos-validation'"
    ).fetchone()[0]
    assert rows >= a["job_runs"]


def test_chaos_day_types_cover_all(tmp_path, monkeypatch):
    monkeypatch.setenv("AERIAL_TIME_ACCELERATION", "10")
    monkeypatch.setenv("AERIAL_CHAOS", "1")
    monkeypatch.setenv("AERIAL_VIRTUAL_EPOCH", str(EPOCH))
    clock.reset_anchor()
    p = _store(tmp_path)
    seen = [chaos.inject_day(d, **p)["day_type"] for d in range(7)]
    assert set(seen) >= {"quiet", "failure-storm", "mixed", "recovery",
                         "anomalous"}


# ---------------------------------------------------------------- trend math

def test_trend_math_flags_cluster():
    failed = [2, 1, 3, 2, 60]           # day 4 is an outlier cluster
    flagged, detail = vd.trend_flag(failed, 4)
    assert flagged and "z=" in detail
    assert not vd.trend_flag(failed, 3)[0]  # 2 vs baseline [2,1,3] — normal
    assert not vd.trend_flag([5, 5, 5, 5], 0)[0]   # no baseline yet


# ---------------------------------------------------------------- mini
# accelerated run: ×1000 → 7 virtual days in ~10 real minutes. Marked slow
# so quick suites skip it; run with `pytest -m slow` / `-k mini`.

def test_mini_accelerated_run(tmp_path, monkeypatch):
    env = dict(os.environ)
    try:
        results = vd.run_accelerated(
            acceleration=1000.0, days=7, store_dir=str(tmp_path / "store"),
            poll_real_s=2.0)
    finally:
        os.environ.clear()
        os.environ.update(env)
        clock.reset_anchor()
    days = results["_days"]
    assert len(days) == 7                       # all 7 virtual days evaluated
    assert [d["day"] for d in days] == list(range(7))
    names = {c["name"] for c in results["criteria"]}
    assert any("promotion gate: observation_window" in n for n in names)
    assert len(results["criteria"]) == 9        # 4 continuous-ops + 4 gate + trend
    assert results["all_pass"] is True
