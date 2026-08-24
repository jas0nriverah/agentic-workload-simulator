#!/usr/bin/env python3
"""Deterministic trace-provider fixture; it never starts a workload."""

import argparse
import hashlib
import json
from pathlib import Path


parser = argparse.ArgumentParser()
for name in (
    "config",
    "case-id",
    "split",
    "input-tokens",
    "output-tokens",
    "repeat-id",
    "phase",
    "start-mono-ns",
    "end-mono-ns",
    "output-dir",
):
    parser.add_argument("--" + name, required=True)
args = parser.parse_args()

output_dir = Path(args.output_dir)
output_dir.mkdir(parents=True, exist_ok=True)
raw_path = output_dir / "trace.bin"
raw_path.write_bytes((args.case_id + "/" + args.repeat_id + "/" + args.phase).encode())
raw_sha256 = hashlib.sha256(raw_path.read_bytes()).hexdigest()
summary = {
    "schema_version": "h100-trace-summary.v1",
    "provenance": "measured",
    "clock_id": "CLOCK_MONOTONIC_RAW",
    "cuda_union_rule": "overlap_aware_request_window",
    "cpu_activity_union_ms": 1.25,
    "cuda_activity_union_ms": 2.5,
    "kernel_duration_sum_ms": 3.75,
    "raw_artifacts": [{"kind": "fixture_trace", "path": "trace.bin", "sha256": raw_sha256}],
}
(output_dir / "trace_summary.json").write_text(
    json.dumps(summary, sort_keys=True) + "\n", encoding="utf-8"
)
