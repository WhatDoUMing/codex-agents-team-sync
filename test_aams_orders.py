import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

import aams_orders as aams
import sync


class Fixture:
    def __init__(self, root):
        self.root = root
        self.hub = root / "hub.db"
        self.database = root / "panel.sqlite3"
        sync.initialize(self.database)
        with closing(sqlite3.connect(self.hub)) as db, db:
            db.execute("CREATE TABLE projections(collection TEXT,key TEXT,data TEXT,PRIMARY KEY(collection,key))")
        (root / "config.json").write_text(json.dumps({
            "url": "http://127.0.0.1:3350", "hub": {"listen": "127.0.0.1:3350"},
            "source": {"name": "local", "key": "s"*32}, "owner": "o"*32, "worker":{"name":"test-machine"}}))
        self.put("config", 1, {"value": {"agents": {a: {"use": "codex", "worker":"test-machine"}
                                                    for _, _, a in aams.ROLES.values()}}})
        self.sent = []
        self.lose_reply = False
        self.loop = aams.LocalLoop(root, self.request)
        self.board = aams.OrderBoard(self.database, self.loop)

    def put(self, coll, key, value):
        with closing(sqlite3.connect(self.hub)) as db, db:
            db.execute("INSERT OR REPLACE INTO projections VALUES(?,?,?)", (coll, str(key), json.dumps(value)))

    def request(self, key, method, path, body=None, port=None):
        if path == "/v1/state":
            assert key == "o"*32
            return {"lines": [{"name": name, "agent": agent, "types": [kind], "source": "local", "paused": None}
                              for kind, name, agent in aams.ROLES.values()], "runs": [],
                    "agents":[{"id":a,"use":"codex","worker":"test-machine"} for _,_,a in aams.ROLES.values()]}
        assert key == "s"*32 and (method, path) == ("POST", "/v1/events")
        n = len(self.sent) + 1
        role = next(k for k, v in aams.ROLES.items() if v[0] == body["type"])
        _, rule, agent = aams.ROLES[role]
        self.sent.append(body)
        self.put("events", n, {**body, "id": n, "key": body["id"], "source": "local", "routed": json.dumps([rule])})
        self.put("run_events", str(n)+":"+str(n), {"event_id": n, "run_id": n})
        self.put("runs", n, {"id": n, "state": "queued", "rule": rule, "agent": agent, "rules_version":1})
        if self.lose_reply:
            self.lose_reply = False
            raise TimeoutError("response lost")
        return {"event": n, "routed": [rule]}

    def finish(self, number, words, token=None):
        event = self.sent[number-1]
        _, rule, agent = next(v for v in aams.ROLES.values() if v[0] == event["type"])
        token = token or event["id"]
        self.put("runs", number, {"id": number, "state": "done", "rule": rule, "agent": agent, "rules_version":1,
                 "claimed_at": "2026-11-26T10:01:00Z", "finished_at": "2026-11-26T10:02:00Z",
                 "summary": "AAMS/1 " + token + " " + words})

    def manager_delegates(self, number):
        self.finish(number, "DELEGATE tp-playtest")
        self.board.tick()
        self.board.tick()

    def issue(self, version):
        return self.board.issue(version-1, "指令"+str(version), "验收"+str(version))


class OrdersTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.f = Fixture(Path(self.directory.name))
        self.board = self.f.board

    def test_revision_requires_manager_and_bound_worker_receipt(self):
        self.assertEqual(self.f.issue(1), 1)
        self.assertEqual(self.f.issue(1), 1)
        with self.assertRaises(aams.Conflict):
            self.board.issue(0, "旧标签页另一条指令", "其他范围")
        self.board.tick()
        self.f.manager_delegates(1)
        self.f.finish(2, "ACK")
        self.board.tick()
        self.assertEqual(self.board.view()["effective"], 1)
        self.f.issue(2)
        self.board.tick()
        self.assertEqual(self.board.view()["effective"], 1)
        self.f.manager_delegates(3)
        detail = json.loads(self.f.sent[3]["detail"])
        self.assertEqual((detail["task"], detail["version"], detail["instruction"], detail["acceptance"]),
                         (aams.TASK, 2, "指令2", "验收2"))
        self.f.finish(4, "RESULT | 两条路线已执行，内门卡住 | evidence/manifest.json")
        self.board.tick()
        self.board.tick()
        status = self.board.view()
        self.assertEqual((len(self.f.sent), status["latest"], status["effective"]), (4, 2, 2))
        worker = status["orders"][0]["deliveries"][1]
        self.assertEqual((worker["state"], worker["run_id"], worker["evidence"]),
                         ("reported", 4, "evidence/manifest.json"))

    def test_late_manager_cannot_delegate_superseded_revision(self):
        self.f.issue(1)
        self.board.tick()
        self.f.issue(2)
        self.f.finish(1, "DELEGATE tp-playtest")
        self.board.tick()
        self.board.tick()
        self.assertEqual([event["type"] for event in self.f.sent], ["tp.aams.manager", "tp.aams.manager"])
        self.assertEqual(self.board.view()["effective"], None)

    def test_wrong_token_and_late_worker_do_not_make_a_revision_effective(self):
        self.f.issue(1)
        self.board.tick()
        self.f.manager_delegates(1)
        self.f.issue(2)
        self.f.finish(2, "RESULT | 已执行 | 原版证据")
        self.board.tick()
        self.f.manager_delegates(3)
        self.f.finish(4, "ACK", token=self.f.sent[1]["id"])
        self.board.tick()
        view = self.board.view()
        self.assertIsNone(view["effective"])
        self.assertEqual(view["orders"][0]["deliveries"][1]["state"], "unverified")
        self.assertEqual(view["orders"][1]["deliveries"][1]["state"], "reported")

    def test_lost_reply_survives_restart_without_another_post(self):
        self.f.issue(1)
        self.f.lose_reply = True
        self.board.tick()
        self.assertEqual(self.board.view()["orders"][0]["deliveries"][0]["state"], "sending")
        restarted = aams.OrderBoard(self.f.database, self.f.loop)
        restarted.tick()
        self.assertEqual(len(self.f.sent), 1)
        self.assertEqual(restarted.view()["orders"][0]["deliveries"][0]["run_id"], 1)

    def test_open_decision_wrong_actor_and_blocked_outcomes(self):
        self.f.issue(1)
        self.board.tick()
        self.f.finish(1, "DELEGATE tp-playtest")
        self.f.put("cards", 10, {"id": 10, "kind": "ask", "state": "open", "run_id": 1, "title": "请选择"})
        self.board.tick()
        self.assertEqual(self.board.view()["orders"][0]["deliveries"][0]["state"], "decision")
        self.f.issue(2)
        self.board.tick()
        self.f.put("runs", 2, {"id": 2, "state": "claimed", "rule": aams.ROLES["manager"][1], "agent": "other"})
        self.board.tick()
        self.assertEqual(self.board.view()["orders"][0]["deliveries"][0]["state"], "unverified")
        self.f.issue(3)
        self.board.tick()
        self.f.manager_delegates(3)
        self.f.finish(4, "BLOCKED | 内门未解锁 | run/evidence.png")
        self.board.tick()
        current = self.board.view()
        self.assertEqual(current["effective"], 3)
        self.assertEqual(current["orders"][0]["deliveries"][1]["state"], "blocked")

    def test_ack_or_blocked_can_continue_once_without_changing_finished_receipts(self):
        self.f.issue(1)
        self.board.tick()
        self.f.manager_delegates(1)
        self.f.finish(2, "ACK")
        self.board.tick()
        self.board.continue_result(1, 2)
        self.board.continue_result(1, 2)
        self.board.tick()
        self.assertEqual(json.loads(self.f.sent[2]["detail"])["after_run"], 2)
        self.f.finish(3, "BLOCKED | 正常线已就绪，折返线待补 | QA记录")
        self.board.tick()
        self.board.continue_result(1, 3)
        self.board.tick()
        self.f.finish(4, "RESULT | 两份复测 | 两份记录")
        self.board.tick()
        states = [d["state"] for d in self.board.view()["orders"][0]["deliveries"]]
        self.assertEqual(states,["delegated","acknowledged","blocked","reported"])
        self.f.issue(2)
        with self.assertRaises(aams.Conflict):
            self.board.continue_result(1, 3)

    def test_definitely_unsent_post_is_retried_with_same_identity(self):
        from unittest.mock import patch
        from loop_control import NotSent
        self.f.issue(1)
        with patch.object(self.f.loop, "send", side_effect=NotSent("refused before send")):
            self.board.tick()
        self.assertEqual(self.board.view()["orders"][0]["deliveries"][0]["state"], "ready")
        restarted=aams.OrderBoard(self.f.database,self.f.loop)
        restarted.tick()
        restarted.tick()
        self.assertEqual(len(self.f.sent),1)

    def test_runtime_backend_and_truncated_summary_are_unverified(self):
        self.f.issue(1)
        self.board.tick()
        self.f.finish(1, "DELEGATE tp-playtest")
        self.f.put("config", 1, {"value":{"agents":{"tp-manager":{"use":"command","worker":"test-machine"}}}})
        self.board.tick()
        self.assertEqual(self.board.view()["orders"][0]["deliveries"][0]["state"], "unverified")
        with self.assertRaises(aams.Conflict):
            aams.parse_receipt({"token":"a","role":"worker"},"AAMS/1 a RESULT | ok | "+("x"*500))

    def test_two_panels_wait_for_earlier_inflight_role(self):
        from unittest.mock import patch
        first_reserved, release = threading.Event(), threading.Event()
        self.f.issue(1)
        original = self.f.loop.send
        def delayed(key, row):
            first_reserved.set()
            self.assertTrue(release.wait(4))
            return original(key,row)
        second = aams.OrderBoard(self.f.database,self.f.loop)
        with patch.object(self.f.loop,"send",side_effect=delayed):
            t=threading.Thread(target=self.board.tick); t.start()
            try:
                self.assertTrue(first_reserved.wait(3))
                second.issue(1,"新版","条件")
                second.tick()
                self.assertEqual(len(self.f.sent),0)
            finally:
                release.set();t.join(4)
        self.assertEqual(len(self.f.sent),1)
        self.f.finish(1,"DELEGATE tp-playtest")
        second.tick()
        self.assertEqual([json.loads(x["detail"])["version"] for x in self.f.sent],[1,2])

    def test_decision_waits_for_distinct_answer_run_exact_receipt(self):
        self.f.issue(1); self.board.tick()
        self.f.finish(1,"DELEGATE tp-playtest")
        self.f.put("cards",4,{"run_id":1,"kind":"ask","state":"open","title":"怎么继续"})
        self.board.tick()
        self.f.put("cards",4,{"run_id":1,"kind":"ask","state":"decided","answer_event":20})
        self.board.tick()
        self.assertEqual(self.board.view()["orders"][0]["deliveries"][0]["state"],"decision")
        self.f.put("run_events","9:20",{"event_id":20,"run_id":9})
        self.f.put("runs",9,{"id":9,"state":"done","rule":aams.ROLES["manager"][1],"agent":"tp-manager",
            "rules_version":1,"claimed_at":"2026-11-26T10:40:00Z","finished_at":"2026-11-26T10:42:00Z",
            "summary":"AAMS/1 "+self.f.sent[0]["id"]+" DELEGATE tp-playtest"})
        self.board.tick()
        first=self.board.view()["orders"][0]["deliveries"][0]
        self.assertEqual((first["state"],first["run_id"],first["decision_run_id"]),("delegated",1,9))
