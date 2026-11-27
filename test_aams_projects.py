import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from aams_projects import ProjectBoard, ProjectLoop
from cas_loop_flow import Conflict
from test_aams_orders import Fixture


def manifest():
    return {"project":{"id":"tp","name":"潮核","reviewers":{"independent":{}}},"departments":[{"id":"combat","name":"战斗池","lead":"boss","mainline":"基线",
        "scope_type":"test.scope","scope_rule":"负责人","employees":[
        {"id":"boss","name":"负责人","task_id":"scope-task","task_title":"部门判断"},
        {"id":"player","name":"试玩","task_id":"fish","task_title":"鱼摊","event_type":"test.play","rule":"试玩"},
        {"id":"reviewer","name":"旁单","task_id":"other","task_title":"历史核对","event_type":"test.review","rule":"旁单"},
        {"id":"observer","name":"观察","task_id":"view","task_title":"观察"}]}]}


class ProjectFixture(Fixture):
    roles={"test.scope":("负责人","boss"),"test.play":("试玩","player"),"test.review":("旁单","reviewer")}

    def __init__(self,root):
        super().__init__(root)
        self.loop=ProjectLoop(root,self.request,codex_home=root/"codex")
        self.online=True
        self.put("config",1,{"value":{"agents":{a:{"use":"codex","worker":"test-machine"} for _,a in self.roles.values()}}})
        self.board=ProjectBoard(self.database,self.loop,root)
        self.board.configure(manifest())

    def request(self,key,method,path,body=None,port=None):
        if path=="/v1/state":
            return {"lines":[{"name":r,"agent":a,"source":"local","types":[t],"paused":None} for t,(r,a) in self.roles.items()],
                    "agents":[{"id":a,"use":"codex","worker":"test-machine","online":self.online} for _,a in self.roles.values()],
                    "runs":[]}
        assert (method,path)==("POST","/v1/events")
        if body["id"] in [e["id"] for e in self.sent]:
            n=next(i for i,e in enumerate(self.sent,1) if e["id"]==body["id"])
            return {"event":n,"routed":[self.roles[body["type"]][0]]}
        n=len(self.sent)+1
        rule,agent=self.roles[body["type"]]
        self.sent.append(body)
        self.put("events",n,{**body,"id":n,"key":body["id"],"source":"local","routed":json.dumps([rule])})
        self.put("run_events",f"{n}:{n}",{"event_id":n,"run_id":n})
        self.put("runs",n,{"id":n,"rule":rule,"agent":agent,"state":"queued","rules_version":1})
        if self.lose_reply:
            self.lose_reply=False
            raise TimeoutError()
        return {"event":n,"routed":[rule]}

    def finish(self,n,receipt,token=None,session=None):
        event=self.sent[n-1]
        rule,agent=self.roles[event["type"]]
        self.put("runs",n,{"id":n,"agent":agent,"rule":rule,"rules_version":1,"state":"done",
             "claimed_at":"2026-11-26T10:00:00Z","finished_at":"2026-11-26T10:10:00Z",
             "session":session,"summary":"AAMS/2 "+(token or event["id"])+" "+receipt})


class ProjectTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.f=ProjectFixture(self.root)
        self.board=self.f.board

    def issue(self,id,scope="mainline",version=0,agent=None,task=None):
        return self.board.issue(id,"tp","combat",scope,agent,task,version,"主管要求 "+id,"实际结果")

    def dept(self):
        return self.board.view("tp")["departments"][0]

    def test_manager_scope_and_personal_change_preserve_sibling_and_mainline(self):
        self.issue("main")
        self.assertEqual(self.dept()["changes"][0]["deliveries"][0]["state"],"ready")
        self.board.tick()
        self.f.finish(1,"SCOPE player | 只影响鱼摊")
        self.board.tick()
        self.board.tick()
        self.assertEqual([e["type"] for e in self.f.sent],["test.scope","test.play"])
        self.f.finish(2,"APPLIED_RESULT | 核对完成 | field/candidate.json")
        self.board.tick()
        before=self.dept()
        self.issue("solo","person",0,"player","fish")
        self.assertEqual(self.issue("solo","person",0,"player","fish"),{"id":"solo","version":1})
        self.f.lose_reply=True
        self.board.tick()
        self.board.tick()
        self.f.finish(3,"APPLIED | 已采用新指令 | field/first.json")
        self.board.tick()
        self.board.continue_change("solo","player",3)
        self.board.continue_change("solo","player",3)
        self.board.tick()
        self.f.finish(4,"APPLIED_RESULT | 两线实测结论 | field/record-solo.json")
        self.board.tick()
        after=self.dept()
        self.assertEqual(len(self.f.sent),4)
        self.assertEqual(after["mainline"],before["mainline"])
        sibling=lambda d:next(a for a in d["employees"] if a["id"]=="reviewer")
        self.assertEqual(sibling(after),sibling(before))
        last=after["changes"][0]["deliveries"][-1]
        self.assertEqual((last["agent"],last["state"],last["run_id"]),("player","reported",4))
        self.assertTrue(last["claimed_at"] and last["applied_at"] and last["finished_at"])
        self.assertEqual(next(a for a in after["employees"] if a["id"]=="player")["task"]["version"],1)

    def test_invalid_targets_offline_pause_and_bounded_runtime_metadata(self):
        self.f.online=False
        self.issue("adjust")
        self.board.tick()
        self.assertEqual(len(self.f.sent),0)
        self.assertEqual(self.dept()["status"],"offline")
        self.f.online=True
        self.board.pause("tp","combat",True)
        self.board.tick()
        self.assertEqual(len(self.f.sent),0)
        self.board.pause("tp","combat",False)
        self.board.tick()
        self.f.finish(1,"SCOPE observer | 试派")
        self.board.tick()
        self.assertEqual(self.dept()["changes"][0]["deliveries"][0]["state"],"unverified")
        with self.assertRaises(Conflict):self.issue("wrong","person",0,"reviewer","fish")
        with self.assertRaises(Conflict):self.issue("obs","person",0,"observer","view")
        self.issue("p","person",0,"player","fish")
        self.board.tick()
        self.board.pause("tp","combat",True)
        sid="11111111-0000-0000-0000-000000000007"
        repo=self.root/"codex"/"sessions"/"2026"/"11"/"26"
        repo.mkdir(parents=True)
        events=[{"timestamp":t,"type":"turn_context","payload":{"model":m,"effort":"max"}} for t,m in
                [("2026-11-26T09:58:00Z","stale-model"),("2026-11-26T10:01:00Z","actual-model")]]
        (repo/("rollout-test-"+sid+".jsonl")).write_text("\n".join(json.dumps(v,separators=(",",":")) for v in events),encoding="utf-8")
        self.f.finish(2,"APPLIED_RESULT | 穿过鱼摊 | field/record-p.json",session=sid)
        self.board.tick()
        player=next(a for a in self.dept()["employees"] if a["id"]=="player")
        self.assertEqual((player["model"],player["effort"],player["work_status"]),("actual-model","max","reported"))

    def test_revisions_no_cross_scope_replay_and_atomic_bootstrap(self):
        self.issue("m1")
        self.issue("m2",version=1)
        self.board.tick()
        self.assertEqual(len(self.f.sent),1)
        with self.assertRaises(Conflict):self.issue("m3",version=0)
        self.f.finish(1,"SCOPE NONE | 本单均不涉及")
        self.board.tick()
        self.assertEqual(self.dept()["changes"][0]["deliveries"][0]["targets"],[])
        other=manifest()
        other["project"]={"id":"bad","name":"回滚验证"}
        other["departments"][0]["employees"][-1]["task_id"]="other"
        with self.assertRaises(sqlite3.IntegrityError):self.board.configure(other)
        self.assertEqual([p["id"] for p in self.board.projects()["projects"]],["tp"])

    def test_reviews_require_bound_artifacts_and_remain_across_reopen(self):
        self.issue("local","person",0,"player","fish")
        self.board.tick()
        self.f.finish(1,"APPLIED_RESULT | 实测 | field/record-local.json")
        self.board.tick()
        folder=self.root/"field";folder.mkdir()
        ref="field/record-local.json"
        (folder/"record-local.json").write_text('{"change_id":"local","routes":2}')
        fields=dict(change_id="local",reviewer="independent",review_run=None,conclusion="两线核对",candidate="a"*64,
                    commit="b"*40,pr="https://github.com/example/product/pull/2",evidence=ref)
        first=self.board.review(**fields)
        self.assertEqual(self.board.review(**fields),first)
        with self.assertRaises(ValueError):self.board.review(**{**fields,"review_run":9})
        with self.assertRaises(ValueError):self.board.review(**{**fields,"review_run":1,"reviewer":"player"})
        again=ProjectBoard(self.f.database,None,self.root)
        self.assertEqual(again.view("tp")["departments"][0]["changes"][0]["reviews"][0]["id"],first["id"])
        self.assertEqual(again.attachment("local",ref)["routes"],2)
        with self.assertRaises(ValueError):again.attachment("no-such-change",ref)
        (folder/"record-local.json").write_text('{"change_id":"another","task_id":"other"}')
        with self.assertRaises(ValueError):again.attachment("local",ref)
        self.assertFalse(again.view("tp")["departments"][0]["changes"][0]["deliveries"][0]["evidence_available"])

    def test_unknown_unsent_stale_revision_is_only_observed_while_newer_advances(self):
        self.issue("old","person",0,"player","fish")
        import sync
        with sync.connect(self.f.database) as db:
            db.execute("UPDATE aams_dispatch SET state='sending',retry_at=0 WHERE change_id='old'")
        self.issue("newer","person",1,"player","fish")
        self.board.tick()
        self.board.tick()
        self.board.tick()
        self.assertEqual([json.loads(e["detail"])["change_id"] for e in self.f.sent],["newer"])
        rows=self.dept()["changes"]
        self.assertEqual(rows[1]["deliveries"][0]["state"],"reconciling")
