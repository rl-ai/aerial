# Features

**Pilot-less, automated agent ops. Agents fly solo — always finding opportunities for you.**

Aerial is the control tower for autonomous agent fleets. Below: the head-to-head against the strongest execution harness in the category, then everything in the box.

*(A few components live in the internal deployment and are noted where so.)*

---

## Head to head — Aerial vs DeepSeek Harness

Aerial is pilot-less operations for autonomous agents — agents fly solo, Aerial runs the control tower. Compared feature-for-feature with **DeepSeek Harness (DSH)**:

| Feature | DSH | Aerial |
|---|---|---|
| **Model choice** | Runs DeepSeek's models (V4.1-Flash) — model breadth beyond the DeepSeek family isn't part of the product today | 🚀 Any model — local VRAM, self-hosted endpoints, or cloud APIs; you choose the placement |
| **Privacy & data control** | Open source desktop app, but the models run in DeepSeek's cloud | 🚀 Everything local — models, storage, and accounts are yours. No telemetry: no data ever leaves your machine |
| **Autonomy** | Executes prompts and scheduled tasks (marked experimental) — reactive rather than self-starting | 🚀 Self-starting agents discover opportunities and execute autonomously — acting on what they find without being asked |
| **Failure handling** | Execution traces and developer tools for inspection — but triage and the fix are on you | 🚀 Triage → ticket → governed fix → verified; infrastructure recovers on its own |
| **Centralized context management** | Context lives inside one conversation — a second agent starts from zero | 🚀 Tickets are the shared brain: multiple agents and roles work the same ticket, each inheriting the full chain of evidence, decisions, and history |
| **Transparency & reporting** | Developer tools show execution traces — durations, turns, tool calls — but no per-seat cost or fleet reporting | 🚀 Per-call and per-seat tokens, tok/s, concurrency — 15-minute reports |
| **Hardware utilization** | Single-user desktop focus — the rest of your hardware waits | 🚀 Full fleet saturated 24/7 — demand displacement, seat-swap, idle-work broker |
| **Harness compatibility** | Is a harness | 🚀 Runs over your harness; ships Hermes-native, others are a small adapter |
| **Interactive experience** | 🚀 Polished chat, file previews, Creator mode | Operational console built for oversight, not conversation |
| **Extensibility** | 🚀 Plugin marketplace + Creator mode | Governed jobs and the hub API |
| **Open source** | ✅ | ✅ MIT |

> **DSH executes. Aerial operates.** DSH is the aircraft — polished, fast, a genuine pleasure for interactive work. Aerial is the control tower around it.

> *\*The supervision layer speaks HTTP + files. Ships Hermes-native; other harnesses are a small adapter away.*

---

## The box — what's inside

### See everything — operator surface

You shouldn't have to open six terminals to know how your fleet slept. One board, one inbox, one queue.

- **Operator console** — a bearer-authed dashboard on `:8777`: daily board, sparklines, decision actions. Read-only unless you hand it a separate write token. → `dashboard/serve_console.py`
- **Daily board** — the morning view: what ran, what failed, what it cost, rendered from live hub and ledger data. → `dashboard/render_daily.py`
- **Exec report** — three headlines a busy operator actually reads. → `dashboard/exec_summary.py`
- **Email delivery** — the report in your inbox, daily. Off until you set a recipient. → `dashboard/email_delivery.py`
- **Exception queue** — anything risky waits for a human: approve, dismiss, escalate — with evidence attached. → `dashboard/exception_queue/`

### Never wake up to a surprise — automation

Autonomous triage: failures get classified, filed, fixed, and verified — the fix backed by root-cause analysis, the resolution backed by evidence.

- **Ops agent** — a 5-minute always-on loop: poll, classify, act, verify. → `dashboard/followup/runner.py`
- **Failure triage (R1–R6)** — security breaches ping immediately; unknown failure classes fail closed. → `dashboard/followup/classifier.py`
- **Safe fixes** — reversible findings get a governed fix attempt, verified against the exact symptom. Verified fixes close silently; failures escalate. → `dashboard/followup/flow.py`
- **Shadow scheduler** — new behavior observes before it goes live, chained curator → lead → exec sessions over real failures. → `integration/scheduler.py`

### Your GPUs, fully loaded — model fleet

Saturation is the standard: models live on VRAM by default, idle gets displaced, and self-starting agents keep every seat working 24/7. Self-healing closes the loop when a seat dies.

- **Seat liveness** — every model seat probed; a dead seat is a finding, not a mystery. → `dashboard/vram_registry.json`
- **VRAM manager** — models live on VRAM by default; demand displaces idle, pressure evicts. → `dashboard/vram_manager.py`
- **Seat swap** — one command to free or load a seat. → `dashboard/seat-swap.sh`
- **Idle-work broker** — measured-idle seats pull from a pre-approved backlog. No plane sits idle. *(internal deployment)*
- **Usage telemetry** — per-seat and per-call tokens, tok/s, concurrency — on a 15-minute report. → `dashboard/data_layer/model_calls_tailer.py`

### Trust the output — quality machinery

Autonomy without evidence is just guessing. The gauntlet and promotion gate turn "done" into verified, evidence-backed fact — 5/5 or it doesn't ship.

- **Gauntlet** — a 5-layer acceptance harness (units, adversarial stress, backtest, day-in-the-life, live smoke). 5/5 or no release. → `dashboard/gauntlet.py`
- **Chaos virtual days** — a week of failures validated in minutes, deterministically. → `dashboard/chaos.py`
- **Promotion gate** — divergence, cost, and observation-window checks before new board state is accepted. → `dashboard/daily_board/promotion.py`
- **Best-practice knowledge base** — curated operational practices with gap detection. → `dashboard/bpkb/`
- **Adversarial stress harness** — attack scenarios with ground-truth scoring for agent honesty and cause-finding. *(internal deployment)*

### Plug into your world — integration

This is where proactive agents earn the name: discovering sources, finding opportunities, and filing them for review — work nobody prompted.

- **Scrape ingest bridge** — external intel flows into the board. → `dashboard/scrape_intel.py`
- **Listening pipeline** — degree-expansion source discovery plus a SWOT cadence that files evidence-backed opportunities into the exception queue. → `gateway/secondbrain/`
- **Gateway MCP + version snapshots** — the second-brain vault over MCP, with pinned, replayable versions. → `gateway/secondbrain_mcp/`

### Boring where it counts — ops

The unglamorous layer that makes everything above survive a reboot, a rename, and a second operator.

- **One config source** — env overrides → YAML → sane defaults; a test proves the stack boots with zero personal values. → `deployment_profile.py`
- **Token scoping** — actor×family×action hub tokens, deny-by-default; console writes need a token distinct from reads. → `dashboard/console/actions.py`
- **Boot-persistent services** — systemd units for console, ops agent, scheduler. → [GETTING-STARTED.md](GETTING-STARTED.md)
- **Cold-start sandbox** — the whole stack boots in a dockerized systemd container, first-boot assertions included. *(internal deployment)*


