import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory


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

    def test_lightning_provider_is_supported_with_explicit_window_and_cap(self):
        now = datetime.now(timezone.utc)
        values = {
            "authorized": "true",
            "provider": "lightning",
            "maximum_gpu_hours": "6",
            "maximum_dollars": "27",
            "maximum_gate": "G3A",
            "authorized_start_utc": (now - timedelta(minutes=5)).isoformat(),
            "stop_launching_utc": (now + timedelta(hours=2)).isoformat(),
            "begin_export_utc": (now + timedelta(hours=2)).isoformat(),
            "hard_console_termination_utc": (now + timedelta(hours=3)).isoformat(),
            "user_available_to_export": "true",
            "user_available_to_terminate": "true",
            "backup_destination": "/tmp/eic-export",
        }
        with TemporaryDirectory() as temp:
            session = Path(temp) / "lightning.yaml"
            session.write_text("\n".join(f"{key}: {value}" for key, value in values.items()) + "\n")
            result = subprocess.run(
                ["bash", str(SCRIPT), "--session", str(session), "--gate", "G3A"],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("paid-session gate passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
