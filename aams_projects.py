"""Local project registry, department scope decisions and task-specific corrections."""
import argparse
from contextlib import closing
import hashlib
import http.client
import json
from pathlib import Path
import re
import secrets
import sqlite3
from threading import Event, Lock, Thread
import time

import sync
from aams_orders import PENDING
from aams_project_loop import (
    ASSIGNMENT_STAGES, DELEGATION_SCOPES, DEPARTMENT_DELEGATION, EMPLOYEE_DELEGATION,
    PROJECT_DELEGATION, ProjectLoop, delegation_evidence, instant,
)
from cas_loop_flow import Conflict
from loop_control import NotSent, Rejected


SCHEMA = """
CREATE TABLE IF NOT EXISTS aams_projects(id TEXT PRIMARY KEY,name TEXT NOT NULL,reviewers TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS aams_departments(
 project TEXT NOT NULL,id TEXT NOT NULL,name TEXT NOT NULL,lead TEXT NOT NULL,baseline TEXT NOT NULL,
 scope_type TEXT,scope_rule TEXT,paused INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(project,id));
CREATE TABLE IF NOT EXISTS aams_staff(
 project TEXT NOT NULL,department TEXT NOT NULL,agent TEXT NOT NULL,name TEXT NOT NULL,
 task_id TEXT NOT NULL,task_title TEXT NOT NULL,event_type TEXT,rule TEXT,history_run INTEGER,
 PRIMARY KEY(project,department,agent),UNIQUE(project,task_id));
CREATE TABLE IF NOT EXISTS aams_changes(
 id TEXT PRIMARY KEY,project TEXT NOT NULL,department TEXT NOT NULL,scope TEXT NOT NULL,lane TEXT NOT NULL,
 target TEXT,task_id TEXT,version INTEGER NOT NULL,instruction TEXT NOT NULL,acceptance TEXT NOT NULL,
 roster TEXT NOT NULL,created REAL NOT NULL,UNIQUE(project,department,lane,version));
CREATE TABLE IF NOT EXISTS aams_dispatch(
 token TEXT PRIMARY KEY,change_id TEXT NOT NULL,stage TEXT NOT NULL,agent TEXT NOT NULL,bound_task TEXT NOT NULL,
 event_type TEXT NOT NULL,rule TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'ready',
 event_id INTEGER UNIQUE,run_id INTEGER UNIQUE,decision_run_id INTEGER,claimed_at TEXT,applied_at TEXT,
 finished_at TEXT,result TEXT,evidence TEXT,note TEXT,model TEXT,effort TEXT,model_source TEXT,retry_at REAL,
 UNIQUE(change_id,stage,agent));
CREATE TABLE IF NOT EXISTS aams_reviews(
 id TEXT PRIMARY KEY,change_id TEXT NOT NULL,reviewer TEXT NOT NULL,review_run INTEGER,source TEXT NOT NULL,
 conclusion TEXT NOT NULL,candidate TEXT NOT NULL,commit_sha TEXT NOT NULL,pr TEXT NOT NULL,evidence TEXT NOT NULL,created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_aams_dispatch_state ON aams_dispatch(state,agent);
"""

ADDITIONS = {
    "aams_projects": {"reviewers": "TEXT NOT NULL DEFAULT '{}'", "manager": "TEXT",
                      "manager_type": "TEXT", "manager_rule": "TEXT",
                      "delegations_paused": "INTEGER NOT NULL DEFAULT 0"},
    "aams_departments": {"delegation_only": "INTEGER NOT NULL DEFAULT 0"},
    "aams_staff": {"proof_kind": "TEXT"},
    "aams_changes": {"parent_id": "TEXT", "assigned_run_id": "INTEGER", "predecessor_id": "TEXT", "context": "TEXT"},
    "aams_dispatch": {"session": "TEXT", "rules_version": "INTEGER", "decision_session": "TEXT", "proof": "TEXT"},
}
PROOF_KINDS = ("result", "pr", "review")
PR = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/[1-9]\d*")
SHA = re.compile("[a-fA-F0-9]{7,64}")


def required(db, sql, args, message):
    row=db.execute(sql,args).fetchone()
    if row is None:
        raise ValueError(message)
    return row


def compact(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"))


def bound_route(event_type,rule):
    if event_type is None and rule is None:
        return None,None
    if not isinstance(event_type,str) or not re.fullmatch(r"[a-z][a-z0-9.-]{1,119}",event_type):
        raise ValueError("需要有效的专用 Loop 事件类型")
    return event_type,sync.clean_text(rule,100)


class ProjectBoard:
    def __init__(self,path,loop=None,evidence_root=None,delegation_evidence_root=None):
        self.path,self.loop=Path(path),loop
        self.evidence_root=Path(evidence_root).resolve() if evidence_root else None
        self.delegation_evidence_root=Path(delegation_evidence_root).resolve() if delegation_evidence_root else self.evidence_root
        self.wake,self.stop=Event(),Event()
        self.lock,self.thread=Lock(),None
        self.presence,self.presence_at={},0
        with sync.connect(self.path) as db:
            db.executescript(SCHEMA)
            db.execute("BEGIN IMMEDIATE")
            for table, additions in ADDITIONS.items():
                columns = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
                for name, definition in additions.items():
                    if name not in columns:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            db.execute("CREATE INDEX IF NOT EXISTS idx_aams_changes_parent ON aams_changes(parent_id)")

    def start(self):
        if self.loop is not None:
            self.thread=Thread(target=self._work,name="aams-department-reconciliation",daemon=True)
            self.thread.start()

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.thread:
            self.thread.join(8)

    def _work(self):
        while not self.stop.is_set():
            self.wake.clear()
            try:
                self.tick()
            except sqlite3.OperationalError:
                pass
            self.wake.wait(2)

    def projects(self):
        with sync.connect(self.path,readonly=True) as db:
            return {"projects":[dict(r) for r in db.execute("SELECT id,name FROM aams_projects ORDER BY name,id")]}

    def create_project(self,id,name):
        id,name=sync.identifier(id),sync.clean_text(name,100)
        with sync.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            self._project(db,id,name)
        return {"id":id,"name":name}

    @staticmethod
    def _project(db,id,name):
        db.execute("INSERT OR IGNORE INTO aams_projects(id,name) VALUES(?,?)",(id,name))
        if db.execute("SELECT name FROM aams_projects WHERE id=?",(id,)).fetchone()[0]!=name:
            raise Conflict("该项目编号已属于另一个项目")

    def add_department(self,project,id,name,lead,mainline):
        project,id,lead=map(sync.identifier,(project,id,lead))
        name,mainline=sync.clean_text(name,100),sync.clean_text(mainline,950)
        with sync.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            self._department(db,project,id,name,lead,mainline)
        return {"id":id}

    @staticmethod
    def _department(db,project,id,name,lead,mainline):
        required(db,"SELECT id FROM aams_projects WHERE id=?",(project,),"本地项目不存在")
        old=db.execute("SELECT name,lead,baseline FROM aams_departments WHERE project=? AND id=?",(project,id)).fetchone()
        if old and tuple(old)!=(name,lead,mainline):
            raise Conflict("该部门编号已登记")
        if not old:
            db.execute("INSERT INTO aams_departments(project,id,name,lead,baseline) VALUES(?,?,?,?,?)",(project,id,name,lead,mainline))
            db.execute("INSERT INTO aams_staff(project,department,agent,name,task_id,task_title) VALUES(?,?,?,?,?,?)",
                       (project,id,lead,lead,id+"-lead","部门主线判断"))

    def employee(self,project,department,id,name,task_id,task_title):
        project,department,id,task_id=map(sync.identifier,(project,department,id,task_id))
        name,task_title=sync.clean_text(name,100),sync.clean_text(task_title,240)
        with sync.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            required(db,"SELECT id FROM aams_departments WHERE project=? AND id=?",(project,department),"部门不存在")
            old=db.execute("SELECT name,task_id,task_title FROM aams_staff WHERE project=? AND department=? AND agent=?",
                           (project,department,id)).fetchone()
            if old and tuple(old)!=(name,task_id,task_title):
                raise Conflict("岗位已登记，不能通过新建覆盖它正在执行的任务")
            if not old:
                db.execute("INSERT INTO aams_staff(project,department,agent,name,task_id,task_title) VALUES(?,?,?,?,?,?)",
                           (project,department,id,name,task_id,task_title))
        return {"id":id}

    def configure(self,manifest):
        """Bind a local operator's explicitly supplied roster. Re-imports may not switch active work."""
        project=manifest["project"]
        pid,name=sync.identifier(project["id"]),sync.clean_text(project["name"],100)
        with sync.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            self._project(db,pid,name)
            if "manager" in project:
                manager = project["manager"]
                desired = (sync.identifier(manager["id"]), *bound_route(manager["event_type"], manager["rule"]))
                if desired[1] is None:
                    raise ValueError("项目管理员需要专用 Loop 路由")
                current = db.execute("SELECT manager,manager_type,manager_rule FROM aams_projects WHERE id=?", (pid,)).fetchone()
                if tuple(current) != desired:
                    if db.execute("SELECT 1 FROM aams_changes WHERE project=? AND scope=?", (pid, PROJECT_DELEGATION)).fetchone():
                        raise Conflict("已有项目委托，不能覆盖原管理员绑定")
                    db.execute("UPDATE aams_projects SET manager=?,manager_type=?,manager_rule=? WHERE id=?", (*desired, pid))
            if "reviewers" in project:
                routes={}
                for reviewer,item in project["reviewers"].items():
                    reviewer=sync.identifier(reviewer)
                    route=bound_route(item.get("event_type"),item.get("rule"))
                    routes[reviewer]=list(route) if route[0] else None
                db.execute("UPDATE aams_projects SET reviewers=? WHERE id=?",(compact(routes),pid))
            for dept in manifest["departments"]:
                did,lead=sync.identifier(dept["id"]),sync.identifier(dept["lead"])
                self._department(db,pid,did,sync.clean_text(dept["name"],100),lead,sync.clean_text(dept["mainline"],950))
                scope=bound_route(dept.get("scope_type"),dept.get("scope_rule"))
                existing=bool(db.execute("SELECT 1 FROM aams_changes WHERE project=? AND department=?",(pid,did)).fetchone())
                current=db.execute("SELECT scope_type,scope_rule,delegation_only FROM aams_departments WHERE project=? AND id=?",(pid,did)).fetchone()
                delegation_only = dept.get("delegation_only", bool(current["delegation_only"]))
                if type(delegation_only) is not bool or (delegation_only and scope[0] is None):
                    raise ValueError("委托部门需要单独绑定负责人路由")
                if existing and tuple(current)!=(*scope, delegation_only):
                    raise Conflict("已有工单，不能覆盖负责人路由")
                db.execute("UPDATE aams_departments SET scope_type=?,scope_rule=?,delegation_only=? WHERE project=? AND id=?",
                           (*scope,delegation_only,pid,did))
                for member in dept["employees"]:
                    agent,task=map(sync.identifier,(member["id"],member["task_id"]))
                    name,title=sync.clean_text(member["name"],100),sync.clean_text(member["task_title"],240)
                    route=bound_route(member.get("event_type"),member.get("rule"))
                    history=member.get("history_run")
                    if history is not None and (type(history) is not int or history<1):
                        raise ValueError("历史轮次无效")
                    proof_kind = member.get("proof_kind")
                    if proof_kind is not None and (not delegation_only or proof_kind not in PROOF_KINDS or not route[0]):
                        raise ValueError("委托产物类型须绑定可控员工")
                    prior=db.execute("SELECT name,task_id,task_title,event_type,rule,history_run,proof_kind FROM aams_staff WHERE project=? AND department=? AND agent=?",(pid,did,agent)).fetchone()
                    values=(name,task,title,*route,history,proof_kind)
                    if existing and prior and tuple(prior)!=values:
                        raise Conflict("不能改写已有工单的岗位和任务绑定")
                    db.execute("""INSERT INTO aams_staff(project,department,agent,name,task_id,task_title,event_type,rule,history_run,proof_kind)
                        VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(project,department,agent) DO UPDATE SET name=excluded.name,
                        task_id=excluded.task_id,task_title=excluded.task_title,event_type=excluded.event_type,
                        rule=excluded.rule,history_run=excluded.history_run,proof_kind=excluded.proof_kind""",(pid,did,agent,*values))
        return self.view(project["id"])

    def pause(self,project,department,paused):
        if type(paused) is not bool:
            raise ValueError("暂停标记须为布尔值")
        with sync.connect(self.path) as db:
            if not db.execute("UPDATE aams_departments SET paused=? WHERE project=? AND id=?",(paused,project,department)).rowcount:
                raise ValueError("部门不存在")
        self.wake.set()
        return {"paused":paused}

    def _deliver(self,db,change,stage,agent,task,route,state="ready"):
        token="aams2-"+secrets.token_hex(16)
        db.execute("""INSERT INTO aams_dispatch(token,change_id,stage,agent,bound_task,event_type,rule,state)
                    VALUES(?,?,?,?,?,?,?,?)""",(token,change,stage,agent,task,*route,state))

    def issue(self,request_id,project,department,scope,agent,task_id,expected_version,instruction,acceptance):
        request_id,project,department=map(sync.identifier,(request_id,project,department))
        if scope not in ("mainline","person") or type(expected_version) is not int or not 0<=expected_version<1_000_000:
            raise ValueError("需要实际范围和当前版号")
        if (scope=="mainline" and (agent is not None or task_id is not None)):
            raise ValueError("部门主线不预先指定员工")
        if scope=="person":
            agent,task_id=sync.identifier(agent),sync.identifier(task_id)
        lane="mainline" if scope=="mainline" else "person/"+agent+"/"+task_id
        instruction,acceptance=sync.clean_text(instruction,950),sync.clean_text(acceptance,950)
        version=expected_version+1
        values=(project,department,scope,lane,agent,task_id,version,instruction,acceptance)
        with sync.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            prior=db.execute("SELECT project,department,scope,lane,target,task_id,version,instruction,acceptance FROM aams_changes WHERE id=?",(request_id,)).fetchone()
            if prior:
                if tuple(prior)!=values:
                    raise Conflict("同一请求已绑定另一项修改")
                return {"id":request_id,"version":version}
            dept=required(db,"SELECT * FROM aams_departments WHERE project=? AND id=?",(project,department),"部门不存在")
            if dept["delegation_only"]:
                raise Conflict("该工作池由项目委托逐级分派")
            current=db.execute("SELECT COALESCE(MAX(version),0) FROM aams_changes WHERE project=? AND department=? AND lane=?",(project,department,lane)).fetchone()[0]
            if current!=expected_version:
                raise Conflict("该范围已有新版，请刷新核对")
            staff=db.execute("SELECT * FROM aams_staff WHERE project=? AND department=? ORDER BY agent",(project,department)).fetchall()
            roster=[{"agent":s["agent"],"task_id":s["task_id"],"task_title":s["task_title"],"controllable":bool(s["event_type"] and s["rule"])} for s in staff]
            if scope=="mainline":
                if not dept["scope_type"] or not dept["scope_rule"]:
                    raise Conflict("该部门负责人仅观察，尚未绑定 Loop")
                target=next(s for s in staff if s["agent"]==dept["lead"])
                stage,route="scope",(dept["scope_type"],dept["scope_rule"])
            else:
                target=next((s for s in staff if s["agent"]==agent and s["task_id"]==task_id),None)
                if target is None or not target["event_type"] or not target["rule"]:
                    raise Conflict("只能纠正该岗位当前绑定的可控任务")
                stage,route="apply",(target["event_type"],target["rule"])
            db.execute("""UPDATE aams_dispatch SET state='superseded',note=NULL WHERE state='ready'
                       AND change_id IN (SELECT id FROM aams_changes WHERE project=? AND department=? AND lane=?)""",(project,department,lane))
            db.execute("""INSERT INTO aams_changes(id,project,department,scope,lane,target,task_id,version,instruction,acceptance,roster,created)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",(request_id,*values,compact(roster),time.time()))
            self._deliver(db,request_id,stage,target["agent"],target["task_id"],route)
        self.wake.set()
        return {"id":request_id,"version":version}

    def issue_delegation(self, request_id, project, task_id, expected_version, instruction, acceptance):
        request_id, project, task_id = map(sync.identifier, (request_id, project, task_id))
        if type(expected_version) is not int or not 0 <= expected_version < 1_000_000:
            raise ValueError("需要当前委托版号")
        instruction, acceptance = sync.clean_text(instruction, 950), sync.clean_text(acceptance, 950)
        version = expected_version + 1
        lane = "delegation/" + task_id
        scope = PROJECT_DELEGATION
        with sync.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT project,scope,task_id,version,instruction,acceptance FROM aams_changes WHERE id=?",
                               (request_id,)).fetchone()
            if prior:
                if tuple(prior) != (project, scope, task_id, version, instruction, acceptance):
                    raise Conflict("同一请求已绑定另一项委托")
                return {"id": request_id, "version": version}
            owner = required(db, "SELECT manager,manager_type,manager_rule FROM aams_projects WHERE id=?",
                             (project,), "本地项目不存在")
            if not all(owner):
                raise Conflict("项目管理员尚未绑定正式 Loop")
            manager, event_type, rule = owner
            current = db.execute("SELECT COALESCE(MAX(version),0) FROM aams_changes WHERE project=? AND department='' AND lane=?",
                                 (project, lane)).fetchone()[0]
            if current != expected_version:
                raise Conflict("该项目委托已有新版，请刷新核对")
            departments = [dict(d) for d in db.execute("""SELECT d.id AS department,d.name,d.lead,
                           d.baseline AS mainline,s.task_id,s.task_title FROM aams_departments d JOIN aams_staff s
                           ON s.project=d.project AND s.department=d.id AND s.agent=d.lead
                           WHERE d.project=? AND d.delegation_only=1 AND d.scope_type IS NOT NULL AND d.lead!=?
                           ORDER BY d.id""", (project, manager))]
            if not departments:
                raise Conflict("没有登记身份独立的委托部门负责人")
            db.execute("""WITH RECURSIVE expired(id) AS (
                       SELECT id FROM aams_changes WHERE project=? AND department='' AND lane=?
                       UNION ALL SELECT child.id FROM aams_changes child JOIN expired ON child.parent_id=expired.id)
                       UPDATE aams_dispatch SET state='superseded',note=NULL WHERE state IN ('ready','waiting')
                       AND change_id IN (SELECT id FROM expired)""", (project, lane))
            context = {"root_id": request_id, "root_task_id": task_id}
            db.execute("""INSERT INTO aams_changes(id,project,department,scope,lane,target,task_id,version,
                       instruction,acceptance,roster,created,context) VALUES(?,?,'',?,?,?,?,?,?,?,?,?,?)""",
                       (request_id, project, scope, lane, manager, task_id, version, instruction, acceptance,
                        compact(departments), time.time(), compact(context)))
            self._deliver(db, request_id, "assign-department", manager, task_id, (event_type, rule))
        self.wake.set()
        return {"id": request_id, "version": version}

    def pause_delegations(self, project, paused):
        project = sync.identifier(project)
        if type(paused) is not bool:
            raise ValueError("暂停标记须为布尔值")
        with sync.connect(self.path) as db:
            if not db.execute("UPDATE aams_projects SET delegations_paused=? WHERE id=? AND manager IS NOT NULL",
                              (paused, project)).rowcount:
                raise ValueError("项目管理员尚未登记")
        self.wake.set()
        return {"paused": paused}

    @staticmethod
    def _root(db,row):
        while row["parent_id"]:
            row = required(db, "SELECT * FROM aams_changes WHERE id=?", (row["parent_id"],), "原项目委托不存在")
        return row

    @staticmethod
    def _latest(db,row):
        row = ProjectBoard._root(db, row)
        return db.execute("SELECT MAX(version) FROM aams_changes WHERE project=? AND department=? AND lane=?",
                          (row["project"],row["department"],row["lane"])).fetchone()[0]==row["version"]

    @staticmethod
    def _paused(db, row):
        if row["scope"] in DELEGATION_SCOPES:
            if db.execute("SELECT delegations_paused FROM aams_projects WHERE id=?", (row["project"],)).fetchone()[0]:
                return True
        if row["department"]:
            return bool(db.execute("SELECT paused FROM aams_departments WHERE project=? AND id=?",
                                   (row["project"], row["department"])).fetchone()[0])
        return False

    def continue_change(self,id,agent,after_run):
        id,agent=map(sync.identifier,(id,agent))
        if type(after_run) is not int or after_run<1:
            raise ValueError("需要原岗位已终结的轮次")
        stage="report-"+str(after_run)
        with sync.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            change=required(db,"SELECT * FROM aams_changes WHERE id=?",(id,),"工单不存在")
            if change["scope"] in (PROJECT_DELEGATION, DEPARTMENT_DELEGATION):
                raise Conflict("分派决策不能由执行回执续收")
            if not self._latest(db,change):
                raise Conflict("旧版不能续收结果")
            if db.execute("SELECT 1 FROM aams_dispatch WHERE change_id=? AND agent=? AND stage=?",(id,agent,stage)).fetchone():
                return {"id":id}
            last=required(db,"SELECT * FROM aams_dispatch WHERE change_id=? AND agent=? AND stage!='scope' ORDER BY rowid DESC LIMIT 1",
                          (id,agent),"该员工没有本版分派")
            if last["run_id"]!=after_run or last["state"] not in ("applied","blocked"):
                raise Conflict("需要该岗位本版最后一份已生效或受阻的回执")
            self._deliver(db,id,stage,agent,last["bound_task"],(last["event_type"],last["rule"]))
        self.wake.set()
        return {"id":id}

    def _new_child(self, db, row, values, scope, department, assignee, instruction, roster, stage, route,
                   predecessor=None, proof_kind=None):
        child_id = "delegation-" + secrets.token_hex(16)
        root = self._root(db, row)
        assigned_run = values.get("decision_run_id") or values["run_id"]
        context = {"root_id": root["id"], "root_task_id": root["task_id"],
                   "root_instruction": root["instruction"], "delegated_by": row["agent"]}
        if proof_kind:
            context.update(proof_kind=proof_kind, department_decision=row["instruction"])
        db.execute("""INSERT INTO aams_changes(id,project,department,scope,lane,target,task_id,version,
                   instruction,acceptance,roster,created,parent_id,assigned_run_id,predecessor_id,context)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (child_id, row["project"], department, scope, "delegation/"+child_id, assignee["agent"],
                    assignee["task_id"], row["version"], instruction, row["acceptance"], compact(roster), time.time(),
                    row["change_id"], assigned_run, predecessor, compact(context)))
        self._deliver(db, child_id, stage, assignee["agent"], assignee["task_id"], route,
                      state="waiting" if predecessor else "ready")
        return child_id

    def _dispatch_children(self, db, row, values):
        roster = json.loads(row["roster"])
        if row["stage"] == "assign-department":
            chosen = next(item for item in roster if item["department"] == values["evidence"])
            department = chosen["department"]
            lead = required(db, """SELECT s.agent,s.task_id,d.scope_type,d.scope_rule FROM aams_departments d
                     JOIN aams_staff s ON s.project=d.project AND s.department=d.id AND s.agent=d.lead
                     WHERE d.project=? AND d.id=? AND d.delegation_only=1 AND d.lead=? AND s.task_id=?""",
                     (row["project"], department, chosen["lead"], chosen["task_id"]), "部门负责人绑定已经变化")
            employees = [dict(member) for member in db.execute("""SELECT agent,name,task_id,task_title,
                         COALESCE(proof_kind,'result') AS proof_kind FROM aams_staff WHERE project=? AND department=?
                         AND agent NOT IN (?,?) AND event_type IS NOT NULL AND rule IS NOT NULL ORDER BY agent""",
                         (row["project"], department, row["agent"], lead["agent"]))]
            if not employees:
                raise Conflict("部门没有身份独立且可派发的员工")
            self._new_child(db, row, values, DEPARTMENT_DELEGATION, department, lead, values["result"],
                            employees, "assign-staff", (lead["scope_type"], lead["scope_rule"]))
            return
        eligible = {item["agent"]: item for item in roster}
        predecessor, previous_kind = None, None
        for agent, instruction in json.loads(values["evidence"]):
            chosen = eligible[agent]
            member = required(db, """SELECT agent,task_id,event_type,rule,COALESCE(proof_kind,'result') AS proof_kind
                       FROM aams_staff WHERE project=? AND department=? AND agent=? AND task_id=?
                       AND event_type IS NOT NULL AND rule IS NOT NULL""",
                       (row["project"], row["department"], agent, chosen["task_id"]), "员工任务绑定已经变化")
            proof_kind = chosen["proof_kind"]
            if member["proof_kind"] != proof_kind:
                raise Conflict("员工产物要求已经变化")
            required_previous = {"pr": "result", "review": "pr"}.get(proof_kind)
            if required_previous and previous_kind != required_previous:
                raise Conflict("本次分派顺序缺少必需的候选或 PR 前序任务")
            predecessor = self._new_child(db, row, values, EMPLOYEE_DELEGATION, row["department"], member,
                                          instruction, [], "apply", (member["event_type"], member["rule"]),
                                          predecessor=predecessor, proof_kind=proof_kind)
            previous_kind = proof_kind

    def _delegation_receipt(self, db, row, values):
        session = values.get("decision_session") if values.get("decision_run_id") else values.get("session")
        if not session or not values.get("session"):
            raise Conflict("正式轮次缺少可核对的 Codex 会话")
        root = self._root(db, row)
        reused = db.execute("""WITH RECURSIVE lineage(id) AS (
                   SELECT ? UNION ALL SELECT c.id FROM aams_changes c JOIN lineage ON c.parent_id=lineage.id)
                   SELECT 1 FROM aams_dispatch WHERE change_id IN (SELECT id FROM lineage) AND agent!=?
                   AND (session IN (?,?) OR decision_session IN (?,?))""",
                   (root["id"], row["agent"], session, values["session"], session, values["session"])).fetchone()
        if reused:
            raise Conflict("两个岗位使用同一 Codex 会话，无法核对独立接单")
        if values["state"] != "reported":
            return values
        ref = values["evidence"]
        if ref != delegation_evidence(row):
            raise Conflict("员工结果需要本子任务专属的现场实录")
        try:
            record = self._bound_record(row["change_id"], ref, {row["bound_task"]}, delegation=True)
        except (ValueError, OSError, TypeError) as exc:
            raise Conflict("员工现场实录缺失或与子任务不符") from exc
        if (record.get("task_id") != row["bound_task"] or record.get("version") != row["version"]
                or type(record.get("version")) is not int or record.get("outcome") not in ("passed", "failed")):
            raise Conflict("现场实录须与子任务及本版验收结果对应")
        proof = {key: record[key] for key in ("outcome", "candidate", "commit", "pr") if key in record}
        proof["record_sha256"] = hashlib.sha256(compact(record).encode()).hexdigest()
        for key, pattern in (("candidate", SHA), ("commit", SHA), ("pr", PR)):
            if key in proof and (not isinstance(proof[key], str) or not pattern.fullmatch(proof[key])):
                raise Conflict("现场实录包含无效候选、提交或 PR")
        if proof["outcome"] == "failed":
            return {**values, "state": "blocked", "note": "本版实测未通过，后续派发待确认", "proof": compact(proof)}
        kind = json.loads(row["context"])["proof_kind"]
        if kind in ("pr", "review"):
            if any(key not in proof for key in ("candidate", "commit", "pr")) or not row["previous_proof"]:
                raise Conflict("交付或复核须包含已验证候选、提交及 GitHub PR")
            previous = json.loads(row["previous_proof"])
            keys = ("candidate", "commit", "pr") if kind == "review" else ("candidate",)
            if any(proof[key] != previous.get(key) for key in keys):
                raise Conflict("交付或独立复核与本版上一子任务的实际结果不匹配")
        return {**values, "proof": compact(proof)}

    def _save(self,row,values):
        with sync.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            if values.get("state")=="ready" and not self._latest(db,row):
                values={"state":"superseded","note":None}
            if row["scope"] in DELEGATION_SCOPES and values.get("state") in ("assigned", "applied", "reported"):
                values = self._delegation_receipt(db, row, values)
            if values.get("state") in ("applied","reported"):
                values["applied_at"]=values.get("finished_at")
            if not db.execute("UPDATE aams_dispatch SET "+",".join(k+"=?" for k in values)+
                              " WHERE token=? AND state=?",(*values.values(),row["token"],row["state"])).rowcount:
                return
            if values.get("state") not in ("scoped", "assigned", "reported") or not self._latest(db,row):
                return
            if values["state"] == "assigned":
                self._dispatch_children(db, row, values)
                self.wake.set()
                return
            if values["state"] == "reported":
                if row["scope"] == EMPLOYEE_DELEGATION:
                    db.execute("""UPDATE aams_dispatch SET state='ready' WHERE state='waiting' AND change_id IN
                               (SELECT id FROM aams_changes WHERE predecessor_id=?)""", (row["change_id"],))
                    self.wake.set()
                return
            roster={a["agent"]:a for a in json.loads(row["roster"])}
            for agent in json.loads(values["evidence"]):
                member=required(db,"SELECT * FROM aams_staff WHERE project=? AND department=? AND agent=? AND task_id=?",
                                (row["project"],row["department"],agent,roster[agent]["task_id"]),"员工当前任务已变更")
                self._deliver(db,row["change_id"],"apply",agent,member["task_id"],(member["event_type"],member["rule"]))
            self.wake.set()

    def _advance(self,row):
        if row["state"]!="ready":
            observed=self.loop.observe(row)
            if observed:
                try:
                    self._save(row,{**observed,"note":None})
                except (Conflict, ValueError) as exc:
                    self._save(row,{**observed, "state":"unverified", "note":str(exc)})
                return
            # A fresh, readable official ledger without this key allows retrying the SAME id.
            # The Loop kernel deduplicates source+event.id, including a concurrent late first POST.
            if row["state"]!="sending" or (row["retry_at"] or 0)>time.time():
                return
            with sync.connect(self.path) as db:
                db.execute("BEGIN IMMEDIATE")
                if not self._latest(db,row):
                    db.execute("""UPDATE aams_dispatch SET state='reconciling',note='已有新版，仅继续核对旧事件'
                               WHERE token=? AND state='sending'""",(row["token"],))
                    return
        if row["predecessor_id"]:
            proof = json.loads(row["previous_proof"] or "{}")
            if not proof or not self._evidence_available(row["predecessor_id"],row["previous_ref"],row["previous_task"],
                                                         delegation=True,record_sha=proof.get("record_sha256")):
                note = "前序现场实录不可核对，暂缓后续派发"
                if row["note"] != note:
                    self._save(row,{"note":note})
                return
        presence=self.presence.get(row["agent"])
        if presence!="online":
            note="岗位离线，等待原岗位恢复" if presence=="offline" else "岗位在线情况未知，等待核对"
            if note!=row["note"]:
                self._save(row,{"note":note})
            return
        key=self.loop.check(row)
        with sync.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            if self._paused(db, row):
                return
            if not self._latest(db,row):
                next_state="superseded" if row["state"]=="ready" else "reconciling"
                db.execute("UPDATE aams_dispatch SET state=?,note=NULL WHERE token=? AND state=?",(next_state,row["token"],row["state"]))
                return
            if row["state"]=="ready":
                busy=db.execute("""SELECT 1 FROM aams_dispatch WHERE token!=? AND rule=?
                           AND state IN ('sending','queued','claimed','running','unknown','decision')""",(row["token"],row["rule"])).fetchone()
                if busy:
                    return
            now=time.time()
            reserved=db.execute("""UPDATE aams_dispatch SET state='sending',retry_at=?,note=NULL
                WHERE token=? AND state=? AND (state='ready' OR COALESCE(retry_at,0)<=?)""",
                (now+20,row["token"],row["state"],now)).rowcount
        if not reserved:
            return
        row["state"]="sending"
        try:
            # Serialize the final in-flight POST with a later local edit/pause. The sending
            # reservation is already durable if the process exits inside this transaction.
            with sync.connect(self.path) as db:
                db.execute("BEGIN IMMEDIATE")
                if not self._latest(db,row) or self._paused(db, row):
                    state="ready" if self._latest(db,row) else "reconciling"
                    db.execute("UPDATE aams_dispatch SET state=? WHERE token=? AND state='sending'",(state,row["token"]))
                    return
                number,routed=self.loop.send(key,row)
        except NotSent:
            self._save(row,{"state":"ready","note":"连接在发送前失败，等待恢复"})
            return
        except Rejected:
            self._save(row,{"state":"unrouted","note":"Loop 已明确拒绝事件"})
            return
        except Conflict:
            self._save(row,{"note":"事件应答待核对，保留原绑定码"})
            return
        self._save(row,{"event_id":number,"state":"queued" if routed else "unrouted","note":None})

    def tick(self):
        if self.loop is None:
            return
        with self.lock:
            try:
                self.presence=self.loop.presence()
            except (OSError,ValueError,KeyError,TypeError,http.client.HTTPException,sqlite3.Error):
                self.presence={}
            self.presence_at=time.monotonic()
            with sync.connect(self.path,readonly=True) as db:
                rows=db.execute("""SELECT d.*,c.id,c.task_id,c.project,c.department,c.scope,c.lane,c.version,c.instruction,c.acceptance,
                    c.roster,c.parent_id,c.assigned_run_id,c.predecessor_id,c.context,
                    previous.proof AS previous_proof,previous.result AS previous_result,
                    previous.evidence AS previous_ref,previous.bound_task AS previous_task
                    FROM aams_dispatch d JOIN aams_changes c ON d.change_id=c.id
                    LEFT JOIN aams_dispatch previous ON previous.change_id=c.predecessor_id AND previous.state='reported'
                    WHERE d.state IN (?,?,?,?,?,?,?,'reconciling') ORDER BY c.created,d.rowid""",PENDING).fetchall()
            for source in rows:
                row=dict(source)
                try:
                    self._advance(row)
                except Conflict as exc:
                    self._save(row,{"state":"unverified","note":str(exc)})
                except (OSError,ValueError,KeyError,TypeError,http.client.HTTPException,sqlite3.Error):
                    note="等待 Loop 岗位恢复" if row["state"]=="ready" else "原事件待核对，保留已知回执"
                    if row["note"]!=note:
                        self._save(row,{"note":note})

    def _record_path(self,ref,delegation=False):
        root = self.delegation_evidence_root if delegation else self.evidence_root
        if not root or not isinstance(ref,str) or not re.fullmatch(r"field/[\w.-]{1,160}\.json",ref):
            raise ValueError("需要工单现场目录内的实际 JSON 证据")
        field=root/"field"
        name=field/Path(ref).name
        from aams_evidence import is_link,local_file
        if is_link(field) or field.resolve(strict=True).parent!=root:
            raise ValueError("实录目录位置无效")
        return local_file(field.resolve(strict=True),name.name)

    def _bound_record(self,change_id,ref,tasks,delegation=False,record_sha=None):
        from aams_evidence import as_json
        record=as_json(self._record_path(ref,delegation))
        if any(k in record and record[k] not in tasks for k in ("task","task_id")):
            raise ValueError("实录声明的任务与该岗位不符")
        if "change_id" in record:
            if record["change_id"]!=change_id:
                raise ValueError("实录声明的变更与本单不符")
        elif (ref!="field/candidate.json" or record.get("task") not in tasks
              or not all(isinstance(record.get(k),str) and re.fullmatch("[a-f0-9]{64}",record[k])
                         for k in ("source_manifest_sha256","working_tree_story_sha256"))):
            raise ValueError("实录缺少本次变更绑定或公共候选指纹")
        if record_sha and hashlib.sha256(compact(record).encode()).hexdigest() != record_sha:
            raise ValueError("实录内容已不同于正式回执核验时的记录")
        return record

    def attachment(self,change_id,ref):
        with sync.connect(self.path,readonly=True) as db:
            change = required(db, "SELECT scope FROM aams_changes WHERE id=?", (change_id,), "工单不存在")
            legs=db.execute("SELECT bound_task,proof FROM aams_dispatch WHERE change_id=? AND stage!='scope' AND evidence=?",
                            (change_id,ref)).fetchall()
            tasks={r["bound_task"] for r in legs}
            if not tasks and db.execute("SELECT 1 FROM aams_reviews WHERE change_id=? AND evidence=?",(change_id,ref)).fetchone():
                tasks={r[0] for r in db.execute("SELECT bound_task FROM aams_dispatch WHERE change_id=?",(change_id,))}
        if not tasks:
            raise ValueError("该文件不在本单回执中")
        record_sha=next((json.loads(r["proof"])["record_sha256"] for r in legs if r["proof"]),None)
        return self._bound_record(change_id,ref,tasks,change["scope"]==EMPLOYEE_DELEGATION,record_sha)

    def _evidence_available(self,change_id,ref,task,delegation=False,record_sha=None):
        if not ref:
            return False
        try:
            self._bound_record(change_id,ref,{task},delegation,record_sha)
            return True
        except (ValueError,OSError,TypeError):
            return False

    def review(self,change_id,reviewer,review_run,conclusion,candidate,commit,pr,evidence):
        change_id,reviewer=map(sync.identifier,(change_id,reviewer))
        conclusion=sync.clean_text(conclusion,950)
        if not isinstance(candidate,str) or not re.fullmatch("[a-fA-F0-9]{7,64}",candidate):
            raise ValueError("需要实际候选指纹")
        if not isinstance(commit,str) or not re.fullmatch("[a-fA-F0-9]{7,64}",commit):
            raise ValueError("需要实际提交标识")
        if not isinstance(pr,str) or not re.fullmatch(r"https://github\.com/[\w.-]+/[\w.-]+/pull/[1-9]\d*",pr):
            raise ValueError("需要可核对的 GitHub PR 地址")
        source="本地独立审核员自报"
        record_id=hashlib.sha256(compact([change_id,reviewer,review_run,conclusion,candidate,commit,pr,evidence]).encode()).hexdigest()
        with sync.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            change=required(db,"SELECT * FROM aams_changes WHERE id=?",(change_id,),"业务单不存在")
            if change["scope"] in DELEGATION_SCOPES:
                raise Conflict("项目委托的独立复核由部门负责人分派并经由 Loop 回传")
            legs=db.execute("SELECT agent,stage,bound_task,state FROM aams_dispatch WHERE change_id=?",(change_id,)).fetchall()
            if reviewer in {l["agent"] for l in legs} or reviewer==change["target"]:
                raise ValueError("业务单参与人不能作为本单独立审核员")
            roles=json.loads(required(db,"SELECT reviewers FROM aams_projects WHERE id=?",(change["project"],),"项目不存在")[0])
            central=db.execute("SELECT 1 FROM aams_staff WHERE project='aams' AND department='review' AND agent=?",(reviewer,)).fetchone()
            if reviewer not in roles and not central:
                raise ValueError("该身份尚未登记为项目或本机独立审核员")
            if not any(l["stage"]!="scope" and l["state"]=="reported" for l in legs):
                raise Conflict("请在该业务单有实际执行结果后提交独立审核")
            record=self._bound_record(change_id,evidence,{l["bound_task"] for l in legs})
            if record.get("change_id")!=change_id:
                raise ValueError("独立审核需要明确绑定本单的实录")
            if review_run is not None:
                route=roles.get(reviewer)
                if (type(review_run) is not int or review_run<1 or not route or not isinstance(self.loop,ProjectLoop)
                        or not self.loop.confirm_review(reviewer,route,review_run,change_id,evidence,conclusion,candidate,commit,pr)):
                    raise ValueError("没有独立审核岗位和本单实录共同绑定的正式轮次")
                source="本单 Loop 审核轮次 #"+str(review_run)
            db.execute("INSERT OR IGNORE INTO aams_reviews VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (record_id,change_id,reviewer,review_run,source,conclusion,candidate,commit,pr,evidence,time.time()))
        return {"id":record_id,"source":source}

    def _history(self,staff):
        if not staff["history_run"] or not self.loop or not isinstance(self.loop,ProjectLoop):
            return None
        try:
            with closing(sqlite3.connect((self.loop.home/"hub.db").resolve().as_uri()+"?mode=ro",uri=True,timeout=1)) as db:
                db.execute("BEGIN")
                row=db.execute("SELECT data FROM projections WHERE collection='runs' AND key=?",(str(staff["history_run"]),)).fetchone()
                if not row:
                    return None
                run=json.loads(row[0])
                if run.get("id")!=staff["history_run"] or run.get("agent")!=staff["agent"] or not run.get("claimed_at"):
                    return None
                cfg=db.execute("SELECT data FROM projections WHERE collection='config' AND key=?",(str(run.get("rules_version")),)).fetchone()
                local=json.loads((self.loop.home/"config.json").read_text(encoding="utf-8-sig")).get("worker",{}).get("name")
                who=json.loads(cfg[0])["value"]["agents"].get(staff["agent"],{}) if cfg else {}
                if not local or who.get("worker")!=local or who.get("use")!=self.loop.backend:
                    return None
                return {"run_id":run["id"],"state":run.get("state"),"claimed_at":run["claimed_at"],
                        "finished_at":run.get("finished_at"),"result":str(run.get("summary") or "")[:500],
                        "source":"管理员登记的历史任务关联 · 正式 Loop","task_id":staff["task_id"],
                        **self.loop.execution(run)}
        except (ValueError,TypeError,KeyError,OSError,sqlite3.Error):
            return None

    def _change_view(self, db, row):
        change={k:row[k] for k in ("id","scope","task_id","version","instruction","acceptance","created")}
        change["agent"]=row["target"]
        change["deliveries"]=[]
        for result in db.execute("""SELECT stage,agent,bound_task,state,event_id,run_id,decision_run_id,session,
               rules_version,decision_session,claimed_at,applied_at,finished_at,result,evidence,note,model,effort,
               model_source,proof FROM aams_dispatch WHERE change_id=? ORDER BY rowid""",(row["id"],)):
            leg=dict(result)
            leg["targets"]=json.loads(leg["evidence"]) if leg["state"]=="scoped" else []
            leg["proof"]=json.loads(leg["proof"]) if leg["proof"] else None
            leg["evidence_available"]=leg["stage"] not in ("scope", *ASSIGNMENT_STAGES) and self._evidence_available(
                row["id"],leg["evidence"],leg["bound_task"],row["scope"]==EMPLOYEE_DELEGATION,
                (leg["proof"] or {}).get("record_sha256"))
            change["deliveries"].append(leg)
        change["reviews"]=[dict(v) for v in db.execute("""SELECT id,reviewer,review_run,source,conclusion,candidate,
             commit_sha AS "commit",pr,evidence,created FROM aams_reviews WHERE change_id=? ORDER BY created""",(row["id"],))]
        change["received_by"]=sorted({d["agent"] for d in change["deliveries"] if d["claimed_at"]})
        change["applied_by"]=sorted({d["agent"] for d in change["deliveries"] if d["applied_at"]})
        return change

    def _delegation_view(self, db, row):
        change = self._change_view(db, row)
        change.update({k: row[k] for k in ("department", "parent_id", "assigned_run_id", "predecessor_id")})
        context = json.loads(row["context"])
        change["delegated_by"] = context.get("delegated_by")
        change["proof_kind"] = context.get("proof_kind")
        change["children"] = [self._delegation_view(db, child) for child in db.execute(
            "SELECT * FROM aams_changes WHERE parent_id=? ORDER BY rowid", (row["id"],))]
        change["state"] = change["deliveries"][-1]["state"]
        if change["children"] and all(c["state"] == "reported" for c in change["children"]):
            change["state"] = "reported"
        change["current"] = self._latest(db, row)
        return change

    def view(self,project):
        project=sync.identifier(project)
        with sync.connect(self.path,readonly=True) as db:
            db.execute("BEGIN")
            registration=required(db,"SELECT id,name,manager,delegations_paused FROM aams_projects WHERE id=?",
                                  (project,),"本地未登记该项目")
            view={k:registration[k] for k in ("id","name","manager")}
            view["delegations_paused"]=bool(registration["delegations_paused"])
            view["can_delegate"]=bool(self.loop is not None and view["manager"])
            view["delegations"]=[self._delegation_view(db, row) for row in db.execute(
                "SELECT * FROM aams_changes WHERE project=? AND scope=? ORDER BY created DESC,id", (project,PROJECT_DELEGATION))]
            view["departments"]=[]
            for dept in db.execute("SELECT * FROM aams_departments WHERE project=? ORDER BY rowid",(project,)):
                key=(project,dept["id"])
                changes=[]
                newer={}
                for row in db.execute("SELECT * FROM aams_changes WHERE project=? AND department=? ORDER BY created DESC,id",key):
                    c=self._change_view(db,row)
                    c["parent_id"]=row["parent_id"]
                    scope_leg=next((d for d in c["deliveries"] if d["stage"]=="scope" and d["state"]=="scoped"),None)
                    recipients=set(c["applied_by"])
                    times=[]
                    if c["scope"]=="mainline" and scope_leg and set(scope_leg["targets"]).issubset(recipients):
                        times=[scope_leg["finished_at"]]+[min(d["applied_at"] for d in c["deliveries"]
                               if d["agent"]==a and d["applied_at"]) for a in scope_leg["targets"]]
                    elif c["scope"]=="person" and c["agent"] in recipients:
                        times=[min(d["applied_at"] for d in c["deliveries"] if d["applied_at"])]
                    stamp=max((instant(t).timestamp() for t in times if t),default=None)
                    c["effective"]=stamp is not None and (row["lane"] not in newer or stamp<=newer[row["lane"]])
                    newer[row["lane"]]=row["created"]
                    changes.append(c)
                main=next((c for c in changes if c["scope"]=="mainline"),None)
                effective=next((c for c in changes if c["scope"]=="mainline" and c["effective"]),None)
                department={"id":dept["id"],"name":dept["name"],"paused":bool(dept["paused"]),
                            "delegation_only":bool(dept["delegation_only"]),
                            "can_adjust":bool(self.loop is not None and dept["scope_type"] and dept["scope_rule"] and not dept["delegation_only"]),
                            "mainline":{"version":main["version"] if main else 0,"instruction":main["instruction"] if main else dept["baseline"],
                                        "effective_version":effective["version"] if effective else 0,
                                        "effective_instruction":effective["instruction"] if effective else dept["baseline"]},
                            "changes":changes,"employees":[]}
                for s in db.execute("SELECT * FROM aams_staff WHERE project=? AND department=? ORDER BY (agent=?) DESC,rowid",(*key,dept["lead"])):
                    agent=s["agent"]
                    lead=agent==dept["lead"]
                    legs=[(c,d) for c in changes for d in reversed(c["deliveries"])
                          if d["agent"]==agent and d["bound_task"]==s["task_id"]]
                    person=next((c for c in changes if c["scope"]=="person" and c["agent"]==agent and c["task_id"]==s["task_id"]),None)
                    latest=legs[0][1] if legs else None
                    history=self._history(s) if not latest else None
                    proof=latest or history or {}
                    channel="loop" if s["event_type"] or (lead and dept["scope_type"]) else "observe"
                    status=(self.presence.get(agent,"unknown") if time.monotonic()-self.presence_at<10 else "unknown") if channel=="loop" else "observing"
                    manual=db.execute("SELECT task,status,detail,updated FROM workers WHERE project=? AND id=? AND is_demo=0",
                                      (project,agent)).fetchone()
                    manual=dict(manual) if manual and re.search(r"(?<![\w-])"+re.escape(s["task_id"])+r"(?![\w-])",manual["task"]) else None
                    if manual:
                        manual["source"]="本地岗位自报"
                    employee={"id":agent,"name":s["name"],"lead":lead,"channel":channel,"status":status,
                       "can_correct":bool(self.loop is not None and s["event_type"] and s["rule"] and not dept["delegation_only"]),
                       "model":proof.get("model"),"effort":proof.get("effort"),"model_source":proof.get("model_source"),
                       "task":{"id":s["task_id"],"title":s["task_title"],"version":person["version"] if person else 0},
                       "work_status":proof.get("state") or ("self_report" if manual else "registered"),
                       "last_delivery":latest,"history":history,"manual":manual}
                    department["employees"].append(employee)
                pending=sum(any(d["state"]=="reported" for d in c["deliveries"]) and not c["reviews"]
                            for c in changes if c["scope"] not in DELEGATION_SCOPES)
                presence=[e["status"] for e in department["employees"] if e["channel"]=="loop"]
                department["status"]=("paused" if dept["paused"] else "online" if "online" in presence else
                                      "offline" if presence and set(presence)=={"offline"} else "unknown" if presence else "observing")
                department["pending_review"]=pending
                view["departments"].append(department)
        return view


if __name__=="__main__":
    parser=argparse.ArgumentParser(description="登记 AAMS 本地项目、部门和实际 Loop 路由")
    parser.add_argument("--db",type=Path,default=sync.DEFAULT_DB)
    parser.add_argument("action",choices=["import"])
    parser.add_argument("manifest",type=Path)
    args=parser.parse_args()
    sync.initialize(args.db)
    board=ProjectBoard(args.db)
    saved=board.configure(json.loads(args.manifest.read_text(encoding="utf-8-sig")))
    print(json.dumps({"id":saved["id"],"departments":[d["id"] for d in saved["departments"]]},ensure_ascii=False))
