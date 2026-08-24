import os
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DOCTOR = ROOT / "scripts" / "cloud" / "h100_setup_doctor.sh"


class H100SetupDoctorTests(unittest.TestCase):
    def run_doctor(self, *args):
        return subprocess.run(
            ["bash", str(DOCTOR), *args],
            cwd=ROOT,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
        )

    def test_script_is_executable_and_syntactically_valid(self):
        self.assertTrue(DOCTOR.is_file())
        self.assertTrue(os.access(DOCTOR, os.X_OK))
        result = subprocess.run(["bash", "-n", str(DOCTOR)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_offline_mode_checks_sealed_contract_without_runtime_access(self):
        result = self.run_doctor("--offline")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("READY_FOR_VM_PREFLIGHT", result.stdout)
        self.assertIn("no runtime or GPU checks performed", result.stdout)
        self.assertNotIn("H100_MODEL_SNAPSHOT is unset", result.stdout)
        self.assertNotIn("H100_TRACE_PROVIDER is unset", result.stdout)

    def test_default_mode_fails_closed_when_vm_inputs_are_absent(self):
        result = self.run_doctor()
        self.assertNotEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertIn("H100_MODEL_SNAPSHOT", combined)
        self.assertIn("H100_TRACE_PROVIDER", combined)

    def test_help_describes_non_mutating_behavior(self):
        result = self.run_doctor("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("never allocates cloud resources", result.stdout)
        self.assertIn("--check-server", result.stdout)
