import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/cloud/lambda_session_gate.sh"


class LambdaSessionGateTests(unittest.TestCase):
    def test_dry_run_is_non_mutating(self):
        result = subprocess.run(
            ["bash", str(SCRIPT), "--session", "/does/not/exist", "--dry-run"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no provider API", result.stdout)

    def test_example_authorization_fails_closed(self):
        result = subprocess.run(
            ["bash", str(SCRIPT), "--session", str(ROOT / "cloud/lambda/cloud_session.yaml.example")],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("authorized must be true", result.stderr)


if __name__ == "__main__":
    unittest.main()
