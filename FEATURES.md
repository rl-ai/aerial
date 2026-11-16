# Features

What Aerial does, grouped by area, with the code that implements it.
Components marked **(internal deployment)** exist in the production
system but are not shipped in this repository.

## Operator surface

- **Operator console** — single-template WSGI dashboard on `127.0.0.1:8777`
  with bearer auth, sparklines, and decision actions; read-only unless a
  distinct write token is supplied. Code: `dashboard/serve_console.py`,
  `dashboard/console/`.
- **Daily board** — HTML board rendered from hub/ledger/scrape data,
  fail-closed with a per-request-refreshed static snapshot fallback.
  Code: `dashboard/render_daily.py`.
- **Exec report** — plain-text/markdown executive summary with three
  headlines for the board's brief panel. Code: `dashboard/exec_summary.py`,
  `dashboard/email_report.py`.
- **Email delivery** — SMTP delivery of the report; a hard no-op unless
  `email.recipient_email` is set in the profile. Code:
  `dashboard/email_delivery.py`.
- **Exception queue** — operator-review queue with mandate-scoped
  approve/dismiss/escalate actions and a queue policy file. Code:
  `dashboard/exception_queue/` (`queue.py`, `mandate_flow.py`,
  `queue-policy.yaml`).

## Automation

- **Ops agent (5-min poll)** — the always-on loop: poll → classify →
  act → verify → dedupe, over failed job runs, health anomalies, and
  seat liveness; runs `--once` for smoke tests. Code:
  `dashboard/followup/runner.py`.
- **Followup classifier (R1–R6)** — severity/route classification per
  decision D1: R1 security breach → immediate ping; R6 fail-closed for
  unknown fix classes. Code: `dashboard/followup/classifier.py`.
- **Safe-fix sessions** — mandate-scoped governed fix for reversible
  findings, with exact-symptom verification after the fix; verified
  fixes close silently, failures escalate. Code: `dashboard/followup/flow.py`,
  `dashboard/followup/runner.py`.
- **Retrigger** — rebuilds the exact cron re-run command for a job,
  preserving the payload after `--`. Code: `runner.retrigger_command()`
  in `dashboard/followup/runner.py`.
- **Shadow scheduler + hierarchy** — cron-shaped scheduler that chains
  curator → lead → exec agent sessions over a failed job and posts the
  brief to the hub. Code: `integration/scheduler.py`,
  `integration/run_shadow_hierarchy.py`.

## Model fleet

- **Seat liveness** — probes model seats listed in
  `dashboard/vram_registry.json`; gaps are reported as findings, not
  crashes. Code: `dashboard/followup/runner.py` (probes), registry:
  `dashboard/vram_registry.json`.
- **VRAM manager** — model-on-VRAM-by-default with demand/idle windows
  (15 min demand-displaceable, 30 min pressure-evictable) and idle
  eviction. Code: `dashboard/vram_manager.py`, CLI: `dashboard/vram-ctl`.
- **Seat swap** — scripted swap of a model seat to free or load VRAM.
  Code: `dashboard/seat-swap.sh`.
- **Idle-work broker** — routes spare seat capacity to queued work
  slots. Code: internal deployment (not in this repo).
- **Usage telemetry + 15-min report** — model-call ledger tailing and
  periodic usage reporting. Code: ledger tailer `dashboard/data_layer/model_calls_tailer.py`
  (ledger path `ledger/model-calls.jsonl` in the profile); the 15-min
  report rollup is internal deployment.

## Quality machinery

- **Gauntlet (5 layers)** — staged acceptance harness: L1 unit suites,
  L2 stress, L3 backtest, L4 day-in-the-life, L5 live smoke; wrapped by
  `gauntlet.sh` with an flock guard. Code: `dashboard/gauntlet.py`,
  `dashboard/gauntlet.sh`.
- **Chaos virtual days** — deterministic day-type injection (quiet,
  failure-storm, mixed, recovery, anomalous) into real stores, guarded
  by `AERIAL_TIME_ACCELERATION > 1` AND `AERIAL_CHAOS=1`. Code:
  `dashboard/chaos.py`, replay validator `dashboard/validate_days.py`.
- **Promotion gate** — board-state promotion checks (P-gates) before
  computed board state is accepted. Code: `dashboard/daily_board/promotion.py`.
- **BP-KB (best-practice knowledge base)** — ingested practice items
  with labeling, gap detection, and polling. Code: `dashboard/bpkb/`,
  seeds: `dashboard/bp_seeds.json`, `dashboard/bp_sources.json`.
- **Tier D practices** — extracted model-provider operational practices
  (e.g. Kimi) consumed by the fix playbooks. Code: `dashboard/tier_d.py`,
  `dashboard/tier_d/`.
- **Attack / stress harness** — adversarial scenario batches with
  ground-truth scoring for agent quality (cause-finding, honesty). Code:
  internal deployment; described in `docs/testing-service.md`.

## Integration

- **Scrape ingest bridge** — ingests scraped intel into the scrapes DB
  consumed by the board. Code: `dashboard/scrape_intel.py`
  (`paths.scrapes_db` in the profile).
- **Listening pipeline** — degree-expansion source discovery and SWOT
  cadence over the second-brain graph. Code: `gateway/secondbrain/listening.py`.
- **Gateway MCP + version snapshots** — MCP server exposing the
  second-brain vault with grant-pinned version snapshots (L2 snapshots
  serve the pinned alias; re-publish before serving material changes).
  Code: `gateway/secondbrain_mcp/`, `gateway/secondbrain/`
  (`curator.py`, `gatekeeper.py`, `grants.py`).

## Ops

- **Deployment profile + env overrides** — single source of config:
  env (`AERIAL_<SECTION>__<KEY>`) → `config/deployment.yaml` → generic
  defaults; one integration test asserts the stack runs with zero
  deployment-specific values. Code: `deployment_profile.py`,
  `config/deployment.yaml.example`, `integration/test_oss_generic.py`.
- **Hub token scoping** — actor×family×action scoped tokens on the hub,
  deny-by-default (Project #6 phase 1, in flight; tracked as S1/S2 in
  `docs/BUGLIST.md`). Code: hub side is internal deployment.
- **Write-token separation** — console decisions require a write token
  distinct from the read token; the read token POST is rejected (401).
  Code: `dashboard/serve_console.py --write-token-file`,
  `dashboard/console/actions.py`.
- **systemd units** — user units for console, ops agent, and shadow
  scheduler with `PYTHONPATH` preserved. Examples in
  [GETTING-STARTED.md](GETTING-STARTED.md); generator (internal):
  `bin/gen_systemd_units.py`.
- **Cold-start sandbox** — dockerized systemd (with chroot-lite
  fallback) boot of the whole stack with stub model seats and a
  first-boot assertion checklist. Code: internal deployment lane; the
  assertions map 1:1 to the troubleshooting table in
  [GETTING-STARTED.md](GETTING-STARTED.md).
