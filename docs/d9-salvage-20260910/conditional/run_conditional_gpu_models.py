#!/usr/bin/env python3
"""Bounded assignment-level conditional GPU request-duration comparison.

The historical training view contains completed request-proxy rows.  This
script evaluates four predeclared, simple models with the existing grouped
``outer_fold`` assignments.  ``output_tokens`` is used only because this is a
trace-conditioned historical simulation: it is a descriptor in an already
realized request trace, not a pre-generation forecast input.  ``observed_ms``
is always the target and never a feature.

The script has no acquisition, inference, network, or repository-module
dependencies.  It writes all outputs beside this file by default and can be
rerun from the repository root or from any working directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
VIEW_DIR = REPO / "docs" / "offline-deliverables-20260909" / "training_view"
MODELS_PATH = VIEW_DIR / "models.jsonl"
MANIFEST_PATH = VIEW_DIR / "manifest.json"
PRIOR_CONDITIONAL_SIMULATOR = REPO / "src" / "agentic_sim" / "assignment" / "workload_simulator.py"

SCHEMA = "assignment.d9.conditional-gpu-model-comparison.v1"
INNER_FOLD_PREFIX = "assignment.d9.conditional-gpu-inner-fold-v1:"
ROBUST_ITERATIONS = 8
RIDGE = 1e-4
HUBER_DELTA = 1.5
WITHIN_THRESHOLD = 25.0

# Four models are fixed before scoring.  The first is the existing feature-free
# baseline.  The remaining models use only declared workload descriptors from
# a realized trace.  The final two are additive/interaction and robust variants
# of the same supported token-workload design, not an open-ended search.
CANDIDATE_ORDER = (
    "global_median_baseline",
    "nonnegative_log_additive",
    "nonnegative_log_token_interaction",
    "robust_nonnegative_log_token_interaction",
)


class ValidationError(ValueError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def fold_for_instance(instance_id: str) -> int:
    return int(hashlib.sha256((INNER_FOLD_PREFIX + instance_id).encode()).hexdigest(), 16) % 3


def finite_positive(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{field} is not numeric")
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValidationError(f"{field} is not positive finite")
    return value


def load_rows(path: Path = MODELS_PATH, manifest_path: Path = MANIFEST_PATH) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValidationError("training manifest is not an object")
    rows: list[dict[str, Any]] = []
    allowed = {
        "context_tokens", "input_tokens", "instance_id", "max_output_tokens",
        "observed_ms", "outer_fold", "output_tokens", "request_id", "run_id",
    }
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict) or set(row) != allowed:
                raise ValidationError(f"unexpected model row fields at line {line_number}")
            for field in ("instance_id", "request_id", "run_id"):
                if not isinstance(row[field], str) or not row[field]:
                    raise ValidationError(f"invalid {field} at line {line_number}")
            if not isinstance(row["outer_fold"], int) or isinstance(row["outer_fold"], bool) or row["outer_fold"] not in range(5):
                raise ValidationError(f"invalid outer_fold at line {line_number}")
            for field in ("input_tokens", "context_tokens", "max_output_tokens", "output_tokens"):
                if isinstance(row[field], bool) or not isinstance(row[field], (int, float)) or not math.isfinite(float(row[field])) or float(row[field]) < 0:
                    raise ValidationError(f"invalid {field} at line {line_number}")
            row["observed_ms"] = finite_positive(row["observed_ms"], "observed_ms")
            rows.append(row)

    counts = manifest.get("counts")
    if not isinstance(counts, Mapping):
        raise ValidationError("manifest counts missing")
    expected_rows = counts.get("models", {}).get("retained") if isinstance(counts.get("models"), Mapping) else None
    expected_instances = counts.get("models", {}).get("instances") if isinstance(counts.get("models"), Mapping) else None
    expected_runs = counts.get("trajectories", {}).get("retained") if isinstance(counts.get("trajectories"), Mapping) else None
    if expected_rows != len(rows) or expected_instances != len({row["instance_id"] for row in rows}) or expected_runs != len({row["run_id"] for row in rows}):
        raise ValidationError("training-view counts do not match manifest")
    excluded_instances = set(manifest.get("scope", {}).get("excluded_instance_ids", []))
    excluded_runs = set(manifest.get("scope", {}).get("excluded_run_ids", []))
    if excluded_instances & {row["instance_id"] for row in rows}:
        raise ValidationError("excluded instance appears in training view")
    if excluded_runs & {row["run_id"] for row in rows}:
        raise ValidationError("excluded run appears in training view")
    by_instance = defaultdict(set)
    by_run = defaultdict(set)
    for row in rows:
        by_instance[row["instance_id"]].add(row["outer_fold"])
        by_run[row["run_id"]].add((row["instance_id"], row["outer_fold"]))
    if any(len(folds) != 1 for folds in by_instance.values()):
        raise ValidationError("instance spans multiple outer folds")
    if any(len(values) != 1 for values in by_run.values()):
        raise ValidationError("run spans multiple instance/fold identities")
    if len(by_instance) != 545 or len(by_run) != 819:
        raise ValidationError("authorized population is not 545 instances / 819 runs")
    if {row["outer_fold"] for row in rows} != set(range(5)):
        raise ValidationError("outer fold partition is incomplete")
    return rows, manifest


def feature_values(row: Mapping[str, Any], interaction: bool) -> list[float]:
    # input and context are equal in this view.  Their sum retains both
    # declared workload fields without fitting two non-identifiable columns.
    prompt = math.log1p(float(row["input_tokens"]) + float(row["context_tokens"]))
    output = math.log1p(float(row["output_tokens"]))
    cap = math.log1p(float(row["max_output_tokens"]))
    values = [prompt, output, cap]
    if interaction:
        values.append(prompt * output)
    return values


def feature_names(interaction: bool) -> list[str]:
    names = ["log1p(input_tokens_plus_context_tokens)", "log1p(output_tokens)", "log1p(max_output_tokens)"]
    if interaction:
        names.append("log1p(input_tokens_plus_context_tokens)*log1p(output_tokens)")
    return names


def _standardize(rows: list[dict[str, Any]], interaction: bool) -> tuple[list[list[float]], list[float], list[float]]:
    raw = [feature_values(row, interaction) for row in rows]
    width = len(raw[0])
    means = [statistics.fmean(item[j] for item in raw) for j in range(width)]
    scales = []
    for j in range(width):
        variance = statistics.fmean((item[j] - means[j]) ** 2 for item in raw)
        scales.append(math.sqrt(variance) if variance > 1e-12 else 1.0)
    transformed = [[(item[j] - means[j]) / scales[j] for j in range(width)] for item in raw]
    return transformed, means, scales


def _solve_linear(matrix: list[list[float]], rhs: list[float]) -> list[float]:
    """Solve a tiny dense system without adding a numerical dependency."""
    augmented = [row[:] + [value] for row, value in zip(matrix, rhs)]
    width = len(rhs)
    for column in range(width):
        pivot = max(range(column, width), key=lambda index: abs(augmented[index][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            raise ValidationError("singular conditional model design")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        scale = augmented[column][column]
        augmented[column] = [value / scale for value in augmented[column]]
        for row in range(width):
            if row == column:
                continue
            scale = augmented[row][column]
            if scale:
                augmented[row] = [left - scale * right for left, right in zip(augmented[row], augmented[column])]
    return [augmented[index][-1] for index in range(width)]


def _fit_weighted_nonnegative(x: list[list[float]], y: list[float], weights: list[float]) -> tuple[float, list[float]]:
    """Fit weighted log-ridge regression with nonnegative slopes.

    The design has at most four slopes.  Build the weighted Gram matrix once,
    enumerate every active slope subset, and retain the feasible solution with
    the smallest ridge objective.  Enumeration avoids the active-set mistake
    where a temporarily negative coefficient is removed permanently even when
    it should re-enter after another correlated feature is fixed.
    """
    width = len(x[0])
    gram = [[0.0 for _ in range(width + 1)] for _ in range(width + 1)]
    rhs = [0.0 for _ in range(width + 1)]
    for row, target, weight in zip(x, y, weights):
        vector = [1.0, *row]
        for left in range(width + 1):
            rhs[left] += weight * vector[left] * target
            for right in range(width + 1):
                gram[left][right] += weight * vector[left] * vector[right]
    for feature in range(1, width + 1):
        gram[feature][feature] += RIDGE

    best: tuple[float, list[float]] | None = None
    for mask in range(1 << width):
        active = [feature for feature in range(width) if mask & (1 << feature)]
        columns = [0, *(feature + 1 for feature in active)]
        reduced = [[gram[left][right] for right in columns] for left in columns]
        reduced_rhs = [rhs[index] for index in columns]
        try:
            solved = _solve_linear(reduced, reduced_rhs)
        except ValidationError:
            continue
        if any(value < -1e-9 for value in solved[1:]):
            continue
        coefficients = [0.0] * (width + 1)
        for index, value in zip(columns, solved):
            coefficients[index] = max(0.0, value) if index else value
        objective = sum(coefficients[i] * gram[i][j] * coefficients[j] for i in range(width + 1) for j in range(width + 1)) - 2.0 * sum(coefficients[i] * rhs[i] for i in range(width + 1))
        if best is None or objective < best[0]:
            best = (objective, coefficients)
    if best is None:
        raise ValidationError("no feasible nonnegative conditional model design")
    coefficients = best[1]
    return coefficients[0], coefficients[1:]


def fit_model(name: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        raise ValidationError("cannot fit empty partition")
    if name == "global_median_baseline":
        return {
            "candidate": name,
            "kind": "global_median",
            "median_ms": statistics.median(row["observed_ms"] for row in rows),
            "feature_names": [],
        }
    interaction = name in {"nonnegative_log_token_interaction", "robust_nonnegative_log_token_interaction"}
    robust = name == "robust_nonnegative_log_token_interaction"
    x, means, scales = _standardize(rows, interaction)
    y = [math.log(row["observed_ms"]) for row in rows]
    weights = [1.0] * len(rows)
    intercept = 0.0
    slopes: list[float] = []
    for _ in range(ROBUST_ITERATIONS if robust else 1):
        intercept, slopes = _fit_weighted_nonnegative(x, y, weights)
        if not robust:
            break
        residuals = [target - (intercept + sum(beta * value for beta, value in zip(slopes, row))) for row, target in zip(x, y)]
        # Fixed Huber reweighting on log duration, with a predeclared delta.
        weights = [1.0 if abs(residual) <= HUBER_DELTA else HUBER_DELTA / abs(residual) for residual in residuals]
    return {
        "candidate": name,
        "kind": "robust_nonnegative_log_ridge" if robust else "nonnegative_log_ridge",
        "feature_names": feature_names(interaction),
        "feature_means": means,
        "feature_scales": scales,
        "intercept_log_ms": intercept,
        "slopes_standardized": slopes,
        "ridge": RIDGE,
        "huber_delta_log_ms": HUBER_DELTA if robust else None,
        "robust_iterations": ROBUST_ITERATIONS if robust else 0,
    }


def predict(model: Mapping[str, Any], row: Mapping[str, Any]) -> float:
    if model["kind"] == "global_median":
        return float(model["median_ms"])
    interaction = len(model["feature_names"]) == 4
    values = feature_values(row, interaction)
    means = model["feature_means"]
    scales = model["feature_scales"]
    log_duration = float(model["intercept_log_ms"]) + sum(
        float(beta) * ((value - float(mean)) / float(scale))
        for beta, value, mean, scale in zip(model["slopes_standardized"], values, means, scales)
    )
    # The log-link is strictly positive and cannot produce a negative duration.
    return max(1e-9, math.exp(min(50.0, log_duration)))


def metrics(predictions: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    rows = list(predictions)
    apes = [float(item["ape_percent"]) for item in rows]
    if not apes:
        raise ValidationError("metric population is empty")
    ordered = sorted(apes)
    by_run: dict[str, list[float]] = defaultdict(list)
    for item in rows:
        by_run[str(item["run_id"])].append(float(item["ape_percent"]))
    all_events = sum(all(value <= WITHIN_THRESHOLD for value in values) for values in by_run.values())
    return {
        "n_events": len(apes),
        "n_runs": len(by_run),
        "within_25_percent_events": 100.0 * sum(value <= WITHIN_THRESHOLD for value in apes) / len(apes),
        "mean_ape_percent": statistics.fmean(apes),
        "p95_ape_percent_nearest_rank": ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)],
        "max_ape_percent": max(apes),
        "all_events_within_25_runs": all_events,
        "all_events_within_25_percent_runs": 100.0 * all_events / len(by_run),
    }


def score(model: Mapping[str, Any], rows: list[dict[str, Any]], outer_fold: int | None, candidate: str) -> list[dict[str, Any]]:
    output = []
    population = rows if outer_fold is None else [row for row in rows if row["outer_fold"] == outer_fold]
    for row in population:
        predicted = predict(model, row)
        observed = float(row["observed_ms"])
        ape = 100.0 * abs(predicted - observed) / observed
        output.append({
            "candidate": candidate,
            "run_id": row["run_id"],
            "request_id": row["request_id"],
            "instance_id": row["instance_id"],
            "outer_fold": row["outer_fold"],
            "input_tokens": row["input_tokens"],
            "context_tokens": row["context_tokens"],
            "output_tokens": row["output_tokens"],
            "max_output_tokens": row["max_output_tokens"],
            "predicted_ms": predicted,
            "observed_ms": observed,
            "ape_percent": ape,
            "within_25_percent": ape <= WITHIN_THRESHOLD,
        })
    return output


def select_candidate(summary: Mapping[str, Mapping[str, Any]]) -> str:
    baseline = summary["global_median_baseline"]
    eligible = [
        name for name in CANDIDATE_ORDER
        if float(summary[name]["within_25_percent_events"]) >= float(baseline["within_25_percent_events"])
        and float(summary[name]["max_ape_percent"]) <= float(baseline["max_ape_percent"])
    ]
    return sorted(
        eligible,
        key=lambda name: (
            -float(summary[name]["within_25_percent_events"]),
            float(summary[name]["max_ape_percent"]),
            CANDIDATE_ORDER.index(name),
        ),
    )[0]


def nested_selection(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    selected_rows: list[dict[str, Any]] = []
    choices: list[dict[str, Any]] = []
    for outer in range(5):
        train = [row for row in rows if row["outer_fold"] != outer]
        test = [row for row in rows if row["outer_fold"] == outer]
        inner_scores: dict[str, list[dict[str, Any]]] = {name: [] for name in CANDIDATE_ORDER}
        for inner in range(3):
            inner_train = [row for row in train if fold_for_instance(row["instance_id"]) != inner]
            inner_test = [row for row in train if fold_for_instance(row["instance_id"]) == inner]
            for name in CANDIDATE_ORDER:
                model = fit_model(name, inner_train)
                inner_scores[name].extend(score(model, inner_test, None, name))
        inner_metrics = {name: metrics(values) for name, values in inner_scores.items()}
        selected = select_candidate(inner_metrics)
        model = fit_model(selected, train)
        selected_rows.extend(score(model, test, None, selected))
        choices.append({
            "outer_fold": outer,
            "selected_candidate": selected,
            "inner_metrics": inner_metrics,
            "outer_test_events": len(test),
            "outer_test_instances": len({row["instance_id"] for row in test}),
            "outer_test_runs": len({row["run_id"] for row in test}),
        })
    return selected_rows, choices


def run(rows: list[dict[str, Any]], manifest: dict[str, Any], models_path: Path, manifest_path: Path) -> dict[str, Any]:
    fixed_predictions: dict[str, list[dict[str, Any]]] = {name: [] for name in CANDIDATE_ORDER}
    fold_fits: list[dict[str, Any]] = []
    for outer in range(5):
        train = [row for row in rows if row["outer_fold"] != outer]
        test = [row for row in rows if row["outer_fold"] == outer]
        for name in CANDIDATE_ORDER:
            model = fit_model(name, train)
            fixed_predictions[name].extend(score(model, test, None, name))
            fold_fits.append({"outer_fold": outer, "candidate": name, "fit_rows": len(train), "fit_instances": len({row["instance_id"] for row in train})})
    fixed_metrics = {name: metrics(values) for name, values in fixed_predictions.items()}
    selected_predictions, choices = nested_selection(rows)
    selected_metrics = metrics(selected_predictions)

    full_inner: dict[str, list[dict[str, Any]]] = {name: [] for name in CANDIDATE_ORDER}
    for inner in range(3):
        fit_rows = [row for row in rows if fold_for_instance(row["instance_id"]) != inner]
        test_rows = [row for row in rows if fold_for_instance(row["instance_id"]) == inner]
        for name in CANDIDATE_ORDER:
            full_inner[name].extend(score(fit_model(name, fit_rows), test_rows, None, name))
    full_inner_metrics = {name: metrics(values) for name, values in full_inner.items()}
    selected_full = select_candidate(full_inner_metrics)
    full_models = {name: fit_model(name, rows) for name in CANDIDATE_ORDER}

    baseline = fixed_metrics["global_median_baseline"]
    improvements = {}
    for name, summary in fixed_metrics.items():
        improvements[name] = {
            "within_25_event_pp_vs_baseline": float(summary["within_25_percent_events"]) - float(baseline["within_25_percent_events"]),
            "all_events_run_pp_vs_baseline": float(summary["all_events_within_25_percent_runs"]) - float(baseline["all_events_within_25_percent_runs"]),
            "p95_change_percent_vs_baseline": float(summary["p95_ape_percent_nearest_rank"]) - float(baseline["p95_ape_percent_nearest_rank"]),
            "worst_change_percent_vs_baseline": float(summary["max_ape_percent"]) - float(baseline["max_ape_percent"]),
        }

    feature_policy = {
        "mode": "trace_conditioned_assignment_level_simulation",
        "declared_or_trace_descriptors": ["input_tokens", "context_tokens", "output_tokens", "max_output_tokens"],
        "derived_feature": "log1p(input_tokens + context_tokens) and one prespecified prompt-output interaction",
        "output_tokens_semantics": "realized output count available in the completed trace; not a pre-generation prediction input",
        "forbidden_features": ["observed_ms", "latency", "residual", "phase_timings", "future_actions", "evaluator_outcome", "cache_state"],
        "target": "observed_ms historical completed-request proxy wall duration",
        "target_boundary": "proxy wall is not native GPU prefill/decode/queue phase time",
        "context_collinearity": "input_tokens equals context_tokens in this view; sum is used so separate coefficients are not claimed",
    }
    return {
        "schema": SCHEMA,
        "data": {
            "models_path": str(models_path),
            "manifest_path": str(manifest_path),
            "models_sha256": sha256_file(models_path),
            "manifest_sha256": sha256_file(manifest_path),
            "rows": len(rows),
            "instances": len({row["instance_id"] for row in rows}),
            "runs": len({row["run_id"] for row in rows}),
            "outer_folds": sorted({row["outer_fold"] for row in rows}),
            "partition": manifest.get("partition"),
        },
        "model_order": list(CANDIDATE_ORDER),
        "feature_policy": feature_policy,
        "selection_rule": "fixed four-model comparison; nested choice maximizes event within-25 coverage subject to baseline worst-error guard, then lower worst error, then prespecified order",
        "fixed_outer_oof_metrics": fixed_metrics,
        "fixed_improvements_vs_baseline": improvements,
        "nested_selected_outer_oof_metrics": selected_metrics,
        "outer_fold_choices": choices,
        "full_training_inner_metrics": full_inner_metrics,
        "full_training_selected_candidate": selected_full,
        "all_events_per_run_gate": {
            "definition": "a run passes only when every retained request event in that run is within 25 percent",
            "denominator_runs": len({row["run_id"] for row in rows}),
            "status": "conditional_request_proxy_only; not the complete CPU+GPU+E2E assignment gate",
        },
        "models": full_models,
        "fold_fit_inventory": fold_fits,
        "deployment": "offline conditional artifact only; no production or pre-generation adoption",
        "prior_conditional_reference": {
            "path": str(PRIOR_CONDITIONAL_SIMULATOR),
            "sha256": sha256_file(PRIOR_CONDITIONAL_SIMULATOR),
            "reference": "gpu_design uses input_tokens, output_tokens, and context_tokens with hardware capacities",
            "comparison_status": "not refit or rescored here; this run establishes gain over the feature-free baseline only",
        },
    }


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", type=Path, default=MODELS_PATH)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--out-dir", type=Path, default=HERE)
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    rows, manifest = load_rows(args.models, args.manifest)
    result = run(rows, manifest, args.models, args.manifest)
    predictions_path = args.out_dir / "predictions.jsonl"
    fit_artifact = {
        "schema": "assignment.d9.conditional-gpu-fit-artifact.v1",
        "source": result["data"],
        "feature_policy": result["feature_policy"],
        "candidate_order": result["model_order"],
        "selected_candidate": result["full_training_selected_candidate"],
        "models": result["models"],
        "fit_partition": "train_calibration",
        "outer_folds_preserved": True,
        "deployment": result["deployment"],
        "prior_conditional_reference": result["prior_conditional_reference"],
    }
    # Predictions are generated after run() so they contain fixed outer OOF
    # rows for every candidate, while the fit artifact contains full-data
    # coefficients only for offline serving/review.
    with predictions_path.open("w", encoding="utf-8") as stream:
        for name in CANDIDATE_ORDER:
            model_rows = []
            for outer in range(5):
                train = [row for row in rows if row["outer_fold"] != outer]
                test = [row for row in rows if row["outer_fold"] == outer]
                model_rows.extend(score(fit_model(name, train), test, None, name))
            for row in sorted(model_rows, key=lambda item: (item["run_id"], item["request_id"])):
                stream.write(json.dumps(row, sort_keys=True) + "\n")
    predictions_sha = sha256_file(predictions_path)
    result["artifacts"] = {
        "predictions": {"path": str(predictions_path), "sha256": predictions_sha, "rows": len(rows) * len(CANDIDATE_ORDER)},
        "fit_artifact": {"path": str(args.out_dir / "fit_artifact.json")},
    }
    write_json(args.out_dir / "comparison.json", result)
    fit_artifact["predictions_sha256"] = predictions_sha
    write_json(args.out_dir / "fit_artifact.json", fit_artifact)
    provenance = {
        "schema": "assignment.d9.conditional-gpu-provenance.v1",
        "script": str(Path(__file__).resolve()),
        "script_sha256": sha256_file(Path(__file__).resolve()),
        "source_models": {"path": str(args.models.resolve()), "sha256": sha256_file(args.models)},
        "source_manifest": {"path": str(args.manifest.resolve()), "sha256": sha256_file(args.manifest)},
        "authorized_population": {"instances": 545, "runs": 819, "rows": len(rows)},
        "excluded_data_used": False,
        "outer_fold_source": "training_view outer_fold; every instance and run validated to one fold",
        "inner_fold_rule": INNER_FOLD_PREFIX + "instance_id modulo 3",
        "candidate_count": len(CANDIDATE_ORDER),
        "candidate_order": list(CANDIDATE_ORDER),
        "prediction_semantics": "trace-conditioned historical comparison; output_tokens is not available for a pre-generation forecast",
        "forbidden_inputs": ["latency", "residual", "observed_ms as feature", "native phase timings", "future actions", "evaluator outcome"],
        "target_semantics": "observed_ms request-proxy wall target; not native GPU phase",
        "prior_conditional_reference": {
            "path": str(PRIOR_CONDITIONAL_SIMULATOR.resolve()),
            "sha256": sha256_file(PRIOR_CONDITIONAL_SIMULATOR),
            "comparison_status": "not refit or rescored; conditional gain here is versus the feature-free baseline, not a demonstrated improvement over the prior workload simulator model",
        },
        "network_or_acquisition": "not used",
        "production_integration": "none",
        "artifacts": result["artifacts"],
    }
    write_json(args.out_dir / "provenance.json", provenance)
    report = render_report(result, predictions_sha)
    (args.out_dir / "REPORT.md").write_text(report, encoding="utf-8")


def render_report(result: Mapping[str, Any], predictions_sha: str) -> str:
    fixed = result["fixed_outer_oof_metrics"]
    baseline = fixed[CANDIDATE_ORDER[0]]
    lines = [
        "# D9 conditional GPU request-duration comparison",
        "",
        "This is a bounded historical, trace-conditioned comparison on the retained `train_calibration` view. It does not change acquisition or production and does not claim a pre-generation predictor.",
        "",
        "## Population and contract",
        "",
        f"The run uses **{result['data']['instances']} instances**, **{result['data']['runs']} runs**, and **{result['data']['rows']} completed request-proxy events**. The pre-existing `outer_fold` assignment is preserved; all rows of each instance and run remain in one outer fold. The view was validated against the manifest and no excluded instance or run was used.",
        "",
        "`output_tokens` is a realized workload descriptor in the completed trace. The comparison therefore answers a conditional simulation question: given the declared/recorded token workload, does a simple duration model improve on the feature-free baseline? It is not a pre-generation forecast. `observed_ms` is the target only. Latency, residuals, phase timing, future actions, evaluator outcomes, and cache state are forbidden features. The target is the historical request-proxy wall duration, not native GPU prefill/decode/queue time.",
        "",
        "Input and context counts are exactly equal in this view, so the model uses their sum and does not claim separately identifiable input/context coefficients. One prespecified prompt-output token interaction is included in the interaction candidates.",
        "",
        "## Relation to the prior conditional simulator",
        "",
        "The repository's prior assignment workload simulator (`src/agentic_sim/assignment/workload_simulator.py::gpu_design`) already uses input, output, and context token descriptors with hardware capacities. This bounded run did not refit or rescore that prior model. Its measured gains therefore establish improvement over the feature-free baseline only; they do not establish improvement over the prior conditional v3 model. The earlier stricter sealed prospective contract also forbade current `output_tokens`, so it answers a different question. Cross-hardware accuracy and native GPU phase accuracy remain unvalidated.",
        "",
        "## Fixed outer-fold results",
        "",
        "| Candidate | Event within 25% | All-events-per-run | Mean APE | P95 APE | Worst APE | Δ event coverage vs baseline | Δ run gate vs baseline |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name in CANDIDATE_ORDER:
        item = fixed[name]
        delta = result["fixed_improvements_vs_baseline"][name]
        lines.append(
            f"| `{name}` | {item['within_25_percent_events']:.2f}% | {item['all_events_within_25_percent_runs']:.2f}% ({item['all_events_within_25_runs']}/{item['n_runs']}) | {item['mean_ape_percent']:.2f}% | {item['p95_ape_percent_nearest_rank']:.2f}% | {item['max_ape_percent']:.2f}% | {delta['within_25_event_pp_vs_baseline']:+.2f} pp | {delta['all_events_run_pp_vs_baseline']:+.2f} pp |"
        )
    best = max(CANDIDATE_ORDER[1:], key=lambda name: fixed[name]["within_25_percent_events"])
    best_delta = result["fixed_improvements_vs_baseline"][best]
    tail_safe = best_delta["worst_change_percent_vs_baseline"] <= 0 and best_delta["p95_change_percent_vs_baseline"] <= 0
    meaningful = best_delta["within_25_event_pp_vs_baseline"] >= 1.0 and best_delta["all_events_run_pp_vs_baseline"] >= 1.0 and tail_safe
    lines.extend([
        "",
        f"The highest event-level coverage among feature models is `{best}`. Relative to the baseline it changes event coverage by **{best_delta['within_25_event_pp_vs_baseline']:+.2f} percentage points**, the all-events-per-run gate by **{best_delta['all_events_run_pp_vs_baseline']:+.2f} points**, p95 APE by **{best_delta['p95_change_percent_vs_baseline']:+.2f} points**, and worst APE by **{best_delta['worst_change_percent_vs_baseline']:+.2f} points**.",
        "",
        f"Using a predeclared review flag of at least 1 percentage point improvement on both event and all-events-per-run coverage with no tail worsening, meaningful conditional improvement is **{'supported' if meaningful else 'not supported'}** by this comparison. The raw metrics remain the evidence; this flag does not create a D9 acceptance criterion.",
        "",
        "## Nested grouped selection",
        "",
        f"The inner procedure selected `{result['full_training_selected_candidate']}` on the full retained partition. Nested outer-fold selected-procedure metrics are: `{json.dumps(result['nested_selected_outer_oof_metrics'], sort_keys=True)}`. Outer-fold choices are recorded in `comparison.json`; no outer-test target was used to fit a fold model.",
        "",
        "## All-events-per-run gate",
        "",
        "A run passes this conditional request gate only when every retained request event in that run is within 25 percent. This is not the complete assignment D9 gate: it does not include CPU events, native GPU phases, lifecycle overhead, or a start-known E2E forecast. It must not be combined with those populations as if they were jointly measured.",
        "",
        "## Reproduction artifacts",
        "",
        f"- `run_conditional_gpu_models.py` is the runnable bounded script; it reads only the named training view and manifest.\n- `predictions.jsonl` contains fixed outer-OOF predictions for all four candidates ({len(CANDIDATE_ORDER)} predictions per retained event); SHA-256: `{predictions_sha}`.\n- `fit_artifact.json` contains full-train coefficients and the selected candidate for offline review.\n- `comparison.json` contains metrics, fold choices, and model definitions.\n- `provenance.json` binds source hashes, script hash, population, feature contract, and exclusions.",
        "",
        "No acquisition, inference, existing module, hardware state, or production configuration was changed.",
        "",
    ])
    return "\n".join(lines)


if __name__ == "__main__":
    main()
