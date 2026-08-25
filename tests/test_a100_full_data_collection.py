import json
import math
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from scripts.analysis import a100_full_data_collection as collection


class A100FullDataCollectionTests(unittest.TestCase):
    def test_manifest_selection_is_deterministic_and_balanced_by_population(self):
        rows = [
            {
                "suite": "verified",
                "repository": "b",
                "instance_id": "b__2",
                "status": "completed",
                "official_resolved": "false",
                "source_file": "z.json",
                "source_sha256": "z",
            },
            {
                "suite": "verified",
                "repository": "b",
                "instance_id": "b__1",
                "status": "completed",
                "official_resolved": "true",
                "source_file": "a.json",
                "source_sha256": "a",
            },
            {
                "suite": "lite",
                "repository": "a",
                "instance_id": "a__1",
                "status": "unavailable",
                "official_resolved": "false",
                "source_file": "a.json",
                "source_sha256": "a",
            },
        ]
        selected = collection.selected_tasks(rows)
        self.assertEqual([(row["suite"], row["repository"], row["instance_id"]) for row in selected], [("lite", "a", "a__1"), ("verified", "b", "b__1")])
        self.assertEqual(selected[1]["population_official_resolved"], True)

    def test_assignment_phase_ratio_uses_tool_and_model_wall_not_gpu_diagnostics(self):
        tool_ms = 75.0
        model_ms = 25.0
        self.assertEqual(tool_ms / model_ms, 3.0)
        self.assertEqual(collection.PHASE_RATIO_FORMULA, "sum(tool_call_wall_ms) / sum(model_request_wall_ms)")
        self.assertNotEqual(collection.PHASE_RATIO_FORMULA, collection.RATIO_FORMULA)

    def test_tool_model_boundary_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            task_dir = Path(directory)
            (task_dir / "one.traj").write_text(json.dumps({"trajectory": [{"action": "ls", "execution_time": 0.1}]}) + "\n", encoding="utf-8")
            model = [{"request_start_mono_ns": 100, "request_end_mono_ns": 200, "request_id": "m1"}, {"request_start_mono_ns": 300, "request_end_mono_ns": 400, "request_id": "m2"}]
            rows, reason = collection.extract_tool_events(task_dir, 0, model, "t")
            self.assertEqual(rows, [])
            self.assertEqual(reason, "tool_model_event_count_mismatch:1:2")

    def test_valid_tool_rows_preserve_nonnegative_duration_and_repeat_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            task_dir = Path(directory)
            (task_dir / "one.traj").write_text(json.dumps({"trajectory": [{"action": "ls", "execution_time": 0.1}, {"action": "pytest", "execution_time": 0.2}]}) + "\n", encoding="utf-8")
            model = [
                {"request_start_mono_ns": 100, "request_end_mono_ns": 200, "request_id": "m1"},
                {"request_start_mono_ns": 500_000, "request_end_mono_ns": 600_000, "request_id": "m2"},
            ]
            rows, reason = collection.extract_tool_events(task_dir, 0, model, "lite:x:r02")
            self.assertIsNone(reason)
            self.assertEqual(len(rows), 2)
            self.assertTrue(math.isclose(sum(row["wall_ms"] for row in rows), 0.3))
            self.assertTrue(all(row["end_mono_ns"] >= row["start_mono_ns"] for row in rows))
            self.assertTrue(all(row["trajectory_id"].endswith(":r02") for row in rows))

    def test_audit_rejects_inconsistent_phase_totals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = {
                "status": "unavailable",
                "request_id": "m1",
                "raw_trace_paths": [],
            }
            model = {
                "trajectory_id": "t",
                "request_id": "m1",
                "request_start_mono_ns": 100,
                "request_end_mono_ns": 200,
                "request_wall_ms": 0.1,
            }
            tool = {
                "trajectory_id": "t",
                "event_id": "e1",
                "start_mono_ns": 200,
                "end_mono_ns": 300,
                "wall_ms": 0.1,
            }
            task = {
                "trajectory_id": "t",
                "model_event_count": 1,
                "tool_event_count": 1,
                "phase_ratio_status": "valid",
                "total_model_request_wall_ms": 99.0,
                "total_tool_call_wall_ms": 0.1,
                "phase_ratio": 0.001,
                "phase_ratio_formula": collection.PHASE_RATIO_FORMULA,
            }
            for name, rows in (("request_rows.jsonl", [request]), ("model_events.jsonl", [model]), ("tool_events.jsonl", [tool]), ("task_rows.jsonl", [task])):
                (root / name).write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            result = collection.audit(Namespace(output_root=root))
            self.assertEqual(result, 2)
            self.assertEqual(collection.read_json(root / "offline_integrity_audit.json")["status"], "failed")


if __name__ == "__main__":
    unittest.main()
