import hashlib
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/cloud/lambda_run_gold_smoke.sh"
LITE_ID = "astropy__astropy-14182"
VERIFIED_ID = "astropy__astropy-14365"
LITE_IMAGE = "swebench/sweb.eval.x86_64.astropy_1776_astropy-14182:latest"
VERIFIED_IMAGE = "swebench/sweb.eval.x86_64.astropy_1776_astropy-14365:latest"
LITE_DIGEST = "sha256:1caa6363958e49791e9dc4c838fbfd8e8e134b7992e20e90def10072cb920c25"
VERIFIED_DIGEST = "sha256:ac22529003ab4df5a84eb0e6be4b269b691c0f3b4aca582161bdfb581e1e9305"
SWE_BENCH_REVISION = "726c5461e2ef52d83cf1ea2107870a8bb3328d57"


def _row(instance_id, image):
    return {
        "instance_id": instance_id,
        "patch": "diff --git a/x b/x\n",
        "repo": "astropy/astropy",
        "version": "6.0",
        "base_commit": "0" * 40,
        "test_patch": "",
        "FAIL_TO_PASS": [],
        "PASS_TO_PASS": [],
        "image": image,
    }


class OfficialEvaluatorContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="official-evaluator-")
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.lite = self.data / "lite.json"
        self.verified = self.data / "verified.json"
        lite_row = _row(LITE_ID, LITE_IMAGE)
        verified_row = _row(VERIFIED_ID, VERIFIED_IMAGE)
        lite_canonical = json.dumps([lite_row], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        verified_canonical = json.dumps([verified_row], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        self.lite_selected_hash = hashlib.sha256(lite_canonical).hexdigest()
        self.verified_selected_hash = hashlib.sha256(verified_canonical).hexdigest()
        self.lite.write_bytes(lite_canonical + b"\n")
        self.verified.write_bytes(verified_canonical + b"\n")
        self.evaluator = self.root / "evaluator"
        (self.evaluator / "swebench/harness").mkdir(parents=True)
        (self.evaluator / "swebench/harness/run_evaluation.py").write_text("# fixture\n", encoding="utf-8")
        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir()
        self.docker = self.fake_bin / "docker"
        self.docker.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "args = ' '.join(sys.argv)\n"
            "if '{{.Os}}/{{.Architecture}}' in args:\n"
            "    print('linux/amd64')\n"
            "elif '14182' in args:\n"
            f"    print({LITE_IMAGE.split(':', 1)[0]!r} + '@' + {LITE_DIGEST!r})\n"
            "else:\n"
            f"    print({VERIFIED_IMAGE.split(':', 1)[0]!r} + '@' + {VERIFIED_DIGEST!r})\n",
            encoding="utf-8",
        )
        self.fake_evaluator = self.root / "fake-evaluator"
        self.fake_evaluator.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, sys\n"
            "run_id = sys.argv[sys.argv.index('--run_id') + 1]\n"
            "status = os.environ.get('MOCK_EVAL_STATUS', 'resolved')\n"
            "if status == 'error':\n"
            "    raise SystemExit(7)\n"
            "report = {'total_instances': 1, 'submitted_instances': 1,\n"
            "          'completed_instances': 1, 'error_ids': []}\n"
            "report['resolved_instances'] = 1 if status == 'resolved' else 0\n"
            "report['unresolved_instances'] = 1 if status == 'unresolved' else 0\n"
            "if os.environ.get('MOCK_REPORT_LAYOUT') == 'unrelated':\n"
            "    pathlib.Path('unrelated.json').write_text(json.dumps(report))\n"
            "elif os.environ.get('MOCK_REPORT_LAYOUT') == 'results':\n"
            "    report_dir = pathlib.Path(sys.argv[sys.argv.index('--report_dir') + 1])\n"
            "    report_dir.mkdir(parents=True, exist_ok=True)\n"
            "    (report_dir / 'results.json').write_text(json.dumps(report))\n"
            "else:\n"
            "    pathlib.Path('gold.' + run_id + '.json').write_text(json.dumps(report))\n",
            encoding="utf-8",
        )
        for path in (self.docker, self.fake_evaluator):
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        datasets_manifest = self.root / "datasets.json"
        datasets_manifest.write_text(
            json.dumps(
                {
                    "schema_version": "datasets.v3",
                    "lite": {
                        "repo": "SWE-bench/SWE-bench_Lite",
                        "revision": "69611d31007e1c6731db8bd5b5c3f2d33f5bab6e",
                        "split": "test",
                        "rows": 300,
                        "source_file_sha256": "f46f2e3f003f2552932393da4b223e1e0456a2c71eba8b73ae58f29646c1278b",
                        "source_file": "fixture-lite.parquet",
                        "selected": [{"instance_id": LITE_ID, "path": str(self.lite), "sha256": hashlib.sha256(lite_canonical).hexdigest()}],
                    },
                    "verified": {
                        "repo": "SWE-bench/SWE-bench_Verified",
                        "revision": "91aa3ed51b709be6457e12d00300a6a596d4c6a3",
                        "split": "test",
                        "rows": 500,
                        "source_file_sha256": "43ed5a3d1d98da36472c1ade65ddd2085d7b4ff694fcaf6a023a07c5c1f32f21",
                        "source_file": "fixture-verified.parquet",
                        "selected": [{"instance_id": VERIFIED_ID, "path": str(self.verified), "sha256": hashlib.sha256(verified_canonical).hexdigest()}],
                    },
                }
            ),
            encoding="utf-8",
        )
        self.manifest = self.root / "manifest.env"
        self.manifest.write_text(
            "\n".join(
                [
                    "SWE_BENCH_REVISION=" + SWE_BENCH_REVISION,
                    "LITE_DATASET_REPO=SWE-bench/SWE-bench_Lite",
                    "LITE_DATASET_REVISION=69611d31007e1c6731db8bd5b5c3f2d33f5bab6e",
                    "LITE_DATASET_SHA256=f46f2e3f003f2552932393da4b223e1e0456a2c71eba8b73ae58f29646c1278b",
                    "LITE_GOLD_DATASET_SHA256=" + self.lite_selected_hash,
                    "LITE_DATASET_PATH=" + str(self.lite),
                    "DATASET_MANIFEST_PATH=" + str(datasets_manifest),
                    "VERIFIED_DATASET_REPO=SWE-bench/SWE-bench_Verified",
                    "VERIFIED_DATASET_REVISION=91aa3ed51b709be6457e12d00300a6a596d4c6a3",
                    "VERIFIED_DATASET_SHA256=43ed5a3d1d98da36472c1ade65ddd2085d7b4ff694fcaf6a023a07c5c1f32f21",
                    "VERIFIED_GOLD_DATASET_SHA256=" + self.verified_selected_hash,
                    "VERIFIED_DATASET_PATH=" + str(self.verified),
                    "GOLD_LITE_INSTANCE_ID=" + LITE_ID,
                    "GOLD_VERIFIED_INSTANCE_ID=" + VERIFIED_ID,
                    "EVALUATOR_IMAGE_NAMESPACE=swebench",
                    "EVALUATOR_PLATFORM=linux/amd64",
                    "EVALUATOR_LITE_GOLD_IMAGE=" + LITE_IMAGE,
                    "EVALUATOR_LITE_GOLD_DIGEST=" + LITE_DIGEST,
                    "EVALUATOR_VERIFIED_GOLD_IMAGE=" + VERIFIED_IMAGE,
                    "EVALUATOR_VERIFIED_GOLD_DIGEST=" + VERIFIED_DIGEST,
                    "SWE_BENCH_EVALUATOR_ROOT=" + str(self.evaluator),
                    "EVALUATOR_PYTHON=" + str(self.fake_evaluator),
                    "GOLD_OUTPUT_ROOT=" + str(self.root / "out"),
                    "GENERATED_OUTPUT_ROOT=" + str(self.root / "generated-out"),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def run_script(self, *args, status=None):
        env = os.environ.copy()
        env["PATH"] = str(self.fake_bin) + os.pathsep + env.get("PATH", "")
        if status is not None:
            env["MOCK_EVAL_STATUS"] = status
        return subprocess.run(
            ["bash", str(SCRIPT), "--manifest", str(self.manifest), *args],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )

    def test_dry_run_prints_distinct_official_commands(self):
        result = self.run_script("--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("--predictions_path gold", result.stdout)
        self.assertIn("--instance_ids " + LITE_ID, result.stdout)
        self.assertIn("--instance_ids " + VERIFIED_ID, result.stdout)
        self.assertIn(LITE_IMAGE.split(":", 1)[0] + "@" + LITE_DIGEST, result.stdout)
        self.assertIn(VERIFIED_IMAGE.split(":", 1)[0] + "@" + VERIFIED_DIGEST, result.stdout)
        self.assertIn("no evaluator or Docker command executes", result.stdout)
        self.assertFalse((self.root / "out").exists())

    def test_missing_asset_or_digest_fails_closed(self):
        original = self.manifest.read_text(encoding="utf-8")
        self.manifest.write_text(original.replace("LITE_DATASET_SHA256=", "LITE_DATASET_SHA256=bad-"), encoding="utf-8")
        result = self.run_script("--suite", "lite", "--dry-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("reviewed manifest hash", result.stdout)
        self.manifest.write_text(original, encoding="utf-8")
        self.lite.unlink()
        result = self.run_script("--suite", "lite", "--dry-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not exist", result.stdout)
        self.lite.write_bytes(json.dumps([_row(LITE_ID, LITE_IMAGE)], sort_keys=True, separators=(",", ":")).encode() + b"\n")
        self.manifest.write_text(original.replace("LITE_GOLD_DATASET_SHA256=" + self.lite_selected_hash, "LITE_GOLD_DATASET_SHA256=" + "0" * 64), encoding="utf-8")
        result = self.run_script("--suite", "lite", "--dry-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("selected-row hash", result.stdout)
        self.manifest.write_text(original, encoding="utf-8")

    def test_lite_and_verified_ids_cannot_alias(self):
        content = self.manifest.read_text(encoding="utf-8")
        self.manifest.write_text(content.replace("GOLD_VERIFIED_INSTANCE_ID=" + VERIFIED_ID, "GOLD_VERIFIED_INSTANCE_ID=" + LITE_ID), encoding="utf-8")
        result = self.run_script("--dry-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(
            "must be distinct" in result.stdout
            or "GOLD_VERIFIED_INSTANCE_ID must be" in result.stdout
        )

    def test_mock_resolved_unresolved_and_error_statuses(self):
        resolved = self.run_script("--suite", "lite", status="resolved")
        self.assertEqual(resolved.returncode, 0, resolved.stdout)
        status_path = self.root / "out/lite-astropy__astropy-14182/status.json"
        self.assertEqual(json.loads(status_path.read_text())["status"], "resolved")
        run_manifest = self.root / "out/lite-astropy__astropy-14182/run_manifest.json"
        manifest_value = json.loads(run_manifest.read_text())
        self.assertEqual(manifest_value["status"], "resolved")
        self.assertEqual(manifest_value["evaluator_exit_code"], 0)
        self.assertEqual(
            manifest_value["report_path"],
            str(self.root / "out/lite-astropy__astropy-14182/gold.gold-lite-astropy__astropy-14182.json"),
        )

        content = self.manifest.read_text(encoding="utf-8")
        content = content.replace("GOLD_OUTPUT_ROOT=" + str(self.root / "out"), "GOLD_OUTPUT_ROOT=" + str(self.root / "out-unresolved"))
        self.manifest.write_text(content, encoding="utf-8")
        unresolved = self.run_script("--suite", "lite", status="unresolved")
        self.assertEqual(unresolved.returncode, 3, unresolved.stdout)
        status_path = self.root / "out-unresolved/lite-astropy__astropy-14182/status.json"
        self.assertEqual(json.loads(status_path.read_text())["status"], "unresolved")
        self.assertEqual(
            json.loads((self.root / "out-unresolved/lite-astropy__astropy-14182/run_manifest.json").read_text())["status"],
            "unresolved",
        )

        content = content.replace("GOLD_OUTPUT_ROOT=" + str(self.root / "out-unresolved"), "GOLD_OUTPUT_ROOT=" + str(self.root / "out-error"))
        self.manifest.write_text(content, encoding="utf-8")
        error = self.run_script("--suite", "lite", status="error")
        self.assertEqual(error.returncode, 1, error.stdout)
        status_path = self.root / "out-error/lite-astropy__astropy-14182/status.json"
        self.assertEqual(json.loads(status_path.read_text())["status"], "evaluator_error")

    def test_report_dir_results_layout_is_classified(self):
        env = os.environ.copy()
        env["MOCK_REPORT_LAYOUT"] = "results"
        # Use the same fixture command with the layout flag explicitly for
        # this compatibility check.
        env["PATH"] = str(self.fake_bin) + os.pathsep + env.get("PATH", "")
        result = subprocess.run(
            ["bash", str(SCRIPT), "--manifest", str(self.manifest), "--suite", "lite"],
            cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        status_path = self.root / "out/lite-astropy__astropy-14182/status.json"
        self.assertEqual(json.loads(status_path.read_text())["status"], "resolved")

    def test_generated_mode_uses_separate_namespace_and_run_id(self):
        predictions = self.root / "predictions.json"
        predictions.write_text(json.dumps([{
            "instance_id": LITE_ID,
            "model_name_or_path": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
            "model_patch": "",
        }]), encoding="utf-8")
        result = self.run_script(
            "--suite", "lite", "--experiment-type", "generated",
            "--predictions-path", str(predictions), "--dry-run",
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("--run_id generated-lite-" + LITE_ID, result.stdout)
        self.assertIn("ARTIFACT_ROOT[lite]: " + str(self.root / "generated-out"), result.stdout)
        self.assertNotIn("--run_id gold-lite-", result.stdout)
        self.assertNotIn(str(self.root / "out"), result.stdout)

    def test_unrelated_json_cannot_become_evaluator_result(self):
        env = os.environ.copy()
        env["MOCK_REPORT_LAYOUT"] = "unrelated"
        env["PATH"] = str(self.fake_bin) + os.pathsep + env.get("PATH", "")
        result = subprocess.run(
            ["bash", str(SCRIPT), "--manifest", str(self.manifest), "--suite", "lite"],
            cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, check=False,
        )
        self.assertEqual(result.returncode, 1, result.stdout)
        status_path = self.root / "out/lite-astropy__astropy-14182/status.json"
        self.assertEqual(json.loads(status_path.read_text())["status"], "evaluator_error")


if __name__ == "__main__":
    unittest.main()
