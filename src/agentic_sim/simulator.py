"""A small, provenance-first hardware latency simulator.

The assignment requires a hardware-parameterized simulator.  This module is
deliberately explicit about its inputs: callers must provide a measured or
calibrated CPU component and a measured/calibrated GPU component expressed at
the reference hardware score.  Aggregate vLLM counters and GPU utilization
are not accepted as substitutes for per-run GPU time.

The model is an additive phase model::

    latency(target) = fixed + cpu_seconds
                     + gpu_seconds_at_reference * reference_score / target_score

``fixed`` is calibrated as the median residual on the training records.  The
target score is a relative hardware parameter, so a score of 2.0 represents a
GPU expected to execute the calibrated GPU phase in half the reference time.
The resulting prediction and holdout errors are derived, never measured.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from statistics import median
from typing import Any, Iterable, Mapping


class SimulatorError(ValueError):
    """The simulator input is invalid or lacks a required provenance field."""


_VALID_PROVENANCE = {"measured", "calibrated"}


@dataclass(frozen=True)
class CalibrationRecord:
    """One explicitly decomposed latency observation."""

    run_id: str
    observed_seconds: float
    cpu_seconds: float
    gpu_seconds_at_reference: float
    reference_score: float = 1.0
    provenance: str = "measured"

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any], index: int = 0) -> "CalibrationRecord":
        def number(name: str) -> float:
            value = row.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(float(value)):
                raise SimulatorError(f"record {index} has invalid {name}")
            return float(value)

        run_id = row.get("run_id", f"record-{index}")
        if not isinstance(run_id, str) or not run_id:
            raise SimulatorError(f"record {index} has invalid run_id")
        provenance = row.get("provenance")
        if provenance not in _VALID_PROVENANCE:
            raise SimulatorError(
                f"record {index} must be measured or calibrated; aggregate-only inputs are rejected"
            )
        observed = number("observed_seconds")
        cpu = number("cpu_seconds")
        gpu = number("gpu_seconds_at_reference")
        score = number("reference_score") if "reference_score" in row else 1.0
        if observed < 0 or cpu < 0 or gpu < 0 or score <= 0:
            raise SimulatorError(f"record {index} has negative duration or non-positive reference_score")
        return cls(run_id, observed, cpu, gpu, score, provenance)


@dataclass(frozen=True)
class HardwareLatencySimulator:
    """Fitted additive model with a relative target hardware score."""

    fixed_seconds: float
    reference_score: float
    training_run_ids: tuple[str, ...]

    @classmethod
    def fit(
        cls,
        records: Iterable[CalibrationRecord | Mapping[str, Any]],
        *,
        reference_score: float = 1.0,
    ) -> "HardwareLatencySimulator":
        if isinstance(reference_score, bool) or not isinstance(reference_score, (int, float)):
            raise SimulatorError("reference_score must be a positive finite number")
        reference_score = float(reference_score)
        if not isfinite(reference_score) or reference_score <= 0:
            raise SimulatorError("reference_score must be a positive finite number")
        normalized: list[CalibrationRecord] = []
        for index, row in enumerate(records):
            normalized.append(row if isinstance(row, CalibrationRecord) else CalibrationRecord.from_mapping(row, index))
        if len(normalized) < 2:
            raise SimulatorError("at least two calibration records are required")
        residuals = [
            record.observed_seconds
            - record.cpu_seconds
            - record.gpu_seconds_at_reference * record.reference_score / reference_score
            for record in normalized
        ]
        fixed = float(median(residuals))
        if fixed < 0:
            raise SimulatorError("calibration residual implies a negative fixed phase")
        return cls(fixed, reference_score, tuple(record.run_id for record in normalized))

    def predict(self, record: CalibrationRecord | Mapping[str, Any], *, target_score: float) -> dict[str, Any]:
        normalized = record if isinstance(record, CalibrationRecord) else CalibrationRecord.from_mapping(record)
        if isinstance(target_score, bool) or not isinstance(target_score, (int, float)):
            raise SimulatorError("target_score must be a positive finite number")
        target_score = float(target_score)
        if not isfinite(target_score) or target_score <= 0:
            raise SimulatorError("target_score must be a positive finite number")
        predicted = (
            self.fixed_seconds
            + normalized.cpu_seconds
            + normalized.gpu_seconds_at_reference * self.reference_score / target_score
        )
        return {
            "schema_version": "simulator.prediction.v1",
            "provenance": "simulated",
            "run_id": normalized.run_id,
            "target_score": target_score,
            "reference_score": self.reference_score,
            "fixed_seconds": self.fixed_seconds,
            "predicted_seconds": predicted,
        }

    def evaluate(
        self,
        records: Iterable[CalibrationRecord | Mapping[str, Any]],
        *,
        target_score: float,
    ) -> dict[str, Any]:
        rows = [record if isinstance(record, CalibrationRecord) else CalibrationRecord.from_mapping(record, i) for i, record in enumerate(records)]
        if not rows:
            raise SimulatorError("at least one holdout record is required")
        predictions = [self.predict(record, target_score=target_score) for record in rows]
        errors = [prediction["predicted_seconds"] - record.observed_seconds for prediction, record in zip(predictions, rows)]
        absolute = [abs(error) for error in errors]
        percentages = [abs(error) / record.observed_seconds * 100.0 for error, record in zip(errors, rows) if record.observed_seconds > 0]
        return {
            "schema_version": "simulator.holdout-evaluation.v1",
            "provenance": "derived",
            "target_score": float(target_score),
            "training_run_ids": list(self.training_run_ids),
            "holdout_run_ids": [record.run_id for record in rows],
            "count": len(rows),
            "mean_absolute_error_seconds": sum(absolute) / len(absolute),
            "mean_absolute_percentage_error": (sum(percentages) / len(percentages)) if percentages else None,
            "predictions": [
                {**prediction, "observed_seconds": record.observed_seconds, "error_seconds": error}
                for prediction, record, error in zip(predictions, rows, errors)
            ],
        }


__all__ = ["CalibrationRecord", "HardwareLatencySimulator", "SimulatorError"]
