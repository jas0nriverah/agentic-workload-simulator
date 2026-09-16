#!/usr/bin/env python3
"""Focused checks for the bounded D9 figure packet."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("d9_figure_builder", HERE / "build_figures.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FigurePacketTests(unittest.TestCase):
    def test_identity_gated_cohorts_and_metrics(self) -> None:
        data = MODULE._load_data()
        self.assertEqual(len(data["ratio_rows"]), 819)
        self.assertEqual(len(data["d3_rows"]), 615)
        self.assertEqual(data["outcome_match_counts"], {"exact_run_id": 615, "unmatched_train_run_id": 204})
        self.assertEqual(sum(row["official_resolved_n"] for row in data["category_aggregates"]), 236)
        self.assertEqual(sum(row["n_runs"] for row in data["category_aggregates"]), 615)
        self.assertEqual(len(data["native_rows"]), 2080)
        self.assertTrue(data["outcome_source_metadata"]["scope_manifest_list_sha256"])

    def test_packaged_join_replays_without_external_outcome_snapshot(self) -> None:
        original = MODULE.HISTORICAL_OUTCOME_CANDIDATES
        scope_module = sys.modules.get("scripts.assignment.historical_analysis_scope")
        if scope_module is None:
            if str(MODULE.ROOT) not in sys.path:
                sys.path.insert(0, str(MODULE.ROOT))
            from scripts.assignment import historical_analysis_scope as scope_module  # type: ignore[no-redef]
        original_scope_loader = scope_module.frozen_scope
        try:
            MODULE.HISTORICAL_OUTCOME_CANDIDATES = ()
            scope_module.frozen_scope = lambda: (_ for _ in ()).throw(AssertionError("packaged replay read the original frozen scope"))
            data = MODULE._load_data()
        finally:
            MODULE.HISTORICAL_OUTCOME_CANDIDATES = original
            scope_module.frozen_scope = original_scope_loader
        self.assertTrue(data["outcome_source_metadata"]["packaged_join"])
        self.assertEqual(len(data["outcome_join"]), 615)
        self.assertEqual(data["outcome_match_counts"]["unmatched_train_run_id"], 204)

    def test_direct_e2e_is_not_composed_from_components(self) -> None:
        data = MODULE._load_data()
        boundaries = {row["boundary"] for row in data["breakdown"]}
        self.assertIn("predicted_direct_e2e_ms", boundaries)
        self.assertIn("predicted_tool_sum_ms", boundaries)
        self.assertIn("predicted_gpu_proxy_sum_ms", boundaries)
        self.assertTrue(data["native_metrics"]["relative_nnls_token"]["target_boundary"].startswith("native:e2e direct request"))


if __name__ == "__main__":
    unittest.main()
