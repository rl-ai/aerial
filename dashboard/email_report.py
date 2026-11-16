#!/usr/bin/env python3
"""Build the daily report as plain text/markdown (no HTML email).

Extracts the exec-summary + scrape-intel + Zone B/A facts from the
rendered board and formats them as a markdown report: headers, bullets,
borders via horizontal rules. Renders cleanly in any mail client,
including iPhone Mail's plain-text view.
"""
from __future__ import annotations

import re
from pathlib import Path

_SECTION_RE = re.compile(r'<section id="([^"]+)"[^>]*>(.*?)</section>', re.S)


def _text(section_html: str) -> str:
    """Section HTML -> readable text lines (strip tags, keep structure)."""
    t = section_html
    t = re.sub(r"<h2>.*?</h2>", "", t, flags=re.S)
    # table rows -> bullet-ish lines
    t = re.sub(r"</t[dh]>", " | ", t)
    t = re.sub(r"</tr>", "\n", t)
    t = re.sub(r"<tr[^>]*>", "- ", t)
    t = re.sub(r"</?(ul|ol|li|div|table|thead|tbody|span|p)[^>]*>", "\n", t)
    t = re.sub(r"<br[^>]*>", "\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = t.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    t = t.replace("&#x27;", "'").replace("&nbsp;", " ")
    lines = []
    for ln in t.splitlines():
        ln = re.sub(r"[ \t]+", " ", ln).strip(" |")
        if ln:
            lines.append(ln)
    # collapse consecutive duplicates (cell-splitting artifacts)
    out = []
    for ln in lines:
        if not out or out[-1] != ln:
            out.append(ln)
    return "\n".join(out)


def gather_done(window_start: str) -> list[str]:
    """What was DONE: commits landed today across the product repos +
    followup items closed. High-level lines for the BLUF bullets."""
    import subprocess
    from deployment_profile import value as _pv
    _root = Path(__file__).resolve().parent.parent
    default_repos = {"aerial": str(_root)}
    repos = dict(_pv("paths.repo_paths") or {}) or default_repos
    lines: list[str] = []
    for label, repo in sorted(repos.items()):
        if not repo:
            continue
        try:
            out = subprocess.run(
                ["git", "-C", repo, "log", f"--since={window_start}",
                 "--oneline", "--no-merges"],
                capture_output=True, text=True, timeout=15)
            for ln in out.stdout.splitlines():
                ln = ln.strip()
                if ln and not ln.startswith("Brownout"):
                    lines.append(f"[{label}] {ln.split(' ', 1)[-1]}")
        except Exception:
            pass
    return lines


def _split_h2(board_html: str) -> dict[str, str]:
    """render_daily board -> {section title: inner html} split on <h2>."""
    parts = re.split(r"<h2>(.*?)</h2>", board_html, flags=re.S)
    out = {}
    for i in range(1, len(parts) - 1, 2):
        out[parts[i].strip()] = parts[i + 1]
    return out


def build_email_text(board_html: str, date_str: str) -> str:
    """BLUF structure (borrowed from executive-briefing practice):
    Bottom line -> Done (3-5 bullets) -> Numbers -> Detail high-to-low
    -> Sources. Model-call and token stats LEAD the numbers (standing
    format rule) and appear in every report."""
    secs = _split_h2(board_html)
    exec_html = secs.get("Executive summary — yesterday", "")
    token_html = secs.get("Token stats (model-calls tailer)", "")
    zone_b = secs.get("Zone B — exception / decision queue", "")

    def _txt(html: str) -> str:
        t = re.sub(r"<h3>[^<]*</h3>", "", html)
        t = re.sub(r"</t[dh]>", " | ", t)
        t = re.sub(r"<tr[^>]*>", "\n- ", t)
        t = re.sub(r"</?(ul|ol|li|div|table|thead|tbody|span|p|pre)[^>]*>", "\n", t)
        t = re.sub(r"<br[^>]*>", "\n", t)
        t = re.sub(r"<[^>]+>", "", t)
        t = (t.replace("&amp;", "&").replace("&lt;", "<")
              .replace("&gt;", ">").replace("&#x27;", "'").replace("&nbsp;", " "))
        lines = []
        for ln in t.splitlines():
            ln = re.sub(r"[ \t]+", " ", ln).strip(" |")
            if ln:
                lines.append(ln)
        out = []
        for ln in lines:
            if not out or out[-1] != ln:
                out.append(ln)
        return "\n".join(out)

    exec_t = _txt(exec_html)
    token_t = _txt(token_html)
    zb_t = _txt(zone_b)

    # Headlines (BLUF candidates) from the exec summary's brief list.
    headlines = re.findall(r"<h3>Headlines</h3><ul class=brief>(.*?)</ul>",
                           exec_html, re.S)
    heads = re.findall(r"<li>(.*?)</li>", headlines[0], re.S) if headlines else []
    heads = [re.sub(r"<[^>]+>", "", h).strip() for h in heads]

    # Numbers: model calls + tokens LEAD (standing format rule).
    strip_m = re.search(r"(\d+ calls / [^<\n|\u00b7]*)", exec_html)
    calls_tokens = strip_m.group(1).strip() if strip_m else "unavailable"
    runs_m = re.search(r"(\d+ runs)", exec_html)
    runs = runs_m.group(1).strip() if runs_m else "unavailable"
    fleet_m = re.search(r"([\d,]+ calls / [^<\n|]*)", _text(token_html))
    fleet = fleet_m.group(1).strip() if fleet_m else ""
    numbers = " \u00b7 ".join([b for b in (calls_tokens, runs) if b])
    if fleet:
        numbers += f" \u00b7 fleet: {fleet}"

    bottom = heads[0] if heads else "No significant change in the last 24h."

    done_start = None
    import datetime as _dt
    try:
        from dashboard import clock as _clock
    except ImportError:  # running as a top-level script from dashboard/
        import clock as _clock  # type: ignore
    window_start = (_dt.datetime.fromtimestamp(_clock.now(), _dt.timezone.utc)
                    - _dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    done = gather_done(window_start)

    parts = ["# Aerial — Daily Executive Report",
             f"{date_str} · window: last 24h",
             "",
             "## Bottom line",
             bottom,
             "",
             "## Done",
             *([f"- {d}" for d in done[:5]]
               or ["- (nothing landed in the window)"]),
             "",
             f"## Numbers — {numbers}",
             ""]
    agent_actions = secs.get("Executive summary — yesterday", "")
    aa_lines = _txt(agent_actions)
    if aa_lines:
        parts += ["## Detail", "### Fleet & ops", aa_lines, ""]
    if token_t:
        parts += ["### Token stats", token_t, ""]
    intel = secs.get("Scrape intel — raw stats") or secs.get(
        "Executive summary — yesterday", "")
    themes = _txt(intel)
    if themes:
        parts += ["### Ecosystem signals", themes, ""]
    zb = _txt(zone_b)
    if zb and "queue empty" not in zb.lower():
        parts += ["### Needs a person (Zone B)", zb, ""]
    parts += ["---",
              "Sources: model-calls.jsonl · hub.db job_runs · git logs · "
              "scrape pipeline. Every claim evidence-labeled. On demand: "
              "ask for a fresh report."]
    return "\n".join(parts)
