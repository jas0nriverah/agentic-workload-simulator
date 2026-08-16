"""Deterministic Chrome/Perfetto trace export from normalized JSONL events.

The exporter consumes monotonic timestamps only and copies no prompts,
commands, or arbitrary payloads into the trace.  JSONL remains the source of
truth; this file is a visualization derivative with explicit provenance.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


class TraceExportError(ValueError):
    """Input is not safe to convert into a deterministic trace."""


_EVENT_NAMES = {
    "run_start": "agent_run",
    "run_end": "agent_run_end",
    "step_start": "agent_step",
    "step_end": "agent_step_end",
    "model_request": "model_request",
    "tool_call": "tool_execution",
    "telemetry_sample": "telemetry_sample",
    "vllm_metrics": "vllm_metrics_interval",
    "gpu_sample": "gpu_sample",
}
_SAFE_ARG_KEYS = {"event_type", "step_id", "request_id", "action_id", "provenance", "scope", "measurement_class"}


def _read_jsonl(path: str | Path) -> tuple[list[dict[str, Any]], str]:
    source = Path(path).read_bytes()
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(source.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise TraceExportError(f"invalid JSONL at {path}:{line_number}") from exc
        if not isinstance(value, dict):
            raise TraceExportError(f"JSONL row at {path}:{line_number} is not an object")
        rows.append(value)
    return rows, hashlib.sha256(source).hexdigest()


def _number(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise TraceExportError(f"{name} must be a finite number")
    return int(value)


def _args(row: Mapping[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key in _SAFE_ARG_KEYS:
        value = row.get(key)
        if value is None and key == "event_type":
            continue
        if isinstance(value, (str, int, float, bool)):
            values[key] = value
    return values


def _event(row: Mapping[str, Any], *, base_ns: int, index: int) -> dict[str, Any] | None:
    event_type = row.get("event_type")
    name = _EVENT_NAMES.get(str(event_type))
    if name is None:
        return None
    start_value = row.get("start_mono_ns", row.get("timestamp_mono_ns", row.get("observed_at_mono_ns")))
    if start_value is None:
        return None
    start_ns = _number(start_value, "start_mono_ns")
    end_ns = _number(row.get("end_mono_ns", start_ns), "end_mono_ns")
    if end_ns < start_ns:
        raise TraceExportError("event end precedes start")
    args = _args(row)
    args["source_row"] = index
    event = {
        "name": name,
        "cat": "agentic",
        "ph": "X" if end_ns > start_ns else "i",
        "ts": (start_ns - base_ns) / 1000.0,
        "pid": str(row.get("run_id", "agentic-run")),
        "tid": str(row.get("step_id", row.get("attempt_id", "agentic"))),
        "args": args,
    }
    if end_ns > start_ns:
        event["dur"] = (end_ns - start_ns) / 1000.0
    return event


def export_perfetto_trace(
    events_path: str | Path,
    *,
    output_path: str | Path | None = None,
    gpu_samples_path: str | Path | None = None,
    run_id: str | None = None,
    attempt_id: str | None = None,
) -> dict[str, Any]:
    """Create a deterministic trace object and optionally write it atomically."""

    event_rows, event_hash = _read_jsonl(events_path)
    extra_rows: list[dict[str, Any]] = []
    extra_hash = None
    if gpu_samples_path:
        extra_rows, extra_hash = _read_jsonl(gpu_samples_path)
        for row in extra_rows:
            if "event_type" not in row:
                row["event_type"] = "gpu_sample"
    rows = event_rows + extra_rows
    times: list[int] = []
    for row in rows:
        value = row.get("start_mono_ns", row.get("timestamp_mono_ns", row.get("observed_at_mono_ns")))
        if value is not None:
            times.append(_number(value, "timestamp_mono_ns"))
    base_ns = min(times) if times else 0
    trace_events = [event for index, row in enumerate(rows) if (event := _event(row, base_ns=base_ns, index=index)) is not None]
    trace_events.sort(key=lambda value: (value["ts"], value.get("dur", 0), value["name"], value["tid"], value["args"].get("source_row", 0)))
    source_hash = hashlib.sha256((event_hash + (extra_hash or "")).encode("ascii")).hexdigest()
    trace = {
        "schema_version": "observability.trace.perfetto.v1",
        "provenance": "derived",
        "source_sha256": source_hash,
        "source_events_sha256": event_hash,
        "source_gpu_samples_sha256": extra_hash,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "clock": "monotonic_ns",
        "privacy": {"payloads_copied": False, "prompts_copied": False, "commands_copied": False},
        "traceEvents": trace_events,
    }
    if output_path is not None:
        path = Path(output_path)
        if path.exists():
            raise FileExistsError(f"refusing to overwrite trace: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(trace, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(path)
    return trace


export_trace = export_perfetto_trace

__all__ = ["TraceExportError", "export_perfetto_trace", "export_trace"]
