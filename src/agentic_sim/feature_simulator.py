"""Pre-execution, feature-only latency prediction.

``HardwareLatencySimulator`` reconstructs a measured run from measured CPU and
GPU phase durations.  That is useful for a controlled residual check, but it
cannot be used before a run starts.  This module deliberately has a different
input contract: prediction inputs contain only values available before
execution (token budgets, input/context sizes, tool-count metadata, and a
hardware score).

The distinction is enforced at the parser boundary.  In particular, measured
CPU/CUDA/wall values, observed latency, and actual/generated output-token
counts are rejected even when they are supplied as otherwise-ignored mapping
keys.  Training labels are accepted only by :class:`FeatureCalibrationRecord`;
``predict`` never accepts or returns a target value.

The fitted model is a small additive model:

.. math::

   t = b + p\,x_{prompt}/h + d\,x_{budget}/h + c\,x_{context}/h
       + q\,x_{tools} + i\,x_{prompt}x_{budget}/h

where token features are represented in thousands, and ``h`` is an
execution-hardware score relative to the calibration hardware.  The model is
intentionally dependency-free and uses a lightly regularized normal equation
so it remains usable with small calibration sets.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any, Iterable, Mapping


class FeatureSimulatorError(ValueError):
    """The feature-only simulator input is invalid or contains leakage."""


FEATURE_INPUT_SCHEMA = "simulator.feature-input.v1"
FEATURE_CALIBRATION_SCHEMA = "simulator.feature-calibration.v1"
FEATURE_PREDICTION_SCHEMA = "simulator.feature-prediction.v1"

# These names are intentionally broader than the fields used by the existing
# phase simulator.  Rejecting aliases prevents a caller from smuggling a
# measured/target-derived value through an unrecognized feature key.
_TARGET_FIELDS = frozenset(
    {
        "observed_seconds",
        "observed_latency",
        "target_seconds",
        "latency_seconds",
        "duration_seconds",
        "wall_seconds",
        "wall_time_seconds",
        "wall_ms",
        "elapsed_seconds",
        "elapsed_ms",
        "cpu_seconds",
        "cpu_time_seconds",
        "cpu_ms",
        "cuda_seconds",
        "cuda_time_seconds",
        "cuda_ms",
        "gpu_seconds",
        "gpu_time_seconds",
        "gpu_ms",
        "gpu_seconds_at_reference",
        "kineto_wall_ms",
        "kineto_cpu_ms",
        "kineto_cuda_ms",
        "kineto_duration_ms",
        "kernel_duration_sum_ms",
        "cpu_activity_union_ms",
        "cuda_activity_union_ms",
        "measured_wall_time_ms",
        "measured_cpu_time_ms",
        "measured_cuda_time_ms",
        "measured_kineto_time_ms",
        "generated_tokens",
        "completion_tokens",
        "output_tokens",
        "actual_output_tokens",
        "decoded_tokens",
        "new_tokens",
        "completion_length",
        "output_length",
        "measured_output_tokens",
        "target_tokens",
    }
)

_FEATURE_FIELDS = frozenset(
    {
        "run_id",
        "prompt_tokens",
        "max_output_tokens",
        "context_tokens",
        "tool_calls",
        "hardware_score",
    }
)
_FEATURE_METADATA_FIELDS = frozenset({"schema_version"})
_REQUIRED_FEATURE_FIELDS = frozenset({"prompt_tokens", "max_output_tokens"})
_MODEL_FEATURE_NAMES = (
    "prompt_tokens",
    "max_output_tokens",
    "context_tokens",
    "tool_calls",
    "prompt_output_interaction",
)


def _number(row: Mapping[str, Any], name: str, *, minimum: float = 0.0) -> float:
    value = row.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FeatureSimulatorError(f"invalid {name}; expected a finite number")
    value = float(value)
    if not isfinite(value) or value < minimum:
        raise FeatureSimulatorError(f"invalid {name}; expected a finite number >= {minimum}")
    return value


@dataclass(frozen=True)
class FeatureInput:
    """Features that are available before execution starts.

    ``max_output_tokens`` is a configured budget, not the number of tokens
    that the model eventually generated.  Actual output length is deliberately
    not represented by this type.
    """

    run_id: str
    prompt_tokens: float
    max_output_tokens: float
    context_tokens: float = 0.0
    tool_calls: float = 0.0
    hardware_score: float = 1.0

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id:
            raise FeatureSimulatorError("run_id must be a non-empty string")
        for name in ("prompt_tokens", "max_output_tokens", "context_tokens", "tool_calls"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise FeatureSimulatorError(f"{name} must be a finite number >= 0")
            if not isfinite(float(value)) or float(value) < 0:
                raise FeatureSimulatorError(f"{name} must be a finite number >= 0")
        if (
            isinstance(self.hardware_score, bool)
            or not isinstance(self.hardware_score, (int, float))
            or not isfinite(float(self.hardware_score))
            or float(self.hardware_score) <= 0
        ):
            raise FeatureSimulatorError("hardware_score must be a positive finite number")

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any], index: int = 0) -> "FeatureInput":
        """Parse a strict feature-only mapping.

        Unknown keys are rejected rather than ignored.  This is important for
        leakage prevention: a holdout's measured timing must not be accepted
        merely because a caller thinks the model will not use that field.
        """

        if not isinstance(row, Mapping):
            raise FeatureSimulatorError(f"feature row {index} must be a mapping")
        if "schema_version" in row and row["schema_version"] != FEATURE_INPUT_SCHEMA:
            raise FeatureSimulatorError("unsupported feature input schema_version")
        leaked = sorted(set(row) & _TARGET_FIELDS)
        if leaked:
            raise FeatureSimulatorError(
                "feature-only input rejects measured or target-derived fields: " + ", ".join(leaked)
            )
        unknown = sorted(set(row) - _FEATURE_FIELDS - _FEATURE_METADATA_FIELDS)
        if unknown:
            raise FeatureSimulatorError("unknown pre-execution feature(s): " + ", ".join(unknown))
        missing = sorted(_REQUIRED_FEATURE_FIELDS - set(row))
        if missing:
            raise FeatureSimulatorError(
                "missing required pre-execution feature(s): " + ", ".join(missing)
            )
        run_id = row.get("run_id", f"feature-{index}")
        if not isinstance(run_id, str) or not run_id:
            raise FeatureSimulatorError(f"feature row {index} has invalid run_id")
        return cls(
            run_id=run_id,
            prompt_tokens=_number(row, "prompt_tokens"),
            max_output_tokens=_number(row, "max_output_tokens"),
            context_tokens=_number(row, "context_tokens") if "context_tokens" in row else 0.0,
            tool_calls=_number(row, "tool_calls") if "tool_calls" in row else 0.0,
            hardware_score=_number(row, "hardware_score", minimum=0.000000000001)
            if "hardware_score" in row
            else 1.0,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": FEATURE_INPUT_SCHEMA,
            "run_id": self.run_id,
            "prompt_tokens": self.prompt_tokens,
            "max_output_tokens": self.max_output_tokens,
            "context_tokens": self.context_tokens,
            "tool_calls": self.tool_calls,
            "hardware_score": self.hardware_score,
        }

    def design_row(self) -> tuple[float, ...]:
        hardware = float(self.hardware_score)
        return (
            1.0,
            float(self.prompt_tokens) / 1000.0 / hardware,
            float(self.max_output_tokens) / 1000.0 / hardware,
            float(self.context_tokens) / 1000.0 / hardware,
            float(self.tool_calls),
            float(self.prompt_tokens) / 1000.0 * float(self.max_output_tokens) / 1000.0 / hardware,
        )


@dataclass(frozen=True)
class FeatureCalibrationRecord:
    """A training-only feature row with an observed latency label.

    This type is never accepted by ``FeatureLatencySimulator.predict``.  A
    mapping marked as a holdout/test row is rejected here so a measured holdout
    cannot accidentally enter fitting through a generic JSON loader.
    """

    features: FeatureInput
    observed_seconds: float
    provenance: str = "measured"

    def __post_init__(self) -> None:
        if isinstance(self.observed_seconds, bool) or not isinstance(
            self.observed_seconds, (int, float)
        ):
            raise FeatureSimulatorError("observed_seconds must be a finite number >= 0")
        if not isfinite(float(self.observed_seconds)) or float(self.observed_seconds) < 0:
            raise FeatureSimulatorError("observed_seconds must be a finite number >= 0")
        if self.provenance not in {"measured", "calibrated"}:
            raise FeatureSimulatorError("calibration provenance must be measured or calibrated")

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any], index: int = 0) -> "FeatureCalibrationRecord":
        if not isinstance(row, Mapping):
            raise FeatureSimulatorError(f"calibration row {index} must be a mapping")
        calibration_keys = {
            "features",
            "observed_seconds",
            "provenance",
            "schema_version",
            "split",
            "run_id",
        }
        unknown = sorted(set(row) - calibration_keys - _FEATURE_FIELDS)
        if unknown:
            raise FeatureSimulatorError("unknown calibration field(s): " + ", ".join(unknown))
        split = row.get("split")
        if isinstance(split, str) and split.lower() in {"holdout", "test", "validation", "eval"}:
            raise FeatureSimulatorError("holdout/test rows may not carry a training latency label")
        leaked = sorted((set(row) & _TARGET_FIELDS) - {"observed_seconds"})
        if leaked:
            raise FeatureSimulatorError(
                "calibration feature row rejects measured or target-derived feature(s): "
                + ", ".join(leaked)
            )
        if "observed_seconds" not in row:
            raise FeatureSimulatorError(
                "calibration row requires observed_seconds as its training label"
            )
        if "features" in row:
            nested = row["features"]
            if not isinstance(nested, Mapping):
                raise FeatureSimulatorError("features must be a mapping")
            feature_row = dict(nested)
            if "run_id" in row and "run_id" not in feature_row:
                feature_row["run_id"] = row["run_id"]
        else:
            feature_row = {
                key: value
                for key, value in row.items()
                if key not in {"observed_seconds", "provenance", "schema_version", "split"}
            }
        features = FeatureInput.from_mapping(feature_row, index)
        provenance = row.get("provenance", "measured")
        return cls(features, _number(row, "observed_seconds"), provenance)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": FEATURE_CALIBRATION_SCHEMA,
            "features": self.features.to_mapping(),
            "observed_seconds": float(self.observed_seconds),
            "provenance": self.provenance,
        }


def _solve_regularized(design: list[tuple[float, ...]], targets: list[float]) -> tuple[float, ...]:
    """Solve a tiny regularized least-squares system without dependencies."""

    width = len(design[0])
    normal = [[0.0 for _ in range(width)] for _ in range(width)]
    rhs = [0.0 for _ in range(width)]
    for row, target in zip(design, targets):
        for i in range(width):
            rhs[i] += row[i] * target
            for j in range(width):
                normal[i][j] += row[i] * row[j]
    # A tiny ridge makes two-row/small calibration fixtures well-defined while
    # leaving ordinary well-conditioned fits unchanged to practical precision.
    ridge = 1e-9
    for i in range(width):
        normal[i][i] += ridge
    for pivot in range(width):
        best = max(range(pivot, width), key=lambda index: abs(normal[index][pivot]))
        if abs(normal[best][pivot]) <= 1e-15:
            raise FeatureSimulatorError("calibration features are numerically degenerate")
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
    return tuple(rhs)


@dataclass(frozen=True)
class FeatureLatencySimulator:
    """Latency model whose prediction API accepts only :class:`FeatureInput`."""

    fixed_seconds: float
    prompt_seconds_per_thousand: float
    output_budget_seconds_per_thousand: float
    context_seconds_per_thousand: float
    tool_call_seconds: float
    prompt_output_interaction_seconds_per_million: float
    training_run_ids: tuple[str, ...]

    @classmethod
    def fit(
        cls, records: Iterable[FeatureCalibrationRecord | Mapping[str, Any]]
    ) -> "FeatureLatencySimulator":
        normalized: list[FeatureCalibrationRecord] = []
        for index, row in enumerate(records):
            if isinstance(row, FeatureCalibrationRecord):
                normalized.append(row)
            else:
                normalized.append(FeatureCalibrationRecord.from_mapping(row, index))
        if len(normalized) < 2:
            raise FeatureSimulatorError("at least two calibration feature records are required")
        coefficients = _solve_regularized(
            [record.features.design_row() for record in normalized],
            [float(record.observed_seconds) for record in normalized],
        )
        if not all(isfinite(value) for value in coefficients):
            raise FeatureSimulatorError("calibration produced non-finite coefficients")
        # Negative latency components are not physically meaningful.  Clipping
        # noisy small-data fits also keeps predictions safe and deterministic.
        coefficients = tuple(max(0.0, value) for value in coefficients)
        return cls(
            fixed_seconds=coefficients[0],
            prompt_seconds_per_thousand=coefficients[1],
            output_budget_seconds_per_thousand=coefficients[2],
            context_seconds_per_thousand=coefficients[3],
            tool_call_seconds=coefficients[4],
            prompt_output_interaction_seconds_per_million=coefficients[5],
            training_run_ids=tuple(record.features.run_id for record in normalized),
        )

    def predict(self, features: FeatureInput | Mapping[str, Any]) -> dict[str, Any]:
        """Predict latency from pre-execution features only.

        Passing a calibration row, a mapping with ``observed_seconds``, or a
        mapping containing measured CPU/CUDA/wall/generated-token values raises
        :class:`FeatureSimulatorError` at the strict parser boundary.
        """

        if isinstance(features, FeatureCalibrationRecord):
            raise FeatureSimulatorError(
                "predict accepts feature-only input, not a labeled calibration record"
            )
        normalized = (
            features if isinstance(features, FeatureInput) else FeatureInput.from_mapping(features)
        )
        row = normalized.design_row()
        predicted = (
            self.fixed_seconds
            + self.prompt_seconds_per_thousand * row[1]
            + self.output_budget_seconds_per_thousand * row[2]
            + self.context_seconds_per_thousand * row[3]
            + self.tool_call_seconds * row[4]
            + self.prompt_output_interaction_seconds_per_million * row[5]
        )
        return {
            "schema_version": FEATURE_PREDICTION_SCHEMA,
            "provenance": "simulated",
            "run_id": normalized.run_id,
            "features": normalized.to_mapping(),
            "predicted_seconds": max(0.0, float(predicted)),
            "training_run_ids": list(self.training_run_ids),
        }

    def predict_many(
        self, rows: Iterable[FeatureInput | Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        """Predict a batch without accepting labels or measured holdout fields."""

        return [self.predict(row) for row in rows]

    def to_mapping(self) -> dict[str, Any]:
        """Return a machine-readable model sidecar without target data."""

        return {
            "schema_version": "simulator.feature-model.v1",
            "provenance": "calibrated",
            "fixed_seconds": self.fixed_seconds,
            "prompt_seconds_per_thousand": self.prompt_seconds_per_thousand,
            "output_budget_seconds_per_thousand": self.output_budget_seconds_per_thousand,
            "context_seconds_per_thousand": self.context_seconds_per_thousand,
            "tool_call_seconds": self.tool_call_seconds,
            "prompt_output_interaction_seconds_per_million": self.prompt_output_interaction_seconds_per_million,
            "training_run_ids": list(self.training_run_ids),
            "feature_names": list(_MODEL_FEATURE_NAMES),
        }


__all__ = [
    "FEATURE_CALIBRATION_SCHEMA",
    "FEATURE_INPUT_SCHEMA",
    "FEATURE_PREDICTION_SCHEMA",
    "FeatureCalibrationRecord",
    "FeatureInput",
    "FeatureLatencySimulator",
    "FeatureSimulatorError",
]
