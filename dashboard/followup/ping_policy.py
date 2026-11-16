"""Notification policy for the failure-followup workflow (decision D5).

Binding policy:
- Immediate pings go out ONLY for SEV-level failures or security breaches
  (platform fully dead, data loss, fail-open / auth-bypass / secret-leak) —
  i.e. the classifier's `sev-incident` and `sev-security` classes.
- EVERYTHING else — auto-fix, mandate, daily-report, unknown (fail-closed),
  and all escalations of non-SEV findings — aggregates to the daily report.
  No immediate ping is ever emitted for a non-SEV, non-security class.

Integration: pings go through the Lane D NotificationEngine ping path
(`_emit_additive`, so a SEV ping is additive and survives the daily cap);
daily-report entries go to the engine's brief (the single daily digest).
"""

from __future__ import annotations

from typing import Any, Callable, Optional

try:
    from .classifier import (IMMEDIATE_CLASSES, SEV_INCIDENT, SEV_SECURITY,
                             UNKNOWN)
except ImportError:
    from classifier import (IMMEDIATE_CLASSES, SEV_INCIDENT, SEV_SECURITY,   # type: ignore
                            UNKNOWN)

# The authoritative immediate-ping set (mirrors classifier.IMMEDIATE_CLASSES).
IMMEDIATE_PING_CLASSES = frozenset(IMMEDIATE_CLASSES)

SEV_PING_KIND = "sev-failure"


def is_immediate(fix_class: str) -> bool:
    """True only for SEV-level failures or security breaches."""
    return fix_class in IMMEDIATE_PING_CLASSES


def route(fix_class: str, finding: Any,
          engine: Optional[Any] = None,
          project: str = "aerial",
          brief_sink: Optional[Callable[[str], None]] = None,
          ping_sink: Optional[Callable[[Any], None]] = None) -> dict:
    """Route a classified finding per the policy.

    - Immediate classes: ONE additive ping through the Lane D engine's ping
      path (survives the daily demotable cap — SEV/security must never be
      demoted or coalesced away).
    - Everything else: a daily-report line via the engine's brief sink. No
      ping. Includes unknown (fail-closed) classes and every escalation of a
      non-SEV finding.
    """
    message = f"[{fix_class}] {getattr(finding, 'claim', finding)}"
    finding_id = getattr(finding, "finding_id", "?")
    if is_immediate(fix_class):
        if engine is not None:
            ping = engine._emit_additive(
                SEV_PING_KIND, finding_id, project, message, engine.clock(),
                finding_id)
            return {"routed": "immediate-ping", "ping": ping}
        if ping_sink is not None:
            ping_sink({"kind": SEV_PING_KIND, "finding_id": finding_id,
                       "message": message})
        return {"routed": "immediate-ping"}
    # Non-SEV, non-security: daily aggregate only — NEVER an immediate ping.
    if engine is not None:
        engine.brief_sink(message)
    elif brief_sink is not None:
        brief_sink(message)
    return {"routed": "daily-report"}


class PingPolicyGuard:
    """Standalone guard for tests / callers without an engine: records routed
    pings and brief lines so the policy can be asserted directly."""

    def __init__(self):
        self.pings: list = []
        self.brief: list = []

    def __call__(self, fix_class: str, finding: Any) -> dict:
        return route(fix_class, finding,
                     ping_sink=self.pings.append,
                     brief_sink=self.brief.append)
