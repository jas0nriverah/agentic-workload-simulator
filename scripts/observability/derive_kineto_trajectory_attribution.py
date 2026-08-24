#!/usr/bin/env python3
"""Stream a large Kineto trace into per-request CUDA activity summaries."""

from __future__ import annotations

import argparse
import bisect
import gzip
import hashlib
import json
import re
import struct
import tempfile
from array import array
from datetime import datetime
from pathlib import Path


DEVICE_CATEGORIES = {"kernel", "gpu_memcpy", "gpu_memset"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def epoch_ns(timestamp: str) -> int:
    return int(datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp() * 1e9)


def interval_union_ns(intervals) -> int:
    iterator = iter(intervals)
    try:
        current_start, current_end = next(iterator)
    except StopIteration:
        return 0
    total = 0
    for start, end in iterator:
        if start <= current_end:
            current_end = max(current_end, end)
        else:
            total += current_end - current_start
            current_start, current_end = start, end
    return total + current_end - current_start


def trace_events(path: Path, supplied_base_ns: int | None):
    decoder = json.JSONDecoder()
    marker = '"traceEvents": ['
    buffer = ""
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        while marker not in buffer:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                raise ValueError("traceEvents array not found")
            buffer += chunk
        prefix, buffer = buffer.split(marker, 1)
        match = re.search(r'"baseTimeNanoseconds"\s*:\s*(\d+)', prefix)
        base_ns = supplied_base_ns if supplied_base_ns is not None else (int(match.group(1)) if match else None)
        if base_ns is None:
            raise ValueError("baseTimeNanoseconds follows traceEvents; pass --base-time-ns from a same-process capability trace")
        yield {"baseTimeNanoseconds": base_ns}
        position = 0
        while True:
            while position < len(buffer) and buffer[position] in " \r\n\t,":
                position += 1
            if position < len(buffer) and buffer[position] == "]":
                return
            try:
                event, position = decoder.raw_decode(buffer, position)
                yield event
            except json.JSONDecodeError:
                buffer = buffer[position:]
                position = 0
                chunk = stream.read(8 * 1024 * 1024)
                if not chunk:
                    raise
                buffer += chunk
            if position > 16 * 1024 * 1024:
                buffer = buffer[position:]
                position = 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests-jsonl", required=True, type=Path)
    parser.add_argument("--trace", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--base-time-ns", type=int)
    parser.add_argument("--temp-dir", type=Path)
    args = parser.parse_args()

    requests = []
    for line in args.requests_jsonl.read_text().splitlines():
        row = json.loads(line)
        if row.get("path") != "/v1/chat/completions" or row.get("status_code") != 200:
            continue
        end_ns = epoch_ns(row["utc_recorded"])
        start_ns = end_ns - round(float(row["duration_ms"]) * 1e6)
        requests.append(
            {
                "request_id": row["request_id"],
                "start_utc_ns": start_ns,
                "end_utc_ns": end_ns,
                "duration_ms": row["duration_ms"],
                "prompt_tokens": row.get("prompt_tokens"),
                "completion_tokens": row.get("completion_tokens"),
                "total_tokens": row.get("total_tokens"),
                "kernel_count": 0,
                "device_activity_count": 0,
                "kernel_duration_sum_ns": 0,
                "device_union_ns": 0,
                "union_start_ns": None,
                "union_end_ns": None,
                "out_of_order_device_events": 0,
                "last_device_start_ns": None,
            }
        )
    requests.sort(key=lambda row: row["start_utc_ns"])
    starts = [row["start_utc_ns"] for row in requests]

    with tempfile.TemporaryDirectory(dir=args.temp_dir) as temporary:
        interval_paths = [Path(temporary) / f"request-{index:03d}.bin" for index in range(len(requests))]
        interval_streams = [path.open("wb") for path in interval_paths]
        try:
            iterator = trace_events(args.trace, args.base_time_ns)
            base_ns = next(iterator)["baseTimeNanoseconds"]
            total_events = device_events = 0
            for event in iterator:
                total_events += 1
                category = event.get("cat")
                if event.get("ph") != "X" or category not in DEVICE_CATEGORIES:
                    continue
                device_events += 1
                start = base_ns + round(float(event["ts"]) * 1000)
                end = start + round(float(event.get("dur", 0)) * 1000)
                index = bisect.bisect_right(starts, start) - 1
                if index < 0:
                    continue
                row = requests[index]
                if start >= row["end_utc_ns"] or end <= row["start_utc_ns"]:
                    continue
                start = max(start, row["start_utc_ns"])
                end = min(end, row["end_utc_ns"])
                interval_streams[index].write(struct.pack("<qq", start, end))
                row["device_activity_count"] += 1
                if category == "kernel":
                    row["kernel_count"] += 1
                    row["kernel_duration_sum_ns"] += end - start
                previous_start = row["last_device_start_ns"]
                if previous_start is not None and start < previous_start:
                    row["out_of_order_device_events"] += 1
                row["last_device_start_ns"] = start
        finally:
            for stream in interval_streams:
                stream.close()

        for row, path in zip(requests, interval_paths):
            values = array("q")
            with path.open("rb") as stream:
                values.fromfile(stream, path.stat().st_size // values.itemsize)
            intervals = sorted(zip(values[0::2], values[1::2]))
            row["device_union_ns"] = interval_union_ns(intervals)

    output_rows = []
    for row in requests:
        output_rows.append(
            {
                key: value
                for key, value in {
                    **row,
                    "kernel_duration_sum_ms": row["kernel_duration_sum_ns"] / 1e6,
                    "device_activity_union_ms": row["device_union_ns"] / 1e6,
                }.items()
                if key not in {"kernel_duration_sum_ns", "device_union_ns", "union_start_ns", "union_end_ns", "last_device_start_ns"}
            }
        )

    result = {
        "schema_version": "kineto-trajectory-request-attribution.v1",
        "provenance": "derived_from_measured_trace",
        "trace_sha256": sha256(args.trace),
        "requests_sha256": sha256(args.requests_jsonl),
        "trace_base_time_nanoseconds": base_ns,
        "trace_event_count": total_events,
        "device_activity_event_count": device_events,
        "request_count": len(output_rows),
        "event_order_valid_for_streaming_union": all(row["out_of_order_device_events"] == 0 for row in output_rows),
        "device_union_method": "per-request binary interval capture followed by timestamp sort and exact union",
        "records": output_rows,
        "scientific_boundary": "Direct Kineto CUDA activities within serialized proxy request windows; no hardware counters or historical-run transfer.",
    }
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
