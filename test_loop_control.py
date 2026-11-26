import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from loop_control import Control, RULE, Rejected, registered
from web_server import create_server


class Fake:
    def __init__(self):
        self.paused = False
        self.runs = []
        self.posts = []
        self.rule = RULE
        self.error = None

    def request(self, key, method, path, data=None):
        if method == "GET":
            return {"lines":[{"name":self.rule,"tags":["tidecore"],"paused":{} if self.paused else None}],
                    "runs":self.runs}
        action = path.rsplit("/",1)[-1]
        self.posts.append(action)
        if self.error:
            raise self.error
        if action == "run":
            self.runs.append({"rule":RULE,"state":"claimed"})
            return {"run":42}
        self.paused = action == "pause"
        return {}


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.loop=Fake()
        self.control=Control(lambda:"test-key",self.loop.request)

    def test_live_controls_and_busy_duplicate_block(self):
        self.assertEqual(self.control.action("pause"),(200,{"accepted":True,"paused":True}))
        self.assertEqual(self.control.action("run")[0],409)
        self.assertEqual(self.control.action("resume"),(200,{"accepted":True,"paused":False}))
        self.assertEqual(self.control.action("run"),(200,{"accepted":True,"run":42}))
        self.assertEqual(self.control.action("run")[0],409)
        self.assertEqual(self.loop.posts,["pause","resume","run"])

    def test_wrong_rule_blocks_all(self):
        self.loop.rule="其他规则"
        self.assertEqual(self.control.status()["status"],"rule_missing")
        self.assertEqual(self.control.action("run")[0],409)
        self.assertFalse(self.loop.posts)

    def test_unknown_response_cannot_be_resent(self):
        self.loop.error=TimeoutError()
        self.assertEqual(self.control.action("run")[0],502)
        self.assertEqual(self.control.status()["status"],"uncertain")
        self.assertEqual(self.control.action("run")[0],409)
        self.assertEqual(self.loop.posts,["run"])

    def test_role_requires_exact_name_tags_and_owner(self):
        good={"name":"潮核·项目管理员","tags":["tidecore","tp-manager"],"agent":"tp-manager"}
        self.assertTrue(registered(good))
        self.assertFalse(registered({**good,"agent":"tp-combat"}))
        self.assertFalse(registered({**good,"tags":["tidecore"]}))
        self.assertFalse(registered({**good,"name":"其他规则"}))

    def test_explicit_rejection_is_not_success(self):
        self.loop.error=Rejected()
        self.assertEqual(self.control.action("run")[0],409)

    def test_http_requires_token_origin_and_exact_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            token="test-only-key-"*4
            server=create_server(Path(tmp)/"cas.db",port=0,token=token,control=self.control)
            thread=threading.Thread(target=server.serve_forever)
            thread.start()
            base="http://127.0.0.1:"+str(server.server_port)
            opener=build_opener(ProxyHandler({}))
            headers={"Authorization":"Bearer "+token,"Content-Type":"application/json","Origin":base}
            try:
                def req(h, body):
                    return opener.open(Request(base+"/api/loop/control",headers=h,data=json.dumps(body).encode()),timeout=5)
                for h,b in [({**headers,"Origin":"https://invalid.test"},{"action":"run"}),
                            ({**headers,"Authorization":"Bearer invalid"},{"action":"run"}),
                            (headers,{"action":"run","rule":"foreign"}),
                            (headers,{"action":"stop"})]:
                    with self.assertRaises(HTTPError) as bad:
                        req(h,b)
                    bad.exception.close()
                self.assertFalse(self.loop.posts)
                with req(headers,{"action":"run"}) as ok:
                    self.assertEqual(json.load(ok)["run"],42)
            finally:
                server.shutdown()
                thread.join()
                server.server_close()
