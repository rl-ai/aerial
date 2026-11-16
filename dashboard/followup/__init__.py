"""Failure-followup package (Lane F).

Implements the failure-followup workflow:
- classifier.py: rules R1-R6 (decisions D1) classifying curator findings into
  fix classes; fail-closed on unknown.
- flow.py: state machine (D3) finding → classified → mandate created (scoped +
  expiring, via the Lane A mandate-creation path) → fix attempted →
  verification → closed-silently or escalated; transitions persisted.
- ping_policy.py: notification policy (D5) — immediate pings ONLY for
  SEV-level failures or security breaches; everything else aggregates to the
  daily report (Lane D engine ping path).
"""
