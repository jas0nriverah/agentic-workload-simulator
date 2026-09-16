#!/usr/bin/env python3
"""Generate assignment Steps 1-3 figures from canonical CSV tables.

The primary CPU:GPU phase ratio used here is the assignment-facing ratio:

    sum(tool event wall time) / sum(model request wall time)

CUDA activity and CPU utilization are intentionally not substitutes for those
phase wall times.  This module is dependency-free and writes deterministic SVG,
JSON, and Markdown outputs.  Every input table is validated completely before
the output directory is modified.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import sys
from collections import defaultdict
from html import escape
from pathlib import Path
from typing import Any, Iterable


TRAJECTORY_COLUMNS = {
    "run_id",
    "suite",
    "repository",
    "category",
    "status",
    "official_resolved",
    "e2e_wall_ms",
}
TOOL_EVENT_COLUMNS = {"event_id", "run_id", "status", "operation_class", "wall_ms"}
MODEL_EVENT_COLUMNS = {
    "request_id",
    "run_id",
    "status",
    "input_tokens",
    "output_tokens",
    "context_tokens",
    "wall_ms",
}
SWEEP_COLUMNS = {
    "run_id",
    "status",
    "sweep_parameter",
    "sweep_value",
    "official_resolved",
    "e2e_wall_ms",
    "tool_wall_ms",
    "model_wall_ms",
}

PALETTE = (
    "#1769aa",
    "#7a5af8",
    "#18794e",
    "#b54708",
    "#c11574",
    "#026aa2",
    "#b42318",
    "#667085",
)
GRID = "#d0d5dd"
TEXT = "#344054"
TOOL_COLOR = "#f79009"
MODEL_COLOR = "#1769aa"
OVERHEAD_COLOR = "#98a2b3"
REQUIRED_SWEEP_PARAMETERS = {
    "call_limit", "max_output_tokens", "observation_length", "temperature"
}
SWEEP_METADATA_COLUMNS = {
    "suite",
    "repository",
    "category",
    "instance_id",
    "repeat_id",
    "config_id",
    "provenance",
}
VALID_LATENCY_KINDS = {"observed", "predicted"}
SUITE_HEADLINE_KEYS = {
    "selected",
    "submitted",
    "completed",
    "resolved",
    "resolved_rate",
    "average_completed_e2e_wall_ms",
}


class DataContractError(ValueError):
    """Raised when a canonical table cannot support truthful figures."""


def _read_csv(path: Path, required: set[str], table: str) -> list[dict[str, str]]:
    if not path.is_file():
        raise DataContractError(f"{table}: file does not exist: {path}")
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None:
            raise DataContractError(f"{table}: missing CSV header")
        missing = sorted(required.difference(reader.fieldnames))
        if missing:
            raise DataContractError(f"{table}: missing required columns: {', '.join(missing)}")
        rows = [{key: (value or "").strip() for key, value in row.items()} for row in reader]
    if not rows:
        raise DataContractError(f"{table}: table is empty")
    return rows


def _text(row: dict[str, str], column: str, table: str, row_number: int) -> str:
    value = row[column].strip()
    if not value:
        raise DataContractError(f"{table} row {row_number}: {column} is empty")
    return value


def _number(
    row: dict[str, str],
    column: str,
    table: str,
    row_number: int,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> float:
    raw = _text(row, column, table, row_number)
    try:
        value = float(raw)
    except ValueError as exc:
        raise DataContractError(
            f"{table} row {row_number}: {column} is not numeric: {raw!r}"
        ) from exc
    if not math.isfinite(value):
        raise DataContractError(f"{table} row {row_number}: {column} is not finite")
    if positive and value <= 0:
        raise DataContractError(f"{table} row {row_number}: {column} must be > 0")
    if nonnegative and value < 0:
        raise DataContractError(f"{table} row {row_number}: {column} must be >= 0")
    return value


def _integer(row: dict[str, str], column: str, table: str, row_number: int) -> int:
    value = _number(row, column, table, row_number, nonnegative=True)
    if not value.is_integer():
        raise DataContractError(f"{table} row {row_number}: {column} must be an integer")
    return int(value)


def _resolved(row: dict[str, str], column: str, table: str, row_number: int) -> int:
    raw = _text(row, column, table, row_number).lower()
    if raw in {"1", "true", "yes"}:
        return 1
    if raw in {"0", "false", "no"}:
        return 0
    raise DataContractError(
        f"{table} row {row_number}: {column} must be one of 0/1/true/false/yes/no"
    )


def _unique(identifier: str, seen: set[str], table: str, row_number: int) -> None:
    if identifier in seen:
        raise DataContractError(f"{table} row {row_number}: duplicate identifier {identifier!r}")
    seen.add(identifier)


def _validate_phase_total(
    *, trajectory_id: str, e2e_ms: float, tool_ms: float, model_ms: float, table: str
) -> None:
    tolerance = max(1.0, e2e_ms * 0.01)
    if tool_ms + model_ms > e2e_ms + tolerance:
        raise DataContractError(
            f"{table}: {trajectory_id!r} has tool+model wall time "
            f"({tool_ms + model_ms:.6f} ms) greater than E2E wall time "
            f"({e2e_ms:.6f} ms) beyond tolerance"
        )


def _latency_column(
    rows: list[dict[str, str]],
    *,
    observed: str,
    predicted: str,
    table: str,
    latency_kind: str,
) -> str:
    """Choose one complete latency column for a rendering pass.

    Predicted exports carry both observed provenance and a separate predicted
    column.  A partially populated predicted column is ambiguous and is
    rejected.  The observed fallback keeps the module's small legacy fixture
    API usable when callers request the predicted label without supplying a
    predicted overlay.
    """

    if latency_kind != "predicted":
        return observed
    present = [bool(str(row.get(predicted, "")).strip()) for row in rows]
    if any(present) and not all(present):
        raise DataContractError(
            f"{table}: {predicted} must be present for every row when latency_kind=predicted"
        )
    return predicted if all(present) else observed


def _read_json_records(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise DataContractError(f"sweep metadata does not exist: {path}")
    try:
        if path.suffix == ".jsonl":
            values = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        else:
            value = json.loads(path.read_text(encoding="utf-8"))
            values = value if isinstance(value, list) else [value]
    except (OSError, json.JSONDecodeError) as exc:
        raise DataContractError(f"sweep metadata is unreadable: {path}: {exc}") from exc
    if any(not isinstance(value, dict) for value in values):
        raise DataContractError(f"sweep metadata records must be JSON objects: {path}")
    return values


def _load_sweep_metadata(path: Path) -> dict[str, dict[str, str]]:
    """Load immutable case identity metadata keyed by canonical run id."""

    metadata: dict[str, dict[str, str]] = {}
    for record in _read_json_records(path):
        run_id = record.get("run_id") or record.get("resume_key")
        variation = record.get("variation")
        if not isinstance(run_id, str) or not run_id:
            continue
        if not isinstance(variation, dict):
            parameter = record.get("sweep_parameter")
            value = record.get("sweep_value")
        else:
            parameter = variation.get("knob")
            value = variation.get("value")
        if not isinstance(parameter, str) or value is None:
            continue
        suite = record.get("suite")
        repository = record.get("repository")
        instance_id = record.get("instance_id")
        if not all(isinstance(item, str) and item for item in (suite, repository, instance_id)):
            raise DataContractError(
                f"sweep metadata record {run_id!r} lacks suite/repository/instance_id"
            )
        item = {
            "suite": suite,
            "repository": repository,
            "category": str(record.get("category", repository)),
            "instance_id": instance_id,
            "repeat_id": str(record.get("repeat_id", "r0")),
            "config_id": str(record.get("config_id", record.get("cell_id", f"{parameter}={value}"))),
            "provenance": str(record.get("provenance", "measured")),
            "sweep_parameter": parameter,
            "sweep_value": str(value),
        }
        previous = metadata.get(run_id)
        if previous is not None and previous != item:
            raise DataContractError(f"sweep metadata has conflicting records for {run_id!r}")
        metadata[run_id] = item
    if not metadata:
        raise DataContractError(f"sweep metadata contains no labelled sweep cases: {path}")
    return metadata


def _sweep_row_metadata(
    row: dict[str, str],
    *,
    row_number: int,
    trajectories_by_id: dict[str, dict[str, Any]],
    metadata_by_run_id: dict[str, dict[str, str]],
) -> dict[str, str]:
    """Resolve suite/category identity without deriving it from measurements."""

    run_id = _text(row, "run_id", "sweep_runs", row_number)
    metadata: dict[str, str] = {}
    for column in SWEEP_METADATA_COLUMNS:
        value = row.get(column, "").strip()
        if value:
            metadata[column] = value

    source_run_id = run_id.split("::step2-baseline::", 1)[0]
    source = trajectories_by_id.get(source_run_id)
    if source is not None:
        for column in SWEEP_METADATA_COLUMNS:
            if column not in metadata and str(source.get(column, "")):
                metadata[column] = str(source[column])
        metadata.setdefault("category", str(source.get("repository", "")))
        if "provenance" not in metadata:
            metadata["provenance"] = (
                "derived_from_measured"
                if "::step2-baseline::" in run_id
                else str(source.get("provenance", "measured"))
            )

    mapped = metadata_by_run_id.get(run_id)
    if mapped is not None:
        for column, value in mapped.items():
            if column not in metadata:
                metadata[column] = value
            elif metadata[column] != value and column in {"suite", "repository", "category", "instance_id", "sweep_parameter", "sweep_value"}:
                raise DataContractError(
                    f"sweep_runs row {row_number}: metadata disagrees for {column}"
                )

    # A ``::step2-baseline::`` row is a materialized view of its shared
    # baseline trajectory.  Preserve that lineage even when an older source
    # CSV incorrectly carried ``provenance=measured``.
    if "::step2-baseline::" in run_id:
        metadata["provenance"] = "derived_from_shared_baseline"

    required = {"suite", "repository", "category", "instance_id", "repeat_id"}
    missing = sorted(column for column in required if not metadata.get(column))
    if missing:
        raise DataContractError(
            f"sweep_runs row {row_number}: missing category identity {', '.join(missing)}; "
            "provide metadata columns or a sealed sweep metadata file"
        )
    return metadata


def _validate_sweep_sample_identities(sweeps: list[dict[str, Any]]) -> None:
    seen: set[tuple[str, str, str, str, str]] = set()
    for row in sweeps:
        key = (
            str(row["parameter"]),
            str(row["value"]),
            str(row["suite"]),
            str(row["instance_id"]),
            str(row["repeat_id"]),
        )
        if key in seen:
            raise DataContractError(
                "sweep_runs: duplicate sample identity for "
                f"parameter={key[0]!r}, value={key[1]!r}, suite={key[2]!r}, "
                f"instance_id={key[3]!r}, repeat_id={key[4]!r}"
            )
        seen.add(key)


def load_canonical_tables(
    trajectories_path: Path,
    tool_events_path: Path,
    model_events_path: Path,
    sweep_runs_path: Path,
    sweep_metadata_path: Path | None = None,
    *,
    latency_kind: str = "observed",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Load, join, and validate all four canonical input tables."""

    if latency_kind not in VALID_LATENCY_KINDS:
        raise DataContractError(
            f"latency_kind must be one of {sorted(VALID_LATENCY_KINDS)}"
        )

    trajectory_rows = _read_csv(trajectories_path, TRAJECTORY_COLUMNS, "trajectories")
    tool_rows = _read_csv(tool_events_path, TOOL_EVENT_COLUMNS, "tool_events")
    model_rows = _read_csv(model_events_path, MODEL_EVENT_COLUMNS, "model_events")
    sweep_rows = _read_csv(sweep_runs_path, SWEEP_COLUMNS, "sweep_runs")

    trajectory_e2e_column = _latency_column(
        trajectory_rows,
        observed="e2e_wall_ms",
        predicted="predicted_e2e_wall_ms",
        table="trajectories",
        latency_kind=latency_kind,
    )
    trajectory_tool_column = _latency_column(
        tool_rows,
        observed="wall_ms",
        predicted="predicted_wall_ms",
        table="tool_events",
        latency_kind=latency_kind,
    )
    trajectory_model_column = _latency_column(
        model_rows,
        observed="wall_ms",
        predicted="predicted_wall_ms",
        table="model_events",
        latency_kind=latency_kind,
    )
    sweep_e2e_column = _latency_column(
        sweep_rows,
        observed="e2e_wall_ms",
        predicted="predicted_e2e_wall_ms",
        table="sweep_runs",
        latency_kind=latency_kind,
    )
    sweep_tool_column = _latency_column(
        sweep_rows,
        observed="tool_wall_ms",
        predicted="predicted_tool_wall_ms",
        table="sweep_runs",
        latency_kind=latency_kind,
    )
    sweep_model_column = _latency_column(
        sweep_rows,
        observed="model_wall_ms",
        predicted="predicted_model_wall_ms",
        table="sweep_runs",
        latency_kind=latency_kind,
    )

    trajectories: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for index, row in enumerate(trajectory_rows, 2):
        run_id = _text(row, "run_id", "trajectories", index)
        _unique(run_id, seen, "trajectories", index)
        if _text(row, "status", "trajectories", index) != "completed":
            raise DataContractError(
                f"trajectories row {index}: final figures require status=completed"
            )
        trajectory = {
            "run_id": run_id,
            "suite": _text(row, "suite", "trajectories", index),
            "repository": _text(row, "repository", "trajectories", index),
            "category": _text(row, "category", "trajectories", index),
            "status": _text(row, "status", "trajectories", index),
            "resolved": _resolved(
                row, "official_resolved", "trajectories", index
            ),
            "e2e_wall_ms": _number(
                row, trajectory_e2e_column, "trajectories", index, positive=True
            ),
            "tool_wall_ms": 0.0,
            "model_wall_ms": 0.0,
            "tool_event_count": 0,
            "model_event_count": 0,
        }
        for optional in (
            "instance_id",
            "config_id",
            "repeat_id",
            "sweep_parameter",
            "sweep_value",
            "submitted",
            "provenance",
        ):
            if optional in row:
                trajectory[optional] = row[optional]
        trajectories[run_id] = trajectory

    seen_tool_events: set[str] = set()
    for index, row in enumerate(tool_rows, 2):
        event_id = _text(row, "event_id", "tool_events", index)
        _unique(event_id, seen_tool_events, "tool_events", index)
        run_id = _text(row, "run_id", "tool_events", index)
        if run_id not in trajectories:
            raise DataContractError(
                f"tool_events row {index}: unknown run_id {run_id!r}"
            )
        if _text(row, "status", "tool_events", index) != "completed":
            raise DataContractError(
                f"tool_events row {index}: completed trajectory contains a non-completed event"
            )
        _text(row, "operation_class", "tool_events", index)
        wall_ms = _number(row, trajectory_tool_column, "tool_events", index, positive=True)
        trajectories[run_id]["tool_wall_ms"] += wall_ms
        trajectories[run_id]["tool_event_count"] += 1

    seen_model_events: set[str] = set()
    for index, row in enumerate(model_rows, 2):
        event_id = _text(row, "request_id", "model_events", index)
        _unique(event_id, seen_model_events, "model_events", index)
        run_id = _text(row, "run_id", "model_events", index)
        if run_id not in trajectories:
            raise DataContractError(
                f"model_events row {index}: unknown run_id {run_id!r}"
            )
        if _text(row, "status", "model_events", index) != "completed":
            raise DataContractError(
                f"model_events row {index}: completed trajectory contains a non-completed event"
            )
        _integer(row, "input_tokens", "model_events", index)
        _integer(row, "output_tokens", "model_events", index)
        _integer(row, "context_tokens", "model_events", index)
        wall_ms = _number(row, trajectory_model_column, "model_events", index, positive=True)
        trajectories[run_id]["model_wall_ms"] += wall_ms
        trajectories[run_id]["model_event_count"] += 1

    joined = []
    for run_id in sorted(trajectories):
        row = trajectories[run_id]
        if row["tool_event_count"] == 0:
            raise DataContractError(f"trajectories: {run_id!r} has no tool events")
        if row["model_event_count"] == 0:
            raise DataContractError(f"trajectories: {run_id!r} has no model events")
        _validate_phase_total(
            trajectory_id=run_id,
            e2e_ms=row["e2e_wall_ms"],
            tool_ms=row["tool_wall_ms"],
            model_ms=row["model_wall_ms"],
            table="trajectories/events",
        )
        row["tool_model_ratio"] = row["tool_wall_ms"] / row["model_wall_ms"]
        row["e2e_residual_wall_ms"] = (
            row["e2e_wall_ms"] - row["tool_wall_ms"] - row["model_wall_ms"]
        )
        # Retain the historical key for downstream callers while giving the
        # figure/report a truthful unknown-residual name.
        row["overhead_wall_ms"] = max(0.0, row["e2e_residual_wall_ms"])
        joined.append(row)

    metadata_by_run_id = _load_sweep_metadata(sweep_metadata_path) if sweep_metadata_path else {}
    trajectories_by_id = {row["run_id"]: row for row in joined}
    sweeps: list[dict[str, Any]] = []
    seen_sweep_runs: set[str] = set()
    values_by_parameter: dict[str, set[str]] = defaultdict(set)
    for index, row in enumerate(sweep_rows, 2):
        sweep_run_id = _text(row, "run_id", "sweep_runs", index)
        _unique(sweep_run_id, seen_sweep_runs, "sweep_runs", index)
        if _text(row, "status", "sweep_runs", index) != "completed":
            raise DataContractError(
                f"sweep_runs row {index}: final figures require status=completed"
            )
        parameter = _text(row, "sweep_parameter", "sweep_runs", index)
        value = _text(row, "sweep_value", "sweep_runs", index)
        metadata = _sweep_row_metadata(
            row,
            row_number=index,
            trajectories_by_id=trajectories_by_id,
            metadata_by_run_id=metadata_by_run_id,
        )
        if metadata.get("sweep_parameter") not in {None, "", parameter}:
            raise DataContractError(
                f"sweep_runs row {index}: metadata sweep_parameter disagrees with table"
            )
        if metadata.get("sweep_value") not in {None, "", value}:
            raise DataContractError(
                f"sweep_runs row {index}: metadata sweep_value disagrees with table"
            )
        e2e_ms = _number(row, sweep_e2e_column, "sweep_runs", index, positive=True)
        tool_ms = _number(row, sweep_tool_column, "sweep_runs", index, nonnegative=True)
        model_ms = _number(row, sweep_model_column, "sweep_runs", index, positive=True)
        _validate_phase_total(
            trajectory_id=sweep_run_id,
            e2e_ms=e2e_ms,
            tool_ms=tool_ms,
            model_ms=model_ms,
            table="sweep_runs",
        )
        values_by_parameter[parameter].add(value)
        sweeps.append(
            {
                "sweep_run_id": sweep_run_id,
                "run_id": sweep_run_id,
                "parameter": parameter,
                "value": value,
                "resolved": _resolved(
                    row, "official_resolved", "sweep_runs", index
                ),
                "e2e_wall_ms": e2e_ms,
                "tool_wall_ms": tool_ms,
                "model_wall_ms": model_ms,
                "tool_model_ratio": tool_ms / model_ms,
                "suite": metadata["suite"],
                "repository": metadata["repository"],
                "category": metadata["category"],
                "instance_id": metadata["instance_id"],
                "repeat_id": metadata["repeat_id"],
                "config_id": metadata.get("config_id", f"{parameter}={value}"),
                "provenance": metadata.get("provenance", "measured"),
            }
        )
    for parameter, values in sorted(values_by_parameter.items()):
        if len(values) < 2:
            raise DataContractError(
                f"sweep_runs: parameter {parameter!r} must contain at least two values"
            )
    _validate_sweep_sample_identities(sweeps)
    return joined, sweeps


def _select_step3_population(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only eligible Step 1 baselines when canonical identity is available."""

    has_config_identity = any("config_id" in row for row in rows)
    if not has_config_identity:
        return rows
    if any(not row.get("config_id") for row in rows):
        raise DataContractError(
            "trajectories: config_id is present but empty for one or more rows"
        )
    candidates = [row for row in rows if row["config_id"] == "shared-baseline"]
    candidates = [
        row
        for row in candidates
        if row.get("provenance", "measured") in {"measured", "derived_from_measured"}
    ]
    if not candidates:
        raise DataContractError(
            "trajectories: no completed measured shared-baseline row is eligible for Step 3"
        )
    return candidates


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _matrix_coverage(
    *,
    reconciliation_report_path: Path | None,
    trajectories_path: Path,
    observed_parameters: set[str],
) -> dict[str, Any]:
    missing_parameters = sorted(REQUIRED_SWEEP_PARAMETERS.difference(observed_parameters))
    result: dict[str, Any] = {
        "required_sweep_parameters": sorted(REQUIRED_SWEEP_PARAMETERS),
        "observed_sweep_parameters": sorted(observed_parameters),
        "missing_sweep_parameters": missing_parameters,
        "parameter_name_coverage_complete": not missing_parameters,
        "complete_assignment_matrix": False,
        "matrix_completeness_basis": "not verified; provide --reconciliation-report",
    }
    if reconciliation_report_path is None:
        return result
    try:
        report = json.loads(reconciliation_report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DataContractError(
            f"reconciliation report is unreadable: {reconciliation_report_path}: {exc}"
        ) from exc
    if report.get("schema_version") != "assignment-plan-reconciliation-report.v1":
        raise DataContractError("reconciliation report has unsupported schema_version")
    if report.get("trajectories_sha256") != _sha256(trajectories_path):
        raise DataContractError(
            "reconciliation report trajectories_sha256 does not match figure input"
        )
    integer_fields = (
        "original_case_count",
        "matched_case_count",
        "remaining_case_count",
        "rejected_or_ambiguous_case_count",
    )
    if any(not isinstance(report.get(field), int) for field in integer_fields):
        raise DataContractError("reconciliation report count fields must be integers")
    complete = (
        report["remaining_case_count"] == 0
        and report["matched_case_count"] == report["original_case_count"]
        and report["rejected_or_ambiguous_case_count"] == 0
        and not missing_parameters
    )
    result.update(
        {
            "complete_assignment_matrix": complete,
            "matrix_completeness_basis": str(reconciliation_report_path),
            "original_case_count": report["original_case_count"],
            "matched_case_count": report["matched_case_count"],
            "remaining_case_count": report["remaining_case_count"],
            "rejected_or_ambiguous_case_count": report[
                "rejected_or_ambiguous_case_count"
            ],
        }
    )
    return result


def _aggregate(rows: Iterable[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row[key])].append(row)
    result = []
    for name in sorted(groups):
        members = groups[name]
        tool_ms = sum(float(row["tool_wall_ms"]) for row in members)
        model_ms = sum(float(row["model_wall_ms"]) for row in members)
        result.append(
            {
                key: name,
                "count": len(members),
                "resolved_count": sum(int(row["resolved"]) for row in members),
                "accuracy_percent": 100.0
                * sum(int(row["resolved"]) for row in members)
                / len(members),
                "average_e2e_wall_ms": sum(float(row["e2e_wall_ms"]) for row in members)
                / len(members),
                "tool_wall_ms": tool_ms,
                "model_wall_ms": model_ms,
                "tool_model_ratio": tool_ms / model_ms,
            }
        )
    return result


def _value_sort(value: str) -> tuple[int, float | str]:
    try:
        parsed = float(value)
    except ValueError:
        return (1, value)
    return (0, parsed)


def aggregate_sweeps(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(row["parameter"], row["value"])].append(row)
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (parameter, value), members in sorted(groups.items()):
        tool_ms = sum(float(row["tool_wall_ms"]) for row in members)
        model_ms = sum(float(row["model_wall_ms"]) for row in members)
        category_rows = _aggregate(members, "category")
        samples = [
            {
                "run_id": str(row["run_id"]),
                "suite": str(row["suite"]),
                "repository": str(row["repository"]),
                "category": str(row["category"]),
                "instance_id": str(row["instance_id"]),
                "repeat_id": str(row["repeat_id"]),
                "config_id": str(row.get("config_id", "")),
                "provenance": str(row.get("provenance", "measured")),
                "resolved": int(row["resolved"]),
                "e2e_wall_ms": float(row["e2e_wall_ms"]),
                "tool_wall_ms": float(row["tool_wall_ms"]),
                "model_wall_ms": float(row["model_wall_ms"]),
                "tool_model_ratio": float(row["tool_model_ratio"]),
            }
            for row in sorted(members, key=lambda item: (str(item["category"]), str(item["instance_id"]), str(item["run_id"])))
        ]
        result[parameter].append(
            {
                "parameter": parameter,
                "value": value,
                "count": len(members),
                "resolved_count": sum(int(row["resolved"]) for row in members),
                "accuracy_percent": 100.0
                * sum(int(row["resolved"]) for row in members)
                / len(members),
                "average_e2e_wall_ms": sum(float(row["e2e_wall_ms"]) for row in members)
                / len(members),
                "tool_model_ratio": tool_ms / model_ms,
                "categories": category_rows,
                "samples": samples,
            }
        )
    return {
        parameter: sorted(settings, key=lambda item: _value_sort(item["value"]))
        for parameter, settings in sorted(result.items())
    }


def _svg_text(x: float, y: float, text: str, size: int = 12, **attrs: str) -> str:
    normalized = {
        key.rstrip("_").replace("_", "-"): value for key, value in attrs.items()
    }
    normalized.setdefault("font-family", "Arial, Helvetica, sans-serif")
    normalized.setdefault("font-weight", "650")
    normalized.setdefault("fill", TEXT)
    extra = " ".join(
        f'{key}="{escape(str(value))}"' for key, value in normalized.items()
    )
    return f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" {extra}>{escape(text)}</text>'


def _svg_style() -> str:
    return (
        "<style>"
        "text{font-family:Arial,Helvetica,sans-serif;fill:#344054;}"
        ".title{font-weight:800;}"
        ".subtitle{font-weight:600;}"
        ".axis-label{font-weight:800;}"
        ".tick{font-weight:700;}"
        ".frame{stroke:#667085;stroke-width:2;fill:none;}"
        ".grid{stroke:#d0d5dd;stroke-width:1;}"
        "</style>"
    )


def _data_attributes(point: dict[str, Any]) -> str:
    """Expose source fields in SVG so dense plots remain inspectable."""

    names = (
        "run_id",
        "event_id",
        "request_id",
        "suite",
        "repository",
        "category",
        "instance_id",
        "parameter",
        "value",
        "operation_class",
        "input_tokens",
        "output_tokens",
        "context_tokens",
        "wall_ms",
        "request_proxy_wall_ms",
    )
    attrs: list[str] = []
    if point.get("data_sample"):
        attrs.append('data-sample="true"')
    for name in names:
        if name not in point or point[name] is None:
            continue
        html_name = name.replace("_", "-")
        attrs.append(f'data-{html_name}="{escape(str(point[name]))}"')
    return " ".join(attrs)


def _bounds(values: list[float], *, log_scale: bool = False) -> tuple[float, float]:
    if not values:
        raise DataContractError("plot: no values")
    if log_scale and any(value <= 0 for value in values):
        raise DataContractError("plot: logarithmic axis received a non-positive value")
    transformed = [math.log10(value) if log_scale else value for value in values]
    low, high = min(transformed), max(transformed)
    if math.isclose(low, high):
        padding = max(abs(low) * 0.1, 0.5)
    else:
        padding = (high - low) * 0.1
    return low - padding, high + padding


def _nonnegative_bounds(values: list[float]) -> tuple[float, float]:
    """Return padded bounds while keeping a nonnegative measured quantity at zero."""

    low, high = _bounds(values)
    return max(0.0, low), high


def _scatter_svg(
    *,
    title: str,
    subtitle: str,
    x_label: str,
    y_label: str,
    points: list[dict[str, Any]],
    log_x: bool = False,
    width: int = 960,
    height: int = 620,
) -> str:
    if not points:
        raise DataContractError(f"plot {title!r}: no points")
    left, right, top, bottom = 100, 210, 90, 90
    plot_w, plot_h = width - left - right, height - top - bottom
    x_values = [float(point["x"]) for point in points]
    y_values = [float(point["y"]) for point in points]
    x_low, x_high = (
        _bounds(x_values, log_scale=True)
        if log_x
        else _nonnegative_bounds(x_values)
    )
    y_low, y_high = _nonnegative_bounds(y_values)
    groups = sorted({str(point.get("group", "all")) for point in points})
    colors = {group: PALETTE[index % len(PALETTE)] for index, group in enumerate(groups)}

    def x_pos(value: float) -> float:
        transformed = math.log10(value) if log_x else value
        return left + (transformed - x_low) / (x_high - x_low) * plot_w

    def y_pos(value: float) -> float:
        return top + plot_h - (value - y_low) / (y_high - y_low) * plot_h

    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        _svg_style(),
        _svg_text(left, 34, title, 23, fill=TEXT, class_="title", **{"font-weight": "800"}),
        _svg_text(left, 60, subtitle, 13, fill=TEXT, class_="subtitle"),
        f'<rect class="frame" x="{left}" y="{top}" width="{plot_w}" height="{plot_h}"/>',
    ]
    for tick in range(6):
        fraction = tick / 5
        xx = left + fraction * plot_w
        yy = top + fraction * plot_h
        x_value = x_low + fraction * (x_high - x_low)
        y_value = y_high - fraction * (y_high - y_low)
        x_tick = 10**x_value if log_x else x_value
        lines.extend(
            [
                f'<line class="grid" x1="{xx:.1f}" y1="{top}" x2="{xx:.1f}" '
                f'y2="{top + plot_h}"/>',
                f'<line class="grid" x1="{left}" y1="{yy:.1f}" x2="{left + plot_w}" '
                f'y2="{yy:.1f}"/>',
                _svg_text(xx, top + plot_h + 26, f"{x_tick:.3g}", 12, fill=TEXT, class_="tick",
                          **{"text-anchor": "middle"}),
                _svg_text(left - 14, yy + 4, f"{y_value:.3g}", 12, fill=TEXT, class_="tick",
                          **{"text-anchor": "end"}),
            ]
        )
    lines.extend(
        [
            _svg_text(left + plot_w / 2, height - 32, x_label, 14, fill=TEXT, class_="axis-label",
                      **{"text-anchor": "middle"}),
            _svg_text(24, top + plot_h / 2, y_label, 14, fill=TEXT, class_="axis-label",
                      transform=f"rotate(-90 24 {top + plot_h / 2:.1f})",
                      **{"text-anchor": "middle"}),
        ]
    )
    for point in sorted(points, key=lambda item: (str(item.get("group", "")), str(item["label"]))):
        group = str(point.get("group", "all"))
        xx, yy = x_pos(float(point["x"])), y_pos(float(point["y"]))
        data = _data_attributes(point)
        detail = str(point.get("title", point["label"]))
        lines.append(
            f'<circle {data} cx="{xx:.1f}" cy="{yy:.1f}" r="6" fill="{colors[group]}" '
            'fill-opacity="0.82">'
            f'<title>{escape(detail)}</title></circle>'
        )
        if point.get("annotate", True):
            lines.append(_svg_text(xx + 8, yy - 8, str(point["label"]), 10, fill=colors[group], font_weight="700"))
    for index, group in enumerate(groups):
        yy = top + index * 22
        lines.append(f'<circle cx="{width - right + 25}" cy="{yy}" r="6" fill="{colors[group]}"/>')
        lines.append(_svg_text(width - right + 38, yy + 4, group, 12, fill=TEXT, font_weight="750"))
    lines.append("</svg>")
    return "\n".join(lines)


def _repository_ratio_svg(
    rows: list[dict[str, Any]], *, latency_kind: str = "observed"
) -> str:
    """Render one sample dot per trajectory, grouped by repository category."""
    if not rows:
        raise DataContractError("repository ratio plot has no rows")
    groups = sorted({str(row["repository"]) for row in rows})
    rows_by_group = {
        group: sorted(
            [row for row in rows if str(row["repository"]) == group],
            key=lambda row: (str(row["suite"]), str(row["run_id"])),
        )
        for group in groups
    }
    width = 1040
    height = max(420, 150 + 54 * len(groups))
    left, right, top, bottom = 260, 70, 78, 65
    plot_w = width - left - right
    plot_h = height - top - bottom
    low, high = _bounds([float(row["tool_model_ratio"]) for row in rows], log_scale=True)
    colors = {"lite": PALETTE[0], "verified": PALETTE[1]}
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        _svg_style(),
        _svg_text(30, 34, f"Step 1: {latency_kind.title()} tool/model latency ratio by repository", 23,
                  fill=TEXT, class_="title", **{"font-weight": "800"}),
        _svg_text(30, 60, f"One {latency_kind} sample dot per trajectory; category = repository.", 13,
                  fill=TEXT, class_="subtitle"),
        f'<rect class="frame" x="{left}" y="{top}" width="{plot_w}" height="{plot_h}"/>',
    ]
    for tick in range(6):
        fraction = tick / 5
        x = left + fraction * plot_w
        value = 10 ** (low + fraction * (high - low))
        lines.append(f'<line class="grid" x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top + plot_h}"/>')
        lines.append(_svg_text(x, top + plot_h + 27, f"{value:.3g}", 12, fill=TEXT, class_="tick",
                               **{"text-anchor": "middle"}))
    step = plot_h / max(1, len(groups))
    for index, group in enumerate(groups):
        center = top + (index + 0.5) * step
        lines.append(f'<line class="grid" x1="{left}" y1="{center:.1f}" x2="{left + plot_w}" y2="{center:.1f}"/>')
        lines.append(_svg_text(left - 12, center + 4, group, 12, fill=TEXT, class_="tick",
                               **{"text-anchor": "end"}))
        samples = rows_by_group[group]
        spacing = min(15.0, (step * 0.70) / max(1, len(samples)))
        offset = -((len(samples) - 1) * spacing) / 2.0
        for sample_index, row in enumerate(samples):
            x = left + (math.log10(float(row["tool_model_ratio"])) - low) / (high - low) * plot_w
            y = center + offset + sample_index * spacing
            suite = str(row["suite"])
            color = colors.get(suite, PALETTE[index % len(PALETTE)])
            lines.append(
                f'<circle data-sample="true" data-run-id="{escape(str(row["run_id"]))}" '
                f'data-repository="{escape(group)}" data-suite="{escape(suite)}" '
                f'cx="{x:.1f}" cy="{y:.1f}" r="6" fill="{color}" fill-opacity="0.82">'
                f'<title>{escape(group)} / {escape(suite)} / {escape(str(row.get("instance_id", row["run_id"])))}, '
                f'ratio={float(row["tool_model_ratio"]):.6g}</title></circle>'
            )
    legend_x = width - right - 118
    for legend_index, suite in enumerate(("lite", "verified")):
        y = top + 18 + legend_index * 22
        lines.append(f'<circle cx="{legend_x}" cy="{y}" r="6" fill="{colors[suite]}"/>')
        lines.append(_svg_text(legend_x + 13, y + 4, suite.title(), 12, fill=TEXT, font_weight="750"))
    lines.append(_svg_text(left + plot_w / 2, height - 18,
                           f"{latency_kind.title()} tool/model wall-latency ratio (log scale)", 14, fill=TEXT, class_="axis-label",
                           **{"text-anchor": "middle"}))
    lines.append("</svg>")
    return "\n".join(lines)


def _step2_category_names(settings: list[dict[str, Any]]) -> list[str]:
    categories = {
        str(category["category"])
        for setting in settings
        for category in setting.get("categories", [])
    }
    if not categories:
        raise DataContractError("Step 2 plot has no category-labelled aggregates")
    return sorted(categories)


def _step2_category_colors(categories: list[str]) -> dict[str, str]:
    return {
        category: PALETTE[index % len(PALETTE)]
        for index, category in enumerate(categories)
    }


STEP2_MARKER_SHAPES = ("circle", "square", "diamond", "triangle")


def _step2_value_orders(settings: list[dict[str, Any]]) -> dict[str, int]:
    values = sorted(
        {str(setting["value"]) for setting in settings},
        key=_value_sort,
    )
    return {value: index for index, value in enumerate(values)}


def _step2_marker_shape(value_order: int) -> str:
    return STEP2_MARKER_SHAPES[value_order % len(STEP2_MARKER_SHAPES)]


def _step2_marker(
    point: dict[str, Any],
    *,
    x: float,
    y: float,
    size: float,
    fill: str,
    opacity: float,
    stroke: str,
    stroke_width: float,
) -> str:
    """Render a value-order marker while retaining every point data attribute."""

    shape = _step2_marker_shape(int(point.get("value_order", 0)))
    data = _data_attributes(point)
    title = escape(str(point.get("title", point.get("label", ""))))
    style = (
        f'{data} fill="{escape(fill)}" fill-opacity="{opacity:.2f}" '
        f'stroke="{escape(stroke)}" stroke-width="{stroke_width:.1f}"'
    )
    if shape == "circle":
        element = f'<circle {style} cx="{x:.1f}" cy="{y:.1f}" r="{size:.1f}">'
        return f'{element}<title>{title}</title></circle>'
    if shape == "square":
        edge = size * 1.65
        element = (
            f'<rect {style} x="{x - edge / 2:.1f}" y="{y - edge / 2:.1f}" '
            f'width="{edge:.1f}" height="{edge:.1f}">'
        )
        return f'{element}<title>{title}</title></rect>'
    if shape == "diamond":
        points = f"{x:.1f},{y - size:.1f} {x + size:.1f},{y:.1f} {x:.1f},{y + size:.1f} {x - size:.1f},{y:.1f}"
    else:
        points = (
            f"{x:.1f},{y - size:.1f} {x + size:.1f},{y + size:.8f} "
            f"{x - size:.1f},{y + size:.8f}"
        )
    return f'<polygon {style} points="{points}"><title>{title}</title></polygon>'


def _step2_category_points(
    settings: list[dict[str, Any]], category: str, x_key: str, y_key: str
) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    value_orders = _step2_value_orders(settings)
    for setting in sorted(settings, key=lambda item: _value_sort(str(item["value"]))):
        matches = [
            item for item in setting.get("categories", [])
            if str(item["category"]) == category
        ]
        if len(matches) > 1:
            raise DataContractError(
                f"Step 2 setting {setting['parameter']}={setting['value']} has duplicate category {category!r}"
            )
        if not matches:
            continue
        aggregate = matches[0]
        points.append(
            {
                "x": float(aggregate[x_key]),
                "y": float(aggregate[y_key]),
                "category": category,
                "parameter": str(setting["parameter"]),
                "value": str(setting["value"]),
                "value_order": value_orders[str(setting["value"])],
                "label": f"{category} / {setting['value']}",
                "group": category,
                "title": (
                    f"category={category}; {setting['parameter']}={setting['value']}; "
                    f"n={aggregate['count']}"
                ),
            }
        )
    return points


def _step2_sample_points(
    settings: list[dict[str, Any]], parameter: str
) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    value_orders = _step2_value_orders(settings)
    for setting in settings:
        for sample in setting.get("samples", []):
            points.append(
                {
                    "x": float(sample["tool_model_ratio"]),
                    "y": float(sample["e2e_wall_ms"]),
                    "category": str(sample["category"]),
                    "repository": str(sample["repository"]),
                    "suite": str(sample["suite"]),
                    "instance_id": str(sample["instance_id"]),
                    "run_id": str(sample["run_id"]),
                    "parameter": parameter,
                    "value": str(setting["value"]),
                    "value_order": value_orders[str(setting["value"])],
                    "label": str(sample["instance_id"]),
                    "group": str(sample["category"]),
                    "data_sample": True,
                    "title": (
                        f"category={sample['category']}; repository={sample['repository']}; "
                        f"{parameter}={setting['value']}; instance={sample['instance_id']}; "
                        f"ratio={float(sample['tool_model_ratio']):.6g}; "
                        f"E2E={float(sample['e2e_wall_ms']):.3f} ms"
                    ),
                }
            )
    return points


def _step2_domains(
    settings: list[dict[str, Any]], x_key: str, y_key: str, *, log_x: bool, sample_view: bool
) -> tuple[float, float, float, float]:
    if sample_view:
        points = _step2_sample_points(settings, str(settings[0]["parameter"]))
        x_values = [float(point["x"]) for point in points]
        y_values = [float(point["y"]) for point in points]
    else:
        points = [
            category
            for setting in settings
            for category in setting.get("categories", [])
        ]
        x_values = [float(point[x_key]) for point in points]
        y_values = [float(point[y_key]) for point in points]
    x_low, x_high = _bounds(x_values, log_scale=log_x) if log_x else _nonnegative_bounds(x_values)
    if y_key == "accuracy_percent":
        y_low, y_high = 0.0, 100.0
    else:
        y_low, y_high = _nonnegative_bounds(y_values)
    return x_low, x_high, y_low, y_high


def _step2_panel(
    lines: list[str],
    *,
    panel_x: float,
    panel_y: float,
    panel_w: float,
    panel_h: float,
    panel_title: str,
    x_label: str,
    y_label: str,
    settings: list[dict[str, Any]],
    categories: list[str],
    x_key: str,
    y_key: str,
    log_x: bool,
    sample_view: bool,
    domains: tuple[float, float, float, float] | None = None,
) -> None:
    if not settings:
        raise DataContractError(f"Step 2 panel {panel_title!r} has no settings")
    x_low, x_high, y_low, y_high = domains or _step2_domains(
        settings, x_key, y_key, log_x=log_x, sample_view=sample_view
    )
    # The right-hand panel has long rotated y-axis text and scientific tick
    # labels.  Give that panel a little more internal left padding so the two
    # text columns remain distinct at native SVG size.
    plot_left = panel_x + (104 if panel_x >= 900 else 70)
    plot_right = panel_x + panel_w - 22
    plot_top = panel_y + 46
    plot_bottom = panel_y + panel_h - 62
    plot_w = plot_right - plot_left
    plot_h = plot_bottom - plot_top
    colors = _step2_category_colors(categories)

    def x_pos(value: float) -> float:
        transformed = math.log10(value) if log_x else value
        return plot_left + (transformed - x_low) / (x_high - x_low) * plot_w

    def y_pos(value: float) -> float:
        return plot_bottom - (value - y_low) / (y_high - y_low) * plot_h

    lines.append(
        f'<rect class="frame" x="{panel_x:.1f}" y="{panel_y:.1f}" '
        f'width="{panel_w:.1f}" height="{panel_h:.1f}"/>'
    )
    lines.append(_svg_text(panel_x + 14, panel_y + 24, panel_title, 13, class_="title", font_weight="800"))
    for tick in range(6):
        fraction = tick / 5
        xx = plot_left + fraction * plot_w
        yy = plot_top + fraction * plot_h
        x_transformed = x_low + fraction * (x_high - x_low)
        x_value = 10 ** x_transformed if log_x else x_transformed
        y_value = y_high - fraction * (y_high - y_low)
        lines.append(
            f'<line class="grid" x1="{xx:.1f}" y1="{plot_top:.1f}" '
            f'x2="{xx:.1f}" y2="{plot_bottom:.1f}"/>'
        )
        lines.append(
            f'<line class="grid" x1="{plot_left:.1f}" y1="{yy:.1f}" '
            f'x2="{plot_right:.1f}" y2="{yy:.1f}"/>'
        )
        lines.append(_svg_text(xx, plot_bottom + 22, f"{x_value:.3g}", 11, class_="tick", font_weight="700", **{"text-anchor": "middle"}))
        lines.append(_svg_text(plot_left - 8, yy + 4, f"{y_value:.3g}", 11, class_="tick", font_weight="700", **{"text-anchor": "end"}))
    lines.append(_svg_text(plot_left + plot_w / 2, panel_y + panel_h - 16, x_label, 12, class_="axis-label", font_weight="800", **{"text-anchor": "middle"}))
    # Keep the rotated label inside the panel's left margin.  The combined
    # figure is rendered at a smaller panel width than the standalone sweep
    # plots, so placing this on the frame edge can clip the first glyphs or
    # crowd the tick labels.
    y_axis_x = panel_x + (28 if panel_x >= 900 else 24)
    lines.append(_svg_text(y_axis_x, plot_top + plot_h / 2, y_label, 12, class_="axis-label", font_weight="800", transform=f"rotate(-90 {y_axis_x:.1f} {plot_top + plot_h / 2:.1f})", **{"text-anchor": "middle"}))

    if sample_view:
        points = _step2_sample_points(settings, str(settings[0]["parameter"]))
        for point in points:
            xx, yy = x_pos(float(point["x"])), y_pos(float(point["y"]))
            lines.append(_step2_marker(
                point,
                x=xx,
                y=yy,
                size=4.5,
                fill=colors[str(point["category"])],
                opacity=0.58,
                stroke="white",
                stroke_width=0.7,
            ))
        return

    for category in categories:
        points = _step2_category_points(settings, category, x_key, y_key)
        coordinates = [(x_pos(point["x"]), y_pos(point["y"])) for point in points]
        if len(coordinates) > 1:
            point_string = " ".join(f"{x:.1f},{y:.1f}" for x, y in coordinates)
            lines.append(
                f'<polyline data-category="{escape(category)}" points="{point_string}" '
                f'fill="none" stroke="{colors[category]}" stroke-width="2.7" '
                'stroke-linejoin="round" stroke-linecap="round"/>'
            )
        for point, (xx, yy) in zip(points, coordinates):
            lines.append(_step2_marker(
                point,
                x=xx,
                y=yy,
                size=5,
                fill=colors[category],
                opacity=1.0,
                stroke="white",
                stroke_width=1.0,
            ))


def _step2_legend(
    lines: list[str],
    categories: list[str],
    *,
    width: int,
    y: float,
    value_settings: list[dict[str, Any]] | None = None,
    columns: int = 4,
) -> None:
    colors = _step2_category_colors(categories)
    cell_w = max(220, (width - 60) / columns)
    for index, category in enumerate(categories):
        column = index % columns
        row = index // columns
        x = 30 + column * cell_w
        yy = y + row * 24
        lines.append(f'<circle cx="{x:.1f}" cy="{yy - 4:.1f}" r="6" fill="{colors[category]}"/>')
        lines.append(_svg_text(x + 13, yy, category, 11, font_weight="750"))
    if value_settings:
        values = sorted(
            {str(setting["value"]) for setting in value_settings},
            key=_value_sort,
        )
        category_rows = math.ceil(len(categories) / columns)
        value_y = y + category_rows * 24 + 18
        lines.append(_svg_text(30, value_y, "Marker shape = sweep value order (low to high):", 11, font_weight="800"))
        cursor = 330.0
        for index, value in enumerate(values):
            shape = _step2_marker_shape(index)
            marker_x = cursor + 8
            marker_y = value_y - 4
            style = 'fill="#667085" stroke="white" stroke-width="0.8"'
            if shape == "circle":
                lines.append(f'<circle cx="{marker_x:.1f}" cy="{marker_y:.1f}" r="6" {style}/>')
            elif shape == "square":
                lines.append(f'<rect x="{marker_x - 5:.1f}" y="{marker_y - 5:.1f}" width="10" height="10" {style}/>')
            elif shape == "diamond":
                lines.append(f'<polygon points="{marker_x:.1f},{marker_y - 6:.1f} {marker_x + 6:.1f},{marker_y:.1f} {marker_x:.1f},{marker_y + 6:.1f} {marker_x - 6:.1f},{marker_y:.1f}" {style}/>')
            else:
                lines.append(f'<polygon points="{marker_x:.1f},{marker_y - 6:.1f} {marker_x + 6:.1f},{marker_y + 6:.1f} {marker_x - 6:.1f},{marker_y + 6:.1f}" {style}/>')
            label = f"{index + 1}: {value}"
            lines.append(_svg_text(marker_x + 12, value_y, label, 11, font_weight="750"))
            cursor += max(135.0, 13.0 + len(label) * 7.0)


def _step2_value_caption(
    lines: list[str],
    parameter: str,
    settings: list[dict[str, Any]],
    *,
    x: float,
    y: float,
) -> None:
    """Add a compact per-row value-order key to the combined sensitivity plot."""

    values = sorted(
        {str(setting["value"]) for setting in settings},
        key=_value_sort,
    )
    prefix = f"{parameter} value order (low to high):"
    lines.append(_svg_text(x, y, prefix, 10, font_weight="800"))
    cursor = x + max(170.0, len(prefix) * 6.2 + 14.0)
    for index, value in enumerate(values):
        shape = _step2_marker_shape(index)
        marker_x = cursor + 6
        marker_y = y - 4
        style = 'fill="#667085" stroke="white" stroke-width="0.8"'
        if shape == "circle":
            lines.append(f'<circle cx="{marker_x:.1f}" cy="{marker_y:.1f}" r="5" {style}/>')
        elif shape == "square":
            lines.append(f'<rect x="{marker_x - 4:.1f}" y="{marker_y - 4:.1f}" width="8" height="8" {style}/>')
        elif shape == "diamond":
            lines.append(f'<polygon points="{marker_x:.1f},{marker_y - 5:.1f} {marker_x + 5:.1f},{marker_y:.1f} {marker_x:.1f},{marker_y + 5:.1f} {marker_x - 5:.1f},{marker_y:.1f}" {style}/>')
        else:
            lines.append(f'<polygon points="{marker_x:.1f},{marker_y - 5:.1f} {marker_x + 5:.1f},{marker_y + 5:.1f} {marker_x - 5:.1f},{marker_y + 5:.1f}" {style}/>')
        label = f"{index + 1}={value}"
        lines.append(_svg_text(marker_x + 10, y, label, 10, font_weight="750"))
        cursor += max(92.0, len(label) * 7.0 + 24.0)


def _step2_parameter_svg(
    parameter: str, settings: list[dict[str, Any]], *, latency_kind: str = "observed"
) -> str:
    categories = _step2_category_names(settings)
    width, height = 1720, 800
    latency_word = latency_kind.title()
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        _svg_style(),
        _svg_text(30, 32, f"Step 2 sweep: {parameter}", 24, class_="title", font_weight="800"),
        _svg_text(
            30,
            58,
            f"Observed accuracy by category; {latency_word.lower()} latency values; marker shape shows sweep value order; right panel shows every sample.",
            13,
            class_="subtitle",
        ),
    ]
    panel_y, panel_w, panel_h = 82, 520, 610
    panel_xs = (30, 590, 1150)
    _step2_panel(
        lines,
        panel_x=panel_xs[0], panel_y=panel_y, panel_w=panel_w, panel_h=panel_h,
        panel_title="Category curves: accuracy vs average E2E",
        x_label=f"{latency_word} E2E wall latency (ms)", y_label="Observed accuracy (%)",
        settings=settings, categories=categories, x_key="average_e2e_wall_ms", y_key="accuracy_percent",
        log_x=False, sample_view=False,
    )
    _step2_panel(
        lines,
        panel_x=panel_xs[1], panel_y=panel_y, panel_w=panel_w, panel_h=panel_h,
        panel_title="Category curves: accuracy vs ratio",
        x_label=f"{latency_word} tool/model ratio (log scale)", y_label="Observed accuracy (%)",
        settings=settings, categories=categories, x_key="tool_model_ratio", y_key="accuracy_percent",
        log_x=True, sample_view=False,
    )
    _step2_panel(
        lines,
        panel_x=panel_xs[2], panel_y=panel_y, panel_w=panel_w, panel_h=panel_h,
        panel_title="Individual samples: latency vs ratio",
        x_label=f"{latency_word} tool/model ratio (log scale)", y_label=f"{latency_word} E2E wall latency (ms)",
        settings=settings, categories=categories, x_key="tool_model_ratio", y_key="e2e_wall_ms",
        log_x=True, sample_view=True,
    )
    _step2_legend(lines, categories, width=width, y=710, value_settings=settings)
    lines.append("</svg>")
    return "\n".join(lines)


def _step2_combined_svg(
    sweeps: dict[str, list[dict[str, Any]]], *, latency_kind: str = "observed"
) -> str:
    """Render all parameters as honest category-faceted rows."""

    if not sweeps:
        raise DataContractError("combined Step 2 plot has no sweep rows")
    parameters = sorted(sweeps)
    all_settings = [setting for parameter in parameters for setting in sweeps[parameter]]
    categories = _step2_category_names(all_settings)
    width = 1440
    row_step, panel_y, panel_w, panel_h = 275, 82, 430, 235
    height = panel_y + len(parameters) * row_step + 95
    latency_word = latency_kind.title()
    all_category_rows = [
        category
        for setting in all_settings
        for category in setting.get("categories", [])
    ]
    all_samples = [sample for setting in all_settings for sample in setting.get("samples", [])]
    e2e_accuracy_x_low, e2e_accuracy_x_high = _nonnegative_bounds(
        [float(row["average_e2e_wall_ms"]) for row in all_category_rows]
    )
    ratio_latency_y_low, ratio_latency_y_high = _nonnegative_bounds(
        [float(sample["e2e_wall_ms"]) for sample in all_samples]
    )
    domains = {
        "e2e_accuracy": (
            e2e_accuracy_x_low,
            e2e_accuracy_x_high,
            0.0,
            100.0,
        ),
        "ratio_accuracy": (
            *_bounds([float(row["tool_model_ratio"]) for row in all_category_rows], log_scale=True),
            0.0,
            100.0,
        ),
        "ratio_latency": (
            *_bounds([float(sample["tool_model_ratio"]) for sample in all_samples], log_scale=True),
            ratio_latency_y_low,
            ratio_latency_y_high,
        ),
    }
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        _svg_style(),
        _svg_text(30, 32, "Step 2: combined hyperparameter sensitivity", 24, class_="title", font_weight="800"),
        _svg_text(
            30,
            58,
            f"Rows are parameters; color = category, marker shape = value order (low to high), right-column markers = samples. "
            f"Latency is {latency_kind}; accuracy is observed.",
            13,
            class_="subtitle",
        ),
    ]
    for index, parameter in enumerate(parameters):
        row_y = panel_y + index * row_step
        settings = sweeps[parameter]
        _step2_panel(
            lines,
            panel_x=30, panel_y=row_y, panel_w=panel_w, panel_h=panel_h,
            panel_title=f"{parameter}: accuracy vs average E2E",
            x_label=f"{latency_word} E2E wall (ms)", y_label="Observed accuracy (%)",
            settings=settings, categories=categories, x_key="average_e2e_wall_ms", y_key="accuracy_percent",
            log_x=False, sample_view=False, domains=domains["e2e_accuracy"],
        )
        _step2_panel(
            lines,
            panel_x=500, panel_y=row_y, panel_w=panel_w, panel_h=panel_h,
            panel_title=f"{parameter}: accuracy vs ratio",
            x_label=f"{latency_word} tool/model ratio", y_label="Observed accuracy (%)",
            settings=settings, categories=categories, x_key="tool_model_ratio", y_key="accuracy_percent",
            log_x=True, sample_view=False, domains=domains["ratio_accuracy"],
        )
        _step2_panel(
            lines,
            panel_x=970, panel_y=row_y, panel_w=panel_w, panel_h=panel_h,
            panel_title=f"{parameter}: individual samples",
            x_label=f"{latency_word} tool/model ratio", y_label=f"{latency_word} E2E wall (ms)",
            settings=settings, categories=categories, x_key="tool_model_ratio", y_key="e2e_wall_ms",
            log_x=True, sample_view=True, domains=domains["ratio_latency"],
        )
        _step2_value_caption(
            lines,
            parameter,
            settings,
            x=30,
            y=row_y + panel_h + 26,
        )
    _step2_legend(lines, categories, width=width, y=height - 58)
    lines.append("</svg>")
    return "\n".join(lines)


def _step3_svg(
    rows: list[dict[str, Any]], *, latency_kind: str = "observed", limit: int = 12
) -> str:
    if len(rows) != 1:
        raise DataContractError("Step 3 breakdown requires exactly one sealed selected trajectory")
    # Step 3 deliberately has one selected trajectory. Keep this a compact,
    # publication-facing accounting figure: the axis gives the E2E scale and
    # the table makes every segment and its share auditable even when a small
    # segment cannot carry an in-bar label.
    selected = rows
    width = 1100
    height = 430
    left, right, top = 275, 55, 125
    bar_height = 42
    plot_w = width - left - right
    row = selected[0]
    total_e2e = max(0.0, float(row["e2e_wall_ms"]))
    if total_e2e <= 0:
        raise DataContractError("Step 3 breakdown E2E wall must be positive")
    latency_word = latency_kind.title()
    residual_label = (
        "unknown E2E residual"
        if latency_kind == "observed"
        else "predicted runner overhead"
    )
    residual_subtitle = (
        "Residual E2E wall is unattributed; request wall is a proxy measurement, not GPU residency"
        if latency_kind == "observed"
        else "Predicted E2E includes the frozen API overhead term; request wall is a proxy, not GPU residency"
    )
    instance_id = str(row.get("instance_id") or row.get("run_id") or "selected instance")
    run_id = _compact_run_id(str(row.get("run_id", "")), limit=34)
    segments = [
        ("tool_wall_ms", TOOL_COLOR, "tool wall"),
        ("model_wall_ms", MODEL_COLOR, "model request proxy wall"),
        ("e2e_residual_wall_ms", OVERHEAD_COLOR, residual_label),
    ]
    segment_values = [max(0.0, float(row.get(key, 0.0))) for key, _, _ in segments]
    if sum(segment_values) <= 0:
        raise DataContractError("Step 3 breakdown has no positive latency segments")

    def fmt_ms(value: float) -> str:
        return f"{value:,.3f}"

    def fmt_pct(value: float) -> str:
        return f"{100.0 * value / total_e2e:.1f}%"

    def x_pos(value: float) -> float:
        return left + (value / total_e2e) * plot_w

    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        _svg_style(),
        _svg_text(30, 34, f"Step 3: high tool/model-ratio {latency_word.lower()} latency breakdown", 23,
                  fill=TEXT, class_="title", **{"font-weight": "800"}),
        _svg_text(30, 58, residual_subtitle, 13, fill=TEXT, class_="subtitle"),
        _svg_text(30, 88, f"Selected instance: {instance_id}", 14, fill=TEXT,
                  class_="subtitle", font_weight="800"),
        _svg_text(30, 106, f"run {run_id}", 11, fill=TEXT, class_="subtitle"),
    ]
    frame_y = top - 14
    frame_height = bar_height + 28
    lines.append(
        f'<rect class="frame" x="{left:.1f}" y="{frame_y:.1f}" '
        f'width="{plot_w:.1f}" height="{frame_height:.1f}"/>'
    )
    y = top
    x = left
    for (key, color, label), value in zip(segments, segment_values):
        bar_width = value / total_e2e * plot_w
        lines.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" height="{bar_height:.1f}" '
            f'fill="{color}"><title>{escape(label)}: {fmt_ms(value)} ms ({fmt_pct(value)})</title></rect>'
        )
        # Keep labels on segments with enough room; the table below is the
        # authoritative label for narrow segments.
        if bar_width >= 145:
            lines.append(_svg_text(
                x + bar_width / 2,
                y + 18,
                f"{label}: {fmt_ms(value)} ms",
                10,
                fill="white",
                font_weight="800",
                **{"text-anchor": "middle"},
            ))
            lines.append(_svg_text(
                x + bar_width / 2,
                y + 34,
                f"{fmt_pct(value)} of E2E",
                10,
                fill="white",
                **{"text-anchor": "middle"},
            ))
        x += bar_width
    lines.append(_svg_text(x + 9, y + 26, f"R={float(row['tool_model_ratio']):.3g}", 11,
                           fill=TEXT, font_weight="750"))

    # Explicit E2E wall axis in milliseconds with bold, readable ticks.
    tick_values = [total_e2e * index / 4.0 for index in range(5)]
    for value in tick_values:
        tick_x = x_pos(value)
        lines.append(
            f'<line class="grid" x1="{tick_x:.1f}" y1="{frame_y:.1f}" '
            f'x2="{tick_x:.1f}" y2="{top + bar_height + 14:.1f}"/>'
        )
        lines.append(_svg_text(tick_x, top + bar_height + 32, fmt_ms(value), 11, fill=TEXT,
                               class_="tick", font_weight="800", **{"text-anchor": "middle"}))
    lines.append(_svg_text(left + plot_w / 2, top + bar_height + 58,
                           f"{latency_word} E2E wall (ms)", 14, fill=TEXT,
                           class_="axis-label", font_weight="800", **{"text-anchor": "middle"}))

    table_y = top + bar_height + 102
    lines.append(_svg_text(30, table_y - 17,
                           "Segment accounting (duration and share of E2E wall)", 13,
                           fill=TEXT, class_="subtitle", font_weight="800"))
    lines.append(_svg_text(30, table_y + 5, "Segment", 11, fill=TEXT, font_weight="800"))
    lines.append(_svg_text(425, table_y + 5, f"{latency_word} duration (ms)", 11, fill=TEXT,
                           font_weight="800", **{"text-anchor": "end"}))
    lines.append(_svg_text(605, table_y + 5, "Share of E2E", 11, fill=TEXT, font_weight="800",
                           **{"text-anchor": "end"}))
    lines.append(_svg_text(760, table_y + 5, "Source / interpretation", 11, fill=TEXT,
                           font_weight="800"))
    lines.append(f'<line class="frame" x1="30" y1="{table_y + 12}" x2="1070" y2="{table_y + 12}"/>')
    for index, ((key, color, label), value) in enumerate(zip(segments, segment_values), 1):
        row_y = table_y + 34 + (index - 1) * 24
        lines.append(f'<rect x="30" y="{row_y - 11}" width="13" height="13" fill="{color}"/>')
        lines.append(_svg_text(52, row_y, label, 11, fill=TEXT, font_weight="700"))
        lines.append(_svg_text(425, row_y, fmt_ms(value), 11, fill=TEXT,
                               **{"text-anchor": "end"}))
        lines.append(_svg_text(605, row_y, fmt_pct(value), 11, fill=TEXT,
                               **{"text-anchor": "end"}))
        if latency_kind == "predicted":
            source = (
                "frozen CPU model predictions"
                if key == "tool_wall_ms"
                else "frozen request model predictions"
                if key == "model_wall_ms"
                else "frozen API runner-overhead term"
            )
        else:
            source = (
                "recorded tool events"
                if key == "tool_wall_ms"
                else "recorded model-request proxy"
                if key == "model_wall_ms"
                else "E2E minus recorded phases"
            )
        lines.append(_svg_text(760, row_y, source, 11, fill=TEXT))
    lines.append("</svg>")
    return "\n".join(lines)


def _compact_run_id(run_id: str, *, limit: int = 18) -> str:
    """Keep a sealed run reference legible in a fixed-width SVG subtitle."""

    value = str(run_id)
    if len(value) <= limit:
        return value
    return "…" + value[-(limit - 1):]


def _selection_sidecar_digest(path: Path) -> str | None:
    sidecar = Path(str(path) + ".sha256")
    if not sidecar.is_file():
        return None
    fields = sidecar.read_text(encoding="utf-8").strip().split()
    if len(fields) != 2 or fields[1] != path.name or not re.fullmatch(r"[0-9a-f]{64}", fields[0]):
        raise DataContractError(f"Step 3 selection sidecar is malformed: {sidecar}")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if fields[0] != actual:
        raise DataContractError("Step 3 selection sidecar does not match selection artifact")
    return actual


def _load_step3_selection(
    path: Path,
    trajectories_path: Path,
    trajectories: list[dict[str, Any]],
    *,
    require_sidecar: bool,
    latency_kind: str = "observed",
) -> tuple[dict[str, Any], str, bool]:
    if not path.is_file():
        raise DataContractError(f"Step 3 selection artifact does not exist: {path}")
    try:
        selection = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DataContractError(f"Step 3 selection artifact is unreadable: {path}: {exc}") from exc
    if not isinstance(selection, dict) or selection.get("schema_version") != "assignment.step3-selection.v1":
        raise DataContractError("Step 3 selection artifact has unsupported schema")
    source_sha = hashlib.sha256(trajectories_path.read_bytes()).hexdigest()
    if latency_kind == "observed" and selection.get("source_trajectories_sha256") != source_sha:
        raise DataContractError("Step 3 selection is not bound to the canonical trajectories table")
    sidecar_digest = _selection_sidecar_digest(path)
    if require_sidecar and sidecar_digest is None:
        raise DataContractError("Step 3 selection SHA-256 sidecar is required")
    selected = selection.get("selected")
    required_selected = {
        "run_id", "suite", "repository", "category", "instance_id", "config_id", "repeat_id",
        "tool_wall_ms", "model_wall_ms", "e2e_wall_ms", "tool_model_ratio", "tool_event_count",
        "model_event_count",
    }
    if not isinstance(selected, dict) or not required_selected.issubset(selected) or set(selected).difference({
        "run_id", "suite", "repository", "category", "instance_id", "config_id", "repeat_id",
        "tool_wall_ms", "model_wall_ms", "e2e_wall_ms", "tool_model_ratio", "tool_event_count",
        "model_event_count", "hardware_id", "model_revision", "swe_agent_revision",
        "swe_bench_revision", "command_sha256",
    }):
        raise DataContractError("Step 3 selection selected row has invalid fields")
    run_id = selected.get("run_id")
    matches = [row for row in trajectories if row.get("run_id") == run_id]
    if len(matches) != 1:
        raise DataContractError("Step 3 selection must name exactly one canonical trajectory")
    row = matches[0]
    if row.get("config_id") != "shared-baseline" or row.get("status") != "completed":
        raise DataContractError("Step 3 selection must name a completed shared-baseline trajectory")
    for field in ("suite", "repository", "category", "instance_id", "config_id", "repeat_id"):
        if field in selected and selected[field] != row.get(field):
            raise DataContractError(f"Step 3 selection {field} disagrees with canonical trajectory")
    if latency_kind == "observed":
        for field in ("tool_wall_ms", "model_wall_ms", "e2e_wall_ms", "tool_model_ratio"):
            if field in selected and not math.isclose(float(selected[field]), float(row[field]), rel_tol=1e-9, abs_tol=1e-9):
                raise DataContractError(f"Step 3 selection {field} disagrees with canonical trajectory")
    for field in ("tool_event_count", "model_event_count"):
        if int(selected[field]) != int(row[field]):
            raise DataContractError(f"Step 3 selection {field} disagrees with canonical trajectory")
    return row, hashlib.sha256(path.read_bytes()).hexdigest(), sidecar_digest is not None


def _discover_step3_selection(trajectories_path: Path) -> Path:
    """Find the sealed selection artifact for the internal API caller.

    The CLI takes an explicit path and therefore requires its sidecar.  The
    audit tool historically calls this module without that argument, so its
    compatibility discovery accepts the two established artifact names and
    still requires the artifact to exist and bind to the input table.
    """
    candidates = [
        trajectories_path.with_name("step3_selection.json"),
        trajectories_path.with_name("selection.json"),
    ]
    present = [path for path in candidates if path.is_file()]
    if len(present) != 1:
        raise DataContractError(
            "exactly one sealed Step 3 selection artifact is required beside trajectories.csv"
        )
    return present[0]


def _suite_headline_metrics(rows: list[dict[str, Any]], suite: str) -> dict[str, Any]:
    selected = [row for row in rows if row["suite"] == suite]
    submitted = [row for row in selected if str(row.get("submitted", "")).lower() in {"1", "true", "yes"}]
    completed = [row for row in selected if row.get("status") == "completed"]
    resolved = [row for row in completed if int(row["resolved"]) == 1]
    return {
        "selected": {"count": len(selected), "denominator": len(selected)},
        "submitted": {"count": len(submitted), "denominator": len(selected)},
        "completed": {"count": len(completed), "denominator": len(selected)},
        "resolved": {"count": len(resolved), "denominator": len(completed)},
        "resolved_rate": {
            "numerator": len(resolved),
            "denominator": len(completed),
            "percent": 100.0 * len(resolved) / len(completed) if completed else None,
        },
        "average_completed_e2e_wall_ms": (
            sum(float(row["e2e_wall_ms"]) for row in completed) / len(completed)
            if completed else None
        ),
    }


def _validate_headline_metrics(
    metrics: Any, *, suite: str, source: Path
) -> dict[str, Any]:
    if not isinstance(metrics, dict):
        raise DataContractError(f"D1 headline metrics for {suite} must be an object: {source}")
    missing = sorted(SUITE_HEADLINE_KEYS.difference(metrics))
    if missing:
        raise DataContractError(
            f"D1 headline metrics for {suite} missing keys: {', '.join(missing)}"
        )
    for field in ("selected", "submitted", "completed", "resolved"):
        value = metrics[field]
        if not isinstance(value, dict) or set(value) != {"count", "denominator"}:
            raise DataContractError(
                f"D1 headline metrics {suite}.{field} must contain count and denominator"
            )
        if any(not isinstance(value[name], int) or value[name] < 0 for name in value):
            raise DataContractError(f"D1 headline metrics {suite}.{field} counts must be nonnegative integers")
    rate = metrics["resolved_rate"]
    if not isinstance(rate, dict) or set(rate) != {"numerator", "denominator", "percent"}:
        raise DataContractError(
            f"D1 headline metrics {suite}.resolved_rate must contain numerator, denominator, percent"
        )
    if any(
        not isinstance(rate[name], int) or rate[name] < 0
        for name in ("numerator", "denominator")
    ):
        raise DataContractError(f"D1 headline metrics {suite}.resolved_rate counts must be nonnegative integers")
    percent = rate["percent"]
    if percent is not None and (
        isinstance(percent, bool)
        or not isinstance(percent, (int, float))
        or not math.isfinite(float(percent))
        or not 0 <= float(percent) <= 100
    ):
        raise DataContractError(f"D1 headline metrics {suite}.resolved_rate.percent is invalid")
    average = metrics["average_completed_e2e_wall_ms"]
    if average is not None and (
        isinstance(average, bool)
        or not isinstance(average, (int, float))
        or not math.isfinite(float(average))
        or float(average) < 0
    ):
        raise DataContractError(
            f"D1 headline metrics {suite}.average_completed_e2e_wall_ms is invalid"
        )
    return json.loads(json.dumps(metrics))


def _load_d1_headline_metrics(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise DataContractError(f"D1 headline metrics do not exist: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DataContractError(f"D1 headline metrics are unreadable: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise DataContractError("D1 headline metrics must be a JSON object")
    if not isinstance(payload.get("schema_version"), str) or not payload["schema_version"]:
        raise DataContractError("D1 headline metrics requires a schema_version")
    if not isinstance(payload.get("latency_definition"), str) or not payload["latency_definition"]:
        raise DataContractError("D1 headline metrics requires latency_definition")
    if not any(
        isinstance(payload.get(field), (dict, list, str)) and payload.get(field)
        for field in ("sources", "provenance")
    ):
        raise DataContractError("D1 headline metrics requires non-empty sources or provenance")
    suites = payload.get("suites")
    if not isinstance(suites, dict) or set(suites) != {"lite", "verified"}:
        raise DataContractError("D1 headline metrics suites must contain exactly lite and verified")
    payload["suites"] = {
        suite: _validate_headline_metrics(suites[suite], suite=suite, source=path)
        for suite in ("lite", "verified")
    }
    return payload


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "parameter"


def _report_markdown(summary: dict[str, Any]) -> str:
    latency_kind = summary.get("latency_kind", "observed")
    residual_label = (
        "Unknown E2E residual"
        if latency_kind == "observed"
        else "Predicted runner overhead"
    )
    lines = [
        f"Latency kind: `{latency_kind}`; official resolved outcomes are observed.",
        "Model request wall is proxy wall time and does not measure GPU residency.",
        "# Assignment Steps 1-3 report",
        "",
        f"Primary ratio: `sum({latency_kind} tool wall ms) / sum({latency_kind} model request wall ms)`. ",
        "",
        f"- Trajectories: {summary['counts']['trajectories']}",
        f"- Tool events: {summary['counts']['tool_events']}",
        f"- Model events: {summary['counts']['model_events']}",
        f"- Sweep runs: {summary['counts']['sweep_runs']}",
        "",
        "## Category summary",
        "",
        "| Category | N | Accuracy | Avg E2E ms | Tool/model ratio |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in summary["categories"]:
        lines.append(
            f"| {row['category']} | {row['count']} | {row['accuracy_percent']:.3f}% | "
            f"{row['average_e2e_wall_ms']:.3f} | {row['tool_model_ratio']:.6f} |"
        )
    lines.extend([
        "",
        "## D1 original accepted-case headline metrics",
        "",
        "| Suite | Selected | Submitted | Completed | Resolved | Resolved rate | Avg completed E2E ms |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ])
    for suite in ("lite", "verified"):
        metrics = summary["suite_headline_metrics"][suite]
        lines.append(
            f"| {suite} | {metrics['selected']['count']}/{metrics['selected']['denominator']} | "
            f"{metrics['submitted']['count']}/{metrics['submitted']['denominator']} | "
            f"{metrics['completed']['count']}/{metrics['completed']['denominator']} | "
            f"{metrics['resolved']['count']}/{metrics['resolved']['denominator']} | "
            f"{metrics['resolved_rate']['percent'] if metrics['resolved_rate']['percent'] is not None else 'n/a'}% | "
            f"{metrics['average_completed_e2e_wall_ms'] if metrics['average_completed_e2e_wall_ms'] is not None else 'n/a'} |"
        )
    lines.extend([
        "",
        "## Event-overlay trace metrics",
        "",
        "These metrics are computed from the trajectory table joined to recorded tool/model events and are separate from the D1 headline source.",
        "",
        "| Suite | Selected | Submitted | Completed | Resolved | Resolved rate | Avg completed E2E ms |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ])
    for suite in ("lite", "verified"):
        metrics = summary["trace_overlay_suite_metrics"][suite]
        lines.append(
            f"| {suite} | {metrics['selected']['count']}/{metrics['selected']['denominator']} | "
            f"{metrics['submitted']['count']}/{metrics['submitted']['denominator']} | "
            f"{metrics['completed']['count']}/{metrics['completed']['denominator']} | "
            f"{metrics['resolved']['count']}/{metrics['resolved']['denominator']} | "
            f"{metrics['resolved_rate']['percent'] if metrics['resolved_rate']['percent'] is not None else 'n/a'}% | "
            f"{metrics['average_completed_e2e_wall_ms'] if metrics['average_completed_e2e_wall_ms'] is not None else 'n/a'} |"
        )
    lines.extend([
        "",
        "## Step 3 event accounting",
        "",
        f"- {residual_label}: `{summary['step3_events']['e2e_residual_wall_ms']}` ms.",
        f"- Recorded tool events: `{len(summary['step3_events']['tool_events'])}`.",
        f"- Recorded model requests: `{len(summary['step3_events']['model_events'])}`.",
        "- Model wall values are request-proxy measurements, not GPU residency.",
    ])
    lines.extend(["", "## Generated figures", ""])
    lines.extend(f"- `{path}`" for path in summary["figures"])
    lines.append("")
    return "\n".join(lines)


def generate_figures(
    *,
    trajectories_path: Path,
    tool_events_path: Path,
    model_events_path: Path,
    sweep_runs_path: Path,
    output_dir: Path,
    reconciliation_report_path: Path | None = None,
    step3_selection_path: Path | None = None,
    sweep_metadata_path: Path | None = None,
    d1_headline_metrics_path: Path | None = None,
    latency_kind: str = "observed",
    force: bool = False,
) -> dict[str, Any]:
    if latency_kind not in VALID_LATENCY_KINDS:
        raise DataContractError(
            f"latency_kind must be one of {sorted(VALID_LATENCY_KINDS)}"
        )
    trajectories, sweeps = load_canonical_tables(
        trajectories_path,
        tool_events_path,
        model_events_path,
        sweep_runs_path,
        sweep_metadata_path=sweep_metadata_path,
        latency_kind=latency_kind,
    )
    categories = _aggregate(trajectories, "category")
    repositories = _aggregate(trajectories, "repository")
    sweep_summary = aggregate_sweeps(sweeps)
    selection_path = step3_selection_path or _discover_step3_selection(trajectories_path)
    selected_row, selection_sha256, selection_sidecar_verified = _load_step3_selection(
        selection_path,
        trajectories_path,
        trajectories,
        require_sidecar=step3_selection_path is not None,
        latency_kind=latency_kind,
    )
    selected_run_id = selected_row["run_id"]
    raw_tools = _read_csv(tool_events_path, TOOL_EVENT_COLUMNS, "tool_events")
    raw_models = _read_csv(model_events_path, MODEL_EVENT_COLUMNS, "model_events")
    tool_latency_column = _latency_column(
        raw_tools,
        observed="wall_ms",
        predicted="predicted_wall_ms",
        table="tool_events",
        latency_kind=latency_kind,
    )
    model_latency_column = _latency_column(
        raw_models,
        observed="wall_ms",
        predicted="predicted_wall_ms",
        table="model_events",
        latency_kind=latency_kind,
    )
    selected_tools = [row for row in raw_tools if row["run_id"] == selected_run_id]
    selected_models = [row for row in raw_models if row["run_id"] == selected_run_id]
    if len(selected_tools) != int(selected_row["tool_event_count"]):
        raise DataContractError("Step 3 selected trajectory tool event count is incomplete")
    if len(selected_models) != int(selected_row["model_event_count"]):
        raise DataContractError("Step 3 selected trajectory model event count is incomplete")
    tool_event_inventory = [
        {
            "event_id": row["event_id"],
            "operation_class": row["operation_class"],
            "wall_ms": _number(row, tool_latency_column, "tool_events", index + 2, positive=True),
            "observed_wall_ms": _number(row, "wall_ms", "tool_events", index + 2, positive=True),
            "predicted_wall_ms": (
                _number(row, "predicted_wall_ms", "tool_events", index + 2, positive=True)
                if row.get("predicted_wall_ms")
                else None
            ),
        }
        for index, row in enumerate(selected_tools)
    ]
    model_event_inventory = [
        {
            "request_id": row["request_id"],
            "input_tokens": _integer(row, "input_tokens", "model_events", index + 2),
            "output_tokens": _integer(row, "output_tokens", "model_events", index + 2),
            "context_tokens": _integer(row, "context_tokens", "model_events", index + 2),
            "request_proxy_wall_ms": _number(row, model_latency_column, "model_events", index + 2, positive=True),
            "observed_wall_ms": _number(row, "wall_ms", "model_events", index + 2, positive=True),
            "predicted_wall_ms": (
                _number(row, "predicted_wall_ms", "model_events", index + 2, positive=True)
                if row.get("predicted_wall_ms")
                else None
            ),
        }
        for index, row in enumerate(selected_models)
    ]
    trace_overlay_suite_metrics = {
        suite: _suite_headline_metrics(trajectories, suite)
        for suite in ("lite", "verified")
    }
    d1_payload = _load_d1_headline_metrics(d1_headline_metrics_path) if d1_headline_metrics_path else None
    suite_headline_metrics = (
        d1_payload["suites"] if d1_payload is not None else trace_overlay_suite_metrics
    )

    latency_word = latency_kind.title()
    figures: dict[str, str] = {
        "step1_repository_ratio.svg": _repository_ratio_svg(
            trajectories, latency_kind=latency_kind
        ),
        "step1_accuracy_vs_latency.svg": _scatter_svg(
            title=f"Step 1: observed category accuracy vs {latency_kind} latency",
            subtitle="Each point is a canonical category aggregate; accuracy is observed.",
            x_label=f"Average {latency_word} E2E wall latency (ms)",
            y_label="Observed official resolved rate (%)",
            points=[
                {
                    "x": row["average_e2e_wall_ms"],
                    "y": row["accuracy_percent"],
                    "label": row["category"],
                    "group": row["category"],
                    "category": row["category"],
                }
                for row in categories
            ],
        ),
        "step1_accuracy_vs_ratio.svg": _scatter_svg(
            title=f"Step 1: observed category accuracy vs {latency_kind} ratio",
            subtitle="Accuracy is observed; ratio is sum(tool wall) / sum(model request proxy wall).",
            x_label=f"{latency_word} tool/model wall-latency ratio (log scale)",
            y_label="Observed official resolved rate (%)",
            log_x=True,
            points=[
                {
                    "x": row["tool_model_ratio"],
                    "y": row["accuracy_percent"],
                    "label": row["category"],
                    "group": row["category"],
                    "category": row["category"],
                }
                for row in categories
            ],
        ),
        "step1_sample_latency_vs_ratio.svg": _scatter_svg(
            title=f"Step 1: individual sample {latency_kind} latency vs ratio",
            subtitle=(
                "Individual samples; category colors are preserved; outcomes remain observed."
            ),
            x_label=f"{latency_word} tool/model wall-latency ratio (log scale)",
            y_label=f"{latency_word} E2E wall latency (ms)",
            log_x=True,
            points=[
                {
                    "x": row["tool_model_ratio"],
                    "y": row["e2e_wall_ms"],
                    "label": row["run_id"],
                    "group": row["category"],
                    "category": row["category"],
                    "repository": row["repository"],
                    "suite": row["suite"],
                    "run_id": row["run_id"],
                    "data_sample": True,
                    "title": (
                        f"category={row['category']}; repository={row['repository']}; "
                        f"instance={row.get('instance_id', row['run_id'])}; "
                        f"ratio={float(row['tool_model_ratio']):.6g}; "
                        f"E2E={float(row['e2e_wall_ms']):.3f} ms"
                    ),
                    "annotate": len(trajectories) <= 24,
                }
                for row in trajectories
            ],
        ),
        "step2_combined.svg": _step2_combined_svg(
            sweep_summary, latency_kind=latency_kind
        ),
        "step3_latency_breakdown.svg": _step3_svg(
            [selected_row], latency_kind=latency_kind
        ),
        "step3_tool_events.svg": _scatter_svg(
            title=f"Step 3: selected trajectory {latency_kind} tool events",
            subtitle=(
                f"Sealed selection {_compact_run_id(selected_run_id)}; event IDs remain in point titles."
            ),
            x_label="Serialized tool event ordinal",
            y_label=f"{latency_word} tool wall latency (ms)",
            points=[
                {
                    "x": index + 1,
                    "y": _number(row, tool_latency_column, "tool_events", index + 2, positive=True),
                    "label": row["event_id"],
                    "group": row["operation_class"],
                    "event_id": row["event_id"],
                    "operation_class": row["operation_class"],
                    "wall_ms": _number(row, tool_latency_column, "tool_events", index + 2, positive=True),
                    "observed_wall_ms": _number(row, "wall_ms", "tool_events", index + 2, positive=True),
                    "predicted_wall_ms": (
                        _number(row, "predicted_wall_ms", "tool_events", index + 2, positive=True)
                        if row.get("predicted_wall_ms")
                        else None
                    ),
                    "data_sample": True,
                    "title": (
                        f"event={row['event_id']}; operation={row['operation_class']}; "
                        f"{latency_kind} tool wall={float(row[tool_latency_column]):.3f} ms"
                    ),
                    # Long event IDs are retained in the point title/data
                    # attributes; suppressing every label keeps the plot
                    # readable when a selected run has many events.
                    "annotate": len(selected_tools) <= 8,
                }
                for index, row in enumerate(selected_tools)
            ],
        ),
        "step3_model_tokens_vs_latency.svg": _scatter_svg(
            title=f"Step 3: selected trajectory model-request {latency_kind} telemetry",
            subtitle=(
                f"Sealed selection {_compact_run_id(selected_run_id)}; token descriptors are in point titles. "
                "Request wall is a proxy."
            ),
            x_label="Context tokens at request start",
            y_label=f"{latency_word} model-request proxy wall (ms)",
            points=[
                {
                    "x": _integer(row, "context_tokens", "model_events", index + 2),
                    "y": _number(row, model_latency_column, "model_events", index + 2, positive=True),
                    "label": row["request_id"],
                    "group": "model request",
                    "request_id": row["request_id"],
                    "input_tokens": _integer(row, "input_tokens", "model_events", index + 2),
                    "output_tokens": _integer(row, "output_tokens", "model_events", index + 2),
                    "context_tokens": _integer(row, "context_tokens", "model_events", index + 2),
                    "wall_ms": _number(row, model_latency_column, "model_events", index + 2, positive=True),
                    "request_proxy_wall_ms": _number(row, model_latency_column, "model_events", index + 2, positive=True),
                    "observed_wall_ms": _number(row, "wall_ms", "model_events", index + 2, positive=True),
                    "predicted_wall_ms": (
                        _number(row, "predicted_wall_ms", "model_events", index + 2, positive=True)
                        if row.get("predicted_wall_ms")
                        else None
                    ),
                    "data_sample": True,
                    "title": (
                        f"request={row['request_id']}; input tokens={row['input_tokens']}; "
                        f"output tokens={row['output_tokens']}; context tokens={row['context_tokens']}; "
                        f"{latency_kind} request proxy wall={float(row[model_latency_column]):.3f} ms"
                    ),
                    "annotate": len(selected_models) <= 8,
                }
                for index, row in enumerate(selected_models)
            ],
        ),
    }
    used_slugs: set[str] = set()
    for parameter, settings in sweep_summary.items():
        slug = _slug(parameter)
        if slug in used_slugs:
            raise DataContractError(
                f"sweep_runs: parameter filenames collide after normalization: {parameter!r}"
            )
        used_slugs.add(slug)
        figures[f"step2_{slug}.svg"] = _step2_parameter_svg(
            parameter, settings, latency_kind=latency_kind
        )

    output_names = sorted(figures) + ["assignment_report.json", "assignment_report.md"]
    if not force:
        existing = [name for name in output_names if (output_dir / name).exists()]
        if existing:
            raise FileExistsError(
                "refusing to overwrite generated outputs without --force: " + ", ".join(existing)
            )

    figure_inventory = []
    for name, content in sorted(figures.items()):
        payload = (content + "\n").encode("utf-8")
        figure_inventory.append(
            {
                "path": name,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }
        )

    coverage = _matrix_coverage(
        reconciliation_report_path=reconciliation_report_path,
        trajectories_path=trajectories_path,
        observed_parameters=set(sweep_summary),
    )
    if latency_kind == "predicted":
        coverage["complete_assignment_matrix"] = False
        coverage["matrix_completeness_basis"] = (
            "not claimed: predicted latency inputs do not establish assignment coverage"
        )
        coverage["predicted_latency_full_completeness_claim"] = False
    else:
        coverage["predicted_latency_full_completeness_claim"] = None
    summary = {
        "schema_version": "assignment-step-figures.v1",
        "ratio_definition": (
            "sum(tool_event.wall_ms) / sum(model_event.wall_ms)"
            if latency_kind == "observed"
            else "sum(tool_event.predicted_wall_ms) / sum(model_event.predicted_wall_ms)"
        ),
        "latency_kind": latency_kind,
        "latency_semantics": {
            "latency_values": f"{latency_kind} values from the supplied trajectory/event tables",
            "accuracy_values": "observed official_resolved outcomes",
            "model_wall_ms": "request proxy wall time; not GPU residency",
            "selected_fields": {
                "trajectory_e2e": "e2e_wall_ms" if latency_kind == "observed" else "predicted_e2e_wall_ms",
                "tool_wall": "wall_ms" if latency_kind == "observed" else "predicted_wall_ms",
                "model_wall": "wall_ms" if latency_kind == "observed" else "predicted_wall_ms",
            },
        },
        "sources": {
            "trajectories": str(trajectories_path),
            "tool_events": str(tool_events_path),
            "model_events": str(model_events_path),
            "sweep_runs": str(sweep_runs_path),
            "sweep_metadata": str(sweep_metadata_path) if sweep_metadata_path else None,
            "step3_selection": str(selection_path),
            "d1_headline_metrics": str(d1_headline_metrics_path) if d1_headline_metrics_path else None,
        },
        "source_sha256": {
            "trajectories": _sha256(trajectories_path),
            "tool_events": _sha256(tool_events_path),
            "model_events": _sha256(model_events_path),
            "sweep_runs": _sha256(sweep_runs_path),
            "sweep_metadata": _sha256(sweep_metadata_path) if sweep_metadata_path else None,
            "step3_selection": _sha256(selection_path),
            "d1_headline_metrics": _sha256(d1_headline_metrics_path) if d1_headline_metrics_path else None,
        },
        "counts": {
            "trajectories": len(trajectories),
            "tool_events": sum(row["tool_event_count"] for row in trajectories),
            "model_events": sum(row["model_event_count"] for row in trajectories),
            "sweep_runs": len(sweeps),
        },
        "categories": categories,
        "repositories": repositories,
        "suite_headline_metrics": suite_headline_metrics,
        "trace_overlay_suite_metrics": trace_overlay_suite_metrics,
        "d1_headline_metrics": (
            {
                **d1_payload,
                "path": str(d1_headline_metrics_path),
                "sha256": _sha256(d1_headline_metrics_path),
            }
            if d1_payload is not None
            else None
        ),
        "sweeps": sweep_summary,
        "step3_events": {
            "selected_run_id": selected_run_id,
            "latency_kind": latency_kind,
            "latency_fields": {
                "tool_wall": tool_latency_column,
                "model_wall": model_latency_column,
            },
            "tool_events": tool_event_inventory,
            "model_events": model_event_inventory,
            "model_wall_semantics": "request proxy wall time; not GPU residency",
            "e2e_residual_wall_ms": selected_row["e2e_residual_wall_ms"],
            "e2e_residual_label": (
                "unknown E2E residual (unattributed)"
                if latency_kind == "observed"
                else "predicted runner overhead"
            ),
        },
        "coverage": {
            **coverage,
            "step_3_selected_run_id": selected_run_id,
            "step_3_selection_artifact": str(selection_path),
            "step_3_selection_sha256": selection_sha256,
            "step_3_selection_sidecar_verified": selection_sidecar_verified,
            "step_3_selection_source_trajectories_sha256": _sha256(trajectories_path),
            "step_3_selection_population": "sealed selected trajectory; no independent top-row selection",
        },
        "figures": sorted(figures),
        "figure_inventory": figure_inventory,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, content in sorted(figures.items()):
        (output_dir / name).write_text(content + "\n", encoding="utf-8")
    (output_dir / "assignment_report.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output_dir / "assignment_report.md").write_text(
        _report_markdown(summary), encoding="utf-8"
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trajectories", type=Path, required=True)
    parser.add_argument("--tool-events", type=Path, required=True)
    parser.add_argument("--model-events", type=Path, required=True)
    parser.add_argument("--sweep-runs", type=Path, required=True)
    parser.add_argument(
        "--reconciliation-report",
        type=Path,
        help="verified output from reconcile_plan.py used to prove full matrix coverage",
    )
    parser.add_argument(
        "--step3-selection",
        type=Path,
        help="required sealed Step 3 selection JSON; its .sha256 sidecar is mandatory",
    )
    parser.add_argument(
        "--sweep-metadata",
        type=Path,
        help="portable JSONL identity/category metadata for reduced sweep rows",
    )
    parser.add_argument(
        "--d1-headline-metrics",
        type=Path,
        help="explicit original accepted-case D1 headline metrics JSON",
    )
    parser.add_argument(
        "--latency-kind",
        choices=sorted(VALID_LATENCY_KINDS),
        default="observed",
        help="whether supplied latency values are observed or predicted",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    generate_figures(
        trajectories_path=args.trajectories,
        tool_events_path=args.tool_events,
        model_events_path=args.model_events,
        sweep_runs_path=args.sweep_runs,
        output_dir=args.output_dir,
        reconciliation_report_path=args.reconciliation_report,
        step3_selection_path=args.step3_selection,
        sweep_metadata_path=args.sweep_metadata,
        d1_headline_metrics_path=args.d1_headline_metrics,
        latency_kind=args.latency_kind,
        force=args.force,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (DataContractError, FileExistsError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
