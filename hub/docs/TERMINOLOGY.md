# Terminology

Shared vocabulary for ticket-hub and the operation around it. Use these
terms verbatim in tickets, docs, and reports.

## Promotion

Advancing a build or ticket into `main` / prod. Only the owner can do it —
nothing else (green tests, reviewer approval, agent consensus) counts.
Promotion is therefore always **blocked by owner** until the owner explicitly
accepts. In the hub such items carry a `{"marker": "OWNER-GATED"}` event
(events are append-only; marker values stored before the rename stay as-is and
remain queryable via `TICKET_HUB_EXTRA_GATE_MARKERS`) — so promotion state
stays queryable, not folklore.

## Blocked by owner

The user's term for anything currently awaiting the owner's approval or action —
keep it verbatim (this exact phrase) in UI, CLI, and docs. Mechanically: a
ticket with status `blocked`, or carrying an `OWNER-GATED` marker event,
inside a project's subtree; `hub plan` lists them under
`blocked_by_owner` / "BLOCKED BY OWNER". Blocked ≠ stale: it names a specific
waiting-on-a-person state with a stated reason.

## Seat

A configured runtime slot for one model/family in the orchestration layer:
identity plus model, config, context budget, and isolation rules. Seat
identity is static; runtime observations may reduce a seat's eligibility
for work but never rewrite who the seat is or what authority it has.

## Lane

A parallel workstream that builds one slice of the system against the
design contract in an isolated sandbox worktree (e.g. the CLI lane, the
OUTBOUND lane, Lane A = store, Lane B = API, Lane C = adapters/translate).
Lanes integrate through the gateway facade; the whole test board must be
green before the candidate ships.

## Cycle

One iteration of the forge build loop: pick work → build in lanes → test
board → review → promotion candidate → blocked by owner: accept/reject → next
cycle. A rejection starts the successor cycle from the same frozen
snapshot plus rejection evidence; it never patches reviewed bytes.

## Digest

The reader-facing, event-derived update for a project: bullets translated
from real events, each citing the event ids it paraphrases
(`hub digest`, `GET /tickets/{id}/digest`). No-fabrication is structural —
citations must exist in the input, and the optional GLM polish pass may
reword prose but never add citations.

## Binding

A ThreadBinding linking one ticket to one chat thread on one platform
(matrix / discord / slack): room + thread + root message
(`hub bind`, `POST /tickets/{id}/bind`, `GET /tickets/{id}/bindings`).
Inbound replies become `thread_message` events on the bound ticket;
outbound digests post into it. One ticket = one thread root; child tickets
post under the parent binding's root.
