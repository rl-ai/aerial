#!/usr/bin/env python3
"""Packaging tests for the root pyproject.toml (pip lane).

Checks the declared metadata and that every console-script entry point
resolves to an importable callable, using the same path setup the
installed entry points use (the aerial_entry shim: repo root first, then
dashboard/). aerial_verify is skipped when the file is absent (the verify
lane owns it). stdlib only; no pip install, no network.
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
import tomllib
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYPROJECT = os.path.join(ROOT, "pyproject.toml")


def _entry_path_setup() -> None:
    """Reproduce the path setup aerial_entry uses for its entry points."""
    dash = os.path.join(ROOT, "dashboard")
    for path in (dash, ROOT):  # inserted in this order -> ROOT ends up first
        if path not in sys.path:
            sys.path.insert(0, path)


class TestPyprojectMetadata(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(PYPROJECT, "rb") as fh:
            cls.pyproject = tomllib.load(fh)

    def test_name_and_version(self):
        project = self.pyproject["project"]
        self.assertEqual(project["name"], "aerial")
        self.assertEqual(project["version"], "1.0.1")

    def test_requires_python(self):
        self.assertEqual(self.pyproject["project"]["requires-python"], ">=3.10")

    def test_no_required_dependencies(self):
        project = self.pyproject["project"]
        deps = project.get("dependencies", [])
        self.assertEqual(deps, [],
                         "console/ops agent are stdlib-only: no required deps")
        self.assertNotIn("dependencies", project.get("dynamic", []),
                         "dynamic deps would fill install_requires")

    def test_hub_extra_matches_hub_requirements(self):
        extras = self.pyproject["project"]["optional-dependencies"]
        self.assertIn("hub", extras)
        with open(os.path.join(ROOT, "hub", "requirements.txt")) as fh:
            wanted = [line.strip() for line in fh
                      if line.strip() and not line.startswith("#")]
        self.assertEqual(extras["hub"], wanted)

    def test_console_scripts_declared(self):
        scripts = self.pyproject["project"]["scripts"]
        for name in ("aerial-verify", "aerial-console", "aerial-ops-agent"):
            self.assertIn(name, scripts)
        self.assertEqual(scripts["aerial-verify"], "aerial_verify:main")

    def test_hub_and_gateway_not_packaged(self):
        find = self.pyproject["tool"]["setuptools"]["packages"]["find"]
        self.assertEqual(find["include"], ["dashboard*"])
        for bad in ("hub", "gateway", "integration"):
            self.assertFalse(any(p == bad or p.startswith(bad + ".")
                                 for p in find["include"]))


class TestConsoleScriptTargets(unittest.TestCase):
    def _assert_main(self, module_name, attr="main"):
        module = importlib.import_module(module_name)
        fn = getattr(module, attr)
        self.assertTrue(callable(fn),
                        f"{module_name}:{attr} is not callable")
        return fn

    def test_aerial_verify_resolves(self):
        # Skip when the verify lane's file is absent from this checkout.
        if not os.path.exists(os.path.join(ROOT, "aerial_verify.py")):
            self.skipTest("aerial_verify.py not present (verify lane)")
        _entry_path_setup()
        self._assert_main("aerial_verify")

    def test_aerial_console_resolves(self):
        _entry_path_setup()
        self._assert_main("aerial_entry", "console_main")
        # The shim must reach the real console main the docs start.
        self._assert_main("dashboard.serve_console")

    def test_aerial_ops_agent_resolves(self):
        _entry_path_setup()
        self._assert_main("aerial_entry", "ops_agent_main")
        self._assert_main("dashboard.followup.runner")

    def test_shim_path_order_matches_documented_pythonpath(self):
        """Root wins over dashboard/ for the bare deployment_profile import."""
        import dashboard  # namespace package in this checkout

        here = os.path.abspath(list(dashboard.__path__)[0])
        self.assertEqual(os.path.dirname(here), ROOT)
        _entry_path_setup()
        import deployment_profile

        self.assertEqual(
            os.path.dirname(os.path.abspath(deployment_profile.__file__)),
            ROOT,
            "deployment_profile must resolve to the repo-root module")


if __name__ == "__main__":
    unittest.main()
