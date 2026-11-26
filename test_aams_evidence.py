import hashlib
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

def image_with_pixels(depth=8,color=2,pixels=b"\0\0\0\0",interlace=0,tail=b""):
    header = struct.pack(">IIBBBBB",1,1,depth,color,0,0,interlace)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR",header) +
            chunk(b"IDAT",zlib.compress(pixels)+tail) + chunk(b"IEND",b""))


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/"qa_runs"
        self.root.mkdir()
        self.library=EvidenceLibrary(self.root)
        manifest=json.dumps({"files":[{"path":"previews/lv01_ch01/ch01_story_preview.gd","sha256":"b"*64}]}).encode()
        (self.root.parent/"source-manifest.json").write_bytes(manifest)
        self.candidate=hashlib.sha256(manifest).hexdigest()
        self.image=image_with_pixels()

    def route(self,name,version=2,sha=None):
        directory=self.root/name
        directory.mkdir()
        def write(file,value):(directory/file).write_text(json.dumps(value),encoding="utf-8")
        write("invocation.json",{"source_manifest_sha256":sha or self.candidate,"story_script_sha256":"b"*64,"engine_exit_code":2,
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

    def test_bound_images_require_legal_header_and_decodable_scanlines(self):
        refs=self.route("normal")+";"+self.route("return")
        frame=self.root/"return"/"last.png"
        good=self.library.inspect(2,refs)["routes"][1]["frame_sha256"]
        for wrong in (image_with_pixels(depth=7), image_with_pixels(color=7),
                      image_with_pixels(color=3), image_with_pixels(interlace=2),
                      image_with_pixels(pixels=b"\x05abc"), image_with_pixels(pixels=b"\0ab"),
                      image_with_pixels(pixels=b"\0abcd"), image_with_pixels(tail=b"trailing stream")):
            with self.subTest(sha=hashlib.sha256(wrong).hexdigest()):
                frame.write_bytes(wrong)
                view=self.library.inspect(2,refs)
                self.assertFalse(view["same_candidate"])
                self.assertEqual(view["routes"][1]["outcome"],"missing")
                with self.assertRaises(ValueError):
                    self.library.inspect(2,refs,"return")
        frame.write_bytes(image_with_pixels(interlace=1,pixels=b"\x04ABC"))
        view=self.library.inspect(2,refs)
        self.assertTrue(view["same_candidate"])
        self.assertNotEqual(view["routes"][1]["frame_sha256"],good)
