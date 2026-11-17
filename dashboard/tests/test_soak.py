"""Layer 6 SOAK unit tests: slope detector + mutation discipline.

Synthetic resource series: flat -> PASS, sustained positive slope -> FAIL.
Mutation rule: a deliberate fd-leak double MUST be caught by the same
pipeline the real stack goes through; if not, the layer is not armed.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import soak                                   # noqa: E402


def series(slope_per_req: float, n_samples: int = 10, step: int = 250,
           base: float = 100.0):
    return [{"n": float(i * step), "v": base + slope_per_req * i * step}
            for i in range(n_samples)]


CFG = {"sample_every": 250, "thresholds": {"v": 20.0}, "ceilings": {"v": None}}


def test_flat_series_passes():
    ok, det = soak.evaluate_series(series(0.0), "v", 20.0)
    assert ok
    assert det["slope_per_1000"] == pytest.approx(0.0, abs=1e-6)


def test_positive_slope_fails():
    # +0.05 per request = +50 per 1000 requests, threshold 20
    ok, det = soak.evaluate_series(series(0.05), "v", 20.0)
    assert not ok
    assert det["slope_per_1000"] == pytest.approx(50.0, abs=1.0)
    assert "grows" in det["reason"]


def test_slope_math_exact():
    # exactly +1 unit per request -> +1000 per 1000 requests
    s = series(1.0, n_samples=5)
    assert soak.slope_per_1000([p["n"] for p in s],
                               [p["v"] for p in s]) == pytest.approx(1000.0)


def test_below_threshold_passes():
    # +0.01 per request = +10 per 1000 < threshold 20
    ok, det = soak.evaluate_series(series(0.01), "v", 20.0)
    assert ok


def test_ceiling_fails_even_with_flat_slope():
    s = series(0.0, base=950.0)
    ok, det = soak.evaluate_series(s, "v", 20.0, ceiling=900.0)
    assert not ok
    assert "ceiling" in det["reason"]


def test_too_few_samples_not_judged():
    ok, det = soak.evaluate_series(series(5.0, n_samples=2), "v", 20.0)
    assert ok and "not judged" in det["note"]


def test_evaluate_samples_all_resources():
    samples = [{"n": float(i * 250), "fds": 100 + 0.02 * i * 250,
                "rss_kb": 50_000.0, "disk_kb": 100.0, "wal_kb": 10.0}
               for i in range(10)]
    cfg = dict(CFG, sample_every=250)
    cfg["thresholds"] = {"fds": 20.0, "rss_kb": 10240.0,
                         "disk_kb": 512.0, "wal_kb": 256.0}
    cfg["ceilings"] = {}
    ok, details = soak.evaluate_samples(samples, cfg)
    assert ok                       # fds slope exactly at threshold (20.0), not over
    samples_leak = [{"n": float(i * 250), "fds": 100 + 0.05 * i * 250,
                     "rss_kb": 50_000.0, "disk_kb": 100.0, "wal_kb": 10.0}
                    for i in range(10)]
    ok, details = soak.evaluate_samples(samples_leak, cfg)
    assert not ok
    fds = [d for d in details if d["resource"] == "fds"][0]
    assert fds["slope_per_1000"] == pytest.approx(50.0, abs=1.0)
    # flat everything -> pass
    flat = [{"n": float(i * 250), "fds": 100.0, "rss_kb": 50_000.0,
             "disk_kb": 100.0, "wal_kb": 10.0} for i in range(10)]
    ok, _ = soak.evaluate_samples(flat, cfg)
    assert ok


def test_config_overlay(tmp_path):
    p = tmp_path / "soak.json"
    p.write_text(json.dumps({"requests_per_day": 100,
                             "thresholds": {"fds": 5.0}}))
    old = os.environ.get("AERIAL_SOAK_CONFIG")
    os.environ["AERIAL_SOAK_CONFIG"] = str(p)
    try:
        cfg = soak.load_config()
    finally:
        if old is None:
            os.environ.pop("AERIAL_SOAK_CONFIG", None)
        else:
            os.environ["AERIAL_SOAK_CONFIG"] = old
    assert cfg["requests_per_day"] == 100
    assert cfg["thresholds"]["fds"] == 5.0
    assert cfg["thresholds"]["rss_kb"] == soak.DEFAULTS["thresholds"]["rss_kb"]


def test_fd_leak_double_grows_with_requests():
    d = soak.FdLeakDouble()
    try:
        base = d.sample()["fds"]
        for _ in range(50):
            d.request()
        grown = d.sample()["fds"]
        assert grown > base + 40
    finally:
        d.close()


def test_mutation_check_catches_fd_leak():
    """The deliberate fd-leak double MUST fail the detector."""
    caught, detail = soak.run_mutation_check()
    assert caught, f"mutation NOT caught — detector not armed: {detail}"


def test_mutation_inversion_clean_double_passes():
    """Sanity: a clean target passes the same pipeline (no false positive)."""
    class Clean(soak.LoadTarget):
        name = "clean"
        def request(self):
            pass
        def sample(self):
            return {"fds": 10.0, "rss_kb": 1000.0, "disk_kb": 1.0,
                    "wal_kb": 0.0}
    samples = soak.run_samples(Clean(), n_requests=200, sample_every=50)
    ok, _ = soak.evaluate_samples(
        samples, {"sample_every": 50, "thresholds": {"fds": 5.0},
                  "ceilings": {"fds": 10_000}})
    assert ok


def test_run_samples_counts_requests():
    class Counting(soak.LoadTarget):
        def __init__(self):
            self.n = 0
        def request(self):
            self.n += 1
        def sample(self):
            return {"fds": 0.0}
    t = Counting()
    samples = soak.run_samples(t, n_requests=120, sample_every=40)
    assert t.n == 120
    assert samples[-1]["n"] == 120.0
