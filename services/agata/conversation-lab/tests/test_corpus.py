import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1] / "evaluators/corpus.py"

class CorpusTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(MODULE.exists(), "read-only corpus module missing")
        spec = importlib.util.spec_from_file_location("corpus", MODULE)
        self.c = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.c)
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)/"data"
        self.raw = self.root/"raw/daily-dialog/source.txt"; self.raw.parent.mkdir(parents=True); self.raw.write_text("synthetic raw")
        self.processed = self.root/"processed/daily-dialog/dialogs.jsonl"; self.processed.parent.mkdir(parents=True)
        records=[{"dialogue_id":str(i),"source":"daily-dialog","turns":[{"speaker":"a","text":"hello "+str(i)},{"speaker":"b","text":"reply "+str(i)}]} for i in range(30)]
        self.processed.write_text("".join(json.dumps(r)+"\n" for r in records))
        self.pointers=self.processed.parent/"quality-review.jsonl"
        self.pointers.write_text("".join(json.dumps({"dialogue_id":str(i),"processed_jsonl_line":i+1})+"\n" for i in range(30)))
        self.manifest=self.root/"runs/manifest.json"; self.manifest.parent.mkdir()
        self.m={"datasets":{"daily-dialog":{"files":[{"path":str(self.raw),"sha256":hashlib.sha256(self.raw.read_bytes()).hexdigest()}],"processed_sha256":hashlib.sha256(self.processed.read_bytes()).hexdigest(),"processed_accepted":30,"processing_status":"PROCESSED"}}}
        self.manifest.write_text(json.dumps(self.m))

    def test_reads_full_pointer_records_without_mutation(self):
        before={p:p.read_bytes() for p in (self.raw,self.processed,self.pointers,self.manifest)}
        records=self.c.read_pointer_dialogues(self.root,"daily-dialog")
        self.assertEqual(len(records),30);self.assertEqual(records[29]["turns"][1]["text"],"reply 29")
        self.assertEqual(before,{p:p.read_bytes() for p in before})

    def test_checksum_mismatch_fails_closed(self):
        self.raw.write_text("corrupted")
        with self.assertRaises(ValueError): self.c.verify_sources(self.root)

    def test_manifest_path_escape_rejected(self):
        outside=Path(self.tmp.name)/"outside";outside.write_text("synthetic raw")
        self.m["datasets"]["daily-dialog"]["files"][0]["path"]=str(outside);self.manifest.write_text(json.dumps(self.m))
        with self.assertRaises(ValueError): self.c.verify_sources(self.root)

    def test_processed_symlink_escape_rejected(self):
        outside=Path(self.tmp.name)/"outside";outside.write_bytes(self.processed.read_bytes());self.processed.unlink();self.processed.symlink_to(outside)
        with self.assertRaises(ValueError): self.c.verify_sources(self.root)

    def test_wrong_pointer_identity_rejected(self):
        self.pointers.write_text(self.pointers.read_text().replace('"dialogue_id": "0"','"dialogue_id": "wrong"'))
        with self.assertRaises(ValueError): self.c.read_pointer_dialogues(self.root,"daily-dialog")

    def test_short_pointer_selection_rejected(self):
        self.pointers.write_text(self.pointers.read_text().splitlines()[0]+"\n")
        with self.assertRaises(ValueError): self.c.read_pointer_dialogues(self.root,"daily-dialog")

    def test_report_write_never_overwrites(self):
        p=self.c.write_report(self.root,"phase2-test","result.json",{"status":"one"})
        with self.assertRaises(FileExistsError): self.c.write_report(self.root,"phase2-test","result.json",{"status":"two"})
        self.assertEqual(json.loads(p.read_text()),{"status":"one"})

    def test_output_escape_rejected(self):
        for run,name in [("../raw","x.json"),("phase2-ok","../../manifest.json")]:
            with self.subTest(run=run),self.assertRaises(ValueError): self.c.write_report(self.root,run,name,{})

    def test_report_symlink_directory_rejected(self):
        outside=Path(self.tmp.name)/"outside";outside.mkdir();(self.root/"runs/phase2-test").symlink_to(outside)
        with self.assertRaises(ValueError): self.c.write_report(self.root,"phase2-test","x.json",{})
        self.assertEqual(list(outside.iterdir()),[])

    def test_redacts_contacts_but_preserves_noncontacts(self):
        self.assertEqual(self.c.redact("Сумма 18 500 000 000 рублей, дата 03.10.2026, заказ 81234567890"),"Сумма 18 500 000 000 рублей, дата 03.10.2026, заказ 81234567890")
        out=self.c.redact("email person@example.com, call +7 (999) 123-45-67, https://example.com/private")
        self.assertNotIn("person@",out);self.assertNotIn("999",out);self.assertNotIn("https://",out)
