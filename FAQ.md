# Frequently asked questions

**Does Aerial need specific hardware or a GPU cluster?**
No. The code runs with generic defaults on any machine. Model-seat
liveness monitoring, VRAM management, and seat-swap automation are
useful only if you run local model seats; without them the console and
daily board still work, and seat probes simply report gaps.

**Where does my data go?**
Nowhere. The hub, console, gateway, and runners are all local services.
There are no telemetry or phone-home paths. See PRIVACY.md.

**Why is email disabled by default?**
The generic profile has no recipient (`email.recipient_email` empty).
With no recipient, `send_report` is a no-op that returns `False`. Set
the recipient in `config/deployment.yaml` or
`AERIAL_EMAIL__RECIPIENT_EMAIL` to enable delivery.

**How do I rename the node prefix (`aerial:`) used in job ids?**
Set `cluster.node_name` in the profile (or `AERIAL_NODE_NAME`). Job
parsing, re-trigger commands, and severity-adjacent job lists derive
from it.

**What is a "virtual day"?**
`dashboard/chaos.py` injects deterministic day-types (quiet,
failure-storm, mixed, recovery, anomalous) into real stores so a 7-day
observation window can be validated in minutes. It is guarded: it is a
no-op unless `AERIAL_TIME_ACCELERATION > 1` AND `AERIAL_CHAOS=1`.

**Do I need the deployment profile at all?**
No. Without `config/deployment.yaml` everything falls back to generic
defaults. The profile exists so a real deployment can customize values
without touching code.

**Which Python?**
Python 3.10+ (uses `X | Y` unions). No third-party runtime dependencies
for the dashboard/integration code; the gateway has its own
`pyproject.toml`.
