"""Listening pipeline (lanes-research/listening-pipeline.md §1–2, §5 step 2).

The ``github_repo`` adapter + ingest plumbing: poll a repo's releases and
pushes, filter with the §1 ``match`` semantics, emit normalized markdown
notes into a per-source staging folder, and let the existing Curator
ingest them. Registry registration is the only downstream change (t5 §1.1).

Decisions bound by the lane brief (do not re-decide):

- ``match`` semantics (finding 20): an empty match list matches NOTHING;
  an empty group (``[]``) inside a match list matches ALL; a
  ``github_pushes`` source requires a non-empty match list. One rule,
  every kind.
- Sensitivity (finding 10): taken from the source entry; ``private``
  default when ``relation: own``; adapters never hardcode ``public``.
- High-water mark (finding 3): ``last_seen_ts`` persisted per source,
  initialized to add-time (or an explicit ``backfill_days``); hard
  per-cycle note cap 200/source/cycle; transcript budget 20/source/day.
- Failure state machine (findings 12–13): failure = non-2xx / parse
  error; transient (429/5xx/timeout) → exponential backoff; permanent
  (404/410/schema) → one digest row + auto-disable; 2 consecutive
  failures → one digest line, 4 → Routine queue row with error evidence;
  reset on success.
- Approve-path validation (finding 9): machine-appended entries are
  validated against the §1 schema, slugified + allowlisted ``id``,
  duplicate (kind,target) and id collisions rejected. Staging folder
  names derive from a hash, never from target text.
- Digest windows on ``ingest_ts``; ``published`` carried as a staleness
  attribute; notes whose published age exceeds the threshold are
  suppressed (finding 18).
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .curator import Curator
from .graph import Workspace
from .ingest import IngestAdapter
from .vault import L0Vault


# --- constants bound by the lane brief -------------------------------------

#: Hard per-cycle note cap: 200 notes per source per cycle.
NOTE_CAP_PER_CYCLE = 200
#: Transcript fetch budget: 20 per source per day (shared by all kinds).
TRANSCRIPT_BUDGET_PER_DAY = 20
#: Notes whose published timestamp is older than this many days are
#: suppressed on ingest (finding 18) unless ``backfill_days`` is explicit.
MAX_PUBLISHED_AGE_DAYS = 30
#: Failure thresholds (findings 12–13 / spec §4).
DIGEST_FAILURES = 2      # consecutive failures -> one digest line
ROUTINE_FAILURES = 4     # consecutive failures -> Routine queue row
#: Transient backoff: base seconds, exponential in consecutive failures.
BACKOFF_BASE_S = 300
BACKOFF_MAX_S = 6 * 3600

_KINDS = ("reddit_sub", "github_repo", "github_pushes", "youtube_channel")
_RELATIONS = ("competitor", "dependency", "inspiration", "own")
_WATCH = ("releases", "pushes")
_ID_RE = re.compile(r"^[a-z_]+\.[a-z0-9-]+$")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_ISO = "%Y-%m-%dT%H:%M:%SZ"


def now_iso(ts: float | None = None) -> str:
    return time.strftime(_ISO, time.gmtime(ts if ts is not None else time.time()))


def parse_iso(text: str) -> float | None:
    """ISO-8601 (or epoch) text -> epoch seconds; None when unparseable."""
    if text is None:
        return None
    text = str(text).strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def slugify(text: str) -> str:
    s = _SLUG_RE.sub("-", (text or "").lower()).strip("-")
    return s[:40] or "unknown"


# --- match semantics (finding 20) -------------------------------------------

def item_matches(match, text: str) -> bool:
    """§1 semantics, one rule for every kind: an item matches when ALL
    terms of ANY ONE group appear in ``text``.

    - ``match == []`` (empty list) matches NOTHING — an accidental empty
      list can never silently firehose a source.
    - An empty group (``[]``) inside a non-empty list matches ALL.
    """
    if not match:
        return False
    hay = (text or "").lower()
    for group in match:
        if not group:            # empty group = match all
            return True
        if all(t.lower() in hay for t in group):
            return True
    return False


def source_matches(source: dict, text: str) -> bool:
    """Match semantics PER SOURCE KIND. The ``reddit_sub`` entries predate
    the listening kinds: the production scraper (scrape-subs/sample.py)
    matches when EVERY group has ANY term hit — their one-group-of-14-terms
    shape is uninterpretable under the listening rule (zero matches on the
    real corpus), so discovery keeps the scraper's semantics for them. The
    listening kinds (github_repo / github_pushes / youtube_channel) use the
    lane-2 ``item_matches`` rule bound in the lane brief."""
    match = source.get("match") or []
    if source.get("kind") != "reddit_sub":
        return item_matches(match, text)
    if not match:
        return False
    hay = (text or "").lower()
    return all(any(t.lower() in hay for t in g) for g in match if g)


# --- source schema + approve-path validation (finding 9) --------------------

class SourceValidationError(ValueError):
    pass


#: S3 hardening caps for ``validate_source_entry`` targets (buglist 2026-11-14).
_TARGET_MAX_LEN = 2048
#: shell metacharacters + newlines/control chars are never valid in a target.
_TARGET_FORBIDDEN_RE = re.compile(r"[;|&$`<>()\{}\[\]!~\*\?\"\']|[\x00-\x1f\x7f]")


def validate_source_entry(entry: dict, seen_ids: set[str] | None = None,
                          seen_pairs: set[tuple] | None = None) -> dict:
    """Validate one machine-appended (or hand-written) source entry against
    the §1 schema. Returns a normalized copy. Raises SourceValidationError.

    ``id`` is ``<kind>.<slug>``, slugified and allowlisted to ``[a-z0-9-]``.
    Duplicate ids and duplicate (kind, target) pairs are rejected.
    """
    if not isinstance(entry, dict):
        raise SourceValidationError("source entry must be an object")
    kind = entry.get("kind")
    if kind not in _KINDS:
        raise SourceValidationError(f"unknown source kind: {kind!r}")
    target = entry.get("target")
    if not isinstance(target, str) or not target.strip():
        raise SourceValidationError(f"{kind}: 'target' must be a non-empty string")
    # S3: cap target size + charset (buglist 2026-11-14). A 100KB target or a
    # shell-metachar target must never reach an adapter / subprocess.
    if len(target) > _TARGET_MAX_LEN:
        raise SourceValidationError(
            f"{kind}: target too long ({len(target)} > {_TARGET_MAX_LEN} chars)")
    if _TARGET_FORBIDDEN_RE.search(target):
        raise SourceValidationError(
            f"{kind}: target contains forbidden characters "
            "(shell metachars or control chars)")
    topic = entry.get("topic")
    if not isinstance(topic, str) or not topic.strip():
        raise SourceValidationError(f"{kind}: 'topic' must be a non-empty string")
    match = entry.get("match")
    if not isinstance(match, list) or not all(isinstance(g, list) for g in match):
        raise SourceValidationError(f"{kind}: 'match' must be a list of term groups")
    if kind == "github_pushes" and not match:
        raise SourceValidationError(
            "github_pushes requires a non-empty match list (finding 20)")
    relation = entry.get("relation", "competitor")
    if relation not in _RELATIONS:
        raise SourceValidationError(f"{kind}: unknown relation {relation!r}")
    every_h = entry.get("every_h", 24)
    if not isinstance(every_h, (int, float)) or every_h <= 0:
        raise SourceValidationError(f"{kind}: every_h must be a positive number")
    raw_id = entry.get("id")
    family = kind.split("_")[0]   # github_repo -> github (spec §1 id form)
    slug = slugify(raw_id.split(".", 1)[1]) if isinstance(raw_id, str) and "." in raw_id \
        else slugify(target)
    sid = f"{family}.{slug}"
    if not _ID_RE.fullmatch(sid):
        raise SourceValidationError(f"invalid id: {sid!r}")
    if seen_ids is not None and sid in seen_ids:
        raise SourceValidationError(f"duplicate source id: {sid}")
    pair = (kind, target.strip())
    if seen_pairs is not None and pair in seen_pairs:
        raise SourceValidationError(f"duplicate (kind,target): {pair}")
    watch = entry.get("watch", ["releases"])
    if kind == "github_pushes":
        watch = ["pushes"]
    if kind in ("github_repo", "github_pushes"):
        if not isinstance(watch, list) or not watch \
                or not all(w in _WATCH for w in watch):
            raise SourceValidationError(f"{kind}: 'watch' must be a non-empty "
                                        f"subset of {list(_WATCH)}")
    normalized = {
        "id": sid,
        "kind": kind,
        "topic": topic.strip(),
        "target": target.strip(),
        "every_h": every_h,
        "match": match,
        "relation": relation,
        "added_via": entry.get("added_via", "seed"),
        "enabled": bool(entry.get("enabled", True)),
    }
    if kind in ("github_repo", "github_pushes"):
        normalized["watch"] = watch
        if entry.get("backfill_days") is not None:
            normalized["backfill_days"] = float(entry["backfill_days"])
        if entry.get("weight") is not None:
            normalized["weight"] = float(entry["weight"])
    if kind == "youtube_channel":
        normalized["transcript"] = bool(entry.get("transcript", False))
    if entry.get("expires_days") is not None:
        normalized["expires_days"] = float(entry["expires_days"])
    return normalized


def load_sources(path) -> list[dict]:
    """Load + validate every entry in new_sources.json (§1 schema)."""
    entries = json.loads(Path(path).read_text(encoding="utf-8"))
    seen_ids: set[str] = set()
    seen_pairs: set[tuple] = set()
    out = []
    for entry in entries:
        norm = validate_source_entry(entry, seen_ids, seen_pairs)
        seen_ids.add(norm["id"])
        seen_pairs.add((norm["kind"], norm["target"]))
        out.append(norm)
    return out


def append_source(path, entry: dict) -> dict:
    """Approve path (finding 9): validate + append a machine-generated
    entry to new_sources.json atomically. Rejects duplicates and bad ids
    without touching the file."""
    path = Path(path)
    try:
        existing = load_sources(path)
    except FileNotFoundError:
        existing = []
    norm = validate_source_entry(
        entry,
        {s["id"] for s in existing},
        {(s["kind"], s["target"]) for s in existing})
    entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    entries.append(norm)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, indent=1), encoding="utf-8")
    tmp.replace(path)
    return norm


# --- per-source state: high-water mark, failures, budgets (finding 3) -------

class SourceState:
    """Persisted per-source bookkeeping (JSON file):
    ``last_seen_ts``, failure counters, transcript budget usage, and the
    auto-disable flag. A failed cycle never advances ``last_seen_ts``."""

    def __init__(self, path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data: dict = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.data = {}

    def get(self, sid: str, key: str, default=None):
        return self.data.get(sid, {}).get(key, default)

    def set(self, sid: str, key: str, value) -> None:
        self.data.setdefault(sid, {})[key] = value

    def init_source(self, source: dict, now: float | None = None) -> None:
        """last_seen_ts initialized to add-time, or add-time minus an
        explicit ``backfill_days``. Never rewinds an existing mark."""
        sid = source["id"]
        now = now if now is not None else time.time()
        if self.get(sid, "last_seen_ts") is None:
            offset = float(source.get("backfill_days", 0) or 0) * 86400
            self.set(sid, "last_seen_ts", now - offset)

    def enabled(self, source: dict) -> bool:
        sid = source["id"]
        if self.get(sid, "disabled"):
            return False
        return bool(source.get("enabled", True))

    def record_success(self, sid: str, last_event_ts: float | None) -> None:
        """Reset the failure counter and advance the high-water mark."""
        self.set(sid, "consecutive_failures", 0)
        self.set(sid, "backoff_until", None)
        self.set(sid, "last_error", None)
        if last_event_ts is not None:
            prev = self.get(sid, "last_seen_ts") or 0
            self.set(sid, "last_seen_ts", max(prev, last_event_ts))

    def classify_failure(self, status, exc: Exception | None = None) -> str:
        """Transient (429/5xx/timeout/network) vs permanent (404/410/schema)."""
        if status in (404, 410):
            return "permanent"
        if isinstance(exc, (json.JSONDecodeError, KeyError, TypeError, ValueError)):
            return "permanent"      # schema/parse error
        if status is not None and 500 <= status < 600:
            return "transient"
        if status == 429:
            return "transient"
        return "transient"          # timeouts, connection errors, unknown

    def record_failure(self, sid: str, kind: str, error: str,
                       now: float | None = None) -> str:
        """Advance the failure state machine. Returns the surfaced level:
        ``routine`` (>=4), ``digest`` (>=2), or ``logged`` (single /
        transient-cooldown). Permanent failures also auto-disable."""
        now = now if now is not None else time.time()
        failures = (self.get(sid, "consecutive_failures") or 0) + 1
        self.set(sid, "consecutive_failures", failures)
        self.set(sid, "last_error", {"kind": kind, "error": error,
                                     "ts": now_iso(now)})
        if kind == "permanent":
            self.set(sid, "disabled", True)
            return "disabled"
        # exponential backoff for transient failures
        delay = min(BACKOFF_BASE_S * (2 ** max(failures - 1, 0)), BACKOFF_MAX_S)
        self.set(sid, "backoff_until", now + delay)
        if failures >= ROUTINE_FAILURES:
            return "routine"
        if failures >= DIGEST_FAILURES:
            return "digest"
        return "logged"

    def transcripts_available(self, sid: str, now: float | None = None) -> bool:
        """Transcript fetch budget: 20/source/day (per decisions)."""
        now = now if now is not None else time.time()
        today = time.strftime("%Y-%m-%d", time.gmtime(now))
        used_day, used = self.get(sid, "transcript_day"), \
            self.get(sid, "transcripts_used", 0)
        if used_day != today:
            used = 0
        return used < TRANSCRIPT_BUDGET_PER_DAY

    def spend_transcript(self, sid: str, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        if not self.transcripts_available(sid, now):
            return False
        today = time.strftime("%Y-%m-%d", time.gmtime(now))
        if self.get(sid, "transcript_day") != today:
            self.set(sid, "transcript_day", today)
            self.set(sid, "transcripts_used", 0)
        self.set(sid, "transcripts_used",
                 (self.get(sid, "transcripts_used", 0) or 0) + 1)
        return True

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        tmp.replace(self.path)


# --- the github_repo adapter --------------------------------------------------

def _http_fetch(url: str, timeout: float = 30.0):
    """Default fetcher: returns (status, parsed-json). Raises on network
    errors; parse errors raise json.JSONDecodeError (classified permanent
    only when the body was a 2xx — a 4xx/5xx body is the status's fault)."""
    req = urllib.request.Request(
        url, headers={"User-Agent": "aerial-listening/1.0",
                      "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, None


class FetchError(Exception):
    """A non-2xx response or a network/timeout failure (transient unless
    the status is 404/410). Carries the HTTP status for classification."""

    def __init__(self, message: str, status=None) -> None:
        super().__init__(message)
        self.status = status


class GithubRepoAdapter:
    """Polls releases + pushes for one ``github_repo`` / ``github_pushes``
    source and emits normalized markdown notes.

    Inject ``fetch`` (``url -> (status, json)``) for tests; production
    uses GitHub's REST API. Notes land in a staging folder whose name is
    a hash of the source id (finding 9: never target text)."""

    kind = "github_repo"

    def __init__(self, state: SourceState, fetch=None, now: float | None = None) -> None:
        self.state = state
        self.fetch = fetch or _http_fetch
        self.now = time.time() if now is None else now

    # -- events ---------------------------------------------------------

    def _fetch_events(self, source: dict) -> list[dict]:
        """All events since last_seen_ts, as
        ``{"event": "release"|"push", "ts": epoch, "title", "body", "url"}``."""
        target = source["target"]
        since = self.state.get(source["id"], "last_seen_ts") or self.now
        since_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since))
        watch = source.get("watch", ["releases"])
        events: list[dict] = []
        if "releases" in watch:
            status, data = self.fetch(
                f"https://api.github.com/repos/{target}/releases?per_page=50")
            self._raise_on_bad(status, data)
            for rel in (data or []):
                ts = parse_iso(rel.get("published_at"))
                if ts is None or ts <= since:
                    continue
                events.append({"event": "release", "ts": ts,
                               "title": rel.get("name") or rel.get("tag_name"),
                               "body": rel.get("body") or "",
                               "url": rel.get("html_url")})
        if "pushes" in watch:
            status, data = self.fetch(
                f"https://api.github.com/repos/{target}/commits?since={since_iso}&per_page=100")
            self._raise_on_bad(status, data)
            for c in (data or []):
                ts = parse_iso((c.get("commit") or {}).get("committer", {}).get("date"))
                if ts is None or ts <= since:
                    continue
                events.append({"event": "push", "ts": ts,
                               "title": ((c.get("commit") or {}).get("message")
                                         or "").splitlines()[0][:120],
                               "body": (c.get("commit") or {}).get("message") or "",
                               "url": c.get("html_url")})
        events.sort(key=lambda e: e["ts"])
        return events

    @staticmethod
    def _raise_on_bad(status, data) -> None:
        """Validate response shape before treating a poll as successful
        (finding 13): non-2xx raises FetchError; a 2xx that is not the
        expected list raises a schema error."""
        if status is None:
            raise FetchError("network/timeout error")
        if status in (404, 410):
            raise FetchError(f"HTTP {status}: gone/not found", status)
        if status != 200:
            raise FetchError(f"HTTP {status}", status)
        if not isinstance(data, list):
            raise ValueError("schema error: expected a JSON list")
        for item in data:
            if not isinstance(item, dict):
                raise ValueError("schema error: non-object element")

    # -- notes ------------------------------------------------------------

    def build_note(self, source: dict, event: dict, entity: str) -> str:
        """Normalized markdown note: @entity markers (source + repo),
        #tag from topic, front-matter sensitivity (from the source entry,
        never hardcoded — finding 10), published: timestamp, and a
        machine ``meta:`` line with source_kind + weight (G5) and
        ingest_ts (the digest window clock — finding 18)."""
        lines = [f"@entity:{entity}", f"@entity:{slugify(source['target'])}", ""]
        kind_label = "release" if event["event"] == "release" else "push"
        lines.append(f"# [{kind_label}] {event['title']}")
        lines.append("")
        if event.get("url"):
            lines.append(f"- url: {event['url']}")
        lines.append(f"- published: {now_iso(event['ts'])}")
        meta = {"source_kind": self.kind, "source_id": source["id"],
                "weight": source.get("weight", 0.8),
                "relation": source.get("relation", "competitor"),
                "ingest_ts": now_iso(self.now)}
        lines.append(f"- meta: {json.dumps(meta, sort_keys=True)}")
        lines.append(f"- source: {source['id']}")
        lines.append(f"- topic: {source['topic']}")
        lines.append(f"#{slugify(source['topic'])}")
        lines.append("")
        body = (event.get("body") or "").strip()
        if body:
            lines.append(body)
            lines.append("")
        lines.append(f"sensitivity: {self._sensitivity(source)}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _sensitivity(source: dict) -> str:
        """Sensitivity from the source entry (finding 10): ``private``
        default when relation is ``own``; otherwise the entry's
        ``sensitivity`` field or ``personal`` (conservative default —
        the adapter never stamps public)."""
        if source.get("relation") == "own":
            base = source.get("sensitivity", "private")
        else:
            base = source.get("sensitivity", "personal")
        # a present-but-blank label counts as MISSING -> same defaults
        if base is None or not str(base).strip():
            base = "private" if source.get("relation") == "own" else "personal"
        return base

    # -- one poll cycle -----------------------------------------------------

    def poll(self, source: dict, staging_root) -> dict:
        """One every_h cycle for one source. Returns a per-source report:
        {stored, suppressed, capped, failures: [...], level} — and writes
        matched notes into ``<staging_root>/<hash8>/``. A failed cycle
        neither re-ingests nor skips: last_seen_ts is only advanced on
        success, and the content-addressed vault makes a re-poll of the
        same events a dedupe no-op (G1)."""
        sid = source["id"]
        report = {"source": sid, "stored": 0, "suppressed": 0,
                  "capped": 0, "errors": [], "level": None}
        if not self.state.enabled(source):
            report["level"] = "disabled"
            return report
        backoff_until = self.state.get(sid, "backoff_until")
        if backoff_until and self.now < backoff_until:
            report["level"] = "backoff"
            return report
        self.state.init_source(source, self.now)
        staging = Path(staging_root) / hashlib.sha256(
            sid.encode()).hexdigest()[:8]
        try:
            events = self._fetch_events(source)
        except Exception as exc:  # noqa: BLE001 - classified below
            status = getattr(exc, "status", None)
            kind = self.state.classify_failure(status, exc)
            level = self.state.record_failure(sid, kind, f"{type(exc).__name__}: {exc}",
                                              self.now)
            report["level"] = level
            report["errors"].append({"kind": kind, "error": str(exc)})
            return report
        # success: reset the failure machine
        max_ts = max((e["ts"] for e in events), default=None)
        age_limit = self.now - MAX_PUBLISHED_AGE_DAYS * 86400
        staging.mkdir(parents=True, exist_ok=True)
        entity = slugify(sid)
        written = 0
        for event in events:
            if written >= NOTE_CAP_PER_CYCLE:
                report["capped"] += 1
                continue
            text = f"{event.get('title') or ''}\n{event.get('body') or ''}"
            if not item_matches(source.get("match"), text):
                continue
            if event["ts"] < age_limit:
                # finding 18: stale published age — suppressed, not ingested
                report["suppressed"] += 1
                continue
            try:
                note = self.build_note(source, event, entity)
                name = f"{int(event['ts'])}-{slugify(event['title'])}.md"
                (staging / name).write_text(note, encoding="utf-8")
            except Exception as exc:  # per-event isolation (finding 15)
                report["errors"].append({"event": event.get("title"),
                                         "error": f"{type(exc).__name__}: {exc}"})
                continue
            written += 1
        report["stored"] = written
        self.state.record_success(sid, max_ts)
        return report


class GithubRepoIngestAdapter(IngestAdapter):
    """Registry adapter (registration-only by design): ``ingest`` polls
    one github source ref (a dict entry) and hands the staging folder to
    the Curator's existing folder loop."""

    kind = "github_repo"

    def __init__(self, vault, curator, state: SourceState, fetch=None,
                 now: float | None = None) -> None:
        self.vault = vault
        self.curator = curator
        self.poller = GithubRepoAdapter(state, fetch=fetch, now=now)

    def ingest(self, source_ref) -> list[str]:
        report = self.poll_source(source_ref)
        return report["hashes"]

    def poll_source(self, source_ref, staging_root=None) -> dict:
        staging_root = staging_root or (
            Path(self.vault.root).parent / "listening" / "staging")
        report = self.poller.poll(source_ref, staging_root)
        staging = Path(staging_root) / hashlib.sha256(
            source_ref["id"].encode()).hexdigest()[:8]
        folder_report = self.curator.ingest_folder(staging)
        report["folder_report"] = dict(folder_report)
        report["hashes"] = list(folder_report["stored"])
        return report


def register_listening_adapters(registry, vault, curator, state: SourceState,
                                fetch=None, now: float | None = None) -> None:
    """Register the listening adapters in the ingest Registry — adding a
    source kind is a registration, not a rewrite (t5 §1.1)."""
    registry.register(GithubRepoIngestAdapter(
        vault, curator, state, fetch=fetch, now=now))


# --- the ingest plumbing: one cycle over all sources -------------------------

def run_cycle(sources_path, gateway_root: str, *, fetch=None,
              now: float | None = None, state_path=None) -> dict:
    """One listening cycle: load + validate sources, poll every enabled
    github source (skipping those in backoff), ingest notes through the
    Curator, and produce the digest. The digest windows on ``ingest_ts``
    (finding 18); failure rows follow findings 12–13."""
    root = Path(gateway_root)
    now = time.time() if now is None else now
    state = SourceState(state_path or root / "listening" / "state.json")
    sources = load_sources(sources_path)
    vault = L0Vault(root / "l0")
    ws = Workspace(root / "l1")
    ws.load()
    curator = Curator(vault, ws)
    adapter = GithubRepoAdapter(state, fetch=fetch, now=now)
    staging_root = root / "listening" / "staging"

    reports, all_hashes = [], []
    for source in sources:
        if source["kind"] not in ("github_repo", "github_pushes"):
            continue
        staging = staging_root / hashlib.sha256(
            source["id"].encode()).hexdigest()[:8]
        # staging is transient: clear it so a re-run cannot re-ingest old
        # files (the vault would dedupe them, but the report counts files)
        if staging.exists():
            for old in staging.glob("*.md"):
                old.unlink()
        rep = adapter.poll(source, staging_root)
        if rep["level"] not in ("disabled", "backoff"):
            staging = staging_root / hashlib.sha256(
                source["id"].encode()).hexdigest()[:8]
            folder_report = curator.ingest_folder(staging)
            rep["folder_report"] = dict(folder_report)
            all_hashes.extend(folder_report["stored"])
        reports.append(rep)
    curator.build_cluster_artifacts()
    ws.publish("main", vault)
    ws.save()
    state.save()

    digest = build_listening_digest(state, sources, reports, now=now)
    digest_path = root / "listening" / "digest-latest.json"
    digest_path.parent.mkdir(parents=True, exist_ok=True)
    digest_path.write_text(json.dumps(digest, indent=1), encoding="utf-8")
    return {"reports": reports, "ingested": len(all_hashes), "digest": digest,
            "digest_path": str(digest_path)}


def build_listening_digest(state: SourceState, sources: list[dict],
                           reports: list[dict], *,
                           since_ts: str | None = None, now: float | None = None) -> dict:
    """Listening digest (spec §4): windows on ingest_ts; failure rows per
    findings 12–13 — one digest line at 2 consecutive failures, a Routine
    queue row with error evidence at 4, one row + auto-disable on a
    permanent failure. ``published`` rides along as a staleness attribute."""
    now = time.time() if now is None else now
    by_source = {r["source"]: r for r in reports}
    failures, routine, disabled = [], [], []
    for source in sources:
        sid = source["id"]
        n = state.get(sid, "consecutive_failures") or 0
        err = state.get(sid, "last_error") or {}
        row = {"source": sid, "failures": n, "error": err.get("error"),
               "ts": err.get("ts")}
        if state.get(sid, "disabled"):
            disabled.append(row)
        elif n >= ROUTINE_FAILURES:
            routine.append({**row, "class": "routine",
                            "evidence": state.get(sid, "last_error")})
        elif n >= DIGEST_FAILURES:
            failures.append(row)
    ingested_rows = []
    for source in sources:
        rep = by_source.get(source["id"])
        if not rep:
            continue
        ingested_rows.append({
            "source": source["id"], "stored": rep["stored"],
            "suppressed_stale": rep["suppressed"],
            "capped": rep["capped"],
            "ingest_ts": now_iso(now)})
    return {"built_at": now_iso(now), "window": "ingest_ts",
            "ingested": ingested_rows,
            "failures": failures, "routine_queue": routine,
            "disabled": disabled,
            "waits_for_you": bool(failures or routine or disabled)}
