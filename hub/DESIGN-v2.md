# ticket-hub v2 — Mission Control

Goal: the hub becomes the single central place for the whole operation:
work (tickets/board), knowledge (wiki), visibility (monitoring), control
(admin), and people/machines (roles + swarms). Everything built by the
operation is documented here, wiki-style.

## Pillars

### 1. Wiki (knowledge layer, in the hub)
- `wiki_pages` table, append-only revisions (page_id, slug, rev, title,
  body_md, tags, actor, ts) — same durability ethos as events. Current
  page = max(rev). No UPDATE/DELETE.
- API: `GET /wiki` (index), `GET /wiki/{slug}`, `POST /wiki/{slug}` (new
  revision), `GET /wiki-search?q=` (title/body/tags, substring, v1).
- CLI: `hub wiki ls|get|set|search`.
- Markdown rendering with `[[Wiki Links]]` → internal page links,
  relative repo links stay valid; categories sidebar; page history view.
- Seed corpus: import repo docs (README, docs/*, DESIGN, DESIGN-v2) +
  operation knowledge: forge loop, cluster seats, sandbox/snapshots,
  ops runbook, glossary, decision log index. Each seed page records its
  source path in frontmatter.

### 2. Web app — Mission Control shell
Top nav: **Board | Graph | Wiki | Monitoring | Admin | Roles**.
- Wiki view: page renderer, search box, sidebar by category, history.
- Monitoring: hub health (health-status.json), launchd service states
  (`launchctl list com.ticket-hub.*`), backup freshness (newest snapshot
  age vs 26h budget), last maintenance run summary, seat health snapshot
  file (written by maintenance job from remote-model-health).
- Admin: DB counts (tickets/edges/events/wiki), last hygiene result,
  exact load/unload/install commands for every LaunchAgent (display-only
  — host mutations stay the owner's), log tail viewer (last 200 lines, allow-
  listed paths).
- `GET /admin/status` returns all of the above as JSON (one call per
  page); reads local state only, no shell-outs beyond `launchctl list`.

### 3. Roles & swarms (quality system)
Role registry `roles/registry.json`, one file per role charter:
- **design** — UX/interaction quality bar for anything user-facing.
- **pmf** — product-market-fit challenge: who wants this, why now,
  what would make the owner stop using it.
- **code-design** — architecture review: contracts, edges, failure
  modes; distinct from execution.
- **execution** — builders; the swarm's bulk. Pull assigned tickets,
  work in sandbox lanes, report with evidence.
- **measurement** — defines + tracks the metrics that prove a feature
  works (test counts, latency, adoption); kills vanity claims.
- **product-owner** — planner/reviewer: sequences scope, accepts or
  returns work per ticket (GLM seat is its default engine).
- **orchestrator** — me: coordinates tickets, spawns lanes, keeps the
  ticket as the shared packet. Never reviews its own output.

Ticket = shared packet: `hub packet <id>` renders the complete context
for any agent — ticket body, ancestor chain, plan (children + % +
blocked), recent events, linked wiki pages. One command, one artifact.

`swarm.py` (deterministic harness, stdlib):
- `swarm.py run --role execution --claim-tag <tag> --workers N`
- each worker: claim a `running` ticket tagged for it (CAS via
  `blocked→running` event marker), build packet, call its seat
  (remote-model-chat SSOT), post result as events/comments; on failure
  set blocked with reason. Model never owns state — hub does.
- `swarm.py status` — live worker/ticket map. Inert without config.

### 4. Fit services (low effort, high glue value)
- Weekly digest job: maintenance agent can emit `hub digest` for active
  roots to the bound thread + wiki "Weekly" page (append revision).
- Inbox view: `hub ls --status blocked` + blocked_by_owner across roots,
  first thing on the Mission Control landing.

## Non-negotiables (unchanged)
loopback-only; secrets never in hub/repo/prompts; append-only; installers
and admin print commands, never run them; promotion blocked by owner;
reviewer ≠ author; GLM plans/reviews at interval boundaries, DS-style
critic advisory where available.

## Build order (lanes, parallel)
1. WIKI lane: table + API + CLI + seed import (+ tests).
2. UI lane: Mission Control shell + wiki/monitoring/admin/roles views
   against the frozen API contract below (+ tests for new endpoints via
   TestClient on stub data).
3. SWARM lane: registry + packet builder + swarm.py + tests (FakeSeat,
   FakeAdapter patterns) (+ roles page data).
API contract frozen before dispatch (see sections above). Integration:
orchestrator merges, runs full board + hygiene, GLM review, push both
remotes. Acceptance stays blocked by owner.
