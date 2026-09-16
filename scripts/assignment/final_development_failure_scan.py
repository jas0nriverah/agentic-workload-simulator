#!/usr/bin/env python3
"""Bounded case-level mining of permitted SWE-bench development histories.

This worker is deliberately narrower than the historical improvement review.
It uses the frozen scope before resolving any raw source path or reading the
classification labels, then scans only execution-worker evidence for a finite
set of failure modes:

* repeated actions with repeated observations;
* exact editor no-op/malformed replacement results;
* model-output truncation;
* context-window termination after local validation;
* actual worker timeout markers versus timeout flag false positives; and
* proxy status/error sequences with request-boundary lineage.

It does not run inference, GPU work, evaluator work, fitting, or candidate
selection. Existing output paths are refused so every run has a new artifact.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import historical_analysis_scope as scope_lib


INDEX_REL = (
    "submission/20260908T140000Z-offline-v2/"
    "configuration-analysis/TERMINATION_EVIDENCE_INDEX.json"
)
LABELS_REL = (
    "submission/20260908T010000Z/"
    "audit-step1-resolved-rates-readonly/classifications.csv"
)

FIXED_MODEL_REVISION = "b2cff646eb4bb1d68355c01b18ae02e7cf42d120"
FIXED_OUTPUT_TOKENS = 2048
FIXED_TEMPERATURE = 0.0
FIXED_TOP_P = 1.0
FIXED_SEED = 0
FIXED_SERVING_MAX_MODEL_LEN = 65536
FIXED_PANEL_INSTANCES = 24
FIXED_PANEL_TRAJECTORIES = 96

# These exact cases are already covered by the prior historical report's
# empty-patch/editor-install forensic finding. They remain in the permitted
# source index, but cannot be counted as new findings here.
PRIOR_COVERED_CASE_IDS = frozenset(
    {
        "assignment-case-v1:10cfac47a2f380898d6c9e4d408296e206a54f27c41948645ca2b7bd8fa8588f",
        "assignment-case-v1:ac09ec8a75d626a3a4e75ac6202a0bb4a472b648a2c6f975c1c312a9b485c2a8",
        "assignment-case-v1:2af04a5c1885fbf3a5f8a89375deef88df6b18d8dd315ad1d26ab44345516481",
    }
)

EDITOR_FAILURE_PATTERNS = (
    re.compile(r"No replacement was performed", re.IGNORECASE),
    re.compile(r"old_str .* did not appear", re.IGNORECASE),
    re.compile(r"old_str .* not found", re.IGNORECASE),
    re.compile(r"No match found", re.IGNORECASE),
    re.compile(r"Failed to (?:edit|replace)", re.IGNORECASE),
    re.compile(r"Invalid (?:request|action|edit)", re.IGNORECASE),
)

# These markers are accepted only when they look like a worker/tool result.
# Source snippets frequently contain TimeoutExpired or print("Command timed
# out"), and those are intentionally rejected by actual_timeout_markers().
WORKER_TIMEOUT_LINE = re.compile(
    r"^(?:command|process|tool)\s+(?:was\s+)?timed\s+out"
    r"(?:\s+after\s+\d+(?:\.\d+)?\s*(?:s|sec(?:ond)?s?))?[.!]?$",
    re.IGNORECASE,
)
EXIT_TIMEOUT_LINE = re.compile(
    r"\b(?:exit|return)\s+code\s+(?:124|137)\b", re.IGNORECASE
)
CONTEXT_EXIT = re.compile(r"Exit due to context window", re.IGNORECASE)
POSITIVE_TEST = re.compile(
    r"(?<!not )\b(?:\d+\s+)?(?:tests?|checks?|cases?)\s+(?:passed|pass)\b"
    r"|\b(?:all|targeted|focused|comprehensive)\b.{0,100}\bpass(?:ed|es)?\b",
    re.IGNORECASE,
)
NEGATIVE_TEST = re.compile(
    r"\b(?:failed|failure|traceback|assertionerror|error:)\b", re.IGNORECASE
)


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def compact(value: Any, limit: int = 280) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def truth(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def resolved(label: Mapping[str, Any]) -> bool:
    return truth(label.get("actual_official_resolved"))


def read_json(path: Path) -> tuple[Any, str]:
    raw = path.read_bytes()
    return json.loads(raw), sha256_bytes(raw)


def read_labels(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def resolve_source_path(assignment_root: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    return path if path.is_absolute() else assignment_root / path


def verified_source(
    assignment_root: Path, descriptor: Mapping[str, Any]
) -> tuple[Path, bytes]:
    raw_path = descriptor.get("path")
    expected = descriptor.get("sha256")
    if not isinstance(raw_path, str) or not isinstance(expected, str):
        raise ValueError("source descriptor missing path/sha256")
    path = resolve_source_path(assignment_root, raw_path)
    raw = path.read_bytes()
    actual = sha256_bytes(raw)
    if actual != expected:
        raise ValueError(f"source hash mismatch: {path}: {actual} != {expected}")
    return path, raw


def history_from_payload(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    history = payload.get("trajectory") or payload.get("history")
    if not isinstance(history, list):
        raise ValueError("trajectory payload has no list history")
    return history


def load_case_history(
    assignment_root: Path, row: Mapping[str, Any]
) -> dict[str, Any]:
    descriptors = row.get("source_hashes")
    if not isinstance(descriptors, Mapping):
        raise ValueError(f"case has no source_hashes: {row.get('case_id')}")
    path, raw = verified_source(
        assignment_root, descriptors["trajectory"]  # type: ignore[index]
    )
    payload = json.loads(raw)
    history = history_from_payload(payload)
    return {
        "case_id": row["case_id"],
        "instance_id": row["instance_id"],
        "index_row": row,
        "trajectory_path": str(path),
        "trajectory_sha256": sha256_bytes(raw),
        "history": history,
    }


def action(event: Mapping[str, Any]) -> str:
    return str(event.get("action") or "")


def observation(event: Mapping[str, Any]) -> str:
    return str(event.get("observation") or "")


def response(event: Mapping[str, Any]) -> str:
    return str(event.get("response") or "")


def editor_mutation(action_text: str) -> bool:
    lowered = action_text.lower()
    if "str_replace_editor" not in lowered:
        return False
    return " str_replace " in lowered or " create " in lowered


def actual_timeout_markers(text: str) -> list[str]:
    markers: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Reject code, patch, and source-display lines. They account for most
        # timeout flag false positives in these histories.
        code_cue = (
            "timeoutexpired" in line.lower()
            or "subprocess." in line.lower()
            or "print(" in line.lower()
            or "except " in line.lower()
            or "timeout=" in line.lower()
            or "timeout:" in line.lower()
            or line.startswith(("+", "-", ">>>"))
        )
        if code_cue:
            continue
        if WORKER_TIMEOUT_LINE.fullmatch(line) or EXIT_TIMEOUT_LINE.search(line):
            markers.append(line)
    return markers


def load_labels_after_scope(
    scope: scope_lib.HistoricalScope,
    labels_path: Path,
    seed_identity: Mapping[str, Any],
) -> list[dict[str, str]]:
    # The file read is inside the approved loader. No label file is opened
    # until frozen_scope() and the identity gate above have succeeded.
    bundles = list(
        scope.load_eligible(
            [seed_identity],
            lambda _identity: read_labels(labels_path),
        )
    )
    if len(bundles) != 1:
        raise ValueError("unable to open labels through eligible loader")
    rows = bundles[0]
    return list(scope.load_eligible(rows, lambda row: row))


def build_case_maps(
    eligible_index: list[dict[str, Any]],
    eligible_labels: list[dict[str, str]],
) -> dict[str, dict[str, Any]]:
    labels_by_case: dict[str, dict[str, str]] = {}
    for label in eligible_labels:
        case_id = label.get("resume_key")
        if not case_id:
            raise ValueError("classification row has no resume_key")
        if case_id in labels_by_case:
            raise ValueError(f"duplicate classification case: {case_id}")
        labels_by_case[case_id] = label
    cases: dict[str, dict[str, Any]] = {}
    for row in eligible_index:
        case_id = row.get("case_id")
        if not case_id or case_id not in labels_by_case:
            raise ValueError(f"index/label join missing: {case_id}")
        cases[case_id] = {"index": row, "label": labels_by_case[case_id]}
    if len(cases) != len(eligible_index):
        raise ValueError("duplicate eligible case IDs in termination index")
    return cases


def editor_failures(
    histories: Mapping[str, Mapping[str, Any]],
    cases: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    events: list[dict[str, Any]] = []
    per_case: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case_id, source in histories.items():
        if case_id in PRIOR_COVERED_CASE_IDS:
            continue
        label = cases[case_id]["label"]
        history = source["history"]
        for step, event in enumerate(history):
            action_text = action(event)
            if (
                "str_replace_editor" not in action_text
                and "str_replace" not in action_text
            ):
                continue
            obs = observation(event)
            match = next(
                (pattern.search(obs) for pattern in EDITOR_FAILURE_PATTERNS),
                None,
            )
            if not match:
                continue
            item = {
                "case_id": case_id,
                "instance_id": source["instance_id"],
                "official_resolved": resolved(label),
                "step": step,
                "match": match.group(0)[:220],
                "action_sha256": sha256_text(action_text),
                "observation_sha256": sha256_text(obs),
                "observation_snippet": compact(obs),
            }
            events.append(item)
            per_case[case_id].append(item)
    case_rows = []
    for case_id, items in per_case.items():
        source = histories[case_id]
        case_rows.append(
            {
                "case_id": case_id,
                "instance_id": source["instance_id"],
                "official_resolved": items[0]["official_resolved"],
                "event_count": len(items),
                "trajectory_sha256": source["trajectory_sha256"],
                "events": items,
            }
        )
    case_rows.sort(
        key=lambda item: (
            item["official_resolved"],
            -item["event_count"],
            item["instance_id"],
            item["case_id"],
        )
    )
    unresolved = sum(not item["official_resolved"] for item in case_rows)
    summary = {
        "new_editor_failure_event_count": len(events),
        "new_editor_failure_case_count": len(case_rows),
        "unresolved_case_count": unresolved,
        "resolved_control_case_count": len(case_rows) - unresolved,
        "prior_covered_cases_excluded": len(PRIOR_COVERED_CASE_IDS),
        "representative_cases": representative_editor_cases(case_rows),
    }
    return events, summary


def representative_editor_cases(
    case_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    preferred_instances = (
        "django__django-11532",
        "pytest-dev__pytest-8365",
        "django__django-13590",
    )
    selected: list[dict[str, Any]] = []
    used: set[str] = set()
    for instance_id in preferred_instances:
        matches = [
            item
            for item in case_rows
            if item["instance_id"] == instance_id and not item["official_resolved"]
        ]
        if matches:
            selected.append(matches[0])
            used.add(matches[0]["case_id"])
    for item in case_rows:
        if not item["official_resolved"] and item["case_id"] not in used:
            selected.append(item)
            used.add(item["case_id"])
        if len(selected) >= 3:
            break
    return selected[:3]


def repeated_action_loops(
    histories: Mapping[str, Mapping[str, Any]],
    cases: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    all_candidates: list[dict[str, Any]] = []
    for case_id, source in histories.items():
        history = source["history"]
        positions_by_action: dict[str, list[int]] = defaultdict(list)
        for step, event in enumerate(history):
            action_text = action(event)
            if action_text and action_text not in {"submit", "exit"}:
                positions_by_action[action_text].append(step)
        for action_text, positions in positions_by_action.items():
            if len(positions) < 2:
                continue
            between = [
                step
                for step in range(positions[0] + 1, positions[-1])
                if editor_mutation(action(history[step]))
            ]
            observation_hashes = {
                step: sha256_text(observation(history[step]))
                for step in positions
            }
            common_hash, common_count = Counter(
                observation_hashes.values()
            ).most_common(1)[0]
            if common_count < 2 or between:
                continue
            label = cases[case_id]["label"]
            all_candidates.append(
                {
                    "case_id": case_id,
                    "instance_id": source["instance_id"],
                    "official_resolved": resolved(label),
                    "exit_class": cases[case_id]["index"].get("native_exit_class"),
                    "repeat_count": len(positions),
                    "positions": positions,
                    "action_sha256": sha256_text(action_text),
                    "action_head": compact(action_text, 240),
                    "observation_hash_by_step": observation_hashes,
                    "common_observation_sha256": common_hash,
                    "same_observation_count": common_count,
                    "mutation_steps_between": between,
                    "trajectory_sha256": source["trajectory_sha256"],
                    "common_observation_snippet": compact(
                        observation(history[positions[0]])
                    ),
                }
            )
    all_candidates.sort(
        key=lambda item: (
            item["official_resolved"],
            -item["repeat_count"],
            -item["same_observation_count"],
            item["instance_id"],
            item["case_id"],
        )
    )
    high_confidence = [
        item
        for item in all_candidates
        if not item["official_resolved"]
        and item["repeat_count"] >= 3
        and (
            "reproduce_issue.py" in item["action_head"]
            or (
                "str_replace_editor" in item["action_head"]
                and " view " not in item["action_head"]
            )
        )
    ]
    return {
        "repeat_signal_case_count": len(all_candidates),
        "repeat_signal_unresolved_case_count": sum(
            not item["official_resolved"] for item in all_candidates
        ),
        "repeat_signal_resolved_control_count": sum(
            item["official_resolved"] for item in all_candidates
        ),
        "high_confidence_unresolved_cases": high_confidence[:4],
    }


def late_positive_validation(
    histories: Mapping[str, Mapping[str, Any]],
    cases: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for case_id, source in histories.items():
        if case_id in PRIOR_COVERED_CASE_IDS:
            continue
        label = cases[case_id]["label"]
        if resolved(label):
            continue
        history = source["history"]
        matches: list[dict[str, Any]] = []
        for step in range(max(0, len(history) - 8), len(history)):
            event = history[step]
            action_text = action(event)
            obs = observation(event)
            if not any(
                token in action_text.lower()
                for token in ("pytest", "unittest", "reproduce", "test_", "validate", "check")
            ):
                continue
            if POSITIVE_TEST.search(obs) and not NEGATIVE_TEST.search(obs):
                matches.append(
                    {
                        "step": step,
                        "action_sha256": sha256_text(action_text),
                        "observation_sha256": sha256_text(obs),
                        "action_head": compact(action_text, 300),
                        "observation_snippet": compact(obs, 420),
                    }
                )
        if matches:
            latest = matches[-1]
            candidates.append(
                {
                    "case_id": case_id,
                    "instance_id": source["instance_id"],
                    "official_resolved": False,
                    "exit_class": cases[case_id]["index"].get("native_exit_class"),
                    "trajectory_sha256": source["trajectory_sha256"],
                    "latest_positive_validation": latest,
                }
            )
    candidates.sort(key=lambda item: (item["instance_id"], item["case_id"]))
    preferred = {
        "sympy__sympy-19487",
        "pylint-dev__pylint-4661",
    }
    selected = [item for item in candidates if item["instance_id"] in preferred]
    selected_ids = {item["case_id"] for item in selected}
    selected.extend(item for item in candidates if item["case_id"] not in selected_ids)
    return {
        "late_positive_unresolved_case_count": len(candidates),
        "interpretation": (
            "Local positive output is a capability/validation signal only; "
            "official_resolved=false keeps these cases unresolved."
        ),
        "representative_cases": selected[:3],
    }


def load_verified_logs(
    assignment_root: Path,
    scope: scope_lib.HistoricalScope,
    rows: Iterable[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    def loader(row: Mapping[str, Any]) -> dict[str, Any]:
        descriptor = row["source_hashes"]["log"]
        path, raw = verified_source(assignment_root, descriptor)
        return {
            "case_id": row["case_id"],
            "path": str(path),
            "sha256": sha256_bytes(raw),
            "text": raw.decode("utf-8", errors="replace"),
        }

    loaded = scope.load_eligible(rows, loader)
    return {item["case_id"]: item for item in loaded}


def truncation_findings(
    assignment_root: Path,
    scope: scope_lib.HistoricalScope,
    eligible_index: list[dict[str, Any]],
    cases: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    rows = [
        row
        for row in eligible_index
        if integer(row.get("output_truncation_responses")) > 0
    ]
    logs = load_verified_logs(assignment_root, scope, rows)
    findings = []
    for row in rows:
        case_id = row["case_id"]
        text = logs[case_id]["text"]
        lines = text.splitlines()
        matches = []
        for number, line in enumerate(lines, 1):
            if re.search(
                r"finish_reason\s*=\s*['\"]length['\"]", line, re.IGNORECASE
            ):
                nearby = "\n".join(lines[max(0, number - 3) : number + 4])
                token_match = re.search(
                    r"(?:input|prompt)_tokens[=:\s]+(\d+).*?"
                    r"(?:output|completion)_tokens[=:\s]+(\d+)",
                    nearby,
                    re.IGNORECASE | re.DOTALL,
                )
                matches.append(
                    {
                        "line": number,
                        "line_sha256": sha256_text(line),
                        "token_context": (
                            {
                                "input_tokens": int(token_match.group(1)),
                                "output_tokens": int(token_match.group(2)),
                            }
                            if token_match
                            else None
                        ),
                    }
                )
        label = cases[case_id]["label"]
        findings.append(
            {
                "case_id": case_id,
                "instance_id": row["instance_id"],
                "official_resolved": resolved(label),
                "exit_class": row.get("native_exit_class"),
                "trajectory_sha256": row["source_hashes"]["trajectory"]["sha256"],
                "log_sha256": logs[case_id]["sha256"],
                "indexed_truncation_response_count": integer(
                    row.get("output_truncation_responses")
                ),
                "finish_reason_length_match_count": len(matches),
                "finish_reason_length_matches": matches,
                "prompt_tokens_max": integer(row.get("prompt_tokens_max")),
            }
        )
    return findings


def context_findings(
    histories: Mapping[str, Mapping[str, Any]],
    eligible_index: list[dict[str, Any]],
    cases: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    findings = []
    for row in eligible_index:
        count = integer(row.get("prompt_ge_client_limit"))
        if count <= 0:
            continue
        case_id = row["case_id"]
        source = histories[case_id]
        exits = []
        for step, event in enumerate(source["history"]):
            fields = (observation(event), response(event))
            if any(CONTEXT_EXIT.search(value) for value in fields):
                exits.append(
                    {
                        "step": step,
                        "observation_sha256": sha256_text(observation(event)),
                        "response_sha256": sha256_text(response(event)),
                        "response_snippet": compact(response(event)),
                    }
                )
        findings.append(
            {
                "case_id": case_id,
                "instance_id": row["instance_id"],
                "official_resolved": resolved(cases[case_id]["label"]),
                "exit_class": row.get("native_exit_class"),
                "trajectory_sha256": source["trajectory_sha256"],
                "prompt_ge_client_limit_count": count,
                "prompt_tokens_max": integer(row.get("prompt_tokens_max")),
                "trajectory_event_count": len(source["history"]),
                "context_exit_events": exits,
            }
        )
    return findings


def timeout_findings(
    histories: Mapping[str, Mapping[str, Any]],
    cases: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    flagged = []
    worker_hits = []
    shell_timeout_example = None
    for case_id, source in histories.items():
        label = cases[case_id]["label"]
        if not truth(label.get("timeout_flag")):
            continue
        flagged.append(case_id)
        for step, event in enumerate(source["history"]):
            obs = observation(event)
            markers = actual_timeout_markers(obs)
            if markers:
                worker_hits.append(
                    {
                        "case_id": case_id,
                        "instance_id": source["instance_id"],
                        "step": step,
                        "observation_sha256": sha256_text(obs),
                        "markers": markers,
                    }
                )
            action_text = action(event)
            if (
                shell_timeout_example is None
                and re.search(r"(?:^|&&\s*)timeout\s+\d+\s+", action_text)
            ):
                shell_timeout_example = {
                    "case_id": case_id,
                    "instance_id": source["instance_id"],
                    "step": step,
                    "action_sha256": sha256_text(action_text),
                    "observation_sha256": sha256_text(obs),
                    "action_head": compact(action_text),
                    "official_resolved": resolved(label),
                }
    return {
        "eligible_timeout_flag_case_count": len(flagged),
        "strict_worker_timeout_event_count": len(worker_hits),
        "strict_worker_timeout_cases": worker_hits,
        "shell_timeout_command_control": shell_timeout_example,
        "interpretation": (
            "The timeout flag is not accepted as a worker termination cause "
            "without a structured worker-result marker."
        ),
    }


def parse_proxy_file(
    assignment_root: Path, row: Mapping[str, Any]
) -> dict[str, Any]:
    trajectory_path = resolve_source_path(
        assignment_root, row["source_hashes"]["trajectory"]["path"]
    )
    proxy_path = trajectory_path.parent.parent / "request_proxy.jsonl"
    raw = proxy_path.read_bytes()
    records = []
    for line_number, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), 1):
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            records.append(
                {
                    "line": line_number,
                    "status": None,
                    "error": "invalid_json",
                    "request_id": None,
                    "duration_ms": None,
                }
            )
            continue
        records.append(
            {
                "line": line_number,
                "status": item.get("status_code"),
                "error": item.get("error"),
                "phase": item.get("failure_phase"),
                "request_id": item.get("request_id"),
                "duration_ms": item.get("duration_ms"),
                "completion_tokens": item.get("completion_tokens"),
                "prompt_tokens": item.get("prompt_tokens"),
            }
        )
    statuses = [item["status"] for item in records]
    errors = [item for item in records if item["error"]]
    return {
        "case_id": row["case_id"],
        "proxy_path": str(proxy_path),
        "proxy_sha256": sha256_bytes(raw),
        "line_count": len(records),
        "status_counts": dict(
            Counter("none" if status is None else str(status) for status in statuses)
        ),
        "ok_2xx_count": sum(
            isinstance(status, int) and 200 <= status < 300 for status in statuses
        ),
        "four_xx_lines": [
            item["line"]
            for item in records
            if isinstance(item["status"], int) and 400 <= item["status"] < 500
        ],
        "five_xx_lines": [
            item["line"]
            for item in records
            if isinstance(item["status"], int) and 500 <= item["status"] < 600
        ],
        "remote_disconnected_lines": [
            item["line"]
            for item in records
            if str(item["error"]) == "RemoteDisconnected"
        ],
        "remote_disconnected_durations_ms": [
            item["duration_ms"]
            for item in records
            if str(item["error"]) == "RemoteDisconnected"
        ],
        "error_records": errors,
    }


def proxy_findings(
    assignment_root: Path,
    scope: scope_lib.HistoricalScope,
    eligible_index: list[dict[str, Any]],
    cases: Mapping[str, Mapping[str, Any]],
    histories: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    rows = []
    for row in eligible_index:
        label = cases[row["case_id"]]["label"]
        if (
            integer(label.get("proxy_4xx")) > 0
            or integer(label.get("proxy_remote_disconnected")) > 0
        ):
            rows.append(row)
    # Path resolution and proxy reads happen inside this eligible loader.
    proxy_records = {
        item["case_id"]: item
        for item in scope.load_eligible(
            rows, lambda row: parse_proxy_file(assignment_root, row)
        )
    }
    enriched = []
    for row in rows:
        case_id = row["case_id"]
        item = proxy_records[case_id]
        label = cases[case_id]["label"]
        enriched.append(
            {
                **item,
                "instance_id": row["instance_id"],
                "official_resolved": resolved(label),
                "exit_class": row.get("native_exit_class"),
                "trajectory_sha256": histories[case_id]["trajectory_sha256"],
                "log_sha256": row["source_hashes"]["log"]["sha256"],
            }
        )
    remote_unresolved = sorted(
        [
            item
            for item in enriched
            if item["remote_disconnected_lines"] and not item["official_resolved"]
        ],
        key=lambda item: (
            -len(item["remote_disconnected_lines"]),
            item["instance_id"],
            item["case_id"],
        ),
    )
    four_x_unresolved = sorted(
        [
            item
            for item in enriched
            if item["four_xx_lines"] and not item["official_resolved"]
        ],
        key=lambda item: (item["instance_id"], item["case_id"]),
    )
    four_x_resolved = sorted(
        [
            item
            for item in enriched
            if item["four_xx_lines"] and item["official_resolved"]
        ],
        key=lambda item: (item["instance_id"], item["case_id"]),
    )
    representatives = (
        remote_unresolved[:2]
        + four_x_unresolved[:1]
        + four_x_resolved[:1]
    )
    # Keep the report artifact finite and avoid embedding every request body.
    for item in representatives:
        item.pop("error_records", None)
    return {
        "eligible_proxy_candidate_case_count": len(enriched),
        "case_count_with_4xx": sum(bool(item["four_xx_lines"]) for item in enriched),
        "case_count_with_5xx": sum(bool(item["five_xx_lines"]) for item in enriched),
        "case_count_with_remote_disconnected": sum(
            bool(item["remote_disconnected_lines"]) for item in enriched
        ),
        "representative_cases": representatives,
        "interpretation": (
            "A 4xx boundary and a RemoteDisconnected suffix are execution "
            "signals. They are not substituted for the official outcome."
        ),
    }


def input_artifact(path: Path, assignment_root: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    try:
        display = str(path.relative_to(assignment_root))
    except ValueError:
        display = str(path)
    return {
        "path": display,
        "sha256": sha256_bytes(raw),
        "bytes": len(raw),
    }


def make_findings(
    editor_summary: Mapping[str, Any],
    loop_summary: Mapping[str, Any],
    truncation: list[dict[str, Any]],
    context: list[dict[str, Any]],
    timeout: Mapping[str, Any],
    proxy: Mapping[str, Any],
    near_success: Mapping[str, Any],
) -> list[dict[str, Any]]:
    unresolved_truncation = sum(
        not item["official_resolved"] for item in truncation
    )
    resolved_truncation = len(truncation) - unresolved_truncation
    resolved_context = sum(item["official_resolved"] for item in context)
    return [
        {
            "id": "DEV-01",
            "category": "malformed_or_noop_editor",
            "recommendation": "adopt bounded editor-result telemetry and recovery; reject repeated blind replacement",
            "issue": (
                "The editor returns an exact no-replacement result, and some "
                "unresolved workers issue more replacements without changing "
                "the source context."
            ),
            "evidence": {
                "new_editor_failure_event_count": editor_summary[
                    "new_editor_failure_event_count"
                ],
                "new_editor_failure_case_count": editor_summary[
                    "new_editor_failure_case_count"
                ],
                "unresolved_case_count": editor_summary["unresolved_case_count"],
                "resolved_control_case_count": editor_summary[
                    "resolved_control_case_count"
                ],
                "representative_cases": editor_summary["representative_cases"],
            },
            "impact": (
                "A failed edit consumes a model call and can leave the patch "
                "unchanged near the cost/context boundary."
            ),
            "risk": (
                "The resolved controls show that one editor failure is "
                "recoverable; treating every editor error as a model failure "
                "would misattribute capability and execution behavior."
            ),
            "implementation": (
                "Emit changed-byte and old_str-match metadata at the "
                "executionworker boundary. After one no-op result, require a "
                "fresh view or changed edit plan before another replacement."
            ),
            "validation": (
                "Replay the representative case histories with a fixed "
                "Qwen revision and assert the no-op event, recovery branch, "
                "and final official status are separately recorded."
            ),
        },
        {
            "id": "DEV-02",
            "category": "repeated_action_loop",
            "recommendation": "adopt loop telemetry and bounded replan review; reject an automatic loop-kill or fifth candidate",
            "issue": (
                "Some unresolved histories repeat an identical action with "
                "the same observation hash and no detected editor mutation "
                "between repeats."
            ),
            "evidence": {
                "repeat_signal_case_count": loop_summary[
                    "repeat_signal_case_count"
                ],
                "repeat_signal_unresolved_case_count": loop_summary[
                    "repeat_signal_unresolved_case_count"
                ],
                "resolved_control_count": loop_summary[
                    "repeat_signal_resolved_control_count"
                ],
                "high_confidence_cases": loop_summary[
                    "high_confidence_unresolved_cases"
                ],
            },
            "impact": (
                "Repeated reproduction or diagnosis calls spend the fixed "
                "call/cost budget while preserving the same failure state."
            ),
            "risk": (
                "Repeated tests can be legitimate after a source edit, so "
                "an action-only detector would stop valid exploration."
            ),
            "implementation": (
                "Key loop events by action bytes plus observation bytes, "
                "include detected mutation steps, and surface a replan "
                "prompt or review event after the second unchanged result."
            ),
            "validation": (
                "Use the listed case IDs and hashes to verify unchanged-state "
                "repeats are flagged while resolved controls with intervening "
                "edits remain unflagged."
            ),
        },
        {
            "id": "DEV-03",
            "category": "model_output_truncation",
            "recommendation": "adopt explicit finish_reason capture and bounded continuation accounting; reject model or panel retuning from five cases",
            "issue": (
                "The provider returned finish_reason=length in eligible "
                "development calls, including unresolved and resolved cases."
            ),
            "evidence": {
                "case_count": len(truncation),
                "unresolved_case_count": unresolved_truncation,
                "resolved_control_case_count": resolved_truncation,
                "total_finish_reason_length_matches": sum(
                    item["finish_reason_length_match_count"] for item in truncation
                ),
                "cases": truncation,
            },
            "impact": (
                "A truncated response can consume a call without a complete "
                "tool action and can increase later context pressure."
            ),
            "risk": (
                "Resolved controls prove truncation is not sufficient to "
                "predict failure; continuation can also increase cost."
            ),
            "implementation": (
                "Persist finish_reason with the request ID and make any "
                "continuation bounded and tool-safe, with no automatic "
                "candidate or prompt change."
            ),
            "validation": (
                "Replay the five source/log pairs and check that all six "
                "length markers, subsequent actions, and official outcomes "
                "are recovered exactly."
            ),
        },
        {
            "id": "DEV-04",
            "category": "context_growth_and_termination",
            "recommendation": "adopt explicit context-exit attribution and pre-submit budget telemetry; reject raising limits or changing candidates from these controls",
            "issue": (
                "Two workers reached the indexed client-limit condition after "
                "local validation, then terminated through the context exit."
            ),
            "evidence": {
                "case_count": len(context),
                "officially_resolved_count": resolved_context,
                "cases": context,
            },
            "impact": (
                "A valid repair can be recorded as a termination event unless "
                "termination and official outcome remain separate."
            ),
            "risk": (
                "Both cases are officially resolved, so this evidence does "
                "not support a model-capability failure or a quality change."
            ),
            "implementation": (
                "Record prompt-budget crossings and context exit at the "
                "request boundary, preserve the completed patch for "
                "evaluation, and expose autosubmission separately."
            ),
            "validation": (
                "Replay both case histories and assert the prompt-limit "
                "counter, exit step, local validation hashes, and official "
                "resolved=true outcome remain distinct."
            ),
        },
        {
            "id": "DEV-05",
            "category": "near_success_validation_gap",
            "recommendation": "adopt a near-success review flag; reject it as candidate-selection or fifth-candidate evidence",
            "issue": (
                "Unresolved histories contain late positive local-test output, "
                "but the official case outcome remains false."
            ),
            "evidence": {
                "late_positive_unresolved_case_count": near_success[
                    "late_positive_unresolved_case_count"
                ],
                "representative_cases": near_success["representative_cases"],
            },
            "impact": (
                "Local success claims can hide a wrong patch or incomplete "
                "official behavior and create false confidence at termination."
            ),
            "risk": (
                "The signal is self-reported tool output, so it cannot replace "
                "official evaluation or prove one-test-away proximity."
            ),
            "implementation": (
                "Carry local validation scope, command, output hash, and "
                "official outcome together in review telemetry."
            ),
            "validation": (
                "Check the representative final test outputs against their "
                "trajectory and official result hashes without fitting or "
                "opening any holdout."
            ),
        },
        {
            "id": "DEV-06",
            "category": "retry_and_api_infrastructure",
            "recommendation": "adopt request-attempt lineage telemetry; reject proxy errors as a model-failure label",
            "issue": (
                "Eligible proxy histories contain terminal 4xx boundaries and "
                "repeated RemoteDisconnected response-header failures after "
                "successful requests."
            ),
            "evidence": proxy,
            "impact": (
                "A run can exhaust execution time on infrastructure retries "
                "while its earlier model actions remain valid evidence."
            ),
            "risk": (
                "A 4xx can be a terminal protocol event and a disconnected "
                "model service can be correlated with, rather than cause, an "
                "unresolved patch."
            ),
            "implementation": (
                "Bind status, error, phase, duration, request ID, and retry "
                "position to each physical request and keep that overlay "
                "separate from official resolution."
            ),
            "validation": (
                "Verify the listed proxy hashes reproduce the 2xx-prefix/"
                "4xx-suffix and 2xx-prefix/RemoteDisconnected-suffix "
                "sequences, including the resolved control."
            ),
        },
        {
            "id": "DEV-07",
            "category": "timeout_attribution",
            "recommendation": "adopt strict structured timeout attribution; reject timeout mitigation from the current flag alone",
            "issue": (
                "The broad timeout flag is present in eligible labels, but "
                "the bounded trajectory scan found no structured worker "
                "timeout result."
            ),
            "evidence": timeout,
            "impact": (
                "A broad timeout classifier can redirect remediation toward "
                "runtime changes when the evidence is source text or a model "
                "created subprocess test."
            ),
            "risk": (
                "A strict scan can miss an unstructured timeout, so the "
                "negative result is a reason to improve capture, not proof "
                "that no command ever ran long."
            ),
            "implementation": (
                "Emit a structured worker timeout event with command, elapsed "
                "time, exit code, and request/case ID; stop using free-text "
                "timeout mentions as a failure cause."
            ),
            "validation": (
                "Run the strict marker check over the same eligible histories "
                "and add a small fixture for a real timeout versus a source "
                "snippet containing TimeoutExpired."
            ),
        },
    ]


def scan(assignment_root: Path) -> dict[str, Any]:
    # This is the mandatory first historical operation. Nothing below this
    # line resolves a raw source path or opens the classification labels.
    scope = scope_lib.frozen_scope(assignment_root)
    scope_artifact = scope.artifact()
    if (
        scope_artifact["excluded_instance_count"] != 137
        or scope_artifact["excluded_run_count"] != 451
    ):
        raise ValueError("frozen scope counts changed")
    panel = scope_lib.validate_panel(
        scope, assignment_root / scope_lib.PANEL_PATH
    )

    index_path = assignment_root / INDEX_REL
    labels_path = assignment_root / LABELS_REL
    index_payload, index_sha256 = read_json(index_path)
    if not isinstance(index_payload, Mapping) or not isinstance(
        index_payload.get("cases"), list
    ):
        raise ValueError("invalid termination evidence index")
    index_rows = list(index_payload["cases"])
    # Gate identity-only projections before any raw trajectory path is
    # resolved. The loader returns the original index row only after approval.
    eligible_index = list(
        scope.load_eligible(
            (
                {
                    "instance_id": row.get("instance_id"),
                    "case_id": row.get("case_id"),
                }
                for row in index_rows
            ),
            lambda identity: next(
                row
                for row in index_rows
                if row.get("case_id") == identity.get("case_id")
            ),
        )
    )
    if len(eligible_index) != 647:
        raise ValueError(f"eligible index count changed: {len(eligible_index)}")

    # Labels are opened only through a loader after the identity gate.
    seed = {
        "instance_id": eligible_index[0]["instance_id"],
        "case_id": eligible_index[0]["case_id"],
    }
    eligible_labels = load_labels_after_scope(scope, labels_path, seed)
    cases = build_case_maps(eligible_index, eligible_labels)
    if len(cases) != 647:
        raise ValueError(f"eligible label join count changed: {len(cases)}")

    # Raw trajectory path resolution is owned by this second eligible loader.
    histories = {
        item["case_id"]: item
        for item in scope.load_eligible(
            eligible_index,
            lambda row: load_case_history(assignment_root, row),
        )
    }
    if len(histories) != 647:
        raise ValueError(f"trajectory coverage changed: {len(histories)}")

    _, editor_summary = editor_failures(histories, cases)
    loop_summary = repeated_action_loops(histories, cases)
    near_success = late_positive_validation(histories, cases)
    truncation = truncation_findings(
        assignment_root, scope, eligible_index, cases
    )
    context = context_findings(histories, eligible_index, cases)
    timeout = timeout_findings(histories, cases)
    proxy = proxy_findings(
        assignment_root, scope, eligible_index, cases, histories
    )

    findings = make_findings(
        editor_summary,
        loop_summary,
        truncation,
        context,
        timeout,
        proxy,
        near_success,
    )
    return {
        "schema_version": "assignment.final-development-failure-scan.v1",
        "scan_date": "2026-09-09",
        "worker": "executionworker",
        "assignment_root": str(assignment_root.resolve()),
        "scope": scope_artifact,
        "panel_validation": panel,
        "prior_report_reuse_and_disclosure": {
            "prior_access_disclosure": scope_artifact[
                "prior_access_disclosure"
            ],
            "prior_report_coverage_excluded_case_ids": sorted(
                PRIOR_COVERED_CASE_IDS
            ),
            "reused_existing_data": [
                "termination index case counters and official classification join",
                "fixed panel/model constants and prior aggregate framing",
            ],
            "new_scan_boundary": (
                "Only eligible development histories were scanned for "
                "case-level evidence in the listed categories."
            ),
        },
        "fixed_run_constants": {
            "model_revision": FIXED_MODEL_REVISION,
            "output_tokens": FIXED_OUTPUT_TOKENS,
            "temperature": FIXED_TEMPERATURE,
            "top_p": FIXED_TOP_P,
            "seed": FIXED_SEED,
            "serving_max_model_len": FIXED_SERVING_MAX_MODEL_LEN,
            "panel_instances": FIXED_PANEL_INSTANCES,
            "panel_trajectories": FIXED_PANEL_TRAJECTORIES,
            "core_config_or_gpu_or_evaluator_run": False,
            "candidate_selection_or_fifth_candidate": False,
        },
        "input_artifacts": [
            input_artifact(index_path, assignment_root),
            input_artifact(labels_path, assignment_root),
        ],
        "coverage": {
            "index_rows_seen": len(index_rows),
            "eligible_index_cases": len(eligible_index),
            "eligible_label_cases": len(eligible_labels),
            "verified_trajectory_cases": len(histories),
            "termination_index_sha256": index_sha256,
        },
        "bounded_scans": {
            "editor_failures": editor_summary,
            "repeated_action_loops": loop_summary,
            "near_success_validation": near_success,
            "output_truncation": truncation,
            "context_growth": context,
            "timeouts": timeout,
            "proxy_retry_infrastructure": proxy,
        },
        "findings": findings,
        "stop_rule": (
            "High-impact case-level execution evidence is covered by DEV-01 "
            "through DEV-07; no speculative fifth candidate is proposed."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--assignment-root",
        type=Path,
        default=scope_lib.ASSIGNMENT_ROOT,
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="new JSON artifact; an existing path is refused",
    )
    args = parser.parse_args(argv)
    try:
        artifact = scan(args.assignment_root.resolve())
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as handle:
            json.dump(artifact, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        parser.exit(2, f"final development scan failed: {exc}\n")
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "eligible_cases": artifact["coverage"]["eligible_index_cases"],
                "finding_count": len(artifact["findings"]),
                "excluded_instances": artifact["scope"]["excluded_instance_count"],
                "excluded_runs": artifact["scope"]["excluded_run_count"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
