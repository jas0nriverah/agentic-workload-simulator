import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from scripts.cloud import a100_diagnostic


ROOT = Path(__file__).resolve().parents[2]


class A100DiagnosticTests(unittest.TestCase):
    def _args(self, output):
        return Namespace(
            config=ROOT / "configs/a100_final_validation.json",
            manifest=None,
            json_out=output,
            deadline_epoch=None,
            min_free_bytes=0,
        )

    def test_checks_are_independent_and_collect_multiple_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "diagnostic.json"
            diagnostic = a100_diagnostic.A100Diagnostic(self._args(output))
            with patch.object(diagnostic, "_branch", side_effect=a100_diagnostic.DiagnosticFailure("wrong branch", "check out required branch")), patch.object(
                diagnostic, "_commit", return_value={"commit": a100_diagnostic.EXPECTED_COMMIT}
            ), patch.object(diagnostic, "_clean_checkout", side_effect=a100_diagnostic.DiagnosticFailure("dirty", "clean checkout")), patch.object(
                diagnostic, "_deadline", return_value={"configured_seconds": 14400}
            ):
                results = diagnostic.run()

            by_id = {result.check_id: result for result in results}
            self.assertEqual(by_id["branch"].status, "fail")
            self.assertEqual(by_id["commit"].status, "pass")
            self.assertEqual(by_id["clean_checkout"].status, "fail")
            self.assertIn("docker_daemon", by_id)
            self.assertIn("deadline", by_id)
            self.assertGreaterEqual(len(results), 20)

    def test_json_report_contains_machine_results_and_remediation(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "diagnostic.json"
            args = self._args(output)
            diagnostic = a100_diagnostic.A100Diagnostic(args)
            diagnostic.check("fixture_failure", "hardware_detected", "attach one A100", lambda: (_ for _ in ()).throw(
                a100_diagnostic.DiagnosticFailure("fixture GPU missing", "attach one A100", {"observed": 0})
            ))
            report = a100_diagnostic._report(args, diagnostic.results)
            a100_diagnostic._write_report(output, report)
            parsed = json.loads(output.read_text(encoding="utf-8"))

            self.assertEqual(parsed["schema_version"], "a100-diagnostic.v1")
            self.assertEqual(parsed["status"], "NOT_READY")
            self.assertEqual(parsed["summary"], {"checks": 1, "passed": 0, "failed": 1})
            self.assertEqual(parsed["checks"][0]["remediation"], "attach one A100")
            self.assertFalse(parsed["canonical_artifacts_created"])
            self.assertFalse(parsed["holdout_accessed"])

    def test_json_report_rejects_manifest_runtime_roots_without_creating_them(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.env"
            artifact_root = root / "artifacts"
            manifest.write_text(f"ARTIFACT_ROOT={artifact_root}\nRECOVERY_ROOT={root / 'recovery'}\n", encoding="utf-8")
            report_path = artifact_root / "diagnostic.json"
            report = {"manifest": str(manifest)}

            with self.assertRaisesRegex(ValueError, "ARTIFACT_ROOT"):
                a100_diagnostic._write_report(report_path, report)
            self.assertFalse(artifact_root.exists())

    def test_model_cache_requires_weight_file_or_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot" / a100_diagnostic.EXPECTED_REVISION
            snapshot.mkdir(parents=True)
            (snapshot / "config.json").write_text("{}\n", encoding="utf-8")
            (snapshot / "tokenizer.json").write_text("{}\n", encoding="utf-8")
            values = {
                "MODEL_CACHE": str(root / "snapshot"),
                "MODEL_SNAPSHOT": str(snapshot),
                "VLLM_MODEL": a100_diagnostic.EXPECTED_MODEL,
                "VLLM_MODEL_REVISION": a100_diagnostic.EXPECTED_REVISION,
            }
            diagnostic = a100_diagnostic.A100Diagnostic(self._args(root / "diagnostic.json"))
            with patch.object(a100_diagnostic, "_manifest", return_value=values):
                with self.assertRaisesRegex(a100_diagnostic.DiagnosticFailure, "no safetensors weights"):
                    diagnostic._model_cache()
                (snapshot / "model.safetensors").write_bytes(b"fixture")
                result = diagnostic._model_cache()
            self.assertEqual(result["weight_files"], ["model.safetensors"])

    def test_cli_has_no_production_side_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "diagnostic.json"
            before = {path: path.stat().st_mtime_ns for path in (ROOT / "artifacts", ROOT / "configs/a100_final_validation.json")}
            with patch.object(a100_diagnostic.subprocess, "run", side_effect=FileNotFoundError("blocked")) as run:
                status = a100_diagnostic.main(["--json-out", str(output)])
            after = {path: path.stat().st_mtime_ns for path in before}

            self.assertEqual(status, 1)
            self.assertTrue(output.is_file())
            self.assertEqual(before, after)
            self.assertFalse(any(call.args[0][0] in {"docker", "nvidia-smi"} and "run" in call.args[0] for call in run.call_args_list))
            parsed = json.loads(output.read_text(encoding="utf-8"))
            self.assertFalse(parsed["production_services_started"])
            self.assertFalse(parsed["measurements_started"])
            self.assertFalse(parsed["holdout_accessed"])

            source = (ROOT / "scripts/cloud/a100_diagnostic.py").read_text(encoding="utf-8")
            self.assertNotIn('"docker", "pull"', source)
            self.assertNotIn('"docker", "run"', source)
            self.assertNotIn('"docker", "start"', source)
            for call in run.call_args_list:
                command = call.args[0]
                self.assertNotEqual(command[:2], ["docker", "pull"])
                self.assertNotEqual(command[:2], ["docker", "run"])


if __name__ == "__main__":
    unittest.main()
