# Deploy runbook: ticket-hub

Two processes: the **API server** (`api.py`, FastAPI + SQLite) and the
**outbound daemon** (`outbound.py`, polls the hub and posts digests to bound
chat threads). Both run on the hub host; the daemon is optional until chat
mirroring is wanted.

## OSS deployment profile

The OSS base ships with **generic defaults only** — no hostnames, user paths,
or deployment-specific values are hardcoded anywhere in `ticket_hub/`, the
top-level modules, `api.py`, `scripts/`, or `deploy/`:

- Network: loopback by default (`127.0.0.1:8777`); every URL/port is
  `TICKET_HUB_URL` / `TICKET_HUB_PORT` / `TICKET_HUB_HOST` driven.
- State: everything under `TICKET_HUB_HOME` (default `~/.ticket-hub`), DB at
  `TICKET_HUB_DB`.
- Tokens: no token file paths in code — clients read the file named by
  `HUB_TOKEN_FILE` at call time; the server takes write-class tokens from
  `TICKET_HUB_WRITE_TOKENS` (comma-separated; unset fails closed).
- Auth: `TICKET_HUB_AUTH` (`required` by default; literal `off` disables).

Additional variables beyond the table above:

| Variable | Used by | Default |
|---|---|---|
| `TICKET_HUB_HOST` | `api.py` bind address | `127.0.0.1` |
| `TICKET_HUB_AUTH` | `api.py` auth on/off | `required` (only literal `off` disables) |
| `TICKET_HUB_WRITE_TOKENS` | `api.py` write-class bearer gate | unset = writes fail closed |
| `TICKET_HUB_HEALTH_STATUS` | `api.py` health endpoint status file | `<hub home>/health_status.json` |
| `TICKET_HUB_DIR` | `api.py` jobs/health directory | `TICKET_HUB_HOME` |
| `HUB_BASE_URL` | `bin/hub` CLI | `TICKET_HUB_URL`, else `http://127.0.0.1:8777` |
| `HUB_TOKEN_FILE` | `hub_client.HubClient`, outbound, CLI | unset = no Authorization header |
| `RLAI_PACKAGES_DIR` / `RLAI_ROLES_DB` / `RLAI_RUNTIME_DB` / `RLAI_OPERATOR` | optional Aerial/RLAI integration (`ticket_hub/rlai.py`, `rlai_api.py`) | unset = feature entirely inert |

The optional RLAI/Aerial integration activates only when those env vars are
set; with the generic profile it is inert and its absence is reported, never
crashed. Deployment-specific helpers (NAS backup, legacy per-host data
migrations) are **not** part of the OSS base. The generic features every
deployment needs — the `DELETE /tickets/{id}` route, the family-ACL
(actor × family × action scoping), and bearer-token scoping — are part of
this base, not an add-on.

## Environment variables

| Variable | Used by | Default |
|---|---|---|
| `TICKET_HUB_HOME` | all path defaults below | `~/.ticket-hub` |
| `TICKET_HUB_DB` | api.py | `~/.ticket-hub/hub.db` |
| `TICKET_HUB_PORT` | api.py | `8777` |
| `TICKET_HUB_URL` | hub CLI, outbound | `http://127.0.0.1:8777` |
| `TICKET_HUB_ADAPTERS_CONFIG` | install script → daemon `--config` | `~/.ticket-hub/adapters.json` |
| `TICKET_HUB_OUTBOUND_STATE` | install script → daemon `--state` | `~/.ticket-hub/outbound_state.json` |
| `TICKET_HUB_OUTBOUND_INTERVAL` | install script (seconds) | `60` |
| `TICKET_HUB_OUTBOUND_LOG_DIR` | install script | `~/.ticket-hub/logs` |
| `TICKET_HUB_PYTHON` | install script, hygiene check | `python3` on PATH |
| `TICKET_HUB_POLISH_URL` / `_MODEL` / `_KEY_FILE` | outbound (optional GLM polish) | unset = template-only digests |
| `TICKET_HUB_EXTRA_GATE_MARKERS` | plan projection / gate checks | unset; comma-list of additional legacy gate markers accepted alongside `OWNER-GATED` |
| `HUB_ACTOR` | hub CLI | unset; default actor name for `--actor`/`--actor-name` (else the OS user) |
| `HUB_BACKUP_DEST` | `scripts/backup_offsite.sh` | unset = skip; destination directory on an offsite/backup share |
| `HUB_BACKUP_EXTRA_DIR` | `scripts/backup_offsite.sh` | unset; optional directory of immutable files mirrored into `$HUB_BACKUP_DEST/extra` |

**Upgrading an existing install:** the default home is now `~/.ticket-hub`. If your data lives elsewhere, set
`TICKET_HUB_HOME` (or `TICKET_HUB_DB`) to it, or move the directory, before starting the server; otherwise the
hub starts against a fresh, empty database. Agents that still emit an older gate marker can keep it counted with
`TICKET_HUB_EXTRA_GATE_MARKERS`.

`TICKET_HUB_POLISH_KEY_FILE` points at a bearer-key file the daemon reads
**at call time only**; nothing secret is stored in the plist, the repo, or
the DB, and the key is never logged.

## API server

```bash
cd <repo>
python api.py                     # :8777 (port from $TICKET_HUB_PORT)
# or explicitly:
TICKET_HUB_DB=~/.ticket-hub/hub.db python api.py
```

- SQLite is WAL mode, **one writer** — never run two servers against the
  same `hub.db`.
- Clients (UI, CLI, outbound) use `http://127.0.0.1:8777`. The dev
  entrypoint binds `0.0.0.0` by default; if the host is on an untrusted
  network, front it with a loopback bind / firewall — see SECURITY.md.
- The parent ticket-hub directory is created automatically.

### Health check
```bash
curl -sf http://127.0.0.1:8777/board | head -c 200   # JSON with 7 status columns
curl -s  http://127.0.0.1:8777/tickets?status=running  # array
```
A healthy hub answers both; a dead one hangs / connection-refused.

### Restart
```bash
# find and stop
pkill -f "python api.py"          # or kill the PID from `lsof -i :8777`
# start again (from the repo dir)
cd <repo> && python api.py &
# verify
curl -sf http://127.0.0.1:8777/board >/dev/null && echo ok
```
The outbound daemon survives a hub restart: a tick whose hub call fails is
a silent no-op and retries next tick; watermarks only advance after
successful posts, so nothing is lost.

## Outbound daemon

One-shot loop (dry run — inert when no adapters are configured):
```bash
python outbound.py --once         # one tick and exit
python outbound.py --interval 60  # foreground loop
```

### Install as a user service (the owner runs the load command)
```bash
deploy/install_outbound.sh
```
The script renders `deploy/com.ticket-hub.outbound.plist.template` into
`~/Library/LaunchAgents/com.ticket-hub.outbound.plist`, lints it, and
**prints the exact command** — it never loads the service itself; service
control stays the owner's:
```bash
launchctl load   ~/Library/LaunchAgents/com.ticket-hub.outbound.plist
launchctl unload ~/Library/LaunchAgents/com.ticket-hub.outbound.plist
```

### Log paths (launchd)
```
~/.ticket-hub/logs/outbound.log      # stdout
~/.ticket-hub/logs/outbound.err.log  # stderr
```

### State file
`~/.ticket-hub/outbound_state.json` — per-ticket watermark + set of
already-posted event ids. Atomic writes (tmp + rename); deleting it only
loses the dedupe history (already-posted digests could repost once), it
never affects hub data.

## Adapters config

Location: `~/.ticket-hub/adapters.json` (the `--config` flag and
`TICKET_HUB_ADAPTERS_CONFIG` override it). `chmod 600`. **Never commit it.**
Shape — one section per platform; an absent section (or token) ⇒ that
adapter is inert (no network I/O, never crashes):

```json
{
  "matrix":  {"homeserver": "https://matrix.example.invalid",
              "token": "***", "user_map": {"@alice:example.invalid": "alice"}},
  "discord": {"token": "<discord-bot-token>"},
  "slack":   {"token": "<slack-bot-token>",
              "user_map": {"U0000000000": "alice"}}
}
```

Placeholders only — real tokens exist solely in this file. A malformed or
missing config makes the daemon start inert; a broken platform section is
skipped without taking the daemon down.

## Backup / restore of hub.db

The hub is one SQLite file (plus WAL sidecars) under
`~/.ticket-hub/` — it rides the normal backup rotation. To
take a consistent snapshot explicitly:

```bash
# backup (safe while the server runs — sqlite3 checkpoints the WAL into the copy)
sqlite3 ~/.ticket-hub/hub.db ".backup ~/.ticket-hub/backups/hub-$(date +%Y%m%d).db"

# restore: stop the server, swap the file, restart
pkill -f "python api.py"
cp ~/.ticket-hub/backups/hub-YYYYMMDD.db ~/.ticket-hub/hub.db
rm -f ~/.ticket-hub/hub.db-wal ~/.ticket-hub/hub.db-shm
cd <repo> && python api.py &
```

Events are append-only, so restoring an older DB rewinds history — restore
only when that is what you want. Verify after restore with
`hub ls --json | head` and `hub board`.

## Upgrades

Pull on the hub branch, restart the API server, reload the launchd service
if `outbound.py` changed. The schema migrates in place; the legacy flat-ledger
migration was a one-time import applied before release, and its script is no
longer shipped.

## Access administration

`scripts/hub_admin.py` manages actors, tokens, grants and labels directly on
the database named by `TICKET_HUB_DB` (required; the CLI exits 2 if unset).
Every mutating command needs `--by <owner-id>` naming an owner actor
(`actor add ... --owner` bootstraps the **first** owner with any `--by` value
while none exists yet). All mutations are recorded as internal events on an
`access-ledger` ticket. Tokens are written to a fresh 0600 file (`--out`,
never overwritten) and never printed; `token list` shows ids and prefixes only.

```bash
export TICKET_HUB_DB=~/.ticket-hub/hub.db

# bootstrap the first owner (only possible while no owner exists)
python scripts/hub_admin.py actor add <owner-id> --name "Owner" \
    --kind human --owner --by bootstrap

# actors
python scripts/hub_admin.py actor add <agent-id> --name "Agent" --by <owner-id>
python scripts/hub_admin.py actor add <ext-id> --name "Partner bot" \
    --realm external --sponsor <owner-id> --expires 30d --by <owner-id>
python scripts/hub_admin.py actor suspend <agent-id> --by <owner-id> --reason "offboard"
python scripts/hub_admin.py actor activate <agent-id> --by <owner-id>
# adopt one pre-access-control actor row (must already exist in the DB;
# --kind/--family optional, never touches realm/clearance/sponsor/expiry;
# --owner follows the same bootstrap rule as `actor add --owner`)
python scripts/hub_admin.py actor adopt <legacy-id> --kind human --by <owner-id>
# activate every actor that has no status row yet (cut-over sweep; prints
# count + ids, idempotent, leaves suspended/revoked actors untouched)
python scripts/hub_admin.py actor adopt-legacy --by <owner-id>
python scripts/hub_admin.py actor list

# tokens (token written to /path/to/token, mode 0600; prints id + prefix only)
python scripts/hub_admin.py token issue <agent-id> --out /path/to/token \
    --expires 30d --by <owner-id>
python scripts/hub_admin.py token revoke <credential-id> --by <owner-id> --reason "rotate"
python scripts/hub_admin.py token list

# read grants (external actors: ticket / subtree / wiki_page scopes)
python scripts/hub_admin.py grant add <ext-id> --scope-type subtree \
    --scope <ticket-id> --expires 90d --by <owner-id> --reason "partner access"
python scripts/hub_admin.py grant revoke <grant-id> --by <owner-id>
python scripts/hub_admin.py grant list --actor <ext-id>

# labels (publishing below 'internal' is owner-only)
python scripts/hub_admin.py label ticket <ticket-id> external --by <owner-id> --reason "publish"
python scripts/hub_admin.py label wiki <slug> external --by <owner-id>
```

Durations accept `30d`, `12h`, or an ISO-8601 UTC timestamp; they are stored
as ISO UTC. Exit codes: 0 ok, 2 usage (including unset `TICKET_HUB_DB`),
3 refused/validation, 4 not found.

Clients (`hub_client.HubClient`, the outbound daemon) send the bearer token
**at call time** from the file named by `HUB_TOKEN_FILE`; with the variable
unset they send no header, and an unreadable/empty file fails with an error
naming `HUB_TOKEN_FILE` (never its contents):

```bash
export HUB_TOKEN_FILE=/path/to/token   # mode 0600, from `token issue --out`
```
