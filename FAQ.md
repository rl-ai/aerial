# FAQ

**Does Aerial need a GPU cluster?**
No. It runs on any machine with Python 3.10+. Fleet features — seat liveness, VRAM management, seat-swap — activate when local model seats exist; without them the console and daily board function normally, and seat probes simply report gaps.

**Where does my data go?**
Nowhere. Hub, console, runners, and models are all local. No telemetry, no phone-home. Details in [PRIVACY.md](PRIVACY.md).

**Why is email off by default?**
No recipient configured. Set `email.recipient_email` in `config/deployment.yaml` (or `AERIAL_EMAIL__RECIPIENT_EMAIL`) to turn it on.

**How do I rename the `aerial:` job prefix?**
Set `cluster.node_name` in the profile (or `AERIAL_NODE_NAME`). Job parsing, re-trigger commands, and severity-adjacent job lists derive from it.

**What's a "virtual day"?**
Deterministic fake days (quiet, failure-storm, recovery…) injected into real stores, so a 7-day observation window validates in minutes. Guarded — inert unless `AERIAL_TIME_ACCELERATION > 1` and `AERIAL_CHAOS=1`.

**Do I need the deployment profile?**
No — defaults work out of the box. The profile exists to customize a real deployment without touching code.

**Which Python?**
3.10+. No third-party runtime dependencies for the core services.

**Is Aerial an agent framework?**
No. It's the control tower *over* your agents — autonomous supervision, failure triage to fix, cost accounting, and fleet saturation. See [FEATURES.md](FEATURES.md).

**Can Aerial run my scheduling?**
Yes. A crontab-backed job runner with re-trigger support, plus a shadow scheduler that observes new behavior under a promotion gate before it goes live.

**How do I start?**
[GETTING-STARTED.md](GETTING-STARTED.md) — clone to running console in about five minutes.
