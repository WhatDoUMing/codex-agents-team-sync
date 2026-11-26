"""One-shot control for a dedicated, locally constructed Loop test hub.
Production CAS never supplies a Probe. No route accepts a rule, prompt or address.
"""
import http.client
import json
from threading import Lock

RULE = "cas-browser-probe"
FORBIDDEN_PORTS = {3350, 8765, 8766}


class Probe:
    def __init__(self, port, owner, expected):
        if type(port) is not int or port in FORBIDDEN_PORTS or not 1024 <= port <= 65535:
            raise ValueError("isolated loop port required")
        if not isinstance(owner, str) or len(owner) < 24 or not expected.startswith("CAS-PROBE-"):
            raise ValueError("isolated probe credentials required")
        self._port, self._owner, self._expected = port, owner, expected
        self._lock = Lock()
        self._attempted = False
        self._run = None

    def _json(self, method, route, body=None):
        con = http.client.HTTPConnection("127.0.0.1", self._port, timeout=4)
        try:
            con.request(method, route, body=body, headers={
                "Authorization": "Bearer " + self._owner, "Content-Type": "application/json"})
            r = con.getresponse()
            if r.status != 200:
                raise ValueError("isolated Loop rejected request")
            raw = r.read(1048577)
            if len(raw) > 1048576:
                raise ValueError("invalid Loop payload")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("invalid Loop payload")
            return value
        finally:
            con.close()

    def _state(self):
        value = self._json("GET", "/v1/state")
        if [r.get("name") for r in value["lines"]] != [RULE]:
            raise ValueError("unexpected isolated rules")
        return value

    def status(self):
        with self._lock:
            attempted, run = self._attempted, self._run
        result = {"enabled": True, "run": run, "can_dispatch": False}
        if attempted and run is None:
            return {**result, "state": "delivery_unknown"}
        try:
            state = self._state()
            if not attempted:
                return {**result, "state": "ready" if not state["runs"] else "unavailable",
                        "can_dispatch": not state["runs"]}
            row = next((r for r in state["runs"] if r.get("id") == run and r.get("rule") == RULE), None)
            if row:
                state_name = row.get("state")
                if state_name not in ("queued", "claimed", "done", "failed", "cancelled", "canceled", "stopped"):
                    state_name = "unknown"
                matched = state_name == "done" and row.get("summary") == self._expected
                return {**result, "state": state_name, "receipt_matches": matched}
            return {**result, "state": "awaiting_receipt"}
        except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError, AttributeError):
            return {**result, "state": "unavailable"}

    def dispatch(self):
        with self._lock:
            if self._attempted:
                return {"accepted": False, "run": self._run, "state": "already_attempted"}
            try:
                if self._state()["runs"]:
                    return {"accepted": False, "state": "unavailable"}
            except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError, AttributeError):
                return {"accepted": False, "state": "unavailable"}
            self._attempted = True
            try:
                run = self._json("POST", "/v1/rules/" + RULE + "/run", b"{}")["run"]
                if type(run) is not int or run < 1:
                    raise ValueError("invalid run ID")
                self._run = run
                return {"accepted": True, "run": run, "state": "queued"}
            except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError, AttributeError):
                return {"accepted": False, "state": "delivery_unknown"}
