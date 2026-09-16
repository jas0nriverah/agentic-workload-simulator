"""Hardware profile adapter for the offline D9 simulator.

The assignment implementation already owns the strict
``assignment.hardware-profile.v1`` dataclass.  The salvage package reuses it
so that the CLI and the optional v3 model path cannot drift into two slightly
different hardware schemas.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping


_ROOT = Path(__file__).resolve().parents[3]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from agentic_sim.assignment.event_simulator import (  # noqa: E402
    EventSimulatorError,
    HARDWARE_SCHEMA,
    HardwareProfile,
)


HardwareProfileError = EventSimulatorError


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


def hardware_profile_sha256(profile: HardwareProfile | Mapping[str, Any]) -> str:
    """Hash the canonical, schema-bearing profile representation."""

    parsed = (
        profile
        if isinstance(profile, HardwareProfile)
        else HardwareProfile.from_mapping(profile)
    )
    return hashlib.sha256(_canonical_bytes(parsed.to_mapping())).hexdigest()


def default_hardware_profile() -> HardwareProfile:
    """The historical H100 reference profile used by the retained package."""

    return HardwareProfile.from_mapping(
        {
            "schema_version": HARDWARE_SCHEMA,
            "hardware_id": "h100-80gb-pace",
            "architecture": "x86_64-h100-80gb",
            "cpu_cores": 16,
            "cpu_threads": 32,
            "cpu_base_ghz": 2.8,
            "system_memory_gib": 128.0,
            "storage_read_mbps": 500.0,
            "storage_write_mbps": 500.0,
            "gpu_count": 1,
            "gpu_compute_capability": 9.0,
            "gpu_memory_gib": 80.0,
            "gpu_memory_bandwidth_gbps": 3350.0,
            "gpu_bf16_tflops": 989.4,
        }
    )


__all__ = [
    "HARDWARE_SCHEMA",
    "HardwareProfile",
    "HardwareProfileError",
    "default_hardware_profile",
    "hardware_profile_sha256",
]
