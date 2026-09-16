#!/usr/bin/env python3
"""Build conditional Steps 1--3 figure inputs from the frozen D9 v3 API.

The adapter consumes the already-sealed figure tables and the richer preserved
protocol descriptors needed by the v3 serving API.  It never fits a model and
never uses a measured duration or measured E2E residual as a prediction input.
The resulting tables retain observed evaluator outcomes and observed timing
columns for provenance, and add separate ``predicted_*`` columns for the
renderer.

The prediction is conditional on the realized action/event sequence and token
descriptors in the supplied workload.  In particular, realized
``output_tokens`` are a supplied workload descriptor when used by the frozen
GPU model; they are not presented as a pre-request feature.  This is a
workload-latency simulation export, not a prospective success or trajectory
forecast.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import shlex
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from agentic_sim.assignment.event_simulator import HardwareProfile, ModelEventInput  # noqa: E402
from agentic_sim.assignment.workload_simulator import (  # noqa: E402
    WORKLOAD_MODEL_SCHEMA,
    WorkloadSimulator,
    WorkloadToolInput,
)


ASSIGNMENT = Path("/home/riverahernandezjason/h100-assignment-work-20260905/assignment")
SNAPSHOT = ASSIGNMENT / "submission" / "20260908T140000Z-offline-v2"
DEFAULT_SOURCE = SNAPSHOT / "figures-input"
DEFAULT_PROTOCOL = ASSIGNMENT / "submission" / "20260908T020000Z" / "protocol-input"
DEFAULT_MODEL = (
    ASSIGNMENT
    / "submission"
    / "20260908T060000Z"
    / "d9-cpu-review"
    / "reviewed-workload-model.json"
)
DEFAULT_ACTIONS = DEFAULT_MODEL.with_name("calibration_actions.json")
DEFAULT_OUTPUT = SNAPSHOT / "d9-predicted"
DEFAULT_RENDERER = ROOT / "scripts" / "assignment" / "generate_step_figures.py"

# Generated predictions are a new artifact family.  These are source/model
# snapshots from earlier attempts and are deliberately immutable, even when
# a caller supplies ``--force`` while iterating on an export.
IMMUTABLE_SNAPSHOT_ROOTS = tuple(
    ASSIGNMENT / "submission" / name
    for name in (
        "20260908T020000Z",
        "20260908T041000Z",
        "20260908T060000Z",
        "20260908T080000Z",
    )
)

HOLDOUT_INSTANCE_ID = "sympy__sympy-12481"
# The historical holdout and its duplicate run are both excluded by instance
# metadata.  These IDs are a defensive fallback when an auxiliary metadata
# row has lost its instance_id; they are never used as a prediction key.
HOLDOUT_RUN_IDS = frozenset(
    {
        "assignment-case-v1:8bf8546c12056ce716b300fa4f27abf5bb431fd226bb6c49790a6c60e1eeee2c",
        "assignment-case-v1:b89a472dff772ec6f278ed6fb4b0ead4fed2cbf5c90cf725ffdbbab76ab22c21",
    }
)

API_SERVING_SPLIT = "holdout"
DERIVED_SWEEP_MARKER = "::step2-baseline::"
SWEEP_INLINE_METADATA = (
    "suite",
    "repository",
    "category",
    "instance_id",
    "repeat_id",
    "config_id",
    "provenance",
)
PREDICTED_TRAJECTORY_COLUMNS = (
    "predicted_tool_wall_ms",
    "predicted_model_wall_ms",
    "predicted_event_sum_wall_ms",
    "predicted_overhead_wall_ms",
    "predicted_e2e_wall_ms",
    "predicted_tool_model_ratio",
    "latency_prediction_source",
)
PREDICTED_EVENT_COLUMNS = ("predicted_wall_ms", "latency_prediction_source")
PREDICTED_SWEEP_COLUMNS = (
    "suite",
    "repository",
    "category",
    "instance_id",
    "repeat_id",
    "config_id",
    "provenance",
    "source_prediction_run_id",
    "prediction_row_kind",
    "predicted_tool_wall_ms",
    "predicted_model_wall_ms",
    "predicted_event_sum_wall_ms",
    "predicted_overhead_wall_ms",
    "predicted_e2e_wall_ms",
    "predicted_tool_model_ratio",
    "latency_prediction_source",
)

DEFAULT_HARDWARE = {
    "schema_version": "assignment.hardware-profile.v1",
    "hardware_id": "h100-80gb-pace",
    "architecture": "x86_64-h100-80gb",
    "cpu_cores": 16,
    "cpu_threads": 32,
    "cpu_base_ghz": 2.8,
    "system_memory_gib": 128.0,
    "storage_read_mbps": 500.0,
    "storage_write_mbps": 500.0,
    "gpu_count": 1,
    "gpu_compute_capability": 9.0,
    "gpu_memory_gib": 80.0,
    "gpu_memory_bandwidth_gbps": 3350.0,
    "gpu_bf16_tflops": 989.4,
}


class D9PredictionError(ValueError):
    """Raised when a prediction join cannot be made without fabrication."""


@dataclass(frozen=True)
class RunPrediction:
    """Predicted event and trajectory latency for one covered run."""

    tool_by_event: dict[str, float]
    model_by_request: dict[str, float]
    predicted_tool_ms: float
    predicted_model_ms: float
    predicted_event_sum_ms: float
    predicted_overhead_ms: float
    predicted_e2e_ms: float
    predicted_ratio: float


@dataclass(frozen=True)
class PredictionBundle:
    """Materialized tables and audit metadata before writing the snapshot."""

    source_fields: dict[str, list[str]]
    tables: dict[str, list[dict[str, str]]]
    selection_selected: dict[str, Any] | None
    coverage: dict[str, Any]
    exclusion: dict[str, Any]
    unknown: list[dict[str, Any]]
    sweep_prediction_lineage: list[dict[str, str]]
    source_hashes: dict[str, str]


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise D9PredictionError(f"unreadable JSON artifact: {path}: {exc}") from exc


def _load_recovered_trajectory_actions(
    path: Path,
    *,
    run_id: str,
    instance_id: str,
    expected_event_rows: Mapping[str, Mapping[str, str]],
) -> tuple[dict[str, str], dict[str, Any]]:
    """Recover an exact action sequence from one local runner trajectory.

    The preserved calibration action map is immutable and intentionally does
    not contain a few completed source runs.  A recovery trajectory is
    accepted only when its environment identity and every action event match
    the already sealed protocol event IDs, command hashes, command bytes, and
    observed wall times.  This prevents a different local attempt with the
    same instance and action count from being transplanted into this run.
    Execution times are checked only to identify the same action records used
    by the calibration extractor; they are never passed to the serving API.
    """

    if not path.is_file() or path.is_symlink():
        raise D9PredictionError(
            f"{run_id}: recovered action artifact is not a regular local file: {path}"
        )
    resolved_path = path.resolve()
    if resolved_path.parent.name != instance_id or resolved_path.name != f"{instance_id}.traj":
        raise D9PredictionError(
            f"{run_id}: recovered trajectory path is not identity-bound to {instance_id}: {path}"
        )
    payload = _read_json(path)
    if not isinstance(payload, Mapping):
        raise D9PredictionError(f"{run_id}: recovered trajectory must be a JSON object: {path}")
    environment = str(payload.get("environment") or "")
    if environment != instance_id:
        raise D9PredictionError(
            f"{run_id}: recovered trajectory environment {environment!r} does not match "
            f"instance_id {instance_id!r}"
        )
    steps = payload.get("trajectory")
    if not isinstance(steps, list):
        raise D9PredictionError(f"{run_id}: recovered trajectory has no action list: {path}")

    expected_event_ids = set(expected_event_rows)
    recovered: dict[str, str] = {}
    validated_events: list[dict[str, Any]] = []
    for ordinal, step in enumerate(steps):
        if not isinstance(step, Mapping):
            continue
        action = step.get("action")
        seconds = step.get("execution_time")
        if not isinstance(action, str) or not action.strip():
            continue
        # Match the reviewed local extractor's action-record eligibility.
        if (
            not isinstance(seconds, (int, float))
            or isinstance(seconds, bool)
            or seconds <= 0
        ):
            continue
        event_id = f"{run_id}-tool-{ordinal:04d}"
        if event_id in recovered:
            raise D9PredictionError(f"{run_id}: duplicate recovered action event {event_id}")
        protocol_row = expected_event_rows.get(event_id)
        if protocol_row is None:
            raise D9PredictionError(
                f"{run_id}: recovered action event is absent from protocol input: {event_id}"
            )
        if str(protocol_row.get("run_id") or "") != run_id:
            raise D9PredictionError(
                f"{run_id}: protocol event has a different run identity: {event_id}"
            )
        protocol_ordinal = str(protocol_row.get("ordinal") or "").strip()
        if protocol_ordinal and protocol_ordinal != str(ordinal):
            raise D9PredictionError(
                f"{run_id}: recovered action ordinal disagrees with protocol for {event_id}"
            )
        if str(protocol_row.get("status") or "") != "completed":
            raise D9PredictionError(
                f"{run_id}: recovered action protocol event is not completed: {event_id}"
            )
        expected_hash = str(protocol_row.get("command_sha256") or "").strip().lower()
        if len(expected_hash) != 64:
            raise D9PredictionError(
                f"{run_id}: protocol event lacks a valid command_sha256: {event_id}"
            )
        actual_hash = hashlib.sha256(action.encode("utf-8")).hexdigest()
        if actual_hash != expected_hash:
            raise D9PredictionError(
                f"{run_id}: recovered action hash mismatch for {event_id}; "
                "the local artifact cannot be bound to this protocol event"
            )
        command_bytes = str(protocol_row.get("command_bytes") or "").strip()
        try:
            expected_bytes = int(command_bytes)
        except (TypeError, ValueError) as exc:
            raise D9PredictionError(
                f"{run_id}: protocol event lacks numeric command_bytes: {event_id}"
            ) from exc
        actual_bytes = len(action.encode("utf-8"))
        if actual_bytes != expected_bytes:
            raise D9PredictionError(
                f"{run_id}: recovered command_bytes mismatch for {event_id}: "
                f"{actual_bytes} versus protocol {expected_bytes}"
            )
        try:
            expected_wall_ms = float(str(protocol_row.get("wall_ms") or ""))
            actual_wall_ms = float(seconds) * 1000.0
        except (TypeError, ValueError) as exc:
            raise D9PredictionError(
                f"{run_id}: protocol event lacks numeric wall_ms: {event_id}"
            ) from exc
        if not math.isfinite(expected_wall_ms) or expected_wall_ms <= 0:
            raise D9PredictionError(
                f"{run_id}: protocol event wall_ms is not finite and positive: {event_id}"
            )
        if not math.isclose(actual_wall_ms, expected_wall_ms, rel_tol=1e-9, abs_tol=1e-6):
            raise D9PredictionError(
                f"{run_id}: recovered execution_time does not match protocol wall_ms for "
                f"{event_id}: {actual_wall_ms} versus {expected_wall_ms}"
            )
        recovered[event_id] = action
        validated_events.append(
            {
                "event_id": event_id,
                "action_sha256": actual_hash,
                "command_bytes": actual_bytes,
                "execution_time_s": float(seconds),
                "protocol_wall_ms": expected_wall_ms,
            }
        )

    recovered_ids = set(recovered)
    if recovered_ids != expected_event_ids:
        missing = sorted(expected_event_ids - recovered_ids)
        extra = sorted(recovered_ids - expected_event_ids)
        detail = []
        if missing:
            detail.append(f"missing={missing[:3]}")
        if extra:
            detail.append(f"extra={extra[:3]}")
        raise D9PredictionError(
            f"{run_id}: exact local action support is incomplete ({len(recovered)} actions; "
            f"expected {len(expected_event_ids)}; {'; '.join(detail)})"
        )
    return recovered, {
        "run_id": run_id,
        "instance_id": instance_id,
        "path": str(resolved_path),
        "sha256": sha256_path(resolved_path),
        "action_count": len(recovered),
        "event_id_binding": "protocol tool event IDs by trajectory ordinal",
        "protocol_event_validation": "UTF-8 action SHA-256, command_bytes, and execution_time*1000 ~= wall_ms",
        "validated_events": validated_events,
    }


def _recovered_artifact_provenance(
    path: Path,
    *,
    run_id: str,
    instance_id: str,
    source_row: Mapping[str, str],
    protocol_row: Mapping[str, str],
) -> dict[str, Any]:
    """Bind a recovered trajectory to local case metadata when it exists.

    The protocol CSVs carry the normalized event identity, while a preserved
    runner attempt carries the case-level ``resume_key`` and artifact
    manifest.  Keeping both links in the manifest makes it possible to audit
    why a trajectory was accepted without treating a path name as a run
    identity.  Older local attempts may lack one of the case metadata files;
    in that situation the event-level hash/byte/timing checks remain required
    and the missing metadata is recorded explicitly.
    """

    resolved_path = path.resolve()
    result: dict[str, Any] = {
        "trajectory_path": str(resolved_path),
        "source_run_id": run_id,
        "protocol_run_id": str(protocol_row.get("run_id") or "") or None,
        "source_tool_events_path": str(source_row.get("tool_events_path") or "") or None,
        "source_model_events_path": str(source_row.get("model_events_path") or "") or None,
        "path_identity_binding": "parent directory and filename equal instance_id",
    }
    # A normal runner attempt stores case_result.json three parents above the
    # trajectory (instance/attempt/runner_attempts/case).  Do not infer run
    # identity from an arbitrary ancestor when this layout is unavailable.
    case_root = resolved_path.parents[3] if len(resolved_path.parents) > 3 else None
    case_spec_path = case_root / "case_spec.json" if case_root is not None else None
    case_result_path = case_root / "case_result.json" if case_root is not None else None
    if case_spec_path is None or not case_spec_path.is_file():
        result["case_spec"] = {"present": False, "binding": "event-level checks only"}
    else:
        case_spec = _read_json(case_spec_path)
        if not isinstance(case_spec, Mapping):
            raise D9PredictionError(f"{run_id}: case_spec.json is not an object: {case_spec_path}")
        for field, expected in (
            ("resume_key", run_id),
            ("instance_id", instance_id),
            ("repository", str(source_row.get("repository") or "")),
            ("suite", str(source_row.get("suite") or "")),
        ):
            actual = str(case_spec.get(field) or "")
            if expected and actual != expected:
                raise D9PredictionError(
                    f"{run_id}: recovered case metadata {field}={actual!r} does not match {expected!r}"
                )
        result["case_spec"] = {
            "path": str(case_spec_path),
            "sha256": sha256_path(case_spec_path),
            "resume_key": run_id,
            "instance_id": instance_id,
        }

    if case_result_path is None or not case_result_path.is_file():
        result["case_result"] = {"present": False, "binding": "case manifest unavailable"}
    else:
        case_result = _read_json(case_result_path)
        if not isinstance(case_result, Mapping):
            raise D9PredictionError(
                f"{run_id}: case_result.json is not an object: {case_result_path}"
            )
        artifact_path = resolved_path.relative_to(case_root).as_posix()
        artifacts = case_result.get("artifacts")
        matches = (
            [item for item in artifacts if isinstance(item, Mapping) and item.get("path") == artifact_path]
            if isinstance(artifacts, list)
            else []
        )
        if not matches:
            raise D9PredictionError(
                f"{run_id}: case_result.json does not bind recovered trajectory: {artifact_path}"
            )
        expected_sha = str(matches[0].get("sha256") or "")
        actual_sha = sha256_path(resolved_path)
        if expected_sha != actual_sha:
            raise D9PredictionError(
                f"{run_id}: case_result.json trajectory hash disagrees with local artifact"
            )
        result["case_result"] = {
            "path": str(case_result_path),
            "sha256": sha256_path(case_result_path),
            "artifact_path": artifact_path,
            "artifact_sha256": actual_sha,
            "binding": "case_result artifact manifest",
        }
    return result


def _read_csv(path: Path, required: set[str], table: str) -> tuple[list[str], list[dict[str, str]]]:
    if not path.is_file():
        raise D9PredictionError(f"{table}: file does not exist: {path}")
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None:
            raise D9PredictionError(f"{table}: missing CSV header")
        fields = list(reader.fieldnames)
        if len(fields) != len(set(fields)):
            raise D9PredictionError(f"{table}: duplicate CSV header")
        missing = sorted(required.difference(fields))
        if missing:
            raise D9PredictionError(
                f"{table}: missing required columns: {', '.join(missing)}"
            )
        rows: list[dict[str, str]] = []
        for row_number, row in enumerate(reader, 2):
            if None in row:
                raise D9PredictionError(f"{table} row {row_number}: too many CSV fields")
            rows.append({key: (value or "").strip() for key, value in row.items()})
    if not rows:
        raise D9PredictionError(f"{table}: table is empty")
    return fields, rows


def _unique(rows: Sequence[Mapping[str, str]], field: str, table: str) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for row_number, row in enumerate(rows, 2):
        value = str(row.get(field, "")).strip()
        if not value:
            raise D9PredictionError(f"{table} row {row_number}: {field} is empty")
        if value in result:
            raise D9PredictionError(f"{table}: duplicate {field} {value!r}")
        result[value] = dict(row)
    return result


def _group_unique(
    rows: Sequence[Mapping[str, str]], field: str, table: str
) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        value = str(row.get(field, "")).strip()
        if not value:
            raise D9PredictionError(f"{table}: {field} is empty")
        grouped[value].append(dict(row))
    return grouped


def _is_holdout_metadata(row: Mapping[str, Any]) -> bool:
    if str(row.get("instance_id") or "") == HOLDOUT_INSTANCE_ID:
        return True
    if str(row.get("run_id") or "") in HOLDOUT_RUN_IDS:
        return True
    path = str(row.get("eval_source_path") or "").lower()
    return HOLDOUT_INSTANCE_ID.lower() in path


def _format_number(value: float) -> str:
    if not math.isfinite(float(value)) or float(value) <= 0:
        raise D9PredictionError(f"prediction must be finite and positive: {value!r}")
    return format(float(value), ".12g")


def _sweep_value_sort_key(value: str) -> tuple[int, float | str]:
    try:
        return (0, float(value))
    except ValueError:
        return (1, value)


def _number(value: Any, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise D9PredictionError(f"{name} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise D9PredictionError(f"{name} must be numeric") from exc
    if not math.isfinite(result) or (positive and result <= 0):
        raise D9PredictionError(f"{name} must be finite and positive")
    return result


def _integer(value: Any, name: str, *, positive: bool = False) -> int:
    result = _number(value, name, positive=positive)
    if not result.is_integer() or result < 0:
        raise D9PredictionError(f"{name} must be a nonnegative integer")
    return int(result)


def _check_prediction(value: Any, name: str) -> float:
    result = _number(value, name, positive=True)
    return result


def _add_fields(fields: Sequence[str], additions: Sequence[str]) -> list[str]:
    result = list(fields)
    for field in additions:
        if field not in result:
            result.append(field)
    return result


def _sidecar_digest(path: Path) -> str | None:
    sidecar = Path(str(path) + ".sha256")
    if not sidecar.is_file():
        return None
    fields = sidecar.read_text(encoding="utf-8").strip().split()
    if len(fields) != 2 or fields[1] != path.name or len(fields[0]) != 64:
        raise D9PredictionError(f"malformed SHA-256 sidecar: {sidecar}")
    actual = sha256_path(path)
    if fields[0] != actual:
        raise D9PredictionError(f"SHA-256 sidecar does not match: {sidecar}")
    return actual


def _verify_frozen_source_hashes(model_path: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Check v3 source hashes when the preserved fit manifest supplies them."""

    checks: dict[str, Any] = {}
    for relative, expected in sorted((manifest.get("source_sha256") or {}).items()):
        if not isinstance(relative, str) or not isinstance(expected, str):
            raise D9PredictionError("frozen model manifest has malformed source_sha256")
        candidate = ROOT / relative
        if not candidate.is_file():
            checks[relative] = {"expected": expected, "present": False}
            continue
        actual = sha256_path(candidate)
        if actual != expected:
            raise D9PredictionError(
                f"frozen v3 source changed for {relative}: expected {expected}, got {actual}"
            )
        checks[relative] = {"expected": expected, "actual": actual, "present": True}
    return checks


def load_frozen_model(
    model_path: Path,
) -> tuple[WorkloadSimulator, dict[str, Any]]:
    """Load the serialized v3 model without fitting and record hash evidence."""

    if not model_path.is_file():
        raise D9PredictionError(f"frozen v3 model does not exist: {model_path}")
    before = sha256_path(model_path)
    sidecar = _sidecar_digest(model_path)
    payload = _read_json(model_path)
    if not isinstance(payload, Mapping) or payload.get("schema_version") != WORKLOAD_MODEL_SCHEMA:
        raise D9PredictionError("frozen model is not assignment.workload-simulator.v3")
    simulator = WorkloadSimulator.from_mapping(payload)
    manifest_path = Path(str(model_path) + ".manifest.json")
    manifest = _read_json(manifest_path) if manifest_path.is_file() else {}
    if manifest and manifest.get("artifact_sha256") not in {None, before}:
        raise D9PredictionError("frozen model manifest artifact_sha256 does not match file")
    source_checks = _verify_frozen_source_hashes(model_path, manifest)
    oof_path = model_path.with_name("predictions_instance_id_grouped_semantic_repo_median.json")
    oof_reference: dict[str, Any] = {
        "path": str(oof_path),
        "exists": oof_path.is_file(),
        "used_for_figures": False,
        "meaning": "cross-validation out-of-fold reviewed predictions; diagnostic reference only",
    }
    if oof_path.is_file():
        oof_payload = _read_json(oof_path)
        if isinstance(oof_payload, Mapping):
            oof_reference.update(
                {
                    "sha256": sha256_path(oof_path),
                    "schema_version": oof_payload.get("schema_version"),
                    "candidate": oof_payload.get("candidate"),
                }
            )
    metadata = {
        "schema_version": WORKLOAD_MODEL_SCHEMA,
        "path": str(model_path),
        "file_sha256_before": before,
        "file_sha256_after": None,
        "sidecar_sha256": sidecar,
        "model_sha256": payload.get("model_sha256"),
        "fit_manifest_path": str(manifest_path) if manifest_path.is_file() else None,
        "fit_manifest_artifact_sha256": manifest.get("artifact_sha256") if manifest else None,
        "fit_manifest_model_sha256": manifest.get("artifact_model_sha256") if manifest else None,
        "source_hash_checks": source_checks,
        "calibration_only_artifact": manifest.get("calibration_only") if manifest else None,
        "holdout_scored_in_fit": manifest.get("holdout_scored") if manifest else None,
        "retained_fit_counts": manifest.get("retained_counts") if manifest else None,
        "cross_validation_predictions": oof_reference,
    }
    return simulator, metadata


def _load_hardware(path: Path | None) -> HardwareProfile:
    payload: Mapping[str, Any]
    if path is None:
        payload = DEFAULT_HARDWARE
    else:
        value = _read_json(path)
        if not isinstance(value, Mapping):
            raise D9PredictionError("hardware profile JSON must be an object")
        payload = value
    return HardwareProfile.from_mapping(payload)


def _tool_features(
    row: Mapping[str, str],
    action: str,
    hardware: HardwareProfile,
    metadata: Mapping[str, str],
) -> WorkloadToolInput:
    feature = WorkloadToolInput.from_action(
        action,
        event_id=str(row["event_id"]),
        run_id=str(row["run_id"]),
        split=API_SERVING_SPLIT,
        hardware=hardware,
        repository=str(metadata.get("repository") or ""),
        instance_id=str(metadata.get("instance_id") or ""),
    )
    expected_class = str(row.get("operation_class") or "")
    # ``protocol-input/tool_events.csv`` is an older compact projection.  Its
    # operation class and tool name were derived before the sealed action
    # extractor started skipping the leading ``cd`` segment, so they can
    # legitimately disagree with the exact preserved action.  The action map
    # is the authoritative pre-event descriptor for the v3 API; retaining the
    # compact fields in the output is useful provenance, but they must not
    # suppress a prediction when the local action artifact is present.
    _ = expected_class
    command_sha = str(row.get("command_sha256") or "")
    # The protocol hash uses the historical serialized command projection and
    # is not comparable to the hash of the preserved raw action in all rows.
    # Identity is still bound by event_id/run_id and the action map itself is
    # hash-bound as an input artifact.  Do not turn a stale derived hash into
    # an unsupported workload row.
    _ = command_sha
    return feature


def _model_features(
    row: Mapping[str, str], hardware: HardwareProfile
) -> ModelEventInput:
    # The realized output token count is deliberately carried as a workload
    # descriptor because this frozen v3 model's GPU design uses it.  No wall_ms
    # or residual field is placed in this mapping.
    return ModelEventInput.from_mapping(
        {
            "schema_version": "assignment.model-event-input.v1",
            "request_id": str(row["request_id"]),
            "run_id": str(row["run_id"]),
            "split": API_SERVING_SPLIT,
            "input_tokens": _integer(row.get("input_tokens"), "input_tokens"),
            "output_tokens": _integer(row.get("output_tokens"), "output_tokens"),
            "context_tokens": _integer(row.get("context_tokens"), "context_tokens"),
            "max_output_tokens": _integer(
                row.get("max_output_tokens"), "max_output_tokens", positive=True
            ),
            "hardware": hardware.to_mapping(),
        }
    )


def _assert_same_field(left: Mapping[str, str], right: Mapping[str, str], field: str, label: str) -> None:
    if field in left and field in right and str(left[field]) != str(right[field]):
        raise D9PredictionError(
            f"{label} join disagrees for {left.get('run_id') or left.get('request_id')}: "
            f"{field}={left[field]!r} versus {right[field]!r}"
        )


def _assert_same_numeric_field(
    left: Mapping[str, str],
    right: Mapping[str, str],
    field: str,
    label: str,
    *,
    rel_tol: float = 1e-12,
    abs_tol: float = 1e-6,
) -> None:
    """Validate a numeric identity field without exposing it as a feature."""

    left_value = str(left.get(field) or "").strip()
    right_value = str(right.get(field) or "").strip()
    if not left_value or not right_value:
        raise D9PredictionError(
            f"{label} join lacks numeric identity field {field} for "
            f"{left.get('run_id') or left.get('request_id')}"
        )
    try:
        left_number = float(left_value)
        right_number = float(right_value)
    except (TypeError, ValueError) as exc:
        raise D9PredictionError(
            f"{label} join has nonnumeric identity field {field} for "
            f"{left.get('run_id') or left.get('request_id')}"
        ) from exc
    if not math.isfinite(left_number) or not math.isfinite(right_number):
        raise D9PredictionError(
            f"{label} join has nonfinite identity field {field} for "
            f"{left.get('run_id') or left.get('request_id')}"
        )
    if not math.isclose(left_number, right_number, rel_tol=rel_tol, abs_tol=abs_tol):
        raise D9PredictionError(
            f"{label} join disagrees for {left.get('run_id') or left.get('request_id')}: "
            f"{field}={left_value!r} versus {right_value!r}"
        )


def _predict_run(
    simulator: Any,
    *,
    run_id: str,
    metadata: Mapping[str, str],
    tool_rows: Sequence[Mapping[str, str]],
    model_rows: Sequence[Mapping[str, str]],
    actions: Mapping[str, str],
    hardware: HardwareProfile,
    canonical_tool_rows: Mapping[str, Mapping[str, str]] | None = None,
    canonical_model_rows: Mapping[str, Mapping[str, str]] | None = None,
) -> RunPrediction:
    if not tool_rows or not model_rows:
        raise D9PredictionError(f"{run_id}: missing tool or model event coverage")
    tool_by_event: dict[str, float] = {}
    for protocol_row in tool_rows:
        event_id = str(protocol_row.get("event_id") or "")
        if not event_id or event_id in tool_by_event:
            raise D9PredictionError(f"{run_id}: duplicate or empty tool event identity")
        if canonical_tool_rows is not None:
            canonical = canonical_tool_rows.get(event_id)
            if canonical is None:
                raise D9PredictionError(f"{run_id}: protocol tool event absent from canonical input: {event_id}")
            _assert_same_field(canonical, protocol_row, "run_id", "tool")
            _assert_same_field(canonical, protocol_row, "status", "tool")
            _assert_same_numeric_field(canonical, protocol_row, "wall_ms", "tool")
        action = actions.get(event_id)
        if not isinstance(action, str) or not action.strip():
            raise D9PredictionError(f"{run_id}: preserved action absent for {event_id}")
        feature = _tool_features(protocol_row, action, hardware, metadata)
        tool_by_event[event_id] = _check_prediction(
            simulator.predict_tool_ms(feature), f"tool prediction {event_id}"
        )

    model_by_request: dict[str, float] = {}
    for protocol_row in model_rows:
        request_id = str(protocol_row.get("request_id") or "")
        if not request_id or request_id in model_by_request:
            raise D9PredictionError(f"{run_id}: duplicate or empty model request identity")
        if canonical_model_rows is not None:
            canonical = canonical_model_rows.get(request_id)
            if canonical is None:
                raise D9PredictionError(
                    f"{run_id}: protocol model event absent from canonical input: {request_id}"
                )
            for field in ("run_id", "status", "input_tokens", "output_tokens", "context_tokens"):
                _assert_same_field(canonical, protocol_row, field, "model")
            _assert_same_numeric_field(canonical, protocol_row, "wall_ms", "model")
        feature = _model_features(protocol_row, hardware)
        model_by_request[request_id] = _check_prediction(
            simulator.predict_model_ms(feature), f"model prediction {request_id}"
        )

    if canonical_tool_rows is not None and set(canonical_tool_rows) != set(tool_by_event):
        raise D9PredictionError(f"{run_id}: canonical/protocol tool event coverage is incomplete")
    if canonical_model_rows is not None and set(canonical_model_rows) != set(model_by_request):
        raise D9PredictionError(f"{run_id}: canonical/protocol model event coverage is incomplete")

    predicted_tool_ms = sum(tool_by_event.values())
    predicted_model_ms = sum(model_by_request.values())
    predicted_event_sum_ms = _check_prediction(
        simulator.predict_event_sum_ms(predicted_tool_ms, predicted_model_ms),
        f"event-sum prediction {run_id}",
    )
    predicted_overhead_ms = _check_prediction(
        simulator.predict_overhead_ms(len(tool_by_event), len(model_by_request)),
        f"overhead prediction {run_id}",
    )
    predicted_e2e_ms = _check_prediction(
        simulator.predict_e2e_ms(
            predicted_tool_ms,
            predicted_model_ms,
            len(tool_by_event),
            len(model_by_request),
        ),
        f"E2E prediction {run_id}",
    )
    return RunPrediction(
        tool_by_event=tool_by_event,
        model_by_request=model_by_request,
        predicted_tool_ms=predicted_tool_ms,
        predicted_model_ms=predicted_model_ms,
        predicted_event_sum_ms=predicted_event_sum_ms,
        predicted_overhead_ms=predicted_overhead_ms,
        predicted_e2e_ms=predicted_e2e_ms,
        predicted_ratio=predicted_tool_ms / predicted_model_ms,
    )


def _selection_row(row: Mapping[str, str]) -> dict[str, Any]:
    allowed = (
        "run_id",
        "suite",
        "repository",
        "category",
        "instance_id",
        "config_id",
        "repeat_id",
        "tool_wall_ms",
        "model_wall_ms",
        "e2e_wall_ms",
        "tool_model_ratio",
        "tool_event_count",
        "model_event_count",
    )
    selected: dict[str, Any] = {}
    for field in allowed:
        if field not in row:
            continue
        value = row[field]
        if field in {"tool_event_count", "model_event_count"}:
            selected[field] = int(value)
        elif field in {"tool_wall_ms", "model_wall_ms", "e2e_wall_ms", "tool_model_ratio"}:
            selected[field] = float(value)
        else:
            selected[field] = value
    return selected


def _sweep_identity(
    *,
    sweep_row: Mapping[str, str],
    protocol_row: Mapping[str, str] | None = None,
    source_row: Mapping[str, str] | None = None,
    derived: bool,
) -> dict[str, str]:
    """Return self-contained sweep identity for B's loader.

    The source sweep table predates B's portable identity contract, so the
    adapter writes the identity onto every predicted row.  A derived baseline
    row inherits case identity from its shared-baseline source trajectory but
    keeps its own sweep parameter/value and derived run ID.
    """

    values: dict[str, str] = {}
    identity_fields = ("suite", "repository", "category", "instance_id", "repeat_id")
    for field in identity_fields:
        candidates = [sweep_row]
        if source_row is not None:
            candidates.append(source_row)
        if protocol_row is not None:
            candidates.append(protocol_row)
        present = [str(candidate.get(field) or "").strip() for candidate in candidates]
        present = [value for value in present if value]
        if present and any(value != present[0] for value in present[1:]):
            raise D9PredictionError(
                f"sweep identity disagrees for {sweep_row.get('run_id')}: {field}"
            )
        if present:
            values[field] = present[0]

    for field in ("suite", "repository", "category", "instance_id", "repeat_id"):
        if not values.get(field):
            raise D9PredictionError(
                f"sweep identity incomplete for {sweep_row.get('run_id')}: {field}"
            )
    values["config_id"] = str(
        sweep_row.get("config_id")
        or f"{sweep_row['sweep_parameter']}={sweep_row['sweep_value']}"
    )
    if derived:
        values["provenance"] = "derived_from_shared_baseline"
    else:
        values["provenance"] = values.get("provenance") or "measured"
    return values


def _prediction_fields(
    prediction: RunPrediction,
    *,
    source_prediction_run_id: str,
    prediction_row_kind: str,
) -> dict[str, str]:
    return {
        "source_prediction_run_id": source_prediction_run_id,
        "prediction_row_kind": prediction_row_kind,
        "predicted_tool_wall_ms": _format_number(prediction.predicted_tool_ms),
        "predicted_model_wall_ms": _format_number(prediction.predicted_model_ms),
        "predicted_event_sum_wall_ms": _format_number(prediction.predicted_event_sum_ms),
        "predicted_overhead_wall_ms": _format_number(prediction.predicted_overhead_ms),
        "predicted_e2e_wall_ms": _format_number(prediction.predicted_e2e_ms),
        "predicted_tool_model_ratio": _format_number(prediction.predicted_ratio),
        "latency_prediction_source": (
            "frozen_v3_api_shared_baseline"
            if prediction_row_kind == "derived_shared_baseline"
            else "frozen_v3_api"
        ),
    }


def build_prediction_bundle(
    *,
    source_dir: Path,
    protocol_dir: Path,
    action_map_path: Path,
    simulator: Any,
    hardware: HardwareProfile,
    selection_path: Path | None = None,
    recovered_trajectory_paths: Mapping[str, Path] | None = None,
) -> PredictionBundle:
    """Join covered nonholdout inputs and compute frozen API predictions.

    This function is intentionally injectable with a loaded simulator so the
    focused tests can exercise joins and coverage without fitting or running
    any experiment.  The production CLI always passes a deserialized frozen
    v3 ``WorkloadSimulator``.
    """

    source_specs = {
        "trajectories": (
            {"run_id", "suite", "repository", "category", "instance_id", "status", "official_resolved", "e2e_wall_ms"},
            "trajectories",
        ),
        "tool_events": (
            {"event_id", "run_id", "status", "operation_class", "wall_ms"},
            "tool_events",
        ),
        "model_events": (
            {"request_id", "run_id", "status", "input_tokens", "output_tokens", "context_tokens", "wall_ms"},
            "model_events",
        ),
        "sweep_runs": (
            {"run_id", "status", "sweep_parameter", "sweep_value", "official_resolved", "e2e_wall_ms", "tool_wall_ms", "model_wall_ms"},
            "sweep_runs",
        ),
    }
    source_fields: dict[str, list[str]] = {}
    source_rows: dict[str, list[dict[str, str]]] = {}
    source_hashes: dict[str, str] = {}
    for name, (required, label) in source_specs.items():
        fields, rows = _read_csv(source_dir / f"{name}.csv", required, label)
        source_fields[name] = fields
        source_rows[name] = rows
        source_hashes[f"figures-input/{name}.csv"] = sha256_path(source_dir / f"{name}.csv")

    evaluator_path = source_dir / "evaluator_provenance.csv"
    evaluator_fields: list[str] = []
    evaluator_rows: list[dict[str, str]] = []
    if evaluator_path.is_file():
        evaluator_fields, evaluator_rows = _read_csv(
            evaluator_path,
            {"run_id", "instance_id", "official_resolved", "eval_source_path"},
            "evaluator_provenance",
        )
        source_hashes["figures-input/evaluator_provenance.csv"] = sha256_path(evaluator_path)
    d1_headline_path = source_dir / "d1_headline_metrics.json"
    if d1_headline_path.is_file():
        source_hashes["figures-input/d1_headline_metrics.json"] = sha256_path(d1_headline_path)
    sweep_metadata_source = source_dir / "sweep_metadata.jsonl"
    if sweep_metadata_source.is_file():
        source_hashes["figures-input/sweep_metadata.jsonl"] = sha256_path(sweep_metadata_source)

    protocol_fields: dict[str, list[str]] = {}
    protocol_rows: dict[str, list[dict[str, str]]] = {}
    protocol_specs = {
        "trajectories": (
            {"run_id", "repository", "instance_id", "status"},
            "protocol trajectories",
        ),
        "tool_events": (
            {"event_id", "run_id", "status", "operation_class", "command_sha256"},
            "protocol tool events",
        ),
        "model_events": (
            {"request_id", "run_id", "status", "input_tokens", "max_output_tokens", "output_tokens", "context_tokens"},
            "protocol model events",
        ),
    }
    for name, (required, label) in protocol_specs.items():
        fields, rows = _read_csv(protocol_dir / f"{name}.csv", required, label)
        protocol_fields[name] = fields
        protocol_rows[name] = rows
        source_hashes[f"protocol-input/{name}.csv"] = sha256_path(protocol_dir / f"{name}.csv")

    action_payload = _read_json(action_map_path)
    if not isinstance(action_payload, Mapping):
        raise D9PredictionError("preserved action map must be a JSON object")
    actions = {
        str(key): str(value)
        for key, value in action_payload.items()
        if isinstance(key, str) and isinstance(value, str)
    }
    source_hashes["calibration_actions.json"] = sha256_path(action_map_path)

    trajectory_by_run = _unique(source_rows["trajectories"], "run_id", "trajectories")
    tool_by_run = _group_unique(source_rows["tool_events"], "run_id", "tool_events")
    model_by_run = _group_unique(source_rows["model_events"], "run_id", "model_events")
    sweep_by_run = _unique(source_rows["sweep_runs"], "run_id", "sweep_runs")
    protocol_trajectory_by_run = _unique(
        protocol_rows["trajectories"], "run_id", "protocol trajectories"
    )
    protocol_tool_by_run = _group_unique(protocol_rows["tool_events"], "run_id", "protocol tool events")
    protocol_model_by_run = _group_unique(protocol_rows["model_events"], "run_id", "protocol model events")

    recovered_action_artifacts: dict[str, dict[str, Any]] = {}
    for run_id, path in sorted((recovered_trajectory_paths or {}).items()):
        if run_id in HOLDOUT_RUN_IDS:
            raise D9PredictionError(f"{run_id}: holdout run cannot receive recovered action support")
        source_row = trajectory_by_run.get(run_id)
        protocol_row = protocol_trajectory_by_run.get(run_id)
        if source_row is None or protocol_row is None:
            raise D9PredictionError(
                f"{run_id}: recovered action artifact has no matching source/protocol trajectory"
            )
        instance_id = str(source_row.get("instance_id") or "")
        protocol_instance_id = str(protocol_row.get("instance_id") or "")
        if not instance_id or protocol_instance_id != instance_id:
            raise D9PredictionError(
                f"{run_id}: recovered action source/protocol instance identity disagrees"
            )
        expected_event_rows = {
            str(row.get("event_id") or ""): row
            for row in protocol_tool_by_run.get(run_id, [])
        }
        protocol_tool_rows = protocol_tool_by_run.get(run_id, [])
        if len(expected_event_rows) != len(protocol_tool_rows):
            raise D9PredictionError(f"{run_id}: duplicate protocol tool-event identity")
        if not expected_event_rows or "" in expected_event_rows:
            raise D9PredictionError(
                f"{run_id}: recovered action artifact has no complete protocol tool-event identity"
            )
        artifact_path = Path(path)
        provenance = _recovered_artifact_provenance(
            artifact_path,
            run_id=run_id,
            instance_id=instance_id,
            source_row=source_row,
            protocol_row=protocol_row,
        )
        recovered, artifact = _load_recovered_trajectory_actions(
            artifact_path,
            run_id=run_id,
            instance_id=instance_id,
            expected_event_rows=expected_event_rows,
        )
        artifact["provenance"] = provenance
        duplicate_ids = sorted(set(recovered).intersection(actions))
        if duplicate_ids:
            raise D9PredictionError(
                f"{run_id}: recovered action support would overwrite preserved actions "
                f"({duplicate_ids[:3]})"
            )
        actions.update(recovered)
        recovered_action_artifacts[run_id] = artifact
        source_hashes[f"recovered-trajectory/{run_id}"] = artifact["sha256"]

    excluded_runs = set(HOLDOUT_RUN_IDS)
    excluded_metadata_rows: list[dict[str, Any]] = []
    for row in [*source_rows["trajectories"], *evaluator_rows, *protocol_rows["trajectories"]]:
        if _is_holdout_metadata(row):
            run_id = str(row.get("run_id") or "")
            if run_id:
                excluded_runs.add(run_id)
            excluded_metadata_rows.append(
                {
                    "run_id": run_id or None,
                    "instance_id": row.get("instance_id") or None,
                    "eval_source_path": row.get("eval_source_path") or None,
                }
            )

    supported_runs = {str(run_id) for run_id in getattr(simulator, "calibration_run_ids", ())}
    unknown: list[dict[str, Any]] = []
    baseline_predictions: dict[str, RunPrediction] = {}
    baseline_output: list[dict[str, str]] = []
    baseline_source_rows = source_rows["trajectories"]
    for row in baseline_source_rows:
        run_id = row["run_id"]
        if run_id in excluded_runs or row.get("instance_id") == HOLDOUT_INSTANCE_ID:
            continue
        reason: str | None = None
        support_status: str | None = None
        prediction: RunPrediction | None = None
        metadata = protocol_trajectory_by_run.get(run_id)
        if metadata is None:
            reason = "protocol trajectory metadata absent"
            support_status = "protocol_metadata_missing"
        elif metadata.get("instance_id") == HOLDOUT_INSTANCE_ID:
            reason = "holdout instance metadata"
            support_status = "holdout_excluded"
        elif metadata.get("instance_id") and metadata["instance_id"] != row.get("instance_id"):
            reason = "trajectory instance_id join mismatch"
            support_status = "trajectory_identity_mismatch"
        else:
            protocol_tools = protocol_tool_by_run.get(run_id, [])
            missing_actions = sorted(
                str(item.get("event_id") or "")
                for item in protocol_tools
                if str(item.get("event_id") or "") not in actions
            )
            if missing_actions:
                if run_id in supported_runs:
                    reason = (
                        "frozen calibration manifest row exists but preserved action evidence "
                        f"is incomplete ({len(missing_actions)} event(s))"
                    )
                    support_status = "frozen_calibration_action_evidence_incomplete"
                else:
                    reason = (
                        "run is not in the frozen calibration manifest; exact local action "
                        "artifact is unavailable"
                    )
                    support_status = "outside_frozen_calibration_manifest_exact_action_missing"
            else:
                canonical_tools = {item["event_id"]: item for item in tool_by_run.get(run_id, [])}
                canonical_models = {item["request_id"]: item for item in model_by_run.get(run_id, [])}
                try:
                    prediction = _predict_run(
                        simulator,
                        run_id=run_id,
                        metadata={**metadata, **row},
                        tool_rows=protocol_tools,
                        model_rows=protocol_model_by_run.get(run_id, []),
                        actions=actions,
                        hardware=hardware,
                        canonical_tool_rows=canonical_tools,
                        canonical_model_rows=canonical_models,
                    )
                except D9PredictionError as exc:
                    reason = str(exc)
                    support_status = (
                        "frozen_calibration_prediction_join_failed"
                        if run_id in supported_runs
                        else "outside_frozen_calibration_prediction_join_failed"
                    )
        if prediction is None:
            unknown_row = {
                "scope": "baseline",
                "run_id": run_id,
                "instance_id": row.get("instance_id"),
                "reason": reason,
            }
            if support_status is not None:
                unknown_row["support_status"] = support_status
            unknown.append(unknown_row)
            continue
        baseline_predictions[run_id] = prediction
        output = dict(row)
        output.update(
            {
                "predicted_tool_wall_ms": _format_number(prediction.predicted_tool_ms),
                "predicted_model_wall_ms": _format_number(prediction.predicted_model_ms),
                "predicted_event_sum_wall_ms": _format_number(prediction.predicted_event_sum_ms),
                "predicted_overhead_wall_ms": _format_number(prediction.predicted_overhead_ms),
                "predicted_e2e_wall_ms": _format_number(prediction.predicted_e2e_ms),
                "predicted_tool_model_ratio": _format_number(prediction.predicted_ratio),
                "latency_prediction_source": "frozen_v3_api",
            }
        )
        baseline_output.append(output)

    output_tool_rows: list[dict[str, str]] = []
    for row in source_rows["tool_events"]:
        prediction = baseline_predictions.get(row["run_id"])
        if prediction is None:
            continue
        event_prediction = prediction.tool_by_event.get(row["event_id"])
        if event_prediction is None:
            raise D9PredictionError(f"predicted baseline tool event missing: {row['event_id']}")
        output = dict(row)
        output.update(
            {
                "predicted_wall_ms": _format_number(event_prediction),
                "latency_prediction_source": "frozen_v3_api",
            }
        )
        output_tool_rows.append(output)

    output_model_rows: list[dict[str, str]] = []
    for row in source_rows["model_events"]:
        prediction = baseline_predictions.get(row["run_id"])
        if prediction is None:
            continue
        event_prediction = prediction.model_by_request.get(row["request_id"])
        if event_prediction is None:
            raise D9PredictionError(f"predicted baseline model event missing: {row['request_id']}")
        output = dict(row)
        output.update(
            {
                "predicted_wall_ms": _format_number(event_prediction),
                "latency_prediction_source": "frozen_v3_api",
            }
        )
        output_model_rows.append(output)

    sweep_output: list[dict[str, str]] = []
    sweep_prediction_lineage: list[dict[str, str]] = []
    sweep_independent_source_rows = 0
    sweep_independent_predicted_rows = 0
    sweep_independent_unknown_rows = 0
    sweep_derived_source_rows = 0
    sweep_derived_predicted_rows = 0
    sweep_derived_unknown_rows = 0
    sweep_excluded_rows = 0
    for row in source_rows["sweep_runs"]:
        run_id = row["run_id"]
        derived = DERIVED_SWEEP_MARKER in run_id
        source_prediction_run_id = (
            run_id.split(DERIVED_SWEEP_MARKER, 1)[0] if derived else run_id
        )
        if derived:
            sweep_derived_source_rows += 1
        else:
            sweep_independent_source_rows += 1

        source_row = trajectory_by_run.get(source_prediction_run_id) if derived else None
        reason: str | None = None
        prediction: RunPrediction | None = None
        metadata = protocol_trajectory_by_run.get(run_id)
        identity: dict[str, str] | None = None
        if run_id in excluded_runs:
            sweep_excluded_rows += 1
            continue
        if derived and source_row is not None and _is_holdout_metadata(source_row):
            sweep_excluded_rows += 1
            continue
        if not derived and metadata is not None and _is_holdout_metadata(metadata):
            sweep_excluded_rows += 1
            continue
        if derived:
            if source_row is None:
                reason = "shared-baseline source trajectory absent"
            elif source_prediction_run_id not in baseline_predictions:
                reason = "shared-baseline prediction absent; derived copy remains unknown"
            else:
                prediction = baseline_predictions[source_prediction_run_id]
                try:
                    identity = _sweep_identity(
                        sweep_row=row,
                        protocol_row=metadata,
                        source_row=source_row,
                        derived=True,
                    )
                except D9PredictionError as exc:
                    reason = str(exc)
        else:
            if metadata is None:
                reason = "protocol trajectory metadata absent; prediction coverage unknown"
            else:
                try:
                    prediction = _predict_run(
                        simulator,
                        run_id=run_id,
                        metadata=metadata,
                        tool_rows=protocol_tool_by_run.get(run_id, []),
                        model_rows=protocol_model_by_run.get(run_id, []),
                        actions=actions,
                        hardware=hardware,
                    )
                    identity = _sweep_identity(
                        sweep_row=row,
                        protocol_row=metadata,
                        derived=False,
                    )
                except D9PredictionError as exc:
                    reason = str(exc)
        if prediction is None:
            if derived:
                sweep_derived_unknown_rows += 1
            else:
                sweep_independent_unknown_rows += 1
            unknown.append(
                {
                    "scope": "sweep_derived_baseline" if derived else "sweep_independent",
                    "run_id": run_id,
                    "source_prediction_run_id": source_prediction_run_id,
                    "instance_id": (
                        source_row.get("instance_id")
                        if source_row is not None
                        else metadata.get("instance_id") if metadata else None
                    ),
                    "reason": reason,
                }
            )
            continue
        if identity is None:
            raise D9PredictionError(f"{run_id}: missing inline sweep identity")
        if derived:
            sweep_derived_predicted_rows += 1
        else:
            sweep_independent_predicted_rows += 1
        output = dict(row)
        output.update(
            {
                **identity,
                **_prediction_fields(
                    prediction,
                    source_prediction_run_id=source_prediction_run_id,
                    prediction_row_kind=(
                        "derived_shared_baseline" if derived else "independent_protocol"
                    ),
                ),
            }
        )
        sweep_output.append(output)
        if derived:
            sweep_prediction_lineage.append(
                {
                    "derived_run_id": run_id,
                    "source_prediction_run_id": source_prediction_run_id,
                    "sweep_parameter": row["sweep_parameter"],
                    "sweep_value": row["sweep_value"],
                }
            )

    output_run_ids = {row["run_id"] for row in baseline_output}
    filtered_evaluator = [row for row in evaluator_rows if row.get("run_id") in output_run_ids]

    source_selection_path = selection_path or (source_dir / "step3_selection.json")
    selection_selected: dict[str, Any] | None = None
    desired_instance = "django__django-16901"
    desired_run = ""
    if source_selection_path.is_file():
        selection_payload = _read_json(source_selection_path)
        if isinstance(selection_payload, Mapping):
            selected_payload = selection_payload.get("selected")
            if isinstance(selected_payload, Mapping):
                desired_instance = str(selected_payload.get("instance_id") or desired_instance)
                desired_run = str(selected_payload.get("run_id") or "")
    selected_candidates = [row for row in baseline_output if row.get("instance_id") == desired_instance]
    selected_candidates.sort(key=lambda item: item.get("run_id", ""))
    selected_row = next((row for row in selected_candidates if row.get("run_id") == desired_run), None)
    if selected_row is None and selected_candidates:
        selected_row = selected_candidates[0]
    if selected_row is not None:
        selection_selected = _selection_row(selected_row)
    else:
        unknown.append(
            {
                "scope": "step3_selection",
                "run_id": desired_run or None,
                "instance_id": desired_instance,
                "reason": "preselected instance is absent from predicted subset",
            }
        )

    metadata = {row["run_id"]: row for row in protocol_rows["trajectories"]}
    sweep_values: dict[str, set[str]] = defaultdict(set)
    for row in source_rows["sweep_runs"]:
        sweep_values[row["sweep_parameter"]].add(row["sweep_value"])
    coverage = {
        "baseline_source_runs": len(baseline_source_rows),
        "baseline_predicted_runs": len(baseline_output),
        "baseline_unknown_runs": sum(item["scope"] == "baseline" for item in unknown),
        "baseline_source_tool_events": len(source_rows["tool_events"]),
        "baseline_predicted_tool_events": len(output_tool_rows),
        "baseline_source_model_events": len(source_rows["model_events"]),
        "baseline_predicted_model_events": len(output_model_rows),
        "sweep_source_runs": len(source_rows["sweep_runs"]),
        "sweep_predicted_runs": len(sweep_output),
        "sweep_unknown_runs": sum(
            str(item["scope"]).startswith("sweep") for item in unknown
        ),
        "sweep_independent_source_runs": sweep_independent_source_rows,
        "sweep_independent_predicted_runs": sweep_independent_predicted_rows,
        "sweep_independent_unknown_runs": sweep_independent_unknown_rows,
        "sweep_derived_source_rows": sweep_derived_source_rows,
        "sweep_derived_predicted_rows": sweep_derived_predicted_rows,
        "sweep_derived_unknown_rows": sweep_derived_unknown_rows,
        "sweep_derived_rows_are_shared_baseline_copies": True,
        "sweep_derived_copies_not_extra_measurements": sweep_derived_predicted_rows,
        "sweep_rows_excluded": sweep_excluded_rows,
        "sweep_parameter_values": {
            parameter: sorted(values, key=_sweep_value_sort_key)
            for parameter, values in sorted(sweep_values.items())
        },
        "step3_selection_instance_id": desired_instance,
        "step3_selection_run_id": selection_selected.get("run_id") if selection_selected else None,
        "step3_selection_available": selection_selected is not None,
        "supported_frozen_run_ids": len(supported_runs),
        "recovered_action_run_ids": sorted(recovered_action_artifacts),
        "recovered_action_run_count": len(recovered_action_artifacts),
        "recovered_action_artifacts": recovered_action_artifacts,
        "served_outside_frozen_calibration_support": sorted(
            run_id
            for run_id in baseline_predictions
            if run_id not in supported_runs
        ),
        "protocol_trajectory_metadata_runs": len(metadata),
    }
    exclusion = {
        "instance_id": HOLDOUT_INSTANCE_ID,
        "excluded_run_ids": sorted(excluded_runs),
        "metadata_rows_identifying_exclusion": excluded_metadata_rows,
        "baseline_source_rows_excluded": sum(
            row["run_id"] in excluded_runs or row.get("instance_id") == HOLDOUT_INSTANCE_ID
            for row in baseline_source_rows
        ),
        "baseline_tool_event_rows_excluded": sum(
            row["run_id"] in excluded_runs for row in source_rows["tool_events"]
        ),
        "baseline_model_event_rows_excluded": sum(
            row["run_id"] in excluded_runs for row in source_rows["model_events"]
        ),
        "sweep_rows_excluded": sweep_excluded_rows,
    }

    tables = {
        "trajectories": baseline_output,
        "tool_events": output_tool_rows,
        "model_events": output_model_rows,
        "sweep_runs": sweep_output,
        "evaluator_provenance": filtered_evaluator,
    }
    source_fields["trajectories"] = _add_fields(source_fields["trajectories"], PREDICTED_TRAJECTORY_COLUMNS)
    source_fields["tool_events"] = _add_fields(source_fields["tool_events"], PREDICTED_EVENT_COLUMNS)
    source_fields["model_events"] = _add_fields(source_fields["model_events"], PREDICTED_EVENT_COLUMNS)
    source_fields["sweep_runs"] = _add_fields(source_fields["sweep_runs"], PREDICTED_SWEEP_COLUMNS)
    if evaluator_fields:
        source_fields["evaluator_provenance"] = evaluator_fields

    return PredictionBundle(
        source_fields=source_fields,
        tables=tables,
        selection_selected=selection_selected,
        coverage=coverage,
        exclusion=exclusion,
        unknown=unknown,
        sweep_prediction_lineage=sweep_prediction_lineage,
        source_hashes=source_hashes,
    )


def _write_bytes(path: Path, payload: bytes, *, force: bool) -> str:
    sidecar = Path(str(path) + ".sha256")
    digest = sha256_bytes(payload)
    sidecar_payload = f"{digest}  {path.name}\n".encode("ascii")
    if not force and (path.exists() or sidecar.exists()):
        if path.is_file() and sidecar.is_file() and path.read_bytes() == payload and sidecar.read_bytes() == sidecar_payload:
            return digest
        raise FileExistsError(f"refusing to overwrite generated artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    sidecar.write_bytes(sidecar_payload)
    return digest


def _write_csv(path: Path, fields: Sequence[str], rows: Sequence[Mapping[str, Any]], *, force: bool) -> str:
    from io import StringIO

    stream = StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(fields), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: row.get(field, "") for field in fields})
    return _write_bytes(path, stream.getvalue().encode("utf-8"), force=force)


def _write_json(path: Path, value: Any, *, force: bool) -> str:
    payload = (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    return _write_bytes(path, payload, force=force)


def _assert_output_path_is_new(output_dir: Path) -> None:
    """Keep the generated export out of immutable historical snapshots."""

    candidate = output_dir.expanduser().resolve()
    for immutable_root in IMMUTABLE_SNAPSHOT_ROOTS:
        root = immutable_root.resolve()
        if candidate == root or root in candidate.parents:
            raise D9PredictionError(
                f"refusing predicted export under immutable historical snapshot: {candidate}"
            )


def materialize_inputs(
    bundle: PredictionBundle,
    output_dir: Path,
    *,
    d1_headline_source: Path | None = None,
    sweep_metadata_source: Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Write the new predicted-input tables and a fresh selection binding."""

    input_dir = output_dir / "figures-input"
    output_hashes: dict[str, str] = {}
    for name in ("trajectories", "tool_events", "model_events", "sweep_runs"):
        output_hashes[f"figures-input/{name}.csv"] = _write_csv(
            input_dir / f"{name}.csv",
            bundle.source_fields[name],
            bundle.tables[name],
            force=force,
        )
    if "evaluator_provenance" in bundle.source_fields:
        output_hashes["figures-input/evaluator_provenance.csv"] = _write_csv(
            input_dir / "evaluator_provenance.csv",
            bundle.source_fields["evaluator_provenance"],
            bundle.tables["evaluator_provenance"],
            force=force,
        )

    if d1_headline_source is not None and d1_headline_source.is_file():
        output_hashes["figures-input/d1_headline_metrics.json"] = _write_bytes(
            input_dir / "d1_headline_metrics.json",
            d1_headline_source.read_bytes(),
            force=force,
        )
    if sweep_metadata_source is not None and sweep_metadata_source.is_file():
        output_hashes["figures-input/sweep_metadata.jsonl"] = _write_bytes(
            input_dir / "sweep_metadata.jsonl",
            sweep_metadata_source.read_bytes(),
            force=force,
        )

    selection_payload: dict[str, Any] | None = None
    if bundle.selection_selected is not None:
        selection_path = input_dir / "step3_selection.json"
        selection_payload = {
            "schema_version": "assignment.step3-selection.v1",
            "source_trajectories_sha256": output_hashes["figures-input/trajectories.csv"],
            "metric": "sum(tool_event.predicted_wall_ms) / sum(model_event.predicted_wall_ms)",
            "eligible_count": bundle.coverage["baseline_predicted_runs"],
            "selection_policy": "preserved preselected instance; same run when covered; no re-ranking",
            "selected": bundle.selection_selected,
        }
        output_hashes["figures-input/step3_selection.json"] = _write_json(
            selection_path, selection_payload, force=force
        )
    return {
        "input_dir": str(input_dir),
        "output_hashes": output_hashes,
        "selection": selection_payload,
    }


def _inventory(root: Path, *, exclude: set[str] | None = None) -> dict[str, str]:
    excluded = exclude or set()
    result: dict[str, str] = {}
    if not root.is_dir():
        return result
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name not in excluded:
            result[str(path.relative_to(root))] = sha256_path(path)
    return result


def renderer_command(
    *,
    renderer: Path,
    input_dir: Path,
    figures_dir: Path,
    d1_headline_path: Path | None = None,
    sweep_metadata_path: Path | None = None,
    force: bool,
) -> list[str]:
    """Return B's required renderer invocation for predicted latency."""

    command = [
        sys.executable,
        str(renderer),
        "--trajectories",
        str(input_dir / "trajectories.csv"),
        "--tool-events",
        str(input_dir / "tool_events.csv"),
        "--model-events",
        str(input_dir / "model_events.csv"),
        "--sweep-runs",
        str(input_dir / "sweep_runs.csv"),
        "--step3-selection",
        str(input_dir / "step3_selection.json"),
        "--output-dir",
        str(figures_dir),
        "--latency-kind",
        "predicted",
    ]
    if d1_headline_path is not None and d1_headline_path.is_file():
        command.extend(["--d1-headline-metrics", str(d1_headline_path)])
    if sweep_metadata_path is not None and sweep_metadata_path.is_file():
        command.extend(["--sweep-metadata", str(sweep_metadata_path)])
    if force:
        command.append("--force")
    return command


def invoke_renderer(command: Sequence[str]) -> dict[str, Any]:
    """Invoke the root renderer and retain an auditable command/result."""

    completed = subprocess.run(command, check=False, text=True, capture_output=True)
    if completed.returncode != 0:
        raise D9PredictionError(
            "predicted renderer failed (exit "
            f"{completed.returncode}):\n$ {shlex.join(list(command))}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return {
        "invoked": True,
        "command": list(command),
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "returncode": completed.returncode,
    }


def _report_markdown(manifest: Mapping[str, Any]) -> str:
    coverage = manifest["coverage"]
    exclusion = manifest["holdout_exclusion"]
    unknown = manifest["unknown_coverage"]
    lines = [
        "# D9 predicted Steps 1--3 export",
        "",
        "This snapshot is a conditional workload-latency simulation from the frozen `assignment.workload-simulator.v3` API.",
        "It is conditioned on the supplied realized action/event sequence and token descriptors; it is not a prospective task-success or trajectory forecast.",
        "",
        "## Prediction contract",
        "",
        "- Latency source: frozen-fit v3 API outputs (`frozen_v3_api`).",
        "- Accuracy source: observed evaluator `official_resolved` values retained from the canonical inputs.",
        "- Realized `output_tokens` are labeled as supplied workload descriptors where used by the frozen GPU model; they are not pre-request-known features.",
        "- No measured event duration, E2E duration, observed residual, or observed overhead is passed into prediction.",
        "- E2E latency is the frozen API's predicted event sum plus its frozen nonnegative overhead component evaluated from event counts; the adapter supplies no current-row residual.",
        "- The declared hardware profile is an input assumption; this export does not establish cross-hardware validation.",
        "- Cross-validation reviewed predictions are retained as a diagnostic reference only and are not substituted for frozen-fit API predictions here.",
        "",
        "## Coverage",
        "",
        f"- Baseline trajectories: {coverage['baseline_predicted_runs']} predicted of {coverage['baseline_source_runs']} source rows; {coverage['baseline_unknown_runs']} remain unknown.",
        f"- Baseline tool events: {coverage['baseline_predicted_tool_events']} predicted of {coverage['baseline_source_tool_events']} source rows.",
        f"- Baseline model events: {coverage['baseline_predicted_model_events']} predicted of {coverage['baseline_source_model_events']} source rows.",
        f"- Sweep rows: {coverage['sweep_predicted_runs']} predicted of {coverage['sweep_source_runs']} source rows; {coverage['sweep_unknown_runs']} remain unknown and {coverage['sweep_rows_excluded']} are holdout-excluded.",
        f"- Sweep coverage split: {coverage['sweep_independent_predicted_runs']}/{coverage['sweep_independent_source_runs']} independent protocol rows and {coverage['sweep_derived_predicted_rows']}/{coverage['sweep_derived_source_rows']} derived shared-baseline copies.",
        f"- Derived baseline copies reuse their source prediction and are not extra measurements; source IDs are recorded in `source_prediction_run_id`.",
        f"- Frozen calibration support rows: {coverage['supported_frozen_run_ids']}; exact local action recovery served {coverage['recovered_action_run_count']} run(s) outside that manifest.",
        f"- Recovered action run IDs: {json.dumps(coverage['recovered_action_run_ids'])}.",
        f"- Parameter settings present: {json.dumps(coverage['sweep_parameter_values'], sort_keys=True)}.",
        f"- Step 3 selection: `{coverage['step3_selection_instance_id']}` / `{coverage['step3_selection_run_id']}`; bound to the new predicted trajectories hash.",
        "",
        "## Holdout exclusion",
        "",
        f"The entire instance `{exclusion['instance_id']}` is omitted by ID/path metadata. No predicted table row contains that instance, its known run IDs, or their events.",
        f"- Excluded run IDs: {', '.join(exclusion['excluded_run_ids'])}.",
        f"- Excluded source baseline trajectories: {exclusion['baseline_source_rows_excluded']}; tool events: {exclusion['baseline_tool_event_rows_excluded']}; model events: {exclusion['baseline_model_event_rows_excluded']}; sweep rows: {exclusion['sweep_rows_excluded']}.",
        "",
        "## Unknown coverage",
        "",
    ]
    if unknown:
        lines.extend(
            f"- `{item.get('scope')}` `{item.get('run_id') or 'n/a'}` `{item.get('instance_id') or 'n/a'}`: {item.get('reason')}"
            + (
                f" [support_status={item['support_status']}]"
                if item.get("support_status")
                else ""
            )
            for item in unknown
        )
    else:
        lines.append("- None.")
    lines.extend(
        [
            "",
            "## Integrity and rendering",
            "",
            f"- v3 model SHA-256 before prediction: `{manifest['v3_model']['file_sha256_before']}`.",
            f"- v3 model SHA-256 after prediction/rendering: `{manifest['v3_model']['file_sha256_after']}`.",
            f"- Renderer invoked with `--latency-kind predicted`: `{manifest['renderer']['invoked']}`.",
            "- The original reconciliation report was not passed to the predicted renderer; predicted subset selection has a new input hash binding.",
            "",
            "### Renderer command",
            "",
            "```text",
            shlex.join(manifest["renderer"]["command"]) if manifest["renderer"].get("command") else "(not invoked)",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def generate_export(
    *,
    source_dir: Path,
    protocol_dir: Path,
    model_path: Path,
    action_map_path: Path,
    output_dir: Path,
    renderer: Path,
    hardware: HardwareProfile,
    selection_path: Path | None = None,
    recovered_trajectory_paths: Mapping[str, Path] | None = None,
    skip_render: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Generate predicted inputs, invoke B, and write the sealed audit report."""

    _assert_output_path_is_new(output_dir)
    simulator, model_metadata = load_frozen_model(model_path)
    bundle = build_prediction_bundle(
        source_dir=source_dir,
        protocol_dir=protocol_dir,
        action_map_path=action_map_path,
        simulator=simulator,
        hardware=hardware,
        selection_path=selection_path,
        recovered_trajectory_paths=recovered_trajectory_paths,
    )
    materialized = materialize_inputs(
        bundle,
        output_dir,
        d1_headline_source=source_dir / "d1_headline_metrics.json",
        sweep_metadata_source=source_dir / "sweep_metadata.jsonl",
        force=force,
    )
    input_dir = output_dir / "figures-input"
    figures_dir = output_dir / "figures"
    command = renderer_command(
        renderer=renderer,
        input_dir=input_dir,
        figures_dir=figures_dir,
        d1_headline_path=input_dir / "d1_headline_metrics.json",
        sweep_metadata_path=input_dir / "sweep_metadata.jsonl",
        force=force,
    )
    render_result = {"invoked": False, "command": command, "stdout": "", "stderr": "", "returncode": None}
    if not skip_render:
        if bundle.selection_selected is None:
            raise D9PredictionError("cannot render Step 3 without the preselected instance")
        render_result = invoke_renderer(command)

    after = sha256_path(model_path)
    if after != model_metadata["file_sha256_before"]:
        raise D9PredictionError(
            "frozen v3 model changed during prediction/rendering: "
            f"before={model_metadata['file_sha256_before']} after={after}"
        )
    model_metadata = {
        **model_metadata,
        "file_sha256_after": after,
        "hash_unchanged": after == model_metadata["file_sha256_before"],
    }

    manifest = {
        "schema_version": "assignment.d9-predicted-figures.v1",
        "snapshot_kind": "d9-predicted",
        "source_figures_input": str(source_dir),
        "protocol_input": str(protocol_dir),
        "action_map": str(action_map_path),
        "source_hashes": bundle.source_hashes,
        "v3_model": model_metadata,
        "hardware_profile": hardware.to_mapping(),
        "prediction_source": {
            "kind": "frozen_fit_api",
            "api_schema": WORKLOAD_MODEL_SCHEMA,
            "fit_or_calibration_performed_in_adapter": False,
            "cross_validation_predictions_used_for_figures": False,
            "conditional_on_realized_workload": True,
            "observed_output_tokens_role": "supplied realized workload descriptor where used by the frozen GPU API",
            "observed_accuracy_role": "retained evaluator outcome label only",
            "measured_duration_or_residual_used_as_predictor": False,
        },
        "coverage": bundle.coverage,
        "sweep_prediction_lineage": {
            "field": "source_prediction_run_id",
            "derived_row_kind": "derived_shared_baseline",
            "rows": bundle.sweep_prediction_lineage,
        },
        "holdout_exclusion": bundle.exclusion,
        "unknown_coverage": bundle.unknown,
        "selection": materialized["selection"],
        "renderer": render_result,
        "renderer_contract": {
            "latency_kind": "predicted",
            "accuracy_kind": "observed_evaluator_outcome",
            "reconciliation_report_passed": False,
            "d1_original_headline_override": False,
        },
        "output_inventory": {
            "figures_input": _inventory(output_dir / "figures-input"),
            "figures": _inventory(output_dir / "figures"),
        },
    }
    manifest_path = output_dir / "d9_predicted_manifest.json"
    _write_json(manifest_path, manifest, force=force)
    report = _report_markdown(manifest)
    _write_bytes(output_dir / "d9_predicted_report.md", report.encode("utf-8"), force=force)
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    def parse_recovery_spec(value: str) -> tuple[str, Path]:
        run_id, separator, path = value.partition("=")
        if not separator or not run_id.strip() or not path.strip():
            raise argparse.ArgumentTypeError(
                "expected RUN_ID=PATH for --recovered-trajectory"
            )
        return run_id.strip(), Path(path).expanduser()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-figures-input", "--figures-input", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--protocol-input", type=Path, default=DEFAULT_PROTOCOL)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--action-map", type=Path, default=DEFAULT_ACTIONS)
    parser.add_argument("--hardware-profile", type=Path)
    parser.add_argument("--selection", type=Path)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--renderer", type=Path, default=DEFAULT_RENDERER)
    parser.add_argument(
        "--recovered-trajectory",
        action="append",
        type=parse_recovery_spec,
        default=[],
        metavar="RUN_ID=PATH",
        help=(
            "use an exact local runner trajectory to recover missing action support; "
            "the run ID must match the source/protocol rows"
        ),
    )
    parser.add_argument("--skip-render", "--no-render", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    recovered_trajectory_paths: dict[str, Path] = {}
    for run_id, path in args.recovered_trajectory:
        if run_id in recovered_trajectory_paths:
            parser.error(f"duplicate --recovered-trajectory run ID: {run_id}")
        recovered_trajectory_paths[run_id] = path
    hardware = _load_hardware(args.hardware_profile)
    manifest = generate_export(
        source_dir=args.source_figures_input,
        protocol_dir=args.protocol_input,
        model_path=args.model,
        action_map_path=args.action_map,
        output_dir=args.output_dir,
        renderer=args.renderer,
        hardware=hardware,
        selection_path=args.selection,
        recovered_trajectory_paths=recovered_trajectory_paths,
        skip_render=args.skip_render,
        force=args.force,
    )
    print(
        json.dumps(
            {
                "output_dir": str(args.output_dir),
                "baseline_predicted_runs": manifest["coverage"]["baseline_predicted_runs"],
                "sweep_predicted_runs": manifest["coverage"]["sweep_predicted_runs"],
                "renderer_invoked": manifest["renderer"]["invoked"],
                "v3_model_sha256": manifest["v3_model"]["file_sha256_after"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (D9PredictionError, FileExistsError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
