# Bring your own models (BYOM)

Aerial is BYOM by design: your models, your storage, your accounts. Today
that means editing the config files the code really reads — there is no
model-selection UI yet (a selection interface is planned; seats are
configured by editing `dashboard/vram_registry.json`, see
[ROADMAP.md](../ROADMAP.md)).

There are three places models are configured:

| File | What it decides | Read by |
| --- | --- | --- |
| `config/deployment.yaml` | node name, provider/model labels, hub endpoint | `deployment_profile.py` |
| `dashboard/vram_registry.json` | model seats: what runs, where it listens, how VRAM is paged | `dashboard/vram_manager.py`, `bin/aerial_verify.py` |
| `bin/llm_servers.json` | model servers and named roles for governed model calls | `bin/model_call.py` |

All three are plain JSON/YAML. No file below carries secrets in itself —
tokens live in separate files referenced by path.

## 1. The deployment profile — `config/deployment.yaml`

Copy `config/deployment.yaml.example` to `config/deployment.yaml` (it is
gitignored — never commit a real profile). Resolution order, implemented in
`deployment_profile.py`:

1. environment variables `AERIAL_<SECTION>__<KEY>` (double underscore
   nests, e.g. `AERIAL_CLUSTER__MODEL`); flat aliases like
   `AERIAL_MODEL` also work
2. `config/deployment.yaml`
3. generic defaults in `deployment_profile.py`

Model-relevant keys (all under `cluster:`):

- `provider` — free-form provider label (default `custom:aerial-local`)
- `model` — the model label used by callers that read the profile
  (default `local-model`)
- `kimi_service_unit`, `kimi_model_path` — the systemd user unit and
  weight path of a named seat, if you use that naming
- `qwen_container` — container name for a containerised seat
- `hub_url`, `hub_token_file` — the hub the model-calling agents post to

These are labels and locations; the actual model servers are defined by
the seat registry and the model-server config below.

## 2. Model seats — `dashboard/vram_registry.json`

The fleet manager (`dashboard/vram_manager.py`) loads
`dashboard/vram_registry.json`. Top level: `seats` (list) and an optional
`state_path` (learned state file, default `dashboard/vram_state.json`).
Each seat:

- `name` — seat id, also the key used in the state file and journals
- `port` — the port the model server listens on **on 127.0.0.1**; the
  verifier probes `http://127.0.0.1:<port>/v1/models`, so the server must
  speak the OpenAI-compatible API
- `reload_class` — `FAST` or `SLOW`: FAST (llama.cpp-style) seats partially
  offload under pressure (`total_layers`/`full_gib` drive the `-ngl` math);
  SLOW (vLLM-style) seats need their full footprint resident
- `tier` — `A`/`B`/`C` displacement priority (Tier A standing seats are
  never displaced under demand)
- `never_displace` — `true` pins the seat against eviction entirely
- `service_unit` — systemd user unit that owns the server process
- `load_cmd` — command the manager runs to start the seat
- `vram_gib` — measured resident VRAM at load time (managed state; `0`
  until first load)
- `total_layers`, `full_gib` — llama.cpp layer count and full-weight
  footprint, for partial offload

The ops agent probes seat liveness every poll cycle; a dead seat is a
finding, not a mystery.

## 3. Model servers and roles — `bin/llm_servers.json`

`bin/model_call.py` (one governed call for a configured role) reads a
`llm_servers.json` from its own directory (or `--config PATH` /
`--resources PATH`). The public repo ships the **loader, not the file**:
you create `bin/llm_servers.json` for your deployment. Shape:

```json
{
  "servers": {
    "<server-name>": {
      "url": "http://127.0.0.1:<port>",
      "bearer_file": "<path-to-token-file>",
      "max_parallel": 4
    }
  },
  "roles": {
    "<role-alias>": {
      "server": "<server-name>",
      "model": "<model-name>",
      "max_tokens": 4096,
      "reasoning_effort": "low"
    }
  }
}
```

- `servers.<name>.url` — base URL of the model server
- `servers.<name>.bearer_file` — path to a file holding the bearer token
  (`~` is expanded); omit for servers that need no auth. The token is
  read per call and never printed or logged.
- `servers.<name>.max_parallel` — concurrency cap used when waiting for
  pool room (slot-pinning keys `pool_ctx`, `reserve_slots` and a role
  `slot` also exist for slot-based llama-server setups)
- `roles.<alias>.server` / `.model` — which server and served model name
  the role calls
- `roles.<alias>.max_tokens`, `.reasoning_effort` — per-role generation
  caps and reasoning effort

## Worked examples

### (a) One local OpenAI-compatible server

Say you serve one model with llama.cpp on `127.0.0.1:8081`. A systemd user
unit `aerial-llamacpp.service` starts it.

`dashboard/vram_registry.json`:

```json
{
  "seats": [
    {
      "name": "llamacpp",
      "port": 8081,
      "reload_class": "FAST",
      "tier": "B",
      "never_displace": false,
      "service_unit": "aerial-llamacpp.service",
      "load_cmd": "systemctl --user start aerial-llamacpp.service",
      "vram_gib": 0.0,
      "total_layers": 0,
      "full_gib": 0.0
    }
  ]
}
```

`bin/llm_servers.json`:

```json
{
  "servers": {
    "local": {"url": "http://127.0.0.1:8081", "max_parallel": 2}
  },
  "roles": {
    "worker": {"server": "local", "model": "my-model", "max_tokens": 4096}
  }
}
```

Then check it took:

```bash
python3 bin/aerial_verify.py
```

The `model seats` line should report `llamacpp:up`.

### (b) Two models on one GPU node

Give the seat you care about the higher tier and pin it; let the other
displace under demand. vLLM-style seats are `SLOW` (full footprint
resident), llama.cpp-style seats are `FAST` (partial offload):

```json
{
  "seats": [
    {
      "name": "planner",  "port": 8081, "reload_class": "SLOW",
      "tier": "A", "never_displace": true,
      "service_unit": "aerial-planner.service",
      "load_cmd": "systemctl --user start aerial-planner.service"
    },
    {
      "name": "worker",   "port": 8082, "reload_class": "FAST",
      "tier": "B", "never_displace": false,
      "service_unit": "aerial-worker.service",
      "load_cmd": "systemctl --user start aerial-worker.service",
      "total_layers": 80, "full_gib": 48.0
    }
  ]
}
```

Verify with `python3 bin/aerial_verify.py` — both seats should show `up`.
`python3 dashboard/vram_manager.py status` lists what is resident; seats
settle onto VRAM as the pager runs (see the VRAM manager section of
[TECHNICAL.md](../TECHNICAL.md)).

### (c) A hosted API endpoint with its key in a file

The key never goes in a config file — it lives in its own file, referenced
by path:

```bash
mkdir -p ~/.aerial/secrets
printf '%s\n' '<your-token>' > ~/.aerial/secrets/hosted-api-token
chmod 600 ~/.aerial/secrets/hosted-api-token
```

`bin/llm_servers.json`:

```json
{
  "servers": {
    "hosted": {
      "url": "https://api.<your-provider>.example/v1",
      "bearer_file": "~/.aerial/secrets/hosted-api-token",
      "max_parallel": 1
    }
  },
  "roles": {
    "reviewer": {"server": "hosted", "model": "<their-model>", "max_tokens": 4096}
  }
}
```

You can point a seat at a hosted endpoint too, but note the verifier
probes `http://127.0.0.1:<port>/v1/models` on loopback — seat entries are
for locally listening servers; hosted APIs belong in `llm_servers.json`.

## Checking your change took

Run the verify command:

```bash
python3 bin/aerial_verify.py
```

It prints a pass/fail table: hub, console, state dir, usage ledger, and
model seats (each seat probed at `http://127.0.0.1:<port>/v1/models`;
`up`/`DOWN` per seat). Seat and ledger lines are informational on day 1
and fill as the system runs. If a seat you just added shows `DOWN`, check
that the server actually listens on that loopback port and speaks the
OpenAI-compatible API.
