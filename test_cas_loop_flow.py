import tempfile
import unittest
from pathlib import Path

import cas_loop_flow as flow


BASE, FIX, LOG = "1" * 64, "2" * 64, "3" * 64


def receipt(outcome, input_sha=BASE, candidate_sha=None):
    return dict(outcome=outcome, input_sha=input_sha, candidate_sha=candidate_sha, evidence_sha=LOG)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "bridge.db"
        self.db = flow.connect(self.path)
        self.addCleanup(lambda: self.db.close())
        self.id = "tp-fishgate"
        self.first = flow.start(self.db, self.id, BASE)

    def execute(self, event, run, result):
        flow.reserve(self.db, event)
        flow.bind(self.db, event, run)
        flow.record(self.db, event, run, result)

    def rows(self):
        return flow.view(self.db, self.id)

    def test_repair_chain_uses_review_of_exact_candidate(self):
        self.execute(self.first, 10, receipt("defect"))
        repair = self.rows()[1]["event_id"]
        self.execute(repair, 11, receipt("candidate", candidate_sha=FIX))
        review = self.rows()[2]["event_id"]
        flow.reserve(self.db, review)
        flow.bind(self.db, review, 12)
        with self.assertRaises(flow.Conflict):
            flow.record(self.db, review, 12, receipt("verified"))
        flow.record(self.db, review, 12, receipt("verified", input_sha=FIX))
        self.assertEqual([r["state"] for r in self.rows()], ["defect", "candidate", "verified"])

    def test_unknown_delivery_survives_restart_and_is_not_automatically_replayed(self):
        pending = flow.reserve(self.db, self.first)
        self.assertEqual(pending["type"], flow.RULES["reproduce"])
        self.db.close()
        self.db = flow.connect(self.path)
        self.assertEqual(self.rows()[0]["state"], "delivery_unknown")
        with self.assertRaises(flow.Conflict):
            flow.reserve(self.db, self.first)
        flow.bind(self.db, self.first, 7)
        self.assertEqual(self.rows()[0]["state"], "dispatched")
        with self.assertRaises(flow.Conflict):
            flow.observe_claim(self.db, self.first, 8)
        flow.observe_claim(self.db, self.first, 7)
        self.assertEqual(self.rows()[0]["state"], "claimed")
        self.assertEqual(len(self.rows()), 1)

    def test_repeat_and_wrong_receipts_do_not_advance(self):
        self.assertEqual(flow.start(self.db, self.id, BASE), self.first)
        with self.assertRaises(flow.Conflict):
            flow.start(self.db, self.id, FIX)
        self.execute(self.first, 7, receipt("defect"))
        flow.record(self.db, self.first, 7, receipt("defect"))
        for run, result in [(8, receipt("defect")), (True, receipt("defect")), (7, receipt("pass"))]:
            with self.assertRaises(flow.Conflict):
                flow.record(self.db, self.first, run, result)
        self.assertEqual(len(self.rows()), 2)

    def test_human_decision_blocks_handoff(self):
        self.execute(self.first, 7, receipt("needs_decision"))
        self.assertEqual([r["state"] for r in self.rows()], ["needs_decision"])
