"""Listening pipeline — degree-expansion source discovery (P1b).

Spec: lanes-research/listening-pipeline.md §3–5 (degree-expansion / source
proposals), built on the gateway's existing primitives:

- ``secondbrain.listening.load_sources``  — curated sources (new_sources.json)
- ``secondbrain.proposals.fetch_scrape_items`` — ingested scrape items
  (scrapes.db opened READ-ONLY; this module never writes it)
- ``secondbrain.proposals.extract_entities`` — raw-text entity extraction

Discovery (shadow mode, spec §5 step 3):
  (a) mention candidates — entities mentioned repeatedly in ingested items
      that are NOT already followed (counted per (kind, target) and per
      domain);
  (b) co-citation candidates — entities co-mentioned by ≥2 DISTINCT followed
      sources (the "co-citation links between followed sources" edges).
  Candidates are scored  mention_frequency × recency × distinct-source
  count, ranked, and the top-N become source PROPOSALS written to the
  exception queue (dashboard/exception_queue/queue.py) as ``routine``
  findings with ``source_agent="listening-discovery"``.

Bound lane decisions honored here (do not re-decide):
- SHADOW MODE: proposals are queue rows only. A human approves; nothing is
  EVER appended to new_sources.json by this module (the approve path lives
  in the operator flow, via secondbrain.listening.append_source).
- Idempotency: a re-run never duplicates a queue row for the same
  ``(kind, target)`` — dedup is keyed on
  ``condition_key = "source-proposal:<kind>:<target>"`` and checked against
  the queue store plus a persisted proposal ledger.
- Reddit co-subscription rows are dropped (no data source) — only entities
  extractable from raw item text count.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

# --- gateway bootstrap (the gateway lives outside ~/ai-ops) ------------------
_GATEWAY_DEFAULT = os.path.expanduser(
    "~/workspaces/build/second-brain-gateway/v1")


def _gateway_path() -> str:
    return os.environ.get("SECOND_BRAIN_GATEWAY", _GATEWAY_DEFAULT)


def _import_gateway():
    gp = _gateway_path()
    if gp not in sys.path:
        sys.path.insert(0, gp)
    from secondbrain import proposals as sb_proposals  # noqa: E402
    from secondbrain import listening as sb_listening  # noqa: E402
    return sb_listening, sb_proposals


# --- constants ----------------------------------------------------------------

#: Weekly batch: candidates below this score are never proposed.
PROPOSAL_SCORE_THRESHOLD = 0.55   # [calibration hypothesis — unverified]
#: Top-N candidates per weekly batch that become queue proposals.
TOP_N_PER_BATCH = 5
#: Recency half-life (days): an item's mention weight halves every 14 days.
RECENCY_HALF_LIFE_DAYS = 14.0
#: Distinct-source saturation: ≥3 distinct sources mention => full weight.
DISTINCT_SOURCES_FULL = 3
#: Co-citation floor: an entity must be mentioned by ≥2 distinct followed
#: sources to qualify via the co-citation edge (spec §3.1).
MIN_COCITATION_SOURCES = 2
#: Default window for the weekly batch.
DEFAULT_SINCE_DAYS = 30
#: Proposal ledger key prefix in the exception queue.
CONDITION_KEY_PREFIX = "source-proposal"


# --- entity / candidate extraction --------------------------------------------

_DOMAIN_RE = re.compile(r"\b([a-z0-9-]+\.[a-z0-9.-]{2,})\b", re.IGNORECASE)
#: Domains that appear in nearly every post and are never a "source".
_DOMAIN_STOP = {"github.com", "reddit.com", "old.reddit.com", "np.reddit.com",
                "twitter.com", "x.com", "youtube.com", "youtu.be", "arxiv.org",
                "doi.org", "google.com", "medium.com", "substack.com",
                "huggingface.co", "wikipedia.org", "t.co",
                # reddit media/CDN hosts, raw hosts, loopback
                "preview.redd.it", "i.redd.it", "v.redd.it",
                "raw.githubusercontent.com", "gist.githubusercontent.com",
                "127.0.0.1", "localhost", "0.0.0.0"}
#: File-extension "domains" that are really repo filenames.
_FILE_EXT_TLD = {"md", "json", "sh", "lock", "yaml", "yml", "toml", "txt",
                 "cfg", "ini", "xml", "csv"}
_IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def extract_domains(text: str) -> set[str]:
    """Bare domains in raw text (the ``domain`` candidate axis). Skips the
    stoplist (after ``www.`` stripping), IPs/loopback, and repo-filename
    pseudo-domains like ``readme.md`` or ``settings.json``."""
    out = set()
    for m in _DOMAIN_RE.finditer(text or ""):
        host = m.group(1).lower().rstrip(".").removeprefix("www.")
        if host in _DOMAIN_STOP or _IP_RE.fullmatch(host):
            continue
        if host.rsplit(".", 1)[-1] in _FILE_EXT_TLD:
            continue
        if host.endswith((".png", ".jpg", ".gif")):
            continue
        out.add(host)
    return out


def _source_keys(source: dict) -> set[tuple[str, str]]:
    """Every identity key a followed source occupies, for dedup:
    its (kind, raw-target-lowercased) and (kind, slugified-target), plus
    its target's bare domain."""
    sb_listening, _ = _import_gateway()
    target = (source.get("target") or "").lower()
    keys = {(source["kind"], target),
            (source["kind"], sb_listening.slugify(source["target"]))}
    try:
        host = urlsplit(source["target"] if "//" in source["target"]
                        else f"https://{source['target']}").netloc.lower()
        if host:
            keys.add(("domain", host))
            keys.add(("domain", host.removeprefix("www.")))
    except ValueError:
        pass
    return keys


# --- mention counting + scoring ------------------------------------------------

def count_mentions(sources: list[dict], items: list[dict], *,
                   now: float | None = None) -> list[dict]:
    """Count candidate mentions over the ingested corpus.

    Returns raw candidate records (unsorted):
      {kind, target, mentions, distinct_sources, seeds, item_refs, recency}

    - kind ``github_repo`` / ``reddit_sub`` via the gateway's
      ``extract_entities`` (raw text only — never ``@entity:`` markers);
    - kind ``domain`` via bare-domain extraction;
    - each item contributes AT MOST ONE mention per candidate (per-item dedup);
    - mentions from a followed source's OWN identity are dropped;
    - recency = mean over items of exp(-age / half_life) (half-life 14d).
    """
    now = time.time() if now is None else now
    _, sb_proposals = _import_gateway()
    followed_keys: set[tuple[str, str]] = set()
    for s in sources:
        followed_keys |= _source_keys(s)

    candidates: dict[tuple[str, str], dict] = {}
    for idx, item in enumerate(items):
        text = f"{item.get('title') or ''}\n{item.get('body') or ''}"
        ts = item.get("_ts")
        ents: set[tuple[str, str]] = set()
        for kind, target in sb_proposals.extract_entities(text):
            ents.add((kind, target))
        for dom in extract_domains(text):
            ents.add(("domain", dom))
        source_id = item.get("source_id")
        for ent in ents:
            if ent in followed_keys:
                continue
            rec = candidates.setdefault(ent, {
                "kind": ent[0], "target": ent[1],
                "mentions": 0, "distinct_sources": 0, "seeds": [],
                "item_refs": [], "_rec_sum": 0.0})
            # each item is processed exactly once, so one mention per item
            # per candidate holds by construction (no dedup set needed)
            ref = item.get("guid") or item.get("url") or f"item-{idx}"
            rec["mentions"] += 1
            rec["item_refs"].append({
                "source_id": source_id,
                "ref": ref,
                "url": item.get("url"),
                "ts": ts,
            })
            if source_id not in rec["seeds"]:
                rec["seeds"].append(source_id)
            age_days = max(0.0, (now - (ts or now)) / 86400.0)
            rec["_rec_sum"] += math.exp(-age_days * math.log(2.0)
                                        / RECENCY_HALF_LIFE_DAYS)
    out = []
    for (kind, target), rec in candidates.items():
        n = rec["mentions"]
        rec.pop("_refs_seen", None)
        rec["recency"] = round(rec.pop("_rec_sum") / n, 6) if n else 0.0
        rec["distinct_sources"] = len(rec["seeds"])
        out.append(rec)
    return out


def score_candidate(rec: dict) -> float:
    """``score = freq × recency × distinct`` where

    - freq    = min(mentions / 10, 1.0)        (10+ mentions saturate)
    - recency = the record's mean exp decay    (1.0 = all fresh)
    - distinct = min(distinct_sources / 3, 1.0) (3+ sources saturate)

    Deterministic, pure — tested directly against the math.
    """
    freq = min(rec["mentions"] / 10.0, 1.0)
    distinct = min(rec["distinct_sources"] / float(DISTINCT_SOURCES_FULL), 1.0)
    return round(freq * rec["recency"] * distinct, 6)


def rank_candidates(sources: list[dict], items: list[dict], *,
                    now: float | None = None) -> list[dict]:
    """Count → score → rank. Co-citation candidates (mentioned by ≥2 distinct
    followed sources) are flagged ``cocitation: true`` and get a 1.5× boost
    capped at 1.0 (a link between followed sources is the strongest evidence
    in the corpus). Output: ranked list with evidence attached."""
    recs = count_mentions(sources, items, now=now)
    for rec in recs:
        rec["cocitation"] = rec["distinct_sources"] >= MIN_COCITATION_SOURCES
        score = score_candidate(rec)
        if rec["cocitation"]:
            score = min(1.0, round(score * 1.5, 6))
        rec["score"] = score
    recs.sort(key=lambda r: (-r["score"], -r["mentions"], r["target"]))
    return recs


def dedup_against_followed(candidates: list[dict],
                           sources: list[dict]) -> list[dict]:
    """Drop candidates that ARE a followed source (any identity key:
    kind+slug target, or its domain)."""
    followed: set[tuple[str, str]] = set()
    for s in sources:
        followed |= _source_keys(s)
    return [c for c in candidates
            if (c["kind"], c["target"]) not in followed]


# --- exception-queue proposals (shadow mode) -----------------------------------

def condition_key(kind: str, target: str) -> str:
    return f"{CONDITION_KEY_PREFIX}:{kind}:{target}"


def _queue_has_key(queue, key: str) -> bool:
    try:
        row = queue.conn.execute(
            "SELECT 1 FROM queue_items WHERE condition_key = ? LIMIT 1",
            (key,)).fetchone()
    except Exception:
        return False
    return row is not None


def load_proposal_ledger(path) -> dict:
    p = Path(path)
    if not p.exists():
        return {"proposed": {}}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"proposed": {}}


def save_proposal_ledger(path, ledger: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(ledger, indent=1), encoding="utf-8")
    tmp.replace(p)


def propose_top_n(ranked: list[dict], queue, ledger: dict, *,
                  ledger_path=None,
                  top_n: int = TOP_N_PER_BATCH,
                  threshold: float = PROPOSAL_SCORE_THRESHOLD,
                  now: float | None = None) -> list[dict]:
    """Push the top-N above-threshold ranked candidates into the exception
    queue as ``routine`` findings (shadow mode). Idempotent: a candidate
    already in the queue OR the proposal ledger is skipped. NEVER touches
    new_sources.json. Returns the rows actually proposed this run."""
    from dashboard.exception_queue.queue import Finding  # repo convention
    now = time.time() if now is None else now
    proposed = []
    for rec in ranked:
        if len(proposed) >= top_n:
            break
        if rec["score"] < threshold:
            break                       # ranked desc — everything below is out
        key = condition_key(rec["kind"], rec["target"])
        if key in ledger["proposed"] or _queue_has_key(queue, key):
            continue                    # idempotency (queue + ledger)
        item = queue.admit(Finding(
            condition_key=key,
            claim=(f"Source proposal ({rec['kind']}): {rec['target']} — "
                   f"{rec['mentions']} mentions across "
                   f"{rec['distinct_sources']} ingested sources"
                   + (" (co-citation)" if rec.get("cocitation") else "")),
            impact_class="routine",
            admission_score=rec["score"],
            eval_passed=True,
            evidence_label="inferred",
            source_agent="listening-discovery",
            proposed_action=("Review source proposal (shadow mode). Add to "
                             "new_sources.json ONLY on human approval.")))
        if item is None:
            continue
        ledger["proposed"][key] = {
            "kind": rec["kind"], "target": rec["target"],
            "score": rec["score"], "mentions": rec["mentions"],
            "distinct_sources": rec["distinct_sources"],
            "cocitation": bool(rec.get("cocitation")),
            "item_refs": rec["item_refs"][:5],
            "proposed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                         time.gmtime(now)),
        }
        proposed.append({**rec, "condition_key": key})
    if ledger_path is not None:
        save_proposal_ledger(ledger_path, ledger)
    return proposed


# --- weekly batch + markdown render ---------------------------------------------

def run_discovery(sources_path, db_path, *, queue, ledger_path,
                  since_days: int = DEFAULT_SINCE_DAYS,
                  top_n: int = TOP_N_PER_BATCH, now: float | None = None
                  ) -> dict:
    """One weekly batch: load sources → fetch items (read-only) → rank →
    dedup → propose top-N into the exception queue (``queue`` is a
    dashboard.exception_queue.queue.Queue). Returns the batch summary
    consumed by the daily-board markdown render."""
    now = time.time() if now is None else now
    sb_listening, sb_proposals = _import_gateway()
    sources = sb_listening.load_sources(sources_path)
    items = sb_proposals.fetch_scrape_items(db_path, since_days=since_days)
    # Queue._next_id starts at 1 and does not resume from a persisted store;
    # re-key past any existing rows so weekly re-runs never collide.
    try:
        existing = queue.conn.execute(
            "SELECT item_id FROM queue_items").fetchall()
        maxn = max((int(r[0].rsplit("-", 1)[-1]) for r in existing
                    if r[0].startswith("q-") and
                    r[0].rsplit("-", 1)[-1].isdigit()), default=0)
        queue._next_id = maxn + 1
    except Exception:
        pass
    ranked = dedup_against_followed(rank_candidates(sources, items, now=now),
                                    sources)
    ledger = load_proposal_ledger(ledger_path)
    proposed = propose_top_n(ranked, queue, ledger, ledger_path=ledger_path,
                             top_n=top_n, now=now)
    save_proposal_ledger(ledger_path, ledger)
    return {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "shadow": True,
        "sources_followed": len(sources),
        "items_scanned": len(items),
        "candidates_found": len(ranked),
        "proposed": proposed,
        "waits_for_you": False,          # shadow mode: never pings
    }


def render_markdown(batch: dict, *, limit: int = 10) -> str:
    """Daily-board markdown section for the discovery batch (Zone B annex:
    shadow proposals render collapsed, below blocking classes, and never
    count toward waits-for-you)."""
    lines = ["## Source discovery (degree expansion) — shadow", ""]
    lines.append(f"built {batch.get('built_at')} · "
                 f"{batch.get('items_scanned')} items · "
                 f"{batch.get('candidates_found')} candidates · "
                 f"{batch.get('sources_followed')} followed sources · "
                 "**shadow: nothing is scraped until you approve**")
    lines.append("")
    proposed = batch.get("proposed") or []
    if not proposed:
        lines.append("_No new proposals this cycle._")
        return "\n".join(lines) + "\n"
    lines.append("| # | candidate | kind | score | mentions | sources | evidence |")
    lines.append("|---|-----------|------|-------|----------|---------|----------|")
    for i, rec in enumerate(proposed[:limit], 1):
        evidence = ", ".join(
            str(r.get("ref") or r.get("url") or "?")
            for r in rec.get("item_refs", [])[:3])
        lines.append(
            f"| {i} | `{rec['target']}` | {rec['kind']} | {rec['score']:.3f} "
            f"| {rec['mentions']} | {rec['distinct_sources']} | {evidence} |")
    lines.append("")
    lines.append("Approve/dismiss via the exception queue "
                 f"(`{CONDITION_KEY_PREFIX}:*` rows); approved sources are "
                 "added to new_sources.json by hand, never automatically.")
    return "\n".join(lines) + "\n"
