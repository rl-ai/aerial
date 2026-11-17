#!/usr/bin/env python3
"""Deployment profile loader for Aerial OSS.

Deployment-specific values (node names, email recipients, host paths,
service units) never live in the code. They come from, in order of
precedence:

  1. Environment variables:  ``AERIAL_<SECTION>__<KEY>`` (double
     underscore separates nesting levels), e.g.
     ``AERIAL_EMAIL__RECIPIENT_EMAIL=ops@example.com``.
  2. ``config/deployment.yaml`` (gitignored) — a copy of
     ``config/deployment.yaml.example`` customized per deployment.
  3. The generic defaults below — the OSS version runs with these alone:
     localhost URLs, empty recipients (email disabled), and repo-relative
     paths. No Ray-specific values anywhere.

Usage::

    from deployment_profile import value
    node = value("cluster.node_name")
"""
from __future__ import annotations

import copy
import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

#: Generic defaults — safe for any installation, no personal values.
DEFAULTS: dict = {
    "email": {
        # Empty recipient = email delivery disabled (send_* are no-ops).
        "recipient_email": "",
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "secrets_dir": "~/.aerial/secrets/gmail",
    },
    "paths": {
        # Root for all runtime state (logs, data, workspaces, secrets).
        "state_dir": "~/.aerial",
        # label -> filesystem path of a git repo scanned for commit digests.
        # Empty (or missing) repos are skipped silently.
        "repo_paths": {},
        "python_bin": "python3",
        # Empty = derived from the repo root (integration/, bin/job_run.py).
        "integration_dir": "",
        "exception_queue_fallback": "",
        "data_layer_fallback": "",
        "job_run_script": "",
        # Empty = generic default under state_dir (see GENERIC_SUBPATHS).
        "scrapes_db": "",
        "gateway_v1": "",
        "sources_path": "",
        "stress_dir": "",
        "sre_state": "",
        "sre_log": "",
        "hub_db": "",
        "health_status": "",
        "cluster_bearer": "",
        "model_calls_ledger": "",
        "board_html": "",
        "scheduler_log": "",
        "shadow_dir": "",
        "ops_workspace_root": "",
        "run_dir": "",
        "ops_log": "",
        "email_log": "",
        "bridge_log": "",
        "digest_latest": "",
        "dogfood_log": "",
        "proposals_ledger": "",
        "validation_flags": "",
        "validation_shadow": "",
        "token_ref": "",
        "console_workdir": "",
        "a5_runtime_db": "",
        "models_root": "",
        "model_call_script": "",
    },
    "cluster": {
        "node_name": "aerial",
        "provider": "custom:aerial-local",
        "model": "local-model",
        "kimi_service_unit": "aerial-kimi.service",
        "kimi_model_path": "",
        "qwen_container": "aerial-qwen",
        "hub_url": "http://127.0.0.1:8020",
        "hub_token_file": "~/.aerial/secrets/hub-token",
        "hub_write_token_file": "",   # mutations; empty = reuse hub_token_file
        "epic_id": "",
        "hermes_bin": "hermes",
    },
}

#: Friendly flat aliases (in addition to the AERIAL_SECTION__KEY form).
_ALIASES = {
    "AERIAL_RECIPIENT_EMAIL": "email.recipient_email",
    "AERIAL_SMTP_HOST": "email.smtp_host",
    "AERIAL_SMTP_PORT": "email.smtp_port",
    "AERIAL_SMTP_SECRETS_DIR": "email.secrets_dir",
    "AERIAL_NODE_NAME": "cluster.node_name",
    "AERIAL_PROVIDER": "cluster.provider",
    "AERIAL_MODEL": "cluster.model",
    "AERIAL_HUB_URL": "cluster.hub_url",
    "AERIAL_HUB_TOKEN_FILE": "cluster.hub_token_file",
    "AERIAL_HUB_WRITE_TOKEN_FILE": "cluster.hub_write_token_file",
    "AERIAL_EPIC_ID": "cluster.epic_id",
    "AERIAL_HERMES_BIN": "cluster.hermes_bin",
    "AERIAL_PYTHON_BIN": "paths.python_bin",
    "AERIAL_INTEGRATION_DIR": "paths.integration_dir",
    "AERIAL_SCRAPES_DB": "paths.scrapes_db",
    "AERIAL_GATEWAY_V1": "paths.gateway_v1",
    "AERIAL_SOURCES_PATH": "paths.sources_path",
    "AERIAL_STRESS_DIR": "paths.stress_dir",
    "AERIAL_SRE_STATE": "paths.sre_state",
    "AERIAL_CONSOLE_WORKDIR": "paths.console_workdir",
    "AERIAL_JOB_RUN_SCRIPT": "paths.job_run_script",
    "AERIAL_KIMI_SERVICE_UNIT": "cluster.kimi_service_unit",
    "AERIAL_KIMI_MODEL_PATH": "cluster.kimi_model_path",
    "AERIAL_QWEN_CONTAINER": "cluster.qwen_container",
}


def _coerce(raw: str):
    s = raw.strip()
    if s.lower() in ("true", "yes", "on"):
        return True
    if s.lower() in ("false", "no", "off"):
        return False
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def _parse_simple_yaml(text: str) -> dict:
    """Tiny indent-based parser for the flat mapping YAML we ship.

    Supports nested maps, scalar values, and comments. No lists, anchors,
    or flow syntax — keep deployment.yaml.example that simple.
    """
    root: dict = {}
    stack = [(-1, root)]
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        key, _, val = line.strip().partition(":")
        key = key.strip()
        val = val.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if val == "":
            child: dict = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _coerce(val)
    return root


def _flatten(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        else:
            out[key] = v
    return out


_cache: dict | None = None


def load(refresh: bool = False) -> dict:
    """Load the effective profile: defaults <- deployment.yaml <- env."""
    global _cache
    if _cache is not None and not refresh:
        return _cache
    merged = copy.deepcopy(DEFAULTS)
    cfg_path = REPO_ROOT / "config" / "deployment.yaml"
    if cfg_path.exists():
        try:
            file_vals = _flatten(
                _parse_simple_yaml(cfg_path.read_text(encoding="utf-8")))
            for key, val in file_vals.items():
                merged.setdefault(key.split(".")[0], {})[
                    ".".join(key.split(".")[1:])] = val
        except Exception:
            pass  # a broken profile must never take the service down
    for env_key, dotted in _ALIASES.items():
        if env_key in os.environ:
            section, key = dotted.split(".", 1)
            merged.setdefault(section, {})[key] = _coerce(os.environ[env_key])
    prefix = "AERIAL_"
    for env_key, val in os.environ.items():
        if not env_key.startswith(prefix) or "__" not in env_key:
            continue
        dotted = env_key[len(prefix):].lower().replace("__", ".")
        section, key = dotted.split(".", 1)
        merged.setdefault(section, {})[key] = _coerce(val)
    _cache = merged
    return merged


#: Generic default location (under paths.state_dir) for each optional
#: paths.* key. pvalue(key) returns the profile value if set, else this.
GENERIC_SUBPATHS = {
    "scrapes_db": "data/scrapes.db",
    "sre_state": "logs/sre-health-state.json",
    "sre_log": "logs/sre-health.log",
    "hub_db": "hub/hub.db",
    "health_status": "hub/health-status.json",
    "cluster_bearer": "secrets/cluster_bearer",
    "model_calls_ledger": "ledger/model-calls.jsonl",
    "board_html": "daily-board.html",
    "scheduler_log": "shadow/scheduler.log",
    "shadow_dir": "shadow",
    "ops_workspace_root": "ops",
    "run_dir": "run",
    "ops_log": "logs/ops-agent.log",
    "email_log": "logs/email-delivery.log",
    "bridge_log": "logs/brain-bridge.log",
    "digest_latest": "data/bridge/digest-latest.json",
    "dogfood_log": "data/dogfood-log.md",
    "proposals_ledger": "data/bridge/proposals-ledger.json",
    "validation_flags": "logs/aerial-validation-flags.json",
    "validation_shadow": "logs/aerial-validation-shadow.jsonl",
    "token_ref": "secrets/a5-test-01-read.token",
    "stress_dir": "stress",
    "console_workdir": "backtest/gauntlet",
    "a5_runtime_db": "hub/a5-runtime.db",
    "models_root": "models",
}


def state_dir() -> Path:
    return Path(os.path.expanduser(str(value("paths.state_dir"))))


def pvalue(key: str) -> Path | None:
    """Path for an optional paths.* key: profile value, else the generic
    default under state_dir. Use for every deployment-specific path.
    A registered-but-defaultless key (e.g. email-gated paths) returns None
    rather than KeyError — the caller decides what an unset path means."""
    raw = value(f"paths.{key}") or ""
    if raw:
        return Path(os.path.expanduser(str(raw)))
    generic = GENERIC_SUBPATHS.get(key)
    if generic is None:
        return None
    return state_dir() / generic


def value(dotted_key: str, default=None):
    """Fetch profile value by dotted key, e.g. value("cluster.node_name").

    A bare key that names an optional paths.* entry (e.g. value("hub_db"))
    dispatches to pvalue() and returns the resolved Path — so callers can
    use one import for both dotted lookups and path resolution.
    """
    if "." not in dotted_key and dotted_key in GENERIC_SUBPATHS:
        return pvalue(dotted_key)
    if "." not in dotted_key:
        # Bare key: resolve against the profile sections in order.
        merged = load()
        for section in ("cluster", "email", "paths"):
            if dotted_key in merged.get(section, {}):
                return merged[section][dotted_key]
        return default
    section, key = dotted_key.split(".", 1)
    return load().get(section, {}).get(key, default)


def repo_root() -> Path:
    return REPO_ROOT


def derive_path(key: str, subpath: str = "") -> Path:
    """Path for a profile key, falling back to a repo-relative default.

    If the profile value is empty, ``REPO_ROOT / subpath`` is used.
    """
    raw = value(key) or ""
    if raw:
        return Path(os.path.expanduser(str(raw)))
    return REPO_ROOT / subpath if subpath else REPO_ROOT
