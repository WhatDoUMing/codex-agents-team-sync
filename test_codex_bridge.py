"""Owned bridge tests use fake RPCs only; real model QA is explicitly opt-in."""
import copy
import json
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest
import subprocess
import sys
from unittest.mock import patch
import http.client
from codex_rpc import OwnedRPC, DeliveryUnknown
from codex_bridge import Manager
import sync
import web_server


class FakeProcess:
    alive = True
    def poll(self): return None if self.alive else 0
    def terminate(self): self.alive = False


class FakeRPC:
    def __init__(self):
        self.events, self.calls, self.responses = queue.Queue(), [], []
        self.process, self.readers, self.fault = FakeProcess(), [], None
        self.own_thread, self.active, self.number = "new-owned-thread", None, 0
        self.fail_start = None
        self.fail_steer = False
        self.auto_interrupt = True
    def initialize(self): pass
    def request(self, method, params, timeout=12):
        self.calls.append((method, copy.deepcopy(params)))
        if method == "thread/start":
            return {"model": "gpt-6.1-sol", "thread": {"id": self.own_thread, "environments": []}}
        if method == "turn/start":
            if self.fail_start: raise self.fail_start
            self.number += 1
            self.active = "turn-" + str(self.number)
            return {"turn": {"id": self.active, "status": "inProgress"}}
        if method == "turn/steer":
            if self.fail_steer: raise ValueError("active turn mismatch")
            return {"turnId": self.active}
        if method == "turn/interrupt":
            if self.auto_interrupt: self.finish("", "interrupted")
            return {}
        raise AssertionError("unexpected RPC")
    def respond(self, request_id, result=None, error=None):
        self.responses.append((request_id, result, error))
    def finish(self, text="model result", status="completed"):
        turn = self.active
        self.events.put({"method": "item/completed", "params": {"threadId": self.own_thread,
            "turnId": turn, "item": {"type": "agentMessage", "text": text}}})
        self.events.put({"method": "turn/completed", "params": {"threadId": self.own_thread,
            "turn": {"id": turn, "status": status}}})
        self.active = None
    def close(self): self.process.alive = False


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="owned-bridge-test-")
        self.path = Path(self.tmp.name) / "sync.sqlite3"
        self.cli = Path(self.tmp.name) / "codex.exe"
        self.cli.write_bytes(b"fake, never executed")
        self.rpc = FakeRPC()
        self.manager = Manager(self.path, self.cli, lambda *args: self.rpc)
    def tearDown(self):
        self.manager.close()
        self.tmp.cleanup()
    def wait(self, predicate, seconds=5):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if predicate(): return
            time.sleep(.02)
        self.fail("bridge condition timed out")
    def start(self):
        result = self.manager.start("qa", "create-one")
        self.worker = result["id"]
        self.actor = self.manager.actor("qa", self.worker)
        self.wait(lambda: self.actor.live()["state"] == "idle")
        self.wait(lambda: sync.workers(self.path)[0]["cooperative"] == 1)
        return result
    def enqueue(self, key="one", text="task", mode="queue", turn=None):
        return self.manager.enqueue("qa", self.worker, text, key, mode, turn)
    def command(self, key):
        return next(c for c in sync.snapshot(self.path)["commands"] if c["id"] == key)
    def active(self):
        self.wait(lambda: self.actor.live()["active_turn"] is not None)
        return self.actor.live()["active_turn"]

    def test_owned_registration_is_new_scoped_and_not_generically_sendable(self):
        result = self.start()
        self.assertEqual(self.manager.start("qa", "create-one")["id"], result["id"])
        with self.assertRaises(ValueError): self.manager.start("qa", "create-two")
        with self.assertRaises(ValueError): self.manager.actor("qa", "some-existing-platform-thread")
        with self.assertRaises(ValueError): sync.queue(self.path, self.worker, "generic bypass", project="qa")
        value = self.manager.enrich_live(sync.snapshot(self.path))
        self.assertTrue(value["workers"][0]["native"]["owned_here"])
        self.assertFalse(value["workers"][0]["native"]["verified"])
        self.assertTrue(value["workers"][0]["can_queue"])
        self.assertEqual([c[0] for c in self.rpc.calls], ["thread/start"])
        self.assertEqual(self.rpc.calls[0][1]["environments"], [])
        self.assertTrue(self.rpc.calls[0][1]["ephemeral"])

    def test_accepted_is_not_completed_and_local_idempotency_holds(self):
        self.start()
        self.enqueue()
        self.enqueue()
        turn = self.active()
        self.wait(lambda: self.command("one")["state"] == "acknowledged")
        self.assertNotEqual(self.command("one")["state"], "completed")
        self.enqueue("two", "next task")
        time.sleep(.15)
        self.assertEqual(self.command("two")["state"], "queued")
        self.rpc.finish("first answer")
        self.wait(lambda: self.command("one")["state"] == "completed")
        self.wait(lambda: self.actor.live()["active_turn"] not in (None, turn))
        self.assertEqual(sum(c[0] == "turn/start" for c in self.rpc.calls), 2)
        self.rpc.finish("second answer")
        self.wait(lambda: self.command("two")["state"] == "completed")

    def test_steer_binds_active_turn_and_same_turn_output(self):
        self.start(); self.enqueue()
        turn = self.active()
        with self.assertRaises(ValueError): self.enqueue("bad", "no", "steer", "old-turn")
        self.enqueue("steer", "append", "steer", turn)
        self.wait(lambda: any(c[0] == "turn/steer" for c in self.rpc.calls))
        self.assertEqual(next(c[1] for c in self.rpc.calls if c[0] == "turn/steer")["expectedTurnId"], turn)
        self.rpc.finish("nonce answer")
        self.wait(lambda: self.command("steer")["state"] == "completed")
        self.assertEqual(self.command("one")["result"], "nonce answer")
        self.assertEqual(self.command("steer")["native_delivery"]["turn_id"], turn)
        self.enqueue("steer", "append", "steer", turn)  # Replay receipt only, never RPC again.
        self.assertEqual(sum(c[0] == "turn/steer" for c in self.rpc.calls), 1)

    def test_vendor_rejection_never_falls_back_to_new_turn(self):
        self.start(); self.enqueue(); turn = self.active()
        self.rpc.fail_steer = True
        self.enqueue("steer", "append", "steer", turn)
        self.wait(lambda: self.command("steer")["state"] == "rejected")
        self.assertEqual(sum(c[0] == "turn/start" for c in self.rpc.calls), 1)
        self.assertEqual(self.command("steer")["native_delivery"]["phase"], "api_rejected")
        self.rpc.finish()
        self.wait(lambda: self.command("one")["state"] == "completed")

    def test_unknown_delivery_stops_and_does_not_retry(self):
        self.start(); self.rpc.fail_start = DeliveryUnknown("outcome unknown")
        self.enqueue()
        self.wait(lambda: self.actor.live()["state"] == "failed")
        self.assertEqual(self.command("one")["state"], "acknowledged")
        self.assertEqual(self.command("one")["native_delivery"]["phase"], "delivery_unknown")
        self.enqueue()  # Same local request returns receipt; no new RPC.
        self.assertEqual(sum(c[0] == "turn/start" for c in self.rpc.calls), 1)

    def test_foreign_events_and_duplicate_wait_requests_cannot_change_result(self):
        self.start(); self.enqueue(); turn = self.active()
        wrong = {"method": "item/tool/call", "id": "foreign", "params": {
            "threadId": "unowned", "turnId": turn, "tool": "bridge_wait", "arguments": {"seconds": 5}}}
        self.rpc.events.put(wrong)
        self.rpc.events.put({"method": "item/completed", "params": {"threadId": self.rpc.own_thread,
            "turnId": "old-turn", "item": {"type": "agentMessage", "text": "PRIVATE_FOREIGN_TEXT"}}})
        wait = {"method": "item/tool/call", "id": "wait-one", "params": {
            "threadId": self.rpc.own_thread, "turnId": turn, "tool": "bridge_wait", "arguments": {"seconds": 5}}}
        self.rpc.events.put(wait); self.rpc.events.put(copy.deepcopy(wait))
        self.wait(lambda: "wait-one" in self.actor.pending_waits)
        deadline = self.actor.pending_waits["wait-one"][0]
        self.rpc.events.put(copy.deepcopy(wait)); time.sleep(.15)
        self.assertEqual(self.actor.pending_waits["wait-one"][0], deadline)
        self.assertTrue(any(r[0] == "foreign" and r[2] for r in self.rpc.responses))
        self.rpc.finish("safe result")
        self.wait(lambda: self.command("one")["state"] == "completed")
        self.assertNotIn("PRIVATE_", self.command("one")["result"])

    def test_stop_request_and_interrupt_acceptance_are_not_stop_confirmation(self):
        self.start(); self.enqueue(); self.active()
        self.rpc.auto_interrupt = False
        self.assertTrue(self.manager.stop("qa", self.worker)["stop_requested"])
        self.wait(lambda: any(c[0] == "turn/interrupt" for c in self.rpc.calls))
        self.assertNotEqual(self.actor.live()["state"], "stopped")
        self.rpc.finish("", "interrupted")
        self.wait(lambda: self.actor.live()["state"] == "stopped")
        self.assertEqual(self.command("one")["state"], "rejected")
        self.assertEqual(self.command("one")["native_delivery"]["phase"], "turn_interrupted")
        self.assertEqual(sync.snapshot(self.path)["workers"][0]["native"]["cleanup_confirmed"], 1)

    def test_stop_wins_before_new_admission_or_dispatch(self):
        self.start()
        entered, release = threading.Event(), threading.Event()
        original = self.actor._events
        def held_events():
            entered.set()
            release.wait(2)
            return original()
        self.actor._events = held_events
        self.assertTrue(entered.wait(2))
        try:
            self.manager.stop("qa", self.worker)
            with self.assertRaises(ValueError):
                self.enqueue("after-stop", "must never be submitted")
        finally:
            release.set()
        self.wait(lambda: not self.actor.runner.is_alive())
        self.assertEqual(sum(c[0] == "turn/start" for c in self.rpc.calls), 0)
        self.assertEqual(sync.command_list(self.path), [])

    def test_all_cleanup_exceptions_quarantine_and_persist_failure(self):
        self.start()
        def failed_close():
            raise subprocess.TimeoutExpired("owned fixture", 1)
        self.rpc.close = failed_close
        self.manager.stop("qa", self.worker)
        self.wait(lambda: not self.actor.runner.is_alive())
        self.assertTrue(self.manager.quarantined)
        self.assertEqual(self.actor.live()["state"], "failed")
        self.assertEqual(sync.snapshot(self.path)["workers"][0]["native"]["cleanup_confirmed"], 0)
        with self.assertRaises(ValueError): self.manager.start("qa", "must-not-restart")

    def test_interrupt_confirmation_exception_still_closes_and_quarantines(self):
        self.start(); self.enqueue(); self.active()
        original = self.rpc.request
        def bad_confirmation(method, params, timeout=12):
            if method == "turn/interrupt":
                self.rpc.events.put({"method":"turn/completed", "params":{"threadId":self.rpc.own_thread,
                    "turn":{"id":self.actor.live()["active_turn"]}}})
                return {}
            return original(method, params, timeout)
        self.rpc.request = bad_confirmation
        self.manager.stop("qa", self.worker)
        self.wait(lambda: not self.actor.runner.is_alive())
        self.assertEqual(self.actor.live()["state"], "failed")
        self.assertTrue(self.manager.quarantined)
        self.assertIsNotNone(self.rpc.process.poll())
        with self.assertRaises(ValueError): self.manager.start("qa", "must-not-restart")

    def test_manager_close_permanently_rejects_start_and_enqueue(self):
        self.start()
        self.manager.close()
        with self.assertRaises(ValueError): self.manager.start("qa", "after-close")
        with self.assertRaises(ValueError): self.enqueue("after-close", "must not run")

    def test_rpc_guard_rejects_old_threads_methods_and_environment_overrides(self):
        client = object.__new__(OwnedRPC)
        client.own_thread = "owned"
        client.start_attempted = False
        for method in ("thread/resume", "thread/read", "command/exec", "account/read"):
            with self.assertRaises(ValueError): client.request(method, {})
        with self.assertRaises(ValueError): client.request("turn/steer", {"threadId": "old"})
        with self.assertRaises(ValueError):
            client.request("turn/start", {"threadId": "owned", "model": "gpt-6.1-sol", "environments": [{"cwd": "outside"}]})


class OwnedHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="owned-http-test-")
        self.root = Path(self.tmp.name)
        self.cli = self.root / "codex.exe"; self.cli.write_bytes(b"not executed")
        self.rpc = FakeRPC()
        self.server = web_server.create_server(self.root / "sync.sqlite3", port=0, token="test-key-"+"x"*40,
            codex_cli=self.cli, owned_factory=lambda path, cli: Manager(path, cli, lambda *a: self.rpc))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.port = self.server.server_address[1]
        self.origin = "http://127.0.0.1:" + str(self.port)
    def tearDown(self):
        self.server.shutdown(); self.server.owned.close(); self.server.server_close()
        self.thread.join(5); self.tmp.cleanup()
    def post(self, path, value, auth=True, origin=True):
        headers = {"Content-Type": "application/json"}
        if auth: headers["Authorization"] = "Bearer " + self.server.access_token
        if origin: headers["Origin"] = self.origin
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        try:
            c.request("POST", path, json.dumps(value), headers)
            r = c.getresponse(); return r.status, json.loads(r.read())
        finally: c.close()
    def test_new_routes_require_same_origin_and_auth_and_exact_fields(self):
        body = {"project": "qa", "request_id": "http"}
        self.assertEqual(self.post("/api/owned/start", body, auth=False)[0], 401)
        self.assertEqual(self.post("/api/owned/start", body, origin=False)[0], 403)
        self.assertEqual(self.post("/api/owned/start", {**body, "cli": "arbitrary.exe"})[0], 400)
        self.assertEqual(self.post("/api/owned/commands", {"threadId": "old"})[0], 400)
        self.assertEqual(self.rpc.calls, [])
    def test_cannot_send_or_interrupt_existing_unowned_platform_session(self):
        body = {"project": "qa", "worker": "existing-thread", "text": "no", "command_id": "x",
                "mode": "steer", "expected_turn": "old-turn"}
        self.assertEqual(self.post("/api/owned/commands", body)[0], 400)
        self.assertEqual(self.post("/api/owned/stop", {"project": "qa", "worker": "existing-thread"})[0], 400)
        self.assertEqual(self.rpc.calls, [])


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="owned-rpc-test-")
        self.root = Path(self.tmp.name)
        self.cli = self.root / "codex.exe"
        self.cli.write_bytes(b"fixture, never executed")
        self.real_popen = subprocess.Popen
    def tearDown(self): self.tmp.cleanup()
    def spawn(self, script):
        real = self.real_popen
        return lambda *a, **kw: real([sys.executable, "-B", "-u", "-c", script],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    def test_total_deadline_includes_blocked_pipe_write(self):
        with patch("codex_rpc.subprocess.Popen", side_effect=self.spawn("import time;time.sleep(5)")):
            client = OwnedRPC(self.cli, self.root)
        client.own_thread = "owned"
        start = time.monotonic()
        try:
            with self.assertRaises(DeliveryUnknown):
                client.request("turn/start", {"threadId":"owned", "model":"gpt-6.1-sol", "environments":[],
                    "input":[{"type":"text", "text":"x"*8000}]}, timeout=.1)
            self.assertLess(time.monotonic()-start, 1)
        finally: client.close()
        self.assertFalse(client.writer.is_alive())
        self.assertIsNotNone(client.process.poll())
    def test_rpc_wait_preserves_interleaved_notifications(self):
        script = "import sys,json\nfor line in sys.stdin:\n v=json.loads(line)\n if 'id' in v:\n  print(json.dumps({'method':'fixture/progress','params':{'marker':'keep-event'}}),flush=True)\n  print(json.dumps({'id':v['id'],'result':{}}),flush=True)"
        with patch("codex_rpc.subprocess.Popen", side_effect=self.spawn(script)):
            client = OwnedRPC(self.cli, self.root)
        try:
            client.initialize()
            self.assertEqual(client.events.get(timeout=1)["params"]["marker"], "keep-event")
        finally: client.close()
        self.assertFalse(any(r.is_alive() for r in client.readers))


if __name__ == "__main__":
    unittest.main(verbosity=2)
