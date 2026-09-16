#!/usr/bin/env python3
"""Leakage-safe, interpretable CPU event predictors for the offline D9 view.

The module accepts already identity-filtered action rows and keeps all target
handling in the fit/evaluation driver.  Prediction keys are coarse semantic
descriptors only; exact command text, repository/case identity and measured
current-event fields never enter a key.
"""

from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import math
import re
from statistics import median
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = "assignment.offline-cpu-event-predictor.v1"
EXTRACTOR_ID = "offline-cpu-semantic-whitelist.v1"
GATE_PCT = 25.0
MIN_EVENTS = 25
MIN_INSTANCES = 3
OUTER_FOLDS = 5
INNER_FOLDS = 3
INNER_PREFIX = "assignment.d9.cpu-inner-v1:"
OUTER_PREFIX = "assignment.d9.train-fold-v1:"
CANDIDATES = (
    "coarse_class_median",
    "mechanism_median",
    "log_geometric",
    "coverage_center",
    "tail_shrinkage",
)
COMPLEXITY = {name: index for index, name in enumerate(CANDIDATES)}

FORBIDDEN_FEATURE_NAMES = frozenset(
    {
        "observed_ms",
        "duration_ms",
        "execution_time",
        "wall_ms",
        "current_duration",
        "output_tokens",
        "return_bytes",
        "output_bytes",
        "measured_residual_ms",
        "unknown_residual_ms",
        "residual_ms",
        "failure",
        "status",
        "outcome",
        "end_state",
        "future_state",
        "current_event",
        "case_id",
        "repository",
        "repo",
        "command",
        "action",
        "command_sha256",
        "exact_command",
    }
)

_ORDINAL_RE = re.compile(r"-tool-(\d+)$")


class ContractError(ValueError):
    """Input violates the prospective CPU feature contract."""


def _finite_positive(value: Any, field: str = "value") -> float:
    if isinstance(value, bool):
        raise ContractError(f"{field} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{field} must be numeric") from exc
    if not math.isfinite(number) or number <= 0:
        raise ContractError(f"{field} must be finite and positive")
    return number


def _fold(instance_id: str, prefix: str = OUTER_PREFIX, count: int = OUTER_FOLDS) -> int:
    digest = hashlib.sha256((prefix + str(instance_id)).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % count


def outer_fold(instance_id: str) -> int:
    return _fold(instance_id, OUTER_PREFIX, OUTER_FOLDS)


def inner_fold(instance_id: str) -> int:
    return _fold(instance_id, INNER_PREFIX, INNER_FOLDS)


def event_ordinal(event_id: str) -> int | None:
    match = _ORDINAL_RE.search(str(event_id))
    return int(match.group(1)) if match else None


def _text(value: Any, default: str = "unknown") -> str:
    if value is None:
        return default
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).strip().lower()
    return text or default


def _bucket(value: Any, boundaries: Sequence[float]) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "missing"
    if not math.isfinite(number) or number < 0:
        return "missing"
    for index, boundary in enumerate(boundaries):
        if number <= boundary:
            return str(index)
    return str(len(boundaries))


def _count_bucket(value: Any) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return "missing"
    if number <= 0:
        return "0"
    if number == 1:
        return "1"
    if number == 2:
        return "2"
    return "3+"


def _boolish(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        return int(float(value) != 0)
    except (TypeError, ValueError):
        return 0


def _runner_family(value: Any) -> str:
    text = _text(value, "none")
    if text in {"", "none", "unknown"}:
        return "none"
    if text.startswith("module:") or text in {"python", "python3", "script"}:
        return "python_module"
    if text in {"pytest", "py.test", "unittest", "tox", "tox-uv"}:
        return text
    return "other_runner"


def _imports_bucket(value: Any) -> str:
    if isinstance(value, str):
        values = (value,)
    elif isinstance(value, (list, tuple)):
        values = tuple(str(item).lower() for item in value)
    else:
        values = tuple()
    if not values:
        return "none"
    if "opaque" in values:
        return "opaque"
    if "script" in values:
        return "script"
    if "known_test_runner" in values:
        return "runner"
    return "inline_import"


def _command_bytes_bucket(row: Mapping[str, Any]) -> str:
    for key in ("byte_bucket", "declared_command_bytes_bucket"):
        if key in row and row.get(key) is not None:
            try:
                value = int(row[key])
            except (TypeError, ValueError):
                break
            return str(max(0, min(4, value)))
    for key in ("declared_command_bytes", "command_bytes"):
        if key in row:
            return _bucket(row[key], (64, 256, 1024))
    return "missing"


def _feature_value(features: Mapping[str, Any], row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in features and features.get(key) is not None:
            return features.get(key)
        if key in row and row.get(key) is not None:
            return row.get(key)
    return None


def canonical_features(row: Mapping[str, Any], features: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Whitelist low-cardinality, pre-event descriptors.

    ``features`` may be the output of the shared semantic extractor.  The
    returned map deliberately excludes repository, command text and every
    target/current-event field.
    """

    source: Mapping[str, Any] = features if isinstance(features, Mapping) else row
    leaked = FORBIDDEN_FEATURE_NAMES.intersection(str(key).lower() for key in source)
    if leaked:
        raise ContractError("forbidden feature field(s): " + ", ".join(sorted(leaked)))
    semantic_class = _text(_feature_value(source, row, "semantic_class", "operation_class", "original_class"), "shell")
    operation = _text(_feature_value(source, row, "operation", "semantic_operation", "subcommand"), "unknown")
    executable = _text(_feature_value(source, row, "executable", "tool_name", "launch_family"), "unknown")
    pager = _text(_feature_value(source, row, "git_pager_susceptibility", "git_pager_mode"), "none")
    execution_mode = _text(_feature_value(source, row, "execution_mode", "mode"), "normal")
    find_exec = _text(_feature_value(source, row, "find_exec_mode"), "none")
    pipeline = _feature_value(source, row, "pipeline_stage_bucket")
    if pipeline is None:
        pipeline = _bucket(_feature_value(source, row, "n_pipes", "pipeline_stage_count"), (0, 1, 2))
    else:
        pipeline = _count_bucket(pipeline)
    operand = _feature_value(source, row, "operand_count_bucket")
    if operand is None:
        operand = _count_bucket(_feature_value(source, row, "declared_path_count", "operand_count"))
    else:
        operand = _count_bucket(operand)
    recursive = _boolish(_feature_value(source, row, "recursive"))
    imports = _imports_bucket(_feature_value(source, row, "python_inline_imports"))
    runner = _runner_family(_feature_value(source, row, "runner"))
    # Declared work features are accepted only as coarse, explicit buckets.
    declared_work = _text(_feature_value(source, row, "declared_work_bucket"), "missing")
    return {
        "semantic_class": semantic_class,
        "operation": operation,
        "executable": executable,
        "runner": runner,
        "execution_mode": execution_mode,
        "git_pager_susceptibility": pager,
        "find_exec_mode": find_exec,
        "pipeline_stage_bucket": pipeline,
        "operand_count_bucket": operand,
        "recursive": recursive,
        "python_imports_bucket": imports,
        "declared_command_bytes_bucket": _command_bytes_bucket(row),
        "declared_work_bucket": declared_work,
    }


def mechanism_key(features: Mapping[str, Any]) -> tuple[str, ...]:
    """Stable coarse mechanism key with no exact action/repository identity."""

    return (
        _text(features.get("semantic_class")),
        _text(features.get("operation")),
        _text(features.get("executable")),
        _text(features.get("runner"), "none"),
        _text(features.get("execution_mode"), "normal"),
        _text(features.get("git_pager_susceptibility"), "none"),
        _text(features.get("find_exec_mode"), "none"),
        _text(features.get("pipeline_stage_bucket"), "missing"),
        str(features.get("recursive", 0)),
        _text(features.get("operand_count_bucket"), "missing"),
        _text(features.get("python_imports_bucket"), "none"),
        _text(features.get("declared_command_bytes_bucket"), "missing"),
        _text(features.get("declared_work_bucket"), "missing"),
    )


def encode_key(key: Any) -> str:
    return json.dumps(key, sort_keys=True, separators=(",", ":"))


def _level_key(level: str, value: Any) -> str:
    return level + "|" + encode_key(value)


def _route(row: Mapping[str, Any], candidate: str) -> list[tuple[str, str]]:
    features = row.get("features") if isinstance(row.get("features"), Mapping) else {}
    original = _text(row.get("original_class"), "shell")
    semantic = _text(features.get("semantic_class"), original)
    operation = _text(features.get("operation"), "unknown")
    mech = mechanism_key(features)
    route: list[tuple[str, str]] = []
    if candidate == "coarse_class_median":
        route.append(("class", _level_key("class", original)))
    else:
        route.extend(
            [
                ("mechanism", _level_key("mechanism", mech)),
                ("operation", _level_key("operation", (semantic, operation))),
                ("semantic_class", _level_key("semantic_class", semantic)),
                ("class", _level_key("class", original)),
            ]
        )
    route.append(("global", _level_key("global", "all")))
    return route


def _parent_key(level: str, key: str, groups: Mapping[str, Mapping[str, Any]]) -> str | None:
    if level == "global":
        return None
    try:
        encoded = key.split("|", 1)[1]
        value = json.loads(encoded)
    except (IndexError, TypeError, ValueError):
        return None
    if level == "mechanism":
        values = tuple(value) if isinstance(value, list) else tuple()
        return _level_key("operation", (values[0], values[1])) if len(values) >= 2 else None
    if level == "operation":
        values = tuple(value) if isinstance(value, list) else tuple()
        return _level_key("semantic_class", values[0]) if values else None
    if level == "semantic_class":
        values = tuple(value) if isinstance(value, list) else (value,)
        return _level_key("class", values[0] if values else "shell")
    if level == "class":
        return _level_key("global", "all")
    return None


def _median_center(values: Sequence[float]) -> float:
    return float(median(values)) if values else 1.0


def _geometric_center(values: Sequence[float]) -> float:
    if not values:
        return 1.0
    return float(math.exp(sum(math.log(max(value, 1e-12)) for value in values) / len(values)))


def _gate_center(values: Sequence[float]) -> float:
    """Maximize the finite-sample <=25% gate in O(n log n).

    Each target y contributes the interval [0.75y, 1.25y] of valid centers.
    A sorted endpoint sweep finds maximum overlap; reciprocal prefix sums then
    resolve tied centers by exact finite-sample MAPE without an O(n^2) scan.
    """

    observations = sorted(max(float(value), 1e-12) for value in values)
    if not observations:
        return 1.0
    starts: dict[float, int] = defaultdict(int)
    ends: dict[float, int] = defaultdict(int)
    for observation in observations:
        starts[0.75 * observation] += 1
        ends[1.25 * observation] += 1
    # Coverage is piecewise constant between endpoints.  Include observations
    # as tie candidates because they can minimize MAPE inside a full-coverage
    # interval (for example [1, 1] should choose center 1, not .75).
    coverage_candidates = sorted(set(starts) | set(ends) | set(observations))
    if not coverage_candidates:
        return float(observations[0])
    reciprocal_prefix = [0.0]
    for observation in observations:
        reciprocal_prefix.append(reciprocal_prefix[-1] + 1.0 / observation)

    def mape(point: float) -> float:
        import bisect

        split = bisect.bisect_right(observations, point)
        below = point * reciprocal_prefix[split] - split
        above = (len(observations) - split) - point * (reciprocal_prefix[-1] - reciprocal_prefix[split])
        return (below + above) * 100.0

    scored: list[tuple[int, float, float]] = []
    for point in coverage_candidates:
        # Recompute overlap at a candidate endpoint with binary searches. The
        # number of endpoints is O(n), so this remains O(n log n) overall.
        import bisect

        left = bisect.bisect_left(observations, point / 1.25)
        right = bisect.bisect_right(observations, point / 0.75)
        # Correct endpoint roundoff so the score agrees with the <=25% gate.
        while left < right and abs(point - observations[left]) / observations[left] * 100.0 > GATE_PCT + 1e-10:
            left += 1
        while right > left and abs(point - observations[right - 1]) / observations[right - 1] * 100.0 > GATE_PCT + 1e-10:
            right -= 1
        count = right - left
        scored.append((count, mape(point), point))
    best_coverage = max(item[0] for item in scored)
    tied = [item for item in scored if item[0] == best_coverage]
    min_mape = min(item[1] for item in tied)
    tied = [item for item in tied if abs(item[1] - min_mape) <= 1e-10]
    return float(min(tied, key=lambda item: (math.log(max(item[2], 1e-12)), item[2]))[2])


def _p95(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = int(round(0.95 * (len(ordered) - 1)))
    return float(ordered[index])


def _p90(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return float(ordered[int(round(0.90 * (len(ordered) - 1)))])


class CpuEventPredictor:
    """Support-aware hierarchical predictor for one fixed candidate."""

    def __init__(self, candidate: str, min_events: int = MIN_EVENTS, min_instances: int = MIN_INSTANCES) -> None:
        if candidate not in CANDIDATES:
            raise ValueError(f"unknown candidate: {candidate}")
        if min_events <= 0 or min_instances <= 0:
            raise ValueError("support thresholds must be positive")
        self.candidate = candidate
        self.min_events = int(min_events)
        self.min_instances = int(min_instances)
        self.groups: dict[str, dict[str, Any]] = {}
        self._fitted = False

    def fit(self, rows: Sequence[Mapping[str, Any]]) -> "CpuEventPredictor":
        buckets: dict[str, list[float]] = defaultdict(list)
        instances: dict[str, set[str]] = defaultdict(set)
        for row in rows:
            feature_map = row.get("features")
            if isinstance(feature_map, Mapping):
                leaked = FORBIDDEN_FEATURE_NAMES.intersection(str(key).lower() for key in feature_map)
                if leaked:
                    raise ContractError("forbidden feature field(s): " + ", ".join(sorted(leaked)))
            target = _finite_positive(row.get("observed_ms"), "observed_ms")
            for level, key in _route(row, self.candidate):
                buckets[key].append(target)
                instances[key].add(str(row.get("instance_id") or ""))
        self.groups = {}
        for key, values in buckets.items():
            self.groups[key] = {
                "level": key.split("|", 1)[0],
                "n": len(values),
                "distinct_instances": len(instances[key]),
                "median": _median_center(values),
                "p90": _p90(values),
                "values": values,
            }
        centers: dict[str, float] = {}

        def center_for(key: str) -> float:
            if key in centers:
                return centers[key]
            group = self.groups[key]
            base = float(group["median"])
            if self.candidate == "log_geometric":
                base = _geometric_center(group["values"])
            elif self.candidate == "coverage_center":
                base = _gate_center(group["values"])
            parent = _parent_key(group["level"], key, self.groups)
            if self.candidate == "tail_shrinkage" and group["level"] not in {"class", "global"} and parent in self.groups:
                parent_center = center_for(parent)
                median_value = max(float(group["median"]), 1e-12)
                tail_ratio = max(1.0, float(group["p90"] or median_value) / median_value)
                weight = group["n"] / (group["n"] + 25.0 * tail_ratio)
                base = math.exp(weight * math.log(max(base, 1e-12)) + (1.0 - weight) * math.log(max(parent_center, 1e-12)))
                group["tail_ratio"] = tail_ratio
                group["shrink_weight"] = weight
            centers[key] = max(base, 1e-9)
            return centers[key]

        for key in sorted(self.groups):
            self.groups[key]["prediction"] = center_for(key)
            # Values are retained only in memory for fitting; serialized model
            # tables below omit target samples to keep the artifact predictive.
        for group in self.groups.values():
            group.pop("values", None)
        self._fitted = True
        return self

    def _supported(self, group: Mapping[str, Any]) -> bool:
        return int(group.get("n", 0)) >= self.min_events and int(group.get("distinct_instances", 0)) >= self.min_instances

    def predict_details(self, row: Mapping[str, Any]) -> dict[str, Any]:
        if not self._fitted:
            raise RuntimeError("predictor is not fitted")
        feature_map = row.get("features")
        if isinstance(feature_map, Mapping):
            leaked = FORBIDDEN_FEATURE_NAMES.intersection(str(key).lower() for key in feature_map)
            if leaked:
                raise ContractError("forbidden feature field(s): " + ", ".join(sorted(leaked)))
        feature_status = str(row.get("feature_status") or "missing")
        route = _route(row, self.candidate)
        if feature_status != "ok":
            route = [(level, key) for level, key in route if level in {"class", "global"}]
        for level, key in route:
            group = self.groups.get(key)
            if group is None or not self._supported(group):
                continue
            return {
                "prediction": float(group["prediction"]),
                "selected_level": level,
                "selected_key": key,
                "support": {"n": int(group["n"]), "distinct_instances": int(group["distinct_instances"])},
                "fallback_reason": None if level not in {"class", "global"} else "class_or_global_backoff",
            }
        # A valid fit always has a global group, but retain a finite fail-safe.
        global_key = _level_key("global", "all")
        group = self.groups.get(global_key)
        if group is None:
            return {"prediction": 1.0, "selected_level": "constant", "selected_key": None, "support": {"n": 0, "distinct_instances": 0}, "fallback_reason": "no_support"}
        return {
            "prediction": float(group["prediction"]),
            "selected_level": "global",
            "selected_key": global_key,
            "support": {"n": int(group["n"]), "distinct_instances": int(group["distinct_instances"])},
            "fallback_reason": "no_support",
        }

    def predict(self, row: Mapping[str, Any]) -> float:
        return float(self.predict_details(row)["prediction"])

    def to_mapping(self) -> dict[str, Any]:
        if not self._fitted:
            raise RuntimeError("predictor is not fitted")
        groups = []
        for key, group in sorted(self.groups.items()):
            item = dict(group)
            item["key"] = key
            groups.append(item)
        return {
            "schema_version": SCHEMA_VERSION,
            "extractor_id": EXTRACTOR_ID,
            "candidate": self.candidate,
            "min_events": self.min_events,
            "min_instances": self.min_instances,
            "groups": groups,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "CpuEventPredictor":
        model = cls(str(payload["candidate"]), int(payload.get("min_events", MIN_EVENTS)), int(payload.get("min_instances", MIN_INSTANCES)))
        model.groups = {str(item["key"]): dict(item) for item in payload.get("groups", []) if isinstance(item, Mapping) and item.get("key")}
        model._fitted = True
        return model


def metric_rows(rows: Sequence[Mapping[str, Any]], prediction_key: str = "prediction", include_breakdowns: bool = True) -> dict[str, Any]:
    """Return event, class, instance-trajectory and CPU-sum metrics."""

    if not rows:
        return {"n": 0, "miss_count": 0, "within25_rate": None}
    apes: list[float] = []
    abs_error = 0.0
    signed_bias = 0.0
    observed_sum = 0.0
    predicted_sum = 0.0
    for row in rows:
        observed = _finite_positive(row["observed_ms"], "observed_ms")
        prediction = _finite_positive(row[prediction_key], "prediction")
        apes.append(abs(prediction - observed) / observed * 100.0)
        abs_error += abs(prediction - observed)
        signed_bias += prediction - observed
        observed_sum += observed
        predicted_sum += prediction
    misses = sum(ape > GATE_PCT for ape in apes)
    by_instance: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    by_run: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    by_class: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        by_instance[str(row.get("instance_id") or "__missing_instance__")].append(row)
        run_id = row.get("run_id")
        if run_id:
            by_run[str(run_id)].append(row)
        by_class[str(row.get("original_class") or "unknown")].append(row)

    def group_pass(items: Sequence[Mapping[str, Any]]) -> bool:
        return all(abs(float(item[prediction_key]) - float(item["observed_ms"])) / float(item["observed_ms"]) * 100.0 <= GATE_PCT for item in items)

    def sum_metric(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        obs = sum(float(item["observed_ms"]) for item in items)
        pred = sum(float(item[prediction_key]) for item in items)
        return {"n_events": len(items), "observed_ms": obs, "predicted_ms": pred, "ape_pct": abs(pred - obs) / obs * 100.0 if obs else None, "within25": abs(pred - obs) / obs * 100.0 <= GATE_PCT if obs else False}

    ordered = sorted(apes)
    return {
        "n": len(rows),
        "miss_count": misses,
        "within25_rate": 1.0 - misses / len(rows),
        "mean_ape_pct": sum(apes) / len(apes),
        "median_ape_pct": median(apes),
        "p95_ape_pct": _p95(apes),
        "worst_ape_pct": max(apes),
        "max_ape_pct": max(apes),
        "abs_error_ms": abs_error,
        "observed_ms": observed_sum,
        "predicted_ms": predicted_sum,
        "signed_bias_ms": signed_bias,
        "instance_trajectory_count": len(by_instance),
        "instance_trajectory_pass_count": sum(group_pass(items) for items in by_instance.values()),
        "instance_trajectory_pass_rate": sum(group_pass(items) for items in by_instance.values()) / len(by_instance) if by_instance else None,
        "run_trajectory_count": len(by_run),
        "run_trajectory_pass_count": sum(group_pass(items) for items in by_run.values()),
        "run_trajectory_pass_rate": sum(group_pass(items) for items in by_run.values()) / len(by_run) if by_run else None,
        "cpu_sum_gate": sum_metric(rows),
        "unsupported_feature_count": sum(str(row.get("feature_status")) != "ok" for row in rows),
        "fallback_prediction_count": sum(str(row.get("selected_level")) in {"class", "global", "constant"} for row in rows),
        "by_original_class": {
            name: metric_rows(items, prediction_key, include_breakdowns=False)
            for name, items in sorted(by_class.items())
        }
        if include_breakdowns
        else {},
        "by_instance_cpu_sum": {name: sum_metric(items) for name, items in sorted(by_instance.items())}
        if include_breakdowns
        else {},
    }


def selection_key(metrics: Mapping[str, Any], baseline: Mapping[str, Any]) -> tuple[Any, ...]:
    """Inner rule: coverage subject to baseline worst APE, then worst/complexity."""

    eligible = float(metrics.get("worst_ape_pct", math.inf)) <= float(baseline.get("worst_ape_pct", math.inf)) + 1e-12
    return (
        0 if eligible else 1,
        -float(metrics.get("within25_rate", 0.0)),
        float(metrics.get("worst_ape_pct", math.inf)),
    )


def choose_candidate(metrics_by_candidate: Mapping[str, Mapping[str, Any]], baseline_name: str = "coarse_class_median") -> str:
    baseline = metrics_by_candidate[baseline_name]
    return min(
        metrics_by_candidate,
        key=lambda name: (*selection_key(metrics_by_candidate[name], baseline), COMPLEXITY.get(name, 999), name),
    )


def predict_class_hybrid(model_payload: Mapping[str, Any], row: Mapping[str, Any]) -> dict[str, Any]:
    """Predict from a packaged class-hybrid mapping with coarse fallback.

    The payload is the separate ``class_hybrid_model.json`` artifact.  A
    missing/unknown original class deliberately routes to the coarse class
    model; callers must provide only pre-event feature fields in ``row``.
    """

    models = model_payload.get("models")
    class_map = model_payload.get("class_candidate_map")
    if not isinstance(models, Mapping) or not isinstance(class_map, Mapping):
        raise ContractError("invalid class-hybrid model payload")
    original_class = str(row.get("original_class") or "")
    candidate = str(class_map.get(original_class) or "coarse_class_median")
    if candidate not in models:
        candidate = "coarse_class_median"
    model = CpuEventPredictor.from_mapping(models[candidate])
    details = model.predict_details(row)
    details["selected_candidate"] = candidate
    return details


def json_hash(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


__all__ = [
    "CANDIDATES",
    "COMPLEXITY",
    "ContractError",
    "CpuEventPredictor",
    "EXTRACTOR_ID",
    "FORBIDDEN_FEATURE_NAMES",
    "GATE_PCT",
    "INNER_FOLDS",
    "MIN_EVENTS",
    "MIN_INSTANCES",
    "OUTER_FOLDS",
    "canonical_features",
    "choose_candidate",
    "encode_key",
    "event_ordinal",
    "inner_fold",
    "json_hash",
    "mechanism_key",
    "metric_rows",
    "outer_fold",
    "predict_class_hybrid",
    "selection_key",
]
