# RC1 Console — Verification Report (v0.9-rc1)

**Card:** t_9e849c3b (RC1 lane 6: integrate + acceptance suite + e2e queue loop + verdict)
**Date:** 2026-11-12 · **Built tree:** `<workdir>/dashboard` (mirrored to `<workdir>/rc1/integration/build/dashboard`)
**Full suite:** `pytest build/dashboard/tests/ -q` → **89 passed, exit 0** (analyst venv, from `<workdir>`), of which 15 are the cross-module V1–V13 acceptance suite `tests/test_rc_acceptance.py`.

## 1. Integrated deliverables

- `build/dashboard/console/`: `template.py` (lane 1), `actions.py` (lane 2), `sparklines.py` (lane 3), `serve.py` (lane 4), `installer.py` (lane 5), merged `__init__.py`
- `build/dashboard/docs/pilot-onboarding.md` (160 lines, §1–§7) · `build/dashboard/HUB_INTEGRATION.md`
- `build/dashboard/tests/test_rc_acceptance.py` — V1–V13 wired across modules

## 2. Per-criterion verdict

| # | Criterion | Verdict | Proof (command → exit 0 unless noted) |
|---|---|---|---|
| V1 | §1 panel order (exec summary → sparklines → B → A → themes → C → raw stats); Zone B rows carry A/B/C left borders per `_classify_panel` | **PASS** | `pytest dashboard/tests/test_rc_acceptance.py::test_v1_panel_order_and_zone_b_borders -q` |
| V2 | Per-zone source-state chip (live/stale/unavailable + read ts), degraded banner, stale zone → buttons disabled with stale tooltip | **PASS** | `...::test_v2_stale_zone_chip_banner_disabled_buttons` |
| V3 | Static and hub render byte-identical except enabled/disabled buttons, fallback notice, footer | **PASS** | `...::test_v3_static_and_hub_render_differ_only_in_three_ways` (normalized diff: script stripped, `disabled` removed, notice/footer lines dropped → identical) |
| V4 | Exactly 3 actions/row; Approve expands §4 panel (prefilled scope/expiry); Confirm is the only write; Cancel commits nothing | **PASS** | `...::test_v4_three_actions_confirm_only_write_cancel_commits_nothing` (write-spy: zero `_create_transaction`/`_execute` calls on confirm + cancel) |
| V5 | Approve success → `executed ✓ (attempts N)`, row transitions out; 401/403 → two-step inline error, `not committed — nothing changed`, row stays waiting-on-you, buttons re-enabled | **PASS** | `...::test_v5_approve_success_executed_attempts_row_leaves`, `...::test_v5_write_token_401_two_step_error_then_recover` (WriteToken fail_times fixture, recovery approve executes) |
| V6 | Execution failure → `intent NOT re-run — approve again for a fresh decision`, no auto-retry (NoFreshDecision) | **PASS** | `...::test_v6_execution_failure_no_auto_retry` (bad executor + drained bounded retries) |
| V7 | Blank dismiss posts `'unspecified'`; escalate shows curator → lead → Ray → JEV and collapses the row | **PASS** | `...::test_v7_blank_dismiss_unspecified_and_escalate_route` |
| V8 | Evidence badges verified solid / inferred outline / stale striped+tooltip; age goes amber at the class SLA from `queue-policy.yaml` (4h/24h/72h) | **PASS** | `...::test_v8_evidence_badges_and_sla_amber_from_policy` (fixtures: blocking 5h, degraded 30h, routine 80h → all amber; blocking 2h → gray) |
| V9 | Sparkline strip: 3 charts from 7 daily values each, values match real aggregates, current value + delta arrow, no pointer handlers | **PASS** | `...::test_v9_sparklines_three_charts_seven_values_no_interactivity` (7-day real fixture store: a5-runtime.db job_runs + model-calls.jsonl + scrapes.db via `collect_series`; `days:` tag absent = all 7 present; no onclick/onmouseover/tooltip) |
| V10 | Kept panels render unchanged content: exec summary, themes+trend arrows, stack overlap, Zone A columns, raw stats + untagged bucket | **PASS** | `...::test_v10_kept_panels_render_content` (board from real `render_board()` pipeline) |
| V11 | `/dashboard` 200 with valid bearer, credentials page on 401, no redirect ever | **PASS** | `...::test_v11_bearer_200_401_page_no_redirect` (+ lane-4 suite `test_rc_serve.py` incl. real-socket wsgiref e2e) |
| V12 | No write token → full board read-only with §7 banner; token appears → `decisions enabled ✓` once, never again | **PASS** | `...::test_v12_readonly_banner_flip_via_installer` (serve read-only render + `installer.first_look_banner` flip matrix) |
| V13 | No JS chart lib, no websocket, no polling; only JS is the POST helper on the hub render | **PASS** | `...::test_v13_no_chart_lib_no_websocket_no_polling` (no canvas/chart.js/d3/websocket/setInterval, no `src=`; exactly 1 `<script>` interactive, 0 static) |
| E2E | Full queue loop on real data over a real socket | **PASS** | `...::test_e2e_queue_loop_real_data_over_real_socket` — transcript in §3 |

## 3. End-to-end queue loop — real output (real WSGI app, real socket, real backend)

`GET /dashboard` (Bearer read token) → 200, row `q-1` on the page with its three action buttons:

```
127.0.0.1 - - "GET /dashboard HTTP/1.1" 200 3658
queue before: ['q-1']
board 200, row on page: True
```

POST approve (confirm phase, Bearer write token) → panel data, **zero writes**:

```
127.0.0.1 - - "POST /dashboard/queue/approve HTTP/1.1" 200 367
confirm -> {"status": "error", "message": "confirm-panel data (no write performed)", "phase": "confirm"}
          | prefill scope: act-1  expiry: <policy expiry>
mandates rows after confirm: 0
```

POST approve (commit) → the single write; ledger written; row leaves the queue:

```
127.0.0.1 - - "POST /dashboard/queue/approve HTTP/1.1" 200 137
commit -> {"status": "executed", "message": "executed ✓ (attempts 1)", "attempts": 1, "phase": "commit"}
mandates ledger: [('m-q-1-1', 'q-1', 'act-1', 'consumed')]
action_intents: [('q-1', 'executed', 1)]
audit_records: [('approve-create', 'q-1', 'Ray')]
row state: done | waiting rows: []
```

## 4. Integration reconciliations (deviations from pinned lane interfaces)

1. **§7 banner injection added to `serve._render`** — lane 4's tests stubbed the template to carry `READONLY_BANNER_TEXT`; the real template (lane 1) never emits it. The read-only render now injects the banner div after `<body>`; the flip to `decisions enabled ✓` remains lane 5's `first_look_banner`. Recorded as an integration fix, not a lane deviation.
2. **POST helper URL fixed in `template.py`** — lane 1's helper POSTed to `/dashboard/queue/<itemId>/<action>`; lane 4's server routes `/dashboard/queue/<action>` with `item_id` in the JSON body. Helper rewritten to the server's contract (`{item_id, payload}` body). Cross-lane bug found by integration; would have broken every queue action from the browser.
3. **Lane 4 tests de-stubbed** — `test_rc_serve.py` stubbed `template`/`actions` (siblings not yet merged). In the merged tree the stubs were removed and the affected tests now exercise the real modules (incl. a real Queue/MandateFlow backend with `check_same_thread=False` for the server thread).
4. **Module-copy hygiene** — the merged tree is importable both as `dashboard.console.*` and `build.dashboard.console.*`. Queue actions and the acceptance/e2e suites consistently use the top-level `exception_queue` copy (the same module object `console.actions` imports) so `WriteAuthError`/`NoFreshDecision` except-classes match the live backend.

## 5. Known gaps (scoped out of this verdict)

- **Email ping channel:** blocked on Ray's email provider — the verdict covers **hub-mention pings only**.
- **Live-feed gap:** the board shows bridge/dogfood-derived items until the listening pipeline lands (per parent spec).
- **v0.9-rc1 non-features (per spec):** no notification center, no history/drill-down charts, no multi-tenant switching, no in-page login, no websocket/polling (static-fallback parity is a feature, tested by V13).
- Onboarding installer is CLI-first (`python -m console.installer`); the in-page first-look banner is served by serve.py and dismissed via config, not a JS tour (rejected alternative per spec).

## 6. Cut rule check

- ✅ Queue actions work end-to-end against real data (§3: confirm → commit → mandate ledger + audit + intent executed, row `done`, queue drained).
- ✅ Pilot onboarding doc exists: `build/dashboard/docs/pilot-onboarding.md` (160 lines, §1–§7).
- ✅ All tests green: `pytest build/dashboard/tests/ -q` → 89 passed, exit 0.

**Recommendation:** v0.9-rc1 console meets V1–V13 as scoped; ready for lead RC verdict.
