# Privacy

**The short version:** you choose your models, accounts, platforms, and
storage location. Everything consolidates on your hardware and stays where
you want unless you choose cloud. The public packages make no network
calls beyond your own hub and model endpoints.

## Verified, not promised

An audit of the entire public surface (the MCP server, the gateway
pipeline, the dashboard) found exactly these outbound network calls:

| Component | Calls | To | Why |
|---|---|---|---|
| MCP server | **none** | — | stdio only; it never opens a socket |
| Gateway pipeline | none by default | — | all storage is local (content-addressed vault, SQLite) |
| Dashboard | hub API reads | your hub (loopback by default) | board data |
| Hierarchy runner | hub API + your model endpoints | your hub; your local models (or cloud models you explicitly configure) | investigations |

No analytics, no telemetry sinks, no phone-home, no crash reporters.

## How content is protected

- **Labels at ingest**: every item is classified public / personal /
  sensitive / intimate — with a conservative default when unknown.
- **Citation closure**: an agent can only cite sources inside its grant's
  authorized scope — enforced per read, not by policy documents.
- **Refuse by default**: sensitive content never releases through the
  governed path; when the system is degraded or uncertain, it fails
  closed (an error, never open content).
- **Sanitization**: released text passes disclosure rules (location
  generalization, date granularity); what was stripped is logged.

## The control surface

- **Models**: local (your GPUs) or cloud (only if you configure it) — per
  task.
- **Accounts & platforms**: each connection is a scoped grant with an
  expiry; revoke in one click.
- **Storage**: your vault, your machine; snapshots go where you point
  them.
- **Reports**: your daily brief is generated locally from your data.

## Honest limits

- The console reads are loopback-only; the trust assumption is "the host
  is yours."
- Decision-scoped write tokens are held by local processes — hardening
  (key-file handling) is on the roadmap.
- Multi-tenant/hosted deployment is not a design goal for v1 — this is
  self-hosted software for people who want their data on their machine.
