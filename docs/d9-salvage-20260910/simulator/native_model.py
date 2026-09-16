"""Adapter for the corrected native per-request E2E fit artifact.

The native comparison script owns the feature construction and prediction
routine.  This module only validates the serialized full-training artifact,
selects the explicitly requested E2E candidate, and delegates the numerical
prediction to that routine.  The cache candidate is deliberately opt-in: a
caller must provide ``cache_trace=True`` at the simulator boundary before
``cached_tokens`` is accepted.
"""

from __future__ import annotations

import importlib.util
import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping


SCHEMA = "d9.native-fit-artifact.v1"
TOKEN_CANDIDATE = "relative_nnls_token"
CACHE_CANDIDATE = "relative_nnls_token_cache"
E2E_PHASE = "e2e"
EXPECTED_TOKEN_FEATURES = [
    "intercept",
    "prompt_tokens_div_1000",
    "completion_tokens_div_1000",
]
EXPECTED_CACHE_FEATURES = [
    "intercept",
    "uncached_prompt_tokens_div_1000",
    "completion_tokens_div_1000",
    "prompt_completion_tokens_div_1e6",
]


class NativeArtifactError(ValueError):
    """The native fit artifact or request violates its contract."""


def _finite_nonnegative(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NativeArtifactError(f"{field} must be a finite number >= 0")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise NativeArtifactError(f"{field} must be a finite number >= 0")
    return result


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NativeArtifactError(f"cannot read native fit artifact {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise NativeArtifactError("native fit artifact must be a JSON object")
    return value


def _validate_model(model: Mapping[str, Any], candidate: str) -> None:
    if model.get("candidate") != candidate:
        raise NativeArtifactError(f"native E2E model candidate does not match {candidate}")
    if model.get("phase") != E2E_PHASE:
        raise NativeArtifactError("native simulator only supports the native:e2e phase")
    if model.get("status") != "fitted":
        raise NativeArtifactError("native E2E model is not a fitted full-training model")
    if model.get("kind") != "relative_nonnegative_nnls":
        raise NativeArtifactError("native E2E model is not the corrected scaled NNLS model")
    expected = EXPECTED_CACHE_FEATURES if candidate == CACHE_CANDIDATE else EXPECTED_TOKEN_FEATURES
    if model.get("feature_names") != expected:
        raise NativeArtifactError("native E2E model feature names do not match its contract")
    coefficients = model.get("coefficients")
    if not isinstance(coefficients, list) or len(coefficients) != len(expected):
        raise NativeArtifactError("native E2E model coefficient width is invalid")
    for index, coefficient in enumerate(coefficients):
        _finite_nonnegative(coefficient, f"coefficients[{index}]")


def load_artifact(path: Path) -> dict[str, Any]:
    """Load and validate the native artifact used by conditional replay."""

    payload = _read_json(path)
    if payload.get("schema") != SCHEMA:
        raise NativeArtifactError(
            f"unsupported native fit artifact schema: {payload.get('schema')!r}"
        )
    if payload.get("primary_target") != "native:e2e":
        raise NativeArtifactError("native fit artifact primary target must be native:e2e")
    if payload.get("status") != "full_train_fit_for_conditional_replay_only":
        raise NativeArtifactError("native fit artifact is not marked conditional replay only")
    domain = payload.get("verified_hardware_domain")
    if not isinstance(domain, str) or not domain:
        raise NativeArtifactError("native fit artifact lacks a verified hardware domain")

    feature_contract = payload.get("feature_contract")
    if not isinstance(feature_contract, Mapping):
        raise NativeArtifactError("native fit artifact lacks feature_contract")
    if feature_contract.get("allowed_workload_descriptors") != [
        "prompt_tokens",
        "completion_tokens",
    ]:
        raise NativeArtifactError("native feature contract has unexpected workload descriptors")
    if feature_contract.get("optional_conditional_cache_descriptor") != "cached_tokens":
        raise NativeArtifactError("native feature contract lacks cached_tokens descriptor")

    interface = payload.get("prediction_interface")
    if not isinstance(interface, Mapping):
        raise NativeArtifactError("native fit artifact lacks prediction_interface")
    if interface.get("required_conditional_inputs") != [
        "prompt_tokens",
        "completion_tokens",
    ]:
        raise NativeArtifactError("native prediction interface has unexpected required inputs")
    if interface.get("optional_conditional_cache_input") != "cached_tokens":
        raise NativeArtifactError("native prediction interface lacks cached_tokens input")
    if interface.get("cross_hardware_status") != "unvalidated":
        raise NativeArtifactError("native prediction interface does not preserve transfer status")

    models = payload.get("full_training_models")
    if not isinstance(models, Mapping):
        raise NativeArtifactError("native fit artifact lacks full_training_models")
    e2e_models = models.get(E2E_PHASE)
    if not isinstance(e2e_models, Mapping):
        raise NativeArtifactError("native fit artifact lacks full-training e2e models")
    for candidate in (TOKEN_CANDIDATE, CACHE_CANDIDATE):
        model = e2e_models.get(candidate)
        if not isinstance(model, Mapping):
            raise NativeArtifactError(f"native fit artifact lacks e2e candidate {candidate}")
        _validate_model(model, candidate)
    return payload


@lru_cache(maxsize=1)
def _native_predictor(path: str) -> Any:
    """Load the native comparison module once and reuse its predictor."""

    module_path = Path(path)
    spec = importlib.util.spec_from_file_location("d9_native_comparison_predictor", module_path)
    if spec is None or spec.loader is None:
        raise NativeArtifactError(f"cannot load native prediction routine: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def predict(
    payload: Mapping[str, Any],
    inputs: Mapping[str, Any],
    *,
    cache_trace: bool = False,
    predictor_path: Path,
) -> tuple[float, dict[str, Any]]:
    """Predict direct ``native:e2e`` using the artifact's selected contract.

    ``inputs`` contains only conditional workload descriptors.  The caller is
    responsible for rejecting labels and for requiring the explicit cache
    opt-in before calling this function.
    """

    prompt = _finite_nonnegative(inputs.get("prompt_tokens"), "prompt_tokens")
    completion = _finite_nonnegative(inputs.get("completion_tokens"), "completion_tokens")
    candidate = CACHE_CANDIDATE if cache_trace else TOKEN_CANDIDATE
    row: dict[str, Any] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
    }
    if cache_trace:
        cached = _finite_nonnegative(inputs.get("cached_tokens"), "cached_tokens")
        if cached > prompt:
            raise NativeArtifactError("cached_tokens cannot exceed prompt_tokens")
        row["cached_tokens"] = cached

    models = payload.get("full_training_models")
    if not isinstance(models, Mapping) or not isinstance(models.get(E2E_PHASE), Mapping):
        raise NativeArtifactError("native fit artifact lacks e2e models")
    model = models[E2E_PHASE].get(candidate)
    if not isinstance(model, Mapping):
        raise NativeArtifactError(f"native fit artifact lacks e2e candidate {candidate}")
    _validate_model(model, candidate)

    module = _native_predictor(str(predictor_path.resolve()))
    try:
        predicted, source = module._predict_model(model, row, E2E_PHASE)
        features = module.design(row, candidate, E2E_PHASE)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise NativeArtifactError(f"native E2E feature construction failed: {exc}") from exc
    if predicted is None or not isinstance(predicted, (int, float)):
        raise NativeArtifactError("native E2E predictor returned no prediction")
    predicted_value = float(predicted)
    if not math.isfinite(predicted_value) or predicted_value <= 0:
        raise NativeArtifactError("native E2E prediction must be positive and finite")
    if source != "relative_nnls":
        raise NativeArtifactError(f"native E2E predictor used unexpected source: {source}")
    return predicted_value, {
        "candidate": candidate,
        "kind": model["kind"],
        "phase": E2E_PHASE,
        "target": "native:e2e",
        "features": list(model["feature_names"]),
        "design": features,
        "prediction_source": source,
        "hardware_domain": payload["verified_hardware_domain"],
        "cache_trace_conditioned": cache_trace,
        "prospective_status": payload["prediction_interface"]["prospective_status"],
        "cross_hardware_status": payload["prediction_interface"]["cross_hardware_status"],
    }


__all__ = [
    "CACHE_CANDIDATE",
    "EXPECTED_CACHE_FEATURES",
    "EXPECTED_TOKEN_FEATURES",
    "NativeArtifactError",
    "SCHEMA",
    "TOKEN_CANDIDATE",
    "load_artifact",
    "predict",
]
