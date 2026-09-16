#!/usr/bin/env python3
"""Rank saved historical D9 CPU errors by action-level mechanisms.

This is a read-only descriptive analysis of the already completed D9 review
artifacts.  The frozen historical scope is built before any prediction/action
rows are read.  It does not fit a model, select a split, or authorize training.
The current combined-case fixture is intentionally not read here; it is an
independent conditional evidence source for the report.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


R = Path("/home/riverahernandezjason/agentic-submission-repairs-20260908")
A = Path("/home/riverahernandezjason/h100-assignment-work-20260905/assignment")
P = A / "submission/20260909T000000Z-resume/verification/astra-combined-preflight-20260909-9cP7NP"
D = A / "submission/20260908T060000Z/d9-cpu-review"
SCOPE_MODULE = P / "source-v8/scripts/assignment/historical_analysis_scope.py"


def _load_scope_module():
    spec = importlib.util.spec_from_file_location("d9_historical_scope", SCOPE_MODULE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load frozen scope: {SCOPE_MODULE}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _finite(value: Any) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"non-finite metric value: {value!r}")
    return number


def _p95(values: Iterable[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    return ordered[int(round(0.95 * (len(ordered) - 1)))]


def _metrics(rows: list[tuple[dict[str, Any], dict[str, Any]]]) -> dict[str, Any]:
    if not rows:
        return {
            "n": 0,
            "miss_count": 0,
            "within25_rate": None,
            "abs_error_ms": 0.0,
            "observed_ms": 0.0,
            "signed_bias_ms": 0.0,
            "mean_ape_pct": None,
            "median_ape_pct": None,
            "p95_ape_pct": None,
            "max_ape_pct": None,
        }
    errors = []
    apes = []
    misses = 0
    signed = 0.0
    observed_total = 0.0
    for row, _baseline in rows:
        observed = _finite(row["observed_ms"])
        predicted = _finite(row["predicted_ms"])
        error = abs(predicted - observed)
        ape = error / observed * 100.0
        errors.append(error)
        apes.append(ape)
        signed += predicted - observed
        observed_total += observed
        misses += ape > 25.0
    return {
        "n": len(rows),
        "miss_count": misses,
        "within25_rate": 1.0 - misses / len(rows),
        "abs_error_ms": sum(errors),
        "observed_ms": observed_total,
        "signed_bias_ms": signed,
        "mean_ape_pct": statistics.mean(apes),
        "median_ape_pct": statistics.median(apes),
        "p95_ape_pct": _p95(apes),
        "max_ape_pct": max(apes),
    }


def _paired_metrics(rows: list[tuple[dict[str, Any], dict[str, Any]]]) -> dict[str, Any]:
    selected = _metrics(rows)
    baseline_rows = [(baseline, baseline) for _selected, baseline in rows]
    baseline = _metrics(baseline_rows)
    result = {"selected": selected, "baseline": baseline}
    result["delta_selected_minus_baseline"] = {
        "within25_pp": 100.0 * (selected["within25_rate"] - baseline["within25_rate"]),
        "miss_count": selected["miss_count"] - baseline["miss_count"],
        "abs_error_ms": selected["abs_error_ms"] - baseline["abs_error_ms"],
        "mean_ape_pp": selected["mean_ape_pct"] - baseline["mean_ape_pct"],
    }
    return result


def _feature_value(features: dict[str, Any], name: str) -> str:
    value = features.get(name)
    if isinstance(value, (list, tuple)):
        return ",".join(str(item) for item in value) or "none"
    if value is None or value == "":
        return "none"
    return str(value)


def _mechanism(features: dict[str, Any]) -> str:
    semantic = str(features.get("semantic_class") or "unknown")
    if semantic == "traversal":
        return "/".join(
            (
                "traversal",
                f"exe={_feature_value(features, 'executable')}",
                f"find_exec={_feature_value(features, 'find_exec_mode')}",
                f"pipe={_feature_value(features, 'pipeline_stage_bucket')}",
                f"recursive={_feature_value(features, 'recursive')}",
                f"mode={_feature_value(features, 'execution_mode')}",
            )
        )
    if semantic == "test":
        return "/".join(
            (
                "test",
                f"exe={_feature_value(features, 'executable')}",
                f"runner={_feature_value(features, 'runner')}",
                f"op={_feature_value(features, 'operation')}",
                f"mode={_feature_value(features, 'execution_mode')}",
            )
        )
    if semantic == "shell":
        return "/".join(
            (
                "shell",
                f"exe={_feature_value(features, 'executable')}",
                f"op={_feature_value(features, 'operation')}",
                f"pager={_feature_value(features, 'git_pager_susceptibility')}",
                f"mode={_feature_value(features, 'execution_mode')}",
            )
        )
    return "/".join(
        (
            semantic,
            f"exe={_feature_value(features, 'executable')}",
            f"op={_feature_value(features, 'operation')}",
        )
    )


def _load_rows(scope, semantic_features) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    actions = json.loads((D / "calibration_actions.json").read_text())
    selected = json.loads((D / "predictions_instance_id_grouped_semantic_repo_median.json").read_text())["tool_events"]
    baseline = json.loads((D / "predictions_instance_id_grouped_baseline.json").read_text())["tool_events"]
    baseline_by_id = {row["event_id"]: row for row in baseline}
    rows: list[dict[str, Any]] = []
    skipped = {"non_cpu": 0, "excluded": 0, "scope_error": 0, "missing_action": 0}
    for selected_row in selected:
        if selected_row.get("channel") != "cpu":
            skipped["non_cpu"] += 1
            continue
        identity = {key: selected_row[key] for key in ("instance_id", "run_id") if selected_row.get(key)}
        try:
            eligible = scope.is_eligible(identity)
        except Exception:
            skipped["scope_error"] += 1
            continue
        if not eligible:
            skipped["excluded"] += 1
            continue
        event_id = selected_row["event_id"]
        action = actions.get(event_id)
        if action is None:
            skipped["missing_action"] += 1
            continue
        baseline_row = baseline_by_id[event_id]
        features = semantic_features(action, selected_row.get("repository", ""))
        rows.append(
            {
                "selected": selected_row,
                "baseline": baseline_row,
                "features": features,
                "original_class": str(selected_row.get("original_class") or "unknown"),
                "semantic_class": str(features.get("semantic_class") or "unknown"),
                "action_mechanism": _mechanism(features),
            }
        )
    return rows, skipped


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-csv", type=Path, required=True)
    args = parser.parse_args()

    # Construct and validate the denylist before opening prediction/action
    # labels.  This boundary is intentionally separate from training policy.
    scope_module = _load_scope_module()
    scope = scope_module.frozen_scope()
    from agentic_sim.assignment.semantic_cpu_model import semantic_features

    rows, skipped = _load_rows(scope, semantic_features)
    if len(rows) != 24426:
        raise RuntimeError(f"unexpected eligible CPU event count: {len(rows)}")

    def grouped(name: str) -> list[dict[str, Any]]:
        groups: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
        for row in rows:
            if name == "original_class":
                key = row["original_class"]
            elif name == "semantic_class":
                key = row["semantic_class"]
            elif name == "action_mechanism":
                key = row["action_mechanism"]
            else:
                key = _feature_value(row["features"], name)
            groups[key].append((row["selected"], row["baseline"]))
        total_abs = _metrics([(row["selected"], row["baseline"]) for row in rows])["abs_error_ms"]
        total_miss = _metrics([(row["selected"], row["baseline"]) for row in rows])["miss_count"]
        output = []
        for key, pairs in groups.items():
            if len(pairs) < 5:
                continue
            metrics = _paired_metrics(pairs)
            metrics.update(
                {
                    "group": key,
                    "grouping": name,
                    "error_mass_share_pct": metrics["selected"]["abs_error_ms"] / total_abs * 100.0,
                    "miss_share_pct": metrics["selected"]["miss_count"] / total_miss * 100.0,
                }
            )
            output.append(metrics)
        output.sort(key=lambda item: item["selected"]["abs_error_ms"], reverse=True)
        return output

    all_pairs = [(row["selected"], row["baseline"]) for row in rows]
    result = {
        "schema_version": "assignment.d9-atomic-error-analysis.v1",
        "scope": scope.artifact(),
        "analysis": {
            "purpose": "historical descriptive error ranking; no refit and no holdout claim",
            "protocol": "instance_id_grouped",
            "candidate_selected": "semantic_repo_median",
            "candidate_baseline": "original_hierarchical_baseline",
            "minimum_group_n": 5,
            "fixed_gate_pct": 25,
            "eligible_cpu_events": len(rows),
            "eligible_trajectories": len({row["selected"]["run_id"] for row in rows}),
            "eligible_instances": len({row["selected"]["instance_id"] for row in rows}),
            "skipped": skipped,
            "source_hashes": {
                "selected_predictions": _sha256(D / "predictions_instance_id_grouped_semantic_repo_median.json"),
                "baseline_predictions": _sha256(D / "predictions_instance_id_grouped_baseline.json"),
                "actions": _sha256(D / "calibration_actions.json"),
                "scope_script": _sha256(SCOPE_MODULE),
            },
        },
        "overall": _paired_metrics(all_pairs),
        "groups": {
            "original_class": grouped("original_class"),
            "semantic_class": grouped("semantic_class"),
            "action_mechanism": grouped("action_mechanism"),
            "executable": grouped("executable"),
            "operation": grouped("operation"),
            "find_exec_mode": grouped("find_exec_mode"),
            "git_pager_susceptibility": grouped("git_pager_susceptibility"),
            "runner": grouped("runner"),
            "execution_mode": grouped("execution_mode"),
        },
    }
    for path in (args.output_json, args.output_csv):
        if path.exists():
            raise RuntimeError(f"refusing to overwrite: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)

    args.output_json.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    fields = [
        "grouping",
        "group",
        "n",
        "miss_count",
        "within25_rate",
        "abs_error_ms",
        "error_mass_share_pct",
        "miss_share_pct",
        "mean_ape_pct",
        "median_ape_pct",
        "p95_ape_pct",
        "max_ape_pct",
        "delta_within25_pp",
        "delta_abs_error_ms",
        "delta_mean_ape_pp",
    ]
    with args.output_csv.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for grouping, entries in result["groups"].items():
            for entry in entries:
                row = {
                    "grouping": grouping,
                    "group": entry["group"],
                    "n": entry["selected"]["n"],
                    "miss_count": entry["selected"]["miss_count"],
                    "within25_rate": entry["selected"]["within25_rate"],
                    "abs_error_ms": entry["selected"]["abs_error_ms"],
                    "error_mass_share_pct": entry["error_mass_share_pct"],
                    "miss_share_pct": entry["miss_share_pct"],
                    "mean_ape_pct": entry["selected"]["mean_ape_pct"],
                    "median_ape_pct": entry["selected"]["median_ape_pct"],
                    "p95_ape_pct": entry["selected"]["p95_ape_pct"],
                    "max_ape_pct": entry["selected"]["max_ape_pct"],
                    "delta_within25_pp": entry["delta_selected_minus_baseline"]["within25_pp"],
                    "delta_abs_error_ms": entry["delta_selected_minus_baseline"]["abs_error_ms"],
                    "delta_mean_ape_pp": entry["delta_selected_minus_baseline"]["mean_ape_pp"],
                }
                writer.writerow(row)
    print(args.output_json)
    print(args.output_csv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
