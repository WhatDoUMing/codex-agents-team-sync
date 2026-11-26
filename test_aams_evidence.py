import json
from pathlib import Path
import struct
import tempfile
import unittest
import zlib

from aams_evidence import EvidenceLibrary


def chunk(kind,data):
    body=kind+data
    return struct.pack(">I",len(data))+body+struct.pack(">I",zlib.crc32(body))


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/"qa_runs"
        self.root.mkdir()
        self.library=EvidenceLibrary(self.root)
        self.image=(b"\x89PNG\r\n\x1a\n"+chunk(b"IHDR",struct.pack(">IIBBBBB",1,1,8,2,0,0,0))+
                    chunk(b"IDAT",zlib.compress(b"\0\0\0\0"))+chunk(b"IEND",b""))

    def route(self,name,version=2,sha="a"*64):
        directory=self.root/name
        directory.mkdir()
        def write(file,value):(directory/file).write_text(json.dumps(value),encoding="utf-8")
        write("invocation.json",{"source_manifest_sha256":sha,"story_script_sha256":"b"*64,"engine_exit_code":2,
                                 "arguments":["--",str(directory),name,"v"+str(version)]})
        write("summary.json",{"route":name,"instruction_version":"v"+str(version),"screenshots":["last.png"],
                              "capture_errors":[],"key_events_sent":1,"key_events_observed":1,"external_key_events":0,
                              "first_stop":{"expectation":"内门"},"outcome":"route_stopped"})
        (directory/"last.png").write_bytes(self.image)
        (directory/"path.jsonl").write_text('{"hero":{"x":2}}')
        (directory/"events.jsonl").write_text('{"event":"key_sent"}\n{"event":"key_received"}')
        return "qa_runs/"+name+"/summary.json"

    def test_two_supplied_routes_are_inspected_and_openable(self):
        refs=self.route("normal")+"；"+self.route("return")
        view=self.library.inspect(2,refs)
        self.assertTrue(view["same_candidate"])
        self.assertEqual([r["outcome"] for r in view["routes"]],["route_stopped","route_stopped"])
        self.assertEqual(self.library.inspect(2,refs,"return"),self.image)
        self.assertFalse(self.library.inspect(1,refs)["same_candidate"])
        (self.root/"return"/"last.png").write_bytes(b"not a PNG")
        self.assertFalse(self.library.inspect(2,refs)["same_candidate"])

    def test_missing_wrong_candidate_or_outside_paths_never_validate(self):
        normal=self.route("normal")
        self.assertFalse(self.library.inspect(2,normal)["same_candidate"])
        ret=self.route("return",sha="c"*64)
        self.assertFalse(self.library.inspect(2,normal+";"+ret)["same_candidate"])
        for outside in ("../../summary.json","missing/summary.json","//unavailable.test/share/summary.json",normal+";"+normal):
            self.assertFalse(self.library.inspect(2,outside)["same_candidate"])
