"""Lane B — hub client for the operator dashboard.

Reads go through the READ-ONLY token only. The decision-scoped write token is
exposed as a separate credential and is used exclusively by write methods
(approve/dismiss/escalate, mandate writes) — never by reads (spec §3, rev2 #1).

Failure matrix (D2): 401, 403 (expired/rotated token), 500, timeout → the caller
degrades the panel to the snapshot store's stale-with-timestamp; a first load
with no prior successful read renders 'unavailable'. This client raises
HubReadError so the caller can decide; it never fabricates data.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from typing import Any, Dict, Optional


class HubReadError(Exception):
    """Raised when a hub read fails (auth, server, timeout, connection)."""


class HubWriteError(Exception):
    """Raised when a hub write fails (Q4 path — Lane A handles retry policy)."""


class HubClient:
    HUB = "http://127.0.0.1:8020"

    def __init__(self, read_token: str, write_token: str, base_url: Optional[str] = None, timeout: float = 5.0):
        self.read_token = read_token
        self.write_token = write_token
        self.base_url = (base_url or self.HUB).rstrip("/")
        self.timeout = timeout

    # ---- internals -------------------------------------------------------

    def _request(self, path: str, token: str, method: str = "GET", body: Optional[Dict] = None) -> Any:
        url = f"{self.base_url}{path}"
        data = None
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if body is not None:
            data = _json_encode(body)
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return _json_decode(raw)
        except urllib.error.HTTPError as e:
            raise HubReadError(f"{method} {path} -> HTTP {e.code}") if method == "GET" else HubWriteError(
                f"{method} {path} -> HTTP {e.code}"
            ) from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if method == "GET":
                raise HubReadError(f"{method} {path} -> {type(e).__name__}: {e}") from e
            raise HubWriteError(f"{method} {path} -> {type(e).__name__}: {e}") from e

    # ---- reads (read-only token ONLY) ------------------------------------

    def get_findings(self) -> Any:
        return self._request("/api/findings", self.read_token)

    def get_decisions(self) -> Any:
        return self._request("/api/decisions", self.read_token)

    def get_mandates(self) -> Any:
        return self._request("/api/mandates", self.read_token)

    def get_sessions(self) -> Any:
        return self._request("/api/dashboard-sessions", self.read_token)

    # ---- writes (decision-scoped write token ONLY) ------------------------

    def approve(self, row_id: str, mandate_id: Optional[str] = None, idempotency_key: Optional[str] = None) -> Any:
        body: Dict[str, Any] = {"row_id": row_id}
        if mandate_id is not None:
            body["mandate_id"] = mandate_id
        if idempotency_key is not None:
            body["idempotency_key"] = idempotency_key
        return self._request("/api/decisions/approve", self.write_token, method="POST", body=body)

    def dismiss(self, row_id: str, reason: Optional[str] = None) -> Any:
        # reason=None -> hub records reason 'unspecified' (Q7/C2)
        return self._request("/api/decisions/dismiss", self.write_token, method="POST",
                             body={"row_id": row_id, "reason": reason or "unspecified"})

    def escalate(self, row_id: str) -> Any:
        return self._request("/api/decisions/escalate", self.write_token, method="POST", body={"row_id": row_id})

    def create_mandate(self, scope: Dict, expiry: str, row_id: Optional[str] = None) -> Any:
        body: Dict[str, Any] = {"scope": scope, "expiry": expiry}
        if row_id is not None:
            body["row_id"] = row_id
        return self._request("/api/mandates", self.write_token, method="POST", body=body)


def _json_encode(obj: Dict) -> bytes:
    import json
    return json.dumps(obj).encode("utf-8")


def _json_decode(raw: bytes) -> Any:
    import json
    return json.loads(raw.decode("utf-8") or "null")
