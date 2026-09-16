"""Synthetic package fixtures only; no real SWE-bench evaluator or dataset runs."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agentic_sim.telemetry import cpu_policy as cpu
from scripts.assignment import evaluate_swebench_case as evaluator
from scripts.assignment import render_runtime_manifest as renderer
from scripts.assignment import sweagent_case_runner as runner


ROOT = Path(__file__).resolve().parents[2]


def git(project, *args):
    return subprocess.check_output(["git", "-C", str(project), *args], text=True).strip()


def source_fixture(tmp_path):
    project = tmp_path / "sealed-evaluator"
    harness = project / "swebench/harness"
    harness.mkdir(parents=True)
    (project / "swebench/__init__.py").write_text("")
    (harness / "__init__.py").write_text("")
    (harness / "docker_utils.py").write_text("# fixture\n")
    (harness / "run_evaluation.py").write_text('''import json, pathlib, sys
args = sys.argv
root = pathlib.Path(args[args.index('--report_dir') + 1])
run = args[args.index('--run_id') + 1]
iid = args[args.index('--instance_ids') + 1]
(root / ('model.' + run + '.json')).write_text(json.dumps({
    'total_instances': 1, 'submitted_instances': 1, 'completed_instances': 1,
    'resolved_instances': 0, 'unresolved_instances': 1, 'error_ids': [],
    'resolved_ids': [], 'unresolved_ids': [iid],
}))
''')
    git(project, "init", "-q")
    git(project, "-c", "user.email=fixture@example.invalid", "-c", "user.name=Fixture", "add", ".")
    git(project, "-c", "user.email=fixture@example.invalid", "-c", "user.name=Fixture", "commit", "-qm", "fixture")
    return project, git(project, "rev-parse", "HEAD")


def test_clean_project_environment_and_dirty_revision_rejection(tmp_path):
    project, revision = source_fixture(tmp_path)
    args = SimpleNamespace(evaluator_project=project, evaluator_revision=revision)
    env, binding = evaluator.evaluator_source_environment(args, {"PYTHONPATH": "/old/project"})
    assert env["PYTHONPATH"] == str(project) + os.pathsep + "/old/project"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert binding["project_path"] == str(project)
    assert binding["revision"] == revision
    args.evaluator_revision = "f" * 40
    with pytest.raises(evaluator.EvaluatorError, match="revision differs"):
        evaluator.evaluator_source_environment(args, {})
    args.evaluator_revision = revision
    (project / "swebench/__init__.py").write_text("# changed\n")
    with pytest.raises(evaluator.EvaluatorError, match="must be clean"):
        evaluator.evaluator_source_environment(args, {})


def test_owned_evaluator_requires_explicit_source_pair():
    with pytest.raises(evaluator.EvaluatorError, match="owned evaluator requires"):
        evaluator.evaluator_source_environment(SimpleNamespace(), {"ASSIGNMENT_CASE_OWNER": "a" * 32})


def test_case_runner_binding_is_idempotent_and_rejects_mismatches():
    command = ["python", "adapter.py"]
    bound = runner._bind_evaluator_project_command(command, Path("/reviewed"), "a" * 40)
    assert runner._bind_evaluator_project_command(bound, Path("/reviewed"), "a" * 40) == bound
    for unsafe in (bound + ["--evaluator-project", "/reviewed"],
                   command + ["--evaluator-project", "/other"],
                   command + ["--evaluator-project=/reviewed"]):
        with pytest.raises(runner.CaseRunnerError):
            runner._bind_evaluator_project_command(unsafe, Path("/reviewed"), "a" * 40)


def test_child_imports_exact_checkout_despite_old_pythonpath_and_report_shadow(tmp_path):
    project, revision = source_fixture(tmp_path)
    dependencies = tmp_path / "older-installed-project"
    (dependencies / "swebench").mkdir(parents=True)
    (dependencies / "swebench/__init__.py").write_text("raise RuntimeError('wrong installed checkout')")
    (dependencies / "docker/models").mkdir(parents=True)
    for path in ("docker/__init__.py", "docker/models/__init__.py"):
        (dependencies / path).write_text("")
    (dependencies / "docker/models/containers.py").write_text("class ContainerCollection:\n    def create(self, *a, **kw): pass\n")
    report = tmp_path / "report"
    (report / "swebench").mkdir(parents=True)
    (report / "swebench/__init__.py").write_text("raise RuntimeError('wrong cwd checkout')")
    dataset, predictions = tmp_path / "synthetic.jsonl", tmp_path / "predictions.json"
    dataset.write_text(json.dumps({"instance_id": "fixture__fixture-1"}) + "\n")
    predictions.write_text(json.dumps([{"instance_id": "fixture__fixture-1", "model_name_or_path": "fixture", "model_patch": "fixture"}]))
    result = tmp_path / "result.json"
    env = {**os.environ, "PYTHONPATH": str(dependencies)}
    for key in (cpu.RUNTIME_PATH_ENV, cpu.RUNTIME_SHA_ENV, "ASSIGNMENT_CASE_OWNER"):
        env.pop(key, None)
    completed = subprocess.run([
        sys.executable, str(ROOT / "scripts/assignment/evaluate_swebench_case.py"),
        "--dataset", str(dataset), "--predictions", str(predictions),
        "--instance-id", "fixture__fixture-1", "--run-id", "source-fixture",
        "--report-dir", str(report), "--result", str(result),
        "--evaluator-project", str(project), "--evaluator-revision", revision,
    ], env=env, capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stderr
    value = json.loads(result.read_text())
    assert value["evaluator_project_path"] == str(project)
    assert value["evaluator_project_revision"] == revision
    for key in ("evaluator_source_binding", "evaluator_import_binding"):
        path = Path(value[key + "_path"])
        assert hashlib.sha256(path.read_bytes()).hexdigest() == value[key + "_sha256"]
    imports = json.loads(Path(value["evaluator_import_binding_path"]).read_text())
    assert imports["imported_modules"]
    assert all(Path(row["path"]).is_relative_to(project) for row in imports["imported_modules"])


def test_wrapper_rejects_source_changed_after_validation(tmp_path):
    project, revision = source_fixture(tmp_path)
    _, binding = evaluator.evaluator_source_environment(
        SimpleNamespace(evaluator_project=project, evaluator_revision=revision), {},
    )
    (project / "swebench/__init__.py").write_text("# changed after validation\n")
    with pytest.raises(RuntimeError, match="changed before child import"):
        exec(evaluator.compatibility_wrapper_source(binding), {"__name__": "test_wrapper"})


def test_cpu_policy_binds_the_sealed_copy_and_rejects_worktree_path(tmp_path):
    source = tmp_path / "sealed_cpu_policy.py"
    source.write_bytes(cpu.SOURCE.read_bytes())
    spec = importlib.util.spec_from_file_location("sealed_cpu_policy", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    block = module.policy_config("00")
    assert block["source_path"] == str(source)
    assert module.validate_policy(block) == block
    with pytest.raises(module.CPUPolicyError, match="source SHA-256"):
        module.validate_policy(cpu.policy_config("00"))
    source.write_text(source.read_text() + "\n")
    with pytest.raises(module.CPUPolicyError, match="source SHA-256"):
        module.validate_policy(block)


def test_renderer_dataset_jsonl_hashes_retain_original_parquet_provenance(tmp_path):
    template = json.loads((ROOT / "configs/assignment_runtime_manifest.example.json").read_text())
    value = renderer.render(template, repo=ROOT, work_root=tmp_path, hardware="h100",
                            evaluator_python=Path(sys.executable),
                            state={"branch": "fixture", "commit": "a" * 40}, cpu_worker_id="00")
    for suite in ("lite", "verified"):
        assert value["datasets"][suite]["sha256"] == renderer.DATASET_HASHES[suite]
        assert value["datasets"][suite]["source_parquet_sha256"] == renderer.DATASET_SOURCE_PARQUET_HASHES[suite]
        assert value["datasets"][suite]["revision"] == template["datasets"][suite]["revision"]
    # Schema validation binds a descriptor without reading/probing remote hardware.
    value["runner"]["telemetry"]["remote_hardware_profile"]["sha256"] = "1" * 64
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(value))
    assert runner.load_manifest(path)["datasets"] == value["datasets"]
    value["datasets"]["lite"]["source_parquet_sha256"] = "wrong"
    path.write_text(json.dumps(value))
    with pytest.raises(runner.CaseRunnerError, match="source_parquet_sha256"):
        runner.load_manifest(path)
