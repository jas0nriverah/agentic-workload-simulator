import hashlib
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
PYTHON_LOCK_SHA256 = "7e1177bf4c0b4efe4d64895f39b340413336b77e02d2f72bbf5aad387accc9cc"

class LambdaRuntimeScriptTests(unittest.TestCase):
    def run_script(self, script, *args, env=None):
        merged = os.environ.copy()
        merged.update(env or {})
        return subprocess.run(["bash", str(script), *args], capture_output=True, text=True, env=merged)

    def test_required_scripts_exist_and_are_executable(self):
        for script in RUNTIME:
            self.assertTrue(script.is_file(), script)
            self.assertTrue(os.access(script, os.X_OK), script)

    def test_shell_syntax(self):
        for script in RUNTIME:
            result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, f"{script}: {result.stderr}")

    def test_all_dry_runs_are_local_and_deterministic(self):
        for script in RUNTIME:
            first = self.run_script(script, "--dry-run")
            second = self.run_script(script, "--dry-run")
            self.assertEqual(first.returncode, 0, f"{script}: {first.stderr}")
            self.assertEqual(first.stdout, second.stdout)
            self.assertIn("DRY-RUN", first.stdout)

    def test_dry_run_contains_immutable_runtime_values(self):
        bootstrap = self.run_script(RUNTIME[1], "--dry-run")
        download = self.run_script(RUNTIME[2], "--dry-run")
        start = self.run_script(RUNTIME[3], "--dry-run")
        self.assertIn(VLLM_REVISION, bootstrap.stdout)
        self.assertIn("linux/amd64", bootstrap.stdout)
        self.assertIn(MODEL_REVISION, download.stdout)
        self.assertIn(MODEL_REVISION, start.stdout)
        self.assertIn("requirements-linux-x86_64.txt", bootstrap.stdout)
        self.assertIn("--require-hashes", (ROOT / "scripts/cloud/lambda_bootstrap.sh").read_text(encoding="utf-8"))
        self.assertEqual(hashlib.sha256((ROOT / "cloud/lambda/requirements-linux-x86_64.txt").read_bytes()).hexdigest(), PYTHON_LOCK_SHA256)
        self.assertIn("--tool-call-parser qwen3_coder", start.stdout)
        self.assertIn("--dtype bfloat16", start.stdout)
        self.assertIn("--max-model-len 32768", start.stdout)

    def test_start_dry_run_propagates_non_default_manifest_values(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest = pathlib.Path(temp) / "manifest.env"
            manifest.write_text("\n".join([
                "WORK_ROOT=/tmp/non-default-work",
                "VLLM_MODEL=org/NonDefaultModel",
                "VLLM_MODEL_REVISION=0123456789abcdef0123456789abcdef01234567",
                "VLLM_IMAGE=registry.example/vllm:v0.10.0@sha256:" + "a" * 64,
                "VLLM_IMAGE_DIGEST=sha256:" + "a" * 64,
                "VLLM_IMAGE_PLATFORM=linux/amd64",
                "VLLM_VERSION=0.10.0",
                "VLLM_TOOL_PARSER=qwen3_coder",
                "VLLM_MAX_MODEL_LEN=16384",
                "VLLM_HEALTH_CONTEXT=4096",
                "VLLM_GPU_MEMORY_UTILIZATION=0.75",
                "VLLM_TENSOR_PARALLEL_SIZE=1",
                "VLLM_PORT=8123",
            ]) + "\n", encoding="utf-8")
            result = self.run_script(RUNTIME[3], "--manifest", str(manifest), "--dry-run")
            self.assertEqual(result.returncode, 0, result.stderr)
            for value in ("org/NonDefaultModel", "0123456789abcdef0123456789abcdef01234567", "16384", "0.75", "--port 8123", "--tensor-parallel-size 1"):
                self.assertIn(value, result.stdout)

    def test_manifest_is_not_sourced_or_secret_logged(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest = pathlib.Path(temp) / "manifest.env"
            secret = "do-not-print-this-token"
            manifest.write_text(f"VLLM_MODEL_REVISION={MODEL_REVISION}\nHF_TOKEN={secret}\n")
            result = self.run_script(RUNTIME[2], "--manifest", str(manifest), "--dry-run")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn(secret, result.stdout + result.stderr)

    def test_download_assets_resolves_managed_python_from_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest = pathlib.Path(temp) / "managed.env"
            manifest.write_text(
                "PYTHON_ENV_MODE=managed\n"
                "PYTHON_ENV_ROOT=/opt/managed-python\n",
                encoding="utf-8",
            )
            text = RUNTIME[2].read_text(encoding="utf-8")
            self.assertIn("PYTHON_ENV_ROOT", text)
            self.assertIn('PYTHON_BIN="${PYTHON_BIN:-$PYTHON_ENV_ROOT/bin/python}"', text)
            self.assertIn('HF_CLI="${HF_CLI:-$PYTHON_ENV_ROOT/bin/hf}"', text)
            self.assertIn("--exclude '*optimizer*' --exclude '*checkpoint*'", text)
            result = self.run_script(RUNTIME[2], "--manifest", str(manifest), "--dry-run")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("with /opt/managed-python/bin/hf", result.stdout)

    def test_non_default_vllm_manifest_values_reach_dry_run_command(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest = pathlib.Path(temp) / "manifest.env"
            manifest.write_text(
                "\n".join(
                    [
                        "VLLM_MODEL=org/NonDefaultModel",
                        "VLLM_MODEL_REVISION=0123456789abcdef0123456789abcdef01234567",
                        "VLLM_MAX_MODEL_LEN=16384",
                        "VLLM_HEALTH_CONTEXT=4096",
                        "VLLM_GPU_MEMORY_UTILIZATION=0.73",
                        "VLLM_TOOL_PARSER=qwen3_coder",
                        "VLLM_TENSOR_PARALLEL_SIZE=1",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            result = self.run_script(RUNTIME[3], "--manifest", str(manifest), "--dry-run")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--max-model-len 16384", result.stdout)
            self.assertIn("--gpu-memory-utilization 0.73", result.stdout)
            self.assertIn("--tensor-parallel-size 1", result.stdout)
            self.assertIn("org/NonDefaultModel", result.stdout)

    def test_start_uses_manifest_cache_and_never_pulls_after_bootstrap(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest = pathlib.Path(temp) / "manifest.env"
            manifest.write_text(
                "\n".join([
                    "WORK_ROOT=/tmp/non-default-work",
                    "CACHE_ROOT=/tmp/non-default-cache",
                    "VLLM_MODEL_REVISION=0123456789abcdef0123456789abcdef01234567",
                    "VLLM_IMAGE=registry.example/vllm:v0.10.0@sha256:" + "a" * 64,
                    "VLLM_IMAGE_DIGEST=sha256:" + "a" * 64,
                    "VLLM_IMAGE_PLATFORM=linux/amd64",
                    "VLLM_TOOL_PARSER=qwen3_coder",
                    "VLLM_TENSOR_PARALLEL_SIZE=1",
                ]) + "\n",
                encoding="utf-8",
            )
            result = self.run_script(RUNTIME[3], "--manifest", str(manifest), "--dry-run")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("/tmp/non-default-cache/huggingface:/root/.cache/huggingface", result.stdout)
            self.assertIn("--pull=never", result.stdout)

    def test_health_contract_uses_metrics_endpoint_not_chat_metrics(self):
        text = RUNTIME[4].read_text(encoding="utf-8")
        self.assertIn("/metrics", text)
        self.assertIn("vllm:request_success_total", text)
        self.assertIn("tool-parser failure", text)
        self.assertIn("telemetry-contract failure", text)
        self.assertNotIn('d.get("metrics")', text)
        self.assertIn("vllm:e2e_request_latency_seconds_(bucket|count|sum)", text)
        self.assertIn("vllm_config", text)
        self.assertIn("server manifest does not match instance manifest", text)

    def test_atomic_gpu_lease_and_resolved_config_are_explicit(self):
        text = RUNTIME[3].read_text(encoding="utf-8")
        for field in ("hostname", "task_id", "experiment_id", "gpu", "vllm_port", "config_hash", "acquired_at_utc", "health_context", "tensor_parallel_size"):
            self.assertIn(field, text)
        self.assertIn('mkdir -- "$LOCK_DIR"', text)
        self.assertIn("tool-call-parser", text)
        self.assertIn("--gpus device=0", text)

    def test_start_rechecks_image_digest_and_platform_before_launch(self):
        text = RUNTIME[3].read_text(encoding="utf-8")
        self.assertIn("pinned vLLM image digest mismatch", text)
        self.assertIn("pinned vLLM image platform mismatch", text)
        self.assertIn("--pull=never", text)

    def test_resume_validates_vllm_digest_and_platform(self):
        text = (ROOT / "scripts/cloud/lambda_bootstrap.sh").read_text(encoding="utf-8")
        self.assertIn("RepoDigests", text)
        self.assertIn("vLLM image digest mismatch", text)
        self.assertIn("vLLM image platform mismatch", text)

    def test_bootstrap_installs_utilities_before_blocking_preflight(self):
        text = (ROOT / "scripts/cloud/lambda_bootstrap.sh").read_text(encoding="utf-8")
        self.assertLess(text.index("stage utilities"), text.index("stage preflight"))

    def test_bootstrap_supports_managed_studio_environment_without_venv_creation(self):
        text = (ROOT / "scripts/cloud/lambda_bootstrap.sh").read_text(encoding="utf-8")
        self.assertIn("PYTHON_ENV_MODE", text)
        self.assertIn("managed Python environment is unavailable", text)
        self.assertIn('[[ "$PYTHON_ENV_MODE" == managed ]] || mkdir -p -- "$VENV"', text)
        self.assertIn('[[ "$PYTHON_ENV_MODE" != managed ]]', text)
        with tempfile.TemporaryDirectory() as temp:
            manifest = pathlib.Path(temp) / "managed.env"
            manifest.write_text("PYTHON_ENV_MODE=managed\nPYTHON_ENV_ROOT=/opt/conda\n", encoding="utf-8")
            result = self.run_script(RUNTIME[1], "--manifest", str(manifest), "--dry-run")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("use managed Python environment /opt/conda", result.stdout)

    def test_bootstrap_binds_lock_and_resume_markers_to_runtime_contract(self):
        text = (ROOT / "scripts/cloud/lambda_bootstrap.sh").read_text(encoding="utf-8")
        for field in ("PYTHON_LOCK_PATH", "PYTHON_VERSION_EXACT", "bootstrap_fingerprint", "--python-platform x86_64-manylinux2014", "python-freeze.txt", "pip_check_with_managed_allowlist", "python-pip-check.json"):
            self.assertIn(field, text)
        self.assertIn("grep -Fqx \"bootstrap_fingerprint=$BOOTSTRAP_FINGERPRINT\"", text)

    def test_bootstrap_rejects_python_version_lock_mismatch_before_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest = pathlib.Path(temp) / "bad-lock.env"
            manifest.write_text(
                "PYTHON_VERSION=3.12\n"
                "PYTHON_LOCK_PATH=" + str(ROOT / "cloud/lambda/requirements-linux-x86_64.txt") + "\n"
                "PYTHON_LOCK_SHA256=" + PYTHON_LOCK_SHA256 + "\n",
                encoding="utf-8",
            )
            result = self.run_script(RUNTIME[1], "--manifest", str(manifest), "--dry-run")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not resolved for Python 3.12", result.stderr)

    def test_bootstrap_cannot_mask_failed_package_install_or_inventory_audit(self):
        text = (ROOT / "scripts/cloud/lambda_bootstrap.sh").read_text(encoding="utf-8")
        self.assertIn("( set -Eeuo pipefail; \"$@\" )", text)
        self.assertIn('( set -Eeuo pipefail; eval "$validator" )', text)
        self.assertIn('"${reinstall[@]}" --only-binary=:all: --require-hashes -r "$PYTHON_LOCK" || return 1', text)
        self.assertIn('"$REPOS/SWE-agent" -e "$REPOS/SWE-bench" -e "$ROOT" || return 1', text)
        self.assertIn('pip_check_with_managed_allowlist || return 1', text)
        self.assertIn('"$VENV/bin/python" -m pip freeze --all > "$WORK_ROOT/artifacts/manifests/python-freeze.txt" || return 1', text)
        self.assertIn("validate_python_environment || return 1", text)
        self.assertIn('pip_check_with_managed_allowlist() {', text)

    def test_managed_pip_check_allowlist_is_exact_and_fail_closed(self):
        text = (ROOT / "scripts/cloud/lambda_bootstrap.sh").read_text(encoding="utf-8")
        for fragment in (
            '("matplotlib", "3.8.2", "numpy", "<2,>=1.21", "numpy", "2.4.6")',
            '("scikit-learn", "1.3.2", "numpy", "<2.0,>=1.17.3", "numpy", "2.4.6")',
            '("scipy", "1.11.4", "numpy", "<1.28.0,>=1.21.6", "numpy", "2.4.6")',
            '("lightning-sdk", "2026.6.8", "urllib3", "<=2.5.0", "urllib3", "2.7.0")',
        ):
            self.assertIn(fragment, text)
        for marker in ("PASS_MANAGED_BASE_ALLOWLIST", "unexpected_conflicts", "conflicts", "python-pip-check.txt", "python-pip-check.json", "canonicalize_name"):
            self.assertIn(marker, text)
        self.assertIn("if pip_status == 0:", text)
        self.assertIn('environment_mode != "managed"', text)

    def test_python_packages_imports_all_direct_workload_dependencies(self):
        text = (ROOT / "scripts/cloud/lambda_bootstrap.sh").read_text(encoding="utf-8")
        self.assertIn("import agentic_sim, datasets, docker, numpy, pandas, requests, sweagent, swebench, urllib3", text)

    def test_preflight_dry_run_does_not_create_report(self):
        with tempfile.TemporaryDirectory() as temp:
            output = pathlib.Path(temp) / "preflight.json"
            result = self.run_script(RUNTIME[0], "--dry-run", "--output", str(output))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(output.exists())

    def test_preflight_uses_authenticated_docker_registry_probe(self):
        text = RUNTIME[0].read_text(encoding="utf-8")
        self.assertIn("https://registry-1.docker.io/v2/", text)
        self.assertIn("authentication required", text)
        self.assertIn(":401", text)

if __name__ == "__main__":
    unittest.main()
