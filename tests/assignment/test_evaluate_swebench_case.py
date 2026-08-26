import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/assignment/evaluate_swebench_case.py"


class OfficialEvaluatorAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="assignment-evaluator-")
        self.root = Path(self.temp.name)
        self.instance = "owner__repo-1"
        self.dataset = self.root / "dataset.jsonl"
        self.dataset.write_text(json.dumps({"instance_id": self.instance, "repo": "owner/repo", "patch": ""}) + "\n", encoding="utf-8")
        self.predictions = self.root / "predictions.json"
        self.predictions.write_text(json.dumps([{"instance_id": self.instance, "model_name_or_path": "model/pinned", "model_patch": "diff --git a/x b/x\n"}]) + "\n", encoding="utf-8")
        self.module_root = self.root / "modules"
        (self.module_root / "swebench/harness").mkdir(parents=True)
        for init in (self.module_root / "swebench/__init__.py", self.module_root / "swebench/harness/__init__.py"):
            init.write_text("", encoding="utf-8")
        (self.module_root / "swebench/harness/run_evaluation.py").write_text(
            """import json, os, pathlib, sys, time
args = sys.argv[1:]
if os.environ.get('FAKE_MODE') == 'timeout':
    time.sleep(30)
if os.environ.get('FAKE_MODE') == 'nonzero':
    raise SystemExit(9)
report_dir = pathlib.Path(args[args.index('--report_dir') + 1])
run_id = args[args.index('--run_id') + 1]
instance_id = args[args.index('--instance_ids') + 1]
mode = os.environ.get('FAKE_MODE', 'resolved')
report = {
  'total_instances': 1, 'submitted_instances': 1, 'completed_instances': 1,
  'resolved_instances': 1 if mode == 'resolved' else 0,
  'unresolved_instances': 1 if mode == 'unresolved' else 0,
  'error_ids': [],
  'resolved_ids': [instance_id] if mode == 'resolved' else [],
  'unresolved_ids': [instance_id] if mode == 'unresolved' else [],
}
if mode == 'missing':
    raise SystemExit(0)
if mode == 'rename-stale':
    os.replace(report_dir / 'stale-report.json', report_dir / ('model.' + run_id + '.json'))
    raise SystemExit(0)
if mode == 'malformed':
    report_dir.joinpath('model.' + run_id + '.json').write_text('{bad', encoding='utf-8')
elif mode == 'wrong-id':
    report['resolved_ids'] = ['other__repo-2']
    report_dir.joinpath('model.' + run_id + '.json').write_text(json.dumps(report), encoding='utf-8')
else:
    report_dir.joinpath('model.' + run_id + '.json').write_text(json.dumps(report), encoding='utf-8')
""",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def run_adapter(self, mode="resolved", timeout=3):
        report = self.root / "report"
        result = self.root / "result.json"
        env = os.environ.copy()
        env["PYTHONPATH"] = str(self.module_root) + os.pathsep + env.get("PYTHONPATH", "")
        env["FAKE_MODE"] = mode
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--dataset", str(self.dataset), "--predictions", str(self.predictions),
             "--instance-id", self.instance, "--report-dir", str(report), "--run-id", "run-1",
             "--result", str(result), "--timeout-seconds", str(timeout)],
            env=env, text=True, capture_output=True, check=False,
        ), result, report

    def test_resolved_and_unresolved_write_canonical_result(self):
        completed, result, report = self.run_adapter("resolved")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        value = json.loads(result.read_text(encoding="utf-8"))
        self.assertTrue(value["official_resolved"])
        self.assertTrue(value["submitted"])
        self.assertEqual(value["counts"]["total_instances"], 1)
        self.assertTrue((report / "evaluator.stdout.log").exists())
        result.unlink()
        shutil.rmtree(report)
        completed, result, _ = self.run_adapter("unresolved")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(json.loads(result.read_text(encoding="utf-8"))["official_resolved"])

    def test_timeout_and_nonzero_fail_without_result(self):
        for mode in ("timeout", "nonzero"):
            completed, result, report = self.run_adapter(mode, timeout=1)
            self.assertNotEqual(completed.returncode, 0)
            self.assertFalse(result.exists())
            self.assertTrue((report / "evaluator.stderr.log").exists())

    def test_missing_malformed_and_wrong_id_reports_fail_closed(self):
        for mode in ("missing", "malformed", "wrong-id"):
            completed, result, _ = self.run_adapter(mode)
            self.assertNotEqual(completed.returncode, 0, mode)
            self.assertFalse(result.exists(), mode)

    def test_stale_report_is_not_reused(self):
        report = self.root / "report"
        report.mkdir()
        (report / "results.json").write_text("{}\n", encoding="utf-8")

        completed, result, _ = self.run_adapter("missing")

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("must be clean", completed.stderr)
        self.assertFalse(result.exists())
        self.assertEqual((report / "results.json").read_text(encoding="utf-8"), "{}\n")

    def test_overwritten_preexisting_report_is_rejected(self):
        report = self.root / "report"
        report.mkdir()
        (report / "model.run-1.json").write_text("stale\n", encoding="utf-8")

        completed, result, _ = self.run_adapter("overwrite")

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("must be clean", completed.stderr)
        self.assertFalse(result.exists())
        self.assertEqual((report / "model.run-1.json").read_text(encoding="utf-8"), "stale\n")

    def test_report_must_be_fresh_and_bound_to_a_new_filesystem_entry(self):
        report = self.root / "report"
        report.mkdir()
        (report / "stale-report.json").write_text("stale\n", encoding="utf-8")

        completed, result, _ = self.run_adapter("rename-stale")

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("newly created", completed.stderr)
        self.assertFalse(result.exists())

    def test_input_validation_rejects_ambiguous_prediction(self):
        self.predictions.write_text(
            json.dumps([{"instance_id": self.instance, "model_name_or_path": "m", "model_patch": "p"},
                        {"instance_id": self.instance, "model_name_or_path": "m", "model_patch": "p"}]),
            encoding="utf-8",
        )
        completed, result, _ = self.run_adapter()
        self.assertNotEqual(completed.returncode, 0)
        self.assertFalse(result.exists())


if __name__ == "__main__":
    unittest.main()
