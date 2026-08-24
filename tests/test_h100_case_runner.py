import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from scripts.cloud.h100_case_runner import (
    MODEL_REVISION,
    VLLM_IMAGE,
    PromptBuilder,
    RunnerError,
    _reviewed_profiled_server_allows_gpu_processes,
    _trace_provider_command,
    validate_row_artifacts,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER = REPO_ROOT / "scripts" / "cloud" / "h100_case_runner.py"
CONFIG = REPO_ROOT / "configs" / "h100_final_validation.json"
TRACE_PROVIDER = REPO_ROOT / "tests" / "fixtures" / "h100_fake_trace_provider.py"
MODEL = "Qwen/Qwen3-Coder-30B-A3B-Instruct"


class FakeState:
    def __init__(self, fail_on_completion=None):
        self.fail_on_completion = fail_on_completion
        self.completion_calls = 0
        self.active = 0
        self.max_active = 0
        self.requests = []
        self.errors = []
        self.lock = threading.Lock()


class FakeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        return

    def _send(self, status, body, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/health":
            self._send(200, b"OK")
        elif self.path == "/v1/models":
            self._send(200, json.dumps({"data": [{"id": MODEL}]}).encode())
        elif self.path == "/metrics":
            self._send(200, b"# HELP fake_requests_total 1\nfake_requests_total 1\n", "text/plain")
        else:
            self._send(404, b"{}")

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/v1/completions":
            self._send(404, b"{}")
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        state = self.server.test_state
        with state.lock:
            state.completion_calls += 1
            call_number = state.completion_calls
            state.active += 1
            state.max_active = max(state.max_active, state.active)
        try:
            state.requests.append(payload)
            expected = {
                "model": MODEL,
                "max_tokens": 32,
                "temperature": 0.0,
                "top_p": 1.0,
                "seed": 0,
                "stream": False,
            }
            for key, value in expected.items():
                if payload.get(key) != value:
                    state.errors.append(f"fixed request field mismatch: {key}")
            prompt = payload.get("prompt")
            if not isinstance(prompt, list) or len(prompt) != 128 or not all(
                isinstance(token, int) for token in prompt
            ):
                state.errors.append("prompt was not the expected token-ID list")
            time.sleep(0.01)
            if state.fail_on_completion == call_number:
                self._send(500, b"{\"error\":\"fixture failure\"}")
            else:
                body = {
                    "id": f"fixture-{call_number}",
                    "object": "text_completion",
                    "model": MODEL,
                    "choices": [{"text": "fixture", "index": 0, "finish_reason": "length"}],
                    "usage": {
                        "prompt_tokens": len(prompt),
                        "completion_tokens": 32,
                        "total_tokens": len(prompt) + 32,
                    },
                }
                self._send(200, json.dumps(body).encode())
        finally:
            with state.lock:
                state.active -= 1


def _start_server(state):
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeHandler)
    server.test_state = state
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _hardware_fixture():
    return json.dumps(
        {
            "gpu_name": "NVIDIA H100 80GB HBM3",
            "gpu_uuid": "GPU-fixture",
            "pci_bus_id": "0000:04:00.0",
            "memory_total_mib": 81559,
            "compute_capability": "9.0",
            "driver": "fixture-driver",
            "cuda": "fixture-cuda",
            "power_limit_w": "700.00",
            "power_draw_w": "72.00",
            "application_clocks": {
                "graphics_mhz": "345",
                "sm_mhz": "345",
                "memory_mhz": "2619",
            },
            "host": "fixture-host",
            "kernel": "fixture-kernel",
            "boot_id": "fixture-boot",
        }
    )


def _environment(server):
    env = os.environ.copy()
    base_url = f"http://127.0.0.1:{server.server_port}"
    env.update(
        {
            "H100_RUNNER_TEST_MODE": "1",
            "H100_TEST_SERVER_URL": base_url,
            "H100_VLLM_BASE_URL": base_url,
            "H100_VLLM_MODEL": MODEL,
            "H100_TEST_HARDWARE_JSON": _hardware_fixture(),
            "H100_TRACE_PROVIDER": str(TRACE_PROVIDER),
            "H100_TEST_REQUEST_SPACING_SECONDS": "0",
        }
    )
    return env


def _run(output_dir, repeat_id, env, extra=None):
    command = [
        str(RUNNER),
        "--config",
        str(CONFIG),
        "--case-id",
        "cal_i128_o32",
        "--split",
        "calibration",
        "--input-tokens",
        "128",
        "--output-tokens",
        "32",
        "--repeat-id",
        repeat_id,
        "--output-dir",
        str(output_dir),
    ]
    if extra:
        command.extend(extra)
    return subprocess.run(command, cwd=REPO_ROOT, env=env, capture_output=True, text=True)


class H100CaseRunnerTests(unittest.TestCase):
    def test_production_trace_provider_is_explicitly_armed_but_fixture_stays_test_only(self):
        case = {"case_id": "cal_i128_o32"}
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(
                config=CONFIG,
                split="calibration",
                input_tokens=128,
                output_tokens=32,
                repeat_id="r01",
            )
            with patch.dict(
                os.environ,
                {"H100_TRACE_PROVIDER": str(TRACE_PROVIDER), "H100_RUNNER_TEST_MODE": ""},
            ):
                production_command = _trace_provider_command(
                    {}, case, args, Path(directory), "measured", "arm", 0, 0
                )
            self.assertEqual(production_command[production_command.index("--action") + 1], "arm")
            with patch.dict(
                os.environ,
                {"H100_TRACE_PROVIDER": str(TRACE_PROVIDER), "H100_RUNNER_TEST_MODE": "1"},
            ):
                test_command = _trace_provider_command(
                    {}, case, args, Path(directory), "measured", "collect", 1, 2
                )
            self.assertNotIn("--action", test_command)

    def test_pinned_tokenizer_fallback_uses_tokenizer_json_without_transformers(self):
        fake_tokenizers = types.ModuleType("tokenizers")

        class FakeEncoding:
            ids = [11, 12]

        class FakeTokenizer:
            @classmethod
            def from_file(cls, _path):
                return cls()

            def encode(self, _text, add_special_tokens=False):
                self.add_special_tokens = add_special_tokens
                return FakeEncoding()

        fake_tokenizers.Tokenizer = FakeTokenizer
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / MODEL_REVISION
            snapshot.mkdir()
            for filename in ("config.json", "tokenizer_config.json", "tokenizer.json"):
                (snapshot / filename).write_text("{}", encoding="utf-8")
            with patch.dict(
                os.environ,
                {"H100_MODEL_SNAPSHOT": str(snapshot), "H100_RUNNER_TEST_MODE": ""},
            ), patch.dict(sys.modules, {"transformers": None, "tokenizers": fake_tokenizers}):
                builder = PromptBuilder(4)
            self.assertEqual(builder.token_ids(4), [11, 12, 11, 12])

    def test_profiled_server_gpu_allowance_is_container_scoped_and_fail_closed(self):
        processes = "123, /usr/bin/python3, 100 MiB\n"
        command_json = json.dumps(
            [
                "launch",
                "--session-new=h100-final-validation",
                "--trace=cuda,osrt",
                "--cuda-event-trace=false",
                "--",
                "python3",
                "-m",
                "vllm.entrypoints.openai.api_server",
                "--revision",
                MODEL_REVISION,
            ]
        )

        def fake_run(command, **_kwargs):
            if command[:3] == ["docker", "inspect", "--format"]:
                if command[3] == "{{.State.Running}}":
                    stdout = "true\n"
                elif command[3] == "{{.Config.Image}}":
                    stdout = VLLM_IMAGE + "\n"
                else:
                    stdout = command_json
            elif command[:2] == ["docker", "top"]:
                stdout = "PID\n123\n"
            else:
                stdout = "ID TIME STATE LAUNCH NAME\n1020 00:01 Launched h100-final-validation\n"
            return types.SimpleNamespace(stdout=stdout, stderr="", returncode=0)

        with patch.dict(
            os.environ,
            {
                "H100_EXPECTED_SERVER_CONTAINER": "h100-final-vllm",
                "H100_NSYS_CONTAINER": "h100-final-vllm",
                "H100_NSYS_SESSION": "h100-final-validation",
            },
        ), patch("scripts.cloud.h100_case_runner.subprocess.run", side_effect=fake_run):
            self.assertTrue(_reviewed_profiled_server_allows_gpu_processes(processes))

        def outside_container_run(command, **_kwargs):
            result = fake_run(command, **_kwargs)
            if command[:2] == ["docker", "top"]:
                result.stdout = "PID\n999\n"
            return result

        with patch.dict(
            os.environ,
            {
                "H100_EXPECTED_SERVER_CONTAINER": "h100-final-vllm",
                "H100_NSYS_CONTAINER": "h100-final-vllm",
                "H100_NSYS_SESSION": "h100-final-validation",
            },
        ), patch("scripts.cloud.h100_case_runner.subprocess.run", side_effect=outside_container_run):
            self.assertFalse(_reviewed_profiled_server_allows_gpu_processes(processes))

    def test_validate_only_exact_cli_does_not_contact_server_or_write_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "case" / "r01"
            result = _run(output_dir, "r01", os.environ.copy(), ["--validate-only"])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("no server, GPU, trace provider, or artifact access", result.stdout)
            self.assertFalse(output_dir.exists())

    def test_successful_rows_use_fixed_request_and_serialize_requests(self):
        state = FakeState()
        server, thread = _start_server(state)
        with tempfile.TemporaryDirectory() as directory:
            env = _environment(server)
            case_root = Path(directory) / "cal_i128_o32"
            try:
                for repeat_id in ("r01", "r02", "r03"):
                    result = _run(case_root / repeat_id, repeat_id, env)
                    self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(state.completion_calls, 5)  # two warmups + three measured
                self.assertEqual(state.max_active, 1)
                self.assertEqual(state.errors, [])
                warmups = json.loads((case_root / "warmups.json").read_text(encoding="utf-8"))
                self.assertEqual(warmups["warmup_count"], 2)
                for repeat_id in ("r01", "r02", "r03"):
                    output_dir = case_root / repeat_id
                    row_path = output_dir / "row.json"
                    row = json.loads(row_path.read_text(encoding="utf-8"))
                    self.assertEqual(row["status"], "completed")
                    self.assertEqual(row["actual_prompt_tokens"], 128)
                    self.assertEqual(row["actual_completion_tokens"], 32)
                    self.assertGreater(row["wall_ms"], 0)
                    self.assertEqual(row["clock"]["clock_id"], "CLOCK_MONOTONIC_RAW")
                    self.assertEqual(row["request"]["temperature"], 0.0)
                    self.assertEqual(row["request"]["top_p"], 1.0)
                    self.assertEqual(row["request"]["seed"], 0)
                    self.assertEqual(row["request"]["serialized_concurrency"], 1)
                    self.assertEqual(row["cpu_activity_union_ms"], 1.25)
                    self.assertEqual(row["cuda_activity_union_ms"], 2.5)
                    self.assertEqual(row["kernel_duration_sum_ms"], 3.75)
                    validate_row_artifacts(row_path)
                    sidecar = row_path.with_name("row.json.sha256").read_text(encoding="utf-8").split()[0]
                    self.assertEqual(sidecar, hashlib.sha256(row_path.read_bytes()).hexdigest())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_failure_writes_unavailable_row_without_retry(self):
        state = FakeState(fail_on_completion=3)
        server, thread = _start_server(state)
        with tempfile.TemporaryDirectory() as directory:
            env = _environment(server)
            output_dir = Path(directory) / "cal_i128_o32" / "r01"
            try:
                result = _run(output_dir, "r01", env)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(state.completion_calls, 3)
                row_path = output_dir / "row.json"
                row = json.loads(row_path.read_text(encoding="utf-8"))
                self.assertEqual(row["status"], "unavailable")
                self.assertEqual(row["unavailable_reason"]["code"], "server_response_failed")
                validate_row_artifacts(row_path)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_checksum_validation_rejects_tampered_raw_artifact(self):
        state = FakeState()
        server, thread = _start_server(state)
        with tempfile.TemporaryDirectory() as directory:
            env = _environment(server)
            output_dir = Path(directory) / "cal_i128_o32" / "r01"
            try:
                result = _run(output_dir, "r01", env)
                self.assertEqual(result.returncode, 0, result.stderr)
                (output_dir / "response.json").write_bytes(b"tampered")
                with self.assertRaises(RunnerError):
                    validate_row_artifacts(output_dir / "row.json")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
