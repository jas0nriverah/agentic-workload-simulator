"""Thin and v2 SWE-agent-native telemetry primitives.

The module records observations at existing request/action boundaries. It does
not alter prompts, tool payloads, sampling settings, or the agent control flow.
"""

from .jsonl_writer import AppendOnlyJSONLWriter
from .thin import ThinTelemetry, Telemetry, monotonic_ns, utc_now
from .features import (
    build_conditional_replay_features,
    build_model_features,
    build_tool_features,
    model_vector,
    serialize_model_vector,
    serialize_tool_vector,
    tool_model_vector,
)
from .hardware import HardwareDescriptorError, model_hardware_features, raw_hardware_inventory
from .script_state import ScriptState, ScriptStateLedger
from .work import (
    WORK_FIELDS,
    CommandProbeBinding,
    WorkMeasurement,
    measure_runtime_work,
    parse_strace_summary,
)
from .v2 import AssignmentTelemetry, TelemetryContractError, TelemetryV2, TelemetryV2Recorder
from .sweagent_hooks import (
    SWEAgentEnvironmentTelemetryHook,
    SWEAgentTelemetryHook,
    attach_sweagent_telemetry,
)

__all__ = [
    "AppendOnlyJSONLWriter",
    "AssignmentTelemetry",
    "CommandProbeBinding",
    "HardwareDescriptorError",
    "ScriptState",
    "ScriptStateLedger",
    "SWEAgentEnvironmentTelemetryHook",
    "SWEAgentTelemetryHook",
    "Telemetry",
    "TelemetryContractError",
    "TelemetryV2",
    "TelemetryV2Recorder",
    "ThinTelemetry",
    "WORK_FIELDS",
    "WorkMeasurement",
    "attach_sweagent_telemetry",
    "build_conditional_replay_features",
    "build_model_features",
    "build_tool_features",
    "model_hardware_features",
    "measure_runtime_work",
    "model_vector",
    "monotonic_ns",
    "raw_hardware_inventory",
    "parse_strace_summary",
    "serialize_model_vector",
    "serialize_tool_vector",
    "tool_model_vector",
    "utc_now",
]
