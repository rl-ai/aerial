# Operator Console — pilot onboarding (v0.9-rc1)

Welcome. This page walks you through installing the Operator Console, getting your
decision rights, and your first look. Three steps, at most three screens deep —
you point at **one** hub and you're done. Doc link target: `docs/references.yaml`
(project `operator-dashboard`).

---

## Step 1 — Install (one command)

Run this single command, replacing the URL with your hub's address:

```
python -m console.installer --hub-url https://<your-hub>/
```

Optional: `--config PATH` chooses where your config file lives
(default: `~/.config/operator-console/config.json`).

The installer:

1. Writes your config file — the hub URL and the path to your **read token**.
2. Verifies connectivity to the hub using your read token.
3. Prints `✓` when everything passes, or **the exact failing check** when
   something is wrong (see [Troubleshooting](#troubleshooting-the-exact-failing-checks)).

You need your read token file before running the command — ask the hub admin
(Ray, initially) for it. Reads always work with just the read token; you do not
need the write token to see the board.

## Step 2 — Grant (write token)

The **write token** is what lets you approve or dismiss queue items. Without it
the console is read-only, and that is fine — your first look is never blocked on
an admin.

- Request your decision-scoped write token from the hub admin via the hub's
  token flow.
- The installer checks for it. If it isn't there yet, you'll see:

  ```
  write token: missing — decisions disabled until granted
  ```

  This is not an error. Your install is still valid. Re-run the installer after
  the admin grants your token; it prints `✓ write token: present — decisions
  enabled` once the file exists.

## Step 3 — First look (the banner)

The first time you open the console without a write token, a banner appears at
the top of the page:

> You're viewing Ray's operator console (read-only until your write token is
> granted). See docs: docs/pilot-onboarding.md

- It is **dismissible, one-time** — click it away and it never comes back
  (the dismissal is saved in your config file).
- Once your write token is detected, the banner flips once to
  **"decisions enabled ✓"** and then never appears again — not after dismissal,
  not after restarts.

---

## What the console shows (panel order)

One single-column page, top to bottom, max width 1100px. Read it in this order:

1. **Header** — "Operator Console", generation time, and one source-state chip
   per zone (`A live` / `B stale · read 09:42` / `A unavailable`).
2. **Executive summary** — headlines, the numbers strip, decisions needed, risks.
3. **Sparkline strip** — three tiny charts (failure rate, spend, top-theme
   momentum), each showing 7 days plus a delta arrow. One glance, not a history
   page — nothing here is clickable.
4. **Zone B — Needs a person** — the exception queue. This is where you work;
   it sits near the top on purpose. Each row is a card with a colored left
   border by panel type (A amber, B blue, C violet).
5. **Zone A — Project statuses** — collapsed rows, expanded when waiting/blocked.
6. **Themes & trends** — scrape themes table with trend arrows, stack overlap.
7. **Zone C rail (inline)** — mandates ledger, shadow pulse. Quiet, below the fold.
8. **Raw stats** — token stats table and the untagged bucket.
9. **Footer** — version, hub build, and either "served from hub" or "static
   snapshot HH:MM".

## How to decide a queue item (two-step approve)

Every queue row shows exactly three actions: **Approve**, **Dismiss**,
**Escalate**.

**Approving is two steps — never a bare click:**

1. Click **Approve**. An inline confirmation panel expands below the row showing:
   the action, the mandate it will consume (scope + expiry, pre-filled from the
   row, editable *before* you commit), the evidence badge, and the consequence
   ("consumes 1 mandate use · audit record written").
2. Click **Confirm approve** to commit, or **Cancel** to return the row
   untouched — cancel commits nothing.

**Dismiss** opens the same expansion with an optional one-line reason field
(blank counts as "unspecified"). **Escalate** confirms inline with the route
shown (curator → lead → Ray → JEV) and collapses the row's actions.

After you act, the row itself is the feedback — approve shows
"executed ✓ (attempts N)", dismiss shows "dismissed · calibration sample
recorded". There are no pop-up toasts. If a write fails, you'll see a red
"not committed — nothing changed" line inside the row and the buttons come back.

## What the badges and ages mean

- **Evidence badges:** `verified` (solid badge) — checked evidence;
  `inferred` (outline) — derived, not directly verified; `stale`
  (striped, with tooltip) — evidence older than 24h; stale rows show their
  artifact link muted.
- **Age:** gray normally; turns **amber** when the row passes its class's
  SLA-breach threshold (4h / 24h / 72h depending on the queue class in
  `queue-policy.yaml`); gains a clock icon 24h before expiry. Expired rows
  simply leave the queue — you'll never see an "expired" state.
- **Row states:** `waiting-on-you` (normal), `escalated` (route shown, actions
  collapsed), done/dismissed rows fade out and are removed.

## Where pings go

The page has **no** notification center, bell, or unread markers — on purpose.
Pings live outside the console:

- **Email** = items waiting on **you** (additive — a waiting-on-you ping is
  never dropped).
- **Hub mentions** = everything else.

If you're wondering whether something needs you, check your email first; the
console shows the current state, not a feed.

## Static fallback usage

If the hub is unreachable you can open a static snapshot file
(`dashboard.html`) directly — same page, same layout. Differences:

- Queue action buttons are rendered but **disabled**, with the notice
  "actions disabled — open the hub /dashboard to decide".
- Sparklines come from the last-run snapshot.
- The footer says "static snapshot HH:MM" instead of "served from hub".

Everything else — layout, badges, panels — is byte-identical to the hub page.

## Troubleshooting the exact failing checks

The installer prints the exact check that failed. Match it here:

| Message | Meaning | Fix |
|---|---|---|
| `hub-url check failed: URL must start with http:// or https://` | URL malformed | Re-run with the full URL including scheme |
| `read token check failed: file not found: <path>` | Read token file missing | Copy the read token file to the path in your config |
| `read token check failed: empty token file: <path>` | Token file is blank | Ask the admin to re-issue it |
| `connectivity check failed: cannot reach <url>: ...` | Hub unreachable (network/DNS/down) | Check the hub address and your network |
| `connectivity check failed: hub returned HTTP 401 (401/403 — read token rejected)` | Read token invalid/expired | Request a fresh read token from the admin |
| `write token: missing — decisions disabled until granted` | Not an error — decisions disabled | Request the write token; re-run the installer after it's granted |

Still stuck? The full spec lives in `docs/references.yaml` under project
`operator-dashboard`; your hub admin can walk you through the token flow.
