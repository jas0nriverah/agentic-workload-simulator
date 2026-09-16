#!/usr/bin/env python3
"""Render offline, non-launchable inputs for the 22 observed H100 workers.

The allocation-v4 fingerprint is the source of truth for identity and for
the model-serving arguments that were actually observed.  This command only
reads JSON files and writes a new artifact directory.  It does not contact an
API, inspect a GPU, start a job, change a relay, or produce a final runtime
manifest.

The emitted runtime files are deliberately wrapped as templates.  They carry
the observed 32K setting and explicit placeholders for the source/observer
65K bindings and the fixed remote revision.  A downstream release step must
resolve those gates before anything can be launched.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable


EXPECTED_WORKER_IDS = tuple(
    f"{index:02d}" for index in (*range(0, 11), *range(12, 23))
)
LOCAL_PORT_BASE = 18100
REMOTE_PORT_BASE = 18200
OBSERVED_MAX_MODEL_LEN = 32768
TARGET_MAX_MODEL_LEN = 65536
FINGERPRINT_SCHEMA = "assignment.worker-fingerprint-discovery.v1"
RUNTIME_MANIFEST_SCHEMA = "assignment-runtime-manifest.v1"
OUTPUT_SCHEMA = "assignment.worker-runtime-inputs.v1"
HARDWARE_SCHEMA = "assignment.worker-hardware-profile.v1"
TEMPLATE_SCHEMA = "assignment.worker-runtime-template.v1"

PENDING_SOURCE_65K = "<PENDING_SOURCE_65K_BINDING_FILE>"
PENDING_OBSERVER_65K = "<PENDING_OBSERVER_65K_BINDING_FILE>"
PENDING_FIXED_REVISION = "<PENDING_REMOTE_FIXED_MODEL_REVISION_FILE>"
PENDING_SHA256 = "<PENDING_SHA256_AFTER_APPROVED_SOURCE_AND_OBSERVER_BINDINGS>"


class RenderError(ValueError):
    """Raised when the observed input set cannot form an unambiguous plan."""


def _fail(condition: bool, message: str) -> None:
    if not condition:
        raise RenderError(message)


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _regular_file(path: Path, label: str) -> Path:
    _fail(path.is_file() and not path.is_symlink(), f"{label} is not a regular file: {path}")
    return path.resolve()


def _read_json(path: Path, label: str) -> tuple[dict[str, Any], str]:
    path = _regular_file(path, label)
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RenderError(f"cannot read {label} {path}: {exc}") from exc
    _fail(isinstance(value, dict), f"{label} must be a JSON object: {path}")
    return value, _sha256_bytes(raw)


def _number(value: str, label: str) -> int | float | None:
    value = value.strip()
    if value.upper() in {"N/A", "NA", "UNKNOWN", "NOT SUPPORTED", ""}:
        return None
    try:
        number = float(value)
    except ValueError as exc:
        raise RenderError(f"{label} is not numeric: {value!r}") from exc
    return int(number) if number.is_integer() else number


def _int(value: Any, label: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise RenderError(f"{label} is not an integer: {value!r}") from exc
    return result


def _string(value: Any, label: str) -> str:
    _fail(isinstance(value, str) and bool(value.strip()), f"{label} must be a non-empty string")
    return value


def _command_result(value: Any, label: str) -> dict[str, Any]:
    _fail(isinstance(value, dict), f"{label} result is missing")
    _fail(value.get("returncode") == 0, f"{label} did not succeed: {value.get('returncode')!r}")
    _fail(isinstance(value.get("stdout"), str), f"{label} stdout is missing")
    _fail(isinstance(value.get("stderr"), str), f"{label} stderr is missing")
    return value


def _csv_rows(stdout: str, label: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in csv.reader(stdout.splitlines(), skipinitialspace=True):
        if row and any(cell.strip() for cell in row):
            rows.append([cell.strip() for cell in row])
    _fail(rows, f"{label} has no data rows")
    return rows


def _parse_gpu(result: dict[str, Any]) -> dict[str, Any]:
    rows = _csv_rows(result["stdout"], "GPU inventory")
    _fail(len(rows) == 1, f"GPU inventory must contain exactly one row, got {len(rows)}")
    row = rows[0]
    _fail(len(row) == 11, f"GPU inventory row must have 11 fields, got {len(row)}")
    return {
        "index": _int(row[0], "GPU index"),
        "uuid": _string(row[1], "GPU UUID"),
        "name": _string(row[2], "GPU name"),
        "memory_total_mib": _number(row[3], "GPU memory total"),
        "driver_version": _string(row[4], "GPU driver version"),
        "pci_bus_id": _string(row[5], "GPU PCI bus ID"),
        "compute_capability": _string(row[6], "GPU compute capability"),
        "observed_clocks_mhz": {
            "sm": _number(row[7], "GPU SM clock"),
            "memory": _number(row[8], "GPU memory clock"),
        },
        "power_limit_w": _number(row[9], "GPU power limit"),
        "temperature_gpu_c": _number(row[10], "GPU temperature"),
        "query_fields": [
            "index",
            "uuid",
            "name",
            "memory.total",
            "driver_version",
            "pci.bus_id",
            "compute_cap",
            "clocks.current.sm",
            "clocks.current.memory",
            "power.limit",
            "temperature.gpu",
        ],
    }


def _parse_compute(result: dict[str, Any], gpu_uuid: str) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    for row in _csv_rows(result["stdout"], "compute-process inventory"):
        _fail(len(row) == 3, f"compute-process row must have 3 fields, got {len(row)}")
        if row[1] == gpu_uuid:
            matches.append(
                {
                    "pid": _int(row[0], "compute process PID"),
                    "gpu_uuid": row[1],
                    "used_memory_mib": _number(row[2], "compute process used memory"),
                }
            )
    _fail(matches, f"no compute process is bound to GPU {gpu_uuid}")
    _fail(len(matches) == 1, f"expected one compute process for GPU {gpu_uuid}, got {len(matches)}")
    return matches[0]


def _options(process: dict[str, Any], worker_label: str) -> dict[str, str]:
    options = process.get("options")
    _fail(isinstance(options, dict), f"{worker_label} serving options are missing")
    for key, value in options.items():
        _fail(isinstance(key, str) and isinstance(value, str), f"{worker_label} serving options must be strings")
    required = ("--host", "--max-model-len", "--model", "--port", "--served-model-name")
    for key in required:
        _fail(key in options, f"{worker_label} serving option {key} is missing")
    return dict(options)


def _cpu_vm_binding(path: Path | None) -> tuple[dict[str, Any], str]:
    if path is None:
        binding = {
            "schema_version": "assignment.worker-cpu-vm-profile-binding.v1",
            "status": "pending_separate_reference",
            "embedded_in_gpu_profiles": False,
            "source": {
                "path": "<PENDING_RAW_ACTUAL_CPU_VM_PROFILE>",
                "sha256": PENDING_SHA256,
            },
            "unknown_measurements": ["cpu_frequency"],
            "note": "Provide the raw local CPU/VM inventory after source gates; it stays separate from GPU profiles.",
        }
        return binding, _sha256_bytes(_canonical_bytes(binding))

    value, digest = _read_json(path, "CPU/VM profile")
    binding = {
        "schema_version": "assignment.worker-cpu-vm-profile-binding.v1",
        "status": "reference_only",
        "embedded_in_gpu_profiles": False,
        "source": {
            "path": str(path.resolve()),
            "sha256": digest,
            "manifest_schema_version": value.get("schema_version"),
            "captured_at": value.get("captured_at"),
        },
        "raw_manifest": value,
        "unknown_measurements": ["cpu_frequency"],
        "note": "This reference is kept separate; no CPU frequency is projected into a worker GPU profile.",
    }
    return binding, _sha256_bytes(_canonical_bytes(binding))


def _validate_runtime_template(template: dict[str, Any]) -> None:
    _fail(
        template.get("schema_version") == RUNTIME_MANIFEST_SCHEMA,
        "runtime template has an unsupported schema_version",
    )
    for key in ("model", "pins", "runner", "hardware"):
        _fail(isinstance(template.get(key), dict), f"runtime template {key} is missing")


def _source_ref(path: Path, digest: str, source: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": str(path.resolve()),
        "sha256": digest,
        "schema_version": source.get("schema_version"),
        "captured_at": source.get("captured_at"),
        "probe_scope": source.get("probe_scope"),
        "probe_source_sha256": source.get("probe_source_sha256"),
    }


def _excluded_allocation(record: dict[str, Any]) -> dict[str, Any]:
    inventory = record.get("inventory")
    _fail(isinstance(inventory, dict), "allocation record inventory is missing")
    gpu_result = inventory.get("gpus")
    gpu_uuid = None
    if isinstance(gpu_result, dict) and isinstance(gpu_result.get("stdout"), str):
        rows = _csv_rows(gpu_result["stdout"], "excluded GPU inventory")
        if len(rows) == 1 and len(rows[0]) >= 2:
            gpu_uuid = rows[0][1]
    return {
        "job_id": str(record.get("job_id")),
        "node": str(record.get("node")),
        "hostname": inventory.get("hostname"),
        "gpu_uuid": gpu_uuid,
        "reason": "no_serving_process_observed",
    }


def _worker_record(record: dict[str, Any], node_record_index: int) -> tuple[str, dict[str, Any], dict[str, Any]]:
    _fail(record.get("returncode") == 0, f"allocation record {node_record_index} did not succeed")
    inventory = record.get("inventory")
    _fail(isinstance(inventory, dict), f"allocation record {node_record_index} inventory is missing")
    serving = inventory.get("serving_processes")
    _fail(isinstance(serving, list), f"allocation record {node_record_index} serving process list is missing")
    _fail(len(serving) == 1, f"allocation record {node_record_index} must have one serving process")
    process = serving[0]
    _fail(isinstance(process, dict), f"allocation record {node_record_index} serving process is invalid")
    worker_label = f"allocation record {node_record_index}"
    options = _options(process, worker_label)
    remote_port = _int(options["--port"], f"{worker_label} remote port")
    _fail(
        REMOTE_PORT_BASE <= remote_port <= REMOTE_PORT_BASE + 22
        and remote_port != REMOTE_PORT_BASE + 11,
        f"{worker_label} has an unsupported remote port: {remote_port}",
    )
    worker_id = f"{remote_port - REMOTE_PORT_BASE:02d}"
    _fail(worker_id in EXPECTED_WORKER_IDS, f"{worker_label} maps to unexpected worker {worker_id}")
    _fail(options["--host"] == "127.0.0.1", f"{worker_label} has unexpected bind host")
    observed_max_model_len = _int(options["--max-model-len"], f"{worker_label} max model length")
    _fail(
        observed_max_model_len == OBSERVED_MAX_MODEL_LEN,
        f"{worker_label} does not carry the expected observed 32K setting",
    )

    job_id = str(record.get("job_id"))
    node = _string(record.get("node"), f"{worker_label} node")
    _fail(str(process.get("slurm_job_id")) == job_id, f"{worker_label} job identity does not match serving process")
    api_pid = _int(process.get("pid"), f"{worker_label} API PID")
    api_start_ticks = _int(process.get("start_ticks"), f"{worker_label} API start ticks")
    _fail(api_pid > 0 and api_start_ticks > 0, f"{worker_label} API identity is not positive")
    alias = _string(options["--served-model-name"], f"{worker_label} served model alias")
    boot_id = _string(inventory.get("boot_id"), f"{worker_label} boot ID")
    hostname = _string(inventory.get("hostname"), f"{worker_label} hostname")

    gpu_result = _command_result(inventory.get("gpus"), f"{worker_label} GPU inventory")
    gpu = _parse_gpu(gpu_result)
    compute_result = _command_result(
        inventory.get("compute_processes"), f"{worker_label} compute-process inventory"
    )
    compute = _parse_compute(compute_result, gpu["uuid"])
    _fail(gpu["index"] == 0, f"{worker_label} GPU index is not zero")
    _fail(process.get("cuda_visible_devices") == "0", f"{worker_label} CUDA_VISIBLE_DEVICES is not 0")

    identity = {
        "worker_id": worker_id,
        "job_id": job_id,
        "node": node,
        "hostname": hostname,
        "boot_id": boot_id,
        "gpu_uuid": gpu["uuid"],
        "gpu_index": gpu["index"],
        "cuda_visible_devices": process["cuda_visible_devices"],
        "api_pid": api_pid,
        "api_start_ticks": api_start_ticks,
        "api_cgroup": process.get("cgroup"),
        "model_alias": alias,
        "local_port": LOCAL_PORT_BASE + int(worker_id),
        "remote_port": remote_port,
    }

    return worker_id, {
        "identity": identity,
        "options": options,
        "observed_max_model_len": observed_max_model_len,
        "gpu": gpu,
        "gpu_result": gpu_result,
        "compute": compute,
        "compute_result": compute_result,
        "serving_process": deepcopy(process),
        "source_node_record_index": node_record_index,
    }, record


def prepare_inputs(
    fingerprints_path: Path,
    runtime_template_path: Path,
    cpu_vm_profile_path: Path | None = None,
) -> dict[str, Any]:
    """Read and validate sources, returning a pure in-memory render plan."""

    fingerprints, fingerprints_sha256 = _read_json(fingerprints_path, "allocation-v4 fingerprints")
    _fail(
        fingerprints.get("schema_version") == FINGERPRINT_SCHEMA,
        "allocation fingerprints have an unsupported schema_version",
    )
    _fail(
        fingerprints.get("probe_scope") == "existing_allocation_overlap_step",
        "allocation fingerprints are outside the expected read-only overlap scope",
    )
    nodes = fingerprints.get("nodes")
    _fail(isinstance(nodes, list), "allocation fingerprints nodes are missing")
    template, template_sha256 = _read_json(runtime_template_path, "runtime-manifest example")
    _validate_runtime_template(template)
    cpu_binding, cpu_binding_sha256 = _cpu_vm_binding(cpu_vm_profile_path)

    workers: dict[str, dict[str, Any]] = {}
    excluded: list[dict[str, Any]] = []
    for index, record in enumerate(nodes):
        _fail(isinstance(record, dict), f"allocation record {index} is invalid")
        inventory = record.get("inventory")
        _fail(isinstance(inventory, dict), f"allocation record {index} inventory is missing")
        serving = inventory.get("serving_processes")
        _fail(isinstance(serving, list), f"allocation record {index} serving process list is missing")
        if not serving:
            excluded.append(_excluded_allocation(record))
            continue
        worker_id, worker, _ = _worker_record(record, index)
        _fail(worker_id not in workers, f"duplicate serving worker ID: {worker_id}")
        workers[worker_id] = worker

    _fail(
        tuple(sorted(workers)) == EXPECTED_WORKER_IDS,
        "allocation fingerprints do not contain exactly workers 00-10 and 12-22",
    )
    uuids = [worker["gpu"]["uuid"] for worker in workers.values()]
    _fail(len(set(uuids)) == len(uuids), "GPU UUID binding is not unique across the 22 workers")

    source_ref = _source_ref(fingerprints_path, fingerprints_sha256, fingerprints)
    hardware_profiles: dict[str, dict[str, Any]] = {}
    for worker_id in EXPECTED_WORKER_IDS:
        worker = workers[worker_id]
        identity = worker["identity"]
        hardware_profiles[worker_id] = {
            "schema_version": HARDWARE_SCHEMA,
            "status": "observed_identity_profile",
            "launchable": False,
            "worker_id": worker_id,
            "source": {
                **source_ref,
                "node_record_index": worker["source_node_record_index"],
            },
            "identity": identity,
            "gpu": worker["gpu"],
            "raw_actual_gpu_inventory": {
                "argv": worker["gpu_result"].get("argv"),
                "returncode": worker["gpu_result"].get("returncode"),
                "stdout": worker["gpu_result"].get("stdout"),
                "stderr": worker["gpu_result"].get("stderr"),
            },
            "compute_process": worker["compute"],
            "raw_compute_process_inventory": {
                "argv": worker["compute_result"].get("argv"),
                "returncode": worker["compute_result"].get("returncode"),
                "stdout": worker["compute_result"].get("stdout"),
                "stderr": worker["compute_result"].get("stderr"),
            },
            "observed_model_config": {
                "options": worker["options"],
                "served_model_name": identity["model_alias"],
                "model_path": worker["options"]["--model"],
                "max_model_len": worker["observed_max_model_len"],
            },
            "raw_serving_process": worker["serving_process"],
            "cpu_vm_profile": {
                "embedded": False,
                "status": cpu_binding["status"],
                "path": "../cpu_vm_profile_binding.json",
                "sha256": cpu_binding_sha256,
            },
            "unknown_measurements": [
                "gpu_memory_bandwidth",
                "cpu_frequency",
            ],
        }

    return {
        "fingerprints_path": fingerprints_path.resolve(),
        "fingerprints_sha256": fingerprints_sha256,
        "fingerprints": fingerprints,
        "source_ref": source_ref,
        "runtime_template_path": runtime_template_path.resolve(),
        "runtime_template_sha256": template_sha256,
        "runtime_template": template,
        "cpu_binding": cpu_binding,
        "cpu_binding_sha256": cpu_binding_sha256,
        "workers": workers,
        "hardware_profiles": hardware_profiles,
        "excluded_allocations": excluded,
    }


def _pending_binding(path: str) -> dict[str, str]:
    return {"status": "pending", "path": path, "sha256": PENDING_SHA256}


def _runtime_template(plan: dict[str, Any], worker_id: str, hardware_sha256: str) -> dict[str, Any]:
    worker = plan["workers"][worker_id]
    identity = worker["identity"]
    options = worker["options"]
    example = deepcopy(plan["runtime_template"])

    # Keep the example's schema, but make every release-bound field visibly
    # unresolved.  The wrapper schema and launchable=false prevent accidental
    # use by the final runtime entrypoint.
    example["required_branch"] = "<PENDING_CLEAN_SOURCE_BRANCH>"
    example["required_commit"] = "<PENDING_CLEAN_SOURCE_COMMIT>"
    example["repository_root"] = "<PENDING_CLEAN_SOURCE_ROOT>"
    example["model"]["name"] = identity["model_alias"]
    example["model"]["revision"] = "<PENDING_REMOTE_FIXED_MODEL_REVISION>"
    example["model"]["api_base"] = (
        f"http://127.0.0.1:{identity['local_port']}/v1"
    )
    example["pins"]["model_revision"] = "<PENDING_REMOTE_FIXED_MODEL_REVISION>"
    example["pins"]["tokenizer_revision"] = "<PENDING_REMOTE_FIXED_TOKENIZER_REVISION>"
    telemetry = example.get("runner", {}).get("telemetry")
    if isinstance(telemetry, dict):
        telemetry["remote_hardware_profile"] = {
            "path": "<PENDING_REMOTE_FIXED_HARDWARE_PROFILE>",
            "sha256": PENDING_SHA256,
        }
    example["hardware"] = {
        "gpu_names": [worker["gpu"]["name"]],
        "minimum_memory_mib": worker["gpu"]["memory_total_mib"],
        "compute_capability": worker["gpu"]["compute_capability"],
        "one_gpu_only": True,
        "probe_command": example.get("hardware", {}).get("probe_command"),
    }

    reason_codes = [
        "source_65k_binding_pending",
        "observer_65k_binding_pending",
        "remote_fixed_model_revision_pending",
        "observed_serving_max_model_len_32768_requires_65536_verification",
    ]
    return {
        "schema_version": TEMPLATE_SCHEMA,
        "status": "template_only",
        "launchable": False,
        "worker_id": worker_id,
        "source": plan["source_ref"],
        "hardware_profile": {
            "path": f"hardware-profiles/worker-{worker_id}.json",
            "sha256": hardware_sha256,
        },
        "allocation_identity": {
            "job_id": identity["job_id"],
            "node": identity["node"],
            "hostname": identity["hostname"],
            "boot_id": identity["boot_id"],
            "gpu_uuid": identity["gpu_uuid"],
            "gpu_index": identity["gpu_index"],
            "cuda_visible_devices": identity["cuda_visible_devices"],
            "api_pid": identity["api_pid"],
            "api_start_ticks": identity["api_start_ticks"],
            "api_cgroup": identity["api_cgroup"],
            "model_alias": identity["model_alias"],
        },
        "endpoints": {
            "local": {
                "host": "127.0.0.1",
                "port": identity["local_port"],
                "base_url": f"http://127.0.0.1:{identity['local_port']}/v1",
                "model_alias": identity["model_alias"],
                "status": "pending_runtime_observer",
            },
            "remote": {
                "node": identity["node"],
                "hostname": identity["hostname"],
                "bind_host": options["--host"],
                "port": identity["remote_port"],
                "base_url": f"http://127.0.0.1:{identity['remote_port']}/v1",
                "model_alias": identity["model_alias"],
                "status": "fingerprint_observed_no_endpoint_probe",
            },
        },
        "observed_serving": {
            "options": options,
            "max_model_len": worker["observed_max_model_len"],
            "model_path": options["--model"],
            "model_alias": identity["model_alias"],
        },
        "target_serving": {
            "max_model_len": TARGET_MAX_MODEL_LEN,
            "status": "pending_source_and_observer_verification",
        },
        "required_pending_bindings": {
            "source_65k": _pending_binding(PENDING_SOURCE_65K),
            "observer_65k": _pending_binding(PENDING_OBSERVER_65K),
            "remote_fixed_model_revision": _pending_binding(PENDING_FIXED_REVISION),
            "clean_source_commit": {
                "status": "pending",
                "value": "<PENDING_CLEAN_SOURCE_COMMIT>",
            },
        },
        "readiness": {
            "status": "not_ready",
            "launchable": False,
            "reason_codes": reason_codes,
            "observed_max_model_len": worker["observed_max_model_len"],
            "target_max_model_len": TARGET_MAX_MODEL_LEN,
        },
        "runtime_manifest_example_source": {
            "path": str(plan["runtime_template_path"]),
            "sha256": plan["runtime_template_sha256"],
            "schema_version": RUNTIME_MANIFEST_SCHEMA,
        },
        "runtime_manifest_template": example,
    }


def _write_json(path: Path, value: dict[str, Any]) -> str:
    payload = _canonical_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    digest = _sha256_bytes(payload)
    path.with_name(path.name + ".sha256").write_text(f"{digest}  {path.name}\n", encoding="utf-8")
    return digest


def write_plan(plan: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    """Write a prepared plan into a new directory and return its index."""

    output_dir = output_dir.expanduser()
    _fail(not output_dir.exists(), f"refusing to overwrite existing output directory: {output_dir}")
    output_dir.mkdir(parents=True)

    cpu_path = output_dir / "cpu_vm_profile_binding.json"
    cpu_sha256 = _write_json(cpu_path, plan["cpu_binding"])
    _fail(cpu_sha256 == plan["cpu_binding_sha256"], "CPU/VM binding hash changed during write")

    hardware_hashes: dict[str, str] = {}
    for worker_id in EXPECTED_WORKER_IDS:
        path = output_dir / "hardware-profiles" / f"worker-{worker_id}.json"
        hardware_hashes[worker_id] = _write_json(path, plan["hardware_profiles"][worker_id])

    template_hashes: dict[str, str] = {}
    templates: dict[str, dict[str, Any]] = {}
    for worker_id in EXPECTED_WORKER_IDS:
        template = _runtime_template(plan, worker_id, hardware_hashes[worker_id])
        templates[worker_id] = template
        path = output_dir / "runtime-templates" / f"worker-{worker_id}.json"
        template_hashes[worker_id] = _write_json(path, template)

    index = {
        "schema_version": OUTPUT_SCHEMA,
        "status": "staged_templates_not_launchable",
        "launchable": False,
        "worker_count": len(EXPECTED_WORKER_IDS),
        "worker_ids": list(EXPECTED_WORKER_IDS),
        "remote_ports": [REMOTE_PORT_BASE + int(worker_id) for worker_id in EXPECTED_WORKER_IDS],
        "local_ports": [LOCAL_PORT_BASE + int(worker_id) for worker_id in EXPECTED_WORKER_IDS],
        "sources": {
            "allocation_v4_fingerprints": plan["source_ref"],
            "runtime_manifest_example": {
                "path": str(plan["runtime_template_path"]),
                "sha256": plan["runtime_template_sha256"],
                "schema_version": RUNTIME_MANIFEST_SCHEMA,
            },
            "cpu_vm_profile_binding": {
                "path": "cpu_vm_profile_binding.json",
                "sha256": cpu_sha256,
                "embedded_in_gpu_profiles": False,
            },
        },
        "files": {
            "hardware_profiles": {
                worker_id: {
                    "path": f"hardware-profiles/worker-{worker_id}.json",
                    "sha256": hardware_hashes[worker_id],
                }
                for worker_id in EXPECTED_WORKER_IDS
            },
            "runtime_templates": {
                worker_id: {
                    "path": f"runtime-templates/worker-{worker_id}.json",
                    "sha256": template_hashes[worker_id],
                }
                for worker_id in EXPECTED_WORKER_IDS
            },
        },
        "excluded_allocations": plan["excluded_allocations"],
        "binding_policy": {
            "gpu_uuid": "unique_exact_observed_uuid",
            "allocation": "job_id_node_api_pid_api_start_ticks_from_fingerprint",
            "model_alias": "exact_observed_served_model_name",
            "local_endpoint": "127.0.0.1:181XX/v1_template_only",
            "remote_endpoint": "127.0.0.1:182XX/v1_observed_bind_template_only",
            "raw_actual_gpu_fields_preserved": [
                "observed_clocks_mhz",
                "driver_version",
                "memory_total_mib",
                "compute_capability",
                "power_limit_w",
                "temperature_gpu_c",
            ],
            "unmeasured_fields_left_unknown": ["gpu_memory_bandwidth", "cpu_frequency"],
        },
        "readiness": {
            "status": "not_ready",
            "launchable": False,
            "observed_serving_max_model_len": OBSERVED_MAX_MODEL_LEN,
            "required_target_max_model_len": TARGET_MAX_MODEL_LEN,
            "required_gates": [
                "source_65k_binding",
                "observer_65k_binding",
                "remote_fixed_model_revision_file",
                "clean_source_commit_and_integrity_render",
            ],
            "reason": "Current fingerprint flags are 32768; templates do not claim 65536 readiness.",
        },
        "first_case_inputs_after_gates": {
            "worker_id": "00",
            "runtime_template": "runtime-templates/worker-00.json",
            "hardware_profile": "hardware-profiles/worker-00.json",
            "cpu_vm_profile_binding": "cpu_vm_profile_binding.json",
            "source_fingerprint": plan["source_ref"],
            "required_materialization_before_use": [
                "source_65k_binding",
                "observer_65k_binding",
                "remote_fixed_model_revision_file",
                "clean_source_commit_and_integrity_render",
            ],
            "note": "Worker 00 is a concrete first-case input selection; this staged record remains non-launchable until all gates resolve.",
        },
        "actions_performed": [
            "read_allocation_v4_fingerprints",
            "read_runtime_manifest_example",
            "read_or_stage_separate_cpu_vm_profile_reference",
            "write_hashed_hardware_profiles",
            "write_hashed_non_launchable_runtime_templates",
        ],
        "actions_not_performed": [
            "API_probe",
            "GPU_job",
            "relay_change",
            "final_runtime_manifest_write",
            "source_or_observer_65k_verification",
        ],
    }
    index_path = output_dir / "worker_runtime_inputs_manifest.json"
    index_sha256 = _write_json(index_path, index)

    hash_index: dict[str, str] = {}
    for path in sorted(output_dir.rglob("*.json")):
        if path.name == "artifact_hashes.json":
            continue
        hash_index[str(path.relative_to(output_dir))] = _sha256_bytes(path.read_bytes())
    _write_json(output_dir / "artifact_hashes.json", {
        "schema_version": "assignment.worker-runtime-inputs-artifact-hashes.v1",
        "covers": hash_index,
        "index_sha256": index_sha256,
        "note": "This hash index excludes its own JSON and sidecar so it remains self-contained.",
    })
    return index


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fingerprints", type=Path, required=True)
    parser.add_argument("--runtime-template", type=Path, required=True)
    parser.add_argument("--cpu-vm-profile", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        plan = prepare_inputs(
            args.fingerprints,
            args.runtime_template,
            args.cpu_vm_profile,
        )
        index = write_plan(plan, args.output_dir)
    except (OSError, RenderError) as exc:
        print(f"render_worker_runtime_inputs: error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({
        "output_dir": str(args.output_dir.resolve()),
        "worker_count": index["worker_count"],
        "worker_ids": index["worker_ids"],
        "status": index["status"],
        "launchable": index["launchable"],
        "index_sha256": _sha256_bytes(
            (args.output_dir / "worker_runtime_inputs_manifest.json").read_bytes()
        ),
        "excluded_allocations": index["excluded_allocations"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
