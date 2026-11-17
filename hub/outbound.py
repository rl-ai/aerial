"""Outbound adapter daemon (Lane OUTBOUND).

Polls the ticket-hub HTTP API and posts translated digest updates to bound
chat threads through the Lane C adapters (ticket_hub/adapters/).

Behavior per tick:
  1. GET /board
  2. for each ticket whose ``updated_at`` is newer than its stored watermark:
     build the canonical digest via GET /tickets/{id}/digest?format=json&full=true;
     skip when the digest cites no event ids beyond the already-posted set
     (dedupe across ticks; ``updated_at`` alone never re-posts);
  3. resolve adapters for the ticket's bindings via GET /tickets/{id}/bindings;
  4. call ``adapter.post_update(ticket, digest)`` on each bound platform;
  5. advance the watermark + posted-event set only after ALL posts succeed.

State: JSON at ``state_path``, atomic-written (tmp + os.replace).

Inert-safe: with no config file (or an empty one) the poller constructs and
ticks with zero adapters, zero posts, zero crashes — nothing here touches the
network unless a hub URL is reachable and a platform section has a token.

Polish glue: an optional ``llm_call`` is passed to
``ticket_hub.translate.polish_pass`` (template output is the fallback and the
truth source). ``llm_from_env()`` builds one from TICKET_HUB_POLISH_URL /
TICKET_HUB_POLISH_MODEL / TICKET_HUB_POLISH_KEY_FILE; when any is absent it
returns None and the template digest ships unchanged. The bearer key is read
from the key file at call time and never logged.

Run:
    python outbound.py [--once] [--interval N] [--config PATH] [--state PATH]
                       [--url URL]
"""

from __future__ import annotations

import json
import os
import time
import urllib.request

# Shared bearer-token header reader (ACCESS-P1-CONTRACT §4): reads the file
# named by HUB_TOKEN_FILE at call time. Import is side-effect free and
# stdlib-only. The polish model call keeps its own key
# (TICKET_HUB_POLISH_KEY_FILE) and must NOT use this.
from hub_client import bearer_header

from ticket_hub.adapters import (
    DEFAULT_CONFIG_PATH,
    DiscordAdapter,
    FakeAdapter,
    MatrixAdapter,
    SlackAdapter,
)
from ticket_hub.paths import hub_path
from ticket_hub.translate import polish_pass

DEFAULT_STATE_PATH = hub_path("outbound_state.json")
DEFAULT_HUB_URL = os.environ.get("TICKET_HUB_URL", "http://127.0.0.1:8777")

# Platform -> adapter class (Lane C). The poller never forks these; it reuses
# them exactly as built, including their inert-without-token behavior.
ADAPTER_CLASSES = {
    "matrix": MatrixAdapter,
    "discord": DiscordAdapter,
    "slack": SlackAdapter,
    "fake": FakeAdapter,
}

__all__ = [
    "HubClient",
    "OutboundPoller",
    "llm_from_env",
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_STATE_PATH",
    "DEFAULT_HUB_URL",
]


# ---------------------------------------------------------------------------
# Hub HTTP client (thin; tests inject a stub with the same three methods)
# ---------------------------------------------------------------------------

class HubClient:
    """Minimal read-only HTTP client for the ticket-hub API."""

    def __init__(self, base_url: str = DEFAULT_HUB_URL, http_get=None):
        self.base_url = base_url.rstrip("/")
        self._http_get = http_get  # injectable callable(url, headers) -> parsed JSON

    def _get(self, path: str):
        url = f"{self.base_url}{path}"
        headers = bearer_header()  # read at call time (HUB_TOKEN_FILE)
        if self._http_get is not None:
            return self._http_get(url, headers)
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def get_board(self) -> dict:
        return self._get("/board")

    def get_digest(self, ticket_id: str, full: bool = True) -> dict:
        q = "?format=json" + ("&full=true" if full else "")
        return self._get(f"/tickets/{ticket_id}/digest{q}")

    def get_bindings(self, ticket_id: str) -> list:
        return self._get(f"/tickets/{ticket_id}/bindings") or []


# ---------------------------------------------------------------------------
# Polish glue (GLM via OpenAI-compatible chat completions; env-driven)
# ---------------------------------------------------------------------------

def llm_from_env(env=None):
    """Build a polish llm_call from env, or None when config is absent.

    Env:
      TICKET_HUB_POLISH_URL      OpenAI-compatible /chat/completions base
      TICKET_HUB_POLISH_MODEL    model name
      TICKET_HUB_POLISH_KEY_FILE path to bearer key (read AT CALL TIME only)

    Never logs the key or the request headers. Any call failure raises —
    polish_pass treats that as fallback-to-template.
    """
    env = os.environ if env is None else env
    url = env.get("TICKET_HUB_POLISH_URL")
    model = env.get("TICKET_HUB_POLISH_MODEL")
    key_file = env.get("TICKET_HUB_POLISH_KEY_FILE")
    if not (url and model and key_file):
        return None

    completions_url = url.rstrip("/")
    if not completions_url.endswith("/chat/completions"):
        completions_url += "/chat/completions"

    def _call(digest: dict) -> str:
        with open(key_file, "r", encoding="utf-8") as fh:
            key = fh.read().strip()
        prompt = (
            "Rewrite this ticket digest as a product update: "
            "≤3 bullets, no internal jargon. Keep every cited event id.\n"
            + json.dumps(digest.get("bullets", []), ensure_ascii=False)
        )
        body = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")
        req = urllib.request.Request(completions_url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {key}")
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return payload["choices"][0]["message"]["content"]

    return _call


# ---------------------------------------------------------------------------
# Poller
# ---------------------------------------------------------------------------

class OutboundPoller:
    """Polls the hub and posts digest updates through bound chat adapters."""

    def __init__(self, client_base_url: "str | None" = None, *, client=None,
                 config_path: str = DEFAULT_CONFIG_PATH,
                 state_path: str = DEFAULT_STATE_PATH,
                 interval_s: float = 60.0,
                 adapters: "dict | None" = None,
                 llm_call=None):
        # Either a base URL (construct HubClient) or an injected client/stub.
        if client is None:
            client = HubClient(client_base_url or DEFAULT_HUB_URL)
        self.client = client
        self.config_path = os.path.expanduser(str(config_path)) if config_path else None
        self.state_path = os.path.expanduser(str(state_path)) if state_path else None
        self.interval_s = float(interval_s)
        self.llm_call = llm_call  # None -> template digest unchanged (truth source)
        self.adapters = adapters if adapters is not None else self._build_adapters()
        self.state = self._load_state()
        self.posts = []  # (ticket_id, platform, msg_id) audit trail for this process

    # -- adapters -----------------------------------------------------------
    def _load_config(self) -> dict:
        if not self.config_path:
            return {}
        try:
            with open(self.config_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            # Missing/malformed config -> no adapters -> inert ticks (never raise).
            return {}

    def _build_adapters(self) -> dict:
        adapters = {}
        for platform, cls in ADAPTER_CLASSES.items():
            if platform not in self._load_config():
                continue
            try:
                adapters[platform] = cls(config_path=self.config_path)
            except Exception:
                # A broken adapter must not take the daemon down.
                continue
        return adapters

    # -- state (atomic JSON) --------------------------------------------------
    def _load_state(self) -> dict:
        if not self.state_path:
            return {"tickets": {}}
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {"tickets": {}}
        if not isinstance(data, dict) or not isinstance(data.get("tickets"), dict):
            return {"tickets": {}}
        return data

    def _save_state(self) -> None:
        if not self.state_path:
            return
        directory = os.path.dirname(self.state_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = f"{self.state_path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.state_path)

    # -- one pass --------------------------------------------------------------
    def tick(self) -> int:
        """One poll pass. Returns the number of tickets posted for.

        Hub-side failures (server down mid-restart) are logged-free no-ops:
        the daemon must survive them and retry on the next tick.
        """
        posted_count = 0
        try:
            board = self.client.get_board()
        except Exception:
            return 0
        columns = (board or {}).get("columns") or {}
        tickets = [t for col in columns.values() for t in (col or [])]
        changed = False
        for ticket in tickets:
            if not isinstance(ticket, dict) or not ticket.get("id"):
                continue
            try:
                did_post = self._process_ticket(ticket)
            except Exception:
                # Per-ticket hub/digest failure: skip this ticket this tick;
                # watermark untouched so the next tick retries.
                continue
            if did_post:
                posted_count += 1
            changed = True
        if changed:
            self._save_state()
        return posted_count

    def _process_ticket(self, ticket: dict) -> bool:
        ticket_id = ticket["id"]
        updated_at = ticket.get("updated_at")
        entry = self.state["tickets"].get(ticket_id) or {}
        watermark = entry.get("watermark")

        # Watermark gate: nothing newer -> nothing to even look at.
        if watermark is not None and (updated_at or "") <= watermark:
            return False

        digest = self.client.get_digest(ticket_id, full=True)
        digest = digest or {}
        digest_event_ids = {
            eid
            for bullet in digest.get("bullets", [])
            for eid in (bullet.get("event_ids") or [])
        }
        posted_ids = set(entry.get("posted_event_ids") or [])

        if not (digest_event_ids - posted_ids):
            # No NEW event ids since last post: never re-post on timestamp
            # churn alone. Advance the watermark so we stop re-digesting.
            if updated_at is not None:
                entry = dict(entry, watermark=updated_at)
                self.state["tickets"][ticket_id] = entry
            return False

        # Optional GLM polish; template digest is the fallback and truth source.
        digest = polish_pass(digest, self.llm_call)

        bindings = self.client.get_bindings(ticket_id) or []
        targets = []
        for binding in bindings:
            platform = (binding or {}).get("platform")
            adapter = self.adapters.get(platform)
            if adapter is not None:
                targets.append((platform, adapter, binding))
        if not targets:
            # No adapter for this ticket's bindings: post nothing and do NOT
            # advance the watermark (DESIGN: advance only after successful
            # posts) so binding it later still ships the pending update.
            return False

        try:
            for platform, adapter, binding in targets:
                adapter.register_mapping(
                    ticket_id,
                    room_id=binding.get("room_id"),
                    thread_id=binding.get("thread_id"),
                    root_msg_id=binding.get("root_msg_id"),
                )
                msg_id = adapter.post_update(ticket, digest)
                self.posts.append((ticket_id, platform, msg_id))
        except Exception:
            # Any post failure: watermark and posted-event set stay where they
            # were, so the next tick retries. (Already-succeeded platforms may
            # receive a duplicate on retry; acceptable at v0.)
            return False

        entry = {
            "watermark": updated_at if updated_at is not None else watermark,
            "posted_event_ids": sorted(posted_ids | digest_event_ids),
        }
        self.state["tickets"][ticket_id] = entry
        return True

    # -- loop ------------------------------------------------------------
    def run(self, once: bool = False, ticks: "int | None" = None,
            sleep=time.sleep):
        """Run ticks: ``once=True`` == one tick; ``ticks=N`` == N ticks;
        otherwise loop forever with ``interval_s`` sleeps."""
        limit = 1 if once else ticks
        i = 0
        while True:
            self.tick()
            i += 1
            if limit is not None and i >= limit:
                return i
            sleep(self.interval_s)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="ticket-hub outbound daemon")
    parser.add_argument("--once", action="store_true", help="run one tick and exit")
    parser.add_argument("--interval", type=float, default=60.0,
                        help="seconds between ticks (default 60)")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH,
                        help="adapters.json path")
    parser.add_argument("--state", default=DEFAULT_STATE_PATH,
                        help="outbound_state.json path")
    parser.add_argument("--url", default=DEFAULT_HUB_URL,
                        help="ticket-hub base URL")
    args = parser.parse_args(argv)

    poller = OutboundPoller(
        args.url,
        config_path=args.config,
        state_path=args.state,
        interval_s=args.interval,
        llm_call=llm_from_env(),  # None when polish env is absent
    )
    poller.run(once=args.once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
