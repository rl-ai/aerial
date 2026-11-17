"""SWOT cadence (listening-pipeline spec §5 / gap A3): analyze the curated
digest per configured entity on a weekly schedule and surface the result as
an exception-queue proposal + a daily-board markdown render.

Every claim in a SWOT document MUST carry evidence links back to ingested
item ids — the validator rejects unevidenced claims. Two generation paths:

- model-backed (governed): ``bin/model_call.py --role glm-analyst`` receives
  the windowed digest items and returns a SWOT JSON document; the same
  validator gates its output, so a model cannot invent evidence.
- heuristic fallback (the testable default): keyword clustering of digest
  items into the four quadrants.

Cadence: ``weekly_crontab_line()`` emits a job_run.py crontab entry
(``--job <node>:swot-weekly``) matching the ingest-bridge / scheduler
convention; ``validate_job_entry()`` is its machine check.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ENTITIES_PATH = os.path.join(HERE, "swot_entities.json")
DEFAULT_MODEL_CALL = os.path.expanduser("~/ai-ops/bin/model_call.py")

QUADRANTS = ("strengths", "weaknesses", "opportunities", "threats")
_ISO = "%Y-%m-%dT%H:%M:%SZ"

# Heuristic quadrant lexicons: a claim is filed under a quadrant when the
# item's title+summary contains any keyword of that quadrant.
LEXICONS = {
    "strengths": ("released", "shipped", "improved", "faster", "outperform",
                  "gain", "achieved", "wins", "stable", "merged", "optimized"),
    "weaknesses": ("bug", "crash", "regression", "broken", "fails", "leak",
                   "bottleneck", "limitation", "oom", "error", "flaky",
                   "blocked", "deprecated-internal"),
    "opportunities": ("request", "roadmap", "upcoming", "proposal", "adopt",
                      "integration", "demand", "partnership", "beta", "rsvp",
                      "community", "grant", "pilot"),
    "threats": ("competitor", "alternative", "migration", "price", "pricing",
                "license change", "fork", "security", "cve", "exploit",
                "shut down", "sunset", "acquisition"),
}


class SwotValidationError(ValueError):
    """The document violates the SWOT schema or the evidence rule."""


def now_iso(ts: Optional[float] = None) -> str:
    return time.strftime(_ISO, time.gmtime(ts if ts is not None else time.time()))


def parse_iso(text: str) -> Optional[float]:
    if text is None:
        return None
    text = str(text).strip()
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)
    try:
        return _fromiso(text)
    except ValueError:
        return None


def _fromiso(text: str) -> float:
    from datetime import datetime, timezone
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


# ---------------------------------------------------------------- schema

def validate_swot(swot: Any, known_ids: Optional[set] = None) -> dict:
    """Validate a SWOT document against the schema. Returns the document.

    Schema:
      {entity, generated_at, window_days, strengths[], weaknesses[],
       opportunities[], threats[],
       evidence: [{item_id, quote_or_summary, source, score}]}
    Each quadrant entry is {text, item_ids} with >=1 item_id; every
    item_id must exist in ``evidence`` (and in ``known_ids`` — the digest
    window — when given). No unevidenced claims, ever.
    """
    if not isinstance(swot, dict):
        raise SwotValidationError("SWOT must be an object")
    for key in ("entity", "generated_at", "window_days"):
        if key not in swot:
            raise SwotValidationError(f"missing key: {key}")
    if not isinstance(swot["entity"], str) or not swot["entity"].strip():
        raise SwotValidationError("entity must be a non-empty string")
    if parse_iso(swot["generated_at"]) is None:
        raise SwotValidationError(f"generated_at not ISO: {swot['generated_at']!r}")
    wd = swot["window_days"]
    if not isinstance(wd, int) or isinstance(wd, bool) or wd <= 0:
        raise SwotValidationError(f"window_days must be a positive int: {wd!r}")

    evidence = swot.get("evidence")
    if not isinstance(evidence, list):
        raise SwotValidationError("evidence must be a list")
    ev_ids = set()
    for ev in evidence:
        if not isinstance(ev, dict):
            raise SwotValidationError("evidence entries must be objects")
        for key in ("item_id", "quote_or_summary", "source", "score"):
            if key not in ev:
                raise SwotValidationError(f"evidence missing key: {key}")
        if not isinstance(ev["item_id"], str) or not ev["item_id"]:
            raise SwotValidationError("evidence item_id must be a non-empty string")
        if not isinstance(ev["score"], (int, float)) or isinstance(ev["score"], bool):
            raise SwotValidationError(f"evidence score must be numeric: {ev['item_id']}")
        ev_ids.add(ev["item_id"])
    if known_ids is not None:
        unknown = ev_ids - set(known_ids)
        if unknown:
            raise SwotValidationError(
                f"evidence references unknown items: {sorted(unknown)}")

    for quad in QUADRANTS:
        entries = swot.get(quad)
        if not isinstance(entries, list):
            raise SwotValidationError(f"{quad} must be a list")
        for entry in entries:
            if not isinstance(entry, dict) or "text" not in entry:
                raise SwotValidationError(f"{quad} entries must be objects with text")
            ids = entry.get("item_ids")
            if not isinstance(ids, list) or not ids:
                raise SwotValidationError(
                    f"{quad} claim unevidenced (no item_ids): {entry.get('text')!r}")
            missing = [i for i in ids if i not in ev_ids]
            if missing:
                raise SwotValidationError(
                    f"{quad} claim cites evidence absent from the document: {missing}")
    return swot


def load_entities(path: str = ENTITIES_PATH) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    entities = doc.get("entities")
    if not isinstance(entities, list) or not entities:
        raise ValueError(f"{path}: no entities configured")
    for e in entities:
        for key in ("entity", "topic", "keywords", "impact_class"):
            if key not in e:
                raise ValueError(f"{path}: entity entry missing {key!r}")
    return entities


# ---------------------------------------------------------------- digest in

def normalize_digest(digest: dict) -> list[dict]:
    """Digest -> normalized items [{item_id, title, summary, source, ts, score}].

    Accepts the bridge digest (``ingested`` rows) or a raw item list under
    ``items`` / ``ingested``. item_id falls back to a stable hash of the
    title when the digest row lacks one."""
    rows = digest.get("items") or digest.get("ingested") or []
    items = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        title = row.get("title") or row.get("question") or ""
        item_id = row.get("item_id") or row.get("id") or row.get("guid")
        if not item_id:
            item_id = f"item-{abs(hash(title)) % 10**10:010d}-{i}"
        items.append({
            "item_id": str(item_id),
            "title": title,
            "summary": row.get("summary") or row.get("body") or title,
            "source": row.get("source") or row.get("source_id") or "unknown",
            "ts": parse_iso(row.get("ts") or row.get("fetched_at") or ""),
            "score": row.get("score"),
        })
    return items


def window_items(items: list[dict], window_days: int,
                 now: Optional[float] = None) -> list[dict]:
    """Window math: only items whose ts falls inside the last
    ``window_days`` days are analyzed. Items without a ts are kept
    (undated evidence is not silently dropped)."""
    now = time.time() if now is None else now
    cutoff = now - window_days * 86400
    return [it for it in items
            if it.get("ts") is None or it["ts"] >= cutoff]


def items_for_entity(items: list[dict], entity_cfg: dict) -> list[dict]:
    """Match items to an entity by the topic/keywords of its config."""
    kws = [k.lower() for k in entity_cfg["keywords"]]
    out = []
    for it in items:
        hay = f"{it['title']}\n{it['summary']}".lower()
        if any(k in hay for k in kws):
            out.append(it)
    return out


# ------------------------------------------------------- heuristic path

def _score_item(item: dict, matched: int) -> float:
    base = item.get("score")
    if isinstance(base, (int, float)):
        return round(float(base), 4)
    return round(min(1.0, 0.3 + 0.1 * matched), 4)


def heuristic_swot(entity_cfg: dict, items: list[dict], window_days: int,
                   now: Optional[float] = None) -> dict:
    """Pure-heuristic SWOT: keyword clustering of the windowed digest items
    into the four quadrants. Deterministic; every claim cites its item."""
    now = time.time() if now is None else now
    swot = {"entity": entity_cfg["entity"], "generated_at": now_iso(now),
            "window_days": window_days,
            "strengths": [], "weaknesses": [],
            "opportunities": [], "threats": [], "evidence": []}
    for item in items:
        hay = f"{item['title']}\n{item['summary']}".lower()
        evidence_added = False
        for quad in QUADRANTS:
            hits = [k for k in LEXICONS[quad] if k in hay]
            if not hits:
                continue
            if not evidence_added:
                swot["evidence"].append({
                    "item_id": item["item_id"],
                    "quote_or_summary": (item["summary"] or item["title"])[:160],
                    "source": item["source"],
                    "score": _score_item(item, len(hits)),
                })
                evidence_added = True
            swot[quad].append({
                "text": f"{item['title']} ({', '.join(hits[:3])})",
                "item_ids": [item["item_id"]],
            })
    return swot


# ---------------------------------------------------------- model path

def _default_model_runner(ask_path: str, files: list[str], out_path: str) -> dict:
    """Governed call: bin/model_call.py --role glm-analyst (spawned)."""
    py = sys.executable
    cmd = [py, DEFAULT_MODEL_CALL, "--role", "glm-analyst",
           "--ask", ask_path, "--files", *files, "--out", out_path]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(f"model_call.py exit {proc.returncode}: "
                           f"{(proc.stderr or proc.stdout)[-400:]}")
    with open(out_path, encoding="utf-8") as fh:
        return json.load(fh)


def model_swot(entity_cfg: dict, items: list[dict], window_days: int,
               now: Optional[float] = None,
               runner: Callable = _default_model_runner) -> dict:
    """Model-backed SWOT via the governed model_call.py path. The model's
    JSON is validated with the same evidence rule; a bad model answer never
    reaches the queue. Raises on failure (caller falls back to heuristic)."""
    now = time.time() if now is None else now
    ask = (
        f"Produce a SWOT analysis for the entity '{entity_cfg['entity']}' "
        f"({entity_cfg['display_name']}) from the attached digest items "
        f"(window: last {window_days} days). Return ONLY a JSON object with "
        f"keys entity, generated_at, window_days, strengths, weaknesses, "
        f"opportunities, threats, evidence. Each quadrant entry must be "
        f"{{\"text\": str, \"item_ids\": [str]}} and every item_id MUST be "
        f"one of the attached item ids — claims without evidence are "
        f"rejected. evidence entries: {{item_id, quote_or_summary, source, "
        f"score}}. Attached item ids: "
        f"{json.dumps([it['item_id'] for it in items])}")
    with tempfile.TemporaryDirectory() as tmp:
        ask_path = os.path.join(tmp, "ask.txt")
        items_path = os.path.join(tmp, "items.json")
        out_path = os.path.join(tmp, "swot.json")
        with open(ask_path, "w", encoding="utf-8") as fh:
            fh.write(ask)
        with open(items_path, "w", encoding="utf-8") as fh:
            json.dump(items, fh)
        doc = runner(ask_path, [items_path], out_path)
    doc.setdefault("entity", entity_cfg["entity"])
    doc.setdefault("generated_at", now_iso(now))
    doc.setdefault("window_days", window_days)
    doc.setdefault("strengths", [])
    doc.setdefault("weaknesses", [])
    doc.setdefault("opportunities", [])
    doc.setdefault("threats", [])
    doc.setdefault("evidence", [])
    return validate_swot(doc)  # evidence rule gates the model output


def generate_swot(entity_cfg: dict, items: list[dict], window_days: int = 7,
                  now: Optional[float] = None,
                  runner: Optional[Callable] = "default",
                  use_model: bool = True) -> dict:
    """Generation entry point: try the governed model path, fall back to
    the heuristic clustering when the model path is unavailable or fails.
    ``runner=None`` forces the heuristic (the testable default)."""
    now = time.time() if now is None else now
    windowed = window_items(items, window_days, now)
    if use_model and runner is not None:
        try:
            fn = _default_model_runner if runner == "default" else runner
            return model_swot(entity_cfg, windowed, window_days, now,
                              runner=fn)
        except Exception:  # noqa: BLE001 — model path is best-effort
            pass
    return heuristic_swot(entity_cfg, windowed, window_days, now)


def generate_all(entities_path: str, digest: dict, window_days: int = 7,
                 now: Optional[float] = None, **kw) -> dict:
    """One cadence run over every configured entity. Returns
    {entity: swot_document} plus a 'meta' key with the window and item ids."""
    now = time.time() if now is None else now
    items = normalize_digest(digest)
    windowed = window_items(items, window_days, now)
    out = {"meta": {"generated_at": now_iso(now), "window_days": window_days,
                    "item_count": len(windowed),
                    "item_ids": [it["item_id"] for it in windowed]}}
    for entity_cfg in load_entities(entities_path):
        entity_items = items_for_entity(windowed, entity_cfg)
        out[entity_cfg["entity"]] = generate_swot(
            entity_cfg, entity_items, window_days, now, **kw)
    return out


# --------------------------------------------------------------- render

def render_markdown(swot: dict) -> str:
    """Daily-board markdown render: quadrants with evidence-linked claims."""
    lines = [f"# SWOT — {swot['entity']}", "",
             f"Generated {swot['generated_at']} · window {swot['window_days']}d", ""]
    titles = {"strengths": "Strengths", "weaknesses": "Weaknesses",
              "opportunities": "Opportunities", "threats": "Threats"}
    for quad in QUADRANTS:
        entries = swot.get(quad) or []
        lines.append(f"## {titles[quad]} ({len(entries)})")
        for e in entries:
            lines.append(f"- {e['text']} [evidence: "
                         f"{', '.join(e['item_ids'])}]")
        lines.append("")
    ev = swot.get("evidence") or []
    if ev:
        lines.append(f"## Evidence ({len(ev)})")
        for e in ev:
            lines.append(f"- [{e['item_id']}] ({e['source']}, score {e['score']}) "
                         f"{e['quote_or_summary']}")
        lines.append("")
    return "\n".join(lines)


# ------------------------------------------------------- queue proposal

def submit_to_queue(swot: dict, queue, now: Optional[float] = None):
    """Land the SWOT in the exception queue as a proposal. Idempotent: an
    open queue item with the same condition_key is returned as-is instead
    of re-admitted. Returns (item, created:boolean)."""
    from dashboard.exception_queue.queue import Finding  # repo of record
    now = time.time() if now is None else now
    condition_key = f"swot-weekly:{swot['entity']}"
    # idempotency: one open proposal per entity per cycle
    for existing in queue.ordered(now):
        if existing.condition_key == condition_key \
                and existing.state in ("open", "waiting-on-you"):
            return existing, False
    n_claims = sum(len(swot.get(q) or []) for q in QUADRANTS)
    score = round(min(0.9, 0.35 + 0.05 * n_claims), 4)   # >= routine threshold
    claim = (f"Weekly SWOT for {swot['entity']} "
             f"({n_claims} evidence-backed findings, "
             f"window {swot['window_days']}d)")
    finding = Finding(
        condition_key=condition_key,
        claim=claim,
        impact_class="routine",
        admission_score=score,
        eval_passed=True,
        evidence_label=f"listening-swot:{swot['generated_at']}",
        source_agent="listening-swot",
        proposed_action=("Review the weekly SWOT render; promote notable "
                         "opportunities/threats into tickets"),
    )
    item = queue.admit(finding)
    return item, item is not None


# ---------------------------------------------------------------- cadence

JOB_NAME = "swot-weekly"
JOB_SCHEDULE = "0 6 * * 1"       # weekly: Mondays 06:00
JOB_NODE = "aerial"


def weekly_crontab_line(workdir: str = "~/workspaces/worker/lanes-p1a-swot/out",
                        log_path: str = "~/services/scrape-pipeline/logs/swot-weekly.log",
                        node: str = JOB_NODE, ticket: Optional[str] = None,
                        user: Optional[str] = None) -> str:
    """job_run.py crontab entry (weekly), matching the ingest-bridge
    convention: `sched user cd <dir> && job_run.py --job <node>:<name>
    [--ticket <tk>] -- <payload> >> log 2>&1`."""
    payload = (f"python3 dashboard/listening/swot.py "
               f"--entities dashboard/listening/swot_entities.json "
               f"--digest-path bridge/digest-latest.json --window-days 7")
    ticket_arg = f" --ticket {ticket}" if ticket else ""
    # the system-crontab user column: the caller's, $AERIAL_JOB_USER, or "aerial"
    user = user or os.environ.get("AERIAL_JOB_USER") or "aerial"
    return (f"{JOB_SCHEDULE} {user} cd {workdir} && "
            f"python3 ~/ai-ops/bin/job_run.py --job {node}:{JOB_NAME}"
            f"{ticket_arg} -- {payload} >> {log_path} 2>&1")


def validate_job_entry(line: str) -> dict:
    """Machine check of the cadence entry: 5-field cron spec with Monday
    dow, a --job <node>:<name> id, and a non-empty payload after ' -- '."""
    fields = line.split()
    if len(fields) < 6:
        raise ValueError(f"not a cron line: {line!r}")
    spec = " ".join(fields[:5])
    if len(spec.split()) != 5:
        raise ValueError(f"bad cron spec: {spec!r}")
    if spec.split()[4] != "1":
        raise ValueError(f"weekly cadence must set dow=1 (Monday): {spec!r}")
    if "job_run.py" not in line:
        raise ValueError("entry must run via bin/job_run.py")
    m = re.search(r"--job\s+(\S+)", line)
    if not m:
        raise ValueError("missing --job argument")
    payload = line.split(" -- ", 1)[-1]
    # strip a trailing shell redirect (same convention as
    # dashboard/followup/runner._cron_payload)
    rm = re.match(r"^(.*?)\s*>>?\s+\S.*$", payload)
    if rm:
        payload = rm.group(1).strip()
    if not payload:
        raise ValueError("empty payload after ' -- ' (job_run re-trigger needs it)")
    return {"schedule": spec, "job": m.group(1), "payload": payload.strip(),
            "name": m.group(1).split(":", 1)[-1]}


# -------------------------------------------------------------------- CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--entities", default=ENTITIES_PATH)
    ap.add_argument("--digest-path", default=None,
                    help="bridge/digest-latest.json (curated digest)")
    ap.add_argument("--window-days", type=int, default=7)
    ap.add_argument("--out-dir", default=".",
                    help="where swot-<entity>.json / .md and the queue db land")
    ap.add_argument("--queue-db", default=None,
                    help="exception-queue sqlite path (default: <out-dir>/queue.db)")
    ap.add_argument("--no-model", action="store_true",
                    help="force the heuristic path")
    ap.add_argument("--print-crontab", action="store_true",
                    help="print the weekly crontab line and exit")
    args = ap.parse_args(argv)
    if args.print_crontab:
        print(weekly_crontab_line())
        return 0
    if not args.digest_path:
        ap.error("--digest-path is required (or use --print-crontab)")
    with open(args.digest_path, encoding="utf-8") as fh:
        digest = json.load(fh)
    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)
    results = generate_all(args.entities, digest, args.window_days,
                           use_model=not args.no_model)
    known_ids = set(results["meta"]["item_ids"])
    for entity, swot in results.items():
        if entity == "meta":
            continue
        validate_swot(swot, known_ids=known_ids)
        base = os.path.join(out_dir, f"swot-{entity}")
        with open(base + ".json", "w", encoding="utf-8") as fh:
            json.dump(swot, fh, indent=1)
        with open(base + ".md", "w", encoding="utf-8") as fh:
            fh.write(render_markdown(swot))
    queue_path = args.queue_db or os.path.join(out_dir, "queue.db")
    from dashboard.exception_queue.queue import Queue
    q = Queue(db_path=queue_path)
    for entity, swot in results.items():
        if entity == "meta":
            continue
        _, created = submit_to_queue(swot, q)
        results["meta"][f"queued:{entity}"] = "created" if created else "existing"
    q.close()
    print(json.dumps(results["meta"], indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
