#!/usr/bin/env python3
"""model_call.py — one governed call to a llama.cpp llama-server role.

Contract: ../API.md.  Imports llm_resources (sibling module, same contract);
does NOT reimplement probing/budgeting.  Python stdlib only.

Security rules honoured here:
  * bearer token is read from bearer_file and never printed or logged;
  * prompt text and model output text are never printed or logged —
    the jsonl call log and the summary line carry counts/ids only.

Exit codes — every failure has its OWN code (no code is shared between
two different failures):
  0 ok
  1 usage / config / transport / bad-response error
  4 input too large for the slot budget ("split")
  5 budget too small: finish_reason == "length" AND the content does not
    parse as JSON (empty — reasoning consumed the budget — OR truncated
    mid-JSON).  stderr: "budget too small (output cut at max_tokens=N;
    raise --max-tokens or tighten the schema)"; the call log carries
    finish and verdict null.
  6 per-slot context unknown (returned before any call, when neither
    /props, /slots nor the listening process reports an n_ctx — distinct
    from the misleading exit-4 "input too large" it used to hit)
  7 schema role returned content that is not valid JSON with finish
    "stop" (the model ignored the schema)
  8 timeout: no slot/pool room became free within the budget timeout
"""
import argparse
import importlib.util
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import llm_resources  # noqa: E402  (sibling module under the same contract)
import hub_jobs  # noqa: E402  (Jobs reporting, same contract)

POLL_S = 5.0            # /slots poll interval while waiting for room
HTTP_TIMEOUT_S = 30.0   # per small HTTP request (chat uses the budget timeout)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_TOO_LARGE = 4          # input too large for the slot budget
EXIT_BUDGET_SMALL = 5       # finish "length" + content not parseable JSON
EXIT_PER_SLOT_UNKNOWN = 6   # no n_ctx anywhere (checked before any call)
EXIT_BAD_JSON = 7           # schema role, finish "stop", non-JSON content
EXIT_NO_SLOT = 8            # timeout waiting for a slot / pool room

DEFAULT_LOG = "~/ai-ops-ops/model-calls.jsonl"

# The resources file a deployment may pass with --resources. It is NOT read by
# default: llm_servers.json stays the one default source until every reader
# moves to resources.json together (V1.12), so the two can never disagree.
RESOURCES_CANDIDATES = ("resources.json",)

_CONTRACTS_PKG = os.path.join(_HERE, os.pardir, "packages", "rlai-contracts",
                              "src", "rlai_contracts")


# ----------------------------------------------------------------- plumbing

def read_bearer(bearer_file):
    """Read the bearer token; never print or log its value.
    '~' is expanded (parity with llm_resources._read_bearer): a bearer_file
    configured with '~/' must not silently resolve to nothing here."""
    if not bearer_file:
        return None
    try:
        with open(os.path.expanduser(bearer_file), "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return None


def http_json(method, url, bearer, payload=None, timeout=HTTP_TIMEOUT_S):
    """Small JSON helper. Errors must not surface request bodies or tokens."""
    data = None
    req = urllib.request.Request(url, method=method)
    if bearer:
        req.add_header("Authorization", "Bearer " + bearer)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, data=data, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_slots(server_cfg):
    """The server's /slots list (llama-server), with its bearer."""
    return http_json("GET", server_cfg["url"].rstrip("/") + "/slots",
                     read_bearer(server_cfg.get("bearer_file")))


def _read_text(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _resolve(base_dir, path):
    return path if os.path.isabs(path) else os.path.join(base_dir, path)


# ------------------------------------------------------------ resources config

_RC = None  # rlai_contracts package, loaded on first use (never sys.path)


class ResourcesError(Exception):
    """A C6 resources file is unreadable or fails validation. The message
    names the problem; it never carries a config value."""


def _load_rlai_contracts():
    """Load the vendored rlai_contracts package by FILE PATH (like
    bin/forge_kpis.py loads publicready): no sys.path edit, no assumption the
    package is installed. Cached in sys.modules under its own name so a second
    load reuses the same package (its submodules resolve through it)."""
    global _RC
    if _RC is not None:
        return _RC
    pkg = os.path.abspath(_CONTRACTS_PKG)
    init = os.path.join(pkg, "__init__.py")
    if not os.path.isfile(init):
        raise ResourcesError("rlai_contracts package missing")
    loaded = sys.modules.get("rlai_contracts")
    if (loaded is not None
            and getattr(loaded, "__file__", None) == os.path.abspath(init)):
        _RC = loaded  # same package, already loaded by another module
        return _RC
    for name in [n for n in sys.modules
                 if n == "rlai_contracts" or n.startswith("rlai_contracts.")]:
        del sys.modules[name]  # a copy from elsewhere would hand stale cache
    spec = importlib.util.spec_from_file_location("rlai_contracts", init,
                                                  submodule_search_locations=[pkg])
    if spec is None:
        raise ResourcesError("rlai_contracts package missing")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rlai_contracts"] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop("rlai_contracts", None)
        raise ResourcesError("rlai_contracts package failed to load")
    _RC = mod
    return mod


def config_from_resources(doc):
    """Turn a C6 resources document into the exact llm_servers.json-shaped
    dict model_call and llm_resources already read. Keys that are absent in
    the document stay absent in the result; nothing is invented."""
    if not isinstance(doc, dict):
        raise ResourcesError("resources document is not an object")
    servers = {}
    for name, res in (doc.get("resources") or {}).items():
        if not isinstance(res, dict):
            continue
        srv = {}
        for key in ("url", "bearer_file", "pool_ctx", "max_parallel",
                    "reserve_slots", "reserve_pool_ctx", "billing"):
            if key in res:
                srv[key] = res[key]
        rates = res.get("rates")
        if isinstance(rates, dict):
            if "prefill_tps" in rates:
                srv["fallback_prefill_tps"] = rates["prefill_tps"]
            if "gen_tps" in rates:
                srv["fallback_gen_tps"] = rates["gen_tps"]
        servers[name] = srv

    slots = doc.get("slots") or {}
    resources = doc.get("resources") or {}
    roles = {}
    for alias, slot_name in (doc.get("aliases") or {}).items():
        slot = slots.get(slot_name)
        if not isinstance(slot, dict):
            continue
        role = {"server": slot["resource"]} if "resource" in slot else {}
        if "llama_slot" in slot:
            role["slot"] = slot["llama_slot"]
        for key in ("max_tokens", "reasoning_effort", "schema", "preamble"):
            if key in slot:
                role[key] = slot[key]
        # the slot's model, else the model of the resource it sits on
        model = slot.get("model")
        if model is None:
            res = resources.get(slot.get("resource"))
            if isinstance(res, dict):
                model = res.get("model")
        if model is not None:
            role["model"] = model
        roles[alias] = role

    cfg = {"servers": servers, "roles": roles}
    if "margin" in doc:
        cfg["margin"] = doc["margin"]
    cfg["log"] = DEFAULT_LOG  # the default call log
    return cfg


def _load_resources_file(path):
    """Read a C6 resources file and turn it into an llm_servers-shaped config.

    A file whose top level carries "servers"/"roles" instead of "resources"
    is refused by the C6 shape check (unexpected property 'servers'): one
    shape per file name, never a silent guess.  A validation failure raises
    ResourcesError naming the problem, never printing a value.
    """
    try:
        with open(os.path.expanduser(path), "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except OSError:
        raise ResourcesError("resources file not found")
    except ValueError:
        raise ResourcesError("resources file is not valid JSON")
    rc = _load_rlai_contracts()
    errors = rc.validate("c6-resources", doc if isinstance(doc, dict) else {})
    if errors:
        # name the problem (the validator's own "path: reason" string); a
        # validator message that echoes a value is replaced by its field path
        first = errors[0]
        field, _, _reason = first.partition(":")
        if len(first.split(":", 1)) > 1 and _looks_like_value(_reason):
            first = "%s: invalid" % field
        raise ResourcesError("resources file fails C6 validation: %s" % first)
    return config_from_resources(doc)


def _looks_like_value(reason):
    """True when a validator message tail embeds the checked value itself
    (': 5 < minimum 1', ": value 'x' != const 'y'") — such a tail is never
    echoed, so no config value can reach stderr through it."""
    return bool(re.match(r"\s*[-+0-9eE.]", reason or "")
                or (reason or "").lstrip().startswith("value "))


def _load_config_any(path):
    """Load a config from either file flavour (the tests use it to exercise
    the same source rules against a temp bin/). Returns None on unreadable
    input so the caller keeps the 'cannot load config' handling."""
    try:
        if os.path.basename(str(path)) in RESOURCES_CANDIDATES:
            return _load_resources_file(path)
        return llm_resources.load_config(str(path))
    except ResourcesError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return None
    except (OSError, ValueError):
        return None


def _config_source_path(config=None, resources=None, base_dir=None):
    """The rule-2 source order in one place: --resources PATH when given;
    else --config PATH (an llm_servers.json, read as today); else
    <base_dir>/llm_servers.json as today. A resources.json beside it is not
    read by default (V1.12 switches every reader at once)."""
    if resources is not None:
        return resources
    if config is not None:
        return config
    base = base_dir if base_dir is not None else _HERE
    return os.path.join(base, "llm_servers.json")


# ----------------------------------------------------------------- prompt

def build_user_prompt(files, diff_path, ask_path):
    """Contract: files in SORTED path order, then DIFF, then ASK."""
    parts = []
    for path in sorted(files or []):
        parts.append("=== FILE: %s ===\n%s\n" % (path, _read_text(path)))
    if diff_path:
        parts.append("=== DIFF ===\n%s\n" % _read_text(diff_path))
    parts.append("=== ASK ===\n%s" % _read_text(ask_path))
    return "".join(parts)


def load_role_schema(cfg_dir, schema_name):
    """A role's output schema from llm_schemas.json, or None when it has none."""
    schemas = json.loads(_read_text(os.path.join(cfg_dir, "llm_schemas.json")))
    if schema_name not in schemas:
        raise KeyError("schema %r not in llm_schemas.json" % schema_name)
    return schemas[schema_name]


# ----------------------------------------------------------------- waiting

def pick_ready(server_cfg, role_slot, max_parallel, need):
    """One look: is there pool room AND an acceptable free slot?

    Returns (ready, snapshot, chosen_slot_or_None).
    max_parallel <= 1 (glm): wait until NO slot is processing.
    role slot set: prefer (require) that slot free.

    kind vllm: vLLM has no slots — the role's slot pin does not apply and
    is ignored (chosen stays None). Ready when running + waiting is below
    max_parallel - reserve_slots; with max_parallel <= 1 the serial rule
    waits until nothing is running or waiting. llm_resources.fits_pool
    carries that check on a vllm snapshot (busy_ctx None -> counts).

    A transient HTTP-level failure (llm_resources.ServerError from probe)
    means "not ready for this poll" (snap=None), NOT a crash: the review
    found that an uncaught ServerError (e.g. 503 on /props) aborted the
    process with a traceback instead of polling until budget.timeout_s,
    defeating the exit-8 timeout contract path.
    """
    try:
        snap = llm_resources.probe(server_cfg)
    except llm_resources.ServerError:
        return False, None, None
    if llm_resources.server_kind(server_cfg) == "vllm":
        chosen = None
        if max_parallel <= 1:
            # serial rule: wait until nothing is running or waiting
            ready = (int(snap.slots_busy or 0) + int(snap.deferred or 0)) == 0
        else:
            ready = bool(llm_resources.fits_pool(snap, need))
        return (ready, snap, chosen if ready else None)
    try:
        slots = fetch_slots(server_cfg)
    except (urllib.error.URLError, OSError, ValueError):
        return False, snap, None
    if not isinstance(slots, list):
        return False, snap, None
    busy = [s for s in slots if s.get("is_processing")]
    # shared-pool admission goes through llm_resources.fits_pool so the
    # reserved pool slice (reserve_pool_ctx, admission accounting) cannot be
    # bypassed here; with reserve 0 the check is the old busy+need <= pool.
    fits_pool = llm_resources.fits_pool(snap, need)
    chosen = None
    ready = False
    if max_parallel <= 1:
        # glm rule: max_parallel=1 means wait until NO slot is processing.
        # Both views must agree that nothing is processing: the explicit /slots
        # poll taken here AND the probe snapshot (busy_ctx > 0 or slots_busy > 0
        # means the server still has work in flight).  Trusting only the second
        # read races with probe's own /slots read and can start on top of a
        # busy server.
        ready = (not busy) and snap.slots_busy == 0 and snap.busy_ctx == 0
        chosen = role_slot
    elif role_slot is not None:
        for s in slots:
            if str(s.get("id")) == str(role_slot):
                ready = (not s.get("is_processing")) and len(busy) < max_parallel
                chosen = role_slot
                break
    else:
        free = [s for s in slots if not s.get("is_processing")]
        ready = bool(free) and len(busy) < max_parallel
    if not fits_pool:
        ready = False
    return ready, snap, (chosen if ready else None)


def wait_for_room(server_cfg, role_slot, max_parallel, need, timeout_s):
    """Poll /slots every POLL_S up to timeout_s until fits_pool_now and a
    slot is free. Returns (snapshot, slot) or (None, None) on timeout.
    Probe errors (pick_ready -> snap=None) count as not-ready and simply
    keep polling until the deadline."""
    deadline = time.time() + max(float(timeout_s), 0.0)
    while True:
        ready, _snap, chosen = pick_ready(server_cfg, role_slot, max_parallel, need)
        if ready:
            return _snap, chosen
        if time.time() >= deadline:
            return None, None
        time.sleep(POLL_S)


# ----------------------------------------------------------------- logging

def append_log(cfg, entry):
    """One jsonl line: counts/ids only — never bearer, prompt or output text."""
    path = os.path.expanduser(cfg.get("log") or DEFAULT_LOG)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


# ----------------------------------------------------------------- main

def _run(argv=None):
    ap = argparse.ArgumentParser(
        prog="model_call.py",
        description="One governed llama-server call for a configured role.")
    ap.add_argument("--role", required=True)
    ap.add_argument("--files", nargs="+", action="extend", default=[],
                    metavar="FILE",
                    help="files to include: '--files a b c' and/or repeated "
                         "'--files a --files b' (sent in sorted path order)")
    ap.add_argument("--diff", help="path to a diff file")
    ap.add_argument("--ask", required=True, help="path to the ask text file")
    ap.add_argument("--out", help="write the result here")
    ap.add_argument("--max-tokens", type=int, default=None)
    ap.add_argument("--reasoning-effort", default=None,
                    choices=["low", "medium", "high"],
                    help="overrides the role's reasoning_effort in the request "
                         "body (glm honours this field; chat_template_kwargs "
                         "and budget knobs do not work there)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the budget/plan without calling")
    ap.add_argument("--config", default=None,
                    help="llm_servers.json (default: next to this module)")
    ap.add_argument("--resources", default=None, metavar="PATH",
                    help="C6 resources file (bin/resources.json shape); when "
                         "given, wins over --config and the defaults")
    ap.add_argument("--ticket", default=None,
                    help="hub ticket id for Jobs reporting (default: env AIOPS_TICKET)")
    args = ap.parse_args(argv)

    # Config source: --resources, else --config, else bin/resources.json when
    # it exists, else bin/llm_servers.json as today. Everything from here on
    # is unchanged — the config dict just may come from a resources file now.
    if args.resources is not None:
        cfg_path = args.resources
    elif args.config is not None:
        cfg_path = args.config
    else:
        cfg_path = _config_source_path(base_dir=_HERE)
    cfg_dir = os.path.dirname(os.path.abspath(os.path.expanduser(str(cfg_path))))
    try:
        # --resources is always a C6 file, whatever its name; --config and
        # the default are always llm_servers.json-shaped (Claude review).
        if args.resources is not None:
            cfg = _load_resources_file(cfg_path)
        else:
            cfg = llm_resources.load_config(cfg_path)
    except ResourcesError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return EXIT_ERROR
    except (OSError, ValueError) as exc:
        print("error: cannot load config: %s" % type(exc).__name__, file=sys.stderr)
        return EXIT_ERROR

    roles = cfg.get("roles", {})
    if args.role not in roles:
        print("error: unknown role %s" % args.role, file=sys.stderr)
        return EXIT_ERROR
    role = roles[args.role]
    server_name = role["server"]
    server_cfg = cfg["servers"][server_name]
    margin = float(cfg.get("margin", 0.05))
    max_tokens = args.max_tokens if args.max_tokens is not None else int(role.get("max_tokens", 1024))
    # CLI --reasoning-effort overrides the role's reasoning_effort; when neither
    # is set the field is omitted entirely from the request body.
    reasoning_effort = (args.reasoning_effort if args.reasoning_effort is not None
                        else role.get("reasoning_effort"))
    role_slot = role.get("slot")
    max_parallel = int(server_cfg.get("max_parallel", 1))
    # vLLM has no slots: a role's slot pin does not apply there and is
    # ignored (admission counts requests; summary/log show slot=None).
    if llm_resources.server_kind(server_cfg) == "vllm":
        role_slot = None

    # ---- build
    try:
        preamble = _read_text(_resolve(cfg_dir, role["preamble"]))
        user_prompt = build_user_prompt(args.files, args.diff, args.ask)
    except (OSError, KeyError) as exc:
        print("error: cannot build prompt: %s" % type(exc).__name__, file=sys.stderr)
        return EXIT_ERROR

    # ---- count -> budget
    try:
        tokens_in = (llm_resources.count_tokens(server_cfg, preamble)
                     + llm_resources.count_tokens(server_cfg, user_prompt))
        snap = llm_resources.probe(server_cfg)
        # per-role call-log medians take precedence over live metrics rates
        rates = llm_resources.log_rates(args.role, cfg)
        bud = llm_resources.budget(snap, tokens_in, max_tokens, margin,
                                   rates=rates)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print("error: probe/count failed: %s: %s" % (type(exc).__name__,
              getattr(exc, "reason", "")), file=sys.stderr)
        return EXIT_ERROR

    if args.dry_run:
        print("dry-run role=%s server=%s tokens_in=%s max_tokens=%s need=%s "
              "max_input_tokens=%s fits_slot=%s fits_pool_now=%s busy_ctx=%s "
              "pool_ctx=%s per_slot_ctx=%s est_prefill_s=%s est_gen_s=%s timeout_s=%s"
              % (args.role, server_name, tokens_in, max_tokens, bud.need,
                 bud.max_input_tokens, bud.fits_slot, bud.fits_pool_now,
                 snap.busy_ctx, snap.pool_ctx, snap.per_slot_ctx,
                 bud.est_prefill_s, bud.est_gen_s, bud.timeout_s))
        return EXIT_OK

    if snap.per_slot_ctx is None:
        # Neither /props, /slots, nor the listening process reported an
        # n_ctx: fits_slot could never be true, so the exit-4 "input too
        # large" below would be misleading. Distinct message, distinct path.
        print("per-slot context unknown", file=sys.stderr)
        return EXIT_PER_SLOT_UNKNOWN

    if not bud.fits_slot:
        print("input too large: %d > max_input_tokens %d — split"
              % (tokens_in, bud.max_input_tokens), file=sys.stderr)
        return EXIT_TOO_LARGE

    # ---- wait for pool room + free slot (the real call starts here; main() reports from this point)
    if _ON_CALL is not None:
        _ON_CALL()
    _snap2, chosen = wait_for_room(server_cfg, role_slot, max_parallel,
                                   bud.need, bud.timeout_s)
    if _snap2 is None:
        print("timeout: no slot/pool room for role=%s server=%s within %ss"
              % (args.role, server_name, int(bud.timeout_s)), file=sys.stderr)
        return EXIT_NO_SLOT

    # ---- call
    payload = {
        "model": role.get("model"),
        "messages": [
            {"role": "system", "content": preamble},   # byte-identical every call
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "cache_prompt": True,
    }
    if role_slot is not None:
        payload["id_slot"] = role_slot
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort
    schema_name = role.get("schema")
    if schema_name:
        try:
            schema = load_role_schema(cfg_dir, schema_name)
        except (OSError, KeyError, ValueError) as exc:
            print("error: cannot load schema: %s" % type(exc).__name__, file=sys.stderr)
            return EXIT_ERROR
        payload["response_format"] = {"type": "json_schema",
                                      "json_schema": {"name": schema_name,
                                                      "schema": schema}}

    try:
        t0 = time.time()
        resp = http_json("POST",
                         server_cfg["url"].rstrip("/") + "/v1/chat/completions",
                         read_bearer(server_cfg.get("bearer_file")),
                         payload, timeout=max(bud.timeout_s, HTTP_TIMEOUT_S))
        elapsed_s = time.time() - t0  # wall time of the provider round-trip
    except urllib.error.HTTPError as exc:
        print("error: chat completion failed: HTTP %s %s" % (exc.code, exc.reason),
              file=sys.stderr)
        return EXIT_ERROR
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print("error: chat completion failed: %s: %s" % (type(exc).__name__,
              getattr(exc, "reason", "")), file=sys.stderr)
        return EXIT_ERROR

    try:
        choice = resp["choices"][0]
        content = (choice.get("message") or {}).get("content") or ""
        finish = choice.get("finish_reason")
    except (KeyError, IndexError, TypeError, AttributeError):
        print("error: malformed completion response (no choices[0].message)",
              file=sys.stderr)
        return EXIT_ERROR
    timings = resp.get("timings") or {}
    if not timings and isinstance(resp.get("usage"), dict):
        # vLLM sends no llama-server `timings`, only OpenAI `usage`: take the
        # token counts from it so the call is metered; the millisecond
        # timings stay None, never invented (Claude, 2026-11-10).
        usage = resp["usage"]
        details = usage.get("prompt_tokens_details") or {}
        timings = {"prompt_n": usage.get("prompt_tokens"),
                   "predicted_n": usage.get("completion_tokens"),
                   "cache_n": details.get("cached_tokens") if isinstance(details, dict) else None,
                   "prompt_ms": None, "predicted_ms": None}

    # ---- usage ledger fields (added keys only; the pre-existing schema is
    # unchanged so old consumers reading ts/role/tokens_in keep working)
    tokens_out = timings.get("predicted_n")  # completion_tokens on the vLLM path
    prompt_tps = None
    gen_tps = None
    if timings.get("prompt_ms") and timings.get("prompt_n"):
        prompt_tps = round(timings["prompt_n"] * 1000.0 / timings["prompt_ms"], 2)
    if timings.get("predicted_ms") and timings.get("predicted_n"):
        gen_tps = round(timings["predicted_n"] * 1000.0 / timings["predicted_ms"], 2)

    data = None
    verdict = None
    if content.strip():
        try:
            data = json.loads(content)
        except ValueError:
            data = None
        if isinstance(data, dict):
            verdict = data.get("verdict")

    # ---- log (counts/ids only, no text) — even for failed outcomes
    append_log(cfg, {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "role": args.role,
        "server": server_name,
        "slot": role_slot,
        "effort": reasoning_effort,
        "tokens_in": tokens_in,
        "prompt_n": timings.get("prompt_n"),
        "cache_n": timings.get("cache_n"),
        "prompt_ms": timings.get("prompt_ms"),
        "predicted_n": timings.get("predicted_n"),
        "predicted_ms": timings.get("predicted_ms"),
        "tokens_out": tokens_out,
        "elapsed_s": round(elapsed_s, 3),
        "prompt_tps": prompt_tps,
        "gen_tps": gen_tps,
        "cache_hit_n": timings.get("cache_n"),
        "finish": finish,
        "verdict": verdict,
        "ticket": args.ticket or os.environ.get("AIOPS_TICKET"),
    })

    if finish == "length" and data is None:
        # Output was cut off at max_tokens before any complete JSON came
        # out: empty content (reasoning consumed the whole budget) or
        # truncated mid-JSON — same root cause, same advice.  The log
        # above already recorded finish and verdict null.
        print("budget too small (output cut at max_tokens=%d; raise "
              "--max-tokens or tighten the schema)" % max_tokens,
              file=sys.stderr)
        return EXIT_BUDGET_SMALL
    if not content.strip():
        print("error: empty completion (finish=%s)" % finish, file=sys.stderr)
        return EXIT_ERROR
    if schema_name and data is None:
        # finish was "stop" (the length case returned above): the model
        # ignored the schema and produced prose, not JSON.
        print("error: schema role %s returned non-JSON content (finish=%s)"
              % (args.role, finish), file=sys.stderr)
        return EXIT_BAD_JSON

    # ---- write result
    if args.out:
        text = (json.dumps(data, indent=2, ensure_ascii=False) + "\n"
                if (schema_name and data is not None) else content)
        out_parent = os.path.dirname(os.path.abspath(args.out))
        if out_parent:
            os.makedirs(out_parent, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)

    # ---- one summary line (counts/ids only)
    print("ok role=%s server=%s slot=%s effort=%s in=%s prompt_n=%s cache_n=%s prompt_ms=%s "
          "predicted_n=%s predicted_ms=%s out_n=%s elapsed_s=%s prompt_tps=%s gen_tps=%s "
          "finish=%s verdict=%s%s"
          % (args.role, server_name, role_slot, reasoning_effort, tokens_in,
             timings.get("prompt_n"), timings.get("cache_n"), timings.get("prompt_ms"),
             timings.get("predicted_n"), timings.get("predicted_ms"),
             tokens_out, round(elapsed_s, 3), prompt_tps, gen_tps, finish, verdict,
             (" out=%s" % args.out) if args.out else ""))
    return EXIT_OK


_ON_CALL = None   # set by main(): invoked once validation is done and the real call begins
_META_MAP = {"role": "role", "server": "server", "slot": "slot", "finish": "finish", "verdict": "verdict",
             "in": "prompt_tokens", "cache_n": "cache_tokens", "predicted_n": "completion_tokens"}


class _Tee:
    """Pass writes through to `stream` and keep them (our own one-line status output only)."""
    def __init__(self, stream):
        self.stream, self.buf = stream, []
    def write(self, s):
        self.buf.append(s)
        return self.stream.write(s)
    def flush(self):
        self.stream.flush()
    def __getattr__(self, name):
        return getattr(self.stream, name)


def _flag(argv, name):
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


def main(argv=None):
    """Run one call; report it to the hub Jobs page only once the real call begins
    (no record for --dry-run or arg/config/prompt errors)."""
    global _ON_CALL
    argv = sys.argv[1:] if argv is None else list(argv)
    if "--dry-run" in argv or not hub_jobs.enabled():
        return _run(argv)
    role = _flag(argv, "--role") or "unknown"
    ticket = _flag(argv, "--ticket") or os.environ.get("AIOPS_TICKET")
    rep = hub_jobs.JobReport("%s:agent:model_call:%s" % (hub_jobs.node_name(), role), kind="agent",
                             name="model_call %s" % role, ticket_id=ticket)
    started = []
    out, err = _Tee(sys.stdout), _Tee(sys.stderr)
    old = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    _ON_CALL = lambda: started.append(rep.start())
    rc = EXIT_ERROR
    try:
        rc = _run(argv)
        return rc
    finally:
        _ON_CALL = None
        sys.stdout, sys.stderr = old
        if started:
            summary = hub_jobs.summary_line("".join(out.buf) + "\n" + "".join(err.buf))
            meta = {_META_MAP[k]: v for k, v in (p.split("=", 1) for p in summary.split() if "=" in p)
                    if k in _META_MAP}
            rep.finish("ok" if rc == EXIT_OK else "failed", exit_code=rc, summary=summary, meta=meta)


if __name__ == "__main__":
    sys.exit(main())
