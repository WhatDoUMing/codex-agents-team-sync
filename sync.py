"""Local, multi-project team progress sync. Python standard library only."""
import argparse
from contextlib import contextmanager
import hmac
import json
import math
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "data" / "sync.sqlite3"
STALE_AFTER = 30
MAX_TEXT = 8000
APP_ID = 0x5453594E
TERMINAL = {"completed", "failed", "stopped"}
ACTIVE = {"idle", "running", "blocked"}
STATUSES = ACTIVE | TERMINAL


def clean_text(value, limit=MAX_TEXT):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError("non-empty text required, max %d characters" % limit)
    if any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise ValueError("control characters rejected")
    return value.strip()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value):
        raise ValueError("invalid identifier; use 1-80 ASCII letters, digits, _ or -")
    return value


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("invalid timestamp")
    return value


def _schema_guard(db, allow_empty=False):
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    app_id = db.execute("PRAGMA application_id").fetchone()[0]
    if app_id not in (0, APP_ID) or names - {"workers", "commands", "sources", "codex_sessions", "codex_deliveries",
                                               "aams_orders", "aams_deliveries","aams_projects","aams_departments",
                                               "aams_staff","aams_changes","aams_dispatch","aams_reviews",
                                               "aams_launch_instances","aams_launch_tickets"}:
        raise ValueError("refusing to write an unrelated database")
    if not names and allow_empty:
        return
    if not {"workers", "commands"}.issubset(names):
        raise ValueError("not an initialized sync database")
    for table, required in (
        ("workers", {"id", "team", "role", "task", "status", "detail", "updated", "generation", "cooperative"}),
        ("commands", {"id", "worker", "generation", "text", "state", "created", "acknowledged", "result"}),
    ):
        columns = {r[1] for r in db.execute("PRAGMA table_info(" + table + ")")}
        if not required.issubset(columns):
            raise ValueError("unrecognized sync database schema")


@contextmanager
def connect(path, readonly=False, create=False):
    path = Path(path).resolve()
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    mode = "ro" if readonly else ("rwc" if create else "rw")
    db = sqlite3.connect(path.as_uri() + "?mode=" + mode, uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        if readonly:
            db.execute("PRAGMA query_only=ON")
        else:
            _schema_guard(db, allow_empty=create)
        with db:
            yield db
    finally:
        db.close()


def initialize(path):
    with connect(path, create=True) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE IF NOT EXISTS workers (
            id TEXT PRIMARY KEY, team TEXT NOT NULL, role TEXT NOT NULL,
            task TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL,
            updated REAL NOT NULL, generation TEXT NOT NULL,
            cooperative INTEGER NOT NULL DEFAULT 0)""")
        db.execute("""CREATE TABLE IF NOT EXISTS commands (
            id TEXT PRIMARY KEY, worker TEXT NOT NULL, generation TEXT NOT NULL,
            text TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL,
            acknowledged REAL, result TEXT)""")
        # Additive migration from the initial CLI; never drop or rewrite user rows.
        additions = {
            "workers": {"project": "TEXT NOT NULL DEFAULT 'default'", "last_polled": "REAL", "is_demo": "INTEGER NOT NULL DEFAULT 0"},
            "commands": {"project": "TEXT NOT NULL DEFAULT 'default'", "claimant": "TEXT", "claim_token": "TEXT", "claimed": "REAL", "completed": "REAL"},
        }
        for table, columns in additions.items():
            existing = {r[1] for r in db.execute("PRAGMA table_info(" + table + ")")}
            for column, definition in columns.items():
                if column not in existing:
                    db.execute("ALTER TABLE " + table + " ADD COLUMN " + column + " " + definition)
        db.execute("""CREATE TABLE IF NOT EXISTS sources (
            id TEXT PRIMARY KEY, project TEXT NOT NULL, label TEXT NOT NULL,
            adapter TEXT NOT NULL, path TEXT NOT NULL)""")
        db.execute("CREATE INDEX IF NOT EXISTS commands_worker_state ON commands(project,worker,generation,state)")
        db.execute("PRAGMA application_id=" + str(APP_ID))
        db.execute("PRAGMA user_version=1")


def register(path, worker, team, role, task, cooperative=False, project="default", is_demo=False):
    identifier(worker)
    identifier(project)
    team, role, task = clean_text(team, 120), clean_text(role, 200), clean_text(task)
    initialize(path)
    generation = uuid.uuid4().hex
    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT id FROM workers WHERE id=?", (worker,)).fetchone():
            raise ValueError("worker ID is globally unique; use a new ID for every run and project")
        db.execute("""INSERT INTO workers
            (id,team,role,task,status,detail,updated,generation,cooperative,project,is_demo)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (worker, team, role, task, "idle", "registered; consumer not yet observed",
             time.time(), generation, int(bool(cooperative)), project, int(bool(is_demo))))
    return {"id": worker, "project": project, "generation": generation, "state": "registered",
            "cooperative": bool(cooperative), "note": "self-declared worker, not an automatically discovered Dot agent"}


def _target(db, worker, generation=None, project="default"):
    identifier(worker)
    identifier(project)
    row = db.execute("SELECT * FROM workers WHERE id=? AND project=?", (worker, project)).fetchone()
    if not row or (generation is not None and row["generation"] != generation):
        raise ValueError("unknown worker/project/generation")
    return row


def _fresh_active(row, now=None):
    now = time.time() if now is None else now
    age = now - number(row["updated"])
    return row["status"] in ACTIVE and -5 <= age <= STALE_AFTER


def heartbeat(path, worker, generation, status, detail, project="default"):
    if status not in STATUSES:
        raise ValueError("invalid status")
    detail = clean_text(detail)
    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        row = _target(db, worker, generation, project)
        if row["status"] in TERMINAL and status != row["status"]:
            raise ValueError("terminal worker cannot be revived; register a new worker ID for a new run")
        db.execute("UPDATE workers SET status=?,detail=?,updated=? WHERE id=?",
                   (status, detail, time.time(), worker))
    return {"id": worker, "project": project, "status": status, "evidence": "worker self-report only"}


def workers(path, now=None, project=None):
    now = time.time() if now is None else now
    if project is not None:
        identifier(project)
    if not Path(path).exists():
        return []
    with connect(path, True) as db:
        where, params = (" WHERE project=?", (project,)) if project else ("", ())
        rows = [dict(r) for r in db.execute("SELECT * FROM workers" + where + " ORDER BY project,team,id", params)]
    for row in rows:
        row.pop("generation", None)
        row["reported_status"] = row.pop("status")
        try:
            raw_age = now - number(row["updated"])
            row["age_seconds"] = round(max(0, raw_age), 1)
            row["freshness"] = "terminal" if row["reported_status"] in TERMINAL else ("clock_skew" if raw_age < -5 else ("fresh" if raw_age <= STALE_AFTER else "stale"))
        except ValueError:
            row["age_seconds"], row["freshness"] = None, "invalid_timestamp"
        row["status"] = row["reported_status"] if row["freshness"] in ("terminal", "fresh") else row["freshness"]
        row["source"] = "protocol-demo" if row["is_demo"] else ("declared-cooperative" if row["cooperative"] else "manual-report")
        row["can_queue"] = bool(row["cooperative"] and row["freshness"] == "fresh" and row["reported_status"] in ACTIVE)
        row["inbox_observed"] = row["last_polled"] is not None
        row["evidence"] = "self-report; heartbeat is not independent proof of a live process; acknowledgement is not execution proof"
    return rows


def _public_command(row):
    row = dict(row)
    row.pop("claim_token", None)
    row.pop("generation", None)
    return row


def queue(path, worker, text, command_id=None, project="default"):
    text = clean_text(text)
    command_id = identifier(command_id or uuid.uuid4().hex)
    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        target = _target(db, worker, project=project)
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='codex_sessions'").fetchone():
            if db.execute("SELECT 1 FROM codex_sessions WHERE worker=?", (worker,)).fetchone():
                raise ValueError("owned Codex worker requires the dedicated live bridge endpoint")
        old = db.execute("SELECT * FROM commands WHERE id=?", (command_id,)).fetchone()
        if old:
            if (old["worker"], old["project"], old["generation"], old["text"]) != (worker, project, target["generation"], text):
                raise ValueError("idempotency key conflict")
            result = _public_command(old)
            result["note"] = "existing command returned; not queued a second time"
            return result
        if not target["cooperative"]:
            raise ValueError("target has no declared cooperative inbox; no command queued")
        if not _fresh_active(target):
            raise ValueError("target is terminal, stale, or clock-skewed; no command queued")
        db.execute("""INSERT INTO commands (id,worker,generation,text,state,created,project)
            VALUES(?,?,?,?,'queued',?,?)""", (command_id, worker, target["generation"], text, time.time(), project))
        result = _public_command(db.execute("SELECT * FROM commands WHERE id=?", (command_id,)).fetchone())
    result["note"] = "queued locally only; NOT delivered, accepted, or executed; worker must poll and claim"
    return result


def inbox(path, worker, generation, project="default"):
    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        target = _target(db, worker, generation, project)
        if not target["cooperative"]:
            raise ValueError("worker has not declared an inbox")
        db.execute("UPDATE workers SET last_polled=? WHERE id=?", (time.time(), worker))
        return [_public_command(r) for r in db.execute("""SELECT * FROM commands
            WHERE project=? AND worker=? AND generation=? AND state='queued' ORDER BY created,id""",
            (project, worker, generation))]


def claim(path, worker, generation, command_id, claimant, project="default"):
    identifier(command_id)
    identifier(claimant)
    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        target = _target(db, worker, generation, project)
        if not target["cooperative"] or not _fresh_active(target):
            raise ValueError("worker inbox is not active and fresh")
        token, now = secrets.token_urlsafe(32), time.time()
        changed = db.execute("""UPDATE commands SET state='acknowledged',claimant=?,claim_token=?,
            claimed=?,acknowledged=?,result='claimed by cooperative consumer; not execution proof'
            WHERE id=? AND project=? AND worker=? AND generation=? AND state='queued'""",
            (claimant, token, now, now, command_id, project, worker, generation)).rowcount
        if changed != 1:
            raise ValueError("command unavailable or already claimed; never auto-retry execution")
        row = dict(db.execute("SELECT * FROM commands WHERE id=?", (command_id,)).fetchone())
    return row  # Ownership token is returned ONLY to the successful claiming consumer.


def ack(path, worker, generation, command_id, state, result, claimant, claim_token, project="default"):
    if state not in ("completed", "rejected"):
        raise ValueError("ack state must be completed or rejected; use claim to acknowledge receipt first")
    result = clean_text(result)
    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        _target(db, worker, generation, project)
        row = db.execute("""SELECT * FROM commands WHERE id=? AND project=? AND worker=? AND generation=?""",
                         (command_id, project, worker, generation)).fetchone()
        if not row or row["claimant"] != claimant or not row["claim_token"] or not isinstance(claim_token, str) or not hmac.compare_digest(row["claim_token"], claim_token):
            raise ValueError("command ownership token mismatch")
        if row["state"] == state:
            if row["result"] != result:
                raise ValueError("conflicting repeated acknowledgement")
            return _public_command(row)
        if row["state"] != "acknowledged":
            raise ValueError("invalid state transition")
        db.execute("UPDATE commands SET state=?,result=?,completed=? WHERE id=?",
                   (state, result, time.time(), command_id))
        return _public_command(db.execute("SELECT * FROM commands WHERE id=?", (command_id,)).fetchone())


def command_list(path, project=None):
    if project is not None:
        identifier(project)
    if not Path(path).exists():
        return []
    with connect(path, True) as db:
        where, params = (" WHERE project=?", (project,)) if project else ("", ())
        return [_public_command(r) for r in db.execute("SELECT * FROM commands" + where + " ORDER BY created DESC,id LIMIT 100", params)]


def read_orchestrator(path, now=None):
    now = time.time() if now is None else now
    result = {"available": False, "can_steer": False, "tasks": [],
              "note": "read-only observation; no supported legacy mid-run steer or message consumer"}
    try:
        with connect(path, True) as db:
            db.execute("BEGIN")
            try:
                settings = dict(db.execute("SELECT key,value FROM settings"))
                result["paused"] = settings.get("paused")
                if result["paused"] is not None and not isinstance(result["paused"], str):
                    result["paused"] = None
                    result["settings_warning"] = "paused setting is not text"
            except sqlite3.Error:
                result["paused"] = None
            for raw in db.execute("SELECT id,spec,status,lease,candidate,round FROM tasks ORDER BY id"):
                t = dict(raw)
                try:
                    if not isinstance(t["id"], str) or not isinstance(t["status"], str):
                        raise ValueError("legacy id/status must be text")
                    if t["candidate"] is not None and not isinstance(t["candidate"], str):
                        raise ValueError("legacy candidate must be text or null")
                    if type(t["round"]) is not int:
                        raise ValueError("legacy round must be an integer")
                    if t["lease"] is not None:
                        number(t["lease"])
                    spec = json.loads(t.pop("spec"))
                    if not isinstance(spec, dict):
                        raise ValueError("spec is not a JSON object")
                    run = db.execute("""SELECT id,model,effort,started,finished,status FROM runs
                        WHERE task=? ORDER BY started DESC,id DESC LIMIT 1""", (t["id"],)).fetchone()
                    if run:
                        if any(run[k] is not None and not isinstance(run[k], str) for k in ("id", "model", "effort", "status")):
                            raise ValueError("legacy run text field has unsupported type")
                        for key in ("started", "finished"):
                            if run[key] is not None:
                                number(run[key])
                    t["latest_run"] = dict(run) if run else None
                    t["role"] = str(spec.get("role", "unknown"))[:200]
                    t["task"] = str(spec.get("goal", "unknown"))[:MAX_TEXT]
                    t["reported_status"] = t.pop("status")
                    if t["reported_status"] == "running":
                        if run and run["finished"] is not None:
                            t["status"], t["reason"] = "needs_reconciliation", "task reports running but latest run has finished"
                        elif t["lease"] is None or number(t["lease"]) < now:
                            t["status"], t["reason"] = "stale", "lease expired or absent; not proof of live work"
                        else:
                            t["status"], t["reason"] = "unverified_running", "fresh lease only; no verified process identity"
                    else:
                        t["status"] = t["reported_status"]
                    t["can_steer"] = False
                except (ValueError, TypeError, RecursionError, sqlite3.Error) as exc:
                    t = {"id": t.get("id") if isinstance(t.get("id"), str) else "<invalid id>",
                         "status": "invalid_record", "can_steer": False, "error": str(exc)}
                result["tasks"].append(t)
            db.rollback()
        result["available"] = True
    except (sqlite3.Error, OSError, ValueError) as exc:
        result["error"] = type(exc).__name__ + ": " + str(exc)
    return result


def source_add(path, source_id, project, source_path, label, adapter="legacy-orchestrator", scope_cwd=None):
    identifier(source_id)
    identifier(project)
    if adapter not in ("legacy-orchestrator", "codex-app-server"):
        raise ValueError("unsupported adapter")
    source_path = Path(source_path).resolve()
    own = Path(path).resolve()
    if own == source_path or (own.exists() and source_path.exists() and own.samefile(source_path)):
        raise ValueError("source cannot be the sync database")
    if adapter == "codex-app-server":
        if scope_cwd is None:
            raise ValueError("native metadata source requires an explicit exact --cwd project scope")
        from native_adapter import read_codex_threads
        scope_cwd = str(Path(scope_cwd).resolve())
        probe = read_codex_threads(source_path, scope_cwd)
        stored_path = json.dumps({"cli": str(source_path), "cwd": scope_cwd}, ensure_ascii=False)
    else:
        if scope_cwd is not None:
            raise ValueError("--cwd applies only to the codex-app-server metadata adapter")
        probe, stored_path = read_orchestrator(source_path), str(source_path)
    if not probe["available"]:
        raise ValueError("source unavailable or unsupported: " + probe.get("error", "unknown"))
    initialize(path)
    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT id FROM sources WHERE id=?", (source_id,)).fetchone():
            raise ValueError("source ID exists; choose a new ID; existing sources are preserved")
        db.execute("INSERT INTO sources(id,project,label,adapter,path) VALUES(?,?,?,?,?)",
                   (source_id, project, clean_text(label, 120), adapter, stored_path))
    return {"id": source_id, "project": project, "path": str(source_path), "scope_cwd": scope_cwd,
            "can_send": False, "can_steer": False, "registered": True, "adapter": adapter}


def source_list(path, project=None):
    if not Path(path).exists():
        return []
    with connect(path, True) as db:
        where, params = (" WHERE project=?", (identifier(project),)) if project else ("", ())
        result = [dict(r) for r in db.execute("SELECT * FROM sources" + where + " ORDER BY project,id", params)]
    for item in result:
        if item["adapter"] == "codex-app-server":
            try:
                config = json.loads(item["path"])
                if not isinstance(config, dict) or set(config) != {"cli", "cwd"}:
                    raise ValueError("invalid native source configuration")
                item["path"] = clean_text(config["cli"], 2000)
                item["scope_cwd"] = clean_text(config["cwd"], 2000)
            except (ValueError, TypeError, RecursionError) as exc:
                item["path"] = "(invalid native configuration)"
                item["source_config_error"] = str(exc)
    return result


def snapshot(path, source=None, project=None):
    configured = source_list(path, project)
    if source is not None and project in (None, "default"):
        configured.append({"id": "cli-readonly", "project": "default", "label": "Explicit CLI read-only source",
                           "adapter": "legacy-orchestrator", "path": str(Path(source).resolve())})
    sources = []
    for item in configured:
        if item.get("source_config_error"):
            report = {"available": False, "tasks": [], "threads": [], "can_send": False,
                      "can_steer": False, "error": item["source_config_error"]}
        elif item["adapter"] == "codex-app-server":
            from native_adapter import read_codex_threads
            report = read_codex_threads(item["path"], item["scope_cwd"])
        elif item["adapter"] == "legacy-orchestrator":
            report = read_orchestrator(item["path"])
        else:
            report = {"available": False, "tasks": [], "threads": [], "can_send": False,
                      "can_steer": False, "error": "unsupported source adapter"}
        sources.append({**item, **report})
    reports, commands = workers(path, project=project), command_list(path, project)
    projects = sorted({w["project"] for w in reports} | {s["project"] for s in sources})
    result = {"observed_at": time.time(), "coverage": "only registered/self-reported workers and explicitly scoped read-only sources; native metadata is not cross-process liveness or message delivery",
            "stale_after_seconds": STALE_AFTER, "projects": projects, "workers": reports, "commands": commands, "sources": sources}
    from codex_bridge import enrich_snapshot
    return enrich_snapshot(path, result)


class DemoManager:
    """A protocol echo consumer. Never executes shell commands or controls agents."""
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.items = {}
        self.requests = {}

    def start(self, project="demo", request_id=None):
        identifier(project)
        request_id = identifier(request_id or uuid.uuid4().hex)
        with self.lock:
            if request_id in self.requests:
                item = self.requests[request_id]
                if item["project"] != project:
                    raise ValueError("demo idempotency key conflict")
                return item
            if sum(t.is_alive() for _, t in self.items.values()) >= 4:
                raise ValueError("at most four echo demos at once")
            worker = "demo-" + uuid.uuid4().hex[:12]
            registered = register(self.path, worker, "protocol-demo", "echo-only protocol demo",
                                  "Receive and echo text only. Does not execute instructions.", True, project, True)
            stop = threading.Event()
            thread = threading.Thread(target=self._run, args=(worker, registered["generation"], project, stop), daemon=True)
            item = {"id": worker, "project": project, "is_demo": True, "note": "echo-only demo; NOT a real model agent"}
            self.items[(project, worker)] = (stop, thread)
            self.requests[request_id] = item
            thread.start()
            return item

    def _run(self, worker, generation, project, stop):
        claimant = "demo-consumer-" + uuid.uuid4().hex
        try:
            while not stop.is_set():
                heartbeat(self.path, worker, generation, "idle", "Echo-only demo polling; no instruction execution", project)
                for command in inbox(self.path, worker, generation, project):
                    if stop.is_set():
                        break
                    try:
                        claimed = claim(self.path, worker, generation, command["id"], claimant, project)
                    except ValueError:
                        continue  # A different consumer won. Never execute a losing claim.
                    result = "DEMO ONLY. Text received, not executed: " + command["text"][:MAX_TEXT - 100]
                    ack(self.path, worker, generation, command["id"], "completed", result,
                        claimant, claimed["claim_token"], project)
                stop.wait(1)
        except (sqlite3.Error, OSError, ValueError) as exc:
            try:
                heartbeat(self.path, worker, generation, "failed", "Demo consumer stopped: " + str(exc), project)
            except (sqlite3.Error, OSError, ValueError):
                pass
        finally:
            try:
                heartbeat(self.path, worker, generation, "stopped", "Echo demo stopped", project)
            except (sqlite3.Error, OSError, ValueError):
                pass

    def stop(self, project, worker):
        with self.lock:
            item = self.items.get((project, worker))
        if not item:
            raise ValueError("not a demo owned by this server")
        item[0].set()
        item[1].join(timeout=7)
        return {"id": worker, "project": project, "stop_requested": True, "thread_stopped": not item[1].is_alive()}

    def close(self):
        with self.lock:
            items = list(self.items.values())
        for stop, _ in items:
            stop.set()
        for _, thread in items:
            thread.join(timeout=7)


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, default=DEFAULT_DB, help="independent writable sync DB")
    p.add_argument("--source", type=Path, help="optional explicit read-only legacy DB; never default")
    p.add_argument("--project", default=None, help="project ID/filter (default for writes: default)")
    sub = p.add_subparsers(dest="op", required=True)
    for name in ("status", "commands", "sources", "init"):
        sub.add_parser(name)
    watch = sub.add_parser("watch")
    watch.add_argument("--interval", type=float, default=2)
    r = sub.add_parser("register")
    r.add_argument("worker")
    for name in ("team", "role", "task"):
        r.add_argument("--" + name, required=True)
    r.add_argument("--cooperative", action="store_true", help="declare inbox support; not proof of a consumer")
    h = sub.add_parser("heartbeat")
    h.add_argument("worker")
    h.add_argument("generation")
    h.add_argument("--status", required=True, choices=sorted(STATUSES))
    h.add_argument("--detail", required=True)
    q = sub.add_parser("send")
    q.add_argument("worker")
    q.add_argument("text")
    q.add_argument("--id")
    i = sub.add_parser("inbox")
    i.add_argument("worker")
    i.add_argument("generation")
    c = sub.add_parser("claim")
    c.add_argument("worker")
    c.add_argument("generation")
    c.add_argument("command")
    c.add_argument("--claimant", required=True)
    a = sub.add_parser("ack")
    a.add_argument("worker")
    a.add_argument("generation")
    a.add_argument("command")
    a.add_argument("--state", choices=("completed", "rejected"), required=True)
    a.add_argument("--result", required=True)
    a.add_argument("--claimant", required=True)
    a.add_argument("--claim-token", required=True)
    s = sub.add_parser("source-add")
    s.add_argument("source_id")
    s.add_argument("--path", type=Path, required=True)
    s.add_argument("--label", required=True)
    s.add_argument("--adapter", choices=("legacy-orchestrator", "codex-app-server"), default="legacy-orchestrator")
    s.add_argument("--cwd", type=Path, help="required exact project scope for native metadata only")
    n = sub.add_parser("native-status", help="explicit official App Server metadata-only probe")
    n.add_argument("--cli", type=Path, required=True)
    n.add_argument("--cwd", type=Path, required=True)
    d = sub.add_parser("demo")
    d.add_argument("--seconds", type=float, default=0, help="0 = run until Ctrl+C; echo only")
    server = sub.add_parser("serve")
    server.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1",))
    server.add_argument("--port", type=int, default=8765)
    server.add_argument("--open-browser", action="store_true", help="open and connect a local browser tab automatically")
    server.add_argument("--codex-cli", type=Path, help="opt-in trusted official CLI for NEW owned workers only")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    project = args.project or "default"
    try:
        if args.project is not None:
            identifier(args.project)
        if args.source is not None:
            own, source = args.db.resolve(), args.source.resolve()
            if own == source or (own.exists() and source.exists() and own.samefile(source)):
                raise ValueError("writable sync DB and read-only source must be different files")
        if args.op == "init":
            initialize(args.db)
            out = {"initialized": str(args.db.resolve()), "note": "no source or workers auto-connected"}
        elif args.op in ("status", "watch"):
            if args.op == "watch":
                if not math.isfinite(args.interval) or args.interval < 1:
                    raise ValueError("interval must be finite and at least 1 second")
                while True:
                    print(json.dumps(snapshot(args.db, args.source, args.project), ensure_ascii=False), flush=True)
                    time.sleep(args.interval)
            out = snapshot(args.db, args.source, args.project)
        elif args.op == "register":
            out = register(args.db, args.worker, args.team, args.role, args.task, args.cooperative, project)
        elif args.op == "heartbeat":
            out = heartbeat(args.db, args.worker, args.generation, args.status, args.detail, project)
        elif args.op == "send":
            out = queue(args.db, args.worker, args.text, args.id, project)
        elif args.op == "inbox":
            out = inbox(args.db, args.worker, args.generation, project)
        elif args.op == "claim":
            out = claim(args.db, args.worker, args.generation, args.command, args.claimant, project)
        elif args.op == "ack":
            out = ack(args.db, args.worker, args.generation, args.command, args.state, args.result, args.claimant, args.claim_token, project)
        elif args.op == "native-status":
            from native_adapter import read_codex_threads
            out = read_codex_threads(args.cli, args.cwd, cache_seconds=0)
        elif args.op == "source-add":
            out = source_add(args.db, args.source_id, project, args.path, args.label, args.adapter, args.cwd)
        elif args.op == "sources":
            out = source_list(args.db, args.project)
        elif args.op == "demo":
            if not math.isfinite(args.seconds) or args.seconds < 0:
                raise ValueError("seconds must be finite and nonnegative")
            demo = DemoManager(args.db)
            try:
                print(json.dumps(demo.start(project if args.project else "demo"), ensure_ascii=False), flush=True)
                until = time.monotonic() + args.seconds if args.seconds else None
                while until is None or time.monotonic() < until:
                    time.sleep(0.2)
            finally:
                demo.close()
            return 0
        elif args.op == "serve":
            if args.source is not None or args.project is not None:
                raise ValueError("serve shows all projects; configure source-add instead of --source, and select project in the dashboard")
            from web_server import serve
            serve(args.db, args.host, args.port, args.source, codex_cli=args.codex_cli, open_browser=args.open_browser)
            return 0
        else:
            out = command_list(args.db, args.project)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt:
        return 0
    except (ValueError, sqlite3.Error, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
