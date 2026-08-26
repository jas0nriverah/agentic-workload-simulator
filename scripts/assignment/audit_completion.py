#!/usr/bin/env python3
"""Audit machine-readable completion evidence for assignment Steps 1--3.

This command is deliberately offline and read-only with respect to its input
artifacts.  It joins the existing plan, reconciliation, canonical dataset,
Step 3 selection, figure report, frozen prediction manifest, and evaluation
report contracts.  Any missing, stale, incomplete, or cross-bound artifact
produces a blocked report and a non-zero exit status.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from agentic_sim.assignment.event_simulator import (  # noqa: E402
    EventSimulatorError,
    HardwareProfile,
    verify_frozen_prediction_manifest,
)
from scripts.assignment.generate_step_figures import (  # noqa: E402
    REQUIRED_SWEEP_PARAMETERS,
    generate_figures,
    load_canonical_tables,
)
from scripts.assignment.evaluate_predictions import (  # noqa: E402
    evaluate_predictions as recompute_event_evaluation,
)
from scripts.assignment.reconcile_plan import (  # noqa: E402
    PLAN_SCHEMA,
    REPORT_SCHEMA,
    ReconciliationError,
    _load_trajectories,
    _read_plan,
    _verify_plan_sidecar,
    reconcile,
)
from scripts.assignment.plan_matrix import (  # noqa: E402
    REQUIRED_KNOBS,
    REQUIRED_SUITES,
    load_config,
)
from scripts.assignment.select_step3_case import (  # noqa: E402
    SCHEMA_VERSION as STEP3_SCHEMA,
    select as recompute_step3_selection,
)
from scripts.assignment.adaptive_event_protocol import (  # noqa: E402
    ADAPTIVE_ARM_SCHEMA,
    ADAPTIVE_LABEL_SCHEMA,
    ADAPTIVE_MANIFEST_SCHEMA,
    ADAPTIVE_PROTOCOL_SCHEMA,
    SCORE_SCHEMA as ADAPTIVE_SCORE_SCHEMA,
    AdaptiveProtocolError,
    FrozenCalibrationModel,
    _label_mapping as normalize_adaptive_label,
    _read_hashed_json as read_adaptive_hashed_json,
    _record_digest as adaptive_record_digest,
    verify_trajectory_prediction,
)
from agentic_sim.assignment.schema import TRAJECTORY_FIELDS  # noqa: E402


SCHEMA_VERSION = "assignment.completion-audit.v1"
EVALUATION_SCHEMA = "assignment.event-holdout-evaluation.v1"
INVENTORY_SCHEMA = "assignment.dataset-inventory.v1"
FIGURE_SCHEMA = "assignment-step-figures.v1"
GATE_PERCENT = 25.0
ASSIGNMENT_CONFIG = ROOT / "configs" / "assignment_steps_1_3.json"


class AuditError(ValueError):
    """Raised when an input artifact violates its current contract."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise AuditError(f"cannot read {path}: {exc}") from exc
    return digest.hexdigest()


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditError(f"{label} is unreadable: {exc}") from exc
    if not isinstance(value, dict):
        raise AuditError(f"{label} must contain a JSON object")
    return value


def _verify_sidecar(path: Path, sidecar: Path, label: str) -> str:
    digest = _sha256(path)
    expected = f"{digest}  {path.name}\n".encode("utf-8")
    try:
        actual = sidecar.read_bytes()
    except OSError as exc:
        raise AuditError(f"{label} SHA-256 sidecar is unreadable: {exc}") from exc
    if actual != expected:
        raise AuditError(f"{label} SHA-256 sidecar does not match the exact bytes and filename")
    return digest


def _recognized_sidecar(path: Path, label: str) -> Path:
    """Return the sole accepted sidecar using either repository convention."""
    candidates = list(dict.fromkeys((Path(str(path) + ".sha256"), path.with_suffix(".sha256"))))
    existing = [candidate for candidate in candidates if candidate.exists()]
    if len(existing) != 1:
        raise AuditError(f"{label} requires exactly one recognized SHA-256 sidecar")
    return existing[0]


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AuditError(f"{label} must be a non-negative integer")
    return value


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AuditError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise AuditError(f"{label} must be finite")
    return result


def _csv_scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return str(value)
    raise AuditError("checked-in config sweep value cannot be represented in canonical CSV")


def _load_csv(path: Path, label: str) -> tuple[list[str], list[dict[str, str]]]:
    try:
        with path.open(newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames is None:
                raise AuditError(f"{label} is missing a CSV header")
            rows = list(reader)
    except OSError as exc:
        raise AuditError(f"{label} is unreadable: {exc}") from exc
    if any(None in row for row in rows):
        raise AuditError(f"{label} contains unnamed CSV columns")
    return list(reader.fieldnames), rows


def _path_matches(reported: Any, actual: Path, label: str) -> None:
    if not isinstance(reported, str) or not reported.strip():
        raise AuditError(f"figure report source {label} is missing")
    reported_path = Path(reported)
    if not reported_path.is_absolute():
        reported_path = Path.cwd() / reported_path
    if reported_path.resolve() != actual.resolve():
        raise AuditError(f"figure report source {label} is not bound to the audited CSV")


def _audit_plan_config(header: Mapping[str, Any], cases: list[dict[str, Any]]) -> None:
    """Bind a sealed plan to the exact checked-in assignment configuration.

    A plan hash only proves that one particular plan was sealed.  This check
    additionally proves that its identities, sweep matrix, and cardinality are
    the ones prescribed by the repository's assignment configuration.
    """
    try:
        config = load_config(ASSIGNMENT_CONFIG)
    except ValueError as exc:
        raise AuditError(f"checked-in assignment config is invalid: {exc}") from exc
    config_digest = _sha256(ASSIGNMENT_CONFIG)
    if header.get("config_sha256") != config_digest:
        raise AuditError("sealed plan is not bound to the exact checked-in assignment config")
    if header.get("plan_id") != config["plan_id"]:
        raise AuditError("sealed plan_id does not match the checked-in assignment config")
    if header.get("pins") != config["pins"]:
        raise AuditError("sealed plan pins do not match the checked-in assignment config")
    limits = config["execution_limits"]
    for field in ("concurrency", "per_case_deadline_seconds", "global_deadline_seconds"):
        if header.get(field) != limits[field]:
            raise AuditError(f"sealed plan {field} does not match the checked-in assignment config")

    sources = header.get("sources")
    if not isinstance(sources, dict) or set(sources) != set(REQUIRED_SUITES):
        raise AuditError("sealed plan sources must cover exactly Lite and Verified")
    source_counts: dict[str, int] = {}
    for suite in REQUIRED_SUITES:
        source = sources[suite]
        expected = config["step_1"]["suites"][suite]
        if not isinstance(source, dict) or source.get("dataset") != expected["dataset"] or source.get("revision") != expected["revision"]:
            raise AuditError(f"sealed plan {suite} source does not match the checked-in assignment config")
        count = source.get("task_count")
        digest = source.get("manifest_sha256")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise AuditError(f"sealed plan {suite} source task_count is invalid")
        if not isinstance(digest, str) or len(digest) != 64:
            raise AuditError(f"sealed plan {suite} source manifest SHA-256 is invalid")
        source_counts[suite] = count

    step2 = header.get("step_2")
    expected_step2 = config["step_2"]
    if not isinstance(step2, dict) or set(step2) != {"shared_baseline", "task_selection", "selected_task_ids", "knobs"}:
        raise AuditError("sealed plan Step 2 contract is incomplete")
    for field in ("shared_baseline", "task_selection", "knobs"):
        if step2.get(field) != expected_step2[field]:
            raise AuditError(f"sealed plan Step 2 {field} does not match the checked-in assignment config")
    if header.get("step_3_selection_policy") != config["step_3"]:
        raise AuditError("sealed plan Step 3 policy does not match the checked-in assignment config")

    selected = step2["selected_task_ids"]
    if not isinstance(selected, dict) or set(selected) != set(REQUIRED_SUITES):
        raise AuditError("sealed plan Step 2 selected_task_ids must cover exactly Lite and Verified")
    selected_ids: dict[str, set[str]] = {}
    expected_selected_count = expected_step2["task_selection"]["tasks_per_suite"]
    for suite in REQUIRED_SUITES:
        ids = selected[suite]
        if not isinstance(ids, list) or ids != sorted(ids) or len(ids) != expected_selected_count:
            raise AuditError(f"sealed plan {suite} Step 2 selection has the wrong cardinality or order")
        if any(not isinstance(item, str) or not item for item in ids) or len(set(ids)) != len(ids):
            raise AuditError(f"sealed plan {suite} Step 2 selection contains invalid task identities")
        selected_ids[suite] = set(ids)

    baseline = config["step_1"]["baseline"]
    sweep_values = {
        item["name"]: [value for value in item["values"] if value != baseline[item["name"]]]
        for item in expected_step2["knobs"]
    }
    baseline_ids: dict[str, set[str]] = {suite: set() for suite in REQUIRED_SUITES}
    expected_sweeps: set[tuple[str, str, str, str]] = set()
    actual_sweeps: set[tuple[str, str, str, str]] = set()
    for suite in REQUIRED_SUITES:
        for instance_id in selected_ids[suite]:
            for knob in REQUIRED_KNOBS:
                for value in sweep_values[knob]:
                    expected_sweeps.add((suite, instance_id, knob, _csv_scalar(value)))

    for case in cases:
        suite = case["suite"]
        instance_id = case["instance_id"]
        if case.get("settings") is None or case.get("concurrency") != 1:
            raise AuditError("sealed plan case lacks required immutable settings")
        if case["cell_id"] == "shared-baseline":
            if case["settings"] != baseline or case.get("variation") is not None:
                raise AuditError("sealed plan shared baseline does not match the checked-in assignment config")
            baseline_ids[suite].add(instance_id)
            expected_steps = [1, 2] if instance_id in selected_ids[suite] else [1]
            expected_roles = (
                ["step_1_baseline", "step_2_shared_baseline"]
                if instance_id in selected_ids[suite] else ["step_1_baseline"]
            )
            if case.get("steps") != expected_steps or case.get("roles") != expected_roles:
                raise AuditError("sealed plan baseline case roles do not match the checked-in assignment config")
            continue
        variation = case.get("variation")
        if not isinstance(variation, dict) or set(variation) != {"knob", "value"}:
            raise AuditError("sealed plan sweep case has an invalid variation")
        knob, value = variation["knob"], variation["value"]
        if knob not in REQUIRED_KNOBS or value not in sweep_values[knob]:
            raise AuditError("sealed plan sweep case is not an allowed checked-in config variation")
        canonical_value = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        if case.get("cell_id") != f"{knob}={canonical_value}":
            raise AuditError("sealed plan sweep case identity does not match its checked-in config variation")
        settings = dict(baseline)
        settings[knob] = value
        if case.get("settings") != settings or case.get("steps") != [2] or case.get("roles") != ["step_2_sweep"]:
            raise AuditError("sealed plan sweep case settings or roles do not match the checked-in assignment config")
        actual_sweeps.add((suite, instance_id, knob, _csv_scalar(value)))

    for suite in REQUIRED_SUITES:
        if len(baseline_ids[suite]) != source_counts[suite]:
            raise AuditError(f"sealed plan {suite} baseline cardinality does not match its source manifest")
        if not selected_ids[suite].issubset(baseline_ids[suite]):
            raise AuditError(f"sealed plan {suite} Step 2 selection is not represented by baseline identities")
    if actual_sweeps != expected_sweeps:
        raise AuditError("sealed plan sweep identities or cardinality do not match the checked-in assignment config")
    expected_case_count = sum(source_counts.values()) + len(expected_sweeps)
    if len(cases) != expected_case_count or header.get("execution_case_count") != expected_case_count:
        raise AuditError("sealed plan execution-case cardinality does not match the checked-in assignment config")


def _audit_plan(plan: Path, plan_sha256: Path) -> dict[str, Any]:
    digest = _verify_plan_sidecar(plan, plan_sha256)
    header, cases = _read_plan(plan)
    if header.get("schema_version") != PLAN_SCHEMA:
        raise AuditError("sealed plan has an unsupported schema")
    if header.get("planning_only") is not True or header.get("concurrency") != 1:
        raise AuditError("sealed plan does not enforce planning_only and concurrency=1")
    if not cases:
        raise AuditError("sealed plan has no execution cases")
    _audit_plan_config(header, cases)
    return {
        "passed": True,
        "sha256": digest,
        "plan_id": header["plan_id"],
        "case_count": len(cases),
    }


def _audit_reconciliation(
    report_path: Path,
    plan_path: Path,
    plan_digest: str,
    trajectories: Path,
    plan_case_count: int,
) -> dict[str, Any]:
    report = _read_json(report_path, "reconciliation report")
    if report.get("schema_version") != REPORT_SCHEMA:
        raise AuditError("reconciliation report has an unsupported schema")
    trajectory_digest = _sha256(trajectories)
    if report.get("original_plan_sha256") != plan_digest:
        raise AuditError("reconciliation report is not bound to the sealed plan hash")
    if report.get("trajectories_sha256") != trajectory_digest:
        raise AuditError("reconciliation report is not bound to the audited trajectories hash")
    try:
        header, cases = _read_plan(plan_path)
        _remaining, expected = reconcile(
            header,
            cases,
            _load_trajectories(trajectories),
            original_plan_sha256=plan_digest,
            trajectories_sha256=trajectory_digest,
        )
    except ReconciliationError as exc:
        raise AuditError(f"cannot independently recompute reconciliation: {exc}") from exc
    if report != expected:
        raise AuditError("reconciliation report does not exactly match independent recomputation")
    if expected["matched_case_count"] != plan_case_count or expected["remaining_case_count"] != 0:
        raise AuditError("independent reconciliation does not match every sealed plan case")
    if any(expected[field] != 0 for field in (
        "unmatched_case_count", "rejected_or_ambiguous_case_count", "rejected_trajectory_count",
    )):
        raise AuditError("independent reconciliation contains unmatched, rejected, or ambiguous evidence")
    return {
        "passed": True,
        "sha256": _sha256(report_path),
        "plan_sha256": plan_digest,
        "trajectories_sha256": trajectory_digest,
        "case_count": plan_case_count,
        "matrix_counts": {
            field: expected[field]
            for field in (
                "original_case_count",
                "matched_case_count",
                "remaining_case_count",
                "rejected_or_ambiguous_case_count",
            )
        },
    }


def _audit_inventory(
    inventory_path: Path,
    inventory_sha256: Path,
    trajectories: Path,
    tool_events: Path,
    model_events: Path,
    sweep_runs: Path,
) -> dict[str, Any]:
    inventory_digest = _verify_sidecar(inventory_path, inventory_sha256, "dataset inventory")
    inventory = _read_json(inventory_path, "dataset inventory")
    if inventory.get("schema_version") != INVENTORY_SCHEMA:
        raise AuditError("dataset inventory has an unsupported schema")
    expected_paths = {
        "trajectories": trajectories,
        "tool_events": tool_events,
        "model_events": model_events,
        "sweep_runs": sweep_runs,
    }
    hashes = inventory.get("hashes")
    if not isinstance(hashes, dict) or set(hashes) != set(expected_paths):
        raise AuditError("dataset inventory hashes must cover exactly the four canonical CSV tables")
    for name, path in expected_paths.items():
        actual = _sha256(path)
        if hashes.get(name) != actual:
            raise AuditError(f"dataset inventory hash mismatch for {name}")
    if inventory.get("ratio_definition") != "sum(tool_event.wall_ms) / sum(model_event.wall_ms)":
        raise AuditError("dataset inventory uses the wrong primary ratio definition")
    loaded_trajectories, validated_sweeps = load_canonical_tables(
        trajectories, tool_events, model_events, sweep_runs
    )
    _fields, trajectory_rows = _load_csv(trajectories, "trajectories")
    expected_counts = {
        "trajectory_count": len(trajectory_rows),
        "tool_event_count": len(_load_csv(tool_events, "tool_events")[1]),
        "model_event_count": len(_load_csv(model_events, "model_events")[1]),
        "repository_count": len({row["repository"] for row in loaded_trajectories}),
        "config_count": len({row.get("config_id", "") for row in trajectory_rows}),
    }
    for field, expected in expected_counts.items():
        if inventory.get(field) != expected:
            raise AuditError(f"dataset inventory {field} does not match canonical CSV rows")
    suite_counts = inventory.get("suite_counts")
    if not isinstance(suite_counts, dict) or set(suite_counts) != {"lite", "verified"}:
        raise AuditError("dataset inventory suite_counts must contain lite and verified")
    for suite in suite_counts:
        if suite_counts[suite] != sum(1 for row in trajectory_rows if row.get("suite") == suite):
            raise AuditError(f"dataset inventory suite count mismatch for {suite}")
    if inventory["trajectory_count"] != len(loaded_trajectories):
        raise AuditError("dataset inventory trajectory count does not match validated rows")
    return {
        "passed": True,
        "sha256": inventory_digest,
        "table_hashes": {name: hashes[name] for name in sorted(hashes)},
        "trajectory_count": inventory["trajectory_count"],
        "tool_event_count": inventory["tool_event_count"],
        "model_event_count": inventory["model_event_count"],
        "sweep_run_count": len(validated_sweeps),
    }


def _audit_step3(selection_path: Path, trajectories: Path) -> dict[str, Any]:
    selection = _read_json(selection_path, "Step 3 selection")
    if selection.get("schema_version") != STEP3_SCHEMA:
        raise AuditError("Step 3 selection has an unsupported schema")
    source_digest = _sha256(trajectories)
    if selection.get("source_trajectories_sha256") != source_digest:
        raise AuditError("Step 3 selection is not bound to canonical trajectories")
    _fields, rows = _load_csv(trajectories, "trajectories")
    try:
        expected = recompute_step3_selection(rows, source_digest)
    except (ValueError, KeyError, TypeError) as exc:
        raise AuditError(f"Step 3 selection cannot be recomputed from canonical trajectories: {exc}") from exc
    if selection != expected:
        raise AuditError("Step 3 selection does not equal the deterministic selection from canonical trajectories")
    selected = selection.get("selected")
    if not isinstance(selected, dict) or selected.get("config_id") != "shared-baseline":
        raise AuditError("Step 3 selection is not a shared-baseline trajectory")
    return {
        "passed": True,
        "sha256": _sha256(selection_path),
        "source_trajectories_sha256": source_digest,
        "selected_run_id": selected["run_id"],
        "ratio": selected["tool_model_ratio"],
    }


def _audit_figures(
    report_path: Path,
    trajectories: Path,
    tool_events: Path,
    model_events: Path,
    sweep_runs: Path,
    inventory: Mapping[str, Any],
    reconciliation: Mapping[str, Any],
) -> dict[str, Any]:
    report = _read_json(report_path, "figure report")
    if report.get("schema_version") != FIGURE_SCHEMA:
        raise AuditError("figure report has an unsupported schema")
    if report.get("ratio_definition") != "sum(tool_event.wall_ms) / sum(model_event.wall_ms)":
        raise AuditError("figure report uses the wrong primary ratio definition")
    sources = report.get("sources")
    if not isinstance(sources, dict):
        raise AuditError("figure report sources are missing")
    for name, path in {
        "trajectories": trajectories,
        "tool_events": tool_events,
        "model_events": model_events,
        "sweep_runs": sweep_runs,
    }.items():
        _path_matches(sources.get(name), path, name)
    counts = report.get("counts")
    if not isinstance(counts, dict):
        raise AuditError("figure report counts are missing")
    for field, inventory_field in (
        ("trajectories", "trajectory_count"),
        ("tool_events", "tool_event_count"),
        ("model_events", "model_event_count"),
        ("sweep_runs", "sweep_run_count"),
    ):
        if counts.get(field) != inventory[inventory_field]:
            raise AuditError(f"figure report {field} count does not match dataset inventory")
    coverage = report.get("coverage")
    if not isinstance(coverage, dict):
        raise AuditError("figure report coverage is missing")
    required = set(REQUIRED_SWEEP_PARAMETERS)
    if set(coverage.get("required_sweep_parameters", [])) != required:
        raise AuditError("figure report does not declare the four required sweep parameters")
    if set(coverage.get("observed_sweep_parameters", [])) != required:
        raise AuditError("figure report is missing one or more required sweep parameters")
    if coverage.get("missing_sweep_parameters") != []:
        raise AuditError("figure report declares missing sweep parameters")
    if coverage.get("parameter_name_coverage_complete") is not True:
        raise AuditError("figure report does not prove parameter-name coverage")
    if coverage.get("complete_assignment_matrix") is not True:
        raise AuditError("figure report does not prove a complete assignment matrix")
    required_figures = {
        "step1_repository_ratio.svg",
        "step1_accuracy_vs_latency.svg",
        "step1_accuracy_vs_ratio.svg",
        "step1_sample_latency_vs_ratio.svg",
        "step2_combined.svg",
        "step3_latency_breakdown.svg",
        "step3_tool_events.svg",
        "step3_model_tokens_vs_latency.svg",
        *(f"step2_{name}.svg" for name in (
            "call-limit", "max-output-tokens", "observation-length", "temperature"
        )),
    }
    if not required_figures.issubset(set(report.get("figures", []))):
        raise AuditError("figure report does not list all required Step 1--3 figures")
    listed = report.get("figures")
    if not isinstance(listed, list) or any(not isinstance(item, str) for item in listed):
        raise AuditError("figure report figures must be a string list")
    if len(listed) != len(set(listed)):
        raise AuditError("figure report contains duplicate figure names")
    inventory_rows = report.get("figure_inventory")
    if not isinstance(inventory_rows, list) or len(inventory_rows) != len(listed):
        raise AuditError("figure report inventory must cover every listed figure")
    inventory_by_name: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(inventory_rows):
        if not isinstance(row, dict) or set(row) != {"path", "sha256", "size_bytes"}:
            raise AuditError(f"figure inventory row {index} has invalid fields")
        name = row.get("path")
        if not isinstance(name, str) or not name or Path(name).name != name:
            raise AuditError(f"figure inventory row {index} has an unsafe path")
        if name in inventory_by_name:
            raise AuditError("figure inventory contains duplicate paths")
        digest = row.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            raise AuditError(f"figure inventory row {index} has an invalid SHA-256")
        size = row.get("size_bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise AuditError(f"figure inventory row {index} has an invalid size")
        inventory_by_name[name] = row
    if set(inventory_by_name) != set(listed):
        raise AuditError("figure inventory names do not match the listed figures")
    for name, row in sorted(inventory_by_name.items()):
        figure_path = report_path.parent / name
        if not figure_path.is_file():
            raise AuditError(f"listed figure is missing: {figure_path}")
        if figure_path.stat().st_size != row["size_bytes"]:
            raise AuditError(f"listed figure size mismatch: {name}")
        if _sha256(figure_path) != row["sha256"]:
            raise AuditError(f"listed figure SHA-256 mismatch: {name}")
    # Do not treat the report's figure inventory as evidence of derivation.
    # Regenerate the complete deterministic report and SVG payloads from the
    # canonical tables in an isolated directory, then require exact equality.
    try:
        with tempfile.TemporaryDirectory(prefix="assignment-figure-audit-") as temporary:
            expected = generate_figures(
                trajectories_path=trajectories,
                tool_events_path=tool_events,
                model_events_path=model_events,
                sweep_runs_path=sweep_runs,
                output_dir=Path(temporary),
                reconciliation_report_path=None,
                force=False,
            )
    except (OSError, ValueError) as exc:
        raise AuditError(f"cannot independently regenerate assignment figures: {exc}") from exc
    # Matrix completeness is proven by the separate, independently recomputed
    # reconciliation check.  Its report pathname is intentionally excluded:
    # it is a location annotation, not a canonical-table derivation.
    expected_coverage = dict(expected["coverage"])
    expected_coverage.update({
        "complete_assignment_matrix": True,
        "matrix_completeness_basis": str(report_path.parent / "<independently-recomputed-reconciliation>"),
        **reconciliation["matrix_counts"],
    })
    reported_for_comparison = dict(report)
    reported_coverage = dict(coverage)
    # Normalize only the non-data location annotation before exact comparison.
    reported_coverage["matrix_completeness_basis"] = expected_coverage["matrix_completeness_basis"]
    reported_for_comparison["coverage"] = reported_coverage
    expected["coverage"] = expected_coverage
    if reported_for_comparison != expected:
        raise AuditError("figure report does not exactly match independent canonical-table regeneration")
    return {
        "passed": True,
        "sha256": _sha256(report_path),
        "figure_count": len(listed),
        "figure_hashes": {
            name: inventory_by_name[name]["sha256"] for name in sorted(inventory_by_name)
        },
        "required_sweep_parameters": sorted(required),
    }


def _validate_scored_rows(rows: Any, label: str) -> set[str]:
    if not isinstance(rows, list) or not rows:
        raise AuditError(f"evaluation {label} has no scored rows")
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise AuditError(f"evaluation {label} row {index} is not an object")
        identifier = row.get("event_id", row.get("request_id", row.get("run_id")))
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            raise AuditError(f"evaluation {label} has a missing or duplicate identifier")
        seen.add(identifier)
        ape = _finite(row.get("absolute_percentage_error"), f"evaluation {label} APE")
        if ape > GATE_PERCENT or row.get("within_25_percent") is not True:
            raise AuditError(f"evaluation {label} contains a row above the 25% gate")
    return seen


def _audit_evaluation(
    report_path: Path,
    prediction_manifest: Path,
    holdout_labels: Path,
    prepare_receipt: Path,
) -> dict[str, Any]:
    report_digest = _verify_sidecar(
        report_path, report_path.with_suffix(".sha256"), "event/E2E evaluation report"
    )
    report = _read_json(report_path, "event/E2E evaluation report")
    if report.get("schema_version") != EVALUATION_SCHEMA:
        raise AuditError("event/E2E evaluation report has an unsupported schema")
    try:
        manifest, manifest_digest = verify_frozen_prediction_manifest(prediction_manifest)
    except EventSimulatorError as exc:
        raise AuditError(f"frozen prediction manifest is invalid: {exc}") from exc
    try:
        recomputed = recompute_event_evaluation(
            prediction_manifest, holdout_labels, prepare_receipt
        )
    except EventSimulatorError as exc:
        raise AuditError(f"cannot independently recompute event/E2E evaluation: {exc}") from exc
    if report != recomputed:
        raise AuditError(
            "event/E2E evaluation report does not exactly match independent recomputation"
        )
    if report.get("prediction_manifest_sha256") != manifest_digest:
        raise AuditError("evaluation report is not bound to the frozen prediction manifest")
    if report.get("gate_percent") != GATE_PERCENT:
        raise AuditError("evaluation report does not use the required 25% gate")
    if report.get("passed") is not True or report.get("coverage_complete") is not True:
        raise AuditError("event/E2E evaluation did not pass complete coverage")
    summaries = report.get("summaries")
    if not isinstance(summaries, dict):
        raise AuditError("event/E2E evaluation summaries are missing")
    counts: dict[str, int] = {}
    for name, rows_key in (
        ("tool_events", "tool_events"),
        ("model_events", "model_events"),
        ("trajectories", "trajectories"),
    ):
        summary = summaries.get(name)
        if not isinstance(summary, dict):
            raise AuditError(f"evaluation summary {name} is missing")
        if summary.get("unavailable_count") != 0:
            raise AuditError(f"evaluation summary {name} contains unavailable rows")
        if summary.get("all_available_within_25_percent") is not True:
            raise AuditError(f"evaluation summary {name} does not pass the 25% gate")
        scored_ids = _validate_scored_rows(report.get(rows_key), name)
        manifest_key, manifest_id = {
            "tool_events": ("tool_predictions", "event_id"),
            "model_events": ("model_predictions", "request_id"),
            "trajectories": ("trajectory_predictions", "run_id"),
        }[name]
        predictions = manifest.get(manifest_key)
        if not isinstance(predictions, list) or not predictions:
            raise AuditError(f"frozen prediction manifest has no {name} predictions")
        expected_ids = {
            row.get(manifest_id)
            for row in predictions
            if isinstance(row, dict) and isinstance(row.get(manifest_id), str)
        }
        if scored_ids != expected_ids:
            raise AuditError(f"evaluation {name} does not score every frozen prediction exactly once")
        count = len(scored_ids)
        if summary.get("available_count") != count:
            raise AuditError(f"evaluation summary {name} count does not match scored rows")
        counts[name] = count
    unavailable = report.get("unavailable")
    if not isinstance(unavailable, dict) or any(unavailable.get(name) != [] for name in ("tool_events", "model_events")):
        raise AuditError("event/E2E evaluation contains unavailable or skipped rows")
    return {
        "passed": True,
        "protocol_type": "static_historical",
        "sha256": report_digest,
        "prediction_manifest_sha256": manifest_digest,
        "scored_counts": counts,
        "gate_percent": GATE_PERCENT,
    }


def _require_adaptive_sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise AuditError(f"adaptive {label} must be a lowercase SHA-256 digest")
    return value


def _positive_adaptive(value: Any, label: str) -> float:
    result = _finite(value, f"adaptive {label}")
    if result <= 0:
        raise AuditError(f"adaptive {label} must be positive")
    return result


def _read_adaptive_journal(path: Path, binding: Mapping[str, str], run_id: str) -> list[dict[str, Any]]:
    """Read the immutable adaptive journal without instantiating a launcher.

    This intentionally mirrors the protocol's ordering invariants.  The
    completion audit is independent of the live proxy/hook processes, so a
    persisted ``passed`` flag can never substitute for the primary evidence.
    """
    if not path.is_file() or path.is_symlink():
        raise AuditError("adaptive journal must be a regular non-symlink file")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise AuditError(f"adaptive journal is unreadable: {exc}") from exc
    if not lines:
        raise AuditError("adaptive journal is empty")

    records: list[dict[str, Any]] = []
    previous_hash = ""
    previous_monotonic = -1
    clock_id: str | None = None
    boot_id: Any = None
    pending: dict[str, Any] | None = None
    next_ordinal = 0
    trajectory_label: dict[str, Any] | None = None
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            raise AuditError(f"adaptive journal has blank line {line_number}")
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AuditError(f"adaptive journal line {line_number} is invalid JSON") from exc
        if not isinstance(record, dict) or record.get("schema_version") != ADAPTIVE_PROTOCOL_SCHEMA:
            raise AuditError(f"adaptive journal line {line_number} has an unsupported schema")
        if record.get("journal_ordinal") != len(records):
            raise AuditError("adaptive journal has duplicate or reordered journal ordinal")
        if record.get("chain_prev_sha256") != previous_hash:
            raise AuditError("adaptive journal hash chain is broken")
        if record.get("record_sha256") != adaptive_record_digest(record):
            raise AuditError("adaptive journal record hash is invalid")
        if record.get("binding") != binding:
            raise AuditError("adaptive journal record binding changed")
        witness = record.get("clock")
        if not isinstance(witness, Mapping):
            raise AuditError("adaptive journal record clock witness is missing")
        monotonic = witness.get("monotonic_ns")
        if isinstance(monotonic, bool) or not isinstance(monotonic, int) or monotonic <= previous_monotonic:
            raise AuditError("adaptive journal chronology is not strictly monotonic")
        observed_clock_id = witness.get("clock_id")
        if not isinstance(observed_clock_id, str) or not observed_clock_id:
            raise AuditError("adaptive journal record clock identity is invalid")
        if records and (observed_clock_id != clock_id or witness.get("boot_id") != boot_id):
            raise AuditError("adaptive journal clock identity changed during a trajectory")
        previous_monotonic = monotonic
        clock_id = observed_clock_id
        boot_id = witness.get("boot_id")

        record_type = record.get("record_type")
        identifier = record.get("identifier")
        if record_type in {"event_prediction", "event_label"} and trajectory_label is not None:
            raise AuditError("adaptive journal appends event evidence after trajectory/E2E reveal")
        if record_type == "event_prediction":
            if pending is not None:
                raise AuditError("adaptive journal accepts a second prediction while one is pending")
            if record.get("kind") not in {"tool", "model"}:
                raise AuditError("adaptive event prediction has an invalid kind")
            if not isinstance(identifier, str) or not identifier:
                raise AuditError("adaptive event prediction identifier is missing")
            if record.get("run_id") != run_id or record.get("event_ordinal") != next_ordinal:
                raise AuditError("adaptive event prediction run binding or ordinal is invalid")
            feature = record.get("feature")
            if not isinstance(feature, Mapping) or not isinstance(record.get("feature_sha256"), str):
                raise AuditError("adaptive event prediction feature evidence is invalid")
            # The source protocol has already parsed these strictly.  Hashing
            # here binds the persisted pre-event feature payload to the record.
            from agentic_sim.assignment.event_simulator import canonical_sha256

            if record["feature_sha256"] != canonical_sha256(feature):
                raise AuditError("adaptive event prediction feature hash is invalid")
            prediction = record.get("prediction")
            if not isinstance(prediction, Mapping) or set(prediction) != {"predicted_ms"}:
                raise AuditError("adaptive event prediction payload is invalid")
            _positive_adaptive(prediction["predicted_ms"], "event predicted_ms")
            if any(item.get("identifier") == identifier for item in records if item.get("record_type") == "event_prediction"):
                raise AuditError("adaptive journal contains a duplicate event prediction identifier")
            pending = record
            next_ordinal += 1
        elif record_type == "event_label":
            if pending is None or identifier != pending.get("identifier"):
                raise AuditError("adaptive event label does not immediately follow its matching prediction")
            if record.get("kind") != pending.get("kind") or record.get("run_id") != run_id:
                raise AuditError("adaptive event label kind or run binding is invalid")
            if record.get("event_ordinal") != pending.get("event_ordinal"):
                raise AuditError("adaptive event label ordinal does not match its prediction")
            if record.get("prediction_record_sha256") != pending.get("record_sha256"):
                raise AuditError("adaptive event label is not bound to its prediction record")
            label = record.get("label")
            try:
                if not isinstance(label, Mapping) or normalize_adaptive_label(label) != label:
                    raise AuditError("adaptive event label is not canonical")
            except AdaptiveProtocolError as exc:
                raise AuditError(f"adaptive event label is invalid: {exc}") from exc
            pending = None
        elif record_type == "trajectory_label":
            if pending is not None or trajectory_label is not None:
                raise AuditError("adaptive trajectory/E2E label is duplicated or revealed before event labels")
            if record.get("kind") != "trajectory" or identifier != run_id or record.get("run_id") != run_id:
                raise AuditError("adaptive trajectory/E2E label is not bound to the armed trajectory")
            if record.get("event_ordinal") is not None:
                raise AuditError("adaptive trajectory/E2E label has an event ordinal")
            label = record.get("label")
            if not isinstance(label, Mapping) or label.get("schema_version") != "assignment.trajectory-holdout-label.v1":
                raise AuditError("adaptive trajectory/E2E label has an unsupported schema")
            if label.get("status") != "completed" or set(label) != {"schema_version", "status", "observed_ms"}:
                raise AuditError("adaptive trajectory/E2E label is incomplete")
            _positive_adaptive(label.get("observed_ms"), "trajectory observed_ms")
            _require_adaptive_sha(record.get("prediction_manifest_sha256"), "trajectory prediction_manifest_sha256")
            trajectory_label = record
        else:
            raise AuditError("adaptive journal contains an unknown record type")
        records.append(record)
        previous_hash = record["record_sha256"]

    if pending is not None:
        raise AuditError("adaptive journal ends with a pending event prediction")
    if trajectory_label is None:
        raise AuditError("adaptive journal has no trajectory/E2E reveal")
    return records


def _audit_adaptive_holdout(root: Path, score_path: Path) -> dict[str, Any]:
    """Audit one completed live adaptive holdout trajectory fail-closed."""
    root = Path(root)
    if not root.is_dir() or root.is_symlink():
        raise AuditError("adaptive holdout root must be a directory, not a symlink")
    arm_path = root / "trajectory_arm.json"
    manifest_path = root / "adaptive_prediction_manifest.json"
    journal_path = root / "adaptive_events.jsonl"
    for path, label in ((arm_path, "adaptive trajectory arm"), (manifest_path, "adaptive prediction manifest")):
        if not path.is_file() or path.is_symlink():
            raise AuditError(f"{label} must be a regular non-symlink file")
    try:
        arm, arm_digest = read_adaptive_hashed_json(arm_path, kind="adaptive trajectory arm")
        manifest, manifest_digest = read_adaptive_hashed_json(manifest_path, kind="adaptive prediction manifest")
    except AdaptiveProtocolError as exc:
        raise AuditError(str(exc)) from exc
    if arm.get("schema_version") != ADAPTIVE_ARM_SCHEMA or arm.get("protocol_schema") != ADAPTIVE_PROTOCOL_SCHEMA:
        raise AuditError("adaptive trajectory arm has an unsupported schema")
    if set(arm) != {"schema_version", "protocol_schema", "run_id", "split", "binding", "pre_trajectory_e2e", "labels_accessed"}:
        raise AuditError("adaptive trajectory arm has unexpected or missing fields")
    run_id = arm.get("run_id")
    if not isinstance(run_id, str) or not run_id or arm.get("split") != "holdout" or arm.get("labels_accessed") is not False:
        raise AuditError("adaptive trajectory arm does not represent an unopened holdout")
    binding = arm.get("binding")
    binding_fields = {
        "split_manifest_sha256", "runtime_manifest_sha256", "hardware_profile_sha256",
        "model_revision_sha256", "calibration_model_sha256",
    }
    if not isinstance(binding, Mapping) or set(binding) != binding_fields:
        raise AuditError("adaptive trajectory arm bindings are incomplete")
    normalized_binding = {name: _require_adaptive_sha(binding[name], f"binding.{name}") for name in sorted(binding_fields)}
    pre_trajectory = arm.get("pre_trajectory_e2e")
    if not isinstance(pre_trajectory, Mapping) or set(pre_trajectory) != {
        "predicted_ms",
        "prediction_artifact_path",
        "prediction_artifact_sha256",
        "calibration_model_path",
        "hardware_profile_path",
    }:
        raise AuditError(
            "adaptive trajectory arm must freeze a calibration-derived pre-trajectory E2E artifact"
        )
    _positive_adaptive(pre_trajectory["predicted_ms"], "trajectory predicted_ms")
    prediction_path = Path(str(pre_trajectory["prediction_artifact_path"])).expanduser()
    model_path = Path(str(pre_trajectory["calibration_model_path"])).expanduser()
    hardware_path = Path(str(pre_trajectory["hardware_profile_path"])).expanduser()
    for path, label in (
        (prediction_path, "adaptive E2E prediction artifact"),
        (model_path, "adaptive calibration model"),
        (hardware_path, "adaptive hardware profile"),
    ):
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise AuditError(f"{label} path is not a regular absolute file")
    try:
        hardware_value, hardware_digest = read_adaptive_hashed_json(
            hardware_path,
            kind="adaptive hardware profile",
        )
        calibration_model = FrozenCalibrationModel.load(model_path)
        prediction_value, prediction_digest = verify_trajectory_prediction(
            prediction_path,
            calibration_model,
            run_id=run_id,
            hardware=HardwareProfile.from_mapping(hardware_value).to_mapping(),
        )
    except (AdaptiveProtocolError, EventSimulatorError) as exc:
        raise AuditError(f"adaptive E2E prediction provenance is invalid: {exc}") from exc
    if hardware_digest != normalized_binding["hardware_profile_sha256"]:
        raise AuditError("adaptive hardware profile hash does not match trajectory binding")
    if calibration_model.bindings() != {
        "split_manifest_sha256": normalized_binding["split_manifest_sha256"],
        "runtime_manifest_sha256": normalized_binding["runtime_manifest_sha256"],
        "hardware_profile_sha256": normalized_binding["hardware_profile_sha256"],
        "model_revision_sha256": normalized_binding["model_revision_sha256"],
    }:
        raise AuditError("adaptive calibration model bindings do not match trajectory binding")
    if calibration_model.sha256 != normalized_binding["calibration_model_sha256"]:
        raise AuditError("adaptive calibration model hash does not match trajectory binding")
    if prediction_digest != pre_trajectory["prediction_artifact_sha256"]:
        raise AuditError("adaptive E2E prediction artifact hash does not match trajectory arm")
    if float(prediction_value["predicted_ms"]) != float(pre_trajectory["predicted_ms"]):
        raise AuditError("adaptive trajectory arm prediction differs from its frozen artifact")

    records = _read_adaptive_journal(journal_path, normalized_binding, run_id)
    predictions = [record for record in records if record["record_type"] == "event_prediction"]
    labels = [record for record in records if record["record_type"] == "event_label"]
    trajectory = next(record for record in records if record["record_type"] == "trajectory_label")
    if not predictions or len(labels) != len(predictions):
        raise AuditError("adaptive holdout must contain complete non-empty event prediction/label coverage")

    if manifest.get("schema_version") != ADAPTIVE_MANIFEST_SCHEMA or manifest.get("protocol_schema") != ADAPTIVE_PROTOCOL_SCHEMA:
        raise AuditError("adaptive prediction manifest has an unsupported schema")
    if manifest.get("provenance") != "calibration_only_adaptive_pre_event":
        raise AuditError("adaptive prediction manifest is not calibration-only pre-event evidence")
    if manifest.get("frozen_before_trajectory_e2e_label") is not True or manifest.get("labels_accessed") is not False:
        raise AuditError("adaptive prediction manifest does not prove freeze before trajectory/E2E reveal")
    if manifest.get("binding") != normalized_binding or manifest.get("arm_sha256") != arm_digest or manifest.get("run_id") != run_id:
        raise AuditError("adaptive prediction manifest binding changed")
    if manifest.get("pre_trajectory_e2e") != pre_trajectory:
        raise AuditError("adaptive prediction manifest E2E prediction differs from the armed trajectory")
    if manifest.get("event_count") != len(predictions):
        raise AuditError("adaptive prediction manifest event count is incorrect")
    if manifest.get("trajectory_predictions") != [{"run_id": run_id, **pre_trajectory}]:
        raise AuditError("adaptive prediction manifest trajectory prediction is invalid")
    expected_predictions = [
        {
            "kind": record["kind"],
            "identifier": record["identifier"],
            "run_id": record["run_id"],
            "event_ordinal": record["event_ordinal"],
            "predicted_ms": record["prediction"]["predicted_ms"],
            "feature_sha256": record["feature_sha256"],
            "prediction_record_sha256": record["record_sha256"],
        }
        for record in predictions
    ]
    if manifest.get("event_predictions") != expected_predictions:
        raise AuditError("adaptive prediction manifest does not exactly match pre-event journal records")
    if trajectory.get("prediction_manifest_sha256") != manifest_digest:
        raise AuditError("adaptive trajectory/E2E reveal is not bound to the frozen prediction manifest")

    score_digest = _verify_sidecar(
        score_path,
        _recognized_sidecar(score_path, "adaptive score artifact"),
        "adaptive score artifact",
    )
    score = _read_json(score_path, "adaptive score artifact")
    event_scores: list[dict[str, Any]] = []
    unavailable = 0
    prediction_by_id = {record["identifier"]: record for record in predictions}
    label_by_id = {record["identifier"]: record for record in labels}
    for identifier in sorted(prediction_by_id):
        prediction = prediction_by_id[identifier]
        label = label_by_id[identifier]["label"]
        if label["status"] == "unavailable":
            unavailable += 1
            event_scores.append({"kind": prediction["kind"], "identifier": identifier, "status": "unavailable"})
            continue
        predicted = _positive_adaptive(prediction["prediction"]["predicted_ms"], "event predicted_ms")
        observed = _positive_adaptive(label["observed_ms"], "event observed_ms")
        ape = abs(predicted - observed) / observed * 100.0
        event_scores.append({
            "kind": prediction["kind"], "identifier": identifier, "status": "completed",
            "predicted_ms": predicted, "observed_ms": observed,
            "absolute_percentage_error": ape, "within_25_percent": ape <= GATE_PERCENT,
        })
    trajectory_predicted = _positive_adaptive(pre_trajectory["predicted_ms"], "trajectory predicted_ms")
    trajectory_observed = _positive_adaptive(trajectory["label"]["observed_ms"], "trajectory observed_ms")
    trajectory_ape = abs(trajectory_predicted - trajectory_observed) / trajectory_observed * 100.0
    expected_score = {
        "schema_version": ADAPTIVE_SCORE_SCHEMA,
        "prediction_manifest_sha256": manifest_digest,
        "target_gate_percent": GATE_PERCENT,
        "coverage_complete": True,
        "event_scores": event_scores,
        "unavailable_event_count": unavailable,
        "trajectory_score": {
            "run_id": run_id, "predicted_ms": trajectory_predicted, "observed_ms": trajectory_observed,
            "absolute_percentage_error": trajectory_ape, "within_25_percent": trajectory_ape <= GATE_PERCENT,
        },
        # Availability is itself part of coverage.  An unavailable event may
        # be reported, but it cannot be silently treated as a passing score.
        "passed": unavailable == 0 and bool(event_scores) and all(
            row.get("within_25_percent") is True for row in event_scores
        ) and trajectory_ape <= GATE_PERCENT,
    }
    if score != expected_score:
        raise AuditError("adaptive score artifact does not exactly match independent journal recomputation")
    if score["passed"] is not True:
        raise AuditError("adaptive score artifact does not pass the 25% individual-event and trajectory/E2E gates")
    tool_count = sum(row["kind"] == "tool" for row in event_scores)
    model_count = sum(row["kind"] == "model" for row in event_scores)
    if not tool_count or not model_count:
        raise AuditError("adaptive score artifact must report both tool and model event APEs")
    return {
        "passed": True,
        "protocol_type": "adaptive_live_holdout",
        "root": str(root.resolve()),
        "binding": normalized_binding,
        "trajectory_arm_sha256": arm_digest,
        "prediction_manifest_sha256": manifest_digest,
        "score_sha256": score_digest,
        "event_counts": {"tool": tool_count, "model": model_count, "unavailable": unavailable},
        "trajectory_ape_percent": trajectory_ape,
        "gate_percent": GATE_PERCENT,
    }


def audit_completion(args: argparse.Namespace) -> dict[str, Any]:
    failures: list[str] = []
    checks: dict[str, Any] = {}
    plan_result: dict[str, Any] | None = None
    protocol_types = ["static_historical"]

    def run(name: str, function: Callable[[], dict[str, Any]]) -> None:
        try:
            checks[name] = function()
        except (AuditError, OSError, KeyError, TypeError, ValueError) as exc:
            failures.append(f"{name}: {exc}")
            checks[name] = {"passed": False, "error": str(exc)}

    def plan_check() -> dict[str, Any]:
        nonlocal plan_result
        plan_result = _audit_plan(args.plan, args.plan_sha256)
        return plan_result

    run("sealed_plan", plan_check)
    run(
        "reconciliation",
        lambda: _audit_reconciliation(
            args.reconciliation_report,
            args.plan,
            (plan_result or {}).get("sha256", ""),
            args.trajectories,
            (plan_result or {}).get("case_count", -1),
        ),
    )
    run(
        "dataset_inventory",
        lambda: _audit_inventory(
            args.inventory,
            args.inventory_sha256,
            args.trajectories,
            args.tool_events,
            args.model_events,
            args.sweep_runs,
        ),
    )
    run("step3_selection", lambda: _audit_step3(args.step3_selection, args.trajectories))
    inventory_result = checks.get("dataset_inventory", {})
    reconciliation_result = checks.get("reconciliation", {})
    run(
        "figures",
        lambda: _audit_figures(
            args.figures_report,
            args.trajectories,
            args.tool_events,
            args.model_events,
            args.sweep_runs,
            inventory_result,
            reconciliation_result,
        ),
    )
    run(
        "event_e2e_evaluation",
        lambda: _audit_evaluation(
            args.evaluation_report,
            args.prediction_manifest,
            args.holdout_labels,
            args.prepare_receipt,
        ),
    )
    adaptive_root = getattr(args, "adaptive_holdout_root", None)
    adaptive_score = getattr(args, "adaptive_score_report", None)
    if (adaptive_root is None) != (adaptive_score is None):
        run(
            "adaptive_holdout_protocol",
            lambda: (_ for _ in ()).throw(
                AuditError(
                    "adaptive holdout auditing requires both --adaptive-holdout-root and --adaptive-score-report"
                )
            ),
        )
    elif adaptive_root is not None:
        protocol_types.append("adaptive_live_holdout")
        run(
            "adaptive_holdout_protocol",
            lambda: _audit_adaptive_holdout(adaptive_root, adaptive_score),
        )
    failures.sort()
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "passed" if not failures else "blocked",
        "passed": not failures,
        "protocol_types": protocol_types,
        "checks": checks,
        "failures": failures,
    }


def _atomic_write(path: Path, payload: bytes, *, force: bool) -> None:
    if path.exists() and not force:
        raise AuditError(f"refusing to overwrite {path}; pass --force")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--plan-sha256", required=True, type=Path)
    parser.add_argument("--reconciliation-report", required=True, type=Path)
    parser.add_argument("--trajectories", required=True, type=Path)
    parser.add_argument("--tool-events", required=True, type=Path)
    parser.add_argument("--model-events", required=True, type=Path)
    parser.add_argument("--sweep-runs", required=True, type=Path)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--inventory-sha256", required=True, type=Path)
    parser.add_argument("--step3-selection", required=True, type=Path)
    parser.add_argument("--figures-report", required=True, type=Path)
    parser.add_argument("--prediction-manifest", required=True, type=Path)
    parser.add_argument("--holdout-labels", required=True, type=Path)
    parser.add_argument("--prepare-receipt", required=True, type=Path)
    parser.add_argument("--evaluation-report", required=True, type=Path)
    parser.add_argument(
        "--adaptive-holdout-root",
        type=Path,
        help="optional completed live adaptive-holdout protocol root; audited independently of the static historical protocol",
    )
    parser.add_argument(
        "--adaptive-score-report",
        type=Path,
        help="frozen native adaptive score JSON (requires --adaptive-holdout-root)",
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--output-sha256-sidecar", type=Path)
    parser.add_argument("--force", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    sidecar = args.output_sha256_sidecar
    input_paths = {
        path.resolve()
        for path in (
            args.plan,
            args.plan_sha256,
            args.reconciliation_report,
            args.trajectories,
            args.tool_events,
            args.model_events,
            args.sweep_runs,
            args.inventory,
            args.inventory_sha256,
            args.step3_selection,
            args.figures_report,
            args.prediction_manifest,
            Path(f"{args.prediction_manifest}.sha256"),
            args.holdout_labels,
            args.holdout_labels.with_suffix(".sha256"),
            args.prepare_receipt,
            args.prepare_receipt.with_suffix(".sha256"),
            args.evaluation_report,
            args.evaluation_report.with_suffix(".sha256"),
        )
    }
    if args.adaptive_holdout_root is not None:
        input_paths.update({
            (args.adaptive_holdout_root / "trajectory_arm.json").resolve(),
            (args.adaptive_holdout_root / "trajectory_arm.sha256").resolve(),
            (args.adaptive_holdout_root / "adaptive_events.jsonl").resolve(),
            (args.adaptive_holdout_root / "adaptive_prediction_manifest.json").resolve(),
            (args.adaptive_holdout_root / "adaptive_prediction_manifest.sha256").resolve(),
        })
    if args.adaptive_score_report is not None:
        input_paths.update({
            args.adaptive_score_report.resolve(),
            Path(str(args.adaptive_score_report) + ".sha256").resolve(),
            args.adaptive_score_report.with_suffix(".sha256").resolve(),
        })
    output_paths = {args.output.resolve()}
    if sidecar is not None:
        output_paths.add(sidecar.resolve())
    if input_paths & output_paths:
        print("assignment completion audit: BLOCKED: output aliases an input artifact", file=sys.stderr)
        return 2
    try:
        if not args.force:
            if args.output.exists() or (sidecar is not None and sidecar.exists()):
                raise AuditError("refusing to overwrite audit output; pass --force")
        report = audit_completion(args)
        payload = (json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
        _atomic_write(args.output, payload, force=args.force)
        if sidecar is not None:
            digest = hashlib.sha256(payload).hexdigest()
            sidecar_payload = f"{digest}  {args.output.name}\n".encode("utf-8")
            _atomic_write(sidecar, sidecar_payload, force=args.force)
        print(json.dumps(report, sort_keys=True))
        return 0 if report["passed"] else 1
    except (AuditError, OSError) as exc:
        print(f"assignment completion audit: BLOCKED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
