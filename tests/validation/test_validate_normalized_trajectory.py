import json
import tempfile
import unittest
from pathlib import Path

from scripts.validation.normalize_sweagent_trajectory import normalize_document
from scripts.validation.validate_normalized_trajectory import _read_rows, validate_rows
from tests.validation.test_normalize_sweagent_trajectory import NormalizeSWEAgentTrajectoryTests


class ValidateNormalizedTrajectoryTests(unittest.TestCase):
    def make_index(self, root: Path) -> tuple[Path, str]:
        source = NormalizeSWEAgentTrajectoryTests().make_source(root)
        output = root / "normalized.jsonl"
        summary = normalize_document(
            source, output, run_id="run-1", attempt_id="attempt-001", instance_id="i"
        )
        return output, summary["source_sha256"]

    def test_accepts_lossless_index_and_expected_source_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            output, source_sha = self.make_index(Path(directory))
            summary = validate_rows(_read_rows(output), expected_source_sha256=source_sha)
            self.assertEqual(summary["status"], "PASS")
            self.assertEqual(summary["record_counts"]["tool_execution"], 1)
            self.assertEqual(summary["request_level_timestamps"], "unavailable")

    def test_rejects_count_or_raw_record_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            output, source_sha = self.make_index(Path(directory))
            rows = _read_rows(output)
            manifest = next(row for row in rows if row["record_type"] == "manifest")
            manifest["counts"]["history_messages"] += 1
            with self.assertRaisesRegex(ValueError, "history_message count mismatch"):
                validate_rows(rows, expected_source_sha256=source_sha)

            rows = _read_rows(output)
            step = next(row for row in rows if row["record_type"] == "trajectory_step")
            step["raw_record"]["action"] = "tampered"
            with self.assertRaisesRegex(ValueError, "raw record hash mismatch"):
                validate_rows(rows, expected_source_sha256=source_sha)

    def test_cli_input_remains_jsonl_objects(self):
        with tempfile.TemporaryDirectory() as directory:
            output, _ = self.make_index(Path(directory))
            self.assertTrue(all(isinstance(json.loads(line), dict) for line in output.read_text().splitlines()))


if __name__ == "__main__":
    unittest.main()
