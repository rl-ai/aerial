# FAQ

## Use cases

**What would I actually use this for?**
Three things, in the order people feel them: a daily plan built around you
(the planner connects your calendar, email, and notes, and finds the free,
easy, joyful things — not just efficiency); a knowledge base that stays
current (sources you choose flow in, labeled and connected); and a chief
of staff who reads everything and brings you one brief — what happened,
what it did, what needs you.

**Can it find issues and opportunities?**
That's the core promise. It measures your knowledge base against what's
happening in your world and surfaces the gaps: money leaking (we found 48%
of our own AI spend going to a retired role), time wasted, an opening that
matches your work. Opportunities land on a queue as one-click decisions.

**Does it act on them, or just tell me?**
It acts — by category. Safe, reversible work happens without ceremony
(organizing, filing, drafting, investigating). Anything touching money or
the outside world is escalated to you with exactly what will happen. Every
action is logged with the evidence it was based on.

**Is my data private?**
You choose your models (local or cloud, per task), your accounts and
platforms, and your storage location. Everything consolidates on your
hardware and stays where you want unless you choose cloud. The public
packages make no network calls beyond your own hub and model endpoints.

**What about really private stuff?**
Content is labeled public/personal/sensitive/intimate at ingest, with a
conservative default. Agents read through a governed path that refuses to
release sensitive content and can only cite sources they're authorized
for. Ask it about something intimate — it refuses, by design, and logs
the refusal.

## Workflows

**What does an incident look like?**
A job fails. An investigator agent digs in with real tools — logs,
filesystem, source code — and writes findings where every claim is labeled
verified, inferred, or missing. A reviewer checks the work. An executive
agent writes your brief: what happened, what was decided, what you need to
do. In one real incident, the investigating agent also fixed the stuck
page and left the diagnosis before anyone opened a ticket.

**How do agents get permission to do things?**
New behaviors run in record-only shadow mode first — they log what they
*would* do without doing it. Promotion to live requires passing evidence
criteria, and demotion is automatic on regression. Everyday actions run
inside scoped, expiring mandates.

**What's on my queue?**
Only what's worth a person: decisions with evidence attached, ranked by
admission strength. Everything else is handled and reported.

## Hard questions

**How is this different from LangChain/CrewAI?**
Those are frameworks for building agent systems. Aerial is a governed
operating layer: roles with review gates, evidence labels, an
admission-scored decision queue, and shadow→live promotion. You can build
a system like this on a framework — Aerial ships it working.

**How is it different from Mem0/Zep (memory tools)?**
They gate identity (who can access); Aerial gates content (what gets
shared) — sensitivity labels at ingest, citation-closure enforced per
read, refusal by default.

**What happens when an agent is wrong?**
You see it. Findings carry evidence labels, so an inferred claim never
dresses up as verified. Wrong actions are visible in the audit ledger,
shadow mode catches them before they're live, and regression demotes
agents automatically.

**Why local models?**
Privacy (your data on your hardware), cost (a full investigation costs
pennies on local GPUs), and control (you choose the model per task —
including cloud models for public workloads, since routing is hybrid).

**What's the state of it?**
v0.9-rc1: the console, the hierarchy, the gateway, and the MCP server are
shipped and tested (89+59 tests across the suites). A 7-day reliability
record and dogfood validation are in progress. Self-hosted, single-
operator today.
