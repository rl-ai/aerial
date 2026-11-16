1|# Aerial
2|
3|Aerial is an always-on operations platform for a small local AI cluster:
4|a ticket hub, an ops agent that polls → classifies → acts → verifies,
5|an operator console, a daily exec report, and an integration harness
6|that validates the whole pipeline on compressed virtual days.
7|
8|This is the open-source split of an internal deployment. Everything
9|runs with **generic defaults** out of the box: `localhost` URLs, no
10|email recipient (delivery disabled), and runtime state under
11|`~/.aerial/`. Deployment-specific values (node names, recipients,
12|host paths, service units) live in a gitignored profile — see
13|[Deployment profile](#deployment-profile).
14|
15|## Layout
16|
17|| Path | What it is |
18|| --- | --- |
19|| `dashboard/` | Daily board, exec report, operator console, followup/ops-agent loop, gauntlet, VRAM manager |
20|| `integration/` | Scheduler, shadow-hierarchy runner, qualification harness, hermetic-cycle tests |
21|| `gateway/` | The second-brain gateway: MCP server + bridge to the ticket hub |
22|| `docs/` | Testing service, pipeline checkpoints, decision points, buglist |
23|| `config/deployment.yaml.example` | The deployment profile template |
24|
25|## Quick start
26|
27|```bash
28|cp config/deployment.yaml.example config/deployment.yaml   # optional
29|python3 -m pytest dashboard/tests -q                       # unit suites
30|python3 -m pytest integration -q                           # integration suites
31|```
32|
33|The scheduler and runners accept explicit flags (`--hub-db`, `--hub-url`,
34|…) or read the profile. Nothing phones home; the hub, console, and seats
35|are all services you run yourself.
36|
37|## Deployment profile
38|
39|All deployment-specific values come from, in order of precedence:
40|
41|1. Environment variables — `AERIAL_<SECTION>__<KEY>` (double underscore
42|   nests), e.g. `AERIAL_EMAIL__RECIPIENT_EMAIL=ops@example.com`. Flat
43|   aliases like `AERIAL_NODE_NAME` also work.
44|2. `config/deployment.yaml` (copy from `deployment.yaml.example`; it is
45|   gitignored — never commit a real profile).
46|3. Generic defaults in `deployment_profile.py` (localhost, empty
47|   recipient, `~/.aerial` state root).
48|
49|Key settings:
50|
51|- `email.recipient_email` — empty disables email delivery entirely.
52|- `cluster.node_name`, `cluster.provider`, `cluster.model` — the node
53|  prefix used in job ids, service units, and agent dispatch.
54|- `paths.state_dir` — root for logs, data, hub snapshots, workspaces.
55|- `paths.repo_paths` — git repos scanned for the daily DONE digest.
56|
57|## Docs
58|
59|- [`docs/testing-service.md`](docs/testing-service.md) — the testing service and how suites map to it
60|- [`docs/pipeline-checkpoints.md`](docs/pipeline-checkpoints.md) — PC-1..PC-7 conformance checkpoints
61|- [`docs/decision-points.md`](docs/decision-points.md) — recorded decision points
62|- [`docs/BUGLIST.md`](docs/BUGLIST.md) — known issues
63|
64|See also [PRIVACY.md](PRIVACY.md), [SECURITY.md](SECURITY.md),
65|[TECHNICAL.md](TECHNICAL.md), [CONTRIBUTING.md](CONTRIBUTING.md),
66|[FAQ.md](FAQ.md).

## License

MIT — see [LICENSE](LICENSE).
