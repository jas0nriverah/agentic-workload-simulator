import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "start.sh"


class BootstrapScriptTests(unittest.TestCase):
    def test_script_is_executable_and_fail_closed(self):
        self.assertTrue(SCRIPT.stat().st_mode & 0o111)
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("set -Eeuo pipefail", text)
        self.assertIn("Docker VM validation is unavailable inside a container/Pod", text)

    def test_help_is_read_only(self):
        result = subprocess.run(
            [str(SCRIPT), "--help"], cwd=ROOT, text=True, capture_output=True, check=True
        )
        self.assertIn("--dry-run", result.stdout)
        self.assertIn("--require-docker", result.stdout)

    def test_dry_run_is_read_only(self):
        result = subprocess.run(
            [str(SCRIPT), "--dry-run"], cwd=ROOT, text=True, capture_output=True, check=True
        )
        self.assertIn("DRY-RUN", result.stdout)
        self.assertIn("venv", result.stdout)
        self.assertNotIn("Setup complete.", result.stdout)

    def test_bootstrap_never_launches_a_workload(self):
        text = SCRIPT.read_text(encoding="utf-8")
        for forbidden in ("docker run", "vllm.entrypoints", "run_a100_final_validation.sh"):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
