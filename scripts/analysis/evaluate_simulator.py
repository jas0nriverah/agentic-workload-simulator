#!/usr/bin/env python3
"""Fit and evaluate the assignment simulator from explicit decomposed records.

The input files must contain JSON arrays whose records include
``observed_seconds``, ``cpu_seconds``, and ``gpu_seconds_at_reference``.  The
script intentionally refuses aggregate-only vLLM metrics; a GPU component
must have already been measured or calibrated by a defensible boundary.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agentic_sim.simulator import HardwareLatencySimulator


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train", type=Path, required=True, help="JSON array of calibration records")
    parser.add_argument("--holdout", type=Path, required=True, help="JSON array of held-out records")
    parser.add_argument("--target-score", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    train = json.loads(args.train.read_text(encoding="utf-8"))
    holdout = json.loads(args.holdout.read_text(encoding="utf-8"))
    model = HardwareLatencySimulator.fit(train)
    result = model.evaluate(holdout, target_score=args.target_score)
    result["calibration"] = {
        "fixed_seconds": model.fixed_seconds,
        "reference_score": model.reference_score,
        "training_run_ids": list(model.training_run_ids),
        "train_source": args.train.as_posix(),
        "holdout_source": args.holdout.as_posix(),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
