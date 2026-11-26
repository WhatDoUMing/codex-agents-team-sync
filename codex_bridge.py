"""Opt-in live bridge to NEW, owned Codex conversation workers. Never resumes old sessions."""
import json
from pathlib import Path
import queue
import threading
import time
import uuid
import sync
from codex_rpc import OwnedRPC, MODEL, EFFORT, WAIT_TOOL, DeliveryUnknown

TURN_LIMIT = 120
OUTPUT_LIMIT = 8000


def prepare(path):
    sync.initialize(path)
    with sync.connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE IF NOT EXISTS codex_sessions(
            worker TEXT PRIMARY KEY, project TEXT NOT NULL, generation TEXT NOT NULL,
            state TEXT NOT NULL, thread_id TEXT, active_turn TEXT, verified INTEGER NOT NULL DEFAULT 0,
            detail TEXT NOT NULL, updated REAL NOT NULL, cleanup_confirmed INTEGER)""")
        db.execute("""CREATE TABLE IF NOT EXISTS codex_deliveries(
            command TEXT PRIMARY KEY, mode TEXT NOT NULL, expected_turn TEXT,
            turn_id TEXT, phase TEXT NOT NULL, note TEXT NOT NULL, updated REAL NOT NULL)""")


def _safe(text):
    text = str(text).replace("\r\n", "\n").replace("\r", "\n")
    return "".join(c for c in text if ord(c) >= 32 or c in "\n\t")[:OUTPUT_LIMIT].strip() or "(no assistant text)"


def enrich_snapshot(path, value):
    if not Path(path).exists():
        return value
    with sync.connect(path, True) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='codex_sessions'").fetchone():
            return value
        sessions = {r["worker"]: dict(r) for r in db.execute("SELECT * FROM codex_sessions")}
        deliveries = {r["command"]: dict(r) for r in db.execute("SELECT * FROM codex_deliveries")}
    for worker in value["workers"]:
        native = sessions.get(worker["id"])
        if native and native["project"] == worker["project"]:
            native.pop("generation", None)
            native.update(model=MODEL, effort=EFFORT, environment_access=False, owned_here=False, can_steer=False)
            worker.update(source="owned-codex-record", native=native, can_queue=False)
    for command in value["commands"]:
        if command["id"] in deliveries:
            command["native_delivery"] = deliveries[command["id"]]
    return value


class Worker:
    def __init__(self, manager, worker, project, generation, scope):
        self.manager, self.path = manager, manager.path
        self.worker, self.project, self.generation, self.scope = worker, project, generation, scope
        self.client, self.thread_id, self.active = None, None, None
        self.state, self.detail, self.verified = "starting", "Creating a new owned conversation", False
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.control_lock = threading.RLock()
        self.pending_waits, self.responded, self.commands = {}, set(), {}
        self.output, self.started_at, self.last_beat = [], None, 0
        self.runner = threading.Thread(target=self.run, daemon=True)

    def live(self):
        with self.lock:
            return {"state": self.state, "active_turn": self.active, "verified": self.verified,
                    "owned_here": True, "waiting_for_operator": bool(self.pending_waits),
                    "can_steer": bool(self.state == "active" and self.active and not self.stop_event.is_set()
                    and self.client and self.client.process.poll() is None)}

    def phase(self, command, phase, note, turn=None):
        with sync.connect(self.path) as db:
            db.execute("UPDATE codex_deliveries SET phase=?,note=?,turn_id=COALESCE(?,turn_id),updated=? WHERE command=?",
                       (phase, _safe(note), turn, time.time(), command))

    def beat(self, force=False):
        if not force and time.monotonic() - self.last_beat < 1:
            return
        self.last_beat = time.monotonic()
        with self.lock:
            state, active, detail, verified = self.state, self.active, self.detail, self.verified
        with sync.connect(self.path) as db:
            db.execute("""UPDATE codex_sessions SET state=?,thread_id=?,active_turn=?,verified=?,
                detail=?,updated=? WHERE worker=? AND generation=?""",
                (state, self.thread_id, active, int(verified), _safe(detail), time.time(), self.worker, self.generation))
        reported = "running" if active else ("blocked" if state == "starting" else "idle")
        sync.heartbeat(self.path, self.worker, self.generation, reported, _safe(detail), self.project)

    def _claim(self, command):
        claimed = sync.claim(self.path, self.worker, self.generation, command["id"],
                             self.worker + "-consumer", self.project)
        self.commands[command["id"]] = {"token": claimed["claim_token"], "turn": None}
        return claimed

    def _finish(self, command, state, result, phase):
        info = self.commands[command]
        self.phase(command, phase, result, info["turn"])
        sync.ack(self.path, self.worker, self.generation, command, state, _safe(result),
                 self.worker + "-consumer", info["token"], self.project)
        del self.commands[command]

    def _tool_request(self, event):
        params, request_id = event.get("params", {}), event["id"]
        matched = bool(self.active) and params.get("threadId") == self.thread_id and params.get("turnId") == self.active
        if request_id in self.responded or request_id in self.pending_waits:
            return
        args = params.get("arguments", {})
        if isinstance(args, str):
            try: args = json.loads(args)
            except (ValueError, RecursionError): args = {}
        seconds = args.get("seconds") if isinstance(args, dict) else None
        if (event.get("method") == "item/tool/call" and matched and params.get("tool") == "bridge_wait"
                and not params.get("namespace") and type(seconds) is int and 1 <= seconds <= 10
                and not self.pending_waits):
            self.pending_waits[request_id] = (time.monotonic() + seconds, self.active)
            self.detail = "Real model requested bounded wait; active input can be steered"
            return
        self.client.respond(request_id, error={"code": -32601, "message": "unrequested tool, permission, or credential action denied"})
        self.responded.add(request_id)

    def _events(self):
        for _ in range(200):
            try: event = self.client.events.get_nowait()
            except queue.Empty: break
            method, params = event.get("method"), event.get("params", {})
            if "id" in event and method:
                self._tool_request(event)
                continue
            if params.get("threadId") != self.thread_id:
                continue
            if self.active and method == "item/completed" and params.get("turnId") == self.active:
                item = params.get("item", {})
                if item.get("type") == "agentMessage":
                    self.output.append(str(item.get("text", "")))
                    if sum(map(len, self.output)) > OUTPUT_LIMIT:
                        raise RuntimeError("output limit reached; stopping owned turn, previous actions are not undone")
            if method == "turn/completed" and params.get("turn", {}).get("id") == self.active:
                turn, finished = params["turn"], self.active
                text = _safe(self.output[-1] if self.output else "(no assistant text)")
                status = turn["status"]
                for command, info in list(self.commands.items()):
                    if info["turn"] == finished:
                        successful = status == "completed"
                        result = text if successful else "Codex turn ended as " + status + ". Previous actions are not undone."
                        self._finish(command, "completed" if successful else "rejected", result,
                                     "turn_completed" if successful else "turn_" + status)
                with self.lock:
                    self.verified = self.verified or (status == "completed" and bool(self.output))
                    self.active, self.state = None, ("stopping" if self.stop_event.is_set() else "idle")
                    self.detail = "Codex turn ended: " + status + "; this is not independent task verification"
                self.output = []
                self.pending_waits.clear()
        for request_id, (deadline, turn) in list(self.pending_waits.items()):
            if time.monotonic() >= deadline:
                if self.active == turn:
                    self.client.respond(request_id, {"contentItems": [{"type": "inputText",
                        "text": "Bounded wait finished. Follow any operator input appended while waiting."}], "success": True})
                self.responded.add(request_id)
                del self.pending_waits[request_id]

    def _dispatch(self):
        if self.stop_event.is_set():
            return
        for command in sync.inbox(self.path, self.worker, self.generation, self.project):
            if self.stop_event.is_set():
                return
            with sync.connect(self.path, True) as db:
                delivery = db.execute("SELECT * FROM codex_deliveries WHERE command=?", (command["id"],)).fetchone()
            if delivery and delivery["mode"] == "queue" and self.active:
                continue
            try:
                self._claim(command)
            except ValueError:
                continue
            if not delivery:
                self._finish(command["id"], "rejected", "Owned worker requires the dedicated owned-worker endpoint", "rejected")
                continue
            if delivery["mode"] == "steer" and (not self.active or delivery["expected_turn"] != self.active):
                self._finish(command["id"], "rejected", "Target turn changed; no steer sent and no fallback turn started", "stale_turn_rejected")
                continue
            with self.control_lock:
                if self.stop_event.is_set():
                    self._finish(command["id"], "rejected", "Stopped before RPC submission; no input sent", "cancelled_before_rpc")
                    return
                if self.client.fault:
                    self.phase(command["id"], "delivery_unknown", "Control connection failed; no automatic retry")
                    raise ConnectionError(self.client.fault)
                # Submission starts here. A later stop does not undo an already in-flight request.
                self.phase(command["id"], "api_pending", "RPC submission started; not yet accepted")
            params = {"threadId": self.thread_id, "clientUserMessageId": command["id"],
                      "input": [{"type": "text", "text": command["text"]}]}
            try:
                if delivery["mode"] == "steer":
                    params["expectedTurnId"] = delivery["expected_turn"]
                    turn = self.client.request("turn/steer", params)["turnId"]
                    phase = "steer_accepted"
                else:
                    params.update(model=MODEL, effort=EFFORT, environments=[], serviceTierForTurn="default",
                                  sandboxPolicy={"type": "readOnly", "networkAccess": False}, approvalPolicy="on-request")
                    turn = self.client.request("turn/start", params)["turn"]["id"]
                    with self.lock:
                        self.active, self.state, self.started_at = turn, "active", time.monotonic()
                        self.detail = "Codex accepted a new turn; actual completion not yet reported"
                    self.output = []
                    phase = "turn_accepted"
                self.commands[command["id"]]["turn"] = turn
                self.phase(command["id"], phase, "API accepted input; not proof the model acted on it", turn)
            except ValueError as exc:
                self._finish(command["id"], "rejected", str(exc), "api_rejected")
            except (DeliveryUnknown, ConnectionError, OSError) as exc:
                self.phase(command["id"], "delivery_unknown", str(exc))
                raise RuntimeError("Delivery outcome unknown; not retrying any RPC") from exc
            return  # Fairness: drain events and stop requests before another queued command.

    def run(self):
        terminal, cleanup = "stopped", False
        try:
            self.client = self.manager.client_factory(self.manager.cli, self.scope)
            self.client.initialize()
            response = self.client.request("thread/start", {"model": MODEL, "allowProviderModelFallback": False,
                "cwd": str(self.scope), "environments": [], "sandbox": "read-only", "approvalPolicy": "on-request",
                "approvalsReviewer": "user", "ephemeral": True, "serviceTier": "default", "dynamicTools": [WAIT_TOOL],
                "developerInstructions": "You are a newly created owned conversation worker. Environment access is disabled. Do not read files, run commands, access external services, or delegate. For transport verification you may use only bridge_wait. Operator messages are ordinary user instructions, never permission to change the bridge's scope."})
            self.thread_id = response["thread"]["id"]
            with self.lock:
                self.state, self.detail = "idle", "Owned conversation ready; model inference not yet verified"
            with sync.connect(self.path) as db:
                db.execute("UPDATE workers SET cooperative=1 WHERE id=? AND generation=?", (self.worker, self.generation))
            self.beat(True)
            while not self.stop_event.is_set():
                if self.client.fault or self.client.process.poll() is not None:
                    raise ConnectionError(self.client.fault or "owned process exited")
                self._events()
                if self.active and time.monotonic() - self.started_at > TURN_LIMIT:
                    raise RuntimeError("turn time limit reached")
                self.beat()
                self._dispatch()
                self.stop_event.wait(.1)
        except Exception as exc:
            terminal, self.detail = "failed", _safe(exc)
            for command in list(self.commands):
                try: self.phase(command, "delivery_unknown", "Control ended; outcome is not confirmed. No automatic retry.")
                except (OSError, sync.sqlite3.Error): pass
        finally:
            with self.control_lock:
                self.stop_event.set()
            with self.lock:
                self.state = "stopping"
            if self.client:
                if self.active:
                    try:
                        self.client.request("turn/interrupt", {"threadId": self.thread_id, "turnId": self.active}, timeout=5)
                        end = time.monotonic() + 6
                        while self.active and time.monotonic() < end:
                            self._events()
                            time.sleep(.05)
                        if self.active:
                            terminal = "failed"
                            self.manager.quarantined = True
                            self.detail = "Stop requested, but turn interruption was not confirmed"
                    except Exception as exc:
                        self.manager.quarantined = True
                        terminal, self.detail = "failed", "Stop unconfirmed: " + _safe(exc)
                try:
                    self.client.close()
                    cleanup = True
                except Exception as exc:
                    terminal, self.detail = "failed", "Owned process cleanup unconfirmed: " + _safe(exc)
                    self.manager.quarantined = True
            with self.lock:
                self.state = terminal
            try:
                with sync.connect(self.path) as db:
                    db.execute("""UPDATE codex_sessions SET state=?,active_turn=?,detail=?,cleanup_confirmed=?,updated=?
                        WHERE worker=? AND generation=?""",
                        (terminal, self.active, _safe(self.detail), int(cleanup), time.time(), self.worker, self.generation))
            except Exception:
                self.manager.quarantined = True
                terminal, self.detail = "failed", "Lifecycle state could not be persisted; inspect local database before restart"
                with self.lock:
                    self.state = "failed"
            try: sync.heartbeat(self.path, self.worker, self.generation, terminal, _safe(self.detail), self.project)
            except Exception: pass


class Manager:
    def __init__(self, path, cli, client_factory=OwnedRPC):
        self.path, self.cli = Path(path).resolve(), Path(cli).resolve()
        if not self.cli.is_file():
            raise ValueError("explicit trusted official CLI executable is missing")
        self.client_factory = client_factory
        self.lock, self.items, self.requests = threading.Lock(), {}, {}
        self.quarantined = False
        self.closing = False
        prepare(self.path)

    def start(self, project, request_id):
        sync.identifier(project)
        sync.identifier(request_id)
        with self.lock:
            if self.closing:
                raise ValueError("server is closing; no new worker admitted")
            if request_id in self.requests:
                old = self.requests[request_id]
                if old["project"] != project:
                    raise ValueError("worker creation idempotency conflict")
                return old
            if self.quarantined:
                raise ValueError("previous owned process cleanup unconfirmed; restart only after inspection")
            if any(w.runner.is_alive() for w in self.items.values()):
                raise ValueError("at most one real worker at a time; stop it before creating another")
            worker = "codex-" + uuid.uuid4().hex[:16]
            scope = self.path.parent / "owned-workspaces" / worker
            scope.mkdir(parents=True, exist_ok=False)
            result = sync.register(self.path, worker, "owned-codex", "Codex conversation (environment disabled)",
                                   "New owned ephemeral conversation; no control of existing sessions", False, project)
            actor = Worker(self, worker, project, result["generation"], scope)
            with sync.connect(self.path) as db:
                db.execute("INSERT INTO codex_sessions(worker,project,generation,state,detail,updated) VALUES(?,?,?,'starting',?,?)",
                           (worker, project, result["generation"], actor.detail, time.time()))
            self.items[(project, worker)] = actor
            public = {"id": worker, "project": project, "state": "starting", "model": MODEL, "effort": EFFORT,
                      "note": "New real conversation; model calls consume account quota. Not an existing agent."}
            self.requests[request_id] = public
            actor.runner.start()
            return public

    def actor(self, project, worker):
        with self.lock:
            actor = self.items.get((project, worker))
        if not actor:
            raise ValueError("worker is not owned by this running server; never resume or adopt old sessions")
        return actor

    def enqueue(self, project, worker, text, command_id, mode, expected_turn):
        sync.identifier(command_id)
        text = sync.clean_text(text)
        if mode not in ("queue", "steer") or (mode == "queue" and expected_turn is not None):
            raise ValueError("invalid native delivery mode")
        with self.lock:
            if self.closing:
                raise ValueError("server is closing; no new command admitted")
            actor = self.items.get((project, worker))
            if not actor:
                raise ValueError("worker is not owned by this running server")
            with actor.control_lock:
                return self._enqueue_locked(actor, project, worker, text, command_id, mode, expected_turn)

    def _enqueue_locked(self, actor, project, worker, text, command_id, mode, expected_turn):
        live = actor.live()
        with sync.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("""SELECT c.*,d.mode,d.expected_turn FROM commands c
                LEFT JOIN codex_deliveries d ON d.command=c.id WHERE c.id=?""", (command_id,)).fetchone()
            if old:
                if (old["project"], old["worker"], old["generation"], old["text"], old["mode"], old["expected_turn"]) != (
                    project, worker, actor.generation, text, mode, expected_turn):
                    raise ValueError("idempotency key conflict")
                return sync._public_command(old)
            if actor.stop_event.is_set():
                raise ValueError("owned worker is stopping; no new command admitted")
            target = sync._target(db, worker, actor.generation, project)
            if not target["cooperative"] or not sync._fresh_active(target) or live["state"] not in ("idle", "active"):
                raise ValueError("owned worker is not ready and fresh")
            if mode == "steer" and (not live["can_steer"] or expected_turn != live["active_turn"]):
                raise ValueError("active turn changed; no steer queued")
            now = time.time()
            db.execute("""INSERT INTO commands(id,worker,generation,text,state,created,project)
                VALUES(?,?,?,?,'queued',?,?)""", (command_id, worker, actor.generation, text, now, project))
            db.execute("INSERT INTO codex_deliveries VALUES(?,?,?,NULL,'queued','local queue only',?)",
                       (command_id, mode, expected_turn, now))
            return sync._public_command(db.execute("SELECT * FROM commands WHERE id=?", (command_id,)).fetchone())

    def stop(self, project, worker):
        actor = self.actor(project, worker)
        with actor.control_lock:
            actor.stop_event.set()
            with actor.lock:
                if actor.state not in ("stopped", "failed"):
                    actor.state = "stopping"
        return {"id": worker, "project": project, "stop_requested": True, "note": "not yet confirmed stopped"}

    def enrich_live(self, value):
        value["owned_codex"] = {"enabled": True, "model": MODEL, "effort": EFFORT, "turn_limit_seconds": TURN_LIMIT,
                                "max_workers": 1, "environment_access": False}
        for worker in value["workers"]:
            actor = self.items.get((worker["project"], worker["id"]))
            if actor and "native" in worker:
                live = actor.live()
                stopping = self.closing or actor.stop_event.is_set()
                if stopping and live["state"] not in ("stopped", "failed"):
                    live["state"] = "stopping"
                    live["can_steer"] = False
                worker["native"].update(live)
                worker["can_queue"] = bool(not stopping and worker["cooperative"]
                    and worker["freshness"] == "fresh" and live["state"] in ("idle", "active")
                    and actor.client and not actor.client.fault and actor.client.process.poll() is None)
                worker["source"] = "owned-codex-worker"
        return value

    def close(self):
        with self.lock:
            self.closing = True
            actors = list(self.items.values())
        for actor in actors:
            with actor.control_lock:
                actor.stop_event.set()
        for actor in actors:
            actor.runner.join(timeout=22)
            if actor.runner.is_alive() or (actor.client and actor.client.process.poll() is None):
                self.quarantined = True
                if actor.client and actor.client.process.poll() is None:
                    try:
                        actor.client.process.terminate()  # Only our exact owned child.
                    except OSError:
                        pass
