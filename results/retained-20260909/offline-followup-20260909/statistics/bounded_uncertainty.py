#!/usr/bin/env python3
"""Cluster-bootstrap uncertainty for the fixed CPU OOF prediction files.

This module intentionally consumes completed out-of-fold predictions.  It
does not fit, select, or alter any model.  ``run(output_dir)`` is the pipeline
entry point; its command-line wrapper is useful for reproducing the packet.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path
import random
from typing import Any, Iterable, Mapping, Sequence


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
DEFAULT_COARSE = REPO / "docs/offline-deliverables-20260909/cpu/nested_predictions.csv"
DEFAULT_HYBRID = REPO / "docs/offline-deliverables-20260909/cpu/class_hybrid_predictions.csv"
DEFAULT_MANIFEST = REPO / "docs/offline-deliverables-20260909/training_view/manifest.json"
SEED = 20260909
REPLICATES = 5000
IDENTITY_FIELDS = ("event_id", "run_id", "instance_id", "fold")
REQUIRED_FIELDS = set(IDENTITY_FIELDS) | {"original_class", "observed_ms", "prediction"}


class IdentityMismatchError(ValueError):
    """Raised when the two prediction files cannot be paired exactly."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _identity(row: Mapping[str, str]) -> tuple[str, str, str, str]:
    return tuple(str(row[field]) for field in IDENTITY_FIELDS)  # type: ignore[return-value]


def _load_predictions(path: Path) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or not REQUIRED_FIELDS.issubset(reader.fieldnames):
            missing = sorted(REQUIRED_FIELDS - set(reader.fieldnames or ()))
            raise ValueError(f"{path}: missing required columns {missing}")
        rows: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        event_ids: dict[str, tuple[str, str, str, str]] = {}
        for line_number, raw in enumerate(reader, start=2):
            key = _identity(raw)
            if not all(key):
                raise ValueError(f"{path}:{line_number}: empty exact-pairing identity field")
            if key in rows:
                raise IdentityMismatchError(f"{path}:{line_number}: duplicate exact identity {key!r}")
            prior = event_ids.get(raw["event_id"])
            if prior is not None and prior != key:
                raise IdentityMismatchError(
                    f"{path}:{line_number}: event_id maps to conflicting run/instance/fold identities"
                )
            try:
                observed = float(raw["observed_ms"])
                prediction = float(raw["prediction"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{path}:{line_number}: non-numeric observed_ms or prediction") from exc
            if not (math.isfinite(observed) and math.isfinite(prediction) and observed > 0):
                raise ValueError(f"{path}:{line_number}: invalid observed_ms or prediction")
            rows[key] = {
                **raw,
                "observed_ms": observed,
                "prediction": prediction,
                "within25": abs(prediction - observed) / observed <= 0.25,
            }
            event_ids[raw["event_id"]] = key
    if not rows:
        raise ValueError(f"{path}: no predictions")
    return rows


def pair_predictions(coarse_path: Path | str, hybrid_path: Path | str) -> list[dict[str, Any]]:
    """Pair OOF rows, failing closed on all requested exact identity fields."""
    coarse = _load_predictions(Path(coarse_path))
    hybrid = _load_predictions(Path(hybrid_path))
    if coarse.keys() != hybrid.keys():
        only_coarse = len(coarse.keys() - hybrid.keys())
        only_hybrid = len(hybrid.keys() - coarse.keys())
        raise IdentityMismatchError(
            f"exact identity sets differ: {only_coarse} only in coarse, {only_hybrid} only in hybrid"
        )
    paired: list[dict[str, Any]] = []
    for key in sorted(coarse):
        left, right = coarse[key], hybrid[key]
        for field in ("original_class", "observed_ms"):
            if left[field] != right[field]:
                raise IdentityMismatchError(f"{key!r}: {field} differs between coarse and hybrid")
        paired.append(
            {
                "event_id": left["event_id"],
                "run_id": left["run_id"],
                "instance_id": left["instance_id"],
                "fold": str(left["fold"]),
                "original_class": left["original_class"],
                "observed_ms": left["observed_ms"],
                "coarse_within25": left["within25"],
                "hybrid_within25": right["within25"],
                "coarse_ape_pct": abs(left["prediction"] - left["observed_ms"]) / left["observed_ms"] * 100,
                "hybrid_ape_pct": abs(right["prediction"] - right["observed_ms"]) / right["observed_ms"] * 100,
            }
        )
    return paired


def _validate_manifest(manifest_path: Path, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_events = manifest.get("counts", {}).get("tools", {}).get("retained")
    expected_instances = manifest.get("counts", {}).get("tools", {}).get("instances")
    n_instances = len({str(row["instance_id"]) for row in rows})
    if manifest.get("partition") != "train_calibration":
        raise ValueError("training manifest is not the train_calibration partition")
    if expected_events != len(rows) or expected_instances != n_instances:
        raise ValueError(
            "training manifest counts do not match paired predictions: "
            f"events {expected_events}/{len(rows)}, instances {expected_instances}/{n_instances}"
        )
    return {
        "path": str(manifest_path),
        "sha256": _sha256(manifest_path),
        "partition": manifest["partition"],
        "retained_tool_events": expected_events,
        "observed_instances": expected_instances,
        "manifest_instances": manifest.get("manifest_instances"),
    }


def _clusters(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_instance: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        by_instance[str(row["instance_id"])].append(row)
    return [
        {"instance_id": instance_id, "rows": grouped}
        for instance_id, grouped in sorted(by_instance.items())
    ]


def _scope_cluster_values(clusters: Sequence[Mapping[str, Any]], class_name: str | None = None) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for cluster in clusters:
        rows = [row for row in cluster["rows"] if class_name is None or row["original_class"] == class_name]
        if not rows:
            values.append({"n": 0, "coarse": 0, "hybrid": 0, "coarse_strict": 0, "hybrid_strict": 0})
            continue
        coarse = sum(bool(row["coarse_within25"]) for row in rows)
        hybrid = sum(bool(row["hybrid_within25"]) for row in rows)
        values.append(
            {
                "n": len(rows),
                "coarse": coarse,
                "hybrid": hybrid,
                "coarse_strict": int(coarse == len(rows)),
                "hybrid_strict": int(hybrid == len(rows)),
            }
        )
    return values


def _percentile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _bootstrap_summary(
    values: Sequence[Mapping[str, int]], draws: Sequence[Sequence[int]]
) -> dict[str, dict[str, float | None]]:
    samples: dict[str, list[float]] = {"event_weighted_coverage": [], "instance_weighted_coverage": [], "strict_all_events_gate": []}
    usable = [index for index, value in enumerate(values) if value["n"]]
    if not usable:
        return {metric: {"coarse": None, "hybrid": None, "delta": None, "ci95_low": None, "ci95_high": None} for metric in samples}
    for draw in draws:
        selected = [index for index in draw if values[index]["n"]]
        total_events = sum(values[index]["n"] for index in selected)
        coarse_events = sum(values[index]["coarse"] for index in selected)
        hybrid_events = sum(values[index]["hybrid"] for index in selected)
        event_coarse = coarse_events / total_events
        event_hybrid = hybrid_events / total_events
        instance_coarse = sum(values[index]["coarse"] / values[index]["n"] for index in selected) / len(selected)
        instance_hybrid = sum(values[index]["hybrid"] / values[index]["n"] for index in selected) / len(selected)
        strict_coarse = sum(values[index]["coarse_strict"] for index in selected) / len(selected)
        strict_hybrid = sum(values[index]["hybrid_strict"] for index in selected) / len(selected)
        for metric, coarse, hybrid in (
            ("event_weighted_coverage", event_coarse, event_hybrid),
            ("instance_weighted_coverage", instance_coarse, instance_hybrid),
            ("strict_all_events_gate", strict_coarse, strict_hybrid),
        ):
            samples[metric].append((coarse, hybrid, hybrid - coarse))
    result: dict[str, dict[str, float | None]] = {}
    for metric, triples in samples.items():
        coarse_values, hybrid_values, delta_values = zip(*triples)
        result[metric] = {
            "coarse": sum(coarse_values) / len(coarse_values),
            "hybrid": sum(hybrid_values) / len(hybrid_values),
            "delta": sum(delta_values) / len(delta_values),
            "ci95_low": _percentile(delta_values, 0.025),
            "ci95_high": _percentile(delta_values, 0.975),
        }
    return result


def _point_summary(values: Sequence[Mapping[str, int]]) -> dict[str, Any]:
    used = [value for value in values if value["n"]]
    n_events = sum(value["n"] for value in used)
    coarse_events = sum(value["coarse"] for value in used)
    hybrid_events = sum(value["hybrid"] for value in used)
    def metric(coarse: float, hybrid: float) -> dict[str, float]:
        return {"coarse": coarse, "hybrid": hybrid, "delta": hybrid - coarse}
    return {
        "n_events": n_events,
        "n_clusters": len(used),
        "event_weighted_coverage": metric(coarse_events / n_events, hybrid_events / n_events),
        "instance_weighted_coverage": metric(
            sum(value["coarse"] / value["n"] for value in used) / len(used),
            sum(value["hybrid"] / value["n"] for value in used) / len(used),
        ),
        "strict_all_events_gate": metric(
            sum(value["coarse_strict"] for value in used) / len(used),
            sum(value["hybrid_strict"] for value in used) / len(used),
        ),
        "instances_improved": sum(value["hybrid"] / value["n"] > value["coarse"] / value["n"] for value in used),
        "instances_worse": sum(value["hybrid"] / value["n"] < value["coarse"] / value["n"] for value in used),
        "instances_tie": sum(value["hybrid"] == value["coarse"] for value in used),
    }


def _merge_ci(point: dict[str, Any], bootstrap: Mapping[str, Mapping[str, float | None]]) -> dict[str, Any]:
    merged = dict(point)
    for name in ("event_weighted_coverage", "instance_weighted_coverage", "strict_all_events_gate"):
        merged[name] = dict(point[name])
        merged[name]["delta_ci95"] = {
            "low": bootstrap[name]["ci95_low"],
            "high": bootstrap[name]["ci95_high"],
        }
    return merged


def _fold_rows(rows: Sequence[Mapping[str, Any]], classes: Iterable[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    scopes = [("all", None)] + [("original_class", name) for name in classes]
    for fold in sorted({str(row["fold"]) for row in rows}, key=int):
        fold_clusters = _clusters([row for row in rows if str(row["fold"]) == fold])
        for scope, class_name in scopes:
            point = _point_summary(_scope_cluster_values(fold_clusters, class_name))
            records.append({"scope": scope, "original_class": class_name or "", "fold": fold, **point})
    return records


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    fields = sorted({field for row in rows for field in row})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _ci_rows(scope: str, class_name: str, summary: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for metric in ("event_weighted_coverage", "instance_weighted_coverage", "strict_all_events_gate"):
        values = summary[metric]
        rows.append(
            {
                "scope": scope,
                "original_class": class_name,
                "metric": metric,
                "n_events": summary["n_events"],
                "n_clusters": summary["n_clusters"],
                "coarse_coverage_proportion": values["coarse"],
                "hybrid_coverage_proportion": values["hybrid"],
                "delta_percentage_points": values["delta"] * 100,
                "delta_ci95_low_percentage_points": values["delta_ci95"]["low"] * 100,
                "delta_ci95_high_percentage_points": values["delta_ci95"]["high"] * 100,
            }
        )
    return rows


def _observed_worst_ape(rows: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    return {
        "coarse": max(float(row["coarse_ape_pct"]) for row in rows),
        "hybrid": max(float(row["hybrid_ape_pct"]) for row in rows),
    }


def _render_report(summary: Mapping[str, Any]) -> str:
    overall = summary["overall"]
    event = overall["event_weighted_coverage"]
    instance = overall["instance_weighted_coverage"]
    strict = overall["strict_all_events_gate"]
    lines = [
        "# Bounded uncertainty analysis for fixed CPU OOF predictions",
        "",
        "This follow-up pairs the completed coarse and class-hybrid OOF files by exact "
        "`event_id`, `run_id`, `instance_id`, and `fold`. The common training manifest "
        "matches all 23,245 paired events and 545 observed instances.",
        "",
        "The analysis holds the existing predictions fixed. It uses 5,000 paired nonparametric "
        "bootstrap replicates, sampling instances with replacement (seed 20260909); every event "
        "and run belonging to a sampled instance is carried together. Percentile 95% intervals "
        "describe the hybrid-minus-coarse coverage difference conditional on these OOF predictions.",
        "",
        "It is **not** a bootstrap of the full nested fit or selection procedure, and it is **not** "
        "a blind holdout estimate: the hybrid was an adaptive bounded sixth diagnostic. These "
        "intervals therefore do not repair that selection limitation.",
        "",
        "| Measure | Coarse | Hybrid | Hybrid − coarse | 95% CI for delta |",
        "|---|---:|---:|---:|---:|",
        f"| Event-weighted within-25% coverage | {event['coarse']:.4%} | {event['hybrid']:.4%} | {event['delta'] * 100:+.4f} pp | [{event['delta_ci95']['low'] * 100:.4f}, {event['delta_ci95']['high'] * 100:.4f}] pp |",
        f"| Instance-weighted within-25% coverage | {instance['coarse']:.4%} | {instance['hybrid']:.4%} | {instance['delta'] * 100:+.4f} pp | [{instance['delta_ci95']['low'] * 100:.4f}, {instance['delta_ci95']['high'] * 100:.4f}] pp |",
        f"| Strict all-events instance gate | {strict['coarse']:.4%} | {strict['hybrid']:.4%} | {strict['delta'] * 100:+.4f} pp | [{strict['delta_ci95']['low'] * 100:.4f}, {strict['delta_ci95']['high'] * 100:.4f}] pp |",
        "",
        f"There are {overall['n_clusters']} instance clusters: {overall['instances_improved']} improved, "
        f"{overall['instances_worse']} worsened, and {overall['instances_tie']} tied by each instance's event coverage. "
        "The strict all-events gate is reported separately and remains a conjunction across every "
        "event in an instance; it is not replaced by the average coverage metric. This 0/545 "
        "strict result is only for the retained CPU-event population, not a literal all-required-PDF gate.",
        "",
        "Worst APE is an observed maximum only: coarse "
        f"{summary['observed_worst_ape_pct']['coarse']:.3f}% and hybrid "
        f"{summary['observed_worst_ape_pct']['hybrid']:.3f}%. No bootstrap interval here claims "
        "that tails are bounded.",
        "",
        "`coverage_ci.csv` contains the overall and original-class CIs. `fold_deltas.csv` contains "
        "event- and instance-weighted deltas for every fixed outer fold and original class.",
        "",
    ]
    return "\n".join(lines)


def run(
    output_dir: Path | str,
    *,
    coarse_path: Path | str = DEFAULT_COARSE,
    hybrid_path: Path | str = DEFAULT_HYBRID,
    manifest_path: Path | str = DEFAULT_MANIFEST,
    seed: int = SEED,
    replicates: int = REPLICATES,
) -> dict[str, Any]:
    """Write the bounded uncertainty packet and return its JSON-compatible summary."""
    if replicates < 1:
        raise ValueError("replicates must be positive")
    coarse_path, hybrid_path, manifest_path = Path(coarse_path), Path(hybrid_path), Path(manifest_path)
    paired = pair_predictions(coarse_path, hybrid_path)
    manifest = _validate_manifest(manifest_path, paired)
    clusters = _clusters(paired)
    rng = random.Random(seed)
    draws = [[rng.randrange(len(clusters)) for _ in clusters] for _ in range(replicates)]
    classes = sorted({str(row["original_class"]) for row in paired})
    overall = _merge_ci(_point_summary(_scope_cluster_values(clusters)), _bootstrap_summary(_scope_cluster_values(clusters), draws))
    by_class: dict[str, Any] = {}
    for class_name in classes:
        values = _scope_cluster_values(clusters, class_name)
        by_class[class_name] = _merge_ci(_point_summary(values), _bootstrap_summary(values, draws))
        by_class[class_name]["observed_worst_ape_pct"] = _observed_worst_ape(
            [row for row in paired if row["original_class"] == class_name]
        )
    summary = {
        "schema_version": "assignment.offline-followup-bounded-uncertainty.v1",
        "status": "complete_conditional_oof_uncertainty_only",
        "method": {
            "resampling_unit": "instance_id",
            "paired": True,
            "cluster_contents": "all runs and events for each sampled instance",
            "seed": seed,
            "replicates": replicates,
            "interval": "two-sided percentile 95% CI for hybrid minus coarse",
            "conditional_on": "existing fixed out-of-fold predictions",
            "units": {
                "coverage": "proportion in [0, 1]",
                "reported_delta": "percentage points in CSV and REPORT.md; raw summary delta is proportion",
            },
        },
        "limitations": [
            "Does not refit base models or repeat the nested/adaptive hybrid selection inside bootstrap replicates.",
            "Not a blind holdout estimate because the class hybrid was an adaptive bounded sixth diagnostic.",
            "Worst observed APE is descriptive only; bootstrap results do not claim bounded tails.",
            "The strict all-events gate covers retained CPU events only, not all required PDF events.",
        ],
        "provenance": {
            "coarse_predictions": {"path": str(coarse_path), "sha256": _sha256(coarse_path)},
            "hybrid_predictions": {"path": str(hybrid_path), "sha256": _sha256(hybrid_path)},
            "common_training_manifest": manifest,
        },
        "identity_validation": {
            "paired_on": list(IDENTITY_FIELDS),
            "event_count": len(paired),
            "instance_count": len(clusters),
            "run_count": len({row["run_id"] for row in paired}),
            "status": "passed_exact_pairing",
        },
        "overall": overall,
        "by_original_class": by_class,
        "observed_worst_ape_pct": _observed_worst_ape(paired),
        "fold_deltas": _fold_rows(paired, classes),
    }
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    ci_rows = _ci_rows("overall", "", overall)
    for class_name, class_summary in by_class.items():
        ci_rows.extend(_ci_rows("original_class", class_name, class_summary))
    _write_csv(output / "coverage_ci.csv", ci_rows)
    flat_fold_rows: list[dict[str, Any]] = []
    for row in summary["fold_deltas"]:
        for metric in ("event_weighted_coverage", "instance_weighted_coverage", "strict_all_events_gate"):
            flat_fold_rows.append(
                {
                    "scope": row["scope"], "original_class": row["original_class"], "fold": row["fold"],
                    "metric": metric, "n_events": row["n_events"], "n_clusters": row["n_clusters"],
                    "coarse_coverage_proportion": row[metric]["coarse"],
                    "hybrid_coverage_proportion": row[metric]["hybrid"],
                    "delta_percentage_points": row[metric]["delta"] * 100,
                }
            )
    _write_csv(output / "fold_deltas.csv", flat_fold_rows)
    (output / "REPORT.md").write_text(_render_report(summary), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=HERE / "output")
    parser.add_argument("--coarse-path", type=Path, default=DEFAULT_COARSE)
    parser.add_argument("--hybrid-path", type=Path, default=DEFAULT_HYBRID)
    parser.add_argument("--manifest-path", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--replicates", type=int, default=REPLICATES)
    args = parser.parse_args()
    summary = run(
        args.output_dir, coarse_path=args.coarse_path, hybrid_path=args.hybrid_path,
        manifest_path=args.manifest_path, seed=args.seed, replicates=args.replicates,
    )
    print(json.dumps({"output_dir": str(args.output_dir), "overall": summary["overall"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
