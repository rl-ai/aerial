"""RC onboarding tests — V12 (spec §7/§8) + live HTTP fixture for the installer.

Run: pytest build/dashboard/tests/test_rc_onboarding.py -q
"""

import http.server
import json
import socket
import sys
import threading
from pathlib import Path

import pytest

BUILD_DASHBOARD = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_DASHBOARD))

from console import installer  # noqa: E402


# --------------------------------------------------------------------------
# V12 — first_look_banner logic
# --------------------------------------------------------------------------

def test_v12_read_only_banner_shown_when_no_write_token():
    banner = installer.first_look_banner(write_token_present=False, banner_dismissed=False)
    assert banner is not None
    assert "read-only until your write token is granted" in banner
    assert installer.DOCS_LINK in banner


def test_v12_decisions_enabled_banner_shown_once():
    first = installer.first_look_banner(write_token_present=True, banner_dismissed=False)
    assert first == installer.DECISIONS_ENABLED_BANNER
    # second successive call after the once-banner returns None — never again
    second = installer.first_look_banner(write_token_present=True, banner_dismissed=True)
    assert second is None


def test_v12_read_only_banner_dismissed_stays_gone():
    assert installer.first_look_banner(False, False) is not None
    assert installer.first_look_banner(False, True) is None


def test_v12_banner_state_persists_in_config(tmp_path):
    cfg = tmp_path / "config.json"
    config = installer.load_config(cfg)
    config.setdefault("banner_dismissed", False)
    installer.save_config(cfg, config)
    assert installer.load_config(cfg)["banner_dismissed"] is False
    # operator dismisses → persisted in the config file
    config["banner_dismissed"] = True
    installer.save_config(cfg, config)
    assert installer.load_config(cfg)["banner_dismissed"] is True
    assert installer.first_look_banner(False, config["banner_dismissed"]) is None


# --------------------------------------------------------------------------
# Live local HTTP fixture for the installer (✓ path and exact-failing-check path)
# --------------------------------------------------------------------------

class _HubHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        auth = self.headers.get("Authorization", "")
        if auth != "Bearer good-read-token":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"credentials required")
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):  # silence fixture logs
        pass


@pytest.fixture()
def hub_url():
    server = http.server.HTTPServer(("127.0.0.1", 0), _HubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    thread.join(timeout=5)


def test_installer_success_path_with_missing_write_token(hub_url, tmp_path, capsys):
    read_token = tmp_path / "read-token"
    read_token.write_text("good-read-token")
    cfg = tmp_path / "config.json"
    installer.save_config(
        cfg,
        {"read_token_path": str(read_token), "write_token_path": str(tmp_path / "none")},
    )
    rc = installer.run_install(hub_url, cfg, timeout=5.0)
    out = capsys.readouterr().out
    assert rc == 0
    assert "✓ install complete" in out
    assert "connectivity check passed" in out
    assert installer.WRITE_TOKEN_MISSING_MSG in out
    saved = json.loads(cfg.read_text())
    assert saved["hub_url"] == hub_url
    assert saved["read_token_path"] == str(read_token)
    # exact "write token: missing" wording, not a failure exit
    assert "write token: missing — decisions disabled until granted" in out


def test_installer_exact_failing_check_unreachable(tmp_path, capsys):
    # grab a closed port → guaranteed connection-refused fixture
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()
    cfg = tmp_path / "config.json"
    read_token = tmp_path / "read-token"
    read_token.write_text("good-read-token")
    installer.save_config(cfg, {"read_token_path": str(read_token)})
    rc = installer.run_install(f"http://127.0.0.1:{dead_port}", cfg, timeout=2.0)
    out = capsys.readouterr().out
    assert rc == 1
    assert "connectivity check failed" in out
    assert f"http://127.0.0.1:{dead_port}" in out


def test_installer_exact_failing_check_token_rejected(hub_url, tmp_path, capsys):
    read_token = tmp_path / "read-token"
    read_token.write_text("wrong-token")
    cfg = tmp_path / "config.json"
    installer.save_config(cfg, {"read_token_path": str(read_token)})
    rc = installer.run_install(hub_url, cfg, timeout=5.0)
    out = capsys.readouterr().out
    assert rc == 1
    assert "read token rejected" in out


def test_installer_exact_failing_check_missing_token_file(hub_url, tmp_path, capsys):
    cfg = tmp_path / "config.json"
    rc = installer.run_install(hub_url, cfg, timeout=5.0)
    out = capsys.readouterr().out
    assert rc == 1
    assert "read token check failed: file not found" in out


def test_installer_present_write_token(hub_url, tmp_path, capsys):
    read_token = tmp_path / "read-token"
    read_token.write_text("good-read-token")
    write_token = tmp_path / "write-token"
    write_token.write_text("decisions-token")
    cfg = tmp_path / "config.json"
    saved = installer.load_config(cfg)
    saved["read_token_path"] = str(read_token)
    saved["write_token_path"] = str(write_token)
    installer.save_config(cfg, saved)
    rc = installer.run_install(hub_url, cfg, timeout=5.0)
    out = capsys.readouterr().out
    assert rc == 0
    assert "✓ write token: present — decisions enabled" in out


def test_cli_entrypoint(tmp_path, capsys):
    read_token = tmp_path / "read-token"
    read_token.write_text("good-read-token")
    cfg = tmp_path / "config.json"
    config = {"read_token_path": str(read_token), "write_token_path": str(tmp_path / "none")}
    installer.save_config(cfg, config)
    rc = installer.main(["--hub-url", "not-a-url", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "hub-url check failed" in out
