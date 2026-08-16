import json
import tempfile
import unittest
from pathlib import Path

from agentic_sim.telemetry import ThinTelemetry


class ThinTelemetryTests(unittest.TestCase):
    def test_run_manifest_redacts_credential_like_hardware_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            ThinTelemetry(
                tmp,
                run_id="run-1",
                hardware_manifest={"gpu": "H100", "api_key": "do-not-write", "nested": {"token": "secret"}},
            )
            manifest = json.loads((Path(tmp) / "run_manifest.json").read_text())
            encoded = json.dumps(manifest)
            self.assertIn("H100", encoded)
            self.assertNotIn("do-not-write", encoded)
            self.assertNotIn("secret", encoded)

    def test_model_and_tool_records_are_correlated_and_timed(self):
        with tempfile.TemporaryDirectory() as tmp:
            telemetry = ThinTelemetry(tmp, run_id="run-1", instance_id="i1")
            telemetry.run_start(config_hash="abc")
            request_id = telemetry.record_model_request(step_id=1, request={"messages": [{"role": "user"}]}, response={"usage": {"prompt_tokens": 3}}, start_ns=10, end_ns=20)
            action_id = telemetry.record_tool_call(step_id=1, request_id=request_id, command="echo hi", result={"exit_code": 0}, start_ns=30, end_ns=40)
            telemetry.run_end(status="completed", exit_code=0)
            events = [json.loads(line) for line in (Path(tmp) / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            calls = json.loads((Path(tmp) / "model_calls.jsonl").read_text(encoding="utf-8").splitlines()[0])
            tools = json.loads((Path(tmp) / "tool_calls.jsonl").read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(calls["request_id"], request_id)
            self.assertEqual(tools["request_id"], request_id)
            self.assertEqual(tools["action_id"], action_id)
            self.assertEqual(calls["duration_ms"], 0.00001)
            self.assertEqual(events[0]["event_type"], "run_start")

    def test_context_manager_records_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            telemetry = ThinTelemetry(tmp, run_id="run-1")
            with self.assertRaises(RuntimeError):
                with telemetry.model_span({"step_id": 2}):
                    raise RuntimeError("fixture")
            record = json.loads((Path(tmp) / "model_calls.jsonl").read_text(encoding="utf-8").splitlines()[0])
            self.assertIn("error", record["response"])

    def test_context_manager_keeps_step_correlation_and_rejects_unknown_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            telemetry = ThinTelemetry(tmp, run_id="run-1")
            with telemetry.model_span({"step_id": 7}):
                pass
            record = json.loads((Path(tmp) / "model_calls.jsonl").read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(record["step_id"], 7)
            with self.assertRaises(ValueError):
                telemetry.record_gpu_sample({}, provenance="invented")


if __name__ == "__main__":
    unittest.main()
