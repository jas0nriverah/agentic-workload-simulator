"""Calibration-only sequential latency model for D9.

Predictions for event n may use:
* sealed pre-event features of event n
* revealed labels of events 0..n-1 in the same trajectory (cited by sha256)

They must never use the current event's wall time or output tokens, any later
event, holdout labels during fitting, or evaluator resolve labels.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from math import isfinite
from statistics import median
from typing import Any, Iterable, Mapping, Sequence

from agentic_sim.assignment.event_simulator import (
    EventSimulatorError,
    ModelEventInput,
    ToolEventInput,
    _positive_number,
    canonical_sha256,
)
from agentic_sim.assignment.tool_features import EXTRACTOR_ID, extractor_source_sha256


def _median(values: Sequence[float]) -> float:
    ordered = [float(item) for item in values if isfinite(float(item))]
    if not ordered:
        raise EventSimulatorError("median requires a non-empty finite sample")
    return float(median(ordered))


def _encode_key(parts: Sequence[Any]) -> str:
    return "\x1f".join(str(part) for part in parts)


def _decode_key(text: str) -> tuple[str, ...]:
    return tuple(text.split("\x1f"))


def _linreg_out_ctx(rows: Sequence[Mapping[str, Any]]) -> tuple[float, float, float]:
    n = len(rows)
    if n < 2:
        raise EventSimulatorError("linear output/context fit needs at least two rows")
    s1 = float(n)
    so = sum(float(row["output_tokens"]) for row in rows)
    sc = sum(float(row["context_tokens"]) for row in rows)
    sy = sum(float(row["observed_ms"]) for row in rows)
    soo = sum(float(row["output_tokens"]) ** 2 for row in rows)
    scc = sum(float(row["context_tokens"]) ** 2 for row in rows)
    soc = sum(float(row["output_tokens"]) * float(row["context_tokens"]) for row in rows)
    soy = sum(float(row["output_tokens"]) * float(row["observed_ms"]) for row in rows)
    scy = sum(float(row["context_tokens"]) * float(row["observed_ms"]) for row in rows)
    matrix = [[s1, so, sc, sy], [so, soo, soc, soy], [sc, soc, scc, scy]]
    for i in range(3):
        pivot = max(range(i, 3), key=lambda row: abs(matrix[row][i]))
        matrix[i], matrix[pivot] = matrix[pivot], matrix[i]
        denom = matrix[i][i] if matrix[i][i] != 0 else 1e-12
        for column in range(i, 4):
            matrix[i][column] /= denom
        for row in range(3):
            if row == i:
                continue
            factor = matrix[row][i]
            for column in range(i, 4):
                matrix[row][column] -= factor * matrix[i][column]
    return float(matrix[0][3]), float(matrix[1][3]), float(matrix[2][3])


@dataclass(frozen=True)
class PriorEventSummary:
    """Journal-derived summary of already-revealed earlier events."""

    prior_event_count: int
    prior_median_output_tokens: float
    prior_median_observed_ms: float
    prior_label_sha256s: tuple[str, ...]
    prior_tool_sha_medians: tuple[tuple[str, float], ...]
    prior_tool_name_medians: tuple[tuple[str, str, float], ...]
    last_output_tokens: tuple[float, ...]

    def to_mapping(self) -> dict[str, Any]:
        return {
            "prior_event_count": self.prior_event_count,
            "prior_median_output_tokens": self.prior_median_output_tokens,
            "prior_median_observed_ms": self.prior_median_observed_ms,
            "prior_label_sha256s": list(self.prior_label_sha256s),
            "prior_tool_sha_medians": [
                {"command_sha256": sha, "median_ms": value} for sha, value in self.prior_tool_sha_medians
            ],
            "prior_tool_name_medians": [
                {"operation_class": cls, "tool_name": name, "median_ms": value}
                for cls, name, value in self.prior_tool_name_medians
            ],
            "last_output_tokens": list(self.last_output_tokens),
        }

    @classmethod
    def empty(cls) -> "PriorEventSummary":
        return cls(0, 0.0, 0.0, (), (), (), ())


@dataclass(frozen=True)
class SequentialLatencyModel:
    extractor_id: str
    extractor_sha256: str
    sha_medians: dict[str, float]
    key_medians: dict[str, float]
    prefix_medians: dict[str, float]
    tool_name_medians: dict[str, float]
    class_medians: dict[str, float]
    global_tool_median: float
    model_intercept: float
    model_output_coef: float
    model_context_coef: float
    context_output_medians: dict[str, float]
    global_output_median: float
    e2e_scale: float
    calibration_run_ids: tuple[str, ...]

    def to_mapping(self) -> dict[str, Any]:
        return {
            "kind": "sequential_lookup",
            "extractor_id": self.extractor_id,
            "extractor_sha256": self.extractor_sha256,
            "sha_medians": self.sha_medians,
            "key_medians": self.key_medians,
            "prefix_medians": self.prefix_medians,
            "tool_name_medians": self.tool_name_medians,
            "class_medians": self.class_medians,
            "global_tool_median": self.global_tool_median,
            "model_intercept": self.model_intercept,
            "model_output_coef": self.model_output_coef,
            "model_context_coef": self.model_context_coef,
            "context_output_medians": self.context_output_medians,
            "global_output_median": self.global_output_median,
            "e2e_scale": self.e2e_scale,
            "calibration_run_ids": list(self.calibration_run_ids),
            "coefficients": [1.0],
        }

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "SequentialLatencyModel":
        if not isinstance(row, Mapping) or row.get("kind") != "sequential_lookup":
            raise EventSimulatorError("sequential model mapping is invalid")
        if row.get("extractor_id") != EXTRACTOR_ID:
            raise EventSimulatorError("frozen extractor_id does not match this source")
        if row.get("extractor_sha256") != extractor_source_sha256():
            raise EventSimulatorError("frozen extractor_sha256 does not match this source")
        return cls(
            extractor_id=str(row["extractor_id"]),
            extractor_sha256=str(row["extractor_sha256"]),
            sha_medians={str(k): float(v) for k, v in dict(row["sha_medians"]).items()},
            key_medians={str(k): float(v) for k, v in dict(row["key_medians"]).items()},
            prefix_medians={str(k): float(v) for k, v in dict(row["prefix_medians"]).items()},
            tool_name_medians={str(k): float(v) for k, v in dict(row["tool_name_medians"]).items()},
            class_medians={str(k): float(v) for k, v in dict(row["class_medians"]).items()},
            global_tool_median=float(row["global_tool_median"]),
            model_intercept=float(row["model_intercept"]),
            model_output_coef=float(row["model_output_coef"]),
            model_context_coef=float(row["model_context_coef"]),
            context_output_medians={
                str(k): float(v) for k, v in dict(row["context_output_medians"]).items()
            },
            global_output_median=float(row["global_output_median"]),
            e2e_scale=float(row["e2e_scale"]),
            calibration_run_ids=tuple(row.get("calibration_run_ids") or ()),
        )

    def predict_tool_ms(
        self,
        features: Mapping[str, Any],
        prior: PriorEventSummary | None = None,
    ) -> float:
        prior = prior or PriorEventSummary.empty()
        sha = str(features["command_sha256"])
        for item_sha, value in prior.prior_tool_sha_medians:
            if item_sha == sha:
                return max(1e-6, value)
        if sha in self.sha_medians:
            return max(1e-6, self.sha_medians[sha])
        key = _encode_key(
            (
                features["operation_class"],
                features["tool_name"],
                int(features["declared_path_count"]),
                int(features["has_pipe"]),
                int(features["has_glob"]),
                int(features["declared_command_bytes"]) // 32,
            )
        )
        if key in self.key_medians:
            return max(1e-6, self.key_medians[key])
        prefix = _encode_key((features["operation_class"], features["command_prefix"]))
        if prefix in self.prefix_medians:
            return max(1e-6, self.prefix_medians[prefix])
        tool_key = _encode_key((features["operation_class"], features["tool_name"]))
        for cls, name, value in prior.prior_tool_name_medians:
            if cls == features["operation_class"] and name == features["tool_name"]:
                return max(1e-6, value)
        if tool_key in self.tool_name_medians:
            return max(1e-6, self.tool_name_medians[tool_key])
        return max(
            1e-6,
            self.class_medians.get(str(features["operation_class"]), self.global_tool_median),
        )

    def predict_model_ms(
        self,
        features: Mapping[str, Any],
        prior: PriorEventSummary | None = None,
    ) -> float:
        prior = prior or PriorEventSummary.empty()
        context = float(features["context_tokens"])
        if prior.last_output_tokens:
            predicted_out = _median(prior.last_output_tokens[-3:])
        else:
            predicted_out = self.context_output_medians.get(
                str(int(context) // 500),
                self.global_output_median,
            )
        predicted = (
            self.model_intercept
            + self.model_output_coef * predicted_out
            + self.model_context_coef * context
        )
        return max(1e-6, predicted)

    def predict_e2e_ms(self, predicted_tool_ms: float, predicted_model_ms: float) -> float:
        return max(1e-6, self.e2e_scale * (predicted_tool_ms + predicted_model_ms))


def fit_sequential_model(
    tool_rows: Iterable[Mapping[str, Any]],
    model_rows: Iterable[Mapping[str, Any]],
    trajectory_rows: Iterable[Mapping[str, Any]],
) -> SequentialLatencyModel:
    tools = list(tool_rows)
    models = list(model_rows)
    trajectories = list(trajectory_rows)
    if not tools or not models or not trajectories:
        raise EventSimulatorError("sequential fit requires tool, model, and trajectory rows")
    for row in tools + models:
        if row.get("split") and row["split"] != "calibration":
            raise EventSimulatorError("holdout rows may not enter sequential fitting")
        if "observed_ms" not in row and "wall_ms" not in row:
            raise EventSimulatorError("calibration rows require observed_ms")
    sha: dict[str, list[float]] = defaultdict(list)
    keys: dict[str, list[float]] = defaultdict(list)
    prefixes: dict[str, list[float]] = defaultdict(list)
    names: dict[str, list[float]] = defaultdict(list)
    classes: dict[str, list[float]] = defaultdict(list)
    walls: list[float] = []
    for row in tools:
        wall = _positive_number(row.get("observed_ms", row.get("wall_ms")), "observed_ms")
        walls.append(wall)
        sha[str(row["command_sha256"])].append(wall)
        keys[
            _encode_key(
                (
                    row["operation_class"],
                    row["tool_name"],
                    int(row["declared_path_count"]),
                    int(row["has_pipe"]),
                    int(row["has_glob"]),
                    int(row["declared_command_bytes"]) // 32,
                )
            )
        ].append(wall)
        prefixes[_encode_key((row["operation_class"], row["command_prefix"]))].append(wall)
        names[_encode_key((row["operation_class"], row["tool_name"]))].append(wall)
        classes[str(row["operation_class"])].append(wall)
    intercept, out_coef, ctx_coef = _linreg_out_ctx(
        [
            {
                "output_tokens": int(row["output_tokens"]),
                "context_tokens": int(row["context_tokens"]),
                "observed_ms": _positive_number(row.get("observed_ms", row.get("wall_ms")), "observed_ms"),
            }
            for row in models
            if row.get("output_tokens") is not None
        ]
    )
    ctx_out: dict[str, list[float]] = defaultdict(list)
    outputs: list[float] = []
    for row in models:
        output = float(row["output_tokens"])
        outputs.append(output)
        ctx_out[str(int(row["context_tokens"]) // 500)].append(output)

    draft = SequentialLatencyModel(
        extractor_id=EXTRACTOR_ID,
        extractor_sha256=extractor_source_sha256(),
        sha_medians={key: _median(values) for key, values in sha.items()},
        key_medians={key: _median(values) for key, values in keys.items()},
        prefix_medians={key: _median(values) for key, values in prefixes.items()},
        tool_name_medians={key: _median(values) for key, values in names.items()},
        class_medians={key: _median(values) for key, values in classes.items()},
        global_tool_median=_median(walls),
        model_intercept=intercept,
        model_output_coef=out_coef,
        model_context_coef=ctx_coef,
        context_output_medians={key: _median(values) for key, values in ctx_out.items()},
        global_output_median=_median(outputs),
        e2e_scale=1.0,
        calibration_run_ids=tuple(sorted({str(row["run_id"]) for row in trajectories})),
    )
    scales: list[float] = []
    tools_by: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    models_by: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in tools:
        tools_by[str(row["run_id"])].append(row)
    for row in models:
        models_by[str(row["run_id"])].append(row)
    e2e = {
        str(row["run_id"]): _positive_number(row.get("observed_ms", row.get("e2e_wall_ms")), "observed_ms")
        for row in trajectories
    }
    for run_id, observed in e2e.items():
        pred_tool = 0.0
        pred_model = 0.0
        prior_sha: dict[str, list[float]] = defaultdict(list)
        prior_name: dict[tuple[str, str], list[float]] = defaultdict(list)
        last_out: list[float] = []
        for row in sorted(tools_by[run_id], key=lambda item: int(item.get("ordinal", 0))):
            summary = PriorEventSummary(
                prior_event_count=sum(len(item) for item in prior_sha.values()) + len(last_out),
                prior_median_output_tokens=_median(last_out) if last_out else 0.0,
                prior_median_observed_ms=0.0,
                prior_label_sha256s=(),
                prior_tool_sha_medians=tuple(
                    (key, _median(values)) for key, values in prior_sha.items()
                ),
                prior_tool_name_medians=tuple(
                    (cls, name, _median(values)) for (cls, name), values in prior_name.items()
                ),
                last_output_tokens=tuple(last_out),
            )
            predicted = draft.predict_tool_ms(row, summary)
            pred_tool += predicted
            wall = _positive_number(row.get("observed_ms", row.get("wall_ms")), "observed_ms")
            prior_sha[str(row["command_sha256"])].append(wall)
            prior_name[(str(row["operation_class"]), str(row["tool_name"]))].append(wall)
        for row in sorted(models_by[run_id], key=lambda item: int(item.get("ordinal", 0))):
            summary = PriorEventSummary(
                prior_event_count=len(last_out),
                prior_median_output_tokens=_median(last_out) if last_out else 0.0,
                prior_median_observed_ms=0.0,
                prior_label_sha256s=(),
                prior_tool_sha_medians=(),
                prior_tool_name_medians=(),
                last_output_tokens=tuple(last_out),
            )
            pred_model += draft.predict_model_ms(row, summary)
            last_out.append(float(row["output_tokens"]))
        event_sum = pred_tool + pred_model
        if event_sum > 0:
            scales.append(observed / event_sum)
    scale = _median(scales) if scales else 1.0
    return SequentialLatencyModel(
        extractor_id=draft.extractor_id,
        extractor_sha256=draft.extractor_sha256,
        sha_medians=draft.sha_medians,
        key_medians=draft.key_medians,
        prefix_medians=draft.prefix_medians,
        tool_name_medians=draft.tool_name_medians,
        class_medians=draft.class_medians,
        global_tool_median=draft.global_tool_median,
        model_intercept=draft.model_intercept,
        model_output_coef=draft.model_output_coef,
        model_context_coef=draft.model_context_coef,
        context_output_medians=draft.context_output_medians,
        global_output_median=draft.global_output_median,
        e2e_scale=scale,
        calibration_run_ids=draft.calibration_run_ids,
    )


def sequential_feature_sha256(features: Mapping[str, Any]) -> str:
    return canonical_sha256(dict(features))
