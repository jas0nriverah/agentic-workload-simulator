import pathlib
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts" / "cloud"


class LambdaCollectionScriptTests(unittest.TestCase):
    def test_required_scripts_exist(self):
        for name in (
            "lambda_collect_results.sh",
            "verify_lambda_archive_local.sh",
            "lambda_stop_workloads.sh",
        ):
            self.assertTrue((SCRIPTS / name).is_file(), name)

    def test_shell_syntax(self):
        for path in SCRIPTS.glob("*.sh"):
            result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, f"{path}: {result.stderr}")

    def test_stop_dry_run_does_not_terminate_provider(self):
        result = subprocess.run(
            [str(SCRIPTS / "lambda_stop_workloads.sh"), "--dry-run"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("terminate it separately", result.stdout)

    def test_collection_has_no_provider_api_or_delete_commands(self):
        text = "\n".join(
            (SCRIPTS / name).read_text(encoding="utf-8")
            for name in (
                "lambda_collect_results.sh",
                "verify_lambda_archive_local.sh",
                "lambda_stop_workloads.sh",
            )
        )
        self.assertNotIn("lambda cloud api", text.lower())
        self.assertNotIn("aws terminate", text.lower())
        self.assertNotIn("terraform destroy", text.lower())
