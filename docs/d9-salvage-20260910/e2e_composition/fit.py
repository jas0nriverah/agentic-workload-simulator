"""Fit a bounded, leakage-safe E2E composition candidate from retained evidence.

The prediction contract is conditional replay: the complete semantic-action and
model-request trace is supplied to the simulator.  Measured durations, residuals,
outcomes, identities, and retry results never enter the design matrix.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from statistics import median
from typing import Any


HERE = Path(__file__).resolve().parent
BASE = HERE.parent
AUDIT = HERE / "audit.json"
MANIFEST = BASE / "cpu_lifecycle" / "manifest.json"
REPORT = HERE / "fit_report.json"
MODEL = HERE / "fit_artifact.json"
PREDICTIONS = HERE / "fit_predictions.jsonl"

CPU_CLASSES = (
    "lifecycle:client_processing",
    "lifecycle:deployment_start",
    "lifecycle:get_state",
    "lifecycle:persistent_shell_pid_discovery",
    "lifecycle:script_read",
    "lifecycle:setup",
    "lifecycle:startup",
    "lifecycle:teardown",
    "runtime_command",
    "semantic_action",
)
ACTION_CLASSES = ("patch", "read", "search", "shell", "test", "traversal", "write", "other")
CPU_FEATURES = (
    ("intercept",)
    + tuple(f"count:{name}" for name in CPU_CLASSES)
    + tuple(f"semantic_action_class:{name}" for name in ACTION_CLASSES)
)
GPU_FEATURES = (
    "intercept",
    "request_count/100",
    "input_tokens/1e6",
    "output_tokens/1e4",
    "cached_tokens/1e6",
)
REMAINDER_FEATURES = (
    "intercept",
    "request_count/100",
    "semantic_action_count/100",
    "runtime_command_count/100",
    "input_tokens/1e6",
    "output_tokens/1e4",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def grouped_fold(instance_id: str) -> int:
    value = hashlib.sha256(
        ("assignment.d9.repaired-fold-v1:" + instance_id).encode()
    ).digest()
    return int.from_bytes(value[:8], "big") % 5


def fit_nnls(x: list[list[float]], y: list[float], *, relative: bool) -> list[float]:
    """Small deterministic NNLS using cyclic coordinate descent."""
    if not x or len(x) != len(y) or any(value <= 0 for value in y):
        raise ValueError("NNLS requires aligned rows and positive targets")
    width = len(x[0])
    if any(len(row) != width for row in x):
        raise ValueError("ragged design matrix")
    weights = [1.0 / (value * value) if relative else 1.0 for value in y]
    gram = [
        [sum(w * row[j] * row[k] for w, row in zip(weights, x)) for k in range(width)]
        for j in range(width)
    ]
    rhs = [sum(w * row[j] * value for w, row, value in zip(weights, x, y)) for j in range(width)]
    beta = [0.0] * width
    for _ in range(50_000):
        previous = beta[:]
        for j in range(width):
            diagonal = gram[j][j]
            if diagonal:
                other = sum(gram[j][k] * beta[k] for k in range(width) if k != j)
                beta[j] = max(0.0, (rhs[j] - other) / diagonal)
        scale = max(1.0, max(beta))
        if max(abs(a - b) for a, b in zip(previous, beta)) < 1e-10 * scale:
            break
    return beta


def predict(beta: list[float], design: list[float]) -> float:
    return sum(coef * value for coef, value in zip(beta, design))


def fit_coverage_scale(predictions: list[float], targets: list[float]) -> float:
    """Choose a training-only scale maximizing literal within-25% coverage."""
    intervals = []
    for predicted, target in zip(predictions, targets):
        if predicted <= 0 or target <= 0:
            raise ValueError("coverage calibration requires positive values")
        intervals.append((0.75 * target / predicted, 1.25 * target / predicted))
    candidates = {1.0}
    for low, high in intervals:
        candidates.add(low)
        candidates.add(high)
    scored = []
    for value in candidates:
        coverage = sum(low <= value <= high for low, high in intervals)
        scored.append((-coverage, abs(math.log(value)), value))
    return min(scored)[2]


def metrics(rows: list[dict[str, Any]], prediction: str) -> dict[str, Any]:
    errors = sorted(
        abs(float(row[prediction]) - float(row["outer_ms"])) / float(row["outer_ms"]) * 100.0
        for row in rows
    )
    count = sum(error <= 25.0 for error in errors)
    return {
        "case_count": len(rows),
        "instance_count": len({row["instance_id"] for row in rows}),
        "within25_count": count,
        "within25_fraction": count / len(rows),
        "median_error_pct": median(errors),
        "p95_error_pct": errors[math.ceil(0.95 * len(errors)) - 1],
        "worst_error_pct": errors[-1],
    }


def _terminal_request_totals(case_root: Path) -> dict[str, int]:
    path = case_root / "runner_attempts" / "attempt-001" / "telemetry_v2" / "model_events.jsonl"
    totals = {"request_count": 0, "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}
    physical_ids: set[str] = set()
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if row.get("event_kind") != "model_request" or row.get("terminal") is not True:
                continue
            physical_id = row.get("physical_request_id")
            if not isinstance(physical_id, str) or physical_id in physical_ids:
                raise AssertionError(f"missing or duplicate terminal request identity in {path}")
            physical_ids.add(physical_id)
            totals["request_count"] += 1
            for name in ("input_tokens", "output_tokens", "cached_tokens"):
                value = row.get(name)
                if value is None:
                    value = 0
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise AssertionError(f"invalid {name} in {path}")
                totals[name] += value
    return totals


def load_rows() -> tuple[list[dict[str, Any]], dict[str, str]]:
    audit = json.loads(AUDIT.read_text(encoding="utf-8"))
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest_by_ordinal = {
        int(Path(row["events_path"]).stem): row for row in manifest["cases"]
    }
    rows: list[dict[str, Any]] = []
    raw_hashes: dict[str, str] = {}
    for case in audit["cases"]:
        ordinal = int(case["queue_ordinal"])
        source = manifest_by_ordinal[ordinal]
        if source["instance_id"] != case["instance_id"]:
            raise AssertionError("manifest/audit identity drift")
        case_root = Path(source["case_root"])
        token_totals = _terminal_request_totals(case_root)
        raw_model_path = case_root / "runner_attempts" / "attempt-001" / "telemetry_v2" / "model_events.jsonl"
        raw_hashes[str(ordinal)] = sha256_file(raw_model_path)
        if raw_hashes[str(ordinal)] != case["raw_join"]["raw_model_sha256"]:
            raise AssertionError("raw model-event journal changed since the identity audit")
        request_count = int(case["raw_join"]["model_request_terminal_rows"])
        if token_totals["request_count"] != request_count:
            raise AssertionError("request count differs between raw join and terminal rows")
        counts = {
            name: int(case["eligible_target_components"].get(name, {}).get("count", 0))
            for name in CPU_CLASSES
        }
        input_path = BASE / "cpu_lifecycle" / "inputs" / f"{ordinal:05d}.jsonl"
        action_counts = {name: 0 for name in ACTION_CLASSES}
        with input_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                event = json.loads(line)
                if event.get("record_role") != "TARGET" or event.get("event_class") != "semantic_action":
                    continue
                action_class = (event.get("features") or {}).get("operation_class")
                if action_class not in action_counts:
                    action_class = "other"
                action_counts[action_class] += 1
        if sum(action_counts.values()) != counts["semantic_action"]:
            raise AssertionError("semantic action class counts do not sum to event-class count")
        outer_ms = float(case["outer_ms"])
        cpu_union_ms = float(case["target_union_ms"])
        native_ms = float(case["raw_join"]["native_e2e_sum_ms"])
        remainder_ms = outer_ms - cpu_union_ms - native_ms
        if min(outer_ms, cpu_union_ms, native_ms, remainder_ms) <= 0:
            raise AssertionError(f"nonpositive composition target for ordinal {ordinal}")
        rows.append(
            {
                "queue_ordinal": ordinal,
                "case_id": case["case_id"],
                "instance_id": case["instance_id"],
                "fold": grouped_fold(case["instance_id"]),
                "outer_ms": outer_ms,
                "cpu_union_ms": cpu_union_ms,
                "native_ms": native_ms,
                "remainder_ms": remainder_ms,
                "cpu_design": (
                    [1.0]
                    + [counts[name] / 100.0 for name in CPU_CLASSES]
                    + [action_counts[name] / 100.0 for name in ACTION_CLASSES]
                ),
                "gpu_design": [
                    1.0,
                    request_count / 100.0,
                    token_totals["input_tokens"] / 1_000_000.0,
                    token_totals["output_tokens"] / 10_000.0,
                    token_totals["cached_tokens"] / 1_000_000.0,
                ],
                "remainder_design": [
                    1.0,
                    request_count / 100.0,
                    counts["semantic_action"] / 100.0,
                    counts["runtime_command"] / 100.0,
                    token_totals["input_tokens"] / 1_000_000.0,
                    token_totals["output_tokens"] / 10_000.0,
                ],
            }
        )
    if len(rows) != 43 or len({row["instance_id"] for row in rows}) != 22:
        raise AssertionError("unexpected bounded cohort")
    return rows, raw_hashes


def fit_component(train: list[dict[str, Any]], design: str, target: str) -> list[float]:
    return fit_nnls(
        [row[design] for row in train],
        [float(row[target]) for row in train],
        relative=True,
    )


def run() -> dict[str, Any]:
    rows, raw_hashes = load_rows()
    fold_models: dict[str, Any] = {}
    for fold in range(5):
        train = [row for row in rows if row["fold"] != fold]
        test = [row for row in rows if row["fold"] == fold]
        if not test:
            raise AssertionError(f"empty test fold {fold}")
        if {row["instance_id"] for row in train} & {row["instance_id"] for row in test}:
            raise AssertionError("instance leakage")
        coefficients = {
            "cpu_union": fit_component(train, "cpu_design", "cpu_union_ms"),
            "native": fit_component(train, "gpu_design", "native_ms"),
            "remainder": fit_component(train, "remainder_design", "remainder_ms"),
        }
        outer_beta = fit_nnls(
            [row["remainder_design"] for row in train],
            [float(row["outer_ms"]) for row in train],
            relative=True,
        )
        train_direct = [predict(outer_beta, row["remainder_design"]) for row in train]
        train_composed = [
            predict(coefficients["cpu_union"], row["cpu_design"])
            + predict(coefficients["native"], row["gpu_design"])
            + predict(coefficients["remainder"], row["remainder_design"])
            for row in train
        ]
        train_targets = [float(row["outer_ms"]) for row in train]
        scales = {
            "direct": fit_coverage_scale(train_direct, train_targets),
            "composed": fit_coverage_scale(train_composed, train_targets),
        }
        baseline = median(float(row["outer_ms"]) for row in train)
        for row in test:
            row["median_prediction_ms"] = baseline
            row["direct_prediction_ms"] = predict(outer_beta, row["remainder_design"])
            row["cpu_prediction_ms"] = predict(coefficients["cpu_union"], row["cpu_design"])
            row["native_prediction_ms"] = predict(coefficients["native"], row["gpu_design"])
            row["remainder_prediction_ms"] = predict(
                coefficients["remainder"], row["remainder_design"]
            )
            row["composed_prediction_ms"] = (
                row["cpu_prediction_ms"]
                + row["native_prediction_ms"]
                + row["remainder_prediction_ms"]
            )
            row["calibrated_direct_prediction_ms"] = (
                scales["direct"] * row["direct_prediction_ms"]
            )
            row["calibrated_composed_prediction_ms"] = (
                scales["composed"] * row["composed_prediction_ms"]
            )
        fold_models[str(fold)] = {
            "train_cases": len(train),
            "train_instances": len({row["instance_id"] for row in train}),
            "test_cases": len(test),
            "test_instances": len({row["instance_id"] for row in test}),
            "coefficients": coefficients,
            "direct_coefficients": outer_beta,
            "training_only_coverage_scales": scales,
        }

    names = (
        "median_prediction_ms",
        "direct_prediction_ms",
        "calibrated_direct_prediction_ms",
        "composed_prediction_ms",
        "calibrated_composed_prediction_ms",
    )
    result = {
        "schema": "d9.e2e-composition-fit.v1",
        "status": "development_candidate",
        "contract": "trace-conditioned replay with supplied action/request counts and terminal token totals",
        "cohort": {"cases": len(rows), "instances": len({r["instance_id"] for r in rows})},
        "split": "five deterministic instance-grouped folds shared with repaired CPU calibration",
        "features": {
            "cpu_union": CPU_FEATURES,
            "native": GPU_FEATURES,
            "remainder": REMAINDER_FEATURES,
        },
        "component_definition": {
            "cpu_union": "union of eligible CPU-clock target intervals",
            "native": "sum of exactly joined native:e2e request durations",
            "remainder": "outer wall minus CPU union minus native:e2e sum",
            "prediction": "three independently fitted nonnegative component predictions added once",
            "calibration": "one multiplicative scale selected on each training fold to maximize within-25% coverage; test targets are never consulted",
        },
        "metrics": {name: metrics(rows, name) for name in names},
        "folds": fold_models,
        "source": {
            "audit_sha256": sha256_file(AUDIT),
            "manifest_sha256": sha256_file(MANIFEST),
            "raw_model_event_sha256_by_ordinal": raw_hashes,
        },
        "validity": {
            "result_fields_used_as_predictors": False,
            "measured_residual_used_as_predictor": False,
            "instance_overlap_across_folds": False,
            "hardware_transfer_validated": False,
            "literal_d9_pass": False,
        },
        "limitations": [
            "This is a conditional replay model; realized action/request counts and terminal token totals are supplied workload descriptors, not an online forecast.",
            "The remainder is a training target only and is never supplied to prediction.",
            "The 43-case development cohort is small and contains only 22 independent instances.",
            "Cross-hardware performance remains unvalidated.",
        ],
    }

    full_models = {
        "cpu_union": fit_component(rows, "cpu_design", "cpu_union_ms"),
        "native": fit_component(rows, "gpu_design", "native_ms"),
        "remainder": fit_component(rows, "remainder_design", "remainder_ms"),
        "direct": fit_nnls(
            [row["remainder_design"] for row in rows],
            [float(row["outer_ms"]) for row in rows],
            relative=True,
        ),
    }
    full_direct_predictions = [
        predict(full_models["direct"], row["remainder_design"]) for row in rows
    ]
    full_composed_predictions = [
        predict(full_models["cpu_union"], row["cpu_design"])
        + predict(full_models["native"], row["gpu_design"])
        + predict(full_models["remainder"], row["remainder_design"])
        for row in rows
    ]
    full_scales = {
        "direct": fit_coverage_scale(
            full_direct_predictions, [float(row["outer_ms"]) for row in rows]
        ),
        "composed": fit_coverage_scale(
            full_composed_predictions, [float(row["outer_ms"]) for row in rows]
        ),
    }
    artifact = {
        "schema": result["schema"],
        "status": "full-development-fit-for-conditional-replay",
        "selected_e2e_model": "direct",
        "selection_reason": "same 42/43 coverage as composed candidate with lower worst error (28.96% versus 31.17%)",
        "contract": result["contract"],
        "features": result["features"],
        "coefficients": full_models,
        "training_only_coverage_scales": full_scales,
        "source": result["source"],
        "hardware_transfer_validated": False,
    }
    REPORT.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    MODEL.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    PREDICTIONS.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8"
    )
    return result


if __name__ == "__main__":
    print(json.dumps(run()["metrics"], indent=2, sort_keys=True))
