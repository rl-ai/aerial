# secondbrain-mcp

> **The governed read path for any agent over your private notes.** Sensitivity labels, citation-closure checks, and refuse-by-default sanitization — exposed to Claude Code, Cursor, or any MCP client over the Model Context Protocol.

## What it refuses

Your agent gets *search* and *fetch* — nothing else. Content labeled `intimate` is never returned in any form; content outside the session grant's citation closure is never returned; every snippet passes disclosure sanitization (locations generalized, dates cut to month granularity) before it leaves the process; unknown/missing sensitivity labels default to `personal`. A degraded gateway fails **closed**: if the grant, graph, or vault cannot be fully validated, you get a tool error, never open content.

## Prerequisites

- Python 3.11+ (stdlib only — no pip installs needed to run from source)
- A **vault**: a folder of markdown notes with the secondbrain markers
  - `@entity:<name>` — declares entities (the clustering/scope axis)
  - `sensitivity: public|personal|sensitive|intimate` — the label; **missing means `personal`** (conservative default)
- A **workspace** built by the curator pipeline: `<vault-root>/l0/` (content vault), `<vault-root>/l1/l1.json` (published graph), `<vault-root>/grants.db` (grant ledger)
- An MCP client (Claude Code, Cursor, or any client speaking MCP over stdio)

## One-command run

```bash
python3 -m secondbrain_mcp --vault ~/notes-ws --grant <grant_id> --key <key>
```

(From a source checkout: run it with the `secondbrain_mcp/` parent directory on `PYTHONPATH`.)

### Claude Code / Cursor client config (copy-paste)

```json
{
  "mcpServers": {
    "secondbrain": {
      "command": "python3",
      "args": [
        "-m", "secondbrain_mcp",
        "--vault", "/home/you/notes-ws",
        "--grant", "g-2026-11-abc123",
        "--key", "sk-g…"
      ],
      "env": { "PYTHONPATH": "/path/to/secondbrain-mcp" }
    }
  }
}
```

For Claude Desktop, put the same block in `claude_desktop_config.json`. One grant per agent: your planner agent and your reviewer agent should each get their own grant with their own scope.

## Grant setup

A grant is the *entire* access boundary: it pins an entity scope (anchor entities on a published graph alias), a TTL, a query budget, and disclosure rules. There is no CLI yet — create one with the existing grant machinery (three lines of Python against your workspace's `grants.db`):

```python
from secondbrain import GrantBook
book = GrantBook("/home/you/notes-ws/grants.db")
grant_id, key = book.create(
    counterparty="Ray's planner agent",
    purpose="planning over my training notes",
    anchor_entities=["entity:gym-riverbank"],   # entity:<name> node ids
    alias="main",                                # the published L2 alias
    ttl_days=30, query_budget=200,
    disclosure={"generalize_location": "city",
                "max_date_granularity": "month"})
print(grant_id, key)   # the key is shown ONCE; only its hash is stored
```

The equivalent **JSON grant file format** (what a future CLI will accept; keep one file per grant, never commit keys):

```json
{
  "counterparty": "Ray's planner agent",
  "purpose": "planning over my training notes",
  "anchor_entities": ["entity:gym-riverbank"],
  "alias": "main",
  "ttl_days": 30,
  "query_budget": 200,
  "disclosure": {
    "generalize_location": "city",
    "max_date_granularity": "month",
    "forbid": [],
    "require_citation": false
  }
}
```

Revoke at any time with `book.revoke(grant_id)` — one status flip, and the server starts returning `grant_invalid` on the next call.

## First-query sanity check

Ask your agent: *"search my notes for squat progress"*. Expected: it calls `brain_search` and returns results that each carry a sensitivity label, source filename, ingestion timestamp, and citation hashes — with locations generalized to `[location generalized]` and dates cut to `YYYY-MM` if your grant's disclosure rules say so.

**What a refusal looks like.** Content-level refusals are structured results, not errors, so the agent can adapt gracefully:

```json
{"refused": true, "reason": "intimate"}        // intimate-label content — never released
{"refused": true, "reason": "outside_scope"}   // exists, but not in this grant's closure
{"refused": true, "reason": "not_found"}       // unknown hash or tombstoned (indistinguishable on purpose)
```

Search results additionally carry `refused_count` — the number of matching items the gate withheld. That counter is the governance working, visible to the operator. Identity/infrastructure failures are tool errors: `grant_invalid` (expired, revoked, bad key, budget exhausted) or `vault_error` — the server never falls back to unscoped access.

Every tool call is appended to the existing grant ledger, and governance flags are persisted to `<vault>/flags.jsonl` so they survive restarts and surface in the digest.

## Limitations (named honestly)

- **Read-only.** No ingest, no mutation, no tombstone tools over MCP — writes stay in the curator pipeline.
- **No enumeration.** There is no listing tool; `brain_get` needs a hash from a prior `brain_search` or your own knowledge.
- **Exact dedup only.** Notes are identified by the sha256 of their raw text; near-duplicates and edited versions coexist — there is no supersession/conflict resolution yet.
- **L2 version snapshots are not persisted** by the current workspace format; the server serves the current graph state under the grant's pinned alias. Re-publish (curator) before serving if the vault changed materially.
- **Deterministic governance only.** Checks are regex/ledger/hash based — no LLM de-identification pass; borderline content is refused, not rewritten.
- **Single vault, single user, local process.** No multi-tenancy, no HTTP transport in v0 (stdio only).
