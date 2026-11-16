# Aerial vs. DeepSeek Harness

**Agents fly solo — always finding opportunities for you.**

🛩️ **DSH** — the aircraft. 🗼 **Aerial** — the control tower.

| | 🛩️ DSH | 🗼 Aerial |
|---|---|---|
| Raw execution | **wins** | |
| 3am failures | | **wins** |
| GPU saturation | | **wins** |
| Evidence trail | | **wins** |
| Cost accounting | | **wins** |
| Privacy | | **wins** |
| Self-healing | | **wins** |
| Runs on your harness | | **wins*** |
| Open source | ✅ | ✅ |

*Use DSH for interactive work — it's excellent. Aerial makes the work survive production.*

## What's in the box

**Operator surface**
- Bearer-authed console on `:8777` — daily board, sparklines, decision actions
- Daily board + three-headline exec report, in your inbox if you want
- Exception queue — risky actions wait for a human, evidence attached

**Automation**
- Ops agent: polls every 5 minutes — classify, file, safe-fix, verify
- Failure triage R1–R6: security breaches ping immediately, unknowns fail closed
- Shadow scheduler: new behavior observes before it goes live

**Model fleet**
- Seat liveness probes — a dead seat is a finding, not a mystery
- VRAM manager + seat-swap — models live on VRAM, demand displaces idle
- Idle-work broker — empty seats pull pre-approved backlog work
- Usage telemetry — per-seat, per-call tokens/tok/s on a 15-minute report

**Quality machinery**
- 5-layer gauntlet — units, adversarial stress, backtest, simulated day, live smoke
- Chaos virtual days — a week of failures validated in minutes
- Promotion gate — divergence, cost, and observation-window checks
- Best-practice knowledge base + adversarial stress harness

**Integration & ops**
- Scrape ingest bridge, listening pipeline (source discovery + SWOT cadence)
- Gateway MCP with pinned, replayable version snapshots
- One config source (env → YAML → defaults), token scoping, systemd units

## Why Aerial wins those rows

- **🌙 3am failures** — classified, ticketed, safe-fixed, verified before sunrise. You hear about it only if it deserved to wake you.
- **🏭 GPU saturation** — models live on VRAM, demand displaces idle, and a broker hands empty seats to pre-approved work. No plane sits idle.
- **📜 Evidence** — every claim on a hub ticket, releases gated by a 5-layer gauntlet. 5/5 or no ship.
- **💸 Cost** — every call: tokens, tok/s, concurrency. A 15-minute report, not an archaeology dig.
- **🔒 Privacy** — hub, console, runners, models: all on your hardware. Nothing phones home.
- **🔧 Self-healing** — dead seats detect, eject, relaunch, verify. On their own.
- **🧩 Your harness** — the supervision layer speaks HTTP + files. Ships Hermes-native; other harnesses are a small adapter away.

## Objections, straight

**"Why not just use DSH?"** For interactive work, do. They compose: DSH executes, Aerial operates.

**"Do I need a GPU cluster?"** No. One machine is fine; fleet features wake up when local seats exist.

**"What does it cost?"** MIT, on hardware you own. The better question: what does your agent work cost *today*? Aerial measures it.

---

*Verified against v1.0-mvp. Start with [GETTING-STARTED.md](GETTING-STARTED.md).*
