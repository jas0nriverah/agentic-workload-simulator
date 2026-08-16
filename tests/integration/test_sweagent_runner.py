import json
import http.server
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from agentic_sim.runners import RunnerConfig, build_command, run_sweagent
from agentic_sim.runners.sweagent_runner import RunnerContractError


class SweagentRunnerTests(unittest.TestCase):
    def test_command_requires_immutable_revision(self):
        with self.assertRaises(RunnerContractError):
            build_command(instances_path="/tmp/tasks.json", instance_id="i1", output_dir="/tmp/o", model="openai/Qwen", model_revision="main")

    def test_command_is_direct_run_batch_with_local_pinned_instances(self):
        command = build_command(instances_path="/tmp/tasks.json", instance_id="i1", output_dir="/tmp/o", model="openai/Qwen", model_revision="b2cff646eb4bb1")
        self.assertEqual(command[0:2], ["sweagent", "run-batch"])
        self.assertIn("--instances.type", command)
        self.assertIn("file", command)
        self.assertIn("$VLLM_API_KEY", command)
        self.assertNotIn("--model-revision", command)

    def test_control_and_thin_modes_share_identical_agent_command(self):
        kwargs = dict(instances_path="/tmp/tasks.json", instance_id="i1", output_dir="/tmp/o", model="openai/Qwen", model_revision="b2cff646eb4bb1")
        self.assertEqual(build_command(**kwargs), build_command(**kwargs))

    def test_fixture_direct_run_preserves_logs_and_separate_evaluation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = run_sweagent(RunnerConfig(command=["sh", "-c", "echo trajectory"], experiment_id="e1", instance_id="i1", work_root=root, config={"temperature": 0.0}), evaluator_command=["sh", "-c", "echo evaluator"])
            self.assertEqual(result.status, "completed")
            self.assertIn("trajectory", result.stdout_log.read_text())
            self.assertIn("evaluator", (result.output_dir / "evaluator.stdout.log").read_text())
            summary = json.loads((result.output_dir / "summary.json").read_text())
            self.assertTrue(summary["evaluator"]["runtime_excluded_from_trajectory"])
            self.assertEqual(json.loads((result.output_dir / "prediction.json").read_text())["status"], "unavailable")

    def test_failed_run_is_classified_without_fabricating_prediction(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_sweagent(RunnerConfig(command=["sh", "-c", "exit 7"], experiment_id="e1", instance_id="i1", work_root=tmp))
            self.assertEqual(result.status, "failed")
            summary = json.loads((result.output_dir / "summary.json").read_text())
            self.assertEqual(summary["failure_class"], "runner_error")

    def test_timeout_kills_the_isolated_process_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_sweagent(RunnerConfig(command=[sys.executable, "-c", "import time; time.sleep(3)"], experiment_id="e1", instance_id="i1", work_root=tmp, timeout_seconds=1))
            self.assertEqual(result.status, "timeout")
            self.assertLess(result.duration_ms, 32_000)

    def test_shell_resume_preserves_config_for_an_incomplete_attempt(self):
        script = Path(__file__).resolve().parents[2] / "scripts" / "cloud" / "lambda_run_first_experiment.sh"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.env"
            manifest.write_text("\n".join([
                "FIRST_LITE_INSTANCE_ID=i1", "EXPERIMENT_ID=e1",
                "SWE_AGENT_REVISION=0f3acafacabc0def8cc76b4e48acb4b6cf302cb9",
                "SWE_BENCH_REVISION=726c5461e2ef52d83cf1ea2107870a8bb332d5",
                "VLLM_MODEL_REVISION=b2cff646eb4bb1d68355c01b18ae02e7cf42d120",
                "SWE_AGENT_COMMAND=false run-batch --instances.type file --instances.path /tmp/i.json --agent.model.name openai/Qwen --agent.model.api_base http://127.0.0.1:8000/v1 --agent.model.api_key fixture --num_workers 1", "EVALUATE_COMMAND=true -m swebench.harness.run_evaluation --predictions_path /tmp/preds.json --instance_ids i1", "FIRST_EXPERIMENT_TIMEOUT_SECONDS=2", "\n",
            ]), encoding="utf-8")
            args = [str(script), "--manifest", str(manifest), "--work-root", str(root / "work")]
            first = subprocess.run(args, capture_output=True, text=True, check=False)
            self.assertNotEqual(first.returncode, 0)
            config = root / "work" / "data" / "raw" / "e1" / "lite" / "i1" / "attempt-001" / "config.json"
            before = config.read_bytes()
            resumed = subprocess.run(args + ["--resume"], capture_output=True, text=True, check=False)
            self.assertNotEqual(resumed.returncode, 0)
            self.assertEqual(config.read_bytes(), before)

    def test_shell_thin_mode_observes_metrics_without_request_mutation(self):
        script = Path(__file__).resolve().parents[2] / "scripts" / "cloud" / "lambda_run_first_experiment.sh"

        class MetricsHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - stdlib handler API
                payload = b"vllm:request_success_total 1\nvllm:prompt_tokens_total 2\nvllm:generation_tokens_total 3\nvllm:e2e_request_latency_seconds 0.1\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args):
                return

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MetricsHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                output = root / "work/experiments/e1"
                output.mkdir(parents=True)
                command = (
                    "sh -c 'sleep 1; mkdir -p " + str(output) + "; "
                    "printf \"[{\\\"instance_id\\\":\\\"i1\\\",\\\"model_patch\\\":\\\"\\\"}]\\n\" > "
                    + str(output / "preds.json")
                    + "; printf trajectory > " + str(output / "i1.traj")
                    + "' run-batch --instances.type file --instances.path /tmp/i.json "
                    "--agent.model.name openai/Qwen --agent.model.api_base http://127.0.0.1:8000/v1 "
                    "--agent.model.api_key fixture --num_workers 1"
                )
                manifest = root / "manifest.env"
                manifest.write_text("\n".join([
                    "WORK_ROOT=" + str(root / "work"),
                    "FIRST_LITE_INSTANCE_ID=i1", "EXPERIMENT_ID=e1",
                    "SWE_AGENT_REVISION=0f3acafacabc0def8cc76b4e48acb4b6cf302cb9",
                    "SWE_BENCH_REVISION=726c5461e2ef52d83cf1ea2107870a8bb332d5",
                    "VLLM_MODEL_REVISION=b2cff646eb4bb1d68355c01b18ae02e7cf42d120",
                    "SWE_AGENT_COMMAND=" + command,
                    "SWE_AGENT_TELEMETRY_COMMAND=" + command,
                    "EVALUATE_COMMAND=true -m swebench.harness.run_evaluation --predictions_path " + str(output / "preds.json") + " --instance_ids i1",
                    "PREDICTION_PATH=" + str(output / "preds.json"),
                    "SWE_AGENT_OUTPUT_DIR=" + str(output),
                    "FIRST_EXPERIMENT_TIMEOUT_SECONDS=5",
                    "SWE_BENCH_TIMEOUT_SECONDS=5",
                    "TELEMETRY_INTERVAL_SECONDS=1",
                    "VLLM_METRICS_URL=http://127.0.0.1:" + str(server.server_port) + "/metrics",
                    "\n",
                ]), encoding="utf-8")
                env = os.environ.copy(); env["VLLM_API_KEY"] = "fixture"
                result = subprocess.run([
                    "bash", str(script), "--manifest", str(manifest), "--work-root", str(root / "work"),
                    "--mode", "thin-telemetry",
                ], cwd=script.parents[2], env=env, capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                raw = root / "work/data/raw/e1/lite/i1/attempt-001"
                contract = json.loads((raw / "telemetry_contract.json").read_text())
                self.assertEqual(contract["status"], "measured")
                events = [json.loads(line) for line in (raw / "events.jsonl").read_text().splitlines() if line]
                self.assertTrue(any(event["payload"]["correlation_scope"] == "run_interval" for event in events))
                self.assertFalse(contract["request_mutation"])
                self.assertEqual(json.loads((raw / "status.json").read_text())["status"], "completed")
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
