# Changelog

All notable changes to Aerial are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [1.0.1] - 2026-11-18

### Added

- `aerial verify` — a first-run check (`python3 bin/aerial_verify.py`)
  that prints a pass/fail table across hub, console, state dir, usage
  ledger, and model seats.
- pip packaging: the repo installs as `aerial-*` command entry points,
  with a `[hub]` extra pulling the hub's runtime dependencies.
- Docker compose files for the ticket hub, operator console, and ops
  agent, alongside per-component Dockerfiles.
- BYOM guide (`docs/BYOM.md`): how to bring your own models by editing
  the deployment profile, the VRAM seat registry, and the model-server /
  role config.

### Security

- A pre-publish leak scan (`bin/pii_scan.py`) ships with the repo: it
  checks tracked files for internal identifiers (hostnames, personal
  paths, ticket ids, keys) and fails the push on a hit.

## [1.0.0]

Initial pilot release of the Aerial operations layer for autonomous
agent fleets:

- Ticket hub (`hub/`): FastAPI service on `:8020` backed by SQLite —
  tickets as the shared context unit for swarms, with per-actor,
  deny-by-default token scoping.
- Ops agent (`dashboard/followup/runner.py`): a 5-minute poll loop that
  classifies failures R1–R6, runs governed safe-fix sessions, and
  re-verifies each fix against the exact symptom before closing it.
- Operator console (`dashboard/serve_console.py`): bearer-authed WSGI
  dashboard on `:8777` — daily board, exec report, sparklines, and an
  exception queue with approve/dismiss/escalate.
- Scheduler (`integration/scheduler.py`): cron-shaped runner chaining
  curator → lead → exec sessions over failures, with new behavior
  shadow-running under a promotion gate before going live.
- Fleet manager (`dashboard/vram_manager.py`): models resident on VRAM
  by default, demand displaces idle, and seat liveness is probed every
  cycle — dead seats become findings.
- BYOM model seats: bring your own models — local VRAM, self-hosted
  endpoints, or cloud APIs — configured per seat in
  `dashboard/vram_registry.json`.
- Verification command (`bin/aerial_verify.py`): one command that checks
  the hub, console, state dir, ledger, and model seats.
