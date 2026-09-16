import copy
import importlib
import unittest


REVIEW = importlib.import_module("scripts.assignment.d9_cpu_review")


def _traj(run, instance, repo="repo"):
    return {
        "run_id": run,
        "instance_id": instance,
        "repository": repo,
        "observed_ms": 100.0,
        "tool_wall_ms": 40.0,
        "model_wall_ms": 40.0,
    }


def _tool(run, instance, event, observed=10.0, repo="repo"):
    return {
        "run_id": run,
        "instance_id": instance,
        "repository": repo,
        "event_id": event,
        "operation_class": "read",
        "observed_ms": observed,
        "command_sha256": "x",
    }


class D9CpuReviewTests(unittest.TestCase):
    def test_metadata_quarantine_happens_before_label_validation(self):
        raw = {
            "holdout_excluded": "holdout",
            "trajectories": [_traj("good", "i"), _traj("bad", REVIEW.QUARANTINED_INSTANCE)],
            "tools": [
                _tool("good", "i", "good-event"),
                dict(_tool("bad", REVIEW.QUARANTINED_INSTANCE, "bad-event"), observed_ms=float("nan")),
            ],
            "models": [],
        }
        filtered, summary = REVIEW._metadata_only_quarantine(raw)
        REVIEW._validate_retained_labels(filtered)
        self.assertEqual(summary["excluded_counts"]["tools"], 1)
        self.assertEqual([row["event_id"] for row in filtered["tools"]], ["good-event"])

    def test_metrics_reject_nonpositive_predictions_and_report_requested_fields(self):
        result = REVIEW.metrics([(10.0, 10.0), (20.0, 10.0)])
        self.assertEqual(result["n"], 2)
        self.assertIn("p95_ape", result)
        self.assertIn("signed_error_total_ms", result)
        self.assertIn("aggregate_signed_relative_error", result)
        self.assertIn("aggregate_absolute_error_ms", result)
        with self.assertRaises(ValueError):
            REVIEW.metrics([(0.0, 10.0)])
        with self.assertRaises(ValueError):
            REVIEW.metrics([(10.0, 0.0)])

    def test_nnls_overhead_is_nonnegative_and_observed_target_only(self):
        fit = REVIEW.fit_nnls_overhead(
            [1.0, 2.0, 3.0, 4.0],
            [4.0, 3.0, 2.0, 1.0],
            [17.0, 19.0, 21.0, 23.0],
        )
        self.assertTrue(all(value >= 0.0 for value in fit["coefficients"]))
        self.assertTrue(fit["fit_uses_observed_event_sums_only"])
        self.assertEqual(fit["features"], ["intercept", "n_tools", "n_models"])

    def test_instance_grouped_support_has_no_shared_instances(self):
        trajectories = {
            "a": _traj("a", "i-a"),
            "b": _traj("b", "i-b"),
            "c": _traj("c", "i-c"),
            "d": _traj("d", "i-d"),
            "e": _traj("e", "i-e"),
        }
        tools = {key: [_tool(key, value["instance_id"], key + "-e")] for key, value in trajectories.items()}
        models = {key: [] for key in trajectories}
        support = REVIEW._fold_support("instance_id_grouped", trajectories, tools, models)
        self.assertTrue(support["no_shared_instance_between_instance_grouped_folds"])
        self.assertTrue(all(row["shared_instance_count"] == 0 for row in support["folds"].values()))

    def test_joint_event_and_e2e_gate_is_boolean_intersection(self):
        # Run a has passing tool events but failing E2E; run b has failing tool
        # events but passing E2E. Multiplying marginal rates would be nonzero,
        # while the required per-run conjunction is zero.
        tools = [
            {"run_id": "a", "ape": 0.0},
            {"run_id": "b", "ape": 100.0},
        ]
        models = [
            {"run_id": "a", "ape": 0.0},
            {"run_id": "b", "ape": 0.0},
        ]
        trajectories = [
            {"run_id": "a", "direct": 200.0, "observed_e2e_ms": 100.0},
            {"run_id": "b", "direct": 100.0, "observed_e2e_ms": 100.0},
        ]
        result = REVIEW._combined_event_e2e_gate_rates(
            tools, models, trajectories, e2e_key="direct"
        )
        self.assertEqual(result["allpass_and_e2e_gate_rate"], 0.0)

    def test_overhead_e2e_is_direct_sum_plus_overhead_component(self):
        direct = 110.0
        overhead_component = 25.0
        overhead_e2e = REVIEW._prediction(direct + overhead_component, "overhead e2e")
        self.assertEqual(overhead_e2e, 135.0)
        self.assertNotEqual(overhead_e2e, overhead_component)


if __name__ == "__main__":
    unittest.main()
