## Pull request checklist

- [ ] `python3 -m pytest dashboard/tests integration -q` is green
- [ ] No deployment-specific values hardcoded (node names, hosts,
      recipients, personal paths) — everything via
      `deployment_profile.py` / `config/deployment.yaml.example`
- [ ] New behavior has tests; hermetic (no live hub/seat/email needed)
- [ ] Automation fails closed on unknown inputs
- [ ] Docs updated (README/TECHNICAL/FAQ if behavior or keys changed)

## What changed

