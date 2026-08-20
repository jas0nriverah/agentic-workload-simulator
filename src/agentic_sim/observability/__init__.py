"""Bounded, provenance-first observability helpers for the assignment.

The package is dependency-free and additive to the frozen SWE-agent runner.
Optional GPU/profiler integrations are capability probes or separate attempts;
they are never enabled implicitly by Level-0 control or Level-1 thin runs.
"""

from .gpu import (
    collect_dcgmi_sample,
    collect_nvidia_smi_hardware,
    collect_nvidia_smi_sample,
    discover_dcgmi_capability,
    discover_dcgmi_fields,
)
from .profilers import (
    build_nsys_command,
    build_strace_command,
    detect_tool_capability,
    discover_tool_capabilities,
)
from .vllm_metrics import (
    REQUIRED_VLLM_FAMILIES,
    MissingMetricFamiliesError,
    PerRequestInterpretationError,
    PrometheusSample,
    PrometheusSnapshot,
    counter_delta,
    histogram_delta,
    parse_prometheus_text,
    reject_per_request_interpretation,
    validate_required_families,
)
from .memory import estimate_memory, estimate_peak_memory, estimate_weight_bytes
from .nvtx import annotate as nvtx_annotate, capability as nvtx_capability, range as nvtx_range
from .overhead import PairingError, summarize_overhead_records, summarize_paired_overhead
from .accounting import AccountingError, merge_intervals, summarize_interval_union
from .perfetto import TraceExportError, export_perfetto_trace

__all__ = [
    "REQUIRED_VLLM_FAMILIES",
    "MissingMetricFamiliesError",
    "PerRequestInterpretationError",
    "PrometheusSample",
    "PrometheusSnapshot",
    "counter_delta",
    "histogram_delta",
    "parse_prometheus_text",
    "reject_per_request_interpretation",
    "validate_required_families",
    "collect_nvidia_smi_hardware",
    "collect_nvidia_smi_sample",
    "discover_dcgmi_capability",
    "discover_dcgmi_fields",
    "collect_dcgmi_sample",
    "build_nsys_command",
    "build_strace_command",
    "detect_tool_capability",
    "discover_tool_capabilities",
    "estimate_memory",
    "estimate_peak_memory",
    "estimate_weight_bytes",
    "nvtx_annotate",
    "nvtx_capability",
    "nvtx_range",
    "PairingError",
    "summarize_overhead_records",
    "summarize_paired_overhead",
    "AccountingError",
    "merge_intervals",
    "summarize_interval_union",
    "TraceExportError",
    "export_perfetto_trace",
]
