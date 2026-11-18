#!/usr/bin/env python3
"""Entry-point shims for the Aerial console scripts.

The dashboard/ tree was written to run from the repo with
``PYTHONPATH="$PWD:$PWD/dashboard"``: its modules import siblings bare
(``from deployment_profile import value``, ``import render_daily``,
``from console.serve import ...``). Console-script entry points run with
only site-packages on sys.path, so the two dashboard commands go through
this shim: it puts the installed repo-root directory and the installed
dashboard/ directory on sys.path (root first, matching the documented
PYTHONPATH order) and then calls the real ``main``.

Nothing is moved or renamed; ``dashboard.serve_console.main`` and
``dashboard.followup.runner.main`` stay the source of truth.
"""
from __future__ import annotations

import os
import sys


def _setup_path() -> None:
    import dashboard  # namespace package; installed next to this module

    here = os.path.abspath(list(dashboard.__path__)[0])
    root = os.path.dirname(here)
    # Insert dashboard/ first, then the repo root, so the root ends up
    # ahead — the same precedence as PYTHONPATH="$PWD:$PWD/dashboard".
    for path in (here, root):
        if path not in sys.path:
            sys.path.insert(0, path)


def console_main() -> None:
    """`aerial-console` — the operator console (dashboard/serve_console.py)."""
    _setup_path()
    from dashboard.serve_console import main

    main()


def ops_agent_main() -> None:
    """`aerial-ops-agent` — the ops agent (dashboard/followup/runner.py)."""
    _setup_path()
    from dashboard.followup.runner import main

    main()


if __name__ == "__main__":
    console_main()
