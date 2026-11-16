# Failure-follow-up workflow — design spec

Status: approved design (v1), implements Ray's binding policy of 2026-11-12.
Owner: designer → built by `dashboard/followup/` (worker lane t_2a5dff15).
Bindings D1–D5 in the task body are **settled**; this document elaborates them,
it does not re-open them.

## 0. Policy statement

Ray's policy, binding: **individual failures never reach Ray.** Agents
investigate every admitted failure and fix it themselves when the fix is
safe. Only items that remain unresolved — or that are significant by rule —
aggregate into the daily exec report's `decisions-needed` section. The
`platform-down` health kind is the ONLY immediate ping.

Consequences this design enforces:

- There is no code path that emails/pings Ray for a single failure. The only
  individual-notification channel in the system emits for `platform-down`.
- An agent fix is an ordinary write executed under the existing authority
  mechanism (MandateFlow). No second approval mechanism is introduced (D2).
- The value Ray sees for this workflow is a single line in the daily report:
  "agent-actions: N fixes closed silently, M decisions needed" (D5).

## 1. Pipeline overview

```
curator findings (admitted via Queue.admit, Eval-gate + admission threshold)
        │
        ▼
┌───────────────────────┐
│ safe-fix classifier    │  verdict ∈ {auto_fix, investigate, escalate}  (D1)
└───────────────────────┘
   │            │            │
auto_fix    investigate    escalate
   │            │            │
   │      agent gathers   MandateFlow._escalate → daily report
   │      evidence, then  decisions-needed (D5); state escalated_daily
   │      RE-classifies (once, see §4)
   ▼            │
┌───────────────────────┐
│ fix under scoped       │  MandateFlow._create_transaction → _execute (D2)
│ auto-fix mandate       │
└───────────────────────┘
        │
        ▼
   verification (re-run of the check that produced the finding)
        │
   pass ──────► closed_silent   (logged to hub ticket/ledger; never emailed)
   fail ──────► one bounded retry → still failing → escalate to daily report
```

## 2. State machine (D3)

States: `detected → classified → fix_attempted → verified → closed_silent | escalated_daily`

```
detected ──classify──► classified ──run fix──► fix_attempted ──verify ok──► verified
                          │  ▲                      │                             │
                    investigate│                 verify fail                      close
                    (re-classify, max 1)         │ (bounded retry once, then     ▼
                          │                      │  escalate)               closed_silent
                          └── auto_fix/escalate ─┴──────────────► escalated_daily
```

Rules:

- `investigate` is a **sub-state of classification**, not a terminal: while in
  it the agent gathers evidence and MUST re-classify to `auto_fix` or
  `escalate` before any write. Exactly one re-classification is allowed; if
  evidence is still insufficient, the item escalates (insufficiency is itself
  a decision Ray needs to see).
- `fix_attempted → verified` requires the verification re-run described in §6.
  A failed verification may retry the fix **once** (fresh attempt logged); a
  second failure escalates.
- Every transition appends an audit event (hub ticket event or the
  ledger/audit_records table — see §5). Nothing is emailed per transition (D3).
- Deadlock guard: any item in a non-terminal state older than its impact
  class's `expiry_hours` (queue-policy.yaml) sweeps to `escalated_daily` by
  the expiry sweep (`Queue.sweep_expiry` semantics; brief-only, no ping).

### 2.1 Follow-up record (data shape)

One record per admitted finding, table `followup_items`:

| field | type | notes |
|---|---|---|
| `id` | TEXT PK | `fu-<n>`, references `queue_items.item_id` |
| `source_finding` | JSON | the admitted `Finding`: condition_key, claim, impact_class, evidence_label, admission_score, source_agent |
| `verdict` | TEXT | `auto_fix \| investigate \| escalate` (D1) — null while `detected` |
| `verdict_reason` | TEXT | which classifier rule fired (rule id from §3) |
| `mandate_id` | TEXT NULL | set when a scoped auto-fix mandate is created (§5); null for escalations |
| `attempt_log` | JSON array | one entry per fix attempt: `{ts, actor, action, diff_or_command_ref, mandate_id, outcome}` |
| `verification` | JSON NULL | `{method, check_ref, ts, passed, output_ref}` |
| `state` | TEXT | current state per §2 |
| `final_state` | TEXT NULL | `closed_silent \| escalated_daily`, set once |
| `created_at`, `updated_at` | REAL | epoch seconds |

`attempt_log` entries for executed fixes MUST reference the MandateFlow
`execution_log.idem_key` so the exactly-once guarantee is auditable.

## 3. Safe-fix classifier (D1) — rule table

Verdicts exactly three (D1). Rules evaluate in order; **first match wins**.
Inputs are fields of the admitted Finding plus the touched-artifact metadata
the classifier derives from `condition_key` and `evidence_label`.

| # | rule | inputs | verdict |
|---|---|---|---|
| R1 | money movement | `artifact_type ∈ {payment, billing, invoice, subscription, api_key_with_billing, budget_config}` OR finding mentions charge/refund/spend | `escalate` |
| R2 | public-facing surface | `artifact_type ∈ {public_repo, website, sent_email, social_post, published_doc, dns_record}` OR blast_radius includes external audience | `escalate` |
| R3 | irreversible state | `reversibility_flag = false` OR `artifact_type ∈ {user_data_delete, production_db_migrate, credential_rotate, git_force_push}` | `escalate` |
| R4 | unknown blast radius | `blast_radius` field missing/unresolvable from evidence | `investigate` |
| R5 | unknown touched artifact | `artifact_type` unresolvable from condition_key/evidence | `investigate` |
| R6 | default safe | `reversibility_flag = true` AND blast_radius = `internal` AND artifact_type not in any escalate set | `auto_fix` |

Input definitions (testable):

- `artifact_type`: derived lookup table `condition_key prefix → artifact class`,
  maintained in the classifier module, one entry per known curator condition.
  An unmapped key yields "unknown" (→ R5).
- `reversibility_flag`: boolean; true only when the classifier knows a concrete
  inverse action (revert commit, restore file, re-enable flag, re-run with
  corrected input). Absent ⇒ treated as unknown (→ R4).
- `blast_radius`: one of `internal` / `shared` / `external`. `shared` (other
  agents, user-visible-but-internal dashboards) still auto-fixes; `external`
  (anything a human outside the system sees) is R2.

Notes:

- R1–R3 are hard stops: a `fix` never runs for them, even after investigation.
  `investigate` (R4/R5) exists to *reduce* escalations by gathering facts, not
  to permit risky fixes.
- Verdict + rule id are written to `verdict`/`verdict_reason` on the record.

## 4. Investigate loop

When verdict is `investigate`: the agent reads the finding's evidence, the
affected artifact, and recent logs; fills in `artifact_type`,
`reversibility_flag`, `blast_radius`; then re-runs the rule table (one
re-classification, max). Outcomes: `auto_fix` (proceeds to §5),
`escalate` (proceeds to §5 escalation), or — if still unknown after the
bounded investigation — `escalate` with verdict_reason
`investigation-inconclusive`. The investigation window is bounded by the
item's impact class SLA from queue-policy.yaml; expiring during investigation
escalates via the §2 deadlock guard.

## 5. Mandate integration (D2)

The classifier verdict picks the mandate level. One authority mechanism:
`dashboard/exception_queue/mandate_flow.py::MandateFlow`.

- **`auto_fix`** — the followup engine calls MandateFlow as the acting agent:
  1. `_create_transaction(mandate_id, item, scope, expiry_at, actor)` — a
     **scoped auto-fix mandate** is created. Scope = the single corrective
     action for this finding (mirrors `_prefill`: `proposed_action`/`claim` as
     scope); expiry = the finding's impact-class `expiry_hours`; actor = the
     followup engine's agent identity (not "Ray" — Ray never approves here).
  2. `_execute(item, mandate)` — runs the fix under the existing bounded
     retry / idempotency (`action_intents`, `execution_log.idem_key`,
     WriteToken re-auth-once) semantics unchanged. `mandate_id` is recorded on
     the followup record.
  - No new decision verbs: the existing `decide()` path is NOT used by
    agents (its approvals are human decisions); the engine drives the
    same private methods `_approve` composes (`covering_mandate`,
    `_create_transaction`, `_execute`) through a thin wrapper so the audit
    trail (`audit_records`) stays in one table.
- **`escalate`** — maps to MandateFlow's existing `_escalate(item, actor)`:
  sets row state `escalated`, returns the route; the followup engine then
  writes the item into the daily report's decisions-needed aggregation (§7)
  and sets `final_state = escalated_daily`. Route terminator for this
  workflow is the daily report, not an individual message (D5).
- **`investigate`** — no mandate exists; nothing is written until
  re-classification (§4).

Mandate records remain in MandateFlow's own `mandates` table; the followup
record carries only the reference. `re_execute` is the retry path after a
failed verification attempt (§6), preserving exactly-once per attempt.

## 6. Verification

A fix is only closed when the **same check that produced the finding** (the
Eval-gate check behind `condition_key`) is re-run and passes:

- `verification = {method: "re-run <check_ref>", passed: true, ...}` →
  `verified` → agent closes: `final_state = closed_silent`. The close appends
  a hub-ticket/ledger event and a line to the daily report's agent-actions
  counter. No email, no ping (D3/D5).
- failed verification → one bounded retry: `re_execute(item_id)` under the
  same mandate (within expiry), new `attempt_log` entry; re-verify. Second
  failure → `_escalate` → daily report.
- If the finding's check is not re-runnable (no check_ref resolvable), the
  item escalates (`investigation-inconclusive` analog: verification-impossible
  — an agent must not self-certify).

## 7. Notification policy (D4) — verbatim routing

`dashboard/exception_queue/queue-policy.yaml` gains a `ping_policy` section:

```yaml
ping_policy:
  immediate_ping_kinds: [platform-down]
  # platform-down = console/scheduler dead (health event). N-rules keep it
  # as the ONLY immediate ping kind.
  brief_kinds:            # route to brief / daily aggregate, never ping
    - job-failure         # existing demotable
    - sla-breach          # existing demotable
    - mandate-expiry      # existing demotable
    - lower-admission     # existing demotable
    - waiting-on-you      # additive kind, now brief-only
    - blocked-4h          # additive kind, now brief-only
    - policy-blocking     # additive kind, brief-only
```

Changes to `dashboard/notifications/engine.py` implied by this:

- `platform-down` is a NEW kind: emitted by the health monitor when the
  console or scheduler is detected dead (N consecutive failed health probes;
  N per existing health rules). It bypasses the daily cap, never coalesces,
  and is the only kind written to `ping_sink`.
- `DEMOTABLE_ORDER` kinds and `ADDITIVE_KINDS` (`waiting-on-you`,
  `blocked-4h`) plus the new `policy-blocking` all route to `brief_sink`
  only. The daily cap/coalescing machinery still applies to ordering within
  the brief, but no demotable kind may ever reach `ping_sink`.
- The existing per-kind once-pings (SLA-breach single ping, waiting-on-you
  immediate ping, blocked>4h ping) are demoted to brief lines by this policy —
  their detection logic is unchanged, only the sink changes.

## 8. Daily exec report integration (D5)

The daily exec report (07:35 email + console, v2 design) is the **sole
escalation surface**:

- `decisions-needed` section lists every `escalated_daily` item open at
  report time: id, one-line claim, verdict_reason, what the agent already
  tried (attempt_log summary), and the decision required. Items Ray decides
  on follow the existing queue decision verbs (`approve`/`dismiss`/`escalate`
  via `MandateFlow.decide`).
- `agent-actions` line: counts followups closed as `closed_silent` in the
  reporting window (and running total), e.g.
  `agent-actions: 7 fixes closed silently · 2 decisions needed`. This is the
  headline value Ray sees for the workflow.
- A followup that stays open across days appears once per day until decided;
  it never generates anything outside the report.

## 9. Edge cases

- **Duplicate findings** (same `condition_key` re-admitted): attach to the
  existing open followup record as another `source_finding` entry rather than
  a new record; do not re-run a closed one.
- **Fix touches a file another lane is editing** (hotspot): before executing,
  the engine checks for an open mandate/lock on the artifact; collision →
  `investigate`, defer, re-classify later.
- **Mandate expiry mid-retry**: `_execute` under an expired mandate fails the
  WriteAuth path; the retry escalates instead of minting a longer mandate.
- **Escalated item later becomes safe**: Ray's decision, not the agent's — the
  agent may add evidence to the record but never re-opens auto-fix for an
  escalated item.
- **Platform-down while followups pending**: followup engine halts (no
  scheduler); pending records keep their state; the ping is emitted per §7.

## 10. Tests to write (A5)

Location: `dashboard/tests/test_followup_F.py` (worker lane), plus classifier
unit tests in the same file or `test_classifier_F.py`.

1. **Classifier table cases** — for each rule R1–R6: an input Finding
   (artifact_type, reversibility_flag, blast_radius, condition_key) asserting
   the exact verdict and rule id; including the "first match wins" precedence
   case (money + reversible → escalate, not auto_fix) and the unknown-input
   cases (R4/R5 → investigate).
2. **Investigate re-classification** — investigate → evidence gathered →
   re-classify to auto_fix; investigate + inconclusive → escalate with
   `investigation-inconclusive`; attempt at a second re-classification is
   rejected.
3. **State machine transitions** — legal path detected→classified→
   fix_attempted→verified→closed_silent; verify-fail→retry→verified;
   verify-fail×2→escalated_daily; illegal transition (fix before classified)
   raises. Every transition asserted to append exactly one audit event.
4. **Mandate integration** — auto_fix creates a scoped mandate via
   `_create_transaction` (scope = corrective action, expiry = class
   expiry_hours, actor = agent identity), executes via `_execute`, exactly-once
   on repeat; escalate calls `_escalate` and sets final_state=escalated_daily;
   investigate creates no mandate.
5. **ping_policy routing** — load patched queue-policy.yaml; assert
   `immediate_ping_kinds == ["platform-down"]`; for each kind in
   `brief_kinds` assert the event reaches `brief_sink` and never
   `ping_sink`; assert cap/coalescing still orders the brief.
6. **platform-down ping** — N failed health probes → exactly one
   `platform-down` ping (bypasses cap, not coalesced, not repeated while the
   outage persists); recovery produces a brief line, not a ping.
7. **Silent-close accounting** — closed_silent items appear in the daily
   report agent-actions count and nowhere else; escalated items appear in
   decisions-needed exactly once per open day.
8. **Expiry sweep** — a non-terminal followup older than expiry_hours sweeps
   to escalated_daily with a brief-only entry, no ping.
