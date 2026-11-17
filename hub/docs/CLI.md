# hub CLI

`hub` is the terminal client for the ticket-hub API: one compact line per
ticket for humans, `--json` for machines, exit codes for scripts. The
`bin/hub` shim is PATH-installable (symlink it into `~/.local/bin`); it runs
`hub_cli.py` with the forge venv python when available, else `python3`.
The underlying client (`hub_client.py`) is stdlib-only — any python3 can
import it with no venv or pip.

## Global flags

| Flag | Meaning |
|---|---|
| `--url URL` | Hub base URL (default: `$TICKET_HUB_URL` or `http://127.0.0.1:8777`) |
| `--timeout N` | HTTP timeout in seconds (default 30) |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Any hub error — `error: <code>: <message>` on stderr (API error or `connection_error`) |

## Verbs

### `hub new TITLE [--body B] [--parent ID] [--owner NAME] [--tag TAG]… [--priority P] [--json]`
Create a ticket (starts at `draft`). `--tag` repeats. `--json` prints the
full created ticket as raw JSON; otherwise one line:
```
$ hub new "Fix flaky replay" --body "see logs" --tag qa --owner hermes
TK-01ABC…  draft  Fix flaky replay  [hermes]
```

### `hub get ID [--json]`
Show one ticket (line or full JSON).

### `hub ls [--status S] [--family F] [--tag T] [--q SUB] [--json]`
List tickets; filters AND-combine; `--q` is a substring match.
```
$ hub ls --status running --json
[ { "id": "TK-…", … } ]
```

### `hub set ID [--status S] [--priority P] [--title T] [--body B] [--actor A]`
Patch fields. At least one field flag required. Status changes must be
legal transitions (illegal → exit 1):
```
$ hub set TK-01ABC… --status blocked --actor hermes
TK-01ABC…  blocked  Fix flaky replay  [hermes]
```

### `hub comment ID TEXT [--actor A]`
Append a `comment` event. Prints `EV-…  comment  on TK-…`.

### `hub event ID KIND [--json JSON_OBJECT] [--actor A]`
Append a typed event (`comment`, `checkpoint`, `decision`, `artifact`,
`handoff`, …). Note: on this verb `--json` carries the **payload**, not the
output format:
```
$ hub event TK-01ROOT… checkpoint --json '{"remaining": ["backfill dry-run"]}'
EV-01ABC…  checkpoint  on TK-01ROOT…
```
To flag something for the owner: `hub event ID decision --json
'{"marker": "OWNER-GATED", "note": "awaiting approval"}'`.

### `hub link SRC DST TYPE`
Add an edge: `parent_of | blocks | relates_to | duplicate_of`. Cycle-guarded:
```
$ hub link TK-01ROOT… TK-01CHILD… parent_of
ED-01ABC…  TK-01ROOT…  -parent_of->  TK-01CHILD…
```

### `hub lineage ID [--json]`
Ancestry chain, child/related/event counts (or the full subtree JSON).

### `hub plan ID [--json]`
What this ticket is about, progress, what remains (see docs/API.md for the
JSON shape):
```
$ hub plan TK-01ROOT…
TK-01ROOT…  [running]  Ship the exporter

  Export widget data nightly

  progress  [###############.......] 60%  (3/5 done)

  BLOCKED BY OWNER (1):
    ! TK-01XYZ…  [blocked]  Approve production credentials

  REMAINING (2):
    - TK-01ABC…  [open]  Wire the exporter  [hermes]
    … 1 more

  RECENT:
    2026-10-11T02:05:00  hermes: checkpoint
```

### `hub digest ID [--md] [--json] [--full]`
Translated project digest; every bullet cites event ids. `--md` → markdown,
`--json` → raw JSON, `--full` → canonical translation (all event kinds).
Default: plain-text bullets on stdout.

### `hub board [--json]`
Tickets grouped by status column, with child counts:
```
== running (2) ==
TK-01ABC…  running  Ship the exporter  [alice]  (5 children)
```

### `hub bind ID PLATFORM ROOM [--THREAD T] [--ROOT MSG]`
Bind a chat thread to a ticket (`matrix | discord | slack`):
```
$ hub bind TK-01ABC… matrix '!room:example.invalid' --root '$rootmsg'
TB-01ABC…  bound  matrix:!room:example.invalid
```

### `hub start ID [--actor A]`
Move to `running`, hopping legally through `open` if the ticket is `draft`.

### `hub done ID [--actor A]`
Move to `done`, hopping legally through `open`/`review` as needed
(e.g. `running → review → done`).

### `hub packet ID [--json]`
Print a complete shared packet for one agent: ticket, ancestors, plan, blocked tickets, recent events, threads, and related wiki pages. `--json` prints the raw packet instead of the sectioned text.

```
$ hub packet TK-EXAMPLE
== PACKET: TK-EXAMPLE [running] Ship the exporter ==
owner: alice
tags: export

-- ticket body --
  Export widget data nightly

-- plan --
  progress 60% (3/5 done)
  - TK-EXAMPLE  [open]  Wire the exporter

-- blocked (1) --
  ! TK-EXAMPLE  [blocked]  Approve production credentials

-- last 1 events --
  2026-10-11T02:05:00  alice: checkpoint

-- threads (1) --
  matrix  room=!room:example.invalid  thread=$rootmsg

-- related wiki pages (1) --
  [[export-notes]]  Export notes
```

### `hub seats [--json]`
Show the seat roster: health, load, licensed roles, and open assignments. `--json` prints the raw roster.

```
$ hub seats
SEAT                 HEALTH  LOAD  ROLES
  seat-a              up      0     builder

OPEN ASSIGNMENTS (1):
  TK-EXAMPLE  seat-a@builder  [running]  Ship the exporter
```

### `hub templates [--json]`
List workflow templates. `--json` prints the raw template list.

```
$ hub templates
  example-flow      [repo] 4 steps  — Cut a release with review and handoff
```

### `hub template NAME`
Show one template as JSON.

```
$ hub template example-flow
{ "name": "example-flow", "steps": [ … ] }
```

### `hub instantiate NAME TITLE [--parent ID] [--actor-name NAME] [--no-assign]`
Create a ticket subtree from a template, auto-assigning seats per step. `--no-assign` creates tickets without seat assignment; `--actor-name` defaults to `$HUB_ACTOR` (else the OS user).

```
$ hub instantiate example-flow "Nightly export"
root: TK-EXAMPLE  Nightly export
```

### `hub assign ID ROLE [--seat SEAT] [--actor-name NAME]`
Assign a role (and optionally a seat licensed for the role) to a ticket. `--actor-name` defaults to `$HUB_ACTOR` (else the OS user).

```
$ hub assign TK-EXAMPLE builder --seat seat-a
TK-EXAMPLE  ->  seat-a  (builder, …)  EV-EXAMPLE
```

### `hub release ID [--actor-name NAME]`
Release a ticket's current holder. `--actor-name` defaults to `$HUB_ACTOR` (else the OS user).

```
$ hub release TK-EXAMPLE
TK-EXAMPLE  released  seat-a@builder
```

### `hub wiki ls [--json]`
List wiki pages, latest revision each. `--json` prints the raw list.

```
$ hub wiki ls
export-notes  r3  Export notes  [export]
```

### `hub wiki get SLUG [--json]`
Show one wiki page. `--json` prints the full result.

```
$ hub wiki get export-notes
# Export notes
slug: export-notes  rev: 3  actor: alice  ts: 2026-10-11T02:05:00
tags: export

Export widget data nightly.
```

### `hub wiki set SLUG --title TITLE [--body B] [--file FILE] [--tag TAG]… [--actor A]`
Append a new revision of a page. `--title` is required; give `--body` or `--file`, not both.

```
$ hub wiki set export-notes --title "Export notes" --body "Export widget data nightly." --tag export
export-notes  r4  Export notes  [export]
```

### `hub wiki search QUERY [--json]`
Substring search over page title, body, and tags. `--json` prints the raw results.

```
$ hub wiki search export
export-notes  Export notes  — Export widget data nightly.
```

## Scripting pattern

```bash
set -e
id=$(hub new "Nightly export" --owner hermes --json | python3 -c \
     'import json,sys; print(json.load(sys.stdin)["id"])')
hub start "$id"
hub comment "$id" "schema locked" --actor hermes
hub done "$id"
```

Anything that fails prints `error: <code>: <message>` and exits 1 — branch
on the exit code, parse `--json` on stdout, never on stderr.
