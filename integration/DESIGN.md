# Aerial integration tests: design

## A5a: the hermetic contract test (design doc §15.8a)

Ticket TK-EXAMPLE-2 (plan step A5a, plan review P1). This is the first acceptance gate of the MVP. It drives one full Ops Analyst duty through the three landed products against fakes only: Roles builds and schedules the work, Runtime admits and runs it, and Tickets records it. Its job is to prove the contracts join end to end. The products' own tests already prove each piece alone. No network, no model server, no clock: time is injected and every model call goes to a fake.

The metrics-panel assertions of §15.8a join when A4b lands; until then this test covers the rest.

### Lane `hermetic`

Owns `integration/test_hermetic_cycle.py` only. It imports the three packages from `packages/<name>/src`, putting those three paths on `sys.path` at the top of the test file. That is acceptable in a test file only; package code never does it. No conftest.py, no `__init__.py`. It uses `tmp_path` for every database. It uses the landed APIs, never copies of them: `rlai_roles` (`rolepkg.load_role`, `registry.Registry`, `scheduler.Scheduler`, `tasks.build_task`), `rlai_runtime` (`runs.store.CoreStore`, `runs.records.Emitter`, `runs.recovery`, `adapters.fake.FakeAdapter`, `gateway.Gateway` and `gateway.usage` with a fake provider), and `rlai_tickets` (the store and its C2 `record`). It loads the shipped `curator-ops` role package from `packages/rlai-roles/src/rlai_roles/roster/`.

**Rules.**
1. The deployment: temporary SQLite databases for the three products, a fake resources file with one `local`-billed resource and no rate, a fake provider that returns a fixed completion with usage, and a mock channel (a list).
2. The cycle: hire `curator-ops` in the registry, then fire its first due duty through the scheduler with fixed evidence inputs. Build the C1 task with `build_task`. Runtime admits it (`CoreStore.admit`), starts it on the `FakeAdapter` and finishes it with a result. The run makes one model call through the gateway, and the run's C2 records (emitted by `Emitter`) go to Tickets `record`. The outcome record carries `agent_reported` and an evidence summary.
3. Every check below asserts on what the products return or store, never on internal state.

**Tests (hermetic 1–10).**
1. The scheduler fires, a second fire with the same inputs skips and builds no C1, and a recovery decision appears when the issue clears.
2. The C1 task carries the envelope fields `message_id`, `producer`, `sent_at` and `idempotency_key`, and validates as C1. A root task has no `run_id` or `seq`: under design doc §5.1, Runtime mints the run id on admit.
3. Runtime admits the C1, and a second delivery with the same `idempotency_key` returns the existing run, not a new one.
4. The run reaches its finished state through the `FakeAdapter`.
5. C2 records carry the envelope, their `seq` runs in order with no gap, and each validates as C2.
6. Tickets stores each C2 record once: a second delivery of the same `message_id` is acknowledged and not applied twice.
7. An outcome record with result `agent_reported` and an evidence summary is stored.
8. A simulated controller restart (reopen the Runtime store mid-run) makes `recover()` report the run as pending or uncertain, and creates no duplicate run.
9. Usage is recorded with the call's token counts. The local class shows no provider charge (absent, never 0), as the usage summary reports it.
10. Every existing suite still passes. This is checked by the orchestrator, not written as a test.

Done: tests 1–9 pass from the repo root with `python -m pytest integration -q`.

## A5: the real-path qualification (design doc §15.8b)

Ray approved starting A5 on 2026-11-10. This is the first gate that runs on the real system: Ray's hub, the real Hermes adapter (`rlai_runtime.adapters.hermes`, which drives `hermes kanban` and Docker contained mode), the real model route, and the real stop path. It follows Codex's bypass draft (`AERIAL-A5-BYPASS-DRAFT.md`, reviewed, draft only) for tests T1–T5 and its guardrails.

### Guardrails (every stage)

1. **One synthetic, non-vault employee.** `A5-TEST-01` is hired only for this qualification. It holds no vault profile and no real employee's data. Its tools are one granted echo tool (the control) and one registered tool that is not granted. No roster role runs until every stage has passed.
2. **No real credentials.** Credential stand-ins live only in a dedicated fixture folder and hold the literal text `SYNTHETIC-NOT-A-CREDENTIAL`. Real bearer files are never read, copied or handled, by any agent, harness or person.
3. **Inert targets.** There are no internet targets: only a closed port and a loopback listener the harness owns.
4. **Tickets scope.** Every ticket the test touches is under one test epic tagged `a5-qualification`. Writes target only those tickets.
5. **No destructive cleanup.** Teardown revokes the test token and stops sessions through the documented paths. Records are kept as audit evidence.
6. **No pass on absent logs.** A stage passes only on positive evidence: a recorded refusal, or an observed `verified_stopped`. Otherwise it reports `unverified`, naming the check that is unknown (§6.5 unknown rule).
7. **Opt-in and dry-run by default.** The harness `integration/qualify_real.py` runs nothing live unless the stage is named on the command line, and every live stage first prints what it will do. Each stage writes its own evidence record (JSON) under the harness's output folder and a comment on the test epic.

### Stages, in order (each gates the next)

0. **Preflight (read-only).** The hub answers with the eva-harness token; the Hermes CLI and Docker are reachable; the worker and reviewer model routes answer `/v1/models`; the operator view mount answers 403 without authorization.
1. **Hire.** Hire `A5-TEST-01` from a synthetic role package in Roles, with policy preset `draft`, a non-vault Hermes profile `a5-test` created through `bin/profile-ctl`, the synthetic tool registry and a read-only Tickets token.
2. **End to end (§15.8b).** A synthetic failed job in the test scope is detected by the test employee's duty. The employee attaches evidence with labels (§11.8) and posts one recommendation as a ticket comment, through Tickets, with C2 records. The C2 records, outcome record and usage are checked as in A5a, now against the real products.
3. **Stop tests (§6.5).** Stop during a sandbox command, during a model request, with queued work, across a Runtime restart, with a concurrent admission, and with a late result arriving after the stop. Each reaches `verified_stopped`, or is reported `unverified` naming the unknown check.
4. **Bypass tests (§11.7, Codex T1–T5).** Reach the network directly, read a credential path, call a tool not granted, use the disabled native terminal, and write with the read-only Tickets token. Each must fail at its expected layer (L1 admission or L2 sandbox) and be recorded. T1's verdict is capped at UNKNOWN until its execution mechanism is settled, as the draft says.
5. **Operator view.** On the real hub mount, subagent visibility shows the parent task, role, model and slot, the orchestrator or worker label, elapsed time and links to results.
6. **Metrics end to end (§11.9).** The panel's values match a manual computation from the underlying records, and stage B tiles show not applicable, never zero. Serving the projection through Ray's Datasette is a separate step behind Ray's gate and is not part of this stage.

### Build order

The harness is built in lanes like any ticket: stages 0–2 first, then 3, 4 and 5–6. Each stage's harness code is reviewed, and run in dry-run mode, before its first live run. The hub `/operator` mount (A4) and the Hermes profile for `A5-TEST-01` are prerequisites, landed by hand.
