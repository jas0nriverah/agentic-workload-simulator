import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import ModuleType, SimpleNamespace

import pytest

from agentic_sim.telemetry import cpu_policy as cpu
from scripts.assignment import evaluate_swebench_case as evaluator
from scripts.assignment import render_runtime_manifest as renderer
from scripts.assignment import sweagent_case_runner as runner


ROOT = Path(__file__).resolve().parents[2]


def runtime(tmp_path, worker="12"):
    policy = cpu.policy_config(worker)
    value = {"schema_version": "assignment-runtime-manifest.v1", "runner": {"cpu_policy": policy}}
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(value))
    return path, hashlib.sha256(path.read_bytes()).hexdigest(), policy


def bind(monkeypatch, path, digest):
    monkeypatch.setenv(cpu.RUNTIME_PATH_ENV, str(path))
    monkeypatch.setenv(cpu.RUNTIME_SHA_ENV, digest)


def test_exact_mapping_includes_smt_sharing():
    assert list(cpu.WORKER_CPUS) == [f"{i:02}" for i in (*range(11), *range(12, 23))]
    for i in range(11):
        assert cpu.policy_config(f"{i:02}")["worker_cpuset"] == str(i)
        assert cpu.policy_config(f"{i+12:02}")["worker_cpuset"] == str(i+16)
    assert cpu.cpu_set(cpu.CONTROL_CPUSET).isdisjoint(map(int, cpu.WORKER_CPUS.values()))
    assert cpu.policy_config("00")["physical_cores"] == 16
    for worker in ("11", "23", "0", "worker-00"):
        with pytest.raises(cpu.CPUPolicyError):
            cpu.policy_config(worker)


@pytest.mark.parametrize("key,value", [
    ("worker_cpuset", "1"), ("control_cpuset", "0-31"), ("cpu_quota", 1),
    ("source_sha256", "f" * 64), ("source_path", "/tmp/cpu_policy.py"),
    ("physical_cores", 32), ("extra", True), ("smt_siblings", []),
])
def test_policy_tampering_fails(key, value):
    policy = cpu.policy_config("00")
    policy[key] = value
    with pytest.raises(cpu.CPUPolicyError):
        cpu.validate_policy(policy)


def test_runtime_hash_and_queue_identity_are_required(tmp_path, monkeypatch):
    path, digest, policy = runtime(tmp_path)
    bind(monkeypatch, path, digest)
    assert cpu.from_environment() == policy
    monkeypatch.setenv("ASSIGNMENT_QUEUE_WORKER_ID", "00")
    with pytest.raises(cpu.CPUPolicyError, match="queue worker"):
        cpu.from_environment()
    monkeypatch.delenv("ASSIGNMENT_QUEUE_WORKER_ID")
    path.write_text(path.read_text() + "\n")
    with pytest.raises(cpu.CPUPolicyError, match="runtime SHA-256"):
        cpu.from_environment()
    monkeypatch.delenv(cpu.RUNTIME_SHA_ENV)
    with pytest.raises(cpu.CPUPolicyError, match="incomplete"):
        cpu.from_environment()


@pytest.mark.parametrize("worker", [None, "12", "worker-12"])
def test_queue_worker_authorized_spellings(monkeypatch, worker):
    monkeypatch.delenv("ASSIGNMENT_QUEUE_WORKER_ID", raising=False)
    if worker is not None:
        monkeypatch.setenv("ASSIGNMENT_QUEUE_WORKER_ID", worker)
    assert cpu.validate_policy(cpu.policy_config("12"))["worker_id"] == "12"


@pytest.mark.parametrize("worker", ["", "worker-00", "worker-11", "worker-012", "worker-1",
                                   "Worker-12", " worker-12", "worker-12 ", "other-12"])
def test_queue_worker_other_spellings_rejected(monkeypatch, worker):
    monkeypatch.setenv("ASSIGNMENT_QUEUE_WORKER_ID", worker)
    with pytest.raises(cpu.CPUPolicyError, match="queue worker"):
        cpu.validate_policy(cpu.policy_config("12"))


def test_topology_rejects_different_sibling_pairs(tmp_path):
    (tmp_path / "online").write_text("0-31")
    topology = tmp_path / "cpu0/topology"
    topology.mkdir(parents=True)
    (topology / "thread_siblings_list").write_text("0,1")
    with pytest.raises(cpu.CPUPolicyError, match="SMT topology"):
        cpu.verify_topology(tmp_path)


def test_control_placement_restores_affinity_and_binds_child(tmp_path, monkeypatch):
    path, digest, policy = runtime(tmp_path)
    monkeypatch.setattr(cpu, "verify_topology", lambda: {})
    affinities = {}
    monkeypatch.setattr(os, "sched_getaffinity", lambda tid: affinities.get(tid, {0, 1}))
    monkeypatch.setattr(os, "sched_setaffinity", lambda tid, cpus: affinities.update({tid: cpus}))
    with pytest.raises(RuntimeError, match="fixture"):
        with cpu.runtime_placement(path, digest, active=True):
            assert all(v == cpu.cpu_set(cpu.CONTROL_CPUSET) for v in affinities.values())
            assert cpu.from_environment() == policy
            raise RuntimeError("fixture")
    assert all(v == {0, 1} for v in affinities.values())
    assert cpu.RUNTIME_PATH_ENV not in os.environ


@pytest.mark.parametrize("args", [["--cpus", "1"], ["--cpuset-cpus", "0"],
                                  ["--label", "arbitrary=1"], ["--privileged"]])
def test_worker_rejects_unreviewed_docker_args(args):
    with pytest.raises(cpu.CPUPolicyError):
        cpu.worker_docker_args(cpu.policy_config("12"), args)


@pytest.mark.parametrize("kwargs", [{"cpu_quota": 100000}, {"nano_cpus": 1},
                                    {"cpu_shares": 512}, {"cpuset_cpus": "0"},
                                    {"cgroup_parent": "other"}, {"privileged": True}])
def test_evaluator_rejects_scheduling_overrides(kwargs):
    with pytest.raises(cpu.CPUPolicyError):
        cpu.evaluator_docker_kwargs(cpu.policy_config("12"), kwargs)


@pytest.mark.parametrize("existing", [[], ["--cpuset-cpus", "16"]])
def test_runner_proves_bound_cpuset_and_preserves_owner(tmp_path, monkeypatch, existing):
    path, digest, policy = runtime(tmp_path)
    bind(monkeypatch, path, digest)
    project = tmp_path / "project"
    interpreter = project / ".venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    monkeypatch.setattr(runner, "deadline_from_env", lambda **kw: 1)
    monkeypatch.setattr(runner, "remaining_seconds", lambda _: 20)
    proof = {"docker": True, "empty_args": not existing, "remove_images": False,
             "docker_args": existing}
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **kw:
                        SimpleNamespace(returncode=0, stderr=b"", stdout=(
                            kw["env"][runner.DOCKER_PROOF_MARKER_ENV] + json.dumps(proof) + "\n"
                        ).encode("utf-8")))
    owner = "a" * 32
    command = runner._owned_docker_command(["sweagent", "run-batch"], project, owner,
                                           tmp_path, placement=policy)
    assert existing + json.loads(command[-3]) == ["--cpuset-cpus", "16", "--label",
                                                  f"agentic.assignment.owner={owner}"]
    assert command[-2:] == ["--instances.deployment.remove_container", "false"]
    assert json.loads((tmp_path / "docker_ownership.json").read_text())["runtime_manifest_sha256"] == digest
    monkeypatch.delenv(cpu.RUNTIME_PATH_ENV)
    monkeypatch.delenv(cpu.RUNTIME_SHA_ENV)
    with pytest.raises(runner.CaseRunnerError, match="runtime-bound"):
        runner._owned_docker_command(["sweagent", "run-batch"], project, owner,
                                     tmp_path, placement=policy)


def test_evaluator_wrapper_actual_construction_boundary(tmp_path, monkeypatch):
    path, digest, _ = runtime(tmp_path)
    bind(monkeypatch, path, digest)
    monkeypatch.setenv("ASSIGNMENT_CASE_OWNER", "a" * 32)
    calls = []

    class Collection:
        def create(self, *args, **kwargs):
            calls.append(kwargs)
            host = {key: 0 for key in ("CpuQuota", "CpuPeriod", "NanoCpus", "CpuShares",
                                      "CpuRealtimePeriod", "CpuRealtimeRuntime")}
            host["CpusetCpus"] = kwargs["cpuset_cpus"]
            return SimpleNamespace(reload=lambda: None, attrs={"Id": "fixture", "HostConfig": host})

    for name in ("swebench", "swebench.harness", "swebench.harness.docker_utils",
                 "docker", "docker.models", "docker.models.containers"):
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    sys.modules["docker.models.containers"].ContainerCollection = Collection
    monkeypatch.setattr("runpy.run_module", lambda *a, **kw: None)
    scope = {"__name__": "cpu_wrapper_test"}
    exec(evaluator.compatibility_wrapper_source(), scope)
    Collection().create("fixture", labels={"keep": "yes"})
    assert calls[0]["cpuset_cpus"] == cpu.CONTROL_CPUSET
    assert calls[0]["labels"] == {"keep": "yes", "agentic.assignment.owner": "a" * 32}
    assert calls[0]["auto_remove"] is False
    assert "cpu_quota" not in calls[0]
    with pytest.raises(cpu.CPUPolicyError):
        Collection().create("fixture", nano_cpus=1000000000)
    assert len(calls) == 1


def test_renderer_only_adds_cpu_block_and_preserves_pins(tmp_path):
    template = json.loads((ROOT / "configs/assignment_runtime_manifest.example.json").read_text())
    kwargs = dict(repo=ROOT, work_root=tmp_path, hardware="h100",
                  evaluator_python=Path(sys.executable), state={"branch": "fixture", "commit": "a" * 40})
    plain = renderer.render(template, **kwargs)
    placed = renderer.render(template, **kwargs, cpu_worker_id="22")
    assert placed["runner"].pop("cpu_policy") == cpu.policy_config("22")
    assert placed == plain
    template["runner"]["cpu_policy"] = cpu.policy_config("22")
    with pytest.raises(renderer.RenderError, match="refusing to drop"):
        renderer.render(template, **kwargs)


def test_confirmation_cannot_execute_without_cpu_policy(tmp_path, monkeypatch):
    case = tmp_path / "case.json"
    case.write_text("{}")
    monkeypatch.setattr(runner, "_case_from_args", lambda *a: {
        "schema_version": runner.CONFIRMATION_CASE_SCHEMA,
    })
    args = SimpleNamespace(case_spec=case, output_dir=tmp_path, execute=True)
    with pytest.raises(runner.CaseRunnerError, match="confirmation requires"):
        runner._execute_bound(args, tmp_path / "runtime.json", "a" * 64, {"runner": {}})


def test_cpu_binding_survives_evaluator_environment_sanitization(tmp_path, monkeypatch):
    path, digest, _ = runtime(tmp_path)
    env = {cpu.RUNTIME_PATH_ENV: str(path), cpu.RUNTIME_SHA_ENV: digest,
           "ASSIGNMENT_TELEMETRY_V2_AUTO": "1", "ASSIGNMENT_CASE_OWNER": "a" * 32}
    cleaned = runner._without_v2_activation(env)
    assert cleaned == {key: value for key, value in env.items()
                       if key != "ASSIGNMENT_TELEMETRY_V2_AUTO"}


@pytest.mark.parametrize("existing", [[], ["--cpuset-cpus", "16"]])
def test_installed_sweagent_parser_keeps_final_cpuset_and_owner(tmp_path, monkeypatch, existing):
    project_path = os.environ.get("ASSIGNMENT_CPU_POLICY_PINNED_PROJECT")
    if project_path is None:
        pytest.skip("opt in to read-only configuration parsing with the pinned SWE-agent")
    project = Path(project_path)
    path, digest, policy = runtime(tmp_path)
    bind(monkeypatch, path, digest)
    monkeypatch.setenv("ASSIGNMENT_CASE_DEADLINE_MONOTONIC_NS",
                       str(time.monotonic_ns() + 60_000_000_000))
    instances = tmp_path / "synthetic-instances.json"
    instances.write_text("[]")
    command = runner.build_command(
        project=project, config_path=project / "config/default.yaml",
        request_config_path=ROOT / "cloud/lambda/sweagent_request.yaml",
        instances_path=instances, model="openai/Qwen/Qwen3-Coder-30B-A3B-Instruct",
        model_revision="b2cff646eb4bb1d68355c01b18ae02e7cf42d120", api_key="EMPTY",
        instance_id=None, output_dir=tmp_path,
        extra_args=["--instances.deployment.docker_args", json.dumps(existing)] if existing else [],
    )
    final = runner._owned_docker_command(command, project, "a" * 32, tmp_path, placement=policy)
    code = """import contextlib, io, json, sys
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    from sweagent.run.common import BasicCLI
    from sweagent.run.run_batch import RunBatchConfig
    config = BasicCLI(RunBatchConfig).get_config(sys.argv[1:])
    deployment = config.instances.deployment
print('CPU_POLICY_TEST_PROOF:' + json.dumps({'args': deployment.docker_args, 'remove_container': deployment.remove_container,
                  'remove_images': deployment.remove_images}))
"""
    parsed = subprocess.run(
        [str(project / ".venv/bin/python"), "-c", code, *final[final.index("run-batch") + 1:]],
        cwd=project, env={**runner._without_v2_activation(os.environ), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, timeout=20, check=True,
    )
    proofs = [line.removeprefix("CPU_POLICY_TEST_PROOF:") for line in parsed.stdout.splitlines()
              if line.startswith("CPU_POLICY_TEST_PROOF:")]
    assert len(proofs) == 1
    assert json.loads(proofs[0]) == {
        "args": ["--cpuset-cpus", "16", "--label", "agentic.assignment.owner=" + "a" * 32],
        "remove_container": False, "remove_images": False,
    }
