"""Explicit Docker/direct vLLM runtime backends.

This module owns server lifecycle only.  It deliberately does not construct
validation requests, read experiment rows, or inspect measured labels.  The
sealed request/protocol implementation remains in the existing H100 runner.

The backend is intentionally fail-closed:

* the caller must select ``auto``, ``docker``, or ``direct`` (a manifest value
  is an explicit selection; an ambient environment variable is not);
* the two backends have different command builders and cleanup paths; and
* backend identity, command identity, protocol identity, and prediction
  state are bound into the resumable run state.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .vllm_config import VLLMConfigError, read_instance_manifest, resolve_vllm_config


BACKENDS = ("docker", "direct")
BACKEND_SELECTIONS = ("auto", *BACKENDS)
BACKEND_SCHEMA_VERSION = "runtime-backend.v1"
STATE_SCHEMA_VERSION = "runtime-run-state.v1"
METADATA_SCHEMA_VERSION = "runtime-run-metadata.v1"
DEFAULT_ARTIFACT_ROOT = Path("artifacts/runtime")
DEFAULT_CONTAINER_NAME = "agentic-sim-vllm"
DEFAULT_HEALTH_TIMEOUT_SECONDS = 180.0
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 10.0
DEFAULT_TRACE_SESSION = "h100-final-validation"
MODEL_REVISION = "b2cff646eb4bb1d68355c01b18ae02e7cf42d120"

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_FORBIDDEN_MEASURED_INPUTS = (
    "wall_ms",
    "observed_seconds",
    "actual_prompt_tokens",
    "actual_completion_tokens",
    "generated_tokens",
    "completion_tokens",
    "cpu_activity_union_ms",
    "cuda_activity_union_ms",
    "kernel_duration_sum_ms",
    "target_label",
    "measured_label",
)
_PROCESS_ENV_ALLOWLIST = {
    "CUDA_DEVICE_ORDER",
    "CUDA_VISIBLE_DEVICES",
    "HF_HOME",
    "HF_HUB_CACHE",
    "HF_HUB_OFFLINE",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "NVIDIA_VISIBLE_DEVICES",
    "PATH",
    "PYTHONPATH",
    "PYTHONUNBUFFERED",
    "TOKENIZERS_PARALLELISM",
    "TRANSFORMERS_CACHE",
    "TRANSFORMERS_OFFLINE",
    "VLLM_LOGGING_LEVEL",
}


class RuntimeBackendError(RuntimeError):
    """Base class for fail-closed runtime errors."""


class BackendSelectionError(RuntimeBackendError):
    """The runtime backend was missing, unknown, or contradictory."""


class LeakageProtectionError(RuntimeBackendError):
    """A launch attempted to expose measured target data to the backend."""


class HealthCheckError(RuntimeBackendError):
    """The selected server did not satisfy the health contract."""


class RuntimeTimeoutError(HealthCheckError):
    """The selected server did not become healthy before the deadline."""


class ResumeError(RuntimeBackendError):
    """An existing runtime state cannot be safely resumed."""


class RuntimeStateError(RuntimeBackendError):
    """A runtime state transition or state file was invalid."""


def _canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def command_hash(command: Sequence[str]) -> str:
    """Hash an argv vector without introducing shell parsing ambiguity."""

    return sha256_bytes(b"\0".join(str(part).encode("utf-8") for part in command))


def select_backend(
    backend: str | None,
    *,
    manifest_path: str | Path | None = None,
) -> str:
    """Resolve an explicit backend selector and reject contradictions.

    ``BACKEND`` (or the legacy-compatible ``RUNTIME_BACKEND``) in the
    instance manifest counts as explicit because it is part of the reviewed
    manifest.  Ambient environment variables are handled only by the shell
    entry point's explicit ``BACKEND`` setting and are otherwise ignored.
    """

    manifest_values = read_instance_manifest(manifest_path)
    manifest_backend_values: list[str] = []
    if manifest_path is not None:
        manifest_file = Path(manifest_path)
        if manifest_file.is_file():
            for raw in manifest_file.read_text(encoding="utf-8").splitlines():
                line = raw.split("#", 1)[0].strip()
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key.strip() in {"BACKEND", "RUNTIME_BACKEND"}:
                    manifest_backend_values.append(value.strip().lower())
    if len(manifest_backend_values) > 1:
        raise BackendSelectionError("manifest contains duplicate or conflicting backend selectors")
    manifest_backend = (manifest_values.get("BACKEND") or manifest_values.get("RUNTIME_BACKEND") or "").strip().lower()
    selected = (backend or "").strip().lower()
    if selected and selected not in BACKEND_SELECTIONS:
        raise BackendSelectionError(f"unknown runtime backend: {backend!r}; expected auto, docker, or direct")
    if manifest_backend and manifest_backend not in BACKEND_SELECTIONS:
        raise BackendSelectionError(
            f"manifest backend is invalid: {manifest_backend!r}; expected auto, docker, or direct"
        )
    if manifest_backend == "auto":
        # A manifest's auto value is a default.  A concrete CLI selection is
        # allowed to override that default; two concrete selections must
        # still agree below.
        manifest_backend = ""
    if selected == "auto" and manifest_backend in BACKENDS:
        selected = manifest_backend
    if selected and manifest_backend and selected != manifest_backend:
        raise BackendSelectionError(
            f"runtime backend conflict: CLI selected {selected!r}, manifest selected {manifest_backend!r}"
        )
    selected = selected or manifest_backend
    if not selected:
        raise BackendSelectionError("runtime backend selection is required; choose --backend auto, docker, or direct")
    return selected


def docker_gpu_probe_command(config: Mapping[str, Any]) -> list[str]:
    """Build the non-vLLM capability probe used by ``auto`` selection."""

    return [
        "docker",
        "run",
        "--rm",
        "--pull=never",
        "--network",
        "none",
        "--runtime",
        "nvidia",
        "--gpus",
        "device=0",
        "--entrypoint",
        "python3",
        str(config["image"]),
        "-c",
        'import torch; assert torch.cuda.is_available(); assert torch.cuda.device_count() == 1; assert "H100" in torch.cuda.get_device_name(0)',
    ]


def _running_inside_container(environment: Mapping[str, str] | None = None) -> bool:
    """Detect contexts where this process must not attempt Docker-in-Docker."""

    source = os.environ if environment is None else environment
    if source.get("container") or source.get("CONTAINER"):
        return True
    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        return True
    try:
        cgroup = Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return bool(re.search(r"(?:docker|containerd|kubepods|libpod|lxc)", cgroup, re.IGNORECASE))


def docker_backend_available(
    config: Mapping[str, Any],
    *,
    runner: Callable[..., Any] = subprocess.run,
    executable_finder: Callable[[str], str | None] = shutil.which,
    probe_gpu: bool = True,
    environment: Mapping[str, str] | None = None,
) -> bool:
    """Return whether Docker, NVIDIA Container Toolkit, and GPU access work.

    A false result is intentionally non-exceptional for ``auto``: the caller
    may select direct mode.  The probe never starts vLLM or sends a request.
    An explicit Docker selection turns the same false result into a hard
    error in :func:`resolve_backend`.
    """

    if _running_inside_container(environment):
        # A container with a Docker CLI is not evidence that Docker-in-Docker
        # is supported.  Auto mode may choose direct; explicit Docker fails
        # closed in resolve_backend().
        return False
    if executable_finder("docker") is None:
        return False
    try:
        info = runner(["docker", "info"], capture_output=True, text=True, check=False, timeout=15)
        if getattr(info, "returncode", 1) != 0:
            return False
        runtimes = runner(
            ["docker", "info", "--format", "{{json .Runtimes}}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        runtime_output = getattr(runtimes, "stdout", "") or ""
        if getattr(runtimes, "returncode", 1) != 0 or not re.search(r"\bnvidia\b", runtime_output, re.I):
            return False
        image = runner(
            ["docker", "image", "inspect", str(config["image"])],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        if getattr(image, "returncode", 1) != 0:
            return False
        if not probe_gpu:
            return True
        probe = runner(docker_gpu_probe_command(config), capture_output=True, text=True, check=False, timeout=30)
        return getattr(probe, "returncode", 1) == 0
    except (OSError, subprocess.SubprocessError):
        return False


def resolve_backend(
    requested: str,
    config: Mapping[str, Any],
    *,
    probe: bool = True,
    availability: bool | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> str:
    """Resolve ``auto`` or enforce an explicit Docker/direct selection."""

    selected = requested.strip().lower()
    if selected not in BACKEND_SELECTIONS:
        raise BackendSelectionError(f"unknown runtime backend: {requested!r}; expected auto, docker, or direct")
    if selected == "direct":
        return "direct"
    if not probe:
        return "direct" if selected == "auto" else "docker"
    available = availability
    if available is None:
        available = docker_backend_available(config, runner=runner)
    if available:
        return "docker"
    if selected == "docker":
        raise RuntimeBackendError(
            "Docker backend was explicitly selected, but Docker/NVIDIA GPU access is unavailable "
            "or Docker-in-Docker/unprivileged-container use is unsupported; refusing direct fallback"
        )
    return "direct"


def artifact_root_for_backend(
    base_root: str | Path,
    backend: str,
    *,
    protected_roots: Sequence[str | Path] = (),
) -> Path:
    """Return a backend-specific root below a caller-owned common root.

    The common root is never itself used for artifacts.  This keeps Docker
    and direct outputs disjoint even when the same command-line base is used.
    Protected roots are checked before any directory is created so canonical
    H100 data cannot be accidentally nested below a runtime root.
    """

    if backend not in BACKENDS:
        raise BackendSelectionError(f"unknown runtime backend: {backend!r}")
    base = Path(base_root).expanduser().resolve()
    target = base / backend
    for protected in protected_roots:
        protected_path = Path(protected).expanduser().resolve()
        if target == protected_path or protected_path in target.parents or target in protected_path.parents:
            raise RuntimeBackendError(
                f"runtime artifact root overlaps protected canonical root: {target} vs {protected_path}"
            )
    return target


def _safe_name(value: str, label: str) -> str:
    if not _SAFE_NAME.fullmatch(value):
        raise RuntimeBackendError(f"{label} must be a simple non-empty name")
    return value


def _trace_launch_prefix(
    *,
    trace_binary: str | Path | None = None,
    trace_session: str | None = None,
) -> list[str]:
    """Build the shared low-overhead interactive Nsight launch prefix."""

    if trace_binary is None and trace_session is None:
        return []
    if trace_binary is None or trace_session is None:
        raise RuntimeBackendError("trace_binary and trace_session must be supplied together")
    binary = str(trace_binary)
    if not binary or any(char in binary for char in ("\n", "\r")):
        raise RuntimeBackendError("trace binary is invalid")
    _safe_name(str(trace_session), "trace session")
    return [
        binary,
        "launch",
        f"--session-new={trace_session}",
        "--trace=cuda,osrt",
        "--cuda-event-trace=false",
        "--",
    ]


def _server_arguments(config: Mapping[str, Any], *, model_path: str | Path | None = None) -> list[str]:
    """Build the one shared vLLM server argument vector for both backends."""

    gpu_memory_utilization = f"{float(config['gpu_memory_utilization']):.2f}"
    return [
        "--model",
        str(model_path or config["model"]),
        "--revision",
        str(config["model_revision"]),
        "--served-model-name",
        str(config["model"]),
        "--host",
        "127.0.0.1",
        "--port",
        str(config["port"]),
        "--dtype",
        "bfloat16",
        "--max-model-len",
        str(config["max_model_len"]),
        "--gpu-memory-utilization",
        gpu_memory_utilization,
        "--tensor-parallel-size",
        str(config["tensor_parallel_size"]),
        "--enable-auto-tool-choice",
        "--tool-call-parser",
        str(config["parser"]),
    ]


def build_docker_command(
    config: Mapping[str, Any],
    *,
    container_name: str = DEFAULT_CONTAINER_NAME,
    model_cache: str | Path | None = None,
    model_path: str | Path | None = None,
    model_cache_read_only: bool = False,
    detach: bool = False,
    remove: bool = True,
    trace_binary: str | Path | None = None,
    trace_session: str | None = None,
    trace_root: str | Path | None = None,
) -> list[str]:
    """Build the pinned vLLM command for the NVIDIA Docker runtime."""

    _safe_name(container_name, "container name")
    command = ["docker", "run"]
    if detach:
        command.append("-d")
    if remove:
        command.append("--rm")
    command.extend(
        [
            "--name",
            container_name,
            "--runtime",
            "nvidia",
            "--gpus",
            "device=0",
            "--network",
            "host",
            "--ipc=host",
            "--shm-size=16g",
            "--pull=never",
            "-e",
            "HF_HOME=/root/.cache/huggingface",
            "-e",
            "HF_HUB_OFFLINE=1",
            "-e",
            "TRANSFORMERS_OFFLINE=1",
        ]
    )
    tracing = _trace_launch_prefix(trace_binary=trace_binary, trace_session=trace_session)
    if tracing and trace_root is None:
        raise RuntimeBackendError("Docker tracing requires a host trace root")
    if trace_root is not None and not tracing:
        raise RuntimeBackendError("Docker trace root requires trace_binary and trace_session")
    if model_cache is not None:
        cache = Path(model_cache).expanduser().resolve()
        suffix = ":ro" if model_cache_read_only else ""
        command.extend(["-v", f"{cache}:/root/.cache/huggingface{suffix}"])
    if tracing:
        trace_path = Path(trace_root).expanduser().resolve()
        command.extend(
            [
                "-v",
                "/usr/local/cuda:/host-cuda:ro",
                "-v",
                f"{trace_path}:/trace",
                "--entrypoint",
                "/host-cuda/bin/nsys",
            ]
        )
        command.extend([str(config["image"]), *tracing[1:]])
        command.extend(["python3", "-m", "vllm.entrypoints.openai.api_server"])
    else:
        command.extend([str(config["image"])])
    command.extend(_server_arguments(config, model_path=model_path))
    _assert_no_measured_inputs(command)
    return command


def build_direct_command(
    config: Mapping[str, Any],
    *,
    python_executable: str | Path | None = None,
    model_path: str | Path | None = None,
    phase: str = "calibration",
    prediction_frozen: bool = False,
    target_paths: Sequence[str | Path] = (),
    trace_binary: str | Path | None = None,
    trace_session: str | None = None,
) -> list[str]:
    """Build the pinned vLLM process command for a prepared VM/container.

    Direct mode receives no artifact or target paths.  A holdout launch also
    requires the prediction freeze receipt, so it cannot be used to make a
    measured target available before prediction sealing.
    """

    enforce_prediction_boundary(
        backend="direct",
        phase=phase,
        prediction_frozen=prediction_frozen,
        target_paths=target_paths,
    )
    executable = str(python_executable or sys.executable)
    if not executable or any(char in executable for char in ("\n", "\r")):
        raise RuntimeBackendError("direct runtime Python executable is invalid")
    command = [executable, "-m", "vllm.entrypoints.openai.api_server"]
    command.extend(_server_arguments(config, model_path=model_path))
    prefix = _trace_launch_prefix(trace_binary=trace_binary, trace_session=trace_session)
    if prefix:
        command = [*prefix, *command]
    _assert_no_measured_inputs(command)
    return command


def build_command(
    backend: str,
    config: Mapping[str, Any],
    *,
    container_name: str = DEFAULT_CONTAINER_NAME,
    model_cache: str | Path | None = None,
    model_path: str | Path | None = None,
    model_cache_read_only: bool = False,
    detach: bool = False,
    remove: bool = True,
    python_executable: str | Path | None = None,
    phase: str = "calibration",
    prediction_frozen: bool = False,
    target_paths: Sequence[str | Path] = (),
    trace_binary: str | Path | None = None,
    trace_session: str | None = None,
    trace_root: str | Path | None = None,
) -> list[str]:
    """Dispatch to exactly one backend command builder; never fall back."""

    if backend == "docker":
        enforce_prediction_boundary(
            backend=backend,
            phase=phase,
            prediction_frozen=prediction_frozen,
            target_paths=target_paths,
        )
        return build_docker_command(
            config,
            container_name=container_name,
            model_cache=model_cache,
            model_path=model_path,
            model_cache_read_only=model_cache_read_only,
            detach=detach,
            remove=remove,
            trace_binary=trace_binary,
            trace_session=trace_session,
            trace_root=trace_root,
        )
    if backend == "direct":
        return build_direct_command(
            config,
            python_executable=python_executable,
            model_path=model_path,
            phase=phase,
            prediction_frozen=prediction_frozen,
            target_paths=target_paths,
            trace_binary=trace_binary,
            trace_session=trace_session,
        )
    raise BackendSelectionError(f"unknown runtime backend: {backend!r}")


def _assert_no_measured_inputs(values: Sequence[str]) -> None:
    lowered = " ".join(str(value).lower() for value in values)
    for forbidden in _FORBIDDEN_MEASURED_INPUTS:
        if forbidden in lowered:
            raise LeakageProtectionError(f"runtime command contains forbidden measured input: {forbidden}")


def enforce_prediction_boundary(
    *,
    backend: str,
    phase: str,
    prediction_frozen: bool,
    target_paths: Sequence[str | Path] = (),
) -> None:
    """Enforce the pre-execution/holdout boundary at the runtime edge."""

    if backend not in BACKENDS:
        raise BackendSelectionError(f"unknown runtime backend: {backend!r}")
    normalized_phase = phase.strip().lower()
    if normalized_phase not in {"calibration", "holdout", "sealed_holdout"}:
        raise LeakageProtectionError(f"unknown runtime phase: {phase!r}")
    if target_paths:
        raise LeakageProtectionError("runtime backends cannot receive measured target or artifact paths")
    if normalized_phase in {"holdout", "sealed_holdout"} and not prediction_frozen:
        raise LeakageProtectionError("holdout runtime requires frozen predictions before launch")


def build_process_environment(
    *,
    base_environment: Mapping[str, str] | None = None,
    model_cache: str | Path | None = None,
) -> dict[str, str]:
    """Return an allowlisted runtime environment without target/artifact paths."""

    source = os.environ if base_environment is None else base_environment
    environment = {key: str(source[key]) for key in _PROCESS_ENV_ALLOWLIST if key in source}
    environment.setdefault("CUDA_VISIBLE_DEVICES", "0")
    environment["HF_HUB_OFFLINE"] = "1"
    environment["TRANSFORMERS_OFFLINE"] = "1"
    if model_cache is not None:
        environment["HF_HOME"] = str(Path(model_cache).expanduser().resolve())
    _assert_no_measured_inputs(list(environment.keys()) + list(environment.values()))
    return environment


def _installed_vllm_version() -> str | None:
    try:
        return importlib.metadata.version("vllm")
    except importlib.metadata.PackageNotFoundError:
        return None


def environment_provenance(
    backend: str,
    config: Mapping[str, Any],
    *,
    python_executable: str | Path | None = None,
    command: Sequence[str] = (),
    observed: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return complete, secret-free environment provenance for run metadata."""

    if backend not in BACKENDS:
        raise BackendSelectionError(f"unknown runtime backend: {backend!r}")
    observed = dict(observed or {})
    python_path = str(python_executable or sys.executable)
    return {
        "schema_version": "runtime-environment.v1",
        "backend": backend,
        "backend_contract": BACKEND_SCHEMA_VERSION,
        "host": {
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "kernel": platform.release(),
            "machine": platform.machine(),
            "python_implementation": platform.python_implementation(),
        },
        "python": {
            "executable": python_path,
            "version": platform.python_version(),
        },
        "vllm": {
            "expected_version": str(config["version"]),
            "installed_version": observed.get("vllm_version", _installed_vllm_version()),
            "model": str(config["model"]),
            "model_revision": str(config["model_revision"]),
            "tokenizer_revision": str(config["model_revision"]),
            "tokenizer_source": "pinned_model_snapshot",
            "model_cache": observed.get("model_cache"),
            "model_path": observed.get("model_path"),
            "tool_parser": str(config["parser"]),
        },
        "docker": {
            "required": backend == "docker",
            "executable": shutil.which("docker") if backend == "docker" else None,
            "runtime": "nvidia" if backend == "docker" else None,
            "image": str(config["image"]) if backend == "docker" else None,
            "image_digest": str(config["image_digest"]) if backend == "docker" else None,
            "image_platform": str(config["image_platform"]) if backend == "docker" else None,
        },
        "gpu": {
            "visible_devices": observed.get("visible_devices", os.environ.get("CUDA_VISIBLE_DEVICES", "0")),
            "name": observed.get("gpu_name", os.environ.get("GPU_NAME")),
            "driver": observed.get("driver", os.environ.get("NVIDIA_DRIVER_VERSION")),
            "cuda": observed.get("cuda", os.environ.get("CUDA_VERSION")),
        },
        "tracing": {
            "provider": observed.get("trace_provider", os.environ.get("H100_TRACE_PROVIDER")),
            "binary": observed.get("trace_binary", os.environ.get("H100_NSYS_BIN")),
            "session": observed.get("trace_session", os.environ.get("H100_NSYS_SESSION")),
            "host_trace_root": observed.get("trace_root", os.environ.get("H100_TRACE_MOUNT_ROOT")),
            "request_scoped": True,
        },
        "command_sha256": command_hash(command) if command else None,
        "observed": observed,
    }


def build_run_metadata(
    *,
    backend: str,
    config: Mapping[str, Any],
    command: Sequence[str],
    artifact_root: str | Path,
    protocol_sha256: str | None = None,
    split_manifest_sha256: str | None = None,
    prediction_manifest_sha256: str | None = None,
    prediction_frozen: bool = False,
    phase: str = "calibration",
    python_executable: str | Path | None = None,
    observed_environment: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build metadata that binds runtime identity without exposing labels."""

    enforce_prediction_boundary(
        backend=backend,
        phase=phase,
        prediction_frozen=prediction_frozen,
    )
    metadata = {
        "schema_version": METADATA_SCHEMA_VERSION,
        "backend": backend,
        "backend_contract": BACKEND_SCHEMA_VERSION,
        "artifact_root": str(Path(artifact_root).resolve()),
        "protocol_sha256": protocol_sha256,
        "split_manifest_sha256": split_manifest_sha256,
        "prediction_manifest_sha256": prediction_manifest_sha256,
        "prediction_frozen": bool(prediction_frozen),
        "measured_target_access": False,
        "phase": phase,
        "command": [str(part) for part in command],
        "command_sha256": command_hash(command),
        "vllm": {
            "model": str(config["model"]),
            "model_revision": str(config["model_revision"]),
            "tokenizer_revision": str(config["model_revision"]),
            "tokenizer_source": "pinned_model_snapshot",
            "image": str(config["image"]),
            "image_digest": str(config["image_digest"]),
            "version": str(config["version"]),
            "parser": str(config["parser"]),
            "max_model_len": int(config["max_model_len"]),
            "health_context": int(config["health_context"]),
            "gpu_memory_utilization": float(config["gpu_memory_utilization"]),
            "tensor_parallel_size": int(config["tensor_parallel_size"]),
        },
        "environment": environment_provenance(
            backend,
            config,
            python_executable=python_executable,
            command=command,
            observed=observed_environment,
        ),
    }
    _assert_no_measured_inputs(
        list(metadata["command"])
        + list(metadata["environment"].get("observed", {}).keys())
        + [str(value) for value in metadata["environment"].get("observed", {}).values()]
    )
    return metadata


def deterministic_manifest(metadata: Mapping[str, Any]) -> bytes:
    """Serialize a metadata manifest deterministically for hashing/comparison."""

    return _canonical_json(metadata)


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(_canonical_json(value))
    temporary.replace(path)


def _health_request(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise HealthCheckError(f"health endpoint returned HTTP {response.status}: {url}")
        return response.read()


def default_health_probe(config: Mapping[str, Any], *, base_url: str = "http://127.0.0.1:8000", timeout: float = 5.0) -> dict[str, Any]:
    """Check health, served model identity, and metrics without sending a workload."""

    base = base_url.rstrip("/")
    _health_request(f"{base}/health", timeout)
    models = json.loads(_health_request(f"{base}/v1/models", timeout).decode("utf-8"))
    served = [str(item.get("id")) for item in models.get("data", []) if isinstance(item, Mapping)]
    if str(config["model"]) not in served:
        raise HealthCheckError(f"served model identity mismatch: expected {config['model']!r}, got {served!r}")
    _health_request(f"{base}/metrics", timeout)
    return {"health": True, "served_models": served, "base_url": base}


@dataclass
class RuntimeSession:
    backend: str
    command: list[str]
    artifact_root: Path
    process: Any = None
    identifier: str | None = None
    pid: int | None = None


@dataclass
class RuntimeLifecycle:
    """Start, health-check, stop, and resume one selected runtime."""

    backend: str
    config: Mapping[str, Any]
    command: list[str]
    environment: Mapping[str, str]
    artifact_root: Path
    metadata: Mapping[str, Any]
    protocol_sha256: str | None = None
    split_manifest_sha256: str | None = None
    prediction_manifest_sha256: str | None = None
    process_factory: Callable[..., Any] = subprocess.Popen
    command_runner: Callable[..., Any] = subprocess.run
    health_probe: Callable[..., Mapping[str, Any]] = default_health_probe
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    session: RuntimeSession | None = field(default=None, init=False)
    _active_phase: str | None = field(default=None, init=False)
    _active_prediction_frozen: bool | None = field(default=None, init=False)

    @property
    def state_path(self) -> Path:
        return self.artifact_root / "run_state.json"

    @property
    def metadata_path(self) -> Path:
        return self.artifact_root / "run_metadata.json"

    def _identity(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "backend_contract": BACKEND_SCHEMA_VERSION,
            "artifact_root": str(self.artifact_root.resolve()),
            "protocol_sha256": self.protocol_sha256,
            "split_manifest_sha256": self.split_manifest_sha256,
            "prediction_manifest_sha256": self.prediction_manifest_sha256,
            "command_sha256": command_hash(self.command),
        }

    def _state(self, status: str, **extra: Any) -> dict[str, Any]:
        if status not in {"planned", "starting", "healthy", "stopping", "stopped", "failed", "timed_out"}:
            raise RuntimeStateError(f"unknown runtime state: {status!r}")
        value = {
            "schema_version": STATE_SCHEMA_VERSION,
            **self._identity(),
            "status": status,
            **extra,
        }
        if self._active_phase is not None:
            value["phase"] = self._active_phase
            value["prediction_frozen"] = bool(self._active_prediction_frozen)
        return value

    def _read_state(self) -> dict[str, Any] | None:
        if not self.state_path.exists():
            return None
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ResumeError(f"runtime state is unreadable: {self.state_path}") from exc
        if not isinstance(value, dict) or value.get("schema_version") != STATE_SCHEMA_VERSION:
            raise ResumeError(f"runtime state schema is invalid: {self.state_path}")
        return value

    def _write_state(self, status: str, **extra: Any) -> None:
        _write_json_atomic(self.state_path, self._state(status, **extra))

    def _validate_resume(
        self,
        existing: Mapping[str, Any],
        *,
        allow_healthy: bool = False,
        phase: str | None = None,
        prediction_frozen: bool | None = None,
    ) -> None:
        expected = self._identity()
        mismatches = {
            key: {"expected": value, "actual": existing.get(key)}
            for key, value in expected.items()
            if existing.get(key) != value
        }
        if mismatches:
            raise ResumeError("runtime identity changed; refusing resume: " + json.dumps(mismatches, sort_keys=True))
        context_mismatches = {}
        if phase is not None and existing.get("phase") != phase:
            context_mismatches["phase"] = {"expected": phase, "actual": existing.get("phase")}
        if prediction_frozen is not None and existing.get("prediction_frozen") != bool(prediction_frozen):
            context_mismatches["prediction_frozen"] = {
                "expected": bool(prediction_frozen),
                "actual": existing.get("prediction_frozen"),
            }
        if context_mismatches:
            raise ResumeError("runtime launch context changed; refusing resume: " + json.dumps(context_mismatches, sort_keys=True))
        if existing.get("status") == "healthy" and not allow_healthy:
            pid = existing.get("pid")
            if isinstance(pid, int) and pid > 0 and Path(f"/proc/{pid}").exists():
                raise ResumeError("runtime is already healthy and has a live process; refusing duplicate start")

    def start(
        self,
        *,
        resume: bool = False,
        phase: str = "calibration",
        prediction_frozen: bool = False,
    ) -> RuntimeSession:
        self._active_phase = phase
        self._active_prediction_frozen = bool(prediction_frozen)
        enforce_prediction_boundary(
            backend=self.backend,
            phase=phase,
            prediction_frozen=prediction_frozen,
        )
        existing = self._read_state()
        if existing is not None:
            if not resume:
                raise ResumeError(f"runtime root already has state; pass --resume after inspection: {self.state_path}")
            self._validate_resume(existing, phase=phase, prediction_frozen=bool(prediction_frozen))
        else:
            self.artifact_root.mkdir(parents=True, exist_ok=True)
            _write_json_atomic(self.metadata_path, dict(self.metadata))
            self._write_state("planned", phase=phase, prediction_frozen=bool(prediction_frozen))

        self._write_state("starting", phase=phase, prediction_frozen=bool(prediction_frozen))
        try:
            process = self.process_factory(
                self.command,
                env=dict(self.environment),
                start_new_session=self.backend == "direct",
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            pid = int(getattr(process, "pid", 0) or 0) or None
            identifier = self.command[self.command.index("--name") + 1] if self.backend == "docker" and "--name" in self.command else None
            self.session = RuntimeSession(
                backend=self.backend,
                command=list(self.command),
                artifact_root=self.artifact_root,
                process=process,
                identifier=identifier,
                pid=pid,
            )
            self._write_state("starting", phase=phase, prediction_frozen=bool(prediction_frozen), pid=pid, identifier=identifier)
            return self.session
        except Exception as exc:
            self._write_state("failed", phase=phase, error=type(exc).__name__, error_message=str(exc))
            raise RuntimeBackendError(f"{self.backend} runtime failed to start") from exc

    def check_health(self, *, timeout: float = 5.0) -> Mapping[str, Any]:
        try:
            result = self.health_probe(self.config, timeout=timeout)
        except Exception as exc:
            raise HealthCheckError(f"{self.backend} runtime health check failed") from exc
        if not result or result.get("health") is not True:
            raise HealthCheckError(f"{self.backend} runtime health check returned an invalid result")
        return result

    def wait_until_healthy(
        self,
        *,
        timeout: float = DEFAULT_HEALTH_TIMEOUT_SECONDS,
        poll_interval: float = 1.0,
    ) -> Mapping[str, Any]:
        if self.session is None:
            raise RuntimeStateError("runtime must be started before health checking")
        deadline = self.monotonic() + timeout
        last_error: Exception | None = None
        while self.monotonic() < deadline:
            process = self.session.process
            if self.backend == "direct" and process is not None and getattr(process, "poll", lambda: None)() is not None:
                error = HealthCheckError(f"{self.backend} runtime exited before health checks passed")
                self._fail_and_cleanup(error)
                raise error
            try:
                result = self.check_health(timeout=min(5.0, max(0.1, deadline - self.monotonic())))
                self._write_state("healthy", health=result, pid=self.session.pid, identifier=self.session.identifier)
                return result
            except HealthCheckError as exc:
                last_error = exc
                self.sleep(min(poll_interval, max(0.0, deadline - self.monotonic())))
        error = RuntimeTimeoutError(f"{self.backend} runtime health timeout after {timeout:.1f}s")
        if last_error is not None:
            error.__cause__ = last_error
        self._write_state("timed_out", error=type(error).__name__, error_message=str(error))
        self._stop_process(force=False)
        self._write_state("timed_out", error=type(error).__name__, error_message=str(error), cleanup="forced")
        raise error

    def _fail_and_cleanup(self, error: Exception) -> None:
        self._write_state("failed", error=type(error).__name__, error_message=str(error))
        self._stop_process(force=False)
        self._write_state("failed", error=type(error).__name__, error_message=str(error), cleanup="forced")

    def _docker_control(self, command: Sequence[str], *, timeout: float) -> Any:
        try:
            return self.command_runner(
                list(command),
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError):
            return None

    def _stop_process(self, *, force: bool = False, grace_timeout: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS) -> None:
        if self.session is None:
            return
        process = self.session.process
        if self.backend == "docker" and self.session.identifier:
            stop_result = self._docker_control(
                ["docker", "stop", "--time", str(max(1, int(grace_timeout))), self.session.identifier],
                timeout=grace_timeout,
            )
            if force or stop_result is None or getattr(stop_result, "returncode", 0) != 0:
                self._docker_control(
                    ["docker", "kill", self.session.identifier],
                    timeout=grace_timeout,
                )
            if process is not None:
                try:
                    process.wait(timeout=grace_timeout)
                except (subprocess.TimeoutExpired, TimeoutError, OSError):
                    self._docker_control(["docker", "kill", self.session.identifier], timeout=grace_timeout)
                    try:
                        process.kill()
                    except (OSError, ProcessLookupError):
                        pass
                    try:
                        process.wait(timeout=grace_timeout)
                    except (subprocess.TimeoutExpired, TimeoutError, OSError):
                        pass
        elif process is not None:
            if not force:
                try:
                    os.killpg(self.session.pid or int(process.pid), signal.SIGTERM)
                except (OSError, ProcessLookupError):
                    pass
                try:
                    process.wait(timeout=grace_timeout)
                except (subprocess.TimeoutExpired, TimeoutError):
                    force = True
            if force:
                try:
                    os.killpg(self.session.pid or int(process.pid), signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
                try:
                    process.kill()
                except (OSError, ProcessLookupError):
                    pass
                try:
                    process.wait(timeout=grace_timeout)
                except (subprocess.TimeoutExpired, TimeoutError, OSError):
                    pass

    def stop_recorded_runtime(self, *, grace_timeout: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS) -> None:
        """Stop a runtime recorded in this lifecycle's state file.

        This is used by the CLI after a process has been detached from its
        parent shell.  It validates the same identity binding as resume before
        sending a signal or a Docker control command.
        """

        existing = self._read_state()
        if existing is None:
            raise RuntimeStateError(f"no runtime state exists: {self.state_path}")
        recorded_phase = existing.get("phase")
        if isinstance(recorded_phase, str):
            self._active_phase = recorded_phase
            self._active_prediction_frozen = bool(existing.get("prediction_frozen", False))
        self._validate_resume(existing, allow_healthy=True)
        pid = existing.get("pid")
        identifier = existing.get("identifier")
        self._write_state("stopping", pid=pid, identifier=identifier)
        try:
            if self.backend == "docker":
                if not isinstance(identifier, str) or not identifier:
                    raise RuntimeStateError("Docker runtime state has no container identifier")
                result = self._docker_control(
                    ["docker", "stop", "--time", str(max(1, int(grace_timeout))), identifier],
                    timeout=grace_timeout,
                )
                if result is None or getattr(result, "returncode", 0) != 0:
                    self._docker_control(["docker", "kill", identifier], timeout=grace_timeout)
            else:
                if not isinstance(pid, int) or pid <= 0:
                    raise RuntimeStateError("direct runtime state has no valid process-group PID")
                try:
                    os.killpg(pid, signal.SIGTERM)
                except (OSError, ProcessLookupError):
                    pass
                deadline = time.monotonic() + grace_timeout
                while Path(f"/proc/{pid}").exists() and time.monotonic() < deadline:
                    time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
                if Path(f"/proc/{pid}").exists():
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except (OSError, ProcessLookupError):
                        pass
        finally:
            self._write_state("stopped", pid=pid, identifier=identifier)

    def stop(self, *, grace_timeout: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS) -> None:
        if self.session is None:
            raise RuntimeStateError("runtime was not started by this lifecycle")
        self._write_state("stopping", pid=self.session.pid, identifier=self.session.identifier)
        try:
            self._stop_process(force=False, grace_timeout=grace_timeout)
        finally:
            self._write_state("stopped", pid=self.session.pid, identifier=self.session.identifier)


def check_backend_prerequisites(
    backend: str,
    config: Mapping[str, Any],
    *,
    python_executable: str | Path | None = None,
    model_cache: str | Path | None = None,
    model_path: str | Path | None = None,
    trace_binary: str | Path | None = None,
    check_external: bool = True,
) -> None:
    """Check only the selected backend; never probe the other backend."""

    if backend not in BACKENDS:
        raise BackendSelectionError(f"unknown runtime backend: {backend!r}")
    if model_cache is not None:
        cache_path = Path(model_cache).expanduser()
        if not cache_path.is_dir():
            raise RuntimeBackendError(f"pinned model cache is unavailable: {cache_path}")
    if model_path is not None and backend == "direct":
        snapshot_path = Path(model_path).expanduser()
        if not snapshot_path.is_dir():
            raise RuntimeBackendError(f"pinned direct model snapshot is unavailable: {snapshot_path}")
    if trace_binary is not None and backend == "direct":
        trace_path = Path(trace_binary).expanduser()
        if not trace_path.is_file() or not os.access(trace_path, os.X_OK):
            raise RuntimeBackendError(f"direct tracing binary is unavailable: {trace_path}")
    if backend == "docker":
        if _running_inside_container():
            raise RuntimeBackendError(
                "Docker backend is unsupported from an unprivileged container; Docker-in-Docker is not attempted"
            )
        if not shutil.which("docker"):
            raise RuntimeBackendError("Docker backend selected but docker is unavailable")
        if check_external:
            result = subprocess.run(["docker", "info"], capture_output=True, check=False, timeout=15)
            if result.returncode != 0:
                raise RuntimeBackendError("Docker backend selected but the Docker daemon is unavailable")
            result = subprocess.run(
                ["docker", "image", "inspect", str(config["image"])],
                capture_output=True,
                check=False,
                timeout=15,
            )
            if result.returncode != 0:
                raise RuntimeBackendError("pinned vLLM Docker image is unavailable locally")
        return
    executable = str(python_executable or sys.executable)
    executable_path = Path(executable) if "/" in executable else Path(shutil.which(executable) or "")
    if not executable_path.is_file() or not os.access(executable_path, os.X_OK):
        raise RuntimeBackendError(f"direct backend Python executable is unavailable: {executable}")
    if check_external:
        probe = subprocess.run(
            [executable, "-c", "import importlib.metadata; print(importlib.metadata.version('vllm'))"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        if probe.returncode != 0 or probe.stdout.strip() != str(config["version"]):
            raise RuntimeBackendError(
                f"direct backend requires installed vLLM {config['version']}; got {probe.stdout.strip() or 'unavailable'}"
            )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=BACKEND_SELECTIONS)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--protected-root", action="append", default=[])
    parser.add_argument("--phase", choices=("calibration", "holdout", "sealed_holdout"), default="calibration")
    parser.add_argument("--prediction-frozen", action="store_true")
    parser.add_argument("--protocol-sha256")
    parser.add_argument("--split-manifest-sha256")
    parser.add_argument("--prediction-manifest-sha256")
    parser.add_argument("--model-cache", type=Path)
    parser.add_argument("--model-path")
    parser.add_argument("--python", dest="python_executable", type=Path)
    parser.add_argument("--trace-binary")
    parser.add_argument("--trace-session")
    parser.add_argument("--trace-root", type=Path)
    parser.add_argument("--container-name", default=DEFAULT_CONTAINER_NAME)
    parser.add_argument("--health-timeout", type=float, default=DEFAULT_HEALTH_TIMEOUT_SECONDS)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _recorded_stop_inputs(
    root: Path,
    backend: str,
    *,
    protocol_sha256: str | None,
    split_manifest_sha256: str | None,
    prediction_manifest_sha256: str | None,
) -> tuple[list[str], dict[str, Any], str | None, str | None, str | None]:
    """Load immutable command/bindings so cleanup does not need launch args."""

    state_path = root / "run_state.json"
    metadata_path = root / "run_metadata.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeStateError(f"recorded runtime metadata is unreadable: {root}") from exc
    if not isinstance(state, dict) or state.get("backend") != backend:
        raise RuntimeStateError("recorded runtime backend does not match the selected backend")
    if state.get("artifact_root") != str(root.resolve()):
        raise RuntimeStateError("recorded runtime artifact root does not match the selected root")
    command = metadata.get("command") if isinstance(metadata, dict) else None
    if not isinstance(command, list) or not command or any(not isinstance(item, str) for item in command):
        raise RuntimeStateError("recorded runtime command is missing")
    if state.get("command_sha256") != command_hash(command):
        raise RuntimeStateError("recorded runtime command hash is invalid")
    recorded_values = {
        "protocol_sha256": state.get("protocol_sha256"),
        "split_manifest_sha256": state.get("split_manifest_sha256"),
        "prediction_manifest_sha256": state.get("prediction_manifest_sha256"),
    }
    supplied = {
        "protocol_sha256": protocol_sha256,
        "split_manifest_sha256": split_manifest_sha256,
        "prediction_manifest_sha256": prediction_manifest_sha256,
    }
    for key, value in supplied.items():
        if value is not None and value != recorded_values[key]:
            raise ResumeError(f"recorded runtime {key} differs from the supplied binding")
    return (
        command,
        metadata,
        recorded_values["protocol_sha256"],
        recorded_values["split_manifest_sha256"],
        recorded_values["prediction_manifest_sha256"],
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        requested_backend = select_backend(args.backend, manifest_path=args.manifest)
        config = resolve_vllm_config(args.manifest)
        # Cleanup must remain available when a GPU/container has already
        # failed.  Start-time auto selection probes GPU access; stop-time
        # cleanup uses the recorded state instead of guessing from current
        # Docker availability.
        if args.stop and requested_backend == "auto":
            candidates = []
            for candidate in BACKENDS:
                candidate_root = artifact_root_for_backend(
                    args.artifact_root,
                    candidate,
                    protected_roots=args.protected_root,
                )
                state_path = candidate_root / "run_state.json"
                if state_path.exists():
                    candidates.append((candidate, state_path))
            if len(candidates) != 1:
                raise RuntimeStateError(
                    "auto stop requires exactly one recorded backend state; "
                    "select --backend docker or --backend direct explicitly"
                )
            backend = candidates[0][0]
        else:
            backend = resolve_backend(requested_backend, config, probe=not args.dry_run and not args.stop)
        root = artifact_root_for_backend(args.artifact_root, backend, protected_roots=args.protected_root)
        if args.stop:
            (
                command,
                metadata,
                protocol_sha256,
                split_manifest_sha256,
                prediction_manifest_sha256,
            ) = _recorded_stop_inputs(
                root,
                backend,
                protocol_sha256=args.protocol_sha256,
                split_manifest_sha256=args.split_manifest_sha256,
                prediction_manifest_sha256=args.prediction_manifest_sha256,
            )
        else:
            tracing_requested = args.trace_binary is not None or args.trace_session is not None
            trace_root = (args.trace_root or (root / "traces")) if tracing_requested else None
            trace_binary = args.trace_binary
            if args.trace_session is not None and trace_binary is None:
                trace_binary = "/host-cuda/bin/nsys" if backend == "docker" else "/usr/local/cuda/bin/nsys"
            command = build_command(
                backend,
                config,
                container_name=args.container_name,
                model_cache=args.model_cache,
                model_path=args.model_path,
                python_executable=args.python_executable,
                phase=args.phase,
                prediction_frozen=args.prediction_frozen,
                trace_binary=trace_binary,
                trace_session=args.trace_session,
                trace_root=trace_root,
            )
            protocol_sha256 = args.protocol_sha256
            split_manifest_sha256 = args.split_manifest_sha256
            prediction_manifest_sha256 = args.prediction_manifest_sha256
            metadata = build_run_metadata(
                backend=backend,
                config=config,
                command=command,
                artifact_root=root,
                protocol_sha256=protocol_sha256,
                split_manifest_sha256=split_manifest_sha256,
                prediction_manifest_sha256=prediction_manifest_sha256,
                prediction_frozen=args.prediction_frozen,
                phase=args.phase,
                python_executable=args.python_executable,
                observed_environment={
                    key: value
                    for key, value in {
                        "trace_binary": trace_binary,
                        "trace_session": args.trace_session,
                        "trace_root": str(trace_root) if trace_root is not None else None,
                        "model_cache": str(args.model_cache) if args.model_cache is not None else None,
                        "model_path": args.model_path,
                    }.items()
                    if value is not None
                },
            )
        environment = build_process_environment(model_cache=args.model_cache)
        if args.dry_run:
            if requested_backend == "auto":
                print("DRY-RUN: requested backend=auto; Docker capability/GPU probe skipped; direct preview shown")
            else:
                print("DRY-RUN: requested backend=" + requested_backend)
            print("DRY-RUN: selected backend=" + backend)
            print("DRY-RUN: artifact_root=" + str(root))
            print("DRY-RUN: command=" + json.dumps(command, separators=(",", ":")))
            print("DRY-RUN: command_sha256=" + command_hash(command))
            print("DRY-RUN: no Docker daemon, vLLM process, GPU, health request, or artifact mutation")
            return 0
        lifecycle = RuntimeLifecycle(
            backend=backend,
            config=config,
            command=command,
            environment=environment,
            artifact_root=root,
            metadata=metadata,
            protocol_sha256=protocol_sha256,
            split_manifest_sha256=split_manifest_sha256,
            prediction_manifest_sha256=prediction_manifest_sha256,
        )
        if args.stop:
            if backend == "docker":
                check_backend_prerequisites(backend, config, check_external=False)
            lifecycle.stop_recorded_runtime()
            print(f"runtime stopped: backend={backend} artifact_root={root}")
            return 0
        check_backend_prerequisites(
            backend,
            config,
            python_executable=args.python_executable,
            model_cache=args.model_cache,
            model_path=args.model_path,
            trace_binary=trace_binary,
        )
        lifecycle.start(resume=args.resume, phase=args.phase, prediction_frozen=args.prediction_frozen)
        lifecycle.wait_until_healthy(timeout=args.health_timeout)
        print(f"runtime healthy: backend={backend} artifact_root={root}")
        return 0
    except (VLLMConfigError, RuntimeBackendError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


__all__ = [
    "BACKENDS",
    "BACKEND_SELECTIONS",
    "BACKEND_SCHEMA_VERSION",
    "BackendSelectionError",
    "HealthCheckError",
    "LeakageProtectionError",
    "ResumeError",
    "RuntimeBackendError",
    "RuntimeLifecycle",
    "RuntimeSession",
    "RuntimeStateError",
    "RuntimeTimeoutError",
    "artifact_root_for_backend",
    "build_command",
    "build_direct_command",
    "build_docker_command",
    "build_process_environment",
    "build_run_metadata",
    "check_backend_prerequisites",
    "command_hash",
    "default_health_probe",
    "deterministic_manifest",
    "docker_backend_available",
    "docker_gpu_probe_command",
    "enforce_prediction_boundary",
    "environment_provenance",
    "main",
    "resolve_backend",
    "select_backend",
    "sha256_bytes",
    "sha256_file",
]


if __name__ == "__main__":
    raise SystemExit(main())
