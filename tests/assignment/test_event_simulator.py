import json
from pathlib import Path
import tempfile
import unittest

from agentic_sim.assignment.event_simulator import (
    AssignmentEventSimulator,
    EventSimulatorError,
    HardwareProfile,
    ModelEventInput,
    ToolEventInput,
    verify_frozen_prediction_manifest,
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


def tool_input(run_id, ordinal, split="calibration", profile=None):
    return {
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


def model_input(run_id, ordinal, split="calibration", profile=None):
    return {
        "schema_version": "assignment.model-event-input.v1",
        "request_id": f"{run_id}-request-{ordinal}",
        "run_id": run_id,
        "split": split,
        "input_tokens": 500 + ordinal * 100,
        "context_tokens": 700 + ordinal * 120,
        "max_output_tokens": 64 + ordinal * 16,
        "output_tokens": 8 + ordinal * 4,
        "hardware": profile or hardware(),
    }


def calibration_records():
    tools = []
    models = []
    trajectories = []
    for run_number in range(4):
        run_id = f"cal-{run_number}"
        tool_total = 0.0
        model_total = 0.0
        for ordinal in range(2):
            features = tool_input(run_id, ordinal + run_number)
            observed = 8.0 + run_number * 1.5 + ordinal * 2.0
            tool_total += observed
            tools.append(
                {
                    "schema_version": "assignment.tool-calibration.v1",
                    "split": "calibration",
                    "features": features,
                    "observed_ms": observed,
                }
            )
        for ordinal in range(2):
            features = model_input(run_id, ordinal + run_number)
            observed = 45.0 + run_number * 4.0 + ordinal * 6.0
            model_total += observed
            models.append(
                {
                    "schema_version": "assignment.model-calibration.v1",
                    "split": "calibration",
                    "features": features,
                    "observed_ms": observed,
                }
            )
        trajectories.append(
            {
                "schema_version": "assignment.trajectory-calibration.v1",
                "run_id": run_id,
                "split": "calibration",
                "observed_ms": tool_total + model_total + 12.0 + run_number,
            }
        )
    return tools, models, trajectories


def fitted_simulator(reverse=False):
    tools, models, trajectories = calibration_records()
    if reverse:
        tools.reverse()
        models.reverse()
        trajectories.reverse()
    return AssignmentEventSimulator.fit(tools, models, trajectories)


def holdout_features(profile=None):
    tools = [
        tool_input("hold-1", 2, "holdout", profile),
        tool_input("hold-2", 3, "holdout", profile),
    ]
    models = [
        model_input("hold-1", 2, "holdout", profile),
        model_input("hold-2", 3, "holdout", profile),
    ]
    return tools, models


class HardwareProfileTests(unittest.TestCase):
    def test_profile_is_explicit_and_pluggable(self):
        h100 = HardwareProfile.from_mapping(hardware())
        a100 = HardwareProfile.from_mapping(hardware("a100", 312.0, 2039.0))
        self.assertEqual(h100.architecture, "Hopper")
        self.assertEqual(a100.architecture, "Ampere")
        h100_request = ModelEventInput.from_mapping(model_input("x", 1, profile=hardware()))
        a100_request = ModelEventInput.from_mapping(
            model_input("x", 1, profile=hardware("a100", 312.0, 2039.0))
        )
        self.assertNotEqual(h100_request.design_row(), a100_request.design_row())

    def test_invalid_or_partial_hardware_is_rejected(self):
        row = hardware()
        del row["gpu_memory_bandwidth_gbps"]
        with self.assertRaisesRegex(EventSimulatorError, "missing"):
            HardwareProfile.from_mapping(row)


class LeakageProtectionTests(unittest.TestCase):
    def test_tool_input_rejects_all_measured_timing_classes(self):
        for leaked in (
            "wall_ms",
            "cpu_ms",
            "cuda_ms",
            "kineto_wall_ms",
            "cpu_activity_union_ms",
            "cuda_activity_union_ms",
            "kernel_duration_sum_ms",
        ):
            row = tool_input("hold", 1, "holdout")
            row[leaked] = 1.0
            with self.subTest(leaked=leaked):
                with self.assertRaisesRegex(EventSimulatorError, "target-derived"):
                    ToolEventInput.from_mapping(row)

    def test_model_input_accepts_logged_output_tokens(self):
        row = model_input("hold", 1, "holdout")
        parsed = ModelEventInput.from_mapping(row)
        self.assertEqual(parsed.output_tokens, row["output_tokens"])
        self.assertEqual(parsed.decode_tokens, row["output_tokens"])
        without = dict(row)
        without.pop("output_tokens")
        budget_only = ModelEventInput.from_mapping(without)
        self.assertIsNone(budget_only.output_tokens)
        self.assertEqual(budget_only.decode_tokens, budget_only.max_output_tokens)
        self.assertNotEqual(parsed.design_row(), budget_only.design_row())

    def test_model_input_rejects_measured_timing_and_output_aliases(self):
        for leaked in (
            "actual_output_tokens",
            "completion_tokens",
            "response_bytes",
            "wall_time_ms",
            "kineto_cuda_ms",
        ):
            row = model_input("hold", 1, "holdout")
            row[leaked] = 1
            with self.subTest(leaked=leaked):
                with self.assertRaisesRegex(EventSimulatorError, "target-derived"):
                    ModelEventInput.from_mapping(row)

    def test_holdout_labels_cannot_enter_fit(self):
        tools, models, trajectories = calibration_records()
        tools[0]["split"] = "holdout"
        tools[0]["features"]["split"] = "holdout"
        with self.assertRaisesRegex(EventSimulatorError, "holdout/test"):
            AssignmentEventSimulator.fit(tools, models, trajectories)

    def test_calibration_and_holdout_runs_are_disjoint(self):
        simulator = fitted_simulator()
        tools = [tool_input("cal-0", 9, "holdout")]
        models = [model_input("cal-0", 9, "holdout")]
        with self.assertRaisesRegex(EventSimulatorError, "disjoint"):
            simulator.build_prediction_manifest(tools, models)


class DeterminismAndFreezeTests(unittest.TestCase):
    def test_fit_and_manifest_are_order_independent(self):
        first = fitted_simulator()
        second = fitted_simulator(reverse=True)
        tools, models = holdout_features()
        first_manifest = first.build_prediction_manifest(tools, models)
        second_manifest = second.build_prediction_manifest(
            list(reversed(tools)), list(reversed(models))
        )
        self.assertEqual(first_manifest, second_manifest)

    def test_freeze_hash_and_tampering_are_enforced(self):
        simulator = fitted_simulator()
        tools, models = holdout_features()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "prediction_manifest.json"
            digest = simulator.freeze_predictions(tools, models, path)
            manifest, verified = verify_frozen_prediction_manifest(path)
            self.assertEqual(digest, verified)
            self.assertEqual(manifest["provenance"], "calibration_only")
            self.assertTrue(path.with_suffix(".sha256").is_file())
            path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
            with self.assertRaisesRegex(EventSimulatorError, "tampered"):
                verify_frozen_prediction_manifest(path)

    def test_frozen_manifest_cannot_be_replaced(self):
        simulator = fitted_simulator()
        tools, models = holdout_features()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "prediction_manifest.json"
            simulator.freeze_predictions(tools, models, path)
            changed_tools = json.loads(json.dumps(tools))
            changed_tools[0]["declared_path_count"] += 1
            with self.assertRaisesRegex(EventSimulatorError, "overwrite"):
                simulator.freeze_predictions(changed_tools, models, path)


if __name__ == "__main__":
    unittest.main()
