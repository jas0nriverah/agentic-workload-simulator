#!/usr/bin/env python3
"""A100 adapter for the reviewed, serialized feature-validation runner.

The implementation is shared with the frozen H100 runner.  This adapter only
maps the target-specific environment and schemas; it never reads H100 labels
or artifacts and it cannot select an H100 protocol.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

os.environ["HARDWARE_TARGET"] = "A100"
os.environ["HARDWARE_ROW_SCHEMA"] = "a100-final-row.v1"
os.environ["HARDWARE_TRACE_SCHEMA"] = "a100-trace-summary.v1"
os.environ["HARDWARE_TRACE_PROVIDER_VERSION"] = "a100-nsight-trace-provider.v1"

for suffix in (
    "RUNNER_TEST_MODE",
    "TEST_SERVER_URL",
    "VLLM_BASE_URL",
    "VLLM_MODEL",
    "TEST_HARDWARE_JSON",
    "TRACE_PROVIDER",
    "MODEL_SNAPSHOT",
    "NSYS_CONTAINER",
    "NSYS_SESSION",
    "TRACE_MOUNT_ROOT",
    "TRACE_CONTAINER_ROOT",
    "NSYS_BIN",
    "NSYS_VERSION",
    "EXPECTED_SERVER_CONTAINER",
    "TEST_REQUEST_SPACING_SECONDS",
):
    source = f"A100_{suffix}"
    target = f"H100_{suffix}"
    if source in os.environ:
        os.environ[target] = os.environ[source]

from scripts.cloud.h100_case_runner import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
