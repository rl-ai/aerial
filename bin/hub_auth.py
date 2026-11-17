#!/usr/bin/env python3
"""hub_auth.py -- bearer-token support for the ticket hub (stdlib only).

The hub requires `Authorization: Bearer <token>` on every request. The token
lives in a file, never in an env var and never in code:

  $HUB_TOKEN_FILE  explicit path; when set (non-empty) it is authoritative —
                   unreadable or empty content raises HubAuthError instead of
                   falling back to the default.
  ~/.config/aiops/hub.token   DEFAULT_TOKEN_FILE, used only when it exists
                   and HUB_TOKEN_FILE is unset/empty; no file -> no header.

Security contract: the token value never appears in any exception message,
log line, or return value other than the Authorization header dict built by
bearer_header(). Read AT CALL TIME, so a rotated token file takes effect
without restarting callers.
"""
from __future__ import annotations

import os

DEFAULT_TOKEN_FILE = "~/.config/aiops/hub.token"
#: Since 2026-11-15 the hub refuses writes from read-class tokens (403
#: read_only_token): non-GET requests use this write token when it exists.
DEFAULT_WRITE_TOKEN_FILE = "~/.config/aiops/handoff/ops-write.token"
READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class HubAuthError(RuntimeError):
    """Token file configured but unusable (unreadable or empty).

    The message names HUB_TOKEN_FILE and the path — never the contents.
    """


def token_file(method: str = "GET") -> str | None:
    """Path to read the bearer token from, or None when no token is configured.

    HUB_TOKEN_FILE, when set, is used for every method. Otherwise a write
    (any method but GET, HEAD, OPTIONS) uses HUB_WRITE_TOKEN_FILE or the
    default write token file when present, and everything else the default
    read token file.
    """
    env = os.environ.get("HUB_TOKEN_FILE", "").strip()
    if env:
        return env
    if (method or "GET").upper() not in READ_METHODS:
        wenv = os.environ.get("HUB_WRITE_TOKEN_FILE", "").strip()
        if wenv:
            return wenv
        wdefault = os.path.expanduser(DEFAULT_WRITE_TOKEN_FILE)
        if os.path.isfile(wdefault):
            return wdefault
    default = os.path.expanduser(DEFAULT_TOKEN_FILE)
    if os.path.isfile(default):
        return default
    return None


def bearer_header(method: str = "GET") -> dict:
    """{"Authorization": "Bearer <token>"} when a token file is configured.

    {} when none is (no HUB_TOKEN_FILE, no default file on disk). Raises
    HubAuthError when HUB_TOKEN_FILE is set but the file is unreadable or
    its content is empty/whitespace-only. The exception never carries the
    token value.
    """
    path = token_file(method)
    if path is None:
        return {}
    from_env = path in {os.environ.get("HUB_TOKEN_FILE", "").strip(),
                        os.environ.get("HUB_WRITE_TOKEN_FILE", "").strip()} - {""}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            token = fh.read().strip()
    except OSError as exc:
        if from_env:
            raise HubAuthError(
                "HUB_TOKEN_FILE is set to %s but the file could not be read (%s)"
                % (path, type(exc).__name__)) from None
        return {}
    if not token and from_env:
        raise HubAuthError("HUB_TOKEN_FILE is set to %s but the file is empty"
                           % path)
    if not token:
        return {}
    return {"Authorization": "Bearer " + token}
