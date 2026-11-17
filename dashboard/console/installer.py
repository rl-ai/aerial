"""Pilot onboarding installer — Operator Console v0.9-rc1 (spec §7).

Three steps, max three screens deep, single tenant (one hub URL per operator):

1. **Install** — one command:
       python -m console.installer --hub-url URL [--config PATH]
   Writes config (hub URL + read token path), verifies connectivity, prints ✓
   or the exact failing check.
2. **Grant** — checks for the decision-scoped write token; prints
   "write token: missing — decisions disabled until granted" instead of failing.
   Reads work without the write token — first look is never blocked on admin.
3. **First look** — banner logic via :func:`first_look_banner`.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

#: Link shown in the read-only first-look banner (spec §7 step 3). Targets the
#: pilot onboarding doc, which mirrors spec §1–§7 in plain language.
DOCS_LINK = "docs/pilot-onboarding.md"

READ_ONLY_BANNER = (
    "You're viewing the operator console (read-only until your write token "
    f"is granted). See docs: {DOCS_LINK}"
)
DECISIONS_ENABLED_BANNER = "decisions enabled ✓"
WRITE_TOKEN_MISSING_MSG = "write token: missing — decisions disabled until granted"

DEFAULT_CONFIG_PATH = Path("~/.config/operator-console/config.json")


def load_config(path: Path) -> dict:
    """Load installer config; missing file → empty dict."""
    try:
        return json.loads(Path(path).expanduser().read_text())
    except FileNotFoundError:
        return {}


def save_config(path: Path, config: dict) -> Path:
    """Persist config (JSON). Used by the installer and for banner dismissal state."""
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
    return path


def first_look_banner(
    write_token_present: bool, banner_dismissed: bool
) -> str | None:
    """Spec §7 step 3 banner logic.

    - write token missing → the one-time dismissible read-only banner
      (None once dismissed — dismissal state persists in the config file).
    - write token present → single "decisions enabled ✓", then never again.
    """
    if write_token_present:
        return None if banner_dismissed else DECISIONS_ENABLED_BANNER
    return None if banner_dismissed else READ_ONLY_BANNER


def check_connectivity(hub_url: str, read_token_path: str, timeout: float = 5.0):
    """Verify hub connectivity with the read token.

    Returns None on success or the **exact failing check** (spec §7 step 1)
    as a human-readable string. Never raises.
    """
    if not hub_url or not str(hub_url).startswith(("http://", "https://")):
        return "hub-url check failed: URL must start with http:// or https://"
    url = str(hub_url).rstrip("/") + "/"
    req = urllib.request.Request(url)
    if read_token_path:
        token_file = Path(read_token_path).expanduser()
        if not token_file.exists():
            return f"read token check failed: file not found: {read_token_path}"
        try:
            token = token_file.read_text().strip()
        except OSError as exc:
            return f"read token check failed: cannot read {read_token_path}: {exc}"
        if not token:
            return f"read token check failed: empty token file: {read_token_path}"
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return f"connectivity check failed: hub returned HTTP {resp.status} (expected 200)"
    except urllib.error.HTTPError as exc:
        return (
            f"connectivity check failed: hub returned HTTP {exc.code} "
            + ("(401/403 — read token rejected)" if exc.code in (401, 403) else "")
        ).strip()
    except urllib.error.URLError as exc:
        return f"connectivity check failed: cannot reach {url}: {exc.reason}"
    except OSError as exc:
        return f"connectivity check failed: cannot reach {url}: {exc}"
    return None


def write_token_present(write_token_path: str) -> bool:
    """True when the decision-scoped write token file exists and is non-empty."""
    if not write_token_path:
        return False
    token_file = Path(write_token_path).expanduser()
    try:
        return bool(token_file.read_text().strip())
    except OSError:
        return False


def run_install(hub_url: str, config_path: Path, timeout: float = 5.0) -> int:
    """Run the install flow; returns the process exit code."""
    config_path = Path(config_path).expanduser()
    config = load_config(config_path)
    config["hub_url"] = hub_url
    config.setdefault(
        "read_token_path", str(Path("~/.config/operator-console/read-token").expanduser())
    )
    config.setdefault(
        "write_token_path",
        str(Path("~/.config/operator-console/write-token").expanduser()),
    )
    config.setdefault("banner_dismissed", False)
    save_config(config_path, config)
    print(f"config written: {config_path}")

    failure = check_connectivity(hub_url, config["read_token_path"], timeout=timeout)
    if failure:
        print(f"✗ {failure}")
        print("install incomplete — fix the failing check above and re-run the installer.")
        return 1
    print("✓ connectivity check passed: hub reachable, read token accepted")

    if write_token_present(config["write_token_path"]):
        print("✓ write token: present — decisions enabled")
    else:
        print(WRITE_TOKEN_MISSING_MSG)
    print("✓ install complete")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m console.installer",
        description="Operator Console v0.9-rc1 pilot installer (spec §7).",
    )
    parser.add_argument("--hub-url", required=True, help="single hub URL for this operator")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="config file path (default: %(default)s)",
    )
    args = parser.parse_args(argv)
    return run_install(args.hub_url, Path(args.config))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
