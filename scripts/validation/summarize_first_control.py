#!/usr/bin/env python3
"""Produce a deterministic, payload-free summary of a normalized control run."""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys
from collections import Counter
from typing import Any, Iterable

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from validate_normalized_trajectory import _read_rows, validate_rows  # noqa: E402


def _percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent / 100.0
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _stats(values: Iterable[float]) -> dict[str, Any]:
    numbers = [float(value) for value in values]
    if any(not math.isfinite(value) for value in numbers):
        raise ValueError("execution-time samples must be finite")
    return {
        "count": len(numbers),
        "sum": sum(numbers),
        "mean": (sum(numbers) / len(numbers)) if numbers else None,
        "median": _percentile(numbers, 50.0),
        "p90": _percentile(numbers, 90.0),
        "min": min(numbers) if numbers else None,
        "max": max(numbers) if numbers else None,
    }


def _action_family(action: Any) -> str:
    if not isinstance(action, str) or not action.strip():
        return "unavailable"
    return action.split(None, 1)[0]


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    validation = validate_rows(rows)
    manifest = next(row for row in rows if row.get("record_type") == "manifest")
    info = next(row for row in rows if row.get("record_type") == "info")
    steps = [row for row in rows if row.get("record_type") == "trajectory_step"]
    tools = [row for row in rows if row.get("record_type") == "tool_execution"]
    calls = [row for row in rows if row.get("record_type") == "model_call"]
    execution_seconds = [
        float(row["reported_execution_time_s"])
        for row in steps
        if isinstance(row.get("reported_execution_time_s"), (int, float))
        and not isinstance(row.get("reported_execution_time_s"), bool)
    ]
    tool_seconds = [
        float(row["timing"]["reported_execution_time_s"])
        for row in tools
        if isinstance(row.get("timing", {}).get("reported_execution_time_s"), (int, float))
        and not isinstance(row.get("timing", {}).get("reported_execution_time_s"), bool)
    ]
    dispositions = Counter(str(row.get("disposition", "unavailable")) for row in calls)
    actions = Counter(_action_family(row.get("payload", {}).get("rendered_action")) for row in tools)
    terminal = [row for row in rows if row.get("record_type") == "agent_terminal"]
    return {
        "schema_version": "sweagent.first-control-summary.v1",
        "status": "measured",
        "provenance": "derived_from_measured_normalized_fixture",
        "source_sha256": manifest["source"]["sha256"],
        "run_id": manifest.get("run_id"),
        "attempt_id": manifest.get("attempt_id"),
        "instance_id": manifest.get("instance_id"),
        "agent": manifest.get("agent"),
        "counts": {
            "trajectory_steps": len(steps),
            "tool_executions": len(tools),
            "model_calls": len(calls),
            "terminal_events": len(terminal),
            "history_messages": len([row for row in rows if row.get("record_type") == "history_message"]),
        },
        "model_call_dispositions": dict(sorted(dispositions.items())),
        "tool_action_families": dict(sorted(actions.items())),
        "reported_execution_time_s": _stats(execution_seconds),
        "reported_tool_execution_time_s": _stats(tool_seconds),
        "token_accounting": manifest.get("usage"),
        "agent_exit_status": info.get("raw_record", {}).get("exit_status"),
        "submission_present": info.get("raw_record", {}).get("submission") is not None,
        "timing_boundary": {
            "request_level_timestamps": "unavailable",
            "monotonic_intervals": "unavailable",
            "execution_time_scope": "SWE-agent trajectory-reported duration only",
            "gpu_time_claim": False,
        },
        "validation": validation,
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=pathlib.Path)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    parser.add_argument("--expected-source-sha256")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        rows = _read_rows(args.input)
        if args.expected_source_sha256:
            validate_rows(rows, expected_source_sha256=args.expected_source_sha256)
        result = summarize(rows)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if args.output.exists() and not args.force:
        print(f"ERROR: refusing to overwrite summary: {args.output}", file=sys.stderr)
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(args.output.name + ".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
