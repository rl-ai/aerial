# Security posture — ticket-hub

## Network exposure

The v1 design posture is **loopback-only**: the hub serves
`http://127.0.0.1:8777` and is consumed by local agents (CLI/HubClient),
the browser UI, and the outbound poller on the same host. Note the dev
entrypoint (`python api.py`) passes `0.0.0.0` to uvicorn by default; on
any host with an untrusted network, keep the port firewalled or front it
with a loopback-only bind. Treat the hub as a single-host, trusted-LAN-
excluded service.

## No auth in v1 — and why

There is no authentication or per-actor identity on the HTTP API. This is
deliberate, not an oversight: v1's threat model is a single-user machine
where every consumer is either the owner or an agent the owner runs on that host.
Actor attribution (`owner`/`actor` fields) exists for **audit trail**, not
access control — it records who claimed to do what; it does not verify it.
Network auth is deferred until a non-loopback consumer exists; when it
arrives it will be **bearer-per-actor, not a shared secret** (DESIGN-v1 §7).
Adding remote exposure without that auth is out of policy.

## Secrets handling

Secrets never enter the repo, the database, prompts, or logs:

- **Chat tokens** live only in the runtime config
  (`$TICKET_HUB_HOME/adapters.json`, chmod 600), outside the repo.
  Adapters construct inert with zero credentials; an absent platform
  section means no network I/O.
- **Polish bearer key** (optional GLM pass) lives in the file named by
  `TICKET_HUB_POLISH_KEY_FILE` and is read **at call time**; it is never
  stored in the plist, the DB, or the environment of other processes, and
  never logged.
- **No secrets in examples**: docs and tests use placeholders
  (`<token>`, `example.invalid`) only.
- Ticket payloads are **not** a secrets store — assume anything written to
  the hub (bodies, comments, event payloads) is readable by every local
  consumer and mirrored into bound chat threads by the outbound daemon.
  Prompts built from digests carry the same guarantee: only event-derived
  text.

## Append-only event guarantees

History integrity is a security property: every claim in a digest, plan,
or chat post traces to event ids that cannot be quietly rewritten.

- There is no update/delete API for events — HTTP exposes append only.
- SQLite triggers enforce append-only at the store level: direct
  UPDATE/DELETE against the events table fails in the database itself.
- Digest translation is no-fabrication by construction: every bullet cites
  event ids present in its input (test-enforced), and the optional LLM
  polish pass can rewrite prose but never add citations — violations fall
  back to the template digest wholesale.
- Residual limits: filesystem access to `hub.db` is root-of-trust (same
  host = same trust domain); restoring a backup rewinds history.

## What IS in scope for reports

Please report:

- Any way to reach the hub from a non-loopback interface without auth
  (beyond the documented `0.0.0.0` dev-bind caveat) and act on it.
- Any path that mutates or deletes stored events, or bypasses the
  append-only triggers.
- Any leak of adapter tokens, the polish bearer key, or config-file
  contents into the repo, DB, logs, prompts, chat posts, or error
  messages.
- Any way to make the digest cite events that don't exist or that don't
  support the bullet's claim (fabrication bug).
- Injection through inbound `thread_message` text that corrupts tickets,
  forges other actors' events, or crashes the daemon/adapters.
- Path traversal / arbitrary file read via `--config`/`--state`/API input
  handling.

Out of scope: attacks requiring local filesystem access to `hub.db`, the
config file, or the host (same trust domain); social engineering of the owner;
issues in upstream chat platforms themselves.
