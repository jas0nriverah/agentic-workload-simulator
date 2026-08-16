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

    def test_scripts_do_not_replace_nvidia_driver(self):
        for script in SCRIPTS:
            text = script.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"(nvidia-driver|cuda-drivers|NVIDIA-Linux).*install")


if __name__ == "__main__":
    unittest.main()
