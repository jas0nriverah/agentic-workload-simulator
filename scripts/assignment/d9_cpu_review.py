#!/usr/bin/env python3
"""Calibration-only D9 CPU-model comparisons.

This runner deliberately has a smaller data boundary than the original D9
calibration script. It reads the preserved calibration cache and the
preserved action map only. In particular, it never invokes
``d9_cpu_calibrate.build_cache`` or walks recovery/protocol/holdout sources.

The comparison is intentionally a metrics artifact, rather than a model-choice
report. Four fixed CPU candidates are evaluated with three outer-fold
protocols. The GPU event formula and ridge alpha are held fixed for every
candidate and every fold.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import random
import statistics
import sys
from typing import Any, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

# This import only provides constants and row helpers; this module's cache
# builder/main are never called here.
from scripts.assignment.d9_cpu_calibrate import CACHE, tool_input_row  # noqa: E402
from scripts.assignment.d9_assignment_eval import HARDWARE, model_input  # noqa: E402
from agentic_sim.assignment.cpu_event_model import HierarchicalMedianModel, row_from_tool_input  # noqa: E402
from agentic_sim.assignment.event_simulator import (  # noqa: E402
    EventSimulatorError,
    ModelEventInput,
    _solve_ridge,
)
from agentic_sim.assignment.workload_simulator import (  # noqa: E402
    WorkloadToolInput,
    e2e_design,
    gpu_design,
    workload_cpu_row,
)

try:  # The semantic-model agent provides this separate module.
    from agentic_sim.assignment.semantic_cpu_model import SemanticCpuModel  # type: ignore
except ImportError:  # pragma: no cover - useful while the sibling change is in flight
    SemanticCpuModel = None  # type: ignore[assignment,misc]


ASSIGN = Path("/home/riverahernandezjason/h100-assignment-work-20260905/assignment")
OUT = ASSIGN / "submission" / "20260908T060000Z" / "d9-cpu-review"
ACTION_CACHE = OUT / "calibration_actions.json"
PRESERVED_AUDIT = OUT / "initial_audit.json"
PRESERVED_PROVENANCE = OUT / "provenance.json"

FOLDS = 5
RIDGE_ALPHA = 1e-3
BOOTSTRAP_REPS = 1000
BOOTSTRAP_SEED = 20260908
QUARANTINED_INSTANCE = "sympy__sympy-12481"
GATE_PERCENT = 25.0
HEAVY_CLASSES = frozenset(("shell", "test", "traversal", "other"))

HISTORICAL_BASELINE_REFERENCE = {
    "original_cohort": {
        "within_25_rate": 0.7320504053922048,
        "mean_ape": 46.49201576592535,
    },
    "served_feature_reproduction": {
        "within_25_rate": 0.7320504053922048,
        "mean_ape": 46.492028011586456,
        "changed_prediction_events": 4,
        "total_signed_mass_delta_ms": 1.056,
    },
    "status": "preserved historical reference; not rerun and not used for fitting",
}


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def write(name: str, obj: Any, *, preserve: bool = False) -> Path:
    """Write deterministic JSON plus a checksum sidecar."""

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    if preserve and path.exists():
        return path
    data = (json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
    path.write_bytes(data)
    path.with_suffix(path.suffix + ".sha256").write_text(
        _sha256(data) + "  " + path.name + "\n", encoding="ascii"
    )
    return path


def fold_of(value: str) -> int:
    """The assignment's deterministic five-fold hash."""

    return int(hashlib.sha256(str(value).encode()).hexdigest(), 16) % FOLDS


def _positive(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite and positive")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return result


def _prediction(value: Any, name: str) -> float:
    """Validate predictions without clipping or silently repairing them."""

    return _positive(value, name)


def _metadata_only_quarantine(raw: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Remove holdout and known contaminated instance using metadata only."""

    trajectories = list(raw.get("trajectories") or [])
    excluded_runs: set[str] = set()
    holdout_run = str(raw.get("holdout_excluded") or "")
    if holdout_run:
        excluded_runs.add(holdout_run)
    quarantined_runs: set[str] = set()
    for row in trajectories:
        run_id = str(row.get("run_id") or "")
        if row.get("instance_id") == QUARANTINED_INSTANCE:
            quarantined_runs.add(run_id)
            excluded_runs.add(run_id)

    def keep(row: Mapping[str, Any]) -> bool:
        return str(row.get("run_id") or "") not in excluded_runs and row.get(
            "instance_id"
        ) != QUARANTINED_INSTANCE

    # Only metadata identifiers are touched here. Excluded observed labels are
    # never validated, scored, or sent to a model.
    kept_tools = [dict(row) for row in (raw.get("tools") or []) if keep(row)]
    kept_models = [dict(row) for row in (raw.get("models") or []) if keep(row)]
    kept_trajs = [dict(row) for row in trajectories if keep(row)]
    summary = {
        "cache_holdout_run_id": holdout_run,
        "quarantined_instance_id": QUARANTINED_INSTANCE,
        "quarantined_run_ids": sorted(quarantined_runs),
        "excluded_run_ids": sorted(excluded_runs),
        "raw_counts": {
            "tools": len(raw.get("tools") or []),
            "models": len(raw.get("models") or []),
            "trajectories": len(trajectories),
        },
        "excluded_counts": {
            "tools": len(raw.get("tools") or []) - len(kept_tools),
            "models": len(raw.get("models") or []) - len(kept_models),
            "trajectories": len(trajectories) - len(kept_trajs),
        },
        "retained_counts": {
            "tools": len(kept_tools),
            "models": len(kept_models),
            "trajectories": len(kept_trajs),
        },
        "exclusion_policy": "metadata-only before label validation/fitting/scoring",
    }
    return {
        "holdout_excluded": holdout_run,
        "tools": kept_tools,
        "models": kept_models,
        "trajectories": kept_trajs,
        "_exclusions": summary,
    }, summary


def _validate_retained_labels(data: Mapping[str, Any]) -> None:
    """Reject bad labels in retained rows only."""

    for kind in ("tools", "models", "trajectories"):
        rows = data.get(kind) or []
        seen: set[str] = set()
        identity = "event_id" if kind == "tools" else "request_id" if kind == "models" else "run_id"
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                raise ValueError(f"{kind}[{index}] must be a mapping")
            key = str(row.get(identity) or "")
            if not key:
                raise ValueError(f"{kind}[{index}] missing {identity}")
            if key in seen:
                raise ValueError(f"duplicate retained {identity}: {key}")
            seen.add(key)
            _positive(row.get("observed_ms"), f"{kind}[{index}].observed_ms")
            run_id = str(row.get("run_id") or "")
            if not run_id:
                raise ValueError(f"{kind}[{index}] missing run_id")
    traj_runs = {str(row["run_id"]) for row in data["trajectories"]}
    for kind in ("tools", "models"):
        unknown = {str(row["run_id"]) for row in data[kind]} - traj_runs
        if unknown:
            raise ValueError(f"{kind} rows have no retained trajectory: {sorted(unknown)[:3]}")


def load() -> dict[str, Any]:
    """Load only the preserved Grok calibration cache."""

    if not CACHE.is_file():
        raise FileNotFoundError(f"preserved calibration cache missing: {CACHE}")
    raw = json.loads(CACHE.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("calibration cache must be a JSON object")
    data, exclusions = _metadata_only_quarantine(raw)
    _validate_retained_labels(data)
    data["_cache_sha256"] = _sha256(CACHE.read_bytes())
    data["_exclusions"] = exclusions
    return data


def recover(data: Mapping[str, Any]) -> dict[str, str]:
    """Load the preserved action map; raw recovery is intentionally disabled."""

    if not ACTION_CACHE.is_file():
        raise FileNotFoundError(
            f"preserved calibration action map missing: {ACTION_CACHE}; raw recovery is disabled"
        )
    saved = json.loads(ACTION_CACHE.read_text(encoding="utf-8"))
    if not isinstance(saved, Mapping):
        raise ValueError("calibration_actions.json must be a JSON object")
    actions = {
        str(key): value for key, value in saved.items() if isinstance(key, str) and isinstance(value, str)
    }
    expected = {str(row["event_id"]): row for row in data.get("tools") or []}
    missing = sorted(set(expected) - set(actions))
    if missing:
        raise ValueError(f"preserved action map missing retained events: {missing[:3]}")
    for event_id, row in expected.items():
        digest = hashlib.sha256(actions[event_id].strip().encode()).hexdigest()
        if digest != row.get("command_sha256"):
            raise ValueError(f"preserved action hash mismatch for {event_id}")
    return {event_id: actions[event_id] for event_id in expected}


def _ape(pred: float, observed: float) -> float:
    return abs(pred - observed) / observed * 100.0


def _percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = q * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + fraction * (ordered[upper] - ordered[lower])


def metrics(pairs: Iterable[tuple[float, float]]) -> dict[str, Any]:
    """Compute error and gate metrics for positive prediction/label pairs."""

    materialized = [
        (_prediction(pred, "predicted_ms"), _positive(obs, "observed_ms"))
        for pred, obs in pairs
    ]
    if not materialized:
        return {
            "n": 0,
            "within25": None,
            "within_25_rate": None,
            "mean_ape": None,
            "median_ape": None,
            "p95_ape": None,
            "max_ape": None,
            "signed_error_total_ms": 0.0,
            "aggregate_signed_relative_error": None,
            "aggregate_absolute_error_ms": 0.0,
            "aggregate_absolute_relative_error": None,
            "absolute_aggregate_bias_ms": 0.0,
            "absolute_aggregate_bias_relative": None,
            "all_within25": False,
        }
    errors = [pred - obs for pred, obs in materialized]
    apes = [_ape(pred, obs) for pred, obs in materialized]
    observed_total = sum(obs for _pred, obs in materialized)
    signed_total = sum(errors)
    return {
        "n": len(materialized),
        "within25": sum(ape <= GATE_PERCENT for ape in apes),
        "within_25_rate": sum(ape <= GATE_PERCENT for ape in apes) / len(apes),
        "mean_ape": sum(apes) / len(apes),
        "median_ape": statistics.median(apes),
        "p95_ape": _percentile(apes, 0.95),
        "max_ape": max(apes),
        "signed_error_total_ms": signed_total,
        "aggregate_signed_relative_error": signed_total / observed_total,
        "aggregate_absolute_error_ms": sum(abs(error) for error in errors),
        "aggregate_absolute_relative_error": sum(abs(error) for error in errors) / observed_total,
        "absolute_aggregate_bias_ms": abs(signed_total),
        "absolute_aggregate_bias_relative": abs(signed_total) / observed_total,
        "all_within25": all(ape <= GATE_PERCENT for ape in apes),
    }


def _model_features(row: Mapping[str, Any], split: str = "calibration") -> ModelEventInput:
    payload = dict(row)
    payload["split"] = split
    return model_input(payload)


def _raw_ridge_predict(
    coefficients: Sequence[float],
    design: Sequence[float],
    name: str,
    *,
    floor_nonpositive: bool = False,
    raw_nonpositive_counter: list[int] | None = None,
) -> float:
    if len(coefficients) != len(design):
        raise ValueError(f"{name} ridge design width mismatch")
    raw = sum(float(a) * float(b) for a, b in zip(coefficients, design))
    if not math.isfinite(raw):
        raise ValueError(f"{name} must be finite")
    if raw <= 0:
        if raw_nonpositive_counter is not None:
            raw_nonpositive_counter[0] += 1
        if floor_nonpositive:
            # Match the frozen event-simulator ridge serving floor. The raw
            # count is surfaced in metrics; no rows are silently dropped.
            return 1e-6
        raise ValueError(f"{name} must be finite and positive")
    return _prediction(raw, name)


def _fit_fixed_ridge(
    design: Sequence[Sequence[float]], targets: Sequence[float], ids: Sequence[str]
) -> tuple[float, ...]:
    if not design or len(design) != len(targets):
        raise ValueError("ridge fit requires non-empty aligned rows")
    for index, target in enumerate(targets):
        _positive(target, f"ridge target {index}")
    try:
        coefficients = _solve_ridge(design, targets, RIDGE_ALPHA)
    except EventSimulatorError as exc:
        raise ValueError(f"ridge fit failed: {exc}") from exc
    if not all(math.isfinite(float(value)) for value in coefficients):
        raise ValueError("ridge fit produced non-finite coefficients")
    return tuple(float(value) for value in coefficients)


def _least_squares(design: Sequence[Sequence[float]], targets: Sequence[float]) -> tuple[float, ...]:
    """Use numpy for the tiny NNLS fits, with a dependency-free fallback."""

    try:
        import numpy as np  # type: ignore

        matrix = np.asarray(design, dtype=float)
        vector = np.asarray(targets, dtype=float)
        solution, _residuals, _rank, _singular = np.linalg.lstsq(matrix, vector, rcond=None)
        return tuple(float(value) for value in solution.tolist())
    except ImportError:  # pragma: no cover - assignment venv has numpy
        try:
            return tuple(float(value) for value in _solve_ridge(design, targets, 1e-12))
        except EventSimulatorError as exc:
            raise ValueError(f"least-squares fit failed: {exc}") from exc


def fit_nnls_overhead(
    n_tools: Sequence[float], n_models: Sequence[float], residual_targets: Sequence[float]
) -> dict[str, Any]:
    """Fit [1,n_tools,n_models] NNLS by enumerating active subsets."""

    if not n_tools or len(n_tools) != len(n_models) or len(n_tools) != len(residual_targets):
        raise ValueError("NNLS overhead fit requires non-empty aligned rows")
    if any(
        not math.isfinite(float(value))
        for values in (n_tools, n_models, residual_targets)
        for value in values
    ):
        raise ValueError("NNLS overhead fit received non-finite values")
    design = [(1.0, float(tools), float(models)) for tools, models in zip(n_tools, n_models)]
    target = [float(value) for value in residual_targets]
    best: tuple[float, tuple[float, ...], int] | None = None
    for mask in range(1 << 3):
        active = [index for index in range(3) if mask & (1 << index)]
        candidate = list(_least_squares([[row[index] for index in active] for row in design], target)) if active else []
        if any(value < -1e-8 for value in candidate):
            continue
        full = [0.0, 0.0, 0.0]
        for index, value in zip(active, candidate):
            full[index] = 0.0 if abs(value) < 1e-10 else float(value)
        sse = sum(
            (sum(coef * feature for coef, feature in zip(full, row)) - observed) ** 2
            for row, observed in zip(design, target)
        )
        key = (float(sse), tuple(full), mask)
        if best is None or key < best:
            best = key
    if best is None:
        raise ValueError("NNLS active-subset enumeration found no feasible solution")
    sse, coefficients, _mask = best
    return {
        "coefficients": list(coefficients),
        "features": ["intercept", "n_tools", "n_models"],
        "sse": float(sse),
        "n": len(target),
        "target": "observed_e2e - observed_tool_sum - observed_model_sum",
        "nonnegative": True,
        "fit_uses_observed_event_sums_only": True,
    }


def _overhead_predict(fit: Mapping[str, Any], n_tools: int, n_models: int) -> float:
    coefficients = [float(value) for value in fit["coefficients"]]
    return _prediction(
        coefficients[0] + coefficients[1] * n_tools + coefficients[2] * n_models,
        "predicted_overhead_ms",
    )


def _semantic_class(model: Any, row: Mapping[str, Any], prediction: Any = None) -> str | None:
    if isinstance(prediction, Mapping):
        for key in ("semantic_class", "class", "label", "semantic_label"):
            value = prediction.get(key)
            if isinstance(value, str) and value:
                return value
    if isinstance(prediction, (tuple, list)) and len(prediction) > 1:
        value = prediction[1]
        if isinstance(value, str) and value:
            return value
    for method_name in ("semantic_class", "predict_class", "classify", "semantic_label"):
        method = getattr(model, method_name, None)
        if callable(method):
            try:
                value = method(row)
            except (TypeError, KeyError, ValueError):
                continue
            if isinstance(value, str) and value:
                return value
    for attr_name in ("semantic_classes", "class_labels", "labels"):
        mapping = getattr(model, attr_name, None)
        if isinstance(mapping, Mapping):
            for key in (row.get("event_id"), row.get("command_sha256")):
                value = mapping.get(key)
                if isinstance(value, str) and value:
                    return value
    value = row.get("semantic_class")
    return value if isinstance(value, str) and value else None


def _prediction_value(raw: Any) -> float:
    if isinstance(raw, Mapping):
        for key in ("predicted_ms", "prediction", "value", "latency_ms"):
            if key in raw:
                return _prediction(raw[key], "predicted_cpu_ms")
        raise ValueError("CPU model prediction mapping has no numeric prediction")
    if isinstance(raw, (tuple, list)):
        if not raw:
            raise ValueError("CPU model returned an empty prediction")
        return _prediction(raw[0], "predicted_cpu_ms")
    return _prediction(raw, "predicted_cpu_ms")


def _fit_cpu_model(name: str, rows: Sequence[Mapping[str, Any]]) -> Any:
    if name == "baseline":
        model = HierarchicalMedianModel(min_count=8)
    else:
        if SemanticCpuModel is None:
            raise RuntimeError("SemanticCpuModel is not available yet")
        if name == "semantic_no_repo_median":
            model = SemanticCpuModel(center="median", use_repository=False, min_count=8, min_instances=3)
        elif name == "semantic_repo_median":
            model = SemanticCpuModel(center="median", use_repository=True, min_count=8, min_instances=3)
        elif name == "semantic_repo_gate":
            model = SemanticCpuModel(center="gate", use_repository=True, min_count=8, min_instances=3)
        else:
            raise ValueError(f"unknown fixed candidate: {name}")
    model.fit(rows)
    return model


CANDIDATES = (
    "baseline",
    "semantic_no_repo_median",
    "semantic_repo_median",
    "semantic_repo_gate",
)


def _feature_row(row: Mapping[str, Any], actions: Mapping[str, str]) -> dict[str, Any]:
    event_id = str(row["event_id"])
    if event_id not in actions:
        raise ValueError(f"missing preserved action for {event_id}")
    output = dict(row)
    output["action"] = actions[event_id]
    output.pop("observed_ms", None)
    return output


def _fit_row(row: Mapping[str, Any], actions: Mapping[str, str]) -> dict[str, Any]:
    output = _feature_row(row, actions)
    output["observed_ms"] = _positive(row["observed_ms"], "observed_ms")
    return output


def _cpu_predict(model: Any, row: Mapping[str, Any]) -> tuple[float, str | None]:
    details = getattr(model, "predict_details", None)
    if callable(details):
        result = details(row)
        if not isinstance(result, Mapping) or "prediction" not in result:
            raise ValueError("semantic predict_details must return prediction mapping")
        semantic = None
        features = result.get("semantic_features")
        if isinstance(features, Mapping) and isinstance(features.get("semantic_class"), str):
            semantic = str(features["semantic_class"])
        return _prediction_value(result["prediction"]), semantic
    raw = model.predict(row)
    return _prediction_value(raw), _semantic_class(model, row, raw)


def _protocol_key(protocol: str, row: Mapping[str, Any]) -> str:
    if protocol == "original_run_id":
        return str(row["run_id"])
    if protocol == "instance_id_grouped":
        return str(row["instance_id"])
    if protocol == "repository_transfer":
        return str(row["repository"] or "")
    raise ValueError(f"unknown fold protocol: {protocol}")


def _fold_for(protocol: str, row: Mapping[str, Any]) -> int:
    return fold_of(_protocol_key(protocol, row))


def _fold_support(
    protocol: str,
    run_rows: Mapping[str, Mapping[str, Any]],
    tools_by_run: Mapping[str, Sequence[Mapping[str, Any]]],
    models_by_run: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    run_ids = sorted(run_rows)
    assignment = {run_id: _fold_for(protocol, run_rows[run_id]) for run_id in run_ids}
    if protocol == "instance_id_grouped":
        instances_by_fold: dict[int, set[str]] = defaultdict(set)
        for run_id, fold in assignment.items():
            instances_by_fold[fold].add(str(run_rows[run_id]["instance_id"]))
        shared: set[str] = set()
        for left in range(FOLDS):
            for right in range(left + 1, FOLDS):
                shared |= instances_by_fold[left] & instances_by_fold[right]
        if shared:
            raise AssertionError(f"instance-grouped folds share instances: {sorted(shared)[:3]}")
    fold_info: dict[str, Any] = {}
    for fold in range(FOLDS):
        test_ids = [run_id for run_id in run_ids if assignment[run_id] == fold]
        train_ids = [run_id for run_id in run_ids if assignment[run_id] != fold]
        test_instances = sorted({str(run_rows[run_id]["instance_id"]) for run_id in test_ids})
        train_instances = sorted({str(run_rows[run_id]["instance_id"]) for run_id in train_ids})
        test_repos = sorted({str(run_rows[run_id]["repository"] or "") for run_id in test_ids})
        train_repos = sorted({str(run_rows[run_id]["repository"] or "") for run_id in train_ids})
        fold_info[str(fold)] = {
            "train_runs": len(train_ids),
            "test_runs": len(test_ids),
            "train_tools": sum(len(tools_by_run.get(run_id, ())) for run_id in train_ids),
            "test_tools": sum(len(tools_by_run.get(run_id, ())) for run_id in test_ids),
            "train_models": sum(len(models_by_run.get(run_id, ())) for run_id in train_ids),
            "test_models": sum(len(models_by_run.get(run_id, ())) for run_id in test_ids),
            "train_instances": len(train_instances),
            "test_instances": len(test_instances),
            "train_repositories": len(train_repos),
            "test_repositories": len(test_repos),
            "test_instance_ids": test_instances,
            "test_repository_names": test_repos,
            "shared_instance_count": len(set(train_instances) & set(test_instances)),
        }
    return {
        "protocol": protocol,
        "fold_field": {
            "original_run_id": "run_id",
            "instance_id_grouped": "instance_id",
            "repository_transfer": "repository",
        }[protocol],
        "fold_assignment": assignment,
        "folds": fold_info,
        "no_shared_instance_between_instance_grouped_folds": protocol != "instance_id_grouped"
        or all(item["shared_instance_count"] == 0 for item in fold_info.values()),
    }


def _fit_gpu(train_models: Sequence[Mapping[str, Any]]) -> tuple[float, ...]:
    design = [gpu_design(_model_features(row, "calibration")) for row in train_models]
    targets = [_positive(row["observed_ms"], "model observed_ms") for row in train_models]
    return _fit_fixed_ridge(design, targets, [str(row["request_id"]) for row in train_models])


def _gpu_predict(
    coefficients: Sequence[float],
    row: Mapping[str, Any],
    *,
    raw_nonpositive_counter: list[int] | None = None,
) -> float:
    return _raw_ridge_predict(
        coefficients,
        gpu_design(_model_features(row, "calibration")),
        "predicted_gpu_ms",
        floor_nonpositive=True,
        raw_nonpositive_counter=raw_nonpositive_counter,
    )


def _duration_diagnostics(tools: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Target-only descriptive modes; never passed to any predictor."""

    bins = (
        (0.0, 100.0),
        (100.0, 250.0),
        (250.0, 500.0),
        (500.0, 1000.0),
        (1000.0, 2500.0),
        (2500.0, 5000.0),
        (5000.0, 10000.0),
        (10000.0, 30000.0),
        (30000.0, math.inf),
    )
    by_class: dict[str, list[float]] = defaultdict(list)
    for row in tools:
        by_class[str(row["operation_class"])].append(_positive(row["observed_ms"], "observed_ms"))
    output: dict[str, Any] = {}
    for key, values in sorted(by_class.items()):
        counts = [sum(lower <= value < upper for value in values) for lower, upper in bins]
        mode_index = max(range(len(counts)), key=lambda index: (counts[index], -index))
        output[key] = {
            "n": len(values),
            "median_ms": statistics.median(values),
            "p90_ms": _percentile(values, 0.9),
            "p95_ms": _percentile(values, 0.95),
            "mode_bin_ms": [bins[mode_index][0], bins[mode_index][1]],
            "mode_bin_count": counts[mode_index],
            "bins_are_diagnostic_only": True,
        }
    return output


def _source_conservation(data: Mapping[str, Any]) -> dict[str, Any]:
    tools_by_run: dict[str, float] = defaultdict(float)
    models_by_run: dict[str, float] = defaultdict(float)
    for row in data["tools"]:
        tools_by_run[str(row["run_id"])] += _positive(row["observed_ms"], "tool observed_ms")
    for row in data["models"]:
        models_by_run[str(row["run_id"])] += _positive(row["observed_ms"], "model observed_ms")
    closure = []
    for row in sorted(data["trajectories"], key=lambda item: str(item["run_id"])):
        run_id = str(row["run_id"])
        e2e = _positive(row["observed_ms"], "trajectory observed_ms")
        tool_sum = tools_by_run[run_id]
        model_sum = models_by_run[run_id]
        closure.append(
            {
                "run_id": run_id,
                "instance_id": row.get("instance_id"),
                "repository": row.get("repository") or "",
                "observed_tool_sum_ms": tool_sum,
                "observed_model_sum_ms": model_sum,
                "observed_event_sum_ms": tool_sum + model_sum,
                "observed_e2e_ms": e2e,
                "residual_ms": e2e - tool_sum - model_sum,
                "protocol_tool_ms": _positive(row["tool_wall_ms"], "tool_wall_ms"),
                "protocol_model_ms": _positive(row["model_wall_ms"], "model_wall_ms"),
            }
        )
    pairs = [(row["observed_event_sum_ms"], row["observed_e2e_ms"]) for row in closure]
    event_total = sum(row["observed_event_sum_ms"] for row in closure)
    e2e_total = sum(row["observed_e2e_ms"] for row in closure)
    return {
        "n_trajectories": len(closure),
        "total_observed_tool_ms": sum(row["observed_tool_sum_ms"] for row in closure),
        "total_observed_model_ms": sum(row["observed_model_sum_ms"] for row in closure),
        "total_observed_event_sum_ms": event_total,
        "total_observed_e2e_ms": e2e_total,
        "total_residual_ms": e2e_total - event_total,
        "direct_sum_25pct_gate_infeasible_count": sum(
            1
            for row in closure
            if 1.25 * row["observed_event_sum_ms"] < 0.75 * row["observed_e2e_ms"]
        ),
        "direct_sum_25pct_gate_infeasible_fraction": (
            sum(
                1
                for row in closure
                if 1.25 * row["observed_event_sum_ms"] < 0.75 * row["observed_e2e_ms"]
            )
            / len(closure)
            if closure
            else None
        ),
        "event_sum_vs_e2e": metrics(pairs),
        "closure_rows": closure,
    }


def _input_and_missing_feature_diagnostics(data: Mapping[str, Any]) -> dict[str, Any]:
    models = data["models"]
    equal = sum(int(row.get("input_tokens") == row.get("context_tokens")) for row in models)
    missing_fields = ("file_count", "declared_read_bytes", "declared_write_bytes", "script_body")
    missing = {
        field: sum(field not in row or row.get(field) is None for row in data["tools"])
        for field in missing_fields
    }
    return {
        "n_model_events": len(models),
        "input_equals_context_count": equal,
        "input_equals_context_fraction": equal / len(models) if models else None,
        "gpu_formula_nonidentifiability": "input_tokens and context_tokens are retained in the frozen formula; their separate coefficients are not identifiable when equal",
        "unknown_file_io_and_script_features": missing,
        "unknown_feature_policy": "missing; never substituted with zero work and never used as a duration bin",
    }


def _workload_action_parity(
    data: Mapping[str, Any], actions: Mapping[str, str]
) -> dict[str, Any]:
    """Compare production full-action rows with preserved cached descriptors."""

    fields = (
        "operation_class",
        "tool_name",
        "subcommand",
        "command_prefix",
        "command_sha256",
        "declared_command_bytes",
        "declared_path_count",
        "has_pipe",
        "has_glob",
        "recursive",
        "n_segments",
        "n_pipes",
        "is_python",
        "is_find",
        "is_editor",
        "launch_family",
    )
    mismatches = Counter()
    examples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    compared = 0
    for row in data["tools"]:
        event_id = str(row["event_id"])
        action = actions[event_id]
        features = WorkloadToolInput.from_action(
            action,
            event_id=event_id,
            run_id=str(row["run_id"]),
            split="calibration",
            hardware=HARDWARE,
            repository=str(row.get("repository") or ""),
            instance_id=str(row.get("instance_id") or ""),
        )
        served = workload_cpu_row(features)
        compared += 1
        for field in fields:
            if row.get(field) != served.get(field):
                mismatches[field] += 1
                if len(examples[field]) < 5:
                    examples[field].append(
                        {
                            "event_id": event_id,
                            "cached": row.get(field),
                            "served": served.get(field),
                        }
                    )
    return {
        "compared_events": compared,
        "mismatch_counts": dict(mismatches),
        "mismatch_examples": dict(examples),
        "source": "WorkloadToolInput.from_action -> workload_cpu_row",
        "features_compared": list(fields),
        "target_or_duration_fields_compared": False,
    }


def _audit(data: Mapping[str, Any], actions: Mapping[str, str]) -> dict[str, Any]:
    """Reproduce the small cache-vs-served audit without raw-source walking."""

    skew = Counter()
    examples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in data["tools"]:
        served = row_from_tool_input(tool_input_row(row))
        for key in ("recursive", "n_segments", "n_pipes", "launch_family"):
            if row.get(key) != served.get(key):
                skew[key] += 1
                if len(examples[key]) < 5:
                    examples[key].append(
                        {
                            "event_id": row["event_id"],
                            "action_sha256": hashlib.sha256(actions[row["event_id"]].strip().encode()).hexdigest(),
                            "cached": row.get(key),
                            "served": served.get(key),
                        }
                    )
    conservation = _source_conservation(data)
    return {
        "train_serve_skew": dict(skew),
        "skew_examples": dict(examples),
        "event_sum_vs_e2e": conservation["event_sum_vs_e2e"],
        "negative_residual_runs": sum(row["residual_ms"] < -1 for row in conservation["closure_rows"]),
        "instances": len({row["instance_id"] for row in data["trajectories"]}),
        "runs": len(data["trajectories"]),
        "cohort_exclusions": data["_exclusions"],
        "source": "preserved cache + preserved calibration_actions.json only",
    }


def audit(data: Mapping[str, Any], actions: Mapping[str, str]) -> dict[str, Any]:
    """Write a new adjusted audit while preserving the prior initial audit."""

    result = _audit(data, actions)
    write("review_audit.json", result)
    write(
        "preserved_artifact_manifest.json",
        {
            "initial_audit": {
                "path": str(PRESERVED_AUDIT),
                "exists": PRESERVED_AUDIT.is_file(),
                "sha256": _sha256(PRESERVED_AUDIT.read_bytes()) if PRESERVED_AUDIT.is_file() else None,
            },
            "calibration_actions": {
                "path": str(ACTION_CACHE),
                "exists": ACTION_CACHE.is_file(),
                "sha256": _sha256(ACTION_CACHE.read_bytes()) if ACTION_CACHE.is_file() else None,
            },
            "provenance": {
                "path": str(PRESERVED_PROVENANCE),
                "exists": PRESERVED_PROVENANCE.is_file(),
                "sha256": _sha256(PRESERVED_PROVENANCE.read_bytes()) if PRESERVED_PROVENANCE.is_file() else None,
            },
        },
    )
    return result


def _metric_from_event_records(
    records: Sequence[Mapping[str, Any]], pred_key: str = "predicted_ms"
) -> dict[str, Any]:
    return metrics([(row[pred_key], row["observed_ms"]) for row in records])


def _event_class_metrics(records: Sequence[Mapping[str, Any]], field: str) -> dict[str, Any]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in records:
        value = row.get(field)
        groups[str(value) if value is not None else "unknown"].append(row)
    return {key: _metric_from_event_records(group) for key, group in sorted(groups.items())}


def _trajectory_gate_rates(
    tool_records: Sequence[Mapping[str, Any]],
    model_records: Sequence[Mapping[str, Any]],
    trajectories: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    tools_by_run: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    models_by_run: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in tool_records:
        tools_by_run[str(row["run_id"])].append(row)
    for row in model_records:
        models_by_run[str(row["run_id"])].append(row)
    all_tools: list[bool] = []
    all_models: list[bool] = []
    every_both: list[bool] = []
    for traj in trajectories:
        run_id = str(traj["run_id"])
        tool_pass = all(float(row["ape"]) <= GATE_PERCENT for row in tools_by_run[run_id])
        model_pass = all(float(row["ape"]) <= GATE_PERCENT for row in models_by_run[run_id])
        all_tools.append(tool_pass and bool(tools_by_run[run_id]))
        all_models.append(model_pass and bool(models_by_run[run_id]))
        every_both.append(tool_pass and model_pass and bool(tools_by_run[run_id]) and bool(models_by_run[run_id]))
    denom = max(len(trajectories), 1)
    return {
        "all_tools_pass_rate": sum(all_tools) / denom,
        "all_model_events_pass_rate": sum(all_models) / denom,
        "every_tool_and_gpu_event_pass_rate": sum(every_both) / denom,
        "n_trajectories": len(trajectories),
    }


def _combined_event_e2e_gate_rates(
    tool_records: Sequence[Mapping[str, Any]],
    model_records: Sequence[Mapping[str, Any]],
    trajectories: Sequence[Mapping[str, Any]],
    *,
    e2e_key: str,
    eligible_runs: set[str] | None = None,
) -> dict[str, Any]:
    """Conjoin event and E2E gates per trajectory (never multiply rates)."""

    tools_by_run: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    models_by_run: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in tool_records:
        tools_by_run[str(row["run_id"])].append(row)
    for row in model_records:
        models_by_run[str(row["run_id"])].append(row)
    combined: list[bool] = []
    certified: list[bool] = []
    for traj in trajectories:
        run_id = str(traj["run_id"])
        event_pass = (
            bool(tools_by_run[run_id])
            and bool(models_by_run[run_id])
            and all(float(row["ape"]) <= GATE_PERCENT for row in tools_by_run[run_id])
            and all(float(row["ape"]) <= GATE_PERCENT for row in models_by_run[run_id])
        )
        e2e_pass = _ape(
            _positive(traj[e2e_key], e2e_key),
            _positive(traj["observed_e2e_ms"], "observed_e2e_ms"),
        ) <= GATE_PERCENT
        value = event_pass and e2e_pass
        combined.append(value)
        certified.append(value and (eligible_runs is None or run_id in eligible_runs))
    denominator = max(len(trajectories), 1)
    return {
        "allpass_and_e2e_gate_rate": sum(combined) / denominator,
        "every_tool_and_gpu_event_and_e2e_gate_pass_rate": sum(combined) / denominator,
        "coverage_aware_certified_allpass_rate": sum(certified) / denominator,
        "coverage_eligible_trajectory_count": (
            sum(str(row["run_id"]) in eligible_runs for row in trajectories)
            if eligible_runs is not None
            else len(trajectories)
        ),
        "n_trajectories": len(trajectories),
    }


def _make_event_record(
    row: Mapping[str, Any],
    fold: int,
    predicted: float,
    *,
    semantic_class: str | None = None,
    channel: str,
) -> dict[str, Any]:
    observed = _positive(row["observed_ms"], "observed_ms")
    prediction = _prediction(predicted, f"{channel} prediction")
    ape = _ape(prediction, observed)
    return {
        "channel": channel,
        "event_id": row.get("event_id") or row.get("request_id"),
        "request_id": row.get("request_id"),
        "run_id": row["run_id"],
        "instance_id": row.get("instance_id"),
        "repository": row.get("repository") or "",
        "fold": fold,
        "original_class": row.get("operation_class"),
        "semantic_class": semantic_class,
        "predicted_ms": prediction,
        "observed_ms": observed,
        "ape": ape,
        "within25": ape <= GATE_PERCENT,
    }


def _bootstrap_delta(
    baseline_records: Sequence[Mapping[str, Any]],
    candidate_records: Sequence[Mapping[str, Any]],
    *,
    seed: int = BOOTSTRAP_SEED,
    reps: int = BOOTSTRAP_REPS,
) -> dict[str, Any]:
    baseline_by_id = {str(row["event_id"]): row for row in baseline_records}
    candidate_by_id = {str(row["event_id"]): row for row in candidate_records}
    shared_ids = sorted(set(baseline_by_id) & set(candidate_by_id))
    by_instance: dict[str, list[tuple[bool, bool]]] = defaultdict(list)
    for event_id in shared_ids:
        base = baseline_by_id[event_id]
        candidate = candidate_by_id[event_id]
        by_instance[str(base.get("instance_id"))].append(
            (bool(base["within25"]), bool(candidate["within25"]))
        )
    clusters = sorted(by_instance)
    if not clusters:
        return {
            "n_clusters": 0,
            "n_events": 0,
            "reps": reps,
            "seed": seed,
            "point_delta_candidate_minus_baseline": None,
            "interval_95": [None, None],
        }

    def rate_delta(selected: Sequence[str]) -> float:
        base_pass = sum(int(base) for key in selected for base, _candidate in by_instance[key])
        candidate_pass = sum(int(candidate) for key in selected for _base, candidate in by_instance[key])
        count = sum(len(by_instance[key]) for key in selected)
        return (candidate_pass - base_pass) / count

    point = rate_delta(clusters)
    rng = random.Random(seed)
    samples = [
        rate_delta([clusters[rng.randrange(len(clusters))] for _ in clusters])
        for _ in range(reps)
    ]
    samples.sort()
    return {
        "n_clusters": len(clusters),
        "n_events": len(shared_ids),
        "reps": reps,
        "seed": seed,
        "cluster_key": "instance_id",
        "resampling": "paired event booleans with replacement by instance cluster",
        "point_delta_candidate_minus_baseline": point,
        "interval_95": [_percentile(samples, 0.025), _percentile(samples, 0.975)],
    }


def _retrospective_fixed_key_ceiling(tools: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    groups: dict[tuple[str, str], list[float]] = defaultdict(list)
    for row in tools:
        if str(row.get("operation_class")) not in HEAVY_CLASSES:
            continue
        groups[(str(row.get("repository") or ""), str(row.get("command_sha256") or ""))].append(
            _positive(row["observed_ms"], "observed_ms")
        )
    eligible = {key: values for key, values in groups.items() if len(values) >= 5}
    details = []
    covered_total = 0
    event_total = 0
    for (repository, command_sha256), values in eligible.items():
        ordered = sorted(values)
        left = 0
        best = 0
        for right, value in enumerate(ordered):
            while value / ordered[left] > (5.0 / 3.0):
                left += 1
            best = max(best, right - left + 1)
        n = len(values)
        covered_total += best
        event_total += n
        details.append(
            {
                "repository": repository,
                "command_sha256": command_sha256,
                "n": n,
                "max_fixed_prediction_within25": best,
                "max_coverage_rate": best / n,
                "retrospective_only": True,
            }
        )
    details.sort(key=lambda row: (row["max_coverage_rate"], -row["n"], row["repository"], row["command_sha256"]))
    return {
        "group_key": ["repository", "command_sha256"],
        "heavy_classes": sorted(HEAVY_CLASSES),
        "minimum_group_n": 5,
        "eligible_group_count": len(eligible),
        "eligible_event_count": event_total,
        "max_covered_event_count": covered_total,
        "aggregate_max_coverage_rate": covered_total / event_total if event_total else None,
        "window_condition": "max_observed_ms/min_observed_ms <= 5/3 (overlapping 25% intervals)",
        "interpretation": "retrospective fixed-key ceiling; not a general impossibility bound and never a predictor",
        "top10_irreducible_groups": details[:10],
    }


def _per_repo(
    trajectories: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]],
    models: Sequence[Mapping[str, Any]],
    support: Mapping[str, Any],
    *,
    protocol: str,
    all_trajectories: Sequence[Mapping[str, Any]],
    all_tools: Sequence[Mapping[str, Any]],
    all_models: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    by_repo_traj: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    by_repo_tool: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    by_repo_model: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in trajectories:
        by_repo_traj[str(row.get("repository") or "")].append(row)
    for row in tools:
        by_repo_tool[str(row.get("repository") or "")].append(row)
    for row in models:
        by_repo_model[str(row.get("repository") or "")].append(row)
    all_traj_by_run = {str(row["run_id"]): row for row in all_trajectories}
    all_tools_by_run: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    all_models_by_run: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in all_tools:
        all_tools_by_run[str(row["run_id"])].append(row)
    for row in all_models:
        all_models_by_run[str(row["run_id"])].append(row)
    out = {}
    for repo in sorted(set(by_repo_traj) | set(by_repo_tool) | set(by_repo_model)):
        train_counts_by_fold = {}
        for fold in range(FOLDS):
            train_ids = [
                run_id
                for run_id, row in all_traj_by_run.items()
                if _fold_for(protocol, row) != fold and str(row.get("repository") or "") == repo
            ]
            train_counts_by_fold[str(fold)] = {
                "trajectories": len(train_ids),
                "tools": sum(len(all_tools_by_run[run_id]) for run_id in train_ids),
                "models": sum(len(all_models_by_run[run_id]) for run_id in train_ids),
            }
        repo_trajectories = by_repo_traj[repo]
        repo_tools = by_repo_tool[repo]
        repo_models = by_repo_model[repo]
        out[repo] = {
            "test_counts": {
                "trajectories": len(by_repo_traj[repo]),
                "tools": len(by_repo_tool[repo]),
                "models": len(by_repo_model[repo]),
            },
            "train_counts_by_fold": train_counts_by_fold,
            "test_metrics": {
                "cpu_tool_events": _metric_from_event_records(repo_tools),
                "gpu_model_events": _metric_from_event_records(repo_models),
                "direct_e2e": metrics(
                    [(row["direct_predicted_e2e_ms"], row["observed_e2e_ms"]) for row in repo_trajectories]
                ),
                "overhead_e2e": metrics(
                    [(row["overhead_predicted_e2e_ms"], row["observed_e2e_ms"]) for row in repo_trajectories]
                ),
                "legacy_e2e": metrics(
                    [(row["legacy_predicted_e2e_ms"], row["observed_e2e_ms"]) for row in repo_trajectories]
                ),
            },
        }
    return out


def _coverage_audit(data: Mapping[str, Any]) -> dict[str, Any]:
    """Audit omitted proxy requests from the allowlisted prior-source paths.

    This is a coverage diagnostic only. Proxy labels, request IDs, and failure
    status are never features or targets. The metadata-only quarantine happens
    first, so no source belonging to the excluded instance is opened.
    """

    retained_runs = {str(row["run_id"]): row for row in data["trajectories"]}
    cached_request_ids: dict[str, set[str]] = defaultdict(set)
    for row in data["models"]:
        cached_request_ids[str(row["run_id"])].add(str(row["request_id"]))
    source_rows: dict[str, Mapping[str, Any]] = {}
    if PRESERVED_PROVENANCE.is_file():
        provenance = json.loads(PRESERVED_PROVENANCE.read_text(encoding="utf-8"))
        for item in provenance.get("raw_sources", []) if isinstance(provenance, Mapping) else []:
            run_id = str(item.get("run_id") or "")
            if run_id in retained_runs and run_id not in source_rows:
                source_rows[run_id] = item

    failed_by_run: dict[str, list[str]] = defaultdict(list)
    missing_by_run: dict[str, list[str]] = defaultdict(list)
    source_missing: list[str] = []
    request_count = 0
    failed_count = 0
    missing_count = 0
    source_records = []
    for run_id in sorted(retained_runs):
        item = source_rows.get(run_id)
        if item is None:
            source_missing.append(run_id)
            source_records.append({"run_id": run_id, "source_present": False})
            continue
        traj_path = Path(str(item.get("path") or ""))
        proxy_path = traj_path.parents[1] / "request_proxy.jsonl"
        if not proxy_path.is_file():
            source_missing.append(run_id)
            source_records.append(
                {"run_id": run_id, "source_present": False, "proxy_path": str(proxy_path)}
            )
            continue
        run_requests = 0
        run_failed = 0
        run_missing = 0
        # The source path is explicitly allowlisted by preserved provenance;
        # do not glob or discover any additional files.
        with proxy_path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, Mapping) or record.get("path") != "/v1/chat/completions":
                    continue
                request_id = str(record.get("request_id") or f"line-{line_number}")
                run_requests += 1
                request_count += 1
                status = record.get("status_code")
                try:
                    status_int = int(status)
                except (TypeError, ValueError):
                    status_int = 0
                if not 200 <= status_int < 300:
                    run_failed += 1
                    failed_count += 1
                    failed_by_run[run_id].append(request_id)
                elif request_id not in cached_request_ids[run_id]:
                    run_missing += 1
                    missing_count += 1
                    missing_by_run[run_id].append(request_id)
        source_records.append(
            {
                "run_id": run_id,
                "proxy_path": str(proxy_path),
                "source_present": True,
                "chat_completion_requests": run_requests,
                "failed_requests": run_failed,
                "successful_requests_missing_cached_labels": run_missing,
            }
        )
    incomplete_runs = sorted(
        run_id
        for run_id in retained_runs
        if run_id in source_missing or failed_by_run.get(run_id) or missing_by_run.get(run_id)
    )
    conservation = _source_conservation(data)
    source_incoherent_run_ids = sorted(
        str(row["run_id"])
        for row in conservation["closure_rows"]
        if abs(float(row["observed_tool_sum_ms"]) - float(row["protocol_tool_ms"])) > 0.001
    )
    incomplete_set = set(incomplete_runs) | set(source_incoherent_run_ids)
    incomplete_runs = sorted(incomplete_set)
    eligible_runs = sorted(set(retained_runs) - incomplete_set)
    return {
        "source": "first preserved provenance raw_source per retained run -> request_proxy.jsonl",
        "holdout_or_quarantined_sources_opened": False,
        "n_retained_runs": len(retained_runs),
        "n_runs_with_allowlisted_source": len(source_rows),
        "n_chat_completion_requests": request_count,
        "n_failed_non2xx_requests": failed_count,
        "n_successful_requests_missing_cached_model_labels": missing_count,
        "failed_request_ids_by_run": {key: sorted(value) for key, value in sorted(failed_by_run.items())},
        "missing_request_ids_by_run": {key: sorted(value) for key, value in sorted(missing_by_run.items())},
        "source_missing_run_ids": source_missing,
        "source_incoherent_run_ids": source_incoherent_run_ids,
        "source_incoherent_threshold_ms": 0.001,
        "source_incoherent_count": len(source_incoherent_run_ids),
        "incomplete_run_ids": incomplete_runs,
        "eligible_run_ids": eligible_runs,
        "coverage_aware_denominator_runs": len(retained_runs),
        "conditional_recorded_event_gate_caveat": "runs with omitted failed or unlabeled requests cannot certify all-required-event pass",
        "source_records": source_records,
    }


def _execution_manifest(data: Mapping[str, Any], include_repo_transfer: bool) -> dict[str, Any]:
    source_files = {
        name: ROOT / relative
        for name, relative in {
            "semantic_cpu_model.py": "src/agentic_sim/assignment/semantic_cpu_model.py",
            "workload_simulator.py": "src/agentic_sim/assignment/workload_simulator.py",
            "d9_cpu_review.py": "scripts/assignment/d9_cpu_review.py",
            "cpu_event_model.py": "src/agentic_sim/assignment/cpu_event_model.py",
            "tool_features.py": "src/agentic_sim/assignment/tool_features.py",
        }.items()
    }
    hashes = {
        name: _sha256(path.read_bytes()) if path.is_file() else None
        for name, path in source_files.items()
    }
    command = " ".join(sys.argv)
    return {
        "schema_version": "assignment.d9-cpu-review-execution-manifest.v1",
        "retained_counts": data["_exclusions"]["retained_counts"],
        "excluded_counts": data["_exclusions"]["excluded_counts"],
        "excluded_run_ids": data["_exclusions"]["excluded_run_ids"],
        "fold_definition": {
            "folds": FOLDS,
            "hash": "sha256(value) integer modulo 5",
            "protocols": {
                "original_run_id": "fold_of(run_id)",
                "instance_id_grouped": "fold_of(instance_id)",
                "repository_transfer": "fold_of(repository)",
            },
        },
        "candidates": {
            "baseline": {"model": "HierarchicalMedianModel", "min_count": 8},
            "semantic_no_repo_median": {
                "model": "SemanticCpuModel",
                "center": "median",
                "use_repository": False,
                "min_count": 8,
                "min_instances": 3,
            },
            "semantic_repo_median": {
                "model": "SemanticCpuModel",
                "center": "median",
                "use_repository": True,
                "min_count": 8,
                "min_instances": 3,
            },
            "semantic_repo_gate": {
                "model": "SemanticCpuModel",
                "center": "gate",
                "use_repository": True,
                "min_count": 8,
                "min_instances": 3,
            },
        },
        "gpu_formula": "gpu_design(model_input(row))",
        "gpu_ridge_alpha": RIDGE_ALPHA,
        "bootstrap": {"reps": BOOTSTRAP_REPS, "seed": BOOTSTRAP_SEED, "cluster": "instance_id"},
        "source_sha256": hashes,
        "cache_sha256": data.get("_cache_sha256"),
        "actions_sha256": _sha256(ACTION_CACHE.read_bytes()) if ACTION_CACHE.is_file() else None,
        "provenance_sha256": _sha256(PRESERVED_PROVENANCE.read_bytes()) if PRESERVED_PROVENANCE.is_file() else None,
        "command": command,
        "interpreter": sys.executable,
        "include_repo_transfer": include_repo_transfer,
        "winner_selection": "not performed",
    }


def _run_protocol(
    protocol: str,
    data: Mapping[str, Any],
    actions: Mapping[str, str],
    candidate_names: Sequence[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    trajectories = [dict(row) for row in data["trajectories"]]
    traj_by_run = {str(row["run_id"]): row for row in trajectories}
    tools = [
        dict(row, instance_id=row.get("instance_id") or traj_by_run[str(row["run_id"])]["instance_id"])
        for row in data["tools"]
    ]
    models = [dict(row) for row in data["models"]]
    for row in models:
        traj = traj_by_run[str(row["run_id"])]
        row["instance_id"] = traj["instance_id"]
        row["repository"] = traj.get("repository") or ""
    tools_by_run: dict[str, list[dict[str, Any]]] = defaultdict(list)
    models_by_run: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in tools:
        row["repository"] = row.get("repository") or traj_by_run[str(row["run_id"])].get("repository") or ""
        tools_by_run[str(row["run_id"])].append(row)
    for row in models:
        models_by_run[str(row["run_id"])].append(row)
    support = _fold_support(protocol, traj_by_run, tools_by_run, models_by_run)
    write(f"fold_support_{protocol}.json", support)
    results: dict[str, Any] = {
        "schema_version": "assignment.d9-cpu-review-metrics.v1",
        "protocol": protocol,
        "cohort": data["_exclusions"],
        "gpu_formula": "gpu_design(model_input(row))",
        "gpu_ridge_alpha": RIDGE_ALPHA,
        "candidates": {},
    }
    predictions_for_bootstrap: dict[str, dict[str, list[dict[str, Any]]]] = {}
    run_ids = sorted(traj_by_run)
    coverage = data.get("_coverage")
    overhead_allowed_runs = (
        set(coverage.get("eligible_run_ids", []))
        if isinstance(coverage, Mapping)
        else set(run_ids)
    )

    for candidate in candidate_names:
        print(f"{protocol}: candidate {candidate}", flush=True)
        tool_records: list[dict[str, Any]] = []
        model_records: list[dict[str, Any]] = []
        trajectory_records: list[dict[str, Any]] = []
        raw_nonpositive_counts = {"gpu": 0, "legacy": 0}
        overhead_fit_records: list[dict[str, Any]] = []
        for fold in range(FOLDS):
            print(f"{protocol}: {candidate}: fold {fold}", flush=True)
            train_ids = [run_id for run_id in run_ids if _fold_for(protocol, traj_by_run[run_id]) != fold]
            test_ids = [run_id for run_id in run_ids if _fold_for(protocol, traj_by_run[run_id]) == fold]
            train_tools = [row for run_id in train_ids for row in tools_by_run[run_id]]
            test_tools = [row for run_id in test_ids for row in tools_by_run[run_id]]
            train_models = [row for run_id in train_ids for row in models_by_run[run_id]]
            test_models = [row for run_id in test_ids for row in models_by_run[run_id]]
            gpu_coefficients = _fit_gpu(train_models)
            gpu_train_by_run: dict[str, float] = defaultdict(float)
            gpu_test_by_run: dict[str, float] = defaultdict(float)
            gpu_test_records: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for row in train_models:
                gpu_counter = [raw_nonpositive_counts["gpu"]]
                gpu_train_by_run[str(row["run_id"])] += _gpu_predict(
                    gpu_coefficients, row, raw_nonpositive_counter=gpu_counter
                )
                raw_nonpositive_counts["gpu"] = gpu_counter[0]
            for row in test_models:
                gpu_counter = [raw_nonpositive_counts["gpu"]]
                predicted = _gpu_predict(
                    gpu_coefficients, row, raw_nonpositive_counter=gpu_counter
                )
                raw_nonpositive_counts["gpu"] = gpu_counter[0]
                gpu_test_by_run[str(row["run_id"])] += predicted
                gpu_test_records[str(row["run_id"])].append(
                    _make_event_record(row, fold, predicted, channel="gpu")
                )
            cpu_model = _fit_cpu_model(candidate, [_fit_row(row, actions) for row in train_tools])
            cpu_train_by_run: dict[str, float] = defaultdict(float)
            cpu_test_by_run: dict[str, float] = defaultdict(float)
            for row in train_tools:
                predicted, _semantic = _cpu_predict(cpu_model, _feature_row(row, actions))
                cpu_train_by_run[str(row["run_id"])] += predicted
            candidate_test_tool_records: list[dict[str, Any]] = []
            for row in test_tools:
                predicted, semantic = _cpu_predict(cpu_model, _feature_row(row, actions))
                cpu_test_by_run[str(row["run_id"])] += predicted
                candidate_test_tool_records.append(
                    _make_event_record(
                        row, fold, predicted, semantic_class=semantic, channel="cpu"
                    )
                )
            tool_records.extend(candidate_test_tool_records)
            for run_id in test_ids:
                model_records.extend(gpu_test_records[run_id])

            ordered_train = sorted(train_ids)
            legacy_design = [
                e2e_design(
                    cpu_train_by_run[run_id],
                    gpu_train_by_run[run_id],
                    float(len(tools_by_run[run_id])),
                    float(len(models_by_run[run_id])),
                )
                for run_id in ordered_train
            ]
            legacy_targets = [
                _positive(traj_by_run[run_id]["observed_ms"], "e2e observed_ms")
                for run_id in ordered_train
            ]
            legacy_coefficients = _fit_fixed_ridge(legacy_design, legacy_targets, ordered_train)
            overhead_fit_ids = [run_id for run_id in ordered_train if run_id in overhead_allowed_runs]
            if not overhead_fit_ids:
                raise ValueError(f"no coverage-consistent overhead training runs in fold {fold}")
            overhead_tools: list[float] = []
            overhead_models: list[float] = []
            overhead_targets: list[float] = []
            for run_id in overhead_fit_ids:
                observed_tool_sum = sum(
                    _positive(row["observed_ms"], "tool observed_ms") for row in tools_by_run[run_id]
                )
                observed_model_sum = sum(
                    _positive(row["observed_ms"], "model observed_ms") for row in models_by_run[run_id]
                )
                overhead_tools.append(float(len(tools_by_run[run_id])))
                overhead_models.append(float(len(models_by_run[run_id])))
                overhead_targets.append(
                    _positive(traj_by_run[run_id]["observed_ms"], "e2e observed_ms")
                    - observed_tool_sum
                    - observed_model_sum
                )
            overhead_fit = fit_nnls_overhead(overhead_tools, overhead_models, overhead_targets)
            overhead_fit["fit_run_count"] = len(overhead_fit_ids)
            overhead_fit["excluded_incomplete_run_count"] = len(ordered_train) - len(overhead_fit_ids)
            overhead_fit_records.append({"fold": fold, **overhead_fit})
            for run_id in test_ids:
                test_tool_sum = cpu_test_by_run[run_id]
                test_model_sum = gpu_test_by_run[run_id]
                direct = _prediction(test_tool_sum + test_model_sum, "direct e2e prediction")
                legacy_counter = [raw_nonpositive_counts["legacy"]]
                legacy = _raw_ridge_predict(
                    legacy_coefficients,
                    e2e_design(
                        test_tool_sum,
                        test_model_sum,
                        float(len(tools_by_run[run_id])),
                        float(len(models_by_run[run_id])),
                    ),
                    "legacy e2e prediction",
                    floor_nonpositive=True,
                    raw_nonpositive_counter=legacy_counter,
                )
                raw_nonpositive_counts["legacy"] = legacy_counter[0]
                overhead_component = _overhead_predict(
                    overhead_fit, len(tools_by_run[run_id]), len(models_by_run[run_id])
                )
                overhead_e2e = _prediction(
                    direct + overhead_component, "overhead e2e prediction"
                )
                observed_tool = sum(
                    _positive(row["observed_ms"], "tool observed_ms") for row in tools_by_run[run_id]
                )
                observed_model = sum(
                    _positive(row["observed_ms"], "model observed_ms") for row in models_by_run[run_id]
                )
                trajectory_records.append(
                    {
                        "run_id": run_id,
                        "instance_id": traj_by_run[run_id]["instance_id"],
                        "repository": traj_by_run[run_id].get("repository") or "",
                        "fold": fold,
                        "n_tools": len(tools_by_run[run_id]),
                        "n_models": len(models_by_run[run_id]),
                        "predicted_tool_sum_ms": _prediction(test_tool_sum, "predicted tool sum"),
                        "predicted_gpu_sum_ms": _prediction(test_model_sum, "predicted gpu sum"),
                        "observed_tool_sum_ms": observed_tool,
                        "observed_gpu_sum_ms": observed_model,
                        "observed_e2e_ms": _positive(traj_by_run[run_id]["observed_ms"], "e2e observed_ms"),
                        "direct_predicted_e2e_ms": direct,
                        "legacy_predicted_e2e_ms": legacy,
                        "overhead_predicted_e2e_ms": overhead_e2e,
                        "predicted_overhead_ms": overhead_component,
                        "overhead_coefficients": overhead_fit["coefficients"],
                        "legacy_coefficients": list(legacy_coefficients),
                    }
                )

        all_event_records = tool_records + model_records
        for record in model_records:
            record["original_class"] = None
            record["semantic_class"] = None
        channel_metrics = {
            "cpu_tool_events": _metric_from_event_records(tool_records),
            "gpu_model_events": _metric_from_event_records(model_records),
            "all_tool_and_gpu_events": metrics(
                [(row["predicted_ms"], row["observed_ms"]) for row in all_event_records]
            ),
            "original_class": _event_class_metrics(tool_records, "original_class"),
            "semantic_class": _event_class_metrics(tool_records, "semantic_class")
            if any(row.get("semantic_class") is not None for row in tool_records)
            else {},
            "class_label_denominators": {
                "original_class_events": len(tool_records),
                "semantic_class_events": sum(
                    row.get("semantic_class") is not None for row in tool_records
                ),
                "semantic_denominator_shift_disclosed": True,
            },
        }
        trajectory_pairs = {
            "direct": [(row["direct_predicted_e2e_ms"], row["observed_e2e_ms"]) for row in trajectory_records],
            "legacy": [(row["legacy_predicted_e2e_ms"], row["observed_e2e_ms"]) for row in trajectory_records],
            "overhead": [(row["overhead_predicted_e2e_ms"], row["observed_e2e_ms"]) for row in trajectory_records],
            "tool_sum": [(row["predicted_tool_sum_ms"], row["observed_tool_sum_ms"]) for row in trajectory_records],
        }
        gates = _trajectory_gate_rates(tool_records, model_records, trajectory_records)
        e2e_metrics = {name: metrics(pairs) for name, pairs in trajectory_pairs.items()}
        coverage = data.get("_coverage")
        eligible_runs = (
            set(coverage.get("eligible_run_ids", []))
            if isinstance(coverage, Mapping)
            else None
        )
        channel_gate = {
            name: {
                "metrics": e2e_metrics[name],
                "e2e_gate_pass_rate": e2e_metrics[name]["within_25_rate"],
                **_combined_event_e2e_gate_rates(
                    tool_records,
                    model_records,
                    trajectory_records,
                    e2e_key={
                        "direct": "direct_predicted_e2e_ms",
                        "overhead": "overhead_predicted_e2e_ms",
                        "legacy": "legacy_predicted_e2e_ms",
                    }[name],
                    eligible_runs=eligible_runs,
                ),
            }
            for name in ("direct", "overhead", "legacy")
        }
        candidate_result = {
            "candidate": candidate,
            "protocol": protocol,
            "cpu_model": {
                "kind": "hierarchical_median" if candidate == "baseline" else "SemanticCpuModel",
                "parameters": {
                    "center": "median" if candidate != "semantic_repo_gate" else "gate",
                    "use_repository": candidate in {"semantic_repo_median", "semantic_repo_gate"},
                    "min_count": 8,
                    "min_instances": 3,
                },
            },
            "ridge_serving_diagnostics": {
                "raw_nonpositive_prediction_counts": raw_nonpositive_counts,
                "nonpositive_policy": "frozen ridge serving floor 1e-6; raw counts retained, rows not dropped",
            },
            "overhead_fit_diagnostics": overhead_fit_records,
            "event_metrics": channel_metrics,
            "trajectory_metrics": {
                "tool_sum_per_trajectory": e2e_metrics["tool_sum"],
                "direct": channel_gate["direct"],
                "overhead": channel_gate["overhead"],
                "legacy": channel_gate["legacy"],
                "all_tools_pass_rate": gates["all_tools_pass_rate"],
                "all_model_events_pass_rate": gates["all_model_events_pass_rate"],
                "every_tool_and_gpu_event_pass_rate": gates["every_tool_and_gpu_event_pass_rate"],
            },
            "fold_support": support,
            "per_repository": _per_repo(
                trajectory_records,
                tool_records,
                model_records,
                support,
                protocol=protocol,
                all_trajectories=trajectories,
                all_tools=tools,
                all_models=models,
            ),
        }
        results["candidates"][candidate] = candidate_result
        predictions_for_bootstrap[candidate] = {"tools": tool_records, "models": model_records}
        write(
            f"predictions_{protocol}_{candidate}.json",
            {
                "schema_version": "assignment.d9-cpu-review-oof.v1",
                "candidate": candidate,
                "protocol": protocol,
                "cohort": data["_exclusions"],
                "tool_events": tool_records,
                "gpu_events": model_records,
                "trajectories": trajectory_records,
            },
        )

    baseline = predictions_for_bootstrap.get("baseline", {})
    bootstrap = {}
    for candidate in candidate_names:
        if candidate != "baseline":
            bootstrap[candidate] = _bootstrap_delta(
                baseline.get("tools", []), predictions_for_bootstrap[candidate].get("tools", [])
            )
    results["paired_cluster_bootstrap_tool_within25_delta_vs_baseline"] = bootstrap
    results["notes"] = {
        "historical_baseline_reference": HISTORICAL_BASELINE_REFERENCE,
        "target_bins": "none; duration summaries are diagnostics only",
        "holdout": "excluded and never opened/scored",
    }
    write(f"metrics_{protocol}.json", results)
    write(
        f"bootstrap_{protocol}.json",
        {
            "schema_version": "assignment.d9-cpu-review-bootstrap.v1",
            "protocol": protocol,
            "paired_cluster_bootstrap_tool_within25_delta_vs_baseline": bootstrap,
        },
    )
    return results, predictions_for_bootstrap


def run_comparisons(
    data: Mapping[str, Any], actions: Mapping[str, str], *, include_repo_transfer: bool = True
) -> dict[str, Any]:
    coverage = _coverage_audit(data)
    # ``data`` originates from load() and is a mutable dict in production; the
    # fallback keeps this helper usable with Mapping test doubles.
    if isinstance(data, dict):
        data["_coverage"] = coverage
    diagnostics = {
        "schema_version": "assignment.d9-cpu-review-diagnostics.v1",
        "cache_sha256": data.get("_cache_sha256"),
        "cohort": data["_exclusions"],
        "coverage_audit": coverage,
        "source_conservation": _source_conservation(data),
        "input_and_missing_feature_diagnostics": _input_and_missing_feature_diagnostics(data),
        "workload_action_parity": _workload_action_parity(data, actions),
        "duration_diagnostics": _duration_diagnostics(data["tools"]),
        "retrospective_fixed_key_ceiling": _retrospective_fixed_key_ceiling(data["tools"]),
        "candidate_names": list(CANDIDATES),
        "protocols": ["original_run_id", "instance_id_grouped"]
        + (["repository_transfer"] if include_repo_transfer else []),
        "no_gpu_execution": True,
    }
    write("cohort_diagnostics.json", diagnostics)
    all_results = {}
    for protocol in diagnostics["protocols"]:
        names = CANDIDATES
        if protocol == "repository_transfer":
            names = ("baseline", "semantic_repo_median", "semantic_repo_gate")
        print(f"starting {protocol} on retained cohort", flush=True)
        result, _predictions = _run_protocol(protocol, data, actions, names)
        all_results[protocol] = result
    summary = {
        "schema_version": "assignment.d9-cpu-review-summary.v1",
        "cache_sha256": data.get("_cache_sha256"),
        "cohort": data["_exclusions"],
        "coverage_audit": coverage,
        "protocols": all_results,
        "candidate_names": list(CANDIDATES),
        "repo_transfer_candidates": ["baseline", "semantic_repo_median", "semantic_repo_gate"],
        "gpu_formula": "gpu_design(model_input(row))",
        "gpu_ridge_alpha": RIDGE_ALPHA,
        "no_gpu_execution": True,
        "winner_selection": "not performed",
    }
    write("comparison_summary.json", summary)
    write("execution_manifest.json", _execution_manifest(data, include_repo_transfer))
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-repo-transfer",
        action="store_true",
        help="skip repository-transfer stress if runtime is constrained",
    )
    args = parser.parse_args(argv)
    data = load()
    actions = recover(data)
    audit(data, actions)
    run_comparisons(data, actions, include_repo_transfer=not args.skip_repo_transfer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
