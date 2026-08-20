import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/cloud/render_studio_manifest.sh"


class RenderStudioManifestTests(unittest.TestCase):
    def run_script(self, *args):
        return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True)

    def test_render_rewrites_only_provider_paths_and_preserves_pins(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            source = root / "source.env"
            output = root / "rendered.env"
            source.write_text(
                "PROJECT_ROOT=/home/ubuntu/project\n"
                "WORK_ROOT=/home/ubuntu/work\n"
                "VLLM_MODEL_REVISION=" + "a" * 40 + "\n"
                "VLLM_API_KEY=local-only-placeholder\n",
                encoding="utf-8",
            )
            result = self.run_script(
                "--source", str(source), "--output", str(output),
                "--studio-root", "/teamspace/studios/this_studio",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            rendered = output.read_text(encoding="utf-8")
            self.assertIn("/teamspace/studios/this_studio/project", rendered)
            self.assertIn("/teamspace/studios/this_studio/work", rendered)
            self.assertNotIn("/home/ubuntu", rendered)
            self.assertIn("VLLM_MODEL_REVISION=" + "a" * 40, rendered)
            self.assertIn("VLLM_API_KEY=local-only-placeholder", rendered)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_existing_output_requires_force(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            source = root / "source.env"
            output = root / "rendered.env"
            source.write_text("WORK_ROOT=/home/ubuntu/work\n", encoding="utf-8")
            output.write_text("immutable\n", encoding="utf-8")
            result = self.run_script("--source", str(source), "--output", str(output))
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(output.read_text(encoding="utf-8"), "immutable\n")

    def test_managed_studio_rewrites_python_contract_and_source_lock_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            env_root = pathlib.Path(sys.prefix)
            source = root / "source.env"
            output = root / "rendered.env"
            source.write_text(
                "PROJECT_ROOT=/home/ubuntu/agentic-workload-simulator\n"
                "PYTHON_VERSION=3.11\n"
                "PYTHON_LOCK_PATH=/home/ubuntu/agentic-work/source/agentic-workload-simulator/cloud/lambda/requirements-linux-x86_64.txt\n"
                "EVALUATOR_PYTHON=/home/ubuntu/agentic-work/venv/bin/python\n"
                "SWE_AGENT_COMMAND=/home/ubuntu/agentic-work/venv/bin/sweagent --help\n"
                "VLLM_API_KEY=local-only-placeholder\n",
                encoding="utf-8",
            )
            result = self.run_script(
                "--source", str(source), "--output", str(output),
                "--studio-root", "/teamspace/studios/this_studio",
                "--python-env-mode", "managed",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            rendered = output.read_text(encoding="utf-8")
            self.assertIn("PYTHON_ENV_MODE=managed", rendered)
            self.assertIn(f"PYTHON_ENV_ROOT={env_root}", rendered)
            self.assertRegex(rendered, r"PYTHON_VERSION_EXACT=\d+\.\d+\.\d+")
            self.assertIn("PYTHON_LOCK_PATH=/teamspace/studios/this_studio/agentic-workload-simulator/cloud/lambda/requirements-linux-x86_64.txt", rendered)
            self.assertIn(f"EVALUATOR_PYTHON={env_root}/bin/python", rendered)
            self.assertIn(f"SWE_AGENT_COMMAND={env_root}/bin/sweagent --help", rendered)
            self.assertNotIn("/agentic-work/venv", rendered)

    def test_venv_mode_keeps_lambda_python_version(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            source = root / "source.env"
            output = root / "rendered.env"
            source.write_text(
                "PYTHON_VERSION=3.11\n"
                "WORK_ROOT=/home/ubuntu/work\n"
                "VLLM_API_KEY=local-only-placeholder\n",
                encoding="utf-8",
            )
            result = self.run_script(
                "--source", str(source), "--output", str(output),
                "--studio-root", "/teamspace/studios/this_studio",
                "--python-env-mode", "venv",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            rendered = output.read_text(encoding="utf-8")
            self.assertIn("PYTHON_ENV_MODE=venv", rendered)
            self.assertIn("PYTHON_ENV_ROOT=/teamspace/studios/this_studio/agentic-work/venv", rendered)
            self.assertIn("PYTHON_VERSION=3.11", rendered)

    def test_dry_run_validates_without_writing_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            source = root / "source.env"
            output = root / "rendered.env"
            source.write_text(
                "WORK_ROOT=/home/ubuntu/work\n"
                "VLLM_API_KEY=local-only-placeholder\n",
                encoding="utf-8",
            )
            result = self.run_script(
                "--source", str(source), "--output", str(output),
                "--studio-root", "/teamspace/studios/this_studio", "--dry-run",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("DRY-RUN", result.stdout)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
