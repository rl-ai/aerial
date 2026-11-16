#!/usr/bin/env python3
"""Scrape intelligence — themes, trends, stack overlap from the scrape store.

Reads yesterday's items from scrapes.db (schema: items id, source_id, topic,
guid, title, url, body, published, fetched_at, run_id) and produces:

- Themes: per-topic aggregates — item counts, top discriminating terms
  (term frequency in the current 24h window vs the prior 24h baseline,
  scored by count_delta, stopwords dropped; stdlib tokenizing only), and a
  per-topic trend arrow (up/flat/down vs the prior window, with the ratio).
- Stack overlap: per-project match counts against a project-keywords map
  (slug -> keywords), plus the top matching item titles (max 3 each).

Fail-closed: a missing/unreadable scrapes.db or items table degrades to
{'available': False, 'detail': ...} — never a fabricated number.
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
import time
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.dirname(HERE)
for _p in (_PKG_ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

def _norm_ts(value: Any) -> Optional[float]:
    """Normalize a mixed int/epoch-float/ISO timestamp to a POSIX float."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v <= 0:
            return None
        if v > 1e12:
            v /= 1000.0
        return v
    s = str(value).strip()
    if not s:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return _norm_ts(float(s))
    try:
        from datetime import datetime, timezone
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


from deployment_profile import pvalue as _pv
DEFAULT_SCRAPES_DB = str(_pv("scrapes_db"))
DEFAULT_REFERENCES_YAML = str(
    __import__("pathlib").Path(__file__).resolve().parent.parent
    / "docs" / "references.yaml")

WINDOW_H = 24
MAX_TERMS = 5
MAX_TITLES = 3

_STOPWORDS = frozenset("""
a an and are as at be been but by can could do does for from had has have
how i if in into is it its just may me more most no not of on or our out
over own said same she he should so some such than that the their them
then there these they this those to too up us very was we were what when
where which while who why will with would you your about after all also
am any because before being between both during each few other s t
don now new via com www http https html page pages post blog
""".split())

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9'+\-]*")


def _tokens(text: str) -> List[str]:
    out = []
    for w in _WORD_RE.findall((text or "").lower()):
        w = w.strip("'+-")
        if len(w) < 3 or w in _STOPWORDS or w.isdigit():
            continue
        out.append(w)
    return out


def _item_ts(row: Dict[str, Any]) -> Optional[float]:
    for key in ("published", "fetched_at"):
        ts = _norm_ts(row.get(key))
        if ts is not None:
            return ts
    return None


# ------------------------------------------------------------------ keywords

def load_project_keywords(path: str = DEFAULT_REFERENCES_YAML) -> Dict[str, List[str]]:
    """Project slug -> keyword list from a references.yaml (slug+title words).

    Tolerant loader: uses PyYAML when installed, otherwise a minimal line
    parser that picks up 'slug:'/'- slug'/'title:'/'name:' keys. Returns {}
    when the file is missing/unparsable (overlap section then reports none —
    fail-closed, not fabricated).
    """
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return {}
    docs: Any = None
    try:
        import yaml  # type: ignore
        docs = yaml.safe_load(text)
    except Exception:
        docs = None
    projects: Dict[str, str] = {}
    if isinstance(docs, dict):
        def _walk(node: Any, slug_hint: str = "") -> None:
            if isinstance(node, dict):
                title = node.get("title") or node.get("name") or ""
                slug = node.get("slug") or node.get("id") or ""
                if title and slug:
                    projects[str(slug)] = str(title)
                elif title and slug_hint and slug_hint not in ("projects", "projects.", ""):
                    projects[str(slug_hint)] = str(title)
                for k, v in node.items():
                    _walk(v, str(k))
            elif isinstance(node, list):
                for v in node:
                    _walk(v, slug_hint)
        _walk(docs)
    else:
        # Minimal fallback: "slug:" lines and their title/name values.
        current = ""
        for line in text.splitlines():
            m = re.match(r"^\s*-?\s*([\w\-./]+):\s*$", line)
            if m:
                current = m.group(1)
                continue
            m = re.match(r"^\s*(?:title|name):\s*(.+)$", line)
            if m and current:
                projects[current] = m.group(1).strip().strip("\"'")
        if not projects:
            # Last resort: treat top-level keys as project names themselves.
            projects = {ln.rstrip(":").strip(): ln.rstrip(":").strip()
                        for ln in text.splitlines()
                        if re.match(r"^\S.*:$", ln)}
    out: Dict[str, List[str]] = {}
    for slug, title in projects.items():
        words: List[str] = []
        for w in _tokens(f"{slug} {title}"):
            if w not in words:
                words.append(w)
        if words:
            out[slug] = words
    return out


# -------------------------------------------------------------------- themes

def _theme_stats(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_topic: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        topic = str(r.get("topic") or "untagged")
        by_topic.setdefault(topic, []).append(r)
    themes: List[Dict[str, Any]] = []
    for topic in sorted(by_topic):
        cur = Counter()
        for r in by_topic[topic]:
            cur.update(_tokens(f"{r.get('title') or ''} {r.get('body') or ''}"))
        themes.append({
            "topic": topic,
            "count": len(by_topic[topic]),
            "terms": cur,
            "titles": [str(r.get("title") or "(untitled)")
                       for r in by_topic[topic] if r.get("title")],
        })
    return themes


def _overlap_score(item: Dict[str, Any], keywords: Dict[str, List[str]]
                   ) -> List[str]:
    """Project slugs whose keywords appear in title+body (any-hit)."""
    text = f"{item.get('title') or ''} {item.get('body') or ''}".lower()
    return [slug for slug, kws in keywords.items()
            if any(kw.lower() in text for kw in kws)]


# --------------------------------------------------------------------- gather

def gather(now: Optional[float] = None,
           scrapes_db: str = DEFAULT_SCRAPES_DB,
           window_h: float = WINDOW_H,
           project_keywords: Optional[Dict[str, List[str]]] = None
           ) -> Dict[str, Any]:
    """Windowed scrape intelligence, fail-closed per source."""
    if now is None:
        now = time.time()
    if not os.path.exists(scrapes_db):
        return {"available": False,
                "detail": f"scrapes.db not found: {scrapes_db}"}
    try:
        conn = sqlite3.connect(f"file:{scrapes_db}?mode=ro", uri=True, timeout=0.2)
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute("SELECT * FROM items")]
        conn.close()
    except (sqlite3.Error, OSError) as e:
        return {"available": False, "detail": f"scrapes.db read failed: {e}"}
    start = now - window_h * 3600.0
    prior_start = start - window_h * 3600.0
    cur_rows: List[Dict[str, Any]] = []
    prior_rows: List[Dict[str, Any]] = []
    no_ts = 0
    for r in rows:
        ts = _item_ts(r)
        if ts is None:
            no_ts += 1
            continue
        if start <= ts < now:
            cur_rows.append(r)
        elif prior_start <= ts < start:
            prior_rows.append(r)
    # --- themes ------------------------------------------------------------
    cur_themes = _theme_stats(cur_rows)
    prior_by_topic = {t["topic"]: t for t in _theme_stats(prior_rows)}
    themes: List[Dict[str, Any]] = []
    for t in cur_themes:
        pt = prior_by_topic.get(t["topic"])
        prior_count = pt["count"] if pt else 0
        if t["count"] > prior_count:
            trend = "up"
        elif t["count"] < prior_count:
            trend = "down"
        else:
            trend = "flat"
        ratio = round(t["count"] / prior_count, 2) if prior_count else None
        baseline = pt["terms"] if pt else Counter()
        scored = [(term, cnt - baseline.get(term, 0), cnt)
                  for term, cnt in t["terms"].items()]
        scored.sort(key=lambda x: (-x[1], -x[2], x[0]))
        top_terms = [term for term, delta, _cnt in scored
                     if delta > 0][:MAX_TERMS]
        if not top_terms:  # no positive deltas: fall back to raw frequency
            top_terms = [term for term, _, _ in scored[:MAX_TERMS]]
        themes.append({
            "topic": t["topic"],
            "count": t["count"],
            "prior_count": prior_count,
            "trend": trend,
            "ratio": ratio,
            "top_terms": top_terms,
        })
    themes.sort(key=lambda t: -t["count"])
    # --- stack overlap -----------------------------------------------------
    overlap: List[Dict[str, Any]] = []
    if project_keywords:
        hits: Dict[str, List[str]] = {}
        for r in cur_rows:
            for slug in _overlap_score(r, project_keywords):
                title = str(r.get("title") or "(untitled)")
                if title not in hits.setdefault(slug, []):
                    hits[slug].append(title)
        for slug in project_keywords:
            titles = hits.get(slug, [])
            overlap.append({
                "project": slug,
                "count": len(titles),
                "top_titles": titles[:MAX_TITLES],
            })
        overlap.sort(key=lambda o: (-o["count"], o["project"]))
    return {
        "available": True,
        "window": {"start": start, "end": now,
                   "prior_start": prior_start, "prior_end": start},
        "items": len(cur_rows),
        "prior_items": len(prior_rows),
        "no_ts_items": no_ts,
        "themes": themes,
        "stack_overlap": overlap,
    }


# -------------------------------------------------------------------- render

def intel_table_html(intel: Dict[str, Any]) -> str:
    """Raw gathered stats as a small table so numbers survive vague DS prose."""
    if not intel.get("available"):
        return (f'<p class=unavailable>scrape intel unavailable'
                f' ({_esc(intel.get("detail", ""))})</p>')
    rows = "".join(
        f"<tr><td>{_esc(t['topic'])}</td><td>{t['count']}</td>"
        f"<td>{_esc(t['trend'])}"
        + (f" (x{t['ratio']})" if t.get("ratio") is not None else "")
        + f"</td><td>{_esc(', '.join(t['top_terms']) or '-')}</td></tr>"
        for t in intel["themes"])
    return ("<table class=scrape-intel><tr><th>theme</th><th>count</th>"
            f"<th>trend</th><th>top terms</th></tr>{rows}</table>")


def _esc(v: Any) -> str:
    import html as _html
    return _html.escape("" if v is None else str(v))
