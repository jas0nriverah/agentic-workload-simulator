import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class OptionalScriptContractTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
