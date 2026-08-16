#!/usr/bin/env python3
"""Capture a lossless, explicitly aggregate vLLM Prometheus snapshot.

This is a post-processing observer. It never sends an inference request and
never assigns native server metrics to a SWE-agent request ID.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from agentic_sim.observability.vllm_metrics import (  # noqa: E402
    REQUIRED_VLLM_FAMILIES,
    PrometheusSnapshot,
    parse_prometheus_text,
    required_families,
)
from agentic_sim.telemetry.clock import clock_fields, monotonic_ns, utc_now  # noqa: E402


def _utc() -> str:
    return utc_now()


def _sample(sample: Any) -> dict[str, Any]:
    family = sample.family
    return {
        "name": sample.name,
        "family_name": sample.family_name,
        "labels": dict(sorted(sample.labels.items())),
        "value": sample.value,
        "metric_type": sample.metric_type,
        "timestamp_ms": sample.timestamp_ms,
        "aggregation_scope": family.scope if family else "unknown",
        "aggregation": family.aggregation if family else "unknown",
        "measurement_class": "aggregate_server_metric",
        "per_request": False,
    }


def snapshot_object(
    raw: str,
    *,
    url: str,
    run_id: str | None,
    attempt_id: str | None,
    snapshot_kind: str,
    scope: str,
) -> dict[str, Any]:
    captured_at = _utc()
    mono = monotonic_ns()
    raw_bytes = raw.encode("utf-8")
    try:
        snapshot: PrometheusSnapshot = parse_prometheus_text(raw)
        missing = list(required_families(snapshot))
        samples = [_sample(item) for item in snapshot]
        invalid_required = sorted({item.family_name for item in snapshot if item.family_name in REQUIRED_VLLM_FAMILIES and not math.isfinite(item.value)})
        status = "measured" if not missing and not invalid_required else "incomplete"
        provenance = "measured" if samples else "unavailable"
        error = "non_finite_required_sample" if invalid_required else None
        type_map = dict(snapshot.types)
    except Exception as exc:  # parser failures are recorded, not fabricated
        missing = sorted(REQUIRED_VLLM_FAMILIES)
        samples = []
        status = "unavailable"
        provenance = "unavailable"
        error = f"{type(exc).__name__}: {exc}"
        type_map = {}
    return {
        "schema_version": "observability.vllm-snapshot.v1",
        "status": status,
        "provenance": provenance,
        "aggregation_scope": "server_aggregate",
        "measurement_class": "aggregate_server_metric",
        "snapshot_kind": snapshot_kind,
        "correlation_scope": scope,
        "request_id": None,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "metrics_url": url,
        "captured_at_utc": captured_at,
        "captured_monotonic_ns": mono,
        "clock": clock_fields(),
        "raw_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "raw_bytes": len(raw_bytes),
        "required_families": sorted(REQUIRED_VLLM_FAMILIES),
        "missing_required_families": missing,
        "invalid_required_families": invalid_required,
        "types": type_map,
        "samples": samples,
        "error": error,
    }


def unavailable_object(
    *, url: str, run_id: str | None, attempt_id: str | None, snapshot_kind: str, scope: str, error: str
) -> dict[str, Any]:
    return {
        "schema_version": "observability.vllm-snapshot.v1",
        "status": "unavailable",
        "provenance": "unavailable",
        "aggregation_scope": "server_aggregate",
        "measurement_class": "aggregate_server_metric",
        "snapshot_kind": snapshot_kind,
        "correlation_scope": scope,
        "request_id": None,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "metrics_url": url,
        "captured_at_utc": _utc(),
        "captured_monotonic_ns": monotonic_ns(),
        "clock": clock_fields(),
        "raw_sha256": None,
        "raw_bytes": 0,
        "required_families": sorted(REQUIRED_VLLM_FAMILIES),
        "missing_required_families": sorted(REQUIRED_VLLM_FAMILIES),
        "invalid_required_families": [],
        "types": {},
        "samples": [],
        "error": error,
    }


def _write(path: Path, value: dict[str, Any], *, force: bool = False) -> None:
    if path.exists() and not force:
        raise SystemExit(f"refusing to overwrite existing snapshot: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--raw-output", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--attempt-id")
    parser.add_argument("--snapshot-kind", choices=("start", "end", "interval", "health"), default="interval")
    parser.add_argument("--scope", default="run_interval")
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--validate-required", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.dry_run:
        print(f"DRY-RUN: GET {args.url}; parse losslessly; preserve labels/TYPE; write aggregate snapshot {args.output}")
        if args.raw_output:
            print(f"DRY-RUN: preserve raw exposition at {args.raw_output}")
        return 0
    raw = None
    error = None
    try:
        request = urllib.request.Request(args.url, headers={"Accept": "text/plain; version=0.0.4"})
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        error = f"{type(exc).__name__}: {exc}"
    value = (
        snapshot_object(raw, url=args.url, run_id=args.run_id, attempt_id=args.attempt_id, snapshot_kind=args.snapshot_kind, scope=args.scope)
        if raw is not None
        else unavailable_object(url=args.url, run_id=args.run_id, attempt_id=args.attempt_id, snapshot_kind=args.snapshot_kind, scope=args.scope, error=error or "request_failed")
    )
    _write(args.output, value, force=args.force)
    if args.raw_output is not None and raw is not None:
        if args.raw_output.exists() and not args.force:
            raise SystemExit(f"refusing to overwrite existing raw snapshot: {args.raw_output}")
        args.raw_output.parent.mkdir(parents=True, exist_ok=True)
        raw_temporary = args.raw_output.with_name(args.raw_output.name + ".tmp")
        raw_temporary.write_text(raw, encoding="utf-8")
        raw_temporary.replace(args.raw_output)
    if args.validate_required and (value["missing_required_families"] or value["status"] != "measured"):
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
