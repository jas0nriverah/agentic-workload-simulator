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


def load_canonical_tables(
    trajectories_path: Path,
    tool_events_path: Path,
    model_events_path: Path,
    sweep_runs_path: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Load, join, and validate all four canonical input tables."""

    trajectory_rows = _read_csv(trajectories_path, TRAJECTORY_COLUMNS, "trajectories")
    tool_rows = _read_csv(tool_events_path, TOOL_EVENT_COLUMNS, "tool_events")
    model_rows = _read_csv(model_events_path, MODEL_EVENT_COLUMNS, "model_events")
    sweep_rows = _read_csv(sweep_runs_path, SWEEP_COLUMNS, "sweep_runs")

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
                row, "e2e_wall_ms", "trajectories", index, positive=True
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
        wall_ms = _number(row, "wall_ms", "tool_events", index, positive=True)
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
        wall_ms = _number(row, "wall_ms", "model_events", index, positive=True)
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
        row["overhead_wall_ms"] = max(
            0.0, row["e2e_wall_ms"] - row["tool_wall_ms"] - row["model_wall_ms"]
        )
        joined.append(row)

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
        e2e_ms = _number(row, "e2e_wall_ms", "sweep_runs", index, positive=True)
        tool_ms = _number(row, "tool_wall_ms", "sweep_runs", index, nonnegative=True)
        model_ms = _number(row, "model_wall_ms", "sweep_runs", index, positive=True)
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
            }
        )
    for parameter, values in sorted(values_by_parameter.items()):
        if len(values) < 2:
            raise DataContractError(
                f"sweep_runs: parameter {parameter!r} must contain at least two values"
            )
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
        result[parameter].append(
            {
                "parameter": parameter,
                "value": value,
                "count": len(members),
                "accuracy_percent": 100.0
                * sum(int(row["resolved"]) for row in members)
                / len(members),
                "average_e2e_wall_ms": sum(float(row["e2e_wall_ms"]) for row in members)
                / len(members),
                "tool_model_ratio": tool_ms / model_ms,
            }
        )
    return {
        parameter: sorted(settings, key=lambda item: _value_sort(item["value"]))
        for parameter, settings in sorted(result.items())
    }


def _svg_text(x: float, y: float, text: str, size: int = 12, **attrs: str) -> str:
    extra = " ".join(f'{key}="{escape(str(value))}"' for key, value in attrs.items())
    return f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" {extra}>{escape(text)}</text>'


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
    x_low, x_high = _bounds([float(point["x"]) for point in points], log_scale=log_x)
    y_low, y_high = _bounds([float(point["y"]) for point in points])
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
        _svg_text(left, 34, title, 22, fill=TEXT, **{"font-weight": "700"}),
        _svg_text(left, 58, subtitle, 12, fill=TEXT),
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
                f'<line x1="{xx:.1f}" y1="{top}" x2="{xx:.1f}" '
                f'y2="{top + plot_h}" stroke="{GRID}"/>',
                f'<line x1="{left}" y1="{yy:.1f}" x2="{left + plot_w}" '
                f'y2="{yy:.1f}" stroke="{GRID}"/>',
                _svg_text(xx, top + plot_h + 24, f"{x_tick:.3g}", 11, fill=TEXT,
                          **{"text-anchor": "middle"}),
                _svg_text(left - 12, yy + 4, f"{y_value:.3g}", 11, fill=TEXT,
                          **{"text-anchor": "end"}),
            ]
        )
    lines.extend(
        [
            _svg_text(left + plot_w / 2, height - 32, x_label, 13, fill=TEXT,
                      **{"text-anchor": "middle"}),
            _svg_text(24, top + plot_h / 2, y_label, 13, fill=TEXT,
                      transform=f"rotate(-90 24 {top + plot_h / 2:.1f})",
                      **{"text-anchor": "middle"}),
        ]
    )
    for point in sorted(points, key=lambda item: (str(item.get("group", "")), str(item["label"]))):
        group = str(point.get("group", "all"))
        xx, yy = x_pos(float(point["x"])), y_pos(float(point["y"]))
        lines.append(
            f'<circle cx="{xx:.1f}" cy="{yy:.1f}" r="6" fill="{colors[group]}">'
            f'<title>{escape(str(point["label"]))}</title></circle>'
        )
        if point.get("annotate", True):
            lines.append(_svg_text(xx + 8, yy - 8, str(point["label"]), 10, fill=colors[group]))
    for index, group in enumerate(groups):
        yy = top + index * 22
        lines.append(f'<circle cx="{width - right + 25}" cy="{yy}" r="6" fill="{colors[group]}"/>')
        lines.append(_svg_text(width - right + 38, yy + 4, group, 11, fill=TEXT))
    lines.append("</svg>")
    return "\n".join(lines)


def _repository_ratio_svg(rows: list[dict[str, Any]]) -> str:
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
        _svg_text(30, 34, "Step 1: tool/model latency ratio by repository", 22,
                  fill=TEXT, **{"font-weight": "700"}),
        _svg_text(30, 57, "One measured sample dot per trajectory; category = repository.", 12, fill=TEXT),
    ]
    for tick in range(6):
        fraction = tick / 5
        x = left + fraction * plot_w
        value = 10 ** (low + fraction * (high - low))
        lines.append(f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top + plot_h}" stroke="{GRID}"/>')
        lines.append(_svg_text(x, top + plot_h + 25, f"{value:.3g}", 11, fill=TEXT,
                               **{"text-anchor": "middle"}))
    step = plot_h / max(1, len(groups))
    for index, group in enumerate(groups):
        center = top + (index + 0.5) * step
        lines.append(f'<line x1="{left}" y1="{center:.1f}" x2="{left + plot_w}" y2="{center:.1f}" stroke="{GRID}"/>')
        lines.append(_svg_text(left - 12, center + 4, group, 11, fill=TEXT,
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
                f'cx="{x:.1f}" cy="{y:.1f}" r="6" fill="{color}">'
                f'<title>{escape(str(row["run_id"]))}: {float(row["tool_model_ratio"]):.6g}</title></circle>'
            )
    legend_x = width - right - 118
    for legend_index, suite in enumerate(("lite", "verified")):
        y = top + 18 + legend_index * 22
        lines.append(f'<circle cx="{legend_x}" cy="{y}" r="6" fill="{colors[suite]}"/>')
        lines.append(_svg_text(legend_x + 13, y + 4, suite.title(), 11, fill=TEXT))
    lines.append(_svg_text(left + plot_w / 2, height - 18,
                           "Tool/model wall-latency ratio (log scale)", 13, fill=TEXT,
                           **{"text-anchor": "middle"}))
    lines.append("</svg>")
    return "\n".join(lines)


def _step2_parameter_svg(parameter: str, settings: list[dict[str, Any]]) -> str:
    width, height = 1440, 500
    panels = [
        ("average_e2e_wall_ms", "accuracy_percent", "Average E2E wall (ms)", "Accuracy (%)", False),
        ("tool_model_ratio", "accuracy_percent", "Tool/model wall ratio", "Accuracy (%)", True),
        (
            "tool_model_ratio",
            "average_e2e_wall_ms",
            "Tool/model wall ratio",
            "Average E2E wall (ms)",
            True,
        ),
    ]
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        _svg_text(30, 30, f"Step 2 sweep: {parameter}", 21, fill=TEXT, **{"font-weight": "700"}),
    ]
    for panel_index, (x_key, y_key, x_label, y_label, log_x) in enumerate(panels):
        panel_x = 30 + panel_index * 470
        panel_y = 65
        panel_w, panel_h = 430, 380
        x_values = [float(item[x_key]) for item in settings]
        y_values = [float(item[y_key]) for item in settings]
        x_low, x_high = _bounds(x_values, log_scale=log_x)
        y_low, y_high = _bounds(y_values)

        def x_pos(value: float) -> float:
            transformed = math.log10(value) if log_x else value
            return panel_x + 55 + (transformed - x_low) / (x_high - x_low) * (panel_w - 80)

        def y_pos(value: float) -> float:
            return panel_y + panel_h - 45 - (value - y_low) / (y_high - y_low) * (panel_h - 80)

        lines.append(
            f'<rect x="{panel_x}" y="{panel_y}" width="{panel_w}" height="{panel_h}" '
            f'fill="none" stroke="{GRID}"/>'
        )
        lines.append(_svg_text(panel_x + panel_w / 2, panel_y + panel_h - 12, x_label, 11,
                               fill=TEXT, **{"text-anchor": "middle"}))
        lines.append(_svg_text(panel_x + 12, panel_y + 18, y_label, 11, fill=TEXT))
        ordered = sorted(settings, key=lambda item: _value_sort(item["value"]))
        coordinates = [(x_pos(float(item[x_key])), y_pos(float(item[y_key]))) for item in ordered]
        if len(coordinates) > 1:
            points = " ".join(f"{x:.1f},{y:.1f}" for x, y in coordinates)
            lines.append(
                f'<polyline points="{points}" fill="none" stroke="{PALETTE[0]}" '
                'stroke-width="2"/>'
            )
        for item, (xx, yy) in zip(ordered, coordinates):
            lines.append(f'<circle cx="{xx:.1f}" cy="{yy:.1f}" r="6" fill="{PALETTE[0]}"/>')
            lines.append(_svg_text(xx + 7, yy - 7, item["value"], 10, fill=TEXT))
    lines.append("</svg>")
    return "\n".join(lines)


def _step2_combined_svg(sweeps: dict[str, list[dict[str, Any]]]) -> str:
    """Render all four hyperparameters in the assignment's three required views."""
    if not sweeps:
        raise DataContractError("combined Step 2 plot has no sweep rows")
    width, height = 1440, 540
    panels = [
        ("average_e2e_wall_ms", "accuracy_percent", "Average E2E wall (ms)", "Accuracy (%)", False),
        ("tool_model_ratio", "accuracy_percent", "Tool/model wall ratio", "Accuracy (%)", True),
        (
            "tool_model_ratio",
            "average_e2e_wall_ms",
            "Tool/model wall ratio",
            "Average E2E wall (ms)",
            True,
        ),
    ]
    parameters = sorted(sweeps)
    colors = {parameter: PALETTE[index % len(PALETTE)] for index, parameter in enumerate(parameters)}
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        _svg_text(30, 30, "Step 2: combined hyperparameter sensitivity", 21,
                  fill=TEXT, **{"font-weight": "700"}),
    ]
    for panel_index, (x_key, y_key, x_label, y_label, log_x) in enumerate(panels):
        panel_x = 30 + panel_index * 470
        panel_y = 65
        panel_w, panel_h = 430, 375
        settings = [item for parameter in parameters for item in sweeps[parameter]]
        x_low, x_high = _bounds([float(item[x_key]) for item in settings], log_scale=log_x)
        y_low, y_high = _bounds([float(item[y_key]) for item in settings])

        def x_pos(value: float) -> float:
            transformed = math.log10(value) if log_x else value
            return panel_x + 55 + (transformed - x_low) / (x_high - x_low) * (panel_w - 80)

        def y_pos(value: float) -> float:
            return panel_y + panel_h - 45 - (value - y_low) / (y_high - y_low) * (panel_h - 80)

        lines.append(
            f'<rect x="{panel_x}" y="{panel_y}" width="{panel_w}" height="{panel_h}" '
            f'fill="none" stroke="{GRID}"/>'
        )
        lines.append(_svg_text(panel_x + panel_w / 2, panel_y + panel_h - 12, x_label, 11,
                               fill=TEXT, **{"text-anchor": "middle"}))
        lines.append(_svg_text(panel_x + 12, panel_y + 18, y_label, 11, fill=TEXT))
        for parameter in parameters:
            ordered = sorted(sweeps[parameter], key=lambda item: _value_sort(item["value"]))
            coordinates = [(x_pos(float(item[x_key])), y_pos(float(item[y_key]))) for item in ordered]
            if len(coordinates) > 1:
                points = " ".join(f"{x:.1f},{y:.1f}" for x, y in coordinates)
                lines.append(
                    f'<polyline points="{points}" fill="none" stroke="{colors[parameter]}" '
                    'stroke-width="2"/>'
                )
            for item, (xx, yy) in zip(ordered, coordinates):
                lines.append(
                    f'<circle cx="{xx:.1f}" cy="{yy:.1f}" r="5" fill="{colors[parameter]}">'
                    f'<title>{escape(parameter)}={escape(str(item["value"]))}</title></circle>'
                )
    legend_y = height - 40
    legend_x = 40
    for parameter in parameters:
        lines.append(f'<circle cx="{legend_x}" cy="{legend_y}" r="6" fill="{colors[parameter]}"/>')
        lines.append(_svg_text(legend_x + 12, legend_y + 4, parameter, 11, fill=TEXT))
        legend_x += 120 + 8 * len(parameter)
    lines.append("</svg>")
    return "\n".join(lines)


def _step3_svg(rows: list[dict[str, Any]], limit: int = 12) -> str:
    if len(rows) != 1:
        raise DataContractError("Step 3 breakdown requires exactly one sealed selected trajectory")
    selected = rows
    width = 1100
    height = max(460, 160 + len(selected) * 44)
    left, right, top = 260, 60, 80
    plot_w = width - left - right
    max_total = max(row["e2e_wall_ms"] for row in selected)
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        _svg_text(30, 34, "Step 3: high tool/model-ratio latency breakdown", 22, fill=TEXT,
                  **{"font-weight": "700"}),
        _svg_text(
            30,
            58,
            "Exactly the trajectory named by the sealed Step 3 selection artifact",
            12,
            fill=TEXT,
        ),
    ]
    for index, row in enumerate(selected):
        y = top + index * 44
        lines.append(_svg_text(left - 12, y + 17, row["run_id"], 10, fill=TEXT,
                               **{"text-anchor": "end"}))
        x = left
        for key, color, label in (
            ("tool_wall_ms", TOOL_COLOR, "tool"),
            ("model_wall_ms", MODEL_COLOR, "model"),
            ("overhead_wall_ms", OVERHEAD_COLOR, "overhead"),
        ):
            bar_width = float(row[key]) / max_total * plot_w
            lines.append(
                f'<rect x="{x:.1f}" y="{y}" width="{bar_width:.1f}" height="24" '
                f'fill="{color}"><title>{label}: {row[key]:.3f} ms</title></rect>'
            )
            x += bar_width
        lines.append(_svg_text(x + 7, y + 17, f"R={row['tool_model_ratio']:.3g}", 10, fill=TEXT))
    legend_y = height - 30
    legend_x = left
    for color, label in (
        (TOOL_COLOR, "tool wall"),
        (MODEL_COLOR, "model wall"),
        (OVERHEAD_COLOR, "residual overhead"),
    ):
        lines.append(
            f'<rect x="{legend_x}" y="{legend_y - 12}" width="18" height="12" '
            f'fill="{color}"/>'
        )
        lines.append(_svg_text(legend_x + 24, legend_y, label, 11, fill=TEXT))
        legend_x += 170
    lines.append("</svg>")
    return "\n".join(lines)


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
    path: Path, trajectories_path: Path, trajectories: list[dict[str, Any]], *, require_sidecar: bool
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
    if selection.get("source_trajectories_sha256") != source_sha:
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


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "parameter"


def _report_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# Assignment Steps 1-3 report",
        "",
        "Primary ratio: `sum(tool wall ms) / sum(model request wall ms)`.",
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
        "## Lite/Verified headline metrics",
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
    force: bool = False,
) -> dict[str, Any]:
    trajectories, sweeps = load_canonical_tables(
        trajectories_path, tool_events_path, model_events_path, sweep_runs_path
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
    )
    selected_run_id = selected_row["run_id"]
    raw_tools = _read_csv(tool_events_path, TOOL_EVENT_COLUMNS, "tool_events")
    raw_models = _read_csv(model_events_path, MODEL_EVENT_COLUMNS, "model_events")
    selected_tools = [row for row in raw_tools if row["run_id"] == selected_run_id]
    selected_models = [row for row in raw_models if row["run_id"] == selected_run_id]

    figures: dict[str, str] = {
        "step1_repository_ratio.svg": _repository_ratio_svg(trajectories),
        "step1_accuracy_vs_latency.svg": _scatter_svg(
            title="Step 1: category accuracy vs average latency",
            subtitle="Each point is a canonical category aggregate.",
            x_label="Average E2E wall latency (ms)",
            y_label="Official resolved rate (%)",
            points=[
                {
                    "x": row["average_e2e_wall_ms"],
                    "y": row["accuracy_percent"],
                    "label": row["category"],
                    "group": row["category"],
                }
                for row in categories
            ],
        ),
        "step1_accuracy_vs_ratio.svg": _scatter_svg(
            title="Step 1: category accuracy vs tool/model ratio",
            subtitle="Ratio is sum(tool wall) / sum(model request wall).",
            x_label="Tool/model wall-latency ratio (log scale)",
            y_label="Official resolved rate (%)",
            log_x=True,
            points=[
                {
                    "x": row["tool_model_ratio"],
                    "y": row["accuracy_percent"],
                    "label": row["category"],
                    "group": row["category"],
                }
                for row in categories
            ],
        ),
        "step1_sample_latency_vs_ratio.svg": _scatter_svg(
            title="Step 1: sample latency vs tool/model ratio",
            subtitle="Samples are grouped by canonical category.",
            x_label="Tool/model wall-latency ratio (log scale)",
            y_label="E2E wall latency (ms)",
            log_x=True,
            points=[
                {
                    "x": row["tool_model_ratio"],
                    "y": row["e2e_wall_ms"],
                    "label": row["run_id"],
                    "group": row["category"],
                    "annotate": len(trajectories) <= 24,
                }
                for row in trajectories
            ],
        ),
        "step2_combined.svg": _step2_combined_svg(sweep_summary),
        "step3_latency_breakdown.svg": _step3_svg([selected_row]),
        "step3_tool_events.svg": _scatter_svg(
            title="Step 3: selected trajectory tool-event latency",
            subtitle=f"Sealed Step 3 selection: {selected_run_id}",
            x_label="Serialized tool event ordinal",
            y_label="Tool wall latency (ms)",
            points=[
                {
                    "x": index + 1,
                    "y": _number(row, "wall_ms", "tool_events", index + 2, positive=True),
                    "label": row["event_id"],
                    "group": row["operation_class"],
                    "annotate": len(selected_tools) <= 24,
                }
                for index, row in enumerate(selected_tools)
            ],
        ),
        "step3_model_tokens_vs_latency.svg": _scatter_svg(
            title="Step 3: selected trajectory model-event latency",
            subtitle=f"Sealed Step 3 selection: {selected_run_id}",
            x_label="Context tokens at request start",
            y_label="Model-request wall latency (ms)",
            points=[
                {
                    "x": _integer(row, "context_tokens", "model_events", index + 2),
                    "y": _number(row, "wall_ms", "model_events", index + 2, positive=True),
                    "label": row["request_id"],
                    "group": "model request",
                    "annotate": len(selected_models) <= 24,
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
        figures[f"step2_{slug}.svg"] = _step2_parameter_svg(parameter, settings)

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

    summary = {
        "schema_version": "assignment-step-figures.v1",
        "ratio_definition": "sum(tool_event.wall_ms) / sum(model_event.wall_ms)",
        "sources": {
            "trajectories": str(trajectories_path),
            "tool_events": str(tool_events_path),
            "model_events": str(model_events_path),
            "sweep_runs": str(sweep_runs_path),
            "step3_selection": str(selection_path),
        },
        "counts": {
            "trajectories": len(trajectories),
            "tool_events": sum(row["tool_event_count"] for row in trajectories),
            "model_events": sum(row["model_event_count"] for row in trajectories),
            "sweep_runs": len(sweeps),
        },
        "categories": categories,
        "repositories": repositories,
        "suite_headline_metrics": {
            suite: _suite_headline_metrics(trajectories, suite)
            for suite in ("lite", "verified")
        },
        "sweeps": sweep_summary,
        "coverage": {
            **_matrix_coverage(
                reconciliation_report_path=reconciliation_report_path,
                trajectories_path=trajectories_path,
                observed_parameters=set(sweep_summary),
            ),
            "step_3_selected_run_id": selected_run_id,
            "step_3_selection_artifact": str(selection_path),
            "step_3_selection_sha256": selection_sha256,
            "step_3_selection_sidecar_verified": selection_sidecar_verified,
            "step_3_selection_source_trajectories_sha256": hashlib.sha256(trajectories_path.read_bytes()).hexdigest(),
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
        force=args.force,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (DataContractError, FileExistsError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
