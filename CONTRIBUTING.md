# Contributing

Thanks for your interest in contributing. This project is developed through a
reviewed pipeline, and external contributions follow the same path as internal
ones.

## The pipeline (this is the contribution path)

```
GitHub issue
   ↓
PM triage            — issues are labeled, scoped, and admitted into the backlog
   ↓
Lane                 — work is done in isolated lanes/branches by a worker profile
   ↓
Lead review          — every diff is reviewed before it can land (reviewer sign-off)
   ↓
Integrator land      — the integrator merges; landing runs the secret scan (see below)
```

What this means for you:

1. **Open an issue first.** Do not open a PR for something nobody asked for —
   describe the problem or feature, let it go through triage.
2. **Wait for triage.** An issue labeled and assigned is your green light.
3. **Small, focused PRs** linked to the triaged issue.
4. **Every PR is reviewed** before merge, and CI/tests must pass.

## Local development setup

```bash
git clone <repo-url>
cd <repo>

# Python 3.11+ — the core packages are stdlib-only, no pip installs needed
# to run the MCP server from source. For tests:

python3 -m venv .venv
source .venv/bin/activate
pip install pytest

# Gateway / MCP server tests
pytest secondbrain_mcp/ test_secondbrain.py test_mechanisms.py

# Dashboard tests (lanes are self-contained; run the suite in each)
pytest dashboard/tests/

# The six-question demo (hermetic — uses a temp vault, no live services)
python -m demo
```

## PR rules

- **Tests are required.** New behavior ships with a test; bug fixes ship with a
  test that fails before the fix. A PR whose tests don't pass will not land.
- **No secrets.** The landing process (`land.sh`) scans every diff for tokens,
  keys, bearer credentials, and config values under secret-like keys. A hit
  blocks the land — don't commit `.env` files, tokens, or real vault data.
  Use fixtures and temp dirs (see `demo.py` for the pattern).
- **One lane per PR.** Don't mix unrelated changes; it makes review and revert
  harder.
- **Update docs** when you change behavior (`README.md`, module docstrings).

## Reporting security issues

Do **not** open a public issue for security problems. See
[SECURITY.md](SECURITY.md) — use GitHub private vulnerability reporting.
Authorization-bypass findings are the highest-severity class in this system.

## License

Licensing is under active decision (see the repository's LICENSE file when it
lands). By contributing you agree your contributions will be distributed under
the project's eventual license.
