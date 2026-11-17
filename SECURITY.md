# Security policy

## Reporting

Open a GitHub issue **only for non-sensitive bugs**. For anything that
looks exploitable (auth bypass, injection, secret exposure), contact
the maintainers privately first — see CONTRIBUTING.md for current
contact channels. Please do not include secrets or tokens in reports.

## Security model (short version)

- **Fail closed.** Unknown finding classes are never auto-fixed; the
  followup runner escalates to the daily aggregate instead.
- **Scoped mandates.** Every automated fix session runs under a
  scoped, expiring mandate; the daemon never edits product code
  directly — it dispatches agent sessions that do.
- **Distinct secrets.** The console write token MUST be a distinct
  secret from the read token (an attack-swarm finding: sharing one
  token widened the blast radius).
- **Token references, not tokens.** Job configs reference token files
  by path; raw tokens never appear in tickets or reports.
- **Guarded mutation.** Chaos injection and validation writes require
  explicit opt-in env guards (`AERIAL_CHAOS=1` +
  `AERIAL_TIME_ACCELERATION > 1`); production stores are otherwise
  read-only for those paths.
- **Deduped automation.** A finding never auto-fixes twice in 24h;
  recurrence escalates instead of retrying blind.

## Deployment hygiene

- Keep `config/deployment.yaml` out of git (it is in `.gitignore`).
  It holds your tokens' locations, recipients, and node names.
- The console requires a read token for every page and a distinct
  write token for decisions. It has no user accounts or TLS, so serve
  it on localhost or put your own authentication and TLS in front.
- Put real secrets in files under `paths.state_dir`/secrets or your
  own secret store — never in the profile or code.

## Supported versions

Only the latest commit on the default branch is supported.
