"""Run real Chrome headless UI checks using only temporary fixtures and browser profiles."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import uuid
from unittest.mock import patch
import native_adapter
import sync
import web_server
from aams_orders import LocalLoop
from test_aams_projects import ProjectFixture, manifest
from test_sync import make_legacy


class BrowserProjectFixture(ProjectFixture):
    """Supply captured-format receipts in a disposable Hub ledger, with no external requests."""

    def __init__(self, root):
        root.mkdir()
        super().__init__(root)
        self.put("runs", 10, {
            "id": 10, "agent": "reviewer", "state": "done", "rules_version": 1,
            "claimed_at": "2026-11-26T10:00:00Z", "finished_at": "2026-11-26T10:10:00Z",
            "summary": "浏览器临时历史回执",
        })

    def runtime(self, model, effort):
        session = str(uuid.uuid4())
        date_dir = self.root / "codex" / "sessions" / "2026" / "11" / "26"
        date_dir.mkdir(parents=True, exist_ok=True)
        record = {"timestamp": "2026-11-26T10:01:00Z", "type": "turn_context",
                  "payload": {"model": model, "effort": effort}}
        (date_dir / f"rollout-fixture-{session}.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        return session

    def request(self, key, method, path, body=None, port=None):
        previous = len(self.sent)
        response = super().request(key, method, path, body, port)
        if path == "/v1/state":
            for agent in response["agents"]:
                if agent["id"] == "reviewer":
                    agent["online"] = False
            return response
        if len(self.sent) == previous:
            return response
        event = response["event"]
        data = json.loads(body["detail"])
        if body["type"] == "test.scope":
            self.finish(event, "SCOPE player | 浏览器隔离验收仅影响试玩")
        else:
            reference = f"field/browser-{data['change_id']}.json"
            (self.root.parent / reference).write_text(json.dumps({
                "test": "isolated browser fixture", "change_id": data["change_id"], "task_id": data["task_id"],
            }), encoding="utf-8")
            if data["instruction"] == "浏览器 QA 记录卡点":
                self.finish(event, "BLOCKED | 跳转入口不可用（隔离样例） | " + reference)
            elif data["scope"] == "person" and not data.get("continuation"):
                self.finish(event, "APPLIED | QA 已接收当前工作 | " + reference,
                            session=self.runtime("qa-first-model", "max"))
            else:
                self.finish(event, "APPLIED_RESULT | QA 已记录结果 | " + reference,
                            session=self.runtime("qa-result-model", "xhigh"))
        return response

    def roster(self):
        result = manifest()
        result["project"]["name"] = "潮核浏览器测试项目"
        reviewer = next(agent for agent in result["departments"][0]["employees"] if agent["id"] == "reviewer")
        reviewer["history_run"] = 10
        result["departments"].append({
            "id": "documentation", "name": "文档池", "lead": "scribe",
            "mainline": "浏览器独立验证", "employees": [],
        })
        return result


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
        # Keep the prior order surface isolated. Only the new project surface uses synthetic Loop receipts.
        project_fixture = BrowserProjectFixture(root / "project-loop")
        (root / "field").mkdir()
        server = web_server.create_server(db, port=0, open_browser=True, order_loop=LocalLoop(root),
                                          project_loop=project_fixture.loop, project_evidence_root=root)
        server.projects.configure(project_fixture.roster())
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
