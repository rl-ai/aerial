# Ingest bridge (gap A1)

Daily job that moves the day's top matched items from the scrape store
(`scrapes.db`) into the second-brain gateway's curated layer: rank →
markdown notes → `Curator.ingest_folder` → cluster artifacts → digest.

**Status: stopgap.** This is superseded by the listening pipeline, which
subscribes to sources directly and feeds the gateway continuously. This
bridge exists so the curated layer has fresh material until that
pipeline is live; delete it once it is.

## Usage

```
python3 bridge.py --scrapes-db <path> --gateway-root <path> \
    --since-hours 24 --top 40 [--dry-run]
```

The gateway root must contain the `secondbrain` package (the bridge
inserts `<gateway-root>` into `sys.path`, same as bridge users).

Flow:
1. Read items fetched within the window from `items` (read-only SQLite).
2. Rank by match-term groups (port of the scrape-source semantics: an
   item matches if all terms of any one group appear in title+body;
   score = groups hit, tie-break freshest). Zero-hit items are not
   selected.
3. Take top N, skipping guids already in the state file
   (`<gateway-root>/bridge/state.json`, guid → vault hash).
4. Write markdown notes to `<gateway-root>/bridge/staging/` with
   `@entity:<source_id-slug>` / `@entity:<topic-slug>` markers and
   `sensitivity: personal` — the conservative default. **The bridge
   never labels anything public.**
5. `Curator.ingest_folder` (per-file G6 isolation: a corrupt item is
   counted, not fatal), `build_cluster_artifacts`, `publish("main")`.
6. `build_digest` (since now) plus an `ingested` section with the new
   items, written to `bridge/digest-latest.json`.

Prints a JSON summary:
`{fetched, selected, ingested, skipped, errors, digest_path}`.
`--dry-run` computes selection and reports counts but writes nothing.

## Cron (host, job_run.py pattern like the scrape-pipeline)

```
15 5 * * *  cd /srv/aerial/gateway && \
  python3 bridge.py --scrapes-db ~/.aerial/data/scrapes.db \
    --gateway-root /srv/aerial/gateway --since-hours 24 --top 40 \
    >> ~/.aerial/logs/ingest-bridge.log 2>&1
```

Adjust the scrapes.db / gateway-root paths to the live deployment.

## Config knobs

| Knob | Default | Meaning |
|---|---|---|
| `--since-hours` | 24 | Window over `items.fetched_at` (epoch or ISO) |
| `--top` | 40 | Max items ingested per run |
| `MATCH_GROUPS` (in `bridge.py`) | see source | Match-term groups used for scoring |
| `sensitivity` | `personal` (hardcoded) | Never auto-public |

## Tests

```
python3 -m pytest test_bridge.py -q
```

Hermetic: temp scrapes.db fixture and temp gateway root; covers window
and top-N selection, entity markers + personal sensitivity, idempotent
re-run, corrupt-item isolation (G6), digest content, and dry-run.
