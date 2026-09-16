"""Hardware-aware event latency simulation for the coding assignment.

Step 3 requires logging and modeling CPU events (reads, writes, traversal, …)
and GPU events (input tokens, output tokens, context length). Step 4 then
uses those event models with configurable hardware parameters.  The
assignment-level simulator therefore consumes the Step-3 event descriptors
together with a hardware profile.  Measured wall, CPU, CUDA, and Kineto times
remain labels and are rejected as features.

A stricter sealed online-forecast protocol that forbids current-event
``output_tokens`` is preserved separately in ``sequential_simulator`` and
``scripts/assignment/adaptive_event_protocol.py``.  It is a research result,
not the assignment minimum.

Three dependency-free ridge models are selected deterministically using
leave-one-trajectory-out calibration error:

    * tool-event latency from operation class and declared work;
    * model-request latency from input, output, context, and hardware;
    * trajectory latency from predicted event totals and declared event counts.

Only records explicitly marked ``calibration`` can enter fitting.  Holdout
features are predicted and frozen, with a SHA-256 sidecar, before a separate
labels file can be evaluated.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from math import isfinite
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


class EventSimulatorError(ValueError):
    """The event-simulator contract was violated."""


HARDWARE_SCHEMA = "assignment.hardware-profile.v1"
TOOL_INPUT_SCHEMA = "assignment.tool-event-input.v1"
MODEL_INPUT_SCHEMA = "assignment.model-event-input.v1"
PREDICTION_MANIFEST_SCHEMA = "assignment.event-prediction-manifest.v1"

OPERATION_CLASSES = (
    "read",
    "write",
    "traversal",
    "search",
    "shell",
    "patch",
    "test",
    "other",
)
RIDGE_CANDIDATES = (1e-9, 1e-6, 1e-3, 1e-1, 1.0)

# Unknown keys are rejected too.  This explicit list makes the error useful
# when a caller tries to pass a target under a common alias.
TARGET_DERIVED_FIELDS = frozenset(
    {
        "observed_ms",
        "observed_seconds",
        "target_ms",
        "target_seconds",
        "latency_ms",
        "latency_seconds",
        "duration_ms",
        "duration_seconds",
        "wall_ms",
        "wall_seconds",
        "wall_time_ms",
        "wall_time_seconds",
        "elapsed_ms",
        "elapsed_seconds",
        "cpu_ms",
        "cpu_seconds",
        "cpu_time_ms",
        "cpu_time_seconds",
        "cuda_ms",
        "cuda_seconds",
        "cuda_time_ms",
        "cuda_time_seconds",
        "gpu_ms",
        "gpu_seconds",
        "kineto_ms",
        "kineto_wall_ms",
        "kineto_cpu_ms",
        "kineto_cuda_ms",
        "kernel_duration_sum_ms",
        "cpu_activity_union_ms",
        "cuda_activity_union_ms",
        "start_mono_ns",
        "end_mono_ns",
        "actual_output_tokens",
        "generated_tokens",
        "completion_tokens",
        "response_tokens",
        "response_bytes",
        "completion_length",
        "output_length",
        "current_output_tokens",
        "official_resolved",
        "resolved",
        "evaluator",
        "evaluator_result",
        "future_event",
        "next_event_wall_ms",
    }
)

TOOL_SEQUENTIAL_FIELDS = (
    "tool_name",
    "subcommand",
    "command_prefix",
    "command_sha256",
    "has_pipe",
    "has_glob",
    "extractor_id",
    "extractor_sha256",
)
MODEL_SEQUENTIAL_FIELDS = (
    "prior_event_count",
    "prior_median_output_tokens",
    "prior_median_observed_ms",
    "prior_label_sha256s",
)


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _finite_number(value: Any, name: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EventSimulatorError(f"{name} must be a finite number >= {minimum}")
    result = float(value)
    if not isfinite(result) or result < minimum:
        raise EventSimulatorError(f"{name} must be a finite number >= {minimum}")
    return result


def _positive_number(value: Any, name: str) -> float:
    result = _finite_number(value, name)
    if result <= 0:
        raise EventSimulatorError(f"{name} must be a positive finite number")
    return result


def _nonnegative_integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EventSimulatorError(f"{name} must be a non-negative integer")
    return value


def _positive_integer(value: Any, name: str) -> int:
    result = _nonnegative_integer(value, name)
    if result == 0:
        raise EventSimulatorError(f"{name} must be a positive integer")
    return result


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EventSimulatorError(f"{name} must be a non-empty string")
    return value


def _strict_keys(row: Mapping[str, Any], allowed: set[str], *, kind: str) -> None:
    if not isinstance(row, Mapping):
        raise EventSimulatorError(f"{kind} must be a mapping")
    if any(not isinstance(key, str) for key in row):
        raise EventSimulatorError(f"{kind} keys must be strings")
    leaked = sorted(set(row) & TARGET_DERIVED_FIELDS)
    if leaked:
        raise EventSimulatorError(
            f"{kind} rejects measured or target-derived fields: {', '.join(leaked)}"
        )
    unknown = sorted(set(row) - allowed)
    if unknown:
        raise EventSimulatorError(f"unknown {kind} field(s): {', '.join(unknown)}")


@dataclass(frozen=True)
class HardwareProfile:
    """Explicit, pluggable hardware parameters used by both event models."""

    hardware_id: str
    architecture: str
    cpu_cores: int
    cpu_threads: int
    cpu_base_ghz: float
    system_memory_gib: float
    storage_read_mbps: float
    storage_write_mbps: float
    gpu_count: int
    gpu_compute_capability: float
    gpu_memory_gib: float
    gpu_memory_bandwidth_gbps: float
    gpu_bf16_tflops: float

    def __post_init__(self) -> None:
        _text(self.hardware_id, "hardware_id")
        _text(self.architecture, "architecture")
        _positive_integer(self.cpu_cores, "cpu_cores")
        _positive_integer(self.cpu_threads, "cpu_threads")
        if self.cpu_threads < self.cpu_cores:
            raise EventSimulatorError("cpu_threads must be >= cpu_cores")
        for name in (
            "cpu_base_ghz",
            "system_memory_gib",
            "storage_read_mbps",
            "storage_write_mbps",
            "gpu_compute_capability",
            "gpu_memory_gib",
            "gpu_memory_bandwidth_gbps",
            "gpu_bf16_tflops",
        ):
            _positive_number(getattr(self, name), name)
        _positive_integer(self.gpu_count, "gpu_count")

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "HardwareProfile":
        allowed = {
            "schema_version",
            "hardware_id",
            "architecture",
            "cpu_cores",
            "cpu_threads",
            "cpu_base_ghz",
            "system_memory_gib",
            "storage_read_mbps",
            "storage_write_mbps",
            "gpu_count",
            "gpu_compute_capability",
            "gpu_memory_gib",
            "gpu_memory_bandwidth_gbps",
            "gpu_bf16_tflops",
        }
        _strict_keys(row, allowed, kind="hardware profile")
        if row.get("schema_version") != HARDWARE_SCHEMA:
            raise EventSimulatorError("unsupported hardware profile schema_version")
        missing = sorted(allowed - {"schema_version"} - set(row))
        if missing:
            raise EventSimulatorError("hardware profile is missing: " + ", ".join(missing))
        return cls(
            hardware_id=_text(row["hardware_id"], "hardware_id"),
            architecture=_text(row["architecture"], "architecture"),
            cpu_cores=_positive_integer(row["cpu_cores"], "cpu_cores"),
            cpu_threads=_positive_integer(row["cpu_threads"], "cpu_threads"),
            cpu_base_ghz=_positive_number(row["cpu_base_ghz"], "cpu_base_ghz"),
            system_memory_gib=_positive_number(row["system_memory_gib"], "system_memory_gib"),
            storage_read_mbps=_positive_number(row["storage_read_mbps"], "storage_read_mbps"),
            storage_write_mbps=_positive_number(row["storage_write_mbps"], "storage_write_mbps"),
            gpu_count=_positive_integer(row["gpu_count"], "gpu_count"),
            gpu_compute_capability=_positive_number(
                row["gpu_compute_capability"], "gpu_compute_capability"
            ),
            gpu_memory_gib=_positive_number(row["gpu_memory_gib"], "gpu_memory_gib"),
            gpu_memory_bandwidth_gbps=_positive_number(
                row["gpu_memory_bandwidth_gbps"], "gpu_memory_bandwidth_gbps"
            ),
            gpu_bf16_tflops=_positive_number(row["gpu_bf16_tflops"], "gpu_bf16_tflops"),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": HARDWARE_SCHEMA,
            "hardware_id": self.hardware_id,
            "architecture": self.architecture,
            "cpu_cores": self.cpu_cores,
            "cpu_threads": self.cpu_threads,
            "cpu_base_ghz": self.cpu_base_ghz,
            "system_memory_gib": self.system_memory_gib,
            "storage_read_mbps": self.storage_read_mbps,
            "storage_write_mbps": self.storage_write_mbps,
            "gpu_count": self.gpu_count,
            "gpu_compute_capability": self.gpu_compute_capability,
            "gpu_memory_gib": self.gpu_memory_gib,
            "gpu_memory_bandwidth_gbps": self.gpu_memory_bandwidth_gbps,
            "gpu_bf16_tflops": self.gpu_bf16_tflops,
        }

    @property
    def cpu_capacity(self) -> float:
        return self.cpu_threads * self.cpu_base_ghz

    @property
    def gpu_compute_capacity(self) -> float:
        return self.gpu_count * self.gpu_bf16_tflops

    @property
    def gpu_bandwidth_capacity(self) -> float:
        return self.gpu_count * self.gpu_memory_bandwidth_gbps


@dataclass(frozen=True)
class ToolEventInput:
    """CPU/tool workload descriptors plus the hardware profile."""

    event_id: str
    run_id: str
    split: str
    operation_class: str
    declared_command_bytes: int
    declared_read_bytes: int
    declared_write_bytes: int
    declared_path_count: int
    hardware: HardwareProfile
    tool_name: str = ""
    subcommand: str = ""
    command_prefix: str = ""
    command_sha256: str = ""
    has_pipe: int = 0
    has_glob: int = 0
    extractor_id: str = ""
    extractor_sha256: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.hardware, HardwareProfile):
            raise EventSimulatorError("hardware must be a HardwareProfile")
        _text(self.event_id, "event_id")
        _text(self.run_id, "run_id")
        if self.split not in {"calibration", "holdout"}:
            raise EventSimulatorError("tool-event split must be calibration or holdout")
        if self.operation_class not in OPERATION_CLASSES:
            raise EventSimulatorError("unsupported operation_class")
        for name in (
            "declared_command_bytes",
            "declared_read_bytes",
            "declared_write_bytes",
            "declared_path_count",
        ):
            _nonnegative_integer(getattr(self, name), name)

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "ToolEventInput":
        allowed = {
            "schema_version",
            "event_id",
            "run_id",
            "split",
            "operation_class",
            "declared_command_bytes",
            "declared_read_bytes",
            "declared_write_bytes",
            "declared_path_count",
            "hardware",
            *TOOL_SEQUENTIAL_FIELDS,
        }
        _strict_keys(row, allowed, kind="tool-event input")
        if row.get("schema_version") != TOOL_INPUT_SCHEMA:
            raise EventSimulatorError("unsupported tool-event input schema_version")
        required = allowed - {"schema_version"} - set(TOOL_SEQUENTIAL_FIELDS)
        missing = sorted(required - set(row))
        if missing:
            raise EventSimulatorError("tool-event input is missing: " + ", ".join(missing))
        hardware = row["hardware"]
        if not isinstance(hardware, Mapping):
            raise EventSimulatorError("hardware must be a mapping")
        return cls(
            event_id=_text(row["event_id"], "event_id"),
            run_id=_text(row["run_id"], "run_id"),
            split=_text(row["split"], "split"),
            operation_class=_text(row["operation_class"], "operation_class"),
            declared_command_bytes=_nonnegative_integer(
                row["declared_command_bytes"], "declared_command_bytes"
            ),
            declared_read_bytes=_nonnegative_integer(
                row["declared_read_bytes"], "declared_read_bytes"
            ),
            declared_write_bytes=_nonnegative_integer(
                row["declared_write_bytes"], "declared_write_bytes"
            ),
            declared_path_count=_nonnegative_integer(
                row["declared_path_count"], "declared_path_count"
            ),
            hardware=HardwareProfile.from_mapping(hardware),
            tool_name=str(row.get("tool_name") or ""),
            subcommand=str(row.get("subcommand") or ""),
            command_prefix=str(row.get("command_prefix") or ""),
            command_sha256=str(row.get("command_sha256") or ""),
            has_pipe=_nonnegative_integer(int(row.get("has_pipe") or 0), "has_pipe"),
            has_glob=_nonnegative_integer(int(row.get("has_glob") or 0), "has_glob"),
            extractor_id=str(row.get("extractor_id") or ""),
            extractor_sha256=str(row.get("extractor_sha256") or ""),
        )

    def design_row(self) -> tuple[float, ...]:
        cpu = self.hardware.cpu_capacity
        read_scale = self.hardware.storage_read_mbps / 1000.0
        write_scale = self.hardware.storage_write_mbps / 1000.0
        operation = tuple(
            1.0 if self.operation_class == candidate else 0.0
            for candidate in OPERATION_CLASSES
        )
        return (
            1.0,
            *operation,
            (self.declared_command_bytes / 1024.0) / cpu,
            (self.declared_read_bytes / 1_000_000.0) / read_scale,
            (self.declared_write_bytes / 1_000_000.0) / write_scale,
            self.declared_path_count / cpu,
            (self.declared_command_bytes / 1_000_000.0) / self.hardware.system_memory_gib,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": TOOL_INPUT_SCHEMA,
            "event_id": self.event_id,
            "run_id": self.run_id,
            "split": self.split,
            "operation_class": self.operation_class,
            "declared_command_bytes": self.declared_command_bytes,
            "declared_read_bytes": self.declared_read_bytes,
            "declared_write_bytes": self.declared_write_bytes,
            "declared_path_count": self.declared_path_count,
            "hardware": self.hardware.to_mapping(),
            "tool_name": self.tool_name,
            "subcommand": self.subcommand,
            "command_prefix": self.command_prefix,
            "command_sha256": self.command_sha256,
            "has_pipe": self.has_pipe,
            "has_glob": self.has_glob,
            "extractor_id": self.extractor_id,
            "extractor_sha256": self.extractor_sha256,
        }


@dataclass(frozen=True)
class ModelEventInput:
    """GPU/model workload descriptors plus the hardware profile.

    ``output_tokens`` is the Step-3 GPU-event descriptor used by the
    assignment-level simulator.  It is optional so the sealed online protocol
    can omit it; when omitted, ``max_output_tokens`` is the decode-length
    proxy.
    """

    request_id: str
    run_id: str
    split: str
    input_tokens: int
    context_tokens: int
    max_output_tokens: int
    hardware: HardwareProfile
    output_tokens: int | None = None
    prior_event_count: int = 0
    prior_median_output_tokens: float = 0.0
    prior_median_observed_ms: float = 0.0
    prior_label_sha256s: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.hardware, HardwareProfile):
            raise EventSimulatorError("hardware must be a HardwareProfile")
        _text(self.request_id, "request_id")
        _text(self.run_id, "run_id")
        if self.split not in {"calibration", "holdout"}:
            raise EventSimulatorError("model-event split must be calibration or holdout")
        for name in ("input_tokens", "context_tokens", "max_output_tokens"):
            _nonnegative_integer(getattr(self, name), name)
        if self.max_output_tokens == 0:
            raise EventSimulatorError("max_output_tokens must be positive")
        if self.output_tokens is not None:
            _nonnegative_integer(self.output_tokens, "output_tokens")

    @property
    def decode_tokens(self) -> int:
        """Logged output length when present; otherwise the request budget."""
        if self.output_tokens is not None:
            return self.output_tokens
        return self.max_output_tokens

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "ModelEventInput":
        allowed = {
            "schema_version",
            "request_id",
            "run_id",
            "split",
            "input_tokens",
            "output_tokens",
            "context_tokens",
            "max_output_tokens",
            "hardware",
            *MODEL_SEQUENTIAL_FIELDS,
        }
        _strict_keys(row, allowed, kind="model-event input")
        if row.get("schema_version") != MODEL_INPUT_SCHEMA:
            raise EventSimulatorError("unsupported model-event input schema_version")
        required = (
            allowed
            - {"schema_version", "output_tokens"}
            - set(MODEL_SEQUENTIAL_FIELDS)
        )
        missing = sorted(required - set(row))
        if missing:
            raise EventSimulatorError("model-event input is missing: " + ", ".join(missing))
        hardware = row["hardware"]
        if not isinstance(hardware, Mapping):
            raise EventSimulatorError("hardware must be a mapping")
        output_tokens = row.get("output_tokens")
        return cls(
            request_id=_text(row["request_id"], "request_id"),
            run_id=_text(row["run_id"], "run_id"),
            split=_text(row["split"], "split"),
            input_tokens=_nonnegative_integer(row["input_tokens"], "input_tokens"),
            context_tokens=_nonnegative_integer(row["context_tokens"], "context_tokens"),
            max_output_tokens=_positive_integer(
                row["max_output_tokens"], "max_output_tokens"
            ),
            hardware=HardwareProfile.from_mapping(hardware),
            output_tokens=(
                None
                if output_tokens is None
                else _nonnegative_integer(output_tokens, "output_tokens")
            ),
            prior_event_count=_nonnegative_integer(
                int(row.get("prior_event_count") or 0), "prior_event_count"
            ),
            prior_median_output_tokens=_finite_number(
                float(row.get("prior_median_output_tokens") or 0.0),
                "prior_median_output_tokens",
            ),
            prior_median_observed_ms=_finite_number(
                float(row.get("prior_median_observed_ms") or 0.0),
                "prior_median_observed_ms",
            ),
            prior_label_sha256s=tuple(
                str(item) for item in (row.get("prior_label_sha256s") or ())
            ),
        )

    def design_row(self) -> tuple[float, ...]:
        compute = self.hardware.gpu_compute_capacity / 100.0
        bandwidth = self.hardware.gpu_bandwidth_capacity / 1000.0
        input_k = self.input_tokens / 1000.0
        context_k = self.context_tokens / 1000.0
        output_k = self.decode_tokens / 1000.0
        return (
            1.0,
            input_k / compute,
            context_k / bandwidth,
            output_k / bandwidth,
            input_k * output_k / compute,
            context_k * output_k / bandwidth,
            1.0 / self.hardware.gpu_memory_gib,
            1.0 / self.hardware.gpu_compute_capability,
        )

    def to_mapping(self) -> dict[str, Any]:
        mapping = {
            "schema_version": MODEL_INPUT_SCHEMA,
            "request_id": self.request_id,
            "run_id": self.run_id,
            "split": self.split,
            "input_tokens": self.input_tokens,
            "context_tokens": self.context_tokens,
            "max_output_tokens": self.max_output_tokens,
            "hardware": self.hardware.to_mapping(),
            "prior_event_count": self.prior_event_count,
            "prior_median_output_tokens": self.prior_median_output_tokens,
            "prior_median_observed_ms": self.prior_median_observed_ms,
            "prior_label_sha256s": list(self.prior_label_sha256s),
        }
        if self.output_tokens is not None:
            mapping["output_tokens"] = self.output_tokens
        return mapping


@dataclass(frozen=True)
class ToolCalibrationRecord:
    features: ToolEventInput
    observed_ms: float

    def __post_init__(self) -> None:
        if not isinstance(self.features, ToolEventInput):
            raise EventSimulatorError("tool calibration features must be ToolEventInput")
        if self.features.split != "calibration":
            raise EventSimulatorError("tool calibration record must use split=calibration")
        _positive_number(self.observed_ms, "observed_ms")

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "ToolCalibrationRecord":
        allowed = {"schema_version", "split", "features", "observed_ms"}
        if not isinstance(row, Mapping):
            raise EventSimulatorError("tool calibration record must be a mapping")
        unknown = sorted(set(row) - allowed)
        if unknown:
            raise EventSimulatorError("unknown tool calibration field(s): " + ", ".join(unknown))
        if row.get("schema_version") != "assignment.tool-calibration.v1":
            raise EventSimulatorError("unsupported tool calibration schema_version")
        if row.get("split") != "calibration":
            raise EventSimulatorError("holdout/test tool labels may not enter fitting")
        if "features" not in row or "observed_ms" not in row:
            raise EventSimulatorError("tool calibration requires features and observed_ms")
        if not isinstance(row["features"], Mapping):
            raise EventSimulatorError("tool calibration features must be a mapping")
        return cls(
            ToolEventInput.from_mapping(row["features"]),
            _positive_number(row["observed_ms"], "observed_ms"),
        )


@dataclass(frozen=True)
class ModelCalibrationRecord:
    features: ModelEventInput
    observed_ms: float

    def __post_init__(self) -> None:
        if not isinstance(self.features, ModelEventInput):
            raise EventSimulatorError("model calibration features must be ModelEventInput")
        if self.features.split != "calibration":
            raise EventSimulatorError("model calibration record must use split=calibration")
        _positive_number(self.observed_ms, "observed_ms")

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "ModelCalibrationRecord":
        allowed = {"schema_version", "split", "features", "observed_ms"}
        if not isinstance(row, Mapping):
            raise EventSimulatorError("model calibration record must be a mapping")
        unknown = sorted(set(row) - allowed)
        if unknown:
            raise EventSimulatorError("unknown model calibration field(s): " + ", ".join(unknown))
        if row.get("schema_version") != "assignment.model-calibration.v1":
            raise EventSimulatorError("unsupported model calibration schema_version")
        if row.get("split") != "calibration":
            raise EventSimulatorError("holdout/test model labels may not enter fitting")
        if "features" not in row or "observed_ms" not in row:
            raise EventSimulatorError("model calibration requires features and observed_ms")
        if not isinstance(row["features"], Mapping):
            raise EventSimulatorError("model calibration features must be a mapping")
        return cls(
            ModelEventInput.from_mapping(row["features"]),
            _positive_number(row["observed_ms"], "observed_ms"),
        )


@dataclass(frozen=True)
class TrajectoryCalibrationRecord:
    run_id: str
    split: str
    observed_ms: float

    def __post_init__(self) -> None:
        _text(self.run_id, "run_id")
        if self.split != "calibration":
            raise EventSimulatorError("holdout/test trajectory labels may not enter fitting")
        _positive_number(self.observed_ms, "observed_ms")

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "TrajectoryCalibrationRecord":
        allowed = {"schema_version", "run_id", "split", "observed_ms"}
        if not isinstance(row, Mapping):
            raise EventSimulatorError("trajectory calibration record must be a mapping")
        unknown = sorted(set(row) - allowed)
        if unknown:
            raise EventSimulatorError(
                "unknown trajectory calibration field(s): " + ", ".join(unknown)
            )
        if row.get("schema_version") != "assignment.trajectory-calibration.v1":
            raise EventSimulatorError("unsupported trajectory calibration schema_version")
        return cls(
            run_id=_text(row.get("run_id"), "run_id"),
            split=_text(row.get("split"), "split"),
            observed_ms=_positive_number(row.get("observed_ms"), "observed_ms"),
        )


@dataclass(frozen=True)
class _RidgeModel:
    coefficients: tuple[float, ...]
    alpha: float
    selection_mae_ms: float
    training_ids: tuple[str, ...]

    def predict(self, row: Sequence[float]) -> float:
        if len(row) != len(self.coefficients):
            raise EventSimulatorError("ridge design width mismatch")
        return max(0.0, sum(a * b for a, b in zip(self.coefficients, row)))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "alpha": self.alpha,
            "coefficients": list(self.coefficients),
            "selection": "deterministic_leave_one_trajectory_out_mae",
            "selection_mae_ms": self.selection_mae_ms,
            "training_ids": list(self.training_ids),
        }


def _solve_ridge(
    design: Sequence[Sequence[float]], targets: Sequence[float], alpha: float
) -> tuple[float, ...]:
    if not design or len(design) != len(targets):
        raise EventSimulatorError("ridge fitting requires aligned non-empty rows and targets")
    width = len(design[0])
    if width == 0 or any(len(row) != width for row in design):
        raise EventSimulatorError("ridge design matrix has inconsistent width")
    normal = [[0.0] * width for _ in range(width)]
    rhs = [0.0] * width
    for row, target in zip(design, targets):
        for i in range(width):
            rhs[i] += row[i] * target
            for j in range(width):
                normal[i][j] += row[i] * row[j]
    for i in range(width):
        normal[i][i] += alpha
    for pivot in range(width):
        best = max(range(pivot, width), key=lambda index: abs(normal[index][pivot]))
        if abs(normal[best][pivot]) <= 1e-15:
            raise EventSimulatorError("ridge system is numerically singular")
        if best != pivot:
            normal[pivot], normal[best] = normal[best], normal[pivot]
            rhs[pivot], rhs[best] = rhs[best], rhs[pivot]
        scale = normal[pivot][pivot]
        for column in range(pivot, width):
            normal[pivot][column] /= scale
        rhs[pivot] /= scale
        for row_index in range(width):
            if row_index == pivot:
                continue
            factor = normal[row_index][pivot]
            if factor == 0:
                continue
            for column in range(pivot, width):
                normal[row_index][column] -= factor * normal[pivot][column]
            rhs[row_index] -= factor * rhs[pivot]
    if not all(isfinite(value) for value in rhs):
        raise EventSimulatorError("ridge fitting produced non-finite coefficients")
    return tuple(rhs)


def _select_ridge(
    design: Sequence[Sequence[float]],
    targets: Sequence[float],
    group_ids: Sequence[str],
    training_ids: Sequence[str],
) -> _RidgeModel:
    if len(design) < 2 or len(set(group_ids)) < 2:
        raise EventSimulatorError("fitting requires records from at least two calibration runs")
    if not (len(design) == len(targets) == len(group_ids) == len(training_ids)):
        raise EventSimulatorError("calibration arrays are not aligned")
    groups = sorted(set(group_ids))
    candidates: list[tuple[float, float]] = []
    for alpha in RIDGE_CANDIDATES:
        errors: list[float] = []
        for group in groups:
            train = [index for index, value in enumerate(group_ids) if value != group]
            test = [index for index, value in enumerate(group_ids) if value == group]
            coefficients = _solve_ridge(
                [design[index] for index in train],
                [targets[index] for index in train],
                alpha,
            )
            for index in test:
                predicted = max(
                    0.0,
                    sum(a * b for a, b in zip(coefficients, design[index])),
                )
                errors.append(abs(predicted - targets[index]))
        candidates.append((sum(errors) / len(errors), alpha))
    selection_mae, selected_alpha = min(candidates, key=lambda item: (item[0], item[1]))
    coefficients = _solve_ridge(design, targets, selected_alpha)
    return _RidgeModel(
        coefficients=coefficients,
        alpha=selected_alpha,
        selection_mae_ms=selection_mae,
        training_ids=tuple(sorted(training_ids)),
    )


def _unique(values: Sequence[str], kind: str) -> None:
    if len(values) != len(set(values)):
        raise EventSimulatorError(f"duplicate {kind} identifier")


@dataclass(frozen=True)
class AssignmentEventSimulator:
    """Calibration-only models for tool, model-request, and E2E latency."""

    tool_model: _RidgeModel
    model_model: _RidgeModel
    trajectory_model: _RidgeModel
    calibration_run_ids: tuple[str, ...]

    @classmethod
    def fit(
        cls,
        tool_records: Iterable[ToolCalibrationRecord | Mapping[str, Any]],
        model_records: Iterable[ModelCalibrationRecord | Mapping[str, Any]],
        trajectory_records: Iterable[TrajectoryCalibrationRecord | Mapping[str, Any]],
    ) -> "AssignmentEventSimulator":
        tools = [
            row if isinstance(row, ToolCalibrationRecord) else ToolCalibrationRecord.from_mapping(row)
            for row in tool_records
        ]
        models = [
            row
            if isinstance(row, ModelCalibrationRecord)
            else ModelCalibrationRecord.from_mapping(row)
            for row in model_records
        ]
        trajectories = [
            row
            if isinstance(row, TrajectoryCalibrationRecord)
            else TrajectoryCalibrationRecord.from_mapping(row)
            for row in trajectory_records
        ]
        tools.sort(key=lambda row: (row.features.run_id, row.features.event_id))
        models.sort(key=lambda row: (row.features.run_id, row.features.request_id))
        trajectories.sort(key=lambda row: row.run_id)
        if not tools or not models or not trajectories:
            raise EventSimulatorError("tool, model, and trajectory calibration records are required")
        _unique([row.features.event_id for row in tools], "tool calibration")
        _unique([row.features.request_id for row in models], "model calibration")
        _unique([row.run_id for row in trajectories], "trajectory calibration")
        run_ids = {row.run_id for row in trajectories}
        if {row.features.run_id for row in tools} != run_ids:
            raise EventSimulatorError("tool calibration runs must exactly match trajectory runs")
        if {row.features.run_id for row in models} != run_ids:
            raise EventSimulatorError("model calibration runs must exactly match trajectory runs")

        tool_model = _select_ridge(
            [row.features.design_row() for row in tools],
            [row.observed_ms for row in tools],
            [row.features.run_id for row in tools],
            [row.features.event_id for row in tools],
        )
        model_model = _select_ridge(
            [row.features.design_row() for row in models],
            [row.observed_ms for row in models],
            [row.features.run_id for row in models],
            [row.features.request_id for row in models],
        )

        aggregate: dict[str, dict[str, float]] = {
            run_id: {"tool_ms": 0.0, "model_ms": 0.0, "tools": 0.0, "models": 0.0}
            for run_id in run_ids
        }
        for row in tools:
            item = aggregate[row.features.run_id]
            item["tool_ms"] += tool_model.predict(row.features.design_row())
            item["tools"] += 1.0
        for row in models:
            item = aggregate[row.features.run_id]
            item["model_ms"] += model_model.predict(row.features.design_row())
            item["models"] += 1.0
        ordered = sorted(trajectories, key=lambda row: row.run_id)
        trajectory_design = [_trajectory_design(aggregate[row.run_id]) for row in ordered]
        trajectory_model = _select_ridge(
            trajectory_design,
            [row.observed_ms for row in ordered],
            [row.run_id for row in ordered],
            [row.run_id for row in ordered],
        )
        return cls(
            tool_model=tool_model,
            model_model=model_model,
            trajectory_model=trajectory_model,
            calibration_run_ids=tuple(sorted(run_ids)),
        )

    def predict_tool(self, features: ToolEventInput | Mapping[str, Any]) -> dict[str, Any]:
        if isinstance(features, ToolCalibrationRecord):
            raise EventSimulatorError("predict_tool rejects labeled calibration records")
        row = features if isinstance(features, ToolEventInput) else ToolEventInput.from_mapping(features)
        return {
            "event_id": row.event_id,
            "run_id": row.run_id,
            "predicted_ms": self.tool_model.predict(row.design_row()),
            "feature_sha256": canonical_sha256(row.to_mapping()),
        }

    def predict_model(self, features: ModelEventInput | Mapping[str, Any]) -> dict[str, Any]:
        if isinstance(features, ModelCalibrationRecord):
            raise EventSimulatorError("predict_model rejects labeled calibration records")
        row = (
            features
            if isinstance(features, ModelEventInput)
            else ModelEventInput.from_mapping(features)
        )
        return {
            "request_id": row.request_id,
            "run_id": row.run_id,
            "predicted_ms": self.model_model.predict(row.design_row()),
            "feature_sha256": canonical_sha256(row.to_mapping()),
        }

    def build_prediction_manifest(
        self,
        tool_events: Iterable[ToolEventInput | Mapping[str, Any]],
        model_events: Iterable[ModelEventInput | Mapping[str, Any]],
    ) -> dict[str, Any]:
        tools = [
            row if isinstance(row, ToolEventInput) else ToolEventInput.from_mapping(row)
            for row in tool_events
        ]
        models = [
            row if isinstance(row, ModelEventInput) else ModelEventInput.from_mapping(row)
            for row in model_events
        ]
        if not tools or not models:
            raise EventSimulatorError("holdout requires both tool and model-event features")
        if any(row.split != "holdout" for row in tools + models):
            raise EventSimulatorError("prediction freeze accepts holdout features only")
        _unique([row.event_id for row in tools], "holdout tool-event")
        _unique([row.request_id for row in models], "holdout model-event")
        holdout_runs = {row.run_id for row in tools + models}
        if holdout_runs & set(self.calibration_run_ids):
            raise EventSimulatorError("calibration and holdout run IDs must be disjoint")
        if {row.run_id for row in tools} != holdout_runs or {row.run_id for row in models} != holdout_runs:
            raise EventSimulatorError("every holdout trajectory requires tool and model events")

        tool_predictions = sorted(
            (self.predict_tool(row) for row in tools),
            key=lambda row: (row["run_id"], row["event_id"]),
        )
        model_predictions = sorted(
            (self.predict_model(row) for row in models),
            key=lambda row: (row["run_id"], row["request_id"]),
        )
        aggregate: dict[str, dict[str, float]] = {
            run_id: {"tool_ms": 0.0, "model_ms": 0.0, "tools": 0.0, "models": 0.0}
            for run_id in holdout_runs
        }
        for row in tool_predictions:
            item = aggregate[row["run_id"]]
            item["tool_ms"] += row["predicted_ms"]
            item["tools"] += 1.0
        for row in model_predictions:
            item = aggregate[row["run_id"]]
            item["model_ms"] += row["predicted_ms"]
            item["models"] += 1.0
        trajectory_predictions = []
        for run_id in sorted(aggregate):
            item = aggregate[run_id]
            trajectory_predictions.append(
                {
                    "run_id": run_id,
                    "predicted_ms": self.trajectory_model.predict(_trajectory_design(item)),
                    "predicted_tool_ms": item["tool_ms"],
                    "predicted_model_ms": item["model_ms"],
                    "tool_event_count": int(item["tools"]),
                    "model_event_count": int(item["models"]),
                }
            )
        return {
            "schema_version": PREDICTION_MANIFEST_SCHEMA,
            "provenance": "calibration_only",
            "target_gate_percent": 25.0,
            "calibration_run_ids": list(self.calibration_run_ids),
            "models": {
                "tool_event": self.tool_model.to_mapping(),
                "model_event": self.model_model.to_mapping(),
                "trajectory": self.trajectory_model.to_mapping(),
            },
            "tool_predictions": tool_predictions,
            "model_predictions": model_predictions,
            "trajectory_predictions": trajectory_predictions,
        }

    def freeze_predictions(
        self,
        tool_events: Iterable[ToolEventInput | Mapping[str, Any]],
        model_events: Iterable[ModelEventInput | Mapping[str, Any]],
        manifest_path: Path,
    ) -> str:
        manifest = self.build_prediction_manifest(tool_events, model_events)
        return freeze_prediction_manifest(manifest, manifest_path)


def _trajectory_design(values: Mapping[str, float]) -> tuple[float, ...]:
    return (
        1.0,
        values["tool_ms"] / 1000.0,
        values["model_ms"] / 1000.0,
        values["tools"] / 10.0,
        values["models"] / 10.0,
    )


def prediction_sidecar_path(manifest_path: Path) -> Path:
    return manifest_path.with_suffix(".sha256")


def freeze_prediction_manifest(manifest: Mapping[str, Any], manifest_path: Path) -> str:
    if manifest.get("schema_version") != PREDICTION_MANIFEST_SCHEMA:
        raise EventSimulatorError("unsupported prediction manifest schema")
    payload = _canonical_bytes(manifest)
    digest = hashlib.sha256(payload).hexdigest()
    sidecar = prediction_sidecar_path(manifest_path)
    sidecar_payload = f"{digest}  {manifest_path.name}\n".encode("utf-8")
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    if manifest_path.exists():
        if manifest_path.read_bytes() != payload:
            raise EventSimulatorError("refusing to overwrite frozen prediction manifest")
        if not sidecar.exists() or sidecar.read_bytes() != sidecar_payload:
            raise EventSimulatorError("frozen prediction hash sidecar is missing or changed")
        return digest
    try:
        with manifest_path.open("xb") as stream:
            stream.write(payload)
        with sidecar.open("xb") as stream:
            stream.write(sidecar_payload)
    except OSError as exc:
        raise EventSimulatorError(f"could not freeze prediction manifest: {exc}") from exc
    return digest


def verify_frozen_prediction_manifest(manifest_path: Path) -> tuple[dict[str, Any], str]:
    try:
        payload = manifest_path.read_bytes()
        manifest = json.loads(payload)
    except (OSError, json.JSONDecodeError) as exc:
        raise EventSimulatorError(f"cannot read frozen prediction manifest: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != PREDICTION_MANIFEST_SCHEMA:
        raise EventSimulatorError("unsupported frozen prediction manifest")
    digest = hashlib.sha256(payload).hexdigest()
    sidecar = prediction_sidecar_path(manifest_path)
    expected = f"{digest}  {manifest_path.name}\n"
    try:
        actual = sidecar.read_text(encoding="utf-8")
    except OSError as exc:
        raise EventSimulatorError(f"cannot read prediction hash sidecar: {exc}") from exc
    if actual != expected:
        raise EventSimulatorError("prediction manifest or SHA-256 sidecar was tampered with")
    return manifest, digest


__all__ = [
    "AssignmentEventSimulator",
    "EventSimulatorError",
    "HardwareProfile",
    "ModelCalibrationRecord",
    "ModelEventInput",
    "ToolCalibrationRecord",
    "ToolEventInput",
    "TrajectoryCalibrationRecord",
    "canonical_sha256",
    "freeze_prediction_manifest",
    "prediction_sidecar_path",
    "verify_frozen_prediction_manifest",
]
