"""Cheap, process-scoped resource samples; never a tool-work estimate.

Samples carry their own clock brackets. They are taken when journal records
are emitted, not retroactively at an explicitly supplied event timestamp.
Only same-process, same-boot samples may be differenced. Concurrent threads
and instrumentation contribute to RUSAGE_SELF; container/child work does not.
"""
from __future__ import annotations

from functools import lru_cache
import math
import os
from pathlib import Path
import resource
import sys

from .clock import clock_fields, monotonic_ns

SCHEMA = "assignment.process-resource-snapshot.v1"
FIELDS = (
    "ru_utime", "ru_stime", "ru_minflt", "ru_majflt", "ru_nvcsw",
    "ru_nivcsw", "ru_inblock", "ru_oublock", "ru_maxrss",
)


@lru_cache(maxsize=8)
def _start_ticks(pid: int) -> int | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        # Field 2 (comm) can contain spaces and parentheses; field 22 is
        # position 19 after the final comm delimiter and field 3 (state).
        return int(raw.rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def capture_process_resources() -> dict:
    pid = os.getpid()
    identity = {"pid": pid, "process_start_ticks": _start_ticks(pid),
                "process_start_source": "/proc/<pid>/stat field 22", "clock": dict(clock_fields())}
    begin = monotonic_ns()
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        counters = {field: getattr(usage, field) for field in FIELDS}
        if any(isinstance(v, bool) or not isinstance(v, (int, float))
               or not math.isfinite(v) or v < 0 for v in counters.values()):
            raise ValueError("invalid native resource counter")
        availability, reason = "measured", None
    except (OSError, ValueError, AttributeError) as exc:
        counters = {field: None for field in FIELDS}
        availability, reason = "unavailable", type(exc).__name__
    end = monotonic_ns()
    return {
        "schema_version": SCHEMA, **identity,
        "sample_start_mono_ns": begin, "sample_end_mono_ns": end,
        "source": "resource.getrusage(RUSAGE_SELF)",
        "scope": "recording process including its threads; excludes children and containers",
        "availability": availability, "reason": reason, "counters": counters,
        "units": {"ru_utime": "seconds", "ru_stime": "seconds",
                  "ru_minflt": "faults", "ru_majflt": "faults",
                  "ru_nvcsw": "context_switches", "ru_nivcsw": "context_switches",
                  "ru_inblock": "platform_native_counter_not_bytes",
                  "ru_oublock": "platform_native_counter_not_bytes",
                  "ru_maxrss": "KiB" if sys.platform.startswith("linux") else "platform_native"},
        "interpretation": "Cumulative process counters; maxrss is a high-water mark, not additive work. Differences require identical process/boot identity and monotonic order, and are not causal per-tool measurements.",
    }
