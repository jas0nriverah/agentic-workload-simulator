#!/usr/bin/env python3
"""Build the sealed canonical-table event-prediction protocol.

``capture-features`` seals a feature-only holdout journal before labels exist,
binding it to the checked split, hardware, runtime, and this implementation.
``prepare`` then reads *only calibration* canonical CSV tables and that verified
capture receipt/payload.  It writes calibration labels and a normalized holdout
journal suitable for ``evaluate_predictions.py fit-freeze``.

``reveal`` first verifies the immutable prediction-manifest hash, then (and
only then) reads canonical holdout CSV tables to emit labels bound to that
manifest.  It never fits, predicts, or executes a workload.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from datetime import datetime, timezone
import shutil
import time
from typing import Any, Callable, Iterable, Mapping
from math import isfinite


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from agentic_sim.assignment.event_simulator import (  # noqa: E402
    EventSimulatorError,
    HardwareProfile,
    ModelEventInput,
    ToolEventInput,
    canonical_sha256,
    verify_frozen_prediction_manifest,
)
from agentic_sim.assignment.schema import (  # noqa: E402
    MODEL_EVENT_FIELDS,
    TOOL_EVENT_FIELDS,
    TRAJECTORY_FIELDS,
    AssignmentContractError,
    validate_model_event,
    validate_tool_event,
    validate_trajectory,
)


SPLIT_SCHEMA = "assignment.event-split-manifest.v1"
PREPARE_RECEIPT_SCHEMA = "assignment.event-protocol-prepare-receipt.v1"
CAPTURE_RECEIPT_SCHEMA = "assignment.event-feature-capture-receipt.v1"
LABELS_SCHEMA = "assignment.event-holdout-labels.v1"
STATIC_PROTOCOL_MODE = "static_predeclared"
STATIC_PROTOCOL_SCOPE = "predeclared_static_workload_only"
CAPTURE_RECEIPT_FIELDS = {
    "schema_version",
    "protocol_mode",
    "protocol_scope",
    "split_manifest_sha256",
    "hardware_profile_sha256",
    "runtime_manifest_sha256",
    "feature_journal_sha256",
    "captured_features_sha256",
    "split_manifest_path",
    "hardware_profile_path",
    "runtime_manifest_path",
    "feature_journal_path",
    "captured_features_path",
    "capture_script_path",
    "capture_script_sha256",
    "holdout_run_ids",
    "holdout_labels_accessed",
    "target_derived_fields_rejected",
    "chronology_witness",
}


def _fail(message: str) -> None:
    raise EventSimulatorError(message)


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _sidecar_path(path: Path) -> Path:
    return path.with_suffix(".sha256")


def _verify_json_hash(path: Path, *, kind: str) -> tuple[dict[str, Any], str]:
    try:
        payload = path.read_bytes()
        value = json.loads(payload)
    except (OSError, json.JSONDecodeError) as exc:
        _fail(f"cannot read {kind}: {exc}")
    if not isinstance(value, dict):
        _fail(f"{kind} must be a JSON object")
    digest = hashlib.sha256(payload).hexdigest()
    sidecar = _sidecar_path(path)
    expected = f"{digest}  {path.name}\n"
    try:
        actual = sidecar.read_text(encoding="utf-8")
    except OSError as exc:
        _fail(f"cannot read {kind} SHA-256 sidecar: {exc}")
    if actual != expected:
        _fail(f"{kind} or SHA-256 sidecar was tampered with")
    return value, digest


def _verify_file_sidecar(path: Path, *, kind: str) -> str:
    """Verify a sidecar for any immutable source file, including JSONL."""
    try:
        payload = path.read_bytes()
        actual = _sidecar_path(path).read_text(encoding="utf-8")
    except OSError as exc:
        _fail(f"cannot read {kind} or SHA-256 sidecar: {exc}")
    digest = hashlib.sha256(payload).hexdigest()
    if actual != f"{digest}  {path.name}\n":
        _fail(f"{kind} or SHA-256 sidecar was tampered with")
    return digest


def _file_sha256(path: Path, *, kind: str) -> str:
    try:
        if not path.is_file() or path.is_symlink():
            raise OSError("not a regular file")
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        _fail(f"cannot hash {kind}: {path}: {exc}")


def _load_runtime_manifest(path: Path) -> tuple[dict[str, Any], str]:
    """Accept only the exact reviewed runtime-manifest shape and its sidecar."""
    value, digest = _verify_json_hash(path, kind="runtime manifest")
    required = {
        "schema_version", "required_branch", "required_commit", "repository_root",
        "integrity", "pins", "datasets", "model", "runner", "evaluator",
        "hardware", "deadlines",
    }
    if set(value) != required or value.get("schema_version") != "assignment-runtime-manifest.v1":
        _fail("runtime manifest schema is not the reviewed assignment manifest")
    commit = value.get("required_commit")
    if (
        not isinstance(commit, str)
        or len(commit) != 40
        or set(commit) == {"0"}
        or any(char not in "0123456789abcdefABCDEF" for char in commit)
    ):
        _fail("runtime manifest must bind a non-zero full Git commit")
    branch = value.get("required_branch")
    if not isinstance(branch, str) or not branch.strip():
        _fail("runtime manifest must bind a required Git branch")
    integrity = value.get("integrity")
    if not isinstance(integrity, dict) or set(integrity) != {
        "case_runner_path", "case_runner_sha256", "evaluator_adapter_path",
        "evaluator_adapter_sha256", "request_config_path", "request_config_sha256",
        "request_proxy_path", "request_proxy_sha256", "adaptive_runner_path",
        "adaptive_runner_sha256", "adaptive_runtime_path", "adaptive_runtime_sha256",
        "adaptive_protocol_path", "adaptive_protocol_sha256", "event_simulator_path",
        "event_simulator_sha256",
    }:
        _fail("runtime manifest integrity binding is incomplete")
    for field in (
        "case_runner_sha256", "evaluator_adapter_sha256", "request_config_sha256",
        "request_proxy_sha256", "adaptive_runner_sha256", "adaptive_runtime_sha256",
        "adaptive_protocol_sha256", "event_simulator_sha256",
    ):
        digest_value = integrity.get(field)
        if not isinstance(digest_value, str) or len(digest_value) != 64 or set(digest_value) == {"0"}:
            _fail(f"runtime manifest integrity field is invalid: {field}")
    hardware = value.get("hardware")
    if not isinstance(hardware, dict) or hardware.get("one_gpu_only") is not True:
        _fail("runtime manifest must require one-GPU isolation")
    return value, digest


def _chronology_witness() -> dict[str, Any]:
    boot_id: str | None = None
    try:
        candidate = Path("/proc/sys/kernel/random/boot_id")
        if candidate.is_file():
            value = candidate.read_text(encoding="utf-8").strip()
            if value:
                boot_id = value
    except OSError:
        pass
    return {
        "captured_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "monotonic_ns": time.monotonic_ns(),
        "boot_id": boot_id,
    }


def _write_hashed_json(path: Path, value: Mapping[str, Any], *, force: bool) -> str:
    """Atomically write JSON and its exact digest sidecar; never overwrite by default."""
    payload = _canonical_bytes(value)
    digest = hashlib.sha256(payload).hexdigest()
    sidecar = _sidecar_path(path)
    sidecar_payload = f"{digest}  {path.name}\n".encode("utf-8")
    if not force and (path.exists() or sidecar.exists()):
        _fail(f"refusing to overwrite existing output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    staged: list[tuple[Path, bytes]] = [(path, payload), (sidecar, sidecar_payload)]
    temporary: list[tuple[Path, Path]] = []
    try:
        for destination, contents in staged:
            descriptor, name = tempfile.mkstemp(
                prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
            )
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


def _text_list(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        _fail(f"{name} must be a non-empty array")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        _fail(f"{name} must contain non-empty strings")
    result = tuple(value)
    if len(set(result)) != len(result):
        _fail(f"{name} contains duplicate run IDs")
    return result


def load_split_manifest(path: Path) -> tuple[tuple[str, ...], tuple[str, ...], str]:
    manifest, digest = _verify_json_hash(path, kind="split manifest")
    allowed = {"schema_version", "calibration_run_ids", "holdout_run_ids"}
    unknown = sorted(set(manifest) - allowed)
    if unknown:
        _fail("unknown split manifest field(s): " + ", ".join(unknown))
    if manifest.get("schema_version") != SPLIT_SCHEMA:
        _fail("unsupported split manifest schema_version")
    calibration = _text_list(manifest.get("calibration_run_ids"), "calibration_run_ids")
    holdout = _text_list(manifest.get("holdout_run_ids"), "holdout_run_ids")
    overlap = sorted(set(calibration) & set(holdout))
    if overlap:
        _fail("calibration/holdout split overlaps: " + ", ".join(overlap))
    return tuple(sorted(calibration)), tuple(sorted(holdout)), digest


def _read_json_object(path: Path, *, kind: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _fail(f"cannot read {kind}: {exc}")
    if not isinstance(value, dict):
        _fail(f"{kind} must be a JSON object")
    return value


def _parse_int(value: str, field: str, *, optional: bool = False) -> int | None:
    if value == "" and optional:
        return None
    try:
        if str(int(value)) != value and not (value.startswith("+") and str(int(value)) == value[1:]):
            _fail(f"{field} must be an integer in canonical CSV")
        return int(value)
    except ValueError:
        _fail(f"{field} must be an integer in canonical CSV")


def _parse_float(value: str, field: str, *, optional: bool = False) -> float | None:
    if value == "" and optional:
        return None
    try:
        return float(value)
    except ValueError:
        _fail(f"{field} must be numeric in canonical CSV")


def _parse_bool(value: str, field: str) -> bool | None:
    if value == "":
        return None
    if value == "true":
        return True
    if value == "false":
        return False
    _fail(f"{field} must be true, false, or empty in canonical CSV")


def _read_canonical_csv(
    path: Path,
    fields: tuple[str, ...],
    validate: Callable[[Mapping[str, Any]], dict[str, Any]],
    *,
    kind: str,
) -> list[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames is None or tuple(reader.fieldnames) != fields:
                _fail(f"{kind} CSV must use the exact canonical header")
            raw_rows = list(reader)
    except OSError as exc:
        _fail(f"cannot read canonical {kind} CSV: {exc}")
    if not raw_rows:
        _fail(f"canonical {kind} CSV is empty")

    integer_fields = {"ordinal", "command_bytes", "input_tokens", "max_output_tokens", "output_tokens", "context_tokens", "request_bytes", "response_bytes", "start_mono_ns", "end_mono_ns", "tool_event_count", "model_event_count"}
    optional_integer_fields = {"start_mono_ns", "end_mono_ns", "input_tokens", "max_output_tokens", "output_tokens", "context_tokens", "request_bytes", "response_bytes"}
    number_fields = {"wall_ms", "cpu_ms", "bytes_read", "bytes_written", "cpu_activity_union_ms", "cuda_activity_union_ms", "kernel_duration_sum_ms", "e2e_wall_ms", "tool_wall_ms", "model_wall_ms", "tool_model_ratio", "sweep_value"}
    optional_number_fields = {"wall_ms", "cpu_ms", "bytes_read", "bytes_written", "cpu_activity_union_ms", "cuda_activity_union_ms", "kernel_duration_sum_ms", "e2e_wall_ms", "tool_wall_ms", "model_wall_ms", "tool_model_ratio", "sweep_value"}
    bool_fields = {"submitted", "official_resolved"}
    rows: list[dict[str, Any]] = []
    for number, raw in enumerate(raw_rows, 2):
        row: dict[str, Any] = {}
        for field in fields:
            value = raw[field]
            if field in integer_fields:
                row[field] = _parse_int(value, field, optional=field in optional_integer_fields)
            elif field in number_fields:
                row[field] = _parse_float(value, field, optional=field in optional_number_fields)
            elif field in bool_fields:
                row[field] = _parse_bool(value, field)
            elif field in {"sweep_parameter", "unavailable_reason"}:
                row[field] = None if value == "" else value
            else:
                row[field] = value
        try:
            rows.append(validate(row))
        except AssignmentContractError as exc:
            _fail(f"invalid canonical {kind} row {number}: {exc}")
    return rows


def _require_exact_runs(rows: Iterable[Mapping[str, Any]], expected: tuple[str, ...], *, kind: str) -> None:
    identifiers = [row["run_id"] for row in rows]
    duplicates = sorted({item for item in identifiers if identifiers.count(item) > 1})
    if duplicates:
        _fail(f"duplicate {kind} run IDs: " + ", ".join(duplicates))
    actual = set(identifiers)
    missing = sorted(set(expected) - actual)
    extra = sorted(actual - set(expected))
    if missing or extra:
        _fail(f"{kind} run coverage mismatch; missing={missing}, extra={extra}")


def _require_event_coverage(
    rows: Iterable[Mapping[str, Any]], expected_runs: tuple[str, ...], *, id_field: str, kind: str
) -> None:
    rows = list(rows)
    unexpected = sorted({row["run_id"] for row in rows} - set(expected_runs))
    if unexpected:
        _fail(f"{kind} contains non-split run IDs: " + ", ".join(unexpected))
    identifiers = [str(row[id_field]) for row in rows]
    duplicates = sorted({item for item in identifiers if identifiers.count(item) > 1})
    if duplicates:
        _fail(f"duplicate {kind} IDs: " + ", ".join(duplicates))
    present = {row["run_id"] for row in rows}
    missing = sorted(set(expected_runs) - present)
    if missing:
        _fail(f"{kind} lacks events for split run IDs: " + ", ".join(missing))


def _require_declared_event_counts(
    trajectories: Iterable[Mapping[str, Any]],
    tools: Iterable[Mapping[str, Any]],
    models: Iterable[Mapping[str, Any]],
    *,
    kind: str,
) -> None:
    """Reject a canonical table whose event rows do not cover its trajectory rows."""
    tool_counts: dict[str, int] = {}
    model_counts: dict[str, int] = {}
    for row in tools:
        tool_counts[row["run_id"]] = tool_counts.get(row["run_id"], 0) + 1
    for row in models:
        model_counts[row["run_id"]] = model_counts.get(row["run_id"], 0) + 1
    for row in trajectories:
        run_id = row["run_id"]
        if tool_counts.get(run_id) != row["tool_event_count"]:
            _fail(f"{kind} tool-event count mismatch for {run_id}")
        if model_counts.get(run_id) != row["model_event_count"]:
            _fail(f"{kind} model-event count mismatch for {run_id}")


def _load_hardware(path: Path) -> dict[str, Any]:
    raw = _read_json_object(path, kind="hardware profile")
    return HardwareProfile.from_mapping(raw).to_mapping()


def _hardware_matches(row: Mapping[str, Any], expected: Mapping[str, Any], *, kind: str) -> None:
    if canonical_sha256(row.get("hardware")) != canonical_sha256(expected):
        _fail(f"{kind} hardware profile does not exactly match --hardware-profile")


def _require_trajectory_hardware(
    trajectories: Iterable[Mapping[str, Any]], hardware: Mapping[str, Any], *, kind: str
) -> None:
    """Bind measured trajectory provenance to the supplied hardware identity.

    Canonical event tables carry per-event timings but no hardware column, so
    trajectories are the authoritative measured-run provenance.  Never repair
    a mismatch by replacing this identity with the requested profile.
    """
    expected_id = hardware["hardware_id"]
    for row in trajectories:
        if row.get("hardware_id") != expected_id:
            _fail(
                f"{kind} trajectory hardware_id does not match --hardware-profile: "
                f"{row.get('run_id')}={row.get('hardware_id')!r}, expected={expected_id!r}"
            )


def _load_holdout_features(path: Path, hardware: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read feature-only JSON/JSONL; deliberately never opens a canonical CSV."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        _fail(f"cannot read feature-only holdout input: {exc}")
    tools: list[Any] = []
    models: list[Any] = []
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = None
    if isinstance(value, dict):
        allowed = {"schema_version", "protocol_mode", "tool_events", "model_events"}
        unknown = sorted(set(value) - allowed)
        if unknown:
            _fail("unknown holdout feature root field(s): " + ", ".join(unknown))
        if value.get("schema_version") != "assignment.event-holdout-features.v1":
            _fail("unsupported holdout feature root schema_version")
        if value.get("protocol_mode") != STATIC_PROTOCOL_MODE:
            _fail("static prepare mode rejects adaptive or non-predeclared holdout features")
        tools, models = value.get("tool_events"), value.get("model_events")
        if not isinstance(tools, list) or not isinstance(models, list):
            _fail("holdout feature root requires tool_events and model_events arrays")
    else:
        tools, models = [], []
        for line_number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                _fail(f"invalid holdout feature JSONL at line {line_number}: {exc}")
            if not isinstance(item, dict):
                _fail(f"holdout feature JSONL line {line_number} must be an object")
            if item.get("schema_version") == "assignment.tool-event-input.v1":
                tools.append(item)
            elif item.get("schema_version") == "assignment.model-event-input.v1":
                models.append(item)
            else:
                _fail(f"unsupported holdout feature schema at line {line_number}")
    for row in models:
        if isinstance(row, dict) and "output_tokens" in row:
            _fail("static capture rejects output_tokens as a pre-event feature")
    parsed_tools = [ToolEventInput.from_mapping(row) for row in tools]
    parsed_models = [ModelEventInput.from_mapping(row) for row in models]
    if not parsed_tools or not parsed_models:
        _fail("holdout feature input requires both tool and model events")
    for row in parsed_tools + parsed_models:
        if row.split != "holdout":
            _fail("holdout feature input requires split=holdout")
        _hardware_matches(row.to_mapping(), hardware, kind="holdout feature")
    return (
        [row.to_mapping() for row in sorted(parsed_tools, key=lambda item: (item.run_id, item.event_id))],
        [row.to_mapping() for row in sorted(parsed_models, key=lambda item: (item.run_id, item.request_id))],
    )


def _holdout_payload(tools: list[dict[str, Any]], models: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": "assignment.event-holdout-features.v1",
        "protocol_mode": STATIC_PROTOCOL_MODE,
        "tool_events": tools,
        "model_events": models,
    }


def capture_features(args: argparse.Namespace) -> dict[str, Any]:
    """Seal pre-event holdout features before any holdout labels exist.

    The chronology witness is intentionally bounded: it records local UTC,
    monotonic time, and boot ID when available.  It is execution-bound local
    evidence, not a cryptographic proof of event ordering.
    """
    calibration_ids, holdout_ids, split_digest = load_split_manifest(args.split_manifest)
    del calibration_ids
    hardware = _load_hardware(args.hardware_profile)
    runtime, runtime_digest = _load_runtime_manifest(args.runtime_manifest)
    del runtime
    journal_digest = _verify_file_sidecar(args.feature_journal, kind="feature journal")
    tools, models = _load_holdout_features(args.feature_journal, hardware)
    _require_event_coverage(tools, holdout_ids, id_field="event_id", kind="holdout tool-event")
    _require_event_coverage(models, holdout_ids, id_field="request_id", kind="holdout model-event")
    if {row["run_id"] for row in tools + models} != set(holdout_ids):
        _fail("holdout feature run coverage does not match split manifest")

    output_dir = args.output_dir.resolve()
    captured_features_path = output_dir / "holdout_features.json"
    capture_receipt_path = output_dir / "capture_receipt.json"
    if any(path.exists() or _sidecar_path(path).exists() for path in (captured_features_path, capture_receipt_path)):
        _fail(f"refusing to overwrite existing capture output: {output_dir}")
    captured_features_digest = _write_hashed_json(
        captured_features_path, _holdout_payload(tools, models), force=False
    )
    script_path = Path(__file__).resolve()
    receipt = {
        "schema_version": CAPTURE_RECEIPT_SCHEMA,
        "protocol_mode": STATIC_PROTOCOL_MODE,
        "protocol_scope": STATIC_PROTOCOL_SCOPE,
        "split_manifest_sha256": split_digest,
        "hardware_profile_sha256": canonical_sha256(hardware),
        "runtime_manifest_sha256": runtime_digest,
        "feature_journal_sha256": journal_digest,
        "captured_features_sha256": captured_features_digest,
        "split_manifest_path": str(args.split_manifest.resolve()),
        "hardware_profile_path": str(args.hardware_profile.resolve()),
        "runtime_manifest_path": str(args.runtime_manifest.resolve()),
        "feature_journal_path": str(args.feature_journal.resolve()),
        "captured_features_path": str(captured_features_path.resolve()),
        "capture_script_path": str(script_path),
        "capture_script_sha256": _file_sha256(script_path, kind="capture script"),
        "holdout_run_ids": list(holdout_ids),
        "holdout_labels_accessed": False,
        "target_derived_fields_rejected": True,
        "chronology_witness": _chronology_witness(),
    }
    receipt_digest = _write_hashed_json(capture_receipt_path, receipt, force=False)
    return {
        "status": "features_captured",
        "captured_features": str(captured_features_path),
        "captured_features_sha256": captured_features_digest,
        "capture_receipt": str(capture_receipt_path),
        "capture_receipt_sha256": receipt_digest,
        "feature_journal_sha256": journal_digest,
        "runtime_manifest_sha256": runtime_digest,
        "holdout_labels_accessed": False,
    }


def _load_capture_binding(
    capture_receipt_path: Path,
    split_digest: str,
    hardware: Mapping[str, Any] | None,
    holdout_features_path: Path | None,
) -> tuple[dict[str, Any], str, dict[str, Any], str]:
    capture, capture_digest = _verify_json_hash(capture_receipt_path, kind="capture receipt")
    if set(capture) != CAPTURE_RECEIPT_FIELDS or capture.get("schema_version") != CAPTURE_RECEIPT_SCHEMA:
        _fail("capture receipt schema is invalid")
    if capture.get("protocol_mode") != STATIC_PROTOCOL_MODE or capture.get("protocol_scope") != STATIC_PROTOCOL_SCOPE:
        _fail("only the sealed static-predeclared protocol mode is supported")
    if capture.get("split_manifest_sha256") != split_digest:
        _fail("capture receipt does not bind the supplied split manifest")
    if hardware is not None and capture.get("hardware_profile_sha256") != canonical_sha256(hardware):
        _fail("capture receipt does not bind the supplied hardware profile")
    if capture.get("holdout_labels_accessed") is not False or capture.get("target_derived_fields_rejected") is not True:
        _fail("capture receipt is not a label-free feature capture")
    if not isinstance(capture.get("holdout_run_ids"), list) or not all(
        isinstance(item, str) and item for item in capture["holdout_run_ids"]
    ) or len(set(capture["holdout_run_ids"])) != len(capture["holdout_run_ids"]):
        _fail("capture receipt holdout IDs are invalid")
    if tuple(capture["holdout_run_ids"]) != tuple(sorted(capture["holdout_run_ids"])):
        _fail("capture receipt holdout IDs are not deterministically ordered")
    script_path = Path(str(capture.get("capture_script_path", ""))).resolve()
    if script_path != Path(__file__).resolve() or capture.get("capture_script_sha256") != _file_sha256(script_path, kind="capture script"):
        _fail("capture receipt is not bound to the current capture implementation")
    captured_path = Path(str(capture.get("captured_features_path", ""))).resolve()
    if not captured_path.is_absolute():
        _fail("capture receipt does not point to an absolute copied feature payload")
    if holdout_features_path is not None and holdout_features_path.resolve() != captured_path:
        _fail("prepare accepts only the feature payload copied by capture-features")
    features, features_digest = _verify_json_hash(captured_path, kind="captured holdout feature set")
    if capture.get("captured_features_sha256") != features_digest:
        _fail("capture receipt does not bind the copied feature payload")
    if features.get("schema_version") != "assignment.event-holdout-features.v1" or features.get("protocol_mode") != STATIC_PROTOCOL_MODE:
        _fail("captured holdout feature set schema is invalid")
    chronology = capture.get("chronology_witness")
    if not isinstance(chronology, dict) or set(chronology) != {"captured_at_utc", "monotonic_ns", "boot_id"}:
        _fail("capture receipt chronology witness is invalid")
    if not isinstance(chronology["captured_at_utc"], str) or not chronology["captured_at_utc"].endswith("Z"):
        _fail("capture receipt UTC chronology witness is invalid")
    try:
        parsed_utc = datetime.fromisoformat(chronology["captured_at_utc"].removesuffix("Z") + "+00:00")
    except ValueError:
        _fail("capture receipt UTC chronology witness is invalid")
    if parsed_utc.tzinfo is None or parsed_utc.utcoffset() != timezone.utc.utcoffset(parsed_utc):
        _fail("capture receipt UTC chronology witness is not UTC")
    if isinstance(chronology["monotonic_ns"], bool) or not isinstance(chronology["monotonic_ns"], int) or chronology["monotonic_ns"] <= 0:
        _fail("capture receipt monotonic chronology witness is invalid")
    if chronology["boot_id"] is not None and not isinstance(chronology["boot_id"], str):
        _fail("capture receipt boot chronology witness is invalid")
    return capture, capture_digest, features, features_digest


def _calibration_payload(
    trajectories: list[dict[str, Any]], tools: list[dict[str, Any]], models: list[dict[str, Any]], hardware: Mapping[str, Any]
) -> dict[str, Any]:
    tool_records = []
    for row in tools:
        if row["status"] != "completed" or row["wall_ms"] is None:
            _fail(f"calibration tool event is not completed: {row['event_id']}")
        features = {
            "schema_version": "assignment.tool-event-input.v1",
            "event_id": row["event_id"],
            "run_id": row["run_id"],
            "split": "calibration",
            "operation_class": row["operation_class"],
            "declared_command_bytes": row["command_bytes"],
            "declared_read_bytes": 0,
            "declared_write_bytes": 0,
            "declared_path_count": 0,
            "hardware": hardware,
        }
        tool_records.append({
            "schema_version": "assignment.tool-calibration.v1",
            "split": "calibration",
            "features": ToolEventInput.from_mapping(features).to_mapping(),
            "observed_ms": row["wall_ms"],
        })
    model_records = []
    for row in models:
        if row["status"] != "completed" or row["wall_ms"] is None:
            _fail(f"calibration model event is not completed: {row['request_id']}")
        for name in ("input_tokens", "context_tokens", "max_output_tokens"):
            if row[name] is None:
                _fail(f"calibration model event lacks pre-event {name}: {row['request_id']}")
        features = {
            "schema_version": "assignment.model-event-input.v1",
            "request_id": row["request_id"],
            "run_id": row["run_id"],
            "split": "calibration",
            "input_tokens": row["input_tokens"],
            "context_tokens": row["context_tokens"],
            "max_output_tokens": row["max_output_tokens"],
            "hardware": hardware,
        }
        model_records.append({
            "schema_version": "assignment.model-calibration.v1",
            "split": "calibration",
            "features": ModelEventInput.from_mapping(features).to_mapping(),
            "observed_ms": row["wall_ms"],
        })
    trajectory_records = []
    for row in trajectories:
        if row["status"] != "completed" or row["e2e_wall_ms"] is None:
            _fail(f"calibration trajectory is not completed: {row['run_id']}")
        trajectory_records.append({
            "schema_version": "assignment.trajectory-calibration.v1",
            "run_id": row["run_id"],
            "split": "calibration",
            "observed_ms": row["e2e_wall_ms"],
        })
    return {
        "schema_version": "assignment.event-calibration-set.v1",
        "tool_events": sorted(tool_records, key=lambda item: (item["features"]["run_id"], item["features"]["event_id"])),
        "model_events": sorted(model_records, key=lambda item: (item["features"]["run_id"], item["features"]["request_id"])),
        "trajectories": sorted(trajectory_records, key=lambda item: item["run_id"]),
    }


def prepare(args: argparse.Namespace) -> dict[str, Any]:
    calibration_ids, holdout_ids, split_digest = load_split_manifest(args.split_manifest)
    hardware = _load_hardware(args.hardware_profile)
    capture, capture_digest, captured_features, captured_features_digest = _load_capture_binding(
        args.capture_receipt, split_digest, hardware, args.holdout_features
    )
    if tuple(capture["holdout_run_ids"]) != holdout_ids:
        _fail("capture receipt holdout IDs do not match the split manifest")
    del captured_features
    trajectories = _read_canonical_csv(args.calibration_trajectories, TRAJECTORY_FIELDS, validate_trajectory, kind="trajectory")
    tools = _read_canonical_csv(args.calibration_tool_events, TOOL_EVENT_FIELDS, validate_tool_event, kind="tool-event")
    models = _read_canonical_csv(args.calibration_model_events, MODEL_EVENT_FIELDS, validate_model_event, kind="model-event")
    _require_exact_runs(trajectories, calibration_ids, kind="calibration trajectory")
    _require_trajectory_hardware(trajectories, hardware, kind="calibration")
    _require_event_coverage(tools, calibration_ids, id_field="event_id", kind="calibration tool-event")
    _require_event_coverage(models, calibration_ids, id_field="request_id", kind="calibration model-event")
    _require_declared_event_counts(trajectories, tools, models, kind="calibration")
    calibration = _calibration_payload(trajectories, tools, models, hardware)
    holdout_tools, holdout_models = _load_holdout_features(
        args.capture_receipt.parent / "holdout_features.json", hardware
    )
    _require_event_coverage(holdout_tools, holdout_ids, id_field="event_id", kind="holdout tool-event")
    _require_event_coverage(holdout_models, holdout_ids, id_field="request_id", kind="holdout model-event")
    holdout_runs = {row["run_id"] for row in holdout_tools + holdout_models}
    if holdout_runs != set(holdout_ids):
        _fail("holdout feature run coverage does not match split manifest")
    holdout = _holdout_payload(holdout_tools, holdout_models)
    output_dir = args.output_dir
    calibration_path = output_dir / "calibration.json"
    holdout_path = output_dir / "holdout_features.json"
    receipt_path = output_dir / "prepare_receipt.json"
    capture_copy_path = output_dir / "capture_receipt.json"
    if not args.force and any(path.exists() or _sidecar_path(path).exists() for path in (calibration_path, holdout_path, receipt_path, capture_copy_path)):
        _fail(f"refusing to overwrite existing output directory: {output_dir}")
    calibration_digest = _write_hashed_json(calibration_path, calibration, force=args.force)
    holdout_digest = _write_hashed_json(holdout_path, holdout, force=args.force)
    if holdout_digest != captured_features_digest:
        _fail("prepared holdout feature payload differs from the captured payload")
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copyfile(args.capture_receipt, capture_copy_path)
        shutil.copyfile(_sidecar_path(args.capture_receipt), _sidecar_path(capture_copy_path))
    except OSError as exc:
        _fail(f"cannot copy verified capture receipt into prepare output: {exc}")
    receipt = {
        "schema_version": PREPARE_RECEIPT_SCHEMA,
        "protocol_mode": STATIC_PROTOCOL_MODE,
        "protocol_scope": STATIC_PROTOCOL_SCOPE,
        "split_manifest_sha256": split_digest,
        "hardware_profile_sha256": canonical_sha256(hardware),
        "calibration_sha256": calibration_digest,
        "holdout_features_sha256": holdout_digest,
        "capture_receipt_sha256": capture_digest,
        "captured_features_sha256": captured_features_digest,
        "runtime_manifest_sha256": capture["runtime_manifest_sha256"],
        "capture_script_sha256": capture["capture_script_sha256"],
        "capture_chronology_witness": capture["chronology_witness"],
        "calibration_run_ids": list(calibration_ids),
        "holdout_run_ids": list(holdout_ids),
        "holdout_labels_accessed": False,
        "calibration_tool_declared_read_bytes": 0,
        "calibration_tool_declared_write_bytes": 0,
        "forbidden_holdout_feature_classes": ["wall", "cpu", "cuda", "kineto", "timestamps", "output_tokens", "response_bytes"],
    }
    receipt_digest = _write_hashed_json(receipt_path, receipt, force=args.force)
    return {
        "status": "prepared",
        "calibration": str(calibration_path),
        "calibration_sha256": calibration_digest,
        "holdout_features": str(holdout_path),
        "holdout_features_sha256": holdout_digest,
        "prepare_receipt": str(receipt_path),
        "prepare_receipt_sha256": receipt_digest,
        "capture_receipt_sha256": capture_digest,
        "holdout_labels_accessed": False,
    }


def _prediction_index(rows: Any, *, id_field: str, kind: str, holdout_ids: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    if not isinstance(rows, list) or not rows:
        _fail(f"frozen prediction manifest {kind} predictions must be a non-empty array")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            _fail(f"frozen {kind} prediction must be an object")
        identifier, run_id = row.get(id_field), row.get("run_id")
        if not isinstance(identifier, str) or not identifier or not isinstance(run_id, str) or not run_id:
            _fail(f"frozen {kind} prediction requires {id_field} and run_id")
        predicted_ms = row.get("predicted_ms")
        if isinstance(predicted_ms, bool) or not isinstance(predicted_ms, (int, float)) or not isfinite(float(predicted_ms)) or float(predicted_ms) <= 0:
            _fail(f"frozen {kind} prediction requires positive finite predicted_ms")
        if run_id not in holdout_ids:
            _fail(f"frozen {kind} prediction is outside holdout split: {run_id}")
        if identifier in result:
            _fail(f"duplicate frozen {kind} prediction ID: {identifier}")
        result[identifier] = row
    if {row["run_id"] for row in result.values()} != set(holdout_ids):
        _fail(f"frozen {kind} prediction run coverage does not match holdout split")
    return result


def _labels_for_events(
    canonical: list[dict[str, Any]], predictions: Mapping[str, Mapping[str, Any]], *, id_field: str, label_schema: str, kind: str
) -> list[dict[str, Any]]:
    canonical_index: dict[str, dict[str, Any]] = {}
    for row in canonical:
        identifier = row[id_field]
        if identifier in canonical_index:
            _fail(f"duplicate canonical holdout {kind} ID: {identifier}")
        canonical_index[identifier] = row
    if set(canonical_index) != set(predictions):
        missing = sorted(set(predictions) - set(canonical_index))
        extra = sorted(set(canonical_index) - set(predictions))
        _fail(f"canonical holdout {kind} coverage mismatch; missing={missing}, extra={extra}")
    labels = []
    for identifier in sorted(predictions):
        source, prediction = canonical_index[identifier], predictions[identifier]
        if source["run_id"] != prediction["run_id"]:
            _fail(f"canonical holdout {kind} run_id mismatch for {identifier}")
        if source["status"] == "completed":
            if source["wall_ms"] is None:
                _fail(f"completed canonical holdout {kind} lacks wall_ms: {identifier}")
            labels.append({"schema_version": label_schema, id_field: identifier, "run_id": source["run_id"], "status": "completed", "observed_ms": source["wall_ms"], "unavailable_reason": None})
        else:
            labels.append({"schema_version": label_schema, id_field: identifier, "run_id": source["run_id"], "status": "unavailable", "observed_ms": None, "unavailable_reason": source.get("unavailable_reason") or f"canonical_status:{source['status']}"})
    return labels


def reveal(args: argparse.Namespace) -> dict[str, Any]:
    calibration_ids, holdout_ids, split_digest = load_split_manifest(args.split_manifest)
    # This verification intentionally precedes *all* canonical holdout reads.
    manifest, prediction_digest = verify_frozen_prediction_manifest(args.prediction_manifest)
    binding = manifest.get("event_protocol_binding")
    if not isinstance(binding, dict):
        _fail("frozen prediction manifest lacks event-protocol integrity binding")
    receipt, receipt_digest = _verify_json_hash(args.prepare_receipt, kind="prepare receipt")
    required_receipt = {
        "schema_version",
        "protocol_mode",
        "protocol_scope",
        "split_manifest_sha256",
        "hardware_profile_sha256",
        "calibration_sha256",
        "holdout_features_sha256",
        "capture_receipt_sha256",
        "captured_features_sha256",
        "runtime_manifest_sha256",
        "capture_script_sha256",
        "capture_chronology_witness",
        "calibration_run_ids",
        "holdout_run_ids",
        "holdout_labels_accessed",
        "calibration_tool_declared_read_bytes",
        "calibration_tool_declared_write_bytes",
        "forbidden_holdout_feature_classes",
    }
    if set(receipt) != required_receipt or receipt.get("schema_version") != PREPARE_RECEIPT_SCHEMA:
        _fail("prepare receipt schema is invalid")
    if receipt.get("protocol_mode") != STATIC_PROTOCOL_MODE or receipt.get("protocol_scope") != STATIC_PROTOCOL_SCOPE:
        _fail("reveal only supports the sealed static-predeclared protocol mode")
    capture, capture_digest, _captured_features, captured_features_digest = _load_capture_binding(
        args.prepare_receipt.parent / "capture_receipt.json", split_digest, None, None
    )
    if capture_digest != receipt["capture_receipt_sha256"] or captured_features_digest != receipt["captured_features_sha256"]:
        _fail("prepare receipt does not bind the verified capture receipt and copied features")
    if tuple(capture["holdout_run_ids"]) != holdout_ids:
        _fail("capture receipt holdout IDs do not match the split manifest")
    calibration, calibration_digest = _verify_json_hash(
        args.prepare_receipt.parent / "calibration.json", kind="calibration set"
    )
    holdout_features, holdout_features_digest = _verify_json_hash(
        args.prepare_receipt.parent / "holdout_features.json", kind="holdout feature set"
    )
    if calibration.get("schema_version") != "assignment.event-calibration-set.v1" or holdout_features.get("schema_version") != "assignment.event-holdout-features.v1":
        _fail("prepare inputs have an invalid schema")
    expected_binding = {
        "protocol_mode": STATIC_PROTOCOL_MODE,
        "prepare_receipt_sha256": receipt_digest,
        "calibration_sha256": calibration_digest,
        "holdout_features_sha256": holdout_features_digest,
        "capture_receipt_sha256": receipt["capture_receipt_sha256"],
        "captured_features_sha256": receipt["captured_features_sha256"],
        "runtime_manifest_sha256": receipt["runtime_manifest_sha256"],
        "hardware_profile_sha256": receipt["hardware_profile_sha256"],
    }
    if binding != expected_binding:
        _fail("frozen prediction manifest is not bound to verified prepare inputs")
    if receipt["split_manifest_sha256"] != split_digest or receipt["calibration_sha256"] != calibration_digest or receipt["holdout_features_sha256"] != holdout_features_digest:
        _fail("prepare receipt does not bind the verified split and input hashes")
    if receipt["captured_features_sha256"] != holdout_features_digest or receipt["runtime_manifest_sha256"] != capture["runtime_manifest_sha256"]:
        _fail("prepare receipt does not bind the copied feature payload and runtime")
    if tuple(sorted(manifest.get("calibration_run_ids", []))) != calibration_ids:
        _fail("frozen prediction manifest calibration run IDs do not match split manifest")
    tool_predictions = _prediction_index(manifest.get("tool_predictions"), id_field="event_id", kind="tool-event", holdout_ids=holdout_ids)
    model_predictions = _prediction_index(manifest.get("model_predictions"), id_field="request_id", kind="model-event", holdout_ids=holdout_ids)
    trajectory_predictions = _prediction_index(manifest.get("trajectory_predictions"), id_field="run_id", kind="trajectory", holdout_ids=holdout_ids)
    if set(trajectory_predictions) != set(holdout_ids):
        _fail("frozen trajectory predictions do not exactly match holdout split")

    trajectories = _read_canonical_csv(args.holdout_trajectories, TRAJECTORY_FIELDS, validate_trajectory, kind="holdout trajectory")
    tools = _read_canonical_csv(args.holdout_tool_events, TOOL_EVENT_FIELDS, validate_tool_event, kind="holdout tool-event")
    models = _read_canonical_csv(args.holdout_model_events, MODEL_EVENT_FIELDS, validate_model_event, kind="holdout model-event")
    _require_exact_runs(trajectories, holdout_ids, kind="holdout trajectory")
    _require_event_coverage(tools, holdout_ids, id_field="event_id", kind="holdout tool-event")
    _require_event_coverage(models, holdout_ids, id_field="request_id", kind="holdout model-event")
    _require_declared_event_counts(trajectories, tools, models, kind="holdout")
    labels = {
        "schema_version": LABELS_SCHEMA,
        "prediction_manifest_sha256": prediction_digest,
        "prepare_receipt_sha256": receipt_digest,
        "calibration_sha256": calibration_digest,
        "holdout_features_sha256": holdout_features_digest,
        "capture_receipt_sha256": receipt["capture_receipt_sha256"],
        "captured_features_sha256": receipt["captured_features_sha256"],
        "runtime_manifest_sha256": receipt["runtime_manifest_sha256"],
        "hardware_profile_sha256": receipt["hardware_profile_sha256"],
        "tool_events": _labels_for_events(tools, tool_predictions, id_field="event_id", label_schema="assignment.tool-holdout-label.v1", kind="tool-event"),
        "model_events": _labels_for_events(models, model_predictions, id_field="request_id", label_schema="assignment.model-holdout-label.v1", kind="model-event"),
        "trajectories": [],
    }
    trajectory_by_run = {row["run_id"]: row for row in trajectories}
    for run_id in sorted(holdout_ids):
        source = trajectory_by_run[run_id]
        if source["status"] != "completed" or source["e2e_wall_ms"] is None:
            labels["trajectories"].append({"schema_version": "assignment.trajectory-holdout-label.v1", "run_id": run_id, "status": "unavailable", "observed_ms": None, "unavailable_reason": source.get("unavailable_reason") or f"canonical_status:{source['status']}"})
        else:
            labels["trajectories"].append({"schema_version": "assignment.trajectory-holdout-label.v1", "run_id": run_id, "status": "completed", "observed_ms": source["e2e_wall_ms"], "unavailable_reason": None})
    labels_path = args.output_labels
    labels_digest = _write_hashed_json(labels_path, labels, force=args.force)
    return {"status": "labels_revealed", "prediction_manifest_sha256": prediction_digest, "split_manifest_sha256": split_digest, "labels": str(labels_path), "labels_sha256": labels_digest, "label_count": sum(len(labels[name]) for name in ("tool_events", "model_events", "trajectories"))}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture_parser = commands.add_parser(
        "capture-features",
        help="seal a pre-event holdout feature journal before any holdout labels exist",
    )
    capture_parser.add_argument("--split-manifest", required=True, type=Path)
    capture_parser.add_argument("--hardware-profile", required=True, type=Path)
    capture_parser.add_argument("--runtime-manifest", required=True, type=Path)
    capture_parser.add_argument("--feature-journal", required=True, type=Path)
    capture_parser.add_argument("--output-dir", required=True, type=Path)
    prepare_parser = commands.add_parser("prepare", help="build calibration labels and feature-only holdout journal")
    prepare_parser.add_argument("--split-manifest", required=True, type=Path)
    prepare_parser.add_argument("--hardware-profile", required=True, type=Path)
    prepare_parser.add_argument("--calibration-trajectories", required=True, type=Path)
    prepare_parser.add_argument("--calibration-tool-events", required=True, type=Path)
    prepare_parser.add_argument("--calibration-model-events", required=True, type=Path)
    prepare_parser.add_argument("--capture-receipt", required=True, type=Path)
    prepare_parser.add_argument("--holdout-features", type=Path, help="optional explicit path; must be the capture-features copied payload")
    prepare_parser.add_argument("--output-dir", required=True, type=Path)
    prepare_parser.add_argument("--force", action="store_true")
    reveal_parser = commands.add_parser("reveal", help="bind canonical holdout labels after prediction freeze verification")
    reveal_parser.add_argument("--split-manifest", required=True, type=Path)
    reveal_parser.add_argument("--prediction-manifest", required=True, type=Path)
    reveal_parser.add_argument("--prepare-receipt", type=Path)
    reveal_parser.add_argument("--holdout-trajectories", required=True, type=Path)
    reveal_parser.add_argument("--holdout-tool-events", required=True, type=Path)
    reveal_parser.add_argument("--holdout-model-events", required=True, type=Path)
    reveal_parser.add_argument("--output-labels", required=True, type=Path)
    reveal_parser.add_argument("--force", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "reveal" and args.prepare_receipt is None:
            args.prepare_receipt = args.prediction_manifest.parent / "prepare_receipt.json"
        if args.command == "capture-features":
            result = capture_features(args)
        elif args.command == "prepare":
            result = prepare(args)
        else:
            result = reveal(args)
        print(json.dumps(result, sort_keys=True))
        return 0
    except EventSimulatorError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
