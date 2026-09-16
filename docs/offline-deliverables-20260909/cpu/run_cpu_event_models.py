#!/usr/bin/env python3
"""Fit and compare bounded leakage-safe CPU event model candidates.

This driver consumes the retained labels and the already identity-filtered
common train-calibration view.  It performs fixed five-fold outer diagnostics
and three-fold inner candidate selection without opening mixed heldout data.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from agentic_sim.assignment.semantic_cpu_model import semantic_features  # noqa: E402
from cpu_event_predictor import (  # noqa: E402
    CANDIDATES,
    EXTRACTOR_ID,
    GATE_PCT,
    INNER_FOLDS,
    MIN_EVENTS,
    MIN_INSTANCES,
    OUTER_FOLDS,
    ContractError,
    CpuEventPredictor,
    canonical_features,
    choose_candidate,
    event_ordinal,
    inner_fold,
    json_hash,
    mechanism_key,
    metric_rows,
    outer_fold,
)


DEFAULT_LABELS = REPO / "docs/retained-analysis-20260909/d9/train-calibration-descriptor-experiment.csv"
DEFAULT_VIEW = REPO / "docs/offline-deliverables-20260909/training_view/tools.jsonl"
DEFAULT_VIEW_MANIFEST = REPO / "docs/offline-deliverables-20260909/training_view/manifest.json"
DEFAULT_PRODUCTION_MANIFEST = Path(
    "/home/riverahernandezjason/h100-assignment-work-20260905/assignment/submission/20260908T140000Z-offline-v2/live-plan/production_split_manifest.v2.json"
)
EXPECTED_EVENTS = 23245
EXPECTED_INSTANCES = 545
EXPECTED_MANIFEST_INSTANCES = 546
_RAW_STRING = re.compile(r'"(?P<key>[A-Za-z_][A-Za-z0-9_]*)"\s*:\s*"(?P<value>(?:\\.|[^"\\])*)"')


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _raw_field(line: str, key: str) -> str | None:
    # Common-view rows are one top-level JSON object per line.  Read identity
    # strings from raw bytes first so an ineligible row is discarded before its
    # target/action object is decoded.
    for match in _RAW_STRING.finditer(line):
        if match.group("key") == key:
            return json.loads('"' + match.group("value") + '"')
    return None


def _load_train_instances(production_manifest: Path) -> set[str]:
    document = json.loads(production_manifest.read_text(encoding="utf-8"))
    if document.get("schema_version") != "assignment-production-instance-cluster-split.v2":
        raise ContractError("unexpected production manifest schema")
    clusters = document.get("clusters")
    if not isinstance(clusters, list):
        raise ContractError("production manifest clusters missing")
    train = {
        str(row["instance_id"])
        for row in clusters
        if isinstance(row, Mapping) and row.get("partition") == "train_calibration"
    }
    if len(clusters) != 707 or len(train) != EXPECTED_MANIFEST_INSTANCES:
        raise ContractError(f"unexpected manifest cluster/train counts: {len(clusters)}/{len(train)}")
    return train


def _load_labels(path: Path, train_instances: set[str]) -> dict[str, dict[str, Any]]:
    labels: dict[str, dict[str, Any]] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        required = {"event_id", "instance_id", "original_class", "observed_ms", "fold"}
        if not required.issubset(reader.fieldnames or ()):
            raise ContractError("label CSV lacks required fields")
        for line_number, raw in enumerate(reader, 2):
            event_id = str(raw.get("event_id") or "")
            instance_id = str(raw.get("instance_id") or "")
            if not event_id or not instance_id:
                raise ContractError(f"label row {line_number} missing identity")
            if instance_id not in train_instances:
                raise ContractError(f"label row outside train manifest: {instance_id}")
            if event_id in labels:
                raise ContractError(f"duplicate label event: {event_id}")
            observed = float(raw["observed_ms"])
            if not math.isfinite(observed) or observed <= 0:
                raise ContractError(f"invalid label at row {line_number}")
            fold = int(raw["fold"])
            if fold != outer_fold(instance_id):
                raise ContractError(f"existing fold disagrees with hash fold for {instance_id}")
            labels[event_id] = {
                "event_id": event_id,
                "instance_id": instance_id,
                "original_class": str(raw.get("original_class") or "unknown"),
                "observed_ms": observed,
                "fold": fold,
            }
    if len(labels) != EXPECTED_EVENTS:
        raise ContractError(f"unexpected retained event count: {len(labels)}")
    if len({row["instance_id"] for row in labels.values()}) != EXPECTED_INSTANCES:
        raise ContractError("unexpected retained instance count")
    return labels


def _safe_feature_fallback(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    allowed = (
        "operation_class",
        "original_class",
        "semantic_class",
        "operation",
        "subcommand",
        "tool_name",
        "launch_family",
        "runner",
        "execution_mode",
        "mode",
        "git_pager_susceptibility",
        "find_exec_mode",
        "pipeline_stage_bucket",
        "pipeline_stage_count",
        "n_pipes",
        "recursive",
        "operand_count_bucket",
        "operand_count",
        "declared_path_count",
        "declared_command_bytes",
        "byte_bucket",
    )
    return {key: raw[key] for key in allowed if key in raw}


def _load_rows(view_path: Path, view_manifest: Path, labels: Mapping[str, Mapping[str, Any]], train_instances: set[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    manifest = json.loads(view_manifest.read_text(encoding="utf-8"))
    if manifest.get("partition") != "train_calibration" or manifest.get("manifest_instances") != EXPECTED_MANIFEST_INSTANCES:
        raise ContractError("common view manifest is not the declared train-calibration view")
    output_hashes = manifest.get("output_hashes") or {}
    declared_view_hash = output_hashes.get("tools.jsonl")
    actual_view_hash = _hash(view_path)
    if declared_view_hash and declared_view_hash != actual_view_hash:
        raise ContractError("common tools view hash mismatch")
    rows: list[dict[str, Any]] = []
    skipped_before_decode = 0
    seen: set[str] = set()
    for line_number, line in enumerate(view_path.open(encoding="utf-8"), 1):
        if not line.strip():
            continue
        instance_id = _raw_field(line, "instance_id")
        if instance_id not in train_instances:
            skipped_before_decode += 1
            continue
        partition = _raw_field(line, "partition")
        if partition and partition != "train_calibration":
            skipped_before_decode += 1
            continue
        # Only authorized rows reach complete JSON/action decoding.
        raw = json.loads(line)
        event_id = str(raw.get("event_id") or "")
        if event_id in seen:
            raise ContractError(f"duplicate common-view event at line {line_number}: {event_id}")
        seen.add(event_id)
        label = labels.get(event_id)
        if label is None:
            raise ContractError(f"common-view event not in retained label CSV: {event_id}")
        if str(raw.get("instance_id") or "") != label["instance_id"]:
            raise ContractError(f"event/instance mismatch for {event_id}")
        common_observed = raw.get("observed_ms")
        if common_observed is not None and abs(float(common_observed) - float(label["observed_ms"])) > 1e-9:
            raise ContractError(f"common/CSV target mismatch for {event_id}")
        common_class = raw.get("operation_class", raw.get("original_class"))
        if common_class is not None and str(common_class) != label["original_class"]:
            raise ContractError(f"common/CSV class mismatch for {event_id}")
        if raw.get("outer_fold") is not None and int(raw["outer_fold"]) != label["fold"]:
            raise ContractError(f"common/CSV fold mismatch for {event_id}")
        action = raw.get("action")
        feature_status = "missing"
        feature_map: dict[str, Any] = {}
        if isinstance(action, str) and action.strip():
            try:
                # Empty repository context is intentional; repository identity
                # must not influence scope or a model key.
                semantic = semantic_features(action, "")
                feature_map = canonical_features(raw, semantic)
                feature_status = "ok"
            except (ContractError, TypeError, ValueError):
                feature_status = "invalid_action"
        else:
            try:
                fallback = _safe_feature_fallback(raw)
                if fallback:
                    feature_map = canonical_features(fallback, fallback)
                    feature_status = "partial"
            except ContractError:
                feature_status = "invalid_features"
        rows.append(
            {
                "event_id": event_id,
                "instance_id": label["instance_id"],
                "run_id": str(raw.get("run_id") or ""),
                "original_class": label["original_class"],
                "observed_ms": label["observed_ms"],
                "fold": label["fold"],
                "event_ordinal": event_ordinal(event_id),
                "features": feature_map,
                "feature_status": feature_status,
            }
        )
    if len(rows) != len(labels) or seen != set(labels):
        missing = sorted(set(labels) - seen)[:5]
        raise ContractError(f"common view/label join incomplete: rows={len(rows)} missing={missing}")
    rows.sort(key=lambda row: (row["instance_id"], row["event_ordinal"] is None, row["event_ordinal"] or 0, row["event_id"]))
    metadata = {
        "view_manifest_sha256": _hash(view_manifest),
        "view_tools_sha256": actual_view_hash,
        "view_declared_tools_sha256": declared_view_hash,
        "common_manifest": manifest,
        "skipped_before_complete_decode": skipped_before_decode,
        "feature_status_counts": dict(Counter(row["feature_status"] for row in rows)),
    }
    return rows, metadata


def _predict_rows(model: CpuEventPredictor, rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    predicted: list[dict[str, Any]] = []
    for row in rows:
        detail = model.predict_details(row)
        out = dict(row)
        out.update(detail)
        predicted.append(out)
    return predicted


def _aggregate_predictions(rows: Sequence[Mapping[str, Any]], prediction_key: str = "prediction") -> dict[str, Any]:
    return metric_rows(rows, prediction_key)


def _outer_fixed(rows: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    metrics: dict[str, Any] = {}
    fold_details: dict[str, dict[str, Any]] = {}
    for candidate in CANDIDATES:
        all_predictions: list[dict[str, Any]] = []
        fold_metrics: dict[str, Any] = {}
        for fold in range(OUTER_FOLDS):
            train = [row for row in rows if row["fold"] != fold]
            test = [row for row in rows if row["fold"] == fold]
            model = CpuEventPredictor(candidate).fit(train)
            predicted = _predict_rows(model, test)
            all_predictions.extend(predicted)
            fold_metrics[str(fold)] = _aggregate_predictions(predicted)
        metrics[candidate] = _aggregate_predictions(all_predictions)
        fold_details[candidate] = fold_metrics
    return metrics, fold_details


def _inner_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for candidate in CANDIDATES:
        predictions: list[dict[str, Any]] = []
        for fold in range(INNER_FOLDS):
            train = [row for row in rows if inner_fold(row["instance_id"]) != fold]
            valid = [row for row in rows if inner_fold(row["instance_id"]) == fold]
            model = CpuEventPredictor(candidate).fit(train)
            predictions.extend(_predict_rows(model, valid))
        result[candidate] = _aggregate_predictions(predictions)
    return result


def _nested_outer(rows: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    predictions: list[dict[str, Any]] = []
    selection_audit: list[dict[str, Any]] = []
    for outer in range(OUTER_FOLDS):
        outer_train = [row for row in rows if row["fold"] != outer]
        outer_test = [row for row in rows if row["fold"] == outer]
        inner = _inner_metrics(outer_train)
        selected = choose_candidate(inner)
        model = CpuEventPredictor(selected).fit(outer_train)
        fold_predictions = _predict_rows(model, outer_test)
        for item in fold_predictions:
            item["selected_candidate"] = selected
            item["outer_fold"] = outer
        predictions.extend(fold_predictions)
        selection_audit.append(
            {
                "outer_fold": outer,
                "outer_train_instances": len({row["instance_id"] for row in outer_train}),
                "outer_test_instances": len({row["instance_id"] for row in outer_test}),
                "selected_candidate": selected,
                "inner_metrics": inner,
            }
        )
    return _aggregate_predictions(predictions), selection_audit, predictions


def _final_selection(rows: Sequence[Mapping[str, Any]]) -> tuple[str, dict[str, Any]]:
    metrics = _inner_metrics(rows)
    return choose_candidate(metrics), metrics


def _write_new(path: Path, content: str) -> None:
    if path.exists():
        raise RuntimeError(f"refusing to overwrite existing file: {path}")
    path.write_text(content, encoding="utf-8")


def _write_predictions(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    fields = [
        "event_id",
        "instance_id",
        "run_id",
        "fold",
        "original_class",
        "observed_ms",
        "prediction",
        "selected_level",
        "selected_key",
        "fallback_reason",
        "feature_status",
        "ape_pct",
        "within25",
        "selected_candidate",
        "outer_fold",
    ]
    with path.open("x", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            observed = float(row["observed_ms"])
            prediction = float(row["prediction"])
            enriched = dict(row)
            enriched["ape_pct"] = abs(prediction - observed) / observed * 100.0
            enriched["within25"] = abs(prediction - observed) / observed * 100.0 <= GATE_PCT
            writer.writerow({field: enriched.get(field, "") for field in fields})


def _markdown_report(comparison: Mapping[str, Any]) -> str:
    data = comparison["data"]
    lines = [
        "# Offline CPU event candidate comparison",
        "",
        "This is a bounded train-calibration model selection artifact. It does not modify the frozen model or production snapshot and does not claim a holdout or cross-hardware result.",
        "",
        f"The retained target has {data['event_count']} CPU events across {data['instance_count']} observed instances and {data['run_count']} run trajectories. All events of an instance remain in one of the five established outer folds. The common view was identity-filtered before target/action decoding; prior mixed-artifact exposure remains disclosed.",
        "",
        "## Fixed outer diagnostics",
        "",
        "| Candidate | Events within 25% | Worst APE | p95 APE | Mean APE | CPU observed ms | CPU predicted ms | Instance trajectory pass | Unsupported features |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name in CANDIDATES:
        item = comparison["outer_fixed"][name]
        lines.append(
            f"| `{name}` | {item['within25_rate']:.4f} | {item['worst_ape_pct']:.2f}% | {item['p95_ape_pct']:.2f}% | {item['mean_ape_pct']:.2f}% | {item['observed_ms']:.3f} | {item['predicted_ms']:.3f} | {item['instance_trajectory_pass_rate']:.4f} | {item['unsupported_feature_count']} |"
        )
    selected = comparison["final_selection"]["candidate"]
    nested = comparison["nested_selected"]
    lines.extend(
        [
            "",
            "## Nested selected procedure",
            "",
            f"The final offline candidate selected by the three-fold inner rule is `{selected}`. Inner selection maximizes within-25% coverage subject to worst APE no greater than the coarse class-median comparator, then lower worst APE and simpler candidate. The selected procedure's five outer-fold estimate is {nested['within25_rate']:.4f} within 25%, worst APE {nested['worst_ape_pct']:.2f}%, p95 APE {nested['p95_ape_pct']:.2f}%, and instance trajectory pass rate {nested['instance_trajectory_pass_rate']:.4f}.",
            "",
            "The candidate is packaged for later validation only. CPU sums are reported over this retained event population; no full E2E pass is fabricated from CPU-only labels. No CPU frequency/core or cross-hardware scaling law is inferred.",
            "",
            "## Per-class metrics for nested selected procedure",
            "",
            "| Original class | Events within 25% | Worst APE | p95 APE | Mean APE | Misses |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for name, item in sorted(nested["by_original_class"].items()):
        lines.append(f"| `{name}` | {item['within25_rate']:.4f} | {item['worst_ape_pct']:.2f}% | {item['p95_ape_pct']:.2f}% | {item['mean_ape_pct']:.2f}% | {item['miss_count']} |")
    lines.extend(["", "Unsupported feature rows remain in every denominator and route to the original-class/global fallback. See `comparison.json` for fold audits and CPU sums by instance.", ""])
    return "\n".join(lines)


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
    # Build no sequential feature: same-run order is not proven by this view.
    for row in rows:
        row.pop("event_ordinal", None)
    outer_fixed, outer_folds = _outer_fixed(rows)
    nested_metrics, nested_audit, nested_rows = _nested_outer(rows)
    final_candidate, final_inner = _final_selection(rows)
    final_model = CpuEventPredictor(final_candidate).fit(rows)
    final_artifact = final_model.to_mapping()
    final_artifact.update(
        {
            "artifact_kind": "offline_candidate_only",
            "production_adoption": "not_authorized",
            "training_event_count": len(rows),
            "training_instance_count": len({row["instance_id"] for row in rows}),
            "feature_contract": {
                "mode": "pre_event_only",
                "extractor_id": EXTRACTOR_ID,
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
    )
    comparison = {
        "schema_version": "assignment.offline-cpu-event-comparison.v1",
        "status": "complete_bounded_offline_candidate",
        "data": {
            "event_count": len(rows),
            "instance_count": len({row["instance_id"] for row in rows}),
            "run_count": len({row["run_id"] for row in rows if row.get("run_id")}),
            "manifest_instance_count": len(train_instances),
            "class_counts": dict(Counter(row["original_class"] for row in rows)),
            "feature_status_counts": dict(Counter(row["feature_status"] for row in rows)),
        },
        "protocol": {
            "outer_folds": OUTER_FOLDS,
            "outer_fold_rule": "sha256('assignment.d9.train-fold-v1:' + instance_id) first 8 bytes modulo 5",
            "inner_folds": INNER_FOLDS,
            "inner_fold_rule": "sha256('assignment.d9.cpu-inner-v1:' + instance_id) first 8 bytes modulo 3",
            "candidate_names": list(CANDIDATES),
            "support": {"minimum_events": MIN_EVENTS, "minimum_instances": MIN_INSTANCES},
            "gate_pct": GATE_PCT,
            "selection_rule": "maximum inner within25 subject to worst APE <= coarse_class_median; ties lower worst APE then simpler candidate; retain coarse comparator if no alternative qualifies",
            "prior_mechanism": "excluded because same-run event order is not proven",
            "target": "CPU tool-event duration observed_ms",
        },
        "provenance": {
            "labels_path": str(args.labels),
            "labels_sha256": _hash(args.labels),
            "view_path": str(args.view),
            "view_manifest_path": str(args.view_manifest),
            "production_manifest_path": str(args.production_manifest),
            "production_manifest_sha256": _hash(args.production_manifest),
            "view": view_meta,
            "prior_access_disclosure": "Earlier D9 forensic scripts bulk-read mixed historical prediction/action objects before filtering; this artifact uses only the identity-filtered common train view and does not claim pristine blindness.",
        },
        "outer_fixed": outer_fixed,
        "outer_fold_details": outer_folds,
        "nested_selected": nested_metrics,
        "nested_selection_audit": nested_audit,
        "final_selection": {
            "candidate": final_candidate,
            "inner_metrics_on_all_authorized_rows": final_inner,
            "packaged_as": "offline_candidate_only",
        },
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    _write_new(args.output_dir / "comparison.json", json.dumps(comparison, indent=2, sort_keys=True) + "\n")
    _write_new(args.output_dir / "model.json", json.dumps(final_artifact, indent=2, sort_keys=True) + "\n")
    _write_new(args.output_dir / "report.md", _markdown_report(comparison))
    # Save the nested outer predictions as the directly interpretable CV table.
    _write_predictions(args.output_dir / "nested_predictions.csv", nested_rows)
    print(json.dumps({"candidate": final_candidate, "events": len(rows), "instances": len({row["instance_id"] for row in rows}), "output_dir": str(args.output_dir)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
