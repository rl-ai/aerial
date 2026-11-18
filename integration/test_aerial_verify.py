"""Tests for aerial_verify.py — unit tests with fakes, no network, no hub
deps. Network checks use a tiny http.server on 127.0.0.1:0 in a thread.
"""
import http.server
import importlib.util
import json
import os
import socketserver
import stat
import sys
import tempfile
import threading
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import aerial_verify  # noqa: E402


def load_module():
    """Fresh import of aerial_verify from the repo root."""
    spec = importlib.util.spec_from_file_location(
        "aerial_verify", os.path.join(REPO_ROOT, "aerial_verify.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Handler(http.server.BaseHTTPRequestHandler):
    token = "test-token-123"
    health_body = b'{"ok": true}'

    def do_GET(self):
        auth = self.headers.get("Authorization", "")
        if self.path == "/dashboard/health":
            if auth != f"Bearer {self.token}":
                self.send_response(401)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(self.health_body)
        elif self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(self.health_body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):  # silence
        pass


class _Server:
    """Tiny http.server on 127.0.0.1:0 in a background thread."""

    def __init__(self, handler=Handler):
        self.handler = handler
        socketserver.TCPServer.allow_reuse_address = True
        self.httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"


class UnitCheckTests(unittest.TestCase):
    """Direct tests of the individual check functions with fakes."""

    def test_python_pass(self):
        c = aerial_verify.check_python()
        self.assertEqual(c.status, "PASS")

    def test_imports_pass(self):
        c = aerial_verify.check_imports()
        self.assertEqual(c.status, "PASS")
        self.assertIn("dashboard", c.detail)

    def test_imports_fail_reports_export_line(self):
        def fake_import(name, *a, **kw):
            if name in ("deployment_profile", "dashboard"):
                raise ImportError(name)
            return real_import(name, *a, **kw)

        import builtins
        real_import = builtins.__import__
        try:
            builtins.__import__ = fake_import
            c = aerial_verify.check_imports()
        finally:
            builtins.__import__ = real_import
        self.assertEqual(c.status, "FAIL")
        self.assertIn("PYTHONPATH", c.fix)

    def test_hub_deps_fail_has_pip_line(self):
        # fastapi/uvicorn are NOT installed here -> FAIL with the pip line
        c = aerial_verify.check_hub_deps()
        self.assertEqual(c.status, "FAIL")
        self.assertIn("pip install -r hub/requirements.txt", c.fix)

    def test_hub_deps_pass_when_specs_exist(self):
        real = aerial_verify.importlib.util.find_spec

        def fake_spec(name):
            return real(name) or object()

        aerial_verify.importlib.util.find_spec = staticmethod(fake_spec)
        try:
            c = aerial_verify.check_hub_deps()
        finally:
            aerial_verify.importlib.util.find_spec = real
        self.assertEqual(c.status, "PASS")

    def test_state_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = aerial_verify.check_state_dirs(tmp)
            self.assertEqual(c.status, "FAIL")
            for sub in ("secrets", "logs", "data"):
                self.assertIn(sub, c.detail)
            for sub in ("secrets", "logs", "data"):
                os.makedirs(os.path.join(tmp, sub))
            c = aerial_verify.check_state_dirs(tmp)
            self.assertEqual(c.status, "PASS")

    def test_token_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "hub-token")

            c, token = aerial_verify.check_token_file(path)
            self.assertEqual(c.status, "FAIL")
            self.assertEqual(token, "")

            open(path, "w").write("")
            c, token = aerial_verify.check_token_file(path)
            self.assertEqual(c.status, "FAIL")
            self.assertEqual(token, "")

            open(path, "w").write("sekrit")
            os.chmod(path, 0o644)
            c, token = aerial_verify.check_token_file(path)
            self.assertEqual(c.status, "FAIL")
            self.assertIn("chmod 600", c.fix)
            self.assertEqual(token, "")

            os.chmod(path, 0o600)
            c, token = aerial_verify.check_token_file(path)
            self.assertEqual(c.status, "PASS")
            self.assertEqual(token, "sekrit")

    def test_hub_health(self):
        with _Server() as srv:
            c = aerial_verify.check_hub_health(srv.url)
            self.assertEqual(c.status, "PASS")
            c = aerial_verify.check_hub_health("http://127.0.0.1:1")
            self.assertEqual(c.status, "FAIL")
            self.assertIn("hub/api.py", c.fix)

    def test_hub_health_ok_false(self):
        class H(Handler):
            health_body = b'{"ok": false}'

        with _Server(H) as srv:
            c = aerial_verify.check_hub_health(srv.url)
            self.assertEqual(c.status, "FAIL")

    def test_console_auth(self):
        with _Server() as srv:
            c = aerial_verify.check_console(srv.url, Handler.token)
            self.assertEqual(c.status, "PASS")
            c = aerial_verify.check_console(srv.url, "wrong-token")
            self.assertEqual(c.status, "FAIL")
            self.assertEqual(
                aerial_verify.check_console(srv.url, "").status, "SKIP")

    def test_console_no_bearer_not_refused(self):
        class OpenH(Handler):
            def do_GET(self):
                if self.path == "/dashboard/health":
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{"ok": true}')

        with _Server(OpenH) as srv:
            c = aerial_verify.check_console(srv.url, Handler.token)
            self.assertEqual(c.status, "FAIL")
            self.assertIn("401", c.detail)
            self.assertIn("--open-read", c.fix)

    def test_ops_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            # no profile available in the test env -> generic default path;
            # patch the module-level profile lookup instead
            log = os.path.join(tmp, "ops-agent.log")

            orig = aerial_verify._profile
            class FakeProf:
                @staticmethod
                def pvalue(key):
                    return log if key == "ops_log" else None
            aerial_verify._profile = lambda: FakeProf()
            try:
                c = aerial_verify.check_ops_log()
                self.assertEqual(c.status, "FAIL")
                self.assertIn("--once", c.fix)

                open(log, "w").write("WARNING nothing\n")
                c = aerial_verify.check_ops_log()
                self.assertEqual(c.status, "FAIL")

                open(log, "a").write("2026-01-01 00:00:00 INFO poll ok\n")
                c = aerial_verify.check_ops_log()
                self.assertEqual(c.status, "PASS")
            finally:
                aerial_verify._profile = orig


class MainTests(unittest.TestCase):
    def _run(self, argv, env=None):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        old = dict(os.environ)
        if env:
            os.environ.update(env)
        try:
            with redirect_stdout(buf):
                code = aerial_verify.main(argv)
        finally:
            os.environ.clear()
            os.environ.update(old)
        return code, buf.getvalue()

    def test_all_pass_exit_0(self):
        with tempfile.TemporaryDirectory() as tmp:
            for sub in ("secrets", "logs", "data"):
                os.makedirs(os.path.join(tmp, sub))
            token_path = os.path.join(tmp, "secrets", "hub-token")
            open(token_path, "w").write(Handler.token)
            os.chmod(token_path, 0o600)
            log = os.path.join(tmp, "logs", "ops-agent.log")
            open(log, "w").write("x INFO full poll\n")

            # patch the profile so cluster/paths resolve into tmp, and fake
            # the hub-deps check PASS (fastapi/uvicorn are not installed in
            # the test env) so the all-pass path is exercised
            servers = []

            class FakeProf:
                @staticmethod
                def value(key):
                    return {"cluster.hub_url": servers[0].url,
                            "cluster.hub_token_file": token_path}.get(key)

                @staticmethod
                def pvalue(key):
                    return log if key == "ops_log" else None

            orig_prof = aerial_verify._profile
            orig_deps = aerial_verify.check_hub_deps
            aerial_verify._profile = lambda: FakeProf()
            aerial_verify.check_hub_deps = lambda: aerial_verify.Check(
                "hub deps", "PASS", "fastapi, uvicorn importable")
            try:
                with _Server() as hub, _Server() as console:
                    servers.append(hub)
                    code, out = self._run([
                        "--json",
                        "--hub-url", hub.url,
                        "--console-url", console.url,
                        "--token-file", token_path,
                        "--state-dir", tmp,
                    ], env={"AERIAL_HUB_TOKEN_FILE": token_path})
            finally:
                aerial_verify._profile = orig_prof
                aerial_verify.check_hub_deps = orig_deps
            self.assertEqual(code, 0, out)
            doc = json.loads(out)
            self.assertEqual(doc["summary"], {
                "passed": 8, "failed": 0, "skipped": 0, "exit_code": 0})
            self.assertEqual([c["status"] for c in doc["checks"]],
                             ["PASS"] * 8)
            # the token value never appears in the JSON output
            self.assertNotIn(Handler.token, out)

    def test_failure_exit_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out = self._run([
                "--skip-network",
                "--hub-url", "http://127.0.0.1:1",
                "--console-url", "http://127.0.0.1:1",
                "--token-file", os.path.join(tmp, "nope"),
                "--state-dir", tmp,
            ])
            self.assertEqual(code, 1)
            self.assertIn("[FAIL]", out)
            self.assertIn("[SKIP]", out)

    def test_json_shape_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out = self._run([
                "--json", "--skip-network",
                "--token-file", os.path.join(tmp, "nope"),
                "--state-dir", tmp,
            ])
            self.assertEqual(code, 1)
            doc = json.loads(out)
            self.assertEqual(doc["summary"]["exit_code"], 1)
            for c in doc["checks"]:
                self.assertEqual(
                    set(c), {"name", "status", "detail", "fix"})
            names = [c["name"] for c in doc["checks"]]
            self.assertEqual(names, ["python", "imports", "hub deps",
                                     "state dirs", "token file",
                                     "hub health", "console auth",
                                     "ops agent"])
            self.assertEqual(doc["checks"][5]["status"], "SKIP")
            self.assertEqual(doc["checks"][6]["status"], "SKIP")

    def test_skip_network_marks_network_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out = self._run(["--skip-network", "--state-dir", tmp])
            self.assertEqual(code, 1)
            self.assertNotIn("unreachable", out)

    def test_token_never_in_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_path = os.path.join(tmp, "hub-token")
            open(token_path, "w").write("supersecret-token-value")
            os.chmod(token_path, 0o600)
            for extra in ([], ["--json"]):
                code, out = self._run(
                    ["--skip-network", "--state-dir", tmp,
                     "--token-file", token_path] + extra)
                self.assertNotIn("supersecret-token-value", out)

    def test_usage_error_exit_2(self):
        code, _ = self._run(["--no-such-flag"])
        self.assertEqual(code, 2)

    def test_fix_lines_coming_from_troubleshooting(self):
        with tempfile.TemporaryDirectory() as tmp:
            # a group/other-readable token file -> the chmod 600 fix line
            tok = os.path.join(tmp, "hub-token")
            open(tok, "w").write("x")
            os.chmod(tok, 0o644)
            code, out = self._run(
                ["--skip-network", "--state-dir", tmp,
                 "--token-file", tok])
            self.assertEqual(code, 1)
            # PYTHONPATH fix line: force the import failure path
            import builtins
            real_import = builtins.__import__

            def fake_import(name, *a, **kw):
                if name in ("deployment_profile", "dashboard"):
                    raise ImportError(name)
                return real_import(name, *a, **kw)

            builtins.__import__ = fake_import
            try:
                py_fix = aerial_verify.check_imports().fix
            finally:
                builtins.__import__ = real_import
            self.assertIn('export PYTHONPATH="$PWD:$PWD/dashboard"', py_fix)
            self.assertIn("pip install -r hub/requirements.txt", out)
            self.assertIn("mkdir -p", out)
            self.assertIn("chmod 600", out)


class CliRunTests(unittest.TestCase):
    """The real entry point, run as a subprocess from the repo root."""

    def test_help_exit_0(self):
        import subprocess
        r = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "aerial_verify.py"),
             "--help"], capture_output=True, text=True, cwd=REPO_ROOT,
            timeout=60)
        self.assertEqual(r.returncode, 0)
        self.assertIn("--skip-network", r.stdout)
        self.assertIn("--hub-url", r.stdout)
        self.assertIn("--console-url", r.stdout)
        self.assertIn("--token-file", r.stdout)

    def test_run_from_repo_root(self):
        import subprocess
        r = subprocess.run(
            [sys.executable, "aerial_verify.py", "--skip-network",
             "--state-dir", tempfile.mkdtemp()],
            capture_output=True, text=True, cwd=REPO_ROOT, timeout=60)
        self.assertIn(r.returncode, (0, 1))
        self.assertIn("aerial verify", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
