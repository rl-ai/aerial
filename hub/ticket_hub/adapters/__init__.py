"""Chat bridge adapters (DESIGN.md §Chat bridges).

Tokens live only in the runtime config (default
``$TICKET_HUB_HOME/adapters.json``) — never in this repo. All adapters
import and construct with zero credentials (inert mode).
"""

from __future__ import annotations

from .base import ChatAdapter, DEFAULT_CONFIG_PATH
from .discord import DiscordAdapter
from .fake import FakeAdapter
from .matrix import MatrixAdapter
from .slack import SlackAdapter

__all__ = [
    "ChatAdapter",
    "DEFAULT_CONFIG_PATH",
    "DiscordAdapter",
    "FakeAdapter",
    "MatrixAdapter",
    "SlackAdapter",
]
