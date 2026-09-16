"""Focused checks for the fresh production candidate inventories."""

from __future__ import annotations

import json
import unittest
from collections import Counter
from pathlib import Path

from scripts.assignment.generate_production_candidate_inventories import (
    CANDIDATES,
    FINAL_CONFIGURATION_KEYS,
    PRODUCTION_CASE_COUNT,
    PRODUCTION_GRIDS,
    STEP1_CASE_COUNT,
    STEP2_CASE_COUNT,
    _read_jsonl,
    build_candidate_inventory,
)


ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = (
    ROOT.parent
    / "h100-assignment-work-20260905"
    / "assignment"
    / "submission"
    / "20260908T140000Z-offline-v2"
)
TEMPLATE = SNAPSHOT / "live-plan" / "full_matrix_case_inventory.jsonl"


class ProductionCandidateInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.header, cls.cases, cls.source_sha256 = _read_jsonl(TEMPLATE)

    def test_each_candidate_is_a_fresh_complete_matrix(self):
        for candidate in CANDIDATES:
            rows = build_candidate_inventory(
                source_header=self.header,
                source_cases=self.cases,
                candidate=candidate,
                source_sha256=self.source_sha256,
            )
            header, cases = rows[0], rows[1:]
            self.assertEqual(len(cases), PRODUCTION_CASE_COUNT)
            self.assertEqual(header["candidate_id"], candidate["candidate_id"])
            self.assertEqual(header["plan_id"], "assignment-production-v2-20260908")
            self.assertTrue(all(item["fresh_case_id"] for item in cases))
            self.assertTrue(all(item["case_id"].startswith("assignment-production-v2:") for item in cases))
            self.assertEqual(len({item["case_id"] for item in cases}), PRODUCTION_CASE_COUNT)
            self.assertEqual(sum(item["cell_id"] == "shared-baseline" for item in cases), STEP1_CASE_COUNT)
            self.assertEqual(sum(item["cell_id"] != "shared-baseline" for item in cases), STEP2_CASE_COUNT)
            self.assertEqual(
                {key for item in cases for key in item["settings"]},
                set(FINAL_CONFIGURATION_KEYS),
            )
            self.assertTrue(all(item["resume_key"] == item["case_id"] for item in cases))
            self.assertTrue(all(item["historical_template_case_id"].startswith("assignment-case-v1:") for item in cases))
            self.assertTrue(all(item["serving_configuration"] == {"max_model_len": 65536, "vllm_version": "0.10.0"} for item in cases))

    def test_call_grid_and_other_sweeps_have_three_nonbaseline_values(self):
        for candidate in CANDIDATES:
            rows = build_candidate_inventory(
                source_header=self.header,
                source_cases=self.cases,
                candidate=candidate,
                source_sha256=self.source_sha256,
            )[1:]
            baseline = next(item["settings"] for item in rows if item["cell_id"] == "shared-baseline")
            for knob, grid in PRODUCTION_GRIDS.items():
                values = [
                    item["settings"][knob]
                    for item in rows
                    if isinstance(item.get("variation"), dict) and item["variation"].get("knob") == knob
                ]
                self.assertEqual(Counter(values), Counter({value: 24 for value in grid if value != baseline[knob]}))
                self.assertEqual(set(values), set(grid) - {baseline[knob]})

    def test_holdout_lineage_is_preserved_without_outcome_fields(self):
        rows = build_candidate_inventory(
            source_header=self.header,
            source_cases=self.cases,
            candidate=CANDIDATES[0],
            source_sha256=self.source_sha256,
        )[1:]
        holdout = [item for item in rows if item["instance_id"] == "sympy__sympy-12481"]
        self.assertEqual(len(holdout), 2)
        self.assertTrue(all(item["selection_outcome_blind"] for item in holdout))
        self.assertTrue(all("official_resolved" not in item and "status" not in item for item in holdout))


if __name__ == "__main__":
    unittest.main()
