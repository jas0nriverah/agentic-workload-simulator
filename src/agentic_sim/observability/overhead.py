"""Paired control/profile overhead accounting.

This module intentionally compares wall-clock and agent-side measurements only.
It does not accept GPU-time fields: aggregate vLLM metrics and request wall time
are not device-time measurements.  Inputs are ordinary mappings so the helper
can consume a manifest or a small test fixture without adding dependencies.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections.abc import Mapping, Sequence
from typing import Any, Dict, Iterable, List, Optional, Tuple


class PairingError(ValueError):
    """A control/profile pair is not safe to compare."""


_CONFIG_KEYS = ("config_hash", "resolved_config_hash", "experiment_config_hash")
_IDENTITY_KEYS = (
    "model_revision",
    "dataset",
    "instance_id",
    "seed",
    "command_hash",
    "image_digest",
    "container_image_digest",
    "sweagent_revision",
    "vllm_revision",
)
_METRIC_CONTAINERS = ("metrics", "measurements", "values")
_GPU_WORDS = ("gpu", "cuda")
_METADATA_KEYS = {
    "schema_version",
    "run_id",
    "attempt_id",
    "profile_kind",
    "mode",
    "status",
    "provenance",
    "manifest",
    "config",
    "resolved_config",
    "metrics",
    "measurements",
    "values",
}


def _as_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "__dict__"):
        return vars(value)
    raise TypeError(f"{label} must be a mapping")


def _merged_run(value: Any, label: str) -> Dict[str, Any]:
    run = dict(_as_mapping(value, label))
    manifest = run.get("manifest")
    if isinstance(manifest, Mapping):
        merged = dict(manifest)
        merged.update(run)
        return merged
    return run


def _canonical_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _config_hash(run: Mapping[str, Any]) -> Optional[str]:
    for key in _CONFIG_KEYS:
        value = run.get(key)
        if value is not None:
            if not isinstance(value, str) or not value:
                raise PairingError(f"{key} must be a non-empty string")
            return value
    for key in ("config", "resolved_config"):
        value = run.get(key)
        if isinstance(value, Mapping):
            return _canonical_hash(value)
    return None


def _validate_pair(control: Mapping[str, Any], profile: Mapping[str, Any]) -> str:
    control_id = control.get("run_id", control.get("attempt_id"))
    profile_id = profile.get("run_id", profile.get("attempt_id"))
    if control_id is not None and profile_id is not None and control_id == profile_id:
        raise PairingError("control and profile attempts must have distinct run/attempt IDs")

    control_hash = _config_hash(control)
    profile_hash = _config_hash(profile)
    if not control_hash or not profile_hash:
        raise PairingError("both attempts require config_hash or a config mapping")
    if control_hash != profile_hash:
        raise PairingError("control/profile configuration hashes differ")

    for key in _IDENTITY_KEYS:
        left = control.get(key)
        right = profile.get(key)
        if left is not None and right is not None and left != right:
            raise PairingError(f"control/profile {key} differs")
    return control_hash


def _metric_mapping(run: Mapping[str, Any]) -> Mapping[str, Any]:
    for key in _METRIC_CONTAINERS:
        candidate = run.get(key)
        if isinstance(candidate, Mapping):
            return candidate
    return {
        key: value
        for key, value in run.items()
        if key not in _METADATA_KEYS and not key.endswith("_hash")
    }


def _samples(value: Any, metric: str) -> List[float]:
    if isinstance(value, Mapping):
        for key in ("values", "samples", "observations"):
            if key in value:
                value = value[key]
                break
        else:
            raise PairingError(f"metric {metric!r} must contain values/samples/observations")
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        raw_values = list(value)
    else:
        raw_values = [value]
    if not raw_values:
        raise PairingError(f"metric {metric!r} has no samples")
    result: List[float] = []
    for raw in raw_values:
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise PairingError(f"metric {metric!r} contains a non-numeric sample")
        numeric = float(raw)
        if not math.isfinite(numeric):
            raise PairingError(f"metric {metric!r} contains a non-finite sample")
        result.append(numeric)
    return result


def _reject_gpu_metric(name: str) -> None:
    normalized = name.lower()
    if any(word in normalized for word in _GPU_WORDS):
        raise PairingError(f"GPU/CUDA metrics are not valid overhead inputs: {name}")


def percentile(values: Iterable[float], percent: float) -> float:
    """Return an interpolated percentile using the deterministic ``n - 1`` rule."""

    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise ValueError("percentile requires at least one value")
    if not 0.0 <= percent <= 100.0:
        raise ValueError("percent must be between 0 and 100")
    position = (len(ordered) - 1) * percent / 100.0
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + fraction * (ordered[upper] - ordered[lower])


def _statistics(values: List[float]) -> Dict[str, Any]:
    return {
        "count": len(values),
        "mean": statistics.fmean(values),
        "median": percentile(values, 50.0),
        "p90": percentile(values, 90.0),
        "min": min(values),
        "max": max(values),
        "stdev": statistics.stdev(values) if len(values) > 1 else 0.0,
    }


def _relative_delta(control: float, profile: float) -> Optional[float]:
    if control == 0.0:
        return None
    return (profile - control) / abs(control)


def summarize_paired_overhead(
    control: Mapping[str, Any],
    profile: Mapping[str, Any],
    *,
    metric_names: Optional[Iterable[str]] = None,
    profile_kind: Optional[str] = None,
    observability: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Summarize a same-configuration control/profile pair.

    ``control`` and ``profile`` may contain a nested ``metrics`` mapping.  A
    metric can be one scalar or a sequence of per-instance observations.  The
    pair must have equal sample counts for every selected metric.  The return
    value contains medians, interpolated p90s, absolute deltas, and relative
    deltas; it deliberately contains no GPU-time result.
    """

    left = _merged_run(control, "control")
    right = _merged_run(profile, "profile")
    config_hash = _validate_pair(left, right)
    left_metrics = _metric_mapping(left)
    right_metrics = _metric_mapping(right)
    if metric_names is None:
        names = sorted(set(left_metrics).intersection(right_metrics))
    else:
        names = sorted(set(metric_names))
    if not names:
        raise PairingError("control/profile pair has no common metrics")

    summaries: Dict[str, Any] = {}
    for name in names:
        if not isinstance(name, str) or not name:
            raise PairingError("metric names must be non-empty strings")
        _reject_gpu_metric(name)
        if name not in left_metrics or name not in right_metrics:
            raise PairingError(f"metric {name!r} is missing from one attempt")
        control_values = _samples(left_metrics[name], name)
        profile_values = _samples(right_metrics[name], name)
        if len(control_values) != len(profile_values):
            raise PairingError(f"metric {name!r} has unpaired sample counts")
        control_stats = _statistics(control_values)
        profile_stats = _statistics(profile_values)
        median_delta = profile_stats["median"] - control_stats["median"]
        p90_delta = profile_stats["p90"] - control_stats["p90"]
        relative = {
            "mean": _relative_delta(control_stats["mean"], profile_stats["mean"]),
            "median": _relative_delta(control_stats["median"], profile_stats["median"]),
            "p90": _relative_delta(control_stats["p90"], profile_stats["p90"]),
        }
        summaries[name] = {
            "control": control_stats,
            "profile": profile_stats,
            "delta": {
                "mean": profile_stats["mean"] - control_stats["mean"],
                "median": median_delta,
                "p90": p90_delta,
            },
            "absolute_delta": {
                "mean": profile_stats["mean"] - control_stats["mean"],
                "median": median_delta,
                "p90": p90_delta,
            },
            "relative_delta": relative,
            "overhead_pct": {
                "mean": None if relative["mean"] is None else relative["mean"] * 100.0,
                "median": None if relative["median"] is None else relative["median"] * 100.0,
                "p90": None if relative["p90"] is None else relative["p90"] * 100.0,
            },
        }

    return {
        "schema_version": "observability.overhead.v1",
        "status": "available",
        "provenance": "derived",
        "control_run_id": left.get("run_id", left.get("attempt_id")),
        "profile_run_id": right.get("run_id", right.get("attempt_id")),
        "profile_kind": profile_kind or right.get("profile_kind", right.get("mode", "profile")),
        "config_hash": config_hash,
        "same_config": True,
        "gpu_time_claims": False,
        "observability": dict(observability or {}),
        "metrics": summaries,
    }


def _records_to_run(records: Sequence[Mapping[str, Any]], label: str) -> Dict[str, Any]:
    """Normalize repeated run records into the pair helper's input shape."""

    if not records:
        raise PairingError(f"{label} has no run records")
    rows = [_merged_run(record, f"{label}[{index}]") for index, record in enumerate(records)]
    first = rows[0]
    for index, row in enumerate(rows):
        for key in ("config_hash", "resolved_config_hash", "experiment_config_hash"):
            if key in first and key in row and row[key] != first[key]:
                raise PairingError(f"{label}[{index}] has a different {key}")
        for key in _IDENTITY_KEYS:
            if key in first and key in row and first[key] != row[key]:
                raise PairingError(f"{label}[{index}] has a different {key}")
        for key in ("observability_level", "profilers_enabled", "instrumentation_version"):
            if key not in row:
                raise PairingError(f"{label}[{index}] is missing {key}")
    metric_maps = [_metric_mapping(row) for row in rows]
    names = sorted(set.intersection(*(set(mapping) for mapping in metric_maps)))
    if not names:
        raise PairingError(f"{label} has no common metrics")
    combined: Dict[str, Any] = dict(first)
    combined["run_id"] = first.get("run_id", first.get("attempt_id"))
    combined["metrics"] = {
        name: [value for mapping in metric_maps for value in _samples(mapping[name], name)]
        for name in names
    }
    return combined


def summarize_overhead_records(
    control_records: Sequence[Mapping[str, Any]],
    profile_records: Sequence[Mapping[str, Any]],
    *,
    profile_kind: Optional[str] = None,
) -> Dict[str, Any]:
    """Summarize repeated, identically configured control/profile attempts.

    Each record must carry ``observability_level``, ``profilers_enabled`` and
    ``instrumentation_version``.  The result keeps these values so later
    analysis cannot silently mix instrumentation modes.
    """

    control = _records_to_run(control_records, "control")
    profile = _records_to_run(profile_records, "profile")
    if control_records[0].get("observability_level") != "control":
        raise PairingError("control records must use observability_level=control")
    if profile_records[0].get("observability_level") == "control":
        raise PairingError("profile records must use a non-control observability level")
    for rows, label in ((control_records, "control"), (profile_records, "profile")):
        levels = {row.get("observability_level") for row in rows}
        profilers = {json.dumps(row.get("profilers_enabled"), sort_keys=True, default=str) for row in rows}
        versions = {row.get("instrumentation_version") for row in rows}
        if len(levels) != 1 or len(profilers) != 1 or len(versions) != 1:
            raise PairingError(f"{label} records mix observability metadata")
    result = summarize_paired_overhead(
        control,
        profile,
        profile_kind=profile_kind,
        observability={
            "control": {
                "observability_level": control_records[0]["observability_level"],
                "profilers_enabled": list(control_records[0]["profilers_enabled"]),
                "instrumentation_version": control_records[0]["instrumentation_version"],
                "run_count": len(control_records),
            },
            "profile": {
                "observability_level": profile_records[0]["observability_level"],
                "profilers_enabled": list(profile_records[0]["profilers_enabled"]),
                "instrumentation_version": profile_records[0]["instrumentation_version"],
                "run_count": len(profile_records),
            },
        },
    )
    result["schema_version"] = "observability.profiling-overhead.v1"
    return result


# Short aliases make the bounded helper convenient without adding a package
# export or a second implementation.
summarize_overhead = summarize_paired_overhead
paired_overhead_summary = summarize_paired_overhead
compare_paired_runs = summarize_paired_overhead


__all__ = [
    "PairingError",
    "percentile",
    "summarize_paired_overhead",
    "summarize_overhead",
    "paired_overhead_summary",
    "compare_paired_runs",
    "summarize_overhead_records",
]
