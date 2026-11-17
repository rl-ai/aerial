# The hub as Aerial's Tickets product

Status: built on branch `rlai-tickets-product` (off `hub-access-p1`, the
image `ticket-hub:p1` runs). Not deployed. Redeploying the container is
Ray's call.

## Why

A5 (real-path qualification, `integration/DESIGN.md` in ai-ops) stages 1
and 2 need the real hub to do two things it could not do: take a C5
identity (`POST /identity`) and ingest C2 records the way
`rlai_tickets.Store.record()` does. Stage 0 also checks that `/operator/`
answers 403 without authorization. Until now the harness wrote to a local
`rlai_tickets` store instead.

## What is added

All of it lives in `rlai_api.py` (routes) and `ticket_hub/rlai.py`
(storage). `api.create_app` registers it before `check_policies`.

| Route | Policy | What it does |
|---|---|---|
| `POST /records` | `internal_write` | Ingest one C2 record message |
| `POST /identity` | `internal_write` | Register one C5 identity |
| `GET /operator/…` | `public` at the pipeline; admin-only in the mount | The `rlai_operator` page, only when `RLAI_OPERATOR=1` |

### Loading the Aerial packages

The hub imports nothing from ai-ops at start-up. When a route first needs
Aerial code, `ticket_hub/rlai.py` adds `<RLAI_PACKAGES_DIR>/<pkg>/src` to
`sys.path` for `rlai-tickets`, `rlai-operator`, `rlai-runtime`,
`rlai-roles` and `rlai-contracts`. `rlai_tickets` already finds
`rlai-contracts` as its sibling by file path. Without the folder, the two
POST routes answer 503 `rlai_unavailable` and the operator mount is not
registered. In the container this means a read-only bind mount of
`~/ai-ops/packages` at, say, `/rlai-packages`, plus
`RLAI_PACKAGES_DIR=/rlai-packages`. That is a deploy change (Ray's).

### C2 ingestion: `record()` over the hub's own tables

`rlai_tickets.Store` says it "mirrors the hub", but its DDL does not
match the hub's real schema. The hub uses `tickets.id` (not
`ticket_id`), `actors.id` (not `actor_id`), `events.id` and `events.ts`
(not `event_id` and `created_at`), and an autoincrement global `seq`.
So `record()` cannot run on the hub database unchanged.

`HubRecordStore` subclasses `rlai_tickets.Store` and overrides only its
four storage hooks: `_ticket_by_id`, `_ensure_actor`, `_insert_event` and
`_event_from_row`. Everything that defines C2 ingestion stays the
package's code and runs unchanged: contract validation, the
unknown-enum "unrecognized" rule, dedup by `message_id`
(`json_extract(payload, '$.record.message_id')`, which works on the hub's
`events.payload`), the `seq_gap` flag, and the event-type-to-kind map.
`_insert_event` writes through the hub's own `Store._append_event`, so
the event gets the hub's id, timestamp, label-at-insert and `actor_json`
(with `via` when someone else posted it). A compatibility test pins this
against the landed package, so a change to those hooks upstream fails
here rather than in production.

Rules:

1. A second delivery of a stored `message_id` answers 200 with
   `"duplicate": true` and the first event. Nothing is written. A first
   delivery answers 201.
2. An invalid message answers 422 `invalid_c2` with the validator's
   messages. A missing ticket answers 404.
3. Authority. The pipeline already requires an internal viewer. Unless
   the viewer is an owner, the record's `employee_id` must be a
   registered identity (rules 5-9; else 403 `unknown_employee`), and the
   poster must be the employee itself or the actor that registered it
   (else 403 `not_registrar`). So one internal token cannot write records
   as an employee another token registered. When the poster is not the
   employee, the event's actor carries `via` = the poster, so the audit
   trail shows who delivered it. A5 fits this: `eva-harness` registers
   `A5-TEST-01` in stage 1 and posts its records in stage 2.
4. One lock per connection (`ticket_hub.rlai.conn_lock`) is shared by
   every Aerial writer (`HubRecordStore` and `IdentityStore`). The hub's
   own writers predate it and take no lock on the shared connection, so
   an Aerial commit or rollback can still meet a hub writer mid-statement.
   That is a hub-wide gap, not new here: fixing it means locking every
   hub writer (follow-up).

### C5 identity

A new append-only table `rlai_identities` in the hub database. It is
created once at start-up (`CREATE TABLE IF NOT EXISTS`), never on a
request, and no existing table is altered. One
row per accepted message: `message_id` (primary key), `employee_id`,
`display_name`, `kind`, `token_ref`, `producer`, `sent_at`,
`received_at`, `received_by` and the whole message as JSON.

5. The message is validated with `rlai_contracts.validate("c5-identity")`;
   errors answer 422 `invalid_c5`.
6. A repeat `message_id` answers 200 `"duplicate": true`, no write.
7. An actor that an owner-posted record created before any identity
   arrived (family `aerial`, no identity rows) is a placeholder: the
   first C5 sets its `kind` and name. After that, the rules below hold.
   The employee becomes a hub actor if it is not one already
   (`AccessStore.create_actor`, realm internal, family `aerial`, never
   owner). A known actor keeps its row; a message whose `kind` differs
   from the stored actor's answers 409 `kind_conflict`, because `kind`
   drives relay decisions.
8. Only an owner may register `kind: human`. Others get 403. Once an
   employee is registered, only its first registrar or an owner may send
   it another identity message (403 `not_registrar`), so nobody else can
   change its recorded `token_ref`. The identity row and a new actor row
   land in one commit.
9. `token_ref` is a path reference only. It is stored, never read, and
   never returned by any route.

An employee is "registered" when it has at least one `rlai_identities`
row.

### `/operator` mount

Behind `RLAI_OPERATOR=1`, `create_app` mounts
`rlai_operator.page.asgi_app(view_fn, authorize=operator_auth)` at
`/operator` (package design, "Hub integration").

10. `operator_auth` reads the bearer header or the session cookie,
    resolves it with `AccessStore.viewer_for_token`, and returns the actor
    id only for an owner (the hub's admin). Everyone else, including no
    credential at all, gets 403 from the package. The pipeline marks the
    mount `public` so it does not answer 401 first; the A5 stage 0 check
    is "403 without authorization". Each decision is written to
    `access_log` like any other route.
11. `view_fn` builds `HubTickets` over an in-process getter on the hub's
    own store (no HTTP), and `RuntimeApi` and `RolesApi` over
    `RLAI_RUNTIME_DB` and `RLAI_ROLES_DB` opened read-only
    (`mode=ro`). A missing database leaves that source failing, so
    `build_view` marks only its panels unavailable.
12. The getter translates hub rows to the shape `HubTickets` reads:
    `ticket_id`, `holder` (the seat holder), `blocks` (incoming `blocks`
    edges) and `at`. For `/events?since=`, an event that holds a C2
    record is reported with `kind` = the record's `event_type`, and an
    outcome's payload is mapped to `outcome`, `employee_id`, `elapsed_s`
    and `cost`. The hub has no `finding` event kind yet, so the findings
    panel stays empty: a known gap.

## Not in this change

- The harness side. `qualify_real.py` still talks to a local `Store` for
  stage 2. It needs a small client that maps the `Store` API
  (`create_ticket`, `get`, `append_event`, `events`, `record`) to hub
  routes. Writing tickets and comments *as the employee* with the
  `eva-harness` token also needs an `act_as` scope on that credential,
  or those writes must be attributed to `eva-harness`. That is a minting
  decision, so it is Ray's.
- An expression index on `json_extract(payload,'$.record.message_id')`.
  Dedup scans the events table (about 6,600 rows today). Add the index if
  that grows.
- The deploy: the packages bind mount, the three environment variables,
  rebuilding the image and restarting the container. Rollback is
  `~/workspaces/build/hubp1-cutover/rollback.sh` on Morpheus.

## Review notes

Second-model review with `glm-review` (served by DeepSeek 1017 on
2026-11-10, reasoning effort low): three full rounds, then delta reviews
until the verdict was **approve** (round 6, on 78ca749). Reviews are kept
on Morpheus in `~/workspaces/build/hub-rlai/` (`round1/`-`round3/`,
`review-round4..6.json`).

Medium findings fixed:

1. Naive vs aware timestamps in the operator `since` filter (a 500).
2. Three Aerial writers on one connection under different locks; now one
   lock per connection.
3. The C5 422 body could quote a value; now it names fields only.
4. An identity re-registration hijack, and the identity and actor rows
   not being written together (found in my own review pass).
5. A placeholder actor made by an owner-posted record blocked the later
   C5 kind.
6. Request-time DDL on the shared connection, raised in three forms
   (executescript's implicit COMMIT, a commit of another writer's
   transaction, the table vanishing on that writer's rollback). Root fix:
   the table is made once at start-up, and the request path only checks.
7. A schema failure at start-up would have stopped the hub; now it is
   logged, and the identity routes answer 503.

Open lows (accepted for now):

- The hub's own writers take no lock on the shared sqlite connection.
  This predates the branch, and the fix touches every hub writer
  (follow-up).
- If the table cannot be made at start-up (for example, the database is
  locked), or there is no store then, `/identity` and `/records` answer
  503 until the hub restarts. The 503 says so.
- The operator audit row records `GET /operator` with 200 or 403 from
  the auth decision. A page that then fails 500 is still logged as 200.
- Dedup and `/events?since=` scan the events table (about 6,600 rows).
  Add an index if that grows.
- The findings panel stays empty until the hub has a finding event kind.

Kept as designed, after review: `via` (with act-as, `viewer.via` is the
relay, which is the poster); the mount's policy key (`/operator` covers
every sub-path); `/records` stays internal-only (employees are internal
actors); `view_fn` takes the gateway on purpose; hub tickets are shared,
so a record may name any ticket.

## Tests

`tests/test_rlai.py`, against the landed packages (skipped when
`RLAI_PACKAGES_DIR` is unset):

- records: first delivery 201; second 200 duplicate with one stored
  event; invalid 422; missing ticket 404; unregistered employee 403;
  another internal poster 403;
  owner may post for an unregistered employee; `via` recorded; external
  viewer 403; no token 401; seq gap flagged; unknown enum stored as
  unrecognized.
- identity: 201 and actor created; repeat 200 duplicate; invalid 422;
  re-registration by another internal actor 403 (owner allowed);
  `kind` conflict 409; human by non-owner 403; token_ref never echoed.
- operator: not mounted without the flag (404); 403 without credentials,
  403 for a non-owner token, 200 for an owner bearer and an owner
  cookie; `view.json` panels present; a non-GET is refused; access_log
  rows written.
- compatibility: `HubRecordStore.record()` on a real hub `Store`.
- `check_policies` passes with the flag on and off.
