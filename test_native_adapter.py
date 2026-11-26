"""No real user history is read by these tests. Native transport is mocked."""
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys
import threading
import time
from unittest.mock import patch
import native_adapter
import sync


class FakeClient:
    def __init__(self, replies=None, initial_error=None):
        self.calls = []
        self.replies = replies or {}
        self.initial_error = initial_error
        self.closed = False

    def initialize(self):
        self.calls.append(("initialize", {}))
        if self.initial_error:
            raise self.initial_error

    def request(self, method, params):
        self.calls.append((method, params))
        return self.replies[method]

    def close(self):
        self.closed = True


class NativeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="team-sync-native-test-")
        self.root = Path(self.tmp.name).resolve()
        self.cli = self.root / "codex.exe"
        self.cli.write_bytes(b"mock fixture, never executed")
        self.cwd = self.root / "explicit-project"
        self.cwd.mkdir()
        self.db = self.root / "sync.sqlite3"
        native_adapter._CACHE.clear()
        native_adapter._QUARANTINED.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def fake(self, rows=None):
        return FakeClient({"thread/list": {"data": rows or [], "nextCursor": None},
                           "thread/loaded/list": {"data": []}})

    def test_default_snapshot_does_not_enable_native_query(self):
        sync.initialize(self.db)
        with patch.object(native_adapter, "read_codex_threads", side_effect=AssertionError("not enabled")):
            self.assertEqual(sync.snapshot(self.db)["sources"], [])

    def test_scope_is_required_and_no_configuration_created_on_failure(self):
        with self.assertRaises(ValueError):
            native_adapter.read_codex_threads(self.cli, "")
        with self.assertRaises(ValueError):
            sync.source_add(self.db, "native", "alpha", self.cli, "Native", "codex-app-server")
        self.assertFalse(self.db.exists())

    def test_only_metadata_rpc_and_state_db_no_rollout_scan(self):
        raw = {"id": "session-1", "name": "Real metadata title", "cwd": str(self.cwd),
               "source": "vscode", "createdAt": 1, "updatedAt": 2, "status": {"type": "notLoaded"},
               "preview": "PRIVATE_MESSAGE_BODY_MUST_NOT_BE_RETAINED", "rolloutPath": "PRIVATE_HISTORY_PATH"}
        fake = self.fake([raw])
        with patch.object(native_adapter, "CodexClient", return_value=fake):
            report = native_adapter.read_codex_threads(self.cli, self.cwd, cache_seconds=0)
        self.assertTrue(report["available"])
        self.assertTrue(fake.closed)
        self.assertEqual([c[0] for c in fake.calls], ["initialize", "thread/list", "thread/loaded/list"])
        params = fake.calls[1][1]
        self.assertTrue(params["useStateDbOnly"])
        self.assertEqual(params["cwd"], str(self.cwd))
        self.assertIn("subAgent", params["sourceKinds"])
        self.assertEqual(report["threads"][0]["reported_runtime_status"], "notLoaded")
        self.assertFalse(report["threads"][0]["can_send"])
        self.assertIn("NOT stopped", report["note"])
        self.assertNotIn("PRIVATE_", repr(report))

    def test_exact_scope_enforced_against_bad_server_filter(self):
        fake = self.fake([
            {"id": "in-scope", "name": "allowed", "cwd": str(self.cwd)},
            {"id": "outside", "name": "SHOULD_NOT_LEAK", "cwd": str(self.root / "other")},
            {"id": "no-cwd", "name": "SHOULD_NOT_LEAK_EITHER"}])
        with patch.object(native_adapter, "CodexClient", return_value=fake):
            report = native_adapter.read_codex_threads(self.cli, self.cwd, cache_seconds=0)
        self.assertEqual([r["id"] for r in report["threads"]], ["in-scope"])
        self.assertNotIn("SHOULD_NOT_LEAK", repr(report))

    def test_duplicate_metadata_and_nonfinite_fields_normalized(self):
        fake = self.fake([
            {"id": "one", "name": "old", "cwd": str(self.cwd), "updatedAt": 1},
            {"id": "one", "name": "new", "cwd": str(self.cwd), "updatedAt": 3},
            {"id": "two", "name": "second", "cwd": str(self.cwd), "updatedAt": float("inf")},
            None])
        with patch.object(native_adapter, "CodexClient", return_value=fake):
            report = native_adapter.read_codex_threads(self.cli, self.cwd, cache_seconds=0)
        self.assertEqual([r["name"] for r in report["threads"]], ["new", "second"])
        self.assertIsNone(report["threads"][1]["updated_at"])

    def test_control_and_history_methods_cannot_be_requested(self):
        client = object.__new__(native_adapter.CodexClient)
        for method in ("turn/start", "turn/steer", "thread/resume", "thread/read", "thread/turns/list", "command/exec", "account/read"):
            with self.assertRaises(ValueError):
                client.request(method, {})

    def test_bounded_cache_is_independent_copy_and_scope_bound(self):
        fake = self.fake([{"id": "one", "cwd": str(self.cwd), "name": "original"}])
        with patch.object(native_adapter, "CodexClient", return_value=fake) as factory:
            first = native_adapter.read_codex_threads(self.cli, self.cwd)
            first["threads"][0]["name"] = "client mutation"
            second = native_adapter.read_codex_threads(self.cli, self.cwd)
            self.assertEqual(factory.call_count, 1)
        self.assertTrue(second["cached"])
        self.assertEqual(second["threads"][0]["name"], "original")

    def test_timeout_isolated_and_owned_client_closed(self):
        fake = FakeClient(initial_error=TimeoutError("test deadline"))
        with patch.object(native_adapter, "CodexClient", return_value=fake):
            report = native_adapter.read_codex_threads(self.cli, self.cwd, cache_seconds=0)
        self.assertFalse(report["available"])
        self.assertTrue(fake.closed)
        self.assertFalse(report["can_send"])
        self.assertEqual(report["threads"], [])

    def test_cleanup_warning_quarantines_cli_without_retrying_or_leaking_data(self):
        fake = self.fake([{"id": "one", "name": "sensitive metadata", "cwd": str(self.cwd)}])
        def bad_close():
            raise native_adapter.CleanupBlockedError("test inherited pipe")
        fake.close = bad_close
        with patch.object(native_adapter, "CodexClient", return_value=fake) as factory:
            first = native_adapter.read_codex_threads(self.cli, self.cwd, cache_seconds=0)
            second = native_adapter.read_codex_threads(self.cli, self.root / "another-scope", cache_seconds=0)
        self.assertEqual(factory.call_count, 1)
        self.assertFalse(first["available"])
        self.assertEqual(first["threads"], [])
        self.assertFalse(second["available"])
        self.assertIn("disabled", second["error"])

    def test_inherited_pipe_cleanup_is_bounded(self):
        # An owned, short-lived Python fixture, not a real Codex process or user session.
        real_popen = subprocess.Popen
        helper = "import time;time.sleep(2.5)"
        script = ("import subprocess,sys;"
                  "subprocess.Popen([sys.executable,'-c'," + repr(helper) + "],"
                  "stdout=sys.stdout,stderr=sys.stderr,"
                  "creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))")
        def spawn_fixture(*args, **kwargs):
            return real_popen([sys.executable, "-B", "-c", script],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, encoding="utf-8",
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        with patch.object(native_adapter.subprocess, "Popen", side_effect=spawn_fixture):
            client = native_adapter.CodexClient(self.cli)
        client.process.wait(timeout=3)
        start = time.monotonic()
        with self.assertRaises(native_adapter.CleanupBlockedError):
            client.close()
        self.assertLess(time.monotonic() - start, 2.0)
        # Allow only our finite helper to finish; no process-tree kills or private IPC.
        for reader in client.readers:
            reader.join(timeout=3)
        for stream in (client.process.stdout, client.process.stderr):
            stream.close()

    def test_explicit_native_source_configuration_and_project_filter(self):
        report = {"available": True, "tasks": [], "threads": [{"id": "real-session", "can_send": False}],
                  "can_send": False, "can_steer": False, "note": "metadata only"}
        with patch.object(native_adapter, "read_codex_threads", return_value=report) as read:
            sync.source_add(self.db, "native-alpha", "alpha", self.cli, "Explicit native project",
                            "codex-app-server", self.cwd)
            one = sync.snapshot(self.db, project="alpha")
            self.assertEqual(len(one["sources"]), 1)
            self.assertEqual(one["sources"][0]["scope_cwd"], str(self.cwd))
            self.assertEqual(one["sources"][0]["threads"][0]["id"], "real-session")
            self.assertEqual(sync.snapshot(self.db, project="beta")["sources"], [])
            self.assertTrue(all(c.args[1] == str(self.cwd) for c in read.call_args_list))
        self.assertEqual(sync.workers(self.db), [])

    def test_bad_source_configuration_does_not_execute_or_hide_others(self):
        sync.initialize(self.db)
        with sync.connect(self.db) as db:
            db.execute("INSERT INTO sources VALUES('bad','alpha','Bad native','codex-app-server','not-json')")
        with patch.object(native_adapter, "read_codex_threads", side_effect=AssertionError("must not execute")):
            report = sync.snapshot(self.db)
        self.assertEqual(len(report["sources"]), 1)
        self.assertFalse(report["sources"][0]["available"])
        self.assertFalse(report["sources"][0]["can_send"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
