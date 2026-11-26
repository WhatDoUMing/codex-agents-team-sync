import http.client
import json
import threading
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import sync
import web_server


class BrowserLaunchTest(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        db = Path(self.scratch.name) / "fixture.db"
        sync.initialize(db)
        self.server = web_server.create_server(db, port=0, open_browser=True)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.scratch.cleanup()

    def request(self, method, path, body=None, **headers):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        with_headers = {"Content-Type": "application/json", **headers}
        try:
            conn.request(method, path, None if body is None else json.dumps(body), with_headers)
            res = conn.getresponse()
            return res.status, res.read()
        finally:
            conn.close()

    def redeem(self, offered):
        return self.request("POST", "/api/session/launch", {"ticket": offered}, Origin=self.origin)

    def test_automatic_ticket_does_not_remove_api_access_controls(self):
        ticket = self.server.browser_launch.ticket
        status, raw = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertNotIn(ticket.encode(), raw)
        self.assertNotIn(self.server.access_token.encode(), raw)
        self.assertEqual(self.request("GET", "/api/snapshot")[0], 401)
        self.assertEqual(self.request("POST", "/api/loop/control", {"action":"run"}, Origin=self.origin)[0], 401)
        self.assertEqual(self.request("POST", "/api/session/launch", {"ticket": ticket}, Origin="https://unrelated.invalid")[0], 403)
        self.assertEqual(self.request("POST", "/api/session/launch", {"ticket": ticket})[0], 403)
        self.assertEqual(self.request("POST", "/api/session/launch", {"ticket": ticket}, Origin=self.origin, Host="rebound.invalid")[0], 403)
        self.assertEqual(self.redeem("0" * 43)[0], 401)
        status, raw = self.redeem(ticket)
        self.assertEqual(status, 200)
        token = json.loads(raw)["token"]
        self.assertEqual(self.request("GET", "/api/snapshot", Authorization="Bearer " + token)[0], 200)
        self.assertEqual(self.redeem(ticket)[0], 401)

    def test_one_concurrent_request_can_redeem(self):
        ticket = self.server.browser_launch.ticket
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(self.redeem, [ticket, ticket]))
        self.assertEqual(sorted(result[0] for result in results), [200, 401])

    def test_expired_ticket_is_rejected(self):
        with patch.object(web_server.time, "monotonic", return_value=self.server.browser_launch.expires):
            self.assertEqual(self.redeem(self.server.browser_launch.ticket)[0], 401)
