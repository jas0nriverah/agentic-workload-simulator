#!/usr/bin/env python3
"""Compile normalized measured runs into canonical Steps 1--3 CSV tables."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

DEFAULT_ASSIGNMENT_CONFIG = ROOT / "configs" / "assignment_steps_1_3.json"
REQUIRED_SWEEP_PARAMETERS = (
    "call_limit",
    "max_output_tokens",
    "observation_length",
    "temperature",
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


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssignmentContractError(f"{path} is not a JSON object")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise AssignmentContractError(f"{path}:{number} is not a JSON object")
        rows.append(value)
    return rows


def _csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _write_csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _csv_value(row.get(field)) for field in fields})
    temporary.replace(path)


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_step2_baseline(path: Path) -> tuple[dict[str, Any], dict[str, list[Any]], str]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AssignmentContractError(f"cannot read assignment config {path}: {exc}") from exc
    try:
        baseline = config["step_1"]["baseline"]
        knobs = config["step_2"]["knobs"]
    except (KeyError, TypeError) as exc:
        raise AssignmentContractError("assignment config lacks Step 1 baseline/Step 2 knobs") from exc
    if not isinstance(baseline, dict) or not isinstance(knobs, list):
        raise AssignmentContractError("assignment config baseline/knobs have invalid types")
    declared = {}
    for knob in knobs:
        if not isinstance(knob, dict) or set(knob) != {"name", "values"}:
            raise AssignmentContractError("each Step 2 knob must contain exactly name and values")
        name = knob["name"]
        values = knob["values"]
        if name not in REQUIRED_SWEEP_PARAMETERS or name in declared:
            raise AssignmentContractError(f"invalid or duplicate Step 2 knob: {name!r}")
        if not isinstance(values, list) or len(values) != 4 or baseline.get(name) not in values:
            raise AssignmentContractError(
                f"Step 2 knob {name!r} must declare four values including its baseline"
            )
        declared[name] = values
    if tuple(declared) != REQUIRED_SWEEP_PARAMETERS:
        raise AssignmentContractError("Step 2 knobs must cover the four declared parameters in order")
    return (
        {name: baseline[name] for name in REQUIRED_SWEEP_PARAMETERS},
        {name: list(declared[name]) for name in REQUIRED_SWEEP_PARAMETERS},
        _hash(path),
    )


def _baseline_identity(row: dict[str, Any]) -> tuple[str, str, str]:
    return (str(row["suite"]), str(row["instance_id"]), str(row["repeat_id"]))


def _baseline_sweep_rows(
    trajectories: list[dict[str, Any]],
    measured_sweeps: list[dict[str, Any]],
    baseline: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Project each measured shared baseline into each knob's view once.

    The same Step 1 measurement is reused as the baseline setting for every
    knob.  It is emitted only in ``sweep_runs.csv`` (never in the canonical
    trajectory table) and is bound to the source run by a deterministic ID.
    """
    sweep_keys = {
        (_baseline_identity(row), str(row.get("sweep_parameter")), str(row.get("sweep_value")))
        for row in measured_sweeps
    }
    baseline_rows = {
        _baseline_identity(row): row
        for row in trajectories
        if row.get("config_id") == "shared-baseline"
    }
    measured_run_ids = {str(row["run_id"]) for row in measured_sweeps}
    participating = {
        _baseline_identity(row)
        for row in measured_sweeps
        if row.get("sweep_parameter") in REQUIRED_SWEEP_PARAMETERS
    }
    derived: list[dict[str, Any]] = []
    bindings: list[dict[str, Any]] = []
    for identity in sorted(participating):
        source = baseline_rows.get(identity)
        if source is None:
            raise AssignmentContractError(
                "sweep row has no matching shared-baseline trajectory: "
                + "/".join(identity)
            )
        for parameter in REQUIRED_SWEEP_PARAMETERS:
            value = baseline[parameter]
            key = (identity, parameter, str(value))
            if key in sweep_keys:
                continue
            row = dict(source)
            row["run_id"] = (
                f"{source['run_id']}::step2-baseline::{parameter}={json.dumps(value, sort_keys=True)}"
            )
            row["sweep_parameter"] = parameter
            row["sweep_value"] = str(value)
            row["provenance"] = "derived_from_measured"
            if row["run_id"] in measured_run_ids or row["run_id"] in {
                item["run_id"] for item in derived
            }:
                raise AssignmentContractError(f"derived sweep run_id collision: {row['run_id']}")
            derived.append(row)
            bindings.append({
                "derived_run_id": row["run_id"],
                "source_run_id": source["run_id"],
                "suite": source["suite"],
                "instance_id": source["instance_id"],
                "repeat_id": source["repeat_id"],
                "parameter": parameter,
                "value": value,
            })
    return measured_sweeps + derived, bindings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--assignment-config", type=Path, default=DEFAULT_ASSIGNMENT_CONFIG)
    args = parser.parse_args(argv)
    trajectory_paths = sorted(args.runs_root.glob("**/trajectory.json"))
    if not trajectory_paths:
        raise AssignmentContractError("no normalized trajectory.json files found")
    trajectories: list[dict[str, Any]] = []
    tool_events: list[dict[str, Any]] = []
    model_events: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in trajectory_paths:
        row = validate_trajectory(_read_json(path))
        if row["run_id"] in seen:
            raise AssignmentContractError(f"duplicate run_id: {row['run_id']}")
        seen.add(row["run_id"])
        tool_path = Path(row["tool_events_path"])
        model_path = Path(row["model_events_path"])
        if tool_path.is_absolute() or model_path.is_absolute():
            raise AssignmentContractError(
                f"normalized event paths must be portable and relative for {row['run_id']}"
            )
        tools = [validate_tool_event(item) for item in _read_jsonl(path.parent / tool_path)]
        models = [validate_model_event(item) for item in _read_jsonl(path.parent / model_path)]
        if any(item["run_id"] != row["run_id"] for item in tools + models):
            raise AssignmentContractError(f"event/run mismatch for {row['run_id']}")
        if len(tools) != row["tool_event_count"] or len(models) != row["model_event_count"]:
            raise AssignmentContractError(f"event count mismatch for {row['run_id']}")
        if row["status"] == "completed" and any(
            item["status"] != "completed" for item in tools + models
        ):
            raise AssignmentContractError(
                f"completed trajectory contains incomplete events: {row['run_id']}"
            )
        tool_sum = sum(float(item["wall_ms"]) for item in tools if item["status"] == "completed")
        model_sum = sum(float(item["wall_ms"]) for item in models if item["status"] == "completed")
        if abs(tool_sum - float(row["tool_wall_ms"])) > 1e-6 or abs(model_sum - float(row["model_wall_ms"])) > 1e-6:
            raise AssignmentContractError(f"event sums do not match trajectory {row['run_id']}")
        trajectories.append(row)
        tool_events.extend(tools)
        model_events.extend(models)
    trajectories.sort(key=lambda row: (row["suite"], row["repository"], row["instance_id"], row["config_id"], row["repeat_id"]))
    tool_events.sort(key=lambda row: (row["run_id"], row["ordinal"]))
    model_events.sort(key=lambda row: (row["run_id"], row["ordinal"]))
    tool_ids = [row["event_id"] for row in tool_events]
    model_ids = [row["request_id"] for row in model_events]
    if len(tool_ids) != len(set(tool_ids)):
        raise AssignmentContractError("duplicate tool event_id across normalized runs")
    if len(model_ids) != len(set(model_ids)):
        raise AssignmentContractError("duplicate model request_id across normalized runs")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    outputs = {
        "trajectories": args.output_dir / "trajectories.csv",
        "tool_events": args.output_dir / "tool_events.csv",
        "model_events": args.output_dir / "model_events.csv",
        "sweep_runs": args.output_dir / "sweep_runs.csv",
    }
    _write_csv(outputs["trajectories"], TRAJECTORY_FIELDS, trajectories)
    _write_csv(outputs["tool_events"], TOOL_EVENT_FIELDS, tool_events)
    _write_csv(outputs["model_events"], MODEL_EVENT_FIELDS, model_events)
    baseline, declared_values, config_sha256 = _load_step2_baseline(args.assignment_config)
    measured_sweep_rows = [
        row for row in trajectories if row.get("sweep_parameter") not in (None, "")
    ]
    unknown_parameters = sorted({str(row["sweep_parameter"]) for row in measured_sweep_rows} - set(REQUIRED_SWEEP_PARAMETERS))
    if unknown_parameters:
        raise AssignmentContractError(
            "sweep rows contain undeclared Step 2 parameters: " + ", ".join(unknown_parameters)
        )
    sweep_rows, baseline_bindings = _baseline_sweep_rows(
        trajectories, measured_sweep_rows, baseline
    )
    if measured_sweep_rows:
        expected_values = {
            name: {str(value) for value in declared_values[name]}
            for name in REQUIRED_SWEEP_PARAMETERS
        }
        observed_values = {
            name: {str(row["sweep_value"]) for row in sweep_rows if row["sweep_parameter"] == name}
            for name in REQUIRED_SWEEP_PARAMETERS
        }
        if observed_values != expected_values:
            raise AssignmentContractError(
                "Step 2 sweep matrix does not contain exactly the four declared values per knob"
            )
    _write_csv(outputs["sweep_runs"], TRAJECTORY_FIELDS, sweep_rows)
    baseline_rows = [
        row for row in trajectories
        if row.get("config_id") == "shared-baseline"
    ]

    def suite_metrics(suite: str) -> dict[str, Any]:
        selected = [row for row in baseline_rows if row["suite"] == suite]
        submitted = [row for row in selected if row.get("submitted") is True]
        completed = [row for row in selected if row.get("status") == "completed"]
        resolved = [row for row in completed if row.get("official_resolved") is True]
        return {
            "selected": {"count": len(selected), "denominator": len(selected)},
            "submitted": {"count": len(submitted), "denominator": len(selected)},
            "completed": {"count": len(completed), "denominator": len(selected)},
            "resolved": {"count": len(resolved), "denominator": len(completed)},
            "resolved_rate": {
                "numerator": len(resolved),
                "denominator": len(completed),
                "percent": (100.0 * len(resolved) / len(completed)) if completed else None,
            },
            "average_completed_e2e_wall_ms": (
                sum(float(row["e2e_wall_ms"]) for row in completed) / len(completed)
                if completed else None
            ),
        }

    summary = {
        "schema_version": "assignment.dataset-inventory.v1",
        "trajectory_count": len(trajectories),
        "tool_event_count": len(tool_events),
        "model_event_count": len(model_events),
        "suite_counts": {
            suite: sum(1 for row in trajectories if row["suite"] == suite)
            for suite in ("lite", "verified")
        },
        "suite_headline_metrics": {
            suite: suite_metrics(suite) for suite in ("lite", "verified")
        },
        "repository_count": len({row["repository"] for row in trajectories}),
        "config_count": len({row["config_id"] for row in trajectories}),
        "hashes": {name: _hash(path) for name, path in outputs.items()},
        "step2_baseline_reuse": {
            "schema_version": "assignment-step2-baseline-reuse.v1",
            "assignment_config_sha256": config_sha256,
            "baseline_values": baseline,
            "source_rule": "shared-baseline rows with at least one measured nonbaseline knob row",
            "derived_row_count": len(baseline_bindings),
            "bindings": baseline_bindings,
            "no_double_count_rule": "one derived baseline row per source run, knob, and baseline value",
        },
        "ratio_definition": "sum(tool_event.wall_ms) / sum(model_event.wall_ms)",
        "secondary_diagnostics_not_ratio": [
            "cpu_activity_union_ms",
            "cuda_activity_union_ms",
            "kernel_duration_sum_ms",
            "gpu_utilization",
        ],
    }
    inventory = args.output_dir / "inventory.json"
    inventory.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (args.output_dir / "inventory.sha256").write_text(f"{_hash(inventory)}  inventory.json\n", encoding="utf-8")
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
