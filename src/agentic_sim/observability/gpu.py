"""Optional, dependency-free GPU capability and sampling helpers.

The helpers in this module deliberately use command-line interfaces instead of
importing vendor libraries.  A missing command, an unsupported query field, or
an unparseable value is represented as an explicit field-level ``unavailable``
record; it is never converted to zero or silently omitted.
"""

from __future__ import annotations

import csv
import datetime as _datetime
import re
import shutil
import subprocess
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from agentic_sim.telemetry.clock import clock_fields, monotonic_ns


_Runner = Callable[..., Any]
_SOURCE = "nvidia-smi"

_HARDWARE_FIELDS: Tuple[Tuple[str, str, str], ...] = (
    ("index", "index", "int"),
    ("name", "name", "text"),
    ("uuid", "uuid", "text"),
    ("memory_total_mib", "memory.total", "float"),
    ("compute_capability", "compute_cap", "float"),
    ("power_limit_w", "power.limit", "float"),
    ("pci_bus_id", "pci.bus_id", "text"),
    ("clocks_graphics_mhz", "clocks.current.graphics", "float"),
    ("clocks_sm_mhz", "clocks.current.sm", "float"),
    ("clocks_memory_mhz", "clocks.current.memory", "float"),
)
_SAMPLE_FIELDS: Tuple[Tuple[str, str, str], ...] = (
    ("index", "index", "int"),
    ("name", "name", "text"),
    ("uuid", "uuid", "text"),
    ("memory_used_mib", "memory.used", "float"),
    ("utilization_gpu_pct", "utilization.gpu", "float"),
    ("power_draw_w", "power.draw", "float"),
    ("clocks_graphics_mhz", "clocks.current.graphics", "float"),
    ("clocks_sm_mhz", "clocks.current.sm", "float"),
    ("clocks_memory_mhz", "clocks.current.memory", "float"),
)


def _utc_now() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _timestamps() -> Dict[str, Any]:
    return {"observed_at_utc": _utc_now(), "observed_monotonic_ns": monotonic_ns(), "clock": clock_fields()}


def _field(
    value: Any,
    *,
    source: str,
    timestamps: Mapping[str, Any],
    scope: str,
    error: Optional[str] = None,
    unit: Optional[str] = None,
) -> Dict[str, Any]:
    available = value is not None
    record: Dict[str, Any] = {
        "value": value,
        "status": "measured" if available else "unavailable",
        "provenance": "measured" if available else "unavailable",
        "source": source,
        "scope": scope,
        **dict(timestamps),
    }
    if unit is not None:
        record["unit"] = unit
    if error:
        record["error"] = error
    return record


def _unavailable_fields(
    definitions: Iterable[Tuple[str, str, str]],
    *,
    source: str,
    timestamps: Mapping[str, Any],
    scope: str,
    error: str,
) -> Dict[str, Dict[str, Any]]:
    return {
        name: _field(
            None,
            source=source,
            timestamps=timestamps,
            scope=scope,
            error=error,
            unit=_unit(name),
        )
        for name, _query_name, _kind in definitions
    }


def _unit(name: str) -> Optional[str]:
    if name.endswith("_mib"):
        return "MiB"
    if name.endswith("_pct"):
        return "%"
    if name.endswith("_w"):
        return "W"
    if name.endswith("_mhz"):
        return "MHz"
    return None


def _parse_value(raw: str, kind: str) -> Any:
    value = raw.strip()
    if not value or value.upper() in {"N/A", "NA", "NOT SUPPORTED", "[N/A]"}:
        return None
    if kind == "text":
        return value
    if kind == "int":
        return int(float(value))
    if kind == "float":
        return float(value)
    raise ValueError("unsupported nvidia-smi field kind: %s" % kind)


def _query(
    definitions: Sequence[Tuple[str, str, str]],
    *,
    runner: _Runner,
    executable: str,
    timeout_seconds: float,
) -> Tuple[Optional[str], Optional[str]]:
    query = ",".join(query_name for _name, query_name, _kind in definitions)
    argv = [executable, "--query-gpu=" + query, "--format=csv,noheader,nounits"]
    try:
        completed = runner(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
        return None, type(exc).__name__
    if getattr(completed, "returncode", 1) != 0:
        stderr = str(getattr(completed, "stderr", "") or "").strip()
        return None, "command_exit:%s%s" % (
            getattr(completed, "returncode", 1),
            (":" + stderr[:240]) if stderr else "",
        )
    return str(getattr(completed, "stdout", "") or ""), None


def _records(
    output: str,
    definitions: Sequence[Tuple[str, str, str]],
    *,
    source: str,
    timestamps: Mapping[str, Any],
    scope: str,
    error: Optional[str],
) -> List[Dict[str, Any]]:
    if error:
        return [{"fields": _unavailable_fields(definitions, source=source, timestamps=timestamps, scope=scope, error=error)}]
    rows = list(csv.reader(line for line in output.splitlines() if line.strip()))
    records: List[Dict[str, Any]] = []
    for row in rows:
        fields: Dict[str, Any] = {}
        for position, (name, _query_name, kind) in enumerate(definitions):
            raw = row[position] if position < len(row) else ""
            parse_error: Optional[str] = None
            try:
                value = _parse_value(raw, kind)
            except (TypeError, ValueError) as exc:
                value = None
                parse_error = type(exc).__name__
            fields[name] = _field(
                value,
                source=source,
                timestamps=timestamps,
                scope=scope,
                error=parse_error,
                unit=_unit(name),
            )
        records.append({"fields": fields})
    if not records:
        records.append(
            {"fields": _unavailable_fields(definitions, source=source, timestamps=timestamps, scope=scope, error="empty_output")}
        )
    return records


def _collect(
    definitions: Sequence[Tuple[str, str, str]],
    *,
    scope: str,
    runner: _Runner,
    executable: Optional[str],
    timeout_seconds: float,
    kind: str,
) -> Dict[str, Any]:
    if not scope or not isinstance(scope, str):
        raise ValueError("scope must be a non-empty string")
    timestamps = _timestamps()
    executable = executable or shutil.which(_SOURCE)
    error = None
    output = ""
    if not executable:
        error = "executable_not_found"
    else:
        output, error = _query(
            definitions,
            runner=runner,
            executable=executable,
            timeout_seconds=timeout_seconds,
        )
    devices = _records(
        output or "",
        definitions,
        source=_SOURCE,
        timestamps=timestamps,
        scope=scope,
        error=error,
    )
    command_ok = error is None and bool(output and output.strip())
    return {
        "schema_version": "observability.gpu.v1",
        "kind": kind,
        "source": _SOURCE,
        "scope": scope,
        **timestamps,
        "provenance": "measured" if command_ok else "unavailable",
        "status": "measured" if command_ok else "unavailable",
        "device_count": _field(
            len(devices) if command_ok else None,
            source=_SOURCE,
            timestamps=timestamps,
            scope=scope,
            error=error,
            unit="devices",
        ),
        "devices": devices,
        "error": error,
    }


def collect_nvidia_smi_hardware(
    *,
    scope: str = "host_capability",
    timeout_seconds: float = 5.0,
    executable: Optional[str] = None,
    runner: _Runner = subprocess.run,
) -> Dict[str, Any]:
    """Collect hardware identity/capability fields from ``nvidia-smi``."""

    return _collect(
        _HARDWARE_FIELDS,
        scope=scope,
        runner=runner,
        executable=executable,
        timeout_seconds=timeout_seconds,
        kind="hardware",
    )


def collect_nvidia_smi_sample(
    *,
    scope: str = "run_interval",
    timeout_seconds: float = 3.0,
    executable: Optional[str] = None,
    runner: _Runner = subprocess.run,
) -> Dict[str, Any]:
    """Collect one explicitly scoped utilization/power sample."""

    return _collect(
        _SAMPLE_FIELDS,
        scope=scope,
        runner=runner,
        executable=executable,
        timeout_seconds=timeout_seconds,
        kind="sample",
    )


def discover_dcgmi_capability(
    *,
    scope: str = "host_capability",
    timeout_seconds: float = 3.0,
    executable: Optional[str] = None,
    runner: _Runner = subprocess.run,
) -> Dict[str, Any]:
    """Discover ``dcgmi`` only when installed, without assuming counters.

    A successful ``--version`` proves executable/version availability only. It
    deliberately does not claim that DCGM profiling fields or a host engine
    are available; those require a separately authorized probe.
    """

    if not scope or not isinstance(scope, str):
        raise ValueError("scope must be a non-empty string")
    timestamps = _timestamps()
    path = executable or shutil.which("dcgmi")
    result: Dict[str, Any] = {
        "schema_version": "observability.tool-capability.v1",
        "tool": "dcgmi",
        "source": "PATH",
        "scope": scope,
        **timestamps,
        "provenance": "measured",
        "installed": bool(path),
        "executable": path,
        "version": _field(None, source="dcgmi", timestamps=timestamps, scope=scope),
        "profiling_fields": {
            "status": "unverified",
            "provenance": "unavailable",
            "source": "dcgmi",
            "scope": scope,
            **timestamps,
            "reason": "field availability requires an explicit DCGM probe",
        },
    }
    if not path:
        result["status"] = "unavailable"
        result["provenance"] = "unavailable"
        result["error"] = "executable_not_found"
        result["version"] = _field(
            None, source="dcgmi", timestamps=timestamps, scope=scope, error="executable_not_found"
        )
        return result
    try:
        completed = runner([path, "--version"], check=False, capture_output=True, text=True, timeout=timeout_seconds)
        output = (str(getattr(completed, "stdout", "") or "") + " " + str(getattr(completed, "stderr", "") or "")).strip()
        if getattr(completed, "returncode", 1) == 0 and output:
            result["status"] = "available"
            result["version"] = _field(output[:512], source="dcgmi", timestamps=timestamps, scope=scope)
        else:
            error = "command_exit:%s" % getattr(completed, "returncode", 1)
            result["status"] = "installed_unavailable"
            result["provenance"] = "unavailable"
            result["error"] = error
            result["version"] = _field(None, source="dcgmi", timestamps=timestamps, scope=scope, error=error)
    except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
        result["status"] = "installed_unavailable"
        result["provenance"] = "unavailable"
        result["error"] = type(exc).__name__
        result["version"] = _field(None, source="dcgmi", timestamps=timestamps, scope=scope, error=type(exc).__name__)
    return result


def discover_dcgmi_fields(
    *,
    scope: str = "host_capability",
    timeout_seconds: float = 5.0,
    executable: Optional[str] = None,
    runner: _Runner = subprocess.run,
) -> Dict[str, Any]:
    """Discover field IDs exposed by the installed DCGM CLI.

    The command output is retained as evidence and field IDs are only a
    conservative parse of numeric identifiers.  No candidate metric is
    claimed supported until the H100 host returns it from ``dmon``.
    """

    if not scope or not isinstance(scope, str):
        raise ValueError("scope must be a non-empty string")
    timestamps = _timestamps()
    path = executable or shutil.which("dcgmi")
    result: Dict[str, Any] = {
        "schema_version": "observability.dcgm-fields.v1",
        "source": "dcgmi",
        "scope": scope,
        **timestamps,
        "status": "unavailable",
        "provenance": "unavailable",
        "executable": path,
        "supported_field_ids": [],
        "raw_output_sha256": None,
        "error": "executable_not_found" if not path else None,
    }
    if not path:
        return result
    try:
        completed = runner([path, "discovery", "-l"], check=False, capture_output=True, text=True, timeout=timeout_seconds)
        output = (str(getattr(completed, "stdout", "") or "") + "\n" + str(getattr(completed, "stderr", "") or "")).strip()
        if getattr(completed, "returncode", 1) != 0 or not output:
            result["error"] = "command_exit:%s" % getattr(completed, "returncode", 1)
            return result
        result["status"] = "available"
        result["provenance"] = "measured"
        result["raw_output_sha256"] = __import__("hashlib").sha256(output.encode()).hexdigest()
        result["supported_field_ids"] = sorted({int(value) for value in re.findall(r"(?<![0-9])([0-9]{3,6})(?![0-9])", output)})
        result["raw_output"] = output[:4096]
        return result
    except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
        result["error"] = type(exc).__name__
        return result


def collect_dcgmi_sample(
    field_ids: Sequence[int | str],
    *,
    scope: str = "run_interval",
    executable: Optional[str] = None,
    timeout_seconds: float = 5.0,
    runner: _Runner = subprocess.run,
) -> Dict[str, Any]:
    """Collect one explicit DCGM ``dmon`` sample without guessing field names.

    This is an optional Level-1/Level-2 host path.  The raw DCGM response is
    kept because its columns vary by DCGM version; callers must normalize
    fields only after the host capability report identifies their meaning.
    ``nvidia-smi`` remains the documented fallback when this is unavailable.
    """

    if not scope or not isinstance(scope, str):
        raise ValueError("scope must be a non-empty string")
    normalized: list[str] = []
    for field in field_ids:
        text = str(field).strip()
        if not text.isdigit():
            raise ValueError("DCGM field IDs must be numeric")
        normalized.append(text)
    if not normalized:
        raise ValueError("at least one DCGM field ID is required")
    timestamps = _timestamps()
    path = executable or shutil.which("dcgmi")
    value: Dict[str, Any] = {
        "schema_version": "observability.dcgm-sample.v1",
        "source": "dcgmi",
        "scope": scope,
        **timestamps,
        "field_ids": normalized,
        "poll_count": 1,
        "status": "unavailable",
        "provenance": "unavailable",
        "raw_output": None,
        "raw_output_sha256": None,
        "error": "executable_not_found" if not path else None,
    }
    if not path:
        return value
    try:
        completed = runner(
            [path, "dmon", "-e", ",".join(normalized), "-c", "1"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        output = (str(getattr(completed, "stdout", "") or "") + "\n" + str(getattr(completed, "stderr", "") or "")).strip()
        if getattr(completed, "returncode", 1) != 0 or not output:
            value["error"] = "command_exit:%s" % getattr(completed, "returncode", 1)
            return value
        value["status"] = "measured"
        value["provenance"] = "measured"
        value["raw_output"] = output[:8192]
        value["raw_output_sha256"] = __import__("hashlib").sha256(output.encode()).hexdigest()
        return value
    except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
        value["error"] = type(exc).__name__
        return value
