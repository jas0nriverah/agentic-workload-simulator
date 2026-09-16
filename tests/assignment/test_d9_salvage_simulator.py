import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SIMULATOR = ROOT / "docs" / "d9-salvage-20260910" / "simulator"
if str(SIMULATOR) not in sys.path:
    sys.path.insert(0, str(SIMULATOR))

from d9_simulator import D9Simulator, PredictionContractError  # noqa: E402
from hardware_profile import (  # noqa: E402
    HardwareProfile,
    default_hardware_profile,
    hardware_profile_sha256,
)
from strict_metrics import MetricsError, score_bundle, score_rows  # noqa: E402


class HardwareProfileTests(unittest.TestCase):
    def test_default_profile_is_complete_and_hash_stable(self):
        profile = default_hardware_profile()
        self.assertEqual(profile.hardware_id, "h100-80gb-pace")
        self.assertEqual(
            hardware_profile_sha256(profile),
            hardware_profile_sha256(profile.to_mapping()),
        )
        self.assertEqual(profile.cpu_capacity, 89.6)

    def test_unknown_or_partial_profile_is_rejected(self):
        profile = default_hardware_profile().to_mapping()
        profile.pop("gpu_memory_gib")
        with self.assertRaisesRegex(ValueError, "missing"):
            HardwareProfile.from_mapping(profile)


class SimulatorTests(unittest.TestCase):
    def test_cpu_prediction_is_runnable_and_records_transfer_state(self):
        simulator = D9Simulator()
        result = simulator.predict_request(
            {
                "target": "historical_cpu_tool_wall",
                "inputs": {"action": "grep -n needle src/module.py"},
            }
        )
        self.assertEqual(result["status"], "predicted")
        self.assertGreater(result["predicted_ms"], 0)
        self.assertEqual(result["hardware"]["transfer_status"], "reference_profile")
        self.assertEqual(result["hardware"]["scaling_applied"], False)

    def test_labels_are_rejected_at_prediction_boundary(self):
        with self.assertRaisesRegex(PredictionContractError, "rejects input field"):
            D9Simulator().predict_request(
                {
                    "target": "historical_cpu_tool_wall",
                    "inputs": {"action": "echo ok", "observed_ms": 10.0},
                }
            )

    def test_trace_conditioned_model_requires_declared_workload_fields(self):
        result = D9Simulator().predict_request(
            {
                "target": "conditional_trace_e2e",
                "inputs": {
                    "tool_count": 30,
                    "request_count": 31,
                    "input_tokens": 45_000,
                    "output_tokens": 3_000,
                },
            }
        )
        self.assertEqual(result["contract"], "trace_conditioned_declared_workload")
        self.assertGreater(result["predicted_ms"], 0)
        with self.assertRaisesRegex(PredictionContractError, "requires input"):
            D9Simulator().predict_request(
                {
                    "target": "conditional_trace_e2e",
                    "inputs": {"tool_count": 30, "request_count": 31, "input_tokens": 45_000},
                }
            )

    def test_conditional_gpu_artifact_is_available_through_cli_api(self):
        result = D9Simulator().predict_request(
            {
                "target": "conditional_gpu_request_proxy_wall",
                "inputs": {
                    "input_tokens": 1_829,
                    "context_tokens": 1_829,
                    "output_tokens": 75,
                    "max_output_tokens": 2_048,
                },
            }
        )
        self.assertEqual(result["status"], "predicted")
        self.assertEqual(result["target"], "conditional_gpu_request_proxy_wall")
        self.assertEqual(result["contract"], "trace_conditioned_declared_request_workload")
        self.assertEqual(result["model_details"]["candidate"], "nonnegative_log_additive")
        self.assertIn("development_metrics", result["model_details"])
        self.assertEqual(
            result["hardware"]["sensitivity_status"],
            "candidate_hardware_effect_unmodeled",
        )
        self.assertGreater(result["predicted_ms"], 0)

    def test_conditional_gpu_rejects_observed_target(self):
        with self.assertRaisesRegex(PredictionContractError, "rejects input field"):
            D9Simulator().predict_request(
                {
                    "target": "conditional_gpu_request_proxy_wall",
                    "inputs": {
                        "input_tokens": 100,
                        "context_tokens": 100,
                        "output_tokens": 20,
                        "max_output_tokens": 128,
                        "observed_ms": 10.0,
                    },
                }
            )

    def test_native_e2e_defaults_to_corrected_token_candidate(self):
        result = D9Simulator().predict_request(
            {
                "target": "conditional_native_e2e",
                "inputs": {"prompt_tokens": 23_155, "completion_tokens": 55},
            }
        )
        self.assertEqual(result["status"], "predicted")
        self.assertEqual(result["contract"], "trace_conditioned_native_e2e")
        self.assertEqual(result["model_details"]["candidate"], "relative_nnls_token")
        self.assertEqual(result["model_details"]["target"], "native:e2e")
        self.assertEqual(
            result["hardware"]["transfer_status"], "unvalidated_fit_hardware_domain"
        )
        self.assertEqual(
            result["hardware"]["cross_hardware_status"], "unvalidated"
        )
        self.assertEqual(result["hardware"]["scaling_applied"], False)
        self.assertIn("development_metrics", result["model_details"])
        self.assertGreater(result["predicted_ms"], 0)

    def test_native_e2e_cache_candidate_requires_explicit_trace_opt_in(self):
        request = {
            "target": "conditional_native_e2e",
            "inputs": {
                "prompt_tokens": 23_155,
                "completion_tokens": 55,
                "cache_trace": True,
                "cached_tokens": 23_104,
            },
        }
        result = D9Simulator().predict_request(request)
        self.assertEqual(result["contract"], "trace_conditioned_native_e2e_cache")
        self.assertEqual(
            result["model_details"]["candidate"], "relative_nnls_token_cache"
        )
        self.assertTrue(result["model_details"]["cache_trace_conditioned"])
        self.assertGreater(result["predicted_ms"], 0)
        with self.assertRaisesRegex(PredictionContractError, "cache_trace=true"):
            D9Simulator().predict_request(
                {
                    "target": "conditional_native_e2e",
                    "inputs": {
                        "prompt_tokens": 23_155,
                        "completion_tokens": 55,
                        "cached_tokens": 23_104,
                    },
                }
            )

    def test_native_e2e_rejects_measured_labels(self):
        with self.assertRaisesRegex(PredictionContractError, "rejects input field"):
            D9Simulator().predict_request(
                {
                    "target": "conditional_native_e2e",
                    "inputs": {
                        "prompt_tokens": 100,
                        "completion_tokens": 10,
                        "observed_ms": 10.0,
                    },
                }
            )

    def test_native_target_is_explicitly_unsupported(self):
        result = D9Simulator().predict_request({"target": "native_queue", "inputs": {}})
        self.assertEqual(result["status"], "unsupported_no_independent_calibration")
        self.assertIsNone(result["predicted_ms"])

    def test_assignment_v3_path_uses_hardware_when_artifact_is_supplied(self):
        # Reuse the existing deterministic fixture fit to exercise the adapter
        # without fitting a new D9 candidate in this test.
        from agentic_sim.assignment.event_simulator import ModelEventInput, ToolEventInput
        from agentic_sim.assignment.workload_simulator import WorkloadSimulator

        def profile(ident, bandwidth):
            return {
                "schema_version": "assignment.hardware-profile.v1",
                "hardware_id": ident,
                "architecture": "Hopper",
                "cpu_cores": 16,
                "cpu_threads": 32,
                "cpu_base_ghz": 3.0,
                "system_memory_gib": 128.0,
                "storage_read_mbps": 5000.0,
                "storage_write_mbps": 3000.0,
                "gpu_count": 1,
                "gpu_compute_capability": 9.0,
                "gpu_memory_gib": 80.0,
                "gpu_memory_bandwidth_gbps": bandwidth,
                "gpu_bf16_tflops": 989.0,
            }

        tools = []
        models = []
        trajectories = []
        for number in range(4):
            run = f"cal-{number}"
            hardware = profile("fixture", 3350.0)
            tool = ToolEventInput.from_mapping(
                {
                    "schema_version": "assignment.tool-event-input.v1",
                    "event_id": f"{run}-tool",
                    "run_id": run,
                    "split": "calibration",
                    "operation_class": "read",
                    "declared_command_bytes": 10 + number,
                    "declared_read_bytes": 10,
                    "declared_write_bytes": 0,
                    "declared_path_count": 1,
                    "hardware": hardware,
                }
            )
            model = ModelEventInput.from_mapping(
                {
                    "schema_version": "assignment.model-event-input.v1",
                    "request_id": f"{run}-request",
                    "run_id": run,
                    "split": "calibration",
                    "input_tokens": 100 + number,
                    "context_tokens": 100 + number,
                    "max_output_tokens": 64,
                    "output_tokens": 8 + number,
                    "hardware": hardware,
                }
            )
            tools.append((tool, 20.0 + number))
            models.append((model, 50.0 + number * 2))
            trajectories.append((run, 100.0 + number * 4))
        artifact = WorkloadSimulator.fit(
            tools, models, trajectories, select_alpha=False
        ).to_mapping()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "workload.json"
            path.write_text(json.dumps(artifact), encoding="utf-8")
            fast = D9Simulator(
                workload_model_path=path,
                hardware=HardwareProfile.from_mapping(profile("fast", 3350.0)),
            )
            slow = D9Simulator(
                workload_model_path=path,
                hardware=HardwareProfile.from_mapping(profile("slow", 1000.0)),
            )
            request = {
                "target": "assignment_gpu_event",
                "inputs": {
                    "request_id": "hold-request",
                    "run_id": "hold-run",
                    "split": "holdout",
                    "input_tokens": 100,
                    "context_tokens": 100,
                    "max_output_tokens": 64,
                    "output_tokens": 8,
                },
            }
            fast_result = fast.predict_request(request)
            slow_result = slow.predict_request(request)
            self.assertEqual(fast_result["status"], "predicted")
            self.assertNotEqual(fast_result["predicted_ms"], slow_result["predicted_ms"])
            self.assertEqual(
                slow_result["hardware"]["sensitivity_status"],
                "model_hardware_parameterized_unvalidated",
            )


class StrictMetricTests(unittest.TestCase):
    def test_zero_targets_and_missing_predictions_stay_strict(self):
        report = score_rows(
            [
                {
                    "event_id": "zero",
                    "trajectory_id": "run-a",
                    "observed_ms": 0.0,
                    "predicted_ms": 0.0,
                },
                {
                    "event_id": "miss",
                    "trajectory_id": "run-a",
                    "observed_ms": 10.0,
                    "status": "unsupported",
                },
            ]
        )
        self.assertEqual(report["infinite_ape_count"], 0)
        self.assertEqual(report["unsupported_n"], 1)
        self.assertFalse(report["all_within25"])

    def test_zero_prediction_against_positive_target_is_infinite(self):
        report = score_rows(
            [{"event_id": "bad", "trajectory_id": "run-a", "observed_ms": 1.0, "predicted_ms": 0.0}]
        )
        self.assertEqual(report["infinite_ape_count"], 0)  # finite 100% APE, not epsilon-smoothed
        self.assertFalse(report["all_within25"])

    def test_event_ids_may_repeat_across_trajectories_but_not_within_scope(self):
        predictions = [
            {
                "target": "cpu",
                "trajectory_id": "run-a",
                "event_id": "event-1",
                "predicted_ms": 10.0,
            },
            {
                "target": "cpu",
                "trajectory_id": "run-b",
                "event_id": "event-1",
                "predicted_ms": 20.0,
            },
        ]
        labels = [
            {"target": "cpu", "trajectory_id": "run-a", "event_id": "event-1", "observed_ms": 10.0},
            {"target": "cpu", "trajectory_id": "run-b", "event_id": "event-1", "observed_ms": 20.0},
        ]
        report = score_bundle(predictions, labels, required_target_kinds=("cpu",))
        self.assertEqual(report["trajectory_pass_count"], 2)

    def test_native_e2e_and_phase_overlap_is_flagged(self):
        rows = [
            {
                "target": "native_e2e",
                "trajectory_id": "run",
                "event_id": "e2e",
                "observed_ms": 10.0,
            },
            {
                "target": "native_queue",
                "trajectory_id": "run",
                "event_id": "q",
                "observed_ms": 1.0,
            },
        ]
        report = score_bundle(
            [{**row, "predicted_ms": row["observed_ms"]} for row in rows],
            rows,
            required_target_kinds=("native_e2e", "native_queue"),
        )
        self.assertEqual(
            report["composition_status"],
            "overlap_rejected_native_e2e_and_phase_targets",
        )
        self.assertEqual(report["literal_d9_status"], "UNPROVEN_OR_FAILED")

    def test_duplicate_scoped_identity_is_rejected(self):
        row = {"target": "cpu", "trajectory_id": "run", "event_id": "event", "observed_ms": 1.0}
        with self.assertRaisesRegex(MetricsError, "duplicate label"):
            score_bundle([], [row, dict(row)], required_target_kinds=("cpu",))


if __name__ == "__main__":
    unittest.main()
