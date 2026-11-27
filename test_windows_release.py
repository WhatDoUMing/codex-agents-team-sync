import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import web_server
import release_entry


class ConfiguredProjectLoopTests(unittest.TestCase):
    def test_release_has_no_implicit_project_hub(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(web_server.configured_project_loop(False, 8766))

    def test_explicit_loopback_hub_is_bound_to_selected_home_and_port(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / "config.json").write_text(json.dumps({
                "url": "http://127.0.0.1:34350",
                "hub": {"listen": "127.0.0.1:34350"},
            }), encoding="utf-8")
            with patch.dict(os.environ, {
                "AAMS_PROJECT_HUB_HOME": str(home),
                "AAMS_PROJECT_HUB_PORT": "34350",
                "CODEX_HOME": str(home),
            }, clear=True):
                loop = web_server.configured_project_loop(False, 8766)
            self.assertEqual(loop.home, home.resolve())
            self.assertEqual(loop.port, 34350)

    def test_partial_or_mismatched_hub_configuration_fails_closed(self):
        with patch.dict(os.environ, {"AAMS_PROJECT_HUB_HOME": "C:/isolated/hub"}, clear=True):
            with self.assertRaisesRegex(ValueError, "must be set together"):
                web_server.configured_project_loop(False, 8766)

        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / "config.json").write_text(json.dumps({
                "url": "http://127.0.0.1:3350",
                "hub": {"listen": "127.0.0.1:3350"},
            }), encoding="utf-8")
            with patch.dict(os.environ, {
                "AAMS_PROJECT_HUB_HOME": str(home),
                "AAMS_PROJECT_HUB_PORT": "34350",
            }, clear=True):
                with self.assertRaisesRegex(ValueError, "bind exactly"):
                    web_server.configured_project_loop(False, 8766)

    def test_hub_cannot_share_dashboard_listener(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / "config.json").write_text(json.dumps({
                "url": "http://127.0.0.1:8766",
                "hub": {"listen": "127.0.0.1:8766"},
            }), encoding="utf-8")
            with patch.dict(os.environ, {
                "AAMS_PROJECT_HUB_HOME": str(home),
                "AAMS_PROJECT_HUB_PORT": "8766",
            }, clear=True):
                with self.assertRaisesRegex(ValueError, "distinct from the dashboard"):
                    web_server.configured_project_loop(False, 8766)

    def test_evidence_roots_are_created_inside_user_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "AAMS"
            with patch.dict(os.environ, {}, clear=True):
                release_entry.configure_evidence_roots(data_dir)
                project = Path(os.environ["AAMS_PROJECT_EVIDENCE_ROOT"])
                delegation = Path(os.environ["AAMS_DELEGATION_EVIDENCE_ROOT"])
            self.assertEqual(project, (data_dir / "project-evidence").resolve())
            self.assertEqual(delegation, (data_dir / "delegation-evidence").resolve())
            self.assertTrue((project / "field").is_dir())
            self.assertTrue((delegation / "field").is_dir())


if __name__ == "__main__":
    unittest.main()
