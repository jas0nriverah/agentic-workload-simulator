#!/usr/bin/env python3
"""A100 adapter for the production Nsight Systems trace provider."""

from __future__ import annotations

import os
import runpy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
os.environ["HARDWARE_TARGET"] = "A100"
os.environ["HARDWARE_TRACE_SCHEMA"] = "a100-trace-summary.v1"
os.environ["HARDWARE_TRACE_PROVIDER_VERSION"] = "a100-nsight-trace-provider.v1"
for suffix in (
    "NSYS_CONTAINER",
    "NSYS_SESSION",
    "TRACE_MOUNT_ROOT",
    "TRACE_CONTAINER_ROOT",
    "NSYS_BIN",
    "NSYS_VERSION",
):
    source = f"A100_{suffix}"
    if source in os.environ:
        os.environ[source] = os.environ[source]

runpy.run_path(str(ROOT / "scripts/cloud/h100_nsight_trace_provider.py"), run_name="__main__")
