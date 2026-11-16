# FAQ

**Does Aerial need a GPU cluster?**
No. It runs on any machine with Python 3.10+. Fleet features (seat liveness, VRAM manager, seat-swap) light up when local model seats exist — without them, everything else still works.

**Where does my data go?**
Nowhere. Hub, console, runners, and models are all local. No telemetry, no phone-home. Details in [PRIVACY.md](PRIVACY.md).

**Why is email off by default?**
No recipient configured. Set `email.recipient_email` in `config/deployment.yaml` (or `AERIAL_EMAIL__RECIPIENT_EMAIL`) to turn it on.

**How do I rename the `aerial:` job prefix?**
Set `cluster.node_name` (or `AERIAL_NODE_NAME`). Done.

**What's a "virtual day"?**
Deterministic fake days (quiet, failure-storm, recovery…) injected into real stores, so a 7-day observation window validates in minutes. Guarded — inert unless `AERIAL_TIME_ACCELERATION > 1` and `AERIAL_CHAOS=1`.

**Do I need the deployment profile?**
No — defaults work out of the box. The profile exists to customize a real deployment without touching code.

**Which Python?**
3.10+. No third-party runtime dependencies for the core services.

**Is Aerial an agent framework?**
No. It's the operations layer *over* your agents — supervision, failure triage, cost accounting, fleet management. See [COMPARISON.md](COMPARISON.md).

**Can Aerial run my scheduling?**
Yes. Crontab-backed job runner with re-trigger support, plus a shadow scheduler for observing new behavior before it goes live.

**How do I start?**
[GETTING-STARTED.md](GETTING-STARTED.md) — clone to running console in about five minutes.
