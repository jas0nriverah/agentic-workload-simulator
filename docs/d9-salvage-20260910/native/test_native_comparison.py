import copy
import importlib.util
import pathlib
import unittest


HERE = pathlib.Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("native_comparison_under_test", HERE / "run_native_comparison.py")
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class NativeComparisonContractTests(unittest.TestCase):
    def test_design_ignores_latency_and_residual_fields(self):
        row = {
            "prompt_tokens": 1829,
            "completion_tokens": 75,
            "max_output_tokens": 2048,
            "cached_tokens": 1056,
        }
        before = MODULE.design(row, "relative_nnls_token_cache")
        poisoned = copy.deepcopy(row)
        poisoned.update({"observed_ms": 1e99, "residual_ms": 1e99, "e2e_ms": 1e99, "status": "failed"})
        self.assertEqual(before, MODULE.design(poisoned, "relative_nnls_token_cache"))

    def test_zero_target_metrics_use_exact_zero_without_epsilon(self):
        rows = [
            {
                "case_id": "case-a",
                "observed_ms": {"e2e": 0.0},
                "predictions": {"hardware_domain_median": {"e2e": 0.0}},
            },
            {
                "case_id": "case-b",
                "observed_ms": {"e2e": 0.0},
                "predictions": {"hardware_domain_median": {"e2e": 1.0}},
            },
        ]
        result = MODULE.metric(rows, "hardware_domain_median", "e2e")
        self.assertEqual(result["zero_target_scored_count"], 2)
        self.assertEqual(result["zero_target_exact_pass_count"], 1)
        self.assertEqual(result["infinite_error_count"], 1)
        self.assertEqual(result["within25_count"], 1)
        self.assertEqual(result["p95_status"], "infinite_due_to_zero_target_nonzero_prediction")
        self.assertIsNone(result["p95_ape_percent_nearest_rank"])

    def test_instance_folds_are_grouped_and_cover_five_folds(self):
        instances = [f"repo__case-{index}" for index in range(25)]
        folds = {instance: MODULE.fold_for_instance(instance) for instance in instances}
        self.assertEqual(set(folds.values()), set(range(5)))
        self.assertEqual(MODULE.fold_for_instance(instances[0]), MODULE.fold_for_instance(instances[0]))

    def test_reference_nnls_dependency_is_nonnegative(self):
        rows = [
            {"prompt_tokens": 0, "completion_tokens": index * 1000, "max_output_tokens": 2048, "observed_ms": 2.0 + 3.0 * index}
            for index in range(1, 31)
        ]
        model = MODULE._fit_regression(rows, "relative_nnls_token")
        self.assertEqual(model["status"], "fitted")
        self.assertTrue(all(value >= 0 for value in model["coefficients"]))

    def test_scaled_linear_design_recovers_completion_law(self):
        rows = [
            {"prompt_tokens": 1000, "completion_tokens": index * 1000, "max_output_tokens": 2048, "observed_ms": 5.0 + 2.0 * index}
            for index in range(1, 31)
        ]
        model = MODULE._fit_regression(rows, "relative_nnls_token")
        prediction = MODULE.COMPARE.predict(model["coefficients"], MODULE.design(rows[10], "relative_nnls_token"))
        self.assertAlmostEqual(prediction, rows[10]["observed_ms"], places=6)

    def test_cache_design_uses_uncached_prompt_and_context_interaction(self):
        row = {"prompt_tokens": 2000, "cached_tokens": 500, "completion_tokens": 1000, "max_output_tokens": 2048}
        self.assertEqual(MODULE.design(row, "relative_nnls_token_cache", "e2e"), [1.0, 1.5, 1.0, 2.0])
        self.assertEqual(MODULE.design(row, "relative_nnls_token_cache", "prefill"), [1.0, 1.5, 2.0])
        self.assertEqual(MODULE.design(row, "relative_nnls_token_cache", "decode"), [1.0, 1.0, 2.0])


if __name__ == "__main__":
    unittest.main()
