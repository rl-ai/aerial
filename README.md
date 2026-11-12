# Aerial — governed agents that work for you

Connect the accounts and platforms you choose. The agents listen, curate
your knowledge base, and turn gaps into opportunities that they act on —
and one executive agent, your chief of staff, consolidates it all into a
personalized report built specifically for you.

## The four beats

**1 · Planner** — A daily planner that connects the accounts and platforms
you choose, consolidates everything locally, and plans your day around
you — finding things that bring joy without costing money.

**2 · Privacy by design** — You choose your models, accounts, platforms,
and storage location. Everything stays where you want unless you choose
cloud.

**3 · Opportunities** — Curates your knowledge base from the sources you
choose, measures it against your world, and turns gaps into opportunities
that agents act on.

**4 · Chief of staff** — One executive agent — your chief of staff —
consolidates updates and insights into a personalized report built
specifically for you.

## What it does (features)

**Knowledge & ingestion**
- Content-addressed knowledge base: everything you ingest is labeled,
  connected, and current
- Content-level sensitivity gating: public/personal/sensitive/intimate on
  every item — refuse-by-default
- Citation-closure reads: an agent can only cite sources it's authorized
  to see
- Gap analysis: your knowledge base measured against what's happening in
  your world

**Agent operations**
- Governed role hierarchy: an investigator finds, a reviewer checks, an
  executive briefs — with review gates between
- Evidence-labeled findings: every claim verified, inferred, or missing —
  unverifiable claims never count
- Admission-scored exception queue: you see only what deserves a person
- Scoped, expiring authority: agents act within mandates, everything
  audited
- Shadow→live promotion: new agent behaviors run record-only until they
  earn trust, with automatic demotion on regression

**Proactive surface**
- Daily executive brief: what happened, what it did, what needs you — in
  15 seconds
- Listen → curate → gap → opportunities: dozens of sources distilled
  daily into themes, trends, and what touches your work
- Self-expanding radar: adjacent sources, repos, and communities
  discovered and proposed for your approval

**Platform**
- Local-first model fleet with hybrid routing: your GPUs, cloud optional
  per task
- Stress + adversarial self-testing: every agent improvement is A/B'd
  against attack scenarios before adoption
- Self-hosted, single-command MCP install for your own notes

## How it compares

| Capability | **Aerial** | dsh-superagent | LangChain/LangSmith | CrewAI | Mem0 | Zep | Claude Code/Cursor | Devin |
|---|---|---|---|---|---|---|---|---|
| Content-level sensitivity gating | ✓ | ✗ | ✗ | ✗ | ✗ | partial¹ | ✗ | ✗ |
| Citation-closure enforced reads | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| Evidence-labeled findings | ✓ | ✗ | partial² | ✗ | ✗ | ✗ | ✗ | ✗ |
| Admission-scored exception queue | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| Scoped, expiring authority (mandates) | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ | partial | partial |
| Shadow→live promotion gate | ✓ | partial³ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| Gap analysis → opportunities → action | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| Local-first + hybrid routing | ✓ | partial | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| Adversarial self-testing | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| Self-hosted, one-command install | ✓ | ✓ | ✓⁴ | ✓ | ✓ | ✓ | ✗ | ✗ |

¹ Access-control exists at the enterprise tier; content-level labeling at
retrieval time is not the model. ² Traces and logs, not claim-level
evidence labels. ³ Record-only shadow mode exists; a promotion gate does
not. ⁴ Self-hosting is an enterprise offering.

*Where others are genuinely ahead today: LangSmith's observability depth,
CrewAI's community and maturity, Zep's memory scale, frontier cloud models'
raw capability, and everyone's polish. That's why hybrid routing exists —
use the best model for the task, keep the governance local.*

## The proof from our own fleet

- Our agents discovered that **48% of our own AI spend** went to a retired
  role nobody told the scheduler.
- One morning, a publishing agent found a locked database, **fixed the
  stuck page, and left a written diagnosis** — before anyone opened a
  ticket.
- A prompt-injection attempt embedded in job data was **refused and
  logged as evidence** — the agent treated the attack as a finding, not an
  instruction.

## Honest limits (v0.9-rc1)

- Self-hosted, single-operator focused; multi-tenant is not designed yet.
- The exception queue's live feed is the last build in progress.
- Local models trail frontier cloud models on raw capability — that's why
  routing is hybrid and per-task.
- Seven-day reliability record and dogfood validation are in progress.

## Try it

The governed read path for your own notes installs in one command:

```
uvx secondbrain-mcp --vault ~/notes --grant <id> --key <key>
```

Point Claude Code, Cursor, or any MCP client at it. Ask about something
sensitive — watch it refuse, by design.
