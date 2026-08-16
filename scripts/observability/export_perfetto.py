#!/usr/bin/env python3
"""Export selected normalized events to a deterministic Perfetto-compatible JSON trace."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from agentic_sim.observability.perfetto import export_perfetto_trace  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--gpu-samples", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--attempt-id")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.dry_run:
        print(f"DRY-RUN: normalize monotonic events from {args.events} and optional GPU samples; write visualization-only trace to {args.output}")
        print("DRY-RUN: payloads/prompts/commands are excluded; raw JSONL remains the source of truth")
        return 0
    export_perfetto_trace(
        args.events,
        gpu_samples_path=args.gpu_samples,
        output_path=args.output,
        run_id=args.run_id,
        attempt_id=args.attempt_id,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
