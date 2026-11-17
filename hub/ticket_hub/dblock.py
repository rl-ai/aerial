"""One lock for the hub's shared SQLite connection.

The API serves requests on a thread pool, and the auth middleware reads on the
event-loop thread, all through one sqlite3 connection opened with
``check_same_thread=False``. Interleaved statements on that connection handed
one request another request's rows (fix batch item 9,
TK-EXAMPLE). Every method of the classes that use the
connection runs under this re-entrant lock, so each runs whole: its
statements, its fetches and its commit.
"""

from __future__ import annotations

import functools
import inspect
import threading

DB_LOCK = threading.RLock()


def _locked(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with DB_LOCK:
            return fn(*args, **kwargs)
    return wrapper


def serialized(cls):
    """Class decorator: every plain, static and class method (``__init__``
    included) holds DB_LOCK."""
    for name, attr in list(vars(cls).items()):
        if name.startswith("__") and name != "__init__":
            continue
        if inspect.isfunction(attr):
            setattr(cls, name, _locked(attr))
        elif isinstance(attr, staticmethod):
            setattr(cls, name, staticmethod(_locked(attr.__func__)))
        elif isinstance(attr, classmethod):
            setattr(cls, name, classmethod(_locked(attr.__func__)))
    return cls
