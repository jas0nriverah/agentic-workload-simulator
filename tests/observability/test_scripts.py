import subprocess
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class OptionalScriptContractTests(unittest.TestCase):
    def test_deep_profile_reads_nested_and_legacy_capability_shapes(self):
        module_path = ROOT / "scripts/observability/run_deep_profile.py"
        spec = importlib.util.spec_from_file_location("run_deep_profile", module_path)
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        for payload in (
            {"tools": {"tools": {"nsys": {"status": "available", "executable": "/usr/bin/nsys"}}}},
            {"tools": {"nsys": {"status": "available", "executable": "/usr/bin/nsys"}}},
        ):
            with tempfile.TemporaryDirectory() as temp:
                manifest = Path(temp) / "capabilities.json"
                manifest.write_text(__import__("json").dumps(payload), encoding="utf-8")
                result = module._capability(manifest, "nsys")
                self.assertEqual(result["executable"], "/usr/bin/nsys")

    def test_calibration_is_pinned_container_only(self):
        text = (ROOT / "scripts/observability/calibrate_vllm.sh").read_text(encoding="utf-8")
        self.assertIn("vllm/vllm-openai:v0.10.0@sha256:", text)
        self.assertIn("docker run", text)
        self.assertIn("HF_HUB_OFFLINE=1", text)
        self.assertIn("lambda_session_gate.sh", text)
        self.assertNotIn('command -v "$VLLM_BIN"', text)

    def test_deep_profile_execution_requires_gate_and_capability_manifest(self):
        text = (ROOT / "scripts/observability/deep_profile.sh").read_text(encoding="utf-8")
        self.assertIn("--capability-manifest", text)
        self.assertIn("--first-result-marker", text)
        self.assertIn("lambda_session_gate.sh", text)
        self.assertIn("run_deep_profile.py", text)

    def test_all_observability_shell_dry_runs_are_local(self):
        commands = [
            ["bash", str(ROOT / "scripts/observability/deep_profile.sh"), "--mode", "strace", "--command", "echo ok", "--output", "/tmp/obs-test", "--run-id", "r", "--attempt-id", "a", "--dry-run"],
            ["bash", str(ROOT / "scripts/observability/calibrate_vllm.sh"), "--output", "/tmp/obs-cal", "--first-result-marker", "/tmp/marker", "--dry-run"],
        ]
        for command in commands:
            result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("DRY-RUN", result.stdout)

    def test_calibration_dry_run_tolerates_missing_optional_manifest_keys(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest = Path(temp) / "manifest.env"
            manifest.write_text(
                "VLLM_MODEL=Qwen/Qwen3-Coder-30B-A3B-Instruct\n"
                "VLLM_MODEL_REVISION=" + "a" * 40 + "\n"
                "VLLM_VERSION=0.10.0\n"
                "CACHE_ROOT=/tmp/calibration-cache\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    "bash",
                    str(ROOT / "scripts/observability/calibrate_vllm.sh"),
                    "--manifest",
                    str(manifest),
                    "--output",
                    "/tmp/obs-cal",
                    "--first-result-marker",
                    "/tmp/marker",
                    "--dry-run",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("DRY-RUN", result.stdout)


if __name__ == "__main__":
    unittest.main()
