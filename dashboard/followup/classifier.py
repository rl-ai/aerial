"""Failure classifier — rules R1-R6 implementing decision D1.

Order of evaluation is fixed and first-match-wins:

- R1  security breach (fail-open / auth-bypass / secret-leak, by tag or claim)
      → `sev-security`: immediate ping.
- R2  SEV-level failure (platform fully dead / data loss)
      → `sev-incident`: immediate ping.
- R3  reversible + internal + an active mandate already covers the fix
      → `auto-fix`: fix under the existing mandate, no ping.
- R4  reversible + internal, no covering mandate
      → `mandate`: open the mandate-creation flow (scoped + expiring).
- R5  irreversible fix, or the fix touches external scope
      → `daily-report`: aggregated to the daily report; never auto-executed.
- R6  fail-closed: unknown fix class, unknown scope/reversibility value, or a
      finding missing required fields → `unknown`, routed to the daily
      report. Auto-fix is NEVER applied to an unknown classification.

Fail-closed is the default: anything the classifier cannot positively
recognize falls to the daily report, never to an automated action.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# Fix classes (stable string values, also persisted in the flow state).
SEV_SECURITY = "sev-security"
SEV_INCIDENT = "sev-incident"
AUTO_FIX = "auto-fix"
MANDATE = "mandate"
DAILY_REPORT = "daily-report"
UNKNOWN = "unknown"

# Classes that ping immediately (ping_policy keeps this list authoritative
# for routing, but the classifier must never emit one of these by accident).
IMMEDIATE_CLASSES = frozenset({SEV_SECURITY, SEV_INCIDENT})

# R1 trigger vocabulary (substring match, case-insensitive).
SECURITY_PATTERNS = (
    re.compile(r"fail(?:s|ed|ure)?[-_ ]?open", re.I),
    re.compile(r"auth[-_ ]?bypass", re.I),
    re.compile(r"secret[-_ ]?leak", re.I),
    re.compile(r"credential", re.I),
)

# R2 trigger vocabulary.
SEV_PATTERNS = (
    re.compile(r"platform (?:fully )?dead", re.I),
    re.compile(r"total outage", re.I),
    re.compile(r"data loss", re.I),
    re.compile(r"data[-_ ]?destroy", re.I),
)

KNOWN_SCOPES = frozenset({"internal", "external"})
KNOWN_REVERSIBILITY = frozenset({True, False})

REQUIRED_FIELDS = ("finding_id", "claim")


@dataclass
class CuratorFinding:
    """A curator finding submitted to the followup workflow."""
    finding_id: str
    claim: str
    reversible: Optional[bool] = None          # True | False | None (unknown)
    scope: Optional[str] = None                # internal | external | None
    covered_by_mandate: bool = False           # active mandate covers the fix
    tags: tuple = field(default_factory=tuple)


def _has_security_signal(finding: CuratorFinding) -> bool:
    blob = " ".join([finding.claim, *finding.tags])
    return any(p.search(blob) for p in SECURITY_PATTERNS)


def _has_sev_signal(finding: CuratorFinding) -> bool:
    blob = " ".join([finding.claim, *finding.tags])
    return any(p.search(blob) for p in SEV_PATTERNS)


def _is_unknown(finding: CuratorFinding) -> bool:
    """R6 fail-closed detection: missing required fields, or a
    reversibility/scope value the classifier does not positively recognize."""
    for name in REQUIRED_FIELDS:
        if not getattr(finding, name, None):
            return True
    if finding.reversible not in KNOWN_REVERSIBILITY:
        return True
    if finding.scope not in KNOWN_SCOPES:
        return True
    return False


def classify(finding: CuratorFinding) -> str:
    """Classify a curator finding into a fix class (R1-R6, first match)."""
    # R6 fail-closed gate runs FIRST: an unrecognizable finding is never
    # auto-fixed even if its claim happens to match a known pattern.
    if _is_unknown(finding):
        return UNKNOWN
    # R1: security breach — immediate ping, overrides everything else.
    if _has_security_signal(finding):
        return SEV_SECURITY
    # R2: SEV-level failure (platform dead / data loss) — immediate ping.
    if _has_sev_signal(finding):
        return SEV_INCIDENT
    # R3: reversible + internal + covering mandate → auto-fix.
    if finding.reversible and finding.scope == "internal" \
            and finding.covered_by_mandate:
        return AUTO_FIX
    # R4: reversible + internal, no mandate → mandate-creation flow.
    if finding.reversible and finding.scope == "internal":
        return MANDATE
    # R5: irreversible or external scope → daily report only.
    return DAILY_REPORT


def route_for(cls: str) -> str:
    """Where the class goes: 'immediate-ping' or 'daily-report'."""
    return "immediate-ping" if cls in IMMEDIATE_CLASSES else "daily-report"


def classify_finding(finding: CuratorFinding) -> dict:
    """Full classification record persisted alongside the flow state."""
    cls = classify(finding)
    return {
        "finding_id": finding.finding_id,
        "fix_class": cls,
        "route": route_for(cls),
        "auto_fixable": cls == AUTO_FIX,
    }
