# Aerial vs. DeepSeek Harness

**Agents fly solo — always finding opportunities for you.**

**TL;DR:** DeepSeek Harness (DSH) executes agents brilliantly. Aerial **operates** them — fleet, supervision, failure triage, cost, and hardware that never idles. Not which is better: who runs the work once it leaves the chat window.

## The gap a harness doesn't close

- **Who verified it?** Done ≠ done right.
- **What happens at 3am?** A scheduled task fails. Who fixes it?
- **What did it cost?** Tokens and hardware-hours, every run.
- **Why is your hardware dark?** One model, one machine, one user.

## Head to head

| | DeepSeek Harness | Aerial |
|---|---|---|
| Category | Agent execution harness | Agent operations layer |
| Task execution | ✅ Core strength | ✅ Via its agents |
| Open source | ✅ | ✅ MIT |
| Plugins | ✅ Marketplace + Creator mode | ➖ Governed jobs + hub API instead |
| Subagents / teams | ✅ (experimental) | ✅ Swarm lanes, per-lane logging |
| Scheduling | ✅ (experimental) | ✅ Boot-persistent, crontab-backed |
| Approvals | ✅ (experimental) | ✅ Exception queue with evidence |
| Traces | ✅ Dev tools | ✅ Per-call ledger + hub audit trail |
| **Multi-model fleet** | ➖ One cloud model | ✅ Local VRAM seats, seat-swap, VRAM manager |
| **Saturation** | ➖ One machine | ✅ Idle-work broker, 24/7 |
| **Failure → fix** | ➖ Read the trace yourself | ✅ Detect → classify → RCA → fix → verify |
| **Quality gate** | ➖ | ✅ 5-layer gauntlet + promotion gate |
| **Cost accounting** | ➖ Per conversation | ✅ Per seat, per call, 15-min reports |
| **Privacy** | ⚠️ Cloud model by default | ✅ Fully local, no telemetry |
| **Self-healing** | ➖ | ✅ Seats recover on their own |
| Best for | Daily work with a frontier cloud model | Agent fleets on your own hardware |

## What Aerial adds on top of any agent

It doesn't replace your harness — it manages whatever runs inside it:

- **Console** — daily board, exec report, email delivery.
- **Followup pipeline** — classify, ticket, safe-fix, verify.
- **Promotion gate** — shadow first, go live only when thresholds pass.
- **Gauntlet** — 5/5 layers or no release.
- **Fleet ops** — seats, VRAM, saturation, telemetry.
- **Audit backbone** — every decision on a hub ticket, evidence-labeled.

## Where it fits

- **Solo operator with local models** — GPUs saturated, seats alive, while you sleep.
- **Swarm lead** — parallel lanes, cost accounting, promotion gate before production.
- **Privacy-bound org** — everything local, including the models.

## Objections

**"Why not just use DSH?"** Use it — interactively, it's excellent. Aerial runs what happens *around* it. They compose: DSH executes, Aerial operates.

**"Do I need a GPU cluster?"** No. Defaults run on one machine; fleet features light up when local seats exist.

**"Is this another agent framework?"** No. No agent loop of its own — it's the management layer over whatever you run.

**"What does it cost?"** MIT, on hardware you own. The better question — what does your agent work *already* cost? Aerial measures it.

---

*Commands verified against v1.0-mvp. Start with [GETTING-STARTED.md](GETTING-STARTED.md).*
