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

    def test_official_result_binds_to_repeat_specific_evaluator_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sweagent_output.a100-full-demo__repo-r01.json").write_text(
                json.dumps({"resolved_ids": ["demo__repo"]}), encoding="utf-8"
            )
            (root / "sweagent_output.a100-full-demo__repo-r02.json").write_text(
                json.dumps({"unresolved_ids": ["demo__repo"]}), encoding="utf-8"
            )
            status, resolved, path = collection.official_result(
                root / "missing-local-report",
                "demo__repo",
                [root],
                "a100-full-demo__repo-r02",
            )
            self.assertEqual(status, "unresolved")
            self.assertFalse(resolved)
            self.assertTrue(path.endswith("-r02.json"))

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

    def test_unavailable_model_events_are_materialized_without_direct_timing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_dir = root / "tasks" / "lite" / "demo__repo-1" / "r01"
            task_dir.mkdir(parents=True)
            task = {
                "suite": "lite",
                "repository": "demo/repo",
                "instance_id": "demo__repo-1",
                "repeat_id": "r01",
                "trajectory_id": "lite:demo__repo-1:r01",
                "status": "completed",
                "available": False,
                "trace_error": "trace_arm_failed",
                "official_status": "unavailable",
                "raw_paths": [],
            }
            event = {
                "provenance": "unavailable",
                "trajectory_id": "lite:demo__repo-1:r01",
                "request_id": "m1",
                "ordering": 1,
                "input_tokens": 10,
                "output_tokens": 2,
                "request_wall_ms": 12.5,
                "request_start_mono_ns": 100,
                "request_end_mono_ns": 12600,
                "clock_id": "CLOCK_MONOTONIC_RAW",
                "failure_reason": "trace_arm_failed",
            }
            (task_dir / "task.json").write_text(json.dumps(task), encoding="utf-8")
            (task_dir / "model_events.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
            (root / "task_rows.jsonl").write_text(json.dumps(task) + "\n", encoding="utf-8")
            self.assertEqual(collection.materialize_unavailable_request_rows(root, [task]), 1)
            self.assertEqual(collection.materialize_unavailable_request_rows(root, [task]), 0)
            rows = collection._jsonl(root / "request_rows.jsonl")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "unavailable")
            self.assertEqual(rows[0]["wall_ms"], 12.5)
            self.assertIsNone(rows[0]["cpu_activity_union_ms"])
            self.assertIsNone(rows[0]["cuda_activity_union_ms"])
            self.assertEqual(rows[0]["unavailable_reason"], "trace_arm_failed")

    def test_unavailable_failure_row_gets_explicit_derived_identity_and_zero_counts(self):
        row = collection.canonical_task_row({
            "suite": "verified",
            "instance_id": "demo__repo-2",
            "repeat_id": "r03",
            "status": "unavailable",
            "unavailable_reason": "trace_summary_missing",
        })
        self.assertEqual(row["trajectory_id"], "verified:demo__repo-2:r03")
        self.assertEqual(row["model_event_count"], 0)
        self.assertEqual(row["tool_event_count"], 0)
        self.assertEqual(row["metadata_reconciliation"], "trajectory_id_derived_from_suite_instance_repeat")


if __name__ == "__main__":
    unittest.main()
