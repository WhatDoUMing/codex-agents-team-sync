"""Versioned supervisor orders on the existing AAMS dashboard and local Loop."""
from contextlib import closing
import http.client
import json
from pathlib import Path
import secrets
import sqlite3
from threading import Event, Lock, Thread
import time

from cas_loop_flow import Conflict
from loop_control import NotSent, Rejected, local_request
import sync


TASK = "TP-CH01-FISH-01"
ROLES = {
    "manager": ("tp.aams.manager", "AAMS·潮核项目管理员", "tp-manager"),
    "worker": ("tp.aams.playtest", "AAMS·潮核试玩复测", "tp-playtest"),
}
PENDING = ("ready", "sending", "queued", "claimed", "running", "unknown", "decision")

def role_for(stage):
    return ROLES["worker"] if stage.startswith("report-") else ROLES[stage]


def delivery(db, version, role):
    token = f"aams-{TASK}-v{version}-{role}-{secrets.token_hex(8)}"
    db.execute("INSERT INTO aams_deliveries(version, role, token) VALUES(?,?,?)", (version, role, token))


def envelope(row):
    role, token, version = row["role"], row["token"], row["version"]
    prefix = f"AAMS/1 {token} "
    if role == "manager":
        formats = [prefix + "DELEGATE tp-playtest", prefix + "BLOCKED | 原因 | 证据"]
    elif role == "worker":
        formats = [prefix + "ACK", prefix + "RESULT | 实际结果 | 证据位置", prefix + "BLOCKED | 实际卡点 | 证据位置"]
    else:
        formats = [prefix + "RESULT | 实际结果 | 证据位置", prefix + "BLOCKED | 实际卡点 | 证据位置"]
    detail = {"task": TASK, "version": version, "instruction": row["instruction"], "acceptance": row["acceptance"],
              "role": role_for(role)[2], "receipt_format": formats}
    if role.startswith("report-"):
        detail["after_run"] = int(role.removeprefix("report-"))
        detail["continuation"] = "同版补交结果。核对本版已经生成的宿主试玩实录，禁止重新启动游戏或重跑路线。无可用实录时说明实际阻塞。每个 summary.json 的任务内相对路径用中文分号分隔。"
    return {"id": token, "type": role_for(role)[0], "subject": TASK, "summary": f"{TASK} v{version}",
            "detail": json.dumps(detail, ensure_ascii=False, separators=(",", ":"))}


def parse_receipt(row, summary):
    prefix = f'AAMS/1 {row["token"]} '
    if not isinstance(summary, str) or not summary.startswith(prefix):
        raise Conflict("该轮回执缺少匹配的任务版号和绑定码")
    if len(summary.encode("utf-16-le")) >= 1000:
        raise Conflict("Loop 首行达到长度上限，无法确认结果及证据字段完整")
    message = summary[len(prefix):].strip()
    if row["role"] == "manager" and message == "DELEGATE tp-playtest":
        return "delegated", "分派给 tp-playtest", ""
    if row["role"] == "worker" and message == "ACK":
        return "acknowledged", "执行者已确认该版指令", ""
    fields = [part.strip() for part in message.split("|")]
    if len(fields) == 3 and all(fields):
        kind, result, evidence = fields
        if kind == "BLOCKED":
            return "blocked", result, evidence
        if row["role"] != "manager" and kind == "RESULT":
            return "reported", result, evidence
    raise Conflict("该轮回执的岗位、结果或证据字段不完整")


class LocalLoop:
    """Send through the registered source, reconcile against its read-only production ledger."""
    def __init__(self, home=None, request=local_request, port=3350, backend="codex"):
        self.home = Path(home) if home is not None else Path.home() / ".agent-autoloop"
        self.request, self.port, self.backend = request, port, backend

    def check(self, row):
        config = json.loads((self.home / "config.json").read_text(encoding="utf-8-sig"))
        address = f"127.0.0.1:{self.port}"
        if config.get("url") != "http://" + address or config.get("hub", {}).get("listen") != address:
            raise ValueError("Loop 配置未指向指定本机地址")
        source = config.get("source", {})
        if source.get("name") != "local" or not all(isinstance(k, str) and len(k) >= 24
                for k in (source.get("key"), config.get("owner"))):
            raise ValueError("Loop 缺少本机来源或操作权限")
        event_type, rule, agent = role_for(row["role"])
        state = self.request(config["owner"], "GET", "/v1/state", port=self.port)
        lines = [v for v in state.get("lines", []) if v.get("name") == rule]
        if (len(lines) != 1 or lines[0].get("agent") != agent or lines[0].get("source") != "local"
                or lines[0].get("types") != [event_type] or "paused" not in lines[0]):
            raise ValueError("等待注册对应的 Loop 岗位")
        workers = [a for a in state.get("agents", []) if a.get("id") == agent]
        if (len(workers) != 1 or workers[0].get("use") != self.backend or
                not workers[0].get("worker") or workers[0]["worker"] != config.get("worker", {}).get("name")):
            raise ValueError("岗位未绑定到预期的本机执行后端")
        if lines[0]["paused"] is not None:
            raise ValueError("岗位规则已暂停")
        # A previously queued event could merge with this one; avoid starting a new assignment in that case.
        if any(r.get("rule") == rule and r.get("state") == "queued" for r in state.get("runs", [])):
            raise ValueError("等待该岗位前一单被领取")
        return source["key"]

    def send(self, key, row):
        result = self.request(key, "POST", "/v1/events", envelope(row), port=self.port)
        number = result.get("event")
        if type(number) is not int or number < 1:
            raise Conflict("Loop 未返回可核对的事件号")
        _, rule, _ = role_for(row["role"])
        return number, result.get("routed") == [rule] and not result.get("paused")

    def observe(self, row):
        # The official source endpoint needs the numeric ID returned by POST. The ledger can also
        # recover the exact external key after a lost HTTP reply without ever reposting the event.
        path = (self.home / "hub.db").resolve()
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            db.execute("BEGIN")
            def records(collection, where, args):
                return [json.loads(value) for value, in db.execute(
                    "SELECT data FROM projections WHERE collection=? AND " + where, (collection, *args))]
            found = records("events", "json_extract(data,'$.source')='local' AND json_extract(data,'$.key')=?", [row["token"]])
            if not found:
                return None
            event, = found
            expected = envelope(row)
            if any(event.get(k) != expected[k] for k in ("type", "subject", "detail")):
                raise Conflict("Loop 事件的岗位、任务或指令与本地记录冲突")
            event_id = event["id"]
            if type(event_id) is not int or event_id < 1 or row["event_id"] not in (None, event_id):
                raise Conflict("Loop 事件号与已保存回执冲突")
            _, rule, agent = role_for(row["role"])
            if json.loads(event.get("paused") or "[]") or json.loads(event["routed"]) != [rule]:
                return {"state": "unrouted", "event_id": event_id, "result": "Loop 未按预期岗位派发"}
            links = records("run_events", "json_extract(data,'$.event_id')=?", [event_id])
            if not links:
                return {"state": "queued", "event_id": event_id}
            if len(links) != 1:
                raise Conflict("同一事件对应多个运行轮次")
            run_id = links[0]["run_id"]
            if type(run_id) is not int or run_id < 1 or row["run_id"] not in (None, run_id):
                raise Conflict("Loop 运行编号与已保存回执冲突")
            runs = records("runs", "key=?", [str(run_id)])
            members = records("run_events", "json_extract(data,'$.run_id')=?", [run_id])
            if len(runs) != 1 or len(members) != 1 or runs[0].get("rule") != rule or runs[0].get("agent") != agent:
                raise Conflict("该轮合并了其他事件或运行岗位不匹配")
            run = runs[0]
            snapshots = records("config", "key=?", [str(run.get("rules_version"))])
            assigned = snapshots[0]["value"].get("agents", {}).get(agent, {}) if len(snapshots) == 1 else {}
            local_worker = json.loads((self.home/"config.json").read_text(encoding="utf-8-sig")).get("worker", {}).get("name")
            if not local_worker or assigned.get("use") != self.backend or assigned.get("worker") != local_worker:
                raise Conflict("运行时生效的岗位配置缺少匹配的本机后端身份")
            state = run["state"]
            observed = {"event_id": event_id, "run_id": run_id, "claimed_at": run.get("claimed_at"),
                        "finished_at": run.get("finished_at"), "state": state}
            if state == "claimed" and run.get("session"):
                observed["state"] = "running"
            elif state == "done" and run.get("finished_at") and run.get("claimed_at"):
                asks = records("cards", "json_extract(data,'$.run_id')=? AND json_extract(data,'$.kind')='ask'", [run_id])
                if asks:
                    observed.update(state="decision", result=str(asks[0].get("title") or "该轮有待处理的决策卡"))
                    # An answer has its own Loop event and run. The old pre-decision summary cannot
                    # authorize a handoff. Poll the actual follow-up receipt against the original binding.
                    if len(asks)==1 and asks[0].get("state") in ("decided","closed") and asks[0].get("answer_event"):
                        answered = asks[0]["answer_event"]
                        answer_links = records("run_events", "json_extract(data,'$.event_id')=?", [answered])
                        observed["result"] = "决策已答复，等待续办轮次回执"
                        if len(answer_links)==1:
                            follow_id = answer_links[0]["run_id"]
                            follow_rows = records("runs","key=?",[str(follow_id)])
                            members = records("run_events","json_extract(data,'$.run_id')=?",[follow_id])
                            follow = follow_rows[0] if len(follow_rows)==1 else {}
                            configs = records("config", "key=?", [str(follow.get("rules_version"))])
                            who = configs[0]["value"].get("agents",{}).get(agent,{}) if len(configs)==1 else {}
                            fresh_asks = records("cards","json_extract(data,'$.run_id')=? AND json_extract(data,'$.kind')='ask'",[follow_id])
                            if (len(members)==1 and follow.get("rule")==rule and follow.get("agent")==agent and
                                    who.get("worker")==local_worker and who.get("use")==self.backend and not fresh_asks and
                                    follow.get("state")=="done" and follow.get("claimed_at") and follow.get("finished_at")):
                                observed["decision_run_id"] = follow_id
                                try:
                                    status, result, evidence = parse_receipt(row, follow.get("summary"))
                                    observed.update(state=status,result=result,evidence=evidence)
                                except Conflict:
                                    observed.update(state="unverified",result="决策续办轮次缺少同版有效回执")
                else:
                    try:
                        status, result, evidence = parse_receipt(row, run.get("summary"))
                        observed.update(state=status, result=result, evidence=evidence)
                    except Conflict as exc:
                        observed.update(state="unverified", result=str(exc), evidence=run.get("summary") or "")
            elif state not in ("queued", "claimed", "unknown"):
                observed.update(state="unverified", result=str(run.get("error") or run.get("summary") or state)[:500])
            return observed


class OrderBoard:
    def __init__(self, path, loop):
        self.path, self.loop = path, loop
        self.wake, self.stopped = Event(), Event()
        self.lock = Lock()
        self.thread = None
        with sync.connect(path) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS aams_orders (
                version INTEGER PRIMARY KEY, instruction TEXT NOT NULL, acceptance TEXT NOT NULL,
                created REAL NOT NULL, effective INTEGER NOT NULL DEFAULT 0)""")
            db.execute("""CREATE TABLE IF NOT EXISTS aams_deliveries (
                version INTEGER NOT NULL, role TEXT NOT NULL, token TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL DEFAULT 'ready', event_id INTEGER UNIQUE, run_id INTEGER UNIQUE,
                claimed_at TEXT, finished_at TEXT, result TEXT, evidence TEXT, note TEXT,
                PRIMARY KEY(version,role), FOREIGN KEY(version) REFERENCES aams_orders(version))""")
            if "decision_run_id" not in {r[1] for r in db.execute("PRAGMA table_info(aams_deliveries)")}:
                db.execute("ALTER TABLE aams_deliveries ADD COLUMN decision_run_id INTEGER")

    def start(self):
        self.thread = Thread(target=self._work, name="aams-order-reconciliation", daemon=True)
        self.thread.start()

    def close(self):
        self.stopped.set()
        self.wake.set()
        if self.thread is not None:
            self.thread.join(8)

    def _work(self):
        while not self.stopped.is_set():
            self.wake.clear()
            try:
                self.tick()
            except sqlite3.OperationalError:
                # A second process can briefly hold the panel DB. Persisted reservations
                # let the next cycle resume without submitting an event twice.
                pass
            self.wake.wait(2)

    def issue(self, expected_version, instruction, acceptance):
        if type(expected_version) is not int or not 0 <= expected_version < 1000000:
            raise ValueError("需要当前显示的指令版号")
        instruction, acceptance = sync.clean_text(instruction, 950), sync.clean_text(acceptance, 950)
        with sync.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT COALESCE(MAX(version),0) FROM aams_orders").fetchone()[0]
            if current != expected_version:
                old = db.execute("SELECT instruction,acceptance FROM aams_orders WHERE version=?", (expected_version+1,)).fetchone()
                if not old or tuple(old) != (instruction, acceptance):
                    raise Conflict("指令版号已变化，请刷新核对后再改令")
                return expected_version+1
            version = current + 1
            db.execute("INSERT INTO aams_orders(version,instruction,acceptance,created) VALUES(?,?,?,?)",
                       (version, instruction, acceptance, time.time()))
            db.execute("UPDATE aams_deliveries SET state='superseded',note=NULL WHERE state='ready'")
            delivery(db, version, "manager")
        self.wake.set()
        return version

    def view(self):
        with sync.connect(self.path, readonly=True) as db:
            db.execute("BEGIN")
            orders = [dict(r) for r in db.execute("SELECT * FROM aams_orders ORDER BY version DESC")]
            legs = {}
            for row in db.execute("""SELECT version,role,state,event_id,run_id,decision_run_id,claimed_at,finished_at,result,evidence,note
                                     FROM aams_deliveries ORDER BY version,rowid"""):
                leg = dict(row)
                leg["agent"] = role_for(leg["role"])[2]
                legs.setdefault(leg["version"], []).append(leg)
        for order in orders:
            order["deliveries"] = legs.get(order["version"], [])
        return {"enabled": True, "project": "潮核协议", "task": TASK, "latest": orders[0]["version"] if orders else 0,
                "effective": next((r["version"] for r in orders if r["effective"]), None), "orders": orders}

    def continue_result(self, expected_version, after_run):
        if type(expected_version) is not int or type(after_run) is not int or min(expected_version, after_run) < 1:
            raise ValueError("需要实际版号和上一份执行者回执的 Loop 轮次")
        stage = f"report-{after_run}"
        with sync.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT MAX(version) FROM aams_orders").fetchone()[0] != expected_version:
                raise Conflict("已有新版，旧指令不能续单")
            if db.execute("SELECT 1 FROM aams_deliveries WHERE version=? AND role=?", (expected_version, stage)).fetchone():
                return
            rows = db.execute("SELECT role,state,run_id FROM aams_deliveries WHERE version=? ORDER BY rowid",
                              (expected_version,)).fetchall()
            if (not rows or rows[0]["role"] != "manager" or rows[0]["state"] != "delegated"
                    or rows[-1]["role"] == "manager" or rows[-1]["run_id"] != after_run
                    or rows[-1]["state"] not in ("acknowledged", "blocked")):
                raise Conflict("只有当前版已确认或受阻的最后一份执行者回执可以续收结果")
            delivery(db, expected_version, stage)
        self.wake.set()

    def _save(self, row, values):
        with sync.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            latest = db.execute("SELECT MAX(version) FROM aams_orders").fetchone()[0]
            if values.get("state") == "ready" and latest != row["version"]:
                values = {"state": "superseded", "note": None}
            changed = db.execute("UPDATE aams_deliveries SET " + ",".join(k+"=?" for k in values) +
                                 " WHERE token=? AND state=?", (*values.values(), row["token"], row["state"]))
            if not changed.rowcount:
                return
            version = row["version"]
            if latest != version:
                return
            if values.get("state") == "delegated":
                delivery(db, version, "worker")
                self.wake.set()
            if row["role"] != "manager" and values.get("state") in ("acknowledged", "reported", "blocked"):
                db.execute("UPDATE aams_orders SET effective=1 WHERE version=?", (version,))

    def _advance(self, row):
        if row["state"] != "ready":
            observed = self.loop.observe(row)
            if observed:
                self._save(row, {**observed, "note": None})
            return
        key = self.loop.check(row)
        # Persist intent before contacting Loop. A concurrent issuer can supersede a ready row.
        with sync.connect(self.path) as db:
            reserved = db.execute("""UPDATE aams_deliveries SET state='sending',note=NULL
                WHERE token=? AND state='ready' AND version=(SELECT MAX(version) FROM aams_orders)
                AND NOT EXISTS (SELECT 1 FROM aams_deliveries p WHERE p.token!=?
                      AND (p.role='manager') = (?='manager')
                      AND p.state IN ('sending','queued','claimed','running','unknown'))""",
                (row["token"],row["token"],row["role"])).rowcount
        if not reserved:
            return
        row["state"] = "sending"
        try:
            number, routed = self.loop.send(key, row)
        except NotSent:
            self._save(row, {"state": "ready", "note": "发送前连接失败，将在 Loop 恢复后投递"})
            return
        except Rejected:
            self._save(row, {"state": "unrouted", "note": "Loop 明确拒绝事件"})
            return
        except Conflict:
            self._save(row, {"note": "派发应答不完整，等待按原事件编号核对"})
            return
        self._save(row, {"event_id": number, "state": "queued" if routed else "unrouted",
                         "note": None if routed else "Loop 未按预期岗位派发"})

    def tick(self):
        with self.lock:
            with sync.connect(self.path, readonly=True) as db:
                rows = db.execute("""SELECT d.*,o.instruction,o.acceptance FROM aams_deliveries d
                    JOIN aams_orders o USING(version) WHERE d.state IN (?,?,?,?,?,?,?) ORDER BY d.version,d.rowid""", PENDING).fetchall()
            for source in rows:
                row = dict(source)
                try:
                    self._advance(row)
                except Conflict as exc:
                    self._save(row, {"state": "unverified", "note": str(exc)})
                except (OSError, ValueError, http.client.HTTPException, sqlite3.Error, KeyError, TypeError):
                    note = "等待 Loop 或岗位恢复，尚未派发" if row["state"] == "ready" else "等待核对原事件，保留已记录的回执"
                    if row["note"] != note:
                        self._save(row, {"note": note})
