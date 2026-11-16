# Aerial runtime decision points — the agent's optimization surface (v1)

Every variable the ops agent decides at runtime: the probe that measures
it, the rule that decides it, the action it takes, the authority boundary,
and where the decision is recorded. Decisions execute within the flow
state machine (every transition persisted with evidence).

**Authority tiers:** `AUTO` (act, record, report in the daily aggregate) ·
`PROPOSE` (queue as a decision for Ray) · `RAY` (never act without
explicit approval).

| # | Variable | Probe | Decision rule | Action | Authority | Record |
|---|---|---|---|---|---|---|
| D1 | **GPU offload layers (ngl)** per model seat | `ops_context`: free VRAM per GPU; model VRAM-per-layer | ngl = max layers where Σ layer-VRAM ≤ free VRAM × 0.9; if < 3 layers feasible → CPU-only + cost line in the daily report | Relaunch the seat with the computed ngl (systemd unit edit, verified restart) | AUTO | flow + daily report |
| D2 | **Seat placement** (which GPU runs which model) | **The VRAM Manager handles the mechanical swap** (dashboard/vram_manager.py: VRAM-first, idle displacement, demand reload — D2 shrinks to tier policy). GPU occupancy per seat; model VRAM need; swarm tier policy; swap-pair state | **Kimi and Qwen are a swap pair on the Pro 6000** (historical: Kimi replaced Qwen's planning role; by transitive property they trade the seat). Rule: the active tier's model holds the seat; when a tier goes idle >15 min, the pair swaps (Kimi onto GPU with the freed VRAM at max feasible ngl; Qwen back when the swarm bursts). Swap cost: Kimi load ~2–3 min from page cache — only swap when the incoming model runs ≥30 min. | Restart the pair member on the seat (systemd/vLLM, verified) | AUTO within tier / PROPOSE cross-tier | flow + daily report |
| D3 | **Service units** (bare process → systemd) | `ops_context`: long-lived processes not under a systemd user unit | Any Aerial-owned process running bare > 1 poll cycle → convert | Create the unit (WorkingDirectory, Restart=on-failure), enable, verify | AUTO | flow + runbook |
| D4 | **Monitor freshness budgets** | Actual cadence of the monitored artifact (cron line + last-run history) | budget = artifact cadence × 2.5 (min 10 min); recalibrate when the cadence changes | Edit the monitor's budget table; verify next run | AUTO | flow |
| D5 | **Parallel/context split** per model server | Server config: total ctx, parallel, per-request input+output sizes | per_slot = total_ctx / parallel; require per_slot ≥ input + output + 15%; else raise total ctx (if KV budget allows) or reduce parallel | Edit the server unit; verified restart | AUTO | flow + daily report |
| D6 | **Job cadence** (how often a cron fires) | Job failure rate, staleness incidents, value signals (how often its output is used) | Adjust within pre-approved bands (±1 step in the schedule); beyond bands → PROPOSE with the numbers | crontab edit, verified | AUTO in-band / PROPOSE beyond | flow + daily report |
| D7 | **Dedupe window** per finding class | Recurrence rate of the finding | Recurring (≥2 in 24h) → escalate, don't re-fix; the window auto-extends ×2 per recurrence up to 7d | flow state store update | AUTO | flow |
| D8 | **Swarm tier routing** per task | Task type (governed vs burst); slot availability per tier | Governed/decision-touching → Tier A; burst/bulk → Tier B; attacker/analysis → Tier C; Tier A saturated → Tier B for mechanical parts only | Dispatch target selection | AUTO | flow |
| D9 | **Token class** per caller | Caller identity + action (GET vs mutation) | Mutations require write-class tokens; reads accept any valid bearer; new callers get read-class by default | Token config update | AUTO (security-bound; R1 if bypassed) | access log |
| D10 | **Model seat selection** per task | Seat liveness, load, model capability vs task needs | Best live seat for the task; a dead seat's tasks failover explicitly (never silent substitution) | Task routing | AUTO within policy / explicit cross-family | flow + access log |
| D11 | **Report/email cadence** | Staleness, event volume, SEV events | Daily unless SEV; on-demand always available; SEV pings immediate — nothing else changes the cadence | Cron/policy already fixed; no runtime change | FIXED (Ray set) | daily report |
| D12 | **Backtest window** selection | Incident history: the window containing the latest SEV/postmortem | Post-SEV: replay that window; weekly: the last 7 real days | validate_days --backtest run | AUTO | flow + daily report |

## The meta-rules

1. **Every decision persists** — flow state machine transition with the
   probe data as evidence. A decision without a record is a defect.
2. **Fail-closed always** — an unknown or unprobeable state means no
   action (R6), never a guess.
3. **Ray-facing changes are PROPOSE** — anything altering what Ray sees
   (cadence, report format, escalation policy) queues as a decision;
   the agent never silently changes its own visibility.
4. **Security-bound decisions are AUTO but alarmed** — D9 tightening is
   autonomous; any loosening is R1 (never autonomous).
5. **New decision points require a declared probe + rule + test** — same
   as the pipeline contract's declaration rule (docs/pipeline-checkpoints.md):
   an undeclared runtime decision is a defect.
