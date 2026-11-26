"""Scoped Loop source transport for the CAS workflow, never a browser API."""
import http.client
import json

from cas_loop_flow import RULES, Conflict, reserve, bind, observe_claim, record

RECEIPT = "cas.checkpoint.receipt"


def _reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Conflict("ambiguous Loop payload")
        result[key] = value
    return result


class WorkflowSource:
    def __init__(self, db, port, source_key):
        if type(port) is not int or not 1024 <= port <= 65535 or len(source_key) < 24:
            raise ValueError("explicit local Loop source required")
        self.db, self.port, self._key = db, port, source_key
        db.execute("""CREATE TABLE IF NOT EXISTS workflow_delivery (
            cas_event TEXT PRIMARY KEY REFERENCES step(event_id),
            loop_event INTEGER NOT NULL UNIQUE CHECK(loop_event > 0)
        )""")

    def _request(self, method, route, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=4)
        try:
            conn.request(method, route, None if body is None else json.dumps(body).encode(), {
                "Authorization": "Bearer " + self._key, "Content-Type": "application/json",
            })
            res = conn.getresponse()
            raw = res.read(1048577)
            if res.status != 200 or len(raw) > 1048576:
                raise Conflict("Loop source request not verified")
            value = json.loads(raw, object_pairs_hook=_reject_duplicates)
            if not isinstance(value, dict):
                raise Conflict("invalid Loop response")
            return value
        finally:
            conn.close()

    def dispatch(self, event_id):
        pending = reserve(self.db, event_id)
        body = self._request("POST", "/v1/events", {
            "id": event_id, "type": pending["type"], "subject": pending["subject"],
            "detail": pending["snapshot"],
        })
        loop_event = body.get("event")
        if type(loop_event) is not int or loop_event < 1:
            raise Conflict("event identity not verified; do not dispatch it again")
        paused = body.get("routed") == [] and body.get("paused") == [pending["type"]]
        if not paused and body.get("routed") != [pending["type"]]:
            raise Conflict("event route not verified; do not dispatch it again")
        with self.db:
            self.db.execute("INSERT INTO workflow_delivery VALUES(?,?)", (event_id, loop_event))
            if paused:
                self.db.execute(
                    "UPDATE step SET state='rule_paused' WHERE event_id=? AND state='delivery_unknown'", (event_id,))
        return loop_event

    def reconcile(self, event_id):
        row = self.db.execute("""SELECT step.*, workflow_delivery.loop_event
            FROM step JOIN workflow_delivery ON cas_event=event_id WHERE event_id=?""", (event_id,)).fetchone()
        if row is None:
            raise Conflict("no verified Loop event mapping")
        rule = RULES[row["stage"]]
        body = self._request("GET", f'/v1/events/{row["loop_event"]}')
        runs = body.get("runs")
        if row["state"] == "rule_paused":
            if (type(body.get("event")) is int and body["event"] == row["loop_event"]
                    and body.get("routed") == [] and body.get("paused") == [rule] and runs == []):
                return "rule_paused", None
            raise Conflict("paused event changed; deliberate operator reconciliation required")
        if (type(body.get("event")) is not int or body["event"] != row["loop_event"]
                or body.get("routed") != [rule] or not isinstance(runs, list) or len(runs) != 1
                or not isinstance(runs[0], dict) or runs[0].get("rule") != rule):
            raise Conflict("source event did not resolve to one matching run")
        run = runs[0]
        run_id, state = run.get("run"), run.get("state")
        if state not in {"queued", "claimed", "unknown", "done", "nothing", "cancelled"}:
            raise Conflict("unexpected Loop run state")
        bind(self.db, event_id, run_id)
        if state == "claimed":
            observe_claim(self.db, event_id, run_id)
        if state != "done":
            return state, run_id
        events = run.get("events")
        if (not run.get("finishedAt") or not isinstance(events, list) or len(events) != 1
                or not isinstance(events[0], dict) or events[0].get("id") != event_id + ":result"
                or events[0].get("type") != RECEIPT or events[0].get("subject") != row["flow_id"]
                or not isinstance(events[0].get("detail"), str)):
            raise Conflict("terminal run lacks its exact typed checkpoint")
        receipt = json.loads(events[0]["detail"], object_pairs_hook=_reject_duplicates)
        record(self.db, event_id, run_id, receipt)
        return state, run_id
