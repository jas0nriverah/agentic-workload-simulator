import json
import http.server
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from agentic_sim.runners import RunnerConfig, build_command, run_sweagent, validate_experiment_command
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

    def test_four_assignment_knobs_and_request_fields_are_concrete(self):
        command = build_command(
            instances_path="/tmp/tasks.json",
            instance_id="i1",
            output_dir="/tmp/o",
            model="openai/Qwen",
            model_revision="b2cff646eb4bb1",
            per_instance_call_limit=17,
            max_output_tokens=1536,
            max_observation_length=25_000,
            temperature=0.5,
            seed=2,
        )
        resolved = validate_experiment_command(command)
        self.assertEqual(resolved["per_instance_call_limit"], 17)
        self.assertEqual(resolved["max_output_tokens"], 1536)
        self.assertEqual(resolved["max_observation_length"], 25_000)
        self.assertEqual(resolved["temperature"], 0.5)
        self.assertEqual(resolved["seed"], 2)
        self.assertIn("--agent.model.completion_kwargs.max_tokens", command)
        self.assertIn("--agent.model.completion_kwargs.seed", command)
        self.assertIn("--agent.templates.max_observation_length", command)

    def test_contradictory_output_guard_is_rejected(self):
        command = build_command(instances_path="/tmp/tasks.json", instance_id="i1", output_dir="/tmp/o", model="openai/Qwen", model_revision="b2cff646eb4bb1")
        command[command.index("--agent.model.completion_kwargs.max_tokens") + 1] = "1024"
        with self.assertRaises(RunnerContractError):
            validate_experiment_command(command)

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
                "SWE_AGENT_COMMAND=false run-batch --instances.type file --instances.path /tmp/i.json --instances.filter '^i1$' --agent.model.name openai/Qwen --agent.model.api_base http://127.0.0.1:8000/v1 --agent.model.api_key fixture --agent.model.per_instance_call_limit 30 --agent.model.temperature 0.0 --agent.model.max_input_tokens 32768 --agent.model.max_output_tokens 2048 --agent.model.completion_kwargs.max_tokens 2048 --agent.model.completion_kwargs.seed 0 --agent.templates.max_observation_length 100000 --num_workers 1", "EVALUATE_COMMAND=true -m swebench.harness.run_evaluation --predictions_path /tmp/preds.json --instance_ids i1", "FIRST_EXPERIMENT_TIMEOUT_SECONDS=2", "\n",
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
                    + "' run-batch --instances.type file --instances.path /tmp/i.json --instances.filter '^i1$' "
                    "--agent.model.name openai/Qwen --agent.model.api_base http://127.0.0.1:8000/v1 "
                    "--agent.model.api_key fixture --agent.model.per_instance_call_limit 30 --agent.model.temperature 0.0 --agent.model.max_input_tokens 32768 --agent.model.max_output_tokens 2048 --agent.model.completion_kwargs.max_tokens 2048 --agent.model.completion_kwargs.seed 0 --agent.templates.max_observation_length 100000 --output_dir " + str(output) + " --num_workers 1"
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

    def test_shell_attempts_isolate_agent_and_evaluator_outputs(self):
        script = Path(__file__).resolve().parents[2] / "scripts" / "cloud" / "lambda_run_first_experiment.sh"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.env"
            base = root / "work" / "experiments" / "e1"
            command = (
                f"sweagent run-batch --instances.type file --instances.path {root / 'i.json'} "
                "--instances.filter '^i1$' --agent.model.name openai/Qwen/Qwen3-Coder-30B-A3B-Instruct "
                "--agent.model.api_base http://127.0.0.1:8000/v1 --agent.model.api_key fixture "
                f"--agent.model.per_instance_call_limit 30 --agent.model.temperature 0.0 --agent.model.max_input_tokens 32768 --agent.model.max_output_tokens 2048 --agent.model.completion_kwargs.max_tokens 2048 --agent.model.completion_kwargs.seed 0 --agent.templates.max_observation_length 100000 --output_dir {base} --num_workers 1"
            )
            evaluator = (
                f"python -m swebench.harness.run_evaluation --dataset_name {root / 'i.json'} "
                f"--predictions_path {base / 'preds.json'} --instance_ids i1 --run_id e1 "
                f"--report_dir {root / 'work' / 'artifacts' / 'e1' / 'evaluation'}"
            )
            manifest.write_text("\n".join([
                "FIRST_LITE_INSTANCE_ID=i1", "EXPERIMENT_ID=e1",
                "SWE_AGENT_REVISION=0f3acafacabc0def8cc76b4e48acb4b6cf302cb9",
                "SWE_BENCH_REVISION=726c5461e2ef52d83cf1ea2107870a8bb332d5",
                "VLLM_MODEL_REVISION=b2cff646eb4bb1d68355c01b18ae02e7cf42d120",
                "VLLM_MODEL=Qwen/Qwen3-Coder-30B-A3B-Instruct",
                f"LITE_DATASET_PATH={root / 'i.json'}", f"SWE_AGENT_COMMAND={command}",
                f"SWE_AGENT_TELEMETRY_COMMAND={command}", f"SWE_AGENT_OUTPUT_DIR={base}",
                f"EVALUATOR_REPORT_DIR={root / 'work' / 'artifacts' / 'e1' / 'evaluation'}",
                f"PREDICTION_PATH={base / 'preds.json'}", f"EVALUATE_COMMAND={evaluator}", "\n",
            ]), encoding="utf-8")
            env = os.environ.copy(); env["VLLM_API_KEY"] = "fixture"
            def dry(attempt):
                return subprocess.run([
                    "bash", str(script), "--manifest", str(manifest), "--instance-id", "i1",
                    "--experiment-id", "e1", "--attempt-id", attempt, "--dry-run",
                ], env=env, capture_output=True, text=True, check=False)
            control = dry("attempt-001"); thin = dry("attempt-002")
            self.assertEqual(control.returncode, 0, control.stderr)
            self.assertEqual(thin.returncode, 0, thin.stderr)
            self.assertIn(f"--output_dir {base / 'attempt-001'}", control.stdout)
            self.assertIn(f"--output_dir {base / 'attempt-002'}", thin.stdout)
            self.assertIn(f"--report_dir {root / 'work' / 'artifacts' / 'e1' / 'evaluation' / 'attempt-001'}", control.stdout)
            self.assertIn(f"--report_dir {root / 'work' / 'artifacts' / 'e1' / 'evaluation' / 'attempt-002'}", thin.stdout)
            self.assertNotEqual(control.stdout, thin.stdout)

    def test_official_evaluator_report_is_snapshotted_into_raw_attempt(self):
        script = Path(__file__).resolve().parents[2] / "scripts" / "cloud" / "lambda_run_first_experiment.sh"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_agent = root / "fake_agent.py"
            fake_agent.write_text(
                "import json, pathlib, sys\n"
                "out = pathlib.Path(sys.argv[sys.argv.index('--output_dir') + 1])\n"
                "out.mkdir(parents=True, exist_ok=True)\n"
                "(out / 'preds.json').write_text(json.dumps([{'instance_id': 'i1', 'model_patch': ''}]))\n",
                encoding="utf-8",
            )
            base = root / "work" / "experiments" / "e1"
            report_base = root / "work" / "artifacts" / "e1" / "evaluation"
            command = (
                f"{sys.executable} {fake_agent} run-batch --instances.type file --instances.path {root / 'i.json'} "
                "--instances.filter '^i1$' --agent.model.name openai/Qwen/Qwen3-Coder-30B-A3B-Instruct "
                "--agent.model.api_base http://127.0.0.1:8000/v1 --agent.model.api_key fixture "
                f"--agent.model.per_instance_call_limit 30 --agent.model.temperature 0.0 --agent.model.max_input_tokens 32768 --agent.model.max_output_tokens 2048 --agent.model.completion_kwargs.max_tokens 2048 --agent.model.completion_kwargs.seed 0 --agent.templates.max_observation_length 100000 --output_dir {base} --num_workers 1"
            )
            evaluator = (
                f"{sys.executable} -c 'import pathlib; pathlib.Path(\"official-report.json\").write_text(\"{{\\\"resolved_instances\\\":1}}\")' "
                f"-m swebench.harness.run_evaluation --predictions_path {base / 'preds.json'} --instance_ids i1 --run_id e1 "
                f"--report_dir {report_base}"
            )
            manifest = root / "manifest.env"
            manifest.write_text("\n".join([
                "WORK_ROOT=" + str(root / "work"), "FIRST_LITE_INSTANCE_ID=i1", "EXPERIMENT_ID=e1",
                "SWE_AGENT_REVISION=0f3acafacabc0def8cc76b4e48acb4b6cf302cb9",
                "SWE_BENCH_REVISION=726c5461e2ef52d83cf1ea2107870a8bb3328d57",
                "VLLM_MODEL_REVISION=b2cff646eb4bb1d68355c01b18ae02e7cf42d120",
                "VLLM_MODEL=Qwen/Qwen3-Coder-30B-A3B-Instruct", f"LITE_DATASET_PATH={root / 'i.json'}",
                f"SWE_AGENT_COMMAND={command}", f"SWE_AGENT_TELEMETRY_COMMAND={command}",
                f"SWE_AGENT_OUTPUT_DIR={base}", f"EVALUATOR_REPORT_DIR={report_base}",
                f"PREDICTION_PATH={base / 'preds.json'}", f"EVALUATE_COMMAND={evaluator}", "\n",
            ]), encoding="utf-8")
            result = subprocess.run([
                "bash", str(script), "--manifest", str(manifest), "--instance-id", "i1",
                "--experiment-id", "e1", "--attempt-id", "attempt-001",
            ], cwd=script.parents[2], capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            raw = root / "work" / "data" / "raw" / "e1" / "lite" / "i1" / "attempt-001"
            report = raw / "evaluator_report" / "official-report.json"
            self.assertTrue(report.is_file(), sorted(str(p) for p in raw.rglob("*")))
            self.assertEqual(json.loads(report.read_text())["resolved_instances"], 1)
            evaluation = json.loads((raw / "eval.json").read_text())
            self.assertEqual(evaluation["report_snapshot"], "evaluator_report")
            self.assertIn("official-report.json", evaluation["report_files"])


if __name__ == "__main__":
    unittest.main()
