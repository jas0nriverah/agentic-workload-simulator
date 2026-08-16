#!/usr/bin/env python3
"""Derive reset-safe aggregate deltas from two lossless vLLM snapshots."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
import sys
sys.path.insert(0, str(ROOT / "src"))

from agentic_sim.observability.vllm_metrics import (  # noqa: E402
    PrometheusSample,
    counter_delta,
    histogram_delta,
)


def _sample_from_json(value: dict[str, Any]) -> PrometheusSample:
    from types import MappingProxyType
    return PrometheusSample(
        name=str(value["name"]),
        labels=MappingProxyType(dict(value.get("labels") or {})),
        value=float(value["value"]),
        metric_type=value.get("metric_type"),
        timestamp_ms=value.get("timestamp_ms"),
    )


def _samples(snapshot: dict[str, Any]) -> tuple[PrometheusSample, ...]:
    return tuple(_sample_from_json(item) for item in snapshot.get("samples", []))


def derive(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    old = {sample.series_key: sample for sample in _samples(before)}
    new = {sample.series_key: sample for sample in _samples(after)}
    counters: list[dict[str, Any]] = []
    histograms: dict[str, list[PrometheusSample]] = {}
    for key, sample in old.items():
        if sample.family and sample.family.aggregation == "histogram":
            histograms.setdefault(sample.family_name, []).append(sample)
    for key, sample in new.items():
        if sample.family and sample.family.aggregation == "histogram":
            histograms.setdefault(sample.family_name, []).append(sample)
    for key in sorted(set(old) | set(new), key=str):
        left, right = old.get(key), new.get(key)
        family = (right or left).family if (right or left) else None
        if family and family.aggregation == "cumulative" and not family.metric_type == "histogram":
            result = counter_delta(left, right)
            counters.append({
                "name": (right or left).name,
                "labels": dict((right or left).labels),
                "before": left.value if left else None,
                "after": right.value if right else None,
                "status": result.status,
                "delta": result.value,
                "reason": result.reason,
                "aggregation_scope": "aggregate_delta",
                "measurement_class": "aggregate_server_metric",
                "provenance": "derived" if result.measured else "unavailable",
            })
    histogram_results: list[dict[str, Any]] = []
    families = sorted({sample.family_name for samples in histograms.values() for sample in samples})
    for family_name in families:
        result = histogram_delta(
            tuple(sample for sample in old.values() if sample.family_name == family_name),
            tuple(sample for sample in new.values() if sample.family_name == family_name),
        )
        histogram_results.append({
            "family_name": family_name,
            "status": result.status,
            "deltas": [
                {
                    "series": str(key),
                    "name": key[0] if isinstance(key, tuple) and len(key) == 2 else None,
                    "labels": dict(key[1]) if isinstance(key, tuple) and len(key) == 2 else {},
                    "delta": value,
                }
                for key, value in sorted((result.deltas or {}).items(), key=lambda item: str(item[0]))
            ],
            "reason": result.reason,
            "aggregation_scope": "aggregate_delta",
            "measurement_class": "aggregate_server_metric",
            "provenance": "derived" if result.measured else "unavailable",
        })
    instantaneous = [
        {
            "name": sample.name,
            "labels": dict(sample.labels),
            "value": sample.value,
            "aggregation_scope": "server_aggregate",
            "measurement_class": "instantaneous_server_metric",
            "provenance": "measured",
        }
        for sample in _samples(after)
        if sample.family and sample.family.aggregation == "instantaneous"
    ]
    statuses = [item["status"] for item in counters] + [item["status"] for item in histogram_results]
    if before.get("status") != "measured" or after.get("status") != "measured":
        status = "incomplete"
    else:
        status = "reset" if "reset" in statuses else ("unavailable" if "unavailable" in statuses else "derived")
    return {
        "schema_version": "observability.vllm-delta.v1",
        "status": status,
        "provenance": "derived" if status == "derived" else "unavailable",
        "aggregation_scope": "aggregate_delta",
        "measurement_class": "aggregate_server_metric",
        "request_id": None,
        "run_id": after.get("run_id"),
        "attempt_id": after.get("attempt_id"),
        "before_snapshot": str(before.get("captured_at_utc")),
        "after_snapshot": str(after.get("captured_at_utc")),
        "counters": counters,
        "histograms": histogram_results,
        "instantaneous_end": instantaneous,
        "derived_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", required=True, type=Path)
    parser.add_argument("--after", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.dry_run:
        print(f"DRY-RUN: derive reset-safe aggregate deltas from {args.before} and {args.after} -> {args.output}")
        return 0
    before = json.loads(args.before.read_text(encoding="utf-8"))
    after = json.loads(args.after.read_text(encoding="utf-8"))
    value = derive(before, after)
    if args.output.exists() and not args.force:
        raise SystemExit(f"refusing to overwrite existing delta: {args.output}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(args.output.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    return 0 if value["status"] == "derived" else 3


if __name__ == "__main__":
    raise SystemExit(main())
