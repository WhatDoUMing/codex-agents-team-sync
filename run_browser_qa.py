"""Run real Chrome headless UI checks using only temporary fixtures and browser profiles."""
import json
import os
from pathlib import Path
import subprocess
import sys
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


CARD_PROJECT = "native-cards"
CARD_ACTOR = "card-owner"
CARD_PHASE = "card-consumer-phase.txt"
CARD_CONSUMER = """
import json
from pathlib import Path
import sys
import time
import sync

settings = json.load(sys.stdin)
db, control = Path(settings["db"]), Path(settings["control"])
project, worker, generation = settings["project"], settings["id"], settings["generation"]
sync.opt_in(db, worker, generation, project)
detail = json.dumps({"detail": "Independent fixture heartbeat", "model": "qa-self-reported", "effort": "max"})
claimed = None
previous = None
while True:
    phase = control.read_text(encoding="ascii").strip()
    if phase == "stop":
        break
    if phase == "offline":
        if previous != phase:
            with sync.connect(db) as connection:
                expired = time.time() - 61
                connection.execute("UPDATE workers SET updated=?,last_polled=? WHERE project=? AND id=? AND generation=?",
                                   (expired, expired, project, worker, generation))
    else:
        sync.heartbeat(db, worker, generation, "running", detail, project)
        queued = sync.inbox(db, worker, generation, project)
        if phase == "claim" and claimed is None and queued:
            claimed = sync.claim(db, worker, generation, queued[0]["id"], worker, project)
        if phase == "report" and claimed is not None:
            sync.ack(db, worker, generation, claimed["id"], "completed", "Independent consumer recorded the fixture status",
                     worker, claimed["claim_token"], project)
            claimed = None
    previous = phase
    time.sleep(.3)
"""


class BrowserProjectFixture(ProjectFixture):
    """Supply captured-format receipts in a disposable Hub ledger, with no external requests."""

    roles = {**ProjectFixture.roles,
             "test.browser-project": ("浏览器项目经理", "qa-manager"),
             "test.browser-lead": ("浏览器交付负责人", "qa-lead"),
             "test.browser-result": ("浏览器交付员工", "qa-worker"),
             "test.browser-live-formal": ("浏览器正式员工", "card-formal")}

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
        if body["type"] == "test.browser-project":
            self.finish(event, "DEPARTMENT browser-delivery | 核对候选", session=self.runtime("qa-model", "max"))
        elif body["type"] == "test.browser-lead":
            self.finish(event, 'STAFF | [["qa-worker","记录候选复测数值"]]', session=self.runtime("qa-model", "max"))
        elif body["type"] == "test.browser-result":
            reference = data["evidence_file"]
            (self.root.parent / reference).write_text(json.dumps({
                "change_id": data["change_id"], "task_id": data["task_id"], "version": data["version"],
                "outcome": "passed", "pixel_offset": 0,
            }), encoding="utf-8")
            self.finish(event, "APPLIED_RESULT | 隔离复测数值为 0 | " + reference, session=self.runtime("qa-model", "max"))
        elif body["type"] == "test.scope":
            self.finish(event, "SCOPE player | 浏览器隔离验收仅影响试玩")
        else:
            reference = f"field/browser-{data['change_id']}.json"
            (self.root.parent / reference).write_text(json.dumps({
                "test": "isolated browser fixture", "change_id": data["change_id"], "task_id": data["task_id"],
            }), encoding="utf-8")
            if data["instruction"] == "浏览器 QA 记录卡点":
                self.finish(event, "BLOCKED | 跳转入口不可用（隔离样例） | " + reference)
            elif body["type"] == "test.browser-live-formal":
                self.finish(event, "APPLIED_RESULT | 正式 Loop 隔离核对 | " + reference,
                            session=self.runtime("qa-loop-turn-model", "xhigh"))
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
        result["project"]["manager"] = {"id": "qa-manager", "event_type": "test.browser-project", "rule": "浏览器项目经理"}
        reviewer = next(agent for agent in result["departments"][0]["employees"] if agent["id"] == "reviewer")
        reviewer["history_run"] = 10
        result["departments"].append({
            "id": "documentation", "name": "文档池", "lead": "scribe",
            "mainline": "浏览器独立验证", "employees": [],
        })
        result["departments"].append({
            "id": "browser-delivery", "name": "浏览器委托交付池", "lead": "qa-lead", "mainline": "核对候选",
            "scope_type": "test.browser-lead", "scope_rule": "浏览器交付负责人", "delegation_only": True,
            "employees": [{"id": "qa-worker", "name": "候选复测员工", "task_id": "qa-delivery-worker",
                           "task_title": "核对候选数值", "event_type": "test.browser-result", "rule": "浏览器交付员工"}],
        })
        return result


def live_card_fixture(board, db, directory):
    roles = {
        CARD_ACTOR: "本人原有任务",
        "card-observer": "只观察岗位",
        "card-unpolled": "声明后未轮询",
        "card-formal": "正式 Loop 并有本人自报",
        "card-ambiguous": "无匹配部门的协作岗",
        "card-foreign": "另一项目同名岗位",
        "card-demo": "协议演示",
    }
    employees = [{"id": worker, "name": name, "task_id": "registered-" + worker,
                  "task_title": "岗位登记工作 " + name} for worker, name in roles.items()]
    for member in employees:
        if member["id"] == "card-formal":
            member.update(event_type="test.browser-live-formal", rule="浏览器正式员工")
    board.configure({"project": {"id": CARD_PROJECT, "name": "员工卡验收",
        "manager": {"id": "qa-manager", "event_type": "test.browser-project", "rule": "浏览器项目经理"}},
        "departments": [
            {"id": "collaboration", "name": "协作部门", "lead": "card-lead",
             "mainline": "核对普通员工卡", "employees": employees},
            {"id": "backup-collaboration", "name": "协作观察", "lead": "card-backup-lead",
             "mainline": "核对正式岗位的观察卡", "employees": [
                 {"id": "card-formal", "name": "正式岗位观察卡", "task_id": "observe-card-formal",
                  "task_title": "观察正式岗位回执"},
                 {"id": "card-ambiguous", "name": "另一登记卡", "task_id": "observe-card-ambiguous",
                  "task_title": "只查看明确的部门登记"}]},
        ]})
    owner = sync.register(db,CARD_ACTOR,"collaboration","owner","来自本人的实际任务描述",False,CARD_PROJECT)
    for worker in roles.keys() - {CARD_ACTOR}:
        project = "other-cards" if worker == "card-foreign" else CARD_PROJECT
        team = "unmapped-team" if worker == "card-ambiguous" else "collaboration"
        item = sync.register(db,worker,team,"fixture","Self-report " + worker,worker != "card-observer",
                             project,is_demo=worker == "card-demo")
        sync.heartbeat(db,worker,item["generation"],"running",json.dumps(
            {"detail":"Explicit fixture self-report","model":"qa-self-reported","effort":"max"}),project)
        if worker in ("card-formal","card-foreign","card-demo","card-ambiguous"):
            sync.inbox(db,worker,item["generation"],project)
    control = directory / CARD_PHASE
    control.write_text("poll",encoding="ascii")
    child = subprocess.Popen([sys.executable,"-B","-c",CARD_CONSUMER],cwd=sync.ROOT,stdin=subprocess.PIPE,
                             stdout=subprocess.DEVNULL,encoding="utf-8")
    with child.stdin as stream:
        json.dump({"db":str(db),"control":str(control),**owner},stream)
    return control, child


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
        card_control, card_consumer = live_card_fixture(server.projects,db,root)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        env = {**os.environ, "TEAM_SYNC_QA_URL": "http://127.0.0.1:%d/" % server.server_address[1],
               "TEAM_SYNC_QA_TOKEN": server.access_token, "TEAM_SYNC_QA_TEMP": tmp,
               "TEAM_SYNC_QA_LAUNCH": server.browser_launch.ticket}
        try:
            result = subprocess.run(["node", str(sync.ROOT/"browser_qa.js")], env=env, timeout=120)
            return result.returncode
        finally:
            card_control.write_text("stop",encoding="ascii")
            try:
                card_consumer.wait(timeout=5)
            except subprocess.TimeoutExpired:
                card_consumer.terminate()
                card_consumer.wait(timeout=5)
            server.shutdown()
            server.demos.close()
            server.server_close()
            thread.join(5)
            native_mock.stop()


if __name__ == "__main__":
    raise SystemExit(main())
