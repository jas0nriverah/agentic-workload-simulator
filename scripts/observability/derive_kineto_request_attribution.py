#!/usr/bin/env python3
"""Join serialized request wall intervals to Kineto CUDA activity intervals."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from datetime import datetime
from pathlib import Path


DEVICE_CATEGORIES = {"kernel", "gpu_memcpy", "gpu_memset"}
CPU_CATEGORIES = {"cpu_op"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def epoch_ns(timestamp: str) -> int:
    return int(datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp() * 1e9)


def interval_union_ns(intervals: list[tuple[int, int]]) -> int:
    if not intervals:
        return 0
    merged_total = 0
    current_start, current_end = sorted(intervals)[0]
    for start, end in sorted(intervals)[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
        else:
            merged_total += current_end - current_start
            current_start, current_end = start, end
    return merged_total + current_end - current_start


def merged_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not intervals:
        return []
    result = [sorted(intervals)[0]]
    for start, end in sorted(intervals)[1:]:
        previous_start, previous_end = result[-1]
        if start <= previous_end:
            result[-1] = (previous_start, max(previous_end, end))
        else:
            result.append((start, end))
    return result


def overlap_ns(left: list[tuple[int, int]], right: list[tuple[int, int]]) -> int:
    total = 0
    left_merged = merged_intervals(left)
    right_merged = merged_intervals(right)
    i = j = 0
    while i < len(left_merged) and j < len(right_merged):
        start = max(left_merged[i][0], right_merged[j][0])
        end = min(left_merged[i][1], right_merged[j][1])
        total += max(0, end - start)
        if left_merged[i][1] <= right_merged[j][1]:
            i += 1
        else:
            j += 1
    return total


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests", required=True, type=Path)
    parser.add_argument("--trace", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    requests = json.loads(args.requests.read_text())
    with gzip.open(args.trace, "rt") as stream:
        trace = json.load(stream)
    base_ns = int(trace["baseTimeNanoseconds"])
    activities = []
    for event in trace["traceEvents"]:
        if event.get("ph") != "X" or event.get("cat") not in DEVICE_CATEGORIES | CPU_CATEGORIES:
            continue
        start = base_ns + round(float(event["ts"]) * 1000)
        end = start + round(float(event.get("dur", 0)) * 1000)
        activities.append((start, end, event["cat"]))

    results = []
    for record in requests["records"]:
        request_start = epoch_ns(record["start_utc"])
        request_end = epoch_ns(record["end_utc"])
        clipped = [
            (max(start, request_start), min(end, request_end), category)
            for start, end, category in activities
            if start < request_end and end > request_start
        ]
        kernels = [(start, end) for start, end, category in clipped if category == "kernel"]
        all_device = [(start, end) for start, end, category in clipped if category in DEVICE_CATEGORIES]
        cpu_activity = [(start, end) for start, end, category in clipped if category in CPU_CATEGORIES]
        cpu_union = interval_union_ns(cpu_activity)
        cpu_device_overlap = overlap_ns(cpu_activity, all_device)
        cpu_exclusive = cpu_union - cpu_device_overlap
        device_union = interval_union_ns(all_device)
        request_wall = request_end - request_start
        results.append(
            {
                **record,
                "device_activity_count": len(clipped),
                "kernel_count": len(kernels),
                "kernel_duration_sum_ms": sum(end - start for start, end in kernels) / 1e6,
                "kernel_interval_union_ms": interval_union_ns(kernels) / 1e6,
                "device_activity_union_ms": device_union / 1e6,
                "cpu_op_interval_union_ms": cpu_union / 1e6,
                "cpu_device_overlap_ms": cpu_device_overlap / 1e6,
                "cpu_exclusive_interval_ms": cpu_exclusive / 1e6,
                "uncovered_fixed_interval_ms": (request_wall - cpu_exclusive - device_union) / 1e6,
                "attribution_method": "serialized request UTC interval joined to Kineto baseTimeNanoseconds plus activity ts",
            }
        )

    output = {
        "schema_version": "kineto-request-device-attribution.v1",
        "provenance": "derived_from_measured_trace",
        "scope": "controlled serialized synthetic vLLM requests; not historical SWE-agent trajectories",
        "trace_sha256": sha256(args.trace),
        "requests_sha256": sha256(args.requests),
        "trace_base_time_nanoseconds": base_ns,
        "device_properties": trace.get("deviceProperties"),
        "device_activity_categories": sorted(DEVICE_CATEGORIES),
        "cpu_activity_categories": sorted(CPU_CATEGORIES),
        "decomposition": "request wall = CPU-op union excluding overlap with device activity + device-activity union + uncovered fixed interval",
        "records": results,
    }
    args.output.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
