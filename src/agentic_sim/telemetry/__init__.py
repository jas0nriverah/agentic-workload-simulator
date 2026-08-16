"""Thin, SWE-agent-native telemetry primitives.

The module records observations at existing request/action boundaries. It does
not alter prompts, tool payloads, sampling settings, or the agent control flow.
"""

from .jsonl_writer import AppendOnlyJSONLWriter
from .thin import ThinTelemetry, Telemetry, monotonic_ns, utc_now

__all__ = ["AppendOnlyJSONLWriter", "ThinTelemetry", "Telemetry", "monotonic_ns", "utc_now"]
