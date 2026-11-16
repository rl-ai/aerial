# Aerial

Aerial is an always-on operations platform for a small local AI cluster:
a ticket hub, an ops agent that polls → classifies → acts → verifies,
an operator console, a daily exec report, and an integration harness
that validates the whole pipeline on compressed virtual days.

This is the open-source split of an internal deployment. Everything
runs with **generic defaults** out of the box: `localhost` URLs, no
email recipient (delivery disabled), and runtime state under
`~/.aerial/`. Deployment-specific values (node names, recipients,
host paths, service units) live in a gitignored profile — see
[Deployment profile](#deployment-profile).

## Layout

| Path | What it is |
| --- | --- |
| `dashboard/` | Daily board, exec report, operator console, followup/ops-agent loop, gauntlet, VRAM manager |
| `integration/` | Scheduler, shadow-hierarchy runner, qualification harness, hermetic-cycle tests |
| `gateway/` | The second-brain gateway: MCP server + bridge to the ticket hub |
| `deployment_profile.py` | Single source of config defaults (env vars / deployment.yaml override it) |
| `docs/` | Testing service, pipeline checkpoints, decision points, buglist |
| `config/deployment.yaml.example` | The deployment profile template |

## Quick start

```bash
cp config/deployment.yaml.example config/deployment.yaml   # optional
python3 -m pytest dashboard/tests -q                       # unit suites
python3 -m pytest integration -q                           # integration suites
```

Requires Python 3.10+ (no third-party runtime dependencies). The hub,
console, and seats are services you run yourself - see [TECHNICAL.md](TECHNICAL.md)
for the components.

The scheduler and runners accept explicit flags (`--hub-db`, `--hub-url`,
…) or read the profile. Nothing phones home; the hub, console, and seats
are all services you run yourself.

## Deployment profile

All deployment-specific values come from, in order of precedence:

1. Environment variables — `AERIAL_<SECTION>__<KEY>` (double underscore
   nests), e.g. `AERIAL_EMAIL__RECIPIENT_EMAIL=ops@example.com`. Flat
   aliases like `AERIAL_NODE_NAME` also work.
2. `config/deployment.yaml` (copy from `deployment.yaml.example`; it is
   gitignored — never commit a real profile).
3. Generic defaults in `deployment_profile.py` (localhost, empty
   recipient, `~/.aerial` state root).

Key settings:

- `email.recipient_email` — empty disables email delivery entirely.
- `cluster.node_name`, `cluster.provider`, `cluster.model` — the node
  prefix used in job ids, service units, and agent dispatch.
- `paths.state_dir` — root for logs, data, hub snapshots, workspaces.
- `paths.repo_paths` — git repos scanned for the daily DONE digest.

## Docs

- [`docs/testing-service.md`](docs/testing-service.md) — the testing service and how suites map to it
- [`docs/pipeline-checkpoints.md`](docs/pipeline-checkpoints.md) — PC-1..PC-7 conformance checkpoints
- [`docs/decision-points.md`](docs/decision-points.md) — recorded decision points
- [`docs/BUGLIST.md`](docs/BUGLIST.md) — running fix log (resolved and open items)

See also [PRIVACY.md](PRIVACY.md), [SECURITY.md](SECURITY.md),
[TECHNICAL.md](TECHNICAL.md), [CONTRIBUTING.md](CONTRIBUTING.md),
[FAQ.md](FAQ.md).

## License

MIT — see [LICENSE](LICENSE).
