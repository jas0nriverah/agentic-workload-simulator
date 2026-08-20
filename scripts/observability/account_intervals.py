#!/usr/bin/env python3
"""Derive reset-safe interval-union accounting from a JSONL event stream."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from agentic_sim.observability.accounting import summarize_interval_union  # noqa: E402


def _load(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"JSONL line {number} is not an object")
        rows.append(value)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.dry_run:
        print(f"DRY-RUN: merge same-clock timed intervals from {args.input} -> {args.output}")
        print("DRY-RUN: reject cross-host/boot merges; report overlap, gaps, and coverage; make no GPU-time claim")
        return 0
    try:
        result = summarize_interval_union(_load(args.input))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if args.output.exists() and not args.force:
        print(f"ERROR: refusing to overwrite derived report: {args.output}", file=sys.stderr)
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(args.output.name + ".tmp")
    temporary.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "derived" else 3


if __name__ == "__main__":
    raise SystemExit(main())
