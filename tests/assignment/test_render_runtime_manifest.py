import hashlib
import importlib.util
import json
import stat
import subprocess
import tempfile
import unittest
import venv
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/assignment/render_runtime_manifest.py"
SPEC = importlib.util.spec_from_file_location("render_runtime_manifest", SCRIPT)
assert SPEC and SPEC.loader
RENDERER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RENDERER)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def make_repo(root: Path) -> tuple[Path, str]:
    repo = root / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "parallel-h100-shards", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "fixture@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Fixture"], check=True)
    (repo / "cloud/lambda").mkdir(parents=True)
    (repo / "cloud/lambda/sweagent_request.yaml").write_text("request: fixture\n", encoding="utf-8")
    (repo / "scripts/assignment").mkdir(parents=True)
    (repo / "scripts/assignment/sweagent_case_runner.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    (repo / "scripts/assignment/evaluate_swebench_case.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    (repo / "scripts/assignment/sweagent_adaptive_runner.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    (repo / "scripts/assignment/adaptive_runtime.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    (repo / "scripts/assignment/adaptive_event_protocol.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    (repo / "scripts/observability").mkdir(parents=True)
    (repo / "scripts/observability/request_proxy.py").write_bytes((ROOT / "scripts/observability/request_proxy.py").read_bytes())
    (repo / "src/agentic_sim/assignment").mkdir(parents=True)
    (repo / "src/agentic_sim/assignment/event_simulator.py").write_text("# fixture\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("clean\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)
    return repo, git(repo, "rev-parse", "HEAD")


def invoke(repo: Path, work: Path, output: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["python3", str(SCRIPT), "--repo-root", str(repo), "--work-root", str(work), "--hardware", "a100", "--output", str(output), *extra],
        capture_output=True, text=True, check=False,
    )


class RuntimeManifestRendererTests(unittest.TestCase):
    def test_explicit_venv_python_retains_its_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, _ = make_repo(root)
            environment = root / "evaluator-venv"
            venv.EnvBuilder(with_pip=False, symlinks=True).create(environment)
            interpreter = environment / "bin/python"
            result = invoke(repo, root / "work", root / "manifest.json",
                            "--evaluator-python", str(interpreter), "--validation-only")
            self.assertEqual(result.returncode, 0, result.stderr)
            command = json.loads(result.stdout)["evaluator"]["command"][0]
            prefix = subprocess.check_output([command, "-c", "import sys; print(sys.prefix)"], text=True).strip()
            self.assertEqual(Path(prefix), environment)

    def test_validation_only_is_deterministic_and_does_not_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, commit = make_repo(root)
            work = root / "external"
            output = root / "manifest.json"
            first = invoke(repo, work, output, "--validation-only")
            second = invoke(repo, work, output, "--validation-only")
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(first.stdout, second.stdout)
            self.assertFalse(output.exists())
            value = json.loads(first.stdout)
            self.assertEqual(value["repository_root"], str(repo.resolve()))
            self.assertEqual(value["required_commit"], commit)
            self.assertEqual(value["required_branch"], "parallel-h100-shards")
            self.assertEqual(value["hardware"]["compute_capability"], "8.0")
            self.assertEqual(value["hardware"]["gpu_names"], ["NVIDIA A100-SXM4-80GB", "NVIDIA A100-PCIE-80GB"])
            self.assertEqual(value["datasets"]["lite"]["sha256"], RENDERER.DATASET_HASHES["lite"])
            self.assertEqual(value["integrity"]["case_runner_path"], str((repo / "scripts/assignment/sweagent_case_runner.py").resolve()))
            self.assertEqual(value["integrity"]["case_runner_sha256"], hashlib.sha256((repo / "scripts/assignment/sweagent_case_runner.py").read_bytes()).hexdigest())
            self.assertEqual(value["integrity"]["request_proxy_path"], str((repo / "scripts/observability/request_proxy.py").resolve()))
            self.assertEqual(value["integrity"]["request_proxy_sha256"], hashlib.sha256((repo / "scripts/observability/request_proxy.py").read_bytes()).hexdigest())
            self.assertEqual(value["integrity"]["adaptive_runner_path"], str((repo / "scripts/assignment/sweagent_adaptive_runner.py").resolve()))
            self.assertEqual(value["integrity"]["adaptive_runtime_sha256"], hashlib.sha256((repo / "scripts/assignment/adaptive_runtime.py").read_bytes()).hexdigest())
            self.assertEqual(value["integrity"]["event_simulator_path"], str((repo / "src/agentic_sim/assignment/event_simulator.py").resolve()))

    def test_renders_secure_manifest_and_exact_sidecar(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, commit = make_repo(root)
            output = root / "out/manifest.json"
            result = invoke(repo, root / "work", output, "--expected-branch", "parallel-h100-shards")
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = output.read_bytes()
            digest = hashlib.sha256(payload).hexdigest()
            self.assertEqual(output.with_name("manifest.json.sha256").read_text(), f"{digest}  manifest.json\n")
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(output.with_name("manifest.json.sha256").stat().st_mode), 0o600)
            value = json.loads(payload)
            self.assertEqual(value["required_commit"], commit)
            self.assertEqual(value["runner"]["project"], str((root / "work/repos/SWE-agent").resolve()))
            self.assertEqual(value["evaluator"]["project"], str((root / "work/repos/SWE-bench").resolve()))
            self.assertEqual(value["evaluator"]["command"][0], str((root / "work/venv/bin/python").resolve()))
            self.assertIn("{predictions_path}", value["evaluator"]["command"])
            self.assertIn("{evaluator_result}", value["evaluator"]["command"])
            self.assertEqual(value["integrity"]["evaluator_adapter_sha256"], hashlib.sha256((repo / "scripts/assignment/evaluate_swebench_case.py").read_bytes()).hexdigest())

    def test_h100_profile_and_caller_python(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, _ = make_repo(root)
            output = root / "manifest.json"
            interpreter = root / "python/bin/python"
            result = invoke(repo, root / "work", output, "--hardware", "h100", "--evaluator-python", str(interpreter))
            self.assertEqual(result.returncode, 0, result.stderr)
            value = json.loads(output.read_text())
            self.assertEqual(value["hardware"]["compute_capability"], "9.0")
            self.assertEqual(value["evaluator"]["command"][0], str(interpreter.resolve()))

    def test_dirty_repo_wrong_branch_and_overwrite_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, _ = make_repo(root)
            output = root / "manifest.json"
            (repo / "dirty.txt").write_text("do not render\n", encoding="utf-8")
            result = invoke(repo, root / "work", output)
            self.assertEqual(result.returncode, 1)
            self.assertIn("dirty", result.stderr)
            (repo / "dirty.txt").unlink()
            subprocess.run(["git", "-C", str(repo), "checkout", "-qb", "other"], check=True)
            result = invoke(repo, root / "work", output, "--expected-branch", "parallel-h100-shards")
            self.assertEqual(result.returncode, 1)
            self.assertIn("wrong Git branch", result.stderr)
            subprocess.run(["git", "-C", str(repo), "checkout", "-q", "parallel-h100-shards"], check=True)
            self.assertEqual(invoke(repo, root / "work", output).returncode, 0)
            self.assertEqual(invoke(repo, root / "work", output).returncode, 1)
            self.assertIn("overwrite", invoke(repo, root / "work", output).stderr)
            self.assertEqual(invoke(repo, root / "work", output, "--force").returncode, 0)

    def test_template_and_work_root_contracts_are_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, _ = make_repo(root)
            output = root / "manifest.json"
            result = invoke(repo, Path("relative-work"), output)
            self.assertEqual(result.returncode, 1)
            self.assertIn("absolute", result.stderr)
            bad_template = root / "bad.json"
            bad_template.write_text(json.dumps({"schema_version": "wrong"}), encoding="utf-8")
            result = invoke(repo, root / "work", output, "--template", str(bad_template))
            self.assertEqual(result.returncode, 1)
            self.assertIn("unsupported schema", result.stderr)


if __name__ == "__main__":
    unittest.main()
