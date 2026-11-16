"""dashboard/clock.py — the Aerial virtual clock.

One module every time-gated consumer reads instead of time.time() /
datetime.now(). When the two env vars are unset the clock is the real
wall clock — byte-identical default behavior.

Env:
  AERIAL_VIRTUAL_EPOCH      unix timestamp the virtual clock starts from.
                            Unset → real time (now() == time.time()).
  AERIAL_TIME_ACCELERATION  float, default 1.0. Virtual seconds advance
                            ACCELERATION× real seconds.

    now() = VIRTUAL_EPOCH + (real_elapsed × ACCELERATION)

virtual_day() returns the integer virtual day count since VIRTUAL_EPOCH
(UTC-midnight aligned, so day boundaries are clean for per-day validation).
"""
from __future__ import annotations

import math
import os
import time

#: Real-time anchor: captured on first use so one accelerated run has a
#: stable origin. Lazy (not import-time) so tests can set env first.
_state: dict = {"anchor_real": None}


def _acceleration() -> float:
    raw = os.environ.get("AERIAL_TIME_ACCELERATION", "").strip()
    if not raw:
        return 1.0
    try:
        a = float(raw)
    except ValueError:
        return 1.0
    return a if a > 0.0 else 1.0


def _virtual_epoch() -> float | None:
    raw = os.environ.get("AERIAL_VIRTUAL_EPOCH", "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _real_now() -> float:
    if _state["anchor_real"] is None:
        _state["anchor_real"] = time.time()
    return time.time()


def now() -> float:
    """Virtual time now, in unix seconds.

    Unset env → exactly time.time() (identical float, byte-identical
    default behavior). With AERIAL_VIRTUAL_EPOCH set:
    VIRTUAL_EPOCH + (real_elapsed × ACCELERATION).
    """
    epoch = _virtual_epoch()
    if epoch is None:
        return _real_now()
    if _state["anchor_real"] is None:
        _state["anchor_real"] = time.time()
    return epoch + (_real_now() - _state["anchor_real"]) * _acceleration()


def virtual_day(ts: float | None = None) -> int:
    """Integer virtual day count since VIRTUAL_EPOCH (UTC-midnight aligned).

    Day 0 starts at VIRTUAL_EPOCH floored to UTC midnight, so a day is
    exactly 86400 virtual seconds. Raises when no virtual epoch is set —
    day counts are only meaningful in validation/backtest mode.
    """
    epoch = _virtual_epoch()
    if epoch is None:
        raise RuntimeError(
            "virtual_day() requires AERIAL_VIRTUAL_EPOCH (validation mode)")
    t = now() if ts is None else ts
    day0 = math.floor(epoch / 86400.0) * 86400.0
    return int((t - day0) // 86400.0)


def day_start(day: int) -> float:
    """Virtual unix-ts of the start of virtual day `day`."""
    epoch = _virtual_epoch()
    if epoch is None:
        raise RuntimeError(
            "day_start() requires AERIAL_VIRTUAL_EPOCH (validation mode)")
    day0 = math.floor(epoch / 86400.0) * 86400.0
    return day0 + day * 86400.0


def acceleration() -> float:
    """Current time acceleration factor (1.0 = real time)."""
    return _acceleration()


def validation_mode() -> bool:
    """True when the virtual clock is active (epoch set or accel > 1)."""
    return _virtual_epoch() is not None or _acceleration() != 1.0


def iso(ts: float | None = None) -> str:
    """UTC ISO timestamp for a (virtual) unix ts — the platform's canonical
    store format (`YYYY-MM-DDTHH:MM:SSZ`)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now() if ts is None else ts))


def reset_anchor() -> None:
    """Drop the real-time anchor (tests / new runs)."""
    _state["anchor_real"] = None
