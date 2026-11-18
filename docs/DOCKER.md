# Docker

Run the whole Aerial demo stack — ticket hub, operator console and ops
agent — with `docker compose`. Images are built locally from this tree;
nothing is pulled from a registry except the `python:3.12-slim` base image.

What you get:

| Service | Image | Port |
| --- | --- | --- |
| `hub` | built from `./hub` (see `hub/Dockerfile`) | internal only (`hub:8777`) |
| `console` | one shared image built from the repo root (`dashboard/Dockerfile`) | published on `127.0.0.1:8779` |
| `ops-agent` | same shared image as `console` | none (polls the hub) |

## 1. Prerequisites

- Docker with the Compose v2 plugin (`docker compose version`).
- A clone of this repository.
- No other services on host port **8779** (the console).

## 2. Create the hub token file

The hub token is passed to the console and ops agent as a Compose secret
backed by a local file. Create it once (64 hex chars, matching the
walkthrough in GETTING-STARTED.md section 3):

```bash
mkdir -p secrets
head -c 32 /dev/urandom | xxd -p -c 64 > secrets/hub-token
chmod 600 secrets/hub-token
```

`secrets/` is excluded by `.dockerignore` and the repository's gitignore
rules — the token file is never committed and never baked into an image.
It is mounted read-only into the containers at `/run/secrets/hub-token`.

## 3. Start the stack

```bash
docker compose up -d
```

The first run builds two images locally (`aerial-hub` from `./hub`, and a
shared dashboard image from the repo root used by both `console` and
`ops-agent`). The console and ops agent wait for the hub healthcheck to
pass before starting.

## 4. Verify

The healthchecks are the same endpoints as GETTING-STARTED.md section 7:
the hub answers a public `GET /health`; the console requires the bearer
token on `GET /dashboard/health`. Check them from the host side:

```bash
docker compose ps                                   # all services healthy
docker compose exec hub python3 -c \
  "import json,urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:8777/health'))['ok'])"
# -> True
```

From the console container (which has the token at `/run/secrets/hub-token`):

```bash
docker compose exec console python3 -c \
  "import json,urllib.request; t=open('/run/secrets/hub-token').read().strip(); rq=urllib.request.Request('http://127.0.0.1:8779/dashboard/health', headers={'Authorization': 'Bearer ' + t}); print(json.load(urllib.request.urlopen(rq)))"
# -> {"ok": true, ...}
```

Then run the repo's end-to-end verifier from the console container:

```bash
docker compose exec console python3 aerial_verify.py
```

It prints a pass/fail table and exits 0 when the whole chain is healthy.

The console itself is on <http://127.0.0.1:8779/dashboard> (host loopback
only; the read page is bearer-gated unless you deliberately change it).

## 5. Logs

```bash
docker compose logs -f hub console ops-agent
docker compose exec console cat /home/aerial/.aerial/logs/ops-agent.log
```

## 6. Teardown

```bash
docker compose down          # stop and remove containers, keep state
docker compose down -v       # also delete the state volumes (hub + demo state)
```

## Notes

- Loopback demo configuration: the hub runs with `TICKET_HUB_AUTH=off`
  (GETTING-STARTED.md section 4) inside the compose network; the hub port
  is not published to the host. The console and ops agent still present
  the hub token.
- Inside the container the console binds `0.0.0.0` (the code's default
  `127.0.0.1` would only bind container loopback and the published port
  would never answer); the host side stays restricted to `127.0.0.1` by
  the `ports:` mapping.
- State volumes: `aerial-state` (console/ops agent, mounted at the
  profile's `~/.aerial`) and `hub-state` (the hub's `TICKET_HUB_HOME`).
