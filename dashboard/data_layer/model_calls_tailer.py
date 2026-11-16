"""Lane B — model-calls.jsonl tailer.

Tails the ledger every 5 minutes from a PERSISTED byte offset plus a per-item
aggregate checkpoint. Rules:
  - advance the offset only to the last NEWLINE-TERMINATED byte; a partially
    written trailing line is left for the next tail cycle (never split or
    double-counted — rev2 #4 / L4);
  - malformed or untagged lines are bucketed under an explicit 'untagged' item
    and never silently dropped;
  - a periodic full-scan verification hook recomputes totals from scratch and
    asserts they equal the checkpoint totals (L4).
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

from deployment_profile import pvalue as _pv
DEFAULT_LEDGER = str(_pv("model_calls_ledger"))
DEFAULT_STATE = os.path.expanduser("~/.hermes/dashboard/model_calls_tail_state.json")


class ModelCallsTailer:
    def __init__(self, ledger_path: str = DEFAULT_LEDGER, state_path: str = DEFAULT_STATE):
        self.ledger_path = ledger_path
        self.state_path = state_path
        self.offset = 0
        self.checkpoint: Dict[str, Dict[str, int]] = {}  # item -> {calls, in_tokens, out_tokens}
        self.untagged = {"calls": 0, "in_tokens": 0, "out_tokens": 0}
        self._load_state()

    # ---- persisted state ----------------------------------------------------

    def _load_state(self) -> None:
        if not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                st = json.load(f)
            self.offset = int(st.get("offset", 0))
            self.checkpoint = st.get("checkpoint", {})
            self.untagged = st.get("untagged", {"calls": 0, "in_tokens": 0, "out_tokens": 0})
        except (json.JSONDecodeError, OSError, ValueError):
            # Corrupt state -> full rescan from zero (safe, append-only ledger).
            self.offset = 0
            self.checkpoint = {}
            self.untagged = {"calls": 0, "in_tokens": 0, "out_tokens": 0}

    def save_state(self) -> None:
        d = os.path.dirname(self.state_path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"offset": self.offset, "checkpoint": self.checkpoint, "untagged": self.untagged}, f)
        os.replace(tmp, self.state_path)

    # ---- tailing -------------------------------------------------------------

    def tail(self, max_bytes: Optional[int] = None) -> Dict[str, Any]:
        """Consume all newline-terminated lines from the persisted offset.

        Returns the per-item delta this cycle. The offset advances only to the
        last '\n'; a truncated trailing line is re-read next cycle.
        """
        if not os.path.exists(self.ledger_path):
            return {"items": {}, "untagged": {"calls": 0, "in_tokens": 0, "out_tokens": 0}, "lines": 0}
        delta: Dict[str, Dict[str, int]] = {}
        d_untagged = {"calls": 0, "in_tokens": 0, "out_tokens": 0}
        lines = 0
        with open(self.ledger_path, "rb") as f:
            f.seek(self.offset)
            chunk = f.read(max_bytes) if max_bytes else f.read()
            if not chunk:
                return {"items": {}, "untagged": d_untagged, "lines": 0}
            last_nl = chunk.rfind(b"\n")
            if last_nl == -1:
                # Only a partial line so far — consume nothing this cycle.
                return {"items": {}, "untagged": d_untagged, "lines": 0}
            complete = chunk[: last_nl + 1]
            new_offset = self.offset + last_nl + 1
            for raw in complete.splitlines():
                lines += 1
                item = self._item_of(raw)
                if item is None:
                    d_untagged["calls"] += 1
                else:
                    bucket = delta.setdefault(item, {"calls": 0, "in_tokens": 0, "out_tokens": 0})
                    bucket["calls"] += 1
            # token counts are folded in per line below (single pass)
            self.offset = new_offset
            self._fold(complete, delta, d_untagged)
        # fold delta into checkpoint
        for item, b in delta.items():
            ck = self.checkpoint.setdefault(item, {"calls": 0, "in_tokens": 0, "out_tokens": 0})
            for k in ck:
                ck[k] += b[k]
        for k in self.untagged:
            self.untagged[k] += d_untagged[k]
        self.save_state()
        return {"items": delta, "untagged": d_untagged, "lines": lines}

    def _item_of(self, raw: bytes) -> Optional[str]:
        try:
            rec = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None  # malformed -> untagged bucket
        if not isinstance(rec, dict):
            return None
        item = rec.get("item") or rec.get("item_tag") or rec.get("task")
        if not item or not isinstance(item, str):
            return None
        return item

    def _fold(self, complete: bytes, delta: Dict[str, Dict[str, int]], d_untagged: Dict[str, int]) -> None:
        for raw in complete.splitlines():
            item = self._item_of(raw)
            tin = tout = 0
            try:
                rec = json.loads(raw.decode("utf-8"))
                if isinstance(rec, dict):
                    tin = int(rec.get("in_tokens") or rec.get("input_tokens") or 0)
                    tout = int(rec.get("out_tokens") or rec.get("output_tokens") or 0)
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError):
                pass
            if item is None:
                d_untagged["in_tokens"] += tin
                d_untagged["out_tokens"] += tout
            else:
                b = delta.setdefault(item, {"calls": 0, "in_tokens": 0, "out_tokens": 0})
                b["in_tokens"] += tin
                b["out_tokens"] += tout

    # ---- verification ----------------------------------------------------------

    def verify_full_scan(self) -> Dict[str, Any]:
        """L4: recompute totals from a full scan; compare to checkpoint."""
        full: Dict[str, Dict[str, int]] = {}
        f_untagged = {"calls": 0, "in_tokens": 0, "out_tokens": 0}
        if os.path.exists(self.ledger_path):
            with open(self.ledger_path, "rb") as f:
                data = f.read()
            complete = data[: data.rfind(b"\n") + 1] if b"\n" in data else b""
            for raw in complete.splitlines():
                item = self._item_of(raw)
                tin = tout = 0
                try:
                    rec = json.loads(raw.decode("utf-8"))
                    if isinstance(rec, dict):
                        tin = int(rec.get("in_tokens") or rec.get("input_tokens") or 0)
                        tout = int(rec.get("out_tokens") or rec.get("output_tokens") or 0)
                except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError):
                    pass
                if item is None:
                    f_untagged["calls"] += 1
                    f_untagged["in_tokens"] += tin
                    f_untagged["out_tokens"] += tout
                else:
                    b = full.setdefault(item, {"calls": 0, "in_tokens": 0, "out_tokens": 0})
                    b["calls"] += 1
                    b["in_tokens"] += tin
                    b["out_tokens"] += tout
        match = (full == self.checkpoint and f_untagged == self.untagged)
        return {"match": match, "full_scan": full, "full_untagged": f_untagged,
                "checkpoint": dict(self.checkpoint), "untagged": dict(self.untagged)}

    # ---- reporting helpers -------------------------------------------------------

    def totals_by_item(self) -> Dict[str, Dict[str, int]]:
        out = dict(self.checkpoint)
        out["untagged"] = dict(self.untagged)  # explicit bucket, never dropped
        return out
