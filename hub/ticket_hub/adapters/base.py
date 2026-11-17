"""Chat adapter base (DESIGN.md §Chat bridges).

Contract:
    ChatAdapter.post_update(ticket, digest) -> msg_id
    ChatAdapter.on_inbound(raw) -> Event dict (kind=thread_message)
    ChatAdapter.register_mapping(...)          # ThreadBinding: ticket <-> thread

Security: tokens live only in the runtime config file (default
``$TICKET_HUB_HOME/adapters.json``), never in the repo. An adapter whose
config file (or platform section, or token) is absent constructs in **inert
mode**: it records outbound posts in ``self.outbox`` and maps inbound events,
but performs no network I/O and needs no credentials.
"""

from __future__ import annotations

import json
import os
import urllib.request

from ticket_hub.paths import hub_path

DEFAULT_CONFIG_PATH = hub_path("adapters.json")

__all__ = ["ChatAdapter", "DEFAULT_CONFIG_PATH"]


class ChatAdapter:
    """Base class shared by Matrix / Discord / Slack adapters and FakeAdapter."""

    platform = "base"

    def __init__(self, config_path: "str | None" = DEFAULT_CONFIG_PATH,
                 config=None, http_call=None):
        # ``config`` allows direct injection (tests); otherwise load from
        # ``config_path``. Absent/incomplete config => inert mode, never raise.
        self.config_path = os.path.expanduser(str(config_path)) \
            if config_path else None
        self._http_call = http_call  # injectable for tests; no network otherwise
        self.outbox = []             # recorded outbound posts (also inert sink)
        self._bindings = {}          # ticket_id -> ThreadBinding dict
        self._user_map = {}          # platform chat id -> Actor name
        self._counter = 0

        cfg = config
        if cfg is None:
            cfg = self._load_config()
        section = (cfg or {}).get(self.platform) or {}
        self._config = section
        self._token = section.get("token")
        self.inert = not self._token
        self._user_map = dict(section.get("user_map") or {})

    # -- config ------------------------------------------------------------
    def _load_config(self):
        if not self.config_path:
            return {}
        try:
            with open(self.config_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            # Missing or malformed config -> inert mode (DESIGN.md: inert w/o config).
            return {}

    # -- ThreadBinding / mappings -------------------------------------------
    def register_mapping(self, ticket_id, room_id=None, thread_id=None,
                         root_msg_id=None, parent_ticket=None):
        """Register a ThreadBinding (DESIGN.md object model).

        ``parent_ticket`` records lineage so a child ticket's updates post as
        subthread replies under the parent's root message.
        """
        binding = {
            "ticket_id": ticket_id,
            "platform": self.platform,
            "room_id": room_id,
            "thread_id": thread_id,
            "root_msg_id": root_msg_id,
            "parent_ticket": parent_ticket,
        }
        self._bindings[ticket_id] = binding
        return binding

    def binding_for(self, ticket_id):
        return self._bindings.get(ticket_id)

    def resolve_actor(self, chat_id):
        """Resolve a platform chat id to an actor name via the chat-id map."""
        return self._user_map.get(chat_id, str(chat_id))

    # -- outbound -----------------------------------------------------------
    def post_update(self, ticket, digest):
        """Post digest bullets to the ticket's bound thread. Returns msg_id.

        Ticket -> thread root; **child ticket -> subthread reply under the
        parent's root message** (DESIGN.md §Chat bridges). Inert mode records
        the post in ``self.outbox`` and returns a synthetic ``inert-<n>`` id.
        """
        ticket_id = ticket.get("id") if isinstance(ticket, dict) else ticket
        bullets = (digest or {}).get("bullets", [])
        text = self._format_markdown(bullets)
        binding = self.binding_for(ticket_id) or {}
        # Subthread semantics: reply under the PARENT's root when this ticket
        # is a child and the parent has a binding with a root message.
        parent_ticket = (
            ticket.get("parent") if isinstance(ticket, dict) else None
        ) or binding.get("parent_ticket")
        reply_to = None
        if parent_ticket:
            parent_binding = self.binding_for(parent_ticket)
            if parent_binding:
                # Slack threads anchor on thread_ts (thread_id); Matrix/Discord
                # on the root message id.
                reply_to = (parent_binding.get("root_msg_id")
                            or parent_binding.get("thread_id"))

        record = {
            "ticket_id": ticket_id,
            "text": text,
            "reply_to": reply_to,
            "binding": binding or None,
            "inert": self.inert,
        }
        if self.inert:
            self._counter += 1
            msg_id = f"inert-{self.platform}-{self._counter}"
            record["msg_id"] = msg_id
            self.outbox.append(record)
            return msg_id

        msg_id = self._send(text, binding, reply_to)
        record["msg_id"] = msg_id
        self.outbox.append(record)
        return msg_id

    @staticmethod
    def _format_markdown(bullets):
        """Outbound posts = digest bullets as markdown."""
        return "\n".join(f"- {b.get('text', '')}" for b in bullets) or "- (no updates)"

    def _send(self, text, binding, reply_to):
        """Platform-specific send. Subclasses override using self._request()."""
        raise NotImplementedError

    # -- inbound ------------------------------------------------------------
    def on_inbound(self, raw):
        """Map a platform event to a thread_message Event dict.

        Subclasses override ``_parse_inbound``; actor is resolved by chat-id map.
        """
        parsed = self._parse_inbound(raw)
        return {
            "ticket_id": parsed["ticket_id"],
            "ts": parsed["ts"],
            "actor": {"name": parsed["actor"], "kind": "human",
                      "family": self.platform},
            "kind": "thread_message",
            "payload": {
                "body": parsed["body"],
                "chat_user": parsed["chat_user"],
                "msg_id": parsed["msg_id"],
                "thread_id": parsed["thread_id"],
                "room_id": parsed.get("room_id"),
            },
        }

    def _parse_inbound(self, raw):
        raise NotImplementedError

    # -- HTTP (lazy; injectable; no import-time or inert-path network) -------
    def _request(self, method, url, headers=None, body=None):
        """Single choke point for platform HTTP. ``http_call(method, url,
        headers, body)`` injected in tests keeps everything offline."""
        if self._http_call is not None:
            return self._http_call(method, url, headers or {}, body)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
