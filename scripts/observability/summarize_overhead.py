#!/usr/bin/env python3
"""Summarize paired control/profile overhead without claiming GPU time."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from agentic_sim.observability.overhead import summarize_overhead_records  # noqa: E402


def _load(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return []
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        if not all(isinstance(row, dict) for row in rows):
            raise SystemExit(f"{path} must contain JSON objects")
        return rows
    if isinstance(value, dict) and isinstance(value.get("runs"), list):
        value = value["runs"]
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list) and all(isinstance(row, dict) for row in value):
        return value
    raise SystemExit(f"{path} must be an object, object list, or JSONL")


def _write(path: Path, value: dict[str, Any], *, force: bool) -> None:
    if path.exists() and not force:
        raise SystemExit(f"refusing to overwrite derived report: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile-kind", default="profile")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.dry_run:
        print(f"DRY-RUN: pair same-config control={args.control} profile={args.profile}; write derived medians/p90/overhead to {args.output}")
        print("DRY-RUN: require observability_level, profilers_enabled, and instrumentation_version on every run; no GPU-time claims")
        return 0
    report = summarize_overhead_records(_load(args.control), _load(args.profile), profile_kind=args.profile_kind)
    _write(args.output, report, force=args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
