# Features

What Aerial does, by area. *(A few components live in the internal deployment and are noted where so.)*

## Why Aerial over DeepSeek Harness

Aerial is an operations layer, not another chat surface. Compared feature-for-feature with **DeepSeek Harness (DSH)** — the strongest execution harness in the category:

| Feature | 🛩️ DSH | 🗼 Aerial |
|---|---|---|
| **Fleet-wide operations, 24/7** — an ops agent that polls, classifies, fixes, and verifies without you | | ✅ |
| **Every GPU working** — models resident on VRAM, demand displaces idle, empty seats take backlog work | | ✅ |
| **Failure triage that ends in a fix** — not a trace to read, but a verified resolution | | ✅ |
| **Evidence-backed audit trail** — every claim on a ticket, releases gated 5/5 by the gauntlet | | ✅ |
| **Cost per call and per seat** — tokens, tok/s, concurrency, on a 15-minute report | | ✅ |
| **Self-healing seats** — dead seats detect, eject, relaunch, verify autonomously | | ✅ |
| **Fully local** — hub, console, runners, and models on your hardware; zero telemetry | | ✅ |
| **Runs on your harness** — supervision layer speaks HTTP + files; ships Hermes-native, other harnesses are a small adapter | | ✅* |
| **Interactive execution** — conversational work with file previews | ✅ | capable via agents |
| **Plugin ecosystem** — marketplace plus plugins built from chat (Creator mode) | ✅ | extends via governed jobs |
| **Open source** | ✅ | ✅ MIT |

DSH is the aircraft — polished, fast, a genuine pleasure for interactive work. Aerial is the control tower. They compose: DSH executes, Aerial operates.

*\*The supervision layer speaks HTTP + files. Ships Hermes-native; other harnesses are a small adapter away.*

---


## See everything — operator surface

You shouldn't have to open six terminals to know how your fleet slept. One board, one inbox, one queue.

- **Operator console** — a bearer-authed dashboard on `:8777`: daily board, sparklines, decision actions. Read-only unless you hand it a separate write token. → `dashboard/serve_console.py`
- **Daily board** — the morning view: what ran, what failed, what it cost, rendered from live hub and ledger data. → `dashboard/render_daily.py`
- **Exec report** — three headlines a busy operator actually reads. → `dashboard/exec_summary.py`
- **Email delivery** — the report in your inbox, daily. Off until you set a recipient. → `dashboard/email_delivery.py`
- **Exception queue** — anything risky waits for a human: approve, dismiss, escalate — with evidence attached. → `dashboard/exception_queue/`

## Never wake up to a surprise — automation

Most agent failures die quietly and wait for you. These don't: they get classified, filed, fixed, and verified before you've poured coffee.

- **Ops agent** — a 5-minute always-on loop: poll, classify, act, verify. → `dashboard/followup/runner.py`
- **Failure triage (R1–R6)** — security breaches ping immediately; unknown failure classes fail closed. → `dashboard/followup/classifier.py`
- **Safe fixes** — reversible findings get a governed fix attempt, verified against the exact symptom. Verified fixes close silently; failures escalate. → `dashboard/followup/flow.py`
- **Shadow scheduler** — new behavior observes before it goes live, chained curator → lead → exec sessions over real failures. → `integration/scheduler.py`

## Your GPUs, fully loaded — model fleet

You paid for the VRAM. The fleet features make sure it's always holding the model that's needed — and never holding one that isn't.

- **Seat liveness** — every model seat probed; a dead seat is a finding, not a mystery. → `dashboard/vram_registry.json`
- **VRAM manager** — models live on VRAM by default; demand displaces idle, pressure evicts. → `dashboard/vram_manager.py`
- **Seat swap** — one command to free or load a seat. → `dashboard/seat-swap.sh`
- **Idle-work broker** — measured-idle seats pull from a pre-approved backlog. No plane sits idle. *(internal deployment)*
- **Usage telemetry** — per-seat and per-call tokens, tok/s, concurrency — on a 15-minute report. → `dashboard/data_layer/model_calls_tailer.py`

## Trust the output — quality machinery

"Done" is a claim. These are the mechanisms that turn it into evidence.

- **Gauntlet** — a 5-layer acceptance harness (units, adversarial stress, backtest, day-in-the-life, live smoke). 5/5 or no release. → `dashboard/gauntlet.py`
- **Chaos virtual days** — a week of failures validated in minutes, deterministically. → `dashboard/chaos.py`
- **Promotion gate** — divergence, cost, and observation-window checks before new board state is accepted. → `dashboard/daily_board/promotion.py`
- **Best-practice knowledge base** — curated operational practices with gap detection. → `dashboard/bpkb/`
- **Adversarial stress harness** — attack scenarios with ground-truth scoring for agent honesty and cause-finding. *(internal deployment)*

## Plug into your world — integration

Agents that only see what you paste at them are half agents. These pipes bring the outside in — and surface opportunities you didn't ask for.

- **Scrape ingest bridge** — external intel flows into the board. → `dashboard/scrape_intel.py`
- **Listening pipeline** — degree-expansion source discovery plus a SWOT cadence that files evidence-backed opportunities into the exception queue. → `gateway/secondbrain/`
- **Gateway MCP + version snapshots** — the second-brain vault over MCP, with pinned, replayable versions. → `gateway/secondbrain_mcp/`

## Boring where it counts — ops

The unglamorous layer that makes everything above survive a reboot, a rename, and a second operator.

- **One config source** — env overrides → YAML → sane defaults; a test proves the stack boots with zero personal values. → `deployment_profile.py`
- **Token scoping** — actor×family×action hub tokens, deny-by-default; console writes need a token distinct from reads. → `dashboard/console/actions.py`
- **Boot-persistent services** — systemd units for console, ops agent, scheduler. → [GETTING-STARTED.md](GETTING-STARTED.md)
- **Cold-start sandbox** — the whole stack boots in a dockerized systemd container, first-boot assertions included. *(internal deployment)*


