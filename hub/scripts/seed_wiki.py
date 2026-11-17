#!/usr/bin/env python3
"""seed_wiki — import repo docs + operation knowledge into the hub wiki.

Idempotent *and* freshness-aware (GLM v2 review requirement 1): for every
page we build the deterministic desired body. If the current latest
revision already equals it, the page is skipped; if the repo doc changed
(or the page is missing), a NEW revision is appended. No silent staleness.

Machine attribution (GLM v2 review requirement 2): every revision written
here carries the actor ``seed-import`` — never a human name.

Each page body starts with a quote line ``> source: REPO <relpath>`` or
``> source: ops knowledge`` and cross-links related pages with
``[[wiki-link]]`` syntax. No secrets, no tokens, no real IPs.

Usage:
    python scripts/seed_wiki.py                # DB: $TICKET_HUB_DB or default
    python scripts/seed_wiki.py --db /path/to/hub.db --repo /path/to/repo
"""
from __future__ import annotations

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _REPO_ROOT)

from ticket_hub.store import Store  # noqa: E402
from ticket_hub.paths import hub_path  # noqa: E402

MACHINE_ACTOR = "seed-import"


def _page_body(source_line: str, content: str, links: list[str]) -> str:
    """Deterministic page body: source quote line + content + See-also wiki
    links. Deterministic so re-seeding can compare text and skip when fresh.
    """
    parts = [f"> source: {source_line}", "", content.strip(), ""]
    if links:
        parts += ["## See also", ""]
        parts += [f"- [[{slug}]]" for slug in links]
        parts.append("")
    return "\n".join(parts)


# (slug, title, repo_relpath-or-None, tags, extra_links)
# repo pages: body = file content, imported with a REPO source line.
REPO_PAGES = [
    ("readme", "ticket-hub README", "README.md", ["docs", "overview"],
     ["cli", "api", "agent-guide", "design-v2", "glossary"]),
    ("design-v1", "Design v1 — ticket graph", "DESIGN.md", ["docs", "design"],
     ["design-v2", "decision-log", "glossary"]),
    ("design-v2", "Design v2 — Mission Control", "DESIGN-v2.md",
     ["docs", "design"], ["design-v1", "forge-loop", "glossary"]),
    ("contributing", "Contributing", "CONTRIBUTING.md", ["docs", "process"],
     ["forge-loop", "glossary"]),
    ("security", "Security posture", "SECURITY.md", ["docs", "security"],
     ["cluster-seats", "ops-runbook"]),
    ("changelog", "Changelog", "CHANGELOG.md", ["docs"],
     ["decision-log"]),
    ("cli", "hub CLI", "docs/CLI.md", ["docs", "cli"],
     ["api", "agent-guide"]),
    ("api", "HTTP API", "docs/API.md", ["docs", "api"],
     ["cli", "agent-guide"]),
    ("agent-guide", "Agent guide", "docs/AGENT-GUIDE.md", ["docs", "agents"],
     ["cli", "api", "glossary"]),
    ("deploy", "Deploy runbook (repo)", "docs/DEPLOY.md", ["docs", "ops"],
     ["ops-runbook", "security"]),
    ("terminology", "Terminology", "docs/TERMINOLOGY.md", ["docs", "glossary"],
     ["glossary", "forge-loop"]),
]


def _ops_pages() -> list[tuple[str, str, list[str], str]]:
    """Hand-authored operation-knowledge pages: (slug, title, tags, content).
    Bodies are static text kept in this script (deterministic)."""
    forge_loop = """The forge loop is how ticket-hub (and the wider operation) builds:
work is planned as tickets, built in isolated sandbox **lanes** (git
worktrees per slice, e.g. the wiki lane, the UI lane, the swarm lane),
integrated by the orchestrator, then gated.

## One cycle

1. **Plan** — root ticket holds the plan; children are the work items
   (`hub plan` reads child statuses, never hand-claimed progress).
2. **Build in lanes** — each lane works in its own sandbox worktree against
   the frozen API contract; lanes do not edit each other's files.
3. **Test board** — the full pytest board (core + api + cli + adapters +
   outbound + wiki) must be green before a candidate is offered.
4. **Review** — reviewer must not be the author; GLM is the triggered
   reviewer gate for lane work (revise → author fixes → fresh re-review).
5. **Promotion candidate** — the candidate is **Blocked by owner**: only the
   owner accepts or rejects. Green tests and reviewer approval never count as
   acceptance. Such items carry an `OWNER-GATED` marker event.
6. **Rejection path** — a rejection starts a successor cycle from the same
   frozen snapshot plus rejection evidence; reviewed bytes are never
   patched in place.

Snapshots: candidate state is frozen and hash-bound (exact package SHA) so
reviews review exact bytes, and rejections carry their evidence forward.

See [[glossary]] for seat / lane / cycle / promotion definitions and
[[ops-runbook]] for what runs on the hub host.
"""

    ops_runbook = """Day-1 operations for the hub host (macOS, launchd). All paths under
`$TICKET_HUB_HOME/`; the hub itself serves `http://127.0.0.1:8777`
(loopback-only).

## Services (LaunchAgents)

| Label | Runs | Install |
|---|---|---|
| `com.ticket-hub.server` | `api.py` (FastAPI + SQLite, port 8777) | `deploy/install_server.sh` |
| `com.ticket-hub.outbound` | `outbound.py` digest poller | `deploy/install_outbound.sh` |
| `com.ticket-hub.maintenance` | `scripts/com.ticket-hub.maintenance.sh` | `deploy/install_maintenance.sh` |

Inspect / control (host mutations stay the owner's; scripts print commands,
never run them):

```bash
launchctl list | grep com.ticket-hub
launchctl print gui/$(id -u)/com.ticket-hub.server   # state + last exit
launchctl kickstart -k gui/$(id -u)/com.ticket-hub.server
```

## Logs

`$TICKET_HUB_HOME/logs/` (server/outbound stdout+stderr per install
script). Tail the last lines before debugging anything.

## Healthcheck

`scripts/healthcheck.sh` — one line, exit 0/1: `GET /board` must answer
and show >0 tickets. Side effect: writes last-known result to
`health-status.json`, surfaced by `GET /health` so a FAIL stays visible
between runs.

```bash
scripts/healthcheck.sh
curl -s http://127.0.0.1:8777/health
```

## Backups

`scripts/backup.sh` — WAL-safe `sqlite3 .backup` hot snapshot of `hub.db`,
`PRAGMA integrity_check` on the copy, keeps the newest 14 in
`$TICKET_HUB_HOME/backups/`. Run at least daily (the maintenance job
does); backup age budget is 26h.

## Restore drill

`scripts/restore_drill.sh` — periodically restore the newest backup into a
throwaway DB and integrity-check it, proving backups are restorable
without touching the live DB. Drill = green; anything else is an incident.

## One-writer rule

SQLite is WAL with **one writer**: never run a second server (or a
long-lived write transaction) against the same `hub.db`.

See [[deploy]] for the repo-side runbook, [[security]] for posture,
[[forge-loop]] for the build side.
"""

    cluster_seats = """A **seat** is a configured runtime slot for one model family in the
orchestration layer: identity + model + config + context budget +
isolation rules. Seat identity is static — runtime observations may
reduce a seat's eligibility for work but never rewrite who the seat is or
what authority it has (see [[glossary]]).

## Families in the operation

- **hermes** — coding/orchestration agents (this repo's primary workers).
- **forge** — build-loop harness: snapshots, review gates, promotion
  candidates ([[forge-loop]]).
- **glm** — the triggered reviewer seat: reviews lane work at interval
  boundaries; it is not the fallback worker for every task.
- **the owner** — human. Acceptance and promotion authority live here only.

## Tooling

Model calls route through the remote-model tooling (typed cluster harness:
health, chat, and job entrypoints over a local registry). Seats call it
over **loopback only** — e.g. `http://127.0.0.1:<port>/health` — the same
posture as the hub itself ([[security]]).

## Secrets policy (hard rules)

- Secrets never enter the repo, the hub DB, wiki pages, prompts, or logs.
- Runtime config with credentials lives outside the repo, chmod 600
  (e.g. `$TICKET_HUB_HOME/adapters.json`).
- Bearer keys are read at call time from the file named by the
  `..._KEY_FILE` variable; never logged, never copied into tickets.
- Docs and examples use placeholders (`<token>`, `example.invalid`) and
  localhost addresses only — no real IPs in the hub or wiki.

A seat that cannot reach its model marks its work `blocked` with a reason
([[agent-guide]]); it does not silently switch identity or authority.
"""

    glossary = """Operation vocabulary — use these terms verbatim in tickets, docs,
reports, and pages. (Canonical copy: [[terminology]]; this page adds the
operation-level neighbors.)

- **Seat** — a configured runtime slot for one model/family: identity,
  model, config, context budget, isolation. Static identity; runtime
  observations affect eligibility, never identity or authority.
- **Lane** — a parallel workstream building one slice in an isolated
  sandbox worktree against the contract (wiki lane, UI lane, swarm lane).
  All lanes integrate through the gateway; the whole board must be green.
- **Cycle** — one forge-loop iteration: plan → build lanes → test board →
  review → promotion candidate → accept/reject ([[forge-loop]]).
- **Digest** — event-derived, reader-facing project update; every bullet
  cites the event ids it paraphrases. No fabrication is structural.
- **Binding** — a ThreadBinding linking one ticket to one chat thread
  (platform + room + thread + root message). Inbound replies become
  `thread_message` events; outbound posts digests.
- **Blocked by owner** — keep verbatim, this exact phrase: anything awaiting
  the owner's approval or action (status `blocked` and/or an `OWNER-GATED`
  marker event — marker values stored under the old name stay as-is, since
  events are append-only, and remain queryable via
  `TICKET_HUB_EXTRA_GATE_MARKERS`).
  `hub plan` lists them. Blocked ≠ stale.
- **Promotion** — advancing a build into main/prod. Only the owner does it.
  Green tests, reviewer approval, or agent consensus never count.
- **Snapshot** — a frozen, hash-bound state of the build used as the exact
  reviewable/rejectable artifact; rejections fork successors from it.
- **Seed import** — the machine actor `seed-import` writes wiki copies of
  repo docs; re-import posts a new revision when a source changes
  (`scripts/seed_wiki.py`).
"""

    decision_log = """Index of major decisions, newest first. Details live in CHANGELOG
([[changelog]]), DESIGN ([[design-v1]], [[design-v2]]), and ticket
history; this page is the map, not the record.

| Date | Decision | Where |
|---|---|---|
| 2026-10-12 | **ticket-hub v1 accepted by the owner** — the hub becomes the go-forward work OS (GLM review PASS-CONDITIONAL, blockers closed). Acceptance authority: the owner only. | [[changelog]], [[design-v1]] |
| 2026-10-12 | **v2 scope set** — four pillars: wiki knowledge layer, Mission Control shell, roles & swarms, fit services. Wiki ships first. | [[design-v2]] |
| 2026-10-11 | **Legacy ledger frozen** — the flat pre-hub tickets ledger was migrated once (37 tickets, 33 edges, 236 events with timestamps/actors/owner-gating preserved, idempotent by legacy ids) and is now read-only history. All new work goes through the hub. | one-time migration (script retired) |
| 2026-10-11 | **Loopback-only posture** — hub serves 127.0.0.1:8777 only; no auth in v1 by design (single-user machine); remote exposure requires bearer-per-actor first. | [[security]] |
| 2026-10-11 | **Append-only everywhere** — events and wiki revisions are immutable at the DB trigger level; corrections are new entries, never edits. | [[design-v1]], [[terminology]] |
| 2026-10-11 | **Reviewer ≠ author** — GLM is the triggered review gate; a revise verdict loops back to the author and gets a fresh re-review. | [[contributing]], [[forge-loop]] |

Add new rows as decisions land; do not rewrite history here — like
events, append.
"""

    return [
        ("forge-loop", "Forge loop: sandbox lanes, cycles, promotion",
         ["ops", "process"], forge_loop),
        ("ops-runbook", "Ops runbook: services, logs, health, backups",
         ["ops", "runbook"], ops_runbook),
        ("cluster-seats", "Cluster seats: model slots, tooling, secrets policy",
         ["ops", "seats", "security"], cluster_seats),
        ("glossary", "Glossary — operation vocabulary",
         ["ops", "glossary"], glossary),
        ("decision-log", "Decision log — major decisions index",
         ["ops", "decisions"], decision_log),
    ]


def build_desired_bodies(repo_root: str) -> list[tuple[str, str, list[str], str]]:
    """(slug, title, tags, body) for every seedable page that exists on
    disk, in stable order."""
    out = []
    for slug, title, rel, tags, links in REPO_PAGES:
        path = os.path.join(repo_root, rel)
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as fh:
            content = fh.read()
        body = _page_body(f"REPO {rel}", content, links)
        out.append((slug, title, tags, body))
    for slug, title, tags, content in _ops_pages():
        body = _page_body("ops knowledge", content, [])
        out.append((slug, title, tags, body))
    return out


def seed(store: Store, repo_root: str = _REPO_ROOT) -> list[dict]:
    """Seed/refresh all pages. Returns one record per page:
    {slug, action: created|updated|skipped, rev}. Machine actor:
    'seed-import' on every write."""
    records = []
    for slug, title, tags, body in build_desired_bodies(repo_root):
        current = store.get_page(slug)
        if current is not None and current.body_md == body:
            records.append({"slug": slug, "action": "skipped",
                            "rev": current.rev})
            continue
        page = store.put_page(slug, title=title, body_md=body, tags=tags,
                              actor=MACHINE_ACTOR)
        action = "updated" if current is not None else "created"
        records.append({"slug": slug, "action": action, "rev": page.rev})
    return records


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Seed/refresh the hub wiki from repo docs + ops knowledge")
    ap.add_argument("--db", default=None,
                    help="hub DB path (default: $TICKET_HUB_DB or "
                         "$TICKET_HUB_HOME/hub.db)")
    ap.add_argument("--repo", default=_REPO_ROOT,
                    help="repo root for source docs")
    args = ap.parse_args(argv)
    db = args.db or os.environ.get("TICKET_HUB_DB", hub_path("hub.db"))
    store = Store(db)
    try:
        for rec in seed(store, args.repo):
            print(f"{rec['action']:8s}  {rec['slug']}  r{rec['rev']}")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
