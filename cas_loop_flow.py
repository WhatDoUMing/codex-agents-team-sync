"""CAS-to-Loop lifecycle contract (no credentials, process launch or Git writes)."""

import hashlib
import json
import re
import sqlite3


STAGES = ("reproduce", "repair", "review")
RULES = {
    "reproduce": "tp.fishgate.reproduce",
    "repair": "tp.fishgate.repair",
    "review": "tp.fishgate.review",
}
INITIAL = {"reproduce": {"pass", "defect"}, "repair": {"candidate"}, "review": {"verified", "rejected"}}


class Conflict(ValueError):
    """Inconsistent, stale or insufficient workflow evidence."""


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _sha(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def connect(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS flow (
          id TEXT PRIMARY KEY, baseline TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS step (
          flow_id TEXT NOT NULL REFERENCES flow(id),
          stage TEXT NOT NULL, event_id TEXT NOT NULL UNIQUE, input_sha TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'ready', run_id INTEGER UNIQUE, receipt TEXT,
          PRIMARY KEY (flow_id, stage)
        );
    """)
    return db


def _new_step(db, flow_id, stage, sha):
    eid = "cas-" + _digest([flow_id, stage, sha])
    db.execute("INSERT INTO step(flow_id,stage,event_id,input_sha) VALUES(?,?,?,?)", (flow_id, stage, eid, sha))
    return eid


def start(db, flow_id, baseline):
    if not isinstance(flow_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", flow_id) or not _sha(baseline):
        raise Conflict("invalid workflow id or snapshot fingerprint")
    with db:
        db.execute("INSERT OR IGNORE INTO flow VALUES(?,?)", (flow_id, baseline))
        known = db.execute("SELECT baseline FROM flow WHERE id=?", (flow_id,)).fetchone()
        if known["baseline"] != baseline:
            raise Conflict("workflow id already binds another snapshot")
        old = db.execute("SELECT event_id FROM step WHERE flow_id=? AND stage='reproduce'", (flow_id,)).fetchone()
        return old["event_id"] if old else _new_step(db, flow_id, "reproduce", baseline)


def _row(db, event_id):
    row = db.execute("SELECT * FROM step WHERE event_id=?", (event_id,)).fetchone()
    if row is None:
        raise Conflict("unknown event")
    return row


def reserve(db, event_id):
    """Persist the attempt before transport; an unanswered attempt must be reconciled."""
    with db:
        change = db.execute("UPDATE step SET state='delivery_unknown' WHERE event_id=? AND state='ready'", (event_id,))
        if change.rowcount != 1:
            raise Conflict("event not eligible for new delivery")
        row = _row(db, event_id)
        return {"event_id": event_id, "type": RULES[row["stage"]], "subject": row["flow_id"], "snapshot": row["input_sha"]}


def bind(db, event_id, run_id):
    """Call only after observing the exact source event to Loop run association."""
    if type(run_id) is not int or run_id <= 0:
        raise Conflict("invalid verified Loop run id")
    with db:
        row = _row(db, event_id)
        if row["run_id"] == run_id:
            return
        if row["state"] != "delivery_unknown" or row["run_id"] is not None:
            raise Conflict("run does not match a pending delivery")
        try:
            changed = db.execute(
                "UPDATE step SET run_id=?,state='dispatched' WHERE event_id=? AND state='delivery_unknown' AND run_id IS NULL",
                (run_id, event_id))
        except sqlite3.IntegrityError as exc:
            raise Conflict("Loop run already belongs to another event") from exc
        if changed.rowcount != 1:
            raise Conflict("run binding was changed concurrently")


def observe_claim(db, event_id, run_id):
    """Reflect a trusted Loop lease observation without equating it with completion."""
    if type(run_id) is not int or run_id <= 0:
        raise Conflict("invalid verified Loop run id")
    with db:
        changed = db.execute(
            "UPDATE step SET state='claimed' WHERE event_id=? AND run_id=? AND state IN ('dispatched','claimed')",
            (event_id, run_id))
        if changed.rowcount != 1:
            raise Conflict("claim does not match an active run")


def record(db, event_id, run_id, receipt):
    """Advance only after trusted transport confirms exact run and candidate evidence."""
    if (type(run_id) is not int or run_id <= 0 or not isinstance(receipt, dict)
            or set(receipt) != {"outcome", "input_sha", "candidate_sha", "evidence_sha"}
            or not isinstance(receipt["outcome"], str) or not _sha(receipt["input_sha"])
            or not _sha(receipt["evidence_sha"])):
        raise Conflict("invalid receipt")
    encoded = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
    with db:
        row = _row(db, event_id)
        if row["run_id"] != run_id or receipt["input_sha"] != row["input_sha"]:
            raise Conflict("receipt bound to wrong run or snapshot")
        if row["receipt"] == encoded:
            return
        if row["state"] not in {"dispatched", "claimed"} or row["receipt"] is not None:
            raise Conflict("stale or conflicting receipt")
        index = STAGES.index(row["stage"])
        outcome, candidate = receipt["outcome"], receipt["candidate_sha"]
        if outcome not in INITIAL[row["stage"]] | {"blocked", "needs_decision"}:
            raise Conflict("unrecognized stage outcome")
        if outcome == "candidate":
            if not _sha(candidate) or candidate == row["input_sha"]:
                raise Conflict("repair must supply a distinct candidate fingerprint")
        elif candidate is not None:
            raise Conflict("unexpected candidate fingerprint")
        if outcome == "pass":
            state = "verified"
        else:
            state = outcome
        changed = db.execute("UPDATE step SET state=?,receipt=? WHERE event_id=? AND state IN ('dispatched','claimed') AND receipt IS NULL",
                             (state, encoded, event_id))
        if changed.rowcount != 1:
            raise Conflict("receipt was changed concurrently")
        if outcome in {"defect", "candidate"}:
            _new_step(db, row["flow_id"], STAGES[index + 1], candidate or row["input_sha"])


def view(db, flow_id):
    rows = db.execute("SELECT * FROM step WHERE flow_id=? ORDER BY rowid", (flow_id,)).fetchall()
    return [dict(r) for r in rows]
