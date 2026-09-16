"""Loader for the bounded conditional GPU request proxy artifact.

The artifact is deliberately separate from the assignment v3 workload model.
It is a trace-conditioned historical request-proxy model whose realized output
token count is accepted only when the caller supplies the completed workload.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping


SCHEMA = "assignment.d9.conditional-gpu-fit-artifact.v1"
BASE_FEATURE_NAMES = (
    "log1p(input_tokens_plus_context_tokens)",
    "log1p(output_tokens)",
    "log1p(max_output_tokens)",
)
INTERACTION_FEATURE_NAME = (
    "log1p(input_tokens_plus_context_tokens)*log1p(output_tokens)"
)


class ConditionalGpuArtifactError(ValueError):
    """The conditional GPU artifact is malformed or violates its contract."""


def _finite_nonnegative(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConditionalGpuArtifactError(f"{field} must be a finite number >= 0")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ConditionalGpuArtifactError(f"{field} must be a finite number >= 0")
    return result


def _finite(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConditionalGpuArtifactError(f"{field} must be finite numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ConditionalGpuArtifactError(f"{field} must be finite numeric")
    return result


def load_artifact(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConditionalGpuArtifactError(f"cannot read conditional GPU artifact {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConditionalGpuArtifactError("conditional GPU artifact must be a JSON object")
    if payload.get("schema") != SCHEMA:
        raise ConditionalGpuArtifactError(
            f"unsupported conditional GPU artifact schema: {payload.get('schema')!r}"
        )
    selected = payload.get("selected_candidate")
    models = payload.get("models")
    if not isinstance(selected, str) or not selected:
        raise ConditionalGpuArtifactError("conditional GPU artifact lacks selected_candidate")
    if not isinstance(models, Mapping) or selected not in models:
        raise ConditionalGpuArtifactError("selected conditional GPU model is missing")
    model = models[selected]
    if not isinstance(model, Mapping):
        raise ConditionalGpuArtifactError("selected conditional GPU model must be an object")
    if model.get("candidate") != selected:
        raise ConditionalGpuArtifactError("selected model candidate does not match artifact selection")
    policy = payload.get("feature_policy")
    if not isinstance(policy, Mapping):
        raise ConditionalGpuArtifactError("conditional GPU artifact lacks feature_policy")
    if policy.get("mode") != "trace_conditioned_assignment_level_simulation":
        raise ConditionalGpuArtifactError("conditional GPU artifact is not trace-conditioned")
    semantics = policy.get("output_tokens_semantics")
    if not isinstance(semantics, str) or "not a pre-generation" not in semantics:
        raise ConditionalGpuArtifactError("conditional GPU output-token semantics are not bounded")
    return payload


def _features(inputs: Mapping[str, Any], feature_names: list[Any]) -> list[float]:
    expected = list(BASE_FEATURE_NAMES)
    if feature_names == expected:
        interaction = False
    elif feature_names == expected + [INTERACTION_FEATURE_NAME[0]]:
        interaction = True
    else:
        raise ConditionalGpuArtifactError("selected model has unsupported feature names")
    prompt = math.log1p(inputs["input_tokens"] + inputs["context_tokens"])
    output = math.log1p(inputs["output_tokens"])
    cap = math.log1p(inputs["max_output_tokens"])
    values = [prompt, output, cap]
    if interaction:
        values.append(prompt * output)
    if not all(math.isfinite(value) for value in values):
        raise ConditionalGpuArtifactError("conditional GPU features are not finite")
    return values


def predict(payload: Mapping[str, Any], inputs: Mapping[str, Any]) -> tuple[float, dict[str, Any]]:
    """Predict from a validated fit artifact and completed trace descriptors."""

    values = {
        field: _finite_nonnegative(inputs[field], field)
        for field in ("input_tokens", "context_tokens", "output_tokens", "max_output_tokens")
    }
    selected = payload["selected_candidate"]
    model = payload["models"][selected]
    kind = model.get("kind")
    if kind == "global_median":
        predicted = _finite(model.get("median_ms"), "median_ms")
        features: list[float] = []
    elif kind == "nonnegative_log_ridge":
        feature_names = model.get("feature_names")
        if not isinstance(feature_names, list):
            raise ConditionalGpuArtifactError("selected model feature_names must be a list")
        features = _features(values, feature_names)
        means = model.get("feature_means")
        scales = model.get("feature_scales")
        slopes = model.get("slopes_standardized")
        if not all(isinstance(item, list) for item in (means, scales, slopes)):
            raise ConditionalGpuArtifactError("selected model standardization fields are missing")
        if not (len(means) == len(scales) == len(slopes) == len(features)):
            raise ConditionalGpuArtifactError("selected model feature dimensions do not match")
        intercept = _finite(model.get("intercept_log_ms"), "intercept_log_ms")
        log_duration = intercept
        for index, (slope, value, mean, scale) in enumerate(zip(slopes, features, means, scales)):
            slope_value = _finite(slope, f"slopes_standardized[{index}]")
            if slope_value < -1e-9:
                raise ConditionalGpuArtifactError("selected model has a negative slope")
            scale_value = _finite(scale, f"feature_scales[{index}]")
            if scale_value <= 0:
                raise ConditionalGpuArtifactError("selected model feature scale must be positive")
            log_duration += max(0.0, slope_value) * (value - _finite(mean, f"feature_means[{index}")) / scale_value
        predicted = math.exp(min(50.0, log_duration))
    else:
        raise ConditionalGpuArtifactError(f"unsupported conditional GPU model kind: {kind!r}")
    if not math.isfinite(predicted) or predicted <= 0:
        raise ConditionalGpuArtifactError("conditional GPU prediction must be positive finite")
    return predicted, {
        "candidate": selected,
        "kind": kind,
        "features": model.get("feature_names", []),
        "artifact_schema": payload.get("schema"),
    }


__all__ = ["ConditionalGpuArtifactError", "SCHEMA", "load_artifact", "predict"]
