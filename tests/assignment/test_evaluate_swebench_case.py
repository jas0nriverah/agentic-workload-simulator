import importlib.util
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
SPEC = importlib.util.spec_from_file_location("assignment_evaluate_swebench_case", SCRIPT)
assert SPEC and SPEC.loader
ADAPTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ADAPTER)


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

    def test_native_sweagent_mapping_predictions_are_accepted(self):
        self.predictions.write_text(
            json.dumps({self.instance: {
                "instance_id": self.instance,
                "model_name_or_path": "model/pinned",
                "model_patch": "diff --git a/x b/x\n",
            }}) + "\n",
            encoding="utf-8",
        )
        completed, result, _ = self.run_adapter("resolved")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(json.loads(result.read_text(encoding="utf-8"))["submitted"])

    def assert_empty_patch_unresolved(self, completed, result, report):
        # ``nonzero`` makes the fake harness exit 9, so a returncode of 0
        # proves the harness was never launched for the empty patch.
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse((report / "evaluator.stdout.log").exists())
        value = json.loads(result.read_text(encoding="utf-8"))
        self.assertFalse(value["official_resolved"])
        self.assertTrue(value["submitted"])
        self.assertEqual(value["counts"], {
            "total_instances": 1, "submitted_instances": 1, "completed_instances": 1,
            "resolved_instances": 0, "unresolved_instances": 1, "error_instances": 0,
        })
        # Every provenance field the case runner's verifier requires.
        self.assertLessEqual({
            "schema_version", "official_resolved", "submitted", "instance_id", "run_id",
            "report_path", "report_sha256", "dataset_path", "dataset_sha256",
            "predictions_path", "predictions_sha256", "evaluator_dataset_sha256",
            "evaluator_predictions_sha256", "command_sha256", "counts", "evaluator_python",
            "timeout_seconds",
        }, set(value))
        report_path = Path(value["report_path"])
        self.assertEqual(report_path, report / "model.run-1.json")
        self.assertTrue(report_path.is_file() and not report_path.is_symlink())
        official = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(official["unresolved_ids"], [self.instance])
        self.assertEqual(official["resolved_ids"], [])
        self.assertEqual(official["error_ids"], [])
        for field in ("instance_ids", "submitted_ids", "completed_ids"):
            self.assertEqual(official[field], [self.instance])
        normalized = json.loads((report / "evaluator_predictions.json").read_text(encoding="utf-8"))
        self.assertEqual(normalized[0]["model_patch"], "")

    def test_empty_patch_is_scored_unresolved_without_the_harness(self):
        self.predictions.write_text(
            json.dumps([{
                "instance_id": self.instance,
                "model_name_or_path": "model/pinned",
                "model_patch": "",
            }]) + "\n",
            encoding="utf-8",
        )
        self.assert_empty_patch_unresolved(*self.run_adapter("nonzero"))

    def test_null_patch_in_mapping_predictions_is_scored_unresolved(self):
        self.predictions.write_text(
            json.dumps({self.instance: {
                "instance_id": self.instance,
                "model_name_or_path": "attempt-001",
                "model_patch": None,
            }}) + "\n",
            encoding="utf-8",
        )
        self.assert_empty_patch_unresolved(*self.run_adapter("nonzero"))

    def test_empty_model_name_is_still_rejected(self):
        self.predictions.write_text(
            json.dumps([{"instance_id": self.instance, "model_name_or_path": "", "model_patch": ""}]) + "\n",
            encoding="utf-8",
        )
        completed, result, _ = self.run_adapter()
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("model_name_or_path", completed.stderr)
        self.assertFalse(result.exists())

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

    def test_owner_wrapper_labels_created_container_and_disables_cleanup(self):
        """The compatibility wrapper is exercised with fake Docker objects only."""

        docker_models = self.module_root / "docker/models"
        docker_models.mkdir(parents=True)
        (self.module_root / "docker/__init__.py").write_text("", encoding="utf-8")
        (self.module_root / "docker/models/__init__.py").write_text("", encoding="utf-8")
        (docker_models / "containers.py").write_text(
            """class ContainerCollection:
    last_kwargs = None

    def create(self, *args, **kwargs):
        type(self).last_kwargs = {"args": list(args), "kwargs": kwargs}
        return {"created": True}
""",
            encoding="utf-8",
        )
        # Import a module that eagerly binds the original cleanup functions.
        # The wrapper must replace these aliases by identity, not only patch
        # docker_utils after the harness has imported them.
        (self.module_root / "swebench/__init__.py").write_text(
            "from .harness import docker_build\n",
            encoding="utf-8",
        )
        (self.module_root / "swebench/harness/docker_utils.py").write_text(
            "def cleanup_container(*args, **kwargs):\n    raise AssertionError('original cleanup was called')\n"
            "def clean_images(*args, **kwargs):\n    raise AssertionError('original image cleanup was called')\n"
            "def remove_image(*args, **kwargs):\n    raise AssertionError('original image removal was called')\n",
            encoding="utf-8",
        )
        (self.module_root / "swebench/harness/docker_build.py").write_text(
            "from . import docker_utils\n"
            "cleanup_container = docker_utils.cleanup_container\n"
            "clean_images = docker_utils.clean_images\n"
            "remove_image = docker_utils.remove_image\n",
            encoding="utf-8",
        )
        (self.module_root / "swebench/harness/run_evaluation.py").write_text(
            """import json, os, pathlib, sys
from docker.models.containers import ContainerCollection
from swebench.harness import docker_build, docker_utils

owner = os.environ['ASSIGNMENT_CASE_OWNER']
report_dir = pathlib.Path(sys.argv[sys.argv.index('--report_dir') + 1])
created = ContainerCollection().create(
    'fixture-image', labels={'fixture': 'kept'}, auto_remove=True,
)

class API:
    def inspect_container(self, container_id):
        return {
            'Id': container_id,
            'Name': 'fixture-container',
            'Image': 'fixture-image@sha256:' + 'd' * 64,
            'Created': '2026-09-07T00:00:00Z',
            'Config': {'Labels': {
                'agentic.assignment.owner': owner,
                'fixture-secret': 'must-not-be-retained',
            }},
            'State': {
                'Status': 'running', 'Running': True,
                'StartedAt': '2026-09-07T00:00:01Z', 'Pid': 1234,
            },
        }

class Client:
    api = API()

class Container:
    id = 'b' * 64
    stop_calls = []

    def logs(self):
        return b'retained container logs\\n'

    def stop(self, *, timeout):
        type(self).stop_calls.append(timeout)

class Logger:
    errors = []

    def error(self, message, *args):
        type(self).errors.append(message % args if args else message)

logger = Logger()
docker_build.cleanup_container(Client(), Container(), logger)
clean_result = docker_build.clean_images('shared-image')
remove_result = docker_build.remove_image('shared-image')
(report_dir / 'wrapper-observed.json').write_text(json.dumps({
    'created': created,
    'create_kwargs': ContainerCollection.last_kwargs,
    'cleanup_name': docker_build.cleanup_container.__name__,
    'clean_images_name': docker_build.clean_images.__name__,
    'remove_image_name': docker_build.remove_image.__name__,
    'direct_cleanup_name': docker_utils.cleanup_container.__name__,
    'direct_clean_images_name': docker_utils.clean_images.__name__,
    'direct_remove_image_name': docker_utils.remove_image.__name__,
    'clean_result': clean_result,
    'remove_result': remove_result,
    'stop_calls': Container.stop_calls,
    'errors': Logger.errors,
}, sort_keys=True), encoding='utf-8')
""",
            encoding="utf-8",
        )
        wrapper = self.root / "compat-wrapper.py"
        wrapper.write_text(ADAPTER.PODMAN_COMPAT_WRAPPER, encoding="utf-8")
        report = self.root / "wrapper-report"
        owner = "a" * 32
        env = os.environ.copy()
        env["PYTHONPATH"] = str(self.module_root)
        env["ASSIGNMENT_CASE_OWNER"] = owner
        completed = subprocess.run(
            [sys.executable, str(wrapper), "--report_dir", str(report)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        observed = json.loads((report / "wrapper-observed.json").read_text(encoding="utf-8"))
        self.assertEqual(observed["create_kwargs"]["kwargs"]["labels"], {
            "fixture": "kept",
            "agentic.assignment.owner": owner,
        })
        self.assertFalse(observed["create_kwargs"]["kwargs"]["auto_remove"])
        self.assertEqual(observed["cleanup_name"], "retain_container")
        self.assertEqual(observed["clean_images_name"], "retain_images")
        self.assertEqual(observed["remove_image_name"], "retain_images")
        self.assertEqual(observed["direct_cleanup_name"], "retain_container")
        self.assertEqual(observed["direct_clean_images_name"], "retain_images")
        self.assertEqual(observed["direct_remove_image_name"], "retain_images")
        self.assertIsNone(observed["clean_result"])
        self.assertIsNone(observed["remove_result"])
        self.assertEqual(observed["stop_calls"], [15])
        self.assertEqual(observed["errors"], [])
        evidence = report / "docker_cleanup"
        self.assertEqual(len(list(evidence.glob("*.inspect.json"))), 1)
        metadata = json.loads(next(evidence.glob("*.inspect.json")).read_text(encoding="utf-8"))
        self.assertEqual(metadata["Config"]["Labels"], {"agentic.assignment.owner": owner})
        self.assertNotIn("fixture-secret", json.dumps(metadata))
        self.assertEqual(next(evidence.glob("*.logs")).read_bytes(), b"retained container logs\n")

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
