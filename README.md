# Aerial — governed agents that work for you

Connect the accounts and platforms you choose. The agents listen, curate
your knowledge base, and turn gaps into opportunities that they act on —
and one executive agent, your chief of staff, consolidates it all into a
personalized report built specifically for you.

## In one glance

- **A daily planner built around you** — it connects your accounts,
  plans your day locally, and finds things that bring joy without costing
  money.
- **Privacy by design** — you choose your models, accounts, platforms,
  and storage location. Everything stays where you want unless you choose
  cloud.
- **Gaps become opportunities** — it curates your knowledge base from
  sources you choose, measures it against your world, and turns the gaps
  into work agents act on.
- **A chief of staff, not a dashboard** — one executive agent brings you
  a personalized report: what happened, what it did, what needs you.

## The planner

It plans your day around you — finding things that bring joy without
costing money. It saw the one sunny hour and moved your run there; it
batched the errands; it guarded your deep-work block. Every suggestion
shows why, and dismissing one makes tomorrow quieter.

## Privacy

Everything consolidates on your hardware. Content is labeled at ingest
(public/personal/sensitive/intimate, conservative default), agents read
through a governed path that refuses sensitive material, and every access
is logged where you can see it. The public packages make no network calls
beyond your own hub and model endpoints.

## Opportunities

The agents curate your knowledge base from the sources you choose and
measure it against what's happening in your world — money leaking, time
wasted, an opening that matches your work. From our own fleet: 48% of our
AI spend was going to a retired role nobody told the scheduler, found by
the system itself.

## The chief of staff

One executive agent consolidates updates and insights into a personalized
report: what happened, what the agents did, and the decisions that
genuinely need you — 15 seconds and you know your day. Investigations are
real: in one incident, an agent found a locked database, fixed the stuck
page, and left a written diagnosis before anyone opened a ticket.

## Under the hood

- Content-addressed knowledge base; sensitivity labels on everything
  (public/personal/sensitive/intimate, conservative default)
- Citation-closure reads — agents can only cite sources they're
  authorized for
- Admission-scored decision queue; scoped, expiring authority; full audit
  ledger
- Shadow→live promotion — new behaviors run record-only until they earn
  trust; regression demotes automatically
- Local-first model fleet, hybrid routing — your GPUs, cloud optional per
  task
- Every improvement A/B-tested against adversarial scenarios before
  adoption

## How it compares

| Capability | **Aerial** | dsh-superagent | LangChain/LangSmith | CrewAI | Mem0 | Zep | Claude Code/Cursor | Devin |
|---|---|---|---|---|---|---|---|---|
| Content-level sensitivity gating | ✓ | ✗ | ✗ | ✗ | ✗ | partial¹ | ✗ | ✗ |
| Citation-closure enforced reads | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| Evidence-labeled findings | ✓ | ✗ | partial² | ✗ | ✗ | ✗ | ✗ | ✗ |
| Admission-scored decision queue | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| Scoped, expiring authority (mandates) | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ | partial | partial |
| Shadow→live promotion gate | ✓ | partial³ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| Gap analysis → opportunities → action | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| Local-first + hybrid routing | ✓ | partial | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| Adversarial self-testing | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| Self-hosted | ✓ | ✓ | ✓⁴ | ✓ | ✓ | ✓ | ✗ | ✗ |

¹ Access control exists at the enterprise tier; content-level labeling at
retrieval time is not the model. ² Traces and logs, not claim-level
evidence labels. ³ Record-only shadow mode exists; a promotion gate does
not. ⁴ Self-hosting is an enterprise offering.

*Where others are genuinely ahead today: LangSmith's observability depth,
CrewAI's community and maturity, Zep's memory scale, frontier cloud
models' raw capability, and everyone's polish. That's why routing is
hybrid — use the best model for the task, keep the governance local.*

## Proof from our own fleet

- Our agents discovered that **48% of our own AI spend** went to a retired
  role nobody told the scheduler.
- One morning, a publishing agent found a locked database, **fixed the
  stuck page, and left a written diagnosis** — before anyone opened a
  ticket.
- A prompt-injection attempt embedded in job data was **refused and
  logged as evidence** — the agent treated the attack as a finding, not an
  instruction.

## Honest limits

- v0.9-rc1: self-hosted, single-operator focus; multi-tenant is not
  designed yet.
- The decision queue's live feed is in final integration.
- A 7-day reliability record and dogfood validation are in progress.
- Local models trail frontier cloud models on raw capability — that's why
  routing is hybrid and per-task.
