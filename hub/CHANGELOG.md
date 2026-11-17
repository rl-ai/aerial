# Changelog

All notable changes to ticket-hub are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions are
milestones backfilled from the `hub` branch history (see git log for the
per-commit record).

## [Unreleased]

### Added
- **Seats / role assignment.** Event-sourced assign/release (`POST /tickets/{id}/assign|release`, `GET /seats`, `GET /tickets/{id}/assignment`) plus `hub seats/assign/release`; live seat health via the cluster SSOT, builder-exclusion for review/accept roles, and capacity-aware least-loaded routing. The Roles view shows the seat roster; the ticket drawer shows the current Holder.
- **Workflow templates.** A workflow-shape engine with seven repo templates (feature, bugfix, infra, spike, project, biz-opportunity, forge-cycle), a user-override directory, `{title}` substitution, and per-step seat assignment that reports failures but never aborts. `GET /templates`, `GET /templates/{name}`, `POST /templates/{name}/instantiate`; `hub templates/template/instantiate`.
- **Nightly backups + seat adapter.** A nightly job (`scripts/backup_offsite.sh`, destination `HUB_BACKUP_DEST`) snapshots the hub DB (sqlite `.backup`, seven daily copies) and mirrors immutable forge snapshot bundles; it skips cleanly when the target share is unmounted. `scripts/seat_remote.py` adapts a swarm seat to `remote-model-chat` (packet JSON on stdin, prompt file forwarded); `roles/swarm.json` holds the live worker roster (execution / measurement / triage).
- **Board signal-boost.** Collapse children by parent (persisted, ▾/▸ toggle, done/kids counter, Expand all), ticket-type chips inferred from workflow tags (product, bug, infra, research, project, system, biz) and filterable, and a Top-level-only noise filter. `GET /board` now also returns a `parents` map.
- **Graph edge vocabulary.** Ten semantic edge types added — about, derived-from, trained-on, introduces, implements, provides, seen-in, addresses, measured-by, same-as — defined once in `ticket_hub/model.py`; `related()` includes graph types and only parent_of/blocks are cycle-checked.
- **Ticket events read path.** `GET /tickets/{id}/events` returns the ticket's timeline (own events plus bound thread messages) and 404s for an unknown ticket.
- **Jobs page.** A job + run store sharing the hub DB and a `/jobs` API — register, run start/end, one-shot runs with dedupe, rollup by nearest feature/project ancestor, JSON/Markdown digest, and digest publish as a checkpoint event — plus a Jobs view in the UI sidebar (Projects | Features | Jobs | Digest).

### Changed
- `seat_remote.py` resolves the `remote-model-chat` path from `HUB_REMOTE_MODEL_CHAT` (default `~/.hermes/bin/remote-model-chat`).
- **Breaking:** `GET /tickets/{id}/plan` returns `blocked_by_owner` (was `blocked_by_ray`); the default home moved to
  `~/.ticket-hub` (set `TICKET_HUB_HOME` to keep an existing data directory).
- Neutral naming: Blocked-by-owner gate (`OWNER-GATED`, `blocked_by_owner`; extra legacy markers via
  `TICKET_HUB_EXTRA_GATE_MARKERS`), default home `$TICKET_HUB_HOME` (`~/.ticket-hub`), `HUB_ACTOR` for CLI actor defaults,
  legacy-ledger migration script removed.

### Accepted (v2 Mission Control — accepted by the owner 2026-10-12)
- Wiki: append-only pages (revisions like events), `/wiki*` API, `hub wiki` CLI, `[[WikiLink]]` renderer, seed corpus (repo docs + ops knowledge; re-import freshness; machine actor `seed-import`).
- Mission Control UI: Board | Graph | Wiki | Monitoring | Admin | Roles.
- `GET /admin/status` read-only aggregate (health, services, backups, maintenance, hygiene, counts); hygiene/maintenance emit machine status files.
- Roles registry (7 charters) served at `/roles`; `hub packet` renders the ticket-as-shared-packet.
- `swarm.py`: deterministic harness — CAS claims (claim-then-confirm over append-only log), locked-DB bounded retry, watermark dedupe, INERT without config.
- GLM build review: PASS (no blockers; 4 advisories logged to root ticket, 2 deferred to v2.1).


### Accepted
- v1 accepted by the owner 2026-10-12 — hub becomes the go-forward work OS (GLM review PASS-CONDITIONAL, all blockers closed).

## [1.1] - 2026-10-11 — Plan view
### Added
- `GET /tickets/{id}/plan` endpoint and `hub plan` CLI verb.
- Drawer Plan section: progress %, remaining items, blocked-by-owner.

## [1.0] - 2026-10-11 — CLI + outbound
### Added
- `hub` CLI (`hub_cli.py`) with stdlib `HubClient` (`hub_client.py`) and
  PATH-installable `bin/hub` shim: new/ls/start/done/plan verbs, `--json`
  machine output.
- Outbound poller daemon (`outbound.py`) with watermark dedupe, polish glue,
  and launchd installer (`deploy/install_outbound.sh`).
- Idempotent legacy-ledger → hub migration (one-time script, since retired):
  37 tickets, 33 edges, 236 events with timestamps, actors, and owner-gating
  preserved.
- Integration complete: live end-to-end (CLI verbs + outbound dedupe), 133
  board.

## [0.1] - 2026-10-10 — UI + gateway migration
### Added
- FastAPI endpoints per DESIGN.md + single-page UI (board / graph / drawer /
  updates).
- Chat adapters: matrix / discord / slack / fake, with translate
  template/polish passes; adapters run inert when no config/token present.
- Gateway wired to the real `StoreGateway`; canonical lowercase statuses;
  real-store e2e verified; full digest via lane-C template_pass.

## [0.1] - 2026-10-09 — API + UI (first cut)
### Added
- FastAPI + UI branch landing (`hub-api`): endpoints per DESIGN.md and
  standalone FakeStore tests.

## [0.0] - 2026-10-09 — Core
### Added
- v0 design contract: object graph, lineage, translate, adapters, UI.
- Object model + SQLite WAL store: append-only events, cycle-safe hierarchy.
- Lineage projection with tests.
