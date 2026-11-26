"""No model calls: tests the explicit Windows launcher and its failure paths."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import start_owned_codex as launcher
import sync


class OwnedLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="owned-launcher-test-")
        self.root = Path(self.temp.name)
        self.cli = self.root / "app" / "resources" / "codex.exe"
        self.cli.parent.mkdir(parents=True)
        self.cli.write_bytes(b"test fixture; never executed")
        self.package = {**launcher._OFFICIAL_IDENTITY, "location": str(self.root)}

    def tearDown(self):
        self.temp.cleanup()

    def discover(self, result):
        with patch.object(launcher.sys, "platform", "win32"), \
             patch.dict(launcher.os.environ, {"SystemRoot": r"C:\Windows"}), \
             patch.object(launcher.subprocess, "run", return_value=result) as run:
            value = launcher.find_installed_codex()
        return value, run

    def result(self, packages, code=0):
        return subprocess.CompletedProcess([], code, stdout=json.dumps(packages), stderr="")

    def test_discovery_queries_installed_package_only(self):
        value, run = self.discover(self.result([self.package]))
        self.assertEqual(value, self.cli)
        command = run.call_args.args[0]
        self.assertIn("-NoProfile", command)
        self.assertIn("-NonInteractive", command)
        self.assertIn("Get-AppxPackage -Name 'OpenAI.Codex'", command[-1])
        self.assertFalse(run.call_args.kwargs.get("shell", False))

    def test_same_named_untrusted_packages_are_rejected(self):
        for field, value in (("family", "OpenAI.Codex_another"),
                             ("publisher", "CN=untrusted"),
                             ("signature", "Developer"),
                             ("status", "Modified"),
                             ("location", "relative-path")):
            package = {**self.package, field: value}
            with self.subTest(field=field):
                self.assertIsNone(self.discover(self.result([package]))[0])
        self.assertEqual(self.discover(self.result([{**self.package, "publisher": "CN=untrusted"},
                                                   self.package]))[0], self.cli)

    def test_missing_or_failed_package_never_falls_back(self):
        outcomes = (self.result([], code=1), self.result([]),
                    self.result([{**self.package, "location": str(self.root / "missing")}]),
                    subprocess.CompletedProcess([], 0, stdout="not-json", stderr=""))
        for result in outcomes:
            with self.subTest(result=result):
                self.assertIsNone(self.discover(result)[0])

    def test_discovery_timeout_is_a_clear_failure(self):
        with patch.object(launcher.sys, "platform", "win32"), \
             patch.dict(launcher.os.environ, {"SystemRoot": r"C:\Windows"}), \
             patch.object(launcher.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired("powershell", 15)):
            self.assertIsNone(launcher.find_installed_codex())

    def test_missing_cli_does_not_start_server(self):
        with patch.object(launcher, "find_installed_codex", return_value=None), \
             patch.object(sync, "main") as serve, contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(launcher.main([]), 1)
        serve.assert_not_called()
        self.assertIn("not found or not accessible", error.getvalue())

    def test_explicit_launcher_uses_loopback_and_chosen_port(self):
        with patch.object(launcher, "find_installed_codex", return_value=self.cli), \
             patch.object(sync, "main", return_value=0) as serve, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(launcher.main(["--port", "8766"]), 0)
        serve.assert_called_once_with(["serve", "--host", "127.0.0.1", "--port", "8766",
                                       "--codex-cli", str(self.cli)])

    def test_cannot_override_cli_or_use_invalid_port(self):
        for argv in (["--codex-cli", "other.exe"], ["--port", "65536"], ["--port", "invalid"]):
            with self.subTest(argv=argv), patch.object(launcher, "find_installed_codex") as find, \
                 contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                launcher.main(argv)
            find.assert_not_called()


if __name__ == "__main__":
    unittest.main()
