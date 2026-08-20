#!/usr/bin/env python3
"""Fail-closed validation for an additive SWE-agent normalized JSONL index.

The validator checks only claims proved by the normalized file. It does not
infer request timestamps, vLLM request IDs, or timing relationships absent from
the source fixture.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
from collections import Counter
from typing import Any, Iterable, Mapping

SCHEMA_VERSION = "sweagent-trajectory-normalized.v1"


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _read_rows(path: pathlib.Path) -> list[dict[str, Any]]:
    _require(path.is_file(), f"normalized JSONL does not exist: {path}")
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        _require(bool(line.strip()), f"blank line at JSONL line {line_number}")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON at JSONL line {line_number}: {exc}") from exc
        _require(isinstance(value, dict), f"JSONL line {line_number} is not an object")
        rows.append(value)
    _require(bool(rows), "normalized JSONL is empty")
    return rows


def _source_hashes(rows: Iterable[Mapping[str, Any]]) -> set[str]:
    hashes: set[str] = set()
    for row in rows:
        source = row.get("source")
        if isinstance(source, Mapping) and isinstance(source.get("sha256"), str):
            hashes.add(source["sha256"])
        for record in row.get("source_records", []):
            if isinstance(record, Mapping) and isinstance(record.get("artifact_sha256"), str):
                hashes.add(record["artifact_sha256"])
    return hashes


def validate_rows(rows: list[dict[str, Any]], *, expected_source_sha256: str | None = None) -> dict[str, Any]:
    types = Counter(row.get("record_type") for row in rows)
    _require(types["manifest"] == 1, "normalized index must contain exactly one manifest")
    manifest = next(row for row in rows if row.get("record_type") == "manifest")
    _require(manifest.get("schema_version") == SCHEMA_VERSION, "unsupported schema version")
    source = manifest.get("source")
    _require(isinstance(source, Mapping), "manifest source is missing")
    source_sha = source.get("sha256")
    _require(isinstance(source_sha, str) and len(source_sha) == 64, "invalid manifest source SHA-256")
    if expected_source_sha256 is not None:
        _require(source_sha == expected_source_sha256, "manifest source SHA-256 does not match expectation")

    event_ids = [row.get("event_id") for row in rows]
    _require(all(isinstance(value, str) and value for value in event_ids), "every row needs an event_id")
    _require(len(event_ids) == len(set(event_ids)), "duplicate event_id in normalized index")
    allowed_source_hashes = {source_sha}
    trace = manifest.get("trace")
    if isinstance(trace, Mapping) and isinstance(trace.get("sha256"), str):
        allowed_source_hashes.add(trace["sha256"])
    _require(
        _source_hashes(rows) <= allowed_source_hashes,
        "normalized rows reference an unlisted source artifact",
    )

    counts = manifest.get("counts")
    _require(isinstance(counts, Mapping), "manifest counts are missing")
    expected_rows = {
        "trajectory_step": counts.get("trajectory_steps"),
        "history_message": counts.get("history_messages"),
        "model_call": counts.get("trace_model_responses", 0),
        "tool_execution": counts.get("committed_tool_calls", 0),
        "agent_terminal": counts.get("terminal_trajectory_steps", 0),
    }
    for record_type, expected in expected_rows.items():
        _require(isinstance(expected, int) and types[record_type] == expected,
                 f"{record_type} count mismatch: rows={types[record_type]} manifest={expected}")
    _require(types["info"] == 1, "normalized index must contain exactly one info row")
    _require(types["trace_summary"] in (0, 1), "normalized index has duplicate trace summaries")

    for row in rows:
        _require(row.get("schema_version") == SCHEMA_VERSION, "row has unsupported schema version")
        for record in row.get("source_records", []):
            _require(isinstance(record, Mapping), "source_records contains a non-object")
            _require(record.get("artifact_sha256") in allowed_source_hashes, "source record hash mismatch")

    for row in rows:
        if row.get("record_type") in {"trajectory_step", "history_message", "info"}:
            raw = row.get("raw_record")
            _require(row.get("raw_record_sha256") == _sha256_json(raw),
                     f"{row['record_type']} raw record hash mismatch")

    model_rows = [row for row in rows if row.get("record_type") == "model_call"]
    response_ids: set[str] = set()
    trace_tool_ids: set[str] = set()
    for row in model_rows:
        correlation = row.get("correlation")
        _require(isinstance(correlation, Mapping), "model_call correlation is missing")
        response_id = correlation.get("provider_response_id")
        _require(isinstance(response_id, str) and response_id not in response_ids,
                 "model_call response IDs must be unique")
        response_ids.add(response_id)
        timing = row.get("timing")
        _require(isinstance(timing, Mapping), "model_call timing is missing")
        _require(timing.get("start_mono_ns") is None and timing.get("end_mono_ns") is None
                 and timing.get("duration_ms") is None,
                 "normalizer must not invent model-call monotonic timing")
        _require(correlation.get("request_id") is None, "normalizer must not claim a vLLM request ID")
        for tool_id in correlation.get("tool_call_ids", []):
            _require(isinstance(tool_id, str) and tool_id not in trace_tool_ids,
                     "trace tool-call IDs must be unique")
            trace_tool_ids.add(tool_id)
        source_records = row.get("source_records")
        _require(isinstance(source_records, list) and source_records,
                 "model_call lacks trace provenance")
        first_source = source_records[0]
        _require(isinstance(first_source, Mapping), "model_call trace provenance is malformed")
        raw_line = first_source.get("raw_line")
        if raw_line is not None:
            _require(first_source.get("record_sha256") == _sha256_bytes(raw_line.encode("utf-8")),
                     "trace raw-line hash mismatch")

    tool_rows = [row for row in rows if row.get("record_type") == "tool_execution"]
    tool_ids: set[str] = set()
    for row in tool_rows:
        correlation = row.get("correlation")
        _require(isinstance(correlation, Mapping), "tool_execution correlation is missing")
        tool_id = correlation.get("tool_call_id")
        _require(isinstance(tool_id, str) and tool_id not in tool_ids,
                 "tool execution IDs must be unique")
        tool_ids.add(tool_id)
        _require(correlation.get("request_id") is None, "normalizer must not claim a tool request ID")
        timing = row.get("timing")
        _require(isinstance(timing, Mapping), "tool_execution timing is missing")
        _require(timing.get("start_mono_ns") is None and timing.get("end_mono_ns") is None,
                 "normalizer must not invent tool monotonic timing")

    if types["trace_summary"]:
        _require(isinstance(trace, Mapping) and trace.get("response_count") == len(model_rows),
                 "trace summary/manifest response count mismatch")
    _require(manifest.get("timing", {}).get("request_level_timestamps") == "unavailable",
             "manifest must declare request-level timestamps unavailable")
    return {
        "schema_version": SCHEMA_VERSION,
        "source_sha256": source_sha,
        "records": len(rows),
        "record_counts": dict(sorted(types.items(), key=lambda item: str(item[0]))),
        "model_calls": len(model_rows),
        "tool_executions": len(tool_rows),
        "request_level_timestamps": "unavailable",
        "status": "PASS",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=pathlib.Path)
    parser.add_argument("--expected-source-sha256")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = validate_rows(_read_rows(args.input), expected_source_sha256=args.expected_source_sha256)
    except (OSError, TypeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
