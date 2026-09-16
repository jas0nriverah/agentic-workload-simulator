#!/usr/bin/env python3
"""Crash-safe, leakage-safe prediction protocol for adaptive SWE-agent runs.

This module is deliberately separate from the static ``build_event_protocol``
and ``evaluate_predictions`` path.  A real SWE-agent trajectory is adaptive:
the next tool or model event is not known until the preceding event has
finished.  The protocol therefore freezes one prediction immediately before
each event, durably records it, and only then permits the corresponding label
to be revealed.

The module has no workload launcher and never opens a measured trace.  It is a
small journaling boundary that can be called by a reviewed runner.  Every
feature is parsed by the strict ``event_simulator`` schemas, while timing,
CPU/CUDA/Kineto data, output tokens, and response data are accepted only in a
separate label call.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from math import isclose, isfinite
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Any, Mapping, Protocol

import fcntl


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from agentic_sim.assignment.event_simulator import (  # noqa: E402
    EventSimulatorError,
    HardwareProfile,
    ModelEventInput,
    ToolEventInput,
    canonical_sha256,
)
from agentic_sim.assignment.sequential_simulator import (  # noqa: E402
    PriorEventSummary,
    SequentialLatencyModel,
)


ADAPTIVE_MODEL_SCHEMA = "assignment.adaptive-calibration-model.v1"
ADAPTIVE_PROTOCOL_SCHEMA = "assignment.adaptive-event-protocol.v1"
ADAPTIVE_ARM_SCHEMA = "assignment.adaptive-trajectory-arm.v1"
ADAPTIVE_PREDICTION_SCHEMA = "assignment.adaptive-event-prediction.v1"
ADAPTIVE_LABEL_SCHEMA = "assignment.adaptive-event-label.v1"
ADAPTIVE_MANIFEST_SCHEMA = "assignment.adaptive-event-prediction-manifest.v1"
ADAPTIVE_LABELS_SCHEMA = "assignment.event-holdout-labels.v1"
SCORE_SCHEMA = "assignment.adaptive-event-score.v1"
E2E_PREDICTION_SCHEMA = "assignment.adaptive-e2e-prediction.v1"
GATE_PERCENT = 25.0

_TARGET_KEYS = frozenset(
    {
        "observed_ms", "wall_ms", "wall_time_ms", "cpu_ms", "cuda_ms",
        "kineto_ms", "kineto_wall_ms", "kineto_cpu_ms", "kineto_cuda_ms",
        "cpu_activity_union_ms", "cuda_activity_union_ms",
        "kernel_duration_sum_ms", "start_mono_ns", "end_mono_ns",
        "output_tokens", "actual_output_tokens", "generated_tokens",
        "completion_tokens", "response_tokens", "response_bytes",
        "response_data", "response_body", "trace", "trace_summary",
        "official_resolved", "resolved", "evaluator", "evaluator_result",
        "future_event", "next_event_wall_ms", "current_output_tokens",
    }
)


class AdaptiveProtocolError(EventSimulatorError):
    """A fail-closed adaptive protocol violation."""


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha_file(path: Path) -> str:
    try:
        return _sha_bytes(path.read_bytes())
    except OSError as exc:
        raise AdaptiveProtocolError(f"cannot hash {path}: {exc}") from exc


def _sidecar(path: Path) -> Path:
    return path.with_suffix(".sha256")


def _sidecar_candidates(path: Path) -> list[Path]:
    return list(dict.fromkeys((Path(str(path) + ".sha256"), _sidecar(path))))


def _digest(value: Any) -> str:
    return _sha_bytes(_canonical_bytes(value))


def _require_sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise AdaptiveProtocolError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _positive(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AdaptiveProtocolError(f"{field} must be a positive finite number")
    result = float(value)
    if not isfinite(result) or result <= 0:
        raise AdaptiveProtocolError(f"{field} must be a positive finite number")
    return result


def _nonempty(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AdaptiveProtocolError(f"{field} must be a non-empty string")
    return value


def _write_frozen_json(path: Path, value: Mapping[str, Any]) -> str:
    """Write an object and exact sidecar once; identical re-entry is allowed."""
    payload = _canonical_bytes(value)
    digest = _sha_bytes(payload)
    sidecar_payload = f"{digest}  {path.name}\n".encode("utf-8")
    sidecar = _sidecar(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_sidecars = [candidate for candidate in _sidecar_candidates(path) if candidate.exists()]
    if path.exists() or existing_sidecars:
        if (
            path.exists()
            and len(existing_sidecars) == 1
            and existing_sidecars[0] == sidecar
            and path.read_bytes() == payload
            and sidecar.read_bytes() == sidecar_payload
        ):
            return digest
        raise AdaptiveProtocolError(f"refusing to overwrite frozen artifact: {path}")
    temporary: list[tuple[Path, Path]] = []
    try:
        for destination, contents in ((path, payload), (sidecar, sidecar_payload)):
            descriptor, name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=path.parent)
            candidate = Path(name)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(contents)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.append((candidate, destination))
        for candidate, destination in temporary:
            os.replace(candidate, destination)
    finally:
        for candidate, _destination in temporary:
            if candidate.exists():
                candidate.unlink()
    return digest


def _read_hashed_json(path: Path, *, kind: str) -> tuple[dict[str, Any], str]:
    if path.is_symlink() or not path.is_file():
        raise AdaptiveProtocolError(f"{kind} must be a regular file: {path}")
    try:
        payload = path.read_bytes()
        value = json.loads(payload)
    except (OSError, json.JSONDecodeError) as exc:
        raise AdaptiveProtocolError(f"cannot read {kind}: {exc}") from exc
    if not isinstance(value, dict):
        raise AdaptiveProtocolError(f"{kind} must be a JSON object")
    existing = [candidate for candidate in _sidecar_candidates(path) if candidate.exists()]
    if len(existing) != 1:
        raise AdaptiveProtocolError(f"{kind} requires exactly one recognized SHA-256 sidecar")
    try:
        sidecar = existing[0].read_text(encoding="utf-8")
    except OSError as exc:
        raise AdaptiveProtocolError(f"cannot read {kind} SHA-256 sidecar: {exc}") from exc
    digest = _sha_bytes(payload)
    if existing[0].is_symlink() or not existing[0].is_file() or sidecar != f"{digest}  {path.name}\n":
        raise AdaptiveProtocolError(f"{kind} or SHA-256 sidecar was tampered with")
    return value, digest


def _reject_nested_targets(value: Any, *, path: str = "feature") -> None:
    if isinstance(value, Mapping):
        leaked = sorted(set(value) & _TARGET_KEYS)
        if leaked:
            raise AdaptiveProtocolError(
                f"{path} contains measured or target-derived field(s): {', '.join(leaked)}"
            )
        for key, child in value.items():
            _reject_nested_targets(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_nested_targets(child, path=f"{path}[{index}]")


def _validate_model_block(block: Any, name: str) -> dict[str, Any]:
    if not isinstance(block, Mapping):
        raise AdaptiveProtocolError(f"calibration model {name} must be an object")
    if block.get("kind") == "sequential_lookup":
        required = {
            "kind",
            "extractor_id",
            "extractor_sha256",
            "sha_medians",
            "key_medians",
            "prefix_medians",
            "tool_name_medians",
            "class_medians",
            "global_tool_median",
            "model_intercept",
            "model_output_coef",
            "model_context_coef",
            "context_output_medians",
            "global_output_median",
            "e2e_scale",
            "coefficients",
        }
        if name == "tool_event" and not required.issubset(set(block)):
            raise AdaptiveProtocolError("sequential tool model is missing required tables")
        coefficients = block.get("coefficients")
        if not isinstance(coefficients, list) or not coefficients:
            raise AdaptiveProtocolError(f"calibration model {name} coefficients are invalid")
        return dict(block)
    coefficients = block.get("coefficients")
    if not isinstance(coefficients, list) or not coefficients or any(
        isinstance(x, bool) or not isinstance(x, (int, float)) or not isfinite(float(x))
        for x in coefficients
    ):
        raise AdaptiveProtocolError(f"calibration model {name} coefficients are invalid")
    return dict(block)


def freeze_calibration_model(
    model: Mapping[str, Any],
    output_path: Path,
    *,
    calibration_run_ids: list[str],
    split_manifest_sha256: str,
    runtime_manifest_sha256: str,
    hardware_profile_sha256: str,
    model_revision_sha256: str,
) -> str:
    """Freeze calibration-only model coefficients before any holdout call."""
    if not isinstance(model, Mapping):
        raise AdaptiveProtocolError("calibration model must be an object")
    _reject_nested_targets(model, path="calibration_model")
    if set(model) != {"tool_event", "model_event", "trajectory"}:
        raise AdaptiveProtocolError("calibration model must contain exactly three model blocks")
    if not isinstance(calibration_run_ids, list) or not calibration_run_ids or len(set(calibration_run_ids)) != len(calibration_run_ids):
        raise AdaptiveProtocolError("calibration_run_ids must be a non-empty unique list")
    artifact = {
        "schema_version": ADAPTIVE_MODEL_SCHEMA,
        "provenance": "calibration_only",
        "calibration_run_ids": sorted(calibration_run_ids),
        "bindings": {
            "split_manifest_sha256": _require_sha(split_manifest_sha256, "split_manifest_sha256"),
            "runtime_manifest_sha256": _require_sha(runtime_manifest_sha256, "runtime_manifest_sha256"),
            "hardware_profile_sha256": _require_sha(hardware_profile_sha256, "hardware_profile_sha256"),
            "model_revision_sha256": _require_sha(model_revision_sha256, "model_revision_sha256"),
        },
        "models": {name: _validate_model_block(model[name], name) for name in ("tool_event", "model_event", "trajectory")},
    }
    return _write_frozen_json(output_path, artifact)


@dataclass(frozen=True)
class FrozenCalibrationModel:
    artifact: dict[str, Any]
    sha256: str

    @classmethod
    def load(cls, path: Path) -> "FrozenCalibrationModel":
        artifact, digest = _read_hashed_json(path, kind="adaptive calibration model")
        if artifact.get("schema_version") != ADAPTIVE_MODEL_SCHEMA or artifact.get("provenance") != "calibration_only":
            raise AdaptiveProtocolError("unsupported or non-calibration adaptive model")
        if not isinstance(artifact.get("calibration_run_ids"), list) or not artifact["calibration_run_ids"]:
            raise AdaptiveProtocolError("adaptive calibration model has no calibration run IDs")
        bindings = artifact.get("bindings")
        if not isinstance(bindings, Mapping) or set(bindings) != {
            "split_manifest_sha256", "runtime_manifest_sha256", "hardware_profile_sha256", "model_revision_sha256"
        }:
            raise AdaptiveProtocolError("adaptive calibration model bindings are incomplete")
        for field in bindings:
            _require_sha(bindings[field], f"model bindings.{field}")
        models = artifact.get("models")
        if not isinstance(models, Mapping) or set(models) != {"tool_event", "model_event", "trajectory"}:
            raise AdaptiveProtocolError("adaptive calibration model blocks are incomplete")
        for name in models:
            _validate_model_block(models[name], name)
        return cls(artifact=artifact, sha256=digest)

    def bindings(self) -> dict[str, str]:
        return dict(self.artifact["bindings"])

    def _predict(self, name: str, design: tuple[float, ...]) -> float:
        block = self.artifact["models"][name]
        coefficients = tuple(float(x) for x in block["coefficients"])
        if len(coefficients) != len(design):
            raise AdaptiveProtocolError(f"frozen {name} model design width mismatch")
        value = max(0.0, sum(a * b for a, b in zip(coefficients, design)))
        return _positive(value, f"predicted {name} latency")

    def predict_tool(
        self,
        features: Mapping[str, Any] | ToolEventInput,
        prior: PriorEventSummary | None = None,
    ) -> float:
        row = features if isinstance(features, ToolEventInput) else ToolEventInput.from_mapping(features)
        if row.split != "holdout":
            raise AdaptiveProtocolError("adaptive event predictions require split=holdout")
        block = self.artifact["models"]["tool_event"]
        if block.get("kind") == "sequential_lookup":
            model = SequentialLatencyModel.from_mapping(block)
            mapping = row.to_mapping() if isinstance(features, ToolEventInput) else dict(features)
            return _positive(model.predict_tool_ms(mapping, prior), "predicted tool_event latency")
        return self._predict("tool_event", row.design_row())

    def predict_model(
        self,
        features: Mapping[str, Any] | ModelEventInput,
        prior: PriorEventSummary | None = None,
    ) -> float:
        row = features if isinstance(features, ModelEventInput) else ModelEventInput.from_mapping(features)
        if row.split != "holdout":
            raise AdaptiveProtocolError("adaptive event predictions require split=holdout")
        block = self.artifact["models"]["model_event"]
        tool_block = self.artifact["models"]["tool_event"]
        if tool_block.get("kind") == "sequential_lookup" or block.get("kind") == "sequential_lookup":
            source = tool_block if tool_block.get("kind") == "sequential_lookup" else block
            model = SequentialLatencyModel.from_mapping(source)
            mapping = row.to_mapping() if isinstance(features, ModelEventInput) else dict(features)
            return _positive(model.predict_model_ms(mapping, prior), "predicted model_event latency")
        return self._predict("model_event", row.design_row())

    def predict_trajectory(
        self,
        *,
        predicted_tool_ms: float,
        predicted_model_ms: float,
        tool_event_count: int,
        model_event_count: int,
    ) -> float:
        """Predict E2E latency from pre-trajectory event forecasts.

        The counts and phase totals are forecasts, not measured holdout
        labels.  Keeping this calculation on the frozen model makes the
        arm-time E2E artifact independently reproducible instead of allowing a
        caller to supply an arbitrary number.
        """
        tool_ms = _positive(predicted_tool_ms, "predicted_tool_ms")
        model_ms = _positive(predicted_model_ms, "predicted_model_ms")
        if isinstance(tool_event_count, bool) or not isinstance(tool_event_count, int) or tool_event_count <= 0:
            raise AdaptiveProtocolError("tool_event_count must be a positive integer")
        if isinstance(model_event_count, bool) or not isinstance(model_event_count, int) or model_event_count <= 0:
            raise AdaptiveProtocolError("model_event_count must be a positive integer")
        tool_block = self.artifact["models"]["tool_event"]
        if tool_block.get("kind") == "sequential_lookup":
            sequential = SequentialLatencyModel.from_mapping(tool_block)
            return sequential.predict_e2e_ms(tool_ms, model_ms)
        return self._predict(
            "trajectory",
            (
                1.0,
                tool_ms / 1000.0,
                model_ms / 1000.0,
                tool_event_count / 10.0,
                model_event_count / 10.0,
            ),
        )


def _trajectory_forecast(
    model: FrozenCalibrationModel,
    *,
    run_id: str,
    hardware: Mapping[str, Any],
    tool_events: list[Mapping[str, Any]],
    model_events: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build one strictly pre-execution E2E forecast from frozen models."""
    run_id = _nonempty(run_id, "run_id")
    profile = HardwareProfile.from_mapping(hardware)
    expected_hardware = profile.to_mapping()
    tools: list[ToolEventInput] = []
    models: list[ModelEventInput] = []
    for feature in tool_events:
        row = ToolEventInput.from_mapping(feature)
        if row.run_id != run_id or row.split != "holdout":
            raise AdaptiveProtocolError("E2E tool forecast features must be the armed holdout run")
        if row.hardware.to_mapping() != expected_hardware:
            raise AdaptiveProtocolError("E2E tool forecast hardware does not match the bound profile")
        tools.append(row)
    for feature in model_events:
        row = ModelEventInput.from_mapping(feature)
        if row.run_id != run_id or row.split != "holdout":
            raise AdaptiveProtocolError("E2E model forecast features must be the armed holdout run")
        if row.hardware.to_mapping() != expected_hardware:
            raise AdaptiveProtocolError("E2E model forecast hardware does not match the bound profile")
        models.append(row)
    if not tools or not models:
        raise AdaptiveProtocolError("E2E forecast requires both tool and model features")
    if len({row.event_id for row in tools}) != len(tools):
        raise AdaptiveProtocolError("E2E forecast contains duplicate tool event identifiers")
    if len({row.request_id for row in models}) != len(models):
        raise AdaptiveProtocolError("E2E forecast contains duplicate model request identifiers")
    predicted_tool_ms = sum(model.predict_tool(row) for row in tools)
    predicted_model_ms = sum(model.predict_model(row) for row in models)
    predicted_ms = model.predict_trajectory(
        predicted_tool_ms=predicted_tool_ms,
        predicted_model_ms=predicted_model_ms,
        tool_event_count=len(tools),
        model_event_count=len(models),
    )
    return {
        "schema_version": E2E_PREDICTION_SCHEMA,
        "provenance": "calibration_only_pre_trajectory",
        "run_id": run_id,
        "split": "holdout",
        "calibration_model_sha256": model.sha256,
        "bindings": model.bindings(),
        "hardware": expected_hardware,
        "tool_events": [row.to_mapping() for row in tools],
        "model_events": [row.to_mapping() for row in models],
        "tool_event_count": len(tools),
        "model_event_count": len(models),
        "predicted_tool_ms": predicted_tool_ms,
        "predicted_model_ms": predicted_model_ms,
        "predicted_ms": predicted_ms,
    }


def freeze_trajectory_prediction(
    model: FrozenCalibrationModel,
    *,
    run_id: str,
    hardware: Mapping[str, Any],
    tool_events: list[Mapping[str, Any]],
    model_events: list[Mapping[str, Any]],
    output_path: Path,
) -> str:
    """Freeze a hash-bound, calibration-derived trajectory forecast."""
    artifact = _trajectory_forecast(
        model,
        run_id=run_id,
        hardware=hardware,
        tool_events=tool_events,
        model_events=model_events,
    )
    return _write_frozen_json(output_path, artifact)


def verify_trajectory_prediction(
    path: Path,
    model: FrozenCalibrationModel,
    *,
    run_id: str,
    hardware: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    """Verify and recompute a pre-trajectory forecast before it is armed."""
    artifact, digest = _read_hashed_json(path, kind="adaptive E2E prediction")
    required = {
        "schema_version",
        "provenance",
        "run_id",
        "split",
        "calibration_model_sha256",
        "bindings",
        "hardware",
        "tool_events",
        "model_events",
        "tool_event_count",
        "model_event_count",
        "predicted_tool_ms",
        "predicted_model_ms",
        "predicted_ms",
    }
    if set(artifact) != required or artifact.get("schema_version") != E2E_PREDICTION_SCHEMA:
        raise AdaptiveProtocolError("adaptive E2E prediction artifact schema is invalid")
    if artifact.get("provenance") != "calibration_only_pre_trajectory":
        raise AdaptiveProtocolError("adaptive E2E prediction is not calibration-only pre-trajectory evidence")
    if artifact.get("run_id") != run_id or artifact.get("split") != "holdout":
        raise AdaptiveProtocolError("adaptive E2E prediction is not bound to this holdout run")
    if artifact.get("calibration_model_sha256") != model.sha256:
        raise AdaptiveProtocolError("adaptive E2E prediction model hash does not match the frozen model")
    if artifact.get("bindings") != model.bindings():
        raise AdaptiveProtocolError("adaptive E2E prediction bindings do not match the frozen model")
    profile = HardwareProfile.from_mapping(hardware)
    if artifact.get("hardware") != profile.to_mapping():
        raise AdaptiveProtocolError("adaptive E2E prediction hardware does not match the runtime profile")
    tool_events = artifact.get("tool_events")
    model_events = artifact.get("model_events")
    if not isinstance(tool_events, list) or not all(isinstance(row, Mapping) for row in tool_events):
        raise AdaptiveProtocolError("adaptive E2E tool forecast features are invalid")
    if not isinstance(model_events, list) or not all(isinstance(row, Mapping) for row in model_events):
        raise AdaptiveProtocolError("adaptive E2E model forecast features are invalid")
    expected = _trajectory_forecast(
        model,
        run_id=run_id,
        hardware=hardware,
        tool_events=tool_events,
        model_events=model_events,
    )
    for field in (
        "tool_event_count",
        "model_event_count",
        "predicted_tool_ms",
        "predicted_model_ms",
        "predicted_ms",
    ):
        actual = artifact.get(field)
        recalculated = expected[field]
        if field.endswith("count"):
            if isinstance(actual, bool) or not isinstance(actual, int) or actual != recalculated:
                raise AdaptiveProtocolError(f"adaptive E2E prediction {field} is not reproducible")
        else:
            if isinstance(actual, bool) or not isinstance(actual, (int, float)):
                raise AdaptiveProtocolError(f"adaptive E2E prediction {field} is invalid")
            if not isfinite(float(actual)) or not isclose(
                float(actual),
                float(recalculated),
                rel_tol=1e-12,
                abs_tol=1e-9,
            ):
                raise AdaptiveProtocolError(f"adaptive E2E prediction {field} is not reproducible")
    return artifact, digest


class Clock(Protocol):
    def witness(self) -> dict[str, Any]: ...


class SystemClock:
    def witness(self) -> dict[str, Any]:
        boot_id: str | None = None
        try:
            candidate = Path("/proc/sys/kernel/random/boot_id")
            if candidate.is_file():
                boot_id = candidate.read_text(encoding="utf-8").strip() or None
        except OSError:
            pass
        clock_id = "CLOCK_MONOTONIC"
        monotonic_ns = time.monotonic_ns()
        for candidate_name in ("CLOCK_MONOTONIC_RAW", "CLOCK_MONOTONIC"):
            candidate = getattr(time, candidate_name, None)
            if candidate is not None and hasattr(time, "clock_gettime_ns"):
                clock_id = candidate_name
                monotonic_ns = time.clock_gettime_ns(candidate)
                break
        return {
            "captured_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "clock_id": clock_id,
            "monotonic_ns": monotonic_ns,
            "boot_id": boot_id,
        }


def _record_digest(record: Mapping[str, Any]) -> str:
    unsigned = {key: value for key, value in record.items() if key != "record_sha256"}
    return _digest(unsigned)


def _append_durable(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # The journal is JSONL, so each durable record must occupy exactly one
    # physical line.  Hashing still uses the pretty, canonical representation
    # above; the on-disk line is independently deterministic.
    payload = json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"
    with path.open("a", encoding="utf-8") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _label_mapping(label: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(label, Mapping):
        raise AdaptiveProtocolError("event label must be an object")
    allowed = {
        "schema_version", "status", "observed_ms", "cpu_ms", "cuda_ms", "kineto_ms",
        "kernel_duration_sum_ms", "output_tokens", "response_sha256", "unavailable_reason",
    }
    unknown = sorted(set(label) - allowed)
    if unknown:
        raise AdaptiveProtocolError("unknown event label field(s): " + ", ".join(unknown))
    if label.get("schema_version") not in (None, ADAPTIVE_LABEL_SCHEMA):
        raise AdaptiveProtocolError("unsupported adaptive event label schema")
    status = label.get("status", "completed")
    if status not in {"completed", "unavailable"}:
        raise AdaptiveProtocolError("event label status must be completed or unavailable")
    result = dict(label)
    result["schema_version"] = ADAPTIVE_LABEL_SCHEMA
    result["status"] = status
    if status == "unavailable":
        if not isinstance(result.get("unavailable_reason"), str) or not result["unavailable_reason"].strip():
            raise AdaptiveProtocolError("unavailable event labels require a reason")
        if result.get("observed_ms") not in (None, ""):
            raise AdaptiveProtocolError("unavailable event labels cannot carry observed_ms")
    else:
        result["observed_ms"] = _positive(result.get("observed_ms"), "observed_ms")
        if result.get("unavailable_reason") not in (None, ""):
            raise AdaptiveProtocolError("completed event labels cannot carry unavailable_reason")
    for field in ("cpu_ms", "cuda_ms", "kineto_ms", "kernel_duration_sum_ms"):
        if field in result and result[field] is not None:
            result[field] = _positive(result[field], field)
    if "output_tokens" in result and result["output_tokens"] is not None:
        if isinstance(result["output_tokens"], bool) or not isinstance(result["output_tokens"], int) or result["output_tokens"] < 0:
            raise AdaptiveProtocolError("output_tokens must be a non-negative integer label")
    if "response_sha256" in result and result["response_sha256"] is not None:
        _require_sha(result["response_sha256"], "response_sha256")
    return result


def _read_mapping(path: Path, *, kind: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdaptiveProtocolError(f"cannot read {kind}: {exc}") from exc
    if not isinstance(value, dict):
        raise AdaptiveProtocolError(f"{kind} must be a JSON object")
    return value


@dataclass
class AdaptiveEventProtocol:
    """One adaptive holdout trajectory with append-only crash-safe state."""

    root: Path
    calibration_model: FrozenCalibrationModel
    split_manifest_sha256: str
    runtime_manifest_sha256: str
    hardware_profile_sha256: str
    model_revision_sha256: str
    clock: Clock | None = None

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.clock = self.clock or SystemClock()
        for name, value in (
            ("split_manifest_sha256", self.split_manifest_sha256),
            ("runtime_manifest_sha256", self.runtime_manifest_sha256),
            ("hardware_profile_sha256", self.hardware_profile_sha256),
            ("model_revision_sha256", self.model_revision_sha256),
        ):
            _require_sha(value, name)
        expected = self.calibration_model.bindings()
        actual = {
            "split_manifest_sha256": self.split_manifest_sha256,
            "runtime_manifest_sha256": self.runtime_manifest_sha256,
            "hardware_profile_sha256": self.hardware_profile_sha256,
            "model_revision_sha256": self.model_revision_sha256,
        }
        if expected != actual:
            raise AdaptiveProtocolError("calibration model bindings do not match adaptive protocol")
        self.journal_path = self.root / "adaptive_events.jsonl"
        # The proxy and the SWE-agent hook are distinct processes.  They share
        # this lock for *all* protocol reads, validation, and journal appends;
        # otherwise each process can make a decision from a stale in-memory
        # journal snapshot and violate prediction -> label ordering.
        self.lock_path = self.root / "adaptive_events.lock"
        self.arm_path = self.root / "trajectory_arm.json"
        self.manifest_path = self.root / "adaptive_prediction_manifest.json"
        self._records: list[dict[str, Any]] = []
        self._lock_depth = 0
        with self._journal_transaction():
            pass

    @property
    def binding(self) -> dict[str, str]:
        return {
            "split_manifest_sha256": self.split_manifest_sha256,
            "runtime_manifest_sha256": self.runtime_manifest_sha256,
            "hardware_profile_sha256": self.hardware_profile_sha256,
            "model_revision_sha256": self.model_revision_sha256,
            "calibration_model_sha256": self.calibration_model.sha256,
        }

    def _witness(self) -> dict[str, Any]:
        witness = self.clock.witness()
        if not isinstance(witness, Mapping):
            raise AdaptiveProtocolError("clock witness must be an object")
        try:
            monotonic = int(witness["monotonic_ns"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AdaptiveProtocolError("clock witness requires monotonic_ns") from exc
        if monotonic < 0:
            raise AdaptiveProtocolError("clock monotonic_ns must be non-negative")
        return {
            "captured_at_utc": _nonempty(witness.get("captured_at_utc"), "captured_at_utc"),
            "clock_id": _nonempty(witness.get("clock_id"), "clock_id"),
            "monotonic_ns": monotonic,
            "boot_id": witness.get("boot_id"),
        }

    @contextmanager
    def _journal_transaction(self):
        """Serialize a complete refresh/validate/decision/append operation.

        ``flock`` is advisory, which is the correct POSIX primitive for the
        repository-owned proxy and hook processes.  Both use this protocol
        module, so every operation sees the journal committed by the other
        process before it validates ordering or assigns an ordinal.
        """
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock_stream:
            fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX)
            self._lock_depth += 1
            try:
                self._records = self._load_journal()
                yield
            finally:
                self._lock_depth -= 1
                fcntl.flock(lock_stream.fileno(), fcntl.LOCK_UN)

    def _require_journal_lock(self) -> None:
        if self._lock_depth <= 0:
            raise AdaptiveProtocolError("adaptive journal access requires the protocol file lock")

    def _load_journal(self) -> list[dict[str, Any]]:
        if not self.journal_path.exists():
            return []
        try:
            lines = self.journal_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise AdaptiveProtocolError(f"cannot read adaptive journal: {exc}") from exc
        records: list[dict[str, Any]] = []
        previous_hash = ""
        previous_mono = -1
        boot_id: Any = None
        clock_id: Any = None
        pending: dict[str, Any] | None = None
        next_event = 0
        trajectory_labels = 0
        for line_number, line in enumerate(lines, 1):
            if not line.strip():
                raise AdaptiveProtocolError(f"adaptive journal has blank line {line_number}")
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AdaptiveProtocolError(f"adaptive journal line {line_number} is invalid JSON") from exc
            if not isinstance(record, dict) or record.get("schema_version") != ADAPTIVE_PROTOCOL_SCHEMA:
                raise AdaptiveProtocolError(f"adaptive journal line {line_number} has unsupported schema")
            if record.get("journal_ordinal") != len(records):
                raise AdaptiveProtocolError("adaptive journal has duplicate or reordered journal ordinal")
            if record.get("chain_prev_sha256") != previous_hash:
                raise AdaptiveProtocolError("adaptive journal hash chain is broken")
            if record.get("record_sha256") != _record_digest(record):
                raise AdaptiveProtocolError("adaptive journal record hash is invalid")
            witness = record.get("clock")
            if not isinstance(witness, Mapping) or not isinstance(witness.get("monotonic_ns"), int):
                raise AdaptiveProtocolError("adaptive journal record clock witness is invalid")
            if not isinstance(witness.get("clock_id"), str) or not witness["clock_id"]:
                raise AdaptiveProtocolError("adaptive journal record clock identity is invalid")
            if witness["monotonic_ns"] <= previous_mono:
                raise AdaptiveProtocolError("adaptive journal chronology is not strictly monotonic")
            if records and witness.get("boot_id") != boot_id:
                raise AdaptiveProtocolError("adaptive journal boot_id changed during a trajectory")
            if records and witness.get("clock_id") != clock_id:
                raise AdaptiveProtocolError("adaptive journal clock_id changed during a trajectory")
            boot_id = witness.get("boot_id")
            clock_id = witness.get("clock_id")
            previous_mono = witness["monotonic_ns"]
            record_binding = record.get("binding")
            if record_binding != self.binding:
                raise AdaptiveProtocolError("adaptive journal record binding changed")
            record_type = record.get("record_type")
            identifier = record.get("identifier")
            if trajectory_labels and record_type in {"event_prediction", "event_label"}:
                raise AdaptiveProtocolError("event records cannot be appended after trajectory E2E reveal")
            if record_type == "event_prediction":
                if pending is not None:
                    raise AdaptiveProtocolError("adaptive journal predicts a new event before revealing the prior label")
                if record.get("event_ordinal") != next_event:
                    raise AdaptiveProtocolError("adaptive event ordinals are duplicate or reordered")
                if not isinstance(identifier, str) or not identifier:
                    raise AdaptiveProtocolError("event prediction identifier is missing")
                if any(row.get("identifier") == identifier for row in records if row.get("record_type") == "event_prediction"):
                    raise AdaptiveProtocolError("duplicate adaptive event prediction identifier")
                pending = record
                next_event += 1
            elif record_type == "event_label":
                if pending is None or identifier != pending.get("identifier"):
                    raise AdaptiveProtocolError("event label is not immediately preceded by its durable prediction")
                if record.get("prediction_record_sha256") != pending.get("record_sha256"):
                    raise AdaptiveProtocolError("event label is not bound to its prediction record")
                pending = None
            elif record_type == "trajectory_label":
                if pending is not None or trajectory_labels:
                    raise AdaptiveProtocolError("trajectory label is duplicated or revealed before event labels")
                trajectory_labels += 1
            else:
                raise AdaptiveProtocolError("adaptive journal contains an unknown record type")
            records.append(record)
            if record.get("record_type") == "event_prediction":
                cited = record.get("prior_label_sha256s") or []
                if not isinstance(cited, list):
                    raise AdaptiveProtocolError("prediction prior_label_sha256s must be a list")
                known_labels = {
                    item["record_sha256"]
                    for item in records
                    if item.get("record_type") == "event_label"
                }
                unknown = [digest for digest in cited if digest not in known_labels]
                if unknown:
                    raise AdaptiveProtocolError(
                        "prediction cites a label that was not revealed before this event"
                    )
            previous_hash = record["record_sha256"]
        return records

    def _arm(self) -> dict[str, Any]:
        if not self.arm_path.exists():
            raise AdaptiveProtocolError("trajectory is not armed")
        arm, digest = _read_hashed_json(self.arm_path, kind="trajectory arm")
        if arm.get("schema_version") != ADAPTIVE_ARM_SCHEMA or arm.get("binding") != self.binding:
            raise AdaptiveProtocolError("trajectory arm is not bound to this calibration/runtime")
        arm["arm_sha256"] = digest
        return arm

    def arm_trajectory(
        self,
        run_id: str,
        *,
        predicted_e2e_ms: float | None = None,
        e2e_prediction_method: str | None = None,
        e2e_prediction_artifact_path: str | None = None,
        e2e_prediction_artifact_sha256: str | None = None,
        calibration_model_path: str | None = None,
        hardware_profile_path: str | None = None,
    ) -> dict[str, Any]:
        """Freeze arm-time provenance and an E2E prediction/method before events."""
        with self._journal_transaction():
            _nonempty(run_id, "run_id")
            if (predicted_e2e_ms is None) == (e2e_prediction_method is None):
                raise AdaptiveProtocolError("provide exactly one pre-trajectory E2E prediction or declared method")
            artifact_fields = (
                e2e_prediction_artifact_path,
                e2e_prediction_artifact_sha256,
                calibration_model_path,
                hardware_profile_path,
            )
            if any(value is not None for value in artifact_fields) and not all(
                value is not None for value in artifact_fields
            ):
                raise AdaptiveProtocolError(
                    "E2E prediction provenance requires artifact, model, and hardware paths"
                )
            if any(value is not None for value in artifact_fields) and predicted_e2e_ms is None:
                raise AdaptiveProtocolError(
                    "E2E prediction provenance requires a numeric predicted_e2e_ms"
                )
            pre = (
                {"predicted_ms": _positive(predicted_e2e_ms, "predicted_e2e_ms")}
                if predicted_e2e_ms is not None
                else {"method": _nonempty(e2e_prediction_method, "e2e_prediction_method")}
            )
            if all(value is not None for value in artifact_fields):
                pre.update(
                    {
                        "prediction_artifact_path": _nonempty(
                            e2e_prediction_artifact_path,
                            "e2e_prediction_artifact_path",
                        ),
                        "prediction_artifact_sha256": _require_sha(
                            e2e_prediction_artifact_sha256,
                            "e2e_prediction_artifact_sha256",
                        ),
                        "calibration_model_path": _nonempty(
                            calibration_model_path,
                            "calibration_model_path",
                        ),
                        "hardware_profile_path": _nonempty(
                            hardware_profile_path,
                            "hardware_profile_path",
                        ),
                    }
                )
            if self.arm_path.exists():
                arm = self._arm()
                expected = dict(arm)
                expected.pop("arm_sha256", None)
                candidate = {
                    "schema_version": ADAPTIVE_ARM_SCHEMA,
                    "protocol_schema": ADAPTIVE_PROTOCOL_SCHEMA,
                    "run_id": run_id,
                    "split": "holdout",
                    "binding": self.binding,
                    "pre_trajectory_e2e": pre,
                    "labels_accessed": False,
                }
                if expected != candidate:
                    raise AdaptiveProtocolError("trajectory arm already exists with different immutable inputs")
                return arm
            if self._records:
                raise AdaptiveProtocolError("adaptive journal exists without its immutable trajectory arm")
            arm = {
                "schema_version": ADAPTIVE_ARM_SCHEMA,
                "protocol_schema": ADAPTIVE_PROTOCOL_SCHEMA,
                "run_id": run_id,
                "split": "holdout",
                "binding": self.binding,
                "pre_trajectory_e2e": pre,
                "labels_accessed": False,
            }
            _write_frozen_json(self.arm_path, arm)
            return self._arm()

    def _pending(self) -> dict[str, Any] | None:
        pending: dict[str, Any] | None = None
        for record in self._records:
            if record["record_type"] == "event_prediction":
                pending = record
            elif record["record_type"] == "event_label":
                pending = None
        return pending

    def _append(self, record: dict[str, Any]) -> dict[str, Any]:
        self._require_journal_lock()
        previous = self._records[-1]["record_sha256"] if self._records else ""
        record["schema_version"] = ADAPTIVE_PROTOCOL_SCHEMA
        record["journal_ordinal"] = len(self._records)
        record["chain_prev_sha256"] = previous
        record["binding"] = self.binding
        record["clock"] = self._witness()
        record["record_sha256"] = _record_digest(record)
        _append_durable(self.journal_path, record)
        self._records.append(record)
        return dict(record)

    def _prior_from_journal(self) -> PriorEventSummary:
        self._require_journal_lock()
        sha_walls: dict[str, list[float]] = {}
        name_walls: dict[tuple[str, str], list[float]] = {}
        outputs: list[float] = []
        observed: list[float] = []
        cited: list[str] = []
        predictions = {
            record["identifier"]: record
            for record in self._records
            if record.get("record_type") == "event_prediction"
        }
        for record in self._records:
            if record.get("record_type") != "event_label":
                continue
            cited.append(record["record_sha256"])
            label = record.get("label") or {}
            observed_ms = label.get("observed_ms")
            if isinstance(observed_ms, (int, float)) and not isinstance(observed_ms, bool):
                observed.append(float(observed_ms))
            prediction = predictions.get(record.get("identifier"))
            feature = (prediction or {}).get("feature") or {}
            if record.get("kind") == "tool" and isinstance(observed_ms, (int, float)) and not isinstance(observed_ms, bool):
                digest = str(feature.get("command_sha256") or "")
                if digest:
                    sha_walls.setdefault(digest, []).append(float(observed_ms))
                cls = str(feature.get("operation_class") or "")
                name = str(feature.get("tool_name") or "")
                if cls and name:
                    name_walls.setdefault((cls, name), []).append(float(observed_ms))
            if record.get("kind") == "model":
                tokens = label.get("output_tokens")
                if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0:
                    outputs.append(float(tokens))
        return PriorEventSummary(
            prior_event_count=len(cited),
            prior_median_output_tokens=float(sum(outputs) / len(outputs)) if outputs else 0.0,
            prior_median_observed_ms=float(sum(observed) / len(observed)) if observed else 0.0,
            prior_label_sha256s=tuple(cited),
            prior_tool_sha_medians=tuple(
                (key, sorted(values)[len(values) // 2]) for key, values in sha_walls.items()
            ),
            prior_tool_name_medians=tuple(
                (cls, name, sorted(values)[len(values) // 2])
                for (cls, name), values in name_walls.items()
            ),
            last_output_tokens=tuple(outputs),
        )

    def predict_event(
        self,
        kind: str,
        features: Mapping[str, Any] | ToolEventInput | ModelEventInput,
    ) -> dict[str, Any]:
        """Durably freeze one pre-event prediction; no label is accepted here."""
        with self._journal_transaction():
            arm = self._arm()
            if arm.get("labels_accessed") is True or any(r.get("record_type") == "trajectory_label" for r in self._records):
                raise AdaptiveProtocolError("trajectory labels have already been revealed")
            if kind not in {"tool", "model"}:
                raise AdaptiveProtocolError("event kind must be tool or model")
            if isinstance(features, Mapping):
                # Classify a measured field as leakage before schema parsing, even
                # when the field is also unknown to the strict event schema.
                _reject_nested_targets(features)
                leaked_prior = [key for key in ("prior_label_sha256s", "prior_median_observed_ms") if key in features]
                if leaked_prior:
                    raise AdaptiveProtocolError(
                        "caller may not supply prior-event labels; the journal injects them"
                    )
            prior = self._prior_from_journal()
            try:
                if kind == "tool":
                    row = features if isinstance(features, ToolEventInput) else ToolEventInput.from_mapping(features)
                    predicted = self.calibration_model.predict_tool(row, prior)
                    identifier = row.event_id
                    feature_mapping = row.to_mapping()
                else:
                    row = features if isinstance(features, ModelEventInput) else ModelEventInput.from_mapping(features)
                    predicted = self.calibration_model.predict_model(row, prior)
                    identifier = row.request_id
                    feature_mapping = row.to_mapping()
                    feature_mapping["prior_event_count"] = prior.prior_event_count
                    feature_mapping["prior_median_output_tokens"] = prior.prior_median_output_tokens
                    feature_mapping["prior_median_observed_ms"] = prior.prior_median_observed_ms
                    feature_mapping["prior_label_sha256s"] = list(prior.prior_label_sha256s)
            except EventSimulatorError as exc:
                raise AdaptiveProtocolError(str(exc)) from exc
            if row.run_id != arm["run_id"] or row.split != "holdout":
                raise AdaptiveProtocolError("adaptive event is not the armed holdout trajectory")
            _reject_nested_targets(feature_mapping)
            existing = next((r for r in self._records if r.get("record_type") == "event_prediction" and r.get("identifier") == identifier), None)
            if existing is not None:
                if existing.get("kind") != kind or existing.get("feature_sha256") != canonical_sha256(feature_mapping):
                    raise AdaptiveProtocolError("event identifier was reused with different features")
                return dict(existing)
            pending = self._pending()
            if pending is not None:
                raise AdaptiveProtocolError("resume has an unrevealed prediction; reveal it before predicting another event")
            return self._append(
                {
                    "record_type": "event_prediction",
                    "kind": kind,
                    "identifier": identifier,
                    "run_id": row.run_id,
                    "event_ordinal": sum(r.get("record_type") == "event_prediction" for r in self._records),
                    "feature": feature_mapping,
                    "feature_sha256": canonical_sha256(feature_mapping),
                    "prior_label_sha256s": list(prior.prior_label_sha256s),
                    "prediction": {"predicted_ms": predicted},
                }
            )

    def reveal_event_label(self, kind: str, identifier: str, label: Mapping[str, Any]) -> dict[str, Any]:
        """Append a label only after the matching prediction is durable."""
        with self._journal_transaction():
            arm = self._arm()
            if arm.get("labels_accessed") is True or any(r.get("record_type") == "trajectory_label" for r in self._records):
                raise AdaptiveProtocolError("trajectory labels have already been revealed")
            pending = self._pending()
            if pending is None or pending.get("kind") != kind or pending.get("identifier") != identifier:
                raise AdaptiveProtocolError("event label requires its durable immediately preceding prediction")
            normalized = _label_mapping(label)
            return self._append(
                {
                    "record_type": "event_label",
                    "kind": kind,
                    "identifier": identifier,
                    "run_id": arm["run_id"],
                    "event_ordinal": pending["event_ordinal"],
                    "prediction_record_sha256": pending["record_sha256"],
                    "label": normalized,
                }
            )

    def freeze_prediction_manifest(self) -> str:
        """Freeze all event predictions before the trajectory E2E label is read."""
        with self._journal_transaction():
            arm = self._arm()
            if self._pending() is not None:
                raise AdaptiveProtocolError("cannot freeze trajectory predictions with an unrevealed event label")
            if any(r.get("record_type") == "trajectory_label" for r in self._records):
                raise AdaptiveProtocolError("trajectory E2E label was already revealed")
            predictions = [r for r in self._records if r.get("record_type") == "event_prediction"]
            if not predictions:
                raise AdaptiveProtocolError("cannot freeze an empty adaptive prediction manifest")
            manifest = {
                "schema_version": ADAPTIVE_MANIFEST_SCHEMA,
                "protocol_schema": ADAPTIVE_PROTOCOL_SCHEMA,
                "provenance": "calibration_only_adaptive_pre_event",
                "frozen_before_trajectory_e2e_label": True,
                "binding": self.binding,
                "arm_sha256": arm["arm_sha256"],
                "run_id": arm["run_id"],
                "pre_trajectory_e2e": arm["pre_trajectory_e2e"],
                "event_predictions": [
                    {
                        "kind": row["kind"],
                        "identifier": row["identifier"],
                        "run_id": row["run_id"],
                        "event_ordinal": row["event_ordinal"],
                        "predicted_ms": row["prediction"]["predicted_ms"],
                        "feature_sha256": row["feature_sha256"],
                        "prediction_record_sha256": row["record_sha256"],
                    }
                    for row in predictions
                ],
                "trajectory_predictions": [
                    {
                        "run_id": arm["run_id"],
                        **arm["pre_trajectory_e2e"],
                    }
                ],
                "event_count": len(predictions),
                "labels_accessed": False,
            }
            digest = _write_frozen_json(self.manifest_path, manifest)
            self._verify_manifest(digest)
            return digest

    def _verify_manifest(self, expected_digest: str | None = None) -> tuple[dict[str, Any], str]:
        manifest, digest = _read_hashed_json(self.manifest_path, kind="adaptive prediction manifest")
        if expected_digest is not None and digest != expected_digest:
            raise AdaptiveProtocolError("adaptive prediction manifest changed during verification")
        if manifest.get("schema_version") != ADAPTIVE_MANIFEST_SCHEMA or manifest.get("binding") != self.binding:
            raise AdaptiveProtocolError("adaptive prediction manifest binding is invalid")
        if manifest.get("frozen_before_trajectory_e2e_label") is not True:
            raise AdaptiveProtocolError("adaptive prediction manifest was not frozen before E2E reveal")
        return manifest, digest

    def reveal_trajectory_label(self, observed_e2e_ms: float) -> dict[str, Any]:
        """Reveal E2E only after the immutable prediction manifest exists."""
        with self._journal_transaction():
            arm = self._arm()
            if self._pending() is not None:
                raise AdaptiveProtocolError("cannot reveal trajectory E2E while an event label is pending")
            _manifest, manifest_digest = self._verify_manifest()
            if any(r.get("record_type") == "trajectory_label" for r in self._records):
                raise AdaptiveProtocolError("trajectory E2E label was already revealed")
            record = self._append(
                {
                    "record_type": "trajectory_label",
                    "kind": "trajectory",
                    "identifier": arm["run_id"],
                    "run_id": arm["run_id"],
                    "event_ordinal": None,
                    "prediction_manifest_sha256": manifest_digest,
                    "label": {
                        "schema_version": "assignment.trajectory-holdout-label.v1",
                        "status": "completed",
                        "observed_ms": _positive(observed_e2e_ms, "observed_e2e_ms"),
                    },
                }
            )
            return record

    def build_labels_artifact(self, *, evaluator_bindings: Mapping[str, str] | None = None) -> dict[str, Any]:
        """Emit the existing evaluator's labels-root shape after all reveals.

        ``evaluator_bindings`` is optional for the native adaptive score, but
        required when the artifact is intended for the legacy evaluator.  The
        caller must supply real verified hashes; this method never invents or
        aliases static-protocol hashes.
        """
        with self._journal_transaction():
            manifest, manifest_digest = self._verify_manifest()
            trajectory_label = next((r for r in self._records if r.get("record_type") == "trajectory_label"), None)
            if trajectory_label is None:
                raise AdaptiveProtocolError("trajectory E2E label has not been revealed")
            prediction_by_id = {r["identifier"]: r for r in self._records if r["record_type"] == "event_prediction"}
            labels_by_id = {r["identifier"]: r for r in self._records if r["record_type"] == "event_label"}
            if set(prediction_by_id) != set(labels_by_id):
                raise AdaptiveProtocolError("labels artifact requires one label for every event prediction")
            bindings = {
                "prepare_receipt_sha256": None,
                "calibration_sha256": self.calibration_model.sha256,
                "holdout_features_sha256": None,
                "capture_receipt_sha256": None,
                "captured_features_sha256": None,
                "runtime_manifest_sha256": self.runtime_manifest_sha256,
                "hardware_profile_sha256": self.hardware_profile_sha256,
            }
            if evaluator_bindings is not None:
                allowed = set(bindings)
                if set(evaluator_bindings) != allowed:
                    raise AdaptiveProtocolError("evaluator_bindings must contain the exact legacy evaluator binding fields")
                for field, value in evaluator_bindings.items():
                    bindings[field] = _require_sha(value, field)
            elif any(value is None for value in bindings.values()):
                raise AdaptiveProtocolError("legacy evaluator bindings are required to emit a compatible labels artifact")
            root = {
                "schema_version": ADAPTIVE_LABELS_SCHEMA,
                "prediction_manifest_sha256": manifest_digest,
                **bindings,
                "tool_events": [],
                "model_events": [],
                "trajectories": [{
                    "schema_version": "assignment.trajectory-holdout-label.v1",
                    "run_id": manifest["run_id"],
                    "status": "completed",
                    "observed_ms": trajectory_label["label"]["observed_ms"],
                }],
            }
            for identifier in sorted(prediction_by_id):
                prediction = prediction_by_id[identifier]
                label = labels_by_id[identifier]["label"]
                row = {
                    "schema_version": (
                        "assignment.tool-holdout-label.v1" if prediction["kind"] == "tool"
                        else "assignment.model-holdout-label.v1"
                    ),
                    "event_id" if prediction["kind"] == "tool" else "request_id": identifier,
                    "run_id": manifest["run_id"],
                    "status": label["status"],
                    "observed_ms": label.get("observed_ms"),
                }
                if label["status"] == "unavailable":
                    row["unavailable_reason"] = label["unavailable_reason"]
                root["tool_events" if prediction["kind"] == "tool" else "model_events"].append(row)
            return root

    def score(self) -> dict[str, Any]:
        """Score all revealed event labels and the E2E label at the 25% gate."""
        with self._journal_transaction():
            return self._score_locked()

    def _score_locked(self) -> dict[str, Any]:
        """Score a journal snapshot while ``_journal_transaction`` is held."""
        self._require_journal_lock()
        manifest, manifest_digest = self._verify_manifest()
        labels = {r["identifier"]: r for r in self._records if r.get("record_type") == "event_label"}
        predictions = {r["identifier"]: r for r in self._records if r.get("record_type") == "event_prediction"}
        trajectory = next((r for r in self._records if r.get("record_type") == "trajectory_label"), None)
        if trajectory is None:
            raise AdaptiveProtocolError("cannot score before E2E label reveal")
        if set(labels) != set(predictions):
            raise AdaptiveProtocolError("cannot score with incomplete event-label coverage")
        event_scores: list[dict[str, Any]] = []
        unavailable = 0
        for identifier in sorted(predictions):
            label = labels[identifier]["label"]
            if label["status"] == "unavailable":
                unavailable += 1
                event_scores.append({"kind": predictions[identifier]["kind"], "identifier": identifier, "status": "unavailable"})
                continue
            predicted = _positive(predictions[identifier]["prediction"]["predicted_ms"], "predicted_ms")
            observed = _positive(label["observed_ms"], "observed_ms")
            ape = abs(predicted - observed) / observed * 100.0
            event_scores.append({
                "kind": predictions[identifier]["kind"],
                "identifier": identifier,
                "status": "completed",
                "predicted_ms": predicted,
                "observed_ms": observed,
                "absolute_percentage_error": ape,
                "within_25_percent": ape <= GATE_PERCENT,
            })
        trajectory_pred = manifest["trajectory_predictions"][0]
        if "predicted_ms" not in trajectory_pred:
            raise AdaptiveProtocolError("declared E2E method has no numeric value to score")
        trajectory_observed = _positive(trajectory["label"]["observed_ms"], "observed_e2e_ms")
        trajectory_ape = abs(float(trajectory_pred["predicted_ms"]) - trajectory_observed) / trajectory_observed * 100.0
        completed_errors = [row["absolute_percentage_error"] for row in event_scores if row["status"] == "completed"]
        passed = bool(completed_errors) and all(error <= GATE_PERCENT for error in completed_errors) and trajectory_ape <= GATE_PERCENT
        return {
            "schema_version": SCORE_SCHEMA,
            "prediction_manifest_sha256": manifest_digest,
            "target_gate_percent": GATE_PERCENT,
            "coverage_complete": len(labels) == len(predictions) and trajectory is not None,
            "event_scores": event_scores,
            "unavailable_event_count": unavailable,
            "trajectory_score": {
                "run_id": manifest["run_id"],
                "predicted_ms": float(trajectory_pred["predicted_ms"]),
                "observed_ms": trajectory_observed,
                "absolute_percentage_error": trajectory_ape,
                "within_25_percent": trajectory_ape <= GATE_PERCENT,
            },
            "passed": passed,
        }


def _cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    freeze = sub.add_parser("freeze-model", help="freeze a calibration-only model block")
    freeze.add_argument("--model-json", required=True, type=Path, help="JSON object containing tool_event/model_event/trajectory blocks")
    freeze.add_argument("--output", required=True, type=Path)
    freeze.add_argument("--calibration-run-id", action="append", required=True)
    for name in ("split-manifest-sha256", "runtime-manifest-sha256", "hardware-profile-sha256", "model-revision-sha256"):
        freeze.add_argument("--" + name, required=True)

    e2e_prediction = sub.add_parser(
        "freeze-e2e-prediction",
        help="freeze a calibration-derived pre-trajectory E2E forecast",
    )
    e2e_prediction.add_argument("--calibration-model", required=True, type=Path)
    e2e_prediction.add_argument("--hardware-profile", required=True, type=Path)
    e2e_prediction.add_argument("--run-id", required=True)
    e2e_prediction.add_argument(
        "--features-json",
        required=True,
        type=Path,
        help="feature-only object with tool_events and model_events arrays",
    )
    e2e_prediction.add_argument("--output", required=True, type=Path)

    def add_protocol_arguments(command: argparse.ArgumentParser) -> None:
        command.add_argument("--root", required=True, type=Path)
        command.add_argument("--calibration-model", required=True, type=Path)
        for name in (
            "split-manifest-sha256",
            "runtime-manifest-sha256",
            "hardware-profile-sha256",
            "model-revision-sha256",
        ):
            command.add_argument("--" + name, required=True)

    arm = sub.add_parser("arm", help="freeze one adaptive holdout trajectory before its first event")
    add_protocol_arguments(arm)
    arm.add_argument("--run-id", required=True)
    e2e = arm.add_mutually_exclusive_group(required=True)
    e2e.add_argument("--predicted-e2e-ms", type=float)
    e2e.add_argument("--e2e-prediction-method")

    predict = sub.add_parser("predict-event", help="durably freeze one pre-event prediction")
    add_protocol_arguments(predict)
    predict.add_argument("--kind", choices=("tool", "model"), required=True)
    predict.add_argument("--features-json", required=True, type=Path)

    reveal = sub.add_parser("reveal-event", help="append one measured label after its prediction")
    add_protocol_arguments(reveal)
    reveal.add_argument("--kind", choices=("tool", "model"), required=True)
    reveal.add_argument("--identifier", required=True)
    reveal.add_argument("--label-json", required=True, type=Path)

    freeze_predictions = sub.add_parser(
        "freeze-manifest",
        help="freeze all adaptive predictions before trajectory E2E reveal",
    )
    add_protocol_arguments(freeze_predictions)

    reveal_trajectory = sub.add_parser(
        "reveal-trajectory",
        help="append measured trajectory E2E after prediction-manifest freeze",
    )
    add_protocol_arguments(reveal_trajectory)
    reveal_trajectory.add_argument("--observed-e2e-ms", required=True, type=float)

    score = sub.add_parser("score", help="score every revealed event and trajectory")
    add_protocol_arguments(score)
    score.add_argument("--output", type=Path)

    args = parser.parse_args()
    if args.command == "freeze-model":
        model = _read_mapping(args.model_json, kind="calibration model input")
        digest = freeze_calibration_model(
            model,
            args.output,
            calibration_run_ids=args.calibration_run_id,
            split_manifest_sha256=args.split_manifest_sha256,
            runtime_manifest_sha256=args.runtime_manifest_sha256,
            hardware_profile_sha256=args.hardware_profile_sha256,
            model_revision_sha256=args.model_revision_sha256,
        )
        print(json.dumps({"status": "frozen", "path": str(args.output), "sha256": digest}, sort_keys=True))
        return 0

    if args.command == "freeze-e2e-prediction":
        model = FrozenCalibrationModel.load(args.calibration_model)
        hardware, hardware_digest = _read_hashed_json(
            args.hardware_profile,
            kind="hardware profile",
        )
        if model.bindings().get("hardware_profile_sha256") != hardware_digest:
            raise AdaptiveProtocolError(
                "hardware profile does not match the frozen calibration model binding"
            )
        features = _read_mapping(args.features_json, kind="pre-trajectory E2E features")
        if set(features) != {"tool_events", "model_events"}:
            raise AdaptiveProtocolError(
                "pre-trajectory E2E features must contain exactly tool_events and model_events"
            )
        if not isinstance(features["tool_events"], list) or not all(
            isinstance(row, Mapping) for row in features["tool_events"]
        ):
            raise AdaptiveProtocolError("pre-trajectory tool_events must be a mapping list")
        if not isinstance(features["model_events"], list) or not all(
            isinstance(row, Mapping) for row in features["model_events"]
        ):
            raise AdaptiveProtocolError("pre-trajectory model_events must be a mapping list")
        digest = freeze_trajectory_prediction(
            model,
            run_id=args.run_id,
            hardware=hardware,
            tool_events=features["tool_events"],
            model_events=features["model_events"],
            output_path=args.output,
        )
        print(json.dumps({"status": "frozen", "path": str(args.output), "sha256": digest}, sort_keys=True))
        return 0

    protocol = AdaptiveEventProtocol(
        root=args.root,
        calibration_model=FrozenCalibrationModel.load(args.calibration_model),
        split_manifest_sha256=args.split_manifest_sha256,
        runtime_manifest_sha256=args.runtime_manifest_sha256,
        hardware_profile_sha256=args.hardware_profile_sha256,
        model_revision_sha256=args.model_revision_sha256,
    )
    if args.command == "arm":
        result = protocol.arm_trajectory(
            args.run_id,
            predicted_e2e_ms=args.predicted_e2e_ms,
            e2e_prediction_method=args.e2e_prediction_method,
        )
    elif args.command == "predict-event":
        result = protocol.predict_event(
            args.kind,
            _read_mapping(args.features_json, kind="pre-event features"),
        )
    elif args.command == "reveal-event":
        result = protocol.reveal_event_label(
            args.kind,
            args.identifier,
            _read_mapping(args.label_json, kind="measured event label"),
        )
    elif args.command == "freeze-manifest":
        digest = protocol.freeze_prediction_manifest()
        result = {"status": "frozen", "path": str(protocol.manifest_path), "sha256": digest}
    elif args.command == "reveal-trajectory":
        result = protocol.reveal_trajectory_label(args.observed_e2e_ms)
    elif args.command == "score":
        result = protocol.score()
        if args.output is not None:
            _write_frozen_json(args.output, result)
    else:  # pragma: no cover - argparse enforces this set
        raise AdaptiveProtocolError(f"unsupported command: {args.command}")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(_cli())
    except (AdaptiveProtocolError, OSError, json.JSONDecodeError) as exc:
        print(f"NOT_READY: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
