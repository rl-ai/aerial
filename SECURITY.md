# Security Policy

## Reporting a vulnerability

**Please do not open a public GitHub issue for security reports.**

Use [GitHub private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability) (Security tab → "Report a vulnerability") — this is the recommended default and keeps the report out of the public tracker until a fix ships.

If private reporting is unavailable to you, open a minimal public issue that says "I have a security report" and we will follow up to move the details into a private channel.

## Response SLA

| Step | Target |
|---|---|
| Acknowledgement | **48 hours** |
| Triage & severity assignment | 5 days |
| Fix target | **14 days** (critical issues may ship sooner; we coordinate disclosure timing with you) |
| Disclosure | Coordinated — advisory published after the fix lands, reporter credited unless they prefer anonymity |

## Scope

In-scope components:

- **`secondbrain_mcp/`** — the MCP stdio server (grant binding, citation-closure checks, sanitization, fail-closed behavior)
- **Console** — the dashboard server / `serve.py` entry point and its HTML rendering
- **Hub API** — the token-authenticated HTTP API (`/api/findings`, `/api/decisions/*`, `/api/mandates`, …)

Out of scope: dependencies' own advisories (please report upstream or to us with a link), social engineering of the operator, and reports about the *quality* of sanitization outputs rather than a mechanism that leaks content.

## Governed posture — read this first

This is a **governed agent system**: decisions that change state (approvals, dismissals, mandate creation) are **token-gated**, and the model layer cannot grant itself authority.

**Report authorization bypasses first.** If you find a way to:

- execute a write decision (approve / dismiss / escalate / create mandate) without a valid decision-scoped token,
- read content outside the session grant's citation closure,
- get `intimate`-labeled or out-of-scope content through the sanitization gate,
- or make a degraded component serve content instead of failing closed,

that is the highest-severity class of bug in this system, ahead of memory-safety-style issues. A write that "just works" because a token check was skipped is the failure mode we care about most.

## What we promise

- No retaliation, ever, for good-faith research.
- We will not pursue legal action against reporters who follow this policy.
- We keep you informed at each SLA checkpoint.
