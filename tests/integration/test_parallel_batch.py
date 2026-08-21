import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agentic_sim.runners import build_command
from agentic_sim.runners.parallel import (
    ParallelBatchError,
    build_worker_agent_command,
    build_worker_evaluator_command,
    completed_instance_ids,
    file_sha256,
    load_instance_rows,
    plan_shards,
    validate_batch_manifest,
    write_batch_plan,
)


def rows(count=5):
    return [{"instance_id": f"repo__case-{index}", "problem_statement": f"case {index}"} for index in range(count)]


class ParallelBatchTests(unittest.TestCase):
    def test_round_robin_assignments_are_disjoint_and_ordered(self):
        values = rows()
        plans = plan_shards(
            values,
            batch_id="batch-1",
            experiment_id="exp-1",
            dataset="lite",
            source_dataset="/datasets/lite.parquet",
            source_sha256="a" * 64,
            shard_count=2,
        )
        self.assertEqual([list(plan.instance_ids) for plan in plans], [
            ["repo__case-0", "repo__case-2", "repo__case-4"],
            ["repo__case-1", "repo__case-3"],
        ])
        self.assertEqual(set(plans[0].instance_ids).intersection(plans[1].instance_ids), set())
        self.assertEqual(set(item for plan in plans for item in plan.instance_ids), {row["instance_id"] for row in values})

    def test_resume_excludes_only_final_successes(self):
        values = rows(3)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            summary = root / "data/raw/exp-1/lite/repo__case-0/attempt-001/summary.json"
            summary.parent.mkdir(parents=True)
            summary.write_text(json.dumps({
                "instance_id": "repo__case-0", "status": "completed",
                "agent_returncode": 0, "evaluator_returncode": 0,
            }))
            failed = root / "data/raw/exp-1/lite/repo__case-1/attempt-001/summary.json"
            failed.parent.mkdir(parents=True)
            failed.write_text(json.dumps({
                "instance_id": "repo__case-1", "status": "runner_failed",
                "agent_returncode": 7, "evaluator_returncode": None,
            }))
            done = completed_instance_ids(root, experiment_id="exp-1", dataset="lite")
            self.assertEqual(done, {"repo__case-0"})
            plans = plan_shards(
                values, batch_id="batch-2", experiment_id="exp-1", dataset="lite",
                source_dataset="/datasets/lite.json", source_sha256="b" * 64,
                shard_count=2, completed_instance_ids=done,
            )
            self.assertNotIn("repo__case-0", {item for plan in plans for item in plan.instance_ids})
            self.assertIn("repo__case-1", {item for plan in plans for item in plan.instance_ids})

    def test_plan_round_trip_and_source_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset = root / "dataset.json"
            dataset.write_text(json.dumps(rows()), encoding="utf-8")
            loaded = load_instance_rows(dataset)
            plans = plan_shards(
                loaded, batch_id="b", experiment_id="e", dataset="verified",
                source_dataset=str(dataset), source_sha256=file_sha256(dataset), shard_count=3,
            )
            manifest = write_batch_plan(plans, loaded, output_root=root / "batch")
            payload = json.loads(manifest.read_text())
            self.assertEqual(payload["schema_version"], "parallel-batch.v1")
            self.assertEqual(payload["row_count"], 5)
            self.assertTrue((root / "batch/worker-00/instances.json").is_file())
            self.assertEqual(json.loads((root / "batch/worker-01/shard.json").read_text())["shard_index"], 1)

    def test_batch_manifest_rejects_overlapping_workers(self):
        with self.assertRaises(ParallelBatchError):
            validate_batch_manifest({
                "schema_version": "parallel-batch.v1",
                "assignment_algorithm": "round_robin_v1",
                "all_instance_ids": ["repo__a", "repo__b"],
                "pending_instance_ids": ["repo__a", "repo__b"],
                "completed_instance_ids": [],
                "workers": [
                    {"shard_index": 0, "shard_count": 2, "instance_ids": ["repo__a"], "row_sha256": ["a"]},
                    {"shard_index": 1, "shard_count": 2, "instance_ids": ["repo__a"], "row_sha256": ["b"]},
                ],
            })

    def test_command_rewrite_removes_filter_but_preserves_single_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = Path(tmp) / "request.yaml"
            request.write_text('{"agent":{"model":{"completion_kwargs":{"max_tokens":2048,"seed":0}}}}\n')
            command = build_command(
                instances_path="/source.json", instance_id="repo__case-0", output_dir="/old",
                model="openai/Qwen/Qwen3-Coder-30B-A3B-Instruct",
                model_revision="b2cff646eb4bb1d68355c01b18ae02e7cf42d120",
                request_config_path=request,
            )
            rewritten = build_worker_agent_command(
                command, instances_path="/worker/instances.json", output_dir="/worker/output"
            )
            self.assertNotIn("--instances.filter", rewritten)
            self.assertEqual(rewritten[rewritten.index("--instances.path") + 1], "/worker/instances.json")
            self.assertEqual(rewritten[rewritten.index("--num_workers") + 1], "1")
            self.assertEqual(rewritten[rewritten.index("--output_dir") + 1], "/worker/output")

    def test_evaluator_rewrite_replaces_all_ids(self):
        command = [
            sys.executable, "-m", "swebench.harness.run_evaluation", "--dataset_name", "/old.json",
            "--predictions_path", "/old/preds.json", "--instance_ids", "old__one", "--run_id", "old",
            "--report_dir", "/old/report", "--max_workers", "1",
        ]
        rewritten = build_worker_evaluator_command(
            command, dataset_path="/worker/source.json", predictions_path="/worker/preds.json",
            instance_ids=["repo__a", "repo__b"], report_dir="/worker/report", run_id="batch-worker-00",
        )
        self.assertEqual(rewritten[rewritten.index("--dataset_name") + 1], "/worker/source.json")
        start = rewritten.index("--instance_ids") + 1
        end = rewritten.index("--run_id")
        self.assertEqual(rewritten[start:end], ["repo__a", "repo__b"])

    def test_dry_run_worker_never_starts_commands(self):
        script_root = Path(__file__).resolve().parents[2]
        planner = script_root / "scripts/cloud/plan_parallel_batch.py"
        runner = script_root / "scripts/cloud/lambda_run_parallel_shard.py"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset = root / "dataset.json"
            dataset.write_text(json.dumps(rows(2)), encoding="utf-8")
            request = root / "request.yaml"
            request.write_text('{"agent":{"model":{"completion_kwargs":{"max_tokens":2048,"seed":0}}}}\n')
            agent = (
                f"{sys.executable} -c 'raise SystemExit(99)' run-batch --config /tmp/default.yaml "
                f"--config {request} --instances.type file --instances.path {dataset} --instances.filter '^repo__case-0$' "
                "--agent.model.name openai/Qwen --agent.model.api_base http://127.0.0.1:8000/v1 "
                "--agent.model.api_key '$VLLM_API_KEY' --agent.model.per_instance_call_limit 30 "
                "--agent.model.temperature 0.0 --agent.model.max_input_tokens 32768 --agent.model.max_output_tokens 2048 "
                "--agent.templates.max_observation_length 100000 --output_dir /old --num_workers 1"
            )
            evaluator = f"{sys.executable} -c 'raise SystemExit(99)' -m swebench.harness.run_evaluation --dataset_name {dataset} --predictions_path /old/preds.json --instance_ids repo__case-0 --run_id old --report_dir /old/report"
            manifest = root / "manifest.env"
            manifest.write_text(f"SWE_AGENT_COMMAND={agent}\nEVALUATE_COMMAND={evaluator}\n")
            batch_root = root / "batch"
            subprocess.run([
                sys.executable, str(planner), "--dataset", str(dataset), "--output-root", str(batch_root),
                "--batch-id", "b", "--experiment-id", "e", "--dataset-name", "lite", "--shards", "2",
            ], check=True, capture_output=True, text=True)
            result = subprocess.run([
                sys.executable, str(runner), "--manifest", str(manifest), "--batch-manifest",
                str(batch_root / "batch_manifest.json"), "--worker-index", "0", "--dry-run",
            ], check=False, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("parallel worker: DRY-RUN", result.stdout)
            self.assertIn("--instances.path", result.stdout)
            self.assertFalse((batch_root / "worker-00/attempt-001").exists())

    def test_worker_executes_agent_then_official_evaluator_in_isolated_attempt(self):
        script_root = Path(__file__).resolve().parents[2]
        planner = script_root / "scripts/cloud/plan_parallel_batch.py"
        runner = script_root / "scripts/cloud/lambda_run_parallel_shard.py"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset = root / "dataset.json"
            dataset.write_text(json.dumps(rows(1)), encoding="utf-8")
            agent = root / "agent.py"
            agent.write_text(
                "import json, pathlib, sys\n"
                "out = pathlib.Path(sys.argv[sys.argv.index('--output_dir') + 1])\n"
                "out.mkdir(parents=True, exist_ok=True)\n"
                "(out / 'preds.json').write_text(json.dumps([{'instance_id': 'repo__case-0', 'model_patch': ''}]))\n"
            )
            evaluator = root / "evaluator.py"
            evaluator.write_text(
                "import pathlib, sys\n"
                "report = pathlib.Path(sys.argv[sys.argv.index('--report_dir') + 1])\n"
                "report.mkdir(parents=True, exist_ok=True)\n"
                "(report / 'official.json').write_text('{\"resolved\": 0}')\n"
            )
            request = root / "request.yaml"
            request.write_text('{"agent":{"model":{"completion_kwargs":{"max_tokens":2048,"seed":0}}}}\n')
            agent_command = (
                f"{sys.executable} {agent} run-batch --config /tmp/default.yaml --config {request} "
                f"--instances.type file --instances.path {dataset} --instances.filter '^repo__case-0$' "
                "--agent.model.name openai/Qwen --agent.model.api_base http://127.0.0.1:8000/v1 "
                "--agent.model.api_key '$VLLM_API_KEY' --agent.model.per_instance_call_limit 30 "
                "--agent.model.temperature 0.0 --agent.model.max_input_tokens 32768 --agent.model.max_output_tokens 2048 "
                "--agent.templates.max_observation_length 100000 --output_dir /old --num_workers 1"
            )
            evaluator_command = (
                f"{sys.executable} {evaluator} --dataset_name {dataset} --predictions_path /old/preds.json "
                "--instance_ids repo__case-0 --run_id old --report_dir /old/report"
            )
            manifest = root / "manifest.env"
            manifest.write_text(f"SWE_AGENT_COMMAND={agent_command}\nEVALUATE_COMMAND={evaluator_command}\n")
            batch_root = root / "batch"
            subprocess.run([
                sys.executable, str(planner), "--dataset", str(dataset), "--output-root", str(batch_root),
                "--batch-id", "b", "--experiment-id", "e", "--dataset-name", "lite", "--shards", "1",
            ], check=True, capture_output=True, text=True)
            env = os.environ.copy()
            env["VLLM_API_KEY"] = "fixture-secret"
            result = subprocess.run([
                sys.executable, str(runner), "--manifest", str(manifest), "--batch-manifest",
                str(batch_root / "batch_manifest.json"), "--worker-index", "0",
            ], env=env, check=False, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            status = json.loads((batch_root / "worker-00/attempt-001/worker_status.json").read_text())
            self.assertEqual(status["status"], "completed")
            self.assertEqual(status["agent_returncode"], 0)
            self.assertEqual(status["evaluator_returncode"], 0)
            self.assertTrue((batch_root / "worker-00/attempt-001/evaluator_report/official.json").is_file())
            self.assertNotIn("fixture-secret", (batch_root / "worker-00/attempt-001/worker_status.json").read_text())


if __name__ == "__main__":
    unittest.main()
