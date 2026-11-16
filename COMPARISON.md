# Aerial vs. DeepSeek Harness

**Agents fly solo — always finding opportunities for you.**

DSH is the aircraft: fast, capable, flies anywhere you point it. Aerial is the airline around it — dispatch, maintenance, the black box, the control tower. Planes don't run airlines.

## Six moments DSH can't cover

**1. The victory lap.** The agent announces *done*. Tests pass. The diff looks clean. You want to believe it — and that's exactly the moment trust runs on nothing. With DSH, you are the verification step. With Aerial, every claim lands on a hub ticket with evidence labels, and releases only ship after a 5-layer gauntlet — units, adversarial stress, backtest, a full simulated day, live smoke — passes 5/5.

**2. The 3am failure.** A scheduled task dies quietly. With DSH, it waits in your queue until morning, and *you* are the triage nurse. With Aerial, the ops agent already saw it: classified the failure, filed the ticket, attempted the governed safe fix, verified the exact symptom, and pinged you only if it was serious enough to wake you.

**3. The dark hangar.** You bought 96GB of VRAM. The desktop app loads one model, one machine, one user — and the rest of the fleet sits cold. With Aerial, models live on VRAM by default, demand displaces idle, pressure evicts, seats swap on command — and when nothing needs them, a broker hands idle seats to pre-approved backlog work. No plane sits idle.

**4. The receipt.** End of the month: what did the agent work actually cost? With DSH, you scroll conversation history. With Aerial, every call is a ledger line — tokens in and out, latency, tok/s, concurrency — rolled up per seat on a 15-minute report.

**5. The new hire.** You want to change how the system behaves. Ship it and pray? With DSH, there's no gate to fail. With Aerial, new behavior shadow-runs for an observation window first, and a promotion gate checks divergence and cost before it touches the live board. New hires earn their wings.

**6. The audience.** With DSH, your work rides a cloud model by default. With Aerial, the whole stack — hub, console, runners, and the models themselves — runs on hardware you own. Nothing phones home.

## The short version

| | DeepSeek Harness | Aerial |
|---|---|---|
| It is | The aircraft | The airline |
| Execution | ✅ Its best day | ✅ Through its agents |
| When work fails | You triage | It triages — then fixes, then verifies |
| Your GPUs | One model, one machine | Full fleet, saturated 24/7 |
| Trust | You are the gate | Evidence labels, gauntlet, promotion gate |
| Cost | Per conversation | Every call, on a 15-minute report |
| Privacy | Cloud by default | Local by design |
| Open source | ✅ | ✅ MIT |

Use DSH for the interactive work — it's genuinely excellent. Aerial is what makes the work *survive contact with production*.

## Where Aerial fits

- **You own GPUs** — and want them earning while you sleep.
- **You run swarms** — and need per-lane logging, cost, and a gate before production.
- **You can't use cloud agents** — everything, models included, stays on your hardware.

## Objections, straight

**"Why not just use DSH?"** For interactive work, do. They compose: DSH executes, Aerial operates.

**"Do I need a GPU cluster?"** No. One machine is fine; fleet features wake up when local seats exist.

**"Is this another agent framework?"** No. No agent loop of its own — it manages whatever you run.

**"What does it cost?"** MIT, on hardware you own. The better question: what does your agent work cost *today*? Aerial measures it.

---

*Commands verified against v1.0-mvp. Start with [GETTING-STARTED.md](GETTING-STARTED.md).*
