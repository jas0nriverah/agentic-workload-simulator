"""Opt-in, CPU-only Docker smoke; never invokes SWE-bench or model inference.

Run with the installed Docker-SDK interpreter:
  PYTHONPATH=src <evaluator-python> tests/telemetry/test_cpu_policy_docker.py OUTPUT_DIR
The image must already exist. Only this invocation's labelled containers are removed.
"""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import ModuleType
import uuid

from agentic_sim.telemetry import cpu_policy as cpu


ROOT = Path(__file__).resolve().parents[2]
IMAGE = "assignment-persistent-shell-swe-rex-1-4-0:20260909"
OWNER_LABEL = "agentic.assignment.owner"


def snapshot(container, expected):
    container.reload()
    info = container.attrs
    construction = cpu.verify_container(info, expected)
    pid = info["State"]["Pid"]
    membership = Path(f"/proc/{pid}/cgroup").read_text().strip()
    assert membership.startswith("0::/") and "\n" not in membership
    group = Path("/sys/fs/cgroup") / membership[4:]
    assert group.is_relative_to(Path("/sys/fs/cgroup"))
    effective = (group / "cpuset.cpus.effective").read_text().strip()
    assert cpu.cpu_set(effective) == cpu.cpu_set(expected)
    assert os.sched_getaffinity(pid) == cpu.cpu_set(expected)
    process_stat = Path(f"/proc/{pid}/stat").read_text()
    processor = int(process_stat[process_stat.rfind(")") + 2:].split()[36])
    assert processor in cpu.cpu_set(expected)
    ancestors = []
    for directory in (group, *group.parents):
        if not directory.is_relative_to(Path("/sys/fs/cgroup")):
            break
        quota = directory / "cpu.max"
        raw = quota.read_text().strip() if quota.exists() else None
        # The cgroup-v2 root has no cpu.max; every applicable ancestor must be unlimited.
        assert raw is not None or directory == Path("/sys/fs/cgroup")
        assert raw is None or raw.split()[0] == "max"
        ancestors.append({"path": str(directory), "cpu.max": raw})
    stats = (group / "cpu.stat").read_text()
    values = {key: int(value) for key, value in (line.split() for line in stats.splitlines())}
    return {"construction": construction, "pid": pid, "effective_cpuset": effective,
            "affinity": sorted(os.sched_getaffinity(pid)), "observed_processor": processor,
            "cgroup": str(group), "cpu.stat": stats, "usage_usec": values["usage_usec"],
            "nr_throttled": values.get("nr_throttled", 0), "ancestor_quotas": ancestors,
            "cpu.pressure": (group / "cpu.pressure").read_text(),
            "monotonic_ns": time.monotonic_ns()}


def smoke(output):
    import docker
    from docker.models.containers import ContainerCollection

    sys.path.insert(0, str(ROOT))
    from scripts.assignment import evaluate_swebench_case as evaluator

    output.mkdir(parents=True, exist_ok=False)
    client = docker.from_env()
    image_id = client.images.get(IMAGE).id  # no pull/build
    owner = uuid.uuid4().hex
    containers = []
    original_create = ContainerCollection.create
    original_env = os.environ.copy()
    topology = cpu.verify_topology()
    result = {"schema_version": "assignment.cpu-placement-smoke.v1", "topology": topology,
              "image_id": image_id, "owner": owner, "scope": "three CPU-only synthetic containers",
              "policy_source_sha256": hashlib.sha256(cpu.SOURCE.read_bytes()).hexdigest(),
              "containers": [], "passed": False}
    command = ["/bin/sh", "-c", "while [ ! -e /tmp/cpu-policy-sleep ]; do :; done; exec sleep 30"]
    try:
        for worker in ("00", "12"):
            policy = cpu.policy_config(worker)
            created = subprocess.run(
                ["docker", "create", "--network", "none", *cpu.worker_docker_args(policy, []),
                 "--label", f"{OWNER_LABEL}={owner}", image_id, *command],
                text=True, capture_output=True, check=True, timeout=20,
            )
            container = client.containers.get(created.stdout.strip())
            containers.append((f"worker-{worker}", policy["worker_cpuset"], container))

        runtime = output / "synthetic-runtime.json"
        runtime.write_text(json.dumps({"schema_version": "assignment-runtime-manifest.v1",
                                      "runner": {"cpu_policy": cpu.policy_config("00")}}))
        digest = hashlib.sha256(runtime.read_bytes()).hexdigest()
        os.environ["ASSIGNMENT_CASE_OWNER"] = owner
        # Execute the real adapter wrapper against the real Docker SDK, substituting
        # only the harness module. No evaluator datasets, reports or tests are read/run.
        for name in ("swebench", "swebench.harness", "swebench.harness.docker_utils"):
            sys.modules[name] = ModuleType(name)
        source = evaluator.compatibility_wrapper_source()
        source = source.replace('runpy.run_module("swebench.harness.run_evaluation", run_name="__main__")', "")
        with cpu.runtime_placement(runtime, digest, active=True):
            result["control_affinity"] = sorted(os.sched_getaffinity(0))
            result["child_affinity"] = json.loads(subprocess.check_output(
                [sys.executable, "-S", "-c", "import json,os; print(json.dumps(sorted(os.sched_getaffinity(0))))"],
                text=True, timeout=10,
            ))
            assert result["control_affinity"] == result["child_affinity"] == sorted(cpu.cpu_set(cpu.CONTROL_CPUSET))
            exec(compile(source, "<reviewed-evaluator-wrapper>", "exec"), {"__name__": "cpu_smoke_wrapper"})
            container = client.containers.create(image_id, command, network_mode="none")
            containers.append(("evaluator", cpu.CONTROL_CPUSET, container))
            for _, _, container in containers:
                container.start()
            before = [snapshot(container, cpus) for _, cpus, container in containers]
            time.sleep(1.5)
            busy = [snapshot(container, cpus) for _, cpus, container in containers]
            for _, _, container in containers:
                exit_code, _ = container.exec_run(["/bin/sh", "-c", "touch /tmp/cpu-policy-sleep"])
                assert exit_code == 0
            time.sleep(0.2)
            sleep_start = [snapshot(container, cpus) for _, cpus, container in containers]
            time.sleep(0.8)
            sleep_end = [snapshot(container, cpus) for _, cpus, container in containers]
            for i, (role, cpus, _) in enumerate(containers):
                busy_cpu = busy[i]["usage_usec"] - before[i]["usage_usec"]
                sleep_cpu = sleep_end[i]["usage_usec"] - sleep_start[i]["usage_usec"]
                assert busy_cpu > 100000 and sleep_cpu < busy_cpu / 10
                assert sleep_end[i]["nr_throttled"] == before[i]["nr_throttled"]
                result["containers"].append({"role": role, "cpuset": cpus,
                    "busy_cpu_usec": busy_cpu, "sleep_cpu_usec": sleep_cpu,
                    "before": before[i], "busy": busy[i],
                    "sleep_start": sleep_start[i], "sleep_end": sleep_end[i]})
        result["passed"] = True
    finally:
        ContainerCollection.create = original_create
        os.environ.clear()
        os.environ.update(original_env)
        cleanup = []
        for role, _, container in containers:
            container.reload()
            assert container.attrs["Config"]["Labels"][OWNER_LABEL] == owner
            container.remove(force=True)
            cleanup.append({"role": role, "id": container.id, "removed": True})
        result["cleanup"] = cleanup
        raw = (json.dumps(result, indent=2, sort_keys=True) + "\n").encode()
        (output / "placement.json").write_bytes(raw)
        (output / "placement.json.sha256").write_text(hashlib.sha256(raw).hexdigest() + "  placement.json\n")
        client.close()
    return result


def test_actual_container_placement(tmp_path):
    import pytest
    if os.environ.get("ASSIGNMENT_CPU_POLICY_DOCKER_SMOKE") != "1":
        pytest.skip("explicit opt-in required for three synthetic Docker containers")
    assert smoke(tmp_path / "placement")["passed"]


if __name__ == "__main__":
    result = smoke(Path(sys.argv[1]).resolve())
    print(json.dumps({"passed": result["passed"], "containers": [
        {key: row[key] for key in ("role", "cpuset", "busy_cpu_usec", "sleep_cpu_usec")}
        for row in result["containers"]]}, sort_keys=True))
