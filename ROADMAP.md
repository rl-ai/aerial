# Roadmap

Aerial is a **pilot release (beta)** — production-tested daily on the
team's own fleet, but not yet packaged for general distribution. What
works, what's rough, and what's next.

## Operating principle: ship at 90%

Releases ship at ~90% complete, with hard requirements that are never
traded away: privacy (all-local, zero telemetry), verified core
(remediation fixes proven by symptom probes), and honest documentation
of every known limitation. The last mile is deliberately finished in
public — real users and real failures define it better than we can
pre-launch, and the remediation + gauntlet layers catch what ships
rough. Known limitations are documented before users find them:
imperfect is fine, surprised is not.

## Continuous rebuild loop

The product does not get maintained by hand after release — it gets
**rebuilt continuously**:

1. **Signals in** — the listening pipeline watches data sources,
   upstream listeners (GitHub, Hugging Face, tech forums), and OSS
   activity of related projects; failures and gaps from the running
   deployment flow back as findings.
2. **Seeds** — the founder seeds ideas and direction at ~90%
   specification; the swarm builds them.
3. **Rebuild** — a team of agents driven by the snapshot builder
   (the forge: continuous snapshot builds with integrity gates, role
   fences, and review) rebuilds the product incorporating the new
   signals — scrapes, curation, discovery proposals, verified fixes —
   as an Aerial-managed service.
4. **Gate & ship** — the gauntlet gates every rebuild; verified builds
   ship, unproved changes escalate.

Status: the forge tooling exists and is proven on lane integrations;
connecting it to Aerial's listening/scrape outputs as a managed service
is the next build (see In progress above).

## Working now (v1.0-mvp)

- Ticket hub with task-level shared context, evidence labels, mandates,
  deny-by-default token scoping (671 automated checks)
- Ops agent: 5-minute poll, R1–R6 failure classification, governed
  safe-fixes with proof-of-fix verification (remediation contract),
  seat liveness with registered remediations (435 checks)
- Operator console, daily board, exec report, email delivery
- Fleet manager: VRAM displacement, seat-swap, idle-work broker
- Gauntlet: 5-layer release gate; chaos virtual days
- Listening pipeline: source discovery + SWOT cadence
- Single-clone install verified end to end

## In progress

- **Packaging** — pip packages and Docker images (today: clone-and-run)
- **First-run experience** — `aerial verify` exists; wiring it into a
  default onboarding pass
- **Model-selection UX** — seats are configured in
  `dashboard/vram_registry.json` (YAML/JSON editing); a selection
  interface is planned
- **Hub first-boot health** — a fresh hub reports `{"ok": false}` on
  `/health` until the first healthcheck runs; verifying should not
  require the workaround documented in GETTING-STARTED
- **Harness adapters** — ships Hermes-native; the supervision layer is
  harness-agnostic (HTTP + files), adapters for other OSS harnesses are
  designed but not built

## Known limitations

- The demo console screenshot/video set runs on generic data; a
  populated first-run dataset is not shipped yet
- The exception queue's hub endpoint is served by the ops deployment,
  not the OSS hub (tracked)
- Cold-start sandbox is internal (not self-contained for public use)

## Feedback

Open an issue or read [docs/BUGLIST.md](docs/BUGLIST.md) for the running
fix log — resolved and open items, newest last.
