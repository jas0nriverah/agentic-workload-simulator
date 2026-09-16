"""Offline plan-package regression tests; these never launch a workload."""

import json
import tempfile
import unittest
from pathlib import Path

from scripts.assignment.generate_instrumentation_plan import (
    BASELINE,
    HOLDOUT_INSTANCE_ID,
    IMMUTABLE_PLAN_SHA256,
    MANDATORY_PILOT_REPOSITORIES,
    _instrumentation_schema,
    _read_jsonl,
    generate_package,
    select_pilot,
    sha256_file,
)


ROOT = Path(__file__).resolve().parents[2]
EXTERNAL = ROOT.parent / "h100-assignment-work-20260905"
LITE = EXTERNAL / "datasets" / "SWE-bench_Lite.jsonl"
VERIFIED = EXTERNAL / "datasets" / "SWE-bench_Verified.jsonl"
CONFIG = ROOT / "configs" / "assignment_steps_1_3.json"


class InstrumentationPlanTests(unittest.TestCase):
    def test_preselection_is_fixed_balanced_and_holdout_free(self):
        lite, _ = _read_jsonl(LITE)
        verified, _ = _read_jsonl(VERIFIED)
        categories, selected = select_pilot({"lite": lite, "verified": verified})
        self.assertEqual(categories, list(MANDATORY_PILOT_REPOSITORIES))
        self.assertEqual(len(selected), 16)
        self.assertEqual([row["suite"] for row in selected].count("lite"), 8)
        self.assertEqual([row["suite"] for row in selected].count("verified"), 8)
        self.assertNotIn(HOLDOUT_INSTANCE_ID, {row["instance_id"] for row in selected})
        self.assertEqual({row["category"] for row in selected}, set(MANDATORY_PILOT_REPOSITORIES))

    def test_schema_uses_integrated_telemetry_and_outer_interval_gate(self):
        schema = _instrumentation_schema()
        self.assertEqual(schema["schema_version"], "assignment.telemetry.v2")
        self.assertEqual(schema["instrumentation_version"], "telemetry-v2-20260908")
        self.assertEqual(schema["stream_schemas"]["lifecycle"], "assignment.telemetry.v2.lifecycle")
        gate = schema["reconciliation"]["successful_case_outer_wall_coverage_gate"]
        self.assertEqual(gate["minimum_fraction"], 0.95)
        self.assertTrue(gate["unknown_excluded_from_numerator"])
        self.assertTrue(gate["category_label_count_is_insufficient"])

    def test_package_preserves_exact_matrix_and_binds_pilot_resume_keys(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = generate_package(
                config_path=CONFIG,
                lite_tasks=LITE,
                verified_tasks=VERIFIED,
                output_dir=Path(temporary) / "live-plan",
                historical_split=EXTERNAL / "assignment" / "submission" / "20260908T010000Z" / "d9" / "split_manifest.json",
            )
            output = Path(result["output_dir"])
            self.assertEqual(result["full_matrix_sha256"], IMMUTABLE_PLAN_SHA256)
            self.assertEqual(result["full_case_count"], 1088)
            pilot = json.loads((output / "pilot_cases.json").read_text(encoding="utf-8"))
            plan_lines = [json.loads(line) for line in (output / "pilot_plan.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(pilot["cases"]), 16)
            self.assertEqual(len(plan_lines), 17)  # header plus the exact 16 case rows
            self.assertEqual(
                {row["case_id"] for row in pilot["cases"]},
                {row["resume_key"] for row in plan_lines[1:]},
            )
            self.assertTrue(all(row["settings"] == BASELINE for row in pilot["cases"]))
            self.assertTrue(all("outcome" not in row and "timing" not in row for row in pilot["cases"]))
            self.assertEqual(sha256_file(output / "full_matrix_case_inventory.jsonl"), IMMUTABLE_PLAN_SHA256)
            bundle = json.loads((output / "source_bundle_manifest.json").read_text(encoding="utf-8"))
            paths = {row["path"] for row in bundle["files"]}
            self.assertIn("telemetry/v2.py", paths)
            self.assertIn("validation/audit_v2_journals.py", paths)


if __name__ == "__main__":
    unittest.main()
