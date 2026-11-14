import tempfile
import unittest
from pathlib import Path
from pipeline import connect, ingest, reconcile

HEADER = "firm_id,payment_id,version,amount_cents\n"


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = connect(self.root / "ledger.db")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def load(self, name, rows):
        path = self.root / name
        path.write_text(HEADER + rows, encoding="utf-8")
        return ingest(self.db, path)

    def test_exact_rerun_is_noop(self):
        self.load("a.csv", "a,p1,1,100\n")
        result = self.load("a.csv", "a,p1,1,100\n")
        self.assertEqual(result["status"], "already_loaded")
        self.assertEqual(reconcile(self.db, {"a": 100})[0]["status"], "matched")

    def test_cross_batch_duplicates_and_firm_keys(self):
        self.load("a.csv", "a,p1,1,100\n")
        result = self.load("b.csv", "a,p1,1,100\nb,p1,1,200\n")
        self.assertEqual(result["duplicates"], 1)
        self.assertTrue(all(r["status"] == "matched" for r in reconcile(self.db, {"a":100,"b":200})))

    def test_late_older_version_does_not_overwrite_correction(self):
        self.load("new.csv", "a,p1,2,150\n")
        self.load("old.csv", "a,p1,1,100\n")
        self.assertEqual(reconcile(self.db, {"a":150})[0]["status"], "matched")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM payments").fetchone()[0], 2)

    def test_invalid_rows_are_quarantined(self):
        result = self.load("bad.csv", "a,p1,1,100\na,p2,1,1.50\na,p3,0,5\n")
        self.assertEqual((result["accepted"], result["rejected"]), (1, 2))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM rejects").fetchone()[0], 2)
        self.assertEqual(reconcile(self.db, {"a":200})[0]["difference_cents"], -100)

    def test_conflict_rolls_back_entire_batch(self):
        self.load("a.csv", "a,p1,1,100\n")
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            self.load("conflict.csv", "a,p2,1,200\na,p3,1,bad\na,p1,1,999\n")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM payments").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM batches").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM rejects").fetchone()[0], 0)

    def test_schema_mismatch_does_not_create_receipt(self):
        source = self.root / "wrong.csv"
        source.write_text("unexpected\n100\n")
        with self.assertRaisesRegex(ValueError, "Schema mismatch"):
            ingest(self.db, source)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM batches").fetchone()[0], 0)

    def test_missing_and_unmapped_firms_are_exceptions(self):
        self.load("a.csv", "a,p1,1,100\n")
        rows = reconcile(self.db, {"b": 500})
        self.assertEqual([r["status"] for r in rows], ["unmapped", "mismatch"])

    def test_corrected_rejected_record_can_be_loaded(self):
        self.load("bad.csv", "a,p1,1,broken\n")
        self.load("fixed.csv", "a,p1,1,500\n")
        self.assertEqual(reconcile(self.db, {"a":500})[0]["status"], "matched")

    def test_boolean_control_total_rejected(self):
        with self.assertRaises(ValueError):
            reconcile(self.db, {"a": True})


if __name__ == "__main__":
    unittest.main()
