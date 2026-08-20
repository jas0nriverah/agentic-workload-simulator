import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.validation.normalize_sweagent_trajectory import normalize_document


class NormalizeSWEAgentTrajectoryTests(unittest.TestCase):
    def make_source(self, root: Path) -> Path:
        source = root / "sample.traj"
        value = {
            "trajectory": [
                {
                    "action": "tool view /",
                    "observation": "files",
                    "response": "inspect",
                    "thought": "think",
                    "execution_time": 0.25,
                    "state": {"opaque": [1, 2]},
                    "query": [{"role": "user", "content": "request"}],
                    "extra_info": {"opaque": True},
                },
                {
                    "action": "",
                    "observation": "",
                    "response": "Exit due to cost limit",
                    "thought": "Exit due to cost limit",
                    "execution_time": 0.0,
                    "state": {},
                    "query": [{}],
                    "extra_info": {},
                },
            ],
            "history": [
                {
                    "role": "system",
                    "content": "system",
                    "agent": "main",
                    "message_type": "system_prompt",
                },
                {
                    "role": "assistant",
                    "content": "inspect",
                    "thought": "think",
                    "action": "tool view /",
                    "agent": "main",
                    "tool_calls": [{"id": "call-1", "type": "function"}],
                    "message_type": "action",
                },
                {
                    "role": "tool",
                    "content": "files",
                    "agent": "main",
                    "message_type": "observation",
                    "tool_call_ids": ["call-1"],
                },
            ],
            "info": {
                "swe_agent_version": "1.1.0",
                "model_stats": {"api_calls": 2},
                "exit_status": "exit_cost",
            },
            "replay_config": "opaque replay config",
            "environment": "docker",
        }
        source.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return source

    def test_normalization_is_additive_lossless_and_structural(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.make_source(root)
            before = source.read_bytes()
            source_sha256 = hashlib.sha256(before).hexdigest()
            output = root / "normalized.jsonl"
            summary = normalize_document(
                source,
                output,
                run_id="run-1",
                attempt_id="attempt-001",
                instance_id="instance-1",
            )
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(summary["source_sha256"], source_sha256)
            self.assertEqual(summary["records"], 1 + 2 + 3 + 1 + 1 + 1)
            rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[0]["record_type"], "manifest")
            self.assertEqual(rows[0]["source"]["sha256"], source_sha256)
            self.assertEqual(rows[0]["timing"]["request_level_timestamps"], "unavailable")
            steps = [row for row in rows if row["record_type"] == "trajectory_step"]
            self.assertEqual(steps[0]["raw_record"]["state"], {"opaque": [1, 2]})
            self.assertEqual(steps[0]["correlation"]["history_action_index"], 1)
            self.assertEqual(steps[0]["correlation"]["history_tool_index"], 2)
            self.assertEqual(steps[0]["correlation"]["tool_call_ids"], ["call-1"])
            self.assertEqual(steps[0]["correlation"]["confidence"], "structural_order")
            self.assertIsNone(steps[0]["correlation"]["request_id"])
            self.assertEqual(steps[1]["correlation"]["confidence"], "unavailable")
            self.assertEqual(summary["structural_links"], 1)
            info = next(row for row in rows if row["record_type"] == "info")
            self.assertEqual(info["raw_record"]["exit_status"], "exit_cost")
            tool = next(row for row in rows if row["record_type"] == "tool_execution")
            self.assertEqual(tool["correlation"]["tool_call_id"], "call-1")
            self.assertEqual(tool["payload"]["raw_trajectory_observation"], "files")
            terminal = next(row for row in rows if row["record_type"] == "agent_terminal")
            self.assertEqual(terminal["terminal_reason"], "Exit due to cost limit")
            self.assertEqual(summary["tool_executions"], 1)
            self.assertEqual(summary["terminal_events"], 1)

    def test_output_is_deterministic_and_existing_output_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.make_source(root)
            first = root / "first.jsonl"
            second = root / "second.jsonl"
            normalize_document(
                source, first, run_id="run-1", attempt_id="attempt-001", instance_id="i"
            )
            normalize_document(
                source, second, run_id="run-1", attempt_id="attempt-001", instance_id="i"
            )
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with self.assertRaisesRegex(ValueError, "output exists"):
                normalize_document(
                    source, first, run_id="run-1", attempt_id="attempt-001", instance_id="i"
                )
            with self.assertRaisesRegex(ValueError, "must not overwrite"):
                normalize_document(
                    source, source, run_id="run-1", attempt_id="attempt-001", instance_id="i"
                )

    def test_trace_log_keeps_provider_calls_separate_and_marks_discarded_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.make_source(root)
            trace = root / "sample.trace.log"
            trace.write_text(
                "\n".join(
                    [
                        "2026-08-20 18:40:13,579 - INFO - swea-lm - Response: "
                        "ModelResponse(id='response-1', created=100, model='Qwen/test', "
                        "choices=[Choices(finish_reason='tool_calls', message="
                        "ChatCompletionMessageToolCall(function=Function(arguments='{}'), "
                        "id='call-1', type='function'))], usage=Usage(completion_tokens=2, "
                        "prompt_tokens=5, total_tokens=7))",
                        "2026-08-20 18:40:14,579 - INFO - swea-lm - Response: "
                        "ModelResponse(id='response-2', created=101, model='Qwen/test', "
                        "choices=[Choices(finish_reason='tool_calls', message="
                        "ChatCompletionMessageToolCall(function=Function(arguments='{}'), "
                        "id='call-extra', type='function'))], usage=Usage(completion_tokens=3, "
                        "prompt_tokens=11, total_tokens=14))",
                        "2026-08-20 18:40:14,600 - WARNING - swea-lm - API calls 2 exceeds limit 1",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            output = root / "normalized.jsonl"
            summary = normalize_document(
                source,
                output,
                run_id="run-1",
                attempt_id="attempt-001",
                instance_id="i",
                trace_log=trace,
            )
            rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            calls = [row for row in rows if row["record_type"] == "model_call"]
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0]["disposition"], "committed")
            self.assertEqual(calls[1]["disposition"], "discarded_limit_exceeded")
            self.assertEqual(calls[0]["correlation"]["tool_call_ids"], ["call-1"])
            self.assertEqual(calls[1]["correlation"]["tool_call_ids"], ["call-extra"])
            manifest = rows[0]
            self.assertEqual(manifest["usage"]["provider"]["prompt_tokens"], 16)
            self.assertEqual(manifest["usage"]["provider"]["completion_tokens"], 5)
            self.assertIsNone(manifest["usage"]["sweagent"]["input_tokens"])
            self.assertEqual(manifest["usage"]["sweagent"]["api_calls"], 2)
            trace_summary = next(row for row in rows if row["record_type"] == "trace_summary")
            self.assertEqual(trace_summary["trace"]["response_count"], 2)
            self.assertTrue(trace_summary["trace"]["api_limit_warning"])
            self.assertEqual(summary["model_calls"], 2)

    def test_malformed_or_unsupported_root_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "bad.traj"
            output = root / "normalized.jsonl"
            source.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "root must be a JSON object"):
                normalize_document(source, output, run_id="r", attempt_id="a", instance_id="i")
            source.write_text(json.dumps({"trajectory": [], "history": []}), encoding="utf-8")
            with self.assertRaisesRegex(
                ValueError, "list fields trajectory/history and object field info"
            ):
                normalize_document(source, output, run_id="r", attempt_id="a", instance_id="i")


if __name__ == "__main__":
    unittest.main()
