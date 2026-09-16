"""Regression checks for report provenance and unsupported coverage claims."""
import json
import tempfile
import unittest
from pathlib import Path

from scripts.assignment.repair_submission_reports import (
    RepairContractError, downstream_presence, prediction_coverage,
)


class RepairReportTests(unittest.TestCase):
    def test_absent_export_cannot_assert_a_prediction_count(self):
        with tempfile.TemporaryDirectory() as directory:
            result = prediction_coverage(Path(directory))
            self.assertFalse(result["available"])
            self.assertNotIn("baseline_prediction_scope", result)

    def test_recovered_support_updates_count_and_invalid_coverage_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "d9-predicted/d9_predicted_manifest.json"
            path.parent.mkdir()
            value = {
                "coverage": {"baseline_source_runs": 800, "baseline_predicted_runs": 797,
                             "baseline_unknown_runs": 1, "sweep_derived_predicted_rows": 96},
                "holdout_exclusion": {"baseline_source_rows_excluded": 2},
            }
            path.write_text(json.dumps(value))
            result = prediction_coverage(root)
            self.assertEqual(result["baseline_prediction_scope"], "797/800 canonical baseline rows")
            self.assertEqual(result["missing_support_rows"], 1)
            self.assertIn("does not establish remote artifact loss", result["description"])
            value["coverage"]["baseline_unknown_runs"] = 3
            path.write_text(json.dumps(value))
            with self.assertRaises(RepairContractError):
                prediction_coverage(root)

    def test_prediction_presence_uses_exporter_figure_subdirectory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nested = root / "d9-predicted/figures"
            nested.mkdir(parents=True)
            (nested / "step1_repository_ratio.svg").write_text("<svg/>")
            presence = downstream_presence(root)["prediction_export"]
            self.assertEqual(presence["present_count"], 1)
            self.assertEqual(presence["directory"], "d9-predicted/figures")


if __name__ == "__main__":
    unittest.main()
