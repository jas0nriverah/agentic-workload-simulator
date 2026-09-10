#!/usr/bin/env python3
"""External final-production launcher with parser prewarm and staged births.

The reviewed v1 gates remain the source of launch eligibility.  This external
copy adds one read-only deployment-parser warmup before creating any launch
state, then starts supervisors in groups of five with a bounded pause between
groups.  The pause applies only to future births; already-running supervisors
are never stopped or otherwise delayed.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

HERE = Path(__file__).resolve().parent
N = HERE.parent
MONITOR = N / "production-source-candidate-v9/scripts/assignment/acquisition_monitor.py"
EXECUTOR = N / "launch_selected_workers_v3.py"
EXECUTOR_SHA256 = "6fa7dd2321bd8bc62216ecdfb16648582b1f93b9ca45cbf7a767d81f4fe9381b"
BATCH_LOCK = N / "storage-reclamation-v1/production-rolling-batch.lock"
DECLARED = tuple(f"worker-{n:02d}" for n in (*range(11), *range(12, 23)))
MINIMUM_REMAINING_SECONDS = 6000
PARSER_PREWARM_TIMEOUT_SECONDS = 20.0
STARTUP_GROUP_SIZE = 5
STARTUP_GROUP_PAUSE_SECONDS = 5.0
DOCKER_PROOF_MARKER_ENV = "ASSIGNMENT_DOCKER_PROOF_MARKER"
_V2_ACTIVATION_ENV_KEYS = (
    "ASSIGNMENT_TELEMETRY_V2_AUTO",
    "ASSIGNMENT_TELEMETRY_V2_READY",
    "ASSIGNMENT_TELEMETRY_V2_HANDSHAKE_NONCE",
    "ASSIGNMENT_TELEMETRY_V2_OWNER_PID",
    "ASSIGNMENT_TELEMETRY_V2_SUPERVISOR",
    "ASSIGNMENT_TELEMETRY_V2_REQUIRED",
    "ASSIGNMENT_TELEMETRY_V2_REQUIRE_RAW_REQUEST_BODIES",
    "ASSIGNMENT_TELEMETRY_V2_HARDWARE_PROFILE_PATH",
    "ASSIGNMENT_TELEMETRY_V2_HARDWARE_PROFILE_SHA256",
    "ASSIGNMENT_TELEMETRY_V2_MODEL_HARDWARE_JSON",
    "ASSIGNMENT_TELEMETRY_V2_CPU_COLLECTOR_CONFIG",
    "ASSIGNMENT_TELEMETRY_V2_DIR",
    "ASSIGNMENT_TELEMETRY_V2_RUN_ID",
    "ASSIGNMENT_TELEMETRY_V2_ATTEMPT_ID",
    "ASSIGNMENT_CASE_ID",
    "ASSIGNMENT_INSTANCE_ID",
    "ASSIGNMENT_MODEL",
    "ASSIGNMENT_MODEL_REVISION",
)

# This is the exact read-only parser used by the clean case runner's Docker
# ownership proof.  Keep it byte-for-byte equivalent to the reviewed runner;
# the external launcher only warms imports/config parsing and never launches a
# container, model request, or queue attempt here.
DOCKER_PARSER_CODE = """import contextlib, io, json, os, sys
try:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        from sweagent.run.common import BasicCLI
        from sweagent.run.run_batch import RunBatchConfig
        config = BasicCLI(RunBatchConfig).get_config(sys.argv[1:])
        deployment = config.instances.deployment
    marker = os.environ["ASSIGNMENT_DOCKER_PROOF_MARKER"]
    print(marker + json.dumps({'docker': deployment.type == 'docker', 'empty_args': not deployment.docker_args, 'remove_images': deployment.remove_images, 'docker_args': deployment.docker_args}, sort_keys=True), flush=True)
except BaseException:
    print('deployment configuration could not be verified', file=sys.stderr)
    sys.exit(1)
"""


class Stop(RuntimeError):
    pass


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1048576), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def write_exclusive(path: Path, value: Mapping[str, Any]) -> None:
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(canonical(value))
        stream.flush()
        os.fsync(stream.fileno())


def fsync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def fresh_epoch(value: Any, label: str, now: float | None = None) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise Stop(f"{label} captured_epoch missing or invalid")
    age = (time.time() if now is None else now) - float(value)
    if age < 0 or age > 300:
        raise Stop(f"{label} is not fresh (age {age:.3f}s)")
    return age


def image_for_instance(instance_id: str) -> str:
    return ("swebench/sweb.eval.x86_64." + instance_id.replace("__", "_1776_") + ":latest").lower()


def plan_image_map(plan_path: Path, plan_sha256: str) -> dict[str, str]:
    if sha(plan_path) != plan_sha256:
        raise Stop("authorized frozen plan SHA mismatch")
    rows = [json.loads(line) for line in plan_path.read_text().splitlines() if line.strip()]
    cases = [row for row in rows if isinstance(row, dict) and row.get("record_type") == "case"]
    result: dict[str, str] = {}
    for row in cases:
        case_id, instance_id = row.get("case_id"), row.get("instance_id")
        if not isinstance(case_id, str) or not isinstance(instance_id, str) or case_id in result:
            raise Stop("authorized frozen plan case/image identity malformed")
        result[case_id] = image_for_instance(instance_id)
    if len(result) != 1088:
        raise Stop("authorized frozen plan does not contain 1088 case records")
    return result


def queue_is_idle_and_prefix(auth: Mapping[str, Any], monitor: Any, selected_cases: Sequence[str]) -> dict[str, Any]:
    snapshot = monitor.read_queue_snapshot(auth)
    counts = snapshot.get("case_status_counts", {})
    if snapshot.get("dispatch_halted") or snapshot.get("active_or_orphaned_attempts") or counts.get("blocked", 0):
        raise Stop("queue is halted/active/orphaned/blocked")
    db = sqlite3.connect("file:" + str(auth["db_path"]) + "?mode=ro", uri=True)
    try:
        expected = [row[0] for row in db.execute("select case_id from cases where status='pending' order by ordinal limit ?", (len(selected_cases),))]
    finally:
        db.close()
    if expected != list(selected_cases):
        raise Stop("selected cases are not the current ordered pending prefix")
    return snapshot


def select_eligible_prefix(
    registered: Mapping[str, Mapping[str, Any]],
    health_workers: Mapping[str, Mapping[str, Any]],
    cases: Sequence[str],
    images: Sequence[str],
) -> tuple[tuple[str, ...], list[str], list[str], list[dict[str, str]]]:
    """Return the ordered pending prefix that verified endpoints can accept.

    All twenty prepared bindings must remain present.  An administratively
    disabled endpoint, or one with a missing, failed, or expiring health proof,
    is isolated for this wave; it does not invalidate independent bindings.
    """
    if len(registered) != 20 or any(not isinstance(row, Mapping) for row in registered.values()):
        raise Stop("exactly 20 prepared runtime bindings required")
    eligible: list[str] = []
    isolated: list[dict[str, str]] = []
    for worker_id in sorted(registered):
        if not registered[worker_id].get("enabled"):
            isolated.append({"worker_id": worker_id, "reason": "queue_binding_disabled"})
            continue
        proof = health_workers.get(worker_id)
        if not isinstance(proof, Mapping):
            isolated.append({"worker_id": worker_id, "reason": "health_proof_missing"})
            continue
        if proof.get("status") != "PASS":
            isolated.append({"worker_id": worker_id, "reason": "health_status_not_pass"})
            continue
        remaining = proof.get("remaining_seconds")
        if not isinstance(remaining, int) or isinstance(remaining, bool) or remaining < MINIMUM_REMAINING_SECONDS:
            isolated.append({"worker_id": worker_id, "reason": "slurm_remaining_below_6000"})
            continue
        eligible.append(worker_id)
    selected_count = min(len(cases), len(eligible), 1)
    if selected_count == 0:
        raise Stop("no healthy registered endpoint can accept the pending prefix")
    # Slicing is deliberate: pending work retains ordinal order while endpoint
    # exclusions only reduce wave cardinality.
    return (
        tuple(eligible[:selected_count]),
        list(cases[:selected_count]),
        list(images[:selected_count]),
        isolated,
    )


def verified_cached_images(
    images: Sequence[str],
    run: Callable[..., Any] = subprocess.run,
) -> dict[str, dict[str, Any]]:
    """Bind each selected reference to a present local image and RepoDigest."""
    verified: dict[str, dict[str, Any]] = {}
    for image in images:
        result = run(
            ["docker", "image", "inspect", image],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            raise Stop(f"selected image is not locally cached: {image}")
        try:
            rows = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise Stop(f"selected image inspect is not JSON: {image}") from exc
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise Stop(f"selected image inspect is ambiguous: {image}")
        row = rows[0]
        image_id = row.get("Id")
        digests = row.get("RepoDigests")
        if not isinstance(image_id, str) or not image_id.startswith("sha256:"):
            raise Stop(f"selected image has no immutable local ID: {image}")
        if not isinstance(digests, list) or not digests or any(not isinstance(value, str) or "@sha256:" not in value for value in digests):
            raise Stop(f"selected image has no immutable repository digest: {image}")
        verified[image] = {"image_id": image_id, "repo_digests": sorted(digests)}
    return verified


def _bound_runtime(auth: Mapping[str, Any], worker_id: str) -> dict[str, Any]:
    """Read one immutable runtime binding without touching queue state."""
    connection = sqlite3.connect("file:" + str(auth["db_path"]) + "?mode=ro", uri=True)
    try:
        row = connection.execute(
            "select runtime_json from workers where worker_id = ?", (worker_id,)
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise Stop(f"runtime binding is unavailable for parser prewarm: {worker_id}")
    try:
        binding = json.loads(row[0])
    except (TypeError, json.JSONDecodeError) as exc:
        raise Stop(f"runtime binding is malformed for parser prewarm: {worker_id}") from exc
    if not isinstance(binding, Mapping):
        raise Stop(f"runtime binding is not an object for parser prewarm: {worker_id}")
    path_value, declared = binding.get("path"), binding.get("sha256")
    if not isinstance(path_value, str) or not Path(path_value).is_absolute() or not isinstance(declared, str):
        raise Stop(f"runtime binding identity is malformed for parser prewarm: {worker_id}")
    path = Path(path_value)
    if not path.is_file() or sha(path) != declared:
        raise Stop(f"runtime binding changed before parser prewarm: {worker_id}")
    try:
        runtime = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Stop(f"runtime manifest is unreadable for parser prewarm: {worker_id}") from exc
    if not isinstance(runtime, dict):
        raise Stop(f"runtime manifest is malformed for parser prewarm: {worker_id}")
    return runtime


def _parser_invocation(
    auth: Mapping[str, Any], worker_id: str,
) -> tuple[Path, Path, list[str], dict[str, str], tempfile.TemporaryDirectory[str]]:
    """Build an exact parser argv using only bound clean runtime inputs.

    The temporary request/instances files are parser inputs only.  They are
    outside the production output tree and no case is claimed or executed.
    """
    runtime = _bound_runtime(auth, worker_id)
    runner = runtime.get("runner")
    model = runtime.get("model")
    datasets = runtime.get("datasets")
    if not isinstance(runner, Mapping) or not isinstance(model, Mapping) or not isinstance(datasets, Mapping):
        raise Stop("runtime manifest lacks parser-prewarm bindings")
    project_value = runner.get("project")
    config_value = runner.get("config_path")
    if not isinstance(project_value, str) or not isinstance(config_value, str):
        raise Stop("runtime runner project/config binding is missing for parser prewarm")
    project = Path(project_value)
    config_path = Path(config_value)
    parser_python = project / ".venv" / "bin" / "python"
    if not project.is_dir() or not parser_python.is_file() or not os.access(parser_python, os.X_OK):
        raise Stop("clean SWE-agent parser Python is unavailable")
    if not config_path.is_file():
        raise Stop("clean SWE-agent parser config is unavailable")
    dataset = datasets.get("lite") or datasets.get("verified")
    if not isinstance(dataset, Mapping) or not isinstance(dataset.get("instances_path"), str):
        raise Stop("runtime dataset binding is unavailable for parser prewarm")
    dataset_path = Path(str(dataset["instances_path"]))
    if not dataset_path.is_file():
        raise Stop("runtime dataset file is unavailable for parser prewarm")
    try:
        with dataset_path.open(encoding="utf-8") as dataset_stream:
            first_line = next(line for line in dataset_stream if line.strip())
        instance = json.loads(first_line)
    except (OSError, StopIteration, json.JSONDecodeError) as exc:
        raise Stop("runtime dataset cannot provide a parser-prewarm instance") from exc
    if not isinstance(instance, dict) or not isinstance(instance.get("instance_id"), str) or not instance["instance_id"]:
        raise Stop("runtime dataset has no parser-prewarm instance identity")

    temporary = tempfile.TemporaryDirectory(prefix="assignment-docker-parser-prewarm-")
    root = Path(temporary.name)
    request_config = root / "request_config.json"
    instances = root / "instances.json"
    output_dir = root / "parser-output"
    request_config.write_text(json.dumps({
        "agent": {
            "model": {"completion_kwargs": {"max_tokens": 2048, "seed": 0}, "top_p": 1.0},
            "tools": {"env_variables": {"GIT_PAGER": "cat", "MANPAGER": "cat", "PAGER": "cat"}},
        },
        "instances": {"deployment": {"python_standalone_dir": ""}},
    }, sort_keys=True) + "\n", encoding="utf-8")
    instances.write_text(json.dumps([instance], sort_keys=True) + "\n", encoding="utf-8")
    model_name = model.get("name")
    if not isinstance(model_name, str) or not model_name:
        raise Stop("runtime model name is unavailable for parser prewarm")
    if not model_name.startswith("hosted_vllm/"):
        model_name = "hosted_vllm/" + model_name
    api_base = model.get("api_base") if isinstance(model.get("api_base"), str) else "http://127.0.0.1:8000/v1"
    api_key = model.get("api_key") if isinstance(model.get("api_key"), str) else "EMPTY"
    args = [
        "--config", str(config_path), "--config", str(request_config),
        "--instances.type", "file", "--instances.path", str(instances),
        "--instances.filter", "^" + instance["instance_id"] + "$",
        "--agent.model.name", model_name, "--agent.model.api_base", api_base,
        "--agent.model.api_key", api_key, "--agent.model.total_cost_limit", "0",
        "--agent.model.per_instance_cost_limit", "0", "--agent.model.per_instance_call_limit", "100",
        "--agent.model.temperature", "0.0", "--agent.model.max_input_tokens", "61440",
        "--agent.model.max_output_tokens", "2048", "--agent.templates.max_observation_length", "25000",
        "--output_dir", str(output_dir), "--num_workers", "1",
    ]
    environment = dict(os.environ)
    for key in _V2_ACTIVATION_ENV_KEYS:
        environment.pop(key, None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return parser_python, Path(str(auth["supervisor"]["cwd"])), args, environment, temporary


def prewarm_parser(
    auth: Mapping[str, Any], worker_id: str,
    *, run: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Run the exact Docker deployment parser before any paid supervisor."""
    python, cwd, parser_args, environment, temporary = _parser_invocation(auth, worker_id)
    marker = "ASSIGNMENT_DOCKER_PROOF_V2:" + uuid.uuid4().hex + ":"
    environment[DOCKER_PROOF_MARKER_ENV] = marker
    started = time.monotonic()
    try:
        result = run(
            [str(python), "-c", DOCKER_PARSER_CODE, *parser_args],
            cwd=str(cwd), env=environment, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            timeout=PARSER_PREWARM_TIMEOUT_SECONDS,
        )
        stdout = result.stdout.decode("utf-8", errors="replace") if isinstance(result.stdout, bytes) else (result.stdout or "")
        stderr = result.stderr.decode("utf-8", errors="replace") if isinstance(result.stderr, bytes) else (result.stderr or "")
        lines = [line[len(marker):] for line in stdout.splitlines() if line.startswith(marker)]
        if result.returncode != 0:
            raise Stop(f"Docker parser prewarm failed with returncode {result.returncode}: {stderr[-500:]}")
        if len(lines) != 1:
            raise Stop(f"Docker parser prewarm returned {len(lines)} proof lines")
        try:
            proof = json.loads(lines[0])
        except json.JSONDecodeError as exc:
            raise Stop("Docker parser prewarm proof is not JSON") from exc
        expected = {"docker": True, "docker_args": [], "empty_args": True, "remove_images": False}
        if proof != expected:
            raise Stop(f"Docker parser prewarm proof differs: {proof!r}")
        return {
            "schema_version": "assignment.docker-parser-prewarm.v1",
            "status": "PASS",
            "worker_id": worker_id,
            "python": str(python),
            "cwd": str(cwd),
            "timeout_seconds": PARSER_PREWARM_TIMEOUT_SECONDS,
            "elapsed_seconds": round(time.monotonic() - started, 6),
            "proof": proof,
        }
    except subprocess.TimeoutExpired as exc:
        raise Stop("Docker parser prewarm exceeded 20 seconds") from exc
    finally:
        temporary.cleanup()


def bounded_group_pause(
    group: Sequence[Mapping[str, Any]],
    *, sleep: Callable[[float], Any] = time.sleep,
) -> dict[str, Any]:
    """Give a newly born group a bounded cold-start window.

    The reviewed monitor has no durable parser-readiness event.  A fixed
    five-second pause is therefore the safe fallback permitted by the launch
    policy.  It only gates the next births and does not touch the group.
    """
    if len(group) != STARTUP_GROUP_SIZE:
        raise Stop(f"startup pause requires a complete group of {STARTUP_GROUP_SIZE}")
    sleep(STARTUP_GROUP_PAUSE_SECONDS)
    return {
        "mode": "bounded_pause",
        "seconds": STARTUP_GROUP_PAUSE_SECONDS,
        "workers": [str(row["worker_id"]) for row in group],
    }


def execute_staged(
    monitor: Any,
    executor: Any,
    auth: Mapping[str, Any],
    state_dir: Path,
    gate: Mapping[str, Any],
    workers: Sequence[str],
    *,
    prewarm: Callable[[Mapping[str, Any], str], dict[str, Any]] = prewarm_parser,
    sleep: Callable[[float], Any] = time.sleep,
) -> dict[str, Any]:
    """Prewarm, then preserve the reviewed guardian/receipt protocol."""
    if not workers:
        raise Stop("staged execution requires at least one worker")
    # This call deliberately precedes state-dir creation and every spawn.
    prewarm_receipt = prewarm(auth, str(workers[0]))
    if not isinstance(prewarm_receipt, Mapping) or prewarm_receipt.get("status") != "PASS":
        raise Stop("parser prewarm did not return an explicit PASS")
    state_dir.mkdir(mode=0o700)
    fsync_directory(state_dir.parent)
    receipt_dir = state_dir / "root-receipts"
    receipt_dir.mkdir(mode=0o700)
    write_exclusive(receipt_dir / "preflight.json", gate)
    write_exclusive(receipt_dir / "parser-prewarm.json", dict(prewarm_receipt))
    fsync_directory(receipt_dir)
    launched: list[dict[str, Any]] = []
    group_records: list[dict[str, Any]] = []
    for index, worker_id in enumerate(workers):
        argv = executor.bounded_supervisor_argv(monitor, auth, worker_id, gate.get("max_cases_per_worker"))
        launch = monitor._spawn_supervisor(auth, state_dir, worker_id, argv)
        row = {key: launch[key] for key in ("worker_id", "launch_id", "pid", "start_ticks", "boot_id", "argv_sha256", "exit_receipt", "launch_record")}
        launched.append(row)
        write_exclusive(receipt_dir / f"{worker_id}.json", {
            "schema_version": "assignment.selected-worker-launch.v1",
            "phase": "spawned",
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "authorization_id": auth["authorization_id"],
            "authorization_sha256": auth["sha256"],
            "worker_id": worker_id,
            "launch": row,
            "prohibited_actions": ["monitor_loop", "restart", "clear_halt", "register_workers", "cron_mutation", "raw_sql_write"],
        })
        fsync_directory(receipt_dir)
        complete_group = (index + 1) % STARTUP_GROUP_SIZE == 0
        has_next = index + 1 < len(workers)
        if complete_group and has_next:
            pause = bounded_group_pause(launched[-STARTUP_GROUP_SIZE:], sleep=sleep)
            group_index = index // STARTUP_GROUP_SIZE
            pause["group_index"] = group_index
            group_records.append(pause)
            write_exclusive(receipt_dir / f"startup-group-{group_index:02d}.json", {
                "schema_version": "assignment.final-production-rolling-wave.v3",
                "phase": "startup-stagger",
                "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
                "authorization_id": auth["authorization_id"],
                "authorization_sha256": auth["sha256"],
                **pause,
            })
            fsync_directory(receipt_dir)
    return {
        "state_dir": str(state_dir),
        "launched_workers": [row["worker_id"] for row in launched],
        "launches": launched,
        "parser_prewarm": dict(prewarm_receipt),
        "startup_stagger": group_records,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--monitor-sha256", required=True)
    parser.add_argument("--storage-report", type=Path, required=True)
    parser.add_argument("--storage-report-sha256", required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--health-receipt", type=Path, required=True)
    parser.add_argument("--health-receipt-sha256", required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if sha(MONITOR) != args.monitor_sha256 or sha(args.storage_report) != args.storage_report_sha256 or sha(args.health_receipt) != args.health_receipt_sha256:
        raise Stop("monitor/storage/health SHA mismatch")
    report = json.loads(args.storage_report.read_text())
    health = json.loads(args.health_receipt.read_text())
    count = report.get("selected_wave_count")
    if report.get("status") != "PASS" or not isinstance(count, int) or isinstance(count, bool) or count not in range(1, 21):
        raise Stop("storage report is not a feasible 1..20 wave")
    cases = report.get("selected_wave_case_ids")
    images = report.get("selected_wave_images")
    if (
        not isinstance(cases, list)
        or len(cases) != count
        or len(set(cases)) != len(cases)
        or any(not isinstance(case_id, str) or not case_id for case_id in cases)
        or not isinstance(images, list)
        or len(images) != count
        or any(not isinstance(image, str) or not image for image in images)
    ):
        raise Stop("storage wave identity malformed")
    if args.state_dir.exists():
        raise Stop("exclusive state dir already exists")
    if health.get("status") != "PASS" or health.get("plan_sha256") != report.get("plan_sha256"):
        raise Stop("health receipt is not pass/plan-bound")
    fresh_epoch(report.get("captured_epoch"), "storage report")
    fresh_epoch(health.get("captured_epoch"), "health receipt")
    if not os.environ.get("PYTHONPATH") or "production-source-candidate-v9" not in os.environ["PYTHONPATH"]:
        raise Stop("explicit candidate PYTHONPATH absent")

    if sha(EXECUTOR) != EXECUTOR_SHA256:
        raise Stop("reviewed executor SHA mismatch")
    monitor = load(MONITOR, "final_production_monitor")
    executor = load(EXECUTOR, "reviewed_executor")
    auth = monitor.validate_authorization(args.authorization)
    if auth.get("plan_sha256") != report.get("plan_sha256") or auth.get("plan_sha256") != health.get("plan_sha256"):
        raise Stop("authorization/report/health plan identity mismatch")
    if report.get("plan_path") != str(auth.get("plan_path")):
        raise Stop("storage report plan path differs from authorization")
    images_by_case = plan_image_map(Path(auth["plan_path"]), auth["plan_sha256"])
    if [images_by_case.get(case_id) for case_id in cases] != images:
        raise Stop("storage report case/image mapping differs from authorized frozen plan")
    if tuple(sorted(auth["worker_ids"])) != tuple(sorted(DECLARED)):
        raise Stop("authorization must declare all 22 queue workers")
    # First snapshot chooses a bounded candidate; the same checks run again
    # under the storage batch lock immediately before the guardian starts.
    snapshot = monitor.read_queue_snapshot(auth)
    counts = snapshot.get("case_status_counts", {})
    if snapshot.get("dispatch_halted") or snapshot.get("active_or_orphaned_attempts") or counts.get("blocked", 0):
        raise Stop("queue is halted/active/orphaned/blocked")
    registered = snapshot.get("registered_workers", {})
    if not isinstance(registered, dict):
        raise Stop("registered runtime bindings malformed")
    health_workers = {
        item.get("worker_id"): item
        for item in health.get("workers", [])
        if isinstance(item, dict) and isinstance(item.get("worker_id"), str)
    }
    workers, selected_cases, selected_images, isolated = select_eligible_prefix(registered, health_workers, cases, images)

    queue_is_idle_and_prefix(auth, monitor, selected_cases)

    # The report may list a larger storage-feasible wave; only the selected
    # healthy prefix must already be local and immutable for this launch.
    cached = verified_cached_images(selected_images)
    gate = {
        "schema_version": "assignment.final-production-rolling-wave.v3",
        "authorization": {"path": str(args.authorization), "sha256": auth["sha256"]},
        "monitor": {"path": str(MONITOR), "sha256": args.monitor_sha256},
        "executor": {"path": str(EXECUTOR), "sha256": EXECUTOR_SHA256},
        "storage_report": {"path": str(args.storage_report), "sha256": args.storage_report_sha256},
        "declared_worker_ids": list(DECLARED),
        "prepared_registered_workers": sorted(registered),
        "eligible_workers": [worker_id for worker_id in sorted(registered) if worker_id not in {row["worker_id"] for row in isolated}],
        "isolated_endpoints": isolated,
        "storage_feasible_wave_count": count,
        "selected_wave_case_ids": selected_cases,
        "selected_wave_images": selected_images,
        "selected_workers": list(workers),
        "selected_cached_images": cached,
        "max_cases_per_worker": 1,
        "parser_prewarm": {
            "required_before_execute": True,
            "protocol": "assignment-docker-ownership-proof.v1",
            "timeout_seconds": PARSER_PREWARM_TIMEOUT_SECONDS,
        },
        "startup_stagger": {
            "group_size": STARTUP_GROUP_SIZE,
            "pause_seconds": STARTUP_GROUP_PAUSE_SECONDS,
            "scope": "future_supervisor_births_only",
        },
        "queue_snapshot_sha256": snapshot["snapshot_sha256"],
    }
    if not args.execute:
        print(json.dumps(gate, sort_keys=True))
        return
    lock = BATCH_LOCK
    lock.parent.mkdir(exist_ok=True)
    with lock.open("a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Recheck all short-lived and mutable observations inside the shared
        # storage/launch lock.  No pull is permitted here.
        fresh_epoch(report.get("captured_epoch"), "storage report")
        fresh_epoch(health.get("captured_epoch"), "health receipt")
        locked_snapshot = queue_is_idle_and_prefix(auth, monitor, selected_cases)
        locked_registered = locked_snapshot.get("registered_workers", {})
        if not isinstance(locked_registered, dict):
            raise Stop("registered runtime bindings malformed under launch lock")
        locked_workers, locked_cases, locked_images, _ = select_eligible_prefix(locked_registered, health_workers, cases, images)
        if locked_workers != workers or locked_cases != selected_cases or locked_images != selected_images:
            raise Stop("endpoint eligibility changed before launch")
        if verified_cached_images(selected_images) != cached:
            raise Stop("selected cached image identity changed before launch")
        # execute is the reviewed receipt-writing guardian path; no custom
        # detacher.  A failed prewarm therefore yields zero paid supervisors.
        result = execute_staged(monitor, executor, auth, args.state_dir, gate, workers)
        print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("final_rolling_wave: NOT_READY: " + str(error), file=sys.stderr)
        raise SystemExit(2)
