"""Run real Chrome headless UI checks using only temporary fixtures and browser profiles."""
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from unittest.mock import patch
import native_adapter
import sync
import web_server
from aams_orders import LocalLoop
from test_sync import make_legacy


def main():
    with tempfile.TemporaryDirectory(prefix="team-sync-browser-qa-") as tmp:
        root = Path(tmp)
        db = root / "sync.sqlite3"
        sync.initialize(db)
        sync.register(db, "manual-observer", "notes", "manual status only", "Manual record; no inbox", False, "documentation")
        expired = sync.register(db, "expired-worker", "archived-team", "test fixture", "Expired heartbeat must not look online", True, "archive")
        sync.heartbeat(db, expired["id"], expired["generation"], "running", "Old report, not live proof", "archive")
        with sync.connect(db) as connection:
            connection.execute("UPDATE workers SET updated=? WHERE id='expired-worker'", (time.time()-60,))
        legacy = root / "legacy # temporary.sqlite3"
        make_legacy(legacy, bad=True)
        sync.source_add(db, "fixture-source", "legacy-demo", legacy, "Temporary legacy fixture · read-only")
        sync.register(db, "history-worker", "history-team", "history fixture", "Test per-project latest 100", True, "history")
        sync.queue(db, "history-worker", "Older command must remain visible in its project", "history-older", "history")
        sync.register(db, "busy-worker", "busy-team", "queue fixture", "Many recent records from another project", True, "busy")
        for n in range(105):
            sync.queue(db, "busy-worker", "Temporary queue fixture " + str(n), "busy-command-" + str(n), "busy")
        # Native card UI fixture only. Never opens the user's Codex account or history.
        fake_cli = root / "codex.exe"
        fake_cli.write_bytes(b"fixture, never executed")
        native_scope = root / "native-project"
        native_scope.mkdir()
        native_report = {
            "available": True, "tasks": [], "threads": [{
                "id": "fixture-native-session", "name": "Native metadata fixture (not a live agent)",
                "cwd": str(native_scope), "source_kind": "vscode", "reported_runtime_status": "notLoaded",
                "created_at": time.time(), "updated_at": time.time(), "can_send": False, "can_steer": False}],
            "can_send": False, "can_steer": False, "loaded_count_in_adapter": 0,
            "observed_at": time.time(), "cached": False, "truncated": False}
        native_mock = patch.object(native_adapter, "read_codex_threads", return_value=native_report)
        native_mock.start()
        sync.source_add(db, "native-fixture", "native-fixture", fake_cli,
                        "Official metadata UI fixture", "codex-app-server", native_scope)
        # This empty local fixture has no Loop configuration or keys. Browser orders stay pending here.
        server = web_server.create_server(db, port=0, open_browser=True, order_loop=LocalLoop(root))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        env = {**os.environ, "TEAM_SYNC_QA_URL": "http://127.0.0.1:%d/" % server.server_address[1],
               "TEAM_SYNC_QA_TOKEN": server.access_token, "TEAM_SYNC_QA_TEMP": tmp,
               "TEAM_SYNC_QA_LAUNCH": server.browser_launch.ticket}
        try:
            result = subprocess.run(["node", str(sync.ROOT/"browser_qa.js")], env=env, timeout=120)
            return result.returncode
        finally:
            server.shutdown()
            server.demos.close()
            server.server_close()
            thread.join(5)
            native_mock.stop()


if __name__ == "__main__":
    raise SystemExit(main())
