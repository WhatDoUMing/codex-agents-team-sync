import json
from pathlib import Path
import tempfile
import unittest

from aams_projects import ProjectBoard
from cas_loop_flow import Conflict
from test_aams_projects import ProjectFixture


def employee(id, proof=None):
    row = {"id": id, "name": id, "task_id": "task-" + id, "task_title": "Scope for " + id}
    if proof:
        row.update(event_type="test." + id, rule="Route " + id, proof_kind=proof)
    return row


class DelegationFixture(ProjectFixture):
    roles = {
        **ProjectFixture.roles,
        "test.project": ("Project manager", "manager"),
        "test.team": ("Department lead", "lead"),
        "test.other": ("Other department lead", "alternate"),
        "test.pixels": ("Route pixels", "pixels"),
        "test.publish": ("Route publish", "publish"),
        "test.audit": ("Route audit", "audit"),
    }


class DelegationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.f = DelegationFixture(self.root)
        self.board = self.f.board
        self.board.configure({
            "project": {"id": "tp", "name": "潮核",
                        "manager": {"id": "manager", "event_type": "test.project", "rule": "Project manager"}},
            "departments": [
                {"id": "other", "name": "Other", "lead": "alternate", "mainline": "Other work",
                 "scope_type": "test.other", "scope_rule": "Other department lead", "delegation_only": True,
                 "employees": [employee("alternate")]},
                {"id": "release", "name": "Release", "lead": "lead", "mainline": "Pixel regression and release",
                 "scope_type": "test.team", "scope_rule": "Department lead", "delegation_only": True,
                 "employees": [employee("lead"), employee("pixels", "result"),
                               employee("publish", "pr"), employee("audit", "review"), employee("observer")]},
            ],
        })
        (self.root / "field").mkdir()

    def issue(self, id="parent", version=0):
        return self.board.issue_delegation(id, "tp", "delivery-task", version, "Check candidate", "Compare pixels and deliver PR")

    def finish(self, run, receipt, *, which_session=None, token=None):
        session = f"11111111-0000-0000-0000-{(which_session or run):012d}"
        self.f.finish(run, receipt, token=token, session=session)
        self.board.tick()
        return session

    def delegation(self):
        return self.board.view("tp")["delegations"][0]

    def detail(self, run):
        return json.loads(self.f.sent[run-1]["detail"])

    def record(self, run, outcome="passed", **proof):
        data = self.detail(run)
        path = self.root / data["evidence_file"]
        record = {"change_id": data["change_id"], "task_id": data["task_id"],
                  "version": data["version"], "outcome": outcome, **proof}
        path.write_text(json.dumps(record), encoding="utf-8")
        return data["evidence_file"], path

    def choose(self, order=None):
        self.issue()
        self.board.tick()
        self.finish(1, "DEPARTMENT release | This pool owns the regression and delivery")
        self.board.tick()
        order = order or [["pixels", "Compare the rendered sole in columns A | B"],
                          ["publish", "Open the permitted candidate pull request"],
                          ["audit", "Independently compare the actual candidate and PR"]]
        self.finish(2, "STAFF | " + json.dumps(order))
        self.board.tick()

    def test_delegation_matches_only_both_actual_decisions_and_orders_verified_staff(self):
        self.choose()
        self.assertEqual([event["type"] for event in self.f.sent], ["test.project", "test.team", "test.pixels"])
        self.assertNotIn("department", {k for k, v in self.detail(1).items() if v})
        self.assertEqual(self.detail(2)["parent_id"], "parent")
        self.assertEqual(self.detail(3)["instruction"], "Compare the rendered sole in columns A | B")
        root = self.delegation()
        department, = root["children"]
        pixels, publish, audit = department["children"]
        self.assertEqual((department["assigned_run_id"], department["delegated_by"]), (1, "manager"))
        self.assertEqual((pixels["assigned_run_id"], pixels["delegated_by"]), (2, "lead"))
        self.assertEqual((publish["state"], audit["state"]), ("waiting", "waiting"))
        self.assertEqual((publish["predecessor_id"], audit["predecessor_id"]), (pixels["id"], publish["id"]))
        candidate, commit = "a"*40, "b"*40
        pr = "https://github.com/example/private/pull/3"
        ref, _ = self.record(3, candidate=candidate, pixel_offset=0)
        self.finish(3, f"APPLIED_RESULT | 18 grounded poses compared | {ref}")
        self.board.tick()
        self.assertEqual(self.detail(4)["previous"]["candidate"], candidate)
        ref, _ = self.record(4, candidate=candidate, commit=commit, pr=pr)
        self.finish(4, f"APPLIED_RESULT | Opened verified private PR | {ref}")
        self.board.tick()
        self.assertEqual(self.detail(5)["previous"]["pr"], pr)
        ref, _ = self.record(5, candidate=candidate, commit=commit, pr=pr)
        audit_session = self.finish(5, f"APPLIED_RESULT | Confirmed the same head and capture | {ref}")
        self.assertEqual(len(self.f.sent), 5)
        board = ProjectBoard(self.f.database, None, self.root)
        item = board.view("tp")["delegations"][0]
        self.assertEqual(item["state"], "reported")
        review = item["children"][0]["children"][2]
        self.assertEqual(review["deliveries"][0]["session"], audit_session)
        self.assertEqual(review["deliveries"][0]["rules_version"], 1)
        self.assertEqual(board.attachment(review["id"], ref)["pr"], pr)

    def test_pause_supersede_same_key_and_reconcile_lost_post(self):
        self.board.pause_delegations("tp", True)
        self.assertEqual(self.issue(), self.issue())
        self.board.tick()
        self.assertFalse(self.f.sent)
        self.board.pause_delegations("tp", False)
        self.f.lose_reply = True
        self.board.tick()
        self.board.tick()
        self.assertEqual(len(self.f.sent), 1)
        self.issue("revision", 1)
        self.finish(1, "DEPARTMENT release | First decision came in late")
        self.board.tick()
        self.assertEqual(len(self.f.sent), 2)
        historical = self.board.view("tp")["delegations"][1]
        self.assertFalse(historical["current"])
        self.assertFalse(historical["children"])
        self.board.pause("tp", "release", True)
        self.finish(2, "DEPARTMENT release | Current decision")
        self.board.tick()
        self.assertEqual(len(self.f.sent), 2)
        self.board.pause("tp", "release", False)
        self.board.tick()
        self.assertEqual(self.detail(3)["version"], 2)

    def test_scope_and_independent_session_cannot_be_bypassed(self):
        with self.assertRaises(Conflict):
            self.board.issue("manual", "tp", "release", "person", "publish", "task-publish", 0, "Publish", "PR")
        self.issue()
        self.board.tick()
        self.finish(1, "DEPARTMENT release | Bound test")
        self.board.tick()
        self.finish(2, 'STAFF | [["pixels","Compare soles"]]', which_session=1)
        department = self.delegation()["children"][0]
        self.assertEqual(department["state"], "unverified")
        self.assertIn("同一 Codex 会话", department["deliveries"][0]["note"])
        self.assertFalse(department["children"])
        self.assertEqual(len(self.f.sent), 2)

    def test_invalid_staff_order_does_not_persist_any_partial_subtask(self):
        self.choose([["pixels", "Measure"], ["audit", "Review without a PR"]])
        department = self.delegation()["children"][0]
        self.assertEqual(department["state"], "unverified")
        self.assertFalse(department["children"])
        self.assertEqual(len(self.f.sent), 2)

    def test_failed_evidence_waits_and_continuation_preserves_original_attachment(self):
        self.choose()
        ref, path = self.record(3, outcome="failed", pixel_offset=15)
        self.finish(3, f"APPLIED_RESULT | Sole moves 15 px right | {ref}")
        pixels, publish, _ = self.delegation()["children"][0]["children"]
        self.assertEqual((pixels["state"], publish["state"]), ("blocked", "waiting"))
        self.board.continue_change(pixels["id"], "pixels", 3)
        self.board.continue_change(pixels["id"], "pixels", 3)
        self.board.tick()
        next_ref, _ = self.record(4, candidate="a"*40, pixel_offset=0)
        self.assertNotEqual(ref, next_ref)
        self.finish(4, f"APPLIED_RESULT | Sole stays within tolerance | {next_ref}")
        self.board.tick()
        self.assertEqual(self.detail(5)["previous"]["candidate"], "a"*40)
        self.assertEqual(self.board.attachment(pixels["id"], ref)["pixel_offset"], 15)
        path.write_text(path.read_text(encoding="utf-8").replace("15", "0"), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.board.attachment(pixels["id"], ref)

    def test_different_candidate_or_replaced_evidence_cannot_release_a_followup(self):
        self.choose()
        ref, path = self.record(3, candidate="a"*40, pixel_offset=0)
        self.finish(3, f"APPLIED_RESULT | Verified capture | {ref}")
        original = path.read_bytes()
        path.write_bytes(original.replace(b'pixel_offset": 0', b'pixel_offset": 15'))
        self.board.tick()
        self.assertEqual(len(self.f.sent), 3)
        path.write_bytes(original)
        self.board.tick()
        wrong, _ = self.record(4, candidate="c"*40, commit="b"*40, pr="https://github.com/example/private/pull/2")
        self.finish(4, f"APPLIED_RESULT | Candidate changed | {wrong}")
        self.board.tick()
        _, publish, audit = self.delegation()["children"][0]["children"]
        self.assertEqual((publish["state"], audit["state"]), ("unverified", "waiting"))
        self.assertEqual(len(self.f.sent), 4)
