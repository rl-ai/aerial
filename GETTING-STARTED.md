# Getting started

Zero to a running Aerial stack on one machine. Every command below was
verified against a live stack — see the footer.

## 1. Prerequisites

- **Python 3.10+** (the code uses `X | Y` unions). Check: `python3 --version`.
- **No third-party runtime dependencies.** The dashboard, integration, and
  ops-agent code is stdlib-only — there is no `requirements.txt` or
  `pyproject.toml` to install for them. You clone and run. (The
  `gateway/` subpackage ships its own `pyproject.toml`; install it only
  if you use the MCP server: `pip install -e gateway`.)
- `curl` for the health checks below.
- Optional: Docker, if you prefer running services in containers. The
  code itself is container-agnostic; the cold-start sandbox used to
  verify this doc boots the stack in a systemd-in-Docker container.

## 2. Clone and check out

```bash
git clone https://github.com/rl-ai/aerial.git
cd aerial
python3 -m pytest dashboard/tests -q      # optional sanity check, stdlib + pytest
```

There is nothing to `pip install` for the core services — they run
straight from the tree with `PYTHONPATH` set to the repo root plus
`dashboard/` (this is the #1 first-boot failure mode; see
troubleshooting below).

## 3. Minimal configuration

```bash
cp config/deployment.yaml.example config/deployment.yaml
```

Edit `config/deployment.yaml` (it is gitignored — never commit a real
profile). For a first boot the only values worth touching:

- `cluster.node_name` — prefix used in job ids and dispatch (`aerial` by
  default).
- `email.recipient_email` — leave **empty** to keep email delivery
  disabled (the default; `send_report` becomes a no-op).
- `paths.state_dir`-backed paths default to `~/.aerial/` — that
  directory is created on demand, but its **parent** must exist.

Everything can also be set with environment variables
(`AERIAL_<SECTION>__<KEY>`, e.g. `AERIAL_EMAIL__RECIPIENT_EMAIL`),
which override the YAML, which overrides the defaults in
`deployment_profile.py`.

Token files the services read:

- `~/.aerial/secrets/hub-token` — the bearer token shared with the hub
  (`cluster.hub_token_file`). Create the parent dir and put a secret in
  it:

  ```bash
  mkdir -p ~/.aerial/secrets
  head -c 32 /dev/urandom | xxd -p -c 64 > ~/.aerial/secrets/hub-token
  chmod 600 ~/.aerial/secrets/hub-token
  ```

## 4. Start the ticket hub

The hub is the HTTP system of record (tickets, job_runs, mandates) on
`127.0.0.1:8020` with a SQLite database. It ships in this repository
under `hub/`:

```bash
TICKET_HUB_HOME=~/.aerial/hub-home python3 hub/api.py
```

- `TICKET_HUB_HOME` — where the hub keeps its database and health status
- `TICKET_HUB_AUTH` — set to `off` only for a loopback first boot; the
  default authenticates every non-health request with the hub token

Verify it is live:

```bash
curl -s http://127.0.0.1:8020/health        # -> {"ok": true, ...}
```

On a fresh install the health note reads "no healthcheck has run yet" —
that is expected until `scripts/healthcheck.sh` first runs. Without a
hub the console and board still render (fail-closed, degraded banners),
but the ops agent has nothing to reconcile against.

Run it in the background for a long-lived deployment (systemd unit in
step 6), or `nohup ... &` for a quick tour.

## 5. Start the operator console

```bash
cd aerial                                    # repo root
export PYTHONPATH="$PWD:$PWD/dashboard"
nohup python3 dashboard/serve_console.py --host 127.0.0.1 \
      --hub-token-file ~/.aerial/secrets/hub-token \
      > ~/.aerial/logs/console.log 2>&1 &
```

- Listens on `127.0.0.1:8777`, serves `/dashboard`.
- Auth: the **read token is the hub token file** above; every request
  needs `Authorization: Bearer <token>`. Pass
  `--write-token-file <other file>` with a **distinct** secret to
  enable decision approvals — sharing the read token as the write token
  is a known-bad configuration (attack-swarm finding; the read token
  POST now gets a 401). Without `--write-token-file` the console is
  read-only.
- `--open-read` makes the read page unauthenticated — only do this on
  loopback and only if you accept the tradeoff; the default is
  token-gated.

Verify:

```bash
T=$(cat ~/.aerial/secrets/hub-token)
curl -s -H "Authorization: Bearer $T" http://127.0.0.1:8777/dashboard/health
# -> {"ok": true, "tickets": N}
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8777/dashboard/health
# -> 401 (no bearer)
```

## 6. Start the ops agent and scheduler

Smoke-test first with a single poll:

```bash
cd aerial && export PYTHONPATH="$PWD:$PWD/dashboard"
mkdir -p ~/.aerial/logs
python3 -m dashboard.followup.runner --once --log ~/.aerial/logs/ops-agent-once.log
```

Then run them for real. systemd **user units** (from a verified
deployment — adapt paths):

```ini
# ~/.config/systemd/user/aerial-console.service
[Unit]
Description=Aerial operator console (:8777, bearer-authed dashboard)
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

If you don't run systemd, `nohup ... &` (with `PYTHONPATH` exported)
works the same — that is exactly how the cold-start sandbox boots the
stack.

## 7. Verify the stack

```bash
curl -s http://127.0.0.1:8020/health                       # hub:      {"ok": true}
T=$(cat ~/.aerial/secrets/hub-token)
curl -s -H "Authorization: Bearer $T" http://127.0.0.1:8777/dashboard/health
                                                           # console:  {"ok": true, ...}
grep -c INFO ~/.aerial/logs/ops-agent-once.log             # ops agent ran a full poll
```

The first findings appear after the ops agent's first poll: schedule
reconciliation, seat liveness, and conformance checks produce entries
in the exception queue / daily board (findings against a healthy empty
system are normal — e.g. seat gaps if you run no model seats).

## 8. What you'll see on day 1

- **Daily board** (`/dashboard`, plus the static snapshot the console
  refreshes per request): ticket queue, schedule reconciliation
  findings, seat liveness, exec-summary headlines.
- **Exec report**: three-headline brief plus per-section detail,
  rendered by `dashboard/exec_summary.py`; emailed only if
  `email.recipient_email` is set (otherwise delivery is a no-op).
- **Followup queue / exception queue**: R1–R6-classified findings with
  severity routing — sev-incident/security pings immediately, safe-fix
  sessions for reversible issues, everything else queued for operator
  review.

## Troubleshooting — top first-boot failure modes

| Symptom | Cause | Fix |
| --- | --- | --- |
| `ModuleNotFoundError: dashboard` / `console` when starting any service | `PYTHONPATH` missing the repo root and `dashboard/` (bug class #1) | `export PYTHONPATH="$PWD:$PWD/dashboard"` from the repo root; in unit files use the `Environment=PYTHONPATH=...` line shown above |
| Services crash with `FileNotFoundError` under `~/.aerial/...` | parent directory of a store path doesn't exist — stores are created on demand but parents are not | `mkdir -p ~/.aerial/secrets ~/.aerial/logs ~/.aerial/data` before first boot |
| Console serves but the board shows `degraded`/`stale` sections and the hub shows unreachable | the console's hub client reads the token from the deployment profile (`cluster.hub_token_file`), not only the CLI flag — a fresh profile points at `~/.aerial/secrets/hub-token`, which may not hold the hub token | put the hub token at the profile path, or export `AERIAL_CLUSTER__HUB_TOKEN_FILE=/path/to/hub-token` before starting; the first daily render (`dashboard/render_daily.py`) fills degraded sections as data accumulates |
| `curl :8777/dashboard/health` returns `{"ok": false}` or 401 unexpectedly | health endpoint probe without the bearer, or the wrong token file | the health check requires `Authorization: Bearer <hub-token>`; confirm you're reading the same file passed via `--hub-token-file` |
| Scheduled job re-runs lose their command payload | crontab line mangled by comment/`#` parsing — the retrigger must carry everything after `--` | keep the payload after a literal `--` in the cron line; `runner.retrigger_command()` preserves it (asserted in the cold-start suite) |
| Console pages load without any auth on some setups | `--open-read` was passed (or inherited from an old unit) | omit `--open-read`; default is token-gated (no-bearer `/dashboard` returns 401, asserted in the cold-start suite) |
| Ops-agent poll hangs or times out | hub unreachable at `cluster.hub_url`, or seat probes to a dead seat port block | confirm `curl http://127.0.0.1:8020/health`; seat probes fail-closed and just report gaps — check `dashboard/vram_registry.json` entries |

## Footer

Commands verified against v1.0-mvp. Verified live: hub `/health` (200
`{"ok": true}`), console start via `serve_console.py` (bearer health →
200 `{"ok": true, "tickets": 1}`, no-bearer health and `/dashboard` →
401), ops-agent `--once` poll (full reconciliation pass, stores
created), `scheduler.py --help`, profile load. The console, ops-agent,
and scheduler boot sequence mirrors the cold-start sandbox
(systemd + chroot-lite modes).
