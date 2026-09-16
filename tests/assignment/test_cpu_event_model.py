import unittest

from agentic_sim.assignment.cpu_event_model import (
    HierarchicalMedianModel,
    class_mean_predict,
    row_from_tool_input,
)
from agentic_sim.assignment.event_simulator import ToolEventInput


def _row(op, tool, sub, prefix, observed, *, recursive=0, pipe=0, paths=1):
    return {
        "operation_class": op,
        "tool_name": tool,
        "subcommand": sub,
        "launch_family": "test" if op == "test" else tool,
        "command_prefix": prefix,
        "recursive": recursive,
        "has_pipe": pipe,
        "declared_path_count": paths,
        "observed_ms": observed,
    }


class CpuEventModelTests(unittest.TestCase):
    def test_hierarchical_uses_class_median_not_mean(self):
        rows = [
            _row("test", "python", "-m", "python -m pytest", 200.0) for _ in range(40)
        ] + [
            _row("test", "python", "-m", "python -m pytest", 20000.0) for _ in range(4)
        ] + [
            _row("read", "str_replace_editor", "view", "str_replace_editor view /tmp/a", 180.0)
            for _ in range(20)
        ]
        model = HierarchicalMedianModel(min_count=8).fit(rows)
        predicted = model.predict(rows[0])
        meanish = class_mean_predict(rows, rows[0])
        self.assertGreater(meanish, 1500.0)
        self.assertLess(predicted, 300.0)
        self.assertGreater(predicted, 150.0)

    def test_mapping_roundtrip(self):
        rows = [_row("read", "cat", "", "cat /tmp/a", 190.0 + i) for i in range(12)]
        model = HierarchicalMedianModel(min_count=8).fit(rows)
        restored = HierarchicalMedianModel.from_mapping(model.to_mapping())
        self.assertAlmostEqual(model.predict(rows[0]), restored.predict(rows[0]))

    def test_row_from_tool_input_marks_find_recursive(self):
        features = ToolEventInput.from_mapping(
            {
                "schema_version": "assignment.tool-event-input.v1",
                "event_id": "e1",
                "run_id": "r1",
                "split": "calibration",
                "operation_class": "traversal",
                "declared_command_bytes": 24,
                "declared_read_bytes": 0,
                "declared_write_bytes": 0,
                "declared_path_count": 1,
                "hardware": {
                    "schema_version": "assignment.hardware-profile.v1",
                    "hardware_id": "h100",
                    "architecture": "Hopper",
                    "cpu_cores": 16,
                    "cpu_threads": 32,
                    "cpu_base_ghz": 2.8,
                    "system_memory_gib": 128.0,
                    "storage_read_mbps": 5000.0,
                    "storage_write_mbps": 3000.0,
                    "gpu_count": 1,
                    "gpu_compute_capability": 9.0,
                    "gpu_memory_gib": 80.0,
                    "gpu_memory_bandwidth_gbps": 3350.0,
                    "gpu_bf16_tflops": 989.0,
                },
                "tool_name": "find",
                "subcommand": "/testbed",
                "command_prefix": "find /testbed -name",
            }
        )
        row = row_from_tool_input(features)
        self.assertEqual(row["launch_family"], "find")
        self.assertEqual(row["recursive"], 1)


if __name__ == "__main__":
    unittest.main()
