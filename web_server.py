"""Loopback-only authenticated dashboard. No external assets, CORS, or command execution."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import http.client
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import threading
import time
import webbrowser
from urllib.parse import parse_qs, urlsplit
import sync
import loop_status
import loop_control
from aams_orders import LocalLoop, OrderBoard, Conflict
from aams_evidence import EvidenceLibrary, ROUTES
from aams_projects import ProjectBoard, ProjectLoop

ASSETS = Path(__file__).resolve().parent / "ui"
MAX_BODY = 40000
LAUNCH_TTL = 120


class BrowserLaunch:
    """Short-lived, one-use nonce handed to the local browser, not the API key."""
    def __init__(self):
        self.ticket = secrets.token_urlsafe(32)
        self.expires = time.monotonic() + LAUNCH_TTL
        self.lock = threading.Lock()
        self.instance = secrets.token_hex(16)

    def redeem(self, offered):
        if not isinstance(offered, str) or not offered.isascii() or len(offered) != 43:
            return False
        with self.lock:
            if (self.ticket is None or time.monotonic() >= self.expires or
                    not hmac.compare_digest(self.ticket, offered)):
                return False
            self.ticket = None
            return True

    def register(self,path,port):
        with sync.connect(path) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS aams_launch_instances(port INTEGER PRIMARY KEY,instance TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS aams_launch_tickets(instance TEXT NOT NULL,digest TEXT PRIMARY KEY,expires REAL NOT NULL)")
            db.execute("INSERT INTO aams_launch_instances VALUES(?,?) ON CONFLICT(port) DO UPDATE SET instance=excluded.instance",(port,self.instance))
            db.execute("DELETE FROM aams_launch_tickets WHERE expires<=? OR instance NOT IN (SELECT instance FROM aams_launch_instances)",(time.time(),))

    def redeem_local(self,path,ticket):
        if not isinstance(ticket,str) or len(ticket)!=43 or not ticket.isascii():
            return False
        with sync.connect(path) as db:
            return db.execute("DELETE FROM aams_launch_tickets WHERE instance=? AND digest=? AND expires>?",
                              (self.instance,hashlib.sha256(ticket.encode()).hexdigest(),time.time())).rowcount==1


def existing_launch(path,port):
    """Mint a single-use browser ticket through the same local database as the live server."""
    if not Path(path).is_file():
        return None
    conn=http.client.HTTPConnection("127.0.0.1",port,timeout=2)
    try:
        conn.request("GET","/api/session/instance")
        response=conn.getresponse()
        if response.status!=200:
            return None
        info=json.loads(response.read(512))
        if info.get("protocol")!="AAMS-local-launch/1" or not isinstance(info.get("instance"),str):
            return None
        ticket=secrets.token_urlsafe(32)
        with sync.connect(path) as db:
            db.execute("BEGIN IMMEDIATE")
            found=db.execute("SELECT instance FROM aams_launch_instances WHERE port=?",(port,)).fetchone()
            if not found or found["instance"]!=info["instance"]:
                return None
            db.execute("DELETE FROM aams_launch_tickets WHERE expires<=?",(time.time(),))
            db.execute("INSERT INTO aams_launch_tickets VALUES(?,?,?)",
                       (info["instance"],hashlib.sha256(ticket.encode()).hexdigest(),time.time()+LAUNCH_TTL))
        return f"http://127.0.0.1:{port}/#launch="+ticket
    except (OSError,sqlite3.Error,ValueError,TypeError,http.client.HTTPException):
        return None
    finally:
        conn.close()


class LoopbackHTTPServer(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR allows two listeners on the same loopback port.
    # Exclusive binding makes a second dashboard fail instead of serving the
    # wrong session behind the same URL. Preserve normal reuse on other OSes.
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def server_close(self):
        if getattr(self, "projects", None) is not None:
            self.projects.close()
        if getattr(self, "orders", None) is not None:
            self.orders.close()
        super().server_close()


def json_bytes(value):
    # Foreign legacy databases can contain nonfinite REAL values. Emit valid JSON.
    def safe(v):
        if isinstance(v, float) and not sync.math.isfinite(v):
            return None
        if isinstance(v, dict):
            return {k: safe(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [safe(x) for x in v]
        return v
    return json.dumps(safe(value), ensure_ascii=False, allow_nan=False).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalTeamSync"
    sys_version = ""

    def log_message(self, fmt, *args):
        pass  # No tokens, command bodies, or private source paths in access logs.

    def _reply(self, status, body, kind="application/json; charset=utf-8"):
        raw = json_bytes(body) if isinstance(body, (dict, list)) else body
        try:
            self.send_response(status)
            for key, value in {
                "Content-Type": kind, "Content-Length": str(len(raw)),
                "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
                "Cross-Origin-Resource-Policy": "same-origin",
                "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
            }.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(raw)

        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            pass  # Browser navigated away; never attempt a second response.

    def _validate(self, authenticated=False, mutation=False):
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or hosts[0] not in self.server.allowed_hosts:
            self._reply(403, {"error": "invalid loopback Host"})
            return False
        parts = urlsplit(self.path)
        if parts.scheme or parts.netloc or parts.fragment:
            self._reply(400, {"error": "relative request path required"})
            return False
        origins = self.headers.get_all("Origin", [])
        if len(origins) > 1 or (origins and origins[0] != "http://" + hosts[0]):
            self._reply(403, {"error": "cross-origin request rejected"})
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self._reply(403, {"error": "cross-site request rejected"})
            return False
        if mutation and len(origins) != 1:
            self._reply(403, {"error": "same-origin Origin required for mutations"})
            return False
        if authenticated:
            auth = self.headers.get_all("Authorization", [])
            expected = ("Bearer " + self.server.access_token).encode("ascii")
            if len(auth) != 1 or not hmac.compare_digest(auth[0].encode("utf-8"), expected):
                self._reply(401, {"error": "session access key required"})
                return False
        return True

    def do_GET(self):
        if not self._validate():
            return
        parts = urlsplit(self.path)
        assets = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                  "/app.css": ("app.css", "text/css; charset=utf-8")}
        if parts.path in assets and not parts.query:
            name, kind = assets[parts.path]
            try:
                self._reply(200, (ASSETS / name).read_bytes(), kind)
            except OSError:
                self._reply(500, {"error": "dashboard assets missing"})
            return
        if parts.path=="/api/session/instance" and not parts.query and self.server.browser_launch is not None:
            self._reply(200,{"protocol":"AAMS-local-launch/1","instance":self.server.browser_launch.instance})
            return
        if parts.path in ("/api/aams/projects","/api/aams/project","/api/aams/attachment"):
            if not self._validate(authenticated=True):
                return
            try:
                params=parse_qs(parts.query,strict_parsing=True,keep_blank_values=True,max_num_fields=2)
                if any(len(v)!=1 for v in params.values()):
                    raise ValueError("参数重复")
                if parts.path=="/api/aams/projects" and not params:
                    result=self.server.projects.projects()
                elif parts.path=="/api/aams/project" and set(params)=={"project"}:
                    result=self.server.projects.view(params["project"][0])
                elif parts.path=="/api/aams/attachment" and set(params)=={"change_id","ref"}:
                    result=self.server.projects.attachment(params["change_id"][0],params["ref"][0])
                else:
                    raise ValueError("项目查询参数不完整")
                self._reply(200,result)
            except (ValueError,TypeError,KeyError,OSError,sqlite3.Error) as exc:
                self._reply(400,{"error":str(exc)})
            return
        if parts.path == "/api/loop/status" and not parts.query:
            if self._validate(authenticated=True):
                self._reply(200, loop_status.read_status())
            return
        if parts.path == "/api/aams/order" and not parts.query:
            if self._validate(authenticated=True):
                self._reply(200, self.server.orders.view() if self.server.orders else {"enabled": False})
            return
        if parts.path == "/api/aams/evidence":
            if not self._validate(authenticated=True):
                return
            try:
                query = parse_qs(parts.query, strict_parsing=True, keep_blank_values=True, max_num_fields=3)
                if (self.server.orders is None or set(query)-{"version","run","frame"} or
                        any(len(v)!=1 for v in query.values())):
                    raise ValueError("只能查看本单已有的执行者回执")
                version, run = int(query["version"][0]), int(query["run"][0])
                frame = query.get("frame",[None])[0]
                if frame is not None and frame not in ROUTES:
                    raise ValueError("未知路线")
                with sync.connect(self.server.db_path, readonly=True) as db:
                    row = db.execute("""SELECT evidence FROM aams_deliveries WHERE version=? AND run_id=?
                        AND role!='manager' AND state IN ('reported','blocked')""",(version,run)).fetchone()
                if not row:
                    raise ValueError("没有该轮可查看的执行者证据")
                result = self.server.evidence.inspect(version,row["evidence"],frame)
                if frame and not isinstance(result,bytes):
                    raise ValueError("该路线没有可查看的截图")
                self._reply(200,result,"image/png" if frame else "application/json; charset=utf-8")
            except (ValueError, KeyError, sqlite3.Error, OSError):
                self._reply(404,{"error":"该轮实录或视口文件不可用"})
            return
        if parts.path == "/api/loop/control":
            if self._validate(authenticated=True):
                control = self.server.loop_control
                try:
                    query = parse_qs(parts.query, strict_parsing=True, keep_blank_values=True, max_num_fields=1)
                    rule = query.get("rule", [loop_control.RULE])[0]
                    if set(query) - {"rule"} or rule not in loop_control.RULES:
                        raise ValueError("unknown local rule")
                except ValueError:
                    self._reply(400, {"error":"仅支持已登记的潮核规则"})
                    return
                self._reply(200, control.status(rule) if control else {"status":"disabled","allowed":[]})
            return
        if parts.path == "/api/loop/probe" and not parts.query:
            if self._validate(authenticated=True):
                probe = self.server.loop_probe
                self._reply(200, probe.status() if probe else {"enabled": False})
            return
        if parts.path != "/api/snapshot":
            self._reply(404, {"error": "not found"})
            return
        if not self._validate(authenticated=True):
            return
        try:
            query = parse_qs(parts.query, keep_blank_values=True, strict_parsing=True)
            if set(query) - {"project"} or any(len(v) != 1 for v in query.values()):
                raise ValueError("only one project filter is supported")
            project = query.get("project", [None])[0]
            value = sync.snapshot(self.server.db_path, self.server.source, project)
            if self.server.owned is not None:
                self.server.owned.enrich_live(value)
            else:
                value["owned_codex"] = {"enabled": False}
            self._reply(200, value)
        except (ValueError, sqlite3.Error, OSError) as exc:
            self._reply(400, {"error": str(exc)})

    def do_POST(self):
        launch = self.path == "/api/session/launch"
        if not self._validate(authenticated=not launch, mutation=True):
            return
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            self._reply(415, {"error": "application/json required"})
            return
        if self.headers.get("Transfer-Encoding"):
            self._reply(400, {"error": "transfer encoding not supported"})
            return
        try:
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1:
                raise ValueError("one Content-Length is required")
            size = int(lengths[0])
            if size < 2 or size > MAX_BODY:
                self._reply(413, {"error": "request body must be 2-40000 bytes"})
                return
            self.connection.settimeout(5)
            body = json.loads(self.rfile.read(size).decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("JSON object required")
            if launch:
                if (self.server.browser_launch is None or
                        self.headers["Host"] != self.server.launch_host or
                        set(body) != {"ticket"} or
                        not (self.server.browser_launch.redeem(body["ticket"]) or
                             self.server.browser_launch.redeem_local(self.server.db_path,body["ticket"]))):
                    self._reply(401, {"error": "local launch ticket invalid, expired or already used"})
                    return
                self._reply(200, {"token": self.server.access_token})
                return
            registry=self.server.projects
            actions={"/api/aams/projects":registry.create_project,"/api/aams/departments":registry.add_department,
                     "/api/aams/employees":registry.employee,"/api/aams/changes":registry.issue,
                     "/api/aams/delegations":registry.issue_delegation,"/api/aams/delegations/pause":registry.pause_delegations,
                     "/api/aams/changes/continue":registry.continue_change,"/api/aams/departments/pause":registry.pause,
                     "/api/aams/roster/import":registry.configure,"/api/aams/reviews":registry.review}
            if self.path in actions:
                try:
                    if self.path in ("/api/aams/changes","/api/aams/changes/continue","/api/aams/departments/pause",
                                     "/api/aams/delegations","/api/aams/delegations/pause") and registry.loop is None:
                        self._reply(403,{"error":"当前入口仅观察，请使用本机操作看板"})
                        return
                    self._reply(200,actions[self.path](**body))
                except Conflict as exc:
                    self._reply(409,{"error":str(exc)})
                except KeyError:
                    self._reply(400,{"error":"缺少登记字段"})
                return
            if self.path in ("/api/aams/order","/api/aams/order/continue"):
                if self.server.orders is None:
                    self._reply(403, {"error": "请在本机操作看板派单"})
                    return
                try:
                    if self.path.endswith("/continue"):
                        if set(body) != {"expected_version","after_run"}:
                            raise ValueError("需要本版号和上一份执行者回执的轮次")
                        self.server.orders.continue_result(**body)
                        version = body["expected_version"]
                    else:
                        if set(body) != {"expected_version", "instruction", "acceptance"}:
                            raise ValueError("需要当前版号、主管指令和验收条件")
                        version = self.server.orders.issue(**body)
                except Conflict as exc:
                    self._reply(409, {"error": str(exc)})
                    return
                self._reply(200, {"version": version})
                return
            if self.path == "/api/loop/control":
                action = body.get("action")
                rule = body.get("rule", loop_control.RULE)
                if (self.server.loop_control is None or set(body)-{"action","rule"} or
                        not isinstance(action,str) or action not in loop_control.ACTIONS or
                        not isinstance(rule,str) or rule not in loop_control.RULES):
                    self._reply(403, {"error": "fixed local Loop action required"})
                    return
                status, result = self.server.loop_control.action(action, rule)
                self._reply(status, result)
                return
            elif self.path == "/api/loop/probe":
                if self.server.loop_probe is None or body:
                    self._reply(403, {"error": "isolated fixed probe not available"})
                    return
                result = self.server.loop_probe.dispatch()
            elif self.path == "/api/commands":
                if set(body) != {"project", "worker", "text", "command_id"}:
                    raise ValueError("project, worker, text and command_id required")
                result = sync.queue(self.server.db_path, body["worker"], body["text"],
                                    sync.identifier(body["command_id"]), sync.identifier(body["project"]))
            elif self.path == "/api/owned/start":
                if self.server.owned is None:
                    raise ValueError("owned Codex bridge is not enabled; explicit trusted --codex-cli required")
                if set(body) != {"project", "request_id"}:
                    raise ValueError("project and request_id required; arbitrary CLI/thread/environment overrides forbidden")
                result = self.server.owned.start(sync.identifier(body["project"]), sync.identifier(body["request_id"]))
            elif self.path == "/api/owned/commands":
                if self.server.owned is None:
                    raise ValueError("owned Codex bridge is not enabled")
                if set(body) != {"project", "worker", "text", "command_id", "mode", "expected_turn"}:
                    raise ValueError("exact owned-worker command fields required; no arbitrary thread target")
                result = self.server.owned.enqueue(sync.identifier(body["project"]), sync.identifier(body["worker"]),
                    body["text"], body["command_id"], body["mode"], body["expected_turn"])
            elif self.path == "/api/owned/stop":
                if self.server.owned is None or set(body) != {"project", "worker"}:
                    raise ValueError("owned bridge and exact project/worker required")
                result = self.server.owned.stop(sync.identifier(body["project"]), sync.identifier(body["worker"]))
            elif self.path == "/api/demo":
                if set(body) != {"project", "request_id"}:
                    raise ValueError("project and request_id required")
                result = self.server.demos.start(sync.identifier(body["project"]), sync.identifier(body["request_id"]))
            elif self.path == "/api/demo/stop":
                if set(body) != {"project", "worker"}:
                    raise ValueError("project and worker required")
                result = self.server.demos.stop(sync.identifier(body["project"]), sync.identifier(body["worker"]))
            else:
                self._reply(404, {"error": "not found"})
                return
            self._reply(200, result)
        except (ValueError, UnicodeError, TypeError, RecursionError, sqlite3.Error, OSError) as exc:
            self._reply(400, {"error": str(exc)})

    def do_OPTIONS(self):
        self._reply(405, {"error": "CORS is not enabled"})

    def do_PUT(self):
        self._reply(405, {"error": "method not allowed"})

    do_DELETE = do_PUT
    do_PATCH = do_PUT


def create_server(path, host="127.0.0.1", port=8765, token=None, source=None, codex_cli=None, owned_factory=None, loop_probe=None, control=None, open_browser=False, order_loop=None, evidence_root=None, project_loop=None, project_evidence_root=None, delegation_evidence_root=None):
    if host != "127.0.0.1":
        raise ValueError("only 127.0.0.1 loopback binding is permitted")
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("invalid port")
    if source is not None:
        raise ValueError("server sources must be explicitly registered with source-add")
    # Claim the port before touching the database. On failure, leave it intact.
    server = LoopbackHTTPServer((host, port), Handler)
    try:
        sync.initialize(path)
    except BaseException:
        server.server_close()
        raise
    server.daemon_threads = True
    server.db_path, server.source = Path(path).resolve(), source
    server.access_token = token or secrets.token_urlsafe(32)
    if not isinstance(server.access_token, str) or not server.access_token.isascii() or len(server.access_token) < 32:
        server.server_close()
        raise ValueError("session key must be at least 32 ASCII characters")
    actual_port = server.server_address[1]
    server.launch_host = "127.0.0.1:" + str(actual_port)
    server.allowed_hosts = {server.launch_host, "localhost:" + str(actual_port)}
    server.browser_launch = BrowserLaunch() if open_browser else None
    if server.browser_launch:
        server.browser_launch.register(path,actual_port)
    server.demos = sync.DemoManager(path)
    server.loop_probe = loop_probe
    server.loop_control = control
    server.owned = None
    if codex_cli is not None:
        from codex_bridge import Manager
        try:
            server.owned = (owned_factory or Manager)(path, codex_cli)
        except Exception:
            server.server_close()
            raise
    server.orders = None
    server.projects=ProjectBoard(path,project_loop,project_evidence_root,delegation_evidence_root)
    server.projects.start()
    server.evidence = EvidenceLibrary(evidence_root)
    if order_loop is not None:
        try:
            server.orders = OrderBoard(path, order_loop)
            server.orders.start()
        except Exception:
            server.server_close()
            raise
    return server


def open_local_dashboard(url):
    try:
        if webbrowser.open(url, new=2):
            return
    except (OSError, webbrowser.Error):
        pass
    print("Could not open the browser. Open the dashboard using the fallback session key above.", flush=True)


def configured_project_loop(control, dashboard_port):
    home_value = os.environ.get("AAMS_PROJECT_HUB_HOME")
    port_value = os.environ.get("AAMS_PROJECT_HUB_PORT")
    if home_value is None and port_value is None:
        return ProjectLoop() if control else None
    if not home_value or not port_value:
        raise ValueError("AAMS_PROJECT_HUB_HOME and AAMS_PROJECT_HUB_PORT must be set together")
    try:
        hub_port = int(port_value)
    except ValueError as exc:
        raise ValueError("AAMS_PROJECT_HUB_PORT must be an integer from 1 through 65535") from exc
    if not 1 <= hub_port <= 65535 or (dashboard_port and hub_port == dashboard_port):
        raise ValueError("AAMS_PROJECT_HUB_PORT must be a free local Hub port distinct from the dashboard")
    home = Path(home_value).expanduser().resolve()
    config_path = home / "config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("The selected local Hub must have a readable config.json") from exc
    address = f"127.0.0.1:{hub_port}"
    hub_config = config.get("hub") if isinstance(config, dict) else None
    if (not isinstance(hub_config, dict) or config.get("url") != "http://" + address
            or hub_config.get("listen") != address):
        raise ValueError("The selected Hub config must bind exactly to its explicit 127.0.0.1 address")
    return ProjectLoop(home=home, port=hub_port)


def serve(path, host="127.0.0.1", port=8765, source=None, codex_cli=None, open_browser=False):
    release_mode = os.environ.get("AAMS_WINDOWS_RELEASE") == "1"
    control = None if release_mode else loop_control.configured()
    project_loop = configured_project_loop(control, port)
    if open_browser and host=="127.0.0.1" and port:
        url=existing_launch(path,port)
        if url:
            open_local_dashboard(url)
            return
    server = create_server(path, host, port, source=source, codex_cli=codex_cli,
                           control=control, open_browser=open_browser, order_loop=LocalLoop() if control else None,
                           project_loop=project_loop,project_evidence_root=os.environ.get("AAMS_PROJECT_EVIDENCE_ROOT"),
                           delegation_evidence_root=os.environ.get("AAMS_DELEGATION_EVIDENCE_ROOT"),
                           evidence_root=os.environ.get("AAMS_EVIDENCE_ROOT") if control else None)
    url = "http://" + server.launch_host + "/"
    print("Local dashboard: " + url, flush=True)
    print("Session access key (manual fallback only; valid during this process run; do not share): " + server.access_token, flush=True)
    print("Browser launch tickets are separate and single-use.", flush=True)
    if release_mode:
        print("Legacy CAS control is disabled in this Windows release.", flush=True)
        print("AAMS project dispatch uses only an explicitly selected local Hub." if project_loop else
              "AAMS project dispatch is disabled until a local Hub is explicitly selected.", flush=True)
    if server.browser_launch is not None:
        launch_url = url + "#launch=" + server.browser_launch.ticket
        threading.Thread(target=open_local_dashboard, args=(launch_url,), daemon=True).start()
    print("Only explicitly registered workers/sources. Echo demos do NOT execute instructions.", flush=True)
    print("Ctrl+C stops this server and its own echo demos. Other workers/data are untouched.", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.demos.close()
        if server.owned is not None:
            server.owned.close()
        server.server_close()
        print("AAMS dashboard stopped.", flush=True)
