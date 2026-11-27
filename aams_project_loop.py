"""Bind department and personal revisions to actual local Loop runs."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
from contextlib import closing

from aams_orders import LocalLoop
from cas_loop_flow import Conflict


PROJECT_DELEGATION = "project-delegation"
DEPARTMENT_DELEGATION = "department-delegation"
EMPLOYEE_DELEGATION = "employee-delegation"
DELEGATION_SCOPES = (PROJECT_DELEGATION, DEPARTMENT_DELEGATION, EMPLOYEE_DELEGATION)
ASSIGNMENT_STAGES = ("assign-department", "assign-staff")
SESSION_ID = re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}")


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def delegation_evidence(row):
    suffix = "-" + row["stage"].removeprefix("report-") if row["stage"].startswith("report-") else ""
    return f"field/record-{row['change_id']}{suffix}.json"


class ProjectLoop(LocalLoop):
    def __init__(self, *args, codex_home=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.codex_home = Path(codex_home or os.environ.get("CODEX_HOME") or Path.home()/".codex")
        self.finished_models={}

    def route(self, row):
        return row["event_type"], row["rule"], row["agent"]

    def event(self, row):
        prefix = "AAMS/2 " + row["token"] + " "
        formats = ([prefix+"SCOPE agent[,agent...] | 判断依据（无影响用 NONE）"]
                   if row["stage"] == "scope" else
                   [prefix+"APPLIED | 已落实的具体调整 | 证据", prefix+"APPLIED_RESULT | 应用后的实际结果 | 证据"])
        detail = {"protocol":"AAMS/2","change_id":row["change_id"],"project":row["project"],
                  "department":row["department"],"scope":row["scope"],"version":row["version"],
                  "agent":row["agent"],"task_id":row["bound_task"],"instruction":row["instruction"],
                  "acceptance":row["acceptance"],"roster":json.loads(row["roster"]),
                  "receipt_format":[*formats, prefix+"BLOCKED | 实际卡点 | 证据"]}
        if row["stage"].startswith("report-"):
            detail["continuation"] = "同版续收。仅核验本版已经生成的证据，禁止重复执行或重开游戏。"
            detail["after_run"] = int(row["stage"].removeprefix("report-"))
        if row["scope"] in DELEGATION_SCOPES:
            detail.update(json.loads(row["context"]), stage=row["stage"], parent_id=row["parent_id"],
                          assigned_run_id=row["assigned_run_id"])
            if row["scope"] == EMPLOYEE_DELEGATION:
                detail["evidence_file"] = delegation_evidence(row)
            if row["previous_proof"]:
                detail["previous"] = {"id": row["predecessor_id"], "receipt": row["previous_result"],
                                      "evidence_file": row["previous_ref"], "task_id": row["previous_task"],
                                      **json.loads(row["previous_proof"])}
            if row["stage"] == "assign-department":
                detail["receipt_format"] = [prefix + "DEPARTMENT 部门ID | 分派范围与理由", detail["receipt_format"][-1]]
            elif row["stage"] == "assign-staff":
                detail["receipt_format"] = [prefix + 'STAFF | [["员工ID","具体子任务范围"],["员工ID","具体子任务范围"]]',
                                            detail["receipt_format"][-1]]
        return {"id":row["token"],"type":row["event_type"],"subject":row["token"],
                "summary":f'{row["department"]} · {row["scope"]} v{row["version"]}',
                "detail":json.dumps(detail,ensure_ascii=False,separators=(",",":"))}

    def receipt(self, row, summary):
        prefix = "AAMS/2 " + row["token"] + " "
        if not isinstance(summary,str) or not summary.startswith(prefix) or len(summary.encode("utf-16-le")) >= 1000:
            raise Conflict("回执的绑定码或长度无效")
        receipt = summary[len(prefix):]
        command, separator, payload = receipt.partition("|")
        if row["stage"] == "assign-staff" and separator and command.strip() == "STAFF":
            try:
                assignments = json.loads(payload)
                if not isinstance(assignments, list) or not 1 <= len(assignments) <= 10:
                    raise ValueError()
                available = {item["agent"] for item in json.loads(row["roster"])}
                seen = set()
                for assignment in assignments:
                    if not isinstance(assignment, list) or len(assignment) != 2:
                        raise ValueError()
                    agent, instruction = assignment
                    if (not isinstance(agent, str) or agent not in available or agent in seen or
                            not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 240 or
                            any(ord(ch) < 32 for ch in instruction)):
                        raise ValueError()
                    seen.add(agent)
            except (TypeError, ValueError):
                raise Conflict("部门负责人必须独立指定登记员工与具体子任务") from None
            return "assigned", "已确定员工分派顺序与各自范围", json.dumps(assignments, ensure_ascii=False)
        parts = [s.strip() for s in receipt.split("|")]
        if len(parts)==3 and all(parts) and parts[0]=="BLOCKED":
            return "blocked",parts[1],parts[2]
        if row["stage"] == "assign-department" and len(parts) == 2 and parts[0].startswith("DEPARTMENT ") and parts[1]:
            department = parts[0][len("DEPARTMENT "):]
            if any(item["department"] == department for item in json.loads(row["roster"])):
                return "assigned", parts[1], department
        if row["stage"]=="scope" and len(parts)==2 and parts[0].startswith("SCOPE ") and parts[1]:
            selected = parts[0][6:]
            agents = [] if selected=="NONE" else [a.strip() for a in selected.split(",")]
            roster = {a["agent"] for a in json.loads(row["roster"]) if a["controllable"]}
            if len(agents)!=len(set(agents)) or not set(agents).issubset(roster):
                raise Conflict("负责人所选岗位不属于本版可控任务范围")
            return "scoped",parts[1],json.dumps(agents,ensure_ascii=False,separators=(",",":"))
        if row["stage"] not in (*ASSIGNMENT_STAGES, "scope") and len(parts)==3 and all(parts) and parts[0] in ("APPLIED","APPLIED_RESULT"):
            return ("applied" if parts[0]=="APPLIED" else "reported"),parts[1],parts[2]
        raise Conflict("回执缺少本岗位要求的作用范围、实际结果或证据")

    def run_identity(self, run, *, decision=False):
        session = run.get("session") or ""
        if not isinstance(session, str) or not SESSION_ID.fullmatch(session):
            return {}
        if decision:
            return {"decision_session": session}
        return {"session": session, "rules_version": run.get("rules_version")}

    def presence(self):
        config=json.loads((self.home/"config.json").read_text(encoding="utf-8-sig"))
        local=config.get("worker",{}).get("name")
        address=f"127.0.0.1:{self.port}"
        if config.get("url")!="http://"+address or config.get("hub",{}).get("listen")!=address or not local:
            return {}
        state=self.request(config["owner"],"GET","/v1/state",port=self.port)
        return {a["id"]:("online" if a["online"] else "offline")
                for a in state.get("agents",[]) if a.get("use")==self.backend and a.get("worker")==local
                and isinstance(a.get("id"),str) and type(a.get("online")) is bool}

    def confirm_review(self,reviewer,route,run_id,change_id,evidence,conclusion,candidate,commit,pr):
        """Only a distinct, configured review event may authenticate a Loop review."""
        try:
            def one(db,collection,sql,args):
                rows=db.execute("SELECT data FROM projections WHERE collection=? AND "+sql,(collection,*args)).fetchall()
                return json.loads(rows[0][0]) if len(rows)==1 else {}
            with closing(sqlite3.connect((self.home/"hub.db").resolve().as_uri()+"?mode=ro",uri=True,timeout=2)) as db:
                db.execute("BEGIN")
                run=one(db,"runs","key=?",[str(run_id)])
                if (run.get("id")!=run_id or run.get("agent")!=reviewer or run.get("rule")!=route[1]
                        or run.get("state")!="done" or not run.get("claimed_at") or not run.get("finished_at")):
                    return False
                config=one(db,"config","key=?",[str(run.get("rules_version"))])
                agent=config.get("value",{}).get("agents",{}).get(reviewer,{})
                worker=json.loads((self.home/"config.json").read_text(encoding="utf-8-sig")).get("worker",{}).get("name")
                if not worker or agent.get("worker")!=worker or agent.get("use")!=self.backend:
                    return False
                link=one(db,"run_events","json_extract(data,'$.run_id')=?",[run_id])
                event=one(db,"events","key=?",[str(link.get("event_id"))])
                detail=json.loads(event.get("detail") or "{}")
                binding={"change_id":change_id,"reviewer":reviewer,"evidence":evidence,
                         "candidate":candidate,"commit":commit,"pr":pr}
                return (event.get("source")=="local" and event.get("type")==route[0] and
                        json.loads(event.get("routed") or "[]")==[route[1]] and not json.loads(event.get("paused") or "[]")
                        and all(detail.get(k)==v for k,v in binding.items())
                        and run.get("summary")==f"AAMS/2 {event['key']} REVIEW | {conclusion} | {evidence}")
        except (OSError,TypeError,KeyError,ValueError,sqlite3.Error):
            return False

    def execution(self, run):
        session=run.get("session") or ""
        if not SESSION_ID.fullmatch(session) or not run.get("claimed_at"):
            return {}
        key=(session,run.get("id"),run.get("claimed_at"),run.get("finished_at"))
        if key in self.finished_models:
            return self.finished_models[key]
        try:
            start=instant(run["claimed_at"])
            end=instant(run["finished_at"]) if run.get("finished_at") else datetime.now(timezone.utc)
            sources=self._rollouts(session,start)
            for path in sources:
                found=[]
                with path.open("rb") as stream:
                    for line in stream:
                        if b'"turn_context"' not in line[:260]:
                            continue
                        event=json.loads(line)
                        if event.get("type")!="turn_context" or not start <= instant(event["timestamp"]) <= end:
                            continue
                        context=event["payload"]
                        model,effort=context.get("model"),context.get("effort")
                        if all(isinstance(v,str) and re.fullmatch(r"[\w.-]{1,90}",v) for v in (model,effort)):
                            found.append((model,effort))
                if found and len(set(found))==1:
                    observed={"model":found[0][0],"effort":found[0][1],
                              "model_source":"Codex 当前轮 turn_context · Loop #"+str(run["id"])}
                    if run.get("finished_at"):
                        if len(self.finished_models)>=512:
                            self.finished_models.clear()
                        self.finished_models[key]=observed
                    return observed
        except (OSError,ValueError,KeyError,TypeError,sqlite3.Error):
            pass
        return {}

    def _rollouts(self, session, start):
        base=(self.codex_home/"sessions").resolve()
        index=self.codex_home/"state_5.sqlite"
        if index.is_file():
            try:
                with closing(sqlite3.connect(index.resolve().as_uri()+"?mode=ro",uri=True,timeout=1)) as db:
                    row=db.execute("SELECT rollout_path FROM threads WHERE id=?",(session,)).fetchone()
            except (OSError,sqlite3.Error):
                row=None
            if row and row[0]:
                path=Path(row[0]).resolve()
                if path.is_relative_to(base) and path.name.endswith("-"+session+".jsonl") and path.is_file():
                    return [path]
        # The session index may not yet know a newly started turn. Only check adjacent dates.
        return [p for delta in (-1,0,1) for p in (base/(start+timedelta(days=delta)).strftime("%Y/%m/%d")).glob("*-"+session+".jsonl")]
