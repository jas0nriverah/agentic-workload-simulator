import unittest

from agentic_sim.feature_simulator import (
    FEATURE_PREDICTION_SCHEMA,
    FeatureCalibrationRecord,
    FeatureInput,
    FeatureLatencySimulator,
    FeatureSimulatorError,
)


class FeatureLatencySimulatorTests(unittest.TestCase):
    def setUp(self):
        # Labels are present only in calibration rows.  max_output_tokens is a
        # configured budget and intentionally differs from actual output length.
        self.calibration = [
            {
                "run_id": "cal-a",
                "prompt_tokens": 1000,
                "max_output_tokens": 100,
                "context_tokens": 0,
                "tool_calls": 1,
                "hardware_score": 1.0,
                "observed_seconds": 2.0,
            },
            {
                "run_id": "cal-b",
                "prompt_tokens": 2000,
                "max_output_tokens": 200,
                "context_tokens": 1000,
                "tool_calls": 2,
                "hardware_score": 1.0,
                "observed_seconds": 4.0,
            },
            {
                "run_id": "cal-c",
                "prompt_tokens": 4000,
                "max_output_tokens": 400,
                "context_tokens": 2000,
                "tool_calls": 4,
                "hardware_score": 1.0,
                "observed_seconds": 8.0,
            },
        ]

    def test_prediction_uses_only_pre_execution_features(self):
        model = FeatureLatencySimulator.fit(self.calibration)
        features = FeatureInput.from_mapping(
            {
                "run_id": "holdout",
                "prompt_tokens": 3000,
                "max_output_tokens": 300,
                "context_tokens": 1000,
                "tool_calls": 3,
                "hardware_score": 2.0,
            }
        )
        prediction = model.predict(features.to_mapping())

        self.assertEqual(prediction["schema_version"], FEATURE_PREDICTION_SCHEMA)
        self.assertEqual(prediction["provenance"], "simulated")
        self.assertEqual(prediction["run_id"], "holdout")
        self.assertGreater(prediction["predicted_seconds"], 0)
        self.assertNotIn("observed_seconds", prediction)
        self.assertNotIn("generated_tokens", prediction)

    def test_target_derived_fields_are_rejected_at_prediction_boundary(self):
        model = FeatureLatencySimulator.fit(self.calibration)
        for name in (
            "observed_seconds",
            "wall_seconds",
            "cpu_seconds",
            "cuda_seconds",
            "gpu_seconds_at_reference",
            "cpu_activity_union_ms",
            "cuda_activity_union_ms",
            "kernel_duration_sum_ms",
            "kineto_wall_ms",
            "kineto_cuda_ms",
            "measured_kineto_time_ms",
            "generated_tokens",
            "completion_tokens",
            "output_tokens",
        ):
            row = {
                "run_id": "leaky",
                "prompt_tokens": 100,
                "max_output_tokens": 10,
                name: 1.0,
            }
            with self.subTest(name=name):
                with self.assertRaises(FeatureSimulatorError):
                    model.predict(row)

    def test_holdout_label_cannot_enter_fit(self):
        row = dict(self.calibration[0])
        row["split"] = "holdout"
        with self.assertRaises(FeatureSimulatorError):
            FeatureLatencySimulator.fit([row, self.calibration[1]])

    def test_labeled_record_is_not_accepted_by_predict(self):
        model = FeatureLatencySimulator.fit(self.calibration)
        labeled = FeatureCalibrationRecord.from_mapping(self.calibration[0])
        with self.assertRaises(FeatureSimulatorError):
            model.predict(labeled)

    def test_feature_parser_rejects_unknown_keys_instead_of_ignoring_them(self):
        with self.assertRaises(FeatureSimulatorError):
            FeatureInput.from_mapping(
                {
                    "run_id": "x",
                    "prompt_tokens": 1,
                    "max_output_tokens": 1,
                    "future_measured_field": 3,
                }
            )

    def test_hardware_score_changes_prediction_without_measured_target(self):
        model = FeatureLatencySimulator.fit(self.calibration)
        base = model.predict(
            {"run_id": "base", "prompt_tokens": 2000, "max_output_tokens": 200, "hardware_score": 1}
        )
        faster = model.predict(
            {
                "run_id": "faster",
                "prompt_tokens": 2000,
                "max_output_tokens": 200,
                "hardware_score": 2,
            }
        )
        self.assertLessEqual(faster["predicted_seconds"], base["predicted_seconds"])


if __name__ == "__main__":
    unittest.main()
