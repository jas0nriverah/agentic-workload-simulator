import hashlib
import fcntl
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

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
if os.environ.get('FIXTURE_AGENT_MODE', 'request') != 'no-request-no-prediction':
    (out / 'preds.json').write_text(json.dumps([{'instance_id': 'owner__repo-1', 'model_name_or_path': 'fixture/model', 'model_patch': 'diff --git a/a b/a'}]) + '\\n')
(out / 'agent_success.marker').write_text('ran\\n')
""")
    evaluator = root / "fake_evaluator.py"
    write_executable(evaluator, f"""
import hashlib, json, os, pathlib, sys
args = sys.argv[1:]
def value(name):
    return args[args.index(name) + 1]
mode = os.environ.get('FIXTURE_EVALUATOR_MODE', {evaluator_mode!r})
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
if mode == 'empty-patch':
    record['official_resolved'] = False
    record['counts']['resolved_instances'] = 0
    record['counts']['unresolved_instances'] = 1
    record['empty_patch_unresolved'] = True
elif mode == 'dataset-mismatch':
    record['dataset_sha256'] = '0' * 64
elif mode == 'predictions-mismatch':
    record['predictions_sha256'] = '1' * 64
elif mode == 'report-hash-mismatch':
    record['report_sha256'] = '2' * 64
elif mode == 'dataset-path-mismatch':
    record['dataset_path'] = str(result.parent / 'other-dataset.jsonl')
elif mode == 'external-report':
    external = result.parent.parent.parent.parent / 'external-report.json'
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
    def test_native_deferred_requires_hash_bound_server_archive_descriptor(self):
        base = {
            "mode": "v2",
            "schema_version": ADAPTER.TELEMETRY_V2_SCHEMA,
            "instrumentation_version": ADAPTER.TELEMETRY_V2_VERSION,
            "require_activation": True,
            "require_raw_request_payloads": True,
            "require_cpu_work": True,
            "cpu_work": {
                "backend": "bcc",
                "trace_format": "raw individual",
                "attach_existing_process": True,
                "require_persistent_runtime_pid": True,
            },
            "remote_hardware_profile": {"path": "/tmp/profile.json", "sha256": "1" * 64},
            "serving_metrics": {
                "schema_version": ADAPTER.SERVING_METRICS_CONFIG_SCHEMA,
                "enabled": True,
                "metrics_url": "http://127.0.0.1:1/metrics",
                "server_identity": "worker-22",
                "counter_epoch": "epoch-1",
                "timeout_seconds": 1.0,
                "access_witness_path": "/tmp/witness.jsonl",
                "access_witness_evidence_kind": "external_access_lease",
                "vllm_version": "0.10.0",
                "mode": "native_deferred",
            },
        }
        with self.assertRaises(ADAPTER.CaseRunnerError):
            ADAPTER._validate_telemetry_config(base)
        base["native_server_archive"] = {
            "fetch": {
                "ssh_host": "jriverah3@128.61.254.151",
                "ssh_control": "/tmp/jriverah3-pace-login3.sock",
                "journal": "/storage/capture/serving-observer.jsonl",
                "native_journal": "/storage/capture/native-vllm.jsonl",
            }
        }
        normalized = ADAPTER._validate_telemetry_config(base)
        self.assertEqual(normalized["native_server_archive"]["fetch"]["ssh_host"], "jriverah3@128.61.254.151")
        # A legacy metrics-only configuration is still a valid optional subset.
        base.pop("native_server_archive")
        base["serving_metrics"].pop("mode")
        normalized = ADAPTER._validate_telemetry_config(base)
        self.assertIsNone(normalized["native_server_archive"])

    def test_response_usage_reconstructs_explicit_cache_tokens_from_sse(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "response.bin"
            path.write_bytes(
                b'data: {"choices":[],"usage":{"prompt_tokens":9,"completion_tokens":2,'
                b'"prompt_tokens_details":{"cached_tokens":6}}}\n\n'
                b'data: [DONE]\n\n'
            )
            self.assertEqual(
                ADAPTER._response_usage(path),
                {"prompt_tokens": 9, "completion_tokens": 2, "cached_tokens": 6},
            )

    def test_reviewed_runner_is_directly_executable_by_matrix(self):
        self.assertTrue(os.access(SCRIPT, os.X_OK))

    def test_materializes_case_local_request_config_for_token_sweep(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "request.json"
            source.write_text(
                json.dumps(
                    {
                        "agent": {
                            "model": {
                                "completion_kwargs": {"max_tokens": 2048, "seed": 0}
                            }
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            output = root / "attempt"
            path, digest = ADAPTER._materialize_request_config(
                source=source, max_output_tokens=512, output_dir=output, top_p=0.75
            )
            materialized_model = json.loads(path.read_text(encoding="utf-8"))["agent"]["model"]
            self.assertEqual(materialized_model["top_p"], 0.75)
            self.assertNotIn("top_p", materialized_model["completion_kwargs"])
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["agent"]["model"]["completion_kwargs"]["max_tokens"],
                512,
            )
            self.assertEqual(
                json.loads(source.read_text(encoding="utf-8"))["agent"]["model"]["completion_kwargs"]["max_tokens"],
                2048,
            )
            self.assertEqual(digest, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_owned_docker_command_requires_verified_defaults_and_adds_exact_owner_label(self):
        """CPU-Docker launch is label-scoped and rejects custom cleanup knobs."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            parser = project / ".venv/bin/python"
            parser.parent.mkdir(parents=True)
            output = root / "attempt"
            output.mkdir()
            proof = {"docker": True, "empty_args": True, "remove_images": False}
            marker_env = ADAPTER.DOCKER_PROOF_MARKER_ENV
            parser_source = lambda value: (
                "import json, os; print('pinned BasicCLI INFO banner 👋'); "
                "print(os.environ[" + repr(marker_env) + "] + json.dumps(" + repr(value) + ", sort_keys=True))\n"
            )
            write_executable(parser, parser_source(proof))
            command = ["sweagent", "run-batch", "--config", "config.yaml"]
            owner = "a" * 32
            deadline = str(time.monotonic_ns() + 5_000_000_000)
            with patch.dict(os.environ, {ADAPTER.CASE_OWNER_ENV: owner, "ASSIGNMENT_CASE_DEADLINE_MONOTONIC_NS": deadline}):
                owned = ADAPTER._owned_docker_command(command, project, owner, output)
            self.assertEqual(
                owned[-4:],
                [
                    "--instances.deployment.docker_args",
                    json.dumps(["--label", f"{ADAPTER.CASE_OWNER_LABEL}={owner}"]),
                    "--instances.deployment.remove_container",
                    "false",
                ],
            )
            ownership = json.loads((output / "docker_ownership.json").read_text(encoding="utf-8"))
            self.assertEqual(ownership["owner"], owner)
            self.assertEqual(ownership["label"], ADAPTER.CASE_OWNER_LABEL)
            self.assertFalse(ownership["deployment_verified"]["remove_images"])
            capture = ownership["parser_capture"]
            self.assertEqual(capture["protocol"], ADAPTER.DOCKER_PROOF_PROTOCOL)
            captured_stdout = (output / capture["stdout"]["path"]).read_text(encoding="utf-8")
            self.assertTrue(captured_stdout.startswith("pinned BasicCLI INFO banner 👋\n"))
            self.assertEqual(capture["stdout"]["size_bytes"], len(captured_stdout.encode("utf-8")))
            self.assertEqual(capture["stdout"]["sha256"], hashlib.sha256(captured_stdout.encode("utf-8")).hexdigest())
            self.assertEqual(capture["stderr"]["size_bytes"], 0)

            for unsafe in (
                {"docker": True, "empty_args": False, "remove_images": False},
                {"docker": True, "empty_args": True, "remove_images": True},
                {"docker": False, "empty_args": True, "remove_images": False},
            ):
                write_executable(parser, parser_source(unsafe))
                with patch.dict(os.environ, {"ASSIGNMENT_CASE_DEADLINE_MONOTONIC_NS": deadline}):
                    with self.assertRaisesRegex(ADAPTER.CaseRunnerError, "owned Docker launch"):
                        ADAPTER._owned_docker_command(command, project, owner, output)

    def test_owned_docker_parser_requires_one_proof_and_retains_raw_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            parser = project / ".venv/bin/python"
            parser.parent.mkdir(parents=True)
            output = root / "attempt"
            output.mkdir()
            command = ["sweagent", "run-batch", "--config", "config.yaml"]
            owner = "b" * 32
            deadline = str(time.monotonic_ns() + 5_000_000_000)
            marker_env = ADAPTER.DOCKER_PROOF_MARKER_ENV

            write_executable(
                parser,
                "import os, sys; print('unstructured stdout'); print('parser warning', file=sys.stderr)\n",
            )
            with patch.dict(os.environ, {"ASSIGNMENT_CASE_DEADLINE_MONOTONIC_NS": deadline}):
                with self.assertRaisesRegex(ADAPTER.CaseRunnerError, "exactly one machine-readable proof"):
                    ADAPTER._owned_docker_command(command, project, owner, output)
            stdout_path = output / "docker_ownership_parser.stdout.log"
            stderr_path = output / "docker_ownership_parser.stderr.log"
            self.assertEqual(stdout_path.read_bytes(), b"unstructured stdout\n")
            self.assertEqual(stderr_path.read_bytes(), b"parser warning\n")
            capture = json.loads(
                (output / "docker_ownership_parser_capture.json").read_text(encoding="utf-8")
            )
            self.assertEqual(capture["returncode"], 0)
            self.assertEqual(capture["stdout"]["size_bytes"], len(stdout_path.read_bytes()))
            self.assertEqual(capture["stderr"]["size_bytes"], len(stderr_path.read_bytes()))
            self.assertEqual(capture["stdout"]["sha256"], hashlib.sha256(stdout_path.read_bytes()).hexdigest())
            self.assertEqual(capture["stderr"]["sha256"], hashlib.sha256(stderr_path.read_bytes()).hexdigest())

            write_executable(
                parser,
                "import json, os; marker = os.environ[" + repr(marker_env) + "]; "
                "print(marker + '{}'); print(marker + '{}')\n",
            )
            with patch.dict(os.environ, {"ASSIGNMENT_CASE_DEADLINE_MONOTONIC_NS": deadline}):
                with self.assertRaisesRegex(ADAPTER.CaseRunnerError, "exactly one machine-readable proof"):
                    ADAPTER._owned_docker_command(command, project, owner, output)
            self.assertEqual(len([line for line in stdout_path.read_text(encoding="utf-8").splitlines() if line.startswith("ASSIGNMENT_DOCKER_PROOF_V1:")]), 2)

    def test_failure_result_inventories_root_and_attempt_artifacts_without_following_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "case"
            output.mkdir()
            case = {"resume_key": "fixture-resume"}
            evaluator = output / "evaluator_result.json"
            evaluator.write_text('{"submitted":false}\n', encoding="utf-8")
            os.utime(evaluator, ns=(1_234_000_000, 1_234_000_000))
            attempt = output / "runner_attempts/attempt-001"
            attempt.mkdir(parents=True)
            trajectory = attempt / "trajectory.json"
            trajectory.write_text('{"steps":[]}\n', encoding="utf-8")
            target = root / "outside-retained.txt"
            target.write_text("do not traverse\n", encoding="utf-8")
            link = output / "raw-output-link"
            link.symlink_to(target)

            with self.assertRaisesRegex(RuntimeError, "synthetic evaluator failure"):
                with ADAPTER._locked_case(output, case, execute=True):
                    raise RuntimeError("synthetic evaluator failure")

            result_path = output / "case_result.json"
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(result["schema_version"], ADAPTER.FAILURE_RESULT_SCHEMA)
            self.assertEqual(result["status"], "failed")
            self.assertFalse(result["accepted"])
            self.assertEqual(result["failure"]["type"], "RuntimeError")
            self.assertEqual(result["failure"]["message"], "synthetic evaluator failure")
            self.assertIsInstance(result["failure"]["recorded_epoch_ns"], int)
            self.assertEqual(result["inventory_errors"], [])
            inventory = {item["path"]: item for item in result["artifacts"]}
            self.assertIn("evaluator_result.json", inventory)
            self.assertIn("runner_attempts/attempt-001/trajectory.json", inventory)
            self.assertIn("raw-output-link", inventory)
            self.assertEqual(inventory["evaluator_result.json"]["size"], evaluator.stat().st_size)
            self.assertEqual(inventory["evaluator_result.json"]["mtime_ns"], evaluator.stat().st_mtime_ns)
            self.assertEqual(inventory["raw-output-link"]["kind"], "symlink")
            self.assertEqual(inventory["raw-output-link"]["target"], str(target))
            self.assertNotIn("case_result.json", inventory)
            self.assertNotIn("case_result.json.sha256", inventory)
            for item in result["artifacts"]:
                if item["kind"] != "symlink":
                    self.assertIn("sha256", item)
                    self.assertIn("size", item)
                    self.assertIn("mtime_ns", item)
            sidecar = Path(str(result_path) + ".sha256")
            self.assertEqual(sidecar.read_text(encoding="utf-8"), f"{hashlib.sha256(result_path.read_bytes()).hexdigest()}  {result_path.name}\n")
            history_dirs = [path for path in (output / "metadata_history").iterdir() if path.is_dir()]
            self.assertEqual(len(history_dirs), 1)
            self.assertTrue((history_dirs[0] / "evaluator_result.json").is_file())

    def test_case_lock_contention_preserves_existing_result_and_writes_no_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "case"
            output.mkdir()
            result_path = output / "case_result.json"
            result_path.write_text("preserve exactly\n", encoding="utf-8")
            lock_path = output / ".case-runner.lock"
            held = lock_path.open("a+b")
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                with self.assertRaisesRegex(ADAPTER.CaseRunnerError, "output lock"):
                    with ADAPTER._locked_case(output, {"resume_key": "fixture"}, execute=True):
                        pass
            finally:
                fcntl.flock(held.fileno(), fcntl.LOCK_UN)
                held.close()
            self.assertEqual(result_path.read_text(encoding="utf-8"), "preserve exactly\n")
            self.assertFalse((output / "case_result.json.sha256").exists())

    def test_retry_retains_attempt_evaluator_outputs_with_unique_run_ids_and_history(self):
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
            env["FIXTURE_EVALUATOR_MODE"] = "dataset-mismatch"
            first = subprocess.run(
                [sys.executable, str(SCRIPT), "--runtime-manifest", str(manifest), "--case-spec", str(case), "--output-dir", str(output), "--execute"],
                capture_output=True, text=True, check=False, env=env,
            )
            self.assertEqual(first.returncode, 1, first.stderr)
            failed = json.loads((output / "case_result.json").read_text(encoding="utf-8"))
            self.assertEqual(failed["schema_version"], ADAPTER.FAILURE_RESULT_SCHEMA)
            first_eval = output / "runner_attempts/attempt-001/evaluator_result.json"
            first_value = json.loads(first_eval.read_text(encoding="utf-8"))
            (output / "case_result.json").unlink()
            (output / "case_result.json.sha256").unlink()

            env["FIXTURE_EVALUATOR_MODE"] = "valid"
            second = subprocess.run(
                [sys.executable, str(SCRIPT), "--runtime-manifest", str(manifest), "--case-spec", str(case), "--output-dir", str(output), "--execute"],
                capture_output=True, text=True, check=False, env=env,
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            completed = json.loads((output / "case_result.json").read_text(encoding="utf-8"))
            self.assertEqual(completed["schema_version"], ADAPTER.RESULT_SCHEMA)
            second_eval = output / "runner_attempts/attempt-002/evaluator_result.json"
            second_value = json.loads(second_eval.read_text(encoding="utf-8"))
            self.assertNotEqual(first_value["run_id"], second_value["run_id"])
            self.assertTrue(first_value["run_id"].startswith("assignment-"))
            self.assertIn("-attempt-001-", first_value["run_id"])
            self.assertIn("-attempt-002-", second_value["run_id"])
            self.assertEqual(completed["evaluator"]["result_path"], "runner_attempts/attempt-002/evaluator_result.json")
            history_dirs = [path for path in (output / "metadata_history").iterdir() if path.is_dir()]
            self.assertGreaterEqual(len(history_dirs), 2)
            self.assertEqual(len({path.name for path in history_dirs}), len(history_dirs))
            self.assertTrue(any((path / "runner_state.json").is_file() for path in history_dirs))

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

    def test_empty_proxy_events_are_completed_without_model_requests(self):
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
            self.assertEqual(result.returncode, 0, result.stderr)
            completed = json.loads((output / "case_result.json").read_text())
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["proxy"]["event_count"], 0)

    def test_zero_proxy_events_retry_missing_prediction_as_empty_patch(self):
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
            env["FIXTURE_AGENT_MODE"] = "no-request-no-prediction"
            env["FIXTURE_EVALUATOR_MODE"] = "empty-patch"
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--runtime-manifest", str(manifest), "--case-spec", str(case), "--output-dir", str(output), "--execute"],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            completed = json.loads((output / "case_result.json").read_text())
            self.assertEqual(completed["status"], "completed")
            self.assertFalse(completed["evaluator"]["official_resolved"])
            self.assertTrue(completed["evaluator"]["submitted"])
            predictions = json.loads(
                (output / "runner_attempts/attempt-001/preds.json").read_text()
            )
            self.assertEqual(predictions, [{
                "instance_id": "owner__repo-1",
                "model_name_or_path": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
                "model_patch": "",
            }])
            retry = json.loads(
                (output / "runner_attempts/attempt-001/evaluator_retry.json").read_text()
            )
            self.assertEqual(retry["status"], "completed")

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
                failure = json.loads((output / "case_result.json").read_text())
                self.assertEqual(failure["schema_version"], ADAPTER.FAILURE_RESULT_SCHEMA)
                self.assertEqual(failure["status"], "failed")
                self.assertFalse(failure["accepted"])

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


def proxy_event(index: int, **overrides) -> dict:
    """Build one measured proxy event with the live journal's field shape."""

    start = 178_000_000_000_000 + index * 10_000_000_000
    end = start + 1_500_000
    row = {
        "schema_version": "observability.request-proxy.v1",
        "event_type": "model_request_boundary",
        "request_id": f"request-{index:032d}",
        "provenance": "measured",
        "request_mutation": False,
        "status_code": 200,
        "error": None,
        "failure_phase": None,
        "start_mono_ns": start,
        "end_mono_ns": end,
        "duration_ms": (end - start) / 1_000_000.0,
        "request_sha256": "a" * 64,
        "response_sha256": "b" * 64,
    }
    row.update(overrides)
    return row


class ProxyEventValidationTests(unittest.TestCase):
    """``_validate_proxy_events`` accepts measured failures, not bad evidence."""

    def journal(self, root: Path, rows) -> Path:
        path = root / "request_proxy.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return path

    def test_mid_stream_remote_disconnect_then_recovery_is_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            disconnect = proxy_event(
                3,
                status_code=None,
                error="RemoteDisconnected",
                failure_phase="response_headers",
                duration_ms=10008.666153,
                end_mono_ns=proxy_event(3)["start_mono_ns"] + 10_008_666_153,
                response_bytes=0,
            )
            rows = [proxy_event(1), proxy_event(2), disconnect, proxy_event(4), proxy_event(5)]
            summary = ADAPTER._validate_proxy_events(self.journal(root, rows))
            self.assertEqual(summary["event_count"], 5)

    def test_remote_disconnect_without_a_response_digest_is_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            disconnect = proxy_event(2, status_code=None, error="RemoteDisconnected", failure_phase="response_headers")
            disconnect.pop("response_sha256")
            summary = ADAPTER._validate_proxy_events(self.journal(root, [proxy_event(1), disconnect, proxy_event(3)]))
            self.assertEqual(summary["event_count"], 3)

    def test_journal_ending_in_an_http_400_reject_is_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rows = [proxy_event(1), proxy_event(2), proxy_event(3, status_code=400)]
            summary = ADAPTER._validate_proxy_events(self.journal(root, rows))
            self.assertEqual(summary["event_count"], 3)

    def test_all_transport_failures_are_infrastructure_not_unresolved_solver(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rows = [
                proxy_event(1, status_code=None, error="ConnectionRefusedError", failure_phase="connect"),
                proxy_event(2, status_code=None, error="RemoteDisconnected", failure_phase="response_headers"),
            ]
            path = self.journal(root, rows)
            classified = ADAPTER._classify_model_transport_failure(path)
            self.assertIsNotNone(classified)
            self.assertEqual(classified["status"], "infrastructure_failure")
            self.assertTrue(classified["halt_matrix"])

    def test_mixed_transport_and_context_rejection_remains_measured_outcome(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rows = [
                proxy_event(1, status_code=None, error="RemoteDisconnected", failure_phase="connect"),
                proxy_event(2, status_code=400),
            ]
            self.assertIsNone(ADAPTER._classify_model_transport_failure(self.journal(root, rows)))

    def test_all_server_5xx_attempts_are_infrastructure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = self.journal(root, [proxy_event(1, status_code=503), proxy_event(2, status_code=500)])
            classified = ADAPTER._classify_model_transport_failure(path)
            self.assertEqual(classified["classification"], "model_transport_or_server_infrastructure")

    def test_missing_journal_still_fails_closed_and_empty_is_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(ADAPTER.CaseRunnerError):
                ADAPTER._validate_proxy_events(root / "absent.jsonl")
            empty = root / "request_proxy.jsonl"
            empty.write_text("", encoding="utf-8")
            summary = ADAPTER._validate_proxy_events(empty)
            self.assertEqual(summary["event_count"], 0)
            self.assertEqual(summary["sha256"], hashlib.sha256(b"").hexdigest())

    def test_empty_journal_with_adaptive_runtime_still_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            empty = Path(temporary) / "request_proxy.jsonl"
            empty.write_text("", encoding="utf-8")
            with self.assertRaises(ADAPTER.CaseRunnerError):
                ADAPTER._validate_proxy_events(empty, adaptive_required=True)

    def test_blank_but_nonempty_journal_still_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            for label, payload in {"newline": "\n", "whitespace": "   \n"}.items():
                blank = Path(temporary) / f"{label}.jsonl"
                blank.write_text(payload, encoding="utf-8")
                with self.subTest(label):
                    with self.assertRaises(ADAPTER.CaseRunnerError):
                        ADAPTER._validate_proxy_events(blank)

    def test_unmeasured_or_tampered_events_still_fail_closed(self):
        cases = {
            "unknown outcome": proxy_event(2, status_code=None, error=None),
            "blank error": proxy_event(2, status_code=None, error="   "),
            "redirect status": proxy_event(2, status_code=302, error=None),
            "unmeasured provenance": proxy_event(2, status_code=400, provenance="synthetic"),
            "mutated request": proxy_event(2, status_code=400, request_mutation=True),
            "bad duration": proxy_event(2, status_code=400, duration_ms=1.0),
            "bad bounds": proxy_event(2, status_code=400, end_mono_ns=proxy_event(2)["start_mono_ns"]),
        }
        no_request_digest = proxy_event(2, status_code=None, error="RemoteDisconnected")
        no_request_digest.pop("request_sha256")
        cases["missing request digest"] = no_request_digest
        duplicate = proxy_event(2, status_code=400)
        duplicate["request_id"] = proxy_event(1)["request_id"]
        cases["duplicate request_id"] = duplicate
        for label, row in cases.items():
            with self.subTest(label):
                with tempfile.TemporaryDirectory() as temporary:
                    path = self.journal(Path(temporary), [proxy_event(1), row])
                    with self.assertRaises(ADAPTER.CaseRunnerError):
                        ADAPTER._validate_proxy_events(path)


if __name__ == "__main__":
    unittest.main()
