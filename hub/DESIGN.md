# ticket-hub — centralized object-oriented ticket graph

Status: v0 build (prod-rough by design; forge will harden).
Owner: the owner. Acceptance: the owner only.

## Purpose
A centralized, object-oriented **context object per request** that multiple agents
(within and across families) read/write, with:
- graph model: hierarchy (parent/child) + typed edges
- immutable event history = lineage for every request/project
- a product-grade UI: board, hierarchy, lineage, and **translated updates**
  (work events → readable, marketing-quality digests)
- chat bridges: Element (Matrix), Discord, Slack — ticket ↔ thread/subthread

## Non-goals (v0)
- No auth/multi-tenant (local trusted network; Matrix self-host keeps sensitive threads private)
- No sync/CRDT — single writer store, agents go through the API
- No migration of trading-platform tickets.db (coexists; can import later)

## Object model (package `ticket_hub`)

```
Actor        id, name, family (str, e.g. "hermes", "forge", "alice"), kind (agent|human|system)
Ticket       id (TK-<ulid>), title, body, status, priority, owner: Actor,
             tags, families[], external_refs{}, artifacts[], created_at, updated_at
Status       Draft → Open → Running → Blocked → Review → Done, Cancelled
Edge         id, src, dst, type (parent_of | blocks | relates_to | duplicate_of), meta
Event        id, ticket_id, ts, actor, kind, payload
             kinds: created, updated, status_changed, comment, checkpoint,
                    decision, approval_receipt, artifact, handoff, thread_message, link
ThreadBinding ticket_id, platform (matrix|discord|slack), room_id, thread_id, root_msg_id
```

Store: SQLite WAL, append-only events, `updated_at`/status denormalized on Ticket row.
DB path: `~/.ticket-hub/hub.db` (local, never iCloud).
Graph queries: `children(t)`, `ancestry(t)`, `descendants(t)`, `related(t)`; cycles rejected.

### Projections (read models)
- `board` — tickets grouped by status
- `lineage(t)` — ancestry chain + descendant tree + full event chain
- `timeline(t)` — events merged with linked thread messages
- `digest(t | project)` — translated updates (see translation)

## Translation layer (`translate.py`)
Deterministic-first, model-polish optional:
1. Group events into update blocks (per day or per N events).
2. Template pass maps event kinds to reader-facing sentences
   (status_changed → "advanced to Review"; artifact → "produced <name>";
   decision/approval_receipt → "decision recorded / approved by the owner";
   QUEUED FINDING / KNOWN-FAILS payloads → "known issue logged").
3. Optional polish: one GLM call per digest ("product update voice, ≤3 bullets,
   no internal jargon"); template output is the fallback and the truth source.
Never fabricate: digest only paraphrases events that exist; each bullet cites event ids.

## HTTP API (FastAPI, `api.py`, default port 8777)
- `POST /tickets` (title, body, parent?, owner, tags)
- `GET /tickets?status=&family=&tag=&q=`  · `GET /tickets/{id}`
- `PATCH /tickets/{id}` (status/priority/title/body → emits events)
- `POST /tickets/{id}/events` (kind, actor, payload)
- `POST /tickets/{id}/edges` (type, dst)
- `GET /tickets/{id}/lineage` · `GET /tickets/{id}/digest?format=json|md`
- `GET /board`
- `POST /tickets/{id}/bind` (platform, room_id, thread_id, root_msg_id)
- `POST /adapters/{platform}/inbound` (chat message → thread_message event; reply posts back)

## Chat bridges (`adapters/`)
Contract: `ChatAdapter.post_update(ticket, digest) -> msg_id`,
`on_inbound(platform event) -> Event(thread_message)`, ticket↔thread mapping in ThreadBinding.
- Ticket → chat thread root; **child ticket → subthread reply** under parent's root
  (Discord: thread per ticket; Slack: thread in channel; Matrix: thread).
- Outbound: on status_changed/decision/approval_receipt/artifact → post digest bullet.
- Inbound replies become `thread_message` events with actor resolved by chat-id map.
- v0 ships: interface + Matrix/Discord/Slack adapters behind config (tokens in
  `$TICKET_HUB_HOME/adapters.json`, never in repo), and a `FakeAdapter` for tests.
  No credentials committed; adapters are inert without config.

## UI (`ui/`, served at `/`)
Single-page, no build step, theme via CSS vars. Views:
1. **Board** — columns by status, cards show title/owner family/priority/child count
2. **Graph** — hierarchy tree + edges (SVG, collapsible)
3. **Ticket drawer** — event timeline, lineage breadcrumb, artifacts, bound threads
4. **Updates** — translated product-update digest per project, copy button
Design language: calm, high-legibility, Linear/Height-class. Status colors consistent,
family = avatar chip. This is the "marketing-readable" surface: Updates view, not raw events.

## Lanes (parallel against this contract)
- A core: model.py, store.py, lineage.py + tests
- B api+ui: api.py, ui/*, digest wiring (use FakeStore contract = store.py signatures in this doc)
- C adapters+translate: adapters/*, translate.py + tests (FakeAdapter, template pass)
Integration: A → B → C; frozen = this DESIGN.md is the contract; deviations reported, not silent.
