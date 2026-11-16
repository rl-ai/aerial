"""Listening pipeline lane 3: degree expansion + proposal rows (shadow mode).

Spec §3–4 + §5 step 3: a weekly batch job discovers 1st/2nd-degree source
candidates from the scrape corpus, scores them with the exception queue's
``0.5·A + 0.3·U + 0.2·G`` shape, and writes **proposal rows** — Routine class —
into a persisted proposal ledger. **Shadow mode**: proposals are computed and
logged, never surfaced to Ray (observation-only for 2 weeks).

Decisions bound by the lane brief (do not re-decide):

- Third decision = **Snooze** (not escalate). Snooze holds for 14 days PAST
  the routine expiry (``expiry = now + 14d + hold``), persists
  ``snooze_count``, and re-proposes at most 2 times, then auto-dismisses
  (finding 1).
- Scoring (findings 2, 6, 7, 17): A-term density is normalized against a
  **fixed absolute scale** (log items/wk, capped at 40/wk) — never against
  the seed's own rate; ``seed_rate == 0`` is guarded; a candidate needs a
  minimum sample of **≥5 matched items** before it can score; overlap
  requires **≥2 independent term-groups**. ``G = min(seeds/3, 1.0)``.
  **U cold-start = 0.5** and no learning effect until **≥5 dismissals** are
  recorded. U and A-weight learning are NEW code paths (the spec's claim
  that no new scoring code path is needed is false — kept out of docs).
- Queue load (finding 8): outstanding proposals capped at 5, rendered in a
  separate section below blocking classes, excluded from ``waits_for_you``.
- Candidate ledger (finding 5): persisted keyed by ``(kind, target)``;
  suppressed for 8 weeks after a dismissal, permanently after 3 dismissals.
- Discovery (findings 4, 16): entities are extracted independently from RAW
  source text (never from adapter ``@entity:`` markers) and require
  co-mention in **≥3 distinct items from ≥2 distinct seeds**. Reddit
  "co-subscription" rows are DROPPED (no data source).
- Burst detection (finding 11): the absolute items/day alarm is NEVER
  suppressed; the relative alarm compares against the trailing 7-day median
  with a floor of 3/day.
- Default thresholds 0.55 / 0.4 stand as calibration hypotheses (spec's
  unverified register).
"""

from __future__ import annotations

import json
import re
import sqlite3
import statistics
import time
from pathlib import Path

from .listening import item_matches, load_sources, now_iso, parse_iso, slugify, source_matches

# --- constants bound by the lane brief ---------------------------------------

PROPOSAL_SCORE_THRESHOLD = 0.55   # [calibration hypothesis — unverified]
PROPOSAL_A_THRESHOLD = 0.4        # [calibration hypothesis — unverified]
TOP_N_PER_BATCH = 5
MAX_OUTSTANDING_PROPOSALS = 5     # finding 8
MIN_SAMPLE_ITEMS = 5              # finding 17: sample-size floor
MIN_TERM_GROUPS = 2               # overlap requires >=2 independent term-groups
MIN_CO_MENTION_ITEMS = 3          # finding 16: >=3 distinct items
MIN_CO_MENTION_SEEDS = 2          # finding 16: >=2 distinct seeds
DENSITY_CAP_PER_WK = 40           # finding 2: fixed absolute scale cap
U_COLD_START = 0.5
LEARNING_MIN_DISMISSALS = 5       # no U learning before this many dismissals
DUPLICATE_U_PENALTY = 0.25        # per similar dismissed duplicate
G_FULL_SEEDS = 3                  # G = min(seeds / 3, 1.0)

SUPPRESS_AFTER_DISMISS_DAYS = 56  # finding 5: 8 weeks
PERMANENT_AFTER_DISMISSALS = 3    # finding 5

ROUTINE_SLA_HOURS = 72            # spec §3.3 (Routine class)
ROUTINE_EXPIRY_DAYS = 14
SNOOZE_HOLD_DAYS = 14
SNOOZE_MAX_REPROPOSES = 2         # finding 1: re-propose at most 2 times

BURST_ABS_PER_DAY = 50            # absolute alarm — never suppressed (finding 11)
BURST_REL_FACTOR = 5              # spec §4: "a sub suddenly 5x noisier"
BURST_MEDIAN_FLOOR = 3            # relative-alarm median floor, items/day

ROUTINE_EXPIRY_S = ROUTINE_EXPIRY_DAYS * 86400
SNOOZE_HOLD_S = SNOOZE_HOLD_DAYS * 86400
SUPPRESS_S = SUPPRESS_AFTER_DISMISS_DAYS * 86400

_DAY = 86400
_WEEK = 7 * _DAY


# --- raw-text entity extraction (findings 4, 16) ------------------------------

_URL_RE = re.compile(r"https?://\S+")
_GH_URL_RE = re.compile(
    r"github\.com/([A-Za-z0-9_.-]{2,40})/([A-Za-z0-9_.-]{2,60})")
_R_MENTION_RE = re.compile(r"(?:^|[\s(\[])[rR]/([A-Za-z0-9_]{2,30})\b")

#: github.com SITE paths that are never repo owners (issues/features/...)
_GH_SITE_STOP = {"en", "features", "settings", "topics", "trending", "orgs",
                 "search", "issues", "pulls", "marketplace", "apps",
                 "collections", "events", "explore", "notifications", "login",
                 "join", "site", "github", "about", "pricing", "security",
                 "contact", "docs", "profile"}


def extract_entities(text: str) -> set[tuple[str, str]]:
    """Entities from RAW source text — never from ``@entity:`` markers
    (finding 4: discovery must not read the pipeline's own extraction
    output). GitHub repos are taken ONLY from github.com URLs (a loose
    prose ``x/y`` regex matches common word pairs — ai/ml, copy/paste —
    and floods discovery with noise); ``r/`` sub mentions come from the
    remaining text. Returns ``(kind, target)`` pairs, targets slugified."""
    ents: set[tuple[str, str]] = set()
    raw = text or ""
    for m in _GH_URL_RE.finditer(raw):
        owner, repo = m.group(1).lower(), m.group(2).lower()
        if owner in _GH_SITE_STOP:
            continue
        ents.add(("github_repo", f"{owner}/{repo}"))
    bare = _URL_RE.sub(" ", raw)
    for m in _R_MENTION_RE.finditer(bare):
        ents.add(("reddit_sub", slugify(m.group(1))))
    return ents


# --- scrape item loading --------------------------------------------------------

def fetch_scrape_items(db_path: str, *, since_days: float = 30,
                       source_ids: list[str] | None = None,
                       per_source_limit: int = 1500) -> list[dict]:
    """Matched-candidate corpus: raw scrape items from scrapes.db (read-only),
    newest-first per source. ``source_ids`` restricts and caps per source —
    a global LIMIT would be swallowed by firehose subs (wallstreetbets is
    2.7M rows). Items keep RAW title/body — discovery extracts entities
    itself (finding 4)."""
    cutoff = time.time() - since_days * _DAY
    cutoff_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(cutoff))
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if source_ids:
            rows = []
            for sid in source_ids:
                rows.extend(conn.execute(
                    "SELECT source_id, title, body, url, published, fetched_at "
                    "FROM items WHERE source_id = ? AND fetched_at >= ? "
                    "ORDER BY fetched_at DESC LIMIT ?",
                    (sid, cutoff_iso, per_source_limit)).fetchall())
        else:
            rows = conn.execute(
                "SELECT source_id, title, body, url, published, fetched_at "
                "FROM items WHERE fetched_at >= ? LIMIT 200000",
                (cutoff_iso,)).fetchall()
    finally:
        conn.close()
    items = []
    for row in rows:
        item = dict(row)
        ts = parse_iso(item.get("fetched_at")) or parse_iso(item.get("published"))
        if ts is None or ts < cutoff:
            continue
        item["_ts"] = ts
        items.append(item)
    return items


# --- discovery -------------------------------------------------------------------

def discover_candidates(sources: list[dict], items: list[dict], *,
                        approved_proposal_keys: set[str] | None = None
                        ) -> list[dict]:
    """Spec §3.1: co-mention discovery over matched items, per seed source.

    A candidate entity qualifies when it is co-mentioned with a seed's own
    stream in **≥3 distinct items from ≥2 distinct seeds** (finding 16).
    Entities come from RAW item text only (finding 4). A candidate's degree
    is 2 only when EVERY seed that generated it is an approved 1st-degree
    proposal (spec §3.1: never expand from dismissed candidates)."""
    approved = approved_proposal_keys or set()
    seeds = [s for s in sources if s.get("enabled", True)]
    # matched items per seed
    per_seed: dict[str, list[dict]] = {}
    for source in seeds:
        sid = source["id"]
        matched = []
        for item in items:
            if item.get("source_id") != sid:
                continue
            text = f"{item.get('title') or ''}\n{item.get('body') or ''}"
            # reddit_sub corpus rows are ALREADY match-filtered by the
            # production scraper — trust them; listening kinds re-filter.
            if source["kind"] != "reddit_sub" and not source_matches(source, text):
                continue
            item = dict(item, _text=text)
            matched.append(item)
        per_seed[sid] = matched
    # co-mention index: (kind, target) -> {item_key: item}, seeds
    mentions: dict[tuple[str, str], dict] = {}
    for source in seeds:
        sid = source["id"]
        own = slugify(source["target"])
        own_key = (source["kind"], own)
        for item in per_seed[sid]:
            key = f"{sid}:{item.get('guid') or item.get('url') or item.get('_ts')}"
            for ent in extract_entities(item["_text"]):
                if ent == own_key or ent[1] == own:
                    continue
                rec = mentions.setdefault(ent, {"items": {}, "seeds": set()})
                rec["items"].setdefault(key, item)
                rec["seeds"].add(sid)
    candidates = []
    seed_added_via = {s["id"]: s.get("added_via", "seed") for s in seeds}
    for (kind, target), rec in sorted(mentions.items()):
        if len(rec["items"]) < MIN_CO_MENTION_ITEMS or len(rec["seeds"]) < MIN_CO_MENTION_SEEDS:
            continue
        seed_ids = sorted(rec["seeds"])
        # degree: 2nd degree ONLY from approved 1st-degree sources
        all_approved = all(
            seed_added_via.get(sid, "seed").startswith("proposal:")
            or f"{kind}:{target}" in approved
            for sid in seed_ids)
        corpus_items = sorted(rec["items"].values(), key=lambda i: -i["_ts"])
        candidates.append({
            "kind": kind,
            "target": target,
            "degree": 2 if all_approved else 1,
            "seed_ids": seed_ids,
            "items": corpus_items,
            "distinct_items": len(rec["items"]),
        })
    return candidates


# --- scoring (findings 2, 6, 7, 17) ------------------------------------------------

def density_score(rate_per_week: float) -> float:
    """Fixed absolute scale (finding 2): log items/wk, capped at 40/wk.
    Independent of the seed's own rate; a 40/wk candidate scores 1.0."""
    if rate_per_week <= 0:
        return 0.0
    return min(1.0, _log1p(rate_per_week) / _log1p(DENSITY_CAP_PER_WK))


def _log1p(x: float) -> float:
    import math
    return math.log1p(max(x, 0.0))


def _term_groups_hit(match: list[list[str]], corpus: str, kind: str = "github_repo") -> int:
    """Number of the source's match term-groups present in the corpus —
    any-term-per-group for reddit_sub (the production scraper's semantics),
    all-terms-per-group for the listening kinds."""
    hay = (corpus or "").lower()
    hit = 0
    for g in match:
        if not g:
            hit += 1
            continue
        if kind == "reddit_sub":
            if any(t.lower() in hay for t in g):
                hit += 1
        else:
            if all(t.lower() in hay for t in g):
                hit += 1
    return hit


def _target_similarity(a: str, b: str) -> float:
    """Crude target-name similarity for the U-term redundancy model."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.9
    ta, tb = set(a.split("-")), set(b.split("-"))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(len(ta | tb), 1)


def score_candidate(candidate: dict, sources: list[dict], ledger: dict, *,
                    now: float | None = None) -> dict | None:
    """Score one candidate: ``0.5·A + 0.3·U + 0.2·G``. Returns None when the
    candidate cannot score (sample-size floor, term-group floor, or all
    seed rates zero — findings 2, 7, 17)."""
    now = time.time() if now is None else now
    if candidate["distinct_items"] < MIN_SAMPLE_ITEMS:
        return None                      # finding 17: sample-size floor
    corpus = "\n".join(i["_text"] for i in candidate["items"])
    by_id = {s["id"]: s for s in sources}
    # A = overlap x density (fixed absolute scale, finding 2)
    groups_hit = 0
    groups_total = 0
    seed_rates_zero = True
    overlap_vals = []
    span = max((i["_ts"] for i in candidate["items"]), default=now) - \
        min((i["_ts"] for i in candidate["items"]), default=now)
    weeks = max(span, _WEEK) / _WEEK
    rate = candidate["distinct_items"] / max(weeks, 1e-9)
    density = density_score(rate)
    for sid in candidate["seed_ids"]:
        seed = by_id.get(sid)
        if not seed:
            continue
        # seed_rate guard (finding 2): a seed with no matched items in the
        # window contributes no overlap signal.
        seed_rate = seed.get("_rate_per_week", 1.0)
        if seed_rate <= 0:
            continue
        seed_rates_zero = False
        hit = _term_groups_hit(seed.get("match", []), corpus, seed.get("kind"))
        total = len([g for g in seed.get("match", []) if g])
        if total:
            groups_hit += hit
            groups_total += total
            overlap_vals.append(hit / total)
    if seed_rates_zero:
        return None                      # finding 2: seed_rate == 0 guard
    if groups_hit < MIN_TERM_GROUPS:
        return None                      # overlap requires >=2 term-groups
    overlap = (sum(overlap_vals) / len(overlap_vals)) if overlap_vals else 0.0
    a = overlap * density
    # U: cold-start 0.5; redundancy with already-followed sources; the
    # learning effect activates only at >=5 recorded dismissals (finding 6).
    followed_targets = [slugify(s["target"]) for s in sources
                        if s.get("enabled", True)]
    redundancy = max((_target_similarity(candidate["target"], t)
                      for t in followed_targets), default=0.0)
    u = U_COLD_START * (1.0 - redundancy)
    calibration = ledger.get("calibration", {})
    if calibration.get("dismissals", 0) >= LEARNING_MIN_DISMISSALS:
        for rec in calibration.get("records", []):
            if rec.get("reason_kind") != "duplicate":
                continue
            if _target_similarity(candidate["target"], rec.get("target", "")) >= 0.5:
                u -= DUPLICATE_U_PENALTY
    u = max(0.0, min(1.0, u))
    # G = min(independent_seed_count / 3, 1.0) — 1 seed -> 0.33
    g = min(len(set(candidate["seed_ids"])) / G_FULL_SEEDS, 1.0)
    return {"A": round(a, 4), "U": round(u, 4), "G": round(g, 4),
            "score": round(0.5 * a + 0.3 * u + 0.2 * g, 4),
            "density": round(density, 4), "overlap": round(overlap, 4),
            "rate_per_week": round(rate, 4)}


def build_evidence(candidate: dict, n: int = 3) -> list[dict]:
    """≥3 verbatim evidence items (spec §3.3) — raw text excerpts with links."""
    out = []
    for item in candidate["items"][:n]:
        text = (item.get("_text") or item.get("title") or "").strip()
        out.append({
            "source_id": item.get("source_id"),
            "title": item.get("title"),
            "url": item.get("url"),
            "excerpt": text[:280],
            "ts": now_iso(item.get("_ts")),
        })
    return out


# --- proposal ledger (finding 5) -----------------------------------------------------

def ledger_key(kind: str, target: str) -> str:
    return f"{kind}:{target}"


def load_ledger(path) -> dict:
    path = Path(path)
    if not path.exists():
        return {"candidates": {}, "calibration": {"dismissals": 0, "records": []}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"candidates": {}, "calibration": {"dismissals": 0, "records": []}}


def save_ledger(path, ledger: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(ledger, indent=1), encoding="utf-8")
    tmp.replace(path)


def suppressed(ledger: dict, kind: str, target: str, now: float) -> bool:
    """Finding 5: after a dismissal the candidate is suppressed for 8 weeks;
    permanently after 3 dismissals."""
    rec = ledger.get("candidates", {}).get(ledger_key(kind, target))
    if not rec:
        return False
    if rec.get("dismiss_count", 0) >= PERMANENT_AFTER_DISMISSALS:
        return True
    until = rec.get("dismissed_until")
    return bool(until and parse_iso(until) and parse_iso(until) > now)


def record_decision(ledger: dict, key: str, decision: str, *, reason: str | None = None,
                    now: float | None = None) -> dict:
    """Approve / Dismiss / Snooze on a ledger entry (spec §3.3 + bound decisions).

    - dismiss: optional one-line reason; counts as a calibration negative
      sample (duplicate-reasons feed the U model, relevance-reasons the A
      weights); suppression per finding 5.
    - snooze: holds 14 days PAST the routine expiry
      (``expiry = now + 14d + hold``), persists ``snooze_count``; after
      SNOOZE_MAX_REPROPOSES re-proposals the candidate auto-dismisses
      (finding 1)."""
    now = time.time() if now is None else now
    cal = ledger.setdefault("calibration", {"dismissals": 0, "records": []})
    rec = ledger.setdefault("candidates", {}).setdefault(key, {
        "kind": key.split(":", 1)[0], "target": key.split(":", 1)[1]})
    rec["updated_at"] = now_iso(now)
    if decision == "approve":
        rec["state"] = "approved"
        rec["approved_at"] = now_iso(now)
    elif decision == "dismiss":
        rec["state"] = "dismissed"
        rec["dismiss_count"] = rec.get("dismiss_count", 0) + 1
        rec["dismissed_at"] = now_iso(now)
        rec["dismissed_until"] = now_iso(now + SUPPRESS_S)
        rec["last_reason"] = reason
        if rec["dismiss_count"] >= PERMANENT_AFTER_DISMISSALS:
            rec["dismissed_until"] = None       # permanent suppression
        reason_kind = "duplicate" if reason and "duplicate" in reason.lower() \
            else ("relevance" if reason and any(
                w in reason.lower() for w in ("relevant", "noise", "off-topic")) else "other")
        cal["dismissals"] = cal.get("dismissals", 0) + 1
        cal["records"].append({"key": key, "target": rec.get("target"),
                               "reason": reason, "reason_kind": reason_kind,
                               "ts": now_iso(now)})
    elif decision == "snooze":
        rec["state"] = "snoozed"
        rec["snooze_count"] = rec.get("snooze_count", 0) + 1
        # expiry = now + 14d routine + 14d hold (finding 1)
        rec["expires_at"] = now_iso(now + ROUTINE_EXPIRY_S + SNOOZE_HOLD_S)
        rec["snooze_until"] = now_iso(now + SNOOZE_HOLD_S)
        if rec["snooze_count"] > SNOOZE_MAX_REPROPOSES:
            rec["state"] = "dismissed"         # auto-dismiss after cap
            rec["dismiss_count"] = rec.get("dismiss_count", 0) + 1
            rec["dismissed_at"] = now_iso(now)
            rec["last_reason"] = "auto-dismiss: snooze cap reached"
    else:
        raise ValueError(f"unknown decision: {decision!r}")
    return rec


# --- proposal rows (Routine class, spec §3.3; shadow per §5 step 3) ------------------

def proposal_row(candidate: dict, scores: dict, *, now: float | None = None) -> dict:
    """One proposal row: Routine class (SLA 72h breach, 14-day expiry),
    candidate name/kind/degree, the three score components, ≥3 verbatim
    evidence items, and the seed sources that generated it."""
    now = time.time() if now is None else now
    return {
        "id": ledger_key(candidate["kind"], candidate["target"]),
        "class": "routine",
        "kind": candidate["kind"],
        "target": candidate["target"],
        "degree": candidate["degree"],
        "A": scores["A"], "U": scores["U"], "G": scores["G"],
        "score": scores["score"],
        "evidence": build_evidence(candidate),
        "seeds": list(candidate["seed_ids"]),
        "created_at": now_iso(now),
        "sla_breach_at": now_iso(now + ROUTINE_SLA_HOURS * 3600),
        "expires_at": now_iso(now + ROUTINE_EXPIRY_S),
        "status": "shadow",      # §5 step 3: logged, NOT surfaced
    }


def outstanding_proposals(ledger: dict) -> list[dict]:
    return [r for r in ledger.get("candidates", {}).values()
            if r.get("state") == "proposed"]


def expire_proposals(ledger: dict, now: float | None = None) -> list[dict]:
    """SLA/expiry sweep (spec §6 e, adapted): a row unactioned past 72h has
    ONE breach ping logged; past 14 days it expires — dropped, logged,
    visible once. Returns the log lines (shadow mode: ledger log, not queue)."""
    now = time.time() if now is None else now
    events = []
    for key, rec in ledger.get("candidates", {}).items():
        if rec.get("state") not in ("proposed", "snoozed"):
            continue
        if rec.get("state") == "proposed":
            created = parse_iso(rec.get("created_at")) or now
            if not rec.get("sla_breached") and now > created + ROUTINE_SLA_HOURS * 3600:
                rec["sla_breached"] = True
                events.append({"event": "sla_breach", "id": key, "ts": now_iso(now)})
        expiry = parse_iso(rec.get("expires_at"))
        if expiry and now > expiry:
            rec["state"] = "expired"
            events.append({"event": "expired", "id": key, "ts": now_iso(now)})
    return events


# --- burst detection (finding 11, spec §4) --------------------------------------------

def detect_bursts(sources: list[dict], items: list[dict], *,
                  now: float | None = None) -> list[dict]:
    """Per-source daily burst alarms: the ABSOLUTE alarm (items/day over the
    hard cap) is never suppressed; the RELATIVE alarm fires when today's
    matched-item rate is BURST_REL_FACTOR x the trailing 7-day median, with
    the median floored at 3/day."""
    now = time.time() if now is None else now
    alarms = []
    for source in sources:
        if not source.get("enabled", True):
            continue
        sid = source["id"]
        daily: dict[str, int] = {}
        for item in items:
            if item.get("source_id") != sid:
                continue
            text = f"{item.get('title') or ''}\n{item.get('body') or ''}"
            if not source_matches(source, text):
                continue
            day = time.strftime("%Y-%m-%d", time.gmtime(item["_ts"]))
            daily[day] = daily.get(day, 0) + 1
        if not daily:
            continue
        today = time.strftime("%Y-%m-%d", time.gmtime(now))
        today_rate = daily.get(today, 0)
        trailing = [daily[d] for d in sorted(daily)
                    if d < today and (now - (parse_iso(d + "T00:00:00Z") or now)) <= 7 * _DAY]
        median = statistics.median(trailing) if trailing else 0.0
        if today_rate >= BURST_ABS_PER_DAY:
            alarms.append({"source": sid, "kind": "absolute",
                           "today_rate": today_rate, "median_7d": median})
        elif today_rate >= BURST_REL_FACTOR * max(median, BURST_MEDIAN_FLOOR):
            alarms.append({"source": sid, "kind": "relative",
                           "today_rate": today_rate, "median_7d": median})
    return alarms


# --- the weekly batch (spec §3, §5 step 3) ---------------------------------------------

def run_weekly_batch(sources_path, items: list[dict], ledger_path, *,
                     now: float | None = None, seed_rates: dict[str, float] | None = None
                     ) -> dict:
    """One weekly batch in SHADOW MODE: discover → score → top-N proposal
    rows logged to the proposal ledger. Proposals are NEVER surfaced: the
    digest section is separate from blocking classes and excluded from
    ``waits_for_you`` (finding 8 + §5 step 3)."""
    now = time.time() if now is None else now
    sources = load_sources(sources_path)
    for s in sources:
        s["_rate_per_week"] = (seed_rates or {}).get(s["id"], 1.0)
    ledger = load_ledger(ledger_path)

    # expiry sweep first: expired rows free their outstanding slots
    expiry_events = expire_proposals(ledger, now)
    outstanding = len(outstanding_proposals(ledger))

    approved_keys = {k for k, r in ledger.get("candidates", {}).items()
                     if r.get("state") == "approved"}
    candidates = discover_candidates(sources, items, approved_proposal_keys=approved_keys)

    scored = []
    for cand in candidates:
        key = ledger_key(cand["kind"], cand["target"])
        if suppressed(ledger, cand["kind"], cand["target"], now):
            continue                      # finding 5
        scores = score_candidate(cand, sources, ledger, now=now)
        if scores is None:
            continue
        cand["_scores"] = scores
        scored.append(cand)
    # top-N by score, respecting the outstanding cap (finding 8)
    scored.sort(key=lambda c: -c["_scores"]["score"])
    slots = max(MAX_OUTSTANDING_PROPOSALS - outstanding, 0)
    proposed, retained = [], []
    for cand in scored:
        key = ledger_key(cand["kind"], cand["target"])
        rec = ledger["candidates"].get(key)
        if proposed and len(proposed) >= slots:
            retained.append({"id": key, "score": cand["_scores"]["score"]})
            continue
        if rec and rec.get("state") == "snoozed":
            # snoozed candidates re-propose only when the score rises (§3.3)
            prev = rec.get("last_score", 0.0)
            if cand["_scores"]["score"] <= prev:
                retained.append({"id": key, "score": cand["_scores"]["score"]})
                continue
        row = proposal_row(cand, cand["_scores"], now=now)
        row["last_score"] = cand["_scores"]["score"]
        ledger.setdefault("candidates", {})[key] = {
            **row, "kind": cand["kind"], "target": cand["target"],
            "state": "proposed"}
        proposed.append(row)
    # §4: sub-threshold / over-cap candidates accumulate silently WITH their
    # scores (retained 90 days in shadow, pruned later) — never surfaced.
    shadow_scores = ledger.setdefault("shadow_scores", {})
    for cand in scored:
        key = ledger_key(cand["kind"], cand["target"])
        if any(p["id"] == key for p in proposed):
            continue
        shadow_scores[key] = {
            "kind": cand["kind"], "target": cand["target"],
            "degree": cand["degree"], "seeds": list(cand["seed_ids"]),
            **cand["_scores"],
            "evidence": build_evidence(cand),
            "ts": now_iso(now)}
    save_ledger(ledger_path, ledger)

    bursts = detect_bursts(sources, items, now=now)
    return {
        "built_at": now_iso(now),
        "shadow": True,
        "proposals": proposed,
        "retained_scores": retained,     # sub-threshold/rest accumulate silently (§4)
        "scored_candidates": len(scored),
        "burst_alarms": bursts,
        "expiry_events": expiry_events,
        # finding 8 + shadow mode: proposals never count as waiting-for-you
        "waits_for_you": False,
    }
