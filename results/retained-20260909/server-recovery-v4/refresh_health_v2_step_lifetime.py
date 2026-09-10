#!/usr/bin/env python3
"""Read-only health refresh for the twenty prepared production workers.

The worker-pool manifest is the binding source for declared worker IDs,
relay ports, model expectations, and Slurm jobs.  This command sends only
GET /health and GET /v1/models requests, then makes one squeue request for
those binding jobs.  It never sends inference, launches/authenticates work,
or reads quota/storage state.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


PROD = Path(__file__).resolve().parent
DEFAULT_WORKER_POOL = PROD / "worker-pool-manifest.json"
DEFAULT_PLAN = PROD / "frozen-plan.jsonl"
DEFAULT_OUTPUT = PROD / "health-preflight-v2"
DEFAULT_SSH_CONTROL = "/tmp/jriverah3-pace-fresh.sock"
DEFAULT_SSH_TARGET = "jriverah3@128.61.254.151"
MINIMUM_REMAINING_SECONDS = 6000
WORKER_RE = re.compile(r"worker-[0-9]{2}\Z")


def sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_write(path: Path, value: bytes, mode: int = 0o600) -> str:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, mode)
    os.replace(temporary, path)
    return sha_bytes(value)


def duration_seconds(value: str) -> int | None:
    value = value.strip()
    if not value or value.upper() in {"UNLIMITED", "INFINITE", "N/A", "UNKNOWN"}:
        return None
    days = 0
    if "-" in value:
        day_part, value = value.split("-", 1)
        if not day_part.isdigit():
            return None
        days = int(day_part)
    parts = value.split(":")
    if not parts or not all(part.isdigit() for part in parts):
        return None
    numbers = [int(part) for part in parts]
    if len(numbers) == 3:
        hours, minutes, seconds = numbers
    elif len(numbers) == 2:
        hours, minutes, seconds = 0, numbers[0], numbers[1]
    elif len(numbers) == 1:
        hours, minutes, seconds = 0, 0, numbers[0]
    else:
        return None
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def get_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def load_workers(worker_pool: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = get_json(worker_pool)
    prepared = manifest.get("prepared_worker_ids")
    rows = manifest.get("workers")
    if not isinstance(prepared, list) or not isinstance(rows, list) or len(rows) != 20:
        raise ValueError("worker-pool manifest must contain exactly 20 prepared rows")
    if len(prepared) != 20 or len(set(prepared)) != 20:
        raise ValueError("worker-pool prepared IDs must contain exactly 20 unique IDs")
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("worker-pool row is not an object")
        worker_id = row.get("worker_id")
        endpoint = row.get("endpoint")
        runtime_descriptor = row.get("runtime")
        if not isinstance(worker_id, str) or not WORKER_RE.fullmatch(worker_id):
            raise ValueError("worker-pool declared worker ID is malformed")
        if worker_id in by_id or worker_id not in prepared:
            raise ValueError("worker-pool declared worker IDs do not match prepared IDs")
        if not isinstance(endpoint, dict) or not isinstance(runtime_descriptor, dict):
            raise ValueError(f"worker-pool metadata missing for {worker_id}")
        relay_port = endpoint.get("relay_port")
        job_id = endpoint.get("job_id")
        if not isinstance(relay_port, int) or not 1 <= relay_port <= 65535:
            raise ValueError(f"relay port missing for {worker_id}")
        if not isinstance(job_id, str) or not re.fullmatch(r"[0-9]+", job_id):
            raise ValueError(f"Slurm job missing for {worker_id}")
        runtime_path = runtime_descriptor.get("path")
        if not isinstance(runtime_path, str) or not Path(runtime_path).is_file():
            raise ValueError(f"clean runtime metadata missing for {worker_id}")
        by_id[worker_id] = row
    if set(by_id) != set(prepared):
        raise ValueError("worker-pool rows do not cover prepared IDs")
    return manifest, [by_id[worker_id] for worker_id in prepared]


def load_runtime(row: dict[str, Any]) -> dict[str, Any]:
    worker_id = row["worker_id"]
    runtime_path = Path(row["runtime"]["path"])
    runtime = get_json(runtime_path)
    runner = runtime.get("runner", {})
    cpu_policy = runner.get("cpu_policy", {})
    telemetry = runner.get("telemetry", {})
    metrics = telemetry.get("serving_metrics", {})
    if not isinstance(cpu_policy, dict) or not isinstance(metrics, dict):
        raise ValueError(f"clean runtime metadata malformed for {worker_id}")
    return {
        "path": str(runtime_path.resolve()),
        "sha256": sha_file(runtime_path),
        "schema_version": runtime.get("schema_version"),
        "runtime_epoch": metrics.get("counter_epoch"),
        "server_identity": metrics.get("server_identity"),
        "cpu_worker_id": cpu_policy.get("worker_id"),
        "worker_cpuset": cpu_policy.get("worker_cpuset"),
        "control_cpuset": cpu_policy.get("control_cpuset"),
        "model_name": runtime.get("model", {}).get("name"),
    }


def http_get(port: int, path: str) -> dict[str, Any]:
    url = f"http://127.0.0.1:{port}{path}"
    request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    started = time.monotonic()
    status: int | None = None
    headers: dict[str, str] = {}
    body = b""
    error: str | None = None
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=10) as response:
            status = int(response.status)
            headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
            body = response.read()
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        headers = {str(key).lower(): str(value) for key, value in (exc.headers or {}).items()}
        body = exc.read()
        error = f"HTTPError:{exc.code}"
    except Exception as exc:  # endpoint-level failure remains a worker FAIL
        error = f"{type(exc).__name__}:{exc}"
    return {
        "url": url,
        "method": "GET",
        "status": status,
        "headers": headers,
        "body": body,
        "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
        "error": error,
    }


def save_probe(result: dict[str, Any], path: Path) -> dict[str, Any]:
    body = result.pop("body")
    body_path = path.with_suffix(".body")
    headers_path = path.with_suffix(".headers.txt")
    body_sha = atomic_write(body_path, body)
    header_text = "".join(f"{key}: {value}\n" for key, value in sorted(result["headers"].items()))
    headers_sha = atomic_write(headers_path, header_text.encode("utf-8"))
    result.update(
        {
            "body_bytes": len(body),
            "body_sha256": body_sha,
            "raw_body_path": str(body_path.resolve()),
            "raw_headers_path": str(headers_path.resolve()),
            "raw_headers_sha256": headers_sha,
        }
    )
    return result


def query_slurm(
    jobs: list[str], ssh_control: str, ssh_target: str, raw_dir: Path
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    job_csv = ",".join(jobs)
    remote = f"squeue -h -j {job_csv} -o '%i|%T|%M|%l|%L|%N'"
    command = [
        "ssh",
        "-F",
        "/dev/null",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-S",
        ssh_control,
        ssh_target,
        remote,
    ]
    completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False)
    stdout_path = raw_dir / "slurm-squeue.raw.txt"
    stderr_path = raw_dir / "slurm-squeue.stderr.txt"
    stdout_sha = atomic_write(stdout_path, completed.stdout)
    stderr_sha = atomic_write(stderr_path, completed.stderr)
    rows: dict[str, dict[str, Any]] = {}
    for line in completed.stdout.decode("utf-8", "replace").splitlines():
        fields = line.strip().split("|")
        if len(fields) != 6:
            continue
        job_id, state, elapsed, time_limit, timeleft, node = fields
        rows[job_id] = {
            "job_id": job_id,
            "state": state,
            "elapsed": elapsed,
            "time_limit": time_limit,
            "timeleft": timeleft,
            "remaining_seconds": duration_seconds(timeleft),
            "node": node,
        }
    # Bound launch lifetime by every current numeric Slurm step in the allocation.
    # This conservatively covers both the server and the relay, including steps
    # whose limit is shorter than the enclosing allocation. No host clock join.
    step_command = command[:-1] + [f"squeue --steps -h -j {job_csv} -o '%i|%M|%l|%N'"]
    step_result = subprocess.run(step_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False)
    step_path = raw_dir / "slurm-steps.raw.txt"
    step_sha = atomic_write(step_path, step_result.stdout)
    step_err_path = raw_dir / "slurm-steps.stderr.txt"
    step_err_sha = atomic_write(step_err_path, step_result.stderr)
    step_rows = {job: [] for job in jobs}
    invalid_jobs = set()
    for line in step_result.stdout.decode("utf-8", "replace").splitlines():
        fields = line.strip().split("|")
        if len(fields) != 4:
            invalid_jobs.update(jobs)
            continue
        step_id, elapsed, limit, node = fields
        job_id, _, suffix = step_id.partition(".")
        if job_id not in step_rows or suffix in {"batch", "extern"}:
            continue
        elapsed_s = duration_seconds(elapsed)
        limit_s = duration_seconds(limit) if limit != "UNLIMITED" else None
        valid = suffix.isdigit() and isinstance(elapsed_s, int) and (limit == "UNLIMITED" or isinstance(limit_s, int))
        if not valid:
            invalid_jobs.add(job_id)
        step_rows[job_id].append({"step_id": step_id, "elapsed": elapsed, "time_limit": limit, "node": node,
            "remaining_seconds": max(0, limit_s - elapsed_s) if valid and limit_s is not None else None,
            "unlimited": limit == "UNLIMITED", "valid": valid})
    for job_id, row in rows.items():
        observed = step_rows.get(job_id, [])
        valid = step_result.returncode == 0 and bool(observed) and job_id not in invalid_jobs
        allocation_remaining = row["remaining_seconds"]
        finite = [x["remaining_seconds"] for x in observed if x["remaining_seconds"] is not None]
        row["allocation_remaining_seconds"] = allocation_remaining
        row["steps"] = observed
        row["step_lifetime_valid"] = valid
        row["remaining_seconds"] = min([allocation_remaining] + finite) if valid and isinstance(allocation_remaining, int) else None
    return rows, {
        "step_command": step_command,
        "step_returncode": step_result.returncode,
        "step_raw_stdout_path": str(step_path.resolve()),
        "step_raw_stdout_sha256": step_sha,
        "step_raw_stderr_path": str(step_err_path.resolve()),
        "step_raw_stderr_sha256": step_err_sha,
        "command": command,
        "returncode": completed.returncode,
        "raw_stdout_path": str(stdout_path.resolve()),
        "raw_stdout_sha256": stdout_sha,
        "raw_stderr_path": str(stderr_path.resolve()),
        "raw_stderr_sha256": stderr_sha,
        "requested_job_ids": jobs,
        "parsed_rows": len(rows),
    }


def make_worker_row(
    row: dict[str, Any], runtime: dict[str, Any], probes: dict[tuple[str, str], dict[str, Any]], slurm: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    worker_id = row["worker_id"]
    endpoint = row["endpoint"]
    health = probes[(worker_id, "/health")]
    models = probes[(worker_id, "/v1/models")]
    expected_model = endpoint.get("served_model")
    expected_max_len = endpoint.get("max_model_len")
    model_id: str | None = None
    max_model_len: int | None = None
    model_error = models.get("error")
    try:
        payload = json.loads(Path(models["raw_body_path"]).read_text())
        entries = payload.get("data", []) if isinstance(payload, dict) else []
        match = next((entry for entry in entries if isinstance(entry, dict) and entry.get("id") == expected_model), None)
        if match is not None:
            model_id = match.get("id")
            max_model_len = match.get("max_model_len")
        if not isinstance(payload, dict) or not isinstance(entries, list):
            model_error = model_error or "models_payload_missing_data"
    except Exception as exc:
        model_error = model_error or f"{type(exc).__name__}:{exc}"
    job = slurm.get(str(endpoint["job_id"]))
    remaining = job.get("remaining_seconds") if job else None
    checks = {
        "actual_healthy": health.get("status") == 200,
        "models_status_200": models.get("status") == 200,
        "models_65536": models.get("status") == 200 and model_id == expected_model and max_model_len == expected_max_len == 65536,
        "job_running": bool(job and job.get("state") == "RUNNING"),
        "bound_serving_step_present": not endpoint.get("serving_step_id") or bool(job and any(x.get("step_id") == endpoint["serving_step_id"] for x in job.get("steps", []))),
        "remaining_seconds_numeric": isinstance(remaining, int) and not isinstance(remaining, bool) and remaining > 0,
    }
    status = "PASS" if all(checks.values()) else "FAIL"
    reasons = [name for name, passed in checks.items() if not passed]
    if model_error:
        reasons.append("models_payload_error")
    launch_eligible = status == "PASS" and isinstance(remaining, int) and remaining >= MINIMUM_REMAINING_SECONDS
    if status == "PASS" and not launch_eligible:
        reasons.append("remaining_seconds_below_6000_required_launch")
    return {
        "worker_id": worker_id,
        "declared_production_worker_id": worker_id,
        "relay_table_id": endpoint.get("endpoint_id"),
        "relay_port": endpoint.get("relay_port"),
        "server_port": endpoint.get("server_port"),
        "job_id": str(endpoint["job_id"]),
        "hostname": endpoint.get("hostname"),
        "gpu_uuid": endpoint.get("gpu_uuid"),
        "runtime_epoch": runtime.get("runtime_epoch"),
        "runtime": runtime,
        "remaining_seconds": remaining,
        "status": status,
        "launch_eligible": launch_eligible,
        "required_launch_min_remaining_seconds": MINIMUM_REMAINING_SECONDS,
        "failure_reasons": reasons,
        "health": {
            "status": health.get("status"),
            "actual_healthy": checks["actual_healthy"],
            "elapsed_ms": health.get("elapsed_ms"),
            "error": health.get("error"),
            "body_bytes": health.get("body_bytes"),
            "body_sha256": health.get("body_sha256"),
            "raw_body_path": health.get("raw_body_path"),
            "raw_headers_path": health.get("raw_headers_path"),
        },
        "models": {
            "status": models.get("status"),
            "model_id": model_id,
            "expected_model_id": expected_model,
            "max_model_len": max_model_len,
            "expected_max_model_len": expected_max_len,
            "models_65536": checks["models_65536"],
            "elapsed_ms": models.get("elapsed_ms"),
            "error": model_error,
            "body_bytes": models.get("body_bytes"),
            "body_sha256": models.get("body_sha256"),
            "raw_body_path": models.get("raw_body_path"),
            "raw_headers_path": models.get("raw_headers_path"),
        },
        "slurm": {
            "state": job.get("state") if job else None,
            "elapsed": job.get("elapsed") if job else None,
            "time_limit": job.get("time_limit") if job else None,
            "timeleft": job.get("timeleft") if job else None,
            "remaining_seconds": remaining,
            "node": job.get("node") if job else None,
            "job_running": checks["job_running"],
            "remaining_seconds_numeric": checks["remaining_seconds_numeric"],
        },
    }


def run(args: argparse.Namespace) -> Path:
    worker_pool = args.worker_pool.resolve()
    plan = args.plan.resolve()
    output = args.output_dir.resolve()
    manifest, rows = load_workers(worker_pool)
    plan_sha = sha_file(plan)
    start_ns = time.time_ns()
    raw_dir = output / "raw" / f"refresh-{start_ns}"
    raw_http_dir = raw_dir / "http"
    raw_http_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    runtimes = {row["worker_id"]: load_runtime(row) for row in rows}

    probes: dict[tuple[str, str], dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        futures = {
            pool.submit(http_get, int(row["endpoint"]["relay_port"]), path): (row["worker_id"], path)
            for row in rows
            for path in ("/health", "/v1/models")
        }
        for future in concurrent.futures.as_completed(futures):
            worker_id, path = futures[future]
            probes[(worker_id, path)] = future.result()
    for (worker_id, path), probe in probes.items():
        suffix = "health" if path == "/health" else "models"
        probes[(worker_id, path)] = save_probe(probe, raw_http_dir / f"{worker_id}-{suffix}")

    jobs = [str(row["endpoint"]["job_id"]) for row in rows]
    slurm_rows, slurm_receipt = query_slurm(jobs, args.ssh_control, args.ssh_target, raw_dir)
    workers = [make_worker_row(row, runtimes[row["worker_id"]], probes, slurm_rows) for row in rows]
    healthy_count = sum(worker["status"] == "PASS" for worker in workers)
    remaining_values = [worker["remaining_seconds"] for worker in workers if isinstance(worker["remaining_seconds"], int)]
    min_remaining = min(remaining_values) if remaining_values else None
    all_worker_status_pass = healthy_count == len(workers)
    collection_errors = []
    if slurm_receipt["returncode"] != 0:
        collection_errors.append("squeue_returncode_nonzero")
    if len(probes) != 40:
        collection_errors.append("http_probe_collection_incomplete")
    if sha_file(plan) != plan_sha:
        collection_errors.append("frozen_plan_changed_during_probe")
    captured_epoch = time.time()
    generator = Path(__file__).resolve()
    receipt = {
        "schema": "astra.final-production.health-refresh.v1",
        "status": "PASS" if not collection_errors else "FAIL",
        "collection_valid": not collection_errors,
        "collection_errors": collection_errors,
        "captured_epoch": captured_epoch,
        "receipt_captured_epoch": captured_epoch,
        "captured_epoch_ns": int(captured_epoch * 1_000_000_000),
        "observed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "plan_sha256": plan_sha,
        "no_inference": True,
        "no_mutation": True,
        "probe_method": "GET /health and GET /v1/models only; one squeue query; no inference/launch/auth",
        "endpoint_count": len(workers),
        "healthy_count": healthy_count,
        "all_worker_status_pass": all_worker_status_pass,
        "min_remaining_seconds": min_remaining,
        "required_launch_min_remaining_seconds": MINIMUM_REMAINING_SECONDS,
        "launch_ready": all(worker["launch_eligible"] for worker in workers),
        "generator": {"path": str(generator), "sha256": sha_file(generator)},
        "worker_pool": {"path": str(worker_pool), "sha256": sha_file(worker_pool), "prepared_worker_ids": manifest.get("prepared_worker_ids")},
        "slurm": slurm_receipt,
        "workers": workers,
    }
    receipt_name = args.receipt_name
    receipt_path = output / receipt_name
    receipt_sha = atomic_write(receipt_path, (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    atomic_write(Path(str(receipt_path) + ".sha256"), f"{receipt_sha}  {receipt_path.name}\n".encode("utf-8"))
    print(json.dumps({
        "status": receipt["status"],
        "receipt_path": str(receipt_path),
        "receipt_sha256": receipt_sha,
        "healthy_count": healthy_count,
        "all_worker_status_pass": all_worker_status_pass,
        "min_remaining_seconds": min_remaining,
        "launch_ready": receipt["launch_ready"],
        "raw_dir": str(raw_dir),
    }, sort_keys=True))
    return receipt_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-pool", type=Path, default=DEFAULT_WORKER_POOL)
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--receipt-name", default="refresh-health-v1.json")
    parser.add_argument("--ssh-control", default=DEFAULT_SSH_CONTROL)
    parser.add_argument("--ssh-target", default=DEFAULT_SSH_TARGET)
    args = parser.parse_args()
    run(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
