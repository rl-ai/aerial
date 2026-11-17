# Agent guide: working a ticket end-to-end

Every agent (hermes, forge, glm, …) works the same hub through
`hub --json` (or `hub_client.HubClient` directly — stdlib only). This is
the full loop: find work, claim it, record milestones, escalate when
stuck, report for standup, finish.

## Rules first

1. **Events are immutable.** Never edit or delete history — there is no
   edit/delete API and the store enforces append-only. Correct the record
   by appending a new event.
2. **One ticket per task.** Don't pile unrelated work onto a ticket, and
   don't reopen finished (terminal) tickets — `done`/`cancelled` are
   terminal; create a follow-up and `hub link` it.
3. **Blocked = needs someone.** Set `blocked` only when you are genuinely
   waiting on a person or another ticket, and say why in a comment.
   Waiting on the owner's approval specifically = **Blocked by owner**: set status
   `blocked` and/or append
   `hub event ID decision --json '{"marker": "OWNER-GATED", "note": "why"}'`
   — either makes it show up in the parent's `plan.blocked_by_owner`.
4. **Attribute yourself.** Pass `--actor <your-name>` on writes so the
   timeline shows who did what.
5. **Report facts, not vibes.** Progress comes from child statuses and
   events; a comment claiming "90% done" changes nothing. Use `hub plan`.

## The loop

### 1. Find work
```bash
hub ls --status open --family hermes --json      # assigned, unclaimed
hub board --json                                 # everything, by column
```
Pick the highest-priority `open` ticket that is yours (or an unowned child
under your project). Don't grab a ticket another agent has already moved
to `running`.

### 2. Claim it
```bash
hub start TK-01ABC… --actor hermes   # draft→open→running (legal hops)
```

### 3. Work — record milestones as events
```bash
# milestones (comments are fine; use typed kinds when they fit)
hub comment TK-01ABC… "parser landed, 40 tests green" --actor hermes
hub event TK-01ABC… artifact --json '{"name": "exporter-v1.patch"}' --actor hermes
hub event TK-01ABC… decision --json '{"decision": "CSV over Parquet, see rationale comment"}' --actor hermes
```
If you learn what's still left, record it so `plan` surfaces it:
```bash
hub event TK-01ABC… checkpoint --json '{"remaining": ["backfill dry-run"]}' --actor hermes
```

### 4. Stuck? Block with a reason
```bash
hub comment TK-01ABC… "need prod creds before backfill" --actor hermes
hub set TK-01ABC… --status blocked --actor hermes
# if it is the owner's approval you need, flag it explicitly:
hub event TK-01ABC… decision --json '{"marker": "OWNER-GATED", "note": "approve prod creds"}' --actor hermes
```
Blocked tickets surface in `hub plan` on the parent under BLOCKED BY OWNER
(and in the board's `blocked` column). Unblock with
`hub set … --status running`.

### 5. Standup / status questions
```bash
hub plan TK-01ROOT… --json | jq '{progress, blocked_by_owner, remaining_items}'
hub digest TK-01ROOT… --md          # human-readable, event-cited
```
Three answers: what it is (`summary`), how far along (`progress.percent`,
computed from child statuses), what's left (`remaining_items` +
`remaining_notes` + `blocked_by_owner`).

### 6. Finish
```bash
hub comment TK-01ABC… "shipped; dry-run clean on 2026-10-11" --actor hermes
hub done TK-01ABC… --actor hermes   # running→review→done
```
Review/approval is a human decision — blocked by owner — an agent moving a
ticket through `review → done` does not create acceptance authority;
acceptance belongs to the owner.

## Subagent / fan-out pattern

Big task? Make it a project:
```bash
root=$(hub new "Migrate ledger" --body "Migrate X; plan in children" \
        --owner alice --json | jq -r .id)
for t in "Extract" "Transform" "Load" "Verify"; do
  hub new "$t" --parent "$root" --owner hermes
done
```
The parent's body is the plan summary; the children are the plan. Work
each child with the loop above; `hub plan $root` answers status questions
for all of them at once.

## Error handling

- Any failure: exit 1, stderr `error: <code>: <message>`.
- `connection_error` → server down; retry later, don't spin.
- `bad_request: illegal status transition` → you skipped a state; use
  `hub start`/`hub done` which hop legally.
- `cycle_rejected` → your `parent_of`/`blocks` edge would loop; re-parent.
- `not_found` → wrong id or deleted-elsewhere; re-`ls`.

## Reporting job runs

A **job** is the registered definition of recurring work — id, name, node,
schedule, command and the ticket it belongs to. A **run** is one execution of
that job. The hub keeps both beside the tickets and derives each job's health
label from its latest run — `running`, `stuck`, `overdue`, `failed`, `ok` or
`unknown`; it is computed, never a stored column.

### Start, then end

Open a run when the work begins and close it when it finishes:

```bash
curl -sX POST "$HUB/jobs/backup-nightly/runs" -H 'content-type: application/json' \
  -d '{"node": "node-a", "pid": 1234}'
# 201 -> the run: status "running", run_id "run_…"

curl -sX PATCH "$HUB/jobs/runs/run_01ABC…" -H 'content-type: application/json' \
  -d '{"status": "ok", "exit_code": 0, "summary": "snapshot written"}'
```

Only a final status (`ok`, `failed`, `timeout`, `skipped`) may end a run; a
`running` status is `422 bad_status` and an unknown `run_id` is `404
not_found`. Ending sets `ended_at` and `duration_s` and merges `meta` (keys
already there are kept). Keep `summary` short (capped at 500 chars) and put
the detail in `log_tail` (capped at 4000, keeping the end).

### One-shot records and idempotent imports

A reporter that only learns the outcome at the end posts one record with a final
status instead of start/end:

```bash
curl -sX POST "$HUB/jobs/import-nightly/runs" -H 'content-type: application/json' \
  -d '{"status": "ok", "summary": "imported 42 rows",
       "meta": {"dedupe_key": "import-2026-10-28"}}'
```

Recording the same `meta.dedupe_key` again returns the earliest matching run
instead of a duplicate, so a retried importer does not inflate history.
Start/end reporters leave `dedupe_key` out.

### Bind a job to a ticket

Point `ticket_id` at a ticket tagged `feature` or `project` — the nearest
self-or-ancestor wins — and the run rolls up to that ticket's feature and
project in `GET /jobs/rollup` (`level=feature` or `level=project`) and in the
daily digest. A job with no `ticket_id`, or none tagged, lands in
`(unassigned)`.

```bash
curl -sX PUT "$HUB/jobs/backup-nightly" -H 'content-type: application/json' \
  -d '{"node": "node-a", "interval_s": 86400, "ticket_id": "TK-EXAMPLE"}'
```

### Read it back

The Jobs view lives in the UI sidebar (the ⏱ Jobs nav item) with four tabs:
Projects and Features are the rollup tables, Jobs lists each job with its
derived status, next due and 24-hour run/failure counts, and Digest renders
the daily digest. Without the UI:

```bash
curl -s "$HUB/jobs/digest?format=md"   # the last 24h, Markdown
curl -s "$HUB/jobs/digest?format=json"
```

`POST /jobs/digest/publish` appends that digest to a ticket as a
`checkpoint` event.
