"""Dependency-free, assumption-explicit memory estimation.

The estimator is a preflight planning aid, not a GPU measurement.  It reports
``estimated`` provenance when all requested inputs are present and returns an
explicit ``unavailable`` report when a required model or KV-cache assumption
is missing.  Observed peak memory, when available to a caller, remains a
separate field and is never folded into the estimate.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Dict, List, Optional


class MemoryEstimateError(ValueError):
    """Invalid memory-estimator input."""


_PRECISION_BYTES = {
    "bf16": 2.0,
    "bfloat16": 2.0,
    "fp16": 2.0,
    "float16": 2.0,
    "half": 2.0,
    "fp32": 4.0,
    "float32": 4.0,
    "full": 4.0,
    "fp8": 1.0,
    "float8": 1.0,
    "int8": 1.0,
    "uint8": 1.0,
    "int4": 0.5,
    "uint4": 0.5,
}


def _first(mapping: Mapping[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if name in mapping and mapping[name] is not None:
            return mapping[name]
    return None


def _number(value: Any, name: str, *, integer: bool = False, positive: bool = True) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MemoryEstimateError(f"{name} must be numeric")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise MemoryEstimateError(f"{name} must be finite")
    if positive and numeric <= 0:
        raise MemoryEstimateError(f"{name} must be greater than zero")
    if integer and not numeric.is_integer():
        raise MemoryEstimateError(f"{name} must be an integer")
    return numeric


def _precision_bytes(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, str):
        normalized = value.strip().lower().replace("-", "").replace(" ", "")
        if normalized in _PRECISION_BYTES:
            return _PRECISION_BYTES[normalized]
        if normalized.endswith("bit"):
            try:
                return float(normalized[:-3]) / 8.0
            except ValueError:
                return None
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        numeric = float(value)
        if math.isfinite(numeric) and numeric > 0:
            # Numeric precision values are interpreted as bytes per element.
            return numeric
    return None


def _unavailable(
    *,
    assumptions: List[str],
    missing: List[str],
    model_revision: Optional[str],
    precision: Any,
    context_length: Any,
    batch_size: Any,
    include_kv_cache: bool,
) -> Dict[str, Any]:
    return {
        "schema_version": "observability.memory-estimate.v1",
        "status": "unavailable",
        "provenance": "unavailable",
        "method": "weights_plus_kv_cache_plus_runtime_margin",
        "estimator": "dependency_free_assumption_explicit",
        "actual_fit_authoritative": True,
        "model_revision": model_revision,
        "precision": precision,
        "context_length": context_length,
        "batch_size": batch_size,
        "include_kv_cache": include_kv_cache,
        "estimated_peak_bytes": None,
        "estimated_peak_gib": None,
        "components": {
            "weights_bytes": None,
            "kv_cache_bytes": None,
            "runtime_margin_bytes": None,
        },
        "assumptions": assumptions,
        "missing_inputs": missing,
    }


def estimate_memory(
    model_config: Optional[Mapping[str, Any]] = None,
    *,
    model_revision: Optional[str] = None,
    precision: Any = None,
    precision_bytes: Optional[float] = None,
    parameter_count: Optional[float] = None,
    weight_bytes: Optional[float] = None,
    context_length: Optional[float] = None,
    batch_size: float = 1,
    num_layers: Optional[float] = None,
    num_kv_heads: Optional[float] = None,
    head_dim: Optional[float] = None,
    kv_bytes_per_element: Optional[float] = None,
    runtime_margin_fraction: float = 0.20,
    runtime_margin_bytes: Optional[float] = None,
    include_kv_cache: bool = True,
) -> Dict[str, Any]:
    """Estimate peak bytes from explicit weight/KV/runtime assumptions.

    ``model_config`` accepts common Hugging Face names (for example
    ``num_hidden_layers`` and ``num_key_value_heads``).  Explicit keyword
    values take precedence.  Set ``include_kv_cache=False`` only when the
    caller deliberately wants a weight-only planning estimate.
    """

    if not isinstance(model_config, (Mapping, type(None))):
        raise TypeError("model_config must be a mapping")
    config = dict(model_config or {})
    revision = model_revision if model_revision is not None else _first(config, ("model_revision", "revision"))
    precision_value = precision if precision is not None else _first(config, ("precision", "dtype"))
    precision_per_element = precision_bytes
    if precision_per_element is None:
        configured_bytes = _first(config, ("precision_bytes", "bytes_per_parameter"))
        precision_per_element = configured_bytes if configured_bytes is not None else _precision_bytes(precision_value)

    params = parameter_count
    if params is None:
        params = _first(config, ("parameter_count", "num_parameters", "total_params", "n_params"))
    weights = weight_bytes
    if weights is None:
        weights = _first(config, ("weight_bytes", "weights_bytes"))
    context = context_length
    if context is None:
        context = _first(config, ("context_length", "max_model_len", "max_seq_len", "max_position_embeddings"))
    layers = num_layers
    if layers is None:
        layers = _first(config, ("num_layers", "num_hidden_layers", "n_layer"))
    kv_heads = num_kv_heads
    if kv_heads is None:
        kv_heads = _first(config, ("num_kv_heads", "num_key_value_heads", "n_head_kv"))
    dimension = head_dim
    if dimension is None:
        dimension = _first(config, ("head_dim", "attention_head_dim"))
    if dimension is None:
        hidden = _first(config, ("hidden_size", "n_embd"))
        attention_heads = _first(config, ("num_attention_heads", "n_head"))
        if hidden is not None and attention_heads is not None:
            hidden_number = _number(hidden, "hidden_size")
            head_number = _number(attention_heads, "num_attention_heads", integer=True)
            dimension = hidden_number / head_number

    assumptions: List[str] = [
        "weights are resident at the selected precision",
        "KV cache uses separate K and V tensors",
        "runtime margin covers allocator/workspace overhead and is not measured",
    ]
    missing: List[str] = []
    if weights is None and params is None:
        missing.append("weight_bytes_or_parameter_count")
    if precision_per_element is None and weights is None:
        missing.append("precision_or_precision_bytes")
    if include_kv_cache:
        for value, name in (
            (context, "context_length"),
            (layers, "num_layers"),
            (kv_heads, "num_kv_heads"),
            (dimension, "head_dim"),
        ):
            if value is None:
                missing.append(name)
        if precision_per_element is None and kv_bytes_per_element is None and _first(config, ("kv_bytes_per_element", "kv_cache_bytes_per_element")) is None:
            missing.append("precision_or_kv_bytes_per_element")
    if runtime_margin_bytes is not None and runtime_margin_fraction != 0.20:
        raise MemoryEstimateError("provide runtime_margin_bytes or runtime_margin_fraction, not both")
    if missing:
        return _unavailable(
            assumptions=assumptions,
            missing=missing,
            model_revision=revision,
            precision=precision_value,
            context_length=context,
            batch_size=batch_size,
            include_kv_cache=include_kv_cache,
        )

    batch = _number(batch_size, "batch_size", integer=True)
    if weights is None:
        params_number = _number(params, "parameter_count", integer=True)
        assert precision_per_element is not None
        weights_number = params_number * _number(precision_per_element, "precision_bytes", positive=True)
        assumptions.append("weight_bytes = parameter_count * precision_bytes")
    else:
        weights_number = _number(weights, "weight_bytes")
        assumptions.append("weight_bytes supplied directly")

    if include_kv_cache:
        context_number = _number(context, "context_length", integer=True)
        layers_number = _number(layers, "num_layers", integer=True)
        heads_number = _number(kv_heads, "num_kv_heads", integer=True)
        dimension_number = _number(dimension, "head_dim")
        kv_element_bytes = kv_bytes_per_element
        if kv_element_bytes is None:
            kv_element_bytes = _first(config, ("kv_bytes_per_element", "kv_cache_bytes_per_element"))
        if kv_element_bytes is None:
            kv_element_bytes = precision_per_element
            assumptions.append("KV cache element bytes equal the selected weight precision")
        kv_bytes = (
            2.0
            * layers_number
            * heads_number
            * dimension_number
            * context_number
            * batch
            * _number(kv_element_bytes, "kv_bytes_per_element")
        )
    else:
        kv_bytes = 0.0
        assumptions.append("KV cache excluded by caller")

    if runtime_margin_bytes is not None:
        margin = _number(runtime_margin_bytes, "runtime_margin_bytes", positive=False)
        assumptions.append("runtime margin supplied as an absolute byte value")
    else:
        margin_fraction = _number(runtime_margin_fraction, "runtime_margin_fraction", positive=False)
        margin = (weights_number + kv_bytes) * margin_fraction
        assumptions.append(f"runtime margin = {margin_fraction:g} * (weights + KV cache)")

    total = int(math.ceil(weights_number + kv_bytes + margin))
    return {
        "schema_version": "observability.memory-estimate.v1",
        "status": "available",
        "provenance": "estimated",
        "method": "weights_plus_kv_cache_plus_runtime_margin",
        "estimator": "dependency_free_assumption_explicit",
        "actual_fit_authoritative": True,
        "model_revision": revision,
        "precision": precision_value,
        "context_length": int(context) if context is not None and float(context).is_integer() else context,
        "batch_size": int(batch),
        "include_kv_cache": include_kv_cache,
        "estimated_peak_bytes": total,
        "estimated_peak_gib": total / (1024.0**3),
        "components": {
            "weights_bytes": int(math.ceil(weights_number)),
            "kv_cache_bytes": int(math.ceil(kv_bytes)),
            "runtime_margin_bytes": int(math.ceil(margin)),
        },
        "assumptions": assumptions,
        "missing_inputs": [],
    }


estimate_peak_memory = estimate_memory
memory_estimate = estimate_memory


def estimate_weight_bytes(
    *,
    parameter_count: float,
    precision: Any = None,
    precision_bytes: Optional[float] = None,
    model_revision: Optional[str] = None,
) -> Dict[str, Any]:
    """Return an explicit estimated model-weight footprint only.

    This helper intentionally does not estimate KV cache or claim that a model
    will fit on a device; vLLM runtime state and the actual H100 remain
    authoritative.
    """

    bytes_per_parameter = precision_bytes if precision_bytes is not None else _precision_bytes(precision)
    if bytes_per_parameter is None:
        return {
            "schema_version": "observability.memory-estimate.v1",
            "status": "unavailable",
            "provenance": "unavailable",
            "method": "parameter_count_times_precision_bytes",
            "model_revision": model_revision,
            "precision": precision,
            "estimated_weight_bytes": None,
            "estimated_weight_gib": None,
            "missing_inputs": ["precision_or_precision_bytes"],
            "actual_fit_authoritative": True,
        }
    parameters = _number(parameter_count, "parameter_count", integer=True)
    per_parameter = _number(bytes_per_parameter, "precision_bytes")
    total = int(math.ceil(parameters * per_parameter))
    return {
        "schema_version": "observability.memory-estimate.v1",
        "status": "available",
        "provenance": "estimated",
        "method": "parameter_count_times_precision_bytes",
        "model_revision": model_revision,
        "precision": precision,
        "estimated_weight_bytes": total,
        "estimated_weight_gib": total / (1024.0**3),
        "missing_inputs": [],
        "actual_fit_authoritative": True,
    }


__all__ = [
    "MemoryEstimateError",
    "estimate_memory",
    "estimate_peak_memory",
    "estimate_weight_bytes",
    "memory_estimate",
]
