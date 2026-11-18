# Installing Aerial with pip

The repository root ships a `pyproject.toml` (PEP 621, setuptools). A single
`pip install .` from a clone installs the console and ops agent with working
command-line entry points — no `PYTHONPATH` edits, which are the #1 first-boot
failure mode when running the scripts by hand (see the troubleshooting table
in [GETTING-STARTED.md](../GETTING-STARTED.md)).

## 1. Create a virtualenv

```bash
git clone https://github.com/rl-ai/aerial.git
cd aerial
python3 -m venv .venv
source .venv/bin/activate
```

Python 3.10+ is required (the code uses `X | Y` unions).

## 2. Install

The console, ops agent and `aerial-verify` are stdlib-only, so the base
install has no dependencies:

```bash
pip install .
```

To also run the ticket hub (`hub/`, which needs fastapi + uvicorn — the
contents of `hub/requirements.txt`):

```bash
pip install ".[hub]"
```

`hub/` and `gateway/` keep their own `pyproject.toml` files and are not
packaged by the root install; `pip install ./hub` still works if you want the
hub as its own distribution.

## 3. The commands

After install, three scripts are on your `PATH`:

| Command | Runs | Source |
| --- | --- | --- |
| `aerial-console` | the operator console (`dashboard/serve_console.py`) | `dashboard.serve_console:main` |
| `aerial-ops-agent` | the ops agent (`python3 -m dashboard.followup.runner`) | `dashboard.followup.runner:main` |
| `aerial-verify` | the verify lane (`aerial_verify.py` at the repo root) | `aerial_verify:main` |

The console and ops agent go through a small shim module (`aerial_entry.py`)
because the dashboard tree imports siblings bare (`from deployment_profile
import value`, `import render_daily`, `from console.serve import ...`) and
was written to run with the repo root **and** `dashboard/` on `sys.path`.
The shim reproduces exactly that path order (root first) before importing
the real `main`. No module was moved or renamed.

Typical usage (same flags as the underlying modules — nothing invented):

```bash
# Operator console (bearer-authed; --write-token-file enables approvals)
aerial-console --port 8779 --host 127.0.0.1

# Ops agent: one poll cycle, or the always-on 5-minute loop
aerial-ops-agent --once --log ~/.aerial/logs/ops-agent-once.log
aerial-ops-agent --interval 300

# Verify lane (0 = all checks pass, 1 = a check failed, 2 = usage error)
aerial-verify
```

`aerial-console` accepts the same options as `dashboard/serve_console.py`:
`--port` / `--host`, `--hub-url`, `--hub-token-file`,
`--write-token-file` (must be a DISTINCT secret from the read token),
`--open-read` (loopback only; the default is token-gated).

## 4. What still needs config

pip only puts the code on your path. The runtime steps in
[GETTING-STARTED.md](../GETTING-STARTED.md) are unchanged:

- **Deployment profile** — copy `config/deployment.yaml.example` to
  `config/deployment.yaml` and set `cluster.hub_url` /
  `cluster.hub_token_file` ("Configuration, state dir, token", section 3).
  Environment variables (`AERIAL_CLUSTER__HUB_URL`, ...) override the YAML.
- **Hub token** — create the shared secret file
  (`head -c 32 /dev/urandom | xxd -p -c 64 > <token-file>`, section 3).
- **State directories** — `mkdir -p ~/.aerial/secrets ~/.aerial/logs
  ~/.aerial/data` (stores create files on demand but not parent dirs).
- **The ticket hub itself** — started with `python3 hub/api.py` and the
  `TICKET_HUB_*` environment variables (`TICKET_HUB_PORT`, `TICKET_HUB_HOME`,
  `TICKET_HUB_AUTH`, ...); see "Start the ticket hub", section 4. With the
  `hub` extra installed, `fastapi`/`uvicorn` are already present.
- **systemd units** — replace the `Environment=PYTHONPATH=...` line and
  script paths in the section 6 unit files with the installed commands,
  e.g. `ExecStart=%h/.venv/bin/aerial-console --host 127.0.0.1` and
  `ExecStart=%h/.venv/bin/aerial-ops-agent --interval 300`.
