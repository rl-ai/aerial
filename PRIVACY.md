# Privacy

Aerial is built to run entirely on infrastructure you control.

- **No telemetry.** No analytics, crash reporting, or usage tracking
  exists anywhere in the codebase. There are no outbound calls except
  to services you configure yourself (your hub, your SMTP relay, your
  model seats).
- **Local state.** Logs, databases, digests, and workspaces live under
  the state dir (default `~/.aerial/`) or paths you set in the
  deployment profile. Nothing is uploaded.
- **Secrets stay local.** Email credentials, hub tokens, and cluster
  bearer files are read from the paths you configure and are never
  logged, copied, or committed (`config/deployment.yaml` is
  gitignored).
- **Email content.** If you enable email delivery, the daily report
  contains your own board/commit data and is sent only to the recipient
  you configure.
- **Scraped intel** (`scrape_intel.py`, `bp_sources.json`) is fetched
  from sources you list and stored in your own database.

The upstream deployment this project was split from shares none of its
data, credentials, or personal configuration in this repository.
