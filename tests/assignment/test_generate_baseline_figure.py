import json
import tempfile
import unittest
from pathlib import Path

from scripts.assignment.generate_baseline_figure import main


class BaselineFigureTests(unittest.TestCase):
    def test_generates_outcome_only_figure_and_metadata(self):
        source = Path("project/h100_results/repository_coverage.csv")
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            old_argv = __import__("sys").argv
            try:
                __import__("sys").argv = [
                    "generate_baseline_figure.py",
                    "--input",
                    str(source),
                    "--output-dir",
                    str(output_dir),
                ]
                self.assertEqual(main(), 0)
            finally:
                __import__("sys").argv = old_argv

            svg = output_dir / "baseline_resolved_rate.svg"
            metadata = output_dir / "baseline_resolved_rate.json"
            self.assertTrue(svg.is_file())
            self.assertTrue(svg.read_text(encoding="utf-8").startswith("<svg "))
            payload = json.loads(metadata.read_text(encoding="utf-8"))
            self.assertEqual(payload["rows"], 23)
            self.assertFalse(payload["timing_data_included"])
            self.assertFalse(payload["full_suite_claim"])
            self.assertEqual(payload["rows_by_suite"], {"lite": 11, "verified": 12})


if __name__ == "__main__":
    unittest.main()
