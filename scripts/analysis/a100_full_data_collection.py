#!/usr/bin/env python3
"""Collect and validate a separate, resumable A100 SWE-bench data pass.

This entrypoint deliberately lives outside the sealed final-validation driver.
It consumes only the pinned population manifest and pinned dataset rows, writes
to an explicitly supplied external artifact root, and fails closed whenever a
request cannot be matched to direct Nsight CPU/CUDA activity.

The live ``run`` command is serialized.  ``prepare`` and ``aggregate`` are
offline and deterministic, which makes them safe to rehearse in CI.  Raw
traces, model files, evaluator images, and task outputs are never copied into
Git by this module.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import http.client
import json
import math
import os
import signal
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
# This module is also invoked as ``python scripts/analysis/...py`` by the
# resumable runner.  Keep repository-local production providers importable in
# that mode without changing the caller's environment.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
POPULATION = ROOT / "project" / "h100_results" / "population_runs.csv"
FIRST_EXPERIMENT = ROOT / "cloud" / "lambda" / "first_experiment.yaml"
TRACE_PROVIDER = ROOT / "scripts" / "cloud" / "a100_nsight_trace_provider.py"
REQUEST_PROXY = ROOT / "scripts" / "observability" / "request_proxy.py"
PINNED_MODEL = "Qwen/Qwen3-Coder-30B-A3B-Instruct"
PINNED_MODEL_REVISION = "b2cff646eb4bb1d68355c01b18ae02e7cf42d120"
PINNED_TOKENIZER_REVISION = PINNED_MODEL_REVISION
PINNED_SWE_AGENT = "0f3acafacabc0def8cc76b4e48acb4b6cf302cb9"
PINNED_SWE_BENCH = "726c5461e2ef52d83cf1ea2107870a8bb3328d57"
PINNED_LITE_REVISION = "69611d31007e1c6731db8bd5b5c3f2d33f5bab6e"
PINNED_VERIFIED_REVISION = "91aa3ed51b709be6457e12d00300a6a596d4c6a3"
PINNED_VLLM_IMAGE = (
    "vllm/vllm-openai:v0.10.0@sha256:"
    "05a31dc4185b042e91f4d2183689ac8a87bd845713d5c3f987563c5899878271"
)
PINNED_CONFIG = "cloud/lambda/sweagent_request.yaml"
DEFAULT_ROOT = Path("/mnt/eic-work/a100_full_data_collection")
REQUEST_SCHEMA = "a100-full-request.v1"
TASK_SCHEMA = "a100-full-task.v1"
MANIFEST_SCHEMA = "a100-full-profiling-manifest.v1"
RATIO_FORMULA = "cpu_activity_union_ms / cuda_activity_union_ms"
PHASE_RATIO_FORMULA = "sum(tool_call_wall_ms) / sum(model_request_wall_ms)"


class CollectionError(RuntimeError):
    """A collection contract failed closed."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def dump_json(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def write_immutable(path: Path, value: Any) -> str:
    payload = dump_json(value) if not isinstance(value, bytes) else value
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise CollectionError(f"refusing to overwrite immutable artifact: {path}")
    else:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_bytes(payload)
        temporary.replace(path)
    return sha256_file(path)


def atomic_json(path: Path, value: Any) -> str:
    return write_immutable(path, value)


def replace_json(path: Path, value: Any) -> str:
    """Replace a mutable checkpoint atomically.

    Sealed manifests and terminal rows use ``atomic_json``.  Resumable state
    and derived summaries are intentionally mutable and use this separate
    helper so a later checkpoint never rewrites a sealed measurement.
    """

    payload = dump_json(value) if not isinstance(value, bytes) else value
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)
    return sha256_file(path)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CollectionError(f"invalid JSON artifact {path}: {exc}") from exc


def append_jsonl(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    with path.open("ab") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def write_csv(path: Path, rows: list[Mapping[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def manifest_value(path: Path, key: str, default: str = "") -> str:
    if not path.is_file():
        return default
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0]
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name == key:
            return value
    return default


def population_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    required = {"suite", "repository", "instance_id", "status", "official_resolved", "source_file", "source_sha256"}
    if not rows or not required.issubset(rows[0]):
        raise CollectionError(f"population manifest lacks required columns: {path}")
    return rows


def selected_tasks(rows: Iterable[Mapping[str, str]]) -> list[dict[str, Any]]:
    """Select one deterministic population row per suite/repository.

    Historical status is copied as selection provenance only.  The new A100
    official label is populated solely from the evaluator that runs below.
    """

    candidates: dict[tuple[str, str], list[Mapping[str, str]]] = {}
    for row in rows:
        suite = str(row["suite"]).lower()
        if suite not in {"lite", "verified"}:
            continue
        key = (suite, str(row["repository"]))
        candidates.setdefault(key, []).append(row)
    result: list[dict[str, Any]] = []
    for (suite, repository), values in sorted(candidates.items()):
        chosen = sorted(values, key=lambda item: (item["instance_id"], item["source_file"]))[0]
        result.append(
            {
                "suite": suite,
                "repository": repository,
                "instance_id": chosen["instance_id"],
                "population_task_status": chosen["status"],
                "population_official_resolved": chosen["official_resolved"].lower() == "true",
                "population_source_file": chosen["source_file"],
                "population_source_sha256": chosen["source_sha256"],
                "selection_provenance": "one_deterministic_row_per_suite_repository",
            }
        )
    return result


def prepare(args: argparse.Namespace) -> int:
    population = args.population.resolve()
    output = args.output_root.resolve()
    rows = population_rows(population)
    tasks = selected_tasks(rows)
    manifest = {
        "schema_version": MANIFEST_SCHEMA,
        "provenance": "planned_from_pinned_population_only",
        "deterministic": True,
        "source": {
            "population_path": str(population),
            "population_sha256": sha256_file(population),
            "first_experiment_path": str(FIRST_EXPERIMENT),
            "first_experiment_sha256": sha256_file(FIRST_EXPERIMENT),
        },
        "pinned_software": {
            "model": PINNED_MODEL,
            "model_revision": PINNED_MODEL_REVISION,
            "swe_agent_revision": PINNED_SWE_AGENT,
            "swe_bench_revision": PINNED_SWE_BENCH,
            "vllm_image": PINNED_VLLM_IMAGE,
            "lite_dataset_revision": PINNED_LITE_REVISION,
            "verified_dataset_revision": PINNED_VERIFIED_REVISION,
            "request_config": PINNED_CONFIG,
        },
        "hardware_scope": "A100 80GB only; no H100 work",
        "request_protocol": {
            "concurrency": 1,
            "temperature": 0.0,
            "seed": 0,
            "max_calls": 30,
            "max_input_tokens": 32768,
            "max_output_tokens": 2048,
            "max_observation_length": 100000,
            "warmups_per_task": 1,
            "measured_repeats": ["r01", "r02", "r03"],
        },
        "direct_timing": {
            "provider": "production_a100_nsight_trace_provider",
            "clock_id": "CLOCK_MONOTONIC_RAW",
            "cpu_definition": "union of direct target-process OSRT/CUPTI runtime intervals clipped to request window",
            "cuda_definition": "union of direct target-process CUDA kernel/memcpy/memset intervals clipped to request window",
            "kernel_definition": "sum of target-process kernel intervals retained as a secondary diagnostic",
            "diagnostic_ratio": RATIO_FORMULA,
            "reject": ["missing", "zero", "mixed-clock", "cross-host", "non-direct"],
            "gpu_utilization_is_latency": False,
            "nvml_is_latency": False,
            "kernel_sum_is_latency": False,
        },
        "assignment_metric": {
            "phase_ratio": PHASE_RATIO_FORMULA,
            "source": "SWE-agent trajectory tool execution_time and request_proxy model boundaries",
        },
        "hyperparameter_axes": {
            "max_calls": [10, 20, 30, 50],
            "max_output_tokens": [512, 1024, 2048, 4096],
            "max_observation_length": [10000, 25000, 50000, 100000],
            "temperature": [0.0, 0.2, 0.5, 0.8],
        },
        "tasks": tasks,
        "task_count": len(tasks),
        "selection_counts": {
            "lite": sum(item["suite"] == "lite" for item in tasks),
            "verified": sum(item["suite"] == "verified" for item in tasks),
            "repositories": len({item["repository"] for item in tasks}),
        },
    }
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / "profiling_manifest.json"
    digest = atomic_json(manifest_path, manifest)
    (output / "profiling_manifest.sha256").write_text(f"{digest}  profiling_manifest.json\n", encoding="utf-8")
    state = {
        "schema_version": "a100-full-run-state.v1",
        "status": "planned",
        "manifest_sha256": digest,
        "completed": [],
        "unavailable": [],
        "started_at_utc": None,
        "deadline_epoch": None,
    }
    replace_json(output / "run_state.json", state)
    print(json.dumps({"manifest": str(manifest_path), "manifest_sha256": digest, "tasks": len(tasks)}, sort_keys=True))
    return 0


def load_manifest(path: Path) -> dict[str, Any]:
    value = read_json(path)
    if value.get("schema_version") != MANIFEST_SCHEMA or value.get("provenance") != "planned_from_pinned_population_only":
        raise CollectionError("profiling manifest is not the pinned deterministic schema")
    if value.get("request_protocol", {}).get("concurrency") != 1:
        raise CollectionError("profiling concurrency must remain one")
    if value.get("assignment_metric", {}).get("phase_ratio") != PHASE_RATIO_FORMULA:
        raise CollectionError("assignment phase-ratio formula is not declared")
    tasks = value.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise CollectionError("profiling manifest has no selected tasks")
    return value


def load_task(dataset: Path, instance_id: str) -> dict[str, Any]:
    value = read_json(dataset)
    rows = value if isinstance(value, list) else value.get("data") if isinstance(value, dict) else None
    if not isinstance(rows, list):
        raise CollectionError(f"dataset asset is not a JSON row list: {dataset}")
    matches = [row for row in rows if isinstance(row, Mapping) and row.get("instance_id") == instance_id]
    if len(matches) != 1:
        raise CollectionError(f"dataset asset does not contain exactly one selected row: {instance_id}")
    return dict(matches[0])


def evaluator_image(instance_id: str) -> str:
    return f"swebench/sweb.eval.x86_64.{instance_id.replace('__', '_1776_')}:latest".lower()


def runtime_row(source: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(source)
    expected = evaluator_image(str(row.get("instance_id", "")))
    if row.get("image_name") not in (None, expected):
        raise CollectionError(f"dataset image_name conflicts for {row.get('instance_id')}")
    row["image_name"] = expected
    row["repo_name"] = "testbed"
    return row


def clock_metadata() -> dict[str, Any]:
    from agentic_sim.telemetry.clock import clock_metadata as get_metadata

    return dict(get_metadata())


def monotonic_ns() -> int:
    from agentic_sim.telemetry.clock import monotonic_ns as get_mono

    return int(get_mono())


def run_limited(argv: list[str], *, cwd: Path, env: Mapping[str, str], log: Path, timeout_seconds: int) -> tuple[int, bool]:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8") as stream:
        process = subprocess.Popen(argv, cwd=str(cwd), env=dict(env), stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            return process.wait(timeout=timeout_seconds), False
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            return 124, True


def provider_command(*, action: str, output_dir: Path, repeat_id: str, start_ns: int, end_ns: int) -> list[str]:
    return [
        sys.executable,
        str(TRACE_PROVIDER),
        "--config",
        str(ROOT / "configs" / "a100_final_validation.json"),
        "--case-id",
        f"repository-profile-{output_dir.name}",
        "--split",
        "profiling",
        "--input-tokens",
        "1",
        "--output-tokens",
        "1",
        "--repeat-id",
        repeat_id,
        "--phase",
        "full-data-collection",
        "--action",
        action,
        "--start-mono-ns",
        str(start_ns),
        "--end-mono-ns",
        str(end_ns),
        "--output-dir",
        str(output_dir),
    ]


def proxy_command(events: Path, port: int) -> list[str]:
    return [
        sys.executable,
        str(REQUEST_PROXY),
        "--listen-host",
        "127.0.0.1",
        "--listen-port",
        str(port),
        "--upstream-host",
        "127.0.0.1",
        "--upstream-port",
        "8000",
        "--events",
        str(events),
    ]


def command_base(*, sweagent: str, swe_root: Path, request_config: Path, instances: Path, output: Path, api_base: str, settings: Mapping[str, Any]) -> list[str]:
    return [
        sweagent,
        "run-batch",
        "--config",
        str(swe_root / "config" / "default.yaml"),
        "--config",
        str(request_config),
        "--instances.type",
        "file",
        "--instances.path",
        str(instances),
        "--instances.filter",
        f"^{settings['instance_id']}$",
        "--agent.model.name",
        f"openai/{PINNED_MODEL}",
        "--agent.model.api_base",
        api_base,
        "--agent.model.api_key",
        "local-only-placeholder",
        "--agent.model.total_cost_limit",
        "0",
        "--agent.model.per_instance_cost_limit",
        "0",
        "--agent.model.per_instance_call_limit",
        str(settings["max_calls"]),
        "--agent.model.temperature",
        str(settings["temperature"]),
        "--agent.model.max_input_tokens",
        "32768",
        "--agent.model.max_output_tokens",
        str(settings["max_output_tokens"]),
        "--agent.templates.max_observation_length",
        str(settings["max_observation_length"]),
        "--output_dir",
        str(output),
        "--num_workers",
        "1",
    ]


def warmup_request(*, base_url: str, model: str, log: Path) -> tuple[str, str | None]:
    """Warm the pinned endpoint without treating the warmup as a measurement."""

    body = json.dumps({"model": model, "messages": [{"role": "user", "content": "A100 profiling warmup"}], "max_tokens": 1, "temperature": 0.0, "seed": 0}).encode()
    parsed = urlsplit(base_url)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=180)
    try:
        started = time.time_ns()
        connection.request("POST", "/v1/chat/completions", body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        payload = response.read()
        ended = time.time_ns()
        log.write_text(json.dumps({"status": response.status, "duration_ms": (ended - started) / 1e6, "response_sha256": sha256_bytes(payload)}, indent=2) + "\n", encoding="utf-8")
        return ("completed" if response.status == 200 else "unavailable", None if response.status == 200 else f"http_{response.status}")
    except (OSError, http.client.HTTPException) as exc:
        log.write_text(json.dumps({"status": "unavailable", "reason": type(exc).__name__}, indent=2) + "\n", encoding="utf-8")
        return "unavailable", type(exc).__name__
    finally:
        connection.close()


def read_proxy_events(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if isinstance(value, dict) and value.get("event_type") == "model_request_boundary":
            events.append(value)
    return events


def stop_process(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=10)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            process.kill()
        except ProcessLookupError:
            pass


def direct_trace_for_event(trace_dir: Path, event: Mapping[str, Any], arm: Mapping[str, Any]) -> dict[str, Any]:
    """Reuse the production SQLite parser for one proxy request window."""

    os.environ["HARDWARE_TARGET"] = "A100"
    os.environ["HARDWARE_TRACE_SCHEMA"] = "a100-trace-summary.v1"
    os.environ["HARDWARE_TRACE_PROVIDER_VERSION"] = "a100-nsight-trace-provider.v1"
    from scripts.cloud.h100_nsight_trace_provider import ProviderError, _parse_report, _trace_paths

    clock = event.get("clock")
    if not isinstance(clock, Mapping):
        raise CollectionError("request has no clock identity")
    expected = clock_metadata()
    for key in ("clock_id", "hostname", "boot_id"):
        if clock.get(key) != expected.get(key):
            raise CollectionError(f"request clock identity mismatch: {key}")
    if clock.get("clock_id") != "CLOCK_MONOTONIC_RAW":
        raise CollectionError("request clock is not CLOCK_MONOTONIC_RAW")
    args = SimpleNamespace(
        start_mono_ns=int(event["start_mono_ns"]),
        end_mono_ns=int(event["end_mono_ns"]),
    )
    try:
        result = _parse_report(args, _trace_paths(trace_dir), arm)
    except ProviderError as exc:
        raise CollectionError(str(exc)) from exc
    if float(result.get("cpu_activity_union_ms", 0.0)) <= 0:
        raise CollectionError("direct CPU union is zero")
    if float(result.get("cuda_activity_union_ms", 0.0)) <= 0:
        raise CollectionError("direct CUDA union is zero")
    ratio = float(result["cpu_activity_union_ms"]) / float(result["cuda_activity_union_ms"])
    if not math.isfinite(ratio) or ratio <= 0:
        raise CollectionError("CPU:GPU ratio is not finite and positive")
    result["cpu_to_gpu_ratio"] = ratio
    result["ratio_formula"] = RATIO_FORMULA
    result["request_id"] = event.get("request_id")
    result["request_window"] = {
        "start_mono_ns": int(event["start_mono_ns"]),
        "end_mono_ns": int(event["end_mono_ns"]),
    }
    return result


def direct_traces_for_events(trace_dir: Path, events: list[Mapping[str, Any]], arm: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Parse all valid request windows in one production-provider SQLite scan."""

    os.environ["HARDWARE_TARGET"] = "A100"
    os.environ["HARDWARE_TRACE_SCHEMA"] = "a100-trace-summary.v1"
    os.environ["HARDWARE_TRACE_PROVIDER_VERSION"] = "a100-nsight-trace-provider.v1"
    from scripts.cloud.h100_nsight_trace_provider import ProviderError, _trace_paths, parse_report_windows

    expected = clock_metadata()
    results: list[dict[str, Any]] = [{"error": "unavailable_before_batch_parse"} for _ in events]
    valid_indices: list[int] = []
    valid_events: list[Mapping[str, Any]] = []
    for index, event in enumerate(events):
        clock = event.get("clock")
        if not isinstance(clock, Mapping):
            results[index] = {"error": "request has no clock identity"}
            continue
        for key in ("clock_id", "hostname", "boot_id"):
            if clock.get(key) != expected.get(key):
                results[index] = {"error": f"request clock identity mismatch: {key}"}
                break
        else:
            if clock.get("clock_id") != "CLOCK_MONOTONIC_RAW":
                results[index] = {"error": "request clock is not CLOCK_MONOTONIC_RAW"}
            elif event.get("status_code") != 200:
                results[index] = {"error": f"request_http_{event.get('status_code')}"}
            elif not isinstance(event.get("prompt_tokens"), int) or not isinstance(event.get("completion_tokens"), int):
                results[index] = {"error": "missing_token_usage"}
            else:
                valid_indices.append(index)
                valid_events.append(event)
    if not valid_events:
        return results
    try:
        parsed = parse_report_windows(valid_events, _trace_paths(trace_dir), arm)
    except ProviderError as exc:
        for index in valid_indices:
            results[index] = {"error": str(exc)}
        return results
    if len(parsed) != len(valid_indices):
        for index in valid_indices:
            results[index] = {"error": "batch_trace_result_count_mismatch"}
        return results
    for index, event, direct in zip(valid_indices, valid_events, parsed):
        if "error" in direct:
            results[index] = direct
            continue
        if float(direct.get("cpu_activity_union_ms", 0.0)) <= 0:
            results[index] = {"error": "direct CPU union is zero"}
            continue
        if float(direct.get("cuda_activity_union_ms", 0.0)) <= 0:
            results[index] = {"error": "direct CUDA union is zero"}
            continue
        ratio = float(direct["cpu_activity_union_ms"]) / float(direct["cuda_activity_union_ms"])
        if not math.isfinite(ratio) or ratio <= 0:
            results[index] = {"error": "CPU:GPU ratio is not finite and positive"}
            continue
        direct["cpu_to_gpu_ratio"] = ratio
        direct["ratio_formula"] = RATIO_FORMULA
        direct["request_id"] = event.get("request_id")
        direct["request_window"] = {"start_mono_ns": int(event["start_mono_ns"]), "end_mono_ns": int(event["end_mono_ns"])}
        results[index] = direct
    return results


def compact_direct_timing(direct: Mapping[str, Any]) -> dict[str, Any]:
    """Keep request rows compact; full interval evidence remains in raw traces."""

    compact = {key: direct.get(key) for key in ("schema_version", "provenance", "provider_version", "clock_id", "cuda_union_rule", "cpu_activity_union_ms", "cuda_activity_union_ms", "kernel_duration_sum_ms", "raw_artifacts", "ratio_formula", "request_id", "request_window")}
    measurement = direct.get("measurement")
    if isinstance(measurement, Mapping):
        compact["measurement_summary"] = {key: measurement.get(key) for key in ("source", "session_epoch_utc_ns", "raw_minus_realtime_ns", "request_start_mono_ns", "request_end_mono_ns", "target_processes", "cpu_event_counts", "gpu_event_counts", "kernel_event_count")}
    return compact


def classify_tool(action: str) -> str:
    lowered = action.strip().lower()
    if lowered.startswith("str_replace_editor") or "edit" in lowered:
        return "edit"
    if lowered.startswith(("find ", "ls ", "tree ", "pwd", "grep ", "rg ", "cat ", "head ", "tail ")):
        return "traversal" if lowered.startswith(("find ", "ls ", "tree ", "pwd")) else "read"
    if lowered.startswith(("pytest", "python -m pytest", "tox", "make test", "unittest")) or " test" in lowered:
        return "test"
    if lowered.startswith(("rm ", "cp ", "mv ", "mkdir ", "touch ", "chmod ", "git ")):
        return "write"
    if lowered.startswith(("bash", "sh ", "python ", "pip ", "uv ", "git ", "./")):
        return "shell"
    return "other"


def trajectory_path(task_dir: Path) -> Path | None:
    candidates = sorted(task_dir.rglob("*.traj"))
    return candidates[0] if candidates else None


def extract_tool_events(task_dir: Path, task_start_ns: int, model_events: list[Mapping[str, Any]], trajectory_id: str) -> tuple[list[dict[str, Any]], str | None]:
    """Extract SWE-agent's measured tool durations without inventing gaps.

    SWE-agent v1.1.0 records ``execution_time`` for each trajectory action.
    We bind that measured duration to the adjacent request window only when
    the ordering and interval fit are consistent.  A mismatch makes the
    population phase ratio unavailable rather than silently repairing it.
    """

    path = trajectory_path(task_dir)
    if path is None:
        return [], "trajectory_missing"
    try:
        payload = read_json(path)
    except CollectionError as exc:
        return [], str(exc)
    entries = payload.get("trajectory") if isinstance(payload, Mapping) else None
    if not isinstance(entries, list):
        return [], "trajectory_events_missing"
    actions = [entry for entry in entries if isinstance(entry, Mapping) and "action" in entry]
    if len(actions) != len(model_events):
        return [], f"tool_model_event_count_mismatch:{len(actions)}:{len(model_events)}"
    rows: list[dict[str, Any]] = []
    for index, (entry, model) in enumerate(zip(actions, model_events), start=1):
        execution = entry.get("execution_time")
        if isinstance(execution, bool) or not isinstance(execution, (int, float)) or not math.isfinite(float(execution)) or float(execution) < 0:
            return [], f"invalid_tool_execution_time:{index}"
        start = int(model["request_end_mono_ns"])
        duration_ns = int(round(float(execution) * 1_000_000))
        end = start + duration_ns
        next_start = int(model_events[index]["request_start_mono_ns"]) if index < len(model_events) else None
        if next_start is not None and end > next_start:
            return [], f"tool_interval_overlaps_next_model_request:{index}"
        action = str(entry.get("action", ""))
        rows.append({
            "schema_version": "a100-full-tool-event.v1",
            "provenance": "measured_sweagent_execution_time",
            "trajectory_id": trajectory_id,
            "event_id": f"{trajectory_id}:tool:{index:04d}",
            "ordering": index,
            "operation_class": classify_tool(action),
            "action_name": action.split(" ", 1)[0] if action else "unknown",
            "read_write_traversal_shell_edit_test_other": classify_tool(action),
            "start_mono_ns": start,
            "end_mono_ns": end,
            "wall_ms": duration_ns / 1_000_000.0,
            "cpu_time_ms": None,
            "bytes": None,
            "call_count": 1,
            "path_metadata": None,
            "success": True,
            "duration_source": "SWE-agent trajectory execution_time",
            "raw_trajectory_path": str(path),
            "raw_trajectory_sha256": sha256_file(path),
        })
    if rows and rows[0]["start_mono_ns"] < task_start_ns:
        return [], "tool_event_precedes_trajectory_start"
    return rows, None


def artifact_refs(root: Path, paths: Iterable[Path]) -> list[dict[str, str]]:
    refs = []
    for path in sorted(set(paths)):
        if not path.is_file() or path.stat().st_size == 0:
            raise CollectionError(f"raw trace artifact missing or empty: {path}")
        refs.append({"path": str(path.relative_to(root)), "sha256": sha256_file(path)})
    return refs


def collect_hardware_metadata() -> dict[str, Any]:
    """Record host/runtime identity once for every profiling row."""

    metadata: dict[str, Any] = {"clock": clock_metadata(), "gpu": {}, "cpu": {}, "ram": {}, "storage": {}}

    def command(argv: list[str]) -> str | None:
        try:
            result = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=15)
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    gpu = command(["nvidia-smi", "--query-gpu=name,compute_cap,driver_version,memory.total", "--format=csv,noheader,nounits"])
    if gpu:
        parts = [item.strip() for item in gpu.split(",")]
        metadata["gpu"] = {"name": parts[0] if parts else None, "compute_capability": parts[1] if len(parts) > 1 else None, "driver": parts[2] if len(parts) > 2 else None, "vram_mib": int(float(parts[3])) if len(parts) > 3 and parts[3].replace(".", "", 1).isdigit() else None, "architecture": "Ampere" if "A100" in (parts[0] if parts else "") else None}
    metadata["cuda"] = command(["nvcc", "--version"])
    metadata["nsight"] = command(["/usr/local/cuda/bin/nsys", "--version"])
    metadata["cpu"] = {"model": next((line.split(":", 1)[1].strip() for line in Path("/proc/cpuinfo").read_text(errors="ignore").splitlines() if line.lower().startswith("model name")), None), "logical_cpus": os.cpu_count()}
    try:
        metadata["ram"] = {"mem_total_kib": int(next(line.split()[1] for line in Path("/proc/meminfo").read_text().splitlines() if line.startswith("MemTotal:")))}
    except (StopIteration, OSError, ValueError):
        metadata["ram"] = {"mem_total_kib": None}
    try:
        usage = os.statvfs("/mnt/eic-work")
        metadata["storage"] = {"path": "/mnt/eic-work", "free_bytes": usage.f_bavail * usage.f_frsize, "total_bytes": usage.f_blocks * usage.f_frsize}
    except OSError:
        metadata["storage"] = {"path": "/mnt/eic-work", "free_bytes": None, "total_bytes": None}
    metadata["vllm"] = {"image": PINNED_VLLM_IMAGE, "version": "0.10.0", "tool_parser": "qwen3_coder", "tensor_parallel_size": 1, "dtype": "bfloat16", "concurrency": 1}
    return metadata


def official_result(report_root: Path, instance_id: str, extra_roots: Iterable[Path] = (), expected_run_id: str | None = None) -> tuple[str, bool | None, str | None]:
    """Read only the pinned evaluator's report; do not consult population labels."""

    found: list[tuple[Path, dict[str, Any]]] = []
    roots = [report_root, *extra_roots]
    seen: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.json")):
            if path in seen:
                continue
            seen.add(path)
            try:
                value = read_json(path)
            except CollectionError:
                continue
            if isinstance(value, dict):
                if expected_run_id and root != report_root and expected_run_id not in path.name and value.get("run_id") != expected_run_id:
                    continue
                found.append((path, value))
    for path, value in found:
        if instance_id in value.get("resolved_ids", []):
            return "resolved", True, str(path)
        if instance_id in value.get("unresolved_ids", []):
            return "unresolved", False, str(path)
        if value.get("instance_id") == instance_id and isinstance(value.get("resolved"), bool):
            return ("resolved" if value["resolved"] else "unresolved"), bool(value["resolved"]), str(path)
        if value.get("instance_id") == instance_id and isinstance(value.get("status"), str):
            status = str(value["status"])
            if status in {"resolved", "unresolved", "empty_patch", "incomplete", "error"}:
                return status, status == "resolved", str(path)
    return "unavailable", None, None


def evaluator_status_available(status: str) -> bool:
    return status in {"resolved", "unresolved", "empty_patch", "incomplete", "error"}


def existing_trace_recovery_candidate(task_dir: Path) -> bool:
    """Return true only for a complete, unsealed trajectory needing postprocess.

    A collector crash after trace collection must not cause SWE-agent to be
    rerun.  The required files are all outputs of that prior attempt; if any
    are absent, normal execution (or the existing fail-closed path) remains
    responsible for the directory.
    """

    required = (
        task_dir / "request_events.jsonl",
        task_dir / "sweagent_instances.json",
        task_dir / "sweagent_output" / "preds.json",
        task_dir / "trace" / "trace_arm.json",
        task_dir / "trace" / "trace_collect.json",
        task_dir / "trace" / "trace.nsys-rep",
        task_dir / "trace" / "trace.sqlite",
    )
    return all(path.is_file() and path.stat().st_size > 0 for path in required) and trajectory_path(task_dir) is not None


@dataclass
class TaskRun:
    task: Mapping[str, Any]
    repeat_id: str
    root: Path
    settings: Mapping[str, Any]
    trace: bool

    @property
    def task_dir(self) -> Path:
        safe = str(self.task["instance_id"]).replace("/", "_")
        return self.root / "tasks" / str(self.task["suite"]) / safe / self.repeat_id

    def execute(self, *, source_dataset: Path, args: argparse.Namespace, env: Mapping[str, str]) -> dict[str, Any]:
        task_dir = self.task_dir
        task_dir.mkdir(parents=True, exist_ok=True)
        terminal = task_dir / "task.json"
        if terminal.is_file():
            return read_json(terminal)
        source = load_task(source_dataset, str(self.task["instance_id"]))
        runtime_path = task_dir / "sweagent_instances.json"
        if not runtime_path.is_file():
            atomic_json(runtime_path, [runtime_row(source)])
        request_config = task_dir / "sweagent_request.yaml"
        if not request_config.is_file():
            request_config.write_text(
                "agent:\n  model:\n    completion_kwargs:\n      max_tokens: %s\n      seed: 0\n" % self.settings["max_output_tokens"],
                encoding="utf-8",
            )
        agent_output = task_dir / "sweagent_output"
        events = task_dir / "request_events.jsonl"
        logs = task_dir / "logs"
        logs.mkdir(exist_ok=True)
        proxy_port = int(args.proxy_port)
        base_url = f"http://127.0.0.1:{proxy_port}/v1"
        command_settings = {**self.settings, "instance_id": self.task["instance_id"]}
        command = command_base(
            sweagent=args.sweagent,
            swe_root=args.sweagent_root,
            request_config=request_config,
            instances=runtime_path,
            output=agent_output,
            api_base=base_url,
            settings=command_settings,
        )
        command_sha = sha256_bytes("\0".join(command).encode())
        resolved_command = task_dir / "resolved_command.json"
        if resolved_command.is_file():
            try:
                existing_command = read_json(resolved_command)
            except CollectionError:
                existing_command = None
            if isinstance(existing_command, Mapping) and isinstance(existing_command.get("sha256"), str):
                command_sha = str(existing_command["sha256"])
        else:
            atomic_json(resolved_command, {"argv": command, "sha256": command_sha, "settings": dict(self.settings)})
        local_env = dict(env)
        local_env.update({"VLLM_API_KEY": "local-only-placeholder", "EIC_REQUEST_PROXY": "1"})
        proxy: subprocess.Popen[str] | None = None
        arm: dict[str, Any] | None = None
        trace_dir = task_dir / "trace"
        recovering = existing_trace_recovery_candidate(task_dir)
        recovery_reason = "recovered_after_postprocessing_crash_without_trajectory_boundaries" if recovering else None
        start_ns = monotonic_ns()
        end_ns = start_ns
        trace_error: str | None = None
        agent_rc = 1
        timed_out = False
        if recovering:
            recovered_events = read_proxy_events(events)
            if not recovered_events:
                raise CollectionError("recovery_candidate_has_no_model_request_events")
            start_ns = min(int(item["start_mono_ns"]) for item in recovered_events)
            end_ns = max(int(item["end_mono_ns"]) for item in recovered_events)
            arm = read_json(trace_dir / "trace_arm.json")
            agent_rc = 0
        else:
            try:
                if self.trace:
                    arm_result = subprocess.run(provider_command(action="arm", output_dir=trace_dir, repeat_id=self.repeat_id, start_ns=start_ns, end_ns=start_ns + 1), cwd=str(ROOT), env=local_env, capture_output=True, text=True, check=False)
                    (logs / "trace_arm.log").write_text(arm_result.stdout + arm_result.stderr, encoding="utf-8")
                    if arm_result.returncode != 0:
                        trace_error = "trace_arm_failed"
                    else:
                        arm = read_json(trace_dir / "trace_arm.json")
                proxy = subprocess.Popen(proxy_command(events, proxy_port), cwd=str(ROOT), env=local_env, stdout=(logs / "proxy.log").open("w"), stderr=subprocess.STDOUT, start_new_session=True, text=True)
                time.sleep(0.5)
                agent_rc, timed_out = run_limited(command, cwd=ROOT, env=local_env, log=logs / "agent.log", timeout_seconds=int(args.task_timeout_seconds))
            finally:
                end_ns = monotonic_ns()
                stop_process(proxy)
                if self.trace and arm is not None:
                    collect = subprocess.run(provider_command(action="collect", output_dir=trace_dir, repeat_id=self.repeat_id, start_ns=start_ns, end_ns=end_ns), cwd=str(ROOT), env=local_env, capture_output=True, text=True, check=False)
                    (logs / "trace_collect.log").write_text(collect.stdout + collect.stderr, encoding="utf-8")
                    if collect.returncode != 0:
                        trace_error = trace_error or "trace_collect_failed"
        status = "unavailable" if recovering else ("completed" if agent_rc == 0 else ("timeout" if timed_out else "runner_failed"))
        official_status = "unavailable"
        official_resolved: bool | None = None
        evaluator_path: str | None = None
        evaluator_rc: int | None = None
        evaluator_error: str | None = None
        predictions = agent_output / "preds.json"
        report_root = task_dir / "evaluator_report"
        if recovering:
            official_status, official_resolved, evaluator_path = official_result(report_root, str(self.task["instance_id"]), [Path(args.evaluator_root)], f"a100-full-{self.task['instance_id']}-{self.repeat_id}")
            evaluator_rc = 0 if evaluator_path else None
            if evaluator_path is None:
                evaluator_error = "evaluator_report_missing_after_recovery"
        elif agent_rc == 0 and predictions.is_file():
            evaluator = [
                args.evaluator_python,
                "-m",
                "swebench.harness.run_evaluation",
                "--dataset_name",
                str(runtime_path),
                "--split",
                "test",
                "--predictions_path",
                str(predictions),
                "--instance_ids",
                str(self.task["instance_id"]),
                "--max_workers",
                "1",
                "--timeout",
                "1800",
                "--cache_level",
                "instance",
                "--clean",
                "False",
                "--run_id",
                f"a100-full-{self.task['instance_id']}-{self.repeat_id}",
                "--namespace",
                "swebench",
                "--instance_image_tag",
                "latest",
                "--env_image_tag",
                "latest",
                "--report_dir",
                str(report_root),
            ]
            evaluator_rc, _ = run_limited(evaluator, cwd=args.evaluator_root, env=local_env, log=logs / "evaluator.log", timeout_seconds=int(args.evaluator_timeout_seconds))
            official_status, official_resolved, evaluator_path = official_result(report_root, str(self.task["instance_id"]), [Path(args.evaluator_root)], f"a100-full-{self.task['instance_id']}-{self.repeat_id}")
            if evaluator_rc != 0:
                evaluator_error = "evaluator_failed"
        events_values = read_proxy_events(events)
        trajectory_id = f"{self.task['suite']}:{self.task['instance_id']}:{self.repeat_id}"
        model_event_rows: list[dict[str, Any]] = []
        model_event_path = task_dir / "model_events.jsonl"
        tool_event_path = task_dir / "tool_events.jsonl"
        request_path = self.root / "request_rows.jsonl"
        valid_count = 0
        unavailable_count = 0
        refs: list[dict[str, str]] = []
        hardware = collect_hardware_metadata()
        if self.trace and arm is not None and (trace_dir / "trace.sqlite").is_file():
            trace_files = [trace_dir / name for name in ("trace_arm.json", "trace_collect.json", "trace.nsys-rep", "trace.sqlite", "trace_summary.json")]
            refs = artifact_refs(self.root, trace_files)
            batch_direct_results = direct_traces_for_events(trace_dir, events_values, arm)
            for index, event in enumerate(events_values, start=1):
                request_id = str(event.get("request_id", f"unknown-{index}"))
                event_clock = event.get("clock") if isinstance(event.get("clock"), Mapping) else {}
                model_event: dict[str, Any] = {
                    "schema_version": "a100-full-model-event.v1",
                    "provenance": "measured",
                    "trajectory_id": trajectory_id,
                    "request_id": request_id,
                    "ordering": index,
                    "input_tokens": event.get("prompt_tokens"),
                    "output_tokens": event.get("completion_tokens"),
                    "context_length": 32768,
                    "requested_output_budget": self.settings["max_output_tokens"],
                    "request_wall_ms": event.get("duration_ms"),
                    "time_to_first_token_ms": None,
                    "generation_latency_ms": None,
                    "request_start_mono_ns": event.get("start_mono_ns"),
                    "request_end_mono_ns": event.get("end_mono_ns"),
                    "clock_id": event_clock.get("clock_id"),
                    "success": event.get("status_code") == 200,
                    "cpu_activity_union_ms": None,
                    "cuda_activity_union_ms": None,
                    "kernel_duration_sum_ms": None,
                }
                row: dict[str, Any] = {
                    "schema_version": REQUEST_SCHEMA,
                    "provenance": "measured",
                    "status": "unavailable",
                    "suite": self.task["suite"],
                    "repository": self.task["repository"],
                    "instance_id": self.task["instance_id"],
                    "request_id": request_id,
                    "repeat_id": self.repeat_id,
                    "request_index": index,
                    "trajectory_id": trajectory_id,
                    "task_status": status,
                    "official_status": official_status,
                    "official_resolved": official_resolved,
                    "model": PINNED_MODEL,
                    "model_revision": PINNED_MODEL_REVISION,
                    "tokenizer_revision": PINNED_TOKENIZER_REVISION,
                    "runtime": {"swe_agent_revision": PINNED_SWE_AGENT, "swe_bench_revision": PINNED_SWE_BENCH, "vllm_image": PINNED_VLLM_IMAGE, "concurrency": 1},
                    "wall_ms": event.get("duration_ms"),
                    "prompt_tokens": event.get("prompt_tokens"),
                    "completion_tokens": event.get("completion_tokens"),
                    "clock_id": (event.get("clock") or {}).get("clock_id"),
                    "request_start_mono_ns": event.get("start_mono_ns"),
                    "request_end_mono_ns": event.get("end_mono_ns"),
                    "raw_trace_paths": refs,
                    "evaluator_report": evaluator_path,
                    "hardware": hardware,
                }
                try:
                    if event.get("status_code") != 200:
                        raise CollectionError(f"request_http_{event.get('status_code')}")
                    if not isinstance(event.get("prompt_tokens"), int) or not isinstance(event.get("completion_tokens"), int):
                        raise CollectionError("missing_token_usage")
                    direct = batch_direct_results[index - 1]
                    if "error" in direct:
                        raise CollectionError(str(direct["error"]))
                    row.update({
                        "status": "completed",
                        "cpu_activity_union_ms": direct["cpu_activity_union_ms"],
                        "cuda_activity_union_ms": direct["cuda_activity_union_ms"],
                        "kernel_duration_sum_ms": direct["kernel_duration_sum_ms"],
                        "cpu_to_gpu_ratio": direct["cpu_to_gpu_ratio"],
                        "direct_timing": compact_direct_timing(direct),
                    })
                    model_event.update({
                        "cpu_activity_union_ms": direct["cpu_activity_union_ms"],
                        "cuda_activity_union_ms": direct["cuda_activity_union_ms"],
                        "kernel_duration_sum_ms": direct["kernel_duration_sum_ms"],
                    })
                    valid_count += 1
                except CollectionError as exc:
                    row["status"] = "unavailable"
                    row["unavailable_reason"] = str(exc)
                    unavailable_count += 1
                    model_event["success"] = False
                    model_event["failure_reason"] = str(exc)
                model_event_rows.append(model_event)
                append_jsonl(request_path, row)
        else:
            unavailable_count = len(events_values)
            for index, event in enumerate(events_values, start=1):
                clock = event.get("clock") if isinstance(event.get("clock"), Mapping) else {}
                model_event_rows.append({
                    "schema_version": "a100-full-model-event.v1",
                    "provenance": "unavailable",
                    "trajectory_id": trajectory_id,
                    "request_id": event.get("request_id", f"unknown-{index}"),
                    "ordering": index,
                    "input_tokens": event.get("prompt_tokens"),
                    "output_tokens": event.get("completion_tokens"),
                    "context_length": 32768,
                    "requested_output_budget": self.settings["max_output_tokens"],
                    "request_wall_ms": event.get("duration_ms"),
                    "time_to_first_token_ms": None,
                    "generation_latency_ms": None,
                    "request_start_mono_ns": event.get("start_mono_ns"),
                    "request_end_mono_ns": event.get("end_mono_ns"),
                    "clock_id": clock.get("clock_id"),
                    "success": False,
                    "failure_reason": trace_error or "direct_trace_unavailable",
                })
        for model_event in model_event_rows:
            append_jsonl(model_event_path, model_event)
        tool_rows, tool_error = extract_tool_events(task_dir, start_ns, model_event_rows, trajectory_id)
        for tool_event in tool_rows:
            append_jsonl(tool_event_path, tool_event)
        model_total_ms = sum(float(row.get("request_wall_ms", 0.0) or 0.0) for row in model_event_rows if isinstance(row.get("request_wall_ms"), (int, float)))
        tool_total_ms = sum(float(row.get("wall_ms", 0.0) or 0.0) for row in tool_rows)
        if tool_error is None and model_total_ms > 0 and tool_total_ms >= 0:
            phase_ratio = tool_total_ms / model_total_ms
            phase_ratio_status = "valid"
        else:
            phase_ratio = None
            phase_ratio_status = tool_error or "model_request_wall_unavailable"
        event_refs = artifact_refs(self.root, [model_event_path, tool_event_path]) if model_event_path.is_file() and tool_event_path.is_file() else []
        refs_with_events = refs + event_refs
        available = status == "completed" and evaluator_status_available(official_status) and phase_ratio_status == "valid"
        task_row = {
            "schema_version": TASK_SCHEMA,
            "provenance": "recovered_measured_components" if recovering else ("measured" if agent_rc == 0 else "unavailable"),
            "status": status,
            "available": available,
            "suite": self.task["suite"],
            "repository": self.task["repository"],
            "instance_id": self.task["instance_id"],
            "repeat_id": self.repeat_id,
            "trajectory_id": trajectory_id,
            "model": PINNED_MODEL,
            "model_revision": PINNED_MODEL_REVISION,
            "tokenizer_revision": PINNED_TOKENIZER_REVISION,
            "task_manifest_sha256": sha256_file(runtime_path),
            "configuration_id": "baseline",
            "hyperparameters": dict(self.settings),
            "start_mono_ns": None if recovering else start_ns,
            "end_mono_ns": None if recovering else end_ns,
            "e2e_wall_ms": None if recovering else (end_ns - start_ns) / 1e6,
            "task_wall_ms": None if recovering else (end_ns - start_ns) / 1e6,
            "total_tool_call_wall_ms": tool_total_ms,
            "total_model_request_wall_ms": model_total_ms,
            "phase_ratio": phase_ratio,
            "phase_ratio_formula": "sum(tool_call_wall_ms) / sum(model_request_wall_ms)",
            "phase_ratio_status": phase_ratio_status,
            "prompt_tokens": sum(int(item.get("prompt_tokens", 0) or 0) for item in events_values),
            "completion_tokens": sum(int(item.get("completion_tokens", 0) or 0) for item in events_values),
            "request_count": len(events_values),
            "tool_event_count": len(tool_rows),
            "model_event_count": len(model_event_rows),
            "valid_direct_request_count": valid_count,
            "unavailable_direct_request_count": unavailable_count,
            "official_status": official_status,
            "official_resolved": official_resolved,
            "evaluator_returncode": evaluator_rc,
            "evaluator_error": evaluator_error,
            "evaluator_report": evaluator_path,
            "trace_error": trace_error,
            "unavailable_reason": recovery_reason,
            "trajectory_boundary_status": "unavailable_recovered_request_window_only" if recovering else "measured",
            "command_sha256": command_sha,
            "warmup_excluded": True,
            "raw_paths": refs_with_events if self.trace and arm is not None and (trace_dir / "trace.sqlite").is_file() else event_refs,
            "hardware": hardware,
            "recorded_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        atomic_json(terminal, task_row)
        append_jsonl(self.root / "task_rows.jsonl", task_row)
        return task_row


def run_collection(args: argparse.Namespace) -> int:
    manifest_path = args.manifest.resolve()
    manifest = load_manifest(manifest_path)
    root = args.output_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    manifest_hash = sha256_file(manifest_path)
    state_path = root / "run_state.json"
    state = read_json(state_path) if state_path.is_file() else {"schema_version": "a100-full-run-state.v1", "status": "planned", "completed": [], "unavailable": []}
    if state.get("manifest_sha256") not in (None, manifest_hash):
        raise CollectionError("run state is bound to a different profiling manifest")
    deadline = int(args.deadline_epoch) if args.deadline_epoch else int(time.time()) + int(args.max_wall_seconds)
    if int(time.time()) >= deadline:
        raise CollectionError("profiling deadline has already expired")
    state.update({"status": "running", "manifest_sha256": manifest_hash, "started_at_utc": state.get("started_at_utc") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "deadline_epoch": deadline})
    replace_json(state_path, state)
    source_by_suite = {"lite": args.lite_dataset.resolve(), "verified": args.verified_dataset.resolve()}
    run_env = dict(os.environ)
    run_env.update({"BACKEND": "docker", "A100_TRACE_PROVIDER": str(TRACE_PROVIDER), "A100_TRACE_MOUNT_ROOT": str(root), "A100_TRACE_CONTAINER_ROOT": "/trace", "EIC_SOURCE_ROOT": str(ROOT)})
    # The production provider reads these exact server/session values from the
    # separate profiling manifest; no sealed-root value is imported here.
    for key in ("A100_CONTAINER", "A100_NSYS_CONTAINER", "A100_NSYS_SESSION", "A100_NSYS_BIN", "A100_NSYS_VERSION"):
        value = manifest_value(args.runtime_manifest, key)
        if value:
            run_env[key] = value
    settings = {"max_calls": 30, "max_output_tokens": 2048, "max_observation_length": 100000, "temperature": 0.0}
    # Preserve the immutable manifest while scheduling a deterministic,
    # suite-balanced prefix.  This makes a deadline-bounded run comparable to
    # a later resume: the prefix is selected before any A100 outcome exists.
    by_suite = {
        suite: [task for task in manifest["tasks"] if task.get("suite") == suite]
        for suite in ("lite", "verified")
    }
    tasks = []
    for index in range(max(len(by_suite["lite"]), len(by_suite["verified"]))):
        for suite in ("lite", "verified"):
            if index < len(by_suite[suite]):
                tasks.append(by_suite[suite][index])
    if args.max_tasks:
        tasks = tasks[: int(args.max_tasks)]
    for task in tasks:
        if time.time() >= deadline:
            break
        task_key = f"{task['suite']}:{task['instance_id']}"
        if task_key not in state.setdefault("completed", []) and task_key not in state.setdefault("unavailable", []):
            warm_dir = root / "tasks" / task["suite"] / task["instance_id"].replace("/", "_") / "warmup"
            warm_dir.mkdir(parents=True, exist_ok=True)
            warmup_path = warm_dir / "warmup.json"
            if not warmup_path.exists():
                status, reason = warmup_request(base_url="http://127.0.0.1:8000", model=f"openai/{PINNED_MODEL}", log=warm_dir / "warmup_response.json")
                atomic_json(warmup_path, {"schema_version": "a100-full-warmup.v1", "status": status, "reason": reason, "excluded_from_measurements": True})
        source = source_by_suite[str(task["suite"])]
        task_failed = False
        for repeat_id in ("r01", "r02", "r03"):
            if time.time() >= deadline:
                break
            repeat_dir = root / "tasks" / task["suite"] / task["instance_id"].replace("/", "_") / repeat_id
            if (repeat_dir / "task.json").is_file():
                continue
            run = TaskRun(task, repeat_id, root, settings, trace=True)
            try:
                result = run.execute(source_dataset=source, args=args, env=run_env)
                if result.get("status") != "completed" or result.get("available") is not True:
                    task_failed = True
            except (CollectionError, OSError, subprocess.SubprocessError) as exc:
                task_failed = True
                failure = {"schema_version": TASK_SCHEMA, "provenance": "unavailable", "status": "unavailable", "suite": task["suite"], "repository": task["repository"], "instance_id": task["instance_id"], "repeat_id": repeat_id, "unavailable_reason": str(exc)}
                atomic_json(repeat_dir / "task.json", failure)
                append_jsonl(root / "task_rows.jsonl", failure)
            state = read_json(state_path)
            state["status"] = "running"
            replace_json(state_path, state)
        state = read_json(state_path)
        bucket = "unavailable" if task_failed else "completed"
        state.setdefault(bucket, [])
        other_bucket = "completed" if bucket == "unavailable" else "unavailable"
        if task_key in state.setdefault(other_bucket, []):
            state[other_bucket].remove(task_key)
        if task_key not in state[bucket]:
            state[bucket].append(task_key)
        replace_json(state_path, state)
    state = read_json(state_path)
    state["status"] = "deadline_reached" if time.time() >= deadline else "partial"
    replace_json(state_path, state)
    print(json.dumps({"status": state["status"], "completed_tasks": len(state.get("completed", [])), "unavailable_tasks": len(state.get("unavailable", [])), "deadline_epoch": deadline}, sort_keys=True))
    return 0


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
    return rows


def reconcile_evaluator_labels(root: Path, evaluator_root: Path) -> list[dict[str, Any]]:
    """Bind evaluator outcomes to the exact trajectory repeat without mutation."""

    reconciled: list[dict[str, Any]] = []
    for task in _jsonl(root / "task_rows.jsonl"):
        instance_id = str(task.get("instance_id"))
        repeat_id = str(task.get("repeat_id"))
        expected_run_id = f"a100-full-{instance_id}-{repeat_id}"
        local_report = root / "tasks" / str(task.get("suite")) / instance_id.replace("/", "_") / repeat_id / "evaluator_report"
        status, resolved, report_path = official_result(local_report, instance_id, [evaluator_root], expected_run_id)
        report_hash = sha256_file(Path(report_path)) if report_path and Path(report_path).is_file() else None
        reconciled.append({
            "schema_version": "a100-full-evaluator-reconciliation.v1",
            "trajectory_id": task.get("trajectory_id"),
            "suite": task.get("suite"),
            "repository": task.get("repository"),
            "instance_id": instance_id,
            "repeat_id": repeat_id,
            "expected_run_id": expected_run_id,
            "official_status": status,
            "official_resolved": resolved,
            "evaluator_report": report_path,
            "evaluator_report_sha256": report_hash,
            "source_task_row_status": task.get("status"),
        })
    replace_json(root / "evaluator_label_reconciliation.json", reconciled)
    return reconciled


def aggregate(args: argparse.Namespace) -> int:
    root = args.output_root.resolve()
    request_rows = _jsonl(root / "request_rows.jsonl")
    task_rows = _jsonl(root / "task_rows.jsonl")
    reconciled = reconcile_evaluator_labels(root, args.evaluator_root.resolve()) if args.evaluator_root else []
    if reconciled:
        write_csv(root / "evaluator_label_reconciliation.csv", reconciled, ["trajectory_id", "suite", "repository", "instance_id", "repeat_id", "expected_run_id", "official_status", "official_resolved", "evaluator_report", "evaluator_report_sha256", "source_task_row_status"])
    labels = {str(row.get("trajectory_id")): row for row in reconciled}
    effective_task_rows = []
    for task in task_rows:
        effective = dict(task)
        effective.setdefault("model", PINNED_MODEL)
        effective.setdefault("model_revision", PINNED_MODEL_REVISION)
        effective.setdefault("tokenizer_revision", PINNED_TOKENIZER_REVISION)
        label = labels.get(str(task.get("trajectory_id")))
        if label:
            effective.update({"official_status": label.get("official_status"), "official_resolved": label.get("official_resolved"), "evaluator_report": label.get("evaluator_report")})
        effective_task_rows.append(effective)
    task_rows = effective_task_rows
    valid = [row for row in request_rows if row.get("status") == "completed" and row.get("provenance") == "measured"]
    phase_valid_tasks = [row for row in task_rows if row.get("available") is True and row.get("phase_ratio_status") == "valid"]
    task_fields = [
        "schema_version", "provenance", "status", "available", "suite", "repository", "instance_id", "repeat_id", "trajectory_id", "model", "model_revision", "tokenizer_revision",
        "task_manifest_sha256", "configuration_id", "hyperparameters", "start_mono_ns", "end_mono_ns", "e2e_wall_ms", "task_wall_ms",
        "total_tool_call_wall_ms", "total_model_request_wall_ms", "phase_ratio", "phase_ratio_formula", "phase_ratio_status",
        "prompt_tokens", "completion_tokens", "request_count", "tool_event_count", "model_event_count", "valid_direct_request_count",
        "unavailable_direct_request_count", "official_status", "official_resolved", "evaluator_returncode", "evaluator_error", "evaluator_report",
        "trace_error", "unavailable_reason", "trajectory_boundary_status", "command_sha256", "raw_paths", "hardware", "recorded_at_utc",
    ]
    write_csv(root / "task_rows.csv", task_rows, task_fields)
    request_fields = ["schema_version", "status", "suite", "repository", "instance_id", "request_id", "repeat_id", "request_index", "task_status", "official_status", "official_resolved", "model", "model_revision", "tokenizer_revision", "wall_ms", "prompt_tokens", "completion_tokens", "cpu_activity_union_ms", "cuda_activity_union_ms", "kernel_duration_sum_ms", "cpu_to_gpu_ratio", "clock_id", "request_start_mono_ns", "request_end_mono_ns", "raw_trace_paths", "unavailable_reason"]
    write_csv(root / "request_rows.csv", request_rows, request_fields)
    repos: list[dict[str, Any]] = []
    all_keys = sorted({(row.get("suite"), row.get("repository")) for row in task_rows})
    for key in all_keys:
        suite, repository = key
        subset = [row for row in valid if row.get("suite") == suite and row.get("repository") == repository]
        ratios = [float(row["cpu_to_gpu_ratio"]) for row in subset if isinstance(row.get("cpu_to_gpu_ratio"), (int, float))]
        task_subset = [row for row in phase_valid_tasks if row.get("suite") == suite and row.get("repository") == repository]
        all_task_subset = [row for row in task_rows if row.get("suite") == suite and row.get("repository") == repository]
        repos.append({"suite": suite, "repository": repository, "configuration": "baseline", "valid_phase_ratio_rows": len(task_subset), "task_denominator": len(all_task_subset), "request_denominator": len([row for row in request_rows if row.get("suite") == suite and row.get("repository") == repository]), "repeat_count": len({(row.get("instance_id"), row.get("repeat_id")) for row in task_subset}), "median_tool_model_phase_ratio": statistics.median(float(row["phase_ratio"]) for row in task_subset) if task_subset else None, "mean_e2e_wall_ms": statistics.mean(float(row["e2e_wall_ms"]) for row in task_subset) if task_subset else None, "resolved_count": sum(row.get("official_resolved") is True for row in all_task_subset), "official_label_denominator": sum(evaluator_status_available(str(row.get("official_status"))) for row in all_task_subset), "valid_direct_request_rows": len(subset), "median_cpu_cuda_diagnostic_ratio": statistics.median(ratios) if ratios else None, "median_cpu_union_ms": statistics.median(float(row["cpu_activity_union_ms"]) for row in subset) if subset else None, "median_cuda_union_ms": statistics.median(float(row["cuda_activity_union_ms"]) for row in subset) if subset else None, "provenance": "derived_phase_ratio_primary_direct_nsight_secondary"})
    repo_fields = ["suite", "repository", "configuration", "valid_phase_ratio_rows", "task_denominator", "request_denominator", "repeat_count", "median_tool_model_phase_ratio", "mean_e2e_wall_ms", "resolved_count", "official_label_denominator", "valid_direct_request_rows", "median_cpu_cuda_diagnostic_ratio", "median_cpu_union_ms", "median_cuda_union_ms", "provenance"]
    write_csv(root / "repository_phase_ratio_summary.csv", repos, repo_fields)
    write_csv(root / "repository_cpu_gpu_summary.csv", repos, repo_fields)
    latency: list[dict[str, Any]] = []
    for suite in ("lite", "verified"):
        subset = [row for row in task_rows if row.get("suite") == suite and row.get("status") == "completed"]
        latency.append({"suite": suite, "completed_task_rows": len(subset), "task_denominator": len([row for row in task_rows if row.get("suite") == suite]), "median_task_wall_ms": statistics.median(float(row["task_wall_ms"]) for row in subset) if subset else None, "resolved_count": sum(row.get("official_resolved") is True for row in subset), "unresolved_count": sum(row.get("official_resolved") is False for row in subset), "official_label_denominator": sum(row.get("official_status") in {"resolved", "unresolved", "empty_patch", "incomplete", "error"} for row in subset), "provenance": "derived_from_new_A100_evaluator_rows"})
    write_csv(root / "lite_verified_latency_summary.csv", latency, ["suite", "completed_task_rows", "task_denominator", "median_task_wall_ms", "resolved_count", "unresolved_count", "official_label_denominator", "provenance"])
    # A stable, plot-ready table.  It intentionally does not render a figure;
    # the latest collection instruction requests offline plotting later.
    write_csv(root / "plot_ready_phase_ratio.csv", repos, repo_fields)
    write_csv(root / "repository_accuracy_latency.csv", [{"suite": row["suite"], "repository": row["repository"], "resolved_count": row["resolved_count"], "official_label_denominator": row["official_label_denominator"], "resolved_rate_percent": (100.0 * row["resolved_count"] / row["official_label_denominator"]) if row["official_label_denominator"] else None, "mean_e2e_wall_ms": row["mean_e2e_wall_ms"]} for row in repos], ["suite", "repository", "resolved_count", "official_label_denominator", "resolved_rate_percent", "mean_e2e_wall_ms"])
    write_csv(root / "repository_accuracy_phase_ratio.csv", [{"suite": row["suite"], "repository": row["repository"], "resolved_count": row["resolved_count"], "official_label_denominator": row["official_label_denominator"], "resolved_rate_percent": (100.0 * row["resolved_count"] / row["official_label_denominator"]) if row["official_label_denominator"] else None, "median_tool_model_phase_ratio": row["median_tool_model_phase_ratio"]} for row in repos], ["suite", "repository", "resolved_count", "official_label_denominator", "resolved_rate_percent", "median_tool_model_phase_ratio"])
    all_model_events: list[dict[str, Any]] = []
    all_tool_events: list[dict[str, Any]] = []
    for path in sorted(root.glob("tasks/*/*/*/model_events.jsonl")):
        all_model_events.extend(_jsonl(path))
    for path in sorted(root.glob("tasks/*/*/*/tool_events.jsonl")):
        all_tool_events.extend(_jsonl(path))
    (root / "model_events.jsonl").write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in all_model_events), encoding="utf-8")
    (root / "tool_events.jsonl").write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in all_tool_events), encoding="utf-8")
    write_csv(root / "model_events.csv", all_model_events, ["schema_version", "trajectory_id", "request_id", "ordering", "input_tokens", "output_tokens", "context_length", "requested_output_budget", "request_wall_ms", "time_to_first_token_ms", "generation_latency_ms", "request_start_mono_ns", "request_end_mono_ns", "clock_id", "cpu_activity_union_ms", "cuda_activity_union_ms", "kernel_duration_sum_ms", "success", "failure_reason"])
    write_csv(root / "tool_events.csv", all_tool_events, ["schema_version", "trajectory_id", "event_id", "ordering", "operation_class", "action_name", "read_write_traversal_shell_edit_test_other", "start_mono_ns", "end_mono_ns", "wall_ms", "cpu_time_ms", "bytes", "call_count", "path_metadata", "success", "duration_source", "raw_trajectory_path", "raw_trajectory_sha256"])
    hyper_rows = _jsonl(root / "hyperparameter_rows.jsonl")
    write_csv(root / "hyperparameter_results.csv", hyper_rows, ["suite", "repository", "instance_id", "axis", "value", "repeat_id", "status", "task_wall_ms", "official_status", "prompt_tokens", "completion_tokens", "source_manifest_sha256", "artifact_sha256", "failure_reason"])
    simulator = build_simulator_validation(root, task_rows)
    replace_json(root / "simulator_validation.json", simulator)
    audit = {
        "schema_version": "a100-full-offline-audit.v1",
        "request_rows": len(request_rows),
        "valid_direct_rows": len(valid),
        "unavailable_rows": sum(row.get("status") == "unavailable" for row in request_rows),
        "ratio_formula": PHASE_RATIO_FORMULA,
        "secondary_direct_ratio_formula": RATIO_FORMULA,
        "rejected_gpu_substitutes": True,
        "prediction_features_exclude": ["wall_ms", "cpu_activity_union_ms", "cuda_activity_union_ms", "kernel_duration_sum_ms", "completion_tokens", "actual_completion_tokens"],
        "canonical_h100_paths_read": False,
        "sealed_a100_root_read": False,
        "plot_generated": False,
    }
    replace_json(root / "offline_integrity_audit.json", audit)
    inventory = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name not in {"artifact_inventory.json", "artifact_inventory.sha256"}:
            inventory.append({"path": str(path.relative_to(root)), "sha256": sha256_file(path), "bytes": path.stat().st_size})
    digest = replace_json(root / "artifact_inventory.json", {"schema_version": "a100-full-artifact-inventory.v1", "provenance": "derived", "artifacts": inventory})
    (root / "artifact_inventory.sha256").write_text(f"{digest}  artifact_inventory.json\n", encoding="utf-8")
    print(json.dumps({"valid_direct_rows": len(valid), "request_rows": len(request_rows), "repositories": len(repos), "inventory_sha256": digest}, sort_keys=True))
    return 0


def build_simulator_validation(root: Path, task_rows: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [row for row in task_rows if row.get("status") == "completed" and row.get("request_count", 0)]
    if len(completed) < 4:
        return {"schema_version": "a100-full-simulator-validation.v1", "status": "unavailable", "reason": "fewer than four completed task rows", "features_are_pre_execution_only": True}
    completed = sorted(completed, key=lambda row: (str(row.get("suite")), str(row.get("repository")), str(row.get("instance_id")), str(row.get("repeat_id"))))
    midpoint = max(2, len(completed) // 2)
    calibration = completed[:midpoint]
    validation = completed[midpoint:]
    try:
        from agentic_sim.feature_simulator import FeatureCalibrationRecord, FeatureInput, FeatureLatencySimulator

        def feature_input(row: Mapping[str, Any]) -> FeatureInput:
            # Prompt tokens are a request property known before generation; the
            # completion count and every timing field remain labels only.
            return FeatureInput.from_mapping({"run_id": f"{row['instance_id']}:{row['repeat_id']}", "prompt_tokens": max(0, int(row.get("prompt_tokens", 0) or 0)), "max_output_tokens": 2048, "context_tokens": 32768, "tool_calls": 0, "hardware_score": 1.0})

        def fit_target(name: str) -> Any:
            records = []
            for row in calibration:
                value = float(row.get(name, row.get("e2e_wall_ms", 0.0))) / 1000.0
                records.append(FeatureCalibrationRecord(features=feature_input(row), observed_seconds=value))
            return FeatureLatencySimulator.fit(records)

        wall_model = fit_target("e2e_wall_ms")
        # Task-level direct event medians are retained as labels.  The event
        # models use the same feature-only simulator and calibration split.
        request_rows = _jsonl(root / "request_rows.jsonl")
        by_task: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in request_rows:
            if row.get("status") == "completed":
                by_task.setdefault((str(row.get("instance_id")), str(row.get("repeat_id"))), []).append(row)
        event_models = {}
        for name in ("wall_ms", "cpu_activity_union_ms", "cuda_activity_union_ms", "kernel_duration_sum_ms"):
            labels = []
            for row in calibration:
                items = by_task.get((str(row["instance_id"]), str(row["repeat_id"])), [])
                if not items:
                    continue
                label = statistics.median(float(item.get(name, item.get("wall_ms", 0.0))) for item in items) / 1000.0
                labels.append(FeatureCalibrationRecord(features=feature_input(row), observed_seconds=label))
            if len(labels) >= 2:
                event_models[name] = FeatureLatencySimulator.fit(labels)
        validation_rows = []
        for row in validation:
            inputs = feature_input(row)
            predicted_wall = float(wall_model.predict(inputs.to_mapping())["predicted_seconds"])
            items = by_task.get((str(row["instance_id"]), str(row["repeat_id"])), [])
            if not items:
                continue
            measured_wall = float(row["e2e_wall_ms"]) / 1000.0
            predicted_events = {name: float(model.predict(inputs.to_mapping())["predicted_seconds"]) for name, model in event_models.items()}
            measured_events = {name: statistics.median(float(item.get(name, 0.0)) for item in items) / 1000.0 for name in ("wall_ms", "cpu_activity_union_ms", "cuda_activity_union_ms", "kernel_duration_sum_ms")}
            validation_rows.append({"suite": row["suite"], "repository": row["repository"], "instance_id": row["instance_id"], "repeat_id": row["repeat_id"], "split": "validation", "features": inputs.to_mapping(), "measured_event_values": measured_events, "predicted_event_values": predicted_events, "measured_e2e_seconds": measured_wall, "predicted_e2e_seconds": predicted_wall, "absolute_error_seconds": abs(predicted_wall - measured_wall), "percentage_error": abs(predicted_wall - measured_wall) / measured_wall * 100 if measured_wall > 0 else None, "provenance": "derived_prediction_from_calibration_only"})
        return {"schema_version": "a100-full-simulator-validation.v1", "status": "completed", "features_are_pre_execution_only": True, "feature_manifest": {"features": ["prompt_tokens", "max_output_tokens", "context_tokens", "tool_calls", "hardware_score"], "forbidden_target_fields": ["wall_ms", "cpu_activity_union_ms", "cuda_activity_union_ms", "kernel_duration_sum_ms", "completion_tokens"]}, "calibration_row_count": len(calibration), "validation_row_count": len(validation_rows), "rows": validation_rows}
    except Exception as exc:  # fail closed in the artifact, not with a fabricated metric
        return {"schema_version": "a100-full-simulator-validation.v1", "status": "unavailable", "reason": f"simulator_validation_failed:{type(exc).__name__}:{exc}", "features_are_pre_execution_only": True}


def audit(args: argparse.Namespace) -> int:
    root = args.output_root.resolve()
    rows = _jsonl(root / "request_rows.jsonl")
    task_rows = _jsonl(root / "task_rows.jsonl")
    model_events = _jsonl(root / "model_events.jsonl")
    tool_events = _jsonl(root / "tool_events.jsonl")
    errors = []
    request_keys: set[tuple[str, str]] = set()
    hash_cache: dict[Path, str] = {}
    for row in rows:
        request_key = (str(row.get("trajectory_id")), str(row.get("request_id")))
        if request_key in request_keys:
            errors.append(f"duplicate_request_row:{request_key[0]}:{request_key[1]}")
        request_keys.add(request_key)
        if row.get("status") == "completed":
            for field in ("cpu_activity_union_ms", "cuda_activity_union_ms", "cpu_to_gpu_ratio"):
                value = row.get(field)
                if not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) <= 0:
                    errors.append(f"invalid_{field}:{row.get('request_id')}")
            if abs(float(row["cpu_to_gpu_ratio"]) - float(row["cpu_activity_union_ms"]) / float(row["cuda_activity_union_ms"])) > 1e-9:
                errors.append(f"ratio_mismatch:{row.get('request_id')}")
            start = row.get("request_start_mono_ns")
            end = row.get("request_end_mono_ns")
            wall = row.get("wall_ms")
            if not isinstance(start, int) or not isinstance(end, int) or end <= start:
                errors.append(f"invalid_request_window:{row.get('request_id')}")
            if not isinstance(wall, (int, float)) or not math.isfinite(float(wall)) or float(wall) <= 0:
                errors.append(f"invalid_request_wall:{row.get('request_id')}")
            refs = row.get("raw_trace_paths")
            if not isinstance(refs, list) or not refs:
                errors.append(f"missing_trace_refs:{row.get('request_id')}")
            else:
                for ref in refs:
                    if not isinstance(ref, Mapping) or not isinstance(ref.get("path"), str) or not isinstance(ref.get("sha256"), str) or len(str(ref.get("sha256"))) != 64:
                        errors.append(f"invalid_trace_ref:{row.get('request_id')}")
                        continue
                    ref_path = Path(str(ref["path"]))
                    candidate = ref_path if ref_path.is_absolute() else root / ref_path
                    if not candidate.is_file():
                        errors.append(f"missing_trace_artifact:{candidate}")
                        continue
                    if candidate not in hash_cache:
                        hash_cache[candidate] = sha256_file(candidate)
                    if hash_cache[candidate] != ref["sha256"]:
                        errors.append(f"trace_hash_mismatch:{candidate}")
    model_by_trajectory: dict[str, list[dict[str, Any]]] = {}
    tool_by_trajectory: dict[str, list[dict[str, Any]]] = {}
    for event in model_events:
        model_by_trajectory.setdefault(str(event.get("trajectory_id")), []).append(event)
    for event in tool_events:
        tool_by_trajectory.setdefault(str(event.get("trajectory_id")), []).append(event)
    for trajectory, events in model_by_trajectory.items():
        request_ids = [str(event.get("request_id")) for event in events]
        if len(request_ids) != len(set(request_ids)):
            errors.append(f"duplicate_model_event:{trajectory}")
        for event in events:
            start = event.get("request_start_mono_ns")
            end = event.get("request_end_mono_ns")
            duration = event.get("request_wall_ms")
            if not isinstance(start, int) or not isinstance(end, int) or end <= start:
                errors.append(f"invalid_model_window:{trajectory}:{event.get('request_id')}")
            if not isinstance(duration, (int, float)) or not math.isfinite(float(duration)) or float(duration) <= 0:
                errors.append(f"invalid_model_duration:{trajectory}:{event.get('request_id')}")
    for trajectory, events in tool_by_trajectory.items():
        event_ids = [str(event.get("event_id")) for event in events]
        if len(event_ids) != len(set(event_ids)):
            errors.append(f"duplicate_tool_event:{trajectory}")
        for event in events:
            start = event.get("start_mono_ns")
            end = event.get("end_mono_ns")
            duration = event.get("wall_ms")
            if not isinstance(start, int) or not isinstance(end, int) or end < start:
                errors.append(f"invalid_tool_window:{trajectory}:{event.get('event_id')}")
            if not isinstance(duration, (int, float)) or not math.isfinite(float(duration)) or float(duration) < 0:
                errors.append(f"invalid_tool_duration:{trajectory}:{event.get('event_id')}")
    model_keys = {(str(event.get("trajectory_id")), str(event.get("request_id"))) for event in model_events}
    for key in request_keys:
        if key not in model_keys and any(row.get("trajectory_id") == key[0] and row.get("status") == "completed" for row in rows):
            errors.append(f"request_model_identity_mismatch:{key[0]}:{key[1]}")
    for event in model_events:
        key = (str(event.get("trajectory_id")), str(event.get("request_id")))
        if key not in request_keys:
            errors.append(f"orphan_model_event:{key[0]}:{key[1]}")
    for task in task_rows:
        trajectory = str(task.get("trajectory_id"))
        models = model_by_trajectory.get(trajectory, [])
        tools = tool_by_trajectory.get(trajectory, [])
        if task.get("model_event_count") != len(models):
            errors.append(f"model_count_mismatch:{trajectory}")
        if task.get("tool_event_count") != len(tools):
            errors.append(f"tool_count_mismatch:{trajectory}")
        model_total = sum(float(event.get("request_wall_ms", 0.0)) for event in models if isinstance(event.get("request_wall_ms"), (int, float)))
        tool_total = sum(float(event.get("wall_ms", 0.0)) for event in tools if isinstance(event.get("wall_ms"), (int, float)))
        if task.get("phase_ratio_status") == "valid":
            if model_total <= 0 or abs(float(task.get("total_model_request_wall_ms")) - model_total) > 1e-6:
                errors.append(f"model_total_mismatch:{trajectory}")
            if abs(float(task.get("total_tool_call_wall_ms")) - tool_total) > 1e-6:
                errors.append(f"tool_total_mismatch:{trajectory}")
            expected = tool_total / model_total if model_total > 0 else None
            if expected is None or abs(float(task.get("phase_ratio")) - expected) > 1e-9:
                errors.append(f"phase_ratio_mismatch:{trajectory}")
            if task.get("phase_ratio_formula") != PHASE_RATIO_FORMULA:
                errors.append(f"phase_ratio_formula_mismatch:{trajectory}")
    for event in model_events:
        features = event.get("features", {})
        if isinstance(features, Mapping):
            for forbidden in ("wall_ms", "cpu_activity_union_ms", "cuda_activity_union_ms", "kernel_duration_sum_ms", "completion_tokens", "actual_completion_tokens"):
                if forbidden in features:
                    errors.append(f"target_leakage:{event.get('trajectory_id')}:{forbidden}")
    if errors:
        replace_json(root / "offline_integrity_audit.json", {"schema_version": "a100-full-offline-audit.v1", "status": "failed", "errors": errors, "ratio_formula": PHASE_RATIO_FORMULA, "secondary_direct_ratio_formula": RATIO_FORMULA})
        print(json.dumps({"status": "failed", "errors": errors}, sort_keys=True))
        return 2
    replace_json(root / "offline_integrity_audit.json", {"schema_version": "a100-full-offline-audit.v1", "status": "passed", "request_rows": len(rows), "valid_direct_rows": sum(row.get("status") == "completed" for row in rows), "ratio_formula": PHASE_RATIO_FORMULA, "secondary_direct_ratio_formula": RATIO_FORMULA, "sealed_roots_touched": False, "canonical_h100_artifacts_touched": False, "plot_generated": False})
    print(json.dumps({"status": "passed", "request_rows": len(rows)}, sort_keys=True))
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--population", type=Path, default=POPULATION)
    p.add_argument("--output-root", type=Path, default=DEFAULT_ROOT)
    r = sub.add_parser("run")
    r.add_argument("--manifest", type=Path, required=True)
    r.add_argument("--output-root", type=Path, required=True)
    r.add_argument("--lite-dataset", type=Path, required=True)
    r.add_argument("--verified-dataset", type=Path, required=True)
    r.add_argument("--sweagent", default="sweagent")
    r.add_argument("--sweagent-root", type=Path, required=True)
    r.add_argument("--evaluator-python", default=sys.executable)
    r.add_argument("--evaluator-root", type=Path, required=True)
    r.add_argument("--runtime-manifest", type=Path, required=True)
    r.add_argument("--proxy-port", type=int, default=8001)
    r.add_argument("--task-timeout-seconds", type=int, default=3600)
    r.add_argument("--evaluator-timeout-seconds", type=int, default=1800)
    r.add_argument("--max-tasks", type=int, default=0)
    r.add_argument("--max-wall-seconds", type=int, default=7200)
    r.add_argument("--deadline-epoch", type=int)
    a = sub.add_parser("aggregate")
    a.add_argument("--output-root", type=Path, required=True)
    a.add_argument("--evaluator-root", type=Path)
    d = sub.add_parser("audit")
    d.add_argument("--output-root", type=Path, required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "prepare":
            return prepare(args)
        if args.command == "run":
            return run_collection(args)
        if args.command == "aggregate":
            return aggregate(args)
        return audit(args)
    except CollectionError as exc:
        print(f"A100 full-data collection: BLOCKED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
