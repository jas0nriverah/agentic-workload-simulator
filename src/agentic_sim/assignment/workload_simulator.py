"""Assignment workload simulator.

The assignment has two slightly different consumers of this module.  Older
artifacts contain only the cached :class:`ToolEventInput` fields, while the
reviewed CPU model consumes the complete action selected by the agent.  The
``WorkloadToolInput`` adapter below keeps those two protocols explicit: an
action-bearing row is canonicalized through the one sealed extractor and a
legacy row is still accepted by the old CPU model.

The trajectory model deliberately does not regress end-to-end latency on
predicted event sums.  Event sums are the prediction, and a small, separately
identifiable non-negative runner-overhead model is fitted to the measured
trajectory residual.  This keeps runner overhead from silently correcting a
biased CPU predictor.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from math import isfinite
from statistics import median
from typing import Any, Mapping, Sequence

from agentic_sim.assignment.cpu_event_model import (
    HierarchicalMedianModel,
    cpu_action_flags,
    row_from_tool_input,
)
from agentic_sim.assignment.semantic_cpu_model import SemanticCpuModel

from agentic_sim.assignment.event_simulator import (
    EventSimulatorError,
    HardwareProfile,
    ModelEventInput,
    ToolEventInput,
    _RidgeModel,
    _select_ridge,
    _solve_ridge,
    canonical_sha256,
)
from agentic_sim.assignment.tool_features import extract_tool_features


WORKLOAD_MODEL_SCHEMA = "assignment.workload-simulator.v3"
GATE_PERCENT = 25.0
_TIMEOUT_WALL_MS = 20_000.0
_NNLS_TOL = 1e-8
_ACTION_FIELDS = {"action", "repository", "instance_id"}


def _ape(predicted: float, observed: float) -> float:
    return abs(predicted - observed) / max(observed, 1e-9) * 100.0


def _positive_target(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EventSimulatorError(f"{name} must be a positive finite number")
    result = float(value)
    if not isfinite(result) or result <= 0:
        raise EventSimulatorError(f"{name} must be a positive finite number")
    return result


def _finite_nonnegative(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EventSimulatorError(f"{name} must be a finite number >= 0")
    result = float(value)
    if not isfinite(result) or result < 0:
        raise EventSimulatorError(f"{name} must be a finite number >= 0")
    return result


def _serial_fraction(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise EventSimulatorError("cpu_serial_fraction must be finite and in [0, 1]") from exc
    if not isfinite(result) or not 0 <= result <= 1:
        raise EventSimulatorError("cpu_serial_fraction must be finite and in [0, 1]")
    return result


def _optional_text(value: Any, name: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise EventSimulatorError(f"{name} must be text")
    return value


@dataclass(frozen=True)
class WorkloadToolInput(ToolEventInput):
    """Tool input with action and stable identity used by the CPU model.

    ``instance_id`` is retained for fit-time support accounting only.  It is
    intentionally not a prediction key or a lookup-table key.  Whenever an
    action is supplied, ``from_mapping`` canonicalizes every cached extractor
    field from that action so training and serving have identical semantics.
    """

    action: str = ""
    repository: str = ""
    instance_id: str = ""

    @classmethod
    def _from_base(
        cls,
        base: ToolEventInput,
        *,
        action: str,
        repository: Any,
        instance_id: Any,
    ) -> "WorkloadToolInput":
        return cls(
            event_id=base.event_id,
            run_id=base.run_id,
            split=base.split,
            operation_class=base.operation_class,
            declared_command_bytes=base.declared_command_bytes,
            declared_read_bytes=base.declared_read_bytes,
            declared_write_bytes=base.declared_write_bytes,
            declared_path_count=base.declared_path_count,
            hardware=base.hardware,
            tool_name=base.tool_name,
            subcommand=base.subcommand,
            command_prefix=base.command_prefix,
            command_sha256=base.command_sha256,
            has_pipe=base.has_pipe,
            has_glob=base.has_glob,
            extractor_id=base.extractor_id,
            extractor_sha256=base.extractor_sha256,
            action=action,
            repository=_optional_text(repository, "repository"),
            instance_id=_optional_text(instance_id, "instance_id"),
        )

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "WorkloadToolInput":
        if not isinstance(row, Mapping):
            raise EventSimulatorError("workload tool input must be a mapping")
        # Strip workload-only fields before delegating strict target/feature
        # validation to the sealed base input parser.
        base_row = {key: value for key, value in row.items() if key not in _ACTION_FIELDS}
        base = ToolEventInput.from_mapping(base_row)
        action_value = row.get("action", "")
        if action_value is None:
            action_value = ""
        if not isinstance(action_value, str):
            raise EventSimulatorError("action must be text")
        action = action_value.strip()

        if action:
            # Canonicalize rather than trusting contradictory cached fields.
            extracted = extract_tool_features(action)
            canonical = base.to_mapping()
            for key in (
                "tool_name",
                "subcommand",
                "command_prefix",
                "operation_class",
                "declared_command_bytes",
                "declared_path_count",
                "has_pipe",
                "has_glob",
                "command_sha256",
                "extractor_id",
                "extractor_sha256",
            ):
                canonical[key] = getattr(extracted, key)
            base = ToolEventInput.from_mapping(canonical)

        return cls._from_base(
            base,
            action=action,
            repository=row.get("repository", ""),
            instance_id=row.get("instance_id", ""),
        )

    @classmethod
    def from_action(
        cls,
        action: str,
        event_id: str,
        run_id: str,
        split: str,
        hardware: HardwareProfile | Mapping[str, Any],
        repository: str = "",
        instance_id: str = "",
        declared_read_bytes: int = 0,
        declared_write_bytes: int = 0,
    ) -> "WorkloadToolInput":
        """Build all cached tool fields from ``action`` using one extractor."""
        extracted = extract_tool_features(action)
        hardware_mapping = (
            hardware.to_mapping() if isinstance(hardware, HardwareProfile) else hardware
        )
        base = ToolEventInput.from_mapping(
            {
                "schema_version": "assignment.tool-event-input.v1",
                "event_id": event_id,
                "run_id": run_id,
                "split": split,
                "operation_class": extracted.operation_class,
                "declared_command_bytes": extracted.declared_command_bytes,
                "declared_read_bytes": declared_read_bytes,
                "declared_write_bytes": declared_write_bytes,
                "declared_path_count": extracted.declared_path_count,
                "hardware": hardware_mapping,
                "tool_name": extracted.tool_name,
                "subcommand": extracted.subcommand,
                "command_prefix": extracted.command_prefix,
                "command_sha256": extracted.command_sha256,
                "has_pipe": extracted.has_pipe,
                "has_glob": extracted.has_glob,
                "extractor_id": extracted.extractor_id,
                "extractor_sha256": extracted.extractor_sha256,
            }
        )
        return cls._from_base(
            base,
            action=extracted.action,
            repository=repository,
            instance_id=instance_id,
        )

    def to_mapping(self) -> dict[str, Any]:
        mapping = super().to_mapping()
        mapping.update(
            {
                "action": self.action,
                "repository": self.repository,
                "instance_id": self.instance_id,
            }
        )
        return mapping


def _base_cached_row(features: ToolEventInput) -> dict[str, Any]:
    """Return cached descriptors, without any measured target."""
    row = row_from_tool_input(features)
    # Keep the original cache distinct from action-derived flags for auditing.
    # These values are not identifiers and are never used as instance keys.
    for key in (
        "tool_name",
        "subcommand",
        "command_prefix",
        "operation_class",
        "declared_command_bytes",
        "declared_path_count",
        "has_pipe",
        "has_glob",
        "command_sha256",
        "extractor_id",
        "extractor_sha256",
    ):
        row[f"original_{key}"] = getattr(features, key, row.get(key))
    row["original_cached_flags"] = {
        key: getattr(features, key, row.get(key))
        for key in (
            "tool_name",
            "subcommand",
            "command_prefix",
            "operation_class",
            "declared_command_bytes",
            "declared_path_count",
            "has_pipe",
            "has_glob",
            "command_sha256",
            "extractor_id",
            "extractor_sha256",
        )
    }
    return row


def workload_cpu_row(features: ToolEventInput) -> dict[str, Any]:
    """Map a tool input to semantic or legacy CPU row descriptors."""
    row = _base_cached_row(features)
    if isinstance(features, WorkloadToolInput) and features.action:
        # Re-extract at prediction time.  Stale cached flags cannot smuggle a
        # different class into the semantic model.
        row.update(cpu_action_flags(features.action))
        row["action"] = features.action
        row["repository"] = features.repository
        # Support accounting only; SemanticCpuModel must not use this key.
        row["instance_id"] = features.instance_id
        row["run_id"] = features.run_id
        row["event_id"] = features.event_id
    return row


row_from_workload_tool_input = workload_cpu_row


def gpu_design(features: ModelEventInput) -> tuple[float, ...]:
    """Memory-bandwidth-bound GPU design: tokens scaled by hardware bandwidth."""
    if features.output_tokens is None:
        raise EventSimulatorError(
            "assignment workload simulator requires logged output_tokens"
        )
    bandwidth = features.hardware.gpu_bandwidth_capacity
    compute = features.hardware.gpu_compute_capacity
    return (
        1.0 / compute,
        features.input_tokens / bandwidth,
        features.output_tokens / bandwidth,
        features.context_tokens / bandwidth,
    )


def cpu_design(features: ToolEventInput) -> tuple[float, ...]:
    """Legacy CPU design retained for reproducibility of old artifacts."""
    cpu = features.hardware.cpu_capacity
    read_scale = max(features.hardware.storage_read_mbps, 1e-9)
    write_scale = max(features.hardware.storage_write_mbps, 1e-9)
    classes = tuple(
        1.0 if features.operation_class == name else 0.0
        for name in (
            "read",
            "write",
            "traversal",
            "search",
            "shell",
            "patch",
            "test",
            "other",
        )
    )
    return (
        *classes,
        features.declared_command_bytes / cpu,
        features.declared_path_count / cpu,
        float(features.has_pipe),
        float(features.has_glob),
        features.declared_read_bytes / (read_scale * 1_000_000.0),
        features.declared_write_bytes / (write_scale * 1_000_000.0),
    )


def e2e_design(tool_ms: float, model_ms: float, n_tools: float, n_models: float) -> tuple[float, ...]:
    """Legacy trajectory design, retained only for explicit legacy mode."""
    return (1.0, tool_ms / 1000.0, model_ms / 1000.0, n_tools / 10.0, n_models / 10.0)


@dataclass(frozen=True)
class _NonnegativeLinearModel:
    """Tiny dependency-free NNLS model for runner overhead."""

    coefficients: tuple[float, float, float]
    training_ids: tuple[str, ...]
    selection_sse: float
    active_columns: tuple[int, ...]

    def predict(self, n_tools: float, n_models: float) -> float:
        result = (
            self.coefficients[0]
            + self.coefficients[1] * n_tools
            + self.coefficients[2] * n_models
        )
        return max(0.0, result)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "kind": "nonnegative_least_squares",
            "design": ["intercept", "n_tools", "n_models"],
            "coefficients": list(self.coefficients),
            "training_ids": list(self.training_ids),
            "selection_sse": self.selection_sse,
            "active_columns": list(self.active_columns),
            "nonnegative": True,
        }


def _fit_nonnegative_overhead(
    design: Sequence[Sequence[float]],
    targets: Sequence[float],
    training_ids: Sequence[str],
) -> _NonnegativeLinearModel:
    if not design or len(design) != len(targets) or len(design) != len(training_ids):
        raise EventSimulatorError("overhead fitting requires aligned non-empty rows")
    if any(len(row) != 3 for row in design):
        raise EventSimulatorError("overhead design must be [1, n_tools, n_models]")
    candidates: list[tuple[float, int, tuple[float, float, float], tuple[int, ...]]] = []
    # Enumerating active subsets is exact NNLS for this three-column model and
    # avoids introducing a runtime numpy dependency.
    for width in range(4):
        for active in combinations(range(3), width):
            coefficients = [0.0, 0.0, 0.0]
            if active:
                try:
                    solved = _solve_ridge(
                        [[row[index] for index in active] for row in design],
                        targets,
                        1e-12,
                    )
                except EventSimulatorError:
                    continue
                if any(value < -_NNLS_TOL or not isfinite(value) for value in solved):
                    continue
                for index, value in zip(active, solved):
                    coefficients[index] = 0.0 if abs(value) <= _NNLS_TOL else float(value)
            errors = [
                sum(coefficient * value for coefficient, value in zip(coefficients, row)) - target
                for row, target in zip(design, targets)
            ]
            sse = sum(error * error for error in errors)
            candidates.append((sse, len(active), tuple(coefficients), tuple(active)))
    if not candidates:
        raise EventSimulatorError("overhead NNLS fitting failed")
    sse, _width, coefficients, active = min(
        candidates, key=lambda item: (item[0], item[1], item[3])
    )
    return _NonnegativeLinearModel(
        coefficients=coefficients,
        training_ids=tuple(str(value) for value in training_ids),
        selection_sse=float(sse),
        active_columns=active,
    )


def _model_mapping(model: Any) -> dict[str, Any]:
    if hasattr(model, "to_mapping"):
        return dict(model.to_mapping())
    return {"kind": type(model).__name__}


def _ridge_from_mapping(payload: Mapping[str, Any]) -> _RidgeModel:
    return _RidgeModel(
        coefficients=tuple(float(value) for value in payload.get("coefficients", ())),
        alpha=float(payload.get("alpha", 0.0)),
        selection_mae_ms=float(payload.get("selection_mae_ms", 0.0)),
        training_ids=tuple(str(value) for value in payload.get("training_ids", ())),
    )


def _trajectory_item(item: Any) -> tuple[str, float]:
    """Normalize tuple and calibration-mapping trajectory records."""
    if isinstance(item, Mapping):
        run_id = item.get("run_id")
        split = item.get("split", "calibration")
        target = item.get("observed_ms", item.get("e2e_wall_ms"))
    elif hasattr(item, "run_id") and hasattr(item, "observed_ms"):
        run_id = getattr(item, "run_id")
        split = getattr(item, "split", "calibration")
        target = getattr(item, "observed_ms")
    else:
        try:
            run_id, target = item
        except (TypeError, ValueError) as exc:
            raise EventSimulatorError("invalid trajectory calibration record") from exc
        split = "calibration"
    if not isinstance(run_id, str) or not run_id.strip():
        raise EventSimulatorError("trajectory run_id must be a non-empty string")
    if split != "calibration":
        raise EventSimulatorError("holdout/test trajectory labels may not enter fitting")
    return run_id, _positive_target(target, "trajectory observed_ms")


def _event_pair(item: Any, *, kind: str) -> tuple[Any, float]:
    if isinstance(item, Mapping):
        if "features" in item:
            feature_row = item["features"]
            target = item.get("observed_ms")
        else:
            # Labels belong to the fit record, not the public feature parser.
            # Remove only this outer label before delegating strict validation.
            feature_row = {
                key: value for key, value in item.items() if key != "observed_ms"
            }
            target = item.get("observed_ms")
        if target is None:
            raise EventSimulatorError(f"{kind} calibration row requires observed_ms")
    else:
        try:
            feature_row, target = item
        except (TypeError, ValueError) as exc:
            raise EventSimulatorError(f"invalid {kind} calibration record") from exc
    if kind == "tool":
        if isinstance(feature_row, (ToolEventInput, WorkloadToolInput)):
            features = feature_row
        elif not isinstance(feature_row, Mapping):
            raise EventSimulatorError("tool calibration features must be ToolEventInput or mapping")
        else:
            features = WorkloadToolInput.from_mapping(feature_row)
    else:
        if isinstance(feature_row, ModelEventInput):
            features = feature_row
        elif not isinstance(feature_row, Mapping):
            raise EventSimulatorError("model calibration features must be ModelEventInput or mapping")
        else:
            features = ModelEventInput.from_mapping(feature_row)
    return features, _positive_target(target, f"{kind} observed_ms")


def _predict_model_value(model: Any, row: Mapping[str, Any]) -> float:
    return _finite_nonnegative(model.predict(row), "CPU prediction")


def _selected_timeout_mode(model: Any, row: Mapping[str, Any], predicted: float) -> bool:
    """Use the semantic model's selected pager mode, never the current target."""
    details_fn = getattr(model, "predict_details", None)
    if not callable(details_fn):
        return False
    details = details_fn(row)
    if not isinstance(details, Mapping):
        return False
    semantic = details.get("semantic_features")
    if not isinstance(semantic, Mapping) or predicted < _TIMEOUT_WALL_MS:
        return False
    if semantic.get("git_pager_susceptibility") == "tty_candidate":
        return True
    # Future extractor versions may expose an explicitly parsed pager process;
    # accept only that structured field, not arbitrary action substrings.
    pager_process = semantic.get("pager_process") or semantic.get("pager_executable")
    return isinstance(pager_process, str) and pager_process.lower() in {
        "less",
        "more",
        "most",
        "pager",
        "bat",
    }


@dataclass(frozen=True)
class WorkloadSimulator:
    """Hardware-parameterized CPU/GPU event simulator."""

    cpu_model: Any
    model_model: _RidgeModel
    trajectory_model: _RidgeModel | None
    calibration_run_ids: tuple[str, ...]
    cpu_ref_capacity: float
    cpu_ref_ghz: float
    cpu_serial_fraction: float = 1.0
    cpu_kind: str = "semantic"
    cpu_center: str = "median"
    e2e_mode: str = "additive_overhead"
    overhead_model: _NonnegativeLinearModel | None = None
    legacy_cpu_model: Any | None = None
    formulation: str = "assignment_pdf_logged_event_descriptors"

    def __post_init__(self) -> None:
        if self.cpu_kind not in {"semantic", "legacy"}:
            raise EventSimulatorError("cpu_kind must be semantic or legacy")
        if self.cpu_center not in {"median", "gate"}:
            raise EventSimulatorError("cpu_center must be median or gate")
        if self.e2e_mode not in {"additive_overhead", "legacy_ridge"}:
            raise EventSimulatorError("unsupported workload e2e mode")
        _serial_fraction(self.cpu_serial_fraction)
        if self.e2e_mode == "additive_overhead" and self.overhead_model is None:
            raise EventSimulatorError("additive_overhead mode requires an overhead model")

    @classmethod
    def fit(
        cls,
        tool_records: Sequence[Any],
        model_records: Sequence[Any],
        trajectory_records: Sequence[Any],
        *,
        select_alpha: bool = True,
        cpu_center: str = "median",
        cpu_kind: str | None = None,
        cpu_serial_fraction: float = 1.0,
        e2e_mode: str = "additive_overhead",
        legacy_e2e: bool = False,
    ) -> "WorkloadSimulator":
        if not tool_records or not model_records or not trajectory_records:
            raise EventSimulatorError("workload fit requires tool, model, and trajectory rows")
        if cpu_center not in {"median", "gate"}:
            raise EventSimulatorError("cpu_center must be median or gate")
        cpu_serial_fraction = _serial_fraction(cpu_serial_fraction)
        if legacy_e2e:
            e2e_mode = "legacy_ridge"
        if e2e_mode in {"legacy", "legacy_ridge", "ridge"}:
            e2e_mode = "legacy_ridge"
        elif e2e_mode in {"additive", "overhead", "additive_overhead"}:
            e2e_mode = "additive_overhead"
        else:
            raise EventSimulatorError("unsupported workload e2e mode")

        tools = [_event_pair(item, kind="tool") for item in tool_records]
        models = [_event_pair(item, kind="model") for item in model_records]
        trajectories = [_trajectory_item(item) for item in trajectory_records]
        tool_features = [item[0] for item in tools]
        model_features = [item[0] for item in models]
        if any(not isinstance(item, (ToolEventInput, WorkloadToolInput)) for item in tool_features):
            raise EventSimulatorError("tool calibration features must be ToolEventInput")
        if any(not isinstance(item, ModelEventInput) for item in model_features):
            raise EventSimulatorError("model calibration features must be ModelEventInput")
        if any(item.split != "calibration" for item in tool_features):
            raise EventSimulatorError("holdout/test tool labels may not enter fitting")
        if any(item.split != "calibration" for item in model_features):
            raise EventSimulatorError("holdout/test model labels may not enter fitting")
        if any(item.output_tokens is None for item in model_features):
            raise EventSimulatorError(
                "assignment workload simulator requires logged output_tokens"
            )

        event_ids = [item.event_id for item in tool_features]
        request_ids = [item.request_id for item in model_features]
        trajectory_ids = [item[0] for item in trajectories]
        if len(set(event_ids)) != len(event_ids):
            raise EventSimulatorError("tool calibration event IDs must be unique")
        if len(set(request_ids)) != len(request_ids):
            raise EventSimulatorError("model calibration request IDs must be unique")
        if len(set(trajectory_ids)) != len(trajectory_ids):
            raise EventSimulatorError("trajectory calibration IDs must be unique")
        run_ids = set(trajectory_ids)
        if {item.run_id for item in tool_features} != run_ids:
            raise EventSimulatorError("tool calibration runs must exactly match trajectory runs")
        if {item.run_id for item in model_features} != run_ids:
            raise EventSimulatorError("model calibration runs must exactly match trajectory runs")

        action_rows = [item for item in tool_features if isinstance(item, WorkloadToolInput) and item.action]
        has_legacy_rows = any(not (isinstance(item, WorkloadToolInput) and item.action) for item in tool_features)
        if cpu_kind is None:
            cpu_kind = "semantic" if action_rows else "legacy"
        if cpu_kind not in {"semantic", "legacy"}:
            raise EventSimulatorError("cpu_kind must be semantic or legacy")
        if cpu_kind == "semantic":
            # Semantic labels are wall times at the calibration host.  The
            # wrapper applies only the explicit frequency sensitivity policy;
            # it must not silently pool labels from hosts whose storage/core
            # capacity also changed.
            reference_cpu_profile = tuple(
                getattr(tool_features[0].hardware, field)
                for field in (
                    "cpu_base_ghz",
                    "storage_read_mbps",
                    "storage_write_mbps",
                    "cpu_cores",
                    "cpu_threads",
                )
            )
            if any(
                tuple(
                    getattr(item.hardware, field)
                    for field in (
                        "cpu_base_ghz",
                        "storage_read_mbps",
                        "storage_write_mbps",
                        "cpu_cores",
                        "cpu_threads",
                    )
                )
                != reference_cpu_profile
                for item in tool_features
            ):
                raise EventSimulatorError(
                    "semantic CPU fitting requires one reference CPU profile; "
                    "base GHz/storage/core/thread fields must match"
                )

        cpu_rows = []
        for features, observed in tools:
            row = workload_cpu_row(features)
            row["observed_ms"] = observed
            cpu_rows.append(row)
        if cpu_kind == "semantic":
            cpu_model = SemanticCpuModel(center=cpu_center, use_repository=True)
            cpu_model.fit(cpu_rows)
            legacy_cpu_model = None
            if has_legacy_rows:
                legacy_rows = [
                    {**row_from_tool_input(features), "observed_ms": observed}
                    for features, observed in tools
                    if not (isinstance(features, WorkloadToolInput) and features.action)
                ]
                legacy_cpu_model = HierarchicalMedianModel(min_count=8).fit(legacy_rows)
        else:
            cpu_model = HierarchicalMedianModel(min_count=8).fit(cpu_rows)
            legacy_cpu_model = None

        model_targets = [target for _features, target in models]
        if select_alpha:
            model_model = _select_ridge(
                [gpu_design(row) for row in model_features],
                model_targets,
                [row.run_id for row in model_features],
                [row.request_id for row in model_features],
            )
        else:
            model_model = _RidgeModel(
                coefficients=_solve_ridge(
                    [gpu_design(row) for row in model_features], model_targets, 1e-3
                ),
                alpha=1e-3,
                selection_mae_ms=0.0,
                training_ids=tuple(row.request_id for row in model_features),
            )

        cpu_ref_capacity = float(median(item.hardware.cpu_capacity for item in tool_features))
        cpu_ref_ghz = float(median(item.hardware.cpu_base_ghz for item in tool_features))

        # Measured event totals are used solely for overhead targets.  The CPU
        # predictor is deliberately absent from this calculation.
        aggregate: dict[str, dict[str, float]] = {
            run_id: {"tool_ms": 0.0, "model_ms": 0.0, "tools": 0.0, "models": 0.0}
            for run_id in run_ids
        }
        for features, observed in tools:
            aggregate[features.run_id]["tool_ms"] += observed
            aggregate[features.run_id]["tools"] += 1.0
        for features, observed in models:
            aggregate[features.run_id]["model_ms"] += observed
            aggregate[features.run_id]["models"] += 1.0
        ordered = sorted(trajectories, key=lambda item: item[0])
        residuals: list[float] = []
        for run_id, observed in ordered:
            item = aggregate[run_id]
            residual = observed - item["tool_ms"] - item["model_ms"]
            tolerance = max(1e-6, abs(observed) * 1e-9)
            if residual < -tolerance:
                raise EventSimulatorError(
                    "trajectory observed_ms is below measured CPU+GPU event sums; "
                    "measurement boundary mismatch"
                )
            residuals.append(max(0.0, residual))

        trajectory_model: _RidgeModel | None = None
        overhead_model: _NonnegativeLinearModel | None = None
        if e2e_mode == "additive_overhead":
            overhead_model = _fit_nonnegative_overhead(
                [
                    (1.0, aggregate[run_id]["tools"], aggregate[run_id]["models"])
                    for run_id, _observed in ordered
                ],
                residuals,
                [run_id for run_id, _observed in ordered],
            )
        else:
            # Explicit compatibility mode reproduces the previous trajectory
            # ridge, including its use of predicted event sums.
            tmp = cls(
                cpu_model=cpu_model,
                model_model=model_model,
                trajectory_model=_RidgeModel(
                    coefficients=(1.0, 1.0, 1.0, 0.0, 0.0),
                    alpha=0.0,
                    selection_mae_ms=0.0,
                    training_ids=(),
                ),
                calibration_run_ids=tuple(sorted(run_ids)),
                cpu_ref_capacity=cpu_ref_capacity,
                cpu_ref_ghz=cpu_ref_ghz,
                cpu_serial_fraction=cpu_serial_fraction,
                cpu_kind=cpu_kind,
                cpu_center=cpu_center,
                e2e_mode="legacy_ridge",
                overhead_model=None,
                legacy_cpu_model=legacy_cpu_model,
            )
            predicted_aggregate: dict[str, dict[str, float]] = {
                run_id: {"tool_ms": 0.0, "model_ms": 0.0, "tools": 0.0, "models": 0.0}
                for run_id in run_ids
            }
            for features, _observed in tools:
                predicted_aggregate[features.run_id]["tool_ms"] += tmp.predict_tool_ms(features)
                predicted_aggregate[features.run_id]["tools"] += 1.0
            for features, _observed in models:
                predicted_aggregate[features.run_id]["model_ms"] += model_model.predict(gpu_design(features))
                predicted_aggregate[features.run_id]["models"] += 1.0
            trajectory_design = [
                e2e_design(
                    predicted_aggregate[run_id]["tool_ms"],
                    predicted_aggregate[run_id]["model_ms"],
                    predicted_aggregate[run_id]["tools"],
                    predicted_aggregate[run_id]["models"],
                )
                for run_id, _observed in ordered
            ]
            trajectory_targets = [observed for _run_id, observed in ordered]
            if select_alpha:
                trajectory_model = _select_ridge(
                    trajectory_design,
                    trajectory_targets,
                    [run_id for run_id, _observed in ordered],
                    [run_id for run_id, _observed in ordered],
                )
            else:
                trajectory_model = _RidgeModel(
                    coefficients=_solve_ridge(trajectory_design, trajectory_targets, 1e-3),
                    alpha=1e-3,
                    selection_mae_ms=0.0,
                    training_ids=tuple(run_id for run_id, _observed in ordered),
                )

        return cls(
            cpu_model=cpu_model,
            model_model=model_model,
            trajectory_model=trajectory_model,
            calibration_run_ids=tuple(sorted(run_ids)),
            cpu_ref_capacity=cpu_ref_capacity,
            cpu_ref_ghz=cpu_ref_ghz,
            cpu_serial_fraction=cpu_serial_fraction,
            cpu_kind=cpu_kind,
            cpu_center=cpu_center,
            e2e_mode=e2e_mode,
            overhead_model=overhead_model,
            legacy_cpu_model=legacy_cpu_model,
        )

    def _coerce_tool(self, features: ToolEventInput | Mapping[str, Any]) -> ToolEventInput:
        if isinstance(features, (ToolEventInput, WorkloadToolInput)):
            return features
        if not isinstance(features, Mapping):
            raise EventSimulatorError("tool prediction requires ToolEventInput or mapping")
        if "action" in features or "repository" in features or "instance_id" in features:
            return WorkloadToolInput.from_mapping(features)
        return ToolEventInput.from_mapping(features)

    def predict_tool_ms(self, features: ToolEventInput | Mapping[str, Any]) -> float:
        item = self._coerce_tool(features)
        row = workload_cpu_row(item)
        if self.cpu_kind == "legacy" or not (isinstance(item, WorkloadToolInput) and item.action):
            model = self.legacy_cpu_model if self.cpu_kind == "semantic" and self.legacy_cpu_model is not None else self.cpu_model
            predicted = _finite_nonnegative(model.predict(row_from_tool_input(item)), "CPU prediction")
            if self.cpu_kind == "legacy":
                # Explicit compatibility mode only: no thread/core speedup in
                # semantic mode.
                predicted *= self.cpu_ref_capacity / max(item.hardware.cpu_capacity, 1e-9)
            return max(1e-6, predicted)

        predicted = _predict_model_value(self.cpu_model, row)
        if _selected_timeout_mode(self.cpu_model, row, predicted):
            return max(1e-6, predicted)
        target_ghz = max(item.hardware.cpu_base_ghz, 1e-9)
        frequency_ratio = self.cpu_ref_ghz / target_ghz
        scale = (1.0 - self.cpu_serial_fraction) + self.cpu_serial_fraction * frequency_ratio
        return max(1e-6, predicted * scale)

    def predict_model_ms(self, features: ModelEventInput | Mapping[str, Any]) -> float:
        item = features if isinstance(features, ModelEventInput) else ModelEventInput.from_mapping(features)
        return self.model_model.predict(gpu_design(item))

    def predict_event_sum_ms(self, tool_ms: float, model_ms: float) -> float:
        """Return the exact predicted CPU+GPU event sum."""
        return _finite_nonnegative(tool_ms, "predicted CPU event sum") + _finite_nonnegative(
            model_ms, "predicted GPU event sum"
        )

    def predict_overhead_ms(self, n_tools: float, n_models: float) -> float:
        tools = _finite_nonnegative(n_tools, "n_tools")
        models = _finite_nonnegative(n_models, "n_models")
        if self.overhead_model is None:
            return 0.0
        return self.overhead_model.predict(tools, models)

    def predict_e2e_ms(
        self, tool_ms: float, model_ms: float, n_tools: float, n_models: float
    ) -> float:
        if self.e2e_mode == "legacy_ridge":
            if self.trajectory_model is None:
                raise EventSimulatorError("legacy workload model lacks a trajectory model")
            return self.trajectory_model.predict(e2e_design(tool_ms, model_ms, n_tools, n_models))
        return self.predict_event_sum_ms(tool_ms, model_ms) + self.predict_overhead_ms(
            n_tools, n_models
        )

    def to_mapping(self) -> dict[str, Any]:
        reference_cpu = {
            "base_ghz": self.cpu_ref_ghz,
            "capacity": self.cpu_ref_capacity,
            "serial_fraction": self.cpu_serial_fraction,
            "frequency_sensitivity": "assumed_serial_work_not_calibrated_physical_decomposition",
            "timeout_wall_invariant_threshold_ms": _TIMEOUT_WALL_MS,
        }
        mapping: dict[str, Any] = {
            "schema_version": WORKLOAD_MODEL_SCHEMA,
            "formulation": self.formulation,
            "mode": self.e2e_mode,
            "e2e_mode": self.e2e_mode,
            "cpu_kind": self.cpu_kind,
            "cpu_center": self.cpu_center,
            "cpu_serial_fraction": self.cpu_serial_fraction,
            "cpu_serial_fraction_assumption": "assumed, not a calibrated physical decomposition",
            "calibration_run_ids": list(self.calibration_run_ids),
            # Retained for readers of v2 artifacts; semantic prediction does
            # not use this capacity value.
            "cpu_ref_capacity": self.cpu_ref_capacity,
            "cpu_ref_ghz": self.cpu_ref_ghz,
            "reference_cpu": reference_cpu,
            "reference_cpu_parameters": reference_cpu,
            "limitations": {
                "storage": "storage bytes/volumes are not logged and are not fitted",
                "host_transfer": (
                    "CPU reference is a hardcoded historical profile, not a verified "
                    "host measurement; cross-host transfer accuracy is not verified by this model"
                ),
            },
            "storage_limitation": "storage bytes/volumes are not logged and are not fitted",
            "tool_event": _model_mapping(self.cpu_model),
            "legacy_tool_event": (
                _model_mapping(self.legacy_cpu_model)
                if self.legacy_cpu_model is not None
                else None
            ),
            "model_event": self.model_model.to_mapping(),
            "overhead": self.overhead_model.to_mapping() if self.overhead_model is not None else None,
            "trajectory": self.trajectory_model.to_mapping() if self.trajectory_model is not None else None,
            "notes": {
                "event_sum": "predict_e2e_ms is predicted CPU+GPU sums plus separately fitted nonnegative runner overhead",
                "legacy_e2e": "legacy_ridge reproduces the pre-v3 trajectory ridge when explicitly selected",
                "instance_id": "used for fit support accounting only; never a prediction key",
            },
        }
        mapping["model_sha256"] = canonical_sha256(
            {key: value for key, value in mapping.items() if key != "model_sha256"}
        )
        return mapping

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "WorkloadSimulator":
        """Restore a frozen model mapping for offline prediction."""
        if not isinstance(payload, Mapping):
            raise EventSimulatorError("workload model must be a mapping")
        schema = payload.get("schema_version")
        if schema not in {"assignment.workload-simulator.v2", WORKLOAD_MODEL_SCHEMA}:
            raise EventSimulatorError("unsupported workload model schema_version")
        if schema == WORKLOAD_MODEL_SCHEMA:
            supplied_hash = payload.get("model_sha256")
            expected_hash = canonical_sha256(
                {key: value for key, value in payload.items() if key != "model_sha256"}
            )
            if not isinstance(supplied_hash, str) or supplied_hash != expected_hash:
                raise EventSimulatorError("workload model_sha256 does not match model contents")
        cpu_kind = str(payload.get("cpu_kind") or "legacy")
        cpu_center = str(payload.get("cpu_center") or "median")
        cpu_payload = payload.get("tool_event") or {}
        if cpu_kind == "legacy":
            cpu_model = HierarchicalMedianModel.from_mapping(cpu_payload)
        else:
            cpu_model = SemanticCpuModel.from_mapping(cpu_payload)
        legacy_payload = payload.get("legacy_tool_event")
        legacy_cpu_model = (
            HierarchicalMedianModel.from_mapping(legacy_payload)
            if isinstance(legacy_payload, Mapping)
            else None
        )
        model_model = _ridge_from_mapping(payload.get("model_event") or {})
        trajectory_payload = payload.get("trajectory")
        trajectory_model = None if not trajectory_payload else _ridge_from_mapping(trajectory_payload)
        overhead_payload = payload.get("overhead")
        overhead_model = None
        if overhead_payload:
            coeffs = tuple(float(value) for value in overhead_payload.get("coefficients", ()))
            if len(coeffs) != 3 or any(
                not isfinite(value) or value < -_NNLS_TOL for value in coeffs
            ):
                raise EventSimulatorError("invalid workload overhead coefficients")
            overhead_model = _NonnegativeLinearModel(
                coefficients=tuple(max(0.0, value) for value in coeffs),  # type: ignore[arg-type]
                training_ids=tuple(str(value) for value in overhead_payload.get("training_ids", ())),
                selection_sse=float(overhead_payload.get("selection_sse", 0.0)),
                active_columns=tuple(int(value) for value in overhead_payload.get("active_columns", ())),
            )
        reference = payload.get("reference_cpu") or {}
        return cls(
            cpu_model=cpu_model,
            model_model=model_model,
            trajectory_model=trajectory_model,
            calibration_run_ids=tuple(str(value) for value in payload.get("calibration_run_ids", ())),
            cpu_ref_capacity=float(payload.get("cpu_ref_capacity", reference.get("capacity", 1.0))),
            cpu_ref_ghz=float(
                payload.get("cpu_ref_ghz", payload.get("reference_cpu_ghz", reference.get("base_ghz", 1.0)))
            ),
            cpu_serial_fraction=float(payload.get("cpu_serial_fraction", reference.get("serial_fraction", 1.0))),
            cpu_kind=cpu_kind,
            cpu_center=cpu_center,
            e2e_mode=str(payload.get("e2e_mode", payload.get("mode", "legacy_ridge"))),
            overhead_model=overhead_model,
            legacy_cpu_model=legacy_cpu_model,
        )


def score_channel(pairs: Sequence[tuple[float, float]]) -> dict[str, Any]:
    if not pairs:
        return {
            "n": 0,
            "mean_ape": None,
            "max_ape": None,
            "within_25_rate": None,
            "all_within_25": False,
        }
    apes = [_ape(pred, obs) for pred, obs in pairs if isfinite(pred) and isfinite(obs)]
    return {
        "n": len(apes),
        "mean_ape": sum(apes) / len(apes),
        "max_ape": max(apes),
        "within_25_rate": sum(item <= GATE_PERCENT for item in apes) / len(apes),
        "all_within_25": all(item <= GATE_PERCENT for item in apes),
    }


def hardware_prediction_shifts(
    features: ModelEventInput, simulator: WorkloadSimulator
) -> dict[str, float]:
    """Show that plugging a different GPU profile changes the prediction."""
    baseline = simulator.predict_model_ms(features)
    slower = HardwareProfile.from_mapping(
        {
            **features.hardware.to_mapping(),
            "hardware_id": "slower-gpu",
            "gpu_memory_bandwidth_gbps": features.hardware.gpu_memory_bandwidth_gbps / 2.0,
            "gpu_bf16_tflops": features.hardware.gpu_bf16_tflops / 2.0,
        }
    )
    shifted = ModelEventInput.from_mapping(
        {**features.to_mapping(), "hardware": slower.to_mapping()}
    )
    slower_ms = simulator.predict_model_ms(shifted)
    return {"reference_ms": baseline, "half_bandwidth_compute_ms": slower_ms}


__all__ = [
    "GATE_PERCENT",
    "WORKLOAD_MODEL_SCHEMA",
    "WorkloadSimulator",
    "WorkloadToolInput",
    "cpu_design",
    "e2e_design",
    "gpu_design",
    "hardware_prediction_shifts",
    "row_from_workload_tool_input",
    "score_channel",
    "workload_cpu_row",
]
