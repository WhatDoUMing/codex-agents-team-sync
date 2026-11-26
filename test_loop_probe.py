import unittest
from unittest.mock import Mock
from loop_probe import Probe, RULE


class ProbeTest(unittest.TestCase):
    def test_production_port_rejected(self):
        with self.assertRaises(ValueError):
            Probe(3350, "A"*32, "CAS-PROBE-test")

    def test_refuse_wrong_rules_without_dispatch(self):
        p = Probe(3399, "A"*32, "CAS-PROBE-test")
        p._json = Mock(return_value={"lines":[{"name":"other"}],"runs":[]})
        self.assertFalse(p.dispatch()["accepted"])
        self.assertEqual(p._json.call_count,1)

    def test_does_not_retry_lost_response(self):
        p = Probe(3399, "A"*32, "CAS-PROBE-test")
        p._json = Mock(side_effect=[{"lines":[{"name":RULE}],"runs":[]}, TimeoutError()])
        self.assertEqual(p.dispatch()["state"],"delivery_unknown")
        self.assertEqual(p.dispatch()["state"],"already_attempted")
        self.assertEqual(p._json.call_count,2)

    def test_only_same_run_matches(self):
        p = Probe(3399, "A"*32, "CAS-PROBE-test")
        empty={"lines":[{"name":RULE}],"runs":[]}
        unrelated={"lines":empty["lines"],"runs":[{"id":7,"rule":RULE,"state":"done","summary":"CAS-PROBE-test"}]}
        done={"lines":empty["lines"],"runs":[{"id":1,"rule":RULE,"state":"done","summary":"CAS-PROBE-test"}]}
        p._json=Mock(side_effect=[empty,{"run":1},unrelated,done])
        self.assertEqual(p.dispatch()["run"],1)
        self.assertEqual(p.status()["state"],"awaiting_receipt")
        self.assertTrue(p.status()["receipt_matches"])
