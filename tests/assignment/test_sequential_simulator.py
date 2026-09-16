import unittest

from agentic_sim.assignment.event_simulator import EventSimulatorError, ModelEventInput, ToolEventInput
from agentic_sim.assignment.sequential_simulator import (
    PriorEventSummary,
    SequentialLatencyModel,
    fit_sequential_model,
)
from agentic_sim.assignment.tool_features import extract_tool_features, extractor_source_sha256


def tool_row(run_id, ordinal, action, observed_ms, split="calibration"):
    extracted = extract_tool_features(action)
    return {
        "run_id": run_id,
        "ordinal": ordinal,
        "split": split,
        "tool_name": extracted.tool_name,
        "subcommand": extracted.subcommand,
        "command_prefix": extracted.command_prefix,
        "operation_class": extracted.operation_class,
        "declared_command_bytes": extracted.declared_command_bytes,
        "declared_path_count": extracted.declared_path_count,
        "has_pipe": extracted.has_pipe,
        "has_glob": extracted.has_glob,
        "command_sha256": extracted.command_sha256,
        "observed_ms": observed_ms,
        "wall_ms": observed_ms,
        "output_tokens": None,
        "context_tokens": 0,
    }


def model_row(run_id, ordinal, context, output, observed_ms, split="calibration"):
    return {
        "run_id": run_id,
        "ordinal": ordinal,
        "split": split,
        "context_tokens": context,
        "input_tokens": context,
        "output_tokens": output,
        "observed_ms": observed_ms,
        "wall_ms": observed_ms,
    }


class SequentialSimulatorTests(unittest.TestCase):
    def test_fit_rejects_holdout_rows(self):
        tools = [tool_row("cal-0", 0, "cat /tmp/a.py", 10.0, split="holdout")]
        models = [model_row("cal-0", 0, 100, 20, 50.0)]
        traj = [{"run_id": "cal-0", "observed_ms": 100.0, "split": "calibration"}]
        with self.assertRaisesRegex(EventSimulatorError, "holdout"):
            fit_sequential_model(tools, models, traj)

    def test_current_event_output_tokens_are_not_used_at_predict_time(self):
        tools = [
            tool_row("cal-0", 0, "cat /tmp/a.py", 12.0),
            tool_row("cal-1", 0, "cat /tmp/b.py", 14.0),
        ]
        models = [
            model_row("cal-0", 0, 1000, 40, 200.0),
            model_row("cal-1", 0, 1200, 80, 400.0),
        ]
        traj = [
            {"run_id": "cal-0", "observed_ms": 300.0},
            {"run_id": "cal-1", "observed_ms": 500.0},
        ]
        model = fit_sequential_model(tools, models, traj)
        empty = PriorEventSummary.empty()
        first = model.predict_model_ms({"context_tokens": 1100, "input_tokens": 1100}, empty)
        leaked = dict(empty.to_mapping())
        leaked["current_output_tokens"] = 10_000
        second = model.predict_model_ms({"context_tokens": 1100, "input_tokens": 1100}, empty)
        self.assertEqual(first, second)

    def test_prior_revealed_output_changes_later_prediction(self):
        tools = [
            tool_row("cal-0", 0, "cat /tmp/a.py", 12.0),
            tool_row("cal-1", 0, "cat /tmp/b.py", 14.0),
        ]
        models = [
            model_row("cal-0", 0, 800, 20, 100.0),
            model_row("cal-0", 1, 900, 40, 180.0),
            model_row("cal-1", 0, 800, 80, 300.0),
            model_row("cal-1", 1, 900, 160, 500.0),
        ]
        traj = [
            {"run_id": "cal-0", "observed_ms": 400.0},
            {"run_id": "cal-1", "observed_ms": 900.0},
        ]
        fitted = fit_sequential_model(tools, models, traj)
        short = PriorEventSummary(
            prior_event_count=1,
            prior_median_output_tokens=20.0,
            prior_median_observed_ms=100.0,
            prior_label_sha256s=("a" * 64,),
            prior_tool_sha_medians=(),
            prior_tool_name_medians=(),
            last_output_tokens=(20.0,),
        )
        long = PriorEventSummary(
            prior_event_count=1,
            prior_median_output_tokens=160.0,
            prior_median_observed_ms=500.0,
            prior_label_sha256s=("b" * 64,),
            prior_tool_sha_medians=(),
            prior_tool_name_medians=(),
            last_output_tokens=(160.0,),
        )
        features = {"context_tokens": 900, "input_tokens": 900}
        self.assertNotEqual(
            fitted.predict_model_ms(features, short),
            fitted.predict_model_ms(features, long),
        )

    def test_frozen_extractor_hash_must_match_source(self):
        tools = [
            tool_row("cal-0", 0, "cat /tmp/a.py", 12.0),
            tool_row("cal-1", 0, "cat /tmp/b.py", 14.0),
        ]
        models = [
            model_row("cal-0", 0, 1000, 40, 200.0),
            model_row("cal-1", 0, 1200, 80, 400.0),
        ]
        traj = [
            {"run_id": "cal-0", "observed_ms": 300.0},
            {"run_id": "cal-1", "observed_ms": 500.0},
        ]
        fitted = fit_sequential_model(tools, models, traj)
        payload = fitted.to_mapping()
        payload["extractor_sha256"] = "0" * 64
        with self.assertRaisesRegex(EventSimulatorError, "extractor_sha256"):
            SequentialLatencyModel.from_mapping(payload)
        self.assertEqual(fitted.extractor_sha256, extractor_source_sha256())

    def test_schema_accepts_logged_output_tokens_but_rejects_wall_and_evaluator(self):
        hardware = {
            "schema_version": "assignment.hardware-profile.v1",
            "hardware_id": "h100",
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
            "gpu_memory_bandwidth_gbps": 3350.0,
            "gpu_bf16_tflops": 989.0,
        }
        row = {
            "schema_version": "assignment.model-event-input.v1",
            "request_id": "r1",
            "run_id": "hold",
            "split": "holdout",
            "input_tokens": 10,
            "context_tokens": 10,
            "max_output_tokens": 32,
            "output_tokens": 99,
            "hardware": hardware,
        }
        parsed = ModelEventInput.from_mapping(row)
        self.assertEqual(parsed.output_tokens, 99)
        leaked = dict(row)
        leaked["wall_ms"] = 12.0
        with self.assertRaisesRegex(EventSimulatorError, "target-derived"):
            ModelEventInput.from_mapping(leaked)
        tool = {
            "schema_version": "assignment.tool-event-input.v1",
            "event_id": "t1",
            "run_id": "hold",
            "split": "holdout",
            "operation_class": "read",
            "declared_command_bytes": 8,
            "declared_read_bytes": 0,
            "declared_write_bytes": 0,
            "declared_path_count": 1,
            "hardware": hardware,
            "official_resolved": True,
        }
        with self.assertRaisesRegex(EventSimulatorError, "target-derived"):
            ToolEventInput.from_mapping(tool)


if __name__ == "__main__":
    unittest.main()
