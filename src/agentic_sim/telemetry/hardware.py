"""Evidence backed hardware descriptors for v2 telemetry and features.

The descriptor separates raw inventory from model-facing terms.  Raw values
may be retained for audit, while :func:`model_hardware_features` only emits
fields with an explicit source/availability and refuses unsupported scaling
inputs such as thread-count multipliers or unmeasured storage bandwidth.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import platform
from pathlib import Path
from typing import Any, Mapping


MODEL_HARDWARE_FIELDS = frozenset(
    {
        "cpu_frequency_hz",
        "cpu_frequency_source",
        "gpu_memory_bandwidth_bytes_per_s",
        "gpu_compute_tflops",
        "clock",
        "availability",
    }
)
AVAILABILITY = frozenset({"measured", "declared", "derived", "unavailable"})
STORAGE_FIELDS = frozenset({"bandwidth_bytes_per_s", "measured_bytes", "measured_files", "source"})


class HardwareDescriptorError(ValueError):
    """A hardware descriptor would introduce an unsupported model term."""


def _nonnegative(value: Any, field: str, *, integer: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HardwareDescriptorError(f"{field} must be non-negative")
    try:
        finite = math.isfinite(value)
    except (OverflowError, ValueError):
        finite = False
    if not finite or value < 0:
        raise HardwareDescriptorError(f"{field} must be a finite non-negative number")
    if integer and not isinstance(value, int):
        raise HardwareDescriptorError(f"{field} must be an integer")


def model_hardware_features(hardware: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate/copy the model-facing subset of a hardware inventory.

    Missing values stay absent; callers may use ``availability`` to state why
    a term is unavailable.  The function never invents a frequency, GPU
    bandwidth, precision, or VRAM value.
    """

    if hardware is None:
        return {"availability": {}}
    if not isinstance(hardware, Mapping):
        raise HardwareDescriptorError("hardware must be a mapping")
    unknown = sorted(set(str(key) for key in hardware).difference(MODEL_HARDWARE_FIELDS))
    if unknown:
        raise HardwareDescriptorError(f"unsupported model hardware fields: {', '.join(unknown)}")
    result = copy.deepcopy(dict(hardware))
    availability = result.get("availability", {})
    if availability is None:
        availability = {}
    if not isinstance(availability, Mapping):
        raise HardwareDescriptorError("availability must be a mapping")
    for field, state in availability.items():
        if field not in MODEL_HARDWARE_FIELDS or state not in AVAILABILITY:
            raise HardwareDescriptorError(f"invalid availability for hardware.{field}")
    result["availability"] = dict(availability)

    for field in ("cpu_frequency_hz", "gpu_memory_bandwidth_bytes_per_s", "gpu_compute_tflops"):
        if field in result and result[field] is not None:
            _nonnegative(result[field], f"hardware.{field}")
    for field in ("cpu_frequency_source",):
        if field in result and result[field] is not None and not isinstance(result[field], str):
            raise HardwareDescriptorError(f"hardware.{field} must be text or null")
    clock = result.get("clock")
    if clock is not None and not isinstance(clock, Mapping):
        raise HardwareDescriptorError("hardware.clock must be a mapping or null")

    # GPU identity, VRAM, precision, model size, capabilities, thread counts,
    # and storage inventory remain descriptive raw telemetry.  They are not
    # v2 model terms until a separately reviewed policy consumes them.
    return result


def raw_hardware_inventory(hardware: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a defensive copy for descriptive telemetry.

    This function performs no modeling and therefore may retain inventory
    fields that are intentionally excluded from :func:`model_hardware_features`.
    """

    return copy.deepcopy(dict(hardware or {}))


def local_cpu_profile() -> dict[str, Any]:
    """Return descriptive identity for the CPU host running the recorder.

    The profile is retained for provenance and hardware joins.  CPU thread
    count and these inventory fields are deliberately excluded from the
    model-facing projection; no workload scaling is inferred from them.
    Frequency files are read as observed descriptors and are not treated as a
    calibrated sensitivity.
    """

    model_name: str | None = None
    cpuinfo_mhz: list[float] = []
    cpuinfo_text: dict[str, str] = {}
    cpuinfo_raw: str | None = None
    try:
        candidates: dict[str, str] = {}
        cpuinfo_raw = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace")
        for line in cpuinfo_raw.splitlines():
            key, separator, value = line.partition(":")
            key, value = key.strip().lower(), value.strip()
            if separator and key in {"model name", "hardware", "processor"} and value:
                # Linux x86 lists a numeric processor index before model name.
                # Older ARM cpuinfo can instead use Processor as model text.
                if key != "processor" or not value.isdecimal():
                    candidates.setdefault(key, value)
            elif separator and key == "cpu mhz" and value:
                # Kernel-reported clock. Virtualization may expose a nominal
                # value; this alone does not measure the operating frequency.
                try:
                    parsed_mhz = float(value)
                except ValueError:
                    parsed_mhz = float("nan")
                if math.isfinite(parsed_mhz) and parsed_mhz > 0:
                    cpuinfo_mhz.append(parsed_mhz)
            elif separator and key in {"cache size", "cpu cores", "siblings", "cpu family", "model", "stepping", "microcode"} and value:
                cpuinfo_text.setdefault(key.replace(" ", "_"), value)
        model_name = next((candidates[key] for key in ("model name", "hardware", "processor") if key in candidates), None)
    except OSError:
        pass
    frequencies: dict[str, int | None] = {}
    for name in ("cpuinfo_min_freq", "cpuinfo_max_freq", "scaling_min_freq", "scaling_max_freq"):
        value: int | None = None
        for candidate in (Path("/sys/devices/system/cpu/cpu0/cpufreq") / name, Path("/sys/devices/system/cpu/cpufreq") / name):
            try:
                text = candidate.read_text(encoding="ascii").strip()
                parsed = int(text)
            except (OSError, UnicodeError, ValueError):
                continue
            if parsed >= 0:
                value = parsed
                break
        frequencies[name] = value
    return {
        "architecture": platform.machine() or None,
        "system": platform.system() or None,
        "kernel_release": platform.release() or None,
        "model_name": model_name,
        "logical_cpu_count": os.cpu_count(),
        "frequency_khz": frequencies,
        "cpuinfo_clock": {
            "availability": "declared" if cpuinfo_mhz else "unavailable",
            "source": "/proc/cpuinfo:cpu MHz",
            "unit": "MHz",
            "values": cpuinfo_mhz,
            "minimum": min(cpuinfo_mhz) if cpuinfo_mhz else None,
            "maximum": max(cpuinfo_mhz) if cpuinfo_mhz else None,
            "interpretation": "Kernel-reported descriptor; may be nominal under virtualization; not a measured operating frequency or calibrated scaling sensitivity.",
        },
        "cpuinfo_descriptors": cpuinfo_text,
        # Retain the exact decoded source used above, including per-processor
        # topology, so later exports need not infer it from first-CPU values.
        "cpuinfo_source": {
            "path": "/proc/cpuinfo",
            "availability": "observed" if cpuinfo_raw is not None else "unavailable",
            "decoded_text": cpuinfo_raw,
            "decoded_utf8_sha256": hashlib.sha256(cpuinfo_raw.encode("utf-8")).hexdigest() if cpuinfo_raw is not None else None,
        },
        "source": "local_proc_sysfs_inventory",
    }


def composite_hardware_id(
    *,
    remote_profile_sha256: str,
    local_cpu: Mapping[str, Any],
    clock: Mapping[str, Any],
) -> str:
    """Bind a v2 attempt to its verified remote GPU and local CPU clock host.

    A generic execution mode is insufficient: two remote GPUs would otherwise
    normalize to the same ID when the recorder runs on a CPU host.  The remote
    profile digest is therefore mandatory and is joined with the actual local
    CPU inventory and boot/clock identity.  This ID is a provenance key only;
    it is not a fitted latency model.
    """

    if not isinstance(remote_profile_sha256, str) or not re_full_hex64(remote_profile_sha256):
        raise HardwareDescriptorError("remote_profile_sha256 must be a non-zero SHA-256")
    if not isinstance(local_cpu, Mapping):
        raise HardwareDescriptorError("local_cpu profile must be a mapping")
    if not isinstance(clock, Mapping) or not clock:
        raise HardwareDescriptorError("clock identity must be a non-empty mapping")
    payload = {
        "schema_version": "assignment.telemetry.v2.hardware-identity.v1",
        "remote_profile_sha256": remote_profile_sha256.lower(),
        "local_cpu_profile": copy.deepcopy(dict(local_cpu)),
        "clock_identity": copy.deepcopy(dict(clock)),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def re_full_hex64(value: str) -> bool:
    """Small dependency-free SHA-256 shape check used by the identity helper."""

    return len(value) == 64 and value.lower() == value and all(char in "0123456789abcdef" for char in value) and set(value) != {"0"}


__all__ = [
    "AVAILABILITY",
    "MODEL_HARDWARE_FIELDS",
    "HardwareDescriptorError",
    "composite_hardware_id",
    "local_cpu_profile",
    "model_hardware_features",
    "raw_hardware_inventory",
]
