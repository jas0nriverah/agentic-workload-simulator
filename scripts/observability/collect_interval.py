#!/usr/bin/env python3
"""Append one Level-1 interval observation without mutating the agent."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from agentic_sim.observability.gpu import collect_dcgmi_sample, collect_nvidia_smi_sample  # noqa: E402
from agentic_sim.observability.vllm_metrics import (  # noqa: E402
    PrometheusSnapshot,
    parse_prometheus_text,
    required_families,
)
from scrape_vllm import snapshot_object, unavailable_object  # noqa: E402


def _append(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
    try:
        view = memoryview(encoded)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)


def _fetch(url: str, timeout: float) -> tuple[str | None, str | None]:
    try:
        request = urllib.request.Request(url, headers={"Accept": "text/plain; version=0.0.4"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", "replace"), None
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def collect(args: argparse.Namespace) -> dict[str, Any]:
    now = time.monotonic_ns()
    raw, error = _fetch(args.metrics_url, args.timeout)
    if raw is None:
        vllm = unavailable_object(
            url=args.metrics_url,
            run_id=args.run_id,
            attempt_id=args.attempt_id,
            snapshot_kind=args.snapshot_kind,
            scope="run_interval",
            error=error or "request_failed",
        )
    else:
        vllm = snapshot_object(
            raw,
            url=args.metrics_url,
            run_id=args.run_id,
            attempt_id=args.attempt_id,
            snapshot_kind=args.snapshot_kind,
            scope="run_interval",
        )
    dcgm = None
    dcgm_fields = getattr(args, "dcgmi_fields", None)
    if dcgm_fields:
        dcgm = collect_dcgmi_sample(dcgm_fields.split(","), scope="run_interval")
    if dcgm and dcgm.get("status") == "measured":
        gpu = dcgm
    else:
        gpu = collect_nvidia_smi_sample(scope="run_interval")
        if dcgm is not None:
            gpu["dcgm_attempt"] = dcgm
            gpu["fallback"] = "nvidia-smi"
    vllm_ok = vllm.get("status") == "measured"
    gpu_ok = gpu.get("status") == "measured"
    complete = vllm_ok and gpu_ok
    event = {
        "schema_version": "obs.telemetry.v1",
        "seq": args.seq,
        "event_id": "event-" + uuid.uuid4().hex,
        "run_id": args.run_id,
        "attempt_id": args.attempt_id,
        "instance_id": args.instance_id,
        "event_type": "telemetry_sample",
        "request_id": None,
        "action_id": None,
        "step_id": None,
        "start_mono_ns": now,
        "end_mono_ns": now,
        "duration_ms": 0.0,
        "utc_recorded": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        # A partial sample can still contain a valid vLLM scrape. Keep the
        # source-level statuses below and mark `partial=true`; callers must
        # not read this top-level marker as complete GPU+server evidence.
        "provenance": "measured" if vllm_ok else "unavailable",
        "measurement_class": "run_interval_observation",
        "correlation_scope": "run_interval",
        "partial": not complete and (vllm_ok or gpu_ok),
        "payload": {
            "vllm": vllm,
            "gpu": gpu,
            "metrics_endpoint": args.metrics_url,
            "correlation_scope": "run_interval",
            "source_status": {"vllm": vllm.get("status"), "gpu": gpu.get("status")},
        },
    }
    return {"event": event, "scrape": {"event": event, "vllm": vllm, "gpu": gpu, "raw": raw}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metrics-url", required=True)
    parser.add_argument("--events", required=True, type=Path)
    parser.add_argument("--scrapes", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--seq", required=True, type=int)
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--snapshot-kind", choices=("interval", "final"), default="interval")
    parser.add_argument("--dcgmi-fields", help="optional comma-separated numeric DCGM field IDs; nvidia-smi is the fallback")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.dry_run:
        print(f"DRY-RUN: scrape aggregate {args.metrics_url}; use explicit DCGM fields when supplied, otherwise nvidia-smi fallback; append to {args.events}")
        return 0
    value = collect(args)
    _append(args.events, value["event"])
    scrape = value["scrape"]
    scrape.pop("raw", None)
    _append(args.scrapes, scrape)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
