import os
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
CLOUD = ROOT / "scripts" / "cloud"
RUNTIME = tuple(CLOUD / name for name in ("lambda_preflight.sh", "lambda_bootstrap.sh", "lambda_download_assets.sh", "lambda_start_vllm.sh", "lambda_healthcheck.sh"))
MODEL_REVISION = "b2cff646eb4bb1d68355c01b18ae02e7cf42d120"
VLLM_REVISION = "6d8d0a24c02bfd84d46b3016b865a44f048ae84b"

class LambdaRuntimeScriptTests(unittest.TestCase):
    def run_script(self, script, *args, env=None):
        merged = os.environ.copy(); merged.update(env or {})
        return subprocess.run(["bash", str(script), *args], capture_output=True, text=True, env=merged)

    def test_required_scripts_exist_and_are_executable(self):
        for script in RUNTIME:
            self.assertTrue(script.is_file(), script); self.assertTrue(os.access(script, os.X_OK), script)

    def test_shell_syntax(self):
        for script in RUNTIME:
            result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, f"{script}: {result.stderr}")

    def test_all_dry_runs_are_local_and_deterministic(self):
        for script in RUNTIME:
            first = self.run_script(script, "--dry-run"); second = self.run_script(script, "--dry-run")
            self.assertEqual(first.returncode, 0, f"{script}: {first.stderr}"); self.assertEqual(first.stdout, second.stdout); self.assertIn("DRY-RUN", first.stdout)

    def test_dry_run_contains_immutable_runtime_values(self):
        bootstrap = self.run_script(RUNTIME[1], "--dry-run"); download = self.run_script(RUNTIME[2], "--dry-run"); start = self.run_script(RUNTIME[3], "--dry-run")
        self.assertIn(VLLM_REVISION, bootstrap.stdout); self.assertIn("linux/amd64", bootstrap.stdout); self.assertIn(MODEL_REVISION, download.stdout); self.assertIn(MODEL_REVISION, start.stdout)
        self.assertIn("--tool-call-parser qwen3_coder", start.stdout); self.assertIn("--dtype bfloat16", start.stdout); self.assertIn("--max-model-len 32768", start.stdout)

    def test_manifest_is_not_sourced_or_secret_logged(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest = pathlib.Path(temp) / "manifest.env"; secret = "do-not-print-this-token"
            manifest.write_text(f"VLLM_MODEL_REVISION={MODEL_REVISION}\nHF_TOKEN={secret}\n")
            result = self.run_script(RUNTIME[2], "--manifest", str(manifest), "--dry-run")
            self.assertEqual(result.returncode, 0, result.stderr); self.assertNotIn(secret, result.stdout + result.stderr)

    def test_health_contract_uses_metrics_endpoint_not_chat_metrics(self):
        text = RUNTIME[4].read_text(encoding="utf-8")
        self.assertIn("/metrics", text); self.assertIn("vllm:request_success_total", text); self.assertIn("tool-parser failure", text); self.assertIn("telemetry-contract failure", text); self.assertNotIn('d.get("metrics")', text)

    def test_atomic_gpu_lease_and_resolved_config_are_explicit(self):
        text = RUNTIME[3].read_text(encoding="utf-8")
        for field in ("hostname", "task_id", "experiment_id", "gpu", "vllm_port", "config_hash", "acquired_at_utc"): self.assertIn(field, text)
        self.assertIn('mkdir -- "$LOCK_DIR"', text); self.assertIn("tool-call-parser", text); self.assertIn("--gpus device=0", text)

    def test_preflight_dry_run_does_not_create_report(self):
        with tempfile.TemporaryDirectory() as temp:
            output = pathlib.Path(temp) / "preflight.json"; result = self.run_script(RUNTIME[0], "--dry-run", "--output", str(output))
            self.assertEqual(result.returncode, 0, result.stderr); self.assertFalse(output.exists())

if __name__ == "__main__": unittest.main()
