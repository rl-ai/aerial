"""Where the hub keeps local state. Override with TICKET_HUB_HOME."""
import os

def hub_home() -> str:
    return os.environ.get("TICKET_HUB_HOME") or os.path.expanduser("~/.ticket-hub")

def hub_path(*parts: str) -> str:
    return os.path.join(hub_home(), *parts)
