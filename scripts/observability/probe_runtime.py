#!/usr/bin/env python3
"""Record optional observability capabilities without installing anything."""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from agentic_sim.observability.gpu import (  # noqa: E402
    collect_nvidia_smi_hardware,
    discover_dcgmi_capability,
    discover_dcgmi_fields,
)
from agentic_sim.observability.profilers import discover_tool_capabilities  # noqa: E402
from agentic_sim.telemetry.clock import clock_fields  # noqa: E402


def _version(command: list[str]) -> str | None:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError, TimeoutError):
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or result.stderr).strip()[:500] or None


def _write(path: Path, value: dict) -> None:
    if path.exists():
        raise SystemExit(f"refusing to overwrite capability report: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scope", default="host_capability")
    parser.add_argument("--vllm-bin", default="vllm")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.dry_run:
        print("DRY-RUN: probe nvidia-smi, optional dcgmi/nsys/strace/py-spy, and pinned vLLM CLI help; no installs or GPU work")
        print(f"DRY-RUN: write hardware/capability manifest to {args.output}")
        return 0
    hardware = collect_nvidia_smi_hardware(scope=args.scope)
    dcgm = discover_dcgmi_capability(scope=args.scope)
    dcgm_fields = discover_dcgmi_fields(scope=args.scope)
    tools = discover_tool_capabilities(scope=args.scope)
    vllm_path = shutil.which(args.vllm_bin)
    vllm = {
        "executable": vllm_path,
        "status": "unavailable" if not vllm_path else "installed_unverified",
        "provenance": "unavailable" if not vllm_path else "measured",
        "version": _version([args.vllm_bin, "--version"]) if vllm_path else None,
        "help_flags": {},
    }
    if vllm_path:
        for label, command in {
            "root": [args.vllm_bin, "--help"],
            "bench": [args.vllm_bin, "bench", "--help"],
            "bench_serve": [args.vllm_bin, "bench", "serve", "--help"],
        }.items():
            text = _version(command) or ""
            vllm["help_flags"][label] = {
                "status": "measured" if text else "unavailable",
                "contains_bench_serve": "bench serve" in text or label == "bench_serve",
                "contains_otlp": "--otlp-traces-endpoint" in text,
                "contains_detailed_traces": "--collect-detailed-traces" in text,
                "text_sha256": __import__("hashlib").sha256(text.encode()).hexdigest() if text else None,
            }
    value = {
        "schema_version": "observability.hardware-capabilities.v1",
        "status": "measured" if hardware.get("status") == "measured" else "unavailable",
        "provenance": "measured" if hardware.get("status") == "measured" else "unavailable",
        "scope": args.scope,
        "captured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "clock": clock_fields(),
        "platform": {"system": platform.system(), "release": platform.release(), "machine": platform.machine()},
        "hardware": hardware,
        "dcgm": dcgm,
        "dcgm_fields": dcgm_fields,
        "tools": tools,
        "vllm": vllm,
        "notes": [
            "nvidia-smi fields are direct host observations, not per-request GPU time",
            "DCGM fields remain unverified until an explicit host probe",
            "OTLP is not enabled by this probe or the frozen first-session launcher",
        ],
    }
    _write(args.output, value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
