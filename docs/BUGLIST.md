# Aerial — open bug & issue list (2026-11-14)

Consolidated from: the attack swarm (TK-EXAMPLE-1), the
gap review (GAPS-20261112.md), the release checklist, ops-agent
escalations, and the gauntlet runs. Fixed items stay at the bottom for
the record.

## OPEN — security

| # | Sev | Bug | Fix path |
|---|---|---|---|
| S1 | CRITICAL | Hub does not scope tokens: any valid bearer can create tickets and pass PATCH gates (attack-swarm) | **Project #6 phase 1** — scoped tokens actor×family×action, deny-by-default. Kimi phase-1 brief in flight |
| S2 | HIGH | Hub PATCH gate accepts the read-only bearer (same root as S1) | P6 phase 1 |
| S3 | LOW | Bridge `validate_source_entry` accepts 100KB targets and shell-metachar targets (no length/charset cap) | small lane: length + charset validation |
| S4 | LOW | Hub has no DELETE route — attack-created tickets can only be cancelled, not removed | P6 design note |

## OPEN — functional

| # | Sev | Bug | Fix path |
|---|---|---|---|
| F1 | MED | Stress-harness collection flake: 1/25 runs lost findings under parallel load (s19, intermittent) | harness pass: retry or mount-scan on collection |
| F2 | LOW | Agent transcripts occasionally hallucinate ticket ids (verified no real mutation; the report layer should verify ids before printing) | report-layer verification |
| F3 | LOW | Hub data gap 2026-11-08/09 + scheduler.log 2-day retention → backtest marks those days unevaluable | retention bump + gap backfill |

## FIXED (record)

| Sev | Bug | Fixed |
|---|---|---|
| HIGH | Console write gate accepted the read-only bearer (shared default token) | ✅ 2026-11-14 — distinct ops-write token; read token POST → 401 verified |
| HIGH | roles-runner/roles-supervisor hub 400/401/404 cluster | ✅ lane 1 (a9234b3) |
| MED | curator-poll 300s timeouts / "(no summary)" cluster | ✅ lane 2 (incremental rating, 8e0c4c2) |
| MED | Daily board: duplicate sections, misleading n/a, wrong dogfood path | ✅ lane 3 (01c3ebd) |
| MED | Bridge refused unlabeled raw items entirely (40 errors, 0 ingested) | ✅ missing→personal default (015f4b8) |
| MED | Shadow scheduler died in reboot, stayed dead ~7h | ✅ systemd unit (aerial-shadow-scheduler) |
| SEV | Kimi seat down post-reboot (bare process + incomplete model download) | ✅ karlex 17-shard variant + systemd unit |
| MED | board_render monitor budget vs daily cadence | ✅ split budgets (bridge 90m, board 26h) |
| MED | ds-writer truncation at 16k tokens | ✅ 32768 |
| MED | Duplicate-run 3× spend on one incident | ✅ dedupe guard (1h window) |
| HIGH | Console exec-summary 500 (stale lineage overwrite) | ✅ lineage merge + pathlib restore |
