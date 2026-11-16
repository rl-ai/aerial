# Failure-Followup Workflow (Lane F)

Curator findings flow through a classify → mandate → fix → verify pipeline,
with a strict notification policy: immediate pings ONLY for SEV-level
failures or security breaches; everything else aggregates to the daily report.

## Flow (decision D3)

```
finding → classified → mandate-created → fix-attempted → verified
    → closed (SILENT — no ping) | escalated
```

Failure branches: mandate creation fails → `mandate-failed` → escalated;
fix execution raises → `fix-failed` → escalated; verification says not fixed →
escalated. Every transition is persisted in sqlite (`followups` + `transitions`
tables) with timestamps, so the audit trail shows which branch a finding took.

## Classifier (decision D1, rules R1–R6, first-match-wins)

| Rule | Condition | Fix class | Route |
|------|-----------|-----------|-------|
| R1 | security breach (fail-open / auth-bypass / secret-leak) | `sev-security` | immediate ping |
| R2 | SEV failure (platform fully dead / data loss) | `sev-incident` | immediate ping |
| R3 | reversible + internal + active covering mandate | `auto-fix` | daily report |
| R4 | reversible + internal, no mandate | `mandate` | daily report |
| R5 | irreversible or external scope | `daily-report` | daily report |
| R6 | unknown class / scope / reversibility, or missing fields | `unknown` | daily report (fail-closed) |

Fail-closed: anything the classifier cannot positively recognize is routed to
the daily report and NEVER auto-fixed — even if a mandate exists.

## Ping policy (decision D5)

`ping_policy.route()` is the single routing point:

- `sev-security` / `sev-incident` → ONE additive ping through the Lane D
  NotificationEngine's ping path (`_emit_additive`), so a SEV ping survives
  the daily demotable cap and is never demoted or coalesced away.
- Everything else (auto-fix, mandate, daily-report, unknown, and every
  escalation of a non-SEV finding) → the engine's brief — the single daily
  digest. No immediate ping, ever.

## Mandate integration (Lane A)

Mandates are created scoped (the finding's proposed action) and expiring
(default 24h) through `exception_queue.mandate_flow.MandateFlow`'s
mandate-creation transaction via `MandateFlowAdapter.create()` — one hub
transaction; any exception leaves nothing committed. The creator is injectable
so the flow is fully hermetic under test.

## Files

- `classifier.py` — rules R1–R6, fail-closed classification
- `flow.py` — state machine, transition persistence, mandate adapter
- `ping_policy.py` — immediate vs daily-aggregate routing
- `../tests/test_followup.py` — hermetic suite (classifier, state machine,
  failure paths, ping policy, Lane A integration)

## Status note

`design.md` was not present in the repo when this was implemented; the module
docstrings encode the decisions (D1, D3, D5) and rules (R1–R6) as built.
