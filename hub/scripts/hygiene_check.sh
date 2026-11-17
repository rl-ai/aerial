#!/usr/bin/env bash
# hygiene_check.sh — ONE pre-push command for the team.
#
#   ./scripts/hygiene_check.sh          full: lint + secrets + tests + DB
#   ./scripts/hygiene_check.sh --quick  skip pytest (pre-push quick mode)
#
# Checks (each prints a PASS/FAIL line; exit 1 if any FAIL):
#   1. lint        ruff if available, else python -m compileall (syntax)
#   2. secrets     git grep for credential-shaped literals (see below)
#   3. tests       pytest -q summary line (skipped in --quick)
#   4. db          PRAGMA integrity_check if a hub.db is present
#
# Non-interactive by design. Secrets scan runs against TRACKED files only
# (git grep), so vendored caches and the runtime DB never trip it.
#
# Why the pattern is literal-shaped: adapters read tokens from the external
# runtime config (~/.config/ticket-hub/adapters.json / TICKET_HUB_ADAPTERS_CONFIG,
# outside the repo). Source and doc comments legitimately mention
# token/bearer/password words around INERT config — no config file or token
# present means the adapter runs inert and posts nothing. So the scan flags
# only *credential assignments with a literal value of >=8 chars* or a
# `Bearer <long-literal>` — not doc text, not f-string interpolation
# (f"Bearer {self._token}" has no literal after Bearer), not short test
# fixtures. Additional allowlist: .secrets-allowlist (globs, one per line).
set -uo pipefail

QUICK=0
[ "${1:-}" = "--quick" ] && QUICK=1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_DIR}"

PYTHON="${TICKET_HUB_PYTHON:-}"
if [ -z "${PYTHON}" ]; then
    for cand in "${TICKET_HUB_PYTHON:-}" "$(command -v python3 || true)"; do
        [ -x "${cand}" ] && PYTHON="${cand}" && break
    done
fi
[ -n "${PYTHON}" ] || { echo "FAIL  python not found (set TICKET_HUB_PYTHON)"; exit 1; }

FAILURES=0
pass() { printf 'PASS  %s\n' "$1"; }
fail() { printf 'FAIL  %s\n' "$1"; FAILURES=$((FAILURES + 1)); }

# ---- 1. lint ---------------------------------------------------------------
# NOTE: capture output then echo it — `if cmd | sed` would test sed's status.
lint_run() {  # $@ = lint command; returns its exit code, prints indented tail
    local out rc
    out="$("$@" 2>&1)"; rc=$?
    printf '%s\n' "${out}" | tail -3 | sed 's/^/      /'
    return "${rc}"
}
if command -v ruff >/dev/null 2>&1; then
    if lint_run ruff check .; then pass "lint (ruff)"; else fail "lint (ruff) — see output above"; fi
elif "${PYTHON}" -m ruff --version >/dev/null 2>&1; then
    if lint_run "${PYTHON}" -m ruff check .; then pass "lint (python -m ruff)"; else fail "lint (python -m ruff) — see output above"; fi
else
    # no ruff anywhere: fall back to full syntax compile of first-party code
    if lint_run "${PYTHON}" -m compileall -q api.py hub_cli.py hub_client.py \
            hub_gateway.py outbound.py ticket_hub tests scripts; then
        pass "lint (compileall fallback — ruff not installed)"
    else
        fail "lint (syntax errors above)"
    fi
fi

# ---- 2. secrets ------------------------------------------------------------
SECRET_RE='(api[_-]?key|secret|passwd|password|token|bearer)["'"'"']?[[:space:]]*[:=][[:space:]]*["'"'"'][A-Za-z0-9._/+==_-]{8,}["'"'"']|Bearer[[:space:]]+[A-Za-z0-9._/+][A-Za-z0-9._/+-]{14,}'
EXCLUDES=( -e 'scripts/hygiene_check.sh' -e 'pyproject.toml' )
ALLOWLIST=".secrets-allowlist"
if [ -f "${ALLOWLIST}" ]; then
    while IFS= read -r line; do
        case "${line}" in ''|'#'*) continue ;; esac
        EXCLUDES+=( -e "${line}" )
    done < "${ALLOWLIST}"
fi
# Ellipsis placeholders ("token": "...", "xoxb-...") in adapter docstrings
# describe the INERT config shape (no file/no token => adapter posts nothing)
# and are not credentials. Filter matches ONLY a quoted value that *ends* in
# the ellipsis (dots immediately before the closing quote) — a real secret
# with dots in the middle still trips the scan.
if HITS="$(git grep -IniE "${SECRET_RE}" -- "${EXCLUDES[@]+"${EXCLUDES[@]}"}" . 2>/dev/null | grep -vE '\.\.\.["'"'"']')"; then
    printf '%s\n' "${HITS}" | sed 's/^/      /'
    fail "secrets scan — credential-shaped literals above (move them to the external adapters config; add .secrets-allowlist entry + justification only for true inert fixtures)"
else
    pass "secrets scan (tracked files)"
fi

# ---- 3. tests --------------------------------------------------------------
if [ "${QUICK}" -eq 1 ]; then
    printf 'SKIP  tests (--quick)\n'
else
    TESTOUT="$("${PYTHON}" -m pytest -q 2>&1)"
    TESTRC=$?
    echo "${TESTOUT}" | grep -E '(passed|failed|error)' | tail -1 | sed 's/^/      /'
    if [ "${TESTRC}" -eq 0 ]; then
        pass "tests (pytest -q)"
    else
        fail "tests (pytest exit ${TESTRC}; TICKET_HUB_PYTHON may need fastapi/httpx/uvicorn)"
    fi
fi

# ---- 4. DB integrity -------------------------------------------------------
DB="${TICKET_HUB_DB:-${TICKET_HUB_HOME:-$HOME/.ticket-hub}/hub.db}"
if [ -f "${DB}" ] && command -v sqlite3 >/dev/null 2>&1; then
    # integrity_check is a read-only PRAGMA; plain path (a WAL-mode DB with
    # no -shm beside it cannot be opened via ?mode=ro).
    RES="$(sqlite3 "${DB}" 'PRAGMA integrity_check;' 2>&1)"
    if [ "${RES}" = "ok" ]; then
        pass "db integrity (${DB})"
    else
        printf '      %s\n' "${RES}"
        fail "db integrity (${DB})"
    fi
else
    printf 'SKIP  db integrity (no db at %s or no sqlite3)\n' "${DB}"
fi

# ---- verdict ---------------------------------------------------------------
# Machine-readable summary for GET /admin/status (Mission Control).
HS="${TICKET_HUB_HYGIENE_STATUS:-${TICKET_HUB_HOME:-$HOME/.ticket-hub}/hygiene-status.json}"
mkdir -p "$(dirname "${HS}")" 2>/dev/null || true
printf '%s' "{\"result\": \"$( [ "${FAILURES}" -eq 0 ] && echo PASS || echo FAIL )\", \"ts\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\", \"failures\": ${FAILURES}}" \
    > "${HS}.tmp" 2>/dev/null && mv "${HS}.tmp" "${HS}" 2>/dev/null || true

if [ "${FAILURES}" -eq 0 ]; then
    echo "HYGIENE: PASS"
    exit 0
else
    echo "HYGIENE: FAIL (${FAILURES} check(s) failed)"
    exit 1
fi
