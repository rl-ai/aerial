# Getting started

Zero to a running Aerial stack from ONE clone, on one machine. Every command
below was verified against a live stack — see the footer.

## Architecture

Aerial is one repository with one clean boundary in the middle:

- `hub/` is the ticket hub — a **self-contained component** with its own HTTP
  API (`api.py`), its own env contract (`TICKET_HUB_*` variables), its own
  test suite (`hub/tests`), and its own docs. It is designed to be extracted
  and run standalone when needed: no `hub/` file imports anything from
  `dashboard/`, `integration/`, or `gateway/`.
- Everything else (console, ops agent, board, integration) talks to the hub
  **over HTTP only**, through small client modules (`dashboard/data_layer/`,
  `integration/hub_remote.py`). The hub can be lifted out into its own repo
  without code changes; until then, one clone is the whole product.

## 1. Prerequisites

- **Python 3.10+** (the code uses `X | Y` unions). Check: `python3 --version`.
- The dashboard/integration/ops-agent code is stdlib-only. The hub needs
  `pip install -r hub/requirements.txt` (fastapi, uvicorn).
- `curl` for the health checks below.

## 2. Clone and check out

```bash
git clone https://github.com/rl-ai/aerial.git
cd aerial
python3 -m pytest dashboard/tests -q   # optional sanity check (stdlib + pytest)
python3 -m pytest hub/tests -q         # optional: hub suite (needs fastapi)
pip install -r hub/requirements.txt    # hub runtime deps
```

Services run straight from the tree with `PYTHONPATH` set to the repo root
plus `dashboard/` (this is the #1 first-boot failure mode; see
troubleshooting below).

## 3. Configuration, state dir, token

Copy the example profile and point it at your demo state dir (this walkthrough
uses `~/aerial-monorepo-demo`, ports **8021** for the hub and **8779** for the
console so you can't collide with anything already running):

```bash
cp config/deployment.yaml.example config/deployment.yaml
mkdir -p ~/aerial-monorepo-demo/secrets ~/aerial-monorepo-demo/logs \
         ~/aerial-monorepo-demo/hub-home ~/.aerial
```

Edit `config/deployment.yaml` (it is gitignored — never commit a real
profile). Two values for this walkthrough:

```yaml
cluster:
  hub_url: http://127.0.0.1:8021
  hub_token_file: ~/aerial-monorepo-demo/secrets/hub-token
```

Everything can also be set with environment variables
(`AERIAL_<SECTION>__<KEY>`, e.g. `AERIAL_CLUSTER__HUB_URL`), which override
the YAML, which overrides the defaults in `deployment_profile.py`. Leave
`email.recipient_email` empty to keep email delivery disabled (the default).

Create the shared hub token:

```bash
head -c 32 /dev/urandom | xxd -p -c 64 > ~/aerial-monorepo-demo/secrets/hub-token
chmod 600 ~/aerial-monorepo-demo/secrets/hub-token
```

## 4. Start the ticket hub

The hub is the HTTP system of record (tickets, job_runs, mandates), SQLite
backed, configured entirely with `TICKET_HUB_*` variables:

- `TICKET_HUB_PORT` — listen port (default 8777; this walkthrough uses 8021)
- `TICKET_HUB_HOST` — bind address (default `127.0.0.1`)
- `TICKET_HUB_HOME` — state directory (default `~/.ticket-hub`)
- `TICKET_HUB_AUTH` — `off` for a loopback demo; default (`required`) demands
  bearer tokens and additionally serves write-token scoping via
  `TICKET_HUB_WRITE_TOKENS` / `TICKET_HUB_WRITE_TOKENS_FILE`
- `TICKET_HUB_DB`, `TICKET_HUB_HEALTH_STATUS` — db / health-status overrides

```bash
cd aerial                                   # repo root
TICKET_HUB_HOME=~/aerial-monorepo-demo/hub-home \
TICKET_HUB_PORT=8021 TICKET_HUB_AUTH=off \
nohup python3 hub/api.py > ~/aerial-monorepo-demo/logs/hub.log 2>&1 &
```

Verify:

```bash
curl -s http://127.0.0.1:8021/health
# -> {"ok": true, "ts": null, "last_error": null, "note": "no healthcheck has run yet"}
```

(`ok: true` = the hub is up. With auth left on, `/health` is public but
reflects the last `hub/scripts/healthcheck.sh` run instead.)

Without a hub the console and board still render (fail-closed, degraded
banners), but the ops agent has nothing to reconcile against.

## 5. Start the operator console

```bash
cd aerial                                    # repo root
export PYTHONPATH="$PWD:$PWD/dashboard"
nohup python3 dashboard/serve_console.py --port 8779 --host 127.0.0.1 \
      > ~/aerial-monorepo-demo/logs/console.log 2>&1 &
```

- Reads `cluster.hub_url` / `cluster.hub_token_file` from
  `config/deployment.yaml` (or pass `--hub-url` / `--hub-token-file`).
- Auth: the **read token is the hub token file** above; every request
  needs `Authorization: Bearer …`. Pass `--write-token-file <other file>`
  with a **distinct** secret to enable decision approvals — sharing the read
  token as the write token is a known-bad configuration (attack-swarm
  finding; the read token POST now gets a 401). Without
  `--write-token-file` the console is read-only.
- `--open-read` makes the read page unauthenticated — only do this on
  loopback and only if you accept the tradeoff; the default is token-gated.

Verify:

```bash
T=$(cat ~/aerial-monorepo-demo/secrets/hub-token)
curl -s -H "Authorization: Bearer $T" http://127.0.0.1:8779/dashboard/health
# -> {"ok": true, "tickets": 1}
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8779/dashboard/health
# -> 401 (no bearer)
```

## 6. Start the ops agent

Smoke-test first with a single poll (a full reconciliation pass takes about
a minute — seat probes and hub polls are what it does):

```bash
cd aerial && export PYTHONPATH="$PWD:$PWD/dashboard"
python3 -m dashboard.followup.runner --once \
  --log ~/aerial-monorepo-demo/logs/ops-agent-once.log
grep -c INFO ~/aerial-monorepo-demo/logs/ops-agent-once.log   # > 0
```

Then run it for real — systemd **user unit** (adapt paths):

```ini
# ~/.config/systemd/user/aerial-console.service
[Unit]
Description=Aerial operator console (bearer-authed dashboard)
After=network-online.target

[Service]
WorkingDirectory=%h/aerial
Environment=PYTHONPATH=%h/aerial:%h/aerial/dashboard
ExecStart=/usr/bin/python3 %h/aerial/dashboard/serve_console.py --host 127.0.0.1
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

```ini
# ~/.config/systemd/user/aerial-ops-agent.service
[Unit]
Description=Aerial ops agent (5-min poll)
After=network-online.target

[Service]
WorkingDirectory=%h/aerial
Environment=PYTHONPATH=%h/aerial:%h/aerial/dashboard
ExecStart=/usr/bin/python3 -m dashboard.followup.runner --interval 300
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

```ini
# ~/.config/systemd/user/aerial-shadow-scheduler.service
[Unit]
Description=Aerial shadow scheduler
After=network.target

[Service]
WorkingDirectory=%h/aerial
Environment=PYTHONPATH=%h/aerial
ExecStart=/usr/bin/python3 %h/aerial/integration/scheduler.py --interval 30
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now aerial-console aerial-ops-agent aerial-shadow-scheduler
```

If you don't run systemd, `nohup ... &` (with `PYTHONPATH` exported) works
the same. The hub can run under its own unit the same way
(`ExecStart=/usr/bin/python3 %h/aerial/hub/api.py` with the `TICKET_HUB_*`
environment); `hub/deploy/` ships launchd templates and install scripts.

## 7. Verify the chain

One command checks the whole chain (python, imports, hub deps, state dirs,
token file, hub health, console auth, ops-agent log) and prints a fix for
anything that fails:

```bash
python3 aerial_verify.py          # add --json for machine output
```

Manual alternative (the same checks by hand):

```bash
curl -s http://127.0.0.1:8021/health                        # hub:      ok: true
T=$(cat ~/aerial-monorepo-demo/secrets/hub-token)
curl -s -H "Authorization: Bearer $T" http://127.0.0.1:8779/dashboard/health
                                                            # console:  {"ok": true, ...}
grep -c INFO ~/aerial-monorepo-demo/logs/ops-agent-once.log # ops agent ran a full poll
```

The first findings appear after the ops agent's first poll: schedule
reconciliation, seat liveness, and conformance checks produce entries in the
exception queue / daily board (findings against a healthy empty system are
normal — e.g. seat gaps if you run no model seats).

## 8. What you'll see on day 1

- **Daily board** (`/dashboard`, plus the static snapshot the console
  refreshes per request): ticket queue, schedule reconciliation findings,
  seat liveness, exec-summary headlines.
- **Exec report**: three-headline brief plus per-section detail, rendered by
  `dashboard/exec_summary.py`; emailed only if `email.recipient_email` is
  set (otherwise delivery is a no-op).
- **Followup queue / exception queue**: R1–R6-classified findings with
  severity routing — sev-incident/security pings immediately, safe-fix
  sessions for reversible issues, everything else queued for operator review.
- **Idle-work broker** (`dashboard/idle_broker/`): optional. Fill
  measured-idle model seats from `dashboard/idle_broker/backlog.yaml`:
  `python3 dashboard/idle_broker/broker.py --once` (or `--daemon`).

## Troubleshooting — top first-boot failure modes

| Symptom | Cause | Fix |
| --- | --- | --- |
| `ModuleNotFoundError: dashboard` / `console` when starting any service | `PYTHONPATH` missing the repo root and `dashboard/` (bug class #1) | `export PYTHONPATH="$PWD:$PWD/dashboard"` from the repo root; in unit files use the `Environment=PYTHONPATH=...` line shown above |
| `ModuleNotFoundError: fastapi` starting the hub | hub runtime deps not installed | `pip install -r hub/requirements.txt` |
| Services crash with `FileNotFoundError` under `~/.aerial/...` | parent directory of a store path doesn't exist — stores are created on demand but parents are not | `mkdir -p ~/.aerial/secrets ~/.aerial/logs ~/.aerial/data` before first boot |
| Hub binds but `/health` says `{"ok": false}` with auth on | no healthcheck has written `health-status.json` yet | run `TICKET_HUB_URL=http://127.0.0.1:8779 bash hub/scripts/healthcheck.sh` on a schedule, or use `TICKET_HUB_AUTH=off` for a loopback demo |
| Console serves but the board shows `degraded`/`stale` sections and the hub shows unreachable | the console reads the token from the deployment profile (`cluster.hub_token_file`), not only the CLI flag — a fresh profile points at `~/.aerial/secrets/hub-token`, which may not hold the hub token | put the hub token at the profile path, or export `AERIAL_CLUSTER__HUB_TOKEN_FILE=/path/to/hub-token` before starting; the first daily render (`dashboard/render_daily.py`) fills degraded sections as data accumulates |
| `curl :8779/dashboard/health` returns `{"ok": false}` or 401 unexpectedly | health endpoint probe without the bearer, or the wrong token file | the health check requires `Authorization: Bearer …`; confirm you're reading the same file the profile (or `--hub-token-file`) points at |
| Scheduled job re-runs lose their command payload | crontab line mangled by comment/`#` parsing — the retrigger must carry everything after `--` | keep the payload after a literal `--` in the cron line; `runner.retrigger_command()` preserves it (asserted in the cold-start suite) |
| Console pages load without any auth on some setups | `--open-read` was passed (or inherited from an old unit) | omit `--open-read`; default is token-gated (no-bearer `/dashboard` returns 401, asserted in the cold-start suite) |
| Ops-agent poll hangs or times out | hub unreachable at `cluster.hub_url`, or seat probes to a dead seat port block | confirm `curl http://127.0.0.1:8021/health`; seat probes fail-closed and just report gaps — check `dashboard/vram_registry.json` entries |

## Footer

Commands verified live against this tree (2026-11-16, isolated demo home
`~/aerial-monorepo-demo`): clone → config → token → hub start
(`python3 hub/api.py`, `TICKET_HUB_HOME/TICKET_HUB_PORT/TICKET_HUB_AUTH`,
`/health` → 200 `{"ok": true, ...}`), console start via
`serve_console.py --port 8779` (bearer health → 200
`{"ok": true, "tickets": 1}`, no-bearer → 401), ops-agent `--once` poll
(exit 0, full reconciliation pass), both suites from the monorepo:
`dashboard/tests` and `hub/tests` green.
