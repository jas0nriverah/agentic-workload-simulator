import unittest

from agentic_sim.assignment.event_simulator import (
    EventSimulatorError,
    ModelEventInput,
    ToolEventInput,
)
from agentic_sim.assignment.workload_simulator import (
    WorkloadSimulator,
    hardware_prediction_shifts,
    score_channel,
)


def hardware(identifier="h100", tflops=989.0, bandwidth=3350.0):
    return {
        "schema_version": "assignment.hardware-profile.v1",
        "hardware_id": identifier,
        "architecture": "Hopper" if identifier == "h100" else "Ampere",
        "cpu_cores": 16,
        "cpu_threads": 32,
        "cpu_base_ghz": 3.0,
        "system_memory_gib": 128.0,
        "storage_read_mbps": 5000.0,
        "storage_write_mbps": 3000.0,
        "gpu_count": 1,
        "gpu_compute_capability": 9.0 if identifier == "h100" else 8.0,
        "gpu_memory_gib": 80.0,
        "gpu_memory_bandwidth_gbps": bandwidth,
        "gpu_bf16_tflops": tflops,
    }


def tool_features(run_id, ordinal, split="calibration", profile=None):
    return ToolEventInput.from_mapping(
        {
            "schema_version": "assignment.tool-event-input.v1",
            "event_id": f"{run_id}-tool-{ordinal}",
            "run_id": run_id,
            "split": split,
            "operation_class": ("read", "write", "test", "search")[ordinal % 4],
            "declared_command_bytes": 100 + ordinal * 20,
            "declared_read_bytes": 1000 + ordinal * 500,
            "declared_write_bytes": ordinal * 250,
            "declared_path_count": ordinal + 1,
            "hardware": profile or hardware(),
        }
    )


def model_features(run_id, ordinal, split="calibration", profile=None, output=None):
    return ModelEventInput.from_mapping(
        {
            "schema_version": "assignment.model-event-input.v1",
            "request_id": f"{run_id}-request-{ordinal}",
            "run_id": run_id,
            "split": split,
            "input_tokens": 500 + ordinal * 100,
            "context_tokens": 700 + ordinal * 120,
            "max_output_tokens": 2048,
            "output_tokens": 8 + ordinal * 4 if output is None else output,
            "hardware": profile or hardware(),
        }
    )


def fitted():
    tools = []
    models = []
    trajectories = []
    for run_number in range(4):
        run_id = f"cal-{run_number}"
        tool_total = 0.0
        model_total = 0.0
        for ordinal in range(2):
            observed = 8.0 + run_number * 1.5 + ordinal * 2.0
            tools.append((tool_features(run_id, ordinal + run_number), observed))
            tool_total += observed
        for ordinal in range(2):
            output = 8 + (ordinal + run_number) * 4
            observed = 20.0 + output * 2.0 + run_number
            models.append((model_features(run_id, ordinal + run_number), observed))
            model_total += observed
        trajectories.append((run_id, tool_total + model_total + 5.0))
    return WorkloadSimulator.fit(tools, models, trajectories, select_alpha=False)


class WorkloadSimulatorTests(unittest.TestCase):
    def test_requires_logged_output_tokens(self):
        tools = [(tool_features("cal-0", 0), 10.0), (tool_features("cal-1", 0), 12.0)]
        missing = model_features("cal-0", 0)
        missing = ModelEventInput.from_mapping(
            {k: v for k, v in missing.to_mapping().items() if k != "output_tokens"}
        )
        models = [
            (missing, 40.0),
            (model_features("cal-1", 0), 50.0),
        ]
        with self.assertRaisesRegex(EventSimulatorError, "output_tokens"):
            WorkloadSimulator.fit(tools, models, [("cal-0", 50.0), ("cal-1", 62.0)], select_alpha=False)

    def test_rejects_wall_time_as_a_feature(self):
        row = model_features("cal-0", 0).to_mapping()
        row["wall_ms"] = 99.0
        with self.assertRaisesRegex(EventSimulatorError, "target-derived"):
            ModelEventInput.from_mapping(row)

    def test_hardware_parameters_change_predictions(self):
        simulator = fitted()
        request = model_features("hold", 3, "holdout")
        shifted = hardware_prediction_shifts(request, simulator)
        self.assertGreater(
            shifted["half_bandwidth_compute_ms"], shifted["reference_ms"]
        )

    def test_output_tokens_change_gpu_predictions(self):
        simulator = fitted()
        short = model_features("hold", 3, "holdout", output=8)
        long = model_features("hold", 3, "holdout", output=80)
        self.assertNotEqual(
            simulator.predict_model_ms(short), simulator.predict_model_ms(long)
        )

    def test_cpu_median_does_not_follow_class_mean(self):
        simulator = fitted()
        request = tool_features("hold", 0, "holdout")
        predicted = simulator.predict_tool_ms(request)
        self.assertLess(predicted, 50.0)

    def test_cpu_hardware_scale_changes_predictions(self):
        simulator = fitted()
        fast = tool_features("hold", 0, "holdout", profile=hardware())
        slow = tool_features(
            "hold",
            0,
            "holdout",
            profile=hardware("slow-cpu"),
        )
        slow = ToolEventInput.from_mapping(
            {
                **slow.to_mapping(),
                "hardware": {
                    **slow.hardware.to_mapping(),
                    "hardware_id": "slow-cpu",
                    "cpu_base_ghz": 1.5,
                },
            }
        )
        self.assertGreater(simulator.predict_tool_ms(slow), simulator.predict_tool_ms(fast))

    def test_score_channel_reports_gate(self):
        summary = score_channel([(100.0, 100.0), (90.0, 100.0), (200.0, 100.0)])
        self.assertEqual(summary["n"], 3)
        self.assertFalse(summary["all_within_25"])
        self.assertAlmostEqual(summary["within_25_rate"], 2 / 3)


if __name__ == "__main__":
    unittest.main()
