#!/usr/bin/env python3
"""Provider-neutral, fail-closed SWE-agent case runner for the assignment matrix.

The matrix executor owns ordering and resume state.  This adapter owns one case:
it validates an external JSON runtime manifest, builds the repository's reviewed
SWE-agent command, and delegates execution to ``agentic_sim.runners``.  It does
not source shell files, expand shell syntax, or silently substitute missing
dependencies.  Without ``--execute`` it performs validation only.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
import re
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from agentic_sim.runners.sweagent_runner import (  # noqa: E402
    RunnerConfig,
    RunnerContractError,
    _without_v2_activation,
    build_command,
    command_hash,
    run_sweagent,
)
from agentic_sim.runners.case_lifecycle import (  # noqa: E402
    deadline_environment,
    deadline_from_env,
    deadline_with_timeout,
    remaining_seconds,
    run_owned_process,
)
from agentic_sim.runners.owned_docker import cleanup_owned_containers  # noqa: E402
from agentic_sim.assignment.event_simulator import HardwareProfile  # noqa: E402
from agentic_sim.telemetry.clock import clock_metadata  # noqa: E402
from agentic_sim.telemetry import cpu_policy  # noqa: E402
from agentic_sim.telemetry.bpf_work import (  # noqa: E402
    BPF_EVENT_ABI,
    BPF_EVENT_SCHEMA,
    BPF_EVENT_SCHEMA_LEGACY,
    BPF_PATH_CAP,
    iter_bpf_events,
)
from agentic_sim.telemetry.hardware import (  # noqa: E402
    composite_hardware_id,
    local_cpu_profile,
    model_hardware_features,
)
from agentic_sim.telemetry.process_resources import (  # noqa: E402
    FIELDS as PROCESS_RESOURCE_FIELDS,
    SCHEMA as PROCESS_RESOURCE_SCHEMA,
)
from agentic_sim.telemetry.v2 import TelemetryV2  # noqa: E402
from scripts.assignment.adaptive_event_protocol import (  # noqa: E402
    FrozenCalibrationModel,
    verify_trajectory_prediction,
)


MANIFEST_SCHEMA = "assignment-runtime-manifest.v1"
CASE_SCHEMA = "assignment-steps-1-3-plan.v1"
PRODUCTION_CASE_SCHEMA = "assignment-production-v2-plan.v1"
CONFIRMATION_CASE_SCHEMA = "assignment.configuration-confirmation-case.v2"
CONFIRMATION_PLAN_SCHEMA = "assignment.configuration-confirmation-execution-plan.v2"
CONFIRMATION_PLAN_ID = "assignment-configuration-confirmation-v2-20260908"
CONFIRMATION_NAMESPACE = "assignment-configuration-confirmation-v2"
RESULT_SCHEMA = "assignment-case-result.v1"
FAILURE_RESULT_SCHEMA = "assignment-case-failure.v2"
CASE_OWNER_ENV = "ASSIGNMENT_CASE_OWNER"
CASE_OWNER_LABEL = "agentic.assignment.owner"
DOCKER_PROOF_MARKER_ENV = "ASSIGNMENT_DOCKER_PROOF_MARKER"
DOCKER_PROOF_PROTOCOL = "assignment-docker-ownership-proof.v1"
DOCKER_PARSER_CAPTURE_SCHEMA = "assignment-docker-parser-capture.v1"
STATE_SCHEMA = "assignment-case-runner-state.v1"
OFFICIAL_EVALUATOR_SCHEMA = "assignment-official-evaluator.v1"
SHA256_RE = re.compile(r"^[0-9a-f]{40}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
BPF_WORK_SUMMARY_SCHEMA = "assignment.linux-bpf-work-summary.v1"
BPF_WORK_RAW_SCHEMA = "assignment.linux-bpf-work-raw.v2"
BPF_WORK_EVENT_SCHEMA = BPF_EVENT_SCHEMA
BPF_WORK_LEGACY_EVENT_SCHEMA = "assignment.linux-bpf-work-event.v1"
BPF_WORK_EVENT_SCHEMAS = frozenset(
    {BPF_WORK_EVENT_SCHEMA, BPF_EVENT_SCHEMA_LEGACY, BPF_WORK_LEGACY_EVENT_SCHEMA}
)
SUITES = {"lite", "verified"}
SETTINGS = {"call_limit", "max_output_tokens", "observation_length", "temperature"}
OPTIONAL_SETTINGS = {"max_input_tokens", "top_p", "seed"}
FULL_SETTINGS = SETTINGS | OPTIONAL_SETTINGS
TOP_LEVEL = {
    "schema_version", "required_branch", "required_commit", "repository_root",
    "integrity", "pins", "datasets", "model", "runner", "evaluator", "hardware", "deadlines",
}
PIN_KEYS = {"model_revision", "tokenizer_revision", "swe_agent_revision", "swe_bench_revision", "vllm_version"}
TELEMETRY_V2_SCHEMA = "assignment.telemetry.v2"
TELEMETRY_V2_VERSION = "telemetry-v2-20260908"
TELEMETRY_CONFIG_FIELDS = {
    "mode",
    "schema_version",
    "instrumentation_version",
    "require_activation",
    "require_raw_request_payloads",
    "require_cpu_work",
    "cpu_work",
    "remote_hardware_profile",
}
TELEMETRY_OPTIONAL_CONFIG_FIELDS = {"serving_metrics", "native_server_archive"}
CPU_WORK_FIELDS = {
    "backend",
    "trace_format",
    "attach_existing_process",
    "require_persistent_runtime_pid",
}
SERVING_METRICS_CONFIG_SCHEMA = "assignment.serving-metrics-config.v1"
REMOTE_HARDWARE_FIELDS = {"path", "sha256"}
NATIVE_ARCHIVE_FETCH_FIELDS = {"ssh_host", "ssh_control", "journal", "native_journal", "remote_python"}


class CaseRunnerError(ValueError):
    """A manifest, case, environment, or output contract is unsafe."""


class EvidenceIntegrityError(CaseRunnerError):
    """Required v2 evidence is absent, corrupt, or not independently bound."""


def _fail(condition: bool, message: str) -> None:
    if not condition:
        raise CaseRunnerError(message)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise CaseRunnerError(f"cannot hash {path}: {exc}") from exc
    return digest.hexdigest()


def _verify_sidecar(path: Path, label: str) -> str:
    _fail(path.is_file() and not path.is_symlink(), f"{label} must be a regular file: {path}")
    digest = sha256_file(path)
    sidecar = Path(str(path) + ".sha256")
    _fail(sidecar.is_file() and not sidecar.is_symlink(), f"{label} SHA-256 sidecar is missing: {sidecar}")
    try:
        actual = sidecar.read_text(encoding="utf-8")
    except OSError as exc:
        raise CaseRunnerError(f"cannot read {label} sidecar {sidecar}: {exc}") from exc
    _fail(actual == f"{digest}  {path.name}\n", f"{label} or SHA-256 sidecar was tampered with")
    return digest


def _verify_recognized_sidecar(path: Path, label: str) -> str:
    """Verify exactly one of the two repository sidecar conventions."""
    _fail(path.is_file() and not path.is_symlink(), f"{label} must be a regular file: {path}")
    digest = sha256_file(path)
    candidates = list(dict.fromkeys((Path(str(path) + ".sha256"), path.with_suffix(".sha256"))))
    existing = [candidate for candidate in candidates if candidate.exists()]
    _fail(len(existing) == 1, f"{label} requires exactly one recognized SHA-256 sidecar")
    sidecar = existing[0]
    _fail(
        sidecar.is_file() and not sidecar.is_symlink(),
        f"{label} sidecar must be a regular file",
    )
    _fail(
        sidecar.read_text(encoding="utf-8") == f"{digest}  {path.name}\n",
        f"{label} or SHA-256 sidecar was tampered with",
    )
    return digest


def _verify_manifest_sidecar(path: Path) -> str:
    return _verify_sidecar(path, "runtime manifest")


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _atomic_bytes(path: Path, value: bytes) -> None:
    """Persist captured process bytes without exposing a partial artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CaseRunnerError(f"cannot read {label} {path}: {exc}") from exc
    _fail(isinstance(value, dict), f"{label} must be a JSON object: {path}")
    return value


def _string(value: Any, label: str, *, nonempty: bool = True) -> str:
    _fail(isinstance(value, str), f"{label} must be a string")
    if nonempty:
        _fail(bool(value.strip()), f"{label} must not be empty")
    return value


def _immutable_commit(value: Any, label: str) -> str:
    result = _string(value, label).lower()
    _fail(bool(SHA256_RE.fullmatch(result)), f"{label} must be a 40-character lowercase commit SHA")
    _fail(set(result) != {"0"}, f"{label} is still the zero placeholder")
    return result


def _relative_or_absolute(value: Any, label: str) -> str:
    result = _string(value, label)
    _fail("\x00" not in result, f"{label} contains a NUL")
    return result


def _legacy_telemetry_config() -> dict[str, Any]:
    """Return the explicit compatibility descriptor used by local fixtures.

    Production manifests must carry the v2 descriptor.  A small number of
    historical unit fixtures invoke this adapter with a fake executable and no
    instrumentation; those manifests are normalized to an explicit legacy
    mode so they cannot be mistaken for a v2 run.
    """

    return {
        "mode": "legacy",
        "schema_version": TELEMETRY_V2_SCHEMA,
        "instrumentation_version": "legacy",
        "require_activation": False,
        "require_raw_request_payloads": False,
        "require_cpu_work": False,
        "cpu_work": {
            "backend": "none",
            "trace_format": "unavailable",
            "attach_existing_process": False,
            "require_persistent_runtime_pid": False,
        },
        "remote_hardware_profile": {"path": None, "sha256": None},
        "serving_metrics": None,
        "native_server_archive": None,
    }


def _normalise_serving_metrics_config(value: Any, *, label: str) -> dict[str, Any]:
    """Use the proxy's canonical serving-metrics boundary validator.

    The case runner owns the manifest binding, while the proxy owns the
    capture implementation.  Calling the same validator here prevents a
    manifest from being accepted with a descriptor the child proxy would
    interpret differently.  ``None`` is the explicit disabled descriptor.
    """

    try:
        from scripts.observability.request_proxy import _validate_serving_metrics_config

        result = _validate_serving_metrics_config(value)
    except (ImportError, TypeError, ValueError) as exc:
        raise CaseRunnerError(f"{label} is invalid: {exc}") from exc
    _fail(
        isinstance(result, dict)
        and result.get("schema_version") == SERVING_METRICS_CONFIG_SCHEMA,
        f"{label} has an unsupported schema",
    )
    return dict(result)


def _serving_metrics_config_sha256(value: Mapping[str, Any]) -> str:
    """Hash the exact canonical object bytes used by the proxy collector."""

    return hashlib.sha256(_canonical(dict(value)).encode("utf-8")).hexdigest()


def _normalise_native_server_archive(value: Any, *, label: str, required: bool) -> dict[str, Any] | None:
    """Validate the post-run, hash-bound native serving archive descriptor.

    The proxy only receives the serving-metrics object.  This sibling
    descriptor is consumed after the request window closes, when the runner
    fetches the immutable ASGI/native journals once and derives request rows.
    No polling or inference occurs during retrieval.
    """

    if value is None:
        _fail(not required, f"{label} is required for native_deferred serving attribution")
        return None
    _fail(isinstance(value, Mapping), f"{label} must be a mapping")
    descriptor = dict(value)
    has_fetch = "fetch" in descriptor
    has_local = "path" in descriptor or "sha256" in descriptor
    _fail(has_fetch != has_local, f"{label} must select exactly one of fetch or path/sha256")
    if has_fetch:
        _fail(set(descriptor) == {"fetch"}, f"{label}.fetch descriptor has unknown fields")
        fetch = descriptor["fetch"]
        _fail(isinstance(fetch, Mapping), f"{label}.fetch must be a mapping")
        _fail(
            set(fetch) in (NATIVE_ARCHIVE_FETCH_FIELDS - {"remote_python"}, NATIVE_ARCHIVE_FETCH_FIELDS),
            f"{label}.fetch has unknown or missing fields",
        )
        fetch = dict(fetch)
        for key in ("ssh_host", "journal", "native_journal"):
            _fail(isinstance(fetch.get(key), str) and bool(fetch[key].strip()), f"{label}.fetch.{key} is required")
        for key in ("journal", "native_journal"):
            _fail(Path(fetch[key]).is_absolute(), f"{label}.fetch.{key} must be absolute")
        _fail(
            isinstance(fetch.get("ssh_control"), str)
            and Path(fetch["ssh_control"]).is_absolute()
            and bool(fetch["ssh_control"].strip()),
            f"{label}.fetch.ssh_control must be an absolute path",
        )
        if "remote_python" in fetch:
            _fail(isinstance(fetch["remote_python"], str) and bool(fetch["remote_python"].strip()), f"{label}.fetch.remote_python is invalid")
        descriptor["fetch"] = fetch
        return descriptor
    _fail(set(descriptor) == {"path", "sha256"}, f"{label} local descriptor has unknown or missing fields")
    _fail(isinstance(descriptor["path"], str) and Path(descriptor["path"]).is_absolute(), f"{label}.path must be absolute")
    _fail(isinstance(descriptor["sha256"], str) and bool(HEX64_RE.fullmatch(descriptor["sha256"].lower())), f"{label}.sha256 is invalid")
    return descriptor


def _validate_telemetry_config(value: Any, *, label: str = "runner.telemetry") -> dict[str, Any]:
    _fail(
        isinstance(value, dict)
        and TELEMETRY_CONFIG_FIELDS <= set(value)
        and set(value) <= TELEMETRY_CONFIG_FIELDS | TELEMETRY_OPTIONAL_CONFIG_FIELDS,
        f"{label} has invalid fields",
    )
    value = dict(value)
    value["serving_metrics"] = _normalise_serving_metrics_config(
        value.get("serving_metrics"), label=f"{label}.serving_metrics"
    )
    native_mode = value["serving_metrics"].get("mode") == "native_deferred"
    value["native_server_archive"] = _normalise_native_server_archive(
        value.get("native_server_archive"),
        label=f"{label}.native_server_archive",
        required=native_mode,
    )
    mode = value["mode"]
    _fail(mode in {"v2", "legacy"}, f"{label}.mode must be v2 or legacy")
    _fail(value["schema_version"] == TELEMETRY_V2_SCHEMA, f"{label}.schema_version is unsupported")
    _string(value["instrumentation_version"], f"{label}.instrumentation_version")
    for key in ("require_activation", "require_raw_request_payloads", "require_cpu_work"):
        _fail(isinstance(value[key], bool), f"{label}.{key} must be boolean")
    cpu = value["cpu_work"]
    _fail(isinstance(cpu, dict) and set(cpu) == CPU_WORK_FIELDS, f"{label}.cpu_work has invalid fields")
    _string(cpu["backend"], f"{label}.cpu_work.backend")
    _string(cpu["trace_format"], f"{label}.cpu_work.trace_format")
    for key in ("attach_existing_process", "require_persistent_runtime_pid"):
        _fail(isinstance(cpu[key], bool), f"{label}.cpu_work.{key} must be boolean")
    profile = value["remote_hardware_profile"]
    _fail(
        isinstance(profile, dict) and set(profile) == REMOTE_HARDWARE_FIELDS,
        f"{label}.remote_hardware_profile has invalid fields",
    )
    if mode == "legacy":
        _fail(
            value["require_activation"] is False
            and value["require_raw_request_payloads"] is False
            and value["require_cpu_work"] is False,
            f"{label} legacy mode cannot require v2 evidence",
        )
        _fail(cpu["backend"] == "none", f"{label} legacy mode must disable CPU collection")
        _fail(profile["path"] is None and profile["sha256"] is None, f"{label} legacy mode cannot bind remote hardware")
        return value
    _fail(value["instrumentation_version"] == TELEMETRY_V2_VERSION, f"{label} has unsupported instrumentation version")
    _fail(value["require_activation"] is True, f"{label} must require child activation")
    _fail(value["require_raw_request_payloads"] is True, f"{label} must require raw request payloads")
    _fail(value["require_cpu_work"] is True, f"{label} must require CPU work collection")
    # The production candidate is the compact BCC kernel aggregate collector.
    # Keep the descriptor name backend-neutral at the manifest boundary so the
    # host can report its concrete BCC implementation, but reject the old
    # diagnostic strace/legacy backends for a required v2 run.  A later backend
    # must emit the same raw-boundary contract before it can be admitted here.
    _fail(cpu["backend"] in {"kernel_aggregate", "bcc"}, f"{label}.cpu_work.backend must select the reviewed BCC kernel aggregate collector")
    trace_format = cpu["trace_format"].lower()
    _fail(
        trace_format not in {"unavailable", ""}
        and "raw" in trace_format
        and "individual" in trace_format,
        f"{label}.cpu_work.trace_format must require retained individual raw operation records",
    )
    _fail(cpu["attach_existing_process"] is True, f"{label}.cpu_work must attach an existing runtime process")
    _fail(cpu["require_persistent_runtime_pid"] is True, f"{label}.cpu_work must require a persistent runtime PID")
    profile_path = profile["path"]
    _fail(isinstance(profile_path, str) and bool(profile_path.strip()), f"{label}.remote_hardware_profile.path is required")
    _fail(Path(profile_path).is_absolute(), f"{label}.remote_hardware_profile.path must be absolute")
    digest = profile["sha256"]
    _fail(isinstance(digest, str) and bool(HEX64_RE.fullmatch(digest.lower())) and set(digest.lower()) != {"0"}, f"{label}.remote_hardware_profile.sha256 must be a non-zero SHA-256")
    return value


def _manifest_telemetry(manifest: Mapping[str, Any]) -> dict[str, Any]:
    runner = manifest.get("runner")
    if not isinstance(runner, Mapping):
        raise CaseRunnerError("runtime manifest runner is missing")
    value = runner.get("telemetry")
    if value is None:
        # Compatibility is deliberately narrow: a production reviewed project
        # must opt in explicitly.  Existing fake executable fixtures remain
        # legacy and are never reported as v2 evidence.
        executable = str(runner.get("executable", ""))
        project = str(runner.get("project", ""))
        _fail(
            Path(executable).name != "sweagent" and not project.rstrip("/").endswith("/SWE-agent"),
            "runner.telemetry is required for the reviewed production runner",
        )
        value = _legacy_telemetry_config()
        if isinstance(runner, dict):
            runner["telemetry"] = value
    return _validate_telemetry_config(value)


def _load_remote_hardware_profile(telemetry: Mapping[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Read and hash the sealed remote profile referenced by a v2 manifest."""

    if telemetry.get("mode") != "v2":
        return {}, None
    descriptor = telemetry["remote_hardware_profile"]
    path = Path(str(descriptor["path"])).expanduser().resolve()
    _fail(path.is_file() and not path.is_symlink(), f"remote hardware profile is unavailable: {path}")
    digest = sha256_file(path)
    _fail(digest == str(descriptor["sha256"]).lower(), "remote hardware profile SHA-256 does not match the manifest")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CaseRunnerError(f"remote hardware profile is not valid JSON: {path}: {exc}") from exc
    _fail(isinstance(value, dict), "remote hardware profile must be a JSON object")
    _fail(isinstance(value.get("schema_version"), str) and value["schema_version"].startswith("assignment."), "remote hardware profile schema_version is missing")
    return value, digest


def _model_hardware_from_remote_profile(profile: Mapping[str, Any], *, clock: Mapping[str, Any]) -> dict[str, Any]:
    """Project remote GPU terms; remote CPU metadata is not tool-host evidence.

    The agent's CPU tools can run on another host. Keep that frequency
    explicitly unavailable until a separately bound tool-host value exists;
    retain the complete remote profile as raw inventory only.
    """

    def number(*names: str) -> float | int | None:
        for name in names:
            value = profile.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if math.isfinite(float(value)) and float(value) >= 0:
                return value
        return None

    cpu = None
    cpu_source = None
    bandwidth = number("gpu_memory_bandwidth_bytes_per_s")
    if bandwidth is None:
        gbps = number("gpu_memory_bandwidth_gbps")
        if gbps is not None:
            bandwidth = float(gbps) * 1_000_000_000
    compute = number("gpu_compute_tflops")
    if compute is None:
        compute = number("gpu_bf16_tflops")
    projection = {
        "cpu_frequency_hz": cpu,
        "cpu_frequency_source": cpu_source,
        "gpu_memory_bandwidth_bytes_per_s": bandwidth,
        "gpu_compute_tflops": compute,
        "clock": dict(clock),
        "availability": {
            "cpu_frequency_hz": "declared" if cpu is not None else "unavailable",
            "gpu_memory_bandwidth_bytes_per_s": "declared" if bandwidth is not None else "unavailable",
            "gpu_compute_tflops": "declared" if compute is not None else "unavailable",
        },
    }
    return model_hardware_features(projection)


def load_manifest(path: Path) -> dict[str, Any]:
    manifest = _read_json(path, "runtime manifest")
    _fail(set(manifest) == TOP_LEVEL, "runtime manifest has missing or unknown top-level fields")
    _fail(manifest.get("schema_version") == MANIFEST_SCHEMA, "unsupported runtime manifest schema")
    _string(manifest["required_branch"], "required_branch")
    _immutable_commit(manifest["required_commit"], "required_commit")
    _relative_or_absolute(manifest["repository_root"], "repository_root")

    integrity = manifest["integrity"]
    integrity_keys = {
        "case_runner_path", "case_runner_sha256", "evaluator_adapter_path",
        "evaluator_adapter_sha256", "request_config_path", "request_config_sha256",
        "request_proxy_path", "request_proxy_sha256",
        "adaptive_runner_path", "adaptive_runner_sha256",
        "adaptive_runtime_path", "adaptive_runtime_sha256",
        "adaptive_protocol_path", "adaptive_protocol_sha256",
        "event_simulator_path", "event_simulator_sha256",
    }
    _fail(isinstance(integrity, dict) and set(integrity) == integrity_keys, "integrity has invalid fields")
    for key in (
        "case_runner_path", "evaluator_adapter_path", "request_config_path", "request_proxy_path",
        "adaptive_runner_path", "adaptive_runtime_path", "adaptive_protocol_path", "event_simulator_path",
    ):
        _relative_or_absolute(integrity[key], f"integrity.{key}")
    for key in (
        "case_runner_sha256", "evaluator_adapter_sha256", "request_config_sha256", "request_proxy_sha256",
        "adaptive_runner_sha256", "adaptive_runtime_sha256", "adaptive_protocol_sha256", "event_simulator_sha256",
    ):
        digest = _string(integrity[key], f"integrity.{key}").lower()
        _fail(bool(HEX64_RE.fullmatch(digest)), f"integrity.{key} must be SHA-256")
        _fail(set(digest) != {"0"}, f"integrity.{key} is still the zero placeholder")

    pins = manifest["pins"]
    _fail(isinstance(pins, dict) and set(pins) == PIN_KEYS, "pins must contain exactly the required pin fields")
    for key in PIN_KEYS - {"vllm_version"}:
        _immutable_commit(pins[key], f"pins.{key}")
    _string(pins["vllm_version"], "pins.vllm_version")

    datasets = manifest["datasets"]
    _fail(isinstance(datasets, dict) and set(datasets) == SUITES, "datasets must contain exactly lite and verified")
    for suite in sorted(SUITES):
        dataset = datasets[suite]
        dataset_fields = {"name", "revision", "instances_path", "sha256"}
        _fail(isinstance(dataset, dict) and dataset_fields <= set(dataset) <= dataset_fields | {"source_parquet_sha256"}, f"datasets.{suite} has invalid fields")
        _string(dataset["name"], f"datasets.{suite}.name")
        _immutable_commit(dataset["revision"], f"datasets.{suite}.revision")
        _relative_or_absolute(dataset["instances_path"], f"datasets.{suite}.instances_path")
        digest = _string(dataset["sha256"], f"datasets.{suite}.sha256").lower()
        _fail(bool(HEX64_RE.fullmatch(digest)), f"datasets.{suite}.sha256 must be SHA-256")
        if "source_parquet_sha256" in dataset:
            source_digest = _string(dataset["source_parquet_sha256"], f"datasets.{suite}.source_parquet_sha256")
            _fail(bool(HEX64_RE.fullmatch(source_digest)) and set(source_digest) != {"0"},
                  f"datasets.{suite}.source_parquet_sha256 must be non-placeholder SHA-256")

    model = manifest["model"]
    _fail(isinstance(model, dict) and set(model) == {"name", "revision", "api_base", "api_key"}, "model has invalid fields")
    _string(model["name"], "model.name")
    _immutable_commit(model["revision"], "model.revision")
    _string(model["api_base"], "model.api_base")
    parsed = urlparse(model["api_base"])
    _fail(parsed.scheme in {"http", "https"} and bool(parsed.netloc), "model.api_base must be an HTTP(S) URL")
    _string(model["api_key"], "model.api_key")
    _fail(model["api_key"] in {"EMPTY", "$VLLM_API_KEY", "${VLLM_API_KEY}"}, "model.api_key must be EMPTY or the literal VLLM_API_KEY marker")
    _fail(model["revision"] == pins["model_revision"], "model.revision disagrees with pins.model_revision")

    runner = manifest["runner"]
    runner_fields = {"executable", "project", "config_path", "request_config_path", "working_directory", "extra_args"}
    _fail(
        isinstance(runner, dict)
        and runner_fields <= set(runner) <= runner_fields | {"telemetry", "cpu_policy", "tool_runtime"},
        "runner has invalid fields",
    )
    for key in runner_fields - {"extra_args"}:
        _relative_or_absolute(runner[key], f"runner.{key}")
    _fail(isinstance(runner["extra_args"], list) and all(isinstance(item, str) for item in runner["extra_args"]), "runner.extra_args must be a string list")
    _fail(all("\x00" not in item for item in runner["extra_args"]), "runner.extra_args contains a NUL")
    _manifest_telemetry(manifest)
    if "cpu_policy" in runner:
        cpu_policy.validate_policy(runner["cpu_policy"])
    if "tool_runtime" in runner:
        tool_runtime = runner["tool_runtime"]
        _fail(isinstance(tool_runtime, dict) and set(tool_runtime) == {"manifest_path", "manifest_sha256", "config_sha256"},
              "runner.tool_runtime has invalid fields")
        _relative_or_absolute(tool_runtime["manifest_path"], "runner.tool_runtime.manifest_path")
        for field in ("manifest_sha256", "config_sha256"):
            digest = _string(tool_runtime[field], f"runner.tool_runtime.{field}")
            _fail(bool(HEX64_RE.fullmatch(digest)) and set(digest) != {"0"},
                  f"runner.tool_runtime.{field} must be a non-placeholder SHA-256")

    evaluator = manifest["evaluator"]
    evaluator_fields = {"command", "project", "result_path", "resolved_field", "submitted_field"}
    _fail(isinstance(evaluator, dict) and set(evaluator) == evaluator_fields, "evaluator has invalid fields")
    _fail(isinstance(evaluator["command"], list) and bool(evaluator["command"]) and all(isinstance(item, str) and item for item in evaluator["command"]), "evaluator.command must be a non-empty argv list")
    _relative_or_absolute(evaluator["project"], "evaluator.project")
    _relative_or_absolute(evaluator["result_path"], "evaluator.result_path")
    _string(evaluator["resolved_field"], "evaluator.resolved_field")
    _string(evaluator["submitted_field"], "evaluator.submitted_field")

    hardware = manifest["hardware"]
    hardware_fields = {"gpu_names", "minimum_memory_mib", "compute_capability", "one_gpu_only", "probe_command"}
    _fail(isinstance(hardware, dict) and set(hardware) == hardware_fields, "hardware has invalid fields")
    _fail(isinstance(hardware["gpu_names"], list) and bool(hardware["gpu_names"]) and all(isinstance(item, str) and item for item in hardware["gpu_names"]), "hardware.gpu_names must be a non-empty string list")
    _fail(isinstance(hardware["minimum_memory_mib"], int) and not isinstance(hardware["minimum_memory_mib"], bool) and hardware["minimum_memory_mib"] > 0, "hardware.minimum_memory_mib must be positive")
    _string(hardware["compute_capability"], "hardware.compute_capability")
    _fail(hardware["one_gpu_only"] is True, "hardware.one_gpu_only must be true")
    _fail(isinstance(hardware["probe_command"], list) and bool(hardware["probe_command"]) and all(isinstance(item, str) and item for item in hardware["probe_command"]), "hardware.probe_command must be a non-empty argv list")

    deadlines = manifest["deadlines"]
    _fail(isinstance(deadlines, dict) and set(deadlines) == {"per_case_seconds", "global_seconds"}, "deadlines has invalid fields")
    for key in deadlines:
        _fail(isinstance(deadlines[key], int) and not isinstance(deadlines[key], bool) and deadlines[key] > 0, f"deadlines.{key} must be a positive integer")
    _fail(deadlines["global_seconds"] >= deadlines["per_case_seconds"], "global deadline must cover one case")
    return manifest


def _verify_execution_integrity(manifest: Mapping[str, Any], repo: Path) -> dict[str, str]:
    integrity = manifest["integrity"]
    paths = {
        "case_runner": _strict_path(integrity["case_runner_path"], "integrity.case_runner_path"),
        "evaluator_adapter": _strict_path(integrity["evaluator_adapter_path"], "integrity.evaluator_adapter_path"),
        "request_config": _strict_path(integrity["request_config_path"], "integrity.request_config_path"),
        "request_proxy": _strict_path(integrity["request_proxy_path"], "integrity.request_proxy_path"),
        "adaptive_runner": _strict_path(integrity["adaptive_runner_path"], "integrity.adaptive_runner_path"),
        "adaptive_runtime": _strict_path(integrity["adaptive_runtime_path"], "integrity.adaptive_runtime_path"),
        "adaptive_protocol": _strict_path(integrity["adaptive_protocol_path"], "integrity.adaptive_protocol_path"),
        "event_simulator": _strict_path(integrity["event_simulator_path"], "integrity.event_simulator_path"),
    }
    expected_paths = {
        "case_runner": Path(__file__).resolve(),
        "evaluator_adapter": (repo / "scripts/assignment/evaluate_swebench_case.py").resolve(),
        "request_config": _resolve(repo, str(manifest["runner"]["request_config_path"])),
        "request_proxy": (repo / "scripts/observability/request_proxy.py").resolve(),
        "adaptive_runner": (repo / "scripts/assignment/sweagent_adaptive_runner.py").resolve(),
        "adaptive_runtime": (repo / "scripts/assignment/adaptive_runtime.py").resolve(),
        "adaptive_protocol": (repo / "scripts/assignment/adaptive_event_protocol.py").resolve(),
        "event_simulator": (repo / "src/agentic_sim/assignment/event_simulator.py").resolve(),
    }
    result: dict[str, str] = {}
    for name, path in paths.items():
        _fail(path == expected_paths[name], f"integrity.{name}_path does not match the reviewed execution path")
        _fail(path.is_file() and not path.is_symlink(), f"reviewed {name} is not a regular file")
        digest = sha256_file(path)
        _fail(digest == integrity[f"{name}_sha256"], f"reviewed {name} SHA-256 mismatch")
        result[f"{name}_sha256"] = digest
    evaluator_command = manifest["evaluator"]["command"]
    _fail(len(evaluator_command) >= 2, "evaluator command must name the reviewed adapter")
    _fail(Path(evaluator_command[1]).expanduser().resolve() == paths["evaluator_adapter"], "evaluator command does not invoke the reviewed adapter")
    return result


def load_case(
    path: Path, *, confirmation_plan: Path | None = None,
    confirmation_plan_sha256: str | None = None,
    runtime_manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    case = _read_json(path, "case specification")
    schema = case.get("schema_version")
    historical_required = {"record_type", "schema_version", "plan_id", "steps", "roles", "suite", "instance_id", "repository", "task_sha256", "source_manifest_sha256", "cell_id", "settings", "variation", "concurrency", "per_case_deadline_seconds", "resume_key"}
    production_required = {
        "record_type", "schema_version", "plan_id", "namespace", "candidate_id", "case_id", "resume_key",
        "historical_template_case_id", "historical_template_plan_id", "fresh_case_id", "production_case",
        "confirmation_case", "holdout_instance_id", "selection_outcome_blind", "suite", "repository",
        "instance_id", "task_sha256", "source_manifest_sha256", "cell_id", "steps", "roles", "settings",
        "final_configuration", "serving_configuration", "variation", "concurrency", "per_case_deadline_seconds",
    }
    confirmation_required = {
        "record_type", "schema_version", "plan_id", "namespace", "case_id", "resume_key",
        "candidate_id", "panel_case_id", "historical_template_case_id", "suite", "repository",
        "instance_id", "task_sha256", "source_manifest_sha256", "cell_id", "settings",
        "final_configuration", "serving_configuration", "instrumentation", "outcome_blind",
        "outcomes_accessed", "source_case_spec_sha256", "case_spec_sha256",
    }
    required = (confirmation_required if schema == CONFIRMATION_CASE_SCHEMA else
                production_required if schema == PRODUCTION_CASE_SCHEMA else historical_required)
    _fail(set(case) == required, "case specification has missing or unknown fields")
    _fail(case["record_type"] == "case" and case["schema_version"] in {CASE_SCHEMA, PRODUCTION_CASE_SCHEMA, CONFIRMATION_CASE_SCHEMA}, "unsupported case specification")
    if schema != CONFIRMATION_CASE_SCHEMA:
        _fail(confirmation_plan is None and confirmation_plan_sha256 is None, "confirmation plan requires a confirmation case")
    _fail(case["suite"] in SUITES, "case suite must be lite or verified")
    for key in ("plan_id", "instance_id", "repository", "cell_id", "resume_key"):
        _string(case[key], f"case.{key}")
    for key in ("task_sha256", "source_manifest_sha256"):
        value = _string(case[key], f"case.{key}").lower()
        _fail(bool(HEX64_RE.fullmatch(value)), f"case.{key} must be SHA-256")
    _fail(
        isinstance(case["settings"], dict)
        and set(case["settings"]) in (SETTINGS, FULL_SETTINGS),
        "case.settings must contain either the four legacy knobs or the complete seven-key runtime configuration",
    )
    settings = case["settings"]
    for key in ("call_limit", "max_output_tokens", "observation_length"):
        _fail(isinstance(settings[key], int) and not isinstance(settings[key], bool) and settings[key] > 0, f"case.settings.{key} must be positive")
    _fail(isinstance(settings["temperature"], (int, float)) and not isinstance(settings["temperature"], bool) and math.isfinite(float(settings["temperature"])) and 0 <= settings["temperature"] <= 2, "case.settings.temperature is invalid")
    if set(settings) == FULL_SETTINGS:
        _fail(isinstance(settings["max_input_tokens"], int) and not isinstance(settings["max_input_tokens"], bool) and settings["max_input_tokens"] > 0, "case.settings.max_input_tokens must be positive")
        _fail(isinstance(settings["top_p"], (int, float)) and not isinstance(settings["top_p"], bool) and math.isfinite(float(settings["top_p"])) and 0 <= settings["top_p"] <= 1, "case.settings.top_p is invalid")
        _fail(isinstance(settings["seed"], int) and not isinstance(settings["seed"], bool) and settings["seed"] >= 0, "case.settings.seed must be a non-negative integer")
    if schema == CONFIRMATION_CASE_SCHEMA:
        return _bind_confirmation_case(
            path, case, plan_path=confirmation_plan, expected_plan_sha256=confirmation_plan_sha256,
            runtime_manifest=runtime_manifest,
        )
    if schema == PRODUCTION_CASE_SCHEMA:
        _fail(case["namespace"] == "assignment-production-v2", "production case namespace is unsupported")
        _fail(isinstance(case["candidate_id"], str) and bool(case["candidate_id"].strip()), "production case candidate_id is required")
        _fail(
            isinstance(case["case_id"], str)
            and re.fullmatch(r"assignment-production-v2:[0-9a-f]{64}", case["case_id"]) is not None
            and case["resume_key"] == case["case_id"],
            "production case must have a fresh stable case identity",
        )
        _fail(
            isinstance(case["historical_template_case_id"], str)
            and re.fullmatch(r"assignment-case-v1:[0-9a-f]{64}", case["historical_template_case_id"]) is not None,
            "production case historical lineage identity is invalid",
        )
        _fail(case["historical_template_plan_id"] == "assignment-steps-1-3", "production case historical plan lineage is invalid")
        _fail(case["fresh_case_id"] is True and case["production_case"] is True and case["confirmation_case"] is False, "production case identity metadata is invalid")
        _fail(case["holdout_instance_id"] == "sympy__sympy-12481" and case["selection_outcome_blind"] is True, "production case holdout metadata is invalid")
        _fail(set(settings) == FULL_SETTINGS, "production case settings must contain all seven runtime keys")
        _fail(case["final_configuration"] == settings, "production case final_configuration differs from settings")
        _fail(case["serving_configuration"] == {"max_model_len": 65536, "vllm_version": "0.10.0"}, "production case serving configuration is unsupported")
        _fail(case["concurrency"] == 1, "production case concurrency must be 1")
    _fail(case["concurrency"] == 1, "case concurrency must be 1")
    _fail(isinstance(case["per_case_deadline_seconds"], int) and not isinstance(case["per_case_deadline_seconds"], bool) and case["per_case_deadline_seconds"] > 0, "case deadline must be a positive integer")
    return case


def _bind_confirmation_case(
    path: Path, case: dict[str, Any], *, plan_path: Path | None,
    expected_plan_sha256: str | None, runtime_manifest: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Validate a declared confirmation coordinate and derive only execution metadata.

    The caller supplies the reviewed plan hash; adjacent sidecars alone are not
    a trust anchor. Neither the staged case nor any planning artifact is edited.
    """
    _fail(plan_path is not None, "confirmation case requires --confirmation-plan")
    _fail(isinstance(expected_plan_sha256, str) and bool(HEX64_RE.fullmatch(expected_plan_sha256)),
          "confirmation case requires --confirmation-plan-sha256")
    assert plan_path is not None
    _fail(_verify_sidecar(plan_path, "confirmation plan") == expected_plan_sha256,
          "confirmation plan SHA-256 differs from the reviewed binding")
    plan_path = plan_path.resolve()
    plan = _read_json(plan_path, "confirmation plan")
    _fail(plan.get("schema_version") == CONFIRMATION_PLAN_SCHEMA
          and plan.get("plan_id") == case["plan_id"] == CONFIRMATION_PLAN_ID
          and plan.get("namespace") == case["namespace"] == CONFIRMATION_NAMESPACE
          and plan.get("case_schema") == CONFIRMATION_CASE_SCHEMA,
          "confirmation plan/schema identity is unsupported")
    _fail(isinstance(runtime_manifest, Mapping), "confirmation case requires its runtime manifest")
    assert runtime_manifest is not None
    _fail(_manifest_telemetry(runtime_manifest)["mode"] == "v2", "confirmation requires full v2 telemetry")
    pins = plan.get("pins")
    runtime_pins = runtime_manifest.get("pins")
    _fail(isinstance(pins, Mapping) and isinstance(runtime_pins, Mapping), "confirmation pins are missing")
    for key in PIN_KEYS:
        _fail(isinstance(pins.get(key), str) and pins[key] == runtime_pins.get(key),
              f"confirmation runtime pin {key} differs from the plan")

    def semantic_hash(value: Any) -> str:
        return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()

    def reference(base: Path, descriptor: Any, label: str) -> Path:
        _fail(isinstance(descriptor, Mapping), f"{label} descriptor is missing")
        name = descriptor.get("path")
        digest = descriptor.get("sha256")
        _fail(isinstance(name, str) and bool(name), f"{label} path is missing")
        _fail(isinstance(digest, str) and bool(HEX64_RE.fullmatch(digest)), f"{label} hash is invalid")
        candidate = Path(name)
        if not candidate.is_absolute():
            candidate = base / candidate
        _fail(not any(parent.is_symlink() for parent in (candidate, *candidate.parents)),
              f"{label} must not traverse a symlink")
        resolved = candidate.resolve()
        _fail(_inside(resolved, plan_path.parent.parent) and resolved.is_file(),
              f"{label} is missing or escapes the planning snapshot")
        _fail(sha256_file(resolved) == digest, f"{label} SHA-256 mismatch")
        return resolved

    def index_rows(value: Any, key: str, count: int, label: str) -> dict[str, Mapping[str, Any]]:
        _fail(isinstance(value, list) and len(value) == count, f"{label} must contain {count} rows")
        result: dict[str, Mapping[str, Any]] = {}
        for row in value:
            _fail(isinstance(row, Mapping), f"{label} row is malformed")
            identity = row.get(key)
            _fail(isinstance(identity, str) and bool(identity) and identity not in result,
                  f"{label} has a duplicate or missing {key}")
            result[identity] = row
        return result

    bindings = plan.get("source_bindings")
    _fail(isinstance(bindings, Mapping), "confirmation plan source bindings are missing")
    panel_path = reference(plan_path.parent, bindings.get("configuration_panel"), "confirmation panel")
    panel = _read_json(panel_path, "confirmation panel")
    _fail(panel.get("schema_version") == "assignment.configuration-confirmation-panel.v1",
          "confirmation panel schema is unsupported")
    panel_meta = panel.get("panel")
    _fail(isinstance(panel_meta, Mapping) and panel_meta.get("instance_count") == 24
          and panel_meta.get("candidate_case_count") == 96
          and panel_meta.get("holdout_accessed") is False
          and panel_meta.get("final_run_outcomes_used") is False,
          "confirmation panel counts or outcome-blind declaration are invalid")
    candidates = index_rows(panel.get("candidates"), "candidate_id", 4, "confirmation candidates")
    instances = index_rows(panel_meta.get("instances"), "panel_case_id", 24, "confirmation instances")
    _fail(len({row.get("instance_id") for row in instances.values()}) == 24
          and all(row.get("instance_id") != panel_meta.get("holdout_instance_id") for row in instances.values()),
          "confirmation instance membership duplicates or includes the holdout")
    declared = index_rows(panel.get("candidate_cases"), "candidate_case_id", 96, "confirmation panel cases")
    executions = index_rows(plan.get("cases"), "candidate_case_id", 96, "confirmation execution cases")
    _fail(plan.get("execution_case_count") == 96 and plan.get("panel_instance_count") == 24
          and plan.get("candidate_count") == 4 and set(declared) == set(executions),
          "confirmation execution membership differs from the panel")
    pairs = {(row.get("panel_case_id"), row.get("candidate_id")) for row in declared.values()}
    _fail(pairs == {(instance, candidate) for instance in instances for candidate in candidates},
          "confirmation panel does not cover the declared 24 by 4 coordinates")
    for identity, row in declared.items():
        derived = "configuration-confirmation-case-v1:" + semantic_hash({
            "candidate_id": row.get("candidate_id"), "panel_case_id": row.get("panel_case_id"),
        })
        _fail(identity == derived, "confirmation candidate identity does not match its coordinate")
        for key in ("candidate_id", "panel_case_id", "instance_id", "suite", "repository", "historical_template_case_id"):
            _fail(executions[identity].get(key) == row.get(key), f"confirmation execution membership {key} differs")
    _fail(case["case_id"] == case["resume_key"] and case["case_id"] in executions,
          "confirmation case is not a declared member")
    execution = executions[case["case_id"]]
    declared_case = declared[case["case_id"]]
    candidate = candidates[declared_case["candidate_id"]]
    instance = instances[declared_case["panel_case_id"]]
    original_path = reference(plan_path.parent, execution.get("case_spec"), "confirmation case spec")
    _fail(path.is_file() and not path.is_symlink() and sha256_file(path) == sha256_file(original_path),
          "staged confirmation case bytes differ from the declared case spec")
    for key in ("candidate_id", "panel_case_id", "instance_id", "suite", "repository", "historical_template_case_id"):
        _fail(case[key] == declared_case.get(key), f"confirmation case {key} differs from panel membership")
    for key in ("instance_id", "suite", "repository", "historical_template_case_id"):
        _fail(case[key] == instance.get(key), f"confirmation source instance {key} differs")
    _fail(case["outcome_blind"] is True and case["outcomes_accessed"] is False,
          "confirmation case must retain its outcome-blind declaration")
    _fail(set(case["settings"]) == FULL_SETTINGS, "confirmation requires all seven settings")
    for descriptor in (candidate, declared_case):
        _fail(_canonical(descriptor.get("settings")) == _canonical(case["settings"])
              and _canonical(descriptor.get("final_configuration")) == _canonical(case["settings"])
              and descriptor.get("settings_sha256") == semantic_hash(case["settings"]),
              "confirmation candidate settings/hash mismatch")
    _fail(_canonical(execution.get("settings")) == _canonical(case["settings"])
          and _canonical(case["final_configuration"]) == _canonical(case["settings"]),
          "confirmation execution settings mismatch")
    serving = {"max_model_len": 65536, "vllm_version": pins["vllm_version"]}
    for descriptor in (case, candidate, declared_case, execution):
        _fail(_canonical(descriptor.get("serving_configuration")) == _canonical(serving),
              "confirmation serving configuration mismatch")
    _fail(case["instrumentation"] == plan.get("instrumentation") == {
        "feature_schema": "assignment.d9-feature.v2", "instrumentation_version": TELEMETRY_V2_VERSION,
        "manifest_schema": "assignment.telemetry.v2.manifest", "schema_version": TELEMETRY_V2_SCHEMA,
    }, "confirmation instrumentation binding mismatch")

    source = instance.get("source_case_spec")
    _fail(isinstance(source, Mapping), "confirmation source case specification is missing")
    source_digest = semantic_hash(source)
    _fail(source_digest == instance.get("source_case_spec_sha256")
          == declared_case.get("source_case_spec_sha256") == case["source_case_spec_sha256"],
          "confirmation source case hash lineage mismatch")
    _fail(source.get("resume_key") == case["historical_template_case_id"],
          "confirmation historical identity differs from source case")
    for key in ("instance_id", "suite", "repository", "task_sha256", "source_manifest_sha256", "cell_id"):
        _fail(case[key] == source.get(key), f"confirmation source lineage {key} mismatch")
    lineage_digest = semantic_hash({"candidate_id": case["candidate_id"], "settings": case["settings"],
                                    "serving_configuration": case["serving_configuration"], "source_case_spec": source})
    _fail(lineage_digest == declared_case.get("case_spec_sha256") == case["case_spec_sha256"],
          "confirmation candidate case hash lineage mismatch")
    selection = panel.get("selection")
    _fail(isinstance(selection, Mapping) and isinstance(selection.get("seed"), str),
          "confirmation selection seed is missing")
    _fail(case["panel_case_id"] == "configuration-confirmation-panel-v1:" + semantic_hash({
        "case_id": case["historical_template_case_id"], "panel_role": instance.get("panel_role"), "seed": selection["seed"],
    }), "confirmation panel identity hash lineage mismatch")
    request_path = reference(plan_path.parent, execution.get("request_config"), "confirmation request config")
    request = _read_json(request_path, "confirmation request config")
    agent = request.get("agent")
    model = agent.get("model") if isinstance(agent, Mapping) else None
    completion = model.get("completion_kwargs") if isinstance(model, Mapping) else None
    _fail(isinstance(completion, Mapping), "confirmation request config completion settings are missing")
    for key, setting in (("max_tokens", "max_output_tokens"), ("top_p", "top_p"), ("seed", "seed")):
        _fail(_canonical(completion.get(key)) == _canonical(case["settings"][setting]),
              f"confirmation request config {key} differs from settings")

    deadlines = runtime_manifest.get("deadlines")
    _fail(isinstance(deadlines, Mapping), "confirmation runtime deadline is missing")
    runtime_deadline = deadlines.get("per_case_seconds")
    _fail(isinstance(runtime_deadline, int) and not isinstance(runtime_deadline, bool) and runtime_deadline > 0,
          "confirmation runtime deadline must be a positive integer")
    deadline = source.get("per_case_deadline_seconds", runtime_deadline)
    _fail(isinstance(deadline, int) and not isinstance(deadline, bool) and deadline > 0,
          "confirmation declared deadline must be a positive integer")
    _fail(deadline == runtime_deadline, "confirmation declared deadline disagrees with runtime manifest")
    runner = plan.get("runner")
    _fail(isinstance(runner, Mapping) and type(runner.get("concurrency")) is int and runner["concurrency"] == 1,
          "confirmation execution requires declared concurrency 1")
    return {
        **case, "concurrency": 1, "per_case_deadline_seconds": deadline,
        "steps": [], "roles": ["configuration_confirmation"], "variation": None,
        "confirmation_binding": {
            "plan_path": str(plan_path), "plan_sha256": expected_plan_sha256,
            "panel_path": str(panel_path), "panel_sha256": sha256_file(panel_path),
            "case_spec_sha256": sha256_file(original_path), "request_config_sha256": sha256_file(request_path),
            "source_case_spec_sha256": source_digest, "candidate_case_spec_sha256": lineage_digest,
            "deadline_source": ("panel.source_case_spec.per_case_deadline_seconds"
                                if "per_case_deadline_seconds" in source else "runtime.deadlines.per_case_seconds"),
            "per_case_deadline_seconds": deadline,
        },
    }


def _resolve(root: Path, value: str) -> Path:
    path = Path(value)
    return (path if path.is_absolute() else root / path).resolve()


def _git(repo: Path, args: Sequence[str]) -> str:
    try:
        result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CaseRunnerError(f"git preflight failed: {exc}") from exc
    if result.returncode != 0:
        raise CaseRunnerError(f"git preflight failed for {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout.strip()


def validate_checkout(manifest: Mapping[str, Any]) -> tuple[Path, dict[str, str]]:
    repo = Path(str(manifest["repository_root"])).expanduser()
    if not repo.is_absolute():
        repo = (Path.cwd() / repo).resolve()
    _fail(repo.is_dir() and (repo / ".git").exists(), f"repository_root is not a Git checkout: {repo}")
    branch = _git(repo, ["branch", "--show-current"])
    commit = _git(repo, ["rev-parse", "HEAD"]).lower()
    dirty = _git(repo, ["status", "--porcelain", "--untracked-files=all"])
    _fail(branch == manifest["required_branch"], f"wrong Git branch: expected {manifest['required_branch']}, got {branch}")
    _fail(commit == manifest["required_commit"], f"wrong Git commit: expected {manifest['required_commit']}, got {commit}")
    _fail(not dirty, "working tree is dirty; commit or remove tracked and untracked changes before execution")
    return repo, {"branch": branch, "commit": commit}


def _validate_tool_runtime(manifest: Mapping[str, Any], repo: Path) -> dict[str, Any] | None:
    reference = manifest["runner"].get("tool_runtime")
    if reference is None:
        return None
    from agentic_sim.runners.tool_runtime import validate_tool_runtime_bundle

    try:
        config_path = _resolve(repo, manifest["runner"]["config_path"])
        _fail(sha256_file(config_path) == reference["config_sha256"],
              "isolated tool runtime config SHA-256 mismatch")
        return validate_tool_runtime_bundle(
            _resolve(repo, reference["manifest_path"]), reference["manifest_sha256"],
            expected_config_path=config_path,
            expected_swe_agent_revision=manifest["pins"]["swe_agent_revision"],
        )
    except (ValueError, OSError) as exc:
        raise CaseRunnerError(f"isolated tool runtime validation failed: {exc}") from exc


def _retain_tool_runtime_inputs(manifest: Mapping[str, Any], repo: Path, output_dir: Path) -> list[dict[str, Any]]:
    """Retain every verified bundle byte, not just a pointer to a live directory."""
    reference = manifest["runner"].get("tool_runtime")
    if reference is None:
        return []
    _validate_tool_runtime(manifest, repo)
    source_manifest = _resolve(repo, reference["manifest_path"])
    source_root = source_manifest.parent
    destination = output_dir / "execution_inputs" / "tool_runtime"
    retained = []
    for source in sorted(source_root.rglob("*")):
        _fail(not source.is_symlink(), f"tool runtime input is a symlink: {source}")
        if source.is_dir():
            continue
        _fail(source.is_file(), f"tool runtime input is not a regular file: {source}")
        relative = source.relative_to(source_root)
        target = destination / relative
        _fail(_inside(target, output_dir), f"tool runtime artifact escapes its case: {relative}")
        _fail(not any(parent.is_symlink() for parent in target.parents if parent.is_relative_to(output_dir)),
              f"tool runtime artifact parent is a symlink: {relative}")
        _fail(not target.is_symlink(), f"tool runtime artifact is a symlink: {target}")
        digest = sha256_file(source)
        source_mode = source.stat().st_mode & 0o777
        if target.exists():
            _fail(target.is_file() and sha256_file(target) == digest
                  and target.stat().st_mode & 0o777 == source_mode,
                  f"tool runtime artifact collision: {relative}")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            _copy_native_archive(source, target)
            target.chmod(source_mode)
        _fail(sha256_file(source) == sha256_file(target) == digest,
              f"tool runtime input changed during retention: {relative}")
        retained.append({"path": str(target.relative_to(output_dir)), "sha256": digest,
                         "size": target.stat().st_size, "mode": source_mode})
    _validate_tool_runtime(manifest, repo)
    return retained


def validate_static_environment(manifest: Mapping[str, Any], case: Mapping[str, Any], repo: Path) -> dict[str, Any]:
    _fail(case["per_case_deadline_seconds"] == manifest["deadlines"]["per_case_seconds"], "case deadline disagrees with runtime manifest")
    datasets = manifest["datasets"][case["suite"]]
    for key in ("config_path", "request_config_path"):
        path = _resolve(repo, str(manifest["runner"][key]))
        _fail(path.is_file(), f"runner.{key} is unavailable: {path}")
    project_path = _resolve(repo, str(manifest["runner"]["project"]))
    _fail(project_path.is_dir(), f"runner.project is unavailable: {project_path}")
    _fail((project_path / ".git").exists(), f"runner.project is not a Git checkout: {project_path}")
    runner_revision = _git(project_path, ["rev-parse", "HEAD"]).lower()
    _fail(runner_revision == manifest["pins"]["swe_agent_revision"], "runner.project SWE-agent revision mismatch")
    _fail(not _git(project_path, ["status", "--porcelain", "--untracked-files=all"]), "runner.project SWE-agent checkout is dirty")
    tool_runtime_proof = _validate_tool_runtime(manifest, repo)
    evaluator_project = _resolve(repo, str(manifest["evaluator"]["project"]))
    _fail(evaluator_project.is_dir() and (evaluator_project / ".git").exists(), f"evaluator.project is not a Git checkout: {evaluator_project}")
    evaluator_revision = _git(evaluator_project, ["rev-parse", "HEAD"]).lower()
    _fail(evaluator_revision == manifest["pins"]["swe_bench_revision"], "evaluator.project SWE-bench revision mismatch")
    _fail(not _git(evaluator_project, ["status", "--porcelain", "--untracked-files=all"]), "evaluator.project SWE-bench checkout is dirty")
    executable = str(manifest["runner"]["executable"])
    if not manifest["runner"]["project"] and ("/" in executable or executable.startswith(".")):
        _fail(_resolve(repo, executable).is_file(), f"runner.executable is unavailable: {executable}")
    elif not manifest["runner"]["project"]:
        _fail(shutil.which(executable) is not None, f"runner.executable is not on PATH: {executable}")
    instances_path = _resolve(repo, str(datasets["instances_path"]))
    _fail(instances_path.is_file(), f"dataset instances_path is unavailable: {instances_path}")
    _fail(sha256_file(instances_path) == datasets["sha256"], "dataset instances_path SHA-256 mismatch")
    for line_number, line in enumerate(instances_path.read_text(encoding="utf-8").splitlines(), 1):
        _fail(bool(line.strip()), f"dataset has a blank line at {line_number}: {instances_path}")
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CaseRunnerError(f"dataset is not JSONL at line {line_number}: {exc}") from exc
        _fail(isinstance(row, dict), f"dataset line {line_number} is not an object")
    return {
        "instances_path": str(instances_path),
        "dataset_revision": datasets["revision"],
        "dataset_name": datasets["name"],
        "dataset_sha256": datasets["sha256"],
        "source_parquet_sha256": datasets.get("source_parquet_sha256"),
        "swe_agent_revision": runner_revision,
        "swe_bench_revision": evaluator_revision,
        **({"tool_runtime": tool_runtime_proof} if tool_runtime_proof is not None else {}),
    }


def _evaluator_image(instance_id: str) -> str:
    """Return the Docker-compatible SWE-bench image name for an instance."""
    return f"swebench/sweb.eval.x86_64.{instance_id.replace('__', '_1776_')}:latest".lower()


def _vllm_client_model(model: str) -> str:
    """Select LiteLLM's vLLM transport without changing the served model ID."""
    return model if model.startswith("hosted_vllm/") else f"hosted_vllm/{model}"


def _materialize_runner_instances(*, source: Path, instance_id: str, output_dir: Path) -> tuple[Path, str]:
    """Create the one-row SWE-agent input while preserving the raw dataset binding.

    The pinned SWE-agent ``InstancesFromFile`` loader consumes its already-
    normalized ``SimpleBatchInstance`` schema and requires ``image_name``.
    Pinned SWE-bench JSONL rows intentionally omit that derived runtime field.
    Keep the source dataset unchanged for provenance/evaluation, and write an
    auditable one-row runtime view for SWE-agent instead of weakening either
    contract or relying on an implicit loader conversion.
    """
    try:
        rows = [
            json.loads(line)
            for line in source.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CaseRunnerError(f"cannot materialize SWE-agent dataset view: {source}: {exc}") from exc
    _fail(all(isinstance(row, dict) for row in rows), "SWE-bench dataset rows must be JSON objects")
    matches = [dict(row) for row in rows if row.get("instance_id") == instance_id]
    _fail(len(matches) == 1, f"dataset must contain exactly one row for {instance_id}; found {len(matches)}")
    row = matches[0]
    expected_image = _evaluator_image(instance_id)
    _fail(
        row.get("image_name") in (None, expected_image),
        f"dataset image_name conflicts for {instance_id}",
    )
    row["image_name"] = expected_image
    row["repo_name"] = "testbed"
    path = output_dir / "runner_inputs" / "sweagent_instances.json"
    _atomic_json(path, [row])
    return path, sha256_file(path)


def _materialize_request_config(
    *,
    source: Path,
    max_output_tokens: int,
    output_dir: Path,
    top_p: float = 1.0,
    seed: int = 0,
) -> tuple[Path, str]:
    """Derive an immutable per-case request fragment from the reviewed template.

    The assignment sweeps ``max_output_tokens``.  SWE-agent reads that value
    from the final ``--config`` fragment, while the runner guard receives it
    on the command line.  Keeping the checked-in template fixed at its
    baseline value makes sweep cells fail before launch, so each case gets a
    case-local fragment with the exact planned value.  The template itself is
    still verified by the runtime manifest and the derived fragment is
    included in the attempt artifacts.
    """
    try:
        config = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CaseRunnerError(f"cannot materialize request config: {source}: {exc}") from exc
    _fail(isinstance(config, dict), "request config template must be a JSON object")
    agent = config.get("agent")
    _fail(isinstance(agent, dict), "request config template lacks agent mapping")
    model = agent.get("model")
    _fail(isinstance(model, dict), "request config template lacks agent.model mapping")
    completion = model.get("completion_kwargs")
    _fail(isinstance(completion, dict), "request config template lacks completion_kwargs mapping")
    _fail(isinstance(max_output_tokens, int) and not isinstance(max_output_tokens, bool) and max_output_tokens > 0, "request max_output_tokens must be positive")
    _fail(isinstance(top_p, (int, float)) and not isinstance(top_p, bool) and math.isfinite(float(top_p)) and 0 <= top_p <= 1, "request top_p is invalid")
    _fail(isinstance(seed, int) and not isinstance(seed, bool) and seed >= 0, "request seed must be a non-negative integer")
    completion["max_tokens"] = max_output_tokens
    # Pinned SWE-agent passes top_p explicitly to litellm.completion. Putting
    # it in completion_kwargs duplicates the keyword before HTTP dispatch.
    completion.pop("top_p", None)
    model["top_p"] = float(top_p)
    completion["seed"] = seed
    path = output_dir / "request_config.json"
    _atomic_json(path, config)
    return path, sha256_file(path)


def _probe_hardware(manifest: Mapping[str, Any], *, cpu_docker: bool = False) -> dict[str, Any]:
    if cpu_docker:
        return {
            "command": [],
            "gpus": [],
            "execution_mode": "cpu-docker-runner+h100-inference",
        }
    command = [str(item) for item in manifest["hardware"]["probe_command"]]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CaseRunnerError(f"hardware probe failed: {exc}") from exc
    _fail(result.returncode == 0, f"hardware probe failed: {result.stderr.strip()}")
    rows = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    _fail(rows, "hardware probe returned no GPUs")
    parsed: list[dict[str, Any]] = []
    for row in rows:
        fields = [part.strip() for part in row.split(",")]
        _fail(len(fields) >= 3, "hardware probe output must be name,memory_mib,compute_capability CSV")
        try:
            memory = int(float(fields[1]))
        except ValueError as exc:
            raise CaseRunnerError(f"hardware probe memory is invalid: {fields[1]}") from exc
        parsed.append({"name": fields[0], "memory_mib": memory, "compute_capability": fields[2]})
    hardware = manifest["hardware"]
    _fail(len(parsed) == 1, "hardware isolation failed: exactly one GPU must be visible")
    gpu = parsed[0]
    _fail(gpu["name"] in hardware["gpu_names"], f"wrong GPU: {gpu['name']}")
    _fail(gpu["memory_mib"] >= hardware["minimum_memory_mib"], "GPU memory is below the manifest requirement")
    _fail(gpu["compute_capability"] == hardware["compute_capability"], "GPU compute capability does not match the manifest")
    return {"command": command, "gpus": parsed}


def _format_argv(argv: Sequence[str], values: Mapping[str, str]) -> list[str]:
    result: list[str] = []
    allowed = set(values)
    for token in argv:
        _fail("\x00" not in token, "command contains a NUL")
        # Explicitly reject unknown braces so a typo cannot silently reach a shell-like tool.
        fields = set(re.findall(r"\{([A-Za-z0-9_]+)\}", token))
        _fail(fields.issubset(allowed), f"command contains an unknown placeholder: {sorted(fields - allowed)}")
        result.append(token.format(**values))
    return result


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _artifact_kind(path: Path) -> str:
    name = path.name.lower()
    if "trajectory" in name:
        return "trajectory"
    if "request" in name or "proxy" in name:
        return "request"
    if "eval" in name or "result" in name:
        return "evaluator"
    return "runner"


def _strict_path(value: Any, label: str) -> Path:
    """Return a resolved path only after rejecting non-string path claims."""
    return Path(_string(value, label)).expanduser().resolve()


def _validate_adaptive_config(
    path: Path,
    *,
    manifest_path: Path,
    manifest_sha256: str,
    run_id: str,
    repo: Path,
    output_dir: Path,
) -> dict[str, Any]:
    path = path.expanduser().resolve()
    _fail(not _inside(path, repo), "adaptive runtime config must live outside the repository")
    digest = _verify_sidecar(path, "adaptive runtime config")
    value = _read_json(path, "adaptive runtime config")
    required = {
        "schema_version", "run_id", "protocol_root", "calibration_model_path",
        "split_manifest_path", "runtime_manifest_path", "hardware_profile_path",
        "bindings", "tokenizer", "pre_trajectory_e2e",
    }
    _fail(set(value) == required, "adaptive runtime config has missing or unknown fields")
    _fail(value.get("schema_version") == "assignment.adaptive-runtime-config.v1", "unsupported adaptive runtime config")
    _fail(value.get("run_id") == run_id, "adaptive runtime config run_id does not match this case")
    claimed_manifest = _strict_path(value.get("runtime_manifest_path"), "adaptive runtime runtime_manifest_path")
    _fail(claimed_manifest == manifest_path, "adaptive runtime config does not bind the active runtime manifest")
    bindings = value.get("bindings")
    _fail(isinstance(bindings, dict), "adaptive runtime config bindings are missing")
    _fail(bindings.get("runtime_manifest_sha256") == manifest_sha256, "adaptive runtime manifest SHA-256 binding mismatch")
    protocol_root = _strict_path(value.get("protocol_root"), "adaptive runtime protocol_root")
    _fail(not _inside(protocol_root, repo), "adaptive protocol root must live outside the repository")
    _fail(_inside(protocol_root, output_dir), "adaptive protocol root must stay inside this case output-dir")
    pre = value.get("pre_trajectory_e2e")
    _fail(
        isinstance(pre, dict)
        and set(pre) == {"predicted_ms", "prediction_artifact_path", "prediction_artifact_sha256"},
        "adaptive runtime requires a hash-bound pre-reveal E2E prediction",
    )
    predicted = pre.get("predicted_ms")
    _fail(isinstance(predicted, (int, float)) and not isinstance(predicted, bool) and predicted > 0, "adaptive E2E prediction must be positive")
    prediction_path = _strict_path(
        pre.get("prediction_artifact_path"),
        "adaptive runtime prediction_artifact_path",
    )
    _fail(not _inside(prediction_path, repo), "adaptive E2E prediction must live outside the repository")
    prediction_sha = _verify_recognized_sidecar(
        prediction_path,
        "adaptive E2E prediction artifact",
    )
    _fail(
        pre.get("prediction_artifact_sha256") == prediction_sha,
        "adaptive E2E prediction artifact hash does not match its config binding",
    )

    split_path = _strict_path(value.get("split_manifest_path"), "adaptive split_manifest_path")
    hardware_path = _strict_path(value.get("hardware_profile_path"), "adaptive hardware_profile_path")
    model_path = _strict_path(value.get("calibration_model_path"), "adaptive calibration_model_path")
    runtime_path = _strict_path(value.get("runtime_manifest_path"), "adaptive runtime_manifest_path")
    _fail(runtime_path == manifest_path, "adaptive runtime manifest path does not match the active manifest")
    runtime_sha = _verify_recognized_sidecar(runtime_path, "adaptive runtime manifest")
    _fail(runtime_sha == manifest_sha256, "adaptive runtime manifest hash binding mismatch")
    split_sha = _verify_recognized_sidecar(split_path, "adaptive split manifest")
    hardware_sha = _verify_recognized_sidecar(hardware_path, "adaptive hardware profile")
    model_sha = _verify_recognized_sidecar(model_path, "adaptive calibration model")
    _fail(
        set(bindings) == {
            "split_manifest_sha256",
            "runtime_manifest_sha256",
            "hardware_profile_sha256",
            "model_revision_sha256",
        },
        "adaptive runtime bindings are incomplete",
    )
    _fail(bindings.get("split_manifest_sha256") == split_sha, "adaptive split manifest hash binding mismatch")
    _fail(bindings.get("runtime_manifest_sha256") == runtime_sha, "adaptive runtime manifest hash binding mismatch")
    _fail(bindings.get("hardware_profile_sha256") == hardware_sha, "adaptive hardware profile hash binding mismatch")
    try:
        hardware = HardwareProfile.from_mapping(_read_json(hardware_path, "adaptive hardware profile"))
        calibration_model = FrozenCalibrationModel.load(model_path)
        runtime_manifest = _read_json(runtime_path, "adaptive runtime manifest")
    except (CaseRunnerError, RunnerContractError, ValueError, OSError) as exc:
        raise CaseRunnerError(f"adaptive dependency validation failed: {exc}") from exc
    _fail(
        runtime_manifest.get("schema_version") == MANIFEST_SCHEMA,
        "adaptive runtime manifest schema is invalid",
    )
    model_bindings = calibration_model.bindings()
    _fail(
        model_bindings == {
            "split_manifest_sha256": split_sha,
            "runtime_manifest_sha256": runtime_sha,
            "hardware_profile_sha256": hardware_sha,
            "model_revision_sha256": hashlib.sha256(
                str(runtime_manifest.get("model", {}).get("revision", "")).encode("utf-8")
            ).hexdigest(),
        },
        "adaptive calibration model bindings are inconsistent",
    )
    tokenizer = value.get("tokenizer")
    _fail(
        isinstance(tokenizer, dict)
        and set(tokenizer) == {"snapshot_path", "revision", "required_files_sha256"},
        "adaptive tokenizer binding is incomplete",
    )
    snapshot = _strict_path(tokenizer.get("snapshot_path"), "adaptive tokenizer snapshot")
    _fail(snapshot.is_dir(), "adaptive tokenizer snapshot must be a directory")
    file_hashes = tokenizer.get("required_files_sha256")
    _fail(isinstance(file_hashes, dict) and file_hashes, "adaptive tokenizer file hashes are missing")
    for name, expected_hash in sorted(file_hashes.items()):
        _fail(isinstance(name, str) and Path(name).name == name, "adaptive tokenizer file name is unsafe")
        candidate = snapshot / name
        sidecars = list(
            dict.fromkeys((Path(str(candidate) + ".sha256"), candidate.with_suffix(".sha256")))
        )
        existing_sidecars = [sidecar for sidecar in sidecars if sidecar.exists()]
        actual_hash = (
            _verify_recognized_sidecar(candidate, f"adaptive tokenizer file {name}")
            if existing_sidecars
            else sha256_file(candidate)
        )
        _fail(actual_hash == expected_hash, f"adaptive tokenizer file hash mismatch: {name}")
    try:
        prediction, verified_prediction_sha = verify_trajectory_prediction(
            prediction_path,
            calibration_model,
            run_id=run_id,
            hardware=hardware.to_mapping(),
        )
    except ValueError as exc:
        raise CaseRunnerError(f"adaptive E2E prediction validation failed: {exc}") from exc
    _fail(verified_prediction_sha == prediction_sha, "adaptive E2E prediction hash changed during validation")
    _fail(
        float(prediction["predicted_ms"]) == float(predicted),
        "adaptive E2E prediction does not match its config value",
    )
    return {
        "config_path": str(path),
        "config_sha256": digest,
        "protocol_root": str(protocol_root),
        "pre_trajectory_e2e_ms": float(predicted),
        "prediction_artifact_path": str(prediction_path),
        "prediction_artifact_sha256": prediction_sha,
        "calibration_model_sha256": model_sha,
    }


def _validate_official_evaluator_result(
    *,
    path: Path,
    output_dir: Path,
    instance_id: str,
    run_id: str,
    dataset_path: Path,
    dataset_sha256: str,
    predictions_path: Path,
) -> dict[str, Any]:
    """Validate the evaluator's complete provenance before accepting a case.

    A zero exit code only establishes that an evaluator process returned.  This
    verifier binds the result to the exact one-instance dataset and the exact
    prediction file emitted by the preceding runner attempt, and prevents a
    stale or external report from being credited to this case.
    """
    _fail(path.is_file() and not path.is_symlink(), "official evaluator result must be a regular file")
    _fail(_inside(path.resolve(), output_dir), "official evaluator result must stay inside output-dir")
    official = _read_json(path, "official evaluator result")
    required = {
        "schema_version", "official_resolved", "submitted", "instance_id", "run_id",
        "report_path", "report_sha256", "dataset_path", "dataset_sha256",
        "predictions_path", "predictions_sha256", "evaluator_dataset_sha256",
        "evaluator_predictions_sha256", "command_sha256", "counts", "evaluator_python",
        "timeout_seconds",
    }
    _fail(required.issubset(official), "official evaluator result omits required provenance")
    _fail(official["schema_version"] == OFFICIAL_EVALUATOR_SCHEMA, "unsupported official evaluator result schema")
    _fail(official["instance_id"] == instance_id, "official evaluator instance_id does not match case")
    _fail(official["run_id"] == run_id, "official evaluator run_id does not match deterministic case run_id")
    _fail(official["submitted"] is True, "official evaluator did not submit the case")
    _fail(isinstance(official["official_resolved"], bool), "official evaluator official_resolved must be boolean")

    claimed_dataset = _strict_path(official["dataset_path"], "official evaluator dataset_path")
    _fail(claimed_dataset == dataset_path.resolve(), "official evaluator dataset_path does not match preflight dataset")
    _fail(_string(official["dataset_sha256"], "official evaluator dataset_sha256").lower() == dataset_sha256, "official evaluator dataset hash does not match preflight dataset")
    _fail(sha256_file(dataset_path) == dataset_sha256, "preflight dataset changed before evaluator verification")

    _fail(predictions_path.is_file() and not predictions_path.is_symlink(), "runner did not produce a regular preds.json")
    claimed_predictions = _strict_path(official["predictions_path"], "official evaluator predictions_path")
    _fail(claimed_predictions == predictions_path.resolve(), "official evaluator predictions_path does not match runner preds.json")
    predictions_sha256 = sha256_file(predictions_path)
    _fail(_string(official["predictions_sha256"], "official evaluator predictions_sha256").lower() == predictions_sha256, "official evaluator prediction hash does not match runner preds.json")

    report_path = _strict_path(official["report_path"], "official evaluator report_path")
    _fail(_inside(report_path, output_dir), "official evaluator report_path must stay inside output-dir")
    _fail(report_path.is_file() and not report_path.is_symlink(), "official evaluator report_path must be a regular file")
    report_sha256 = _string(official["report_sha256"], "official evaluator report_sha256").lower()
    _fail(bool(HEX64_RE.fullmatch(report_sha256)), "official evaluator report_sha256 must be SHA-256")
    _fail(sha256_file(report_path) == report_sha256, "official evaluator report hash does not match report_path")

    for field in ("evaluator_dataset_sha256", "evaluator_predictions_sha256", "command_sha256"):
        digest = _string(official[field], f"official evaluator {field}").lower()
        _fail(bool(HEX64_RE.fullmatch(digest)), f"official evaluator {field} must be SHA-256")
    _string(official["evaluator_python"], "official evaluator evaluator_python")
    _fail(isinstance(official["timeout_seconds"], int) and not isinstance(official["timeout_seconds"], bool) and official["timeout_seconds"] > 0, "official evaluator timeout_seconds must be positive")

    counts = official["counts"]
    count_keys = {
        "total_instances", "submitted_instances", "completed_instances",
        "resolved_instances", "unresolved_instances", "error_instances",
    }
    _fail(isinstance(counts, dict) and set(counts) == count_keys, "official evaluator counts are incomplete")
    for key in count_keys:
        _fail(isinstance(counts[key], int) and not isinstance(counts[key], bool) and counts[key] >= 0, f"official evaluator counts.{key} must be a non-negative integer")
    _fail(counts["total_instances"] == counts["submitted_instances"] == counts["completed_instances"] == 1, "official evaluator counts must describe exactly one submitted, completed instance")
    _fail(counts["error_instances"] == 0, "official evaluator counts contain errors")
    _fail(counts["resolved_instances"] + counts["unresolved_instances"] == 1, "official evaluator counts must contain exactly one resolved or unresolved instance")
    _fail(counts["resolved_instances"] == (1 if official["official_resolved"] else 0), "official evaluator resolved count disagrees with official_resolved")
    _fail(counts["unresolved_instances"] == (0 if official["official_resolved"] else 1), "official evaluator unresolved count disagrees with official_resolved")
    return official


def inventory_artifacts(root: Path, output_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not root.exists():
        return records
    for path in sorted(item for item in root.rglob("*") if item.is_file() and not item.is_symlink()):
        records.append({"kind": _artifact_kind(path), "path": str(path.relative_to(output_dir)), "sha256": sha256_file(path), "size": path.stat().st_size})
    return records


def _single_artifact(
    records: Sequence[Mapping[str, Any]], *, label: str, predicate: Any
) -> str:
    matches = [str(item["path"]) for item in records if predicate(Path(str(item["path"])))]
    _fail(len(matches) == 1, f"completed assignment case requires exactly one {label}; found {len(matches)}")
    return matches[0]


def _result(case: Mapping[str, Any], status: str, *, reason: str | None = None, **extra: Any) -> dict[str, Any]:
    value: dict[str, Any] = {"schema_version": RESULT_SCHEMA, "resume_key": case["resume_key"], "status": status}
    if case.get("schema_version") == CONFIRMATION_CASE_SCHEMA:
        value["confirmation"] = {
            "schema_version": CONFIRMATION_CASE_SCHEMA, "case_id": case["case_id"],
            "candidate_id": case["candidate_id"], "panel_case_id": case["panel_case_id"],
            "binding": case["confirmation_binding"],
        }
    if reason:
        value["reason"] = reason
    value.update(extra)
    return value


def _failure_inventory(output_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Inventory retained evidence without following links or hiding unreadable files."""
    records: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for directory, dirs, files in os.walk(output_dir, followlinks=False, onerror=lambda exc: errors.append({"path": str(exc.filename), "error": str(exc)})):
        parent = Path(directory)
        links = [name for name in dirs if (parent / name).is_symlink()]
        dirs[:] = [name for name in dirs if name not in links]
        for name in sorted(files + links):
            path = parent / name
            relative = str(path.relative_to(output_dir))
            if relative in {"case_result.json", "case_result.json.sha256"}:
                continue
            try:
                before = path.lstat()
                if path.is_symlink():
                    records.append({"path": relative, "kind": "symlink", "target": os.readlink(path), "mtime_ns": before.st_mtime_ns})
                    continue
                if not path.is_file():
                    records.append({"path": relative, "kind": "special", "mtime_ns": before.st_mtime_ns})
                    continue
                digest = sha256_file(path)
                after = path.stat()
                records.append({"path": relative, "kind": _artifact_kind(path), "sha256": digest, "size": after.st_size, "mtime_ns": after.st_mtime_ns})
                if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                    errors.append({"path": relative, "error": "artifact changed during inventory"})
            except (OSError, CaseRunnerError) as exc:
                errors.append({"path": relative, "error": str(exc)})
    return sorted(records, key=lambda item: item["path"]), errors


def _write_failure_result(output_dir: Path, case: Mapping[str, Any], exc: Exception) -> None:
    """Called only while holding the case lock; failure evidence cannot be accepted."""
    result_path = output_dir / "case_result.json"
    if result_path.exists() or result_path.is_symlink():
        return
    artifacts, inventory_errors = _failure_inventory(output_dir)
    failure = {
        "schema_version": FAILURE_RESULT_SCHEMA,
        "resume_key": case["resume_key"],
        "status": "failed",
        "reason": str(exc) or type(exc).__name__,
        "failure": {
            "type": type(exc).__name__,
            "message": str(exc),
            "recorded_epoch_ns": time.time_ns(),
            "halt_matrix": isinstance(exc, EvidenceIntegrityError),
            "classification": "infrastructure_evidence" if isinstance(exc, EvidenceIntegrityError) else "case_runner",
        },
        "accepted": False,
        "artifacts": artifacts,
        "inventory_errors": inventory_errors,
    }
    _atomic_json(result_path, failure)
    Path(str(result_path) + ".sha256").write_text(f"{sha256_file(result_path)}  {result_path.name}\n", encoding="utf-8")


def _bind_evaluator_project_command(command: list[str], project: Path, revision: str) -> list[str]:
    """Pass the already-validated checkout to the adapter; never duplicate overrides."""
    command = list(command)
    for flag, expected in (("--evaluator-project", str(project)), ("--evaluator-revision", revision)):
        _fail(not any(token.startswith(flag + "=") for token in command),
              f"evaluator source binding requires canonical {flag} argv")
        count = command.count(flag)
        _fail(count <= 1, f"duplicate evaluator source binding: {flag}")
        if count:
            index = command.index(flag)
            _fail(index + 1 < len(command) and command[index + 1] == expected,
                  f"evaluator source binding disagrees with runtime: {flag}")
        else:
            command.extend([flag, expected])
    return command


def _owned_docker_command(command: list[str], project: Path, owner: str, output_dir: Path,
                          *, placement: Mapping[str, Any] | None = None) -> list[str]:
    """Prove the pinned deployment defaults before adding a per-attempt label.

    Parsing is read-only. With a bound CPU policy only its exact cpuset may
    accompany the existing owner label; arbitrary arguments still fail closed.
    """
    _fail(bool(re.fullmatch(r"[0-9a-f]{32}", owner)), "invalid case ownership identity")
    python = project / ".venv" / "bin" / "python"
    _fail(python.is_file(), "owned Docker launch requires the pinned project Python environment")
    parser_marker = f"ASSIGNMENT_DOCKER_PROOF_V1:{uuid.uuid4().hex}:"
    parse_code = """import contextlib, io, json, os, sys
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
    deadline = deadline_from_env(required=True)
    assert deadline is not None
    remaining = remaining_seconds(deadline)
    _fail(remaining > 0, "case deadline expired before deployment verification")
    parsed = subprocess.run(
        [str(python), "-c", parse_code, *command[command.index("run-batch") + 1:]],
        cwd=str(project), capture_output=True, timeout=min(20.0, remaining), check=False,
        env={
            **_without_v2_activation(os.environ),
            "PYTHONDONTWRITEBYTECODE": "1",
            DOCKER_PROOF_MARKER_ENV: parser_marker,
        },
    )
    stdout_bytes = parsed.stdout if isinstance(parsed.stdout, bytes) else (parsed.stdout or "").encode("utf-8")
    stderr_bytes = parsed.stderr if isinstance(parsed.stderr, bytes) else (parsed.stderr or "").encode("utf-8")
    stdout_path = output_dir / "docker_ownership_parser.stdout.log"
    stderr_path = output_dir / "docker_ownership_parser.stderr.log"
    _atomic_bytes(stdout_path, stdout_bytes)
    _atomic_bytes(stderr_path, stderr_bytes)
    parser_capture = {
        "schema_version": DOCKER_PARSER_CAPTURE_SCHEMA,
        "protocol": DOCKER_PROOF_PROTOCOL,
        "marker": parser_marker,
        "returncode": parsed.returncode,
        "stdout": {
            "path": stdout_path.name,
            "size_bytes": len(stdout_bytes),
            "sha256": sha256_file(stdout_path),
        },
        "stderr": {
            "path": stderr_path.name,
            "size_bytes": len(stderr_bytes),
            "sha256": sha256_file(stderr_path),
        },
    }
    _atomic_json(output_dir / "docker_ownership_parser_capture.json", parser_capture)
    capture_note = (
        f"; raw parser output retained in {stdout_path.name} and {stderr_path.name}"
    )
    _fail(parsed.returncode == 0, "owned Docker deployment configuration could not be verified" + capture_note)
    try:
        stdout_text = stdout_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CaseRunnerError(
            "owned Docker deployment proof was not UTF-8" + capture_note
        ) from exc
    proof_payloads = [
        line[len(parser_marker):]
        for line in stdout_text.splitlines()
        if line.startswith(parser_marker)
    ]
    _fail(
        len(proof_payloads) == 1,
        "owned Docker deployment requires exactly one machine-readable proof" + capture_note,
    )
    try:
        proof = json.loads(proof_payloads[0])
    except json.JSONDecodeError as exc:
        raise CaseRunnerError(
            "owned Docker deployment proof was not valid JSON" + capture_note
        ) from exc
    _fail(isinstance(proof, dict), "owned Docker deployment proof must be a JSON object" + capture_note)
    existing_args = proof.pop("docker_args", None)
    docker_args: list[str] = []
    if placement is not None:
        bound = cpu_policy.from_environment()
        _fail(bound is not None and bound == placement, "owned Docker launch requires runtime-bound CPU policy")
        _fail(isinstance(existing_args, list), "owned Docker launch requires explicit Docker argument proof")
        docker_args = cpu_policy.worker_docker_args(placement, existing_args)
        # Pinned BasicCLI concatenates list-valued overrides. Keep a reviewed
        # cpuset already supplied by YAML/CLI and append only missing entries.
        docker_args = docker_args[len(existing_args):]
        _fail(proof.get("empty_args") == (not existing_args), "owned Docker argument proof disagrees")
    else:
        _fail(existing_args in (None, []), "owned Docker launch rejects unreviewed Docker args")
    expected_proof = {"docker": True, "empty_args": not existing_args, "remove_images": False}
    _fail(proof == expected_proof, "owned Docker launch requires Docker with reviewed docker_args and no shared-image removal")
    _atomic_json(output_dir / "docker_ownership.json", {
        "schema_version": "assignment-docker-ownership.v1", "owner": owner,
        "label": CASE_OWNER_LABEL, "deployment_verified": proof,
        "retention": "retain stopped owned containers", "recorded_epoch_ns": time.time_ns(),
        "cpu_policy": placement,
        "runtime_manifest_sha256": os.environ.get(cpu_policy.RUNTIME_SHA_ENV) if placement else None,
        "configured_docker_args": existing_args,
        "parser_capture": parser_capture,
    })
    return [*command, "--instances.deployment.docker_args", json.dumps([*docker_args, "--label", f"{CASE_OWNER_LABEL}={owner}"]), "--instances.deployment.remove_container", "false"]


@contextmanager
def _locked_case(output_dir: Path, case: Mapping[str, Any], *, execute: bool):
    with (output_dir / ".case-runner.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CaseRunnerError("another case-runner process holds the output lock") from exc
        # Snapshot mutable root metadata before a retry replaces any of it.
        if execute and not (output_dir / "case_result.json").exists():
            sources = [p for p in output_dir.iterdir() if p.is_file() and not p.is_symlink() and p.name != ".case-runner.lock"]
            if sources:
                history = output_dir / "metadata_history" / f"{time.time_ns()}-{uuid.uuid4().hex}"
                history.mkdir(parents=True, exist_ok=False)
                for source in sources:
                    shutil.copy2(source, history / source.name)
        try:
            yield
        except Exception as exc:
            if execute:
                try:
                    _write_failure_result(output_dir, case, exc)
                except Exception as record_exc:
                    # Preserve the original exception even if evidence storage fails.
                    print(f"FAILURE_RECORD_UNAVAILABLE: {type(record_exc).__name__}: {record_exc}", file=sys.stderr)
            raise


def _upstream_endpoint(api_base: str) -> tuple[str, int, str]:
    parsed = urlparse(api_base)
    _fail(parsed.scheme == "http", "request proxy requires an http model.api_base; TLS termination is outside the reviewed proxy")
    _fail(bool(parsed.hostname), "model.api_base must include an upstream host")
    try:
        port = parsed.port or 80
    except ValueError as exc:
        raise CaseRunnerError(f"model.api_base has an invalid port: {api_base}") from exc
    _fail(1 <= port <= 65535, "model.api_base port is outside the valid range")
    return str(parsed.hostname), port, parsed.path.rstrip("/")


def _proxy_api_base(path: str, port: int) -> str:
    return f"http://127.0.0.1:{port}{path}"


def _candidate_proxy_ports(run_id: str) -> list[int]:
    base = 20000 + (int(hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:8], 16) % 20000)
    return [20000 + ((base - 20000 + offset) % 20000) for offset in range(128)]


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _terminate_proxy(process: subprocess.Popen[str] | None) -> int | None:
    if process is None:
        return None
    if process.poll() is None:
        process.terminate()
    try:
        return process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.wait(timeout=10)


def _start_request_proxy(
    *,
    manifest: Mapping[str, Any],
    repo: Path,
    output_dir: Path,
    run_id: str,
    deadline_seconds: int,
    adaptive_config_path: Path | None = None,
    attempt_id: str = "attempt-001",
    case_id: str | None = None,
    instance_id: str | None = None,
    telemetry: Mapping[str, Any] | None = None,
    model_hardware: Mapping[str, Any] | None = None,
) -> tuple[subprocess.Popen[str], dict[str, Any]]:
    proxy_path = _strict_path(manifest["integrity"]["request_proxy_path"], "integrity.request_proxy_path")
    upstream_host, upstream_port, upstream_path = _upstream_endpoint(str(manifest["model"]["api_base"]))
    events_path = output_dir / "request_proxy.jsonl"
    stdout_path = output_dir / "request_proxy.stdout.log"
    stderr_path = output_dir / "request_proxy.stderr.log"
    telemetry_v2_dir = output_dir / "telemetry_v2"
    telemetry_config = _validate_telemetry_config(
        telemetry if telemetry is not None else _legacy_telemetry_config(),
        label="runner.telemetry",
    )
    v2_enabled = telemetry_config["mode"] == "v2"
    v2_source_available = (repo / "src/agentic_sim/telemetry/v2.py").is_file()

    def startup_fail(message: str) -> None:
        if v2_enabled:
            raise EvidenceIntegrityError(message)
        _fail(False, message)

    if v2_enabled:
        if not v2_source_available:
            startup_fail("v2 production mode requires the reviewed telemetry recorder source")
    command_hash_value: str | None = None
    process: subprocess.Popen[str] | None = None
    command: list[str] = []
    serving_metrics_config_path: Path | None = None
    serving_metrics_config_sha256: str | None = None
    serving_metrics_config_file_sha256: str | None = None
    for port in _candidate_proxy_ports(run_id):
        if not _port_is_free(port):
            continue
        command = [
            sys.executable, str(proxy_path),
            "--listen-host", "127.0.0.1", "--listen-port", str(port),
            "--upstream-host", upstream_host, "--upstream-port", str(upstream_port),
            "--events", str(events_path), "--timeout-seconds", str(float(deadline_seconds)),
        ]
        if v2_enabled:
            profile = telemetry_config["remote_hardware_profile"]
            cpu_config = dict(telemetry_config["cpu_work"])
            cpu_config["output_dir"] = str((telemetry_v2_dir / "linux_work").resolve())
            serving_metrics_config = telemetry_config["serving_metrics"]
            serving_metrics_config_path = telemetry_v2_dir / "serving_metrics_config.json"
            _atomic_json(serving_metrics_config_path, serving_metrics_config)
            serving_metrics_config_sha256 = _serving_metrics_config_sha256(serving_metrics_config)
            serving_metrics_config_file_sha256 = sha256_file(serving_metrics_config_path)
            command.extend([
                "--v2-output-dir", str(telemetry_v2_dir), "--run-id", run_id,
                "--attempt-id", attempt_id,
                "--v2-hardware-profile", str(Path(profile["path"]).expanduser().resolve()),
                "--v2-hardware-profile-sha256", str(profile["sha256"]),
                "--v2-model-hardware", _canonical(dict(model_hardware or {})),
                "--v2-require-request-payloads",
                "--v2-cpu-collector-config", _canonical(cpu_config),
                "--serving-metrics-config", str(serving_metrics_config_path.resolve()),
            ])
            if case_id is not None:
                command.extend(["--case-id", case_id])
            if instance_id is not None:
                command.extend(["--instance-id", instance_id])
        if adaptive_config_path is not None:
            command.extend(["--adaptive-runtime-config", str(adaptive_config_path)])
        command_hash_value = command_hash(command)
        # The case runner itself is a supervisor, not the reviewed agent.
        # Clear any inherited sitecustomize opt-in before launching the proxy;
        # the proxy receives its v2 settings through explicit CLI arguments.
        env = _without_v2_activation(os.environ)
        if v2_enabled:
            env.update(
                {
                    "ASSIGNMENT_TELEMETRY_V2_REQUIRED": "1",
                    "ASSIGNMENT_TELEMETRY_V2_REQUIRE_RAW_REQUEST_BODIES": "1",
                    "ASSIGNMENT_TELEMETRY_V2_HARDWARE_PROFILE_SHA256": str(
                        telemetry_config["remote_hardware_profile"]["sha256"]
                    ),
                    "ASSIGNMENT_TELEMETRY_V2_CPU_COLLECTOR_CONFIG": _canonical(cpu_config),
                }
            )
        source_root = repo / "src"
        if source_root.is_dir():
            env["PYTHONPATH"] = str(source_root) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        stdout_handle = stdout_path.open("w", encoding="utf-8")
        stderr_handle = stderr_path.open("w", encoding="utf-8")
        try:
            process = subprocess.Popen(
                command,
                cwd=str(repo),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=stderr_handle,
                text=True,
                shell=False,
                close_fds=True,
                env=env,
            )
            ready_line = ""
            expected = f"request proxy listening on 127.0.0.1:{port}; upstream={upstream_host}:{upstream_port}"
            deadline = time.monotonic() + min(15.0, max(1.0, float(deadline_seconds)))
            assert process.stdout is not None
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                readable, _, _ = select.select([process.stdout], [], [], min(0.25, max(0.0, deadline - time.monotonic())))
                if not readable:
                    continue
                line = process.stdout.readline()
                if not line:
                    break
                stdout_handle.write(line)
                stdout_handle.flush()
                ready_line = line.strip()
                if ready_line == expected:
                    break
            if ready_line != expected:
                startup_fail("request proxy did not report readiness on the reviewed endpoint")
            if process.poll() is not None:
                startup_fail("request proxy exited during startup")
            metadata = {
                "schema_version": "assignment-request-proxy-run.v1",
                "script_path": str(proxy_path),
                "script_sha256": sha256_file(proxy_path),
                "command": command,
                "command_sha256": command_hash_value,
                "upstream_api_base": str(manifest["model"]["api_base"]),
                "listen_api_base": _proxy_api_base(upstream_path, port),
                "events_path": str(events_path.relative_to(output_dir)),
                "stdout_path": str(stdout_path.relative_to(output_dir)),
                "stderr_path": str(stderr_path.relative_to(output_dir)),
                "readiness": "stdout_exact_match_without_model_request",
                "adaptive_runtime_enabled": adaptive_config_path is not None,
                "telemetry_v2_dir": str(telemetry_v2_dir.relative_to(output_dir)) if v2_enabled else None,
                "telemetry_v2_enabled": v2_enabled,
                "telemetry": telemetry_config,
                "remote_hardware_profile_sha256": (
                    telemetry_config["remote_hardware_profile"]["sha256"] if v2_enabled else None
                ),
                "request_payload_capture_required": bool(
                    telemetry_config["require_raw_request_payloads"]
                ),
                "cpu_collector_config": cpu_config if v2_enabled else None,
                "serving_metrics_config": serving_metrics_config if v2_enabled else None,
                "serving_metrics_config_path": (
                    str(serving_metrics_config_path.relative_to(output_dir))
                    if serving_metrics_config_path is not None
                    else None
                ),
                "serving_metrics_config_sha256": serving_metrics_config_sha256,
                "serving_metrics_config_file_sha256": serving_metrics_config_file_sha256,
            }
            _atomic_json(output_dir / "request_proxy_provenance.json", metadata)
            return process, metadata
        except BaseException as exc:
            _terminate_proxy(process)
            process = None
            if v2_enabled and isinstance(exc, (OSError, CaseRunnerError)) and not isinstance(exc, EvidenceIntegrityError):
                raise EvidenceIntegrityError(f"v2 request proxy startup failed: {exc}") from exc
            raise
        finally:
            stdout_handle.close()
            stderr_handle.close()
    startup_fail("no deterministic free loopback endpoint was available for the request proxy")
    raise AssertionError("unreachable")


def _validate_proxy_events(path: Path, *, adaptive_required: bool = False) -> dict[str, Any]:
    _fail(path.is_file() and not path.is_symlink(), f"request proxy events are missing: {path}")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise CaseRunnerError(f"cannot read request proxy events: {exc}") from exc
    # A listening proxy always creates this journal, so an untouched empty file
    # is proof of a measured zero-request run rather than a missing journal.
    if not lines:
        _fail(not adaptive_required, "request proxy events are empty")
        return {"event_count": 0, "sha256": sha256_file(path)}
    seen: set[str] = set()
    for index, line in enumerate(lines, 1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CaseRunnerError(f"request proxy event {index} is not JSON: {exc}") from exc
        _fail(isinstance(row, dict), f"request proxy event {index} is not an object")
        _fail(row.get("schema_version") == "observability.request-proxy.v1", f"request proxy event {index} has an unsupported schema")
        _fail(row.get("event_type") == "model_request_boundary", f"request proxy event {index} has an invalid event type")
        request_id = row.get("request_id")
        _fail(isinstance(request_id, str) and request_id and request_id not in seen, f"request proxy event {index} has a duplicate or missing request_id")
        seen.add(request_id)
        _fail(row.get("provenance") == "measured" and row.get("request_mutation") is False, f"request proxy event {index} lacks measured immutable provenance")
        status_code = row.get("status_code")
        error = row.get("error")
        numeric_status = isinstance(status_code, int) and not isinstance(status_code, bool)
        successful = numeric_status and 200 <= status_code < 300 and error is None
        # An upstream rejection (HTTP 4xx/5xx, e.g. a context-overflow reject)
        # and a transport failure such as RemoteDisconnected are measured
        # observations of this run, not evidence that the journal is
        # untrustworthy.  Accept them so a finished agent attempt still
        # reaches the official evaluator, while keeping every integrity and
        # provenance check below.
        measured_failure = (
            numeric_status and 400 <= status_code < 600 and (error is None or isinstance(error, str))
        ) or (
            status_code is None and isinstance(error, str) and bool(error.strip())
        )
        _fail(successful or measured_failure, f"request proxy event {index} is not a measured request outcome")
        _fail(isinstance(row.get("start_mono_ns"), int) and not isinstance(row["start_mono_ns"], bool) and isinstance(row.get("end_mono_ns"), int) and not isinstance(row["end_mono_ns"], bool) and row["end_mono_ns"] > row["start_mono_ns"], f"request proxy event {index} has invalid monotonic bounds")
        _fail(isinstance(row.get("duration_ms"), (int, float)) and not isinstance(row["duration_ms"], bool) and row["duration_ms"] > 0, f"request proxy event {index} has invalid duration")
        _fail(
            math.isclose(
                float(row["duration_ms"]),
                (row["end_mono_ns"] - row["start_mono_ns"]) / 1_000_000.0,
                rel_tol=1e-6,
                abs_tol=1e-6,
            ),
            f"request proxy event {index} duration disagrees with monotonic bounds",
        )
        # A request that never received response headers may omit
        # ``response_sha256``; the request digest is always required.
        digest_keys = ["request_sha256"]
        if successful or "response_sha256" in row:
            digest_keys.append("response_sha256")
        for key in digest_keys:
            _fail(isinstance(row.get(key), str) and bool(HEX64_RE.fullmatch(row[key])), f"request proxy event {index} has invalid {key}")
        if adaptive_required:
            for key in ("adaptive_prediction_record_sha256", "adaptive_label_record_sha256"):
                _fail(isinstance(row.get(key), str) and bool(HEX64_RE.fullmatch(row[key])), f"request proxy event {index} has invalid {key}")
            _fail(row.get("prediction_durable_before_upstream") is True, f"request proxy event {index} lacks pre-dispatch prediction proof")
    return {"event_count": len(lines), "sha256": sha256_file(path)}


def _classify_model_transport_failure(path: Path) -> dict[str, Any] | None:
    """Classify an attempt whose model service never produced a usable reply.

    Complete request/response bytes prove that the recorder worked; they do
    not turn a vanished endpoint into a solver outcome.  A v2 case is halted
    when every physical model attempt is an observed transport failure or
    server-side 5xx.  Context, validation, and other 4xx responses remain
    measured model outcomes and are left for the normal evaluator path.
    """

    try:
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CaseRunnerError(f"cannot classify request proxy transport failures: {exc}") from exc
    if not rows:
        return None
    infrastructure: list[dict[str, Any]] = []
    successful = 0
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        status_code = row.get("status_code")
        error = row.get("error")
        failure_phase = row.get("failure_phase")
        is_success = (
            isinstance(status_code, int)
            and not isinstance(status_code, bool)
            and 200 <= status_code < 300
            and error is None
        )
        if is_success:
            successful += 1
            continue
        transport = (
            status_code is None
            and isinstance(error, str)
            and bool(error.strip())
        )
        server_error = (
            isinstance(status_code, int)
            and not isinstance(status_code, bool)
            and 500 <= status_code < 600
        )
        if transport or server_error:
            infrastructure.append(
                {
                    "request_id": row.get("request_id"),
                    "status_code": status_code,
                    "error": error,
                    "failure_phase": failure_phase,
                }
            )
    if successful or len(infrastructure) != len(rows):
        return None
    return {
        "classification": "model_transport_or_server_infrastructure",
        "status": "infrastructure_failure",
        "halt_matrix": True,
        "physical_attempt_count": len(rows),
        "failed_attempts": infrastructure,
        "reason": "all physical model attempts failed before a successful model response",
    }


def _v2_rows(directory: Path) -> list[dict[str, Any]]:
    """Read the four v2 streams after both writer processes have stopped."""

    stream_names = (
        "lifecycle_events.jsonl",
        "tool_events.jsonl",
        "model_events.jsonl",
        "hardware_snapshots.jsonl",
    )
    rows: list[dict[str, Any]] = []
    for name in stream_names:
        path = directory / name
        _fail(path.is_file() and not path.is_symlink(), f"v2 journal is missing: {path}")
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise CaseRunnerError(f"cannot read v2 journal {path}: {exc}") from exc
        for number, line in enumerate(lines, 1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CaseRunnerError(f"v2 journal {path}:{number} is not JSON: {exc}") from exc
            _fail(isinstance(value, dict), f"v2 journal {path}:{number} is not an object")
            _fail(str(value.get("schema_version", "")).startswith("assignment.telemetry.v2"), f"v2 journal {path}:{number} has an unsupported schema")
            rows.append(value)
    return rows


def _verify_payload_artifact(root: Path, descriptor: Mapping[str, Any], *, label: str) -> Path:
    _fail(isinstance(descriptor, Mapping), f"{label} descriptor is missing")
    artifact_path = descriptor.get("artifact_path")
    _fail(isinstance(artifact_path, str) and artifact_path and not Path(artifact_path).is_absolute(), f"{label} path is invalid")
    path = (root / artifact_path).resolve()
    _fail(_inside(path, root), f"{label} escapes the telemetry directory")
    _fail(path.is_file() and not path.is_symlink(), f"{label} artifact is missing: {path}")
    _fail(isinstance(descriptor.get("sha256"), str) and HEX64_RE.fullmatch(descriptor["sha256"].lower()), f"{label} hash is invalid")
    _fail(sha256_file(path) == descriptor["sha256"].lower(), f"{label} hash does not match bytes")
    size = descriptor.get("bytes")
    _fail(isinstance(size, int) and not isinstance(size, bool) and size == path.stat().st_size, f"{label} byte count does not match bytes")
    return path


def _response_usage(path: Path) -> dict[str, int | None] | None:
    """Read explicit usage from a retained JSON or final SSE response frame.

    Cache details remain null when the server omitted them.  The response
    bytes and their hash are still retained independently, so this projection
    cannot turn an unavailable cache field into a measured zero.
    """

    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
        usage: Mapping[str, Any] | None = None
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].lstrip()
            if not data or data == "[DONE]":
                continue
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            candidate = event.get("usage") if isinstance(event, Mapping) else None
            if isinstance(candidate, Mapping):
                usage = candidate
    else:
        usage = payload.get("usage") if isinstance(payload, Mapping) else None
    if not isinstance(usage, Mapping):
        return None

    def integer(value: Any) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    details = usage.get("prompt_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, Mapping) else None
    return {
        "prompt_tokens": integer(usage.get("prompt_tokens")),
        "completion_tokens": integer(usage.get("completion_tokens")),
        "cached_tokens": integer(cached),
    }


def _validate_native_token_work(sidecar: Mapping[str, Any], usage: Mapping[str, Any] | None,
                                *, label: str) -> dict[str, Any]:
    _fail(isinstance(usage, Mapping), f"{label} lacks retained API token usage")
    usage = dict(usage)
    finished = sidecar.get("native_measurement", {}).get("finished", {})
    if usage.get("cached_tokens") is None:
        cached = finished.get("cached_tokens")
        provenance = finished.get("cached_tokens_provenance")
        _fail(type(cached) is int and cached >= 0 and provenance in {
                  "engine_core_output.num_cached_tokens@pinned_process_outputs_finish_caller",
                  "finished_request_stats.num_cached_tokens", "finished_request_stats.cached_tokens"},
              f"{label} cache-token work is unavailable in both API and native evidence")
        usage.update(cached_tokens=cached, api_cached_tokens=None,
                     cached_tokens_provenance=provenance)
    for field in ("prompt_tokens", "completion_tokens", "cached_tokens"):
        value = usage.get(field)
        _fail(isinstance(value, int) and not isinstance(value, bool) and value >= 0,
              f"{label} lacks measured {field}")
    _fail(usage["cached_tokens"] <= usage["prompt_tokens"], f"{label} cached_tokens exceeds prompt_tokens")
    finished = sidecar.get("native_measurement", {}).get("finished", {})
    for native, api in (("num_prompt_tokens", "prompt_tokens"), ("num_generation_tokens", "completion_tokens")):
        value = finished.get(native)
        _fail(isinstance(value, int) and not isinstance(value, bool) and value == usage[api],
              f"{label} native {native} differs from retained API usage")
    if finished.get("cached_tokens") is not None:
        value = finished["cached_tokens"]
        _fail(isinstance(value, int) and not isinstance(value, bool) and value == usage["cached_tokens"],
              f"{label} native cached_tokens differs from retained API usage")
    return dict(usage)


def _copy_native_archive(source: Path, destination: Path) -> None:
    """Copy a local native archive into the attempt with a durability fence."""

    _fail(source.is_file() and not source.is_symlink(), f"native server archive is unavailable: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _fail(not destination.exists() and not destination.is_symlink(), f"native archive output already exists: {destination}")
    with source.open("rb") as source_stream, destination.open("xb") as target:
        for block in iter(lambda: source_stream.read(1024 * 1024), b""):
            target.write(block)
        target.flush()
        os.fsync(target.fileno())
    if hasattr(os, "O_DIRECTORY"):
        directory_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def _acquire_native_server_evidence(
    *,
    output_dir: Path,
    telemetry_dir: Path,
    telemetry_config: Mapping[str, Any],
    expected_identity: Mapping[str, str | None],
) -> dict[str, Any]:
    """Fetch one server archive after the run and prove every model join.

    This is deliberately post-completion: the proxy's native-deferred mode
    has already emitted zero metrics/witness reads.  The archive and derived
    rows are new immutable evidence; the original unavailable serving records
    are never rewritten.
    """

    descriptor = telemetry_config.get("native_server_archive")
    _fail(isinstance(descriptor, Mapping), "native_deferred requires native_server_archive")
    archive_dir = output_dir / "native_serving"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / "server_batch.tar"
    if "fetch" in descriptor:
        from types import SimpleNamespace
        from scripts.validation import check_live_native_serving as live

        fetch = dict(descriptor["fetch"])
        live.fetch(
            SimpleNamespace(
                remote_python=fetch.get("remote_python", "python3"),
                ssh_host=fetch["ssh_host"],
                ssh_control=fetch["ssh_control"],
                journal=fetch["journal"],
                native_journal=fetch["native_journal"],
                output=str(archive_path),
            )
        )
    else:
        source = Path(str(descriptor["path"])).expanduser().resolve()
        _fail(sha256_file(source) == str(descriptor["sha256"]).lower(), "native server archive hash does not match descriptor")
        _copy_native_archive(source, archive_path)

    from scripts.validation import check_live_native_serving as live

    archive_sha256 = sha256_file(archive_path)
    extraction_root = archive_dir / "archive_root"
    manifest = live.unpack_archive(archive_path, extraction_root)
    _fail(manifest.get("complete_jsonl_prefixes") is True, "native server archive contains incomplete journal prefixes")
    journal = live.resolve_artifact(manifest, extraction_root, manifest["journal"])
    native_journal = live.resolve_artifact(manifest, extraction_root, manifest["native_journal"])
    derive = live.load_derive()
    data = derive.read_observer_journal(journal)
    serving = telemetry_config.get("serving_metrics", {})
    for field in ("server_identity", "counter_epoch"):
        expected = serving.get(field)
        _fail(isinstance(expected, str) and bool(expected), f"native archive expected {field} is absent")
        _fail(data.header.get(field) == expected, f"native archive {field} differs from configured serving runtime")
    rows = _v2_rows(telemetry_dir)
    terminals = [
        row for row in rows
        if row.get("terminal") is True
        and row.get("phase") == "model_request"
        and row.get("event_kind") == "model_request"
    ]
    sidecars: list[dict[str, Any]] = []
    seen_physical: set[str] = set()
    for index, terminal in enumerate(terminals, 1):
        for field, expected in expected_identity.items():
            _fail(terminal.get(field) == expected, f"native model terminal {index} {field} differs from expected identity")
        physical = terminal.get("physical_request_id")
        _fail(isinstance(physical, str) and bool(physical), f"native model terminal {index} lacks physical request ID")
        _fail(physical not in seen_physical, f"native model terminal {index} duplicates physical request ID")
        seen_physical.add(physical)
        reference = terminal.get("serving_metrics_record")
        _fail(isinstance(reference, Mapping), f"native model terminal {index} lacks original serving record reference")
        record_value = reference.get("path")
        _fail(isinstance(record_value, str) and record_value and not Path(record_value).is_absolute(), f"native model terminal {index} serving record path is invalid")
        record_path = (telemetry_dir / record_value).resolve()
        _fail(_inside(record_path, telemetry_dir) and record_path.is_file() and not record_path.is_symlink(), f"native model terminal {index} serving record is unavailable")
        _fail(sha256_file(record_path) == reference.get("sha256"), f"native model terminal {index} serving record hash differs")
        record = _read_json(record_path, f"native serving record {index}")
        _fail(record.get("request_id") == physical, f"native model terminal {index} serving record ID differs")
        for field in ("server_identity", "counter_epoch"):
            _fail(record.get(field) == serving[field], f"native model terminal {index} serving record {field} differs")
        _fail(record.get("attribution_mode") == "native_deferred", f"native model terminal {index} is not native_deferred")
        _fail(record.get("status") == "unavailable", f"native model terminal {index} rewrote deferred serving status")
        _fail(record.get("capture", {}).get("scrape_count") == 0, f"native model terminal {index} claims a proxy scrape")
        _fail(record.get("witness") is None and record.get("witness_source") is None, f"native model terminal {index} claims a witness read")
        for phase in ("before", "after"):
            snapshot = record.get("snapshots", {}).get(phase, {})
            _fail(snapshot.get("raw_path") is None, f"native model terminal {index} deferred {phase} snapshot has raw scrape bytes")
        sidecar = derive.derive_server_attribution(
            journal=data.path,
            native_journal=native_journal,
            request_id=physical,
        )
        sidecar = dict(sidecar)
        sidecar["physical_request_id"] = physical
        measured = sidecar.get("status") == "measured"
        successful = (
            terminal.get("status") == "success"
            and terminal.get("transport_status_code") == 200
            and terminal.get("transport_response_complete") is True
        )
        if measured:
            target = sidecar.get("target_request", {})
            _fail(
                target.get("engine_request_id") == "chatcmpl-" + physical,
                f"native model terminal {index} engine ID is not bound to physical request",
            )
            _fail(
                target.get("observation_id") in data.terminals,
                f"native model terminal {index} sidecar target is absent from ASGI archive",
            )
            target_record = data.terminals[target["observation_id"]]
            for field in ("case_id", "attempt_id"):
                _fail(target_record.get(field) == expected_identity.get(field), f"native model terminal {index} ASGI {field} differs from expected identity")
            _fail(target_record.get("request_body_sha256") == terminal.get("request_body_sha256"), f"native model terminal {index} request bytes do not join")
            _fail(target_record.get("response_body_sha256") == terminal.get("response_body_sha256"), f"native model terminal {index} response bytes do not join")
            payload = terminal.get("request_payload_artifact", {})
            response_path = _verify_payload_artifact(
                telemetry_dir, payload.get("response"), label=f"native model terminal {index} response",
            )
            _fail(sha256_file(response_path) == terminal.get("response_body_sha256"),
                  f"native model terminal {index} retained response differs from joined body")
            sidecar["verified_api_token_work"] = _validate_native_token_work(
                sidecar, _response_usage(response_path), label=f"native model terminal {index}",
            )
        elif successful:
            raise EvidenceIntegrityError(
                f"native model terminal {index} completed successfully but native attribution is unavailable: "
                + str(sidecar.get("unavailable_reason"))
            )
        sidecars.append(sidecar)

    sidecar_path = archive_dir / "native_attribution.jsonl"
    with sidecar_path.open("xb") as stream:
        for row in sidecars:
            stream.write((_canonical(row) + "\n").encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())
    summary = {
        "schema_version": "assignment.native-serving-evidence.v1",
        "status": "measured" if all(row.get("status") == "measured" for row in sidecars) else "partial_unavailable",
        "archive_path": str(archive_path.relative_to(output_dir)),
        "archive_sha256": archive_sha256,
        "manifest_sha256": sha256_file(extraction_root / "manifest.json"),
        "journal_path": str(journal),
        "native_journal_path": str(native_journal),
        "journal_sha256": sha256_file(journal),
        "native_journal_sha256": sha256_file(native_journal),
        "sidecar_path": str(sidecar_path.relative_to(output_dir)),
        "sidecar_sha256": sha256_file(sidecar_path),
        "physical_request_count": len(terminals),
        "measured_count": sum(row.get("status") == "measured" for row in sidecars),
        "unavailable_count": sum(row.get("status") != "measured" for row in sidecars),
        "server_identity": data.header.get("server_identity"),
        "counter_epoch": data.header.get("counter_epoch"),
        "observer_instance_id": data.header.get("observer_instance_id"),
        "expected_identity": dict(expected_identity),
    }
    _atomic_json(archive_dir / "native_evidence_manifest.json", summary)
    return summary


def _audit_bpf_work_evidence(
    *,
    work_dir: Path,
    summary: Mapping[str, Any],
    expected_identity: Mapping[str, str | None],
    expected_tool_rows: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Validate the durable BCC evidence behind a v2 case.

    Action rows are compact boundary records.  Individual packets live in one
    durable binary stream and a range may contain packets for other overlapping
    actions, so counts are always checked against ``iter_bpf_events(...,
    token=...)`` rather than against the byte range or the aggregate map.
    Deferred actions intentionally produce two rows with one event identity:
    the original boundary and one collector-stop finalization.  The latter is
    the canonical count row and may carry explicit bounded censor witnesses.
    """

    def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
        _fail(
            isinstance(value, int) and not isinstance(value, bool) and value >= minimum,
            f"{label} must be an integer >= {minimum}",
        )
        return int(value)

    def _artifact(value: Any, label: str) -> Path:
        _fail(isinstance(value, str) and bool(value), f"{label} path is missing")
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = work_dir / candidate
        _fail(not candidate.is_symlink(), f"{label} must not be a symlink")
        resolved = candidate.resolve()
        _fail(_inside(resolved, work_dir), f"{label} escapes the attempt directory")
        _fail(resolved.is_file() and not resolved.is_symlink(), f"{label} is unavailable: {resolved}")
        return resolved

    def _validate_event(
        event: Mapping[str, Any],
        *,
        token: int,
        label: str,
        sequences: set[int],
        expected_schema: str,
    ) -> None:
        _fail(event.get("schema_version") == expected_schema, f"{label} has an unsupported event schema")
        if expected_schema == BPF_EVENT_SCHEMA:
            _fail(event.get("event_abi") == BPF_EVENT_ABI, f"{label} has an unsupported scalar-argument ABI")
            scalar_args = event.get("raw_scalar_args")
            _fail(
                isinstance(scalar_args, list)
                and len(scalar_args) == 6
                and all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in scalar_args),
                f"{label} does not retain six raw scalar syscall arguments",
            )
        elif expected_schema == BPF_EVENT_SCHEMA_LEGACY:
            _fail(event.get("event_abi") is None, f"{label} relabels a historical v2 event as a current ABI")
        _fail(_integer(event.get("token"), f"{label}.token", minimum=1) == token, f"{label} is bound to another action token")
        sequence = _integer(event.get("sequence"), f"{label}.sequence")
        _fail(sequence not in sequences, f"{label} reuses an individual event sequence")
        sequences.add(sequence)
        start = _integer(event.get("kernel_start_ns"), f"{label}.kernel_start_ns")
        kind = _integer(event.get("kind"), f"{label}.kind", minimum=1)
        _fail(kind <= 16, f"{label} has an unknown event kind")
        status = event.get("status")
        _fail(isinstance(status, int) and not isinstance(status, bool) and status in {1, 2, 3}, f"{label} has an unknown event status")
        for field in ("syscall_nr", "tgid", "tid", "parent_tgid", "child_pid"):
            _integer(event.get(field), f"{label}.{field}", minimum=0)
        # A syscall without a descriptor uses a negative sentinel, and
        # path-oriented calls may carry Linux's AT_FDCWD (-100).  Preserve the
        # native signed 32-bit field rather than treating every negative value
        # below -1 as corruption.
        fd = event.get("fd")
        _fail(
            isinstance(fd, int)
            and not isinstance(fd, bool)
            and -(1 << 31) <= fd <= (1 << 31) - 1,
            f"{label}.fd must be a signed 32-bit integer",
        )
        for prefix in ("path", "path2"):
            path_status = _integer(event.get(prefix + "_status"), f"{label}.{prefix}_status")
            _fail(path_status <= 3, f"{label}.{prefix}_status is invalid")
            path_length = _integer(event.get(prefix + "_len"), f"{label}.{prefix}_len")
            _fail(path_length <= BPF_PATH_CAP, f"{label}.{prefix}_len exceeds the path bound")
        end = event.get("kernel_end_ns")
        duration = event.get("duration_ns")
        ret = event.get("ret")
        if status == 3:
            _fail(end is None and duration is None and ret is None, f"{label} censor witness invents a completion")
            censor = _integer(event.get("censor_boundary_ns"), f"{label}.censor_boundary_ns")
            _fail(censor >= start, f"{label} censor boundary precedes kernel start")
            status_name = event.get("status_name")
            if status_name is not None:
                _fail(status_name == "censored_process_exit", f"{label} has an invalid censor status name")
        else:
            end_value = _integer(end, f"{label}.kernel_end_ns")
            _fail(end_value >= start, f"{label} has a reversed kernel interval")
            _fail(_integer(duration, f"{label}.duration_ns") == end_value - start, f"{label} has an inconsistent kernel duration")
            _fail(isinstance(ret, int) and not isinstance(ret, bool), f"{label}.ret must be an integer")
            _fail(event.get("censor_boundary_ns") is None, f"{label} has an unexpected censor boundary")

    def _validate_pending(value: Any, *, token: int, label: str) -> list[Mapping[str, Any]]:
        _fail(isinstance(value, list), f"{label} must be a list")
        pending: list[Mapping[str, Any]] = []
        for ordinal, witness in enumerate(value, 1):
            witness_label = f"{label}[{ordinal}]"
            _fail(isinstance(witness, Mapping), f"{witness_label} is malformed")
            _fail(_integer(witness.get("token"), f"{witness_label}.token", minimum=1) == token, f"{witness_label} is bound to another action token")
            status = witness.get("status")
            _fail(status in {3, "censored", "censored_process_exit", "censored_unresolved_in_flight"}, f"{witness_label} is not an explicit censor witness")
            start = _integer(witness.get("kernel_start_ns"), f"{witness_label}.kernel_start_ns")
            censor = _integer(witness.get("censor_boundary_ns"), f"{witness_label}.censor_boundary_ns")
            _fail(censor >= start, f"{witness_label} censor boundary precedes kernel start")
            if "kernel_end_ns" in witness:
                _fail(witness.get("kernel_end_ns") is None, f"{witness_label} invents a kernel end")
            _fail("duration_ns" in witness and witness.get("duration_ns") is None, f"{witness_label} must have null duration")
            return_key = "ret" if "ret" in witness else "return_value" if "return_value" in witness else None
            _fail(return_key is not None and witness.get(return_key) is None, f"{witness_label} must have null return value")
            pending.append(witness)
        return pending

    def _validate_censor_boundary(row: Mapping[str, Any], pending: Sequence[Mapping[str, Any]], *, label: str, required: bool) -> None:
        value = row.get("censor_boundary")
        if value is None:
            _fail(not required, f"{label} lacks the collector-stop censor boundary")
            return
        _fail(isinstance(value, Mapping), f"{label}.censor_boundary is malformed")
        boundary_ns = _integer(value.get("censor_boundary_ns"), f"{label}.censor_boundary.censor_boundary_ns")
        _fail(value.get("clock_id") == "CLOCK_MONOTONIC", f"{label}.censor_boundary clock is not kernel monotonic")
        _fail(_integer(value.get("pending_count"), f"{label}.censor_boundary.pending_count") == len(pending), f"{label}.censor_boundary pending count differs from witnesses")
        in_flight = _integer(value.get("in_flight_at_stop"), f"{label}.censor_boundary.in_flight_at_stop")
        _fail(in_flight >= len(pending), f"{label}.censor_boundary has more witnesses than in-flight calls")
        _fail(_integer(value.get("pending_witness_gap"), f"{label}.censor_boundary.pending_witness_gap") == 0, f"{label}.censor_boundary has an unresolved pending witness gap")
        for ordinal, witness in enumerate(pending, 1):
            _fail(_integer(witness.get("censor_boundary_ns"), f"{label}.censored_pending[{ordinal}].censor_boundary_ns") <= boundary_ns, f"{label} witness lies after its censor boundary")

    def _validate_path_records(value: Any, *, label: str) -> int:
        _fail(isinstance(value, list), f"{label} lacks individual path records")
        for ordinal, operation in enumerate(value, 1):
            operation_label = f"{label}[{ordinal}]"
            _fail(isinstance(operation, Mapping), f"{operation_label} is malformed")
            for field in ("sequence", "kernel_timestamp_ns", "tgid", "tid", "kind", "status", "length"):
                _integer(operation.get(field), f"{operation_label}.{field}")
        return len(value)

    _fail(summary.get("schema_version") == BPF_WORK_SUMMARY_SCHEMA, "Linux work collector summary has an unsupported schema")
    identity = summary.get("identity")
    _fail(isinstance(identity, Mapping), "Linux work collector summary lacks process identity binding")
    for key, value in expected_identity.items():
        if value is not None:
            _fail(identity.get(key) == value, f"Linux work collector identity {key} is not bound to this attempt")
    binding = summary.get("identity_binding_digest")
    _fail(isinstance(binding, str) and bool(HEX64_RE.fullmatch(binding.lower())), "Linux work collector identity binding digest is missing or malformed")
    identity_fields = (
        "pid", "start_ticks", "boot_id", "pid_namespace_inode", "run_id", "attempt_id",
        "case_id", "instance_id", "container_pid", "pid_namespace", "mapping_source",
    )
    _fail(
        all(field in identity for field in identity_fields)
        and hashlib.sha256(_canonical({field: identity.get(field) for field in identity_fields}).encode("utf-8")).hexdigest() == binding.lower(),
        "Linux work collector identity binding digest does not match its process identity",
    )
    program_sha256 = summary.get("program_sha256")
    _fail(isinstance(program_sha256, str) and HEX64_RE.fullmatch(program_sha256.lower()), "Linux work collector program hash is missing or malformed")

    raw_value = summary.get("raw_aggregate_journal")
    _fail(isinstance(raw_value, str) and bool(raw_value), "Linux work collector raw aggregate journal is missing")
    raw_path = _artifact(raw_value, "Linux work collector raw aggregate journal")
    try:
        lines = raw_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise CaseRunnerError(f"cannot read Linux work raw aggregate journal: {exc}") from exc

    raw_stream = summary.get("raw_event_stream")
    binary_mode = isinstance(raw_stream, Mapping)
    binary_path: Path | None = None
    binary_sha256: str | None = None
    binary_record_size: int | None = None
    binary_event_schema: str | None = None
    binary_size = 0
    if binary_mode:
        binary_path = _artifact(raw_stream.get("path"), "Linux work binary event stream")
        binary_event_schema = raw_stream.get("schema_version")
        _fail(binary_event_schema in {BPF_EVENT_SCHEMA, BPF_EVENT_SCHEMA_LEGACY}, "Linux work binary event stream has an unsupported schema")
        binary_record_size = _integer(raw_stream.get("record_size_bytes"), "Linux work binary event stream.record_size_bytes", minimum=1)
        if binary_event_schema == BPF_EVENT_SCHEMA:
            if "event_abi" in raw_stream:
                _fail(raw_stream.get("event_abi") == BPF_EVENT_ABI, "Linux work binary event stream scalar-argument ABI is unsupported")
        else:
            if "event_abi" in raw_stream:
                _fail(raw_stream.get("event_abi") is None, "Linux work binary event stream relabels a historical v2 ABI")
        binary_size = binary_path.stat().st_size
        binary_sha256 = raw_stream.get("sha256")
        if binary_sha256 is None:
            binary_sha256 = summary.get("raw_event_stream_sha256")
        _fail(isinstance(binary_sha256, str) and HEX64_RE.fullmatch(binary_sha256.lower()), "Linux work summary lacks a valid binary event-stream SHA-256")
        _fail(sha256_file(binary_path) == binary_sha256.lower(), "Linux work binary event stream SHA-256 does not match the summary")
        if "bytes_written" in raw_stream:
            _fail(_integer(raw_stream.get("bytes_written"), "Linux work binary event stream.bytes_written") == binary_size, "Linux work binary event stream byte count differs from the file")
        if "records_written" in raw_stream:
            _fail(_integer(raw_stream.get("records_written"), "Linux work binary event stream.records_written") * binary_record_size == binary_size, "Linux work binary event stream record count differs from the file")
        _fail(binary_size % binary_record_size == 0, "Linux work binary event stream is not a whole-record file")
    else:
        # Historical v2 fixtures predate the durable packet stream.  They are
        # accepted only when every action carries explicit inline records; a
        # compact row with events=[] cannot use this compatibility path.
        binary_path = None

    raw_rows: list[dict[str, Any]] = []
    boundary_rows: list[dict[str, Any]] = []
    finalization_rows: list[dict[str, Any]] = []
    decoded_counts: dict[int, int] = {}
    path_descriptor_counts: dict[int, int] = {}
    decoded_cache: dict[tuple[int, int, int], list[dict[str, Any]]] = {}
    for index, line in enumerate(lines, 1):
        label = f"Linux work raw aggregate journal line {index}"
        _fail(bool(line.strip()), f"{label} is blank")
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CaseRunnerError(f"{label} is not JSON: {exc}") from exc
        _fail(isinstance(row, dict), f"{label} is not an object")
        _fail(row.get("schema_version") == BPF_WORK_RAW_SCHEMA, f"{label} has an unsupported schema")
        _fail(row.get("program_sha256") == program_sha256, f"{label} program hash differs from summary")
        raw_identity = row.get("identity")
        _fail(isinstance(raw_identity, Mapping), f"{label} lacks identity")
        _fail(_canonical(dict(raw_identity)) == _canonical(dict(identity)), f"{label} identity differs from summary")
        _fail(row.get("identity_binding_digest") == binding, f"{label} identity binding differs")
        for key, value in expected_identity.items():
            if value is not None:
                _fail(raw_identity.get(key) == value, f"{label} {key} is not bound")
        boundary = row.get("boundary")
        _fail(isinstance(boundary, Mapping), f"{label} lacks action boundary")
        event_id = boundary.get("event_id")
        _fail(isinstance(event_id, str) and bool(event_id), f"{label} lacks action event identity")
        _fail(boundary.get("phase") == "complete", f"{label} boundary is not terminal")
        command = boundary.get("command")
        _fail(isinstance(command, str) and "\x00" not in command, f"{label} boundary command is missing or invalid")
        command_digest = hashlib.sha256(command.encode("utf-8")).hexdigest()
        _fail(boundary.get("command_sha256") == command_digest, f"{label} boundary command hash does not match command")
        _fail(row.get("command_sha256") == command_digest, f"{label} command binding does not match command")
        start_mono = _integer(boundary.get("start_mono_ns"), f"{label}.boundary.start_mono_ns")
        end_mono = _integer(boundary.get("end_mono_ns"), f"{label}.boundary.end_mono_ns")
        _fail(end_mono >= start_mono, f"{label} boundary monotonic interval is reversed")
        _fail(boundary.get("identity") == raw_identity, f"{label} boundary identity differs from raw identity")
        aggregate = row.get("raw_aggregate")
        _fail(isinstance(aggregate, Mapping), f"{label} lacks aggregate counters")
        _fail(row.get("aggregate_missing") is False, f"{label} is missing its kernel aggregate")
        action_token = _integer(row.get("action_token"), f"{label}.action_token", minimum=1)
        in_flight = _integer(row.get("in_flight_at_flush_timeout"), f"{label}.in_flight_at_flush_timeout")
        for loss_field in ("lost_event_records", "lost_path_records", "lost_pending_records", "lineage_map_failures"):
            loss = _integer(aggregate.get(loss_field), f"{label}.raw_aggregate.{loss_field}")
            _fail(loss == 0, f"Linux work collector reported {loss_field} and cannot prove complete raw operation capture")
        _fail(_integer(row.get("perf_lost_events"), f"{label}.perf_lost_events") == 0, f"{label} reports perf-buffer loss")
        callback_errors = row.get("event_callback_errors")
        _fail(isinstance(callback_errors, list) and not callback_errors, f"{label} reports perf callback errors")
        pending = _validate_pending(row.get("censored_pending", []), token=action_token, label=f"{label}.censored_pending")
        record_type = row.get("record_type", "action_boundary")
        _fail(record_type in {"action_boundary", "action_finalization"}, f"{label} has an unsupported record type")
        is_final = record_type == "action_finalization"

        event_schema = row.get("event_schema_version")
        _fail(event_schema in ({BPF_EVENT_SCHEMA, BPF_EVENT_SCHEMA_LEGACY} if binary_mode else BPF_WORK_EVENT_SCHEMAS), f"{label} has an unsupported individual-event schema")
        if binary_mode:
            _fail(event_schema == binary_event_schema, f"{label} individual-event schema differs from the binary stream ABI")
        if event_schema == BPF_EVENT_SCHEMA:
            if "event_abi" in row:
                _fail(row.get("event_abi") == BPF_EVENT_ABI, f"{label} scalar-argument ABI is unsupported")
        elif event_schema == BPF_EVENT_SCHEMA_LEGACY:
            if "event_abi" in row:
                _fail(row.get("event_abi") is None, f"{label} relabels a historical v2 ABI")
        events = row.get("events")
        _fail(isinstance(events, list), f"{label} lacks individual kernel event records")
        event_count = _integer(row.get("event_count"), f"{label}.event_count")
        required_event_count = _integer(row.get("required_event_count"), f"{label}.required_event_count")
        _fail(required_event_count <= event_count, f"{label} requires more events than it retained")
        _fail(isinstance(row.get("event_records_complete"), bool), f"{label}.event_records_complete is invalid")
        path_count = _validate_path_records(row.get("path_records"), label=f"{label}.path_records")

        if binary_mode:
            _fail(row.get("event_storage") == "binary", f"{label} does not declare binary individual-event storage")
            _fail(events == [], f"{label} duplicates binary packets inline")
            stream = row.get("binary_event_stream")
            _fail(isinstance(stream, Mapping), f"{label} lacks a binary event range")
            stream_path = _artifact(stream.get("path"), f"{label}.binary_event_stream")
            _fail(stream_path == binary_path, f"{label} binary event range uses a different stream")
            _fail(stream.get("schema_version") == binary_event_schema, f"{label} binary event range schema is unsupported")
            record_size = _integer(stream.get("record_size_bytes"), f"{label}.binary_event_stream.record_size_bytes", minimum=1)
            _fail(record_size == binary_record_size, f"{label} binary event range record size differs from summary")
            if binary_event_schema == BPF_EVENT_SCHEMA:
                if "event_abi" in stream:
                    _fail(stream.get("event_abi") == BPF_EVENT_ABI, f"{label} binary event range scalar-argument ABI is unsupported")
            else:
                if "event_abi" in stream:
                    _fail(stream.get("event_abi") is None, f"{label} binary event range relabels a historical v2 ABI")
            offset_start = _integer(stream.get("offset_start"), f"{label}.binary_event_stream.offset_start")
            offset_end = _integer(stream.get("offset_end"), f"{label}.binary_event_stream.offset_end")
            _fail(offset_start <= offset_end <= binary_size, f"{label} binary event range is outside the stream")
            _fail(offset_start % record_size == 0 and offset_end % record_size == 0, f"{label} binary event range is unaligned")
            _fail(_integer(stream.get("byte_length"), f"{label}.binary_event_stream.byte_length") == offset_end - offset_start, f"{label} binary event range byte length is inconsistent")
            _fail(stream.get("durable_at_boundary") is True, f"{label} binary event range was not durable at the boundary")
            stream_count = _integer(stream.get("record_count"), f"{label}.binary_event_stream.record_count")
            _fail(stream_count == event_count, f"{label} binary event count differs from its range descriptor")
            cache_key = (offset_start, offset_end, action_token)
            if cache_key not in decoded_cache:
                try:
                    decoded_cache[cache_key] = list(
                        iter_bpf_events(
                            binary_path,
                            offset_start=offset_start,
                            offset_end=offset_end,
                            token=action_token,
                            schema_version=binary_event_schema,
                            record_size_bytes=binary_record_size,
                        )
                    )
                except Exception as exc:
                    raise CaseRunnerError(f"{label} binary event range could not be decoded: {exc}") from exc
            decoded = decoded_cache[cache_key]
            _fail(len(decoded) == event_count, f"{label} binary event range decoded {len(decoded)} records but declares {event_count}")
            sequences: set[int] = set()
            for ordinal, event in enumerate(decoded, 1):
                _fail(isinstance(event, Mapping), f"{label} decoded event {ordinal} is malformed")
                _validate_event(
                    event,
                    token=action_token,
                    label=f"{label} decoded event {ordinal}",
                    sequences=sequences,
                    expected_schema=str(binary_event_schema),
                )
            decoded_event_count = len(decoded)
        else:
            _fail(row.get("event_storage") in {None, "inline"}, f"{label} has no supported legacy event storage")
            _fail(event_count == len(events), f"{label} has an invalid individual-event count")
            decoded_event_count = len(events)
            sequences = set()
            for ordinal, event in enumerate(events, 1):
                _fail(isinstance(event, Mapping), f"{label} event {ordinal} is malformed")
                _fail(event.get("schema_version") in BPF_WORK_EVENT_SCHEMAS, f"{label} event {ordinal} has an unsupported schema")
                if event.get("schema_version") == BPF_EVENT_SCHEMA:
                    if "event_abi" in event:
                        _fail(event.get("event_abi") == BPF_EVENT_ABI, f"{label} event {ordinal} has an unsupported scalar-argument ABI")
                    raw_scalar_args = event.get("raw_scalar_args")
                    _fail(
                        isinstance(raw_scalar_args, list)
                        and len(raw_scalar_args) == 6
                        and all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in raw_scalar_args),
                        f"{label} event {ordinal} lacks six raw scalar syscall arguments",
                    )
                elif event.get("schema_version") == BPF_EVENT_SCHEMA_LEGACY:
                    if "event_abi" in event:
                        _fail(event.get("event_abi") is None, f"{label} event {ordinal} relabels a historical v2 event")
                _fail(_integer(event.get("token"), f"{label} event {ordinal}.token", minimum=1) == action_token, f"{label} event {ordinal} is bound to another action token")
                sequence = _integer(event.get("sequence"), f"{label} event {ordinal}.sequence")
                _fail(sequence not in sequences, f"{label} reuses an individual event sequence")
                sequences.add(sequence)
            _fail(required_event_count == decoded_event_count, f"{label} does not prove all required individual events were decoded")
            _fail(row.get("event_records_complete") is True, f"{label} does not prove complete individual-event capture")

        _fail(required_event_count <= decoded_event_count, f"{label} required event count exceeds decoded records")
        if not is_final and row.get("deferred_quiescence") is not True and in_flight == 0:
            _fail(row.get("event_records_complete") is True, f"{label} does not prove complete individual-event capture")
        if is_final:
            _fail(binary_mode and row.get("event_storage") == "binary", f"{label} finalization is not backed by binary packets")
            _fail(row.get("event_records_complete") is True, f"{label} finalization does not prove complete individual-event capture")
            _fail(required_event_count == decoded_event_count, f"{label} finalization required count differs from decoded records")
        _validate_censor_boundary(
            row,
            pending,
            label=label,
            # A boundary with an in-flight shell read is finalized later.  Its
            # collector-stop censor boundary belongs to that finalization row;
            # requiring it here would reject the faithful two-row contract.
            required=is_final or bool(pending),
        )
        censored_event_count = 0
        if binary_mode:
            cache_key = (
                _integer(row["binary_event_stream"]["offset_start"], f"{label}.binary_event_stream.offset_start"),
                _integer(row["binary_event_stream"]["offset_end"], f"{label}.binary_event_stream.offset_end"),
                action_token,
            )
            censored_event_count = sum(1 for event in decoded_cache[cache_key] if event.get("status") == 3)
        censored_counter = _integer(aggregate.get("censored_pending_records", 0), f"{label}.raw_aggregate.censored_pending_records")
        if is_final:
            _fail(censored_counter <= len(pending) + censored_event_count, f"{label} censor counter exceeds retained censor witnesses")
        raw_rows.append(row)
        decoded_counts[id(row)] = decoded_event_count
        path_descriptor_counts[id(row)] = path_count
        (finalization_rows if is_final else boundary_rows).append(row)

    _fail(bool(raw_rows), "Linux work raw aggregate journal has no action rows")
    boundary_by_id: dict[str, dict[str, Any]] = {}
    final_by_id: dict[str, dict[str, Any]] = {}
    for row in boundary_rows:
        event_id = str(row["boundary"]["event_id"])
        _fail(event_id not in boundary_by_id, f"Linux work raw aggregate journal reuses an action boundary identity: {event_id}")
        boundary_by_id[event_id] = row
    for row in finalization_rows:
        event_id = str(row["boundary"]["event_id"])
        _fail(event_id not in final_by_id, f"Linux work raw aggregate journal has duplicate action finalization: {event_id}")
        _fail(event_id in boundary_by_id, f"Linux work action finalization has no original boundary: {event_id}")
        final_by_id[event_id] = row
        initial = boundary_by_id[event_id]
        _fail(row["action_token"] == initial["action_token"], f"Linux work action finalization token differs: {event_id}")
        _fail(row["command_sha256"] == initial["command_sha256"], f"Linux work action finalization command differs: {event_id}")
        initial_boundary = initial["boundary"]
        final_boundary = row["boundary"]
        for field in ("event_id", "command", "command_sha256", "start_wall_ns", "start_mono_ns", "identity"):
            _fail(final_boundary.get(field) == initial_boundary.get(field), f"Linux work action finalization changes boundary field {field}: {event_id}")
        _fail(initial.get("deferred_quiescence") is True or initial.get("in_flight_at_flush_timeout", 0) > 0, f"Linux work action finalization has no deferred original boundary: {event_id}")
        initial_stream = initial.get("binary_event_stream")
        final_stream = row.get("binary_event_stream")
        if binary_mode:
            _fail(isinstance(initial_stream, Mapping) and isinstance(final_stream, Mapping), f"Linux work action finalization lacks binary ranges: {event_id}")
            _fail(final_stream.get("path") == initial_stream.get("path"), f"Linux work action finalization changes binary stream: {event_id}")
            _fail(final_stream.get("offset_start") == initial_stream.get("offset_start"), f"Linux work action finalization changes binary range start: {event_id}")
            _fail(_integer(final_stream.get("offset_end"), f"Linux work action finalization.offset_end") >= _integer(initial_stream.get("offset_end"), f"Linux work action boundary.offset_end"), f"Linux work action finalization moves binary range backwards: {event_id}")
            _fail(_integer(row.get("event_count"), f"Linux work action finalization.event_count") >= _integer(initial.get("event_count"), f"Linux work action boundary.event_count"), f"Linux work action finalization loses packets: {event_id}")
        _fail(row.get("deferred_quiescence") is True, f"Linux work action finalization is not marked deferred: {event_id}")
        _fail(row.get("in_flight_at_flush_timeout") == initial.get("in_flight_at_flush_timeout"), f"Linux work action finalization changes boundary in-flight count: {event_id}")
        _fail(isinstance(row.get("censor_boundary"), Mapping), f"Linux work action finalization lacks its censor boundary: {event_id}")
    if final_by_id:
        _fail(set(final_by_id) <= set(boundary_by_id), "Linux work finalization coverage is malformed")
    for event_id, initial in boundary_by_id.items():
        if initial.get("deferred_quiescence") is True or initial.get("in_flight_at_flush_timeout", 0) > 0:
            _fail(event_id in final_by_id, f"Linux work deferred action has no collector-stop finalization: {event_id}")

    actions = summary.get("actions")
    _fail(isinstance(actions, list), "Linux work collector summary lacks action rows")
    action_ids = [action.get("event_id") for action in actions if isinstance(action, Mapping)]
    _fail(len(action_ids) == len(actions) and all(isinstance(value, str) and value for value in action_ids), "Linux work collector summary has malformed action identities")
    _fail(len(action_ids) == len(set(action_ids)) and set(action_ids) == set(boundary_by_id), "Linux work summary and raw action boundaries do not have one-to-one coverage")
    for action in actions:
        raw = action.get("raw") if isinstance(action, Mapping) else None
        event_id = action.get("event_id") if isinstance(action, Mapping) else None
        _fail(isinstance(raw, Mapping), "Linux work summary action lacks its retained raw operation record")
        _fail(raw.get("boundary", {}).get("event_id") == event_id, "Linux work summary action raw identity differs")
        _fail(_canonical(raw) == _canonical(boundary_by_id[str(event_id)]), "Linux work summary action does not equal its retained raw boundary record")
    summary_finalizations = summary.get("action_finalizations", [])
    _fail(isinstance(summary_finalizations, list), "Linux work collector summary finalizations are malformed")
    final_ids = [row.get("boundary", {}).get("event_id") if isinstance(row, Mapping) else None for row in summary_finalizations]
    _fail(len(final_ids) == len(set(final_ids)) and set(final_ids) == set(final_by_id), "Linux work summary and raw finalizations do not have one-to-one coverage")
    for final in summary_finalizations:
        event_id = final.get("boundary", {}).get("event_id") if isinstance(final, Mapping) else None
        _fail(isinstance(final, Mapping) and isinstance(event_id, str), "Linux work summary finalization is malformed")
        _fail(_canonical(final) == _canonical(final_by_id[event_id]), "Linux work summary finalization differs from its raw journal record")

    expected_by_event: dict[str, Mapping[str, Any]] = {}
    for tool in expected_tool_rows:
        event_id = tool.get("event_id")
        _fail(isinstance(event_id, str) and bool(event_id), "executed tool journal contains a row without an event identity")
        _fail(event_id not in expected_by_event, f"executed tool journal reuses action identity: {event_id}")
        command = tool["actual_action"] if "actual_action" in tool else tool.get("action")
        command_digest = tool.get("actual_action_sha256") or tool.get("action_sha256")
        _fail(isinstance(command, str) and "\x00" not in command, f"tool boundary {event_id} has no executable command text")
        _fail(bool(command) or tool.get("event_kind") == "runtime_command_start",
              f"tool boundary {event_id} has an empty non-lifecycle command")
        _fail(isinstance(command_digest, str) and HEX64_RE.fullmatch(command_digest.lower()), f"tool boundary {event_id} has no executable command hash")
        _fail(command_digest.lower() == hashlib.sha256(command.encode("utf-8")).hexdigest(), f"tool boundary {event_id} command hash does not match command")
        expected_by_event[event_id] = tool
    _fail(set(expected_by_event) == set(boundary_by_id), "Linux work raw action coverage differs from the independent executed tool journal")
    for event_id, raw in boundary_by_id.items():
        tool = expected_by_event[event_id]
        expected_command = tool["actual_action"] if "actual_action" in tool else tool.get("action")
        _fail(raw["boundary"]["command"] == expected_command, f"Linux work action {event_id} command differs from the actual guarded tool command")
        _fail(raw["boundary"]["command_sha256"] == str(tool.get("actual_action_sha256") or tool.get("action_sha256")).lower(), f"Linux work action {event_id} command hash differs from the actual guarded tool command")

    total_events = sum(decoded_counts[id(row)] for row in boundary_rows)
    final_event_count = sum(decoded_counts[id(row)] for row in finalization_rows)
    return {
        "schema_version": summary["schema_version"],
        "raw_aggregate_journal": str(raw_path.relative_to(work_dir)),
        "raw_action_count": len(boundary_rows),
        "action_finalization_count": len(finalization_rows),
        "individual_operation_count": total_events,
        "finalized_individual_operation_count": final_event_count,
        "path_descriptor_count": sum(path_descriptor_counts[id(row)] for row in raw_rows),
        "loss_counters_zero": True,
        "binary_event_stream_sha256": binary_sha256,
        "identity_binding_digest": binding,
    }


def _audit_bpf_service_artifacts(
    *,
    work_dir: Path,
    summary: Mapping[str, Any],
) -> dict[str, Any]:
    """Verify the closed privileged service left readable, hashed artifacts.

    The BCC process may run as root while the case auditor runs as the
    workload user.  Readability is therefore tested with the auditor's real
    effective credentials, including execute permission on every parent
    directory.  The check deliberately does not require a particular mode:
    the launcher may grant access through the caller's group.
    """

    def artifact(value: Any, label: str) -> Path:
        _fail(
            isinstance(value, (str, Path)) and bool(str(value)),
            f"{label} path is missing",
        )
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = work_dir / candidate
        _fail(not candidate.is_symlink(), f"{label} must not be a symlink")
        resolved = candidate.resolve()
        _fail(_inside(resolved, work_dir), f"{label} escapes the attempt directory")
        _fail(resolved.is_file() and not resolved.is_symlink(), f"{label} is unavailable: {resolved}")
        current = resolved.parent
        parents: list[Path] = []
        while True:
            parents.append(current)
            if current == current.parent:
                break
            current = current.parent
        for parent in reversed(parents):
            _fail(os.access(parent, os.X_OK), f"{label} parent is not traversable by the runtime auditor: {parent}")
        _fail(os.access(resolved, os.R_OK), f"{label} is not readable by the runtime auditor: {resolved}")
        try:
            with resolved.open("rb") as handle:
                handle.read(1)
            metadata = resolved.stat()
        except OSError as exc:
            raise CaseRunnerError(f"{label} cannot be read by the runtime auditor: {resolved}: {exc}") from exc
        return resolved

    def json_artifact(path: Path, label: str) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CaseRunnerError(f"{label} is not readable JSON: {path}: {exc}") from exc
        _fail(isinstance(value, dict), f"{label} is not a JSON object")
        return value

    def same_artifact_path(value: Any, expected: Path, label: str) -> None:
        actual = artifact(value, label)
        _fail(actual == expected, f"{label} does not bind to the audited artifact")

    manifest_path = artifact(work_dir / "bpf_collector_manifest.json", "BPF collector manifest")
    manifest = json_artifact(manifest_path, "BPF collector manifest")
    _fail(manifest.get("schema_version") == "assignment.linux-bpf-work-collector.v1", "BPF collector manifest has an unsupported schema")
    _fail(manifest.get("status") == "closed", "BPF collector manifest is not closed")
    _fail(manifest.get("identity") == summary.get("identity"), "BPF collector manifest identity is not bound to the summary")
    _fail(manifest.get("program_sha256") == summary.get("program_sha256"), "BPF collector manifest program hash is not bound to the summary")
    aggregate_path = artifact(summary.get("raw_aggregate_journal"), "BPF raw aggregate journal")
    same_artifact_path(manifest.get("raw_aggregate_journal"), aggregate_path, "BPF collector manifest raw aggregate journal")
    summary_stream = summary.get("raw_event_stream")
    _fail(isinstance(summary_stream, Mapping), "BPF summary lacks its binary event stream descriptor")
    binary_path = artifact(summary_stream.get("path"), "BPF binary event stream")
    manifest_stream = manifest.get("raw_event_stream")
    _fail(isinstance(manifest_stream, Mapping), "BPF collector manifest lacks its binary event stream descriptor")
    same_artifact_path(manifest_stream.get("path"), binary_path, "BPF collector manifest binary event stream")
    for field in ("schema_version", "record_size_bytes"):
        _fail(manifest_stream.get(field) == summary_stream.get(field), f"BPF collector manifest binary stream {field} is not bound")
    if summary_stream.get("schema_version") == BPF_EVENT_SCHEMA:
        _fail(manifest_stream.get("event_abi") == summary_stream.get("event_abi") == BPF_EVENT_ABI, "BPF collector manifest binary stream scalar-argument ABI is not bound")
    stream_hash = summary_stream.get("sha256") or summary.get("raw_event_stream_sha256")
    _fail(isinstance(stream_hash, str) and HEX64_RE.fullmatch(stream_hash.lower()), "BPF summary binary event stream hash is malformed")
    _fail(manifest.get("raw_event_stream_sha256") == stream_hash.lower(), "BPF collector manifest binary event stream hash is not bound")

    lifecycle_path = artifact(work_dir / "service_lifecycle.json", "BPF service lifecycle")
    lifecycle = json_artifact(lifecycle_path, "BPF service lifecycle")
    _fail(lifecycle.get("schema_version") == "assignment.linux-bpf-work-service-lifecycle.v1", "BPF service lifecycle has an unsupported schema")
    _fail(lifecycle.get("status") == "stopped", "BPF service lifecycle is not stopped cleanly")
    _fail(lifecycle.get("service_returncode") == 0, "BPF service lifecycle reports a non-zero service exit")
    _fail(lifecycle.get("error") is None, "BPF service lifecycle reports a service error")
    _fail(lifecycle.get("identity") == summary.get("identity"), "BPF service lifecycle identity is not bound to the summary")
    target = lifecycle.get("target")
    identity = summary.get("identity")
    _fail(isinstance(target, Mapping) and isinstance(identity, Mapping), "BPF service lifecycle target identity is malformed")
    for field in ("pid", "run_id", "attempt_id", "case_id", "instance_id", "container_pid", "pid_namespace", "mapping_source"):
        _fail(target.get(field) == identity.get(field), f"BPF service lifecycle target {field} is not bound")
    trace_dir = lifecycle.get("trace_dir")
    _fail(isinstance(trace_dir, str) and Path(trace_dir).expanduser().resolve() == work_dir.resolve(), "BPF service lifecycle trace directory is not bound")
    lifecycle_summary = lifecycle.get("summary")
    _fail(isinstance(lifecycle_summary, Mapping), "BPF service lifecycle lacks its stop summary")
    _fail(lifecycle_summary.get("schema_version") == summary.get("schema_version"), "BPF service lifecycle summary schema is not bound")
    _fail(lifecycle_summary.get("action_count") == len(summary.get("actions", [])), "BPF service lifecycle action count is not bound")
    _fail(lifecycle_summary.get("finalization_count") == len(summary.get("action_finalizations", [])), "BPF service lifecycle finalization count is not bound")
    same_artifact_path(lifecycle_summary.get("raw_aggregate_journal"), aggregate_path, "BPF service lifecycle raw aggregate journal")
    lifecycle_stream = lifecycle_summary.get("raw_event_stream")
    _fail(isinstance(lifecycle_stream, Mapping), "BPF service lifecycle lacks its binary event stream summary")
    same_artifact_path(lifecycle_stream.get("path"), binary_path, "BPF service lifecycle binary event stream")
    _fail(lifecycle_stream.get("sha256") == stream_hash.lower(), "BPF service lifecycle binary event stream hash is not bound")

    native = summary.get("native_sink")
    _fail(isinstance(native, Mapping), "BPF summary lacks native source/library provenance")
    native_artifacts: dict[str, dict[str, Any]] = {}
    for field in ("source", "library"):
        path_key = f"{field}_path"
        hash_key = f"{field}_sha256"
        native_path = artifact(native.get(path_key), f"native BPF {field}")
        digest = native.get(hash_key)
        _fail(isinstance(digest, str) and HEX64_RE.fullmatch(digest.lower()), f"native BPF {field} hash is malformed")
        actual_digest = sha256_file(native_path)
        _fail(actual_digest == digest.lower(), f"native BPF {field} hash does not match the retained artifact")
        native_artifacts[field] = {
            "path": str(native_path.relative_to(work_dir)),
            "sha256": actual_digest,
            "mode": native_path.stat().st_mode & 0o777,
        }

    required_artifacts = {
        "collector_manifest": manifest_path,
        "raw_aggregate_journal": aggregate_path,
        "binary_event_stream": binary_path,
        "work_summary": artifact(work_dir / "work_summary.json", "BPF work summary"),
        "service_lifecycle": lifecycle_path,
    }
    return {
        "runtime_readable": True,
        "artifacts": {
            name: {
                "path": str(path.relative_to(work_dir)),
                "mode": path.stat().st_mode & 0o777,
                "uid": path.stat().st_uid,
                "gid": path.stat().st_gid,
            }
            for name, path in required_artifacts.items()
        },
        "native_sink": native_artifacts,
        "program_sha256": summary.get("program_sha256"),
        "binary_event_stream_sha256": stream_hash.lower(),
    }


def _audit_v2_snapshots(
    *,
    rows: Sequence[Mapping[str, Any]],
    telemetry_manifest: Mapping[str, Any],
    hardware_profile_sha256: str,
) -> dict[str, Any]:
    """Audit per-row process samples and manifest-bound hardware snapshots."""

    manifest_clock = telemetry_manifest.get("clock")
    _fail(isinstance(manifest_clock, Mapping), "v2 telemetry manifest lacks clock identity")
    clock_identity = (
        manifest_clock.get("hostname"),
        manifest_clock.get("boot_id"),
        manifest_clock.get("clock_id"),
    )
    _fail(all(isinstance(value, str) and value for value in clock_identity), "v2 telemetry manifest clock identity is incomplete")
    resource_identity_by_writer: dict[str, tuple[int, int, str, str]] = {}
    hardware_rows: list[Mapping[str, Any]] = []
    hardware_writers: set[str] = set()
    writers: set[str] = set()
    measured_samples = 0
    unavailable_samples = 0
    for ordinal, row in enumerate(rows, 1):
        label = f"v2 row {ordinal}"
        writer = row.get("writer_role")
        _fail(isinstance(writer, str) and bool(writer.strip()), f"{label} has no writer role")
        writers.add(writer)
        _fail(row.get("hardware_profile_sha256") == hardware_profile_sha256, f"{label} hardware profile hash is not bound")
        row_clock = row.get("clock")
        _fail(isinstance(row_clock, Mapping), f"{label} has no clock metadata")
        _fail(
            (row_clock.get("hostname"), row_clock.get("boot_id"), row_clock.get("clock_id")) == clock_identity,
            f"{label} belongs to a different host/boot/clock domain",
        )
        snapshot = row.get("process_resources_at_record")
        _fail(isinstance(snapshot, Mapping), f"{label} lacks a process resource snapshot")
        _fail(snapshot.get("schema_version") == PROCESS_RESOURCE_SCHEMA, f"{label} has an unsupported process resource snapshot schema")
        snapshot_clock = snapshot.get("clock")
        _fail(isinstance(snapshot_clock, Mapping), f"{label} process resource snapshot lacks clock metadata")
        _fail(
            (snapshot_clock.get("hostname"), snapshot_clock.get("boot_id"), snapshot_clock.get("clock_id")) == clock_identity,
            f"{label} process resource snapshot clock is not bound",
        )
        pid = snapshot.get("pid")
        start_ticks = snapshot.get("process_start_ticks")
        _fail(isinstance(pid, int) and not isinstance(pid, bool) and pid > 0, f"{label} process resource PID is invalid")
        _fail(isinstance(start_ticks, int) and not isinstance(start_ticks, bool) and start_ticks > 0, f"{label} process resource start identity is invalid")
        identity = (pid, start_ticks, str(snapshot_clock.get("boot_id")), str(snapshot_clock.get("clock_id")))
        previous = resource_identity_by_writer.get(writer)
        _fail(previous is None or previous == identity, f"{label} changes the process identity for writer {writer}")
        resource_identity_by_writer[writer] = identity
        sample_start = snapshot.get("sample_start_mono_ns")
        sample_end = snapshot.get("sample_end_mono_ns")
        _fail(
            isinstance(sample_start, int)
            and not isinstance(sample_start, bool)
            and isinstance(sample_end, int)
            and not isinstance(sample_end, bool)
            and 0 <= sample_start <= sample_end,
            f"{label} process resource sample bracket is invalid",
        )
        counters = snapshot.get("counters")
        _fail(isinstance(counters, Mapping) and set(counters) == set(PROCESS_RESOURCE_FIELDS), f"{label} process resource counters are incomplete")
        availability = snapshot.get("availability")
        _fail(availability in {"measured", "unavailable"}, f"{label} process resource availability is invalid")
        if availability == "measured":
            measured_samples += 1
            _fail(
                all(
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and math.isfinite(value)
                    and value >= 0
                    for value in counters.values()
                ),
                f"{label} measured process resource counter is invalid",
            )
        else:
            unavailable_samples += 1
            _fail(all(value is None for value in counters.values()), f"{label} unavailable process resource snapshot contains a value")
            _fail(isinstance(snapshot.get("reason"), str) and bool(snapshot["reason"]), f"{label} unavailable process resource snapshot lacks a reason")

        if row.get("event_kind") != "hardware_snapshot":
            continue
        hardware_writers.add(writer)
        hardware_rows.append(row)
        _fail(row.get("phase") == "startup", f"{label} hardware snapshot has an invalid phase")
        _fail(row.get("terminal") is True and row.get("status") == "success", f"{label} hardware snapshot is not a successful terminal record")
        snapshot_id = row.get("snapshot_id")
        timestamp = row.get("timestamp_mono_ns")
        _fail(isinstance(snapshot_id, str) and bool(snapshot_id), f"{label} hardware snapshot identity is missing")
        _fail(isinstance(timestamp, int) and not isinstance(timestamp, bool) and timestamp >= 0, f"{label} hardware snapshot timestamp is invalid")
        _fail(row.get("start_mono_ns") == timestamp and row.get("end_mono_ns") == timestamp, f"{label} hardware snapshot timestamp is not its zero-width boundary")
        raw_hardware = row.get("raw_hardware")
        model_hardware = row.get("model_hardware")
        _fail(isinstance(raw_hardware, Mapping), f"{label} hardware snapshot lacks raw inventory")
        _fail(isinstance(model_hardware, Mapping), f"{label} hardware snapshot lacks model projection")
        raw_profile_hash = raw_hardware.get("remote_profile_sha256")
        if raw_profile_hash is not None:
            _fail(raw_profile_hash == hardware_profile_sha256, f"{label} raw hardware profile hash is not bound")
        try:
            rebuilt = model_hardware_features(model_hardware)
        except Exception as exc:
            raise CaseRunnerError(f"{label} hardware model projection is invalid: {exc}") from exc
        _fail(_canonical(rebuilt) == _canonical(dict(model_hardware)), f"{label} hardware model projection is not canonical")

    _fail(bool(hardware_rows), "v2 telemetry has no hardware snapshot rows")
    _fail(hardware_writers == writers, "v2 telemetry hardware snapshots do not cover every writer")
    return {
        "row_count": len(rows),
        "resource_snapshot_count": len(rows),
        "measured_resource_snapshot_count": measured_samples,
        "unavailable_resource_snapshot_count": unavailable_samples,
        "hardware_snapshot_count": len(hardware_rows),
        "hardware_snapshot_writers": sorted(hardware_writers),
        "resource_process_identities": {
            writer: {
                "pid": identity[0],
                "process_start_ticks": identity[1],
                "boot_id": identity[2],
                "clock_id": identity[3],
            }
            for writer, identity in sorted(resource_identity_by_writer.items())
        },
        "hardware_profile_sha256": hardware_profile_sha256,
    }


def _expected_cpu_action_rows(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Join BPF actions to independent tool AND physical lifecycle commands.

    Projection is only for the existing exact-coverage checker. The original
    lifecycle rows, command bytes, parent spans and event identities are kept.
    PID discovery precedes collector availability and has its own explicitly
    suspended event kind; it cannot masquerade as a captured runtime command.
    """
    expected = [
        row for row in rows
        if row.get("schema_version") == "assignment.telemetry.v2.tool"
        and row.get("event_kind") == "tool_event_start"
        and row.get("terminal") is False
        and row.get("intent_only") is not True
    ]
    commands: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        if row.get("schema_version") != "assignment.telemetry.v2.lifecycle":
            continue
        if row.get("event_kind") not in {"runtime_command_start", "runtime_command"}:
            continue
        span_id = row.get("span_id")
        _fail(isinstance(span_id, str) and bool(span_id), "runtime command lacks span identity")
        commands.setdefault(span_id, []).append(row)
    for span_id, pair in commands.items():
        starts = [row for row in pair if row.get("event_kind") == "runtime_command_start" and row.get("terminal") is False]
        ends = [row for row in pair if row.get("event_kind") == "runtime_command" and row.get("terminal") is True]
        _fail(len(pair) == 2 and len(starts) == len(ends) == 1,
              f"runtime command {span_id} lacks a unique start/terminal pair")
        start, end = starts[0], ends[0]
        for row in pair:
            _fail(row.get("cpu_action_required") is True,
                  f"runtime command {span_id} cannot disable required CPU capture")
            command = row.get("runtime_command")
            _fail(isinstance(command, str) and "\x00" not in command,
                  f"runtime command {span_id} has no command text")
            _fail(row.get("runtime_command_sha256") == hashlib.sha256(command.encode("utf-8")).hexdigest(),
                  f"runtime command {span_id} command hash differs from its bytes")
        for field in ("runtime_command", "runtime_command_sha256", "run_id", "case_id", "attempt_id",
                      "instance_id", "clock", "parent_event_id", "start_mono_ns"):
            _fail(start.get(field) == end.get(field),
                  f"runtime command {span_id} start/terminal {field} mismatch")
        expected.append({
            **start, "actual_action": start["runtime_command"],
            "actual_action_sha256": start["runtime_command_sha256"],
        })
    return expected


def _audit_v2_evidence_impl(
    *,
    telemetry_dir: Path,
    telemetry_config: Mapping[str, Any],
    expected_identity: Mapping[str, str | None],
) -> dict[str, Any]:
    """Run the merged-disk v2 audit and enforce essential capture artifacts."""

    _fail(telemetry_config.get("mode") == "v2", "v2 evidence audit requires runner.telemetry.mode=v2")
    manifest_path = telemetry_dir / "telemetry_manifest.json"
    _fail(manifest_path.is_file() and not manifest_path.is_symlink(), f"v2 telemetry manifest is missing: {manifest_path}")
    telemetry_manifest = _read_json(manifest_path, "v2 telemetry manifest")
    _fail(telemetry_manifest.get("schema_version") == "assignment.telemetry.v2.manifest", "v2 telemetry manifest has an unsupported schema")
    for key, value in expected_identity.items():
        if value is not None:
            _fail(telemetry_manifest.get(key) == value, f"v2 telemetry manifest {key} is not bound to this attempt")
    _fail(telemetry_manifest.get("request_payload_persisted") is True, "v2 telemetry manifest does not require payload persistence")
    _fail(telemetry_manifest.get("raw_hardware_inventory") is not None, "v2 telemetry manifest lacks raw hardware inventory")
    profile_hash = telemetry_config["remote_hardware_profile"]["sha256"]
    _fail(telemetry_manifest.get("hardware_profile_sha256") == profile_hash, "v2 telemetry manifest hardware profile hash is not bound")
    rows = _v2_rows(telemetry_dir)
    snapshot_evidence = _audit_v2_snapshots(
        rows=rows,
        telemetry_manifest=telemetry_manifest,
        hardware_profile_sha256=profile_hash,
    )
    try:
        from scripts.validation.audit_v2_journals import audit as audit_journal

        summary = audit_journal(rows)
    except Exception as exc:
        raise CaseRunnerError(f"v2 journal audit failed: {exc}") from exc
    _fail(summary.get("status") == "pass", "v2 journal audit reported internal consistency failures")
    for key in ("run_id", "attempt_id", "case_id"):
        _fail(
            summary.get(key) == telemetry_manifest.get(key) == expected_identity.get(key),
            f"v2 journal {key} is not bound to this attempt's expected manifest identity",
        )
    physical = [
        row for row in rows
        if row.get("terminal") is True
        and row.get("phase") == "model_request"
        and row.get("event_kind") == "model_request"
    ]
    usage_evidence = {
        "json_response_count": 0,
        "explicit_cached_token_count": 0,
        "unavailable_cached_token_count": 0,
    }
    for index, row in enumerate(physical, 1):
        _fail(row.get("request_payload_pre_dispatch") is True, f"physical request {index} lacks pre-dispatch payload proof")
        artifact = row.get("request_payload_artifact")
        _fail(isinstance(artifact, Mapping), f"physical request {index} lacks request payload artifact")
        _verify_payload_artifact(telemetry_dir, artifact.get("request"), label=f"physical request {index} request")
        response_path = _verify_payload_artifact(
            telemetry_dir, artifact.get("response"), label=f"physical request {index} response"
        )
        _fail(artifact.get("physical_request_id") == row.get("physical_request_id"), f"physical request {index} payload identity disagrees")
        usage = _response_usage(response_path)
        if usage is not None:
            usage_evidence["json_response_count"] += 1
            _fail(
                row.get("input_tokens") == usage["prompt_tokens"],
                f"physical request {index} input-token projection differs from retained API response",
            )
            _fail(
                row.get("output_tokens") == usage["completion_tokens"],
                f"physical request {index} output-token projection differs from retained API response",
            )
            if usage["cached_tokens"] is None:
                usage_evidence["unavailable_cached_token_count"] += 1
            else:
                usage_evidence["explicit_cached_token_count"] += 1
            _fail(
                row.get("cached_tokens") == usage["cached_tokens"],
                f"physical request {index} cache-token projection differs from retained API response",
            )
    work_dir = telemetry_dir / "linux_work"
    work_summary = work_dir / "work_summary.json"
    _fail(work_summary.is_file() and not work_summary.is_symlink(), "required Linux work collector summary is missing")
    try:
        parsed_work = _read_json(work_summary, "Linux work collector summary")
    except CaseRunnerError:
        raise
    _fail(parsed_work.get("schema_version") == BPF_WORK_SUMMARY_SCHEMA, "Linux work collector summary has an unsupported schema")
    expected_tool_rows = _expected_cpu_action_rows(rows)
    work_evidence = _audit_bpf_work_evidence(
        work_dir=work_dir,
        summary=parsed_work,
        expected_identity=expected_identity,
        expected_tool_rows=expected_tool_rows,
    )
    artifact_evidence = _audit_bpf_service_artifacts(
        work_dir=work_dir,
        summary=parsed_work,
    )
    operation_count = int(work_evidence["individual_operation_count"])
    _fail(operation_count > 0, "Linux work collector retained no individual CPU operation records")
    raw_model_request_records = len(physical)
    return {
        "summary": summary,
        "journal_sha256": {
            name: sha256_file(telemetry_dir / name)
            for name in (
                "lifecycle_events.jsonl",
                "tool_events.jsonl",
                "model_events.jsonl",
                "hardware_snapshots.jsonl",
            )
        },
        "telemetry_manifest_sha256": sha256_file(manifest_path),
        "physical_request_count": len(physical),
        "raw_model_request_records": raw_model_request_records,
        "individual_cpu_operation_records": operation_count,
        "dropped_cpu_records": 0,
        "cpu_capture_map_failures": 0,
        "missing_raw_request_bodies": 0,
        "payload_capture_verified": True,
        "linux_work_summary": str(work_summary.relative_to(telemetry_dir)),
        "linux_work_evidence": work_evidence,
        "linux_work_artifacts": artifact_evidence,
        "snapshot_evidence": snapshot_evidence,
        "model_usage_evidence": usage_evidence,
    }


def _audit_v2_evidence(
    *,
    telemetry_dir: Path,
    telemetry_config: Mapping[str, Any],
    expected_identity: Mapping[str, str | None],
) -> dict[str, Any]:
    try:
        return _audit_v2_evidence_impl(
            telemetry_dir=telemetry_dir,
            telemetry_config=telemetry_config,
            expected_identity=expected_identity,
        )
    except EvidenceIntegrityError:
        raise
    except CaseRunnerError as exc:
        raise EvidenceIntegrityError(str(exc)) from exc


def _run_official_evaluator_retry(
    *,
    command: Sequence[str],
    runner_config: RunnerConfig,
    runner_result: Any,
    runner_output: Path,
) -> dict[str, Any]:
    """Re-run only the official evaluator for a measured zero-request attempt."""
    environment = _without_v2_activation(os.environ)
    environment.update({str(key): str(value) for key, value in runner_config.environment.items()})
    environment = _without_v2_activation(environment)
    deadline = deadline_from_env(environment, required=True)
    assert deadline is not None
    evaluator_output = runner_result.output_dir
    stdout_path = evaluator_output / "evaluator.stdout.log"
    stderr_path = evaluator_output / "evaluator.stderr.log"
    started = time.monotonic_ns()
    with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
        outcome = run_owned_process(
            [str(item) for item in command],
            cwd=str(runner_config.cwd) if runner_config.cwd is not None else None,
            env=environment,
            stdout=stdout,
            stderr=stderr,
            deadline_mono_ns=deadline,
            timeout_seconds=None,
        )
    ended = time.monotonic_ns()
    metadata = {
        "schema_version": "assignment-empty-patch-evaluator-retry.v1",
        "status": "timeout" if outcome.timed_out else ("completed" if outcome.returncode == 0 else "failed"),
        "returncode": outcome.returncode,
        "timed_out": outcome.timed_out,
        "command_sha256": command_hash(command),
        "started_mono_ns": started,
        "ended_mono_ns": ended,
        "deadline_mono_ns": deadline,
        "cleanup": dict(outcome.cleanup),
    }
    _atomic_json(runner_output / "evaluator_retry.json", metadata)
    return metadata


def _validate_adaptive_outputs(metadata: Mapping[str, Any]) -> dict[str, Any]:
    root = Path(_string(metadata.get("protocol_root"), "adaptive protocol root")).resolve()
    manifest = root / "adaptive_prediction_manifest.json"
    score_path = root / "adaptive_score_report.json"
    journal = root / "adaptive_events.jsonl"
    manifest_sha = _verify_sidecar(manifest, "adaptive prediction manifest")
    score_sha = _verify_sidecar(score_path, "adaptive score report")
    _fail(journal.is_file() and not journal.is_symlink(), "adaptive journal is missing")
    score = _read_json(score_path, "adaptive score report")
    _fail(score.get("schema_version") == "assignment.adaptive-event-score.v1", "unsupported adaptive score report")
    _fail(score.get("prediction_manifest_sha256") == manifest_sha, "adaptive score does not bind the frozen prediction manifest")
    _fail(isinstance(score.get("passed"), bool), "adaptive score report lacks a boolean acceptance result")
    _fail(isinstance(score.get("event_scores"), list) and bool(score["event_scores"]), "adaptive score report has no event scores")
    return {
        **dict(metadata),
        "prediction_manifest_path": str(manifest),
        "prediction_manifest_sha256": manifest_sha,
        "journal_path": str(journal),
        "journal_sha256": sha256_file(journal),
        "score_path": str(score_path),
        "score_sha256": score_sha,
        "passed": score["passed"],
        "unavailable_event_count": score.get("unavailable_event_count"),
        "event_count": len(score["event_scores"]),
        "trajectory_score": score.get("trajectory_score"),
    }


def _case_from_args(args: argparse.Namespace, manifest: Mapping[str, Any] | None = None) -> dict[str, Any]:
    path = Path(args.case_spec).resolve()
    # Execution needs the bound deadline before entering the owned lifecycle.
    # Validate the same manifest/plan again inside _execute before any launch.
    if manifest is None and _read_json(path, "case specification").get("schema_version") == CONFIRMATION_CASE_SCHEMA:
        manifest_path = args.runtime_manifest or os.environ.get("ASSIGNMENT_RUNTIME_MANIFEST")
        _fail(manifest_path is not None, "confirmation case requires its runtime manifest")
        manifest_path = Path(manifest_path).resolve()
        _verify_manifest_sidecar(manifest_path)
        manifest = load_manifest(manifest_path)
    return load_case(
        path, confirmation_plan=getattr(args, "confirmation_plan", None),
        confirmation_plan_sha256=getattr(args, "confirmation_plan_sha256", None),
        runtime_manifest=manifest,
    )


def execute(args: argparse.Namespace) -> int:
    if not args.execute:
        return _execute(args)
    inherited = deadline_from_env()
    case = _case_from_args(args)
    local_deadline = deadline_with_timeout(case["per_case_deadline_seconds"])
    deadline = min(inherited, local_deadline) if inherited is not None else local_deadline
    old_owner = os.environ.get(CASE_OWNER_ENV)
    owner = old_owner or uuid.uuid4().hex
    _fail(bool(re.fullmatch(r"[0-9a-f]{32}", owner)), "invalid inherited case ownership identity")
    os.environ[CASE_OWNER_ENV] = owner
    try:
        with deadline_environment(deadline):
            return _execute(args)
    finally:
        if old_owner is None:
            os.environ.pop(CASE_OWNER_ENV, None)
        else:
            os.environ[CASE_OWNER_ENV] = old_owner


def _execute(args: argparse.Namespace) -> int:
    manifest_path = args.runtime_manifest or (Path(os.environ["ASSIGNMENT_RUNTIME_MANIFEST"]) if os.environ.get("ASSIGNMENT_RUNTIME_MANIFEST") else None)
    _fail(manifest_path is not None, "runtime manifest is required via --runtime-manifest or ASSIGNMENT_RUNTIME_MANIFEST")
    manifest_path = Path(manifest_path).resolve()
    manifest_digest = _verify_manifest_sidecar(manifest_path)
    manifest = load_manifest(manifest_path)
    if "cpu_policy" in manifest["runner"]:
        _fail(getattr(args, "cpu_docker", False), "CPU policy requires actual CPU-Docker placement")
        with cpu_policy.runtime_placement(manifest_path, manifest_digest, active=args.execute):
            return _execute_bound(args, manifest_path, manifest_digest, manifest)
    _fail(cpu_policy.from_environment() is None, "inherited CPU policy is absent from this runtime")
    return _execute_bound(args, manifest_path, manifest_digest, manifest)


def _bind_execution_snapshot(source_manifest: Path, *, repo: Path, output_dir: Path,
                             runtime_path: Path) -> dict[str, Any]:
    """Verify all frozen source bytes and retain the receipt with this case."""
    from scripts.validation.materialize_execution_snapshot import verify

    receipt = _read_json(source_manifest, "execution source manifest")
    _fail(Path(receipt.get("execution_root", "")).resolve() == repo.resolve(),
          "execution source manifest belongs to a different checkout")
    proof = verify(source_manifest, compare_working=False)
    inputs = output_dir / "execution_inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    retained = []
    for source, name in (
        (source_manifest, "source_manifest.json"),
        (Path(str(source_manifest) + ".sha256"), "source_manifest.json.sha256"),
        (runtime_path, "runtime_manifest.json"),
        (Path(str(runtime_path) + ".sha256"), "runtime_manifest.json.sha256"),
    ):
        target = inputs / name
        expected = sha256_file(source)
        if target.exists():
            _fail(not target.is_symlink() and sha256_file(target) == expected,
                  f"execution input collision: {name}")
        else:
            _copy_native_archive(source, target)
        retained.append({"path": str(target.relative_to(output_dir)), "sha256": expected,
                         "size": target.stat().st_size})
    return {**proof, "artifacts": retained}


def _execute_bound(args: argparse.Namespace, manifest_path: Path, manifest_digest: str,
                   manifest: dict[str, Any]) -> int:
    case_path = Path(args.case_spec).resolve()
    output_dir = Path(args.output_dir).resolve()
    _fail(case_path.is_file(), f"case specification is unavailable: {case_path}")
    case = _case_from_args(args, manifest)
    if case["schema_version"] == CONFIRMATION_CASE_SCHEMA:
        _fail("cpu_policy" in manifest["runner"], "confirmation requires the reviewed fixed CPU policy")
    run_id = f"assignment-{hashlib.sha256(case['resume_key'].encode()).hexdigest()[:16]}"
    output_dir.mkdir(parents=True, exist_ok=True)
    _fail(_inside(case_path, output_dir), "case specification must be inside output-dir")
    with _locked_case(output_dir, case, execute=args.execute):
        result_path = output_dir / "case_result.json"
        _fail(not result_path.exists(), f"output collision: existing case result must be handled by the matrix resume layer: {result_path}")
        repo, git_state = validate_checkout(manifest)
        integrity = _verify_execution_integrity(manifest, repo)
        execution_snapshot = None
        if getattr(args, "execution_source_manifest", None) is not None:
            execution_snapshot = _bind_execution_snapshot(
                Path(args.execution_source_manifest).resolve(), repo=repo,
                output_dir=output_dir, runtime_path=manifest_path,
            )
        static = validate_static_environment(manifest, case, repo)
        tool_runtime_artifacts = _retain_tool_runtime_inputs(manifest, repo, output_dir)
        adaptive: dict[str, Any] | None = None
        if args.adaptive_runtime_config is not None:
            adaptive = _validate_adaptive_config(
                Path(args.adaptive_runtime_config),
                manifest_path=manifest_path,
                manifest_sha256=manifest_digest,
                run_id=run_id,
                repo=repo,
                output_dir=output_dir,
            )
        validation = {"schema_version": "assignment-case-validation.v1", "status": "passed", "case_sha256": sha256_file(case_path), "manifest_sha256": manifest_digest, "integrity": integrity, "git": git_state, "suite": case["suite"], "instance_id": case["instance_id"], "settings": case["settings"], "dataset": static, "adaptive_runtime": adaptive}
        if execution_snapshot is not None:
            validation["execution_snapshot"] = execution_snapshot
        if tool_runtime_artifacts:
            validation["tool_runtime_artifacts"] = tool_runtime_artifacts
        if "cpu_policy" in manifest["runner"]:
            validation["cpu_policy"] = manifest["runner"]["cpu_policy"]
            validation["control_affinity_cpus"] = sorted(os.sched_getaffinity(0)) if args.execute else None
        if case["schema_version"] == CONFIRMATION_CASE_SCHEMA:
            validation["confirmation_binding"] = case["confirmation_binding"]
        _atomic_json(output_dir / "validation.json", validation)
        if not args.execute:
            print(json.dumps(validation, sort_keys=True))
            return 0

        if manifest["runner"]["project"]:
            _fail(shutil.which("uv") is not None, "uv is required for the reviewed project runner")
        hardware = _probe_hardware(manifest, cpu_docker=getattr(args, "cpu_docker", False))
        telemetry_config = _manifest_telemetry(manifest)
        try:
            remote_profile, remote_profile_sha256 = _load_remote_hardware_profile(telemetry_config)
            model_hardware = _model_hardware_from_remote_profile(
                remote_profile,
                clock=clock_metadata(),
            ) if telemetry_config["mode"] == "v2" else model_hardware_features(
                {
                    "cpu_frequency_hz": None,
                    "cpu_frequency_source": None,
                    "gpu_memory_bandwidth_bytes_per_s": None,
                    "gpu_compute_tflops": None,
                    "clock": clock_metadata(),
                    "availability": {
                        "cpu_frequency_hz": "unavailable",
                        "gpu_memory_bandwidth_bytes_per_s": "unavailable",
                        "gpu_compute_tflops": "unavailable",
                    },
                }
            )
        except EvidenceIntegrityError:
            raise
        except CaseRunnerError as exc:
            if telemetry_config["mode"] == "v2":
                raise EvidenceIntegrityError(f"v2 remote hardware evidence is unavailable: {exc}") from exc
            raise
        local_cpu = local_cpu_profile()
        raw_hardware = {
            "local_probe": hardware,
            "local_cpu_profile": local_cpu,
            "remote_profile": remote_profile if telemetry_config["mode"] == "v2" else None,
            "remote_profile_sha256": remote_profile_sha256,
            "model_hardware_policy": {
                "numeric_terms": [
                    "cpu_frequency_hz",
                    "gpu_memory_bandwidth_bytes_per_s",
                    "gpu_compute_tflops",
                ],
                "clock_assumption": "declared numeric capacities only; no thread or I/O scaling",
                "storage_term": "absent until measured work volume is paired with bandwidth",
            },
        }
        state_path = output_dir / "runner_state.json"
        attempt = 1
        if state_path.exists():
            previous = _read_json(state_path, "runner state")
            _fail(previous.get("status") != "completed", "successful runner state already exists")
            attempt = int(previous.get("attempt", 0)) + 1
        state = {"schema_version": STATE_SCHEMA, "status": "starting", "attempt": attempt, "case_sha256": validation["case_sha256"], "manifest_sha256": validation["manifest_sha256"], "git": git_state, "hardware": hardware, "telemetry": telemetry_config, "remote_hardware_profile_sha256": remote_profile_sha256}
        _atomic_json(state_path, state)
        runner_output = output_dir / "runner_attempts" / f"attempt-{attempt:03d}"
        _fail(not runner_output.exists(), f"output collision: runner output already exists: {runner_output}")
        runner_output.mkdir(parents=True)
        evaluator_run_id = f"{run_id}-attempt-{attempt:03d}-{uuid.uuid4().hex}"
        runner_instances_path, runner_instances_sha256 = _materialize_runner_instances(
            source=Path(static["instances_path"]),
            instance_id=case["instance_id"],
            output_dir=runner_output,
        )

        settings = case["settings"]
        runner_manifest = manifest["runner"]
        request_config_path, request_config_sha256 = _materialize_request_config(
            source=_resolve(repo, str(runner_manifest["request_config_path"])),
            max_output_tokens=settings["max_output_tokens"],
            output_dir=runner_output,
            top_p=float(settings.get("top_p", 1.0)),
            seed=int(settings.get("seed", 0)),
        )
        proxy_process: subprocess.Popen[str] | None = None
        proxy_metadata: dict[str, Any] = {}
        proxy_exit_before_stop: int | None = None
        proxy_shutdown_returncode: int | None = None
        owned_docker_enabled = False
        docker_cleanup: dict[str, Any] | None = None
        runner_command: list[str] = []
        runner_result = None
        try:
            deadline = deadline_from_env(required=True)
            _fail(deadline is not None and remaining_seconds(deadline) > 0, "case deadline expired before proxy launch")
            proxy_process, proxy_metadata = _start_request_proxy(
                manifest=manifest,
                repo=repo,
                output_dir=runner_output,
                run_id=f"assignment-{hashlib.sha256(case['resume_key'].encode()).hexdigest()[:16]}",
                deadline_seconds=case["per_case_deadline_seconds"],
                adaptive_config_path=Path(adaptive["config_path"]) if adaptive is not None else None,
                attempt_id=f"attempt-{attempt:03d}",
                case_id=case["resume_key"],
                instance_id=case["instance_id"],
                telemetry=manifest["runner"]["telemetry"],
                model_hardware=model_hardware,
            )
            proxy_api_base = str(proxy_metadata["listen_api_base"])
            reviewed_executable = (
                str(_strict_path(manifest["integrity"]["adaptive_runner_path"], "integrity.adaptive_runner_path"))
                if adaptive is not None
                else str(runner_manifest["executable"])
            )
            runner_command = build_command(
                executable=reviewed_executable,
                project=_resolve(repo, str(runner_manifest["project"])),
                config_path=_resolve(repo, str(runner_manifest["config_path"])),
                request_config_path=request_config_path,
                instances_path=runner_instances_path,
                model=_vllm_client_model(manifest["model"]["name"]),
                model_revision=manifest["model"]["revision"],
                api_base=proxy_api_base,
                api_key=manifest["model"]["api_key"],
                instance_id=case["instance_id"],
                output_dir=runner_output,
                max_input_tokens=int(settings.get("max_input_tokens", 32768)),
                max_output_tokens=settings["max_output_tokens"],
                max_observation_length=settings["observation_length"],
                temperature=float(settings["temperature"]),
                seed=int(settings.get("seed", 0)),
                per_instance_call_limit=settings["call_limit"],
                num_workers=1,
                extra_args=runner_manifest["extra_args"],
            )
            _fail("--num_workers" in runner_command and runner_command[runner_command.index("--num_workers") + 1] == "1", "runner command does not enforce concurrency=1")
            if getattr(args, "cpu_docker", False):
                runner_command = _owned_docker_command(
                    runner_command, _resolve(repo, str(runner_manifest["project"])),
                    os.environ[CASE_OWNER_ENV], runner_output,
                    placement=runner_manifest.get("cpu_policy"),
                )
                owned_docker_enabled = True
            result_template = str(manifest["evaluator"]["result_path"])
            gpu_identity = (
                hardware["gpus"][0]
                if hardware["gpus"]
                else {"execution_mode": "cpu-docker-runner+h100-inference"}
            )
            if telemetry_config["mode"] == "v2":
                # A CPU-side runner often has no visible GPU.  Bind the
                # normalization identity to the sealed remote GPU profile and
                # the actual local CPU/clock host instead of a constant mode
                # string, so transfer provenance cannot collapse across GPUs.
                hardware_identity = composite_hardware_id(
                    remote_profile_sha256=str(remote_profile_sha256),
                    local_cpu=local_cpu,
                    clock=clock_metadata(),
                )
            else:
                hardware_identity = hashlib.sha256(_canonical(gpu_identity).encode("utf-8")).hexdigest()
            normalization_spec_path = output_dir / "normalization_spec.json"
            normalization_spec = {
                "run_id": run_id,
                "suite": case["suite"],
                "repository": case["repository"],
                "category": case["repository"],
                "instance_id": case["instance_id"],
                "config_id": case["cell_id"],
                "repeat_id": "r0",
                "hardware_id": hardware_identity,
                "hardware_identity_components": {
                    "remote_profile_sha256": remote_profile_sha256,
                    "local_cpu_profile": local_cpu,
                    "clock": clock_metadata(),
                    "model_facing_projection": [
                        "cpu_frequency_hz",
                        "gpu_memory_bandwidth_bytes_per_s",
                        "gpu_compute_tflops",
                    ],
                },
                "model_revision": manifest["pins"]["model_revision"],
                "swe_agent_revision": manifest["pins"]["swe_agent_revision"],
                "swe_bench_revision": manifest["pins"]["swe_bench_revision"],
                "source_dataset_path": static["instances_path"],
                "source_dataset_sha256": static["dataset_sha256"],
                "runner_instances_path": str(runner_instances_path.relative_to(output_dir)),
                "runner_instances_sha256": runner_instances_sha256,
                "request_config_path": str(request_config_path.relative_to(output_dir)),
                "request_config_sha256": request_config_sha256,
                "request_config_template_sha256": integrity["request_config_sha256"],
                "command_sha256": command_hash(runner_command),
                "settings": settings,
                "sweep_parameter": case["variation"]["knob"] if case["variation"] else None,
                "sweep_value": case["variation"]["value"] if case["variation"] else None,
            }
            _atomic_json(normalization_spec_path, normalization_spec)
            result_values = {
                "case_spec": str(case_path),
                "output_dir": str(runner_output),
                "runner_output_dir": str(runner_output),
                "dataset_path": static["instances_path"],
                "predictions_path": str(runner_output / "preds.json"),
                "instance_id": case["instance_id"],
                "suite": case["suite"],
                "report_dir": str(runner_output / "official_evaluator"),
                "run_id": evaluator_run_id,
            }
            result_fields = set(re.findall(r"\{([A-Za-z0-9_]+)\}", result_template))
            _fail(result_fields.issubset(result_values), "evaluator result path contains an unknown placeholder")
            evaluator_result_path = Path(result_template.format(**result_values))
            if not evaluator_result_path.is_absolute():
                evaluator_result_path = (runner_output / evaluator_result_path).resolve()
            else:
                evaluator_result_path = evaluator_result_path.resolve()
            evaluator_values = {**result_values, "evaluator_result": str(evaluator_result_path)}
            _fail(_inside(evaluator_result_path, runner_output), "evaluator result path must stay inside this attempt output-dir")
            evaluator_command = _format_argv(manifest["evaluator"]["command"], evaluator_values)
            evaluator_command = _bind_evaluator_project_command(
                evaluator_command, _resolve(repo, str(manifest["evaluator"]["project"])),
                manifest["pins"]["swe_bench_revision"],
            )
            state.update({"status": "running", "runner_command_hash": command_hash(runner_command), "runner_output_dir": str(runner_output.relative_to(output_dir)), "evaluator_command_hash": command_hash(evaluator_command), "proxy": proxy_metadata})
            _atomic_json(state_path, state)
            runner_config = RunnerConfig(
                command=runner_command,
                experiment_id=run_id,
                instance_id=case["instance_id"],
                dataset=case["suite"],
                work_root=runner_output / "reviewed_runner_artifacts",
                attempt_id=f"attempt-{attempt:03d}",
                cwd=_resolve(repo, str(runner_manifest["working_directory"])),
                timeout_seconds=case["per_case_deadline_seconds"],
                model_revision=manifest["pins"]["model_revision"],
                swe_agent_revision=manifest["pins"]["swe_agent_revision"],
                swe_bench_revision=manifest["pins"]["swe_bench_revision"],
                config={"assignment_case": case["resume_key"], "dataset_revision": static["dataset_revision"], "request_proxy": proxy_metadata, "adaptive_runtime": adaptive},
                environment=(
                    {
                        "ASSIGNMENT_ADAPTIVE_RUNTIME_CONFIG": str(adaptive["config_path"]),
                        "ASSIGNMENT_TELEMETRY_V2_HARDWARE_PROFILE_PATH": str(
                            telemetry_config["remote_hardware_profile"]["path"]
                        ) if telemetry_config["mode"] == "v2" else "",
                    }
                    if adaptive is not None
                    else {
                        "ASSIGNMENT_TELEMETRY_V2_HARDWARE_PROFILE_PATH": str(
                            telemetry_config["remote_hardware_profile"]["path"]
                        ) if telemetry_config["mode"] == "v2" else "",
                    }
                ),
                telemetry_requirements={
                    **telemetry_config,
                    "hardware_profile_path": str(
                        telemetry_config["remote_hardware_profile"]["path"]
                    ) if telemetry_config["mode"] == "v2" else None,
                    "hardware_profile_sha256": remote_profile_sha256,
                    "model_hardware": model_hardware,
                    "cpu_work": dict(telemetry_config["cpu_work"]),
                },
            )
            telemetry_v2 = TelemetryV2(
                runner_output / "telemetry_v2",
                run_id=run_id,
                attempt_id=f"attempt-{attempt:03d}",
                case_id=case["resume_key"],
                instance_id=case["instance_id"],
                model=manifest["model"]["name"],
                model_revision=manifest["model"]["revision"],
                hardware=raw_hardware,
                model_hardware=model_hardware,
                hardware_profile_sha256=remote_profile_sha256,
                writer_role="runner",
            )
            runner_result = run_sweagent(runner_config, evaluator_command=evaluator_command, telemetry=telemetry_v2)
        finally:
            proxy_exit_before_stop = proxy_process.poll() if proxy_process is not None else None
            proxy_shutdown_returncode = _terminate_proxy(proxy_process)
            if owned_docker_enabled:
                docker_cleanup = cleanup_owned_containers(os.environ[CASE_OWNER_ENV], runner_output / "docker_cleanup")
        def require_evidence(condition: bool, message: str) -> None:
            if condition:
                return
            if telemetry_config["mode"] == "v2":
                raise EvidenceIntegrityError(message)
            _fail(False, message)

        require_evidence(runner_result is not None, "reviewed SWE-agent runner produced no result")
        if telemetry_config["mode"] == "v2" and runner_result is not None:
            # A v2 runner result with a zero exit code is still unusable when
            # the reviewed child never completed its sitecustomize handshake.
            # Keep this classification separate from an ordinary agent result
            # so run_matrix stops assigning the remaining cases.
            require_evidence(
                runner_result.cleanup.get("instrumentation_missing") is not True,
                "reviewed SWE-agent child did not activate the required v2 hooks",
            )
        require_evidence(proxy_exit_before_stop is None, "request proxy exited before the SWE-agent runner completed")
        require_evidence(proxy_shutdown_returncode == 0, "request proxy did not finish graceful event finalization")
        if owned_docker_enabled:
            require_evidence(
                docker_cleanup is not None and docker_cleanup["cleanup_complete"],
                "owned Docker cleanup or evidence capture did not complete",
            )
        proxy_events = runner_output / "request_proxy.jsonl"
        try:
            proxy_event_summary = _validate_proxy_events(proxy_events, adaptive_required=adaptive is not None)
        except CaseRunnerError as exc:
            if telemetry_config["mode"] == "v2":
                raise EvidenceIntegrityError(f"v2 request proxy evidence is invalid: {exc}") from exc
            raise
        proxy_metadata.update({
            "event_count": proxy_event_summary["event_count"],
            "events_sha256": proxy_event_summary["sha256"],
            "exit_code_before_stop": proxy_exit_before_stop,
            "shutdown_returncode": proxy_shutdown_returncode,
        })
        if telemetry_config["mode"] == "v2":
            serving_path_value = proxy_metadata.get("serving_metrics_config_path")
            require_evidence(
                isinstance(serving_path_value, str) and bool(serving_path_value),
                "v2 request proxy did not publish its serving metrics configuration path",
            )
            serving_path = (runner_output / serving_path_value).resolve()
            require_evidence(
                _inside(serving_path, runner_output)
                and serving_path.is_file()
                and not serving_path.is_symlink(),
                "v2 serving metrics configuration artifact is missing or escapes the attempt",
            )
            serving_value = _read_json(serving_path, "serving metrics configuration artifact")
            expected_serving = telemetry_config["serving_metrics"]
            require_evidence(
                _canonical(serving_value) == _canonical(expected_serving),
                "request proxy serving metrics configuration is not bound to the manifest",
            )
            expected_semantic_hash = _serving_metrics_config_sha256(expected_serving)
            require_evidence(
                proxy_metadata.get("serving_metrics_config_sha256") == expected_semantic_hash,
                "request proxy serving metrics semantic hash is not bound to the manifest",
            )
            require_evidence(
                proxy_metadata.get("serving_metrics_config_file_sha256") == sha256_file(serving_path),
                "request proxy serving metrics configuration file hash is not bound",
            )
        transport_failure = (
            _classify_model_transport_failure(proxy_events)
            if telemetry_config["mode"] == "v2"
            else None
        )
        # Archive native partial evidence even when the transport failed.
        # Classification below still rejects infrastructure failures.
        _atomic_json(runner_output / "request_proxy_provenance.json", proxy_metadata)
        native_serving_summary: dict[str, Any] | None = None
        if (
            telemetry_config["mode"] == "v2"
            and telemetry_config["serving_metrics"].get("mode") == "native_deferred"
        ):
            try:
                native_serving_summary = _acquire_native_server_evidence(
                    output_dir=runner_output,
                    telemetry_dir=runner_output / "telemetry_v2",
                    telemetry_config=telemetry_config,
                    expected_identity={
                        "run_id": run_id,
                        "attempt_id": f"attempt-{attempt:03d}",
                        "case_id": case["resume_key"],
                    },
                )
            except Exception as exc:
                raise EvidenceIntegrityError(f"native serving evidence acquisition failed: {exc}") from exc
        if transport_failure is not None:
            # A complete raw request journal is necessary for diagnosis but
            # cannot make a missing model endpoint a valid unresolved solver
            # result.  Raise the evidence-class failure before evaluator
            # fallback so run_matrix halts new assignments.
            raise EvidenceIntegrityError(
                "model transport/server infrastructure failure: "
                + str(transport_failure["reason"])
                + "; "
                + _canonical(transport_failure)
            )
        v2_evidence_summary: dict[str, Any] | None = None
        if telemetry_config["mode"] == "v2":
            v2_evidence_summary = _audit_v2_evidence(
                telemetry_dir=runner_output / "telemetry_v2",
                telemetry_config=telemetry_config,
                expected_identity={
                    "run_id": run_id,
                    "attempt_id": f"attempt-{attempt:03d}",
                    "case_id": case["resume_key"],
                },
            )
        eval_metadata: dict[str, Any] = {}
        eval_files = sorted(runner_result.output_dir.rglob("eval.json"))
        if eval_files:
            eval_metadata = _read_json(eval_files[-1], "runner evaluator status")
        predictions_path = runner_output / "preds.json"
        zero_request_evaluator_retry = False
        if proxy_event_summary["event_count"] == 0:
            predictions_ready = predictions_path.is_file() and not predictions_path.is_symlink()
            zero_request_evaluator_retry = (
                eval_metadata.get("status") != "completed" or not predictions_ready
            )
            if zero_request_evaluator_retry:
                if predictions_path.exists() or predictions_path.is_symlink():
                    _fail(
                        predictions_ready,
                        "zero-request fallback predictions path must be a regular file",
                    )
                else:
                    _atomic_json(
                        predictions_path,
                        [{
                            "instance_id": case["instance_id"],
                            "model_name_or_path": str(manifest["model"]["name"]),
                            "model_patch": "",
                        }],
                    )

                existing_result_is_valid = False
                if evaluator_result_path.exists() or evaluator_result_path.is_symlink():
                    _fail(
                        evaluator_result_path.is_file() and not evaluator_result_path.is_symlink(),
                        "official evaluator result must be a regular file",
                    )
                    try:
                        _validate_official_evaluator_result(
                            path=evaluator_result_path,
                            output_dir=output_dir,
                            instance_id=case["instance_id"],
                            run_id=evaluator_run_id,
                            dataset_path=Path(static["instances_path"]),
                            dataset_sha256=static["dataset_sha256"],
                            predictions_path=predictions_path,
                        )
                    except CaseRunnerError:
                        evaluator_result_path.unlink()
                    else:
                        existing_result_is_valid = True
                if not existing_result_is_valid:
                    _run_official_evaluator_retry(
                        command=evaluator_command,
                        runner_config=runner_config,
                        runner_result=runner_result,
                        runner_output=runner_output,
                    )
        if (
            eval_files
            and eval_metadata.get("status") != "completed"
            and not zero_request_evaluator_retry
        ):
            _fail(eval_metadata.get("status") == "completed", "official evaluator command did not complete successfully")
        adaptive_result = _validate_adaptive_outputs(adaptive) if adaptive is not None else None
        if adaptive_result is not None:
            _fail(
                adaptive_result["passed"] is True,
                "adaptive event/E2E prediction did not pass the 25% acceptance gate",
            )
        refs = inventory_artifacts(runner_output, output_dir)
        if adaptive is not None:
            for item in inventory_artifacts(Path(adaptive["protocol_root"]), output_dir):
                if not any(existing["path"] == item["path"] for existing in refs):
                    refs.append(item)
        eval_status = "missing"
        official: dict[str, Any] = {}
        if evaluator_result_path.is_file() and not evaluator_result_path.is_symlink():
            official = _validate_official_evaluator_result(
                path=evaluator_result_path,
                output_dir=output_dir,
                instance_id=case["instance_id"],
                run_id=evaluator_run_id,
                dataset_path=Path(static["instances_path"]),
                dataset_sha256=static["dataset_sha256"],
                predictions_path=predictions_path,
            )
            _fail(official[manifest["evaluator"]["resolved_field"]] == official["official_resolved"], "runtime manifest resolved field disagrees with evaluator schema")
            _fail(official[manifest["evaluator"]["submitted_field"]] is True, "runtime manifest submitted field is not true")
            eval_status = "completed"
        else:
            eval_status = "missing"
        zero_request_empty_patch_completed = (
            proxy_event_summary["event_count"] == 0
            and eval_status == "completed"
            and official.get("official_resolved") is False
            and official.get("submitted") is True
            and official.get("empty_patch_unresolved") is True
        )
        evaluator_ref = str(evaluator_result_path.relative_to(output_dir)) if evaluator_result_path.is_relative_to(output_dir) else None
        if evaluator_ref and evaluator_result_path.is_file() and not any(item["path"] == evaluator_ref for item in refs):
            refs.append({"kind": "evaluator", "path": evaluator_ref, "sha256": sha256_file(evaluator_result_path), "size": evaluator_result_path.stat().st_size})
        for item in inventory_artifacts(runner_output / "official_evaluator", output_dir):
            if not any(existing["path"] == item["path"] for existing in refs):
                refs.append(item)
        for source, kind in ((case_path, "case_spec"), (output_dir / "validation.json", "validation")):
            reference = str(source.relative_to(output_dir))
            if not any(existing["path"] == reference for existing in refs):
                refs.append({"kind": kind, "path": reference, "sha256": sha256_file(source), "size": source.stat().st_size})
        if execution_snapshot is not None:
            refs.extend(inventory_artifacts(output_dir / "execution_inputs", output_dir))
        normalization_reference = str(normalization_spec_path.relative_to(output_dir))
        refs.append({"kind": "normalization_spec", "path": normalization_reference, "sha256": sha256_file(normalization_spec_path), "size": normalization_spec_path.stat().st_size})
        refs.sort(key=lambda item: item["path"])
        completed = (
            eval_status == "completed"
            and (
                runner_result.status == "completed"
                or zero_request_empty_patch_completed
            )
        )
        normalization_sources: dict[str, str | None] = {
            "run_spec": normalization_reference,
            "trajectory": None,
            "model_events": None,
            "runner_summary": None,
            "official_evaluator_result": evaluator_ref,
        }
        if completed:
            normalization_sources["model_events"] = str(proxy_events.relative_to(output_dir))
            trajectory_matches = [
                str(item["path"])
                for item in refs
                if (
                    Path(str(item["path"])).name in {"trajectory.json", "trajectory.traj"}
                    or Path(str(item["path"])).suffix == ".traj"
                )
            ]
            summary_matches = [
                str(item["path"])
                for item in refs
                if (
                    Path(str(item["path"])).name == "summary.json"
                    and "reviewed_runner_artifacts" in Path(str(item["path"])).parts
                )
            ]
            if zero_request_empty_patch_completed:
                if len(trajectory_matches) == 1:
                    normalization_sources["trajectory"] = trajectory_matches[0]
                if len(summary_matches) == 1:
                    normalization_sources["runner_summary"] = summary_matches[0]
            else:
                _fail(len(trajectory_matches) == 1, "completed assignment case requires exactly one SWE-agent trajectory; found " + str(len(trajectory_matches)))
                _fail(len(summary_matches) == 1, "completed assignment case requires exactly one reviewed runner summary; found " + str(len(summary_matches)))
                normalization_sources["trajectory"] = trajectory_matches[0]
                normalization_sources["runner_summary"] = summary_matches[0]
        status = "completed" if completed else ("timeout" if runner_result.status == "timeout" else "failed")
        reason = None if completed else ("missing_official_evaluator_result" if eval_status != "completed" else f"runner_{runner_result.status}")
        final = _result(
            case,
            status,
            reason=reason,
            run_id=run_id,
            case_sha256=validation["case_sha256"],
            manifest_sha256=validation["manifest_sha256"],
            integrity=integrity,
            git=git_state,
            hardware=hardware,
            runner={"status": runner_result.status, "returncode": runner_result.returncode, "output_dir": str(runner_output.relative_to(output_dir)), "command_hash": runner_result.command_hash},
            proxy=proxy_metadata,
            evaluator={"status": eval_status, "runner_status": eval_metadata.get("status"), "result_path": evaluator_ref, "official_resolved": official.get(manifest["evaluator"]["resolved_field"]), "submitted": official.get(manifest["evaluator"]["submitted_field"])},
            adaptive_runtime=adaptive_result,
            telemetry={
                "mode": telemetry_config["mode"],
                "requirements": telemetry_config,
                "remote_hardware_profile_sha256": remote_profile_sha256,
                "v2_evidence": v2_evidence_summary,
                "native_serving": native_serving_summary,
            },
            normalization_sources=normalization_sources,
            artifacts=refs,
        )
        if not completed:
            final["schema_version"] = FAILURE_RESULT_SCHEMA
            final["accepted"] = False
            final["artifacts"], final["inventory_errors"] = _failure_inventory(output_dir)
            final["failure"] = {"type": "ExecutionFailure", "message": reason, "recorded_epoch_ns": time.time_ns()}
        _atomic_json(result_path, final)
        result_digest = sha256_file(result_path)
        Path(str(result_path) + ".sha256").write_text(
            f"{result_digest}  {result_path.name}\n", encoding="utf-8"
        )
        state.update({"status": status, "ended_epoch": int(time.time()), "result_sha256": result_digest, "artifact_count": len(refs)})
        _atomic_json(state_path, state)
        print(json.dumps(final, sort_keys=True))
        return 0 if completed else 2


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--case-spec", required=True, type=Path)
    result.add_argument("--output-dir", required=True, type=Path)
    result.add_argument("--runtime-manifest", type=Path)
    result.add_argument("--execution-source-manifest", type=Path, help="verify and retain the exact current-byte execution snapshot receipt")
    result.add_argument("--confirmation-plan", type=Path, help="declared 96-case confirmation execution plan")
    result.add_argument("--confirmation-plan-sha256", help="reviewed SHA-256 of the confirmation execution plan")
    result.add_argument(
        "--adaptive-runtime-config",
        type=Path,
        help="sealed per-run adaptive predictor configuration; enables predict-before-reveal event scoring",
    )
    result.add_argument("--execute", action="store_true", help="run the reviewed SWE-agent and official evaluator")
    result.add_argument("--validate-only", action="store_true", help="validate and launch nothing (the default)")
    result.add_argument(
        "--cpu-docker",
        action="store_true",
        help="run the case runner on a CPU VM using Docker while inference stays on the configured H100 endpoint",
    )
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.execute and args.validate_only:
        print("NOT_READY: --execute and --validate-only are mutually exclusive", file=sys.stderr)
        return 1
    try:
        return execute(args)
    except (CaseRunnerError, RunnerContractError, OSError, ValueError) as exc:
        print(f"NOT_READY: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
