# Contributing to ticket-hub

Team process for humans and agents working on this repo. Acceptance and
promotion authority belong to **the owner** — nothing below grants any agent
production or merge authority.

## Branch per ticket

- One ticket = one branch: `hub-<ticket-id>-<short-slug>` (e.g.
  `hub-TK01ABC-deflate-digest`).
- Branch from the integration branch (`hub`); keep branches short-lived and
  rebase rather than merge when they drift.
- Never push to `main`. Merges into `main` are blocked by the owner until the owner accepts.

## Commits

- Reference the ticket id in every commit message subject or body:
  `cli: add --json to hub new (TK-01ABC…)`.
- Small, reviewable commits; no drive-by refactors unrelated to the ticket.

## Tests are required

```bash
uv run pytest tests -q
```

The full board (core + api + adapters + cli + outbound) must be green
before a branch is offered for review. New API/CLI behavior ships with
tests in the matching `tests/test_*.py`; store-level invariants (transition
legality, cycle rejection, append-only events) have dedicated tests — keep
them honest, don't weaken them to make a change pass.

## Review: reviewer must not be author

- Every change is reviewed before it lands on the integration branch.
- **The reviewer must not be the author** — an agent that wrote the change
  never signs off on it.
- Convention: GLM is the triggered reviewer gate for lane work (it reviews;
  it is not the fallback worker for every task). A review verdict of
  revise → the author fixes → fresh re-review.

## Design changes: DESIGN first

Behavioral or contract changes (API shapes, status transitions, event
kinds, edge semantics, digest/no-fabrication rules) need the design
updated **before** the code:

1. Open the change as a ticket with the design question.
2. Update `DESIGN-v1.md` (or a proposal section) and get it accepted.
3. Only then change code + tests to match.

If code and DESIGN disagree, that is a defect in one of them — file it;
don't silently pick a side.

## promotion blocked by owner

- `hub` branch: integration of reviewed lane work.
- `main`: only by the owner, from an exact reviewed candidate (frozen commit).
  Green tests and reviewer approval are inputs to the owner's decision, never a
  substitute for it.
- Anything awaiting the owner's sign-off — merge approval, production actions,
  credential grants — is **Blocked by owner** and must be visible as such in
  the hub (status `blocked` and/or an `OWNER-GATED` marker event on the
  ticket).

## House rules

- No secrets, tokens, room ids, or real credentials in the repo — ever.
  Runtime config lives outside it (see docs/DEPLOY.md and SECURITY.md).
- Docs and code move together: a PR that changes the CLI/API surface
  updates `README.md`, `docs/CLI.md`, `docs/API.md` in the same branch.
