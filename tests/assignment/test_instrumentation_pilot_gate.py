"""Synthetic integrity/advisory tests; fixtures are never live pilot evidence."""
import copy
import unittest
import json
from pathlib import Path

from scripts.validation.check_instrumentation_pilot import SCHEMA, assess, verify_acquisition_proofs


def fixture():
    ids = [f"synthetic-case-{i}" for i in range(16)]
    rows = []
    pairs = []
    for case in ids:
        row = {"case_id": case, "execution_status": "completed", "status": "pass", "internal_consistency_errors": [], "tool_events": 3,
               "physical_requests": 4, "outer_wall_ms": 1000, "attributed_union_ms": 970,
               "individual_cpu_operation_records": 20, "raw_model_request_records": 4,
               "unknown_wall_ms": 30, "closure_error_ms": 0,
               "attribution_excludes_unknown_and_outer_wrappers": True}
        for key in ("missing_pre_actions", "missing_terminal_tools", "missing_terminal_requests", "duplicate_ids", "negative_intervals", "unlinked_retries", "request_mutations", "feature_parity_mismatches", "future_feature_violations", "dropped_cpu_records", "cpu_capture_map_failures", "missing_raw_request_bodies"):
            row[key] = 0
        rows.append(row)
    replay_ids = ["synthetic-file-workload", "synthetic-test-workload", "synthetic-short-model", "synthetic-long-model"]
    for case in replay_ids:
        for repeat in range(3):
            pairs.append({"case_id": case, "repeat": repeat, "order": "AB" if repeat % 2 == 0 else "BA",
                          "control_wall_ms": 1000, "instrumented_wall_ms": 1020,
                          "workload_sha256": "a" * 64, "control_workload_sha256": "a" * 64,
                          "instrumented_workload_sha256": "a" * 64,
                          "same_serving_and_cache_policy": True, "full_production_capture_enabled": True})
    return {"schema_version": SCHEMA, "evidence_kind": "live", "selected_case_ids": ids,
            "case_summaries": rows, "replay_case_ids": replay_ids, "overhead_pairs": pairs,
            "baseline": {"call_limit": 30, "max_output_tokens": 2048, "observation_length": 100000, "temperature": 0, "max_input_tokens": 32768, "top_p": 1, "seed": 0},
            "frozen_pilot_configuration": {"call_limit": 30, "max_output_tokens": 2048, "observation_length": 100000, "temperature": 0, "max_input_tokens": 32768, "top_p": 1, "seed": 0},
            "review": {key: True for key in ("offline_tests_passed", "instrumentation_tests_passed", "remote_artifacts_reconciled", "remote_pins_verified", "legacy_process_noninterference_verified", "useful_live_workload_descriptors_verified", "train_serve_projection_reviewed", "holdout_isolation_verified", "external_event_coverage_verified", "e2e_attribution_improvement_verified", "evaluator_correctness_verified", "interruption_resume_verified", "literal_pdf_acquisition_verified", "historical_failure_regressions_verified", "individual_cpu_records_verified", "raw_model_records_verified")}}


class PilotGateTests(unittest.TestCase):
    def test_threshold_pass_never_launches(self):
        result = assess(fixture())
        self.assertEqual(result["status"], "pass", result)
        self.assertFalse(result["launch_authorized"])

    def test_residual_arithmetic_does_not_count_as_attribution(self):
        value = fixture()
        value["case_summaries"][0].update(attributed_union_ms=480, unknown_wall_ms=520)
        result = assess(value)
        self.assertEqual(result["status"], "pass")
        self.assertTrue(any("UNKNOWN" in item for item in result["advisories"]))
        self.assertIn("requires_main_review", result["measurement_representativeness"])

    def test_missing_retry_or_preaction_blocks(self):
        for key in ("unlinked_retries", "missing_pre_actions", "missing_terminal_requests", "future_feature_violations"):
            value = fixture()
            value["case_summaries"][0][key] = 1
            self.assertEqual(assess(value)["status"], "fail", key)

    def test_high_tail_overhead_is_visible_without_an_invented_pdf_threshold(self):
        value = fixture()
        for pair in value["overhead_pairs"][:3]:
            pair["instrumented_wall_ms"] = 1200
        result = assess(value)
        self.assertGreater(result["p95_overhead_percent"], 10)
        self.assertEqual(result["status"], "pass")
        self.assertTrue(any("p95" in item for item in result["advisories"]))
        self.assertFalse(result["launch_authorized"])

    def test_baseline_payload_and_preselection_are_binding(self):
        original = fixture()
        mutations = [lambda v: v["baseline"].update(observation_length=25000),
                     lambda v: v["overhead_pairs"][0].update(instrumented_workload_sha256="b" * 64),
                     lambda v: v["selected_case_ids"].__setitem__(0, "other"),
                     lambda v: v["review"].pop("evaluator_correctness_verified")]
        for mutate in mutations:
            value = copy.deepcopy(original)
            mutate(value)
            self.assertEqual(assess(value)["status"], "fail")

    def test_engineering_campaign_count_and_remote_copy_do_not_block(self):
        value = fixture()
        value["selected_case_ids"] = value["selected_case_ids"][:1]
        value["case_summaries"] = value["case_summaries"][:1]
        value["review"].pop("remote_artifacts_reconciled")
        result = assess(value)
        self.assertEqual(result["status"], "pass", result)
        self.assertTrue(any("16 cases" in item for item in result["advisories"]))
        self.assertTrue(any("remote_artifacts_reconciled" in item for item in result["advisories"]))

    def test_synthetic_artifacts_cannot_pass_live_gate(self):
        value = fixture()
        value["evidence_kind"] = "synthetic"
        self.assertEqual(assess(value)["status"], "fail")

    def test_aggregate_only_or_missing_raw_attempt_evidence_blocks(self):
        for key, invalid in (("individual_cpu_operation_records", None), ("raw_model_request_records", 3),
                             ("dropped_cpu_records", 1), ("cpu_capture_map_failures", 1),
                             ("missing_raw_request_bodies", 1), ("dropped_cpu_records", False)):
            value = fixture()
            value["case_summaries"][0][key] = invalid
            self.assertEqual(assess(value)["status"], "fail", key)
        value = fixture()
        value["overhead_pairs"][0]["full_production_capture_enabled"] = False
        self.assertEqual(assess(value)["status"], "fail")

    def test_no_tool_trajectory_does_not_fabricate_positive_operation_count(self):
        value = fixture()
        value["case_summaries"][0].update(tool_events=0, individual_cpu_operation_records=0)
        result = assess(value)
        self.assertEqual(result["status"], "pass", result)
        self.assertTrue(any("not exercised" in item for item in result["advisories"]))

    def test_complete_source_bound_acquisition_and_regression_proof_required(self):
        contract = json.loads((Path(__file__).parents[2] / "configs/assignment_acquisition_contract.v2.json").read_text())
        payloads = {"acquisition_contract": contract}
        hashes = {"acquisition_contract": "a" * 64, "source_bundle": "b" * 64}
        for role, schema, field, items in (
            ("acquisition_proof", "assignment.acquisition-proof.v2", "requirements", contract["requirements"]),
            ("historical_regression_proof", "assignment.historical-regression-proof.v2", "regressions", contract["historical_regressions"]),
        ):
            payloads[role] = {"schema_version": schema, "evidence_kind": "live_prelaunch", "contract_sha256": "a" * 64,
                "source_bundle_sha256": "b" * 64, field: [dict(id=r["id"], status="pass", artifact_roles=["witness"],
                verification="Synthetic gate unit test only", disposition="measured_explicitly",
                remaining_limitation="Final accuracy/transfer is not established by this synthetic fixture") for r in items]}
        verify_acquisition_proofs(payloads, hashes, {"witness"})
        altered = copy.deepcopy(payloads)
        altered["acquisition_proof"]["requirements"].pop()
        with self.assertRaises(ValueError):
            verify_acquisition_proofs(altered, hashes, {"witness"})
        altered = copy.deepcopy(payloads)
        altered["historical_regression_proof"]["regressions"][9]["disposition"] = "validity_limitation"
        with self.assertRaises(ValueError):
            verify_acquisition_proofs(altered, hashes, {"witness"})
        altered = copy.deepcopy(payloads)
        altered["acquisition_proof"]["source_bundle_sha256"] = "c" * 64
        with self.assertRaises(ValueError):
            verify_acquisition_proofs(altered, hashes, {"witness"})


if __name__ == "__main__":
    unittest.main()
