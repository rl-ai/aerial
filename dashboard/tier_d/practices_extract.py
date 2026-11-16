#!/usr/bin/env python3
"""practices_extract.py — Kimi LEARNS from the frontier (Tier D adapter).

After each Tier D signal consolidation (a plan/review signal in
~/workspaces/worker/tier-d-signals/<task_id>/signal.json), this module asks
the DS-or-Kimi analyst: "what did the frontier plans/reviews do that Kimi
should adopt?" — and appends structured, dated, versioned entries to
dashboard/tier_d/kimi-practices.md. Kimi's kimi-plan / kimi-review role
preambles reference that file, so the next consolidation already applies
what the frontier demonstrated.

Each practice entry records:
  * what it is            — the technique, one line;
  * who demonstrated it   — which frontier model (claude / codex);
  * how Kimi applies it   — the standing-instruction form for Kimi.

Append-only: an extraction never rewrites prior entries (dedup is by
case/whitespace-insensitive practice key). The file's version counter in
the header is bumped once per extraction that adds entries.

Standalone usage (also imported by dashboard/tier_d.py via importlib —
dashboard/tier_d.py and dashboard/tier_d/ share a name, so no package
import is used):
  python3 dashboard/tier_d/practices_extract.py <signal.json> [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PRACTICES_PATH = Path(__file__).resolve().parent / "kimi-practices.md"
MODEL_CALL = REPO_ROOT / "bin" / "model_call.py"

#: Role prompted with the extraction question (DS or Kimi analyst).
#: Override with TIER_D_PRACTICES_ROLE; set to "" to disable LLM prompting
#: and fall back to the frontier's own PRACTICE: lines only.
ANALYST_ROLE = "kimi-review"

ANALYST_PROMPT = (
    "You are the practices analyst. Below are independent plan/review "
    "signals produced by frontier models (Claude, Codex). Identify the "
    "planning and review PRACTICES the frontier demonstrated that Kimi "
    "(our primary planner/reviewer) should adopt as standing instructions. "
    "Only concrete, reusable techniques — not task-specific observations. "
    "Respond with JSON only:\n"
    '[{"practice": "...", "demonstrated_by": "claude|codex|both", '
    '"how_kimi_applies": "..."}]\n\n'
    "FRONTIER SIGNAL:\n{signal}")


# ------------------------------------------------------------------ analyst

def default_analyst(prompt: str) -> Optional[str]:
    """Ask the configured role via bin/model_call.py; None on any failure."""
    role = __import__("os").environ.get("TIER_D_PRACTICES_ROLE", ANALYST_ROLE)
    if not role:
        return None
    try:
        p = subprocess.run(
            [sys.executable, str(MODEL_CALL), "--role", role,
             "--ask", prompt],
            capture_output=True, text=True, timeout=1800)
    except Exception:
        return None
    if p.returncode != 0:
        return None
    return p.stdout


def parse_entries(analyst_out: Optional[str]) -> List[Dict[str, str]]:
    """Pull the JSON practice list out of analyst output (tolerant)."""
    if not analyst_out:
        return []
    m = re.search(r"\[.*\]", analyst_out, re.DOTALL)
    if not m:
        return []
    try:
        raw = json.loads(m.group(0))
    except Exception:
        return []
    out = []
    for e in raw if isinstance(raw, list) else []:
        if not isinstance(e, dict):
            continue
        practice = str(e.get("practice", "")).strip()
        if not practice:
            continue
        out.append({
            "practice": practice,
            "demonstrated_by": str(e.get("demonstrated_by", "unknown")).strip()
                               or "unknown",
            "how_kimi_applies": str(e.get("how_kimi_applies", "")).strip(),
        })
    return out


def fallback_entries(signal: Dict[str, Any]) -> List[Dict[str, str]]:
    """Heuristic entries from the signal's own PRACTICE: lines when no
    analyst is reachable — the frontier still teaches Kimi something."""
    seen: set = set()
    out = []
    for tool, src in (signal.get("sources") or {}).items():
        for p in src.get("practices") or []:
            key = " ".join(p.lower().split())
            if not key or key in seen:
                continue
            seen.add(key)
            kind = signal.get("kind")
            noun = "planning" if kind == "plan" else "review"
            out.append({
                "practice": p,
                "demonstrated_by": tool,
                "how_kimi_applies": f"Adopt as a standing {noun} practice.",
            })
    return out


# -------------------------------------------------------------------- file

def _bump_version(text: str) -> str:
    """Version header: '(version N)' -> '(version N+1)', adding one if absent."""
    def _inc(m):
        return f"(version {int(m.group(1)) + 1})"
    new, n = re.subn(r"\(version (\d+)\)", _inc, text, count=1)
    return new if n else "# Kimi accumulated practices (version 1)\n" + text


def append_entries(entries: List[Dict[str, str]], signal: Dict[str, Any],
                   path: Path = PRACTICES_PATH) -> List[Dict[str, str]]:
    """Append a dated, versioned section; dedup vs everything already in
    the file. Appends — never overwrites. Returns the entries actually
    written."""
    if not entries:
        return []
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text() if path.exists() else ""
    seen = {" ".join(ln[3:].lower().split())
            for ln in existing.splitlines() if ln.startswith("### ")}
    new = [e for e in entries
           if " ".join(e["practice"].lower().split()) not in seen]
    if not new:
        return []
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [f"\n## {stamp} — {signal.get('kind', '?')} signal "
             f"{signal.get('task_id', '?')}\n"]
    for e in new:
        lines.append(f"### {e['practice']}\n")
        lines.append(f"- Demonstrated by: {e['demonstrated_by']}\n")
        if e.get("how_kimi_applies"):
            lines.append(f"- How Kimi applies it: {e['how_kimi_applies']}\n")
    with open(path, "a") as fh:
        fh.write("".join(lines))
    # bump the version counter in the header (rewrite of the first line only)
    text = path.read_text()
    head, sep, rest = text.partition("\n")
    if sep:
        path.write_text(_bump_version(head) + sep + rest)
    return new


# -------------------------------------------------------------------- main

def extract_and_record(signal: Dict[str, Any],
                       analyst: Optional[Callable[[str], Optional[str]]]
                       = None,
                       path: Path = PRACTICES_PATH,
                       use_analyst: bool = True) -> List[Dict[str, str]]:
    """One learning step: consolidate a Tier D signal -> extract the
    practices the frontier demonstrated -> append them to the practices
    file. Returns the newly recorded entries. Never raises."""
    try:
        prompt = ANALYST_PROMPT.replace(
            "{signal}", json.dumps(signal, default=str)[:8000])
        fn = analyst if analyst is not None else default_analyst
        entries = parse_entries(fn(prompt) if use_analyst else None)
        if not entries:
            entries = fallback_entries(signal)
        return append_entries(entries, signal, path)
    except Exception:
        return []


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="practices_extract")
    ap.add_argument("signal", help="path to a signal.json (or '-' for stdin)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the entries that would be recorded")
    args = ap.parse_args(argv)
    raw = sys.stdin.read() if args.signal == "-" \
        else Path(args.signal).read_text()
    signal = json.loads(raw)
    if args.dry_run:
        prompt = ANALYST_PROMPT.replace(
            "{signal}", json.dumps(signal, default=str)[:8000])
        print(json.dumps(parse_entries(default_analyst(prompt))
                         or fallback_entries(signal), indent=1))
        return 0
    new = extract_and_record(signal)
    print(json.dumps({"recorded": len(new),
                      "path": str(PRACTICES_PATH)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
