import hashlib
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
STARTUP = ROOT / "scripts/cloud/start_h100.sh"
CONFIG = ROOT / "configs/h100_final_validation.json"
PYTHON_LOCK = ROOT / "cloud/lambda/requirements-linux-x86_64.txt"
SYSTEM_LOCK = ROOT / "cloud/gcp/h100_system_packages_ubuntu22.04-amd64.lock"
MODEL_REVISION = "b2cff646eb4bb1d68355c01b18ae02e7cf42d120"
MODEL = "Qwen/Qwen3-Coder-30B-A3B-Instruct"
IMAGE = "vllm/vllm-openai:v0.10.0@sha256:05a31dc4185b042e91f4d2183689ac8a87bd845713d5c3f987563c5899878271"


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class H100StartupTests(unittest.TestCase):
    def _fixture_repo(self, temp):
        repo = pathlib.Path(temp) / "repo"
        for relative in (
            "scripts/cloud/start_h100.sh",
            "scripts/cloud/start_h100_vllm_nsight.sh",
            "scripts/cloud/h100_nsight_trace_provider.py",
            "configs/h100_final_validation.json",
            "cloud/lambda/requirements-linux-x86_64.txt",
            "cloud/gcp/h100_system_packages_ubuntu22.04-amd64.lock",
        ):
            destination = repo / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, destination)
        subprocess.run(["git", "init", "-b", "parallel-h100-shards"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "H100 startup test"], cwd=repo, check=True)
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "fixture"], cwd=repo, check=True, capture_output=True)
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
        return repo, commit

    def _manifest(self, repo, commit, work_root, model_exists=True, provider=True, python_hash=None):
        cache = pathlib.Path(work_root) / "cache" / "huggingface"
        snapshot = cache / "hub" / f"models--{MODEL.replace('/', '--')}" / "snapshots" / MODEL_REVISION
        cache.mkdir(parents=True, exist_ok=True)
        if model_exists:
            snapshot.mkdir(parents=True, exist_ok=True)
            (snapshot / "config.json").write_text("{}\n", encoding="utf-8")
            (snapshot / "tokenizer.json").write_text("{}\n", encoding="utf-8")
            (snapshot / "model.safetensors").write_bytes(b"fixture")
        if not provider:
            (repo / "scripts/cloud/h100_nsight_trace_provider.py").unlink()
        manifest = pathlib.Path(work_root).parent / "h100-startup.env"
        manifest.write_text(
            "\n".join(
                [
                    "REQUIRED_BRANCH=parallel-h100-shards",
                    f"REQUIRED_COMMIT={commit}",
                    f"PROTOCOL_SHA256={sha256(repo / 'configs/h100_final_validation.json')}",
                    f"PYTHON_LOCK_SHA256={python_hash or sha256(repo / 'cloud/lambda/requirements-linux-x86_64.txt')}",
                    f"SYSTEM_LOCK_SHA256={sha256(repo / 'cloud/gcp/h100_system_packages_ubuntu22.04-amd64.lock')}",
                    f"WORK_ROOT={work_root}",
                    f"PYTHON_ENV_ROOT={work_root}/venv",
                    "PYTHON_BOOTSTRAP_BIN=python3.11",
                    f"MODEL_CACHE={cache}",
                    f"MODEL_SNAPSHOT={snapshot}",
                    f"TRACE_ROOT={work_root}/h100-startup-traces",
                    f"VLLM_MODEL={MODEL}",
                    f"VLLM_MODEL_REVISION={MODEL_REVISION}",
                    f"VLLM_IMAGE={IMAGE}",
                    "VLLM_IMAGE_PLATFORM=linux/amd64",
                    "VLLM_TOOL_PARSER=qwen3_coder",
                    "VLLM_MAX_MODEL_LEN=32768",
                    "VLLM_TENSOR_PARALLEL_SIZE=1",
                    "VLLM_PORT=8000",
                    "VLLM_GPU_MEMORY_UTILIZATION=0.90",
                    "H100_CONTAINER=h100-final-vllm",
                    "H100_NSYS_SESSION=h100-final-validation",
                    "CUDA_VERSION=13.0",
                    "NSYS_VERSION_PREFIX=2025.1.3",
                    "HF_TOKEN=must-not-be-read-or-printed",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return manifest

    def _run_dry(self, repo, manifest):
        return subprocess.run(
            ["bash", str(repo / "scripts/cloud/start_h100.sh"), "--manifest", str(manifest), "--dry-run"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_fresh_setup_dry_run_is_non_mutating_and_resolves_repo_root(self):
        with tempfile.TemporaryDirectory() as temp:
            repo, commit = self._fixture_repo(temp)
            work_root = pathlib.Path(temp) / "work"
            manifest = self._manifest(repo, commit, work_root)
            result = self._run_dry(repo, manifest)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("DRY-RUN", result.stdout)
            self.assertIn("branch=parallel-h100-shards", result.stdout)
            self.assertFalse((work_root / "state").exists())
            self.assertFalse((work_root / "cache/pip").exists())
            self.assertNotIn("must-not-be-read-or-printed", result.stdout + result.stderr)

    def test_missing_model_fails_closed_before_any_start(self):
        with tempfile.TemporaryDirectory() as temp:
            repo, commit = self._fixture_repo(temp)
            manifest = self._manifest(repo, commit, pathlib.Path(temp) / "work", model_exists=False)
            result = self._run_dry(repo, manifest)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("exact model snapshot is missing", result.stderr)

    def test_missing_provider_fails_closed_and_never_falls_back_to_fixture(self):
        with tempfile.TemporaryDirectory() as temp:
            repo, commit = self._fixture_repo(temp)
            manifest = self._manifest(repo, commit, pathlib.Path(temp) / "work", provider=False)
            result = self._run_dry(repo, manifest)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("real production trace provider is missing", result.stderr)

    def test_lockfile_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            repo, commit = self._fixture_repo(temp)
            manifest = self._manifest(repo, commit, pathlib.Path(temp) / "work", python_hash="0" * 64)
            result = self._run_dry(repo, manifest)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Python lock hash mismatch", result.stderr)

    def test_already_installed_packages_are_reused_only_after_lock_validation(self):
        text = STARTUP.read_text(encoding="utf-8")
        self.assertIn('"$SYSTEM_LOCK_SHA256" && "$packages_ok" == 1', text)
        self.assertIn('"$PYTHON_LOCK_SHA256" && "$packages_ok" == 1', text)
        self.assertIn("System packages already validated", text)
        self.assertIn("Python packages already validated", text)
        self.assertIn("packages_ok=0", text)

    def test_held_system_package_at_locked_version_is_accepted(self):
        text = STARTUP.read_text(encoding="utf-8")
        self.assertIn('== *" ok installed $version"', text)
        self.assertIn('!= *" ok installed $version"', text)

    def test_runtime_config_extraction_heredoc_is_valid_python(self):
        text = STARTUP.read_text(encoding="utf-8")
        marker = 'CONFIG_VALUES="$("$PYTHON_BIN" - "$CONFIG" <<\'PY\'\n'
        start = text.index(marker) + len(marker)
        end = text.index("\nPY\n)\"", start)
        compile(text[start:end], "start_h100_config_values.py", "exec")

    def test_missing_packages_use_pinned_offline_first_hash_checked_install(self):
        text = STARTUP.read_text(encoding="utf-8")
        self.assertIn("--no-download", text)
        self.assertIn("--allow-downgrades", text)
        self.assertIn("--no-index", text)
        self.assertIn("--require-hashes", text)
        self.assertIn("--only-binary=:all:", text)
        self.assertIn("PIP_CACHE_DIR=", text)
        self.assertNotIn("apt-get install -y --no-install-recommends \"${missing", text)

    def test_stale_server_is_not_restarted_or_removed(self):
        text = STARTUP.read_text(encoding="utf-8")
        self.assertIn("stale stopped container exists; inspect before recovery", text)
        self.assertIn("running container is stale or pin-mismatched", text)
        self.assertNotIn("docker rm", text)
        self.assertNotIn("docker stop", text)

    def test_duplicate_server_is_rejected(self):
        text = STARTUP.read_text(encoding="utf-8")
        self.assertIn("duplicate or conflicting running container", text)
        self.assertIn("GPU already has compute processes", text)
        self.assertIn("port $PORT is occupied by a non-reviewed process", text)

    def test_health_failure_is_explicit_and_fail_closed(self):
        text = STARTUP.read_text(encoding="utf-8")
        for endpoint in ("$base/health", "$base/v1/models", "$base/v1/completions", "$base/v1/chat/completions", "$base/metrics"):
            self.assertIn(endpoint, text)
        self.assertIn("qwen3_coder tool parsing", text)
        self.assertIn("fatal text in server logs", text)

    def test_startup_never_uses_nvprof_or_starts_protocol_phases(self):
        text = STARTUP.read_text(encoding="utf-8")
        self.assertNotIn("nvprof", text)
        self.assertNotIn("run_h100_final_validation.sh", text)
        self.assertNotIn("--phase holdout", text)
        self.assertIn("h100_nsight_trace_provider.py", text)
        self.assertIn("start_h100_vllm_nsight.sh", text)

    def test_handoff_documents_one_command_startup_and_non_destructive_recovery(self):
        text = (ROOT / "H100_FINAL_VM_HANDOFF.md").read_text(encoding="utf-8")
        self.assertIn("scripts/cloud/start_h100.sh --manifest /mnt/eic-work/h100-startup.env", text)
        self.assertIn("--dry-run", text)
        self.assertIn("docker inspect h100-final-vllm", text)
        self.assertIn("docker rm h100-final-vllm", text)
        self.assertIn("without touching any repository or artifact root", text)

    def test_shell_syntax_and_executable_contract(self):
        self.assertTrue(os.access(STARTUP, os.X_OK))
        result = subprocess.run(["bash", "-n", str(STARTUP)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
