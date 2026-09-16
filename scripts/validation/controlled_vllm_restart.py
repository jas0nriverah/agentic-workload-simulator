#!/usr/bin/env python3
"""Prepare/restart one identity-bound vLLM process inside its existing Slurm job.

Run on the assigned compute node. Preparation is read-only except for the new
private checkpoint. Execution requires the checkpoint hash and an explicit
flag; it never cancels a job, kills a process group, or changes a relay.
Original credentials/environment remain in a mode-0600 remote checkpoint and
must not be copied into public submission artifacts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
import urllib.request


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def durable_json(path, value):
    data = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    directory = os.open(str(Path(path).parent), os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def process(pid):
    p = Path("/proc") / str(pid)
    assert p.stat().st_uid == os.getuid(), "process is not owned by this user"
    raw = (p / "stat").read_text()
    fields = raw[raw.rfind(")") + 2:].split()
    return {
        "pid": pid, "start_ticks": int(fields[19]), "state": fields[0],
        "argv": [v.decode() for v in (p / "cmdline").read_bytes().split(b"\0") if v],
        "environment": dict(v.decode().split("=", 1) for v in (p / "environ").read_bytes().split(b"\0") if b"=" in v),
        "cwd": str((p / "cwd").resolve()), "cgroup": (p / "cgroup").read_text(),
        "cpu_affinity": sorted(os.sched_getaffinity(pid)),
    }


def replacement_binding(original):
    """Preserve vLLM's numeric CUDA selector and the original CPU allocation.

    vLLM 0.10.0 converts CUDA_VISIBLE_DEVICES entries to integers. Verify the
    GPU UUID independently through NVML, rather than substituting it here.
    """
    selector = original["environment"].get("CUDA_VISIBLE_DEVICES", "")
    assert selector.isdecimal(), "single-GPU vLLM requires its original numeric CUDA selector"
    affinity = original.get("cpu_affinity")
    if affinity is None:
        # Older prepared checkpoints retained Slurm's explicit binding mask.
        mask = original["environment"].get("SLURM_CPU_BIND_LIST", "")
        assert mask.startswith("0x") and "," not in mask, "original CPU binding unavailable"
        bits = int(mask, 16)
        affinity = [i for i in range(bits.bit_length()) if bits & (1 << i)]
    assert affinity and sorted(os.sched_getaffinity(0)) == sorted(affinity), "replacement CPU allocation differs from original"
    assert os.environ.get("CUDA_VISIBLE_DEVICES") == selector, "Slurm CUDA selector changed"
    return selector, sorted(affinity)


def option(argv, name):
    matches = []
    for i, arg in enumerate(argv):
        if arg == name:
            matches.append(argv[i + 1])
        elif arg.startswith(name + "="):
            matches.append(arg.partition("=")[2])
    assert len(matches) == 1, f"expected exactly one {name}"
    return matches[0]


def reviewed_middleware(argv):
    values = []
    for index, arg in enumerate(argv):
        if arg.startswith('--middleware='):
            values.append(arg.partition('=')[2])
        elif arg == '--middleware':
            end = index + 1
            while end < len(argv) and not argv[end].startswith('--'):
                values.append(argv[end])
                end += 1
            assert end > index + 1, 'middleware option has no value'
    assert values in ([], ['serving_observer.ServingObserver']), 'unreviewed or duplicate middleware'
    return bool(values)


def idle(port):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=10) as r:
        assert r.status == 200
        raw = r.read()
    totals = {}
    for line in raw.decode().splitlines():
        if line.startswith("#") or not line.strip():
            continue
        name = line.split("{", 1)[0].split()[0]
        if name in {"vllm:num_requests_running", "vllm:num_requests_waiting"}:
            totals[name] = totals.get(name, 0.0) + float(line.split()[1])
    assert set(totals) == {"vllm:num_requests_running", "vllm:num_requests_waiting"}, "missing native idle gauges"
    assert all(value == 0 for value in totals.values()), "server has active or waiting requests"
    return {"gauge_totals": totals, "raw_sha256": hashlib.sha256(raw).hexdigest()}


def prepare(args):
    target = process(args.pid)
    assert target["start_ticks"] == args.start_ticks, "PID identity changed"
    assert target["environment"].get("SLURM_JOB_ID") == args.job_id, "job mismatch"
    assert "vllm.entrypoints.openai.api_server" in target["argv"], "unexpected process"
    assert int(option(target["argv"], "--port")) == args.port, "port mismatch"
    reviewed_middleware(target["argv"])
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    evidence = idle(args.port)
    durable_json(args.output / "original.private.json", target)
    plan = {
        "schema_version": "assignment.controlled-vllm-restart.v1",
        "hostname": socket.gethostname(), "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "pid": args.pid, "start_ticks": args.start_ticks, "job_id": args.job_id,
        "port": args.port, "worker_id": args.worker_id, "gpu_uuid": args.gpu_uuid,
        "original_sha256": digest(args.output / "original.private.json"),
        "old_max_model_len": option(target["argv"], "--max-model-len"),
        "new_max_model_len": "65536", "model": option(target["argv"], "--model"),
        "idle": evidence, "prepared_unix_seconds": time.time(),
    }
    durable_json(args.output / "plan.json", plan)
    print(json.dumps({"status": "prepared", "plan": str(args.output / "plan.json"), "sha256": digest(args.output / "plan.json")}))


def execute(args):
    assert args.execute, "--execute required"
    plan_path = args.output / "plan.json"
    assert digest(plan_path) == args.plan_sha256, "plan hash mismatch"
    plan = json.loads(plan_path.read_text())
    assert plan["hostname"] == socket.gethostname(), "node mismatch"
    assert plan["boot_id"] == Path("/proc/sys/kernel/random/boot_id").read_text().strip(), "boot mismatch"
    assert os.environ.get("SLURM_JOB_ID") == plan["job_id"], "execute inside the exact existing Slurm allocation"
    original_path = args.output / "original.private.json"
    assert digest(original_path) == plan["original_sha256"], "private checkpoint mismatch"
    original = json.loads(original_path.read_text())
    has_observer = reviewed_middleware(original["argv"])
    current = process(plan["pid"])
    assert current["start_ticks"] == plan["start_ticks"], "PID reused"
    assert current["argv"] == original["argv"], "launch arguments changed"
    assert current["environment"].get("SLURM_JOB_ID") == plan["job_id"]
    cuda_selector, cpu_affinity = replacement_binding(original)
    gpu = subprocess.check_output(["nvidia-smi", "--query-gpu=uuid", "--format=csv,noheader"], text=True, timeout=15).splitlines()
    assert gpu == [plan["gpu_uuid"]], "allocation GPU visibility does not match exact UUID"
    assert digest(args.observer_env) == args.observer_env_sha256, "observer environment hash mismatch"
    observer_env = json.loads(args.observer_env.read_text())
    assert all(k.startswith("EIC_") and isinstance(v, str) for k, v in observer_env.items())
    assert observer_env.get("EIC_SERVING_OBSERVER_POSTCOMPLETION_DELAYS", "") == "", "optional sampling disabled"
    for key in ("EIC_SERVING_OBSERVER_JOURNAL", "EIC_SERVING_OBSERVER_ARTIFACT_DIR",
                "EIC_SERVER_IDENTITY", "EIC_SERVER_LEASE_ID", "EIC_COUNTER_EPOCH",
                "EIC_NATIVE_VLLM_JOURNAL", "EIC_NATIVE_VLLM_EXPECTED_HOOK_SHA256"):
        assert observer_env.get(key), f"missing observer setting: {key}"
    assert observer_env.get("EIC_NATIVE_VLLM_OBSERVER") == "true"
    assert observer_env.get("EIC_SERVER_DEDICATED") == "true"
    source_manifest = json.loads(args.source_manifest.read_text())
    assert digest(args.source_manifest) == args.source_manifest_sha256
    source_dir = args.source_manifest.parent
    for name, expected in source_manifest["files"].items():
        assert Path(name).name == name and digest(source_dir / name) == expected, "observer source mismatch"
    assert {"serving_observer.py", "native_vllm_observer.py"} <= set(source_manifest["files"])
    env = dict(original["environment"])
    for key in list(env):
        if key.startswith("SLURM_"):
            del env[key]
    env.update({k: v for k, v in os.environ.items() if k.startswith("SLURM_")})
    env.update(observer_env)
    env["CUDA_VISIBLE_DEVICES"] = cuda_selector
    env["PYTHONPATH"] = str(source_dir) + (":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    # Fail imports/version/source binding before stopping the old server. This
    # does not install hooks, construct an observer, or initialize model work.
    precheck = """import ast,hashlib,os,importlib.metadata,importlib.util
from pathlib import Path
import serving_observer,native_vllm_observer
assert importlib.metadata.version('vllm') == '0.10.0'
spec = importlib.util.find_spec('vllm')
root = Path(next(iter(spec.submodule_search_locations)))
raw = (root/'v1/engine/output_processor.py').read_bytes()
tree = ast.parse(raw); lines = raw.splitlines(keepends=True)
cls = next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name == 'OutputProcessor')
fn = next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name == '_update_stats_from_finished')
assert [a.arg for a in fn.args.args] == ['self','req_state','finish_reason','iteration_stats']
assert not fn.decorator_list and not fn.args.vararg and not fn.args.kwarg
source = b''.join(lines[fn.lineno-1:fn.end_lineno])
assert hashlib.sha256(source).hexdigest() == os.environ['EIC_NATIVE_VLLM_EXPECTED_HOOK_SHA256']
"""
    subprocess.run([original["argv"][0], "-c", precheck], env=env, check=True, timeout=60)
    idle_evidence = idle(plan["port"])
    durable_json(args.output / "restart-intent.json", {
        "plan_sha256": args.plan_sha256, "source_manifest_sha256": args.source_manifest_sha256,
        "observer_env_sha256": args.observer_env_sha256, "idle": idle_evidence,
        "started_unix_seconds": time.time(), "replacement_step_id": os.environ.get("SLURM_STEP_ID"),
        "cpu_affinity": cpu_affinity, "cuda_visible_devices": cuda_selector,
    })
    # Recheck identity directly before signaling only the reviewed process.
    assert process(plan["pid"])["start_ticks"] == plan["start_ticks"]
    os.kill(plan["pid"], signal.SIGTERM)
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            old = process(plan["pid"])
        except FileNotFoundError:
            break
        if old["start_ticks"] != plan["start_ticks"] or old["state"] == "Z":
            break
        time.sleep(.2)
    else:
        raise RuntimeError("old process did not stop; no new server launched and no force kill sent")
    argv = list(original["argv"])
    for i, value in enumerate(argv):
        if value == "--max-model-len":
            argv[i + 1] = "65536"
        elif value.startswith("--max-model-len="):
            argv[i] = "--max-model-len=65536"
    if not has_observer:
        argv += ["--middleware", "serving_observer.ServingObserver"]
    if "--enable-prompt-tokens-details" not in argv:
        # Report native cached prompt work in the already-retained API usage.
        # This flag changes reporting, not prefix-cache policy or sampling.
        argv.append("--enable-prompt-tokens-details")
    durable_json(args.output / "replacement-exec.json", {
        "pid": os.getpid(), "start_ticks": process(os.getpid())["start_ticks"],
        "argv_sha256": hashlib.sha256(json.dumps(argv).encode()).hexdigest(),
        "source_manifest_sha256": args.source_manifest_sha256, "unix_seconds": time.time(),
    })
    os.chdir(original["cwd"])
    os.execve(argv[0], argv, env)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["prepare", "execute"])
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--pid", type=int)
    p.add_argument("--start-ticks", type=int)
    p.add_argument("--job-id")
    p.add_argument("--port", type=int)
    p.add_argument("--worker-id")
    p.add_argument("--gpu-uuid")
    p.add_argument("--plan-sha256")
    p.add_argument("--observer-env", type=Path)
    p.add_argument("--observer-env-sha256")
    p.add_argument("--source-manifest", type=Path)
    p.add_argument("--source-manifest-sha256")
    p.add_argument("--execute", action="store_true")
    args = p.parse_args()
    assert args.output.is_absolute(), "absolute durable output path required"
    (prepare if args.action == "prepare" else execute)(args)


if __name__ == "__main__":
    main()
