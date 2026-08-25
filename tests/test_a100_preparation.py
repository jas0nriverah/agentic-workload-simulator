import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.analysis.feature_validation import fit, load_protocol, seal
from scripts.cloud.a100_setup_doctor import DoctorError, read_manifest, validate_hardware_record, validate_manifest, validate_protocol
from tests.test_h100_case_runner import FakeState, _start_server


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs/a100_final_validation.json"
sys.path.insert(0, str(ROOT / "src"))


class A100PreparationTests(unittest.TestCase):
    def test_protocol_is_exact_ampere_matrix_and_safety_bound(self):
        protocol, protocol_hash, _ = load_protocol(CONFIG)
        validate_protocol(protocol)
        self.assertEqual(protocol["hardware"]["architecture"], "Ampere")
        self.assertEqual(protocol["hardware"]["required_compute_capability"], "8.0")
        self.assertEqual(len(protocol["calibration_configs"]), 24)
        self.assertEqual(len(protocol["sealed_holdouts"]), 12)
        self.assertEqual(sum(x["holdout_kind"] == "interpolation" for x in protocol["sealed_holdouts"]), 8)
        self.assertEqual(sum(x["holdout_kind"] == "extrapolation" for x in protocol["sealed_holdouts"]), 4)
        self.assertEqual(protocol["safety"]["max_total_wall_clock_seconds"], 14400)
        self.assertEqual(len(protocol_hash), 64)

    def test_hardware_guard_accepts_only_one_a100_80gb_ampere(self):
        protocol, _, _ = load_protocol(CONFIG)
        record = {"gpu_name": "NVIDIA A100-SXM4-80GB", "memory_total_mib": 81251,
                  "compute_capability": "8.0", "architecture": "Ampere", "gpu_count": 1,
                  "compute_processes": [], "uuid": "fixture", "driver": "580.0", "cuda": "13.0"}
        self.assertEqual(validate_hardware_record(record, protocol)["architecture"], "Ampere")
        for key, value in (("gpu_name", "NVIDIA A100-SXM4-40GB"), ("compute_capability", "9.0"), ("architecture", "Hopper")):
            bad = dict(record)
            bad[key] = value
            with self.assertRaises(DoctorError):
                validate_hardware_record(bad, protocol)

    def test_startup_manifest_is_hash_bound_and_separates_recovery_root(self):
        protocol, _, _ = load_protocol(CONFIG)
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        content = (ROOT / "cloud/gcp/a100_startup_manifest.env.example").read_text()
        content = content.replace("<40-hex-pushed-commit>", commit)
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "a100-startup.env"
            manifest.write_text(content)
            validate_manifest(read_manifest(manifest), protocol, CONFIG)

    def test_a100_runner_validate_only_has_no_gpu_or_artifact_access(self):
        command = [str(ROOT / "scripts/cloud/a100_case_runner.py"), "--validate-only",
                   "--config", str(CONFIG), "--case-id", "cal_i128_o32", "--split", "calibration",
                   "--input-tokens", "128", "--output-tokens", "32", "--repeat-id", "r01",
                   "--output-dir", "/tmp/a100-runner-contract"]
        env = os.environ.copy()
        result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no server, GPU, trace provider, or artifact access", result.stdout)

    def test_a100_runner_adapter_executes_serialized_fixture_row(self):
        state = FakeState()
        server, thread = _start_server(state)
        try:
            env = os.environ.copy()
            base = "http://127.0.0.1:{}".format(server.server_port)
            env.update({
                "A100_RUNNER_TEST_MODE": "1",
                "A100_TEST_SERVER_URL": base,
                "A100_VLLM_BASE_URL": base,
                "A100_VLLM_MODEL": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
                "A100_TEST_HARDWARE_JSON": json.dumps({
                    "gpu_name": "NVIDIA A100-SXM4-80GB", "memory_total_mib": 81251,
                    "compute_capability": "8.0", "architecture": "Ampere", "gpu_count": 1,
                    "compute_processes": [],
                }),
                "A100_TRACE_PROVIDER": str(ROOT / "tests/fixtures/a100_fake_trace_provider.py"),
                "A100_TEST_REQUEST_SPACING_SECONDS": "0",
            })
            with tempfile.TemporaryDirectory() as directory:
                output_dir = Path(directory) / "cal_i128_o32" / "r01"
                result = subprocess.run([
                    str(ROOT / "scripts/cloud/a100_case_runner.py"), "--config", str(CONFIG),
                    "--case-id", "cal_i128_o32", "--split", "calibration", "--input-tokens", "128",
                    "--output-tokens", "32", "--repeat-id", "r01", "--output-dir", str(output_dir),
                ], cwd=ROOT, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr + " errors=" + repr(state.errors) + " calls=" + str(state.completion_calls))
                row = json.loads((output_dir / "row.json").read_text())
                self.assertEqual(row["schema_version"], "a100-final-row.v1")
                self.assertEqual(row["hardware"]["gpu_name"], "NVIDIA A100-SXM4-80GB")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_a100_fit_freezes_predictions_using_calibration_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            protocol, _, _ = load_protocol(CONFIG)
            seal(CONFIG, root)
            for case in protocol["calibration_configs"]:
                for index, repeat in enumerate(("r01", "r02", "r03")):
                    path = root / "calibration" / case["case_id"] / repeat / "row.json"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps({"schema_version": "a100-final-row.v1", "status": "completed",
                                                "wall_ms": 1000 + case["input_tokens"] + index}) + "\n")
            result = fit(CONFIG, root)
            prediction = json.loads(Path(result["prediction_manifest"]).read_text())
            self.assertEqual(prediction["schema_version"], "a100-feature-predictions.v1")
            self.assertEqual(len(prediction["predictions"]), 12)
            self.assertNotIn("wall_ms", json.dumps(prediction))
            self.assertTrue((root / "derived/prediction_manifest.sha256").is_file())

    def test_a100_driver_dry_run_is_gpu_free_and_deadline_is_declared(self):
        result = subprocess.run([str(ROOT / "scripts/cloud/run_a100_final_validation.sh"), "--dry-run"],
                                cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("24 calibration", result.stdout)
        self.assertIn("no GPU", result.stdout)
        self.assertIn("max_total_wall_clock_seconds", CONFIG.read_text())

    def test_a100_resume_and_failure_paths_are_fail_closed(self):
        source = (ROOT / "scripts/cloud/a100_execution.py").read_text()
        self.assertIn("--resume", source)
        self.assertIn("immutable row exists", source)
        self.assertIn("pre-reveal proof", source)
        self.assertIn("RECOVERY_ROOT", source)
        self.assertIn('docker", "stop"', source)


if __name__ == "__main__":
    unittest.main()
