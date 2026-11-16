# Features

What Aerial does, by area. *(A few components live in the internal deployment and are noted where so.)*

## Why Aerial over DeepSeek Harness

Aerial is pilot-less operations for autonomous agents — agents fly solo, Aerial runs the control tower. Compared feature-for-feature with **DeepSeek Harness (DSH)**, the strongest execution harness in the category:

| Feature | DSH | Aerial |
|---|---|---|
| **Privacy and control, first** — you choose the models, the storage locations, and the accounts to connect | Decisions default to the vendor's cloud | 🚀 Every placement is yours: local or cloud models, local storage, your accounts — nothing phones home |
| **Autonomous, self-starting agents** — they find work and opportunities on their own, not just execute prompts | Reactive: acts on prompts and schedules you set | 🚀 Proactive: discovers opportunities through its listening pipeline, self-assigns backlog work, and acts without being asked |
| **Bring your own model** — load models onto or from any resource, local or cloud | Locked to DeepSeek's cloud models | 🚀 Any model, any placement: local VRAM seats, self-hosted endpoints, or cloud APIs |
| **Self-healing and self-maintenance** — failures triage to fixes, and the infrastructure recovers on its own | Leaves you a trace to read and a mystery to solve | 🚀 Failures classified → ticketed → governed safe-fix → verified; dead seats eject, relaunch, and recover autonomously |
| **Context management** — every action carries its evidence | Conversation history is the only record | 🚀 Every claim on a hub ticket with evidence labels; the full chain survives beyond the chat |
| **Transparency and reporting** — see what the work actually costs | Buried in conversation logs | 🚀 Tokens, tok/s, concurrency per seat — a 15-minute report, not an archaeology dig |
| **Runs on your harness** | Is a harness | 🚀 Supervision layer speaks HTTP + files; ships Hermes-native, others are a small adapter |
| **Interactive execution** | 🚀 Conversational work with file previews, Creator mode | Capable through its agents |
| **Plugin ecosystem** | 🚀 Marketplace plus plugins built from chat | Extends via governed jobs and the hub API |
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

Autonomous triage: failures get classified, filed, fixed, and verified — the fix backed by root-cause analysis, the resolution backed by evidence.

- **Ops agent** — a 5-minute always-on loop: poll, classify, act, verify. → `dashboard/followup/runner.py`
- **Failure triage (R1–R6)** — security breaches ping immediately; unknown failure classes fail closed. → `dashboard/followup/classifier.py`
- **Safe fixes** — reversible findings get a governed fix attempt, verified against the exact symptom. Verified fixes close silently; failures escalate. → `dashboard/followup/flow.py`
- **Shadow scheduler** — new behavior observes before it goes live, chained curator → lead → exec sessions over real failures. → `integration/scheduler.py`

## Your GPUs, fully loaded — model fleet

Saturation is the standard: models live on VRAM by default, idle gets displaced, and self-starting agents keep every seat working 24/7. Self-healing closes the loop when a seat dies.

- **Seat liveness** — every model seat probed; a dead seat is a finding, not a mystery. → `dashboard/vram_registry.json`
- **VRAM manager** — models live on VRAM by default; demand displaces idle, pressure evicts. → `dashboard/vram_manager.py`
- **Seat swap** — one command to free or load a seat. → `dashboard/seat-swap.sh`
- **Idle-work broker** — measured-idle seats pull from a pre-approved backlog. No plane sits idle. *(internal deployment)*
- **Usage telemetry** — per-seat and per-call tokens, tok/s, concurrency — on a 15-minute report. → `dashboard/data_layer/model_calls_tailer.py`

## Trust the output — quality machinery

Autonomy without evidence is just guessing. The gauntlet and promotion gate turn "done" into verified, evidence-backed fact — 5/5 or it doesn't ship.

- **Gauntlet** — a 5-layer acceptance harness (units, adversarial stress, backtest, day-in-the-life, live smoke). 5/5 or no release. → `dashboard/gauntlet.py`
- **Chaos virtual days** — a week of failures validated in minutes, deterministically. → `dashboard/chaos.py`
- **Promotion gate** — divergence, cost, and observation-window checks before new board state is accepted. → `dashboard/daily_board/promotion.py`
- **Best-practice knowledge base** — curated operational practices with gap detection. → `dashboard/bpkb/`
- **Adversarial stress harness** — attack scenarios with ground-truth scoring for agent honesty and cause-finding. *(internal deployment)*

## Plug into your world — integration

This is where proactive agents earn the name: discovering sources, finding opportunities, and filing them for review — work nobody prompted.

- **Scrape ingest bridge** — external intel flows into the board. → `dashboard/scrape_intel.py`
- **Listening pipeline** — degree-expansion source discovery plus a SWOT cadence that files evidence-backed opportunities into the exception queue. → `gateway/secondbrain/`
- **Gateway MCP + version snapshots** — the second-brain vault over MCP, with pinned, replayable versions. → `gateway/secondbrain_mcp/`

## Boring where it counts — ops

The unglamorous layer that makes everything above survive a reboot, a rename, and a second operator.

- **One config source** — env overrides → YAML → sane defaults; a test proves the stack boots with zero personal values. → `deployment_profile.py`
- **Token scoping** — actor×family×action hub tokens, deny-by-default; console writes need a token distinct from reads. → `dashboard/console/actions.py`
- **Boot-persistent services** — systemd units for console, ops agent, scheduler. → [GETTING-STARTED.md](GETTING-STARTED.md)
- **Cold-start sandbox** — the whole stack boots in a dockerized systemd container, first-boot assertions included. *(internal deployment)*


