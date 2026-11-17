"""ticket_hub.paths: neutral default home + TICKET_HUB_HOME override."""
import importlib.util
import os

from ticket_hub import paths


def _fresh(module_name: str):
    """Load a fresh module instance under an isolated name (re-executes the
    module so import-time constants see the current env, without mutating the
    shared module other tests already imported)."""
    spec = importlib.util.find_spec(module_name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_default_hub_home(monkeypatch):
    monkeypatch.delenv("TICKET_HUB_HOME", raising=False)
    assert paths.hub_home() == os.path.expanduser("~/.ticket-hub")
    assert paths.hub_path("hub.db") == os.path.join(
        os.path.expanduser("~/.ticket-hub"), "hub.db")
    assert paths.hub_path("logs", "server.log") == os.path.join(
        os.path.expanduser("~/.ticket-hub"), "logs", "server.log")


def test_env_override(monkeypatch):
    monkeypatch.setenv("TICKET_HUB_HOME", "/tmp/alt-hub-home")
    assert paths.hub_home() == "/tmp/alt-hub-home"
    assert paths.hub_path("hub.db") == "/tmp/alt-hub-home/hub.db"


def test_runtime_defaults_resolve_under_hub_home(monkeypatch):
    """seats.DEFAULT_ROLES_PATH / templates.USER_TEMPLATES are computed via
    hub_path at import time, so a fresh load after setenv must see the
    override."""
    monkeypatch.setenv("TICKET_HUB_HOME", "/tmp/alt-hub-home")
    seats = _fresh("seats")
    templates = _fresh("templates")
    assert seats.DEFAULT_ROLES_PATH == os.path.join(
        "/tmp/alt-hub-home", "roles.json")
    assert templates.USER_TEMPLATES == os.path.join(
        "/tmp/alt-hub-home", "templates")
