# Roadmap

Aerial is an early preview. What works, what's rough, and what's next.

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
