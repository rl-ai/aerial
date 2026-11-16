"""Lane B — snapshot store: last-known-good payloads persisted with read timestamp.

Every panel wraps its reads in SnapshotStore so that on hub/API failure it can
degrade to stale-with-timestamp (D2), and on first load with no prior successful
read it renders explicit 'unavailable' — never blank or fabricated data.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Any, Dict, Optional

UNAVAILABLE = "unavailable"


class SnapshotStore:
    """Persists {payload, read_ts} per panel key in a JSON file."""

    def __init__(self, path: str):
        self.path = path
        self._mem: Dict[str, Dict[str, Any]] = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self._mem = json.load(f)
            except (json.JSONDecodeError, OSError):
                self._mem = {}

    def _atomic_write(self) -> None:
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d or ".", prefix=".snapshot-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._mem, f)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def save(self, key: str, payload: Any, read_ts: Optional[float] = None) -> None:
        """Record a last-known-good payload with its read timestamp."""
        self._mem[key] = {"payload": payload, "read_ts": read_ts if read_ts is not None else time.time()}
        self._atomic_write()

    def get_stale(self, key: str) -> Dict[str, Any]:
        """Return {'state': 'stale'|'unavailable', 'payload', 'read_ts'}.

        stale: a prior successful read exists — render payload with its timestamp.
        unavailable: no prior successful read — render explicit 'unavailable'.
        """
        entry = self._mem.get(key)
        if entry is None:
            return {"state": UNAVAILABLE, "payload": None, "read_ts": None}
        return {
            "state": "stale",
            "payload": entry.get("payload"),
            "read_ts": entry.get("read_ts"),
        }

    def read_ts(self, key: str) -> Optional[float]:
        entry = self._mem.get(key)
        return entry.get("read_ts") if entry else None
