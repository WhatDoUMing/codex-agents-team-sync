import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import loop_status
import web_server


def sample():
    return {"now":"2026-11-26T04:00:00Z", "lines":[
        {"name":loop_status.RULE, "prompt":"private"}, {"name":"foreign"},
    ],"runs":[
        {"id":1,"rule":loop_status.RULE,"state":"claimed","claimedAt":"2026-11-26T03:59:00Z","summary":"private"},
        {"id":2,"rule":"shore","tags":["tidecore"],"state":"failed","error":"secret"},
        {"id":3,"rule":"foreign","state":"done"},
        {"id":4,"rule":loop_status.RULE,"state":"future-state"},
    ]}


class LoopStatusTest(unittest.TestCase):
    def test_minimized_projection(self):
        output=loop_status.project(sample())
        self.assertEqual(output["rules"], [loop_status.RULE])
        self.assertEqual([(r["id"],r["state"]) for r in output["runs"]], [(1,"claimed"),(2,"failed"),(4,"unknown")])
        self.assertNotIn("private", json.dumps(output))
        self.assertNotIn("secret", json.dumps(output))
        self.assertEqual(output["observed_at"], sample()["now"])

    def test_not_configured(self):
        with patch.dict(os.environ, {"CAS_LOOP_PANEL":"0"}):
            self.assertEqual(loop_status.read_status()["status"],"disabled")

    def test_invalid_inputs_fail_closed(self):
        with self.assertRaises(ValueError):
            loop_status.project({"lines":[],"runs":"invalid"})

    def test_auth_and_http_rendering(self):
        with tempfile.TemporaryDirectory() as tmp:
            token="test-only-token" * 3
            server=web_server.create_server(Path(tmp)/"cas.db",port=0,token=token)
            runner=threading.Thread(target=server.serve_forever,daemon=True)
            runner.start()
            root="http://127.0.0.1:"+str(server.server_address[1])
            opener=build_opener(ProxyHandler({}))
            try:
                with opener.open(root) as page:
                    self.assertIn("Loop 工作池",page.read().decode())
                url=root+"/api/loop/status"
                for headers, code in [({},401),({"Authorization":"Bearer "+token,"Origin":"https://other.test"},403)]:
                    with self.assertRaises(HTTPError) as blocked:
                        opener.open(Request(url,headers=headers))
                    self.assertEqual(blocked.exception.code,code)
                    blocked.exception.close()
                with patch("loop_status.read_status",return_value=loop_status.project(sample())):
                    with opener.open(Request(url,headers={"Authorization":"Bearer "+token})) as state:
                        self.assertEqual(json.load(state)["runs"][0]["id"],1)
            finally:
                server.shutdown()
                runner.join()
                server.server_close()
