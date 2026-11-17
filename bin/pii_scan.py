#!/usr/bin/env python3
"""pii_scan — pre-push sanitizer gate for the Aerial repo.

Scans tracked files for internal identifiers that must not ship:
hostnames, org emails, internal paths, prod data paths, ticket ids.
Exit 1 with a findings list if anything matches. Run before every push.

Usage: python3 bin/pii_scan.py [--fix-info]
"""
import re
import subprocess
import sys

PATTERNS = [
    (r"ai-morpheus", "internal hostname"),
    (r"\bmorpheus\b", "internal hostname (morpheus)"),
    (r"rlai\.io", "internal domain"),
    (r"owner@example", "personal email"),
    (r"/srv/ai/", "internal absolute path"),
    (r"services/ticket-hub/data", "prod data path"),
    (r"tp-data", "prod data path"),
    (r"/home/ai-eva", "internal home path"),
    (r"TK-01M[A-Z0-9]+", "internal ticket id"),
    (r"\b(?:10|192\.168|172\.(?:1[6-9]|2[0-9]|3[01]))\.\d{1,3}\.\d{1,3}\b", "private LAN address"),
    (r"SHA256:[A-Za-z0-9+/]{43}", "pinned host-key fingerprint"),
]
# Personal identifiers (names, usernames, emails) never live in the repo:
# list them one regex per line in this untracked file (it is gitignored).
LOCAL_PATTERNS_FILE = "config/pii-patterns.local"
SKIP_EXTENSIONS = {".png", ".jpg", ".gif", ".ico", ".pdf", ".gz"}
SKIP_FILES = {"integration/test_oss_generic.py",  # contains the BANNED list itself
              "bin/pii_scan.py"}  # the scanner: its patterns are the banned strings


def load_patterns():
    """Built-in patterns plus the operator's local ones (never committed)."""
    pats = list(PATTERNS)
    try:
        with open(LOCAL_PATTERNS_FILE, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    pats.append((line, "local personal identifier"))
    except FileNotFoundError:
        pass
    return pats


def tracked_files():
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True)
    return [f for f in out.stdout.splitlines()
            if not any(f.endswith(e) for e in SKIP_EXTENSIONS)
            and f not in SKIP_FILES]


def main():
    files = tracked_files()
    patterns = load_patterns()
    findings = []
    for f in files:
        try:
            text = open(f, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for pat, label in patterns:
            for m in re.finditer(pat, text, re.I):
                line = text[:m.start()].count("\n") + 1
                findings.append((f, line, label, m.group(0)[:40]))

    if findings:
        print(f"PII/internal-identifier findings: {len(findings)}")
        for f, line, label, hit in findings[:50]:
            print(f"  {f}:{line}  [{label}]  {hit}")
        if len(findings) > 50:
            print(f"  ... and {len(findings) - 50} more")
        print("Sanitize before pushing (genericize names/paths).")
        return 1
    print("pii_scan: clean — no internal identifiers in tracked files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
