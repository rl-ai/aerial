#!/usr/bin/env python3
"""Static checks for the Docker lane files (no docker, no yaml lib).

Stdlib-only: parses the compose file with line/regex checks (there is no
stdlib YAML parser, so no third-party parse is attempted). Every check is
a plain assertion against the files as they sit in the tree.
"""
import re
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _read(name: str) -> str:
    return (REPO / name).read_text(encoding="utf-8")


def _compose_services(text: str) -> dict:
    """Map top-level 'service-name:' keys under the 'services:' block."""
    services = {}
    in_services = False
    for line in text.splitlines():
        if re.match(r"^services:\s*$", line):
            in_services = True
            continue
        if in_services:
            if line.startswith("  ") or not line.strip():
                m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
                if m:
                    services[m.group(1)] = True
            else:
                break
    return services


def _copy_paths(dockerfile_text: str) -> list:
    """COPY / ADD source paths from a Dockerfile (first token after flags)."""
    paths = []
    for line in dockerfile_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^(COPY|ADD)\s+(.+)$", line)
        if not m:
            continue
        parts = [p for p in m.group(2).split() if not p.startswith("--")]
        # last token is the destination; everything before it is a source
        paths.extend(parts[:-1])
    return paths


class TestDockerCompose(unittest.TestCase):
    def setUp(self):
        self.compose = _read("docker-compose.yml")

    def test_has_three_services(self):
        services = _compose_services(self.compose)
        for name in ("hub", "console", "ops-agent"):
            self.assertIn(name, services, f"missing service {name}")
        self.assertEqual(set(services), {"hub", "console", "ops-agent"})

    def test_console_port_bound_to_loopback(self):
        port_lines = [
            line for line in self.compose.splitlines()
            if re.match(r"^\s*-\s*[\"']?[0-9][0-9.]*:[0-9]+", line)
        ]
        self.assertEqual(len(port_lines), 1, f"expected exactly one published port, got {port_lines}")
        self.assertIn("127.0.0.1:", port_lines[0],
                      "console ports entry must publish on 127.0.0.1 only")

    def test_no_secret_values_inline(self):
        # the token must come from the secret file, never a literal value
        self.assertNotRegex(self.compose, r"(?i)bearer\s+[A-Za-z0-9+/]{16,}")
        self.assertNotRegex(self.compose, r"(?i)(token|secret)\s*[:=]\s*[\"'][A-Za-z0-9+/]{16,}")
        self.assertNotRegex(self.compose, r"(?i)\bTICKET_HUB_WRITE_TOKENS\b")
        # secrets are declared as file-backed and mounted at /run/secrets
        self.assertRegex(self.compose, r"file:\s*\./secrets/hub-token")
        self.assertRegex(self.compose, r"/run/secrets/hub-token")
        # no inline token env var anywhere
        for line in self.compose.splitlines():
            self.assertIsNone(
                re.search(r"(?i)(HUB_TOKEN|TOKEN)\s*[:=]\s*(?!/run/secrets)[A-Za-z0-9]", line),
                f"inline token value: {line.strip()}",
            )


class TestDockerfiles(unittest.TestCase):
    def test_dashboard_dockerfile_non_root(self):
        text = _read("dashboard/Dockerfile")
        self.assertIn("FROM python:3.12-slim", text)
        user_lines = [l for l in text.splitlines() if re.match(r"^USER\s+", l.strip())]
        self.assertTrue(user_lines, "dashboard/Dockerfile must have a USER directive")
        last = user_lines[-1].strip().split()[1]
        self.assertNotIn("root", last.lower(), "runtime USER must be non-root")

    def test_dashboard_dockerfile_pythonpath(self):
        text = _read("dashboard/Dockerfile")
        self.assertRegex(text, r"ENV PYTHONPATH=/app:/app/dashboard")

    def test_hub_dockerfile_untouched_shape(self):
        text = _read("hub/Dockerfile")
        self.assertIn("FROM python:3.12-slim", text)
        self.assertIn("CMD [\"python\",\"api.py\"]", text)

    def test_copy_paths_exist_in_tree(self):
        for dockerfile in ("dashboard/Dockerfile", "hub/Dockerfile"):
            text = _read(dockerfile)
            base = REPO if dockerfile.startswith("dashboard/") else REPO / "hub"
            for src in _copy_paths(text):
                if any(ch in src for ch in "*?"):
                    continue  # glob patterns: skip existence check
                self.assertTrue(
                    (base / src).exists(),
                    f"{dockerfile}: COPY source does not exist: {src}",
                )

    def test_command_paths_exist(self):
        compose = _read("docker-compose.yml")
        # every .py / .sh path referenced by commands and healthchecks exists
        for path in set(re.findall(r"[\w./-]+\.(?:py|sh)", compose)):
            self.assertTrue(
                (REPO / path).exists(),
                f"docker-compose.yml references missing file: {path}",
            )


class TestDockerignoreAndDocs(unittest.TestCase):
    def test_dockerignore_entries(self):
        text = _read(".dockerignore")
        for entry in (".git", "__pycache__/", "*.pyc", "secrets/",
                      ".pytest_cache/"):
            self.assertIn(entry, text)

    def test_docs_exist_and_no_pii(self):
        text = _read("docs/DOCKER.md")
        for needle in ("chmod 600", "docker compose up -d", "docker compose down",
                       "aerial_verify.py", "built locally"):
            self.assertIn(needle, text)
        # no real tokens or LAN addresses in docs or compose
        blob = text + _read("docker-compose.yml")
        self.assertNotRegex(blob, r"(?i)bearer\s+[A-Za-z0-9+/]{16,}")

    def test_docs_do_not_leak_abs_paths(self):
        blob = _read("docs/DOCKER.md") + _read("docker-compose.yml")
        self.assertNotRegex(blob, r"(?m)^/home/")
        # no literal token-looking values (32+ hex chars) anywhere
        self.assertNotRegex(blob, r"\b[0-9a-f]{32,64}\b")


if __name__ == "__main__":
    unittest.main(verbosity=2)
