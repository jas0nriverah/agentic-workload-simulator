"""Regression checks for bounded OOF uncertainty mechanics."""

from __future__ import annotations

import csv
import importlib.util
from pathlib import Path
import tempfile
import unittest


MODULE = Path(__file__).resolve().with_name("bounded_uncertainty.py")
SPEC = importlib.util.spec_from_file_location("bounded_uncertainty", MODULE)
assert SPEC and SPEC.loader
uncertainty = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(uncertainty)


class BoundedUncertaintyTest(unittest.TestCase):
    def test_cluster_resampling_carries_all_events_of_an_instance(self) -> None:
        # Instance A has two successes; B has one failure. Selecting A twice
        # must contribute both of A's events twice, never a partial trajectory.
        values = [
            {"n": 2, "coarse": 2, "hybrid": 2, "coarse_strict": 1, "hybrid_strict": 1},
            {"n": 1, "coarse": 0, "hybrid": 0, "coarse_strict": 0, "hybrid_strict": 0},
        ]
        result = uncertainty._bootstrap_summary(values, [[0, 0], [1, 1], [0, 1]])
        # The three event coverages are 1, 0, and 2/3; their mean verifies
        # that selected clusters retain their full event counts.
        self.assertAlmostEqual(result["event_weighted_coverage"]["coarse"], 5 / 9)
        self.assertAlmostEqual(result["strict_all_events_gate"]["coarse"], 0.5)

    def test_identity_mismatch_rejects_changed_run_for_same_event(self) -> None:
        header = ["event_id", "run_id", "instance_id", "fold", "original_class", "observed_ms", "prediction"]
        base = ["event-1", "run-a", "instance-a", "0", "read", "100", "100"]
        changed = ["event-1", "run-b", "instance-a", "0", "read", "100", "100"]
        with tempfile.TemporaryDirectory() as temporary:
            paths = [Path(temporary) / name for name in ("coarse.csv", "hybrid.csv")]
            for path, row in zip(paths, (base, changed)):
                with path.open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(header)
                    writer.writerow(row)
            with self.assertRaises(uncertainty.IdentityMismatchError):
                uncertainty.pair_predictions(*paths)


if __name__ == "__main__":
    unittest.main()
