"""Lossless, stdlib-only parsing and comparison of vLLM Prometheus text.

The vLLM ``/metrics`` endpoint reports server-level aggregates.  This module
deliberately keeps labels, metric types, and timestamps so a caller cannot
mistake a cumulative or histogram series for a per-request measurement.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterable, Mapping, NoReturn, Sequence
import math
import re


SERVER_AGGREGATE = "server_aggregate"
CUMULATIVE = "cumulative"
INSTANTANEOUS = "instantaneous"
HISTOGRAM = "histogram"
UNKNOWN = "unknown"


class PrometheusParseError(ValueError):
    """The Prometheus text is not valid for the supported exposition format."""


class MissingMetricFamiliesError(ValueError):
    """One or more required vLLM metric families were absent."""

    def __init__(self, missing: Iterable[str]):
        self.missing = tuple(sorted(set(missing)))
        super().__init__("missing required metric families: " + ", ".join(self.missing))


class PerRequestInterpretationError(ValueError):
    """Raised when aggregate vLLM metrics are treated as request-level data."""


@dataclass(frozen=True)
class MetricFamily:
    """The stable semantic classification of one native vLLM family."""

    name: str
    metric_type: str
    scope: str = SERVER_AGGREGATE
    aggregation: str = CUMULATIVE
    per_request: bool = False


def _family(name: str, metric_type: str, aggregation: str = CUMULATIVE) -> MetricFamily:
    return MetricFamily(name=name, metric_type=metric_type, aggregation=aggregation)


# Names are the names emitted on the wire by the pinned vLLM 0.10.0 sources.
# Counter constructors may omit ``_total``; prometheus_client adds it when
# exposing the sample.  Both spellings are retained as exact aliases.
NATIVE_METRIC_FAMILIES: Mapping[str, MetricFamily] = MappingProxyType({
    # Server state and cache gauges.
    "vllm:num_requests_running": _family("vllm:num_requests_running", "gauge", INSTANTANEOUS),
    "vllm:num_requests_waiting": _family("vllm:num_requests_waiting", "gauge", INSTANTANEOUS),
    "vllm:num_requests_swapped": _family("vllm:num_requests_swapped", "gauge", INSTANTANEOUS),
    "vllm:gpu_cache_usage_perc": _family("vllm:gpu_cache_usage_perc", "gauge", INSTANTANEOUS),
    "vllm:cpu_cache_usage_perc": _family("vllm:cpu_cache_usage_perc", "gauge", INSTANTANEOUS),
    "vllm:kv_cache_usage_perc": _family("vllm:kv_cache_usage_perc", "gauge", INSTANTANEOUS),
    "vllm:lora_requests_info": _family("vllm:lora_requests_info", "gauge", INSTANTANEOUS),
    "vllm:cache_config_info": _family("vllm:cache_config_info", "gauge", INSTANTANEOUS),
    # Counters.  The no-suffix aliases are v1 constructor names; samples use
    # the Prometheus ``_total`` spelling for counters.
    "vllm:num_preemptions": _family("vllm:num_preemptions", "counter"),
    "vllm:num_preemptions_total": _family("vllm:num_preemptions_total", "counter"),
    "vllm:prompt_tokens": _family("vllm:prompt_tokens", "counter"),
    "vllm:prompt_tokens_total": _family("vllm:prompt_tokens_total", "counter"),
    "vllm:generation_tokens": _family("vllm:generation_tokens", "counter"),
    "vllm:generation_tokens_total": _family("vllm:generation_tokens_total", "counter"),
    "vllm:request_success": _family("vllm:request_success", "counter"),
    "vllm:request_success_total": _family("vllm:request_success_total", "counter"),
    "vllm:gpu_prefix_cache_queries": _family("vllm:gpu_prefix_cache_queries", "counter"),
    "vllm:gpu_prefix_cache_hits": _family("vllm:gpu_prefix_cache_hits", "counter"),
    "vllm:prefix_cache_queries": _family("vllm:prefix_cache_queries", "counter"),
    "vllm:prefix_cache_hits": _family("vllm:prefix_cache_hits", "counter"),
    "vllm:tokens": _family("vllm:tokens", "counter"),
    "vllm:tokens_total": _family("vllm:tokens_total", "counter"),
    # Iteration and request histograms.  A scrape contains _bucket, _count,
    # and _sum samples for each label series.
    "vllm:iteration_tokens_total": _family("vllm:iteration_tokens_total", "histogram", HISTOGRAM),
    "vllm:time_to_first_token_seconds": _family("vllm:time_to_first_token_seconds", "histogram", HISTOGRAM),
    "vllm:time_per_output_token_seconds": _family("vllm:time_per_output_token_seconds", "histogram", HISTOGRAM),
    "vllm:e2e_request_latency_seconds": _family("vllm:e2e_request_latency_seconds", "histogram", HISTOGRAM),
    "vllm:request_queue_time_seconds": _family("vllm:request_queue_time_seconds", "histogram", HISTOGRAM),
    "vllm:request_inference_time_seconds": _family("vllm:request_inference_time_seconds", "histogram", HISTOGRAM),
    "vllm:request_prefill_time_seconds": _family("vllm:request_prefill_time_seconds", "histogram", HISTOGRAM),
    "vllm:request_decode_time_seconds": _family("vllm:request_decode_time_seconds", "histogram", HISTOGRAM),
    "vllm:request_prompt_tokens": _family("vllm:request_prompt_tokens", "histogram", HISTOGRAM),
    "vllm:request_generation_tokens": _family("vllm:request_generation_tokens", "histogram", HISTOGRAM),
    "vllm:request_max_num_generation_tokens": _family("vllm:request_max_num_generation_tokens", "histogram", HISTOGRAM),
    "vllm:request_params_n": _family("vllm:request_params_n", "histogram", HISTOGRAM),
    "vllm:request_params_max_tokens": _family("vllm:request_params_max_tokens", "histogram", HISTOGRAM),
    "vllm:time_in_queue_requests": _family("vllm:time_in_queue_requests", "histogram", HISTOGRAM),
    "vllm:model_forward_time_milliseconds": _family("vllm:model_forward_time_milliseconds", "histogram", HISTOGRAM),
    "vllm:model_execute_time_milliseconds": _family("vllm:model_execute_time_milliseconds", "histogram", HISTOGRAM),
})


REQUIRED_VLLM_FAMILIES = frozenset({
    "vllm:request_success_total",
    "vllm:prompt_tokens_total",
    "vllm:generation_tokens_total",
    "vllm:e2e_request_latency_seconds",
})


_SAMPLE_RE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)"
    r"(?:\{(?P<labels>.*)\})?"
    r"\s+(?P<value>[+-]?(?:NaN|Inf|(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?))"
    r"(?:\s+(?P<timestamp>[+-]?\d+))?$"
)
_LABEL_NAME_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")
_HISTOGRAM_SUFFIXES = ("_bucket", "_count", "_sum")


def _parse_labels(text: str, line_number: int) -> Mapping[str, str]:
    if not text:
        return MappingProxyType({})
    labels: dict[str, str] = {}
    index = 0
    length = len(text)
    while index < length:
        while index < length and text[index].isspace():
            index += 1
        match = _LABEL_NAME_RE.match(text, index)
        if match is None:
            raise PrometheusParseError(f"line {line_number}: invalid label name")
        name = match.group(0)
        index = match.end()
        while index < length and text[index].isspace():
            index += 1
        if index >= length or text[index] != "=":
            raise PrometheusParseError(f"line {line_number}: expected '=' after label {name}")
        index += 1
        while index < length and text[index].isspace():
            index += 1
        if index >= length or text[index] != '"':
            raise PrometheusParseError(f"line {line_number}: label {name} must be quoted")
        index += 1
        value: list[str] = []
        while index < length:
            char = text[index]
            index += 1
            if char == '"':
                break
            if char == "\\":
                if index >= length:
                    raise PrometheusParseError(f"line {line_number}: unterminated label escape")
                escaped = text[index]
                index += 1
                value.append({"n": "\n", '"': '"', "\\": "\\"}.get(escaped, escaped))
            else:
                value.append(char)
        else:
            raise PrometheusParseError(f"line {line_number}: unterminated label value")
        if name in labels:
            raise PrometheusParseError(f"line {line_number}: duplicate label {name}")
        labels[name] = "".join(value)
        while index < length and text[index].isspace():
            index += 1
        if index >= length:
            break
        if text[index] != ",":
            raise PrometheusParseError(f"line {line_number}: expected ',' between labels")
        index += 1
    return MappingProxyType(labels)


def _family_name(name: str) -> str:
    for suffix in _HISTOGRAM_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    if name.endswith("_created"):
        return name[:-len("_created")]
    return name


def _type_name(name: str) -> str:
    family = _family_name(name)
    if family.endswith("_total"):
        alias = family[:-len("_total")]
        if alias in NATIVE_METRIC_FAMILIES:
            return alias
    return family


@dataclass(frozen=True)
class PrometheusSample:
    """One lossless Prometheus sample, including its label set and timestamp."""

    name: str
    labels: Mapping[str, str]
    value: float
    metric_type: str | None = None
    timestamp_ms: int | None = None

    @property
    def family_name(self) -> str:
        return _family_name(self.name)

    @property
    def family(self) -> MetricFamily | None:
        return classify_metric_family(self.name)

    @property
    def series_key(self) -> tuple[str, tuple[tuple[str, str], ...]]:
        return self.name, tuple(sorted(self.labels.items()))


@dataclass(frozen=True)
class PrometheusSnapshot(Sequence[PrometheusSample]):
    """A deterministic ordered scrape and the TYPE metadata seen in it."""

    samples: tuple[PrometheusSample, ...]
    types: Mapping[str, str]

    def __iter__(self):
        return iter(self.samples)

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, index: int) -> PrometheusSample:
        return self.samples[index]

    def for_family(self, family_name: str) -> tuple[PrometheusSample, ...]:
        canonical = _family_name(family_name)
        return tuple(sample for sample in self.samples if sample.family_name == canonical)

    def series(self) -> Mapping[tuple[str, tuple[tuple[str, str], ...]], PrometheusSample]:
        return MappingProxyType({sample.series_key: sample for sample in self.samples})


def parse_prometheus_text(text: str | bytes, *, strict: bool = True) -> PrometheusSnapshot:
    """Parse Prometheus text exposition without dropping labels or timestamps."""

    if isinstance(text, bytes):
        text = text.decode("utf-8")
    if not isinstance(text, str):
        raise TypeError("Prometheus text must be str or bytes")
    type_names: dict[str, str] = {}
    pending: list[tuple[int, str, Mapping[str, str], float, int | None]] = []
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            parts = line.split(None, 3)
            if len(parts) >= 3 and parts[1] == "TYPE":
                metric_name, metric_type = parts[2], parts[3].lower() if len(parts) == 4 else ""
                if metric_type not in {"counter", "gauge", "histogram", "summary", "info", "stateset", "gaugehistogram", "unknown"}:
                    if strict:
                        raise PrometheusParseError(f"line {line_number}: unsupported TYPE {metric_type!r}")
                    continue
                type_names[metric_name] = metric_type
            continue
        match = _SAMPLE_RE.match(line)
        if match is None:
            if strict:
                raise PrometheusParseError(f"line {line_number}: invalid sample")
            continue
        try:
            value = float(match.group("value"))
            timestamp = match.group("timestamp")
            timestamp_ms = int(timestamp) if timestamp is not None else None
            labels = _parse_labels(match.group("labels") or "", line_number)
        except (OverflowError, ValueError) as exc:
            raise PrometheusParseError(f"line {line_number}: invalid sample value") from exc
        pending.append((line_number, match.group("name"), labels, value, timestamp_ms))

    samples: list[PrometheusSample] = []
    seen: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    for line_number, name, labels, value, timestamp_ms in pending:
        key = (name, tuple(sorted(labels.items())))
        if key in seen:
            raise PrometheusParseError(f"line {line_number}: duplicate sample series {name}")
        seen.add(key)
        sample_type = type_names.get(name) or type_names.get(_type_name(name))
        if sample_type is None and name.endswith(_HISTOGRAM_SUFFIXES):
            sample_type = "histogram"
        samples.append(PrometheusSample(name, labels, value, sample_type, timestamp_ms))
    return PrometheusSnapshot(tuple(samples), MappingProxyType(dict(type_names)))


def classify_metric_family(name: str) -> MetricFamily | None:
    """Return the pinned native classification, or ``None`` for other metrics."""

    canonical = _family_name(name)
    family = NATIVE_METRIC_FAMILIES.get(canonical)
    if family is not None:
        return family
    if canonical.endswith("_total"):
        return NATIVE_METRIC_FAMILIES.get(canonical[:-len("_total")])
    return None


def required_families(snapshot: PrometheusSnapshot, required: Iterable[str] = REQUIRED_VLLM_FAMILIES) -> tuple[str, ...]:
    """Return required native families absent from a snapshot."""

    present = {sample.family_name for sample in snapshot if sample.family is not None}
    return tuple(sorted(family for family in set(required) if family not in present))


def validate_required_families(snapshot: PrometheusSnapshot, required: Iterable[str] = REQUIRED_VLLM_FAMILIES) -> None:
    """Fail closed when a scrape lacks a required native vLLM family."""

    missing = required_families(snapshot, required)
    if missing:
        raise MissingMetricFamiliesError(missing)


def _number(value: float | int | PrometheusSample | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, PrometheusSample):
        return value.value
    if isinstance(value, (int, float)):
        return float(value)
    raise TypeError("delta values must be numbers, PrometheusSample, or None")


@dataclass(frozen=True)
class DeltaResult:
    """A reset-safe delta; ``value`` is absent when measurement is unsafe."""

    status: str
    value: Any = None
    reason: str | None = None

    @property
    def measured(self) -> bool:
        return self.status == "measured"


def counter_delta(before: float | int | PrometheusSample | None, after: float | int | PrometheusSample | None) -> DeltaResult:
    """Compute a non-negative counter delta, marking missing/reset data unsafe."""

    old, new = _number(before), _number(after)
    if old is None or new is None or not math.isfinite(old) or not math.isfinite(new):
        return DeltaResult("unavailable", reason="counter endpoint missing or non-finite")
    if new < old:
        return DeltaResult("reset", reason="counter decreased between scrapes")
    return DeltaResult("measured", new - old)


@dataclass(frozen=True)
class HistogramDeltaResult:
    """Reset-safe deltas for histogram bucket/count/sum series."""

    status: str
    deltas: Mapping[Any, float] | None = None
    reason: str | None = None

    @property
    def measured(self) -> bool:
        return self.status == "measured"


def _histogram_values(value: Mapping[Any, float] | PrometheusSnapshot | Iterable[PrometheusSample] | None) -> dict[Any, float] | None:
    if value is None:
        return None
    if isinstance(value, PrometheusSnapshot):
        samples = value.samples
    elif isinstance(value, Mapping):
        result: dict[Any, float] = {}
        for key, item in value.items():
            if not isinstance(item, (int, float)) or not math.isfinite(float(item)):
                return None
            result[key] = float(item)
        return result
    else:
        samples = tuple(value)
    result = {}
    for sample in samples:
        if not isinstance(sample, PrometheusSample):
            raise TypeError("histogram series must contain PrometheusSample values")
        family = sample.family
        if family is None or family.aggregation != HISTOGRAM:
            continue
        if not math.isfinite(sample.value):
            return None
        result[sample.series_key] = sample.value
    return result


def histogram_delta(
    before: Mapping[Any, float] | PrometheusSnapshot | Iterable[PrometheusSample] | None,
    after: Mapping[Any, float] | PrometheusSnapshot | Iterable[PrometheusSample] | None,
) -> HistogramDeltaResult:
    """Compute histogram series deltas, rejecting missing series or resets."""

    old, new = _histogram_values(before), _histogram_values(after)
    if old is None or new is None:
        return HistogramDeltaResult("unavailable", reason="histogram endpoint missing or non-finite")
    if set(old) != set(new):
        return HistogramDeltaResult("unavailable", reason="histogram series changed between scrapes")
    deltas = {key: new[key] - old[key] for key in old}
    if any(value < 0 for value in deltas.values()):
        return HistogramDeltaResult("reset", reason="histogram series decreased between scrapes")
    return HistogramDeltaResult("measured", MappingProxyType(deltas))


def reject_per_request_interpretation(metric_name: str, request_id: str | None = None) -> NoReturn:
    """Explicitly reject inventing a request-level value from native metrics."""

    suffix = f" for request {request_id!r}" if request_id is not None else ""
    raise PerRequestInterpretationError(
        f"{metric_name}{suffix} is a server aggregate; native vLLM Prometheus "
        "samples carry no SWE-agent request identity"
    )
