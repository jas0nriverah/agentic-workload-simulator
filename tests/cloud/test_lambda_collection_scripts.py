import pathlib
import subprocess
import tempfile
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

    def test_collection_and_local_verification_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            for name, payload in {
                "manifest.json": '{"run_id":"r1"}\n',
                "predictions.json": '{"instance_id":"i1"}\n',
                "trajectory.json": '{"steps":[]}\n',
                "events.jsonl": '{"event_type":"test"}\n',
                "evaluation.json": '{"resolved":false}\n',
                "status.json": '{"status":"completed"}\n',
                "counters.unavailable.json": '{"status":"unavailable","provenance":"unavailable","reason":"fixture"}\n',
            }.items():
                (source / name).write_text(payload, encoding="utf-8")
            collected = subprocess.run(
                [
                    str(SCRIPTS / "lambda_collect_results.sh"),
                    "--source-root", str(source),
                    "--output-dir", str(output),
                    "--run-id", "fixture",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(collected.returncode, 0, collected.stderr + collected.stdout)
            archive = output / "lambda-results-fixture.tar.gz"
            checksum = output / "lambda-results-fixture.sha256"
            self.assertTrue(archive.is_file())
            self.assertTrue(checksum.is_file())
            verified = subprocess.run(
                [str(SCRIPTS / "verify_lambda_archive_local.sh"), "--archive", str(archive)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(verified.returncode, 0, verified.stderr + verified.stdout)

    def test_collection_rejects_missing_counter_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            for name, payload in {
                "manifest.json": '{"run_id":"r1"}\n',
                "predictions.json": '{"instance_id":"i1"}\n',
                "trajectory.json": '{"steps":[]}\n',
                "events.jsonl": '{"event_type":"test"}\n',
                "evaluation.json": '{"resolved":false}\n',
                "status.json": '{"status":"completed"}\n',
            }.items():
                (source / name).write_text(payload, encoding="utf-8")
            result = subprocess.run(
                [str(SCRIPTS / "lambda_collect_results.sh"), "--source-root", str(source), "--output-dir", str(output), "--run-id", "missing-counter"],
                capture_output=True, text=True, check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("counter state", result.stderr)
