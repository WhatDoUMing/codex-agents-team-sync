"""Opt-in control of the one registered Tidecore rule via the local Loop API."""
import http.client
import json
import os
from pathlib import Path
from threading import Lock
from urllib.parse import quote

from loop_status import RULE

ACTIONS = {"run": {}, "pause": {"reason": "由潮核控制台手动暂停"}, "resume": {}}
LOCAL = "http://127.0.0.1:3350"
RULES = {
    RULE: (None, None, None),
    "潮核·项目管理员": ("tp-manager", "gpt-6.2-astra", "max"),
    "潮核·剧情与关卡结构": ("tp-story", "gpt-6.2-sol", "max"),
    "潮核·地图与场景": ("tp-map", "gpt-6.2-astra", "xhigh"),
    "潮核·战斗与机关": ("tp-combat", "gpt-6.2-sol", "max"),
    "潮核·固定动画专员": ("tp-animation", "gpt-6.2-astra", "xhigh"),
    "潮核·独立审核与验收": ("tp-review", "gpt-6.2-astra", "xhigh"),
}


def registered(line):
    if not isinstance(line, dict) or not isinstance(line.get("name"), str):
        return False
    name, tags = line["name"], line.get("tags")
    if name not in RULES or not isinstance(tags, list) or "tidecore" not in tags:
        return False
    agent, _, _ = RULES[name]
    return agent is None or (agent in tags and line.get("agent") == agent)


def local_owner():
    config = json.loads((Path.home()/".agent-autoloop"/"config.json").read_bytes())
    if config.get("url") != LOCAL or (config.get("hub") or {}).get("listen") != "127.0.0.1:3350":
        raise ValueError("local Hub required")
    key = config.get("owner")
    if not isinstance(key, str) or not key.isascii() or len(key) < 24 or any(ch.isspace() for ch in key):
        raise ValueError("local owner access required")
    return key


def local_request(owner, method, path, data=None, *, port=3350):
    con = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        headers = {"Authorization":"Bearer "+owner, "Content-Type":"application/json"}
        body = None if data is None else json.dumps(data, ensure_ascii=True).encode("ascii")
        con.request(method, path, body=body, headers=headers)
        res = con.getresponse()
        if res.status != 200:
            if method == "POST" and res.status in (400, 401, 403, 404, 409):
                raise Rejected()
            raise ValueError("unexpected Loop response")
        raw = res.read(2097153)
        if len(raw) > 2097152:
            raise ValueError("invalid response size")
        data = json.loads(raw)
        if type(data) is not dict:
            raise ValueError("invalid response")
        return data
    finally:
        con.close()


class Rejected(Exception):
    pass


class Control:
    def __init__(self, owner=local_owner, request=local_request):
        self._owner, self._request = owner, request
        self._mutex = Lock()
        self._uncertain = False

    def _status(self, key, rule=RULE):
        data = self._request(key, "GET", "/v1/state")
        if type(data.get("lines")) is not list or type(data.get("runs")) is not list:
            raise ValueError("invalid state")
        known = [r for r in data["lines"] if registered(r)]
        rows = [r for r in known if r["name"] == rule]
        roster = [{"name": r["name"], "agent": r.get("agent"), "configured_model": RULES[r["name"]][1],
                   "configured_effort": RULES[r["name"]][2]} for r in known]
        if len(rows) != 1 or "paused" not in rows[0]:
            return {"status":"rule_missing", "allowed":[], "roster":roster}
        paused = rows[0]["paused"] is not None
        active = any(r.get("rule") == rule and r.get("state") in ("queued","claimed","running") for r in data["runs"])
        allowed = ["resume"] if paused else (["pause"] if active else ["run","pause"])
        return {"status":"uncertain" if self._uncertain else "online", "rule":rule, "roster":roster,
                "paused":paused,"active":active,"allowed":[] if self._uncertain else allowed,
                "observed_at":data.get("now") if isinstance(data.get("now"),str) else None}

    def status(self, rule=RULE):
        with self._mutex:
            try:
                return self._status(self._owner(), rule)
            except (ValueError, OSError, http.client.HTTPException, TypeError, AttributeError):
                return {"status":"unavailable","allowed":[]}

    def action(self, name, rule=RULE):
        with self._mutex:
            try:
                key = self._owner()
                state = self._status(key, rule)
            except (ValueError, OSError, http.client.HTTPException, TypeError, AttributeError):
                return 503, {"error":"Loop 不可用，未发送请求"}
            if name not in state["allowed"]:
                return 409, {"error":"当前不能执行；请核对规则状态"}
            try:
                result = self._request(key, "POST", "/v1/rules/" + quote(rule, safe="")+"/"+name, ACTIONS[name])
                if name == "run":
                    run = result.get("run")
                    if type(run) is not int or run < 1:
                        raise ValueError("invalid run id")
                    return 200, {"accepted":True,"run":run}
                # Only report a control state when a fresh readback confirms it.
                current = self._status(key, rule)
                if current.get("status") != "online" or current.get("paused") != (name=="pause"):
                    raise ValueError("state did not confirm operation")
                return 200, {"accepted":True,"paused":current["paused"]}
            except Rejected:
                return 409, {"error":"Loop 拒绝了请求，未自动重试"}
            except (ValueError, OSError, http.client.HTTPException, TypeError, AttributeError):
                self._uncertain = True
                return 502, {"error":"操作结果未知，已暂停控制，请先核对 Loop，不要重发"}


def configured():
    return Control() if os.environ.get("CAS_LOOP_CONTROL") == "1" else None
