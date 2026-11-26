"""All tests use isolated temporary databases, never the user's existing projects."""
import hashlib
import http.client
import json
from pathlib import Path
from contextlib import closing
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import sync
import web_server


def make_legacy(path, bad=False):
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript("""
        CREATE TABLE tasks(id TEXT PRIMARY KEY,spec TEXT,status TEXT,lease REAL,candidate TEXT,round INTEGER);
        CREATE TABLE runs(id TEXT,task TEXT,model TEXT,effort TEXT,started REAL,finished REAL,status TEXT);
        CREATE TABLE settings(key TEXT,value TEXT);
        INSERT INTO settings VALUES('paused','true');
        """)
        db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?)",
                   ("legacy-task", json.dumps({"role": "tester", "goal": "legacy observation"}), "running", time.time() + 300, None, 0))
        db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?)",
                   ("run-1", "legacy-task", "test-model", "test-effort", 1, 2, "finished"))
        if bad:
            for i, spec in enumerate(("[]", "null", "{bad", "42", "["*2000 + "0" + "]"*2000)):
                db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?)", ("bad-" + str(i), spec, "running", None, None, 0))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="team-sync-test-")
        self.root = Path(self.tmp.name)
        self.path = self.root / "sync.sqlite3"
        sync.initialize(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def register(self, worker="worker-1", project="alpha", cooperative=True):
        return sync.register(self.path, worker, "team-A", "tester", "test-only task", cooperative, project)

    def test_fresh_registration_is_explicit_self_report(self):
        self.register()
        row = sync.workers(self.path)[0]
        self.assertTrue(row["can_queue"])
        self.assertFalse(row["inbox_observed"])
        self.assertEqual(row["source"], "declared-cooperative")
        self.assertNotIn("generation", row)

    def test_default_snapshot_does_not_read_any_legacy(self):
        with patch.object(sync, "read_orchestrator", side_effect=AssertionError("unexpected source")):
            self.assertEqual(sync.snapshot(self.path)["sources"], [])

    def test_manual_worker_cannot_queue(self):
        self.register(cooperative=False)
        with self.assertRaises(ValueError):
            sync.queue(self.path, "worker-1", "no effect", project="alpha")
        self.assertEqual(sync.command_list(self.path), [])

    def test_worker_ids_globally_unique_between_projects_and_runs(self):
        self.register()
        with self.assertRaises(ValueError):
            self.register(project="beta")
        with self.assertRaises(ValueError):
            self.register()

    def test_generation_and_project_isolation(self):
        item = self.register()
        for generation, project in (("wrong", "alpha"), (item["generation"], "beta")):
            with self.assertRaises(ValueError):
                sync.heartbeat(self.path, item["id"], generation, "running", "bad", project)
            with self.assertRaises(ValueError):
                sync.inbox(self.path, item["id"], generation, project)
        with self.assertRaises(ValueError):
            sync.queue(self.path, item["id"], "bad target project", project="beta")

    def test_projects_filter_reports_and_commands(self):
        self.register()
        self.register("worker-2", "beta")
        sync.queue(self.path, "worker-1", "one", "cmd-1", "alpha")
        sync.queue(self.path, "worker-2", "two", "cmd-2", "beta")
        one = sync.snapshot(self.path, project="alpha")
        self.assertEqual(one["projects"], ["alpha"])
        self.assertEqual([w["id"] for w in one["workers"]], ["worker-1"])
        self.assertEqual([c["id"] for c in one["commands"]], ["cmd-1"])

    def test_freshness_uses_raw_age_not_rounded_boundary(self):
        self.register()
        with sync.connect(self.path) as db:
            db.execute("UPDATE workers SET updated=100")
        self.assertEqual(sync.workers(self.path, now=130)[0]["freshness"], "fresh")
        self.assertEqual(sync.workers(self.path, now=130.01)[0]["freshness"], "stale")
        with self.assertRaises(ValueError):
            sync.queue(self.path, "worker-1", "stale", project="alpha")

    def test_future_heartbeat_cannot_queue(self):
        self.register()
        with sync.connect(self.path) as db:
            db.execute("UPDATE workers SET updated=?", (time.time() + 100,))
        self.assertEqual(sync.workers(self.path)[0]["status"], "clock_skew")
        with self.assertRaises(ValueError):
            sync.queue(self.path, "worker-1", "not fresh", project="alpha")

    def test_terminal_worker_cannot_revive(self):
        item = self.register()
        sync.heartbeat(self.path, item["id"], item["generation"], "completed", "done", "alpha")
        with self.assertRaises(ValueError):
            sync.heartbeat(self.path, item["id"], item["generation"], "running", "late heartbeat", "alpha")
        row = sync.workers(self.path, now=time.time() + 10000)[0]
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["freshness"], "terminal")
        self.assertFalse(row["can_queue"])

    def test_queue_is_idempotent_and_payload_bound(self):
        self.register()
        first = sync.queue(self.path, "worker-1", "safe text", "cmd-one", "alpha")
        again = sync.queue(self.path, "worker-1", "safe text", "cmd-one", "alpha")
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(len(sync.command_list(self.path)), 1)
        for text in ("different",):
            with self.assertRaises(ValueError):
                sync.queue(self.path, "worker-1", text, "cmd-one", "alpha")
        self.register("worker-2", "beta")
        with self.assertRaises(ValueError):
            sync.queue(self.path, "worker-2", "safe text", "cmd-one", "beta")

    def test_inbox_is_discovery_not_an_execution_claim(self):
        item = self.register()
        sync.queue(self.path, "worker-1", "hello", "cmd-1", "alpha")
        first = sync.inbox(self.path, "worker-1", item["generation"], "alpha")
        second = sync.inbox(self.path, "worker-1", item["generation"], "alpha")
        self.assertEqual([c["id"] for c in first], [c["id"] for c in second])
        self.assertTrue(sync.workers(self.path)[0]["inbox_observed"])
        self.assertEqual(sync.command_list(self.path)[0]["state"], "queued")

    def test_two_concurrent_claimants_have_only_one_winner(self):
        item = self.register()
        sync.queue(self.path, "worker-1", "hello", "cmd-1", "alpha")
        barrier = threading.Barrier(2)
        outcomes = []
        def attempt(claimant):
            barrier.wait()
            try:
                result = sync.claim(self.path, "worker-1", item["generation"], "cmd-1", claimant, "alpha")
                outcomes.append(("win", result))
            except ValueError:
                outcomes.append(("lose", None))
        threads = [threading.Thread(target=attempt, args=("consumer-" + str(i),)) for i in range(2)]
        for t in threads: t.start()
        for t in threads: t.join(10)
        self.assertEqual(sorted(o[0] for o in outcomes), ["lose", "win"])
        self.assertEqual(sync.command_list(self.path)[0]["state"], "acknowledged")
        winner = next(o[1] for o in outcomes if o[0] == "win")
        sync.ack(self.path, "worker-1", item["generation"], "cmd-1", "completed", "self-report",
                 winner["claimant"], winner["claim_token"], "alpha")

    def test_ack_requires_owner_token_and_no_terminal_regression(self):
        item = self.register()
        sync.queue(self.path, "worker-1", "hello", "cmd-1", "alpha")
        claimed = sync.claim(self.path, "worker-1", item["generation"], "cmd-1", "consumer-1", "alpha")
        with self.assertRaises(ValueError):
            sync.claim(self.path, "worker-1", item["generation"], "cmd-1", "consumer-1", "alpha")
        with self.assertRaises(ValueError):
            sync.ack(self.path, "worker-1", item["generation"], "cmd-1", "completed", "done", "consumer-2", claimed["claim_token"], "alpha")
        with self.assertRaises(ValueError):
            sync.ack(self.path, "worker-1", item["generation"], "cmd-1", "completed", "done", "consumer-1", "wrong", "alpha")
        done = sync.ack(self.path, "worker-1", item["generation"], "cmd-1", "completed", "done", "consumer-1", claimed["claim_token"], "alpha")
        same = sync.ack(self.path, "worker-1", item["generation"], "cmd-1", "completed", "done", "consumer-1", claimed["claim_token"], "alpha")
        self.assertEqual(done, same)
        with self.assertRaises(ValueError):
            sync.ack(self.path, "worker-1", item["generation"], "cmd-1", "rejected", "regress", "consumer-1", claimed["claim_token"], "alpha")
        with self.assertRaises(ValueError):
            sync.ack(self.path, "worker-1", item["generation"], "cmd-1", "completed", "changed", "consumer-1", claimed["claim_token"], "alpha")

    def test_claim_token_never_in_public_outputs(self):
        item = self.register()
        sync.queue(self.path, "worker-1", "hello", "cmd-1", "alpha")
        claimed = sync.claim(self.path, "worker-1", item["generation"], "cmd-1", "consumer-1", "alpha")
        public = json.dumps(sync.snapshot(self.path))
        self.assertNotIn(claimed["claim_token"], public)
        self.assertNotIn("claim_token", public)
        repeated = sync.queue(self.path, "worker-1", "hello", "cmd-1", "alpha")
        self.assertNotIn("claim_token", repeated)

    def test_legacy_bad_spec_is_per_row_not_global_crash(self):
        old = self.root / "old # space.sqlite3"
        make_legacy(old, bad=True)
        before = old.read_bytes()
        result = sync.read_orchestrator(old)
        self.assertTrue(result["available"])
        self.assertEqual(sum(t["status"] == "invalid_record" for t in result["tasks"]), 5)
        healthy = next(t for t in result["tasks"] if t["id"] == "legacy-task")
        self.assertEqual(healthy["status"], "needs_reconciliation")
        self.assertFalse(result["can_steer"])
        self.assertEqual(before, old.read_bytes())

    def test_binary_and_nonfinite_legacy_fields_degrade_per_row(self):
        old = self.root / "binary.sqlite3"
        make_legacy(old)
        with closing(sqlite3.connect(old)) as db, db:
            db.execute("UPDATE tasks SET candidate=?", (b"binary candidate",))
            db.execute("UPDATE settings SET value=?", (b"binary setting",))
        report = sync.read_orchestrator(old)
        self.assertTrue(report["available"])
        self.assertEqual(report["tasks"][0]["status"], "invalid_record")
        self.assertIsNone(report["paused"])
        json.dumps(report, allow_nan=False)
        with closing(sqlite3.connect(old)) as db, db:
            db.execute("UPDATE tasks SET candidate=NULL")
            db.execute("UPDATE runs SET model=?", (b"binary model",))
        self.assertEqual(sync.read_orchestrator(old)["tasks"][0]["status"], "invalid_record")
        with closing(sqlite3.connect(old)) as db, db:
            db.execute("UPDATE runs SET model='valid'")
            db.execute("UPDATE tasks SET lease=?", (float("inf"),))
        result = sync.read_orchestrator(old)
        self.assertEqual(result["tasks"][0]["status"], "invalid_record")
        json.dumps(result, allow_nan=False)

    def test_legacy_readonly_and_foreign_write_refusal(self):
        old = self.root / "old.sqlite3"
        make_legacy(old)
        digest = hashlib.sha256(old.read_bytes()).hexdigest()
        with sync.connect(old, readonly=True) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("INSERT INTO settings VALUES('unauthorized','value')")
        with self.assertRaises(ValueError):
            sync.initialize(old)
        with self.assertRaises(ValueError):
            sync.register(old, "bad", "bad", "bad", "bad")
        self.assertEqual(digest, hashlib.sha256(old.read_bytes()).hexdigest())

    def test_missing_readonly_source_never_created(self):
        missing = self.root / "does-not-exist.sqlite3"
        self.assertFalse(sync.read_orchestrator(missing)["available"])
        self.assertFalse(missing.exists())

    def test_temporary_cli_source_keeps_fixed_default_project_identity(self):
        old = self.root / "old.sqlite3"
        make_legacy(old)
        self.assertEqual(sync.snapshot(self.path, old)["sources"][0]["project"], "default")
        self.assertEqual(sync.snapshot(self.path, old, project="alpha")["sources"], [])
        self.assertEqual(sync.snapshot(self.path, old, project="default")["sources"][0]["project"], "default")
        with self.assertRaises(ValueError):
            web_server.create_server(self.path, port=0, source=old)

    def test_explicit_sources_isolated_and_failure_does_not_hide_others(self):
        old = self.root / "old.sqlite3"
        make_legacy(old)
        sync.source_add(self.path, "legacy-alpha", "alpha", old, "Alpha source")
        sync.source_add(self.path, "legacy-beta", "beta", old, "Beta source")
        with sync.connect(self.path) as db:
            db.execute("INSERT INTO sources VALUES('missing','alpha','Missing','legacy-orchestrator',?)", (str(self.root / "missing.sqlite3"),))
        result = sync.snapshot(self.path)
        self.assertEqual(len(result["sources"]), 3)
        self.assertEqual(sum(s["available"] for s in result["sources"]), 2)
        self.assertEqual(len(sync.snapshot(self.path, project="alpha")["sources"]), 2)
        with self.assertRaises(ValueError):
            sync.source_add(self.path, "self", "alpha", self.path, "not allowed")

    def test_invalid_input_rejected(self):
        for value in ("bad project", "../other", "", None):
            with self.assertRaises(ValueError):
                sync.identifier(value)
        for value in ("", "x\x00y", "x"*8001, None):
            with self.assertRaises(ValueError):
                sync.clean_text(value)

    def test_additive_initial_schema_migration_preserves_data(self):
        old = self.root / "initial.sqlite3"
        with closing(sqlite3.connect(old)) as db, db:
            db.executescript("""CREATE TABLE workers (id TEXT PRIMARY KEY,team TEXT,role TEXT,task TEXT,status TEXT,detail TEXT,updated REAL,generation TEXT,cooperative INTEGER);
            CREATE TABLE commands (id TEXT PRIMARY KEY,worker TEXT,generation TEXT,text TEXT,state TEXT,created REAL,acknowledged REAL,result TEXT);""")
            db.execute("INSERT INTO workers VALUES('initial-worker','team','role','task','completed','done',1,'gen',0)")
            db.execute("INSERT INTO commands VALUES('initial-cmd','initial-worker','gen','text','acknowledged',1,2,'historic result')")
        sync.initialize(old)
        self.assertEqual(sync.workers(old)[0]["project"], "default")
        command = sync.command_list(old)[0]
        self.assertEqual(command["result"], "historic result")
        self.assertEqual(command["state"], "acknowledged")

    def test_real_cli_subprocess_full_protocol(self):
        def cli(*args, code=0):
            p = subprocess.run([sys.executable, "-B", str(sync.ROOT/"sync.py"), "--db", str(self.path), "--project", "cli-test", *args],
                               capture_output=True, encoding="utf-8")
            self.assertEqual(p.returncode, code, p.stdout+p.stderr)
            return json.loads(p.stdout)
        item = cli("register", "cli-worker", "--team", "cli", "--role", "tester", "--task", "safe smoke", "--cooperative")
        generation = item["generation"]
        cli("heartbeat", "cli-worker", generation, "--status", "running", "--detail", "smoke")
        command = cli("send", "cli-worker", "safe echo", "--id", "cli-command")
        self.assertEqual(command["state"], "queued")
        self.assertEqual(len(cli("inbox", "cli-worker", generation)), 1)
        claimed = cli("claim", "cli-worker", generation, "cli-command", "--claimant", "cli-consumer")
        done = cli("ack", "cli-worker", generation, "cli-command", "--state", "completed",
                   "--result", "reported complete", "--claimant", "cli-consumer", "--claim-token", claimed["claim_token"])
        self.assertEqual(done["state"], "completed")
        cli("heartbeat", "cli-worker", generation, "--status", "completed", "--detail", "done")
        self.assertIn("error", cli("send", "cli-worker", "no", code=2))
        self.assertEqual(cli("status")["workers"][0]["status"], "completed")


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="team-sync-http-test-")
        self.path = Path(self.tmp.name) / "sync.sqlite3"
        self.token = "test-session-key-" + "x"*32
        self.server = web_server.create_server(self.path, port=0, token=self.token)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.host = "127.0.0.1:" + str(self.port)
        self.origin = "http://" + self.host

    def tearDown(self):
        self.server.shutdown()
        self.server.demos.close()
        self.server.server_close()
        self.thread.join(5)
        self.tmp.cleanup()

    def req(self, method="GET", path="/api/snapshot", body=None, auth=True, origin=None, extra=None, raw=False):
        headers = {"Host": self.host}
        if auth: headers["Authorization"] = "Bearer " + self.token
        if origin is not None: headers["Origin"] = origin
        if body is not None:
            body = body if raw else json.dumps(body)
            headers["Content-Type"] = "application/json"
        if extra: headers.update(extra)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            data, response_headers = response.read(), dict(response.getheaders())
            return response.status, data, response_headers
        finally:
            conn.close()

    def test_loopback_binding_enforced(self):
        with self.assertRaises(ValueError):
            web_server.create_server(self.path, host="0.0.0.0", port=0)
        with self.assertRaises(ValueError):
            web_server.create_server(self.path, host="::", port=0)

    def test_port_conflicts_fail_before_owned_manager_creation(self):
        # Test both directions, including a legacy listener that used
        # ThreadingHTTPServer's default Windows SO_REUSEADDR behavior.
        from http.server import ThreadingHTTPServer
        def no_owned_manager(*args):
            self.fail("an occupied port must fail before creating the owned manager")
        second_db = Path(self.tmp.name) / "second.sqlite3"
        with self.assertRaises(OSError):
            web_server.create_server(second_db, port=self.port,
                                     codex_cli=Path(self.tmp.name) / "codex.exe",
                                     owned_factory=no_owned_manager)
        self.assertFalse(second_db.exists())
        with self.assertRaises(OSError):
            ThreadingHTTPServer(("127.0.0.1", self.port), web_server.Handler)
        legacy = ThreadingHTTPServer(("127.0.0.1", 0), web_server.Handler)
        third_db = Path(self.tmp.name) / "third.sqlite3"
        try:
            with self.assertRaises(OSError):
                web_server.create_server(third_db, port=legacy.server_address[1],
                                         codex_cli=Path(self.tmp.name) / "codex.exe",
                                         owned_factory=no_owned_manager)
        finally:
            legacy.server_close()
        self.assertFalse(third_db.exists())
        self.assertEqual(self.req(auth=False)[0], 401)

    def test_database_initialization_failure_closes_bound_socket(self):
        with patch.object(web_server, "LoopbackHTTPServer") as server, \
             patch.object(sync, "initialize", side_effect=OSError("test initialization failure")):
            with self.assertRaises(OSError):
                web_server.create_server(Path(self.tmp.name) / "unavailable.sqlite3", port=0)
        server.return_value.server_close.assert_called_once_with()

    def test_root_assets_and_security_headers(self):
        status, data, headers = self.req(path="/", auth=False)
        self.assertEqual(status, 200)
        self.assertNotIn(self.token.encode(), data)
        self.assertIn(b'id="login-form"', data)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        for path in ("/app.js", "/app.css"):
            self.assertEqual(self.req(path=path, auth=False)[0], 200)
        self.assertEqual(self.req(path="/../sync.py")[0], 404)

    def test_snapshot_auth_required_and_no_cors(self):
        self.assertEqual(self.req(auth=False)[0], 401)
        code, data, headers = self.req()
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(data)["workers"], [])
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(self.req(method="OPTIONS")[0], 405)

    def test_host_and_origin_rebinding_rejected(self):
        for host in ("evil.test:" + str(self.port), "127.0.0.1:1", "127.0.0.1.evil.test:" + str(self.port)):
            self.assertEqual(self.req(extra={"Host": host})[0], 403)
        for origin in ("https://evil.test", "null", "http://127.0.0.1:1"):
            self.assertEqual(self.req(origin=origin)[0], 403)
        self.assertEqual(self.req(extra={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.req(path="/api/snapshot?token=" + self.token, auth=False)[0], 401)

    def test_post_requires_bearer_origin_json_and_bounded_body(self):
        body = {"project": "demo", "request_id": "request-one"}
        self.assertEqual(self.req("POST", "/api/demo", body, auth=False, origin=self.origin)[0], 401)
        self.assertEqual(self.req("POST", "/api/demo", body)[0], 403)
        self.assertEqual(self.req("POST", "/api/demo", body, origin="http://evil.test")[0], 403)
        self.assertEqual(self.req("POST", "/api/demo", body, origin=self.origin, extra={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.req("POST", "/api/demo", "x"*40001, origin=self.origin, raw=True)[0], 413)
        self.assertEqual(self.req("POST", "/api/demo", [], origin=self.origin)[0], 400)
        self.assertEqual(self.req("POST", "/api/demo", "{", origin=self.origin, raw=True)[0], 413)
        self.assertEqual(self.req("POST", "/api/demo", "["*2000+"0"+"]"*2000, origin=self.origin, raw=True)[0], 400)
        self.assertEqual(sync.workers(self.path), [])

    def test_http_end_to_end_echo_demo_and_idempotent_send(self):
        start = {"project": "demo", "request_id": "demo-request"}
        code, body, _ = self.req("POST", "/api/demo", start, origin=self.origin)
        self.assertEqual(code, 200)
        worker = json.loads(body)["id"]
        self.assertTrue(json.loads(body)["is_demo"])
        self.assertEqual(json.loads(self.req("POST", "/api/demo", start, origin=self.origin)[1])["id"], worker)
        directive = {"project": "demo", "worker": worker, "text": "<script>not executable</script> & echo-only", "command_id": "http-command"}
        for _ in range(2):
            self.assertEqual(self.req("POST", "/api/commands", directive, origin=self.origin)[0], 200)
        deadline = time.monotonic()+8
        while time.monotonic() < deadline:
            status, body, _ = self.req()
            current = json.loads(body)
            if current["commands"] and current["commands"][0]["state"] == "completed":
                break
            time.sleep(.1)
        else:
            self.fail("echo demo did not acknowledge and report completion")
        self.assertEqual(len(current["workers"]), 1)
        self.assertEqual(len(current["commands"]), 1)
        self.assertIn("not executed", current["commands"][0]["result"])
        self.assertNotIn("claim_token", body.decode())
        wrong = {**directive, "project": "other-project", "command_id": "other-id"}
        self.assertEqual(self.req("POST", "/api/commands", wrong, origin=self.origin)[0], 400)
        code, body, _ = self.req("POST", "/api/demo/stop", {"project": "demo", "worker": worker}, origin=self.origin)
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["thread_stopped"])
        self.assertEqual(sync.workers(self.path)[0]["status"], "stopped")

    def test_filtered_snapshot_and_bad_queries(self):
        sync.register(self.path, "alpha-worker", "team", "role", "task", project="alpha")
        sync.register(self.path, "beta-worker", "team", "role", "task", project="beta")
        code, body, _ = self.req(path="/api/snapshot?project=alpha")
        self.assertEqual(code, 200)
        self.assertEqual([w["id"] for w in json.loads(body)["workers"]], ["alpha-worker"])
        for query in ("?project=alpha&project=beta", "?project=", "?other=x", "?project=../bad"):
            self.assertEqual(self.req(path="/api/snapshot"+query)[0], 400)

    def test_nonfinite_legacy_values_still_valid_json(self):
        raw = web_server.json_bytes({"lease": float("inf"), "nested": [float("nan")]})
        self.assertEqual(json.loads(raw), {"lease": None, "nested": [None]})


if __name__ == "__main__":
    unittest.main(verbosity=2)
