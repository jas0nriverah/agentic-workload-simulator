"""Strict D9 event and trajectory metrics.

Scoring is intentionally separate from prediction.  A prediction file may be
committed before labels are opened, and this module then evaluates the frozen
predictions against a separately supplied label file.  Missing or unsupported
targets stay in the denominator.  Zero-duration labels use exact semantics:
zero prediction passes, any positive prediction is an infinite APE.
"""

from __future__ import annotations

import math
from collections import defaultdict
from statistics import mean, median
from typing import Any, Iterable, Mapping, Sequence


GATE_PERCENT = 25.0


class MetricsError(ValueError):
    """Prediction/label rows cannot be scored safely."""


def _number(value: Any, name: str, *, nonnegative: bool = True) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MetricsError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or (nonnegative and result < 0):
        raise MetricsError(f"{name} must be a finite number >= 0")
    return result


def _ape(predicted: float, observed: float) -> float:
    if observed < 0 or predicted < 0:
        raise MetricsError("predicted_ms and observed_ms must be non-negative")
    if observed == 0.0:
        return 0.0 if predicted == 0.0 else math.inf
    return abs(predicted - observed) / observed * 100.0


def _percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return float(ordered[int(round((len(ordered) - 1) * quantile))])


def _sum_metric(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "n": 0,
            "supported_n": 0,
            "unsupported_n": 0,
            "coverage_rate": None,
            "predicted_ms": None,
            "observed_ms": None,
            "ape_pct": None,
            "within25": False,
            "status": "unsupported_no_rows",
        }
    supported = [row for row in rows if row.get("status") == "predicted"]
    unsupported_n = len(rows) - len(supported)
    observed = sum(_number(row["observed_ms"], "observed_ms") for row in rows)
    predicted = sum(_number(row["predicted_ms"], "predicted_ms") for row in supported)
    if unsupported_n:
        return {
            "n": len(rows),
            "supported_n": len(supported),
            "unsupported_n": unsupported_n,
            "coverage_rate": len(supported) / len(rows),
            "predicted_ms": predicted,
            "observed_ms": observed,
            "ape_pct": None,
            "within25": False,
            "status": "unsupported_missing_prediction",
        }
    ape = _ape(predicted, observed)
    return {
        "n": len(rows),
        "supported_n": len(supported),
        "unsupported_n": 0,
        "coverage_rate": 1.0,
        "predicted_ms": predicted,
        "observed_ms": observed,
        "ape_pct": ape,
        "within25": math.isfinite(ape) and ape <= GATE_PERCENT,
        "status": "scored",
    }


def score_rows(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Score one event channel while retaining unsupported rows.

    Rows must contain ``observed_ms`` and either ``predicted_ms`` with
    ``status=predicted`` or a non-predicted status.  ``trajectory_id`` and
    ``event_id`` are carried through in the output for auditability.
    """

    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            raise MetricsError(f"row {index} must be an object")
        if "observed_ms" not in raw:
            raise MetricsError(f"row {index} is missing observed_ms")
        observed = _number(raw["observed_ms"], "observed_ms")
        status = str(raw.get("status") or ("predicted" if "predicted_ms" in raw else "unsupported"))
        item = {
            "event_id": str(raw.get("event_id") or f"row-{index}"),
            "trajectory_id": str(raw.get("trajectory_id") or raw.get("run_id") or "__unassigned__"),
            "target": str(raw.get("target") or "event"),
            "observed_ms": observed,
            "status": status,
        }
        if status == "predicted":
            if "predicted_ms" not in raw:
                raise MetricsError(f"row {index} marked predicted without predicted_ms")
            item["predicted_ms"] = _number(raw["predicted_ms"], "predicted_ms")
            item["ape_pct"] = _ape(item["predicted_ms"], observed)
            item["within25"] = math.isfinite(item["ape_pct"]) and item["ape_pct"] <= GATE_PERCENT
        else:
            item["predicted_ms"] = None
            item["ape_pct"] = None
            item["within25"] = False
        normalized.append(item)

    supported = [row for row in normalized if row["status"] == "predicted"]
    apes = [float(row["ape_pct"]) for row in supported]
    infinite = sum(not math.isfinite(value) for value in apes)
    passed = sum(bool(row["within25"]) for row in normalized)
    event_sum = _sum_metric(normalized)
    return {
        "n": len(normalized),
        "supported_n": len(supported),
        "unsupported_n": len(normalized) - len(supported),
        "coverage_rate": len(supported) / len(normalized) if normalized else None,
        "within25_count": passed,
        "within25_rate_over_required": passed / len(normalized) if normalized else None,
        "within25_rate_over_supported": sum(row["within25"] for row in supported) / len(supported)
        if supported
        else None,
        "mean_ape_pct_over_supported": mean(apes) if apes else None,
        "median_ape_pct_over_supported": median(apes) if apes else None,
        "p95_ape_pct_over_supported": _percentile(apes, 0.95),
        "max_ape_pct_over_supported": max(apes) if apes else None,
        "infinite_ape_count": infinite,
        "event_sum": event_sum,
        "all_within25": bool(normalized) and passed == len(normalized),
        "rows": normalized,
    }


def _identity(row: Mapping[str, Any], index: int = 0) -> tuple[str, str, str, str, str]:
    """Return a collision resistant target identity.

    Event IDs are often only unique within a run.  The trajectory/case/attempt
    scopes are therefore part of the join key even when a caller has omitted
    one of the optional scopes (the empty string is explicit in that case).
    """

    return (
        str(row.get("target") or "event"),
        str(row.get("trajectory_id") or row.get("run_id") or "__unassigned__"),
        str(row.get("case_id") or ""),
        str(row.get("attempt_id") or ""),
        str(row.get("event_id") or row.get("request_id") or f"row-{index}"),
    )


def _validate_unique(
    rows: Sequence[Mapping[str, Any]], label: str
) -> dict[tuple[str, str, str, str, str], Mapping[str, Any]]:
    result: dict[tuple[str, str, str, str, str], Mapping[str, Any]] = {}
    for index, row in enumerate(rows):
        key = _identity(row, index)
        if key in result:
            raise MetricsError(f"duplicate {label} key: {'/'.join(key)}")
        result[key] = row
    return result


def score_bundle(
    predictions: Iterable[Mapping[str, Any]],
    labels: Iterable[Mapping[str, Any]],
    *,
    required_target_kinds: Sequence[str] = (),
) -> dict[str, Any]:
    """Score a frozen prediction bundle against separately supplied labels.

    ``predictions`` and ``labels`` join on ``(target, event_id)``.  A missing
    prediction produces an explicit unsupported row; it is never dropped.
    ``required_target_kinds`` optionally declares the literal conjunction that
    each trajectory must contain (for example CPU, GPU, and E2E targets).
    """

    pred_rows = list(predictions)
    label_rows = list(labels)
    pred_map = _validate_unique(pred_rows, "prediction")
    label_map = _validate_unique(label_rows, "label")
    joined: list[dict[str, Any]] = []
    for key, label in sorted(label_map.items()):
        target, trajectory, case_id, attempt_id, event = key
        trajectory = str(label.get("trajectory_id") or label.get("run_id") or "__unassigned__")
        row = {
            "target": target,
            "event_id": event,
            "trajectory_id": trajectory,
            "case_id": case_id,
            "attempt_id": attempt_id,
            "observed_ms": label.get("observed_ms"),
        }
        prediction = pred_map.get(key)
        if prediction is None:
            row["status"] = "unsupported_missing_prediction"
        else:
            row["status"] = str(
                prediction.get("status")
                or ("predicted" if "predicted_ms" in prediction else "unsupported")
            )
            if "predicted_ms" in prediction:
                row["predicted_ms"] = prediction["predicted_ms"]
        joined.append(row)

    by_target: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_trajectory: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in joined:
        by_target[row["target"]].append(row)
        by_trajectory[row["trajectory_id"]].append(row)
    target_metrics = {
        target: score_rows(rows) for target, rows in sorted(by_target.items())
    }
    required = {str(item) for item in required_target_kinds}
    trajectory_rows: list[dict[str, Any]] = []
    for trajectory, rows in sorted(by_trajectory.items()):
        present = {str(row["target"]) for row in rows}
        missing_targets = sorted(required - present)
        if missing_targets:
            trajectory_rows.append(
                {
                    "trajectory_id": trajectory,
                    "present_targets": sorted(present),
                    "missing_targets": missing_targets,
                    "within25": False,
                    "status": "unsupported_missing_required_target",
                }
            )
            continue
        passes = [
            row["status"] == "predicted"
            and _ape(
                _number(row["predicted_ms"], "predicted_ms"),
                _number(row["observed_ms"], "observed_ms"),
            )
            <= GATE_PERCENT
            for row in rows
        ]
        trajectory_rows.append(
            {
                "trajectory_id": trajectory,
                "present_targets": sorted(present),
                "missing_targets": [],
                "required_row_count": len(rows),
                "within25": bool(rows) and all(passes),
                "status": (
                    "scored"
                    if all(row["status"] == "predicted" for row in rows)
                    else "unsupported_missing_prediction"
                ),
            }
        )
    pass_count = sum(bool(row["within25"]) for row in trajectory_rows)
    present_targets = set(by_target)
    overlapping_native = bool(present_targets & {"native_e2e", "conditional_native_e2e"}) and bool(
        present_targets & {"native_queue", "native_prefill", "native_decode", "conditional_native_phase"}
    )
    composition_status = (
        "overlap_rejected_native_e2e_and_phase_targets"
        if overlapping_native
        else "no_native_e2e_phase_overlap_detected"
    )
    return {
        "schema_version": "assignment.d9-strict-metrics.v1",
        "gate_percent": GATE_PERCENT,
        "prediction_rows": len(pred_rows),
        "label_rows": len(label_rows),
        "unmatched_prediction_count": len(set(pred_map) - set(label_map)),
        "required_target_kinds": sorted(required),
        "targets": target_metrics,
        "trajectory_count": len(trajectory_rows),
        "trajectory_pass_count": pass_count,
        "trajectory_pass_rate": pass_count / len(trajectory_rows) if trajectory_rows else None,
        "all_required_trajectory_pass": bool(trajectory_rows)
        and pass_count == len(trajectory_rows),
        "composition_status": composition_status,
        "trajectory_rows": trajectory_rows,
        "literal_d9_status": (
            "PASS"
            if trajectory_rows
            and pass_count == len(trajectory_rows)
            and not (set(pred_map) - set(label_map))
            and not overlapping_native
            else "UNPROVEN_OR_FAILED"
        ),
    }


__all__ = ["GATE_PERCENT", "MetricsError", "score_bundle", "score_rows"]
