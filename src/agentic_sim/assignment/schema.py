"""Strict canonical schemas for coding-test Steps 1--3.

The assignment-facing CPU:GPU ratio is a phase-latency ratio:

    sum(tool-call wall time) / sum(model-request wall time)

CUDA activity, CPU activity, kernel sums, utilization, and profiler samples are
secondary diagnostics and never replace either side of that ratio.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any


class AssignmentContractError(ValueError):
    """A row is incomplete, inconsistent, or unsafe for assignment claims."""


TRAJECTORY_FIELDS = (
    "schema_version",
    "run_id",
    "suite",
    "repository",
    "category",
    "instance_id",
    "config_id",
    "repeat_id",
    "sweep_parameter",
    "sweep_value",
    "status",
    "submitted",
    "official_resolved",
    "e2e_wall_ms",
    "tool_wall_ms",
    "model_wall_ms",
    "tool_model_ratio",
    "tool_event_count",
    "model_event_count",
    "hardware_id",
    "model_revision",
    "swe_agent_revision",
    "swe_bench_revision",
    "command_sha256",
    "tool_events_path",
    "model_events_path",
    "unavailable_reason",
    "provenance",
)

TOOL_EVENT_FIELDS = (
    "schema_version",
    "run_id",
    "suite",
    "repository",
    "instance_id",
    "config_id",
    "repeat_id",
    "event_id",
    "ordinal",
    "tool_name",
    "operation_class",
    "status",
    "start_mono_ns",
    "end_mono_ns",
    "wall_ms",
    "command_bytes",
    "cpu_ms",
    "bytes_read",
    "bytes_written",
    "command_sha256",
    "timing_scope",
    "provenance",
)

MODEL_EVENT_FIELDS = (
    "schema_version",
    "run_id",
    "suite",
    "repository",
    "instance_id",
    "config_id",
    "repeat_id",
    "request_id",
    "ordinal",
    "status",
    "start_mono_ns",
    "end_mono_ns",
    "wall_ms",
    "input_tokens",
    "max_output_tokens",
    "output_tokens",
    "context_tokens",
    "request_bytes",
    "response_bytes",
    "cpu_activity_union_ms",
    "cuda_activity_union_ms",
    "kernel_duration_sum_ms",
    "timing_scope",
    "provenance",
)

_SUITES = {"lite", "verified"}
_RUN_STATUSES = {"completed", "failed", "timeout", "unavailable"}
_EVENT_STATUSES = {"completed", "failed", "timeout", "unavailable"}
_PROVENANCE = {"measured", "derived_from_measured", "unavailable"}
_OPERATION_CLASSES = {
    "read",
    "write",
    "traversal",
    "search",
    "shell",
    "patch",
    "test",
    "other",
}


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _required(row: Mapping[str, Any], fields: tuple[str, ...], *, kind: str) -> None:
    missing = [field for field in fields if field not in row]
    if missing:
        raise AssignmentContractError(f"{kind} row is missing fields: {', '.join(missing)}")


def _text(row: Mapping[str, Any], field: str, *, allow_empty: bool = False) -> str:
    value = row.get(field)
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise AssignmentContractError(f"{field} must be a non-empty string")
    return value


def _number(
    row: Mapping[str, Any], field: str, *, optional: bool = False, minimum: float = 0.0
) -> float | None:
    value = row.get(field)
    if value in (None, "") and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AssignmentContractError(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise AssignmentContractError(f"{field} must be finite and >= {minimum}")
    return result


def _integer(row: Mapping[str, Any], field: str, *, optional: bool = False) -> int | None:
    value = row.get(field)
    if value in (None, "") and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AssignmentContractError(f"{field} must be a non-negative integer")
    return value


def _sha(row: Mapping[str, Any], field: str) -> str:
    value = _text(row, field)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value.lower()):
        raise AssignmentContractError(f"{field} must be a SHA-256 digest")
    return value.lower()


def validate_tool_event(row: Mapping[str, Any]) -> dict[str, Any]:
    _required(row, TOOL_EVENT_FIELDS, kind="tool event")
    if row["schema_version"] != "assignment.tool-event.v1":
        raise AssignmentContractError("unsupported tool-event schema")
    if row["suite"] not in _SUITES:
        raise AssignmentContractError("tool-event suite must be lite or verified")
    if row["status"] not in _EVENT_STATUSES:
        raise AssignmentContractError("unsupported tool-event status")
    if row["operation_class"] not in _OPERATION_CLASSES:
        raise AssignmentContractError("unsupported operation_class")
    if row["provenance"] not in _PROVENANCE:
        raise AssignmentContractError("unsupported tool-event provenance")
    for field in ("run_id", "repository", "instance_id", "config_id", "repeat_id", "event_id", "tool_name", "timing_scope"):
        _text(row, field)
    _integer(row, "ordinal")
    start = _integer(row, "start_mono_ns", optional=True)
    end = _integer(row, "end_mono_ns", optional=True)
    wall = _number(row, "wall_ms", optional=row["status"] != "completed")
    _integer(row, "command_bytes")
    for field in ("cpu_ms", "bytes_read", "bytes_written"):
        _number(row, field, optional=True)
    _sha(row, "command_sha256")
    if (start is None) != (end is None):
        raise AssignmentContractError("tool-event timestamps must be both present or both absent")
    if start is not None and end is not None:
        if end < start:
            raise AssignmentContractError("tool-event end precedes start")
        measured = (end - start) / 1_000_000.0
        if wall is not None and not math.isclose(measured, wall, rel_tol=1e-6, abs_tol=1e-6):
            raise AssignmentContractError("tool-event wall_ms disagrees with monotonic timestamps")
    return dict(row)


def validate_model_event(row: Mapping[str, Any]) -> dict[str, Any]:
    _required(row, MODEL_EVENT_FIELDS, kind="model event")
    if row["schema_version"] != "assignment.model-event.v1":
        raise AssignmentContractError("unsupported model-event schema")
    if row["suite"] not in _SUITES:
        raise AssignmentContractError("model-event suite must be lite or verified")
    if row["status"] not in _EVENT_STATUSES:
        raise AssignmentContractError("unsupported model-event status")
    if row["provenance"] not in _PROVENANCE:
        raise AssignmentContractError("unsupported model-event provenance")
    for field in ("run_id", "repository", "instance_id", "config_id", "repeat_id", "request_id", "timing_scope"):
        _text(row, field)
    _integer(row, "ordinal")
    start = _integer(row, "start_mono_ns", optional=True)
    end = _integer(row, "end_mono_ns", optional=True)
    wall = _number(row, "wall_ms", optional=row["status"] != "completed")
    for field in (
        "input_tokens", "max_output_tokens", "output_tokens", "context_tokens",
        "request_bytes", "response_bytes",
    ):
        _integer(row, field, optional=True)
    for field in ("cpu_activity_union_ms", "cuda_activity_union_ms", "kernel_duration_sum_ms"):
        _number(row, field, optional=True)
    if (start is None) != (end is None):
        raise AssignmentContractError("model-event timestamps must be both present or both absent")
    if start is not None and end is not None:
        if end < start:
            raise AssignmentContractError("model-event end precedes start")
        measured = (end - start) / 1_000_000.0
        if wall is not None and not math.isclose(measured, wall, rel_tol=1e-6, abs_tol=1e-6):
            raise AssignmentContractError("model-event wall_ms disagrees with monotonic timestamps")
    return dict(row)


def validate_trajectory(row: Mapping[str, Any]) -> dict[str, Any]:
    _required(row, TRAJECTORY_FIELDS, kind="trajectory")
    if row["schema_version"] != "assignment.trajectory.v1":
        raise AssignmentContractError("unsupported trajectory schema")
    if row["suite"] not in _SUITES:
        raise AssignmentContractError("trajectory suite must be lite or verified")
    if row["status"] not in _RUN_STATUSES:
        raise AssignmentContractError("unsupported trajectory status")
    if row["provenance"] not in _PROVENANCE:
        raise AssignmentContractError("unsupported trajectory provenance")
    for field in (
        "run_id", "repository", "category", "instance_id", "config_id", "repeat_id",
        "hardware_id", "model_revision", "swe_agent_revision", "swe_bench_revision",
        "tool_events_path", "model_events_path",
    ):
        _text(row, field)
    _sha(row, "command_sha256")
    for field in ("submitted", "official_resolved"):
        value = row[field]
        if value is not None and not isinstance(value, bool):
            raise AssignmentContractError(f"{field} must be true, false, or null")
    tool_count = _integer(row, "tool_event_count")
    model_count = _integer(row, "model_event_count")
    completed = row["status"] == "completed"
    e2e = _number(row, "e2e_wall_ms", optional=not completed)
    tool = _number(row, "tool_wall_ms", optional=not completed)
    model = _number(row, "model_wall_ms", optional=not completed)
    ratio = _number(row, "tool_model_ratio", optional=not completed)
    if completed:
        if e2e is None or e2e <= 0:
            raise AssignmentContractError("completed trajectory requires positive e2e_wall_ms")
        if tool_count == 0 or tool is None:
            raise AssignmentContractError("completed trajectory requires measured tool events")
        if model_count == 0 or model is None or model <= 0:
            raise AssignmentContractError("completed trajectory requires measured model events")
        expected = tool / model
        if ratio is None or not math.isclose(ratio, expected, rel_tol=1e-9, abs_tol=1e-12):
            raise AssignmentContractError("tool_model_ratio must equal tool_wall_ms/model_wall_ms")
        if row["submitted"] is None or row["official_resolved"] is None:
            raise AssignmentContractError("completed trajectory requires official evaluator outcomes")
        if row["unavailable_reason"] not in (None, ""):
            raise AssignmentContractError("completed trajectory cannot have unavailable_reason")
        if tool + model > e2e * 1.05:
            raise AssignmentContractError(
                "completed trajectory phase wall time exceeds end-to-end wall time"
            )
    else:
        _text(row, "unavailable_reason")
    return dict(row)
