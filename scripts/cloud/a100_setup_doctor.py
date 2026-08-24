#!/usr/bin/env python3
"""Fail-closed, read-only A100 setup and protocol doctor.

Offline mode validates the complete sealed contract without touching GPU,
Docker, Nsight, vLLM, or experiment artifacts. Live mode adds host checks but
never starts a service. The launcher owns service startup.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DEFAULT = ROOT / "configs/a100_final_validation.json"
RUNNER_DEFAULT = ROOT / "scripts/cloud/a100_case_runner.py"
PROVIDER_DEFAULT = ROOT / "scripts/cloud/a100_nsight_trace_provider.py"
H100_ARTIFACT_ROOT = ROOT / "artifacts/h100_final_validation"


class DoctorError(ValueError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DoctorError(f"cannot read JSON: {path}") from exc
    if not isinstance(value, dict):
        raise DoctorError(f"JSON root is not an object: {path}")
    return value


def read_manifest(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if "=" not in line:
            raise DoctorError(f"manifest line {number} is not KEY=VALUE")
        key, value = line.split("=", 1)
        if not key or key in values or any(ch.isspace() for ch in key):
            raise DoctorError(f"manifest key is duplicated or invalid at line {number}")
        values[key] = value
    return values


def validate_protocol(protocol: Mapping[str, Any]) -> None:
    if protocol.get("schema_version") != "a100-final-validation.v1":
        raise DoctorError("not the A100 protocol schema")
    if protocol.get("status") != "sealed_scaffold_not_launched":
        raise DoctorError("A100 protocol status changed")
    if protocol.get("launch_authorized") is not False:
        raise DoctorError("A100 launch guard must remain false in Git")
    hardware = protocol.get("hardware")
    if not isinstance(hardware, Mapping):
        raise DoctorError("hardware metadata is missing")
    if hardware.get("gpu_family") != "A100" or hardware.get("architecture") != "Ampere":
        raise DoctorError("A100 hardware family/architecture metadata is invalid")
    names = set(map(str, hardware.get("gpu_name_allowlist", [])))
    if names != {"NVIDIA A100-SXM4-80GB", "NVIDIA A100-PCIE-80GB"}:
        raise DoctorError("A100 80GB allowlist is not exact")
    if hardware.get("minimum_memory_mib") != 80000 or str(hardware.get("required_compute_capability")) != "8.0":
        raise DoctorError("A100 memory or compute capability guard is invalid")
    calibration = protocol.get("calibration_configs")
    holdouts = protocol.get("sealed_holdouts")
    if not isinstance(calibration, list) or len(calibration) != 24:
        raise DoctorError("A100 calibration matrix must contain exactly 24 cases")
    if not isinstance(holdouts, list) or len(holdouts) != 12:
        raise DoctorError("A100 holdout matrix must contain exactly 12 cases")
    if sum(row.get("holdout_kind") == "interpolation" for row in holdouts) != 8:
        raise DoctorError("A100 holdout matrix must contain 8 interpolation cases")
    if sum(row.get("holdout_kind") == "extrapolation" for row in holdouts) != 4:
        raise DoctorError("A100 holdout matrix must contain 4 extrapolation cases")
    ids = [row.get("case_id") for row in calibration + holdouts]
    if any(not isinstance(case_id, str) or not case_id for case_id in ids) or len(set(ids)) != 36:
        raise DoctorError("A100 case IDs are not unique")
    if any(row.get("split") != "calibration" for row in calibration) or any(
        row.get("split") != "sealed_holdout" for row in holdouts
    ):
        raise DoctorError("A100 split annotations are invalid")
    request = protocol.get("request_protocol", {})
    if not isinstance(request, Mapping) or (
        request.get("concurrency"),
        request.get("warmup_requests"),
        request.get("measured_repetitions_per_case"),
        request.get("repetition_ids"),
    ) != (1, 2, 3, ["r01", "r02", "r03"]):
        raise DoctorError("A100 request protocol is not serialized/2-warmup/3-repeat")
    features = {item.get("name") for item in protocol.get("features", []) if isinstance(item, Mapping)}
    required = {"prompt_tokens", "max_output_tokens", "context_tokens", "tool_calls", "hardware_score", "prompt_output_interaction", "concurrency", "warm_state"}
    if not required.issubset(features):
        raise DoctorError("A100 feature schema is incomplete")
    forbidden = set(protocol.get("leakage_boundaries", {}).get("forbidden_fit_inputs", []))
    if not any("holdout wall_ms" in item for item in forbidden) or not protocol.get("leakage_boundaries", {}).get("seal_before_run"):
        raise DoctorError("A100 leakage boundary is incomplete")
    if protocol.get("fit_specification", {}).get("prediction_artifact_required_before_holdout_join") is not True:
        raise DoctorError("A100 prediction-before-reveal guard is missing")
    safety = protocol.get("safety", {})
    if safety.get("max_total_wall_clock_seconds") != 14400 or safety.get("deadline_must_be_set_before_first_calibration_request") is not True:
        raise DoctorError("A100 hard wall-clock safety contract is missing")
    root = str(protocol.get("artifact_layout", {}).get("root", ""))
    if "h100" in root.lower() or root != "artifacts/a100_final_validation/":
        raise DoctorError("A100 artifact root is not separated from H100")


def validate_hardware_record(data: Mapping[str, Any], protocol: Mapping[str, Any]) -> dict[str, Any]:
    validate_protocol(protocol)
    hardware = protocol["hardware"]
    if data.get("gpu_name") not in set(hardware["gpu_name_allowlist"]):
        raise DoctorError("GPU is not an allowlisted A100 80GB")
    try:
        memory = int(data["memory_total_mib"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DoctorError("GPU memory is not numeric") from exc
    if memory < 80000 or str(data.get("compute_capability")) != "8.0":
        raise DoctorError("GPU does not satisfy A100 80GB/compute 8.0")
    if data.get("architecture") != "Ampere":
        raise DoctorError("GPU architecture metadata is not Ampere")
    if data.get("gpu_count", 1) != 1:
        raise DoctorError("exactly one GPU is required")
    if data.get("compute_processes", []) not in ([], None):
        raise DoctorError("GPU isolation failed: compute process is present")
    return {
        "gpu_name": data["gpu_name"],
        "memory_total_mib": memory,
        "compute_capability": "8.0",
        "architecture": "Ampere",
        "uuid": data.get("uuid", "unavailable-before-live-preflight"),
        "driver": data.get("driver", "unavailable-before-live-preflight"),
        "cuda": data.get("cuda", "unavailable-before-live-preflight"),
        "nsight": data.get("nsight", "unavailable-before-live-preflight"),
    }


def validate_manifest(values: Mapping[str, str], protocol: Mapping[str, Any], config: Path) -> None:
    required = {"REQUIRED_BRANCH", "REQUIRED_COMMIT", "PROTOCOL_SHA256", "PYTHON_LOCK_SHA256", "SYSTEM_LOCK_SHA256", "WORK_ROOT", "PYTHON_ENV_ROOT", "MODEL_CACHE", "MODEL_SNAPSHOT", "TRACE_ROOT", "ARTIFACT_ROOT", "RECOVERY_ROOT", "VLLM_MODEL", "VLLM_MODEL_REVISION", "VLLM_IMAGE", "A100_CONTAINER", "A100_NSYS_SESSION", "MAX_WALL_CLOCK_SECONDS", "REQUIRED_COMPUTE_CAPABILITY", "REQUIRED_ARCHITECTURE"}
    missing = required.difference(values)
    if missing:
        raise DoctorError("manifest missing: " + ", ".join(sorted(missing)))
    if values["REQUIRED_BRANCH"] != "parallel-h100-shards" or values["REQUIRED_COMMIT"] == "<40-hex-pushed-commit>":
        raise DoctorError("manifest branch/commit is not release-bound")
    if values["PROTOCOL_SHA256"] != sha256(config):
        raise DoctorError("A100 protocol hash does not match manifest")
    software = protocol["frozen_software"]
    for key, manifest_key in (("model", "VLLM_MODEL"), ("model_revision", "VLLM_MODEL_REVISION"), ("vllm_image", "VLLM_IMAGE"), ("vllm_tool_parser", "VLLM_TOOL_PARSER")):
        if values.get(manifest_key) != str(software[key]):
            raise DoctorError(f"manifest pin differs for {key}")
    for raw in (values["WORK_ROOT"], values["PYTHON_ENV_ROOT"], values["MODEL_CACHE"], values["MODEL_SNAPSHOT"], values["TRACE_ROOT"], values["ARTIFACT_ROOT"], values["RECOVERY_ROOT"]):
        resolved = os.path.abspath(os.path.expanduser(raw))
        if resolved == str(ROOT) or resolved.startswith(str(ROOT) + os.sep) or resolved.startswith(str(H100_ARTIFACT_ROOT) + os.sep):
            raise DoctorError("A100 runtime/artifact path is inside the checkout or H100 root")
    if values["ARTIFACT_ROOT"].rstrip("/") == values["RECOVERY_ROOT"].rstrip("/"):
        raise DoctorError("artifact and recovery roots must be distinct")
    if values["MAX_WALL_CLOCK_SECONDS"] != "14400" or values["REQUIRED_COMPUTE_CAPABILITY"] != "8.0" or values["REQUIRED_ARCHITECTURE"] != "Ampere":
        raise DoctorError("manifest safety or A100 hardware pins changed")


def check_live_host() -> dict[str, Any]:
    for command in ("nvidia-smi", "docker", "curl"):
        if shutil.which(command) is None:
            raise DoctorError(f"required command is unavailable: {command}")
    docker_info = subprocess.run(["docker", "info", "--format", "{{json .Runtimes}}"],
                                 check=True, capture_output=True, text=True, timeout=20).stdout.lower()
    if "nvidia" not in docker_info:
        raise DoctorError("Docker NVIDIA runtime is not registered")
    nsys = shutil.which("nsys") or ("/usr/local/cuda/bin/nsys" if Path("/usr/local/cuda/bin/nsys").is_file() else None)
    if not nsys:
        raise DoctorError("Nsight Systems executable is unavailable")
    nsys_version = subprocess.run([nsys, "--version"], check=True, capture_output=True, text=True, timeout=20)
    query = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,compute_cap", "--format=csv,noheader,nounits"], check=True, capture_output=True, text=True, timeout=15).stdout
    rows = [line.strip() for line in query.splitlines() if line.strip()]
    if len(rows) != 1:
        raise DoctorError(f"expected exactly one GPU, found {len(rows)}")
    name, memory, compute = [part.strip() for part in rows[0].split(",")]
    processes = subprocess.run(["nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader,nounits"], check=True, capture_output=True, text=True, timeout=15).stdout
    result = {"gpu_name": name, "memory_total_mib": int(float(memory)), "compute_capability": compute, "architecture": "Ampere", "compute_processes": [line for line in processes.splitlines() if line.strip()], "nsight": (nsys_version.stdout + nsys_version.stderr).strip()}
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG_DEFAULT)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--hardware-fixture", type=Path, help="JSON fixture for offline hardware-contract tests")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args(argv)
    try:
        protocol = read_json(args.config)
        validate_protocol(protocol)
        if args.manifest:
            validate_manifest(read_manifest(args.manifest), protocol, args.config)
        if args.hardware_fixture:
            validate_hardware_record(read_json(args.hardware_fixture), protocol)
        elif not args.offline:
            validate_hardware_record(check_live_host(), protocol)
        if not args.offline and args.manifest is None:
            raise DoctorError("live A100 preflight requires the external startup manifest")
        print("READY_FOR_A100_PREFLIGHT: protocol, split, leakage, pins, artifact separation, and safety checks passed")
        if args.offline:
            print("OFFLINE_ONLY: no GPU, Docker, Nsight, vLLM, or experiment artifact access")
        return 0
    except (DoctorError, OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"NOT_READY: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
