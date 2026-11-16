# Aerial vs. DeepSeek Harness

**Agents fly solo — always hunting opportunities for you.**

**TL;DR:** DeepSeek Harness (DSH) is an excellent **agent execution harness** — one user, one chat surface, one cloud model, a polished plugin ecosystem. Aerial is the **operations layer around agent work**: a multi-model fleet, evidence-backed supervision, failure triage that ends in fixes, cost accounting, and hardware that never idles. If you are evaluating DSH, the honest question is not "which is better" — it is "who supervises, triages, and accounts for the work once it leaves the chat window."

## The gap a harness doesn't close

A harness runs the agent in front of you. Production agent work raises questions a chat surface doesn't answer:

- **Who verified it?** Task done ≠ task done right. What evidence backs the claim?
- **What happens at 3am?** A scheduled task fails — who notices, triages, and fixes it?
- **What did it cost?** Tokens, calls, and hardware-hours across every run.
- **Why is my hardware idle?** A desktop app uses one model on one machine while the rest of the fleet sits dark.

## Capability comparison

| Capability | DeepSeek Harness | Aerial |
|---|---|---|
| Agent task execution (coding, docs, research) | ✅ Core strength — polished, conversational, with file previews | ✅ Via its agents — but this is not where Aerial differentiates |
| Open source | ✅ Public preview, open source | ✅ MIT |
| Plugin / extension ecosystem | ✅ "Everything is a plugin", Creator mode builds plugins from chat | ➖ Not a plugin marketplace; integrates via governed jobs and the hub API |
| Subagents / agent teams | ✅ Subagents; agent teams (experimental) | ✅ Parallel worker lanes, swarm dispatch with per-lane logging |
| Scheduled / recurring tasks | ✅ In-app scheduling (experimental) | ✅ Shadow scheduler + crontab-backed job runner, boot-persistent systemd units |
| Approval flow for risky actions | ✅ Auto approval review (experimental) | ✅ Exception queue: human-gated, evidence-attached, admission-scored |
| Execution traces | ✅ Developer tools: traces, tool-call inspection | ✅ Model-calls ledger (per-call tokens, latency, tok/s) + hub event audit trail |
| **Multi-model fleet management** | ➖ DeepSeek-V4.1-Flash (cloud API) | ✅ Any provider: local VRAM seats, seat-swap, VRAM manager that displaces idle models |
| **Hardware saturation** | ➖ One machine, one model at a time | ✅ Idle-work broker fills measured-idle seats from a backlog, 24/7 |
| **Failure triage → fix** | ➖ You read the trace yourself | ✅ Dead-seat and failed-job detection → classification → RCA → proposed fix → verification |
| **Promotion gate / quality gauntlet** | ➖ Not applicable | ✅ 5-layer gauntlet (units, adversarial stress, backtest, day-in-the-life, live smoke) + promotion gate (divergence, cost, observation window) |
| **Cost & usage accounting** | ➖ Per-conversation view | ✅ Per-seat and per-call: tokens in/out, tok/s, concurrency, saturation % — reported on a 15-min cadence |
| **Privacy posture** | ⚠️ Cloud model by default; open source lets you re-point it | ✅ Runs entirely local: hub, console, runners, and models; no telemetry, no phone-home |
| **Self-healing infrastructure** | ➖ Out of scope | ✅ Model seats self-heal: detect → eject GPU holders → relaunch → verify |
| Best for | Individuals doing daily work with a frontier cloud model | Operators running agent fleets on their own hardware who need supervision, uptime, and cost control |

## What Aerial adds on top of any agent

Aerial does not replace your harness — it manages whatever runs inside it:

- **Operator console** — bearer-authed dashboard: daily board, exec report, email delivery.
- **Followup pipeline** — failures classified (R1–R6), tickets filed, safe fixes attempted, re-verified.
- **Promotion gate** — shadow-run new behavior for an observation window before it goes live; divergence and cost must pass thresholds.
- **Gauntlet** — a 5-layer adversarial test harness that must pass 5/5 before a release cuts.
- **Fleet ops** — seat liveness, VRAM displacement, seat-swap, idle-work broker, usage telemetry on a 15-min hub report.
- **Audit backbone** — every governed call, dispatch, and decision lands on hub tickets with evidence labels.

## Where Aerial fits

- **Solo operator with local models** — you own the GPUs; Aerial keeps them saturated and the seats alive while you sleep.
- **Team/platform lead running agent swarms** — parallel lanes with per-lane logging, cost accounting, and a promotion gate before anything touches production.
- **Privacy-bound org** — the whole stack (models included) runs on your hardware; nothing phones home.

## Objections

**"Why not just use DSH?"** Use it — for interactive work it's excellent. Aerial answers what happens around that work: supervision, failure recovery, cost, and fleet utilization. They compose; DSH executes, Aerial operates.

**"Do I need a GPU cluster?"** No. Aerial runs with generic defaults on one machine; fleet features light up when local model seats exist and simply report gaps when they don't.

**"Is this another agent framework?"** No. Aerial has no agent loop of its own — it is the management layer over whatever agents you run, including DSH-style harnesses.

**"What does running it cost?"** The software is MIT and runs on hardware you already own. The cost question Aerial answers for *you* is the interesting one: it measures every call's tokens, latency, and tok/s so you can see what your agent work actually spends.

---

*Commands verified against v1.0-mvp. Start with [GETTING-STARTED.md](GETTING-STARTED.md).*
