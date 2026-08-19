import pathlib
import subprocess
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


if __name__ == "__main__":
    unittest.main()
