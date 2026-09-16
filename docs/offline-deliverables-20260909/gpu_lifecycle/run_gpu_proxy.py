#!/usr/bin/env python3
"""Bounded, grouped-fold prospective GPU historical-proxy comparison.

The input view was identity-filtered before this script runs.  This script
whitelists input_tokens and max_output_tokens, treats observed_ms only as a
target, and deliberately never reads output_tokens as a feature.
"""
from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[3]
VIEW = ROOT / "docs" / "offline-deliverables-20260909" / "training_view" / "models.jsonl"
OUT = Path(__file__).with_name("gpu_proxy_results.json")
OOF_OUT = Path(__file__).with_name("gpu_proxy_oof_predictions.jsonl")
MODEL_OUT = Path(__file__).with_name("gpu_proxy_chosen_model.json")


def rows() -> list[dict[str, Any]]:
    result = [json.loads(line) for line in VIEW.read_text(encoding="utf-8").splitlines() if line]
    allowed = {"instance_id", "run_id", "request_id", "outer_fold", "input_tokens", "context_tokens", "max_output_tokens", "observed_ms", "output_tokens"}
    if any(set(row) - allowed for row in result):
        raise ValueError("unexpected common-view model field")
    if any(not isinstance(row["observed_ms"], (int, float)) or row["observed_ms"] <= 0 for row in result):
        raise ValueError("GPU target must be positive")
    return result


def inner_fold(instance_id: str) -> int:
    return int(hashlib.sha256(("assignment.d9.gpu-inner-fold-v1:" + instance_id).encode()).hexdigest(), 16) % 3


def bucket(row: dict[str, Any]) -> tuple[int, int]:
    return (int(math.log2(max(1, int(row["input_tokens"])))), int(row["max_output_tokens"]))


def solve(a: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    augmented = [a[i][:] + [b[i]] for i in range(n)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda row: abs(augmented[row][col]))
        if abs(augmented[pivot][col]) < 1e-12:
            raise ValueError("singular ridge system")
        augmented[col], augmented[pivot] = augmented[pivot], augmented[col]
        scale = augmented[col][col]
        augmented[col] = [value / scale for value in augmented[col]]
        for row in range(n):
            if row == col:
                continue
            scale = augmented[row][col]
            augmented[row] = [x - scale * y for x, y in zip(augmented[row], augmented[col])]
    return [row[-1] for row in augmented]


def fit_global(train: list[dict[str, Any]]) -> Callable[[dict[str, Any]], float]:
    center = statistics.median(float(row["observed_ms"]) for row in train)
    return lambda _row: center


def fit_prompt_cap_median(train: list[dict[str, Any]]) -> Callable[[dict[str, Any]], float]:
    global_predict = fit_global(train)
    groups: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for row in train:
        groups[bucket(row)].append(row)
    medians = {
        key: statistics.median(float(row["observed_ms"]) for row in group)
        for key, group in groups.items()
        if len(group) >= 25 and len({row["instance_id"] for row in group}) >= 3
    }
    return lambda row: float(medians.get(bucket(row), global_predict(row)))


def fit_positive_log_ridge(train: list[dict[str, Any]]) -> Callable[[dict[str, Any]], float]:
    # Input/context are identical in this cache, so context is intentionally
    # excluded rather than pretending both coefficients are identifiable.
    x = [(1.0, math.log1p(float(row["input_tokens"])), math.log1p(float(row["max_output_tokens"]))) for row in train]
    y = [math.log(float(row["observed_ms"])) for row in train]
    width = len(x[0])
    gram = [[sum(row[i] * row[j] for row in x) + (0.1 if i == j and i > 0 else 0.0) for j in range(width)] for i in range(width)]
    rhs = [sum(row[i] * target for row, target in zip(x, y)) for i in range(width)]
    coefficients = solve(gram, rhs)
    return lambda row: max(1e-6, math.exp(coefficients[0] + coefficients[1] * math.log1p(float(row["input_tokens"])) + coefficients[2] * math.log1p(float(row["max_output_tokens"]))))


CANDIDATES: dict[str, Callable[[list[dict[str, Any]]], Callable[[dict[str, Any]], float]]] = {
    "global_median": fit_global,
    "prompt_log2_bucket_and_cap_median": fit_prompt_cap_median,
    "positive_log_ridge_prompt_and_cap": fit_positive_log_ridge,
}


def predict_refitted_model(model: dict[str, Any], request: dict[str, Any]) -> float:
    """Small serving API for the packaged candidate; accepts no label fields."""
    if set(request) - {"input_tokens", "max_output_tokens"}:
        raise ValueError("prediction request contains non-prospective fields")
    if model.get("candidate") != "global_median" or isinstance(model.get("median_ms"), bool) or not isinstance(model.get("median_ms"), (int, float)) or not math.isfinite(model["median_ms"]) or model["median_ms"] <= 0:
        raise ValueError("unsupported packaged GPU proxy model")
    return float(model["median_ms"])


def metrics(pairs: list[tuple[float, float]]) -> dict[str, float | int]:
    apes = [100.0 * abs(predicted - observed) / observed for predicted, observed in pairs]
    ordered = sorted(apes)
    return {
        "n": len(apes),
        "within_25_percent": 100.0 * sum(value <= 25.0 for value in apes) / len(apes),
        "mean_ape_percent": statistics.mean(apes),
        "p95_ape_percent_nearest_rank": ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)],
        "max_ape_percent": max(apes),
    }


def choose(train: list[dict[str, Any]]) -> tuple[str, dict[str, dict[str, float | int]]]:
    oof: dict[str, list[tuple[float, float]]] = {name: [] for name in CANDIDATES}
    for fold in range(3):
        inner_train = [row for row in train if inner_fold(str(row["instance_id"])) != fold]
        inner_test = [row for row in train if inner_fold(str(row["instance_id"])) == fold]
        for name, factory in CANDIDATES.items():
            predictor = factory(inner_train)
            oof[name].extend((predictor(row), float(row["observed_ms"])) for row in inner_test)
    summary = {name: metrics(values) for name, values in oof.items()}
    baseline = summary["global_median"]
    allowed = [
        name for name, value in summary.items()
        if value["within_25_percent"] >= baseline["within_25_percent"]
        and value["max_ape_percent"] <= baseline["max_ape_percent"]
    ]
    # Fixed protocol: coverage, then worst error, then simpler candidate order.
    selected = sorted(allowed, key=lambda name: (-float(summary[name]["within_25_percent"]), float(summary[name]["max_ape_percent"]), list(CANDIDATES).index(name)))[0]
    return selected, summary


def main() -> None:
    data = rows()
    fixed_oof: dict[str, list[tuple[float, float]]] = {name: [] for name in CANDIDATES}
    selected_oof: list[tuple[float, float]] = []
    selected_oof_rows: list[dict[str, Any]] = []
    choices = []
    for outer in range(5):
        train = [row for row in data if int(row["outer_fold"]) != outer]
        test = [row for row in data if int(row["outer_fold"]) == outer]
        selected, inner = choose(train)
        choices.append({"outer_fold": outer, "selected": selected, "inner_metrics": inner, "outer_test_events": len(test), "outer_test_instances": len({row["instance_id"] for row in test})})
        for name, factory in CANDIDATES.items():
            predictor = factory(train)
            pairs = [(predictor(row), float(row["observed_ms"])) for row in test]
            fixed_oof[name].extend(pairs)
            if name == selected:
                selected_oof.extend(pairs)
                selected_oof_rows.extend(
                    {
                        "request_id": row["request_id"],
                        "run_id": row["run_id"],
                        "instance_id": row["instance_id"],
                        "outer_fold": outer,
                        "selected_candidate": selected,
                        "predicted_ms": predicted,
                        "observed_ms": observed,
                        "ape_percent": 100.0 * abs(predicted - observed) / observed,
                        "within_25_percent": 100.0 * abs(predicted - observed) / observed <= 25.0,
                    }
                    for row, (predicted, observed) in zip(test, pairs)
                )
    final_candidate, final_inner = choose(data)
    payload = {
        "schema_version": "assignment.d9.gpu-proxy-experiment.v1",
        "data": {"rows": len(data), "instances": len({row["instance_id"] for row in data}), "runs": len({row["run_id"] for row in data}), "source": str(VIEW.relative_to(ROOT))},
        "target": "historical model-event observed_ms proxy; not native queue/prefill/decode and not a repaired-native result",
        "coverage_limit": "the saved historical view contains completed model events only; it cannot establish the all-required-event D9 denominator or universal acceptance gate",
        "prediction_time_features": ["input_tokens", "max_output_tokens"],
        "feature_availability_contract": "historical proxy assumes input_tokens was available before request; the repaired native fixture does not establish that condition and uses cap-only fallback",
        "excluded_from_features": ["context_tokens (identical to input_tokens in this cache)", "output_tokens", "observed_ms", "all phase timings", "cache state", "future actions", "residual"],
        "outer_folds": "preexisting grouped instance folds from common training view",
        "inner_folds": "sha256('assignment.d9.gpu-inner-fold-v1:' + instance_id) modulo 3",
        "selection_rule": "maximize inner within-25 coverage subject to maximum APE no worse than global median; then lower worst APE; then simpler",
        "fixed_candidate_oof_metrics": {name: metrics(values) for name, values in fixed_oof.items()},
        "nested_selected_oof_metrics": metrics(selected_oof),
        "outer_fold_choices": choices,
        "full_training_selection": final_candidate,
        "full_training_inner_metrics": final_inner,
        "e2e": {"start_known_status": "unsupported: no authorized start-known full action/request-list feature view", "event_sum_status": "not computed: needs CPU OOF predictions and would be conditional on a realized action/request list", "residual_fit": "not performed"},
    }
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with OOF_OUT.open("w", encoding="utf-8") as handle:
        for row in sorted(selected_oof_rows, key=lambda row: (row["run_id"], row["request_id"])):
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    if final_candidate != "global_median":
        raise ValueError("selected candidate requires a matching serializer; do not substitute a global model")
    final_model = {
        "schema_version": "assignment.d9.gpu-proxy-model.v1",
        "candidate": final_candidate,
        "median_ms": statistics.median(float(row["observed_ms"]) for row in data),
        "fit_partition": "train_calibration",
        "fit_instances": len({row["instance_id"] for row in data}),
        "prediction_time_fields": [],
        "forbidden_fields": ["output_tokens", "observed_ms", "phase_timings", "cache_state", "residual", "future_actions"],
        "availability": "feature-free historical-proxy baseline; no input-token availability assumption",
        "target": "historical completed-request wall proxy; not native GPU service or queue/prefill/decode",
        "hardware_scope": "historical training environment only; cross-hardware transfer unvalidated",
        "deployment_status": "offline candidate only; not adopted or production-integrated",
        "source_sha256": hashlib.sha256(VIEW.read_bytes()).hexdigest(),
        "selection": "grouped inner validation on full training partition; not outer-test choices",
        "api": "predict_refitted_model(model, {})",
    }
    MODEL_OUT.write_text(json.dumps(final_model, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
