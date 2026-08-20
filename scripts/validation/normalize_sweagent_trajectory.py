#!/usr/bin/env python3
"""Losslessly index a pinned SWE-agent ``.traj`` JSON document.

SWE-agent 1.1.x stores a trajectory as one JSON document containing a
``trajectory`` step list, a ``history`` message list, and run metadata. The
normalizer never rewrites that source file. It emits an additive JSONL index
that keeps every source record and adds only deterministic structural joins.

When the corresponding SWE-agent trace log is supplied, anchored fields from
its Python representation are retained as separate ``model_call`` records.
The trace log is an observation of provider responses, not a request-level
clock or a vLLM request correlation source. The normalizer therefore keeps
provider usage and SWE-agent accounting in separate namespaces and never
fabricates monotonic timestamps.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = "sweagent-trajectory-normalized.v1"
SUPPORTED_AGENT_VERSION = "1.1.0"

_TRACE_RESPONSE_ID = re.compile(r"ModelResponse\(id='([^']+)'")
_TRACE_CREATED = re.compile(r"\bcreated=(\d+)")
_TRACE_MODEL = re.compile(r"\bmodel='([^']+)'")
_TRACE_FINISH_REASON = re.compile(r"finish_reason='([^']+)'")
_TRACE_TOOL_ID = re.compile(r"ChatCompletionMessageToolCall\(.*?\bid='([^']+)'")
_TRACE_USAGE = re.compile(
    r"usage=Usage\(completion_tokens=(\d+),\s*prompt_tokens=(\d+),\s*total_tokens=(\d+)"
)
_TRACE_LIMIT = re.compile(r"API calls\s+(\d+)\s+exceeds limit\s+(\d+)")
_TRACE_TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s+-\s+")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return _sha256_bytes(encoded)


def _event_id(source_sha256: str, record_type: str, sequence: int) -> str:
    value = f"{SCHEMA_VERSION}\0{source_sha256}\0{record_type}\0{sequence}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()[:32]


def _raw_record(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {"value": value}


def _history_action_indexes(history: list[Any]) -> list[int]:
    return [
        index
        for index, value in enumerate(history)
        if isinstance(value, Mapping)
        and value.get("role") == "assistant"
        and value.get("message_type") == "action"
    ]


def _history_tool_after(history: list[Any], action_index: int | None) -> int | None:
    if action_index is None:
        return None
    for index in range(action_index + 1, len(history)):
        value = history[index]
        if isinstance(value, Mapping) and value.get("role") == "tool":
            return index
        if isinstance(value, Mapping) and value.get("role") == "assistant" and value.get(
            "message_type"
        ) == "action":
            break
    return None


def _tool_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        item["id"]
        for item in value
        if isinstance(item, Mapping) and isinstance(item.get("id"), str) and item["id"]
    ]


def _string_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _structural_link(
    step: Mapping[str, Any], history: list[Any], step_index: int, action_indexes: list[int]
) -> dict[str, Any]:
    """Return links justified by the observed file structure only."""

    action_index = action_indexes[step_index] if step_index < len(action_indexes) else None
    tool_index = _history_tool_after(history, action_index)
    action_matches = False
    if action_index is not None:
        history_action = history[action_index]
        action_matches = (
            bool(step.get("action"))
            and isinstance(history_action, Mapping)
            and history_action.get("action") == step.get("action")
        )
    action_tool_ids = []
    if action_index is not None and isinstance(history[action_index], Mapping):
        action_tool_ids = _tool_ids(history[action_index].get("tool_calls"))
    observation_tool_ids = []
    if tool_index is not None and isinstance(history[tool_index], Mapping):
        observation_tool_ids = _string_ids(history[tool_index].get("tool_call_ids"))
    if action_tool_ids and observation_tool_ids != action_tool_ids:
        raise ValueError(
            "history assistant/tool call IDs do not form an exact adjacent structural pair"
        )
    return {
        "history_action_index": action_index,
        "history_tool_index": tool_index,
        "action_text_exact_match": action_matches,
        "tool_call_ids": action_tool_ids,
        "tool_observation_ids": observation_tool_ids,
        "request_id": None,
        "action_id": None,
        "confidence": (
            "structural_order" if action_index is not None and action_matches else "unavailable"
        ),
        "reason": (
            "SWE-agent trajectory has no request/action IDs; ordinal history link only"
            if action_index is not None and action_matches
            else "no exact action-bearing history record for this trajectory step"
        ),
    }


def _parse_trace_log(path: pathlib.Path) -> dict[str, Any]:
    """Parse stable anchored fields from a SWE-agent trace log.

    The trace log contains Python ``repr`` values rather than a structured
    interchange format. Every matched source line is retained in the
    corresponding model-call row; this summary contains only anchored IDs,
    usage counters, and the explicit call-limit warning.
    """

    if not path.is_file():
        raise ValueError(f"trace log does not exist: {path}")
    raw_bytes = path.read_bytes()
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"trace log is not readable UTF-8: {path}") from exc

    responses: list[dict[str, Any]] = []
    response_ids: list[str] = []
    tool_call_ids: list[str] = []
    provider_prompt_tokens = 0
    provider_completion_tokens = 0
    provider_total_tokens = 0
    usage_count = 0
    warning: dict[str, int] | None = None
    matched_line_hashes: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r\n")
        limit_match = _TRACE_LIMIT.search(line)
        if limit_match:
            warning = {
                "observed_api_calls": int(limit_match.group(1)),
                "configured_limit": int(limit_match.group(2)),
            }
        if "Response: ModelResponse(" not in line:
            continue
        response_match = _TRACE_RESPONSE_ID.search(line)
        if response_match is None:
            raise ValueError(f"trace response line has no anchored response ID: {path}")
        response_id = response_match.group(1)
        if response_id in response_ids:
            raise ValueError(f"duplicate trace response ID: {response_id}")
        response_ids.append(response_id)
        usage_match = _TRACE_USAGE.search(line)
        usage: dict[str, int | None]
        if usage_match is None:
            usage = {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}
        else:
            completion = int(usage_match.group(1))
            prompt = int(usage_match.group(2))
            total = int(usage_match.group(3))
            provider_prompt_tokens += prompt
            provider_completion_tokens += completion
            provider_total_tokens += total
            usage_count += 1
            usage = {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": total,
            }
        response_tool_ids = _TRACE_TOOL_ID.findall(line)
        for tool_id in response_tool_ids:
            if tool_id in tool_call_ids:
                raise ValueError(f"duplicate trace tool-call ID: {tool_id}")
            tool_call_ids.append(tool_id)
        timestamp = _TRACE_TIMESTAMP.search(line)
        line_sha256 = _sha256_bytes(line.encode("utf-8"))
        matched_line_hashes.append(line_sha256)
        created = _TRACE_CREATED.search(line)
        model = _TRACE_MODEL.search(line)
        finish_reason = _TRACE_FINISH_REASON.search(line)
        responses.append(
            {
                "sequence": len(responses),
                "provider_response_id": response_id,
                "provider_created_unix_s": int(created.group(1)) if created else None,
                "model": model.group(1) if model else None,
                "finish_reason": finish_reason.group(1) if finish_reason else None,
                "tool_call_ids": response_tool_ids,
                "usage": usage,
                "observed_at_log": timestamp.group(1) if timestamp else None,
                "raw_line": line,
                "raw_line_sha256": line_sha256,
            }
        )

    if not responses:
        raise ValueError(f"trace log contains no anchored ModelResponse lines: {path}")
    return {
        "path": str(path),
        "sha256": _sha256_bytes(raw_bytes),
        "bytes": len(raw_bytes),
        "responses": responses,
        "response_ids": response_ids,
        "tool_call_ids": tool_call_ids,
        "response_count": len(responses),
        "usage_count": usage_count,
        "provider_usage": {
            "prompt_tokens": provider_prompt_tokens if usage_count else None,
            "completion_tokens": provider_completion_tokens if usage_count else None,
            "total_tokens": provider_total_tokens if usage_count else None,
        },
        "api_limit_warning": warning is not None,
        "api_limit": warning,
        "matched_line_sha256": matched_line_hashes,
        "correlation": {
            "request_ids": "unavailable",
            "trajectory_links": "tool_call_id_only",
            "reason": (
                "trace IDs are not native vLLM request IDs and no request timestamps are present"
            ),
        },
    }


def _sweagent_usage(model_stats: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "input_tokens": model_stats.get("tokens_sent"),
        "output_tokens": model_stats.get("tokens_received"),
        "cumulative_input_tokens": model_stats.get("tokens_sent"),
        "cumulative_output_tokens": model_stats.get("tokens_received"),
        "api_calls": model_stats.get("api_calls"),
        "provenance": "measured_from_traj_info_model_stats",
    }


def _source_record(
    path: pathlib.Path, sha256: str, locator: str, record_sha256: str | None = None
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "artifact_path": str(path),
        "artifact_sha256": sha256,
        "locator": locator,
    }
    if record_sha256 is not None:
        value["record_sha256"] = record_sha256
    return value


def normalize_document(
    source: pathlib.Path,
    output: pathlib.Path,
    *,
    run_id: str,
    attempt_id: str,
    instance_id: str | None,
    trace_log: pathlib.Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    if not source.is_file():
        raise ValueError(f"trajectory source does not exist: {source}")
    if source.resolve() == output.resolve():
        raise ValueError("normalizer output must not overwrite the raw trajectory")
    if output.exists() and not force:
        raise ValueError(f"normalizer output exists; pass --force to replace: {output}")

    raw_bytes = source.read_bytes()
    try:
        document = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"trajectory is not one readable UTF-8 JSON document: {source}") from exc
    if not isinstance(document, Mapping):
        raise ValueError("trajectory root must be a JSON object")
    trajectory = document.get("trajectory")
    history = document.get("history")
    info = document.get("info")
    if (
        not isinstance(trajectory, list)
        or not isinstance(history, list)
        or not isinstance(info, Mapping)
    ):
        raise ValueError(
            "trajectory must contain list fields trajectory/history and object field info"
        )

    source_sha256 = _sha256_bytes(raw_bytes)
    action_indexes = _history_action_indexes(history)
    model_stats = info.get("model_stats") if isinstance(info.get("model_stats"), Mapping) else {}
    trace = _parse_trace_log(trace_log) if trace_log is not None else None
    api_calls = model_stats.get("api_calls")
    if trace is not None and api_calls is not None and trace["response_count"] != api_calls:
        raise ValueError(
            "trace ModelResponse count does not equal SWE-agent info.model_stats.api_calls: "
            f"{trace['response_count']} != {api_calls}"
        )

    step_links: list[dict[str, Any]] = []
    committed_tool_ids: list[str] = []
    for index, value in enumerate(trajectory):
        raw = _raw_record(value)
        link = _structural_link(raw, history, index, action_indexes)
        step_links.append(link)
        committed_tool_ids.extend(link["tool_call_ids"])
    if len(committed_tool_ids) != len(set(committed_tool_ids)):
        raise ValueError("duplicate committed tool-call IDs in trajectory history")

    trace_by_tool: dict[str, dict[str, Any]] = {}
    if trace is not None:
        for response in trace["responses"]:
            for tool_id in response["tool_call_ids"]:
                trace_by_tool[tool_id] = response
        missing = sorted(set(committed_tool_ids) - set(trace_by_tool))
        if missing:
            raise ValueError(f"committed tool-call IDs absent from trace log: {missing[:3]}")

    rows: list[dict[str, Any]] = []
    sweagent_usage = _sweagent_usage(model_stats)
    counts: dict[str, Any] = {
        "trajectory_steps": len(trajectory),
        "committed_trajectory_steps": sum(
            1 for value in trajectory if _raw_record(value).get("action")
        ),
        "terminal_trajectory_steps": sum(
            1
            for value in trajectory
            if _raw_record(value).get("response") == "Exit due to cost limit"
        ),
        "history_messages": len(history),
        "history_action_messages": len(action_indexes),
        "committed_tool_calls": len(committed_tool_ids),
        "model_stats_api_calls": api_calls,
    }
    if trace is not None:
        counts.update(
            {
                "trace_model_responses": trace["response_count"],
                "trace_tool_calls": len(trace["tool_call_ids"]),
            }
        )
    rows.append(
        {
            "schema_version": SCHEMA_VERSION,
            "record_type": "manifest",
            "event_type": "run_boundary",
            "event_id": _event_id(source_sha256, "manifest", 0),
            "run_id": run_id,
            "attempt_id": attempt_id,
            "instance_id": instance_id,
            "source": {
                "path": str(source),
                "sha256": source_sha256,
                "bytes": len(raw_bytes),
                "format": "swe-agent-traj-json-document",
                "raw_preserved": True,
            },
            "agent": {
                "name": "SWE-agent",
                "version": info.get("swe_agent_version"),
                "expected_version": SUPPORTED_AGENT_VERSION,
                "version_match": info.get("swe_agent_version") == SUPPORTED_AGENT_VERSION,
            },
            "counts": counts,
            "usage": {
                "provider": trace["provider_usage"] if trace else None,
                "sweagent": sweagent_usage,
            },
            "timing": {
                "request_level_timestamps": "unavailable",
                "monotonic_clock_id": None,
                "reason": (
                    "SWE-agent .traj and trace log expose reported duration/wall observations "
                    "only; no request monotonic timestamps"
                ),
            },
            "trace": (
                {key: value for key, value in trace.items() if key != "responses"}
                if trace
                else {"status": "not_supplied"}
            ),
            "correlation_status": "structural_only_no_request_id",
            "provenance": "measured",
        }
    )

    for index, value in enumerate(trajectory):
        raw = _raw_record(value)
        record_sha256 = _sha256_json(raw)
        rows.append(
            {
                "schema_version": SCHEMA_VERSION,
                "record_type": "trajectory_step",
                "event_type": "trajectory_step",
                "event_id": _event_id(source_sha256, "trajectory_step", index),
                "run_id": run_id,
                "attempt_id": attempt_id,
                "instance_id": instance_id,
                "sequence": index,
                "step_index": index,
                "raw_record_sha256": record_sha256,
                "raw_record": raw,
                "reported_execution_time_s": raw.get("execution_time"),
                "timing_provenance": (
                    "source_reported" if raw.get("execution_time") is not None else "unavailable"
                ),
                "correlation": step_links[index],
                "source_records": [
                    _source_record(source, source_sha256, f"/trajectory/{index}", record_sha256)
                ],
                "provenance": "measured",
            }
        )

    for index, value in enumerate(history):
        raw = _raw_record(value)
        record_sha256 = _sha256_json(raw)
        rows.append(
            {
                "schema_version": SCHEMA_VERSION,
                "record_type": "history_message",
                "event_type": "history_message",
                "event_id": _event_id(source_sha256, "history_message", index),
                "run_id": run_id,
                "attempt_id": attempt_id,
                "instance_id": instance_id,
                "sequence": index,
                "raw_record_sha256": record_sha256,
                "raw_record": raw,
                "role": raw.get("role"),
                "message_type": raw.get("message_type"),
                "tool_call_ids": raw.get("tool_call_ids", []),
                "source_records": [
                    _source_record(source, source_sha256, f"/history/{index}", record_sha256)
                ],
                "provenance": "measured",
            }
        )

    for index, response in enumerate(trace["responses"] if trace else []):
        matched_ids = [
            tool_id for tool_id in response["tool_call_ids"] if tool_id in committed_tool_ids
        ]
        if matched_ids:
            disposition = "committed"
            confidence = "exact_tool_call_id"
        elif (
            trace
            and trace["api_limit_warning"]
            and trace["api_limit"] is not None
            and index >= trace["api_limit"]["configured_limit"]
        ):
            disposition = "discarded_limit_exceeded"
            confidence = "structural_trace_order"
        else:
            disposition = "unlinked"
            confidence = "unavailable"
        rows.append(
            {
                "schema_version": SCHEMA_VERSION,
                "record_type": "model_call",
                "event_type": "model_call",
                "event_id": _event_id(source_sha256, "model_call", index),
                "run_id": run_id,
                "attempt_id": attempt_id,
                "instance_id": instance_id,
                "sequence": index,
                "step_index": committed_tool_ids.index(matched_ids[0]) if matched_ids else None,
                "disposition": disposition,
                "correlation": {
                    "provider_response_id": response["provider_response_id"],
                    "tool_call_ids": response["tool_call_ids"],
                    "request_id": None,
                    "confidence": confidence,
                    "reason": "provider response ID is not a native vLLM request ID",
                },
                "timing": {
                    "observed_at_log": response["observed_at_log"],
                    "provider_created_unix_s": response["provider_created_unix_s"],
                    "start_mono_ns": None,
                    "end_mono_ns": None,
                    "duration_ms": None,
                    "completeness": "observed_only" if response["observed_at_log"] else "none",
                    "source": (
                        "trace_log_wall_clock_observation"
                        if response["observed_at_log"]
                        else None
                    ),
                },
                "usage": {"provider": response["usage"], "sweagent": None},
                "payload": {"model": response["model"], "finish_reason": response["finish_reason"]},
                "source_records": [
                    {
                        "artifact_path": str(trace_log),
                        "artifact_sha256": trace["sha256"],
                        "locator": f"/ModelResponse/{index}",
                        "record_sha256": response["raw_line_sha256"],
                        "raw_line": response["raw_line"],
                    }
                ],
                "provenance": {"classification": "measured", "confidence": confidence},
            }
        )

    for index, value in enumerate(trajectory):
        raw = _raw_record(value)
        link = step_links[index]
        if not link["tool_call_ids"] or not link["action_text_exact_match"]:
            continue
        if len(link["tool_call_ids"]) != 1:
            raise ValueError(
                f"trajectory step {index} does not have exactly one structural tool call"
            )
        tool_id = link["tool_call_ids"][0]
        response = trace_by_tool.get(tool_id) if trace is not None else None
        tool_index = link["history_tool_index"]
        history_observation = history[tool_index] if tool_index is not None else None
        rows.append(
            {
                "schema_version": SCHEMA_VERSION,
                "record_type": "tool_execution",
                "event_type": "tool_execution",
                "event_id": _event_id(source_sha256, "tool_execution", index),
                "run_id": run_id,
                "attempt_id": attempt_id,
                "instance_id": instance_id,
                "sequence": index,
                "step_index": index,
                "disposition": "executed",
                "correlation": {
                    "model_call_index": response["sequence"] if response else None,
                    "provider_response_id": response["provider_response_id"] if response else None,
                    "tool_call_id": tool_id,
                    "request_id": None,
                    "confidence": "exact_tool_call_id" if response else "structural_order",
                },
                "timing": {
                    "observed_at_utc": None,
                    "start_mono_ns": None,
                    "end_mono_ns": None,
                    "duration_ms": (
                        float(raw["execution_time"]) * 1000
                        if isinstance(raw.get("execution_time"), (int, float))
                        else None
                    ),
                    "reported_execution_time_s": raw.get("execution_time"),
                    "completeness": (
                        "duration_only" if raw.get("execution_time") is not None else "none"
                    ),
                    "source": (
                        "trajectory_execution_time"
                        if raw.get("execution_time") is not None
                        else None
                    ),
                },
                "usage": {"provider": None, "sweagent": None},
                "payload": {
                    "rendered_action": raw.get("action"),
                    "raw_trajectory_observation": raw.get("observation"),
                    "rendered_history_observation": (
                        history_observation.get("content")
                        if isinstance(history_observation, Mapping)
                        else None
                    ),
                    "raw_history_record": history_observation,
                },
                "source_records": [
                    _source_record(source, source_sha256, f"/trajectory/{index}"),
                    _source_record(source, source_sha256, f"/history/{tool_index}"),
                ],
                "provenance": {
                    "classification": "measured",
                    "confidence": "exact_tool_call_id" if response else "structural",
                },
            }
        )

    for index, value in enumerate(trajectory):
        raw = _raw_record(value)
        if raw.get("action") == "" and raw.get("response") == "Exit due to cost limit":
            rows.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "record_type": "agent_terminal",
                    "event_type": "agent_terminal",
                    "event_id": _event_id(source_sha256, "agent_terminal", index),
                    "run_id": run_id,
                    "attempt_id": attempt_id,
                    "instance_id": instance_id,
                    "sequence": index,
                    "step_index": index,
                    "disposition": "terminal",
                    "terminal_reason": raw.get("response"),
                    "timing": {
                        "duration_ms": 0.0,
                        "start_mono_ns": None,
                        "end_mono_ns": None,
                        "completeness": "duration_only",
                        "source": "trajectory_execution_time",
                    },
                    "payload": {"raw_record": raw},
                    "source_records": [
                        _source_record(source, source_sha256, f"/trajectory/{index}")
                    ],
                    "provenance": {"classification": "measured", "confidence": "exact"},
                }
            )

    rows.append(
        {
            "schema_version": SCHEMA_VERSION,
            "record_type": "info",
            "event_type": "run_boundary",
            "event_id": _event_id(source_sha256, "info", 0),
            "run_id": run_id,
            "attempt_id": attempt_id,
            "instance_id": instance_id,
            "raw_record_sha256": _sha256_json(info),
            "raw_record": info,
            "usage": {
                "provider": trace["provider_usage"] if trace else None,
                "sweagent": sweagent_usage,
            },
            "replay_config_sha256": _sha256_json(document.get("replay_config")),
            "environment": document.get("environment"),
            "source_records": [_source_record(source, source_sha256, "/info")],
            "provenance": "measured",
        }
    )
    if trace is not None:
        rows.append(
            {
                "schema_version": SCHEMA_VERSION,
                "record_type": "trace_summary",
                "event_type": "run_boundary",
                "event_id": _event_id(source_sha256, "trace_summary", 0),
                "run_id": run_id,
                "attempt_id": attempt_id,
                "instance_id": instance_id,
                "trace": {key: value for key, value in trace.items() if key != "responses"},
                "provenance": "measured",
            }
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    encoded = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for row in rows
    )
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(encoded, encoding="utf-8")
    temporary.replace(output)
    return {
        "schema_version": SCHEMA_VERSION,
        "source_sha256": source_sha256,
        "source_bytes": len(raw_bytes),
        "trace_sha256": trace["sha256"] if trace else None,
        "output": str(output),
        "records": len(rows),
        "trajectory_steps": len(trajectory),
        "history_messages": len(history),
        "model_calls": len(trace["responses"]) if trace else 0,
        "tool_executions": sum(1 for row in rows if row.get("record_type") == "tool_execution"),
        "terminal_events": sum(1 for row in rows if row.get("record_type") == "agent_terminal"),
        "structural_links": sum(
            1
            for row in rows
            if row.get("record_type") == "trajectory_step"
            and row.get("correlation", {}).get("confidence") == "structural_order"
        ),
        "request_level_timestamps": "unavailable",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", required=True, type=pathlib.Path, help="raw SWE-agent .traj JSON document"
    )
    parser.add_argument("--trace-log", type=pathlib.Path, help="optional SWE-agent trace log")
    parser.add_argument(
        "--output", required=True, type=pathlib.Path, help="additive normalized JSONL output"
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--instance-id")
    parser.add_argument("--force", action="store_true")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = normalize_document(
            args.input,
            args.output,
            run_id=args.run_id,
            attempt_id=args.attempt_id,
            instance_id=args.instance_id,
            trace_log=args.trace_log,
            force=args.force,
        )
    except (OSError, TypeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
