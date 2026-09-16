#!/usr/bin/env python3
"""One prespecified CPU descriptor experiment on train_calibration only.

The experiment compares a plain original-class median with a deterministic
descriptor median that backs off from an action mechanism to operation,
semantic class, original class, and finally the global median.  Five folds are
grouped by instance with a fixed SHA-256 assignment.  It is a small diagnostic
of whether available action descriptors explain duration classes; it is not a
production fit or a holdout evaluation.
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
from typing import Any


A = Path("/home/riverahernandezjason/h100-assignment-work-20260905/assignment")
P = A / "submission/20260909T000000Z-resume/verification/astra-combined-preflight-20260909-9cP7NP"
D = A / "submission/20260908T060000Z/d9-cpu-review"
SCOPE_MODULE = P / "source-v8/scripts/assignment/historical_analysis_scope.py"
MANIFEST = A / "submission/20260908T140000Z-offline-v2/live-plan/production_split_manifest.v2.json"
FOLDS = 5
MIN_EVENTS = 25
MIN_INSTANCES = 3


def _load_scope_module():
    spec = importlib.util.spec_from_file_location("d9_experiment_scope", SCOPE_MODULE)
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


def _fold(instance_id: str) -> int:
    digest = hashlib.sha256(("assignment.d9.train-fold-v1:" + instance_id).encode()).digest()
    return int.from_bytes(digest[:8], "big") % FOLDS


def _median(values: list[float]) -> float:
    return float(statistics.median(values))


def _key_value(features: dict[str, Any], key: str) -> str:
    value = features.get(key)
    if isinstance(value, (list, tuple)):
        return ",".join(str(item) for item in value) or "none"
    return "none" if value is None or value == "" else str(value)


def _mechanism(features: dict[str, Any]) -> str:
    semantic = _key_value(features, "semantic_class")
    if semantic == "traversal":
        fields = ("executable", "find_exec_mode", "pipeline_stage_bucket", "recursive", "execution_mode")
    elif semantic == "test":
        fields = ("executable", "runner", "operation", "execution_mode")
    elif semantic == "shell":
        fields = ("executable", "operation", "git_pager_susceptibility", "execution_mode")
    else:
        fields = ("executable", "operation")
    return semantic + "|" + "|".join(f"{key}={_key_value(features, key)}" for key in fields)


def _metric(rows: list[dict[str, Any]], prediction_key: str) -> dict[str, Any]:
    apes = []
    absolute = 0.0
    signed = 0.0
    observed = 0.0
    miss = 0
    for row in rows:
        target = float(row["observed_ms"])
        prediction = float(row[prediction_key])
        error = abs(prediction - target)
        ape = error / target * 100.0
        apes.append(ape)
        absolute += error
        signed += prediction - target
        observed += target
        miss += ape > 25.0
    if not rows:
        return {"n": 0, "miss_count": 0, "within25_rate": None}
    ordered = sorted(apes)
    return {
        "n": len(rows),
        "miss_count": miss,
        "within25_rate": 1.0 - miss / len(rows),
        "mean_ape_pct": statistics.mean(apes),
        "median_ape_pct": statistics.median(apes),
        "p95_ape_pct": ordered[int(round(0.95 * (len(ordered) - 1)))],
        "max_ape_pct": max(apes),
        "abs_error_ms": absolute,
        "observed_ms": observed,
        "signed_bias_ms": signed,
    }


def _train_table(rows: list[dict[str, Any]], key: str, fold: int | None) -> dict[str, tuple[float, int, int]]:
    values: dict[str, list[float]] = defaultdict(list)
    instances: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        if fold is not None and row["fold"] == fold:
            continue
        group = row[key]
        values[group].append(row["observed_ms"])
        instances[group].add(row["instance_id"])
    return {group: (_median(items), len(items), len(instances[group])) for group, items in values.items()}


def _predict(row: dict[str, Any], tables: dict[str, dict[str, tuple[float, int, int]]], global_median: float, stratified: bool) -> float:
    if stratified:
        for key in ("mechanism", "operation", "semantic_class", "original_class"):
            item = tables[key].get(row[key])
            if item and item[1] >= MIN_EVENTS and item[2] >= MIN_INSTANCES:
                return item[0]
    item = tables["original_class"].get(row["original_class"])
    if item:
        return item[0]
    return global_median


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-csv", type=Path, required=True)
    args = parser.parse_args()

    # Derive the denylist before opening any prediction/action labels.
    scope_module = _load_scope_module()
    scope = scope_module.frozen_scope()
    manifest = json.loads(MANIFEST.read_text())
    train_instances = {
        row["instance_id"]
        for row in manifest["clusters"]
        if row.get("partition") == "train_calibration"
    }
    if len(train_instances) != 546:
        raise RuntimeError(f"unexpected train_calibration cluster count: {len(train_instances)}")
    if train_instances & scope.excluded_instance_ids:
        raise RuntimeError("frozen denylist overlaps train_calibration")

    from agentic_sim.assignment.semantic_cpu_model import semantic_features

    actions = json.loads((D / "calibration_actions.json").read_text())
    selected = json.loads((D / "predictions_instance_id_grouped_semantic_repo_median.json").read_text())["tool_events"]
    rows: list[dict[str, Any]] = []
    skipped = defaultdict(int)
    for source in selected:
        if source.get("channel") != "cpu":
            continue
        instance = source.get("instance_id")
        if instance not in train_instances:
            skipped["non_train_calibration"] += 1
            continue
        identity = {key: source[key] for key in ("instance_id", "run_id") if source.get(key)}
        if not scope.is_eligible(identity):
            skipped["excluded"] += 1
            continue
        action = actions.get(source["event_id"])
        if action is None:
            skipped["missing_action"] += 1
            continue
        features = semantic_features(action, source.get("repository", ""))
        row = {
            "event_id": source["event_id"],
            "instance_id": instance,
            "fold": _fold(instance),
            "observed_ms": float(source["observed_ms"]),
            "original_class": str(source.get("original_class") or "unknown"),
            "semantic_class": _key_value(features, "semantic_class"),
            "operation": _key_value(features, "operation"),
            "mechanism": _mechanism(features),
        }
        rows.append(row)

    if len(rows) < 10000:
        raise RuntimeError(f"unexpected train_calibration event count: {len(rows)}")
    all_predictions: list[dict[str, Any]] = []
    for fold in range(FOLDS):
        test_rows = [row for row in rows if row["fold"] == fold]
        train_rows = [row for row in rows if row["fold"] != fold]
        global_median = _median([row["observed_ms"] for row in train_rows])
        tables = {
            key: _train_table(rows, key, fold)
            for key in ("mechanism", "operation", "semantic_class", "original_class")
        }
        for row in test_rows:
            row["plain_prediction_ms"] = _predict(row, tables, global_median, stratified=False)
            row["stratified_prediction_ms"] = _predict(row, tables, global_median, stratified=True)
        all_predictions.extend(test_rows)

    result = {
        "schema_version": "assignment.d9-train-calibration-descriptor-experiment.v1",
        "scope": scope.artifact(),
        "experiment": {
            "purpose": "diagnostic comparison of fixed action descriptors; no production fit",
            "data_partition": "train_calibration only",
            "cluster_count": len(train_instances),
            "event_count": len(rows),
            "trajectory_count": len({row["instance_id"] for row in rows}),
            "folds": FOLDS,
            "fold_rule": "sha256('assignment.d9.train-fold-v1:' + instance_id) first 8 bytes modulo 5",
            "gate_pct": 25,
            "minimum_group_events": MIN_EVENTS,
            "minimum_group_instances": MIN_INSTANCES,
            "plain_candidate": "original_class_median with fixed instance folds",
            "stratified_candidate": "mechanism -> operation -> semantic_class -> original_class median backoff",
            "realized_work_or_duration_features": False,
            "skipped": dict(skipped),
            "source_hashes": {
                "manifest": _sha256(MANIFEST),
                "predictions": _sha256(D / "predictions_instance_id_grouped_semantic_repo_median.json"),
                "actions": _sha256(D / "calibration_actions.json"),
                "scope_script": _sha256(SCOPE_MODULE),
            },
        },
        "overall": {
            "plain": _metric(all_predictions, "plain_prediction_ms"),
            "stratified": _metric(all_predictions, "stratified_prediction_ms"),
        },
        "folds_detail": [
            {
                "fold": fold,
                "instances": len({row["instance_id"] for row in all_predictions if row["fold"] == fold}),
                "events": len([row for row in all_predictions if row["fold"] == fold]),
                "plain": _metric([row for row in all_predictions if row["fold"] == fold], "plain_prediction_ms"),
                "stratified": _metric([row for row in all_predictions if row["fold"] == fold], "stratified_prediction_ms"),
            }
            for fold in range(FOLDS)
        ],
        "original_class": {},
    }
    for class_name in sorted({row["original_class"] for row in all_predictions}):
        subset = [row for row in all_predictions if row["original_class"] == class_name]
        result["original_class"][class_name] = {
            "plain": _metric(subset, "plain_prediction_ms"),
            "stratified": _metric(subset, "stratified_prediction_ms"),
        }

    for path in (args.output_json, args.output_csv):
        if path.exists():
            raise RuntimeError(f"refusing to overwrite: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    fields = ["fold", "event_id", "instance_id", "original_class", "observed_ms", "plain_prediction_ms", "stratified_prediction_ms"]
    with args.output_csv.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({field: row[field] for field in fields} for row in all_predictions)
    print(args.output_json)
    print(args.output_csv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
