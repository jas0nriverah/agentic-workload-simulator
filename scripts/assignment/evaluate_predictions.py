#!/usr/bin/env python3
"""Freeze calibration-only event predictions, then score revealed holdouts.

The two subcommands intentionally use different input files:

``fit-freeze``
    Reads calibration labels and feature-only holdout declarations.  It cannot
    accept a holdout labels path.

``score``
    Verifies the frozen manifest and its SHA-256 sidecar before reading a
    separately revealed labels file.  It exits nonzero unless every available
    tool/model event and every trajectory has absolute percentage error <=25%.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from math import isfinite
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from agentic_sim.assignment.event_simulator import (  # noqa: E402
    AssignmentEventSimulator,
    EventSimulatorError,
    freeze_prediction_manifest,
    verify_frozen_prediction_manifest,
)


GATE_PERCENT = 25.0
PREPARE_RECEIPT_SCHEMA = "assignment.event-protocol-prepare-receipt.v1"
CAPTURE_RECEIPT_SCHEMA = "assignment.event-feature-capture-receipt.v1"
STATIC_PROTOCOL_MODE = "static_predeclared"
STATIC_PROTOCOL_SCOPE = "predeclared_static_workload_only"


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _sidecar_path(path: Path) -> Path:
    return path.with_suffix(".sha256")


def _verify_hashed_object(path: Path, *, kind: str) -> tuple[dict[str, Any], str]:
    try:
        payload = path.read_bytes()
        value = json.loads(payload)
    except (OSError, json.JSONDecodeError) as exc:
        raise EventSimulatorError(f"cannot read {kind}: {exc}") from exc
    if not isinstance(value, dict):
        raise EventSimulatorError(f"{kind} must contain a JSON object")
    digest = hashlib.sha256(payload).hexdigest()
    try:
        actual = _sidecar_path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise EventSimulatorError(f"cannot read {kind} SHA-256 sidecar: {exc}") from exc
    if actual != f"{digest}  {path.name}\n":
        raise EventSimulatorError(f"{kind} or SHA-256 sidecar was tampered with")
    return value, digest


def _prepare_context(
    receipt_path: Path, calibration_path: Path, holdout_features_path: Path
) -> tuple[dict[str, Any], str, str, str]:
    receipt, receipt_digest = _verify_hashed_object(receipt_path, kind="prepare receipt")
    required = {
        "schema_version", "protocol_mode", "protocol_scope", "split_manifest_sha256",
        "hardware_profile_sha256", "calibration_sha256", "holdout_features_sha256",
        "capture_receipt_sha256", "captured_features_sha256", "runtime_manifest_sha256",
        "capture_script_sha256", "capture_chronology_witness",
        "calibration_run_ids", "holdout_run_ids", "holdout_labels_accessed",
        "calibration_tool_declared_read_bytes", "calibration_tool_declared_write_bytes",
        "forbidden_holdout_feature_classes",
    }
    if set(receipt) != required or receipt.get("schema_version") != PREPARE_RECEIPT_SCHEMA:
        raise EventSimulatorError("prepare receipt schema is invalid")
    if receipt.get("protocol_mode") != STATIC_PROTOCOL_MODE or receipt.get("protocol_scope") != STATIC_PROTOCOL_SCOPE:
        raise EventSimulatorError("only the sealed static-predeclared protocol mode is supported")
    calibration, calibration_digest = _verify_hashed_object(calibration_path, kind="calibration set")
    holdout, holdout_digest = _verify_hashed_object(holdout_features_path, kind="holdout feature set")
    if calibration.get("schema_version") != "assignment.event-calibration-set.v1":
        raise EventSimulatorError("calibration set schema is invalid")
    if holdout.get("schema_version") != "assignment.event-holdout-features.v1":
        raise EventSimulatorError("holdout feature set schema is invalid")
    if holdout.get("protocol_mode") != STATIC_PROTOCOL_MODE:
        raise EventSimulatorError("fit-freeze rejects adaptive or non-predeclared holdout features")
    if receipt.get("calibration_sha256") != calibration_digest or receipt.get("holdout_features_sha256") != holdout_digest:
        raise EventSimulatorError("prepare receipt does not bind the supplied calibration/features")
    capture_path = receipt_path.parent / "capture_receipt.json"
    capture, capture_digest = _verify_hashed_object(capture_path, kind="capture receipt")
    capture_required = {
        "schema_version", "protocol_mode", "protocol_scope", "split_manifest_sha256",
        "hardware_profile_sha256", "runtime_manifest_sha256", "feature_journal_sha256",
        "captured_features_sha256", "split_manifest_path", "hardware_profile_path",
        "runtime_manifest_path", "feature_journal_path", "captured_features_path",
        "capture_script_path", "capture_script_sha256", "holdout_run_ids",
        "holdout_labels_accessed", "target_derived_fields_rejected", "chronology_witness",
    }
    if set(capture) != capture_required or capture.get("schema_version") != CAPTURE_RECEIPT_SCHEMA:
        raise EventSimulatorError("capture receipt schema is invalid")
    if capture_digest != receipt.get("capture_receipt_sha256"):
        raise EventSimulatorError("prepare receipt does not bind the capture receipt")
    if capture.get("captured_features_sha256") != holdout_digest or receipt.get("captured_features_sha256") != holdout_digest:
        raise EventSimulatorError("capture receipt does not bind the copied holdout features")
    if capture.get("runtime_manifest_sha256") != receipt.get("runtime_manifest_sha256"):
        raise EventSimulatorError("prepare receipt does not bind the runtime manifest")
    return receipt, receipt_digest, calibration_digest, holdout_digest


def _verify_manifest_binding(
    manifest: Mapping[str, Any], *, receipt_digest: str, calibration_digest: str,
    holdout_features_digest: str, capture_receipt_digest: str,
    runtime_manifest_digest: str, hardware_digest: str,
) -> None:
    expected = {
        "protocol_mode": STATIC_PROTOCOL_MODE,
        "prepare_receipt_sha256": receipt_digest,
        "calibration_sha256": calibration_digest,
        "holdout_features_sha256": holdout_features_digest,
        "capture_receipt_sha256": capture_receipt_digest,
        "captured_features_sha256": holdout_features_digest,
        "runtime_manifest_sha256": runtime_manifest_digest,
        "hardware_profile_sha256": hardware_digest,
    }
    if manifest.get("event_protocol_binding") != expected:
        raise EventSimulatorError("frozen prediction manifest is not bound to verified prepare inputs")


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EventSimulatorError(f"cannot read JSON object {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise EventSimulatorError(f"{path} must contain a JSON object")
    return value


def _strict_root(value: Mapping[str, Any], allowed: set[str], schema: str, kind: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise EventSimulatorError(f"unknown {kind} field(s): {', '.join(unknown)}")
    if value.get("schema_version") != schema:
        raise EventSimulatorError(f"unsupported {kind} schema_version")


def fit_and_freeze(
    calibration_path: Path, holdout_features_path: Path, prediction_manifest_path: Path,
    prepare_receipt_path: Path,
) -> dict[str, Any]:
    receipt, receipt_digest, calibration_digest, holdout_digest = _prepare_context(
        prepare_receipt_path, calibration_path, holdout_features_path
    )
    calibration, verified_calibration_digest = _verify_hashed_object(calibration_path, kind="calibration set")
    if verified_calibration_digest != calibration_digest:
        raise EventSimulatorError("calibration hash changed during verification")
    _strict_root(
        calibration,
        {"schema_version", "tool_events", "model_events", "trajectories"},
        "assignment.event-calibration-set.v1",
        "calibration set",
    )
    holdout, verified_holdout_digest = _verify_hashed_object(
        holdout_features_path, kind="holdout feature set"
    )
    if verified_holdout_digest != holdout_digest:
        raise EventSimulatorError("holdout feature hash changed during verification")
    _strict_root(
        holdout,
        {"schema_version", "protocol_mode", "tool_events", "model_events"},
        "assignment.event-holdout-features.v1",
        "holdout feature set",
    )
    for name in ("tool_events", "model_events", "trajectories"):
        if not isinstance(calibration.get(name), list):
            raise EventSimulatorError(f"calibration {name} must be an array")
    for name in ("tool_events", "model_events"):
        if not isinstance(holdout.get(name), list):
            raise EventSimulatorError(f"holdout {name} must be an array")

    simulator = AssignmentEventSimulator.fit(
        calibration["tool_events"],
        calibration["model_events"],
        calibration["trajectories"],
    )
    manifest = simulator.build_prediction_manifest(holdout["tool_events"], holdout["model_events"])
    manifest["event_protocol_binding"] = {
        "protocol_mode": STATIC_PROTOCOL_MODE,
        "prepare_receipt_sha256": receipt_digest,
        "calibration_sha256": calibration_digest,
        "holdout_features_sha256": holdout_digest,
        "capture_receipt_sha256": receipt["capture_receipt_sha256"],
        "captured_features_sha256": receipt["captured_features_sha256"],
        "runtime_manifest_sha256": receipt["runtime_manifest_sha256"],
        "hardware_profile_sha256": receipt["hardware_profile_sha256"],
    }
    digest = freeze_prediction_manifest(manifest, prediction_manifest_path)
    manifest, verified = verify_frozen_prediction_manifest(prediction_manifest_path)
    if digest != verified:
        raise EventSimulatorError("prediction digest changed immediately after freeze")
    return {
        "status": "predictions_frozen",
        "prediction_manifest": str(prediction_manifest_path),
        "prediction_manifest_sha256": digest,
        "prepare_receipt_sha256": receipt_digest,
        "calibration_sha256": calibration_digest,
        "holdout_features_sha256": holdout_digest,
        "hardware_profile_sha256": receipt["hardware_profile_sha256"],
        "calibration_run_count": len(manifest["calibration_run_ids"]),
        "holdout_trajectory_count": len(manifest["trajectory_predictions"]),
        "tool_prediction_count": len(manifest["tool_predictions"]),
        "model_prediction_count": len(manifest["model_predictions"]),
        "holdout_labels_accessed": False,
    }


def _positive_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EventSimulatorError(f"{name} must be a positive finite number")
    result = float(value)
    if not isfinite(result) or result <= 0:
        raise EventSimulatorError(f"{name} must be a positive finite number")
    return result


def _label_index(
    rows: Any,
    *,
    id_field: str,
    expected: Mapping[str, Mapping[str, Any]],
    kind: str,
    require_all_available: bool,
    row_schema: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(rows, list):
        raise EventSimulatorError(f"holdout {kind} labels must be an array")
    labels: dict[str, Mapping[str, Any]] = {}
    allowed = {"schema_version", id_field, "run_id", "status", "observed_ms", "unavailable_reason"}
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise EventSimulatorError(f"{kind} label {index} must be a mapping")
        unknown = sorted(set(row) - allowed)
        if unknown:
            raise EventSimulatorError(f"unknown {kind} label field(s): {', '.join(unknown)}")
        if row.get("schema_version") != row_schema:
            raise EventSimulatorError(f"unsupported {kind} label schema_version")
        identifier = row.get(id_field)
        if not isinstance(identifier, str) or not identifier:
            raise EventSimulatorError(f"{kind} label requires {id_field}")
        if identifier in labels:
            raise EventSimulatorError(f"duplicate {kind} label: {identifier}")
        labels[identifier] = row
    if set(labels) != set(expected):
        missing = sorted(set(expected) - set(labels))
        extra = sorted(set(labels) - set(expected))
        raise EventSimulatorError(
            f"{kind} label coverage mismatch; missing={missing}, extra={extra}"
        )

    scored: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []
    for identifier in sorted(expected):
        prediction = expected[identifier]
        label = labels[identifier]
        if label.get("run_id") != prediction.get("run_id"):
            raise EventSimulatorError(f"{kind} label run_id mismatch for {identifier}")
        status = label.get("status")
        if status == "unavailable":
            reason = label.get("unavailable_reason")
            if require_all_available:
                raise EventSimulatorError(f"trajectory {identifier} is unavailable: {reason}")
            if not isinstance(reason, str) or not reason.strip():
                raise EventSimulatorError(f"unavailable {kind} label requires a reason")
            if label.get("observed_ms") not in (None, ""):
                raise EventSimulatorError(f"unavailable {kind} label cannot carry observed_ms")
            unavailable.append({id_field: identifier, "run_id": label["run_id"], "reason": reason})
            continue
        if status != "completed":
            raise EventSimulatorError(f"{kind} label status must be completed or unavailable")
        if label.get("unavailable_reason") not in (None, ""):
            raise EventSimulatorError(f"completed {kind} label cannot have unavailable_reason")
        observed = _positive_number(label.get("observed_ms"), "observed_ms")
        predicted = _positive_number(prediction.get("predicted_ms"), "predicted_ms")
        ape = abs(predicted - observed) / observed * 100.0
        scored.append(
            {
                id_field: identifier,
                "run_id": label["run_id"],
                "predicted_ms": predicted,
                "observed_ms": observed,
                "absolute_percentage_error": ape,
                "within_25_percent": ape <= GATE_PERCENT,
            }
        )
    return scored, unavailable


def _summary(scored: list[dict[str, Any]], unavailable_count: int) -> dict[str, Any]:
    if not scored:
        raise EventSimulatorError("at least one available label is required for each metric class")
    errors = [row["absolute_percentage_error"] for row in scored]
    return {
        "available_count": len(scored),
        "unavailable_count": unavailable_count,
        "mean_absolute_percentage_error": sum(errors) / len(errors),
        "max_absolute_percentage_error": max(errors),
        "all_available_within_25_percent": all(error <= GATE_PERCENT for error in errors),
    }


def _prediction_index(
    rows: list[Any], *, id_field: str, kind: str
) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise EventSimulatorError(f"frozen {kind} prediction {index} must be a mapping")
        identifier = row.get(id_field)
        run_id = row.get("run_id")
        if not isinstance(identifier, str) or not identifier:
            raise EventSimulatorError(f"frozen {kind} prediction requires {id_field}")
        if not isinstance(run_id, str) or not run_id:
            raise EventSimulatorError(f"frozen {kind} prediction requires run_id")
        _positive_number(row.get("predicted_ms"), "predicted_ms")
        if identifier in result:
            raise EventSimulatorError(f"frozen manifest contains duplicate {kind} predictions")
        result[identifier] = row
    return result


def evaluate_predictions(
    manifest_path: Path, labels_path: Path, prepare_receipt_path: Path
) -> dict[str, Any]:
    manifest, digest = verify_frozen_prediction_manifest(manifest_path)
    receipt, receipt_digest, calibration_digest, holdout_features_digest = _prepare_context(
        prepare_receipt_path,
        prepare_receipt_path.parent / "calibration.json",
        prepare_receipt_path.parent / "holdout_features.json",
    )
    _verify_manifest_binding(
        manifest,
        receipt_digest=receipt_digest,
        calibration_digest=calibration_digest,
        holdout_features_digest=holdout_features_digest,
        capture_receipt_digest=receipt["capture_receipt_sha256"],
        runtime_manifest_digest=receipt["runtime_manifest_sha256"],
        hardware_digest=receipt["hardware_profile_sha256"],
    )
    for name in ("tool_predictions", "model_predictions", "trajectory_predictions"):
        if not isinstance(manifest.get(name), list):
            raise EventSimulatorError(f"frozen manifest {name} must be an array")
    labels, labels_digest = _verify_hashed_object(labels_path, kind="holdout label set")
    _strict_root(
        labels,
        {
            "schema_version",
            "prediction_manifest_sha256",
            "prepare_receipt_sha256",
            "calibration_sha256",
            "holdout_features_sha256",
            "capture_receipt_sha256",
            "captured_features_sha256",
            "runtime_manifest_sha256",
            "hardware_profile_sha256",
            "tool_events",
            "model_events",
            "trajectories",
        },
        "assignment.event-holdout-labels.v1",
        "holdout label set",
    )
    if labels.get("prediction_manifest_sha256") != digest:
        raise EventSimulatorError("holdout labels are not bound to the frozen prediction hash")
    if (
        labels.get("prepare_receipt_sha256") != receipt_digest
        or labels.get("calibration_sha256") != calibration_digest
        or labels.get("holdout_features_sha256") != holdout_features_digest
        or labels.get("capture_receipt_sha256") != receipt["capture_receipt_sha256"]
        or labels.get("captured_features_sha256") != receipt["captured_features_sha256"]
        or labels.get("runtime_manifest_sha256") != receipt["runtime_manifest_sha256"]
        or labels.get("hardware_profile_sha256") != receipt["hardware_profile_sha256"]
    ):
        raise EventSimulatorError("holdout labels are not bound to the verified prepare inputs")

    tool_predictions = _prediction_index(
        manifest["tool_predictions"], id_field="event_id", kind="tool-event"
    )
    model_predictions = _prediction_index(
        manifest["model_predictions"], id_field="request_id", kind="model-event"
    )
    trajectory_predictions = _prediction_index(
        manifest["trajectory_predictions"], id_field="run_id", kind="trajectory"
    )

    tools, tool_unavailable = _label_index(
        labels.get("tool_events"),
        id_field="event_id",
        expected=tool_predictions,
        kind="tool-event",
        require_all_available=False,
        row_schema="assignment.tool-holdout-label.v1",
    )
    models, model_unavailable = _label_index(
        labels.get("model_events"),
        id_field="request_id",
        expected=model_predictions,
        kind="model-event",
        require_all_available=False,
        row_schema="assignment.model-holdout-label.v1",
    )
    trajectories, trajectory_unavailable = _label_index(
        labels.get("trajectories"),
        id_field="run_id",
        expected=trajectory_predictions,
        kind="trajectory",
        require_all_available=True,
        row_schema="assignment.trajectory-holdout-label.v1",
    )
    if trajectory_unavailable:
        raise EventSimulatorError("every trajectory must have an available E2E label")

    summaries = {
        "tool_events": _summary(tools, len(tool_unavailable)),
        "model_events": _summary(models, len(model_unavailable)),
        "trajectories": _summary(trajectories, 0),
    }
    coverage_complete = (
        summaries["tool_events"]["unavailable_count"] == 0
        and summaries["model_events"]["unavailable_count"] == 0
    )
    passed = (
        coverage_complete
        and all(item["all_available_within_25_percent"] for item in summaries.values())
    )
    return {
        "schema_version": "assignment.event-holdout-evaluation.v1",
        "prediction_manifest_sha256": digest,
        "prepare_receipt_sha256": receipt_digest,
        "calibration_sha256": calibration_digest,
        "holdout_features_sha256": holdout_features_digest,
        "capture_receipt_sha256": receipt["capture_receipt_sha256"],
        "captured_features_sha256": receipt["captured_features_sha256"],
        "runtime_manifest_sha256": receipt["runtime_manifest_sha256"],
        "hardware_profile_sha256": receipt["hardware_profile_sha256"],
        "holdout_labels_sha256": labels_digest,
        "gate_percent": GATE_PERCENT,
        "coverage_complete": coverage_complete,
        "passed": passed,
        "summaries": summaries,
        "tool_events": tools,
        "model_events": models,
        "trajectories": trajectories,
        "unavailable": {
            "tool_events": tool_unavailable,
            "model_events": model_unavailable,
        },
    }


def _write_report(path: Path, report: Mapping[str, Any]) -> str:
    """Atomically write an immutable report plus its exact SHA-256 sidecar."""
    payload = _canonical_bytes(report)
    digest = hashlib.sha256(payload).hexdigest()
    sidecar = _sidecar_path(path)
    if path.exists() or sidecar.exists():
        raise EventSimulatorError(f"refusing to overwrite existing score output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: list[tuple[Path, Path]] = []
    try:
        for destination, contents in ((path, payload), (sidecar, f"{digest}  {path.name}\n".encode("utf-8"))):
            descriptor, name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    fit = commands.add_parser("fit-freeze", help="fit on calibration labels and freeze holdout predictions")
    fit.add_argument("--calibration", required=True, type=Path)
    fit.add_argument("--holdout-features", required=True, type=Path)
    fit.add_argument("--prediction-manifest", required=True, type=Path)
    fit.add_argument("--prepare-receipt", type=Path)
    score = commands.add_parser("score", help="score revealed labels against a frozen manifest")
    score.add_argument("--prediction-manifest", required=True, type=Path)
    score.add_argument("--holdout-labels", required=True, type=Path)
    score.add_argument("--prepare-receipt", type=Path)
    score.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "fit-freeze":
            if args.prepare_receipt is None:
                args.prepare_receipt = args.calibration.parent / "prepare_receipt.json"
            result = fit_and_freeze(
                args.calibration,
                args.holdout_features,
                args.prediction_manifest,
                args.prepare_receipt,
            )
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.prepare_receipt is None:
            args.prepare_receipt = args.prediction_manifest.parent / "prepare_receipt.json"
        report = evaluate_predictions(args.prediction_manifest, args.holdout_labels, args.prepare_receipt)
        if args.output is not None:
            _write_report(args.output, report)
        print(json.dumps(report, sort_keys=True))
        return 0 if report["passed"] else 1
    except EventSimulatorError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
