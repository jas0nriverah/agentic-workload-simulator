import pathlib
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
RUNTIME_NAMES = (
    "lambda_preflight.sh", "lambda_bootstrap.sh", "lambda_download_assets.sh",
    "lambda_start_vllm.sh", "lambda_healthcheck.sh", "lambda_run_gold_smoke.sh",
    "lambda_run_first_experiment.sh",
)
SCRIPTS = [ROOT / "scripts" / "cloud" / name for name in RUNTIME_NAMES]


class LambdaRuntimeScriptTests(unittest.TestCase):
    def test_required_scripts_exist(self):
        self.assertTrue(all(p.is_file() for p in SCRIPTS))

    def test_shell_syntax(self):
        for script in SCRIPTS:
            result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, f"{script}: {result.stderr}")

    def test_dry_run_does_not_execute_cloud_commands(self):
        for script in SCRIPTS:
            result = subprocess.run(["bash", str(script), "--dry-run"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, f"{script}: {result.stderr}")
            self.assertIn("DRY-RUN", result.stdout)

    def test_first_experiment_modes_are_explicit(self):
        script = SCRIPTS[-1]
        for mode in ("uninstrumented", "thin-telemetry"):
            result = subprocess.run(
                [str(script), "--dry-run", "--mode", mode],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(mode, result.stdout)

    def test_scripts_do_not_replace_nvidia_driver(self):
        for script in SCRIPTS:
            text = script.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"(nvidia-driver|cuda-drivers|NVIDIA-Linux).*install")

    def test_gpu_lease_metadata_and_release_are_explicit(self):
        start = (ROOT / "scripts" / "cloud" / "lambda_start_vllm.sh").read_text(encoding="utf-8")
        stop = (ROOT / "scripts" / "cloud" / "lambda_stop_workloads.sh").read_text(encoding="utf-8")
        for field in ("hostname", "task_id", "experiment_id", "gpu", "vllm_port", "config_hash", "acquired_at_utc"):
            self.assertIn(field, start)
        self.assertIn("mkdir -- \"$LOCK_DIR\"", start)
        self.assertIn("rmdir -- \"$GPU_LOCK_DIR\"", stop)


if __name__ == "__main__":
    unittest.main()
