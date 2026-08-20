import tempfile
import unittest
from pathlib import Path

from scripts.validation.normalize_sweagent_trajectory import normalize_document
from scripts.validation.summarize_first_control import summarize
from tests.validation.test_normalize_sweagent_trajectory import NormalizeSWEAgentTrajectoryTests
from scripts.validation.validate_normalized_trajectory import _read_rows


class SummarizeFirstControlTests(unittest.TestCase):
    def test_summary_is_payload_free_and_keeps_timing_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = NormalizeSWEAgentTrajectoryTests().make_source(root)
            output = root / "normalized.jsonl"
            normalize_document(source, output, run_id="run-1", attempt_id="a", instance_id="i")
            result = summarize(_read_rows(output))
            self.assertEqual(result["status"], "measured")
            self.assertEqual(result["counts"]["tool_executions"], 1)
            self.assertEqual(result["tool_action_families"], {"tool": 1})
            self.assertEqual(result["timing_boundary"]["request_level_timestamps"], "unavailable")
            self.assertFalse(result["timing_boundary"]["gpu_time_claim"])
            self.assertNotIn("raw_record", result)
            self.assertFalse(result["submission_present"])


if __name__ == "__main__":
    unittest.main()
