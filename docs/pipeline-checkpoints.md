# Pipeline Checkpoint Contract (PC-1 … PC-7)

Aerial's end-to-end pipeline has seven gates. Each gate is a **contract**:
the rule, the enforcement point in code, the failure behavior (always
fail-closed — the stage cannot complete past the gate), the test that
proves the gate, and the live conformance check the ops agent runs against
the running pipeline. Testing is built into the process by design, not
bolted on after (the nine practices, `ai-ops/docs/testing-service.md`).

Pipeline order: ingest labels → curate → proposal admission → evidence
labels + lead review → action authority classifier → report integrity →
the Gauntlet. No artifact crosses a stage boundary without the gate for
that boundary having held; a stage that skips its gate is a defect (see
"The declaration rule").

---

## PC-1 — Ingest sensitivity labeling

- **Rule**: every item entering the vault carries a sensitivity label that
  is either (a) present and on the vault lattice, or (b) absent — which
  takes the conservative default `personal`. A present-but-invalid label
  (not on the lattice) must never be silently relabeled to
  `personal`/`public`.
- **Enforcement point**: `gateway/bridge.py::note_sensitivity`
  (lines ~117-137): missing label → `personal`; valid label → itself;
  invalid label → raises `ClassificationError`. Mirror enforcement in
  `secondbrain/listening.py::_sensitivity` (lines ~503-517, blank counts
  as MISSING → same defaults).
- **Failure behavior**: fail-closed — `ClassificationError` aborts the
  note build; the item is skipped, never relabeled. The bridge docstring
  states it outright: "unknown labels FAIL CLOSED — the bridge never
  relabels an unclassified item."
- **Test**: `gateway/test_bridge_labels.py` (+ `test_bridge.py`).
- **Live conformance check (ops agent)**: sample the N most recent ingests
  from the bridge log / vault; for each, confirm the persisted label is on
  the lattice or the item was recorded as defaulting to `personal`. An
  item with a non-lattice label that *entered* the vault = a bypass (R1).

## PC-2 — Curate

- **Rule**: only parsed, well-formed notes reach the curated store. Every
  ingest run produces an `IngestReport` (counts, failures, governance
  labels parsed back out of the ingested notes) — the curation step cannot
  emit an uncounted, unreported batch.
- **Enforcement point**: `gateway/secondbrain/curator.py`
  (`Curator`, `parse_note`, `IngestReport`); report summary assembled in
  `bridge.py::build_digest_summary` (labels parsed back out of the ingested
  notes — the curation round-trip check).
- **Failure behavior**: fail-closed — a note that cannot be parsed is not
  curated; the report records the failure instead of dropping it silently.
- **Test**: `gateway/test_secondbrain.py` (curator cases).
- **Live conformance check**: sample recent ingest reports and confirm
  `ingested + skipped + failed` reconciles against items seen by the
  listening cycle for the same window (no silent drops).

## PC-3 — Proposal admission

- **Rule**: a proposal may only be admitted if (a) it is not currently
  suppressed — dismissed for 8 weeks, or permanently after 3 dismissals,
  and (b) it is under the absolute burst alarm ceiling (50/day, never
  suppressible). Suppression state must be read from the persisted ledger,
  never recomputed from memory.
- **Enforcement point**: `gateway/secondbrain/proposals.py`
  (`suppressed()` ~L380, `record_decision()` ~L392, `load_ledger()` /
  `save_ledger()` ~L362-372, `BURST_ABS_PER_DAY = 50` ~L73).
- **Failure behavior**: fail-closed — a suppressed or over-ceiling candidate
  is not admitted; the decision (dismiss/suppress reason) is persisted so
  the same candidate cannot loop back in.
- **Test**: `gateway/test_proposals.py`.
- **Live conformance check**: sample recently admitted proposals; verify
  each has a ledger entry and that no admitted proposal matches an active
  suppression key or exceeds the daily ceiling.

## PC-4 — Evidence labels + lead review

- **Rule**: every admitted proposal carries ≥3 verbatim evidence items
  (raw text excerpts with links, spec §3.3) — a proposal is never admitted
  on a bare claim; lead review decisions attach to evidence.
- **Enforcement point**: `gateway/secondbrain/proposals.py::build_evidence`
  (~L341: "≥3 verbatim evidence items") and `proposal_row()` (~L444),
  which writes `evidence` into the persisted row.
- **Failure behavior**: fail-closed — a candidate that cannot produce ≥3
  evidence items does not become a proposal row.
- **Test**: `gateway/test_proposals.py` (evidence build).
- **Live conformance check**: sample persisted proposal rows; verify each
  has ≥3 non-empty evidence entries with source links.

## PC-5 — Action authority classifier

- **Rule**: every curator finding is classified R1-R6 before any action;
  the classification is first-match-wins with R6 fail-closed run FIRST —
  an unrecognizable finding (missing `finding_id`/`claim`, unknown
  reversibility or scope) is `unknown` and is NEVER auto-fixed, even if
  its claim matches a known pattern. Security (R1) and SEV (R2) ping
  immediately; unknown/daily-report classes escalate to the daily report;
  auto-fix only under reversible + internal + covering mandate.
- **Enforcement point**: `ai-ops/dashboard/followup/classifier.py::classify`
  (R6 gate at ~L101 runs before R1) and `FollowupFlow.submit` in
  `dashboard/followup/flow.py` (~L176-195): immediate classes ping, auto-fix
  / mandate continue into the fix path, everything else escalates. Every
  transition is persisted with a timestamp (`transitions` table) so the
  audit trail shows exactly which branch a finding took.
- **Failure behavior**: fail-closed end-to-end — mandate creation failure,
  fix failure, or failed verification all escalate (`flow.py` failure
  branches); unknown classification escalates, never auto-executes.
- **Test**: `ai-ops/dashboard/tests/test_followup.py` (classifier + flow
  hermetic tests; external effects are injected callables).
- **Live conformance check**: sample recent findings in the followup
  sqlite (`followups` + `transitions`); verify every finding has a
  fix_class, no `unknown`-class finding reached `fix-attempted`, and every
  `closed` finding has a persisted verify transition.

## PC-6 — Report integrity

- **Rule**: the daily/exec report never fabricates. A missing or unreadable
  source degrades to a labeled `unavailable` — never invented numbers.
  Known instrumentation gaps are labeled from the constant `KNOWN_GAPS`
  list, never from the model. Ticket IDs cited in prose are verified to
  exist in the hub before the report ships; sections are capped and the
  DeepSeek writer output is parsed back into named sections, not pasted.
- **Enforcement point**: `ai-ops/dashboard/exec_summary.py` — `_unavailable`
  (~L132, fail-closed gatherers), `KNOWN_GAPS` (~L57), `verify_ticket_ids`
  (~L577, rewrites cited IDs against the hub DB), `numbers_strip` (~L670),
  `cap_headlines` / `cap_decisions` / `cap_risks` caps (~L717-752),
  `parse_ds_output` (~L1000), `write_summary_section` (~L1015).
- **Failure behavior**: fail-closed — a source that cannot be read yields
  `unavailable` in that block; the report still ships but never with
  fabricated data in place of a missing source.
- **Test**: `ai-ops/dashboard/tests/test_exec_summary.py`,
  `test_exec_summary_ticket_ids.py` (gather fail-closed, ticket
  verification, caps, section parsing).
- **Live conformance check**: take the latest produced summary and verify
  (a) every cited ticket ID exists in the hub, (b) no `unavailable` source
  contributed numbers, (c) section counts are within caps.

## PC-7 — The Gauntlet (release gate)

- **Rule**: no release without a green Gauntlet — 5/5 layers (units,
  stress, backtest, day-in-the-life, live smoke) PASS on the ticket trail.
  The ticket trail IS the release evidence. Binding at pre-release.
- **Enforcement point**: `ai-ops/dashboard/gauntlet.sh` + `gauntlet.py`;
  standing rule in `ai-ops/docs/testing-service.md` ("No release without
  a green Gauntlet", practice 5); the integrator blocks releases on
  practice-5 failures.
- **Failure behavior**: fail-closed — a failed layer blocks the release;
  there is no partial-release path.
- **Test**: the Gauntlet itself is the test-of-tests (practice 5, 5-layer
  ticket-based); `dashboard/validate_days.py` feeds layer 4.
- **Live conformance check**: for any release event, verify the referenced
  ticket trail shows 5/5 layer PASS records; a release with a missing or
  failed layer record = a bypass (R1).

---

## The conformance verifier (ops agent poll-loop pass)

**Host**: the role runner, `ai-ops/roles/runner.py` — `runner.py tick`
walks enabled charters, runs due duties in-process, and persists a
`role_tick` checkpoint. The conformance verifier is a **duty** on the ops
charter: short, in-process, on the tick cadence (5 min); anything slow is
a worker (runner.py contract: "Duties must be short").

**Spec**: one check function per gate, each asserting the gate rule against
live data:

| Gate | Check against live data | Sample |
|---|---|---|
| PC-1 | Recent ingests: sensitivity label valid-or-defaulted | last N bridge/vault ingests |
| PC-2 | Ingest reports reconcile (seen = ingested + skipped + failed) | last N ingest reports |
| PC-3 | Admitted proposals not suppressed, under burst ceiling | last N proposal rows |
| PC-4 | Proposal rows carry ≥3 verbatim evidence items | last N proposal rows |
| PC-5 | Findings classified; no unknown reached fix-attempted; closed findings verified | followup sqlite |
| PC-6 | Cited ticket IDs exist; unavailable sources contributed nothing | latest summary + hub DB |
| PC-7 | Release tickets show 5/5 layer PASS | hub ticket trail |

**Classification of results** (feeds the existing escalation ladder,
`testing-service.md`):

- A gate rule that **fails against live data** (e.g. a proposal row with
  <3 evidence items) is a finding classified through
  `dashboard/followup/classifier.py` — R6 fail-closed default → daily
  aggregate, immediate ping only if the claim matches R1/R2 vocabulary.
- A gate that was **BYPASSED** — an artifact crossed a stage boundary
  without the gate running at all (no classification record, no ingest
  report, a release with no Gauntlet trail) — is **R1 security** by
  definition: a bypass of the action-authority architecture is a
  fail-open, routing via the classifier's `SECURITY_PATTERNS`
  (fail-open, auth-bypass, secret-leak) to `sev-security` → immediate
  ping + fix mandate.

**GAPS** (to be built — none of this exists yet as a runtime check):

- G-1: No conformance-verifier duty exists in `roles/` — the check
  functions PC-1..PC-7 must be implemented and registered on the ops
  charter.
- G-2: No wired probe reads the followup sqlite / proposal ledger / hub
  DB from the runner today; sampling adapters are new code.
- G-3: PC-2 reconciliation has no persisted "items seen" counter on the
  listening side to reconcile against (`listening.py` digests carry
  failures and evidence, not a seen-count).
- G-4: PC-7 has no automated release-ticket parser; today the Gauntlet
  trail is checked by hand by the integrator.

## The declaration rule

**Stage transitions that skip their gate are defects.** The contract is
the architecture: the gate is not a test that happens to run near the
stage, it is part of the stage's definition. Therefore:
1. **Adding a new pipeline stage REQUIRES declaring its gate** — same
   rule as the release checklist's Gauntlet requirement (no release
   without a green Gauntlet ⇒ no new stage without a declared checkpoint).
   A stage PR without a PC declaration is rejected at review, exactly as
   a release without a Gauntlet trail is blocked by the integrator.
2. **Declaration format**: a new stage lands with (a) its PC number, (b)
   the rule in one sentence, (c) the enforcement function, (d) the
   fail-closed behavior, (e) the unit test, (f) the conformance check
   added to the verifier table above.
3. **Conformance includes declaration**: the verifier treats a stage
   transition with no corresponding gate record as a bypass (R1), and a
   stage found in code with no PC declaration as a finding (R6 → daily
   aggregate, since the stage itself failed to declare).

## Acceptance criteria

a. Every PC-1..PC-7 entry cites a real enforcement function in the tree
   today, and the cited test file exists and passes.
b. The verifier is implementable as a short runner duty: each check reads
   only persisted, queryable state (bridge/vault ingests, ingest reports,
   proposal ledger, followup sqlite, hub DB, Gauntlet tickets).
c. Every failure behavior is fail-closed: a failed or skipped gate
   prevents the stage from completing — no path advances an artifact with
   an unheld or unrun gate.
d. A bypassing gate classifies as R1 (immediate ping) and a merely failing
   gate as R6-default (daily aggregate) via the existing classifier —
   verified by a classifier unit test using bypass claims (fail-open
   vocabulary).
e. The declaration rule is enforceable in review: a new-stage PR is
   rejected without declaration items (a)-(f), and the verifier flags any
   discovered stage with no PC declaration.
f. GAPS G-1..G-4 are the build backlog for the verifier; the contract is
   not live until G-1 and G-2 land.

*Grounded in: `ai-ops/docs/testing-service.md` (nine practices), `ai-ops/
dashboard/followup/{classifier.py,flow.py}` (R1-R6 state machine),
`ai-ops/dashboard/exec_summary.py` (fail-closed gather, KNOWN_GAPS,
verify_ticket_ids, caps), `gateway/bridge.py` +
`secondbrain/{listening,curator,proposals,admission}.py` (labeling,
curation, admission, evidence), `ai-ops/dashboard/{gauntlet.sh,
gauntlet.py,validate_days.py}`, `ai-ops/roles/runner.py` (duty host).*
