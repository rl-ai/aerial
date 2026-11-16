# Technical overview

Aerial is a set of cooperating local services for running a small AI
cluster with an automated ops loop. This document is the 20-minute
tour.

## Components

### Ticket hub (gateway/)
The hub is the system of record: tickets, job_runs, mandates, health
status. `gateway/` contains the second-brain gateway — an MCP server
(`secondbrain_mcp/`) and a bridge (`bridge.py`) that mediates between
agent sessions and the hub, enforcing sensitivities and actor
permissions. Everything else talks to the hub over HTTP with a bearer
token.

### Followup runner (dashboard/followup/runner.py)
The always-on ops agent: **poll → classify → act → verify → dedupe**.

- Polls failed `job_runs`, `health-status.json` anomalies, and model
  seat liveness (seats loaded from `dashboard/vram_registry.json`).
- Classifies each finding (R1–R6) via `followup/classifier.py`.
- Acts per severity: sev-incident/security ping immediately and run a
  mandate-scoped fix session (a governed one-shot model call in a per-incident
  workspace with TASK.md, evidence, and a fix playbook).
- Verifies the exact symptom after auto-fix; verified fixes close
  silently, failures escalate.
- Dedupes: a finding never auto-fixes twice in 24h.

All external effects are injectable — the loop is hermetic under test.

### Daily board + exec report (dashboard/render_daily.py, email_report.py, email_delivery.py)
Renders the daily board HTML from hub/ledger/scrape data, builds a
plain-text/markdown exec report, and emails it via the profile's SMTP
settings (disabled unless `email.recipient_email` is set).

### Operator console (dashboard/console/, serve_console.py)
A single-template WSGI console over the same data: sparklines, actions,
exception queue. Serves on 127.0.0.1:8777 by default.

### Chaos / validation (dashboard/chaos.py, validate_days.py)
Deterministic day-type injection into real stores, guarded by
`AERIAL_TIME_ACCELERATION > 1` AND `AERIAL_CHAOS=1`. `validate_days.py`
replays known incidents against the board and asserts the trend math.

### Gauntlet (dashboard/gauntlet.py)
A staged acceptance harness (L1–L6): unit, cross-module, live smoke,
frontier-signal lanes. Wrapped by `dashboard/gauntlet.sh` with a flock
guard.

### Integration harness (integration/)
- `scheduler.py` — the cron-shaped scheduler for virtual days.
- `run_shadow_hierarchy.py` — chains curator → lead → exec agent
  sessions over a failed job; posts the brief to the hub.
- `qualify_real.py` — the big qualification harness: staged real-system
  checks with token-reference discipline and emit-verify-report flow.
- `test_hermetic_cycle.py` and friends — hermetic end-to-end tests.

## Configuration

`deployment_profile.py` is the single source of deployment values:
profile file → env overrides → generic defaults. Code never hardcodes
a host, node name, recipient, or personal path. Generic defaults use
`~/.aerial/` as the state root with a documented subpath per key
(`GENERIC_SUBPATHS`).

## Testing philosophy

- Hermetic by default: tests inject fakes for probes, pings, mandates.
- Conformance checkpoints (`docs/pipeline-checkpoints.md`, PC-1..PC-7)
  are verified in code (`dashboard/conformance.py`), not by vibes.
- One integration test (`integration/test_oss_generic.py`) asserts the
  whole stack imports, loads its profile, and runs with zero
  deployment-specific values.
