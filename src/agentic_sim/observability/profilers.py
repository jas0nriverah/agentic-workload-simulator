"""Capability discovery and safe command plans for optional profilers.

This module does not install, execute, or infer support for any profiler.  It
only probes an executable that is already on ``PATH`` and builds argv lists for
separate, explicitly profiled attempts.  Callers should persist the returned
plan after applying their own secret-redaction policy.
"""

from __future__ import annotations

import datetime as _datetime
import hashlib
import json
import shutil
import subprocess
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence


_Runner = Callable[..., Any]
_TOOL_VERSION_FLAGS = {
    "dcgmi": "--version",
    "nsys": "--version",
    "strace": "--version",
    "py-spy": "--version",
}
DEFAULT_TOOLS = tuple(_TOOL_VERSION_FLAGS)
_FORBIDDEN_MODES = {"control", "thin", "thin-telemetry", "uninstrumented"}
_PROFILE_MODES = {
    "syscall",
    "nsys",
    "otel",
    "profile",
    "profiling",
    "profiled",
    "deep-profile",
    "deep_profile",
    "cpu-profile",
    "cpu_profile",
    "gpu-profile",
    "gpu_profile",
}


def _utc_now() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _timestamps() -> Dict[str, Any]:
    return {"checked_at_utc": _utc_now(), "checked_monotonic_ns": time.monotonic_ns()}


def _version_record(
    value: Optional[str], *, source: str, scope: str, timestamps: Dict[str, Any], error: Optional[str] = None
) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "value": value,
        "status": "measured" if value else "unavailable",
        "provenance": "measured" if value else "unavailable",
        "source": source,
        "scope": scope,
        **timestamps,
    }
    if error:
        record["error"] = error
    return record


def detect_tool_capability(
    tool: str,
    *,
    scope: str = "host_capability",
    timeout_seconds: float = 3.0,
    executable: Optional[str] = None,
    runner: _Runner = subprocess.run,
) -> Dict[str, Any]:
    """Detect one already-installed profiler/tool and report its version.

    A present executable whose version command fails is reported as
    ``installed_unavailable``.  No profiler-specific fields are inferred from
    executable presence.
    """

    if tool not in _TOOL_VERSION_FLAGS:
        raise ValueError("unsupported capability tool: %s" % tool)
    if not scope or not isinstance(scope, str):
        raise ValueError("scope must be a non-empty string")
    timestamps = _timestamps()
    path = executable or shutil.which(tool)
    result: Dict[str, Any] = {
        "schema_version": "observability.tool-capability.v1",
        "tool": tool,
        "source": "PATH",
        "scope": scope,
        **timestamps,
        "executable": path,
        "installed": bool(path),
        "status": "unavailable" if not path else "installed_unverified",
        "provenance": "unavailable" if not path else "measured",
        "version": _version_record(
            None,
            source=tool,
            scope=scope,
            timestamps=timestamps,
            error="executable_not_found" if not path else None,
        ),
    }
    if not path:
        result["error"] = "executable_not_found"
        return result
    try:
        completed = runner(
            [path, _TOOL_VERSION_FLAGS[tool]],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        output = (str(getattr(completed, "stdout", "") or "") + " " + str(getattr(completed, "stderr", "") or "")).strip()
        if getattr(completed, "returncode", 1) == 0 and output:
            result["status"] = "available"
            result["version"] = _version_record(output[:512], source=tool, scope=scope, timestamps=timestamps)
        else:
            result["status"] = "installed_unavailable"
            result["provenance"] = "unavailable"
            result["error"] = "command_exit:%s" % getattr(completed, "returncode", 1)
            result["version"] = _version_record(
                None,
                source=tool,
                scope=scope,
                timestamps=timestamps,
                error=result["error"],
            )
    except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
        result["status"] = "installed_unavailable"
        result["provenance"] = "unavailable"
        result["error"] = type(exc).__name__
        result["version"] = _version_record(
            None, source=tool, scope=scope, timestamps=timestamps, error=type(exc).__name__
        )
    return result


def discover_tool_capabilities(
    tools: Iterable[str] = DEFAULT_TOOLS,
    *,
    scope: str = "host_capability",
    timeout_seconds: float = 3.0,
    runner: _Runner = subprocess.run,
) -> Dict[str, Any]:
    """Discover installed versions for the optional tool set."""

    if not scope or not isinstance(scope, str):
        raise ValueError("scope must be a non-empty string")
    timestamps = _timestamps()
    names = list(tools)
    result = {
        "schema_version": "observability.tool-capabilities.v1",
        "source": "PATH",
        "scope": scope,
        **timestamps,
        "provenance": "measured",
        "tools": {
            name: detect_tool_capability(
                name,
                scope=scope,
                timeout_seconds=timeout_seconds,
                runner=runner,
            )
            for name in names
        },
    }
    return result


def _validate_profile_mode(mode: str) -> str:
    if not isinstance(mode, str) or not mode:
        raise ValueError("mode must be an explicit profiled mode")
    normalized = mode.strip().lower()
    if normalized in _FORBIDDEN_MODES:
        raise ValueError("profilers are forbidden for control/thin-telemetry modes")
    if normalized not in _PROFILE_MODES:
        raise ValueError("mode must identify a separate profiled attempt")
    return normalized


def _validate_argv(command: Sequence[str]) -> List[str]:
    if isinstance(command, (str, bytes)) or not command:
        raise TypeError("command must be a non-empty sequence of argv strings")
    argv = list(command)
    if any(not isinstance(part, str) or not part or "\x00" in part for part in argv):
        raise ValueError("command argv contains an empty, non-string, or NUL-containing argument")
    return argv


def _validate_output(output_path: str) -> str:
    if not isinstance(output_path, str) or not output_path or "\x00" in output_path:
        raise ValueError("output_path must be a non-empty path without NUL")
    return output_path


def _plan(tool: str, argv: List[str], *, mode: str, output_path: str) -> Dict[str, Any]:
    hash_payload = json.dumps(
        {"tool": tool, "mode": mode, "output_path": output_path, "command": argv},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    command_sha256 = hashlib.sha256(hash_payload).hexdigest()
    return {
        "schema_version": "observability.profiler-command.v1",
        "tool": tool,
        "mode": mode,
        "scope": "profiled_attempt",
        "provenance": "derived",
        "source": "safe_command_builder",
        "created_at_utc": _utc_now(),
        "output_path": output_path,
        "command": argv,
        "command_sha256": command_sha256,
        "command_hash": command_sha256,
    }


def build_strace_command(
    command: Sequence[str],
    *,
    output_path: str,
    mode: str,
    executable: str = "strace",
    trace_filter: str = "%file,%desc,%process",
) -> Dict[str, Any]:
    """Build an explicit CPU syscall trace plan for a separate attempt."""

    normalized_mode = _validate_profile_mode(mode)
    argv = _validate_argv(command)
    output = _validate_output(output_path)
    if not executable or "\x00" in executable or any(char.isspace() for char in executable):
        raise ValueError("executable must be one argv token")
    if not trace_filter or "\x00" in trace_filter or any(char.isspace() for char in trace_filter):
        raise ValueError("trace_filter must be one explicit strace filter token")
    plan = [
        executable,
        "-f",
        "-T",
        "-ttt",
        "-e",
        "trace=" + trace_filter,
        "-o",
        output,
        "--",
        *argv,
    ]
    return _plan("strace", plan, mode=normalized_mode, output_path=output)


def build_nsys_command(
    command: Sequence[str],
    *,
    output_path: str,
    mode: str,
    executable: str = "nsys",
    trace: str = "cuda,nvtx,osrt",
) -> Dict[str, Any]:
    """Build an Nsight Systems CUDA/API/kernel timeline plan."""

    normalized_mode = _validate_profile_mode(mode)
    argv = _validate_argv(command)
    output = _validate_output(output_path)
    if not executable or "\x00" in executable or any(char.isspace() for char in executable):
        raise ValueError("executable must be one argv token")
    if not trace or "\x00" in trace or any(char.isspace() for char in trace):
        raise ValueError("trace must be one Nsight trace token")
    plan = [
        executable,
        "profile",
        "--trace=" + trace,
        "--sample=none",
        "--force-overwrite=true",
        "--output",
        output,
        "--",
        *argv,
    ]
    return _plan("nsys", plan, mode=normalized_mode, output_path=output)
