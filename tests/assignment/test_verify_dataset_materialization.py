import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:
    pa = pq = None

from scripts.assignment import verify_dataset_materialization as verifier


@unittest.skipUnless(pa is not None, "requires the evaluator interpreter's PyArrow")
class DatasetMaterializationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.parquet"
        self.derived = self.root / "rows.jsonl"
        self.rows = [
            {"instance_id": "dev__case-1", "base_commit": "a" * 40,
             "patch": "diff α\n", "test_patch": "test\n", "arbitrary_public_field": None},
            {"instance_id": "dev__case-2", "base_commit": "b" * 40,
             "patch": "diff β\n", "test_patch": "test2\n", "arbitrary_public_field": "kept"},
        ]
        pq.write_table(pa.Table.from_pylist(self.rows), self.source)
        self.raw = b"".join(verifier.canonical(row) + b"\n" for row in self.rows)
        self.derived.write_bytes(self.raw)
        self.source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.derived_hash = hashlib.sha256(self.raw).hexdigest()

    def verify(self, **kwargs):
        return verifier.verify_pair(self.source, self.derived, source_sha256=self.source_hash,
                                    jsonl_sha256=kwargs.get("derived_hash", self.derived_hash), expected_rows=2)

    def test_exact_round_trip_retains_two_hashes_and_every_field(self):
        proof = self.verify()
        self.assertEqual(proof["source"]["sha256"], self.source_hash)
        self.assertEqual(proof["derived"]["sha256"], self.derived_hash)
        self.assertNotEqual(self.source_hash, self.derived_hash)
        self.assertTrue(proof["row_order_and_all_fields_equal"])
        self.assertEqual(set(proof["row_identity_hashes"][0]["source_and_jsonl_field_sha256"]), set(self.rows[0]))

    def test_wrong_source_pin_fails_before_parquet_decode(self):
        self.source_hash = "0" * 64
        with patch.object(pq, "ParquetFile", side_effect=AssertionError("should not decode")):
            with self.assertRaisesRegex(verifier.MaterializationError, "source Parquet SHA-256"):
                self.verify()

    def test_reordering_and_duplicate_rows_fail(self):
        for rows, error in [(self.rows[::-1], "row identity/order"), ([self.rows[0]] * 2, "duplicate JSONL")]:
            with self.subTest(error=error):
                self.derived.write_bytes(b"".join(verifier.canonical(row) + b"\n" for row in rows))
                with self.assertRaisesRegex(verifier.MaterializationError, error):
                    self.verify()

    def test_arbitrary_field_and_patch_changes_fail_even_with_matching_new_file_hash(self):
        for key in ("base_commit", "patch", "test_patch", "arbitrary_public_field"):
            with self.subTest(field=key):
                rows = [dict(row) for row in self.rows]
                rows[0][key] = "changed"
                raw = b"".join(verifier.canonical(row) + b"\n" for row in rows)
                self.derived.write_bytes(raw)
                with self.assertRaisesRegex(verifier.MaterializationError, f"field {key} mismatch"):
                    self.verify(derived_hash=hashlib.sha256(raw).hexdigest())

    def test_noncanonical_serialization_and_wrong_derived_pin_fail(self):
        with self.assertRaisesRegex(verifier.MaterializationError, "derived JSONL SHA-256"):
            self.verify(derived_hash="0" * 64)
        self.derived.write_text("\n".join(json.dumps(row) for row in self.rows) + "\n")
        with self.assertRaisesRegex(verifier.MaterializationError, "canonical ordered materialization"):
            self.verify()

    def test_field_loss_and_duplicate_json_keys_fail(self):
        rows = [dict(row) for row in self.rows]
        del rows[0]["arbitrary_public_field"]
        self.derived.write_bytes(b"".join(verifier.canonical(row) + b"\n" for row in rows))
        with self.assertRaisesRegex(verifier.MaterializationError, "field set mismatch"):
            self.verify()
        self.derived.write_bytes(self.raw.replace(b'{"arbitrary_public_field":', b'{"instance_id":"duplicate","arbitrary_public_field":', 1))
        with self.assertRaisesRegex(verifier.MaterializationError, "duplicate JSON field"):
            self.verify()

    def test_proof_output_refuses_overwrite(self):
        path = self.root / "proof.json"
        digest = verifier.write_proof(path, {"pair": self.verify()})
        original = path.read_bytes()
        self.assertEqual(hashlib.sha256(original).hexdigest(), digest)
        self.assertEqual(path.with_name("proof.json.sha256").read_text().split()[0], digest)
        with self.assertRaisesRegex(verifier.MaterializationError, "overwrite"):
            verifier.write_proof(path, {})
        self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
