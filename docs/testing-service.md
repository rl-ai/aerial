# Aerial Testing Service — formalized practices (v1)

Every testing technique used building Aerial, formalized: what it
validates, when it runs, who owns it, and the PASS criteria. The service
is owned by the **test engineer** (`tester` profile); results land in
the daily report, the release checklist, and per-layer tickets.

## The nine practices

| # | Practice | Validates | Cadence | Trigger | PASS gate |
|---|---|---|---|---|---|
| 1 | **Unit suites** (hermetic, per component) | Component logic | Every land | `land.sh` | 100% green (currently 192+59+38+28) |
| 2 | **Stress harness** (25 labeled scenarios + DS adversarial batch, ground-truth scoring) | Agent quality: cause-finding, honesty, attack resistance | Per prompt/policy change (A/B) | Tuning loop | cause-finding ≥ baseline; honesty intact; A/B verdict at 95% CI |
| 3 | **Backtest** (historical replay) | Gates, promotion criteria, trend math vs REAL windows | Weekly + after every SEV | cron Sun 06:00 + post-SEV hook | All criteria PASS + incident-replay assertions (SEV pings would have fired) |
| 4 | **Accelerated validation** (virtual clock ×N + chaos day-types) | Window logic, cadence behavior, ≥3 day-types | Pre-release | Manual/gauntlet | All criteria PASS over 7 virtual days |
| 5 | **The Gauntlet** (5-layer, ticket-based) | Everything: units, stress, backtest, day-in-the-life, live smoke | **Pre-release (binding)** | `gauntlet.sh` | 5/5 layers PASS on the ticket trail |
| 6 | **Attack swarm** (DS brain + GLM/Qwen swarm, ~15 concurrent) | Security: auth, scoping, injection, fail-open, fuzzing | Monthly + pre-release + after new surfaces | Manual/cron | No CRITICAL/HIGH open; held-list documented |
| 7 | **Live smoke** (console, email, seats, monitor) | The delivery surface works now | Hourly (sre monitor) + on demand | cron | All checks ok |
| 8 | **Chaos day-types** (deterministic injection) | Novel day-types not yet lived | With #4 | validate_days | Day coverage ≥3 types |
| 9 | **Incident replay** (every real outage = a regression test) | The gates would have caught it | After every SEV/postmortem | ops-agent SEV hook | Replay assertions PASS |

## The escalation ladder (what each practice feeds)

```
practice fails → finding classified (followup R1–R6)
  → SEV/security: immediate ping + fix mandate
  → otherwise: daily aggregate
  → every SEV/postmortem: practice #9 replays the window
```

## Standing rules

- **No release without a green Gauntlet** (practice 5) — the ticket
  trail is the release evidence.
- **No prompt/policy change without an A/B** (practice 2) — the
  baseline is the validated reference; regressions revert.
- **Every real incident becomes a backtest assertion** (practice 9) —
  the gates get stronger with every outage.
- **Honest reporting**: unevaluable windows and harness flakes are
  marked, never papered over (the harness fail-loud rule).
- **Attack sessions hold no write authority** — the write gate
  rejecting them IS the test; DS never holds decision authority.

## Ownership

- **Test engineer** (`tester`): owns this document, the cadences, the
  PASS/FAIL reporting, and the practice backlog (new techniques as the
  platform grows — e.g. property-based fuzzing for the MCP protocol,
  load testing for the hub).
- **Ops agent**: executes practices 7 and 9 continuously.
- **Integrator**: blocks releases on practice 5 failures.
