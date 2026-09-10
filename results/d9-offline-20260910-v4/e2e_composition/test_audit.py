import importlib.util
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("d9_e2e_composition_audit", HERE / "audit.py")
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class CompositionAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = MODULE.audit()

    def test_scope_is_the_fully_valid_cpu_cohort(self):
        self.assertEqual(self.result["scope"]["cpu_cases"], 43)
        self.assertEqual(self.result["scope"]["independent_instances"], 22)
        self.assertFalse(self.result["scope"]["protected_labels_opened"])
        self.assertFalse(self.result["scope"]["retry_invalid_cpu_cases_included"])
        self.assertEqual(self.result["status"], "unsupported_sequential_composition")

    def test_observed_interval_union_closes_outer_wall_without_prediction(self):
        aggregate = self.result["aggregate"]
        self.assertAlmostEqual(aggregate["combined_gap_ms"], 0.0, places=6)
        self.assertEqual(aggregate["outer_wrapper_exact_duplicates"], 43)
        self.assertLess(aggregate["eligible_target_union_ms"], aggregate["outer_ms"])
        self.assertGreater(aggregate["eligible_target_sum_ms"], aggregate["eligible_target_union_ms"])

    def test_representative_reconstruction_records_nested_additive_failure(self):
        representative = self.result["representative_reconstruction"]
        self.assertEqual(representative["queue_ordinal"], 1)
        self.assertAlmostEqual(representative["combined_gap_ms"], 0.0, places=6)
        self.assertGreater(representative["naive_to_outer_ratio"], 1.5)
        self.assertGreater(representative["observation_components"]["model_request"]["sum_ms"], 0.0)

    def test_normalized_native_join_and_raw_identity_are_bounded(self):
        native = self.result["native_join"]
        self.assertEqual(native["native_request_count"], 1784)
        self.assertEqual(native["exact_native_to_model_request_joins"], 1784)
        self.assertEqual(native["native_join_missing_or_duplicate"], 0)
        self.assertEqual(native["model_client_physical_id_intersection_count"], 0)
        self.assertEqual(native["model_client_pre_event_id_intersection_count"], 0)
        self.assertEqual(native["native_phase_interval_duplicate_requests"], 1784)
        self.assertEqual(native["native_phase_sum_lt_e2e_requests"], 1784)

        raw = self.result["raw_join"]
        self.assertEqual(raw["exact_parent_logical_client_span_joins"], 1784)
        self.assertEqual(raw["native_physical_id_joins"], 1784)
        self.assertEqual(raw["model_request_rows_within_outer"], 1784)
        self.assertEqual(raw["model_request_rows_on_cpu_outer_clock"], 1784)
        self.assertEqual(raw["native_e2e_rows_fitting_local_model_request_duration"], 1784)
        self.assertEqual(raw["model_request_rows_outside_local_client_interval"], 75)
        self.assertEqual(
            self.result["decision"]["trace_conditioned_envelope"],
            "supported_offline_identity_and_duration_bound",
        )

    def test_nested_lifecycle_pairs_are_reported(self):
        pairs = self.result["important_overlap_totals"]
        get_state_runtime = pairs["lifecycle:get_state|runtime_command"]
        setup_startup = pairs["lifecycle:setup|lifecycle:startup"]
        self.assertEqual(get_state_runtime["overlapping_event_pairs"], 1823)
        self.assertEqual(setup_startup["overlapping_event_pairs"], 43)
        self.assertGreater(get_state_runtime["overlap_ms"], 0.0)


if __name__ == "__main__":
    unittest.main()
