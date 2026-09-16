#!/usr/bin/env python3
"""Evaluate one bounded class-specific composite CPU procedure.

The five base candidates and their fixed outer diagnostics remain unchanged.
This sixth bounded procedure chooses one of those base candidates separately
for each original class using only three inner instance folds, with the same
class-specific worst-APE constraint as the locked protocol.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from cpu_event_predictor import (  # noqa: E402
    CANDIDATES,
    COMPLEXITY,
    GATE_PCT,
    INNER_FOLDS,
    CpuEventPredictor,
    choose_candidate,
    inner_fold,
    metric_rows,
)
from run_cpu_event_models import (  # noqa: E402
    DEFAULT_LABELS,
    DEFAULT_PRODUCTION_MANIFEST,
    DEFAULT_VIEW,
    DEFAULT_VIEW_MANIFEST,
    _aggregate_predictions,
    _hash,
    _load_labels,
    _load_rows,
    _load_train_instances,
    _predict_rows,
    _write_new,
    _write_predictions,
)


def _class_metrics(predictions: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    by_class: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in predictions:
        by_class[str(row.get("original_class") or "unknown")].append(row)
    return {name: metric_rows(items, include_breakdowns=False) for name, items in sorted(by_class.items())}


def _choose_class(metrics_by_candidate: Mapping[str, Mapping[str, Any]]) -> str:
    baseline = metrics_by_candidate["coarse_class_median"]
    eligible = {
        name: metrics
        for name, metrics in metrics_by_candidate.items()
        if float(metrics.get("worst_ape_pct", math.inf)) <= float(baseline.get("worst_ape_pct", math.inf)) + 1e-12
    }
    if not eligible:
        return "coarse_class_median"
    return min(
        eligible,
        key=lambda name: (
            -float(eligible[name].get("within25_rate", 0.0)),
            float(eligible[name].get("worst_ape_pct", math.inf)),
            COMPLEXITY.get(name, 999),
            name,
        ),
    )


def _inner_class_selection(rows: Sequence[Mapping[str, Any]]) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    predictions: dict[str, list[dict[str, Any]]] = {candidate: [] for candidate in CANDIDATES}
    for fold in range(INNER_FOLDS):
        train = [row for row in rows if inner_fold(row["instance_id"]) != fold]
        valid = [row for row in rows if inner_fold(row["instance_id"]) == fold]
        for candidate in CANDIDATES:
            model = CpuEventPredictor(candidate).fit(train)
            predictions[candidate].extend(_predict_rows(model, valid))
    class_metrics_by_candidate: dict[str, dict[str, Any]] = {
        candidate: _class_metrics(predicted) for candidate, predicted in predictions.items()
    }
    classes = sorted({str(row["original_class"]) for row in rows})
    selected: dict[str, str] = {}
    for class_name in classes:
        selected[class_name] = _choose_class({candidate: class_metrics_by_candidate[candidate][class_name] for candidate in CANDIDATES})
    return selected, class_metrics_by_candidate


def _hybrid_predict(
    rows: Sequence[Mapping[str, Any]], models: Mapping[str, CpuEventPredictor], class_choice: Mapping[str, str], outer_fold: int | None = None
) -> list[dict[str, Any]]:
    predictions: list[dict[str, Any]] = []
    for row in rows:
        class_name = str(row.get("original_class") or "unknown")
        candidate = class_choice.get(class_name, "coarse_class_median")
        detail = models[candidate].predict_details(row)
        out = dict(row)
        out.update(detail)
        out["selected_candidate"] = candidate
        if outer_fold is not None:
            out["outer_fold"] = outer_fold
        predictions.append(out)
    return predictions


def _outer_hybrid(rows: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    all_predictions: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    for outer in range(5):
        train = [row for row in rows if row["fold"] != outer]
        test = [row for row in rows if row["fold"] == outer]
        class_choice, inner_metrics = _inner_class_selection(train)
        models = {candidate: CpuEventPredictor(candidate).fit(train) for candidate in CANDIDATES}
        predictions = _hybrid_predict(test, models, class_choice, outer)
        all_predictions.extend(predictions)
        audit.append(
            {
                "outer_fold": outer,
                "selected_by_class": class_choice,
                "inner_metrics_by_candidate_and_class": inner_metrics,
                "outer_test_metrics": _aggregate_predictions(predictions),
            }
        )
    return _aggregate_predictions(all_predictions), audit, all_predictions


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--labels", type=Path, default=DEFAULT_LABELS)
    parser.add_argument("--view", type=Path, default=DEFAULT_VIEW)
    parser.add_argument("--view-manifest", type=Path, default=DEFAULT_VIEW_MANIFEST)
    parser.add_argument("--production-manifest", type=Path, default=DEFAULT_PRODUCTION_MANIFEST)
    parser.add_argument("--output-dir", type=Path, default=HERE)
    args = parser.parse_args()
    train_instances = _load_train_instances(args.production_manifest)
    labels = _load_labels(args.labels, train_instances)
    rows, view_meta = _load_rows(args.view, args.view_manifest, labels, train_instances)
    hybrid_metrics, audit, predictions = _outer_hybrid(rows)
    final_choice, final_inner = _inner_class_selection(rows)
    final_models = {candidate: CpuEventPredictor(candidate).fit(rows) for candidate in CANDIDATES}
    artifact = {
        "schema_version": "assignment.offline-cpu-class-hybrid.v1",
        "artifact_kind": "offline_candidate_only",
        "production_adoption": "not_authorized",
        "class_candidate_map": final_choice,
        "models": {candidate: model.to_mapping() for candidate, model in final_models.items()},
        "selection_rule": "per original class: maximum inner within25 subject to worst APE <= coarse class median for that class; ties lower worst APE then simpler candidate",
        "data": {"events": len(rows), "instances": len({row["instance_id"] for row in rows})},
        "feature_contract": {
            "mode": "pre_event_only",
            "extractor_id": "offline-cpu-semantic-whitelist.v1",
            "fields": [
                "semantic_class",
                "operation",
                "executable",
                "runner",
                "execution_mode",
                "git_pager_susceptibility",
                "find_exec_mode",
                "pipeline_stage_bucket",
                "recursive",
                "operand_count_bucket",
                "declared_command_bytes_bucket",
                "python_imports_bucket",
                "declared_work_bucket",
            ],
            "forbidden": [
                "exact_command",
                "command_sha256",
                "case_id",
                "repository_identity",
                "current_duration",
                "output_tokens",
                "return_bytes",
                "future_state",
                "failure_or_end_state",
                "measured_residual",
            ],
        },
        "target_contract": {
            "target": "historical tool-event wall duration observed_ms",
            "units": "milliseconds",
            "cpu_sum": "sum of predicted/observed historical tool-event durations over the retained event list",
        },
        "hardware_transfer": {
            "status": "unvalidated",
            "protocol": "retain descriptors and require paired destination-hardware measurements; infer no CPU-frequency/core/bandwidth scaling law",
        },
    }
    comparison = {
        "schema_version": "assignment.offline-cpu-class-hybrid-comparison.v1",
        "status": "complete_bounded_sixth_composite_diagnostic",
        "candidate_count_including_hybrid": 6,
        "rationale": "After the five fixed-candidate diagnostics, this bounded sixth composite tests whether compact classes can use a higher-coverage center while retaining the class-specific coarse tail constraint; it was not part of the initial fixed candidate list and is reported separately.",
        "data": {"event_count": len(rows), "instance_count": len({row["instance_id"] for row in rows}), "run_count": len({row["run_id"] for row in rows if row.get("run_id")})},
        "provenance": {
            "labels_path": str(args.labels),
            "labels_sha256": _hash(args.labels),
            "view_path": str(args.view),
            "view_manifest_path": str(args.view_manifest),
            "production_manifest_path": str(args.production_manifest),
            "production_manifest_sha256": _hash(args.production_manifest),
            "view": view_meta,
            "prior_access_disclosure": "Earlier D9 forensic scripts bulk-read mixed historical objects before filtering; this artifact uses only the common train view and does not claim pristine blindness.",
        },
        "outer_nested_hybrid": hybrid_metrics,
        "outer_fold_audit": audit,
        "final_inner_selection": {"class_candidate_map": final_choice, "metrics_by_candidate_and_class": final_inner},
        "comparison_to_global_coarse": {
            "within25_delta": hybrid_metrics["within25_rate"] - 0.6774360077436008,
            "worst_ape_pct": hybrid_metrics["worst_ape_pct"],
        },
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    _write_new(args.output_dir / "class_hybrid_comparison.json", json.dumps(comparison, indent=2, sort_keys=True) + "\n")
    _write_new(args.output_dir / "class_hybrid_model.json", json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    _write_predictions(args.output_dir / "class_hybrid_predictions.csv", predictions)
    print(json.dumps({"class_candidate_map": final_choice, "within25_rate": hybrid_metrics["within25_rate"], "worst_ape_pct": hybrid_metrics["worst_ape_pct"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
