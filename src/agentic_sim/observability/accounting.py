"""Reset-safe interval-union accounting for one timed event stream.

This helper closes overlaps and gaps using the selected monotonic clock. It
never merges records from different clock, host, or boot identities and does
not turn aggregate GPU metrics into device-time claims.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable, Mapping
from typing import Any


class AccountingError(ValueError):
    """Timed records cannot be compared safely."""


@dataclass(frozen=True)
class TimedInterval:
    start_ns: int
    end_ns: int
    source: str
    event_id: str
    identity: tuple[str, str, str | None]

    @property
    def duration_ns(self) -> int:
        return self.end_ns - self.start_ns


def _identity(row: Mapping[str, Any], index: int) -> tuple[str, str, str | None]:
    clock = row.get("clock")
    if not isinstance(clock, Mapping):
        raise AccountingError(f"interval {index} has no nested clock identity")
    values = (clock.get("clock_id"), clock.get("hostname"), clock.get("boot_id"))
    if not isinstance(values[0], str) or not values[0]:
        raise AccountingError(f"interval {index} has no clock_id")
    if not isinstance(values[1], str) or not values[1]:
        raise AccountingError(f"interval {index} has no hostname")
    if values[2] is not None and (not isinstance(values[2], str) or not values[2]):
        raise AccountingError(f"interval {index} has an invalid boot_id")
    return values[0], values[1], values[2]


def _intervals(records: Iterable[Mapping[str, Any]]) -> list[TimedInterval]:
    result: list[TimedInterval] = []
    for index, row in enumerate(records):
        if not isinstance(row, Mapping):
            raise AccountingError(f"interval {index} is not an object")
        start = row.get("start_mono_ns")
        end = row.get("end_mono_ns")
        if isinstance(start, bool) or not isinstance(start, int):
            raise AccountingError(f"interval {index} has no integer start_mono_ns")
        if isinstance(end, bool) or not isinstance(end, int):
            raise AccountingError(f"interval {index} has no integer end_mono_ns")
        if end < start:
            raise AccountingError(f"interval {index} ends before it starts")
        event_id = row.get("event_id", f"row-{index}")
        if not isinstance(event_id, str) or not event_id:
            raise AccountingError(f"interval {index} has an invalid event_id")
        source = row.get("event_type", row.get("record_type", "unknown"))
        if not isinstance(source, str) or not source:
            source = "unknown"
        result.append(TimedInterval(start, end, source, event_id, _identity(row, index)))
    return result


def merge_intervals(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return the deterministic union of timed records from one clock identity."""

    intervals = _intervals(records)
    if not intervals:
        return []
    identities = {item.identity for item in intervals}
    if len(identities) != 1:
        raise AccountingError("cannot merge intervals across clock/host/boot identities")
    ordered = sorted(intervals, key=lambda item: (item.start_ns, item.end_ns, item.event_id))
    merged: list[dict[str, Any]] = []
    current_start = ordered[0].start_ns
    current_end = ordered[0].end_ns
    contributors = [{"event_id": ordered[0].event_id, "source": ordered[0].source}]
    for item in ordered[1:]:
        if item.start_ns <= current_end:
            current_end = max(current_end, item.end_ns)
            contributors.append({"event_id": item.event_id, "source": item.source})
            continue
        merged.append({
            "start_mono_ns": current_start,
            "end_mono_ns": current_end,
            "duration_ns": current_end - current_start,
            "contributors": contributors,
        })
        current_start, current_end = item.start_ns, item.end_ns
        contributors = [{"event_id": item.event_id, "source": item.source}]
    merged.append({
        "start_mono_ns": current_start,
        "end_mono_ns": current_end,
        "duration_ns": current_end - current_start,
        "contributors": contributors,
    })
    return merged


def summarize_interval_union(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Account for raw, overlapping, and uncovered time without GPU claims."""

    intervals = _intervals(records)
    if not intervals:
        return {
            "schema_version": "observability.interval-union.v1",
            "status": "unavailable",
            "provenance": "unavailable",
            "reason": "no timed records",
            "aggregation_scope": "interval_union",
            "gpu_time_claims": False,
        }
    identities = {item.identity for item in intervals}
    if len(identities) != 1:
        raise AccountingError("cannot account intervals across clock/host/boot identities")
    merged = merge_intervals(records)
    identity = intervals[0].identity
    raw_duration = sum(item.duration_ns for item in intervals)
    union_duration = sum(int(item["duration_ns"]) for item in merged)
    start = min(item.start_ns for item in intervals)
    end = max(item.end_ns for item in intervals)
    span = end - start
    return {
        "schema_version": "observability.interval-union.v1",
        "status": "derived",
        "provenance": "derived",
        "aggregation_scope": "interval_union",
        "measurement_class": "timed_event_interval_union",
        "gpu_time_claims": False,
        "clock_identity": {
            "clock_id": identity[0],
            "hostname": identity[1],
            "boot_id": identity[2],
        },
        "raw_interval_count": len(intervals),
        "merged_interval_count": len(merged),
        "raw_duration_ns": raw_duration,
        "union_duration_ns": union_duration,
        "overlap_duration_ns": raw_duration - union_duration,
        "span_duration_ns": span,
        "gap_duration_ns": span - union_duration,
        "coverage_fraction": (union_duration / span) if span else 1.0,
        "intervals": merged,
    }


__all__ = ["AccountingError", "TimedInterval", "merge_intervals", "summarize_interval_union"]
