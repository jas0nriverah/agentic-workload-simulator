"""Fixed Sep 9 placement on the CPU VM: 16 physical / 32 logical CPUs.

Workers share SMT core resources (00/12, ..., 10/22). Placement is not a
quota, a thread-count adjustment, or a promise of independent physical cores.
Only a hash-bound runtime manifest can activate this policy.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping


SCHEMA = "assignment.cpu-placement.20260909.v1"
RUNTIME_PATH_ENV = "ASSIGNMENT_CPU_POLICY_RUNTIME_PATH"
RUNTIME_SHA_ENV = "ASSIGNMENT_CPU_POLICY_RUNTIME_SHA256"
CONTROL_CPUSET = "11-15,27-31"
WORKER_CPUS = {f"{i:02d}": str(i if i <= 10 else i + 4)
               for i in (*range(11), *range(12, 23))}
SOURCE = Path(__file__).resolve()


class CPUPolicyError(ValueError):
    """The requested placement is not the reviewed, runtime-bound policy."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CPUPolicyError(message)


def cpu_set(value: str) -> set[int]:
    result: set[int] = set()
    for part in value.split(","):
        bounds = part.split("-")
        _require(len(bounds) in (1, 2) and all(v.isdigit() for v in bounds),
                 "invalid CPU set")
        low, high = int(bounds[0]), int(bounds[-1])
        _require(0 <= low <= high < 4096, "invalid CPU range")
        result.update(range(low, high + 1))
    return result


def policy_config(worker_id: str, source: Path = SOURCE) -> dict[str, Any]:
    """Build the only accepted placement block, for inclusion in a sealed runtime."""
    _require(isinstance(worker_id, str) and worker_id in WORKER_CPUS,
             "CPU policy worker must be 00-10 or 12-22")
    _require(source.is_file() and not source.is_symlink(), "CPU policy source must be regular")
    return {
        "schema_version": SCHEMA,
        "worker_id": worker_id,
        "worker_cpuset": WORKER_CPUS[worker_id],
        "control_cpuset": CONTROL_CPUSET,
        "physical_cores": 16,
        "logical_cpus": 32,
        "smt_siblings": [[i, i + 16] for i in range(16)],
        "cpu_quota": "none",
        "source_path": str(source.resolve()),
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }


def validate_policy(value: Mapping[str, Any]) -> dict[str, Any]:
    _require(isinstance(value, Mapping), "CPU policy must be an object")
    expected = policy_config(value.get("worker_id"))
    _require(dict(value) == expected, "CPU policy mapping/source SHA-256 differs from review")
    queue_worker = os.environ.get("ASSIGNMENT_QUEUE_WORKER_ID")
    _require(queue_worker is None or queue_worker in
             {expected["worker_id"], "worker-" + expected["worker_id"]},
             "queue worker differs from CPU policy worker")
    return expected


def load_runtime(path: Path, digest: str) -> dict[str, Any]:
    _require(path.is_absolute() and path.is_file() and not path.is_symlink(),
             "CPU policy requires an absolute regular runtime manifest")
    raw = path.read_bytes()
    _require(hashlib.sha256(raw).hexdigest() == digest, "CPU policy runtime SHA-256 mismatch")
    runtime = json.loads(raw)
    _require(runtime.get("schema_version") == "assignment-runtime-manifest.v1",
             "CPU policy requires a runtime manifest")
    return validate_policy(runtime.get("runner", {}).get("cpu_policy"))


def from_environment() -> dict[str, Any] | None:
    path, digest = os.environ.get(RUNTIME_PATH_ENV), os.environ.get(RUNTIME_SHA_ENV)
    if path is None and digest is None:
        return None
    _require(bool(path) and bool(digest), "incomplete CPU policy runtime binding")
    return load_runtime(Path(path), digest)


def verify_topology(root: Path = Path("/sys/devices/system/cpu")) -> dict[str, Any]:
    _require(cpu_set((root / "online").read_text().strip()) == set(range(32)),
             "CPU policy requires exactly 32 online logical CPUs")
    cores = set()
    for i in range(32):
        topology = root / f"cpu{i}" / "topology"
        _require(cpu_set((topology / "thread_siblings_list").read_text().strip())
                 == {i % 16, i % 16 + 16}, "CPU policy SMT topology mismatch")
        cores.add(((topology / "physical_package_id").read_text().strip(),
                   (topology / "core_id").read_text().strip()))
    _require(len(cores) == 16, "CPU policy requires 16 physical cores")
    return {"physical_cores": 16, "logical_cpus": 32,
            "smt_siblings": [[i, i + 16] for i in range(16)]}


@contextmanager
def runtime_placement(path: Path, digest: str, *, active: bool):
    """Place the supervisor before it starts proxies/collectors/agent/evaluator.

    Docker daemon children need the separate CLI/SDK enforcement below.
    Restore this process's affinity and environment for callers using the API.
    """
    policy = load_runtime(path, digest)
    old_env = {key: os.environ.get(key) for key in (RUNTIME_PATH_ENV, RUNTIME_SHA_ENV)}
    old_affinity: dict[int, set[int]] = {}
    try:
        if active:
            verify_topology()
            for task in Path("/proc/self/task").iterdir():
                tid = int(task.name)
                try:
                    old_affinity[tid] = os.sched_getaffinity(tid)
                    os.sched_setaffinity(tid, cpu_set(CONTROL_CPUSET))
                    _require(os.sched_getaffinity(tid) == cpu_set(CONTROL_CPUSET),
                             "control CPU affinity was not applied exactly")
                except ProcessLookupError:
                    continue
            os.environ.update({RUNTIME_PATH_ENV: str(path), RUNTIME_SHA_ENV: digest})
        yield policy
    finally:
        for tid, affinity in old_affinity.items():
            try:
                os.sched_setaffinity(tid, affinity)
            except ProcessLookupError:
                pass
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def worker_docker_args(policy: Mapping[str, Any], existing: list[str]) -> list[str]:
    policy = validate_policy(policy)
    allowed = ["--cpuset-cpus", policy["worker_cpuset"]]
    _require(existing in ([], allowed), "only the reviewed worker cpuset Docker args are allowed")
    return allowed


def evaluator_docker_kwargs(policy: Mapping[str, Any], kwargs: Mapping[str, Any]) -> dict[str, Any]:
    validate_policy(policy)
    result = dict(kwargs)
    _require(result.get("cpuset_cpus", CONTROL_CPUSET) == CONTROL_CPUSET,
             "evaluator CPU set differs from control pool")
    # Accept Docker's default zero values, never add a quota or scheduling knob.
    for key in ("cpu_quota", "cpu_period", "nano_cpus", "cpu_shares", "cpu_rt_period",
                "cpu_rt_runtime", "cpu_count", "cpu_percent"):
        _require(result.get(key) in (None, 0), f"CPU policy rejects {key}")
    for key in ("cpuset_mems", "cgroup_parent", "privileged"):
        _require(not result.get(key), f"CPU policy rejects {key}")
    result["cpuset_cpus"] = CONTROL_CPUSET
    return result


def verify_container(info: Mapping[str, Any], expected: str) -> dict[str, Any]:
    """Check the daemon's actual construction, independently of parent taskset."""
    config = info.get("HostConfig") or {}
    _require(cpu_set(config.get("CpusetCpus", "")) == cpu_set(expected),
             "Docker container CPU set differs from policy")
    for key in ("CpuQuota", "CpuPeriod", "NanoCpus", "CpuShares", "CpuRealtimePeriod",
                "CpuRealtimeRuntime"):
        _require(config.get(key) == 0, f"Docker container has nondefault {key}")
    return {"container_id": info.get("Id"), "cpuset_cpus": config["CpusetCpus"],
            "cpu_quota": config["CpuQuota"], "nano_cpus": config["NanoCpus"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-manifest", type=Path, required=True)
    parser.add_argument("--runtime-sha256", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    _require(bool(command), "CPU policy launcher requires a command")
    with runtime_placement(args.runtime_manifest, args.runtime_sha256, active=True):
        os.execvpe(command[0], command, os.environ)


if __name__ == "__main__":
    main()
