# Contributing to Aerial

Thanks for looking at the code. Practical notes before you open a PR:

## Ground rules

1. **Tests first or tests with.** Every behavior change ships with a
   test. The suites are `dashboard/tests/` (unit + acceptance) and
   `integration/` (hermetic cycle, scheduler, hub remote, shadow
   hierarchy). Run both:

   ```bash
   python3 -m pytest dashboard/tests integration -q
   ```

2. **No personal values in code.** Node names, hosts, recipients, and
   paths go through `deployment_profile.py` (profile / env override /
   generic default). A PR that hardcodes a deployment value will be
   bounced — see `config/deployment.yaml.example` for the keys.
3. **Fail closed.** New automation must fail closed on unknown inputs
   (see SECURITY.md) and be injectable/hermetic under test.
4. **Determinism.** Anything that touches validation or chaos
   generation must be seeded and deterministic (same seed + day → same
   events).

## Style

- Small, single-purpose modules; docstrings explain the *why*.
- Plain stdlib; avoid adding runtime dependencies.
- Type hints on public functions.

## Process

1. Fork, branch, commit small.
2. `python3 -m pytest dashboard/tests integration -q` green.
3. PR with: what changed, which tests cover it, any profile keys added
   (update `config/deployment.yaml.example`).

## Reporting bugs

Check `docs/BUGLIST.md` first — known issues are recorded there with
their status.
