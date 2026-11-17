# Roles

`registry.json` is the charter registry for the swarm (DESIGN-v2 pillar 3).
Served live by the hub at `GET /roles` and `GET /roles/{id}`, read from disk
at request time — edit the file, no restart needed.

Each charter has: `id`, `mission`, `quality_bar`, `inputs`, `outputs`,
`seat_pref`, `default_model_hint`, `max_parallel`.

## Reviewer ≠ author

No seat reviews its own output. Concretely:

- A `product-owner` verdict (accept/return) must come from a seat that did
  not author the work under review. Same rule for `code-design` reviews and
  `design` reviews of orchestrator-coordinated artifacts.
- The `orchestrator` spawns lanes and mutates the ticket graph but never
  signs off on anything it coordinated.
- The hub enforces this only socially (actor names on events make violations
  visible: every claim carries `worker`, every result carries `actor:
  role:<role>`). The operator decides; the model never owns state.

## Ticket = shared packet

`hub packet <id>` renders the **complete** context any agent needs for a
ticket in one artifact: ticket fields, ancestor chain titles, plan (children
status + progress %), blocked list, last 15 events, bound chat threads, and
matching wiki pages. Workers get exactly this on stdin — no side channels,
no "context I happen to remember". If it's not in the packet, it doesn't
exist for the lane; put it in the ticket.

## swarm.json

`swarm.py` reads `roles/swarm.json` (absent → harness is inert: logs what it
would do, claims nothing, calls nothing). See `swarm.example.json` for the
shape: workers with `role`, `tag`, `seat_cmd` (receives the packet on stdin,
supports a `{model}` placeholder), `interval_s`, `max_parallel`.
