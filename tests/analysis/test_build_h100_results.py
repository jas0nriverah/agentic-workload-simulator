"""Contract tests for the canonical H100 results package builder."""

from __future__ import annotations

import csv
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BUILDER_PATH = ROOT / "scripts/analysis/build_h100_results.py"
SPEC = importlib.util.spec_from_file_location("build_h100_results", BUILDER_PATH)
assert SPEC is not None and SPEC.loader is not None
BUILDER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILDER)


class CanonicalH100ResultsContractTest(unittest.TestCase):
    def _build(self, output_dir: Path) -> dict:
        return BUILDER.build(ROOT, output_dir)

    @staticmethod
    def _csv_row_count(path: Path) -> int:
        with path.open(newline="") as stream:
            return sum(1 for _ in csv.DictReader(stream))

    def test_canonical_counts_and_simulator_mape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            canonical = self._build(Path(temporary))

            lite = canonical["primary_evaluation_cohorts"]["lite"]
            self.assertEqual(
                {
                    key: lite[key]
                    for key in (
                        "selected_instances",
                        "submitted_instances",
                        "completed_instances",
                        "resolved_instances",
                        "unresolved_instances",
                        "empty_patch_instances",
                        "incomplete_instances",
                    )
                },
                {
                    "selected_instances": 32,
                    "submitted_instances": 32,
                    "completed_instances": 32,
                    "resolved_instances": 8,
                    "unresolved_instances": 24,
                    "empty_patch_instances": 0,
                    "incomplete_instances": 0,
                },
            )

            verified = canonical["primary_evaluation_cohorts"]["verified"]
            self.assertEqual(
                {
                    key: verified[key]
                    for key in (
                        "selected_instances",
                        "submitted_instances",
                        "completed_instances",
                        "resolved_instances",
                        "unresolved_instances",
                        "empty_patch_instances",
                        "incomplete_instances",
                    )
                },
                {
                    "selected_instances": 32,
                    "submitted_instances": 30,
                    "completed_instances": 29,
                    "resolved_instances": 10,
                    "unresolved_instances": 19,
                    "empty_patch_instances": 1,
                    "incomplete_instances": 2,
                },
            )

            inventory = canonical["canonical_unique_completed_inventory"]
            self.assertEqual(inventory["lite_count"], 32)
            self.assertEqual(inventory["verified_count"], 29)
            self.assertEqual(
                canonical["simulator"]["calibration_records"],
                4,
            )
            self.assertEqual(canonical["simulator"]["holdout_records"], 2)
            self.assertAlmostEqual(
                canonical["simulator"]["mean_absolute_percentage_error"],
                10.715632310651648,
                places=12,
            )

    def test_plot_ready_csv_row_counts(self) -> None:
        expected_rows = {
            "population_runs.csv": 64,
            "evaluation_attempts.csv": 72,
            "repository_coverage.csv": 23,
            "sweep_results.csv": 16,
            "kineto_matrix.csv": 6,
            "service_calibration.csv": 3,
            "observability_summary.csv": 6,
            "claim_provenance.csv": 10,
            "exclusions.csv": 10,
        }
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            self._build(output)
            for filename, expected in expected_rows.items():
                with self.subTest(filename=filename):
                    self.assertEqual(self._csv_row_count(output / filename), expected)

            with (output / "source_inventory.csv").open(newline="") as stream:
                self.assertGreaterEqual(sum(1 for _ in csv.DictReader(stream)), 1)

    def test_rebuild_is_byte_for_byte_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_dir = root / "first"
            second_dir = root / "second"
            first = self._build(first_dir)
            second = self._build(second_dir)
            self.assertEqual(first, second)

            first_files = sorted(path.name for path in first_dir.iterdir())
            second_files = sorted(path.name for path in second_dir.iterdir())
            self.assertEqual(first_files, second_files)
            for filename in first_files:
                with self.subTest(filename=filename):
                    self.assertEqual(
                        (first_dir / filename).read_bytes(),
                        (second_dir / filename).read_bytes(),
                    )

            # Keep this assertion explicit: the machine-readable index must
            # remain valid JSON, not merely a deterministic byte stream.
            json.loads((first_dir / "canonical_results.json").read_text())


if __name__ == "__main__":
    unittest.main()
