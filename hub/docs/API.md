# ticket-hub HTTP API

Base URL: `http://127.0.0.1:8777` (loopback; port from `$TICKET_HUB_PORT`).
All request/response bodies are JSON unless noted. Interactive OpenAPI docs
at `/docs` (FastAPI default).

## Conventions

### Identifiers
Prefixed ULIDs, lexicographically sortable: `TK-…` ticket, `EV-…` event,
`ED-…` edge, `TB-…` thread binding.

### Actors
Any `owner`/`actor` field accepts a string or an object:

```json
{"name": "hermes", "id": "hermes", "family": "hermes", "kind": "agent"}
```

Strings are coerced (`"alice"` → `{"id": "alice", "name": "alice", "family":
"unknown", "kind": "agent"}`); `kind` ∈ `agent | human | system`.

### Error envelope
Every error (HTTP 4xx/5xx) is:

```json
{"error": {"code": "not_found", "message": "Ticket TK-9 nope not found"}}
```

Validation failures add a `details` array (FastAPI error list). Known
codes: `not_found` (404), `bad_request` (400 — unknown kind/type/platform,
illegal transition, empty patch), `cycle_rejected` (400 — parent_of/blocks
edge would cycle), `validation` (422), `unsupported` (501 — store does not
expose bindings), `store_unavailable` (503), `error` (generic).

### Enumerations
- Statuses (canonical lowercase, case-insensitive on input):
  `draft, open, running, blocked, review, done, cancelled`
- Edge types: hierarchy/blocking `parent_of, blocks, relates_to,
  duplicate_of`, plus the semantic graph types (see **Edge types** below)
- Event kinds: `created, updated, status_changed, comment, checkpoint,
  decision, approval_receipt, artifact, handoff, thread_message, link`
- Platforms: `matrix, discord, slack, fake` (`fake` is test-only)

## Ticket CRUD

### `POST /tickets` → 201
```json
{"title": "Fix flaky replay", "body": "see logs", "parent": "TK-…",
 "owner": "hermes", "tags": ["qa"], "families": ["hermes"],
 "priority": "high", "external_refs": {}}
```
`families` defaults to the owner's family when omitted (if known). New
tickets start at `draft`. Returns the Ticket.

Ticket shape:
```json
{"id": "TK-01ABC…", "title": "Fix flaky replay", "body": "see logs",
 "status": "draft", "priority": "medium",
 "owner": {"id": "hermes", "name": "hermes", "family": "hermes",
           "kind": "agent"},
 "tags": ["qa"], "families": ["hermes"], "external_refs": {},
 "artifacts": [], "created_at": "2026-10-11T02:00:00+00:00",
 "updated_at": "2026-10-11T02:00:00+00:00"}
```

### `GET /tickets`
Query params (all optional, AND-combined): `status` (case-insensitive;
unknown status → 400), `family`, `tag`, `q` (SQLite LIKE substring).
Returns a JSON array of Tickets.

### `GET /tickets/{id}`
Returns the Ticket, or 404 `not_found`.

### `PATCH /tickets/{id}`
```json
{"status": "running", "priority": "high", "title": null, "body": null,
 "actor": {"name": "glm", "family": "glm"}}
```
At least one of `status/priority/title/body` required (else 400). Status
changes go through the store's transition table — an illegal move (e.g.
`draft → done`) is 400 `bad_request: illegal status transition`.

## Events (append-only)

### `POST /tickets/{id}/events` → 201
```json
{"kind": "checkpoint", "actor": "hermes",
 "payload": {"remaining": ["wire the exporter", "backfill dry-run"]}}
```
Unknown `kind` → 400. Events are **immutable**: there is no update or
delete endpoint, and DB triggers enforce append-only at the store. Returns:
```json
{"id": "EV-01ABC…", "ticket_id": "TK-01ABC…",
 "ts": "2026-10-11T02:05:00+00:00", "seq": 7,
 "actor": {"id": "hermes", "name": "hermes", "family": "hermes",
           "kind": "agent"},
 "kind": "checkpoint", "payload": {"remaining": ["wire the exporter"]}}
```

Conventions inside payloads:
- `comment` → `{"text": "…"}`
- `checkpoint` → optional `remaining: [str]` list, surfaced by `/plan`
  under `remaining_notes`
- `OWNER-GATED` → `{"marker": "OWNER-GATED", "note": "…"}` flags an item as
  **Blocked by owner** in `/plan` (or set status `blocked`)

## Edges

### `POST /tickets/{id}/edges` → 201
```json
{"type": "parent_of", "dst": "TK-01XYZ…", "meta": {}}
```
Both endpoints must exist (404 otherwise); unknown type → 400; a
`parent_of`/`blocks` edge that would create a cycle → 400
`cycle_rejected`. Returns `{"id": "ED-…", "src": "TK-…", "dst": "TK-…",
"type": "parent_of", "meta": {}}`.

## Projections

### `GET /tickets/{id}/lineage`
Everything about this ticket's neighborhood:
```json
{"ticket": { … },
 "ancestry": [ …root→parent Tickets… ],
 "descendants": [ …all child-subtree Tickets… ],
 "related": [ …edges touching this ticket… ],
 "tree": {"ticket": { … }, "children": [ {"ticket": …, "children": []} ]},
 "events": [ …all events in the subtree, sorted by (ts, id)… ]}
```

### `GET /tickets/{id}/plan`
The three-questions view for a project ticket:
```json
{"ticket": { … },
 "summary": "body of the ticket",
 "progress": {"done": 3, "open": 2, "total": 5, "percent": 60},
 "remaining_items": [{"id": "TK-…", "title": "…", "status": "open",
                      "owner": "hermes"}],
 "remaining_notes": ["wire the exporter"],
 "blocked_by_owner": [{"id": "TK-…", "title": "…", "status": "blocked"}],
 "recent_activity": [ …last 8 events on this ticket… ]}
```
- `progress.percent` = done descendants / (descendants − cancelled); a leaf
  with no work items reports 100 if done else 0.
- `remaining_notes` come from the newest `checkpoint` event carrying
  `payload.remaining` anywhere in the subtree.
- (Renamed from `blocked_by_ray` in this release; no alias is served.)
- `blocked_by_owner` = descendants with status `blocked` **or** an
  `{"marker": "OWNER-GATED"}` event.

### `GET /tickets/{id}/digest?format=json|md&full=false`
Reader-facing translation of the subtree's events. Every bullet cites the
event ids it paraphrases (no-fabrication property, test-enforced).
`full=true` uses the canonical translation layer (all event kinds); the
default is the compact status/artifact/decision/approval pass.
```json
{"project": "TK-01ROOT…", "project_title": "Ship the exporter",
 "ticket": "TK-01ABC…", "updated_at": "…",
 "bullets": [{"text": "advanced to Running", "event_ids": ["EV-01…"]}]}
```
`format=md` returns `text/markdown`:
```markdown
# Updates — Ship the exporter
- advanced to Running (EV-01ABC…)
```

### `GET /board`
```json
{"columns": {"draft": […], "open": […], "running": […], "blocked": […],
             "review": […], "done": […], "cancelled": […]},
 "child_counts": {"TK-01ABC…": 3}}
```
All seven status keys are always present (possibly empty arrays).

## Threads / chat bridges

### `POST /tickets/{id}/bind` → 201
```json
{"platform": "matrix", "room_id": "!room:example.invalid",
 "thread_id": "$threadroot", "root_msg_id": "$rootmsg"}
```
Unknown platform → 400. Returns the ThreadBinding (`id: TB-…`).

### `GET /tickets/{id}/bindings`
Array of ThreadBindings for the ticket. 501 `unsupported` if the store
does not expose bindings.

### `POST /adapters/{platform}/inbound`
Called by chat bridges when a reply lands in a bound thread:
```json
{"room_id": "!room:example.invalid", "thread_id": "$threadroot",
 "root_msg_id": "$reply", "actor": {"name": "alice", "kind": "human"},
 "text": "ship it"}
```
Resolves the binding (404 `not_found` if none) and appends a
`thread_message` event to the bound ticket. Returns
`{"event": { … }, "posted": false}` (`posted` is the reply-post-back flag;
inert in v1).

## UI

- `GET /` serves the SPA (Board / Graph / Ticket drawer / Updates).
- Static assets under `/ui`.

## Jobs

Jobs and agent runs live in the same DB file as the hub (own connection, WAL
safe) but not the Ticket tables. A **job** is the registered definition; a
**run** is one execution of it. The UI surfaces the same data in a Jobs view
(Projects | Features | Jobs | Digest).

### A job vs a run
A job (`jobs`) is `job_id`, `name`, `node`, `kind`, `schedule`,
`interval_s`, `ticket_id`, `command`, `enabled`, plus `created_at` /
`updated_at`. A run (`job_runs`) is `run_id` (`run_…`), `job_id`, `node`,
`started_at`, optional `ended_at`, `status`, `exit_code`, `duration_s`,
`summary`, `log_tail`, `meta`, `pid`. Timestamps are UTC ISO-8601 with `Z`.
`summary` is capped at 500 chars; `log_tail` at 4000, keeping the **end**
(the most recent output). Ending a run sets `ended_at` and `duration_s`.

### Run statuses
`running, ok, failed, timeout, skipped` (`ok/failed/timeout/skipped` are
final). Only a final status may end a run; `meta` is merged (existing keys
kept, new ones added) and `exit_code` coalesces (a later null leaves the
stored value). Reporters that only learn the outcome at the end use a
**one-shot record** (a final `status` on run creation) rather than start/end.

### Derived job status
The status shown for a job is derived from its latest run — a health label,
not a stored column:

- `running` — the latest run is still going, within its horizon.
- `stuck` — that run is past its horizon: `max(2 × interval_s, 6 h)` when
  `interval_s` is set, else `6 h`.
- `overdue` — nothing is running, `interval_s` is set, and more than
  `interval_s + 900 s` has passed since the last run's `started_at` (or
  since `created_at` when there is no run).
- `unknown` — no run and no interval to be overdue against.
- `failed` — the latest run ended `failed` or `timeout`.
- `ok` — the latest run ended `ok` (or `skipped`).

Stuck/running is checked first, so a run past its horizon reads `stuck` even
when the job is also overdue. Rollups and digests fold many jobs to the worst
status: `stuck > failed > overdue > running > ok > unknown`.

### One-shot records and dedupe
A one-shot run may carry `meta.dedupe_key`. Recording the same key again
returns the **earliest** matching run instead of inserting a duplicate, so a
retried reporter does not inflate history. Start/end reporters leave it out.

### Binding runs to tickets
Give a job a `ticket_id` pointing at a ticket tagged `feature` or `project`
(the nearest self-or-ancestor wins). Rollups then group runs under that
ancestor; jobs with no ticket, or none tagged, land in `(unassigned)`.

### Routes

`GET /jobs`
Query: `node` (optional; one node's jobs). Returns the enabled-job views:
each job plus `last_run`, derived `status`, `next_due` (`started_at +
interval_s`, or null), `runs_24h`, `fails_24h`.

```json
[{"job_id": "backup-nightly", "name": "backup-nightly", "node": "node-a",
  "kind": "cron", "interval_s": 86400, "ticket_id": "TK-EXAMPLE",
  "enabled": 1, "status": "ok", "next_due": "2026-10-29T02:00:00Z",
  "last_run": {"run_id": "run_01ABC…", "status": "ok",
               "started_at": "2026-10-28T02:00:00Z", "summary": "ok"},
  "runs_24h": 1, "fails_24h": 0}]
```

`GET /jobs/rollup`
Query: `level` (`feature`, default; or `project`), `since_h` (hours back,
default `24.0`). An unknown level → 422 `bad_level`. Returns group rows,
ordered by group ticket id, unassigned last:

```json
[{"id": "TK-EXAMPLE", "title": "Ship the exporter",
  "jobs": 3, "runs": 5, "ok": 4, "failed": 1, "overdue": 0, "stuck": 1,
  "success_rate": 0.8, "status": "failed",
  "last_failure": {"job_id": "backup-nightly", "run_id": "run_01ABC…",
                   "ended_at": "2026-10-28T02:05:00Z",
                   "summary": "target share unmounted"}}]
```

`success_rate` is `ok / (ok + failed)`, or null with no finished runs.
`last_failure` is the newest failure in the window.

`GET /jobs/digest`
Query: `since_h` (default `24.0`), `format` (`json`, default; `md` → a
`text/markdown` body). JSON:

```json
{"generated_at": "2026-10-28T09:00:00Z",
 "since": "2026-10-27T09:00:00Z",
 "coverage": {"registered": 4, "reporting": 3, "pct": 75.0},
 "totals": {"jobs": 5, "runs": 8, "ok": 6, "failed": 1,
            "overdue": 1, "stuck": 1},
 "problems": [{"job_id": "backup-nightly", "name": "backup-nightly",
              "node": "node-a", "status": "failed",
              "ticket_id": "TK-EXAMPLE",
              "last_summary": "target share unmounted"}],
 "projects": [{"id": "TK-EXAMPLE", "title": "Ship the exporter",
              "status": "failed",
              "features": [{"id": "TK-EXAMPLE", "title": "Exporter",
                            "status": "failed",
                            "jobs": [{"job_id": "backup-nightly",
                                      "name": "backup-nightly",
                                      "node": "node-a",
                                      "status": "failed", "runs": 2,
                                      "failed": 1,
                                      "last_run_at": "2026-10-28T02:00:00Z"}]}]}]}
```

Coverage counts only **scheduled** jobs (`interval_s` set): `reporting` are
those with a run in the window and `pct` is `reporting / registered`.
Totals cover all jobs and window runs. `problems` lists stuck/failed/overdue
jobs, worst first. The Markdown rendering is a short header, totals line, a
problem list and one section per project, truncated under 4000 chars with
`… N more`.

`POST /jobs/digest/publish`
Query: `ticket_id` (optional), `since_h` (default `24.0`). `ticket_id`
defaults to `$JOBS_DIGEST_TICKET`; with neither set → 422 `no_ticket`; an
unknown ticket → 404 `not_found`. Appends a `checkpoint` event to that
ticket — actor `jobs-digest` (family `system`), payload `{"type":
"jobs_digest", "coverage": …, "totals": …, "problems": …(first 20),
"markdown": "…"}` — and returns:

```json
{"published": true, "ticket_id": "TK-EXAMPLE"}
```

`PUT /jobs/{job_id}`
Create or update. Body fields (optional on update): `name`, `node`, `kind`
(default `cron`), `schedule`, `interval_s`, `ticket_id`, `command`,
`enabled`. `name` defaults to the `job_id`, `created_at` is kept across
updates and `enabled` is stored as 0/1. An unknown field → 422
`unknown_fields`. Returns the job.

`GET /jobs/{job_id}`
Returns the job plus `runs` (latest 50, newest first); 404 `not_found`.

`POST /jobs/{job_id}/runs` → 201
Body: the job fields above (a run may (re)define its job) plus `node`,
`pid`, `meta`, `started_at`, `ended_at`, `exit_code`, `summary`, `log_tail`
and `status`. A final `status` makes it a one-shot record (see dedupe);
otherwise (or `running`) it starts a run and auto-registers the job when
missing (`name` defaults to the job id). Returns the run.

```json
{"run_id": "run_01ABC…", "job_id": "backup-nightly", "node": "node-a",
 "started_at": "2026-10-28T02:00:00Z", "ended_at": null,
 "status": "running", "exit_code": null, "duration_s": null,
 "summary": "", "log_tail": "", "meta": {}, "pid": 1234}
```

`PATCH /jobs/runs/{run_id}`
Body: `status` (required, a final status), `exit_code`, `summary`,
`log_tail`, `meta`. A non-final status → 422 `bad_status`; an unknown run →
404 `not_found`. Returns the updated run.

## Templates

Templates are reusable workflow shapes stored as JSON. A template describes a subtree: a root ticket plus steps, each optionally nested one level. The JSON shape is `name`, `description`, `tags`, `priority` (applied to the root), and `steps`; each step has `title`, `role`, `tags`, and may carry `body`, `owner`, `priority`, and nested `steps`. `{title}` in a step title is replaced by the instantiation title. Shipped templates: `biz-opportunity`, `bugfix`, `feature`, `forge-cycle`, `infra`, `project`, `spike`. Repo templates are canonical; a local templates directory may add or override them (later wins).

### `GET /templates` → 200
```json
{"templates": [
  {"name": "feature", "description": "Full product workflow for a user-visible feature.", "tags": ["feature"], "steps": 6, "source": "repo"}
]}
```

### `GET /templates/{name}` → 200
```json
{"name": "feature", "description": "Full product workflow for a user-visible feature.", "tags": ["feature"], "priority": "medium", "steps": [{"title": "PMF check: {title}", "role": "pmf", "tags": ["pmf", "gate"], "body": "Name the user and trigger scenario."}]}
```
Missing or invalid name → 404 `bad_name` or `not_found`.

### `POST /templates/{name}/instantiate` → 201
Body fields: `title` required; `parent`, `actor` optional; `assign` defaults to `true`.
```json
{"title": "Add export", "parent": "TK-EXAMPLE", "actor": "agent-a", "assign": true}
```
```json
{"root": {"id": "TK-EXAMPLE", "title": "Add export"}, "tree": [{"id": "TK-EXAMPLE", "title": "PMF check: Add export", "assigned": "seat-a@pmf", "assign_error": null, "children": []}]}
```
Missing or invalid name → 404 `bad_name` or `not_found`. Assignment failures never abort instantiation; they are reported per step.

## Seats

Seat assignment routes models around tickets. Assignment and release are recorded as append-only `handoff` events: assignment payload has `action: "assigned"` with `role` and `seat`; release payload has `action: "released"`. The latest such event per ticket is the current holder. Contributors are seat names seen as actors on any event of the ticket. Load counts the tickets a seat currently holds that are not `done` or `cancelled`. Seat health comes from an optional external probe command set by env `TICKET_HUB_SEATS_CMD`; seats not reported are treated as up. For review and accept roles, builder-exclusion keeps a seat with history on the ticket from reviewing it.

### `GET /seats` → 200
```json
{"roles": {"pmf": {"mission": "…", "seats": ["seat-a", "seat-b"], "authority": "review", "max_concurrent": 1}}, "seats": {"seat-a": {"roles": ["pmf"], "load": 0, "health": "unknown"}}, "assignments": []}
```

### `POST /tickets/{id}/assign` → 201
Body fields: `role` required; `seat`, `actor` optional.
```json
{"role": "pmf", "seat": "seat-a", "actor": "agent-a"}
```
```json
{"ticket_id": "TK-EXAMPLE", "seat": "seat-a", "role": "pmf", "authority": "review", "event_id": "EV-01ABC"}
```
Refused assignment → 409 with the `SeatError` code, such as `unknown_role`, `seat_not_in_role`, `builder_exclusion`, `no_healthy_seat`, or `at_capacity`.

### `POST /tickets/{id}/release` → 200
```json
{"actor": "agent-a"}
```
```json
{"ticket_id": "TK-EXAMPLE", "released": {"seat": "seat-a", "role": "pmf", "event_id": "EV-01ABC", "ts": "2026-10-11T02:05:00+00:00"}, "event_id": "EV-01DEF"}
```

### `GET /tickets/{id}/assignment` → 200
```json
{"ticket_id": "TK-EXAMPLE", "holder": {"seat": "seat-a", "role": "pmf", "event_id": "EV-01ABC", "ts": "2026-10-11T02:05:00+00:00"}, "contributors": ["seat-a"]}
```

## Ticket events

`GET /tickets/{id}/events` returns the ticket's timeline. Events are append-only. The valid `kind` values are: `created`, `updated`, `status_changed`, `comment`, `checkpoint`, `decision`, `approval_receipt`, `artifact`, `handoff`, `thread_message`, `link`.

### `GET /tickets/{id}/events` → 200
```json
[{"id": "EV-01ABC", "ticket_id": "TK-EXAMPLE", "ts": "2026-10-11T02:05:00+00:00", "seq": 7, "actor": {"id": "agent-a", "name": "agent-a", "family": "agent-a", "kind": "agent"}, "kind": "checkpoint", "payload": {"remaining": ["wire the exporter"]}}]
```
Missing ticket → 404.

## Edge types

Edge types are the typed semantic links between tickets. `parent_of` and `blocks` are cycle-checked; graph edge types are not. The table lists every value in `EDGE_TYPES`.

| Type | Meaning | Cycle-checked |
| --- | --- | --- |
| `parent_of` | hierarchy: parent ticket to child | yes |
| `blocks` | blocking dependency | yes |
| `relates_to` | loose association | no |
| `duplicate_of` | duplicate marker | no |
| `about` | this ticket is about the target (a topic, entity or system) | no |
| `derived-from` | this ticket's work or artifact is derived from the target | no |
| `trained-on` | a model or result was trained on the target dataset | no |
| `introduces` | this change introduces the target (feature, concept, risk) | no |
| `implements` | this ticket implements the target spec or feature | no |
| `provides` | this component provides the target capability | no |
| `seen-in` | the target is where this issue or pattern was observed | no |
| `addresses` | this ticket addresses the target problem or finding | no |
| `measured-by` | the target metric or benchmark measures this one | no |
| `same-as` | both tickets describe the same thing (graph identity) | no |

## Notes

- One writer: SQLite WAL, single process. Do not run two servers against
  the same `hub.db`.
- The store path is `$TICKET_HUB_DB` (default
  `~/.ticket-hub/hub.db`).
