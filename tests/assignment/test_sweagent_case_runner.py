import hashlib
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.assignment.adaptive_event_protocol import (
    FrozenCalibrationModel,
    freeze_calibration_model,
    freeze_trajectory_prediction,
)


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/assignment/sweagent_case_runner.py"
SPEC = importlib.util.spec_from_file_location("assignment_sweagent_case_runner", SCRIPT)
assert SPEC and SPEC.loader
ADAPTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ADAPTER)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def make_repo(root: Path) -> tuple[Path, str]:
    repo = root / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "fixture@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Fixture"], check=True)
    (repo / "config").mkdir()
    (repo / "config/default.yaml").write_text("agent: {}\n", encoding="utf-8")
    (repo / "request.json").write_text(json.dumps({"agent": {"model": {"completion_kwargs": {"max_tokens": 2048, "seed": 0}}}}) + "\n", encoding="utf-8")
    (repo / "lite.jsonl").write_text(json.dumps({"instance_id": "owner__repo-1"}) + "\n", encoding="utf-8")
    (repo / "verified.jsonl").write_text(json.dumps({"instance_id": "owner__repo-1"}) + "\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("clean\n", encoding="utf-8")
    (repo / "scripts/observability").mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)
    return repo, git(repo, "rev-parse", "HEAD")


def write_executable(path: Path, source: str) -> None:
    path.write_text("#!/usr/bin/env python3\n" + source, encoding="utf-8")
    path.chmod(0o755)


def write_fixture(root: Path, *, evaluator_mode: str = "valid") -> dict[str, Path]:
    fake_uv = root / "uv"
    write_executable(fake_uv, """
import os, sys
args = sys.argv[1:]
if args[:1] == ['run']:
    args = args[1:]
if args[:1] == ['--project']:
    args = args[2:]
os.execv(args[0], args)
""")
    fake_agent = root / "fake_sweagent.py"
    write_executable(fake_agent, """
import json, os, pathlib, sys, threading, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
args = sys.argv[1:]
out = pathlib.Path(args[args.index('--output_dir') + 1])
out.mkdir(parents=True, exist_ok=True)
(out / 'trajectory.json').write_text(json.dumps({'trajectory': [{'action': 'echo test', 'execution_time': 0.01}]}) + '\\n')
if os.environ.get('FIXTURE_AGENT_MODE', 'request') == 'request':
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return
        def do_POST(self):
            length = int(self.headers.get('Content-Length', '0'))
            self.rfile.read(length)
            body = b'{"id":"fixture","choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}'
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    class ReusableServer(ThreadingHTTPServer):
        allow_reuse_address = True
    upstream = ReusableServer(('127.0.0.1', int(os.environ.get('FIXTURE_UPSTREAM_PORT', '18080'))), Handler)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    api_base = args[args.index('--agent.model.api_base') + 1]
    request = urllib.request.Request(api_base + '/chat/completions', data=b'{"model":"fixture","messages":[]}', headers={'Content-Type':'application/json'}, method='POST')
    with urllib.request.urlopen(request, timeout=5) as response:
        response.read()
    upstream.shutdown()
    upstream.server_close()
(out / 'preds.json').write_text(json.dumps([{'instance_id': 'owner__repo-1', 'model_name_or_path': 'fixture/model', 'model_patch': 'diff --git a/a b/a'}]) + '\\n')
(out / 'agent_success.marker').write_text('ran\\n')
""")
    evaluator = root / "fake_evaluator.py"
    write_executable(evaluator, f"""
import hashlib, json, pathlib, sys
args = sys.argv[1:]
def value(name):
    return args[args.index(name) + 1]
mode = {evaluator_mode!r}
if mode == 'missing':
    raise SystemExit(0)
result = pathlib.Path(value('--result')).resolve()
dataset = pathlib.Path(value('--dataset')).resolve()
predictions = pathlib.Path(value('--predictions')).resolve()
report_dir = pathlib.Path(value('--report-dir')).resolve()
instance_id = value('--instance-id')
run_id = value('--run-id')
report_dir.mkdir(parents=True, exist_ok=True)
report = report_dir / 'official-report.json'
report.write_text(json.dumps({{'instance_id': instance_id, 'run_id': run_id}}) + '\\n', encoding='utf-8')
sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
record = {{
    'schema_version': 'assignment-official-evaluator.v1',
    'official_resolved': True,
    'submitted': True,
    'instance_id': instance_id,
    'run_id': run_id,
    'report_path': str(report),
    'report_sha256': sha(report),
    'dataset_path': str(dataset),
    'dataset_sha256': sha(dataset),
    'predictions_path': str(predictions),
    'predictions_sha256': sha(predictions),
    'evaluator_dataset_sha256': sha(dataset),
    'evaluator_predictions_sha256': sha(predictions),
    'command_sha256': hashlib.sha256(b'fixture-command').hexdigest(),
    'counts': {{'total_instances': 1, 'submitted_instances': 1, 'completed_instances': 1, 'resolved_instances': 1, 'unresolved_instances': 0, 'error_instances': 0}},
    'evaluator_python': sys.executable,
    'timeout_seconds': 5,
}}
if mode == 'dataset-mismatch':
    record['dataset_sha256'] = '0' * 64
elif mode == 'predictions-mismatch':
    record['predictions_sha256'] = '1' * 64
elif mode == 'report-hash-mismatch':
    record['report_sha256'] = '2' * 64
elif mode == 'dataset-path-mismatch':
    record['dataset_path'] = str(result.parent / 'other-dataset.jsonl')
elif mode == 'external-report':
    external = result.parent.parent / 'external-report.json'
    external.write_text(report.read_text(encoding='utf-8'), encoding='utf-8')
    record['report_path'] = str(external)
    record['report_sha256'] = sha(external)
elif mode == 'run-id-mismatch':
    record['run_id'] = 'wrong-run-id'
elif mode == 'counts-mismatch':
    record['counts']['resolved_instances'] = 0
elif mode == 'missing-provenance':
    del record['command_sha256']
result.parent.mkdir(parents=True, exist_ok=True)
result.write_text(json.dumps(record) + '\\n', encoding='utf-8')
""")
    probe = root / "fake_probe.py"
    write_executable(probe, "print('NVIDIA H100 80GB HBM3, 81559, 9.0')\n")
    return {"uv": fake_uv, "agent": fake_agent, "evaluator": evaluator, "probe": probe}


def write_manifest(path: Path, repo: Path, commit: str, fixture: dict[str, Path]) -> None:
    reviewed_evaluator = repo / "scripts/assignment/evaluate_swebench_case.py"
    reviewed_evaluator.parent.mkdir(parents=True, exist_ok=True)
    reviewed_evaluator.write_bytes(fixture["evaluator"].read_bytes())
    reviewed_evaluator.chmod(0o755)
    subprocess.run(["git", "-C", str(repo), "add", str(reviewed_evaluator.relative_to(repo))], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "add reviewed evaluator"], check=True)
    reviewed_proxy = repo / "scripts/observability/request_proxy.py"
    reviewed_proxy.parent.mkdir(parents=True, exist_ok=True)
    reviewed_proxy.write_bytes((ROOT / "scripts/observability/request_proxy.py").read_bytes())
    reviewed_proxy.chmod(0o755)
    subprocess.run(["git", "-C", str(repo), "add", str(reviewed_proxy.relative_to(repo))], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "add reviewed request proxy"], check=True)
    reviewed_sources = {
        "adaptive_runner": "scripts/assignment/sweagent_adaptive_runner.py",
        "adaptive_runtime": "scripts/assignment/adaptive_runtime.py",
        "adaptive_protocol": "scripts/assignment/adaptive_event_protocol.py",
        "event_simulator": "src/agentic_sim/assignment/event_simulator.py",
    }
    reviewed_paths = {}
    for name, relative in reviewed_sources.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / relative).read_bytes())
        reviewed_paths[name] = target.resolve()
    subprocess.run(["git", "-C", str(repo), "add", *reviewed_sources.values()], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "add reviewed adaptive runtime"], check=True)
    commit = git(repo, "rev-parse", "HEAD")
    lite_sha = hashlib.sha256((repo / "lite.jsonl").read_bytes()).hexdigest()
    verified_sha = hashlib.sha256((repo / "verified.jsonl").read_bytes()).hexdigest()
    manifest = {
        "schema_version": ADAPTER.MANIFEST_SCHEMA,
        "required_branch": git(repo, "branch", "--show-current"),
        "required_commit": commit,
        "repository_root": str(repo),
        "integrity": {
            "case_runner_path": str(SCRIPT.resolve()),
            "case_runner_sha256": hashlib.sha256(SCRIPT.read_bytes()).hexdigest(),
            "evaluator_adapter_path": str(reviewed_evaluator.resolve()),
            "evaluator_adapter_sha256": hashlib.sha256(reviewed_evaluator.read_bytes()).hexdigest(),
            "request_config_path": str((repo / "request.json").resolve()),
            "request_config_sha256": hashlib.sha256((repo / "request.json").read_bytes()).hexdigest(),
            "request_proxy_path": str(reviewed_proxy.resolve()),
            "request_proxy_sha256": hashlib.sha256(reviewed_proxy.read_bytes()).hexdigest(),
            "adaptive_runner_path": str(reviewed_paths["adaptive_runner"]),
            "adaptive_runner_sha256": hashlib.sha256(reviewed_paths["adaptive_runner"].read_bytes()).hexdigest(),
            "adaptive_runtime_path": str(reviewed_paths["adaptive_runtime"]),
            "adaptive_runtime_sha256": hashlib.sha256(reviewed_paths["adaptive_runtime"].read_bytes()).hexdigest(),
            "adaptive_protocol_path": str(reviewed_paths["adaptive_protocol"]),
            "adaptive_protocol_sha256": hashlib.sha256(reviewed_paths["adaptive_protocol"].read_bytes()).hexdigest(),
            "event_simulator_path": str(reviewed_paths["event_simulator"]),
            "event_simulator_sha256": hashlib.sha256(reviewed_paths["event_simulator"].read_bytes()).hexdigest(),
        },
        "pins": {"model_revision": "b" * 40, "tokenizer_revision": "c" * 40, "swe_agent_revision": commit, "swe_bench_revision": commit, "vllm_version": "0.10.0"},
        "datasets": {"lite": {"name": "lite", "revision": "1" * 40, "instances_path": "lite.jsonl", "sha256": lite_sha}, "verified": {"name": "verified", "revision": "2" * 40, "instances_path": "verified.jsonl", "sha256": verified_sha}},
        "model": {"name": "Qwen/Qwen3-Coder-30B-A3B-Instruct", "revision": "b" * 40, "api_base": "http://127.0.0.1:18080/v1", "api_key": "EMPTY"},
        "runner": {"executable": str(fixture["agent"]), "project": str(repo), "config_path": "config/default.yaml", "request_config_path": "request.json", "working_directory": str(repo), "extra_args": []},
        "evaluator": {"command": [sys.executable, str(reviewed_evaluator), "--dataset", "{dataset_path}", "--predictions", "{predictions_path}", "--instance-id", "{instance_id}", "--run-id", "{run_id}", "--report-dir", "{report_dir}", "--result", "{evaluator_result}"], "project": str(repo), "result_path": "{output_dir}/evaluator_result.json", "resolved_field": "official_resolved", "submitted_field": "submitted"},
        "hardware": {"gpu_names": ["NVIDIA H100 80GB HBM3"], "minimum_memory_mib": 80000, "compute_capability": "9.0", "one_gpu_only": True, "probe_command": [sys.executable, str(fixture["probe"])]},
        "deadlines": {"per_case_seconds": 5, "global_seconds": 30},
    }
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    Path(str(path) + ".sha256").write_text(f"{digest}  {path.name}\n", encoding="utf-8")


def write_case(path: Path) -> None:
    case = {"record_type": "case", "schema_version": ADAPTER.CASE_SCHEMA, "plan_id": "fixture", "steps": [1], "roles": ["step_1_baseline"], "suite": "lite", "instance_id": "owner__repo-1", "repository": "owner/repo", "task_sha256": "a" * 64, "source_manifest_sha256": "b" * 64, "cell_id": "shared-baseline", "settings": {"call_limit": 30, "max_output_tokens": 2048, "observation_length": 100000, "temperature": 0.0}, "variation": None, "concurrency": 1, "per_case_deadline_seconds": 5, "resume_key": "assignment-case-v1:fixture"}
    path.write_text(json.dumps(case, sort_keys=True) + "\n", encoding="utf-8")


def write_adaptive_config(path: Path, manifest: Path, case: Path) -> None:
    case_value = json.loads(case.read_text(encoding="utf-8"))
    run_id = "assignment-" + hashlib.sha256(case_value["resume_key"].encode()).hexdigest()[:16]
    manifest_sha256 = hashlib.sha256(manifest.read_bytes()).hexdigest()
    directory = path.parent.parent
    hardware = {
        "schema_version": "assignment.hardware-profile.v1",
        "hardware_id": "fixture-adaptive-h100",
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
    def write_hashed(path: Path, value: object) -> str:
        payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
        path.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        path.with_suffix(".sha256").write_text(f"{digest}  {path.name}\n", encoding="utf-8")
        return digest

    split_path = directory / "event-split.json"
    split_sha256 = write_hashed(
        split_path,
        {
            "schema_version": "assignment.event-split-manifest.v1",
            "calibration_run_ids": ["calibration-1"],
            "holdout_run_ids": [run_id],
        },
    )
    hardware_path = directory / "hardware-profile.json"
    hardware_sha256 = write_hashed(hardware_path, hardware)
    model_path = directory / "calibration-model.json"
    model_revision_sha256 = hashlib.sha256(("b" * 40).encode()).hexdigest()
    freeze_calibration_model(
        {
            "tool_event": {"coefficients": [10.0] + [0.0] * 13},
            "model_event": {"coefficients": [20.0] + [0.0] * 7},
            "trajectory": {"coefficients": [100.0] + [0.0] * 4},
        },
        model_path,
        calibration_run_ids=["calibration-1"],
        split_manifest_sha256=split_sha256,
        runtime_manifest_sha256=manifest_sha256,
        hardware_profile_sha256=hardware_sha256,
        model_revision_sha256=model_revision_sha256,
    )
    tokenizer = directory / "tokenizer"
    tokenizer.mkdir(exist_ok=True)
    tokenizer_hashes = {}
    for name in ("tokenizer.json", "tokenizer_config.json"):
        candidate = tokenizer / name
        candidate.write_text("{}\n", encoding="utf-8")
        tokenizer_hashes[name] = hashlib.sha256(candidate.read_bytes()).hexdigest()
    e2e_prediction = directory / "e2e-prediction.json"
    forecast_hardware = dict(hardware)
    freeze_trajectory_prediction(
        FrozenCalibrationModel.load(model_path),
        run_id=run_id,
        hardware=forecast_hardware,
        tool_events=[{
            "schema_version": "assignment.tool-event-input.v1",
            "event_id": "forecast-tool-1",
            "run_id": run_id,
            "split": "holdout",
            "operation_class": "read",
            "declared_command_bytes": 128,
            "declared_read_bytes": 0,
            "declared_write_bytes": 0,
            "declared_path_count": 1,
            "hardware": forecast_hardware,
        }],
        model_events=[{
            "schema_version": "assignment.model-event-input.v1",
            "request_id": "forecast-model-1",
            "run_id": run_id,
            "split": "holdout",
            "input_tokens": 128,
            "context_tokens": 128,
            "max_output_tokens": 128,
            "hardware": forecast_hardware,
        }],
        output_path=e2e_prediction,
    )
    e2e_prediction_sha256 = hashlib.sha256(e2e_prediction.read_bytes()).hexdigest()
    value = {
        "schema_version": "assignment.adaptive-runtime-config.v1",
        "run_id": run_id,
        "protocol_root": str((path.parent / "adaptive-protocol").resolve()),
        "calibration_model_path": str(model_path.resolve()),
        "split_manifest_path": str(split_path.resolve()),
        "runtime_manifest_path": str(manifest.resolve()),
        "hardware_profile_path": str(hardware_path.resolve()),
        "bindings": {
            "split_manifest_sha256": split_sha256,
            "runtime_manifest_sha256": manifest_sha256,
            "hardware_profile_sha256": hardware_sha256,
            "model_revision_sha256": model_revision_sha256,
        },
        "tokenizer": {
            "snapshot_path": str(tokenizer.resolve()),
            "revision": "b" * 40,
            "required_files_sha256": tokenizer_hashes,
        },
        "pre_trajectory_e2e": {
            "predicted_ms": 100.0,
            "prediction_artifact_path": str(e2e_prediction.resolve()),
            "prediction_artifact_sha256": e2e_prediction_sha256,
        },
    }
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    Path(str(path) + ".sha256").write_text(f"{digest}  {path.name}\n", encoding="utf-8")


def invoke(manifest: Path, case: Path, output: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), "--runtime-manifest", str(manifest), "--case-spec", str(case), "--output-dir", str(output), *extra], capture_output=True, text=True, check=False)


class SWEAgentCaseRunnerTests(unittest.TestCase):
    def test_reviewed_runner_is_directly_executable_by_matrix(self):
        self.assertTrue(os.access(SCRIPT, os.X_OK))

    def test_default_is_validate_only_and_launches_no_runner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, commit = make_repo(root)
            fixture = write_fixture(root)
            manifest = root / "manifest.json"
            write_manifest(manifest, repo, commit, fixture)
            output = root / "case"
            output.mkdir()
            case = output / "case_spec.json"
            write_case(case)
            result = invoke(manifest, case, output)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((root / "agent_success.marker").exists())
            self.assertEqual(json.loads((output / "validation.json").read_text())["status"], "passed")
            self.assertFalse((output / "case_result.json").exists())

    def test_adaptive_config_is_bound_during_validate_only_without_launching(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, commit = make_repo(root)
            fixture = write_fixture(root)
            manifest = root / "manifest.json"
            write_manifest(manifest, repo, commit, fixture)
            output = root / "case"
            output.mkdir()
            case = output / "case_spec.json"
            write_case(case)
            adaptive = output / "adaptive-runtime.json"
            write_adaptive_config(adaptive, manifest, case)

            result = invoke(manifest, case, output, "--adaptive-runtime-config", str(adaptive))

            self.assertEqual(result.returncode, 0, result.stderr)
            validation = json.loads((output / "validation.json").read_text())
            self.assertEqual(validation["adaptive_runtime"]["config_path"], str(adaptive.resolve()))
            self.assertEqual(validation["adaptive_runtime"]["pre_trajectory_e2e_ms"], 100.0)
            self.assertFalse((root / "agent_success.marker").exists())
            self.assertFalse((output / "case_result.json").exists())

    def test_adaptive_config_runtime_manifest_binding_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, commit = make_repo(root)
            fixture = write_fixture(root)
            manifest = root / "manifest.json"
            write_manifest(manifest, repo, commit, fixture)
            output = root / "case"
            output.mkdir()
            case = output / "case_spec.json"
            write_case(case)
            adaptive = output / "adaptive-runtime.json"
            write_adaptive_config(adaptive, manifest, case)
            value = json.loads(adaptive.read_text())
            value["bindings"]["runtime_manifest_sha256"] = "0" * 64
            adaptive.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            digest = hashlib.sha256(adaptive.read_bytes()).hexdigest()
            Path(str(adaptive) + ".sha256").write_text(f"{digest}  {adaptive.name}\n", encoding="utf-8")

            result = invoke(manifest, case, output, "--adaptive-runtime-config", str(adaptive))

            self.assertEqual(result.returncode, 1)
            self.assertIn("manifest SHA-256 binding mismatch", result.stderr)
            self.assertFalse((output / "runner_state.json").exists())

    def test_adaptive_validate_only_rejects_missing_calibration_dependency(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, commit = make_repo(root)
            fixture = write_fixture(root)
            manifest = root / "manifest.json"
            write_manifest(manifest, repo, commit, fixture)
            output = root / "case"
            output.mkdir()
            case = output / "case_spec.json"
            write_case(case)
            adaptive = output / "adaptive-runtime.json"
            write_adaptive_config(adaptive, manifest, case)
            calibration_model = root / "calibration-model.json"
            calibration_model.unlink()
            calibration_model.with_suffix(".sha256").unlink()

            result = invoke(
                manifest,
                case,
                output,
                "--adaptive-runtime-config",
                str(adaptive),
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("adaptive calibration model", result.stderr)
            self.assertFalse((output / "runner_state.json").exists())

    def test_execute_uses_reviewed_runner_and_records_hashed_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, commit = make_repo(root)
            fixture = write_fixture(root)
            manifest = root / "manifest.json"
            write_manifest(manifest, repo, commit, fixture)
            output = root / "case"
            output.mkdir()
            case = output / "case_spec.json"
            write_case(case)
            env = os.environ.copy()
            env["PATH"] = f"{root}{os.pathsep}{env.get('PATH', '')}"
            env["PYTHONPATH"] = str(ROOT / "src")
            result = subprocess.run([sys.executable, str(SCRIPT), "--runtime-manifest", str(manifest), "--case-spec", str(case), "--output-dir", str(output), "--execute"], capture_output=True, text=True, check=False, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads((output / "case_result.json").read_text())
            self.assertEqual(record["status"], "completed")
            self.assertTrue(record["evaluator"]["official_resolved"])
            self.assertTrue(any(item["kind"] == "trajectory" for item in record["artifacts"]))
            self.assertTrue(all(len(item["sha256"]) == 64 for item in record["artifacts"]))
            self.assertEqual(json.loads((output / "runner_state.json").read_text())["status"], "completed")

    def test_proxy_routes_to_manifest_upstream_and_is_reaped(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, commit = make_repo(root)
            fixture = write_fixture(root)
            manifest = root / "manifest.json"
            write_manifest(manifest, repo, commit, fixture)
            output = root / "case"
            output.mkdir()
            case = output / "case_spec.json"
            write_case(case)
            env = os.environ.copy()
            env["PATH"] = f"{root}{os.pathsep}{env.get('PATH', '')}"
            env["PYTHONPATH"] = str(ROOT / "src")
            result = subprocess.run([sys.executable, str(SCRIPT), "--runtime-manifest", str(manifest), "--case-spec", str(case), "--output-dir", str(output), "--execute"], capture_output=True, text=True, check=False, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads((output / "case_result.json").read_text())
            proxy = record["proxy"]
            self.assertEqual(proxy["upstream_api_base"], "http://127.0.0.1:18080/v1")
            self.assertNotEqual(proxy["listen_api_base"], proxy["upstream_api_base"])
            self.assertEqual(proxy["event_count"], 1)
            event = json.loads((output / "runner_attempts/attempt-001/request_proxy.jsonl").read_text())
            self.assertEqual(event["status_code"], 200)
            self.assertEqual(event["path"], "/v1/chat/completions")
            listen_port = int(proxy["listen_api_base"].rsplit(":", 1)[1].split("/", 1)[0])
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.settimeout(1)
                with self.assertRaises(OSError):
                    probe.connect(("127.0.0.1", listen_port))

    def test_proxy_integrity_mismatch_is_rejected_before_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, commit = make_repo(root)
            fixture = write_fixture(root)
            manifest = root / "manifest.json"
            write_manifest(manifest, repo, commit, fixture)
            value = json.loads(manifest.read_text())
            value["integrity"]["request_proxy_sha256"] = "1" * 64
            manifest.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
            digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
            Path(str(manifest) + ".sha256").write_text(f"{digest}  {manifest.name}\n", encoding="utf-8")
            output = root / "case"
            output.mkdir()
            case = output / "case_spec.json"
            write_case(case)
            result = invoke(manifest, case, output)
            self.assertEqual(result.returncode, 1)
            self.assertIn("request_proxy SHA-256 mismatch", result.stderr)
            self.assertFalse((output / "runner_state.json").exists())

    def test_empty_proxy_events_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, commit = make_repo(root)
            fixture = write_fixture(root)
            manifest = root / "manifest.json"
            write_manifest(manifest, repo, commit, fixture)
            output = root / "case"
            output.mkdir()
            case = output / "case_spec.json"
            write_case(case)
            env = os.environ.copy()
            env["PATH"] = f"{root}{os.pathsep}{env.get('PATH', '')}"
            env["PYTHONPATH"] = str(ROOT / "src")
            env["FIXTURE_AGENT_MODE"] = "no-request"
            result = subprocess.run([sys.executable, str(SCRIPT), "--runtime-manifest", str(manifest), "--case-spec", str(case), "--output-dir", str(output), "--execute"], capture_output=True, text=True, check=False, env=env)
            self.assertEqual(result.returncode, 1)
            self.assertIn("request proxy events are missing", result.stderr)
            self.assertFalse((output / "case_result.json").exists())

    def test_dirty_checkout_is_rejected_before_hardware_or_runner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, commit = make_repo(root)
            fixture = write_fixture(root)
            manifest = root / "manifest.json"
            write_manifest(manifest, repo, commit, fixture)
            (repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")
            output = root / "case"
            output.mkdir()
            case = output / "case_spec.json"
            write_case(case)
            result = invoke(manifest, case, output, "--execute")
            self.assertEqual(result.returncode, 1)
            self.assertIn("dirty", result.stderr)
            self.assertFalse((output / "runner_state.json").exists())

    def test_missing_official_result_never_becomes_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, commit = make_repo(root)
            fixture = write_fixture(root, evaluator_mode="missing")
            manifest = root / "manifest.json"
            write_manifest(manifest, repo, commit, fixture)
            output = root / "case"
            output.mkdir()
            case = output / "case_spec.json"
            write_case(case)
            env = os.environ.copy()
            env["PATH"] = f"{root}{os.pathsep}{env.get('PATH', '')}"
            env["PYTHONPATH"] = str(ROOT / "src")
            result = subprocess.run([sys.executable, str(SCRIPT), "--runtime-manifest", str(manifest), "--case-spec", str(case), "--output-dir", str(output), "--execute"], capture_output=True, text=True, check=False, env=env)
            self.assertEqual(result.returncode, 2, result.stderr)
            record = json.loads((output / "case_result.json").read_text())
            self.assertEqual(record["status"], "failed")
            self.assertEqual(record["reason"], "missing_official_evaluator_result")

    def test_evaluator_result_must_bind_the_exact_dataset_predictions_report_and_run_id(self):
        modes = {
            "dataset-mismatch": "dataset hash",
            "dataset-path-mismatch": "dataset_path",
            "predictions-mismatch": "prediction hash",
            "report-hash-mismatch": "report hash",
            "external-report": "report_path must stay inside output-dir",
            "run-id-mismatch": "run_id",
        }
        for mode, expected in modes.items():
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                repo, commit = make_repo(root)
                fixture = write_fixture(root, evaluator_mode=mode)
                manifest = root / "manifest.json"
                write_manifest(manifest, repo, commit, fixture)
                output = root / "case"
                output.mkdir()
                case = output / "case_spec.json"
                write_case(case)
                env = os.environ.copy()
                env["PATH"] = f"{root}{os.pathsep}{env.get('PATH', '')}"
                env["PYTHONPATH"] = str(ROOT / "src")
                result = subprocess.run([sys.executable, str(SCRIPT), "--runtime-manifest", str(manifest), "--case-spec", str(case), "--output-dir", str(output), "--execute"], capture_output=True, text=True, check=False, env=env)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(expected, result.stderr)
                self.assertFalse((output / "case_result.json").exists())

    def test_evaluator_result_requires_complete_provenance_and_consistent_counts(self):
        for mode, expected in (("missing-provenance", "omits required provenance"), ("counts-mismatch", "exactly one resolved")):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                repo, commit = make_repo(root)
                fixture = write_fixture(root, evaluator_mode=mode)
                manifest = root / "manifest.json"
                write_manifest(manifest, repo, commit, fixture)
                output = root / "case"
                output.mkdir()
                case = output / "case_spec.json"
                write_case(case)
                env = os.environ.copy()
                env["PATH"] = f"{root}{os.pathsep}{env.get('PATH', '')}"
                env["PYTHONPATH"] = str(ROOT / "src")
                result = subprocess.run([sys.executable, str(SCRIPT), "--runtime-manifest", str(manifest), "--case-spec", str(case), "--output-dir", str(output), "--execute"], capture_output=True, text=True, check=False, env=env)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(expected, result.stderr)

    def test_existing_result_is_output_collision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, commit = make_repo(root)
            fixture = write_fixture(root)
            manifest = root / "manifest.json"
            write_manifest(manifest, repo, commit, fixture)
            output = root / "case"
            output.mkdir()
            case = output / "case_spec.json"
            write_case(case)
            (output / "case_result.json").write_text("preserve\n", encoding="utf-8")
            result = invoke(manifest, case, output)
            self.assertEqual(result.returncode, 1)
            self.assertIn("output collision", result.stderr)
            self.assertEqual((output / "case_result.json").read_text(), "preserve\n")

    def test_dataset_hash_mismatch_is_rejected_before_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, commit = make_repo(root)
            fixture = write_fixture(root)
            manifest = root / "manifest.json"
            write_manifest(manifest, repo, commit, fixture)
            (repo / "lite.jsonl").write_text(json.dumps({"instance_id": "owner__repo-2"}) + "\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "lite.jsonl"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "mutate dataset"], check=True)
            value = json.loads(manifest.read_text())
            value["required_commit"] = git(repo, "rev-parse", "HEAD")
            value["pins"]["swe_agent_revision"] = value["required_commit"]
            value["pins"]["swe_bench_revision"] = value["required_commit"]
            manifest.write_text(json.dumps(value, indent=2) + "\n")
            digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
            Path(str(manifest) + ".sha256").write_text(f"{digest}  {manifest.name}\n", encoding="utf-8")
            output = root / "case"
            output.mkdir()
            case = output / "case_spec.json"
            write_case(case)
            result = invoke(manifest, case, output)
            self.assertEqual(result.returncode, 1)
            self.assertIn("SHA-256 mismatch", result.stderr)


if __name__ == "__main__":
    unittest.main()
