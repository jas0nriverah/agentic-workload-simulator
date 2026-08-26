#!/usr/bin/env python3
"""Read-only, bounded inventory of recoverable H100 SWE-agent evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = "assignment.h100-recovery-audit.v1"
DEFAULT_MAX_FILES = 100_000
DEFAULT_MAX_FILE_BYTES = 64 * 1024 * 1024
CASE_RE = re.compile(r"[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-\d+")

SKIP_DIRECTORY_NAMES = {
    ".cache",
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "blobs",
    "huggingface",
    "node_modules",
    "snapshots",
    "venv",
}
MODEL_WEIGHT_SUFFIXES = {
    ".bin",
    ".ckpt",
    ".engine",
    ".gguf",
    ".onnx",
    ".pt",
    ".pth",
    ".safetensors",
}
RAW_TRACE_SUFFIXES = {".nsys-rep", ".qdrep"}
JSON_SUFFIXES = {".json", ".jsonl", ".traj"}
TRACE_METADATA_SUFFIXES = {".csv", ".sqlite", ".log", ".txt"}

RUN_KEYS = ("run_id", "batch_id", "runner_id", "job_id")
CASE_KEYS = ("instance_id", "case_id", "problem_id", "task_id")
TOKEN_INPUT_KEYS = ("prompt_tokens", "input_tokens", "tokens_sent")
TOKEN_OUTPUT_KEYS = ("completion_tokens", "output_tokens", "tokens_received")
E2E_DURATION_KEYS = (
    "e2e_wall_ms",
    "trajectory_duration_ms",
    "duration_ms",
    "elapsed_ms",
    "wall_time_ms",
    "elapsed_seconds",
    "duration_seconds",
    "wall_time_seconds",
)
START_KEYS = ("start_mono_ns", "start_time_ns", "started_at", "start_time")
END_KEYS = ("end_mono_ns", "end_time_ns", "finished_at", "end_time")

GENERIC_DIRECTORY_NAMES = {
    "artifacts",
    "batches",
    "evaluation",
    "logs",
    "output",
    "outputs",
    "results",
    "run_evaluation",
    "sweagent-output",
    "sweagent_output",
}


class AuditError(RuntimeError):
    """Raised when the bounded audit contract cannot be honored."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _positive_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and value > 0


def _nonnegative_integer(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def _first_string(mapping: dict[str, Any], keys: Iterable[str]) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _case_from_text(value: str) -> str | None:
    match = CASE_RE.search(value)
    return match.group(0) if match else None


def _path_context(relative_path: Path) -> tuple[str | None, str | None]:
    parts = relative_path.parts
    joined = "/".join(parts)
    case_id = _case_from_text(joined)
    run_id: str | None = None

    if "run_evaluation" in parts:
        index = parts.index("run_evaluation")
        if index + 1 < len(parts):
            run_id = parts[index + 1]
    elif "batches" in parts:
        index = parts.index("batches")
        if index + 1 < len(parts):
            run_id = parts[index + 1]
        if index + 2 < len(parts) - 1:
            candidate = parts[index + 2]
            if candidate not in GENERIC_DIRECTORY_NAMES and candidate != case_id:
                run_id = candidate
    elif "sweagent-output" in parts or "sweagent_output" in parts:
        marker = "sweagent-output" if "sweagent-output" in parts else "sweagent_output"
        index = parts.index(marker)
        for candidate in reversed(parts[:index]):
            if candidate not in GENERIC_DIRECTORY_NAMES:
                run_id = candidate
                break
    else:
        for candidate in reversed(parts[:-1]):
            if candidate not in GENERIC_DIRECTORY_NAMES and candidate != case_id:
                run_id = candidate
                break
    return run_id, case_id


def _is_model_request(row: dict[str, Any]) -> bool:
    schema = str(row.get("schema_version", "")).lower()
    event_type = str(row.get("event_type", row.get("type", ""))).lower()
    path = str(row.get("path", "")).lower()
    requestish = (
        "model-event" in schema
        or "model_event" in schema
        or "model_request" in event_type
        or "llm_request" in event_type
        or "vllm_request" in event_type
        or path.endswith("/v1/chat/completions")
        or (
            isinstance(row.get("request_id"), str)
            and any(key in row for key in (*TOKEN_INPUT_KEYS, "request_bytes", "model"))
        )
    )
    timed = any(_positive_number(row.get(key)) for key in ("duration_ms", "wall_ms"))
    timed = timed or (
        any(row.get(key) is not None for key in START_KEYS)
        and any(row.get(key) is not None for key in END_KEYS)
    )
    return requestish and timed


def _has_token_counts(row: dict[str, Any]) -> bool:
    direct = any(_nonnegative_integer(row.get(key)) for key in TOKEN_INPUT_KEYS) and any(
        _nonnegative_integer(row.get(key)) for key in TOKEN_OUTPUT_KEYS
    )
    if direct:
        return True
    stats = row.get("model_stats")
    if not isinstance(stats, dict) and isinstance(row.get("info"), dict):
        stats = row["info"].get("model_stats")
    return isinstance(stats, dict) and any(
        _nonnegative_integer(stats.get(key)) for key in TOKEN_INPUT_KEYS
    ) and any(_nonnegative_integer(stats.get(key)) for key in TOKEN_OUTPUT_KEYS)


def _has_tool_events(row: dict[str, Any]) -> bool:
    trajectory = row.get("trajectory")
    if isinstance(trajectory, list):
        return any(
            isinstance(step, dict)
            and isinstance(step.get("action"), str)
            and step["action"].strip()
            and _positive_number(step.get("execution_time"))
            for step in trajectory
        )
    schema = str(row.get("schema_version", "")).lower()
    event_type = str(row.get("event_type", row.get("type", ""))).lower()
    toolish = "tool-event" in schema or "tool_event" in schema or event_type in {
        "tool",
        "tool_call",
        "tool_execution",
    }
    return toolish and any(_positive_number(row.get(key)) for key in ("duration_ms", "wall_ms"))


def _has_e2e(row: dict[str, Any], source_kind: str) -> bool:
    event_type = str(row.get("event_type", row.get("type", ""))).lower()
    schema = str(row.get("schema_version", "")).lower()
    e2e_context = (
        source_kind in {"runner_state", "worker_state", "trajectory"}
        or event_type in {"sweagent_trajectory", "trajectory", "run", "run_complete"}
        or "trajectory" in schema
        or "run-state" in schema
        or "runner" in schema
        or "worker" in schema
    )
    if not e2e_context:
        return False
    if any(_positive_number(row.get(key)) for key in E2E_DURATION_KEYS):
        return True
    return any(row.get(key) is not None for key in START_KEYS) and any(
        row.get(key) is not None for key in END_KEYS
    )


def _official_case_rows(value: Any) -> list[tuple[str, dict[str, Any]]]:
    rows: list[tuple[str, dict[str, Any]]] = []
    if not isinstance(value, dict):
        return rows
    for key, item in value.items():
        if CASE_RE.fullmatch(str(key)) and isinstance(item, dict) and isinstance(item.get("resolved"), bool):
            rows.append((str(key), item))
    for key in ("resolved_ids", "unresolved_ids", "completed_ids", "incomplete_ids", "error_ids"):
        items = value.get(key)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, str) and CASE_RE.fullmatch(item):
                    rows.append((item, {"official_outcome": True}))
    return rows


def _has_official_outcome(row: dict[str, Any]) -> bool:
    if row.get("official_outcome") is True:
        return True
    if isinstance(row.get("official_resolved"), bool):
        return True
    if isinstance(row.get("resolved"), bool) and any(
        key in row for key in ("tests_status", "patch_successfully_applied", "patch_exists", "evaluation")
    ):
        return True
    return False


def _looks_like_trace_metadata(path: Path) -> bool:
    lowered = path.name.lower()
    return any(token in lowered for token in ("trace", "kineto", "nsight", "nsys", "telemetry", "profile"))


def _name_kind(path: Path) -> str:
    lowered = path.name.lower()
    if path.suffix.lower() == ".traj" or "trajectory" in lowered:
        return "trajectory"
    if any(token in lowered for token in ("request", "proxy")) and path.suffix.lower() == ".jsonl":
        return "request_proxy"
    if any(token in lowered for token in ("evaluator", "evaluation", "outcome", "report")):
        return "evaluator_outcome"
    if "worker" in lowered:
        return "worker_state"
    if any(token in lowered for token in ("runner", "run_state", "run-state", "progress", "measurement")):
        return "runner_state"
    if _looks_like_trace_metadata(path):
        return "trace_metadata"
    if "summary" in lowered or "state" in lowered or lowered == "events.jsonl":
        return "runner_state"
    return "unknown"


def _read_structured(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None
    if path.suffix.lower() == ".jsonl":
        rows: list[Any] = []
        for number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise AuditError(f"invalid JSONL {path}:{number}: {exc.msg}") from exc
        return rows
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise AuditError(f"invalid JSON {path}: {exc.msg}") from exc


def _iter_units(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for key in ("cases", "results", "rows", "runs", "workers"):
            nested = value.get(key)
            if isinstance(nested, list):
                for item in nested:
                    if isinstance(item, dict):
                        yield item
            elif isinstance(nested, dict):
                for nested_key, item in nested.items():
                    if isinstance(item, dict):
                        enriched = dict(item)
                        if CASE_RE.fullmatch(str(nested_key)):
                            enriched.setdefault("instance_id", str(nested_key))
                        yield enriched
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                yield item


def _classify_source(path: Path, structured: Any) -> str:
    named = _name_kind(path)
    units = list(_iter_units(structured))
    if any(isinstance(unit.get("trajectory"), list) for unit in units):
        return "trajectory"
    if any(_is_model_request(unit) for unit in units):
        return "request_proxy"
    if _official_case_rows(structured) or any(_has_official_outcome(unit) for unit in units):
        return "evaluator_outcome"
    if any(
        (_first_string(unit, RUN_KEYS) or _first_string(unit, CASE_KEYS))
        and (
            any(_positive_number(unit.get(key)) for key in E2E_DURATION_KEYS)
            or (
                any(unit.get(key) is not None for key in START_KEYS)
                and any(unit.get(key) is not None for key in END_KEYS)
            )
        )
        for unit in units
    ):
        return "runner_state"
    return named


def _findings(
    structured: Any,
    source_kind: str,
    inferred_run_id: str | None,
    inferred_case_id: str | None,
) -> list[tuple[str, str, dict[str, bool]]]:
    findings: list[tuple[str, str, dict[str, bool]]] = []
    official = _official_case_rows(structured)
    for case_id, official_row in official:
        run_id = _first_string(official_row, RUN_KEYS) or inferred_run_id or "__unknown_run__"
        findings.append((run_id, case_id, {
            "e2e_recoverable": False,
            "tool_events_recoverable": False,
            "model_request_events_recoverable": False,
            "token_counts_recoverable": _has_token_counts(official_row),
            "official_outcome_present": True,
        }))

    for unit in _iter_units(structured):
        run_id = _first_string(unit, RUN_KEYS) or inferred_run_id or "__unknown_run__"
        case_id = _first_string(unit, CASE_KEYS)
        if case_id is None and isinstance(unit.get("environment"), str):
            case_id = _case_from_text(unit["environment"])
        case_id = case_id or inferred_case_id or "__unknown_case__"
        flags = {
            "e2e_recoverable": _has_e2e(unit, source_kind),
            "tool_events_recoverable": _has_tool_events(unit),
            "model_request_events_recoverable": _is_model_request(unit),
            "token_counts_recoverable": _has_token_counts(unit),
            "official_outcome_present": _has_official_outcome(unit),
        }
        if any(flags.values()):
            findings.append((run_id, case_id, flags))
    return findings


def _candidate(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix in JSON_SUFFIXES:
        return True
    if suffix in TRACE_METADATA_SUFFIXES:
        lowered = path.name.lower()
        return _looks_like_trace_metadata(path) or any(
            token in lowered for token in ("runner", "worker", "run_instance", "agent")
        )
    return False


def _skip_directory(name: str) -> bool:
    lowered = name.lower()
    return lowered in SKIP_DIRECTORY_NAMES or lowered.startswith("models--")


def _display_path(path: Path, root: Path, root_id: str) -> str:
    try:
        relative = path.resolve().relative_to(root).as_posix()
    except ValueError:
        relative = path.name
    return f"{root_id}/{relative}"


def _source_record(path: Path, root: Path, root_id: str, kind: str, digest: str) -> dict[str, Any]:
    relative = path.relative_to(root).as_posix()
    return {
        "kind": kind,
        "path": f"{root_id}/{relative}",
        "relative_path": relative,
        "root_id": root_id,
        "sha256": digest,
        "size_bytes": path.stat().st_size,
    }


def audit_roots(
    roots: Iterable[Path],
    *,
    max_files: int = DEFAULT_MAX_FILES,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> dict[str, Any]:
    if max_files <= 0 or max_file_bytes <= 0:
        raise AuditError("max-files and max-file-bytes must be positive")
    resolved_roots = sorted({Path(root).expanduser().resolve() for root in roots}, key=str)
    if not resolved_roots:
        raise AuditError("at least one --root is required")
    for root in resolved_roots:
        if not root.exists():
            raise AuditError(f"root does not exist: {root}")
        if not root.is_dir():
            raise AuditError(f"root is not a directory: {root}")

    source_by_path: dict[str, dict[str, Any]] = {}
    source_findings: dict[str, list[tuple[str, str, dict[str, bool]]]] = {}
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    examined_files = 0

    root_ids = {root: f"root-{index:03d}" for index, root in enumerate(resolved_roots)}
    for root in resolved_roots:
        root_id = root_ids[root]
        def onerror(error: OSError) -> None:
            path = Path(error.filename or root)
            errors.append({
                "errno": error.errno,
                "path": _display_path(path, root, root_id),
                "reason": "walk_error",
            })

        for directory, directory_names, file_names in os.walk(root, topdown=True, followlinks=False, onerror=onerror):
            directory_path = Path(directory)
            retained_directories: list[str] = []
            for name in sorted(directory_names):
                candidate = directory_path / name
                if candidate.is_symlink():
                    skipped.append({"path": _display_path(candidate, root, root_id), "reason": "symlink_directory"})
                elif _skip_directory(name):
                    skipped.append({"path": _display_path(candidate, root, root_id), "reason": "cache_or_model_directory"})
                else:
                    retained_directories.append(name)
            directory_names[:] = retained_directories

            for name in sorted(file_names):
                path = directory_path / name
                if path.is_symlink():
                    skipped.append({"path": _display_path(path, root, root_id), "reason": "symlink_file"})
                    continue
                suffix = path.suffix.lower()
                if suffix in RAW_TRACE_SUFFIXES:
                    skipped.append({
                        "kind": "raw_nsys_report",
                        "path": _display_path(path, root, root_id),
                        "reason": "raw_trace_payload_skipped_by_default",
                        "size_bytes": path.stat().st_size,
                    })
                    continue
                if suffix in MODEL_WEIGHT_SUFFIXES:
                    skipped.append({"path": _display_path(path, root, root_id), "reason": "model_weight"})
                    continue
                if not _candidate(path):
                    continue
                examined_files += 1
                if examined_files > max_files:
                    raise AuditError(f"candidate file limit exceeded ({max_files})")
                try:
                    size = path.stat().st_size
                except OSError as exc:
                    errors.append({"errno": exc.errno, "path": _display_path(path, root, root_id), "reason": "stat_error"})
                    continue
                if size > max_file_bytes:
                    skipped.append({
                        "path": _display_path(path, root, root_id),
                        "reason": "file_exceeds_max_file_bytes",
                        "size_bytes": size,
                    })
                    continue
                try:
                    structured = _read_structured(path) if suffix in JSON_SUFFIXES else None
                    kind = _classify_source(path, structured)
                    if kind == "unknown":
                        continue
                    digest = _sha256(path)
                except (AuditError, OSError) as exc:
                    errors.append({
                        "path": _display_path(path, root, root_id),
                        "reason": "read_error",
                        "detail": type(exc).__name__,
                    })
                    continue
                source = _source_record(path.resolve(), root, root_id, kind, digest)
                source_by_path[str(path.resolve())] = source
                inferred_run, inferred_case = _path_context(path.relative_to(root))
                source_findings[str(path.resolve())] = _findings(
                    structured, kind, inferred_run, inferred_case
                ) if structured is not None else []

    raw_findings: list[tuple[str, str, str, dict[str, bool]]] = []
    for path_key in sorted(source_by_path):
        for run_id, case_id, flags in source_findings[path_key]:
            raw_findings.append((path_key, run_id, case_id, flags))

    cases_by_run: dict[str, set[str]] = defaultdict(set)
    runs_by_case: dict[str, set[str]] = defaultdict(set)
    for _, run_id, case_id, _ in raw_findings:
        if run_id != "__unknown_run__" and case_id != "__unknown_case__":
            cases_by_run[run_id].add(case_id)
            runs_by_case[case_id].add(run_id)

    reconciled_findings: list[tuple[str, str, str, dict[str, bool]]] = []
    for path_key, run_id, case_id, flags in raw_findings:
        if case_id == "__unknown_case__" and run_id != "__unknown_run__":
            candidates = cases_by_run.get(run_id, set())
            if len(candidates) == 1:
                case_id = next(iter(candidates))
        if run_id == "__unknown_run__" and case_id != "__unknown_case__":
            candidates = runs_by_case.get(case_id, set())
            if len(candidates) == 1:
                run_id = next(iter(candidates))
        reconciled_findings.append((path_key, run_id, case_id, flags))

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    assigned_paths: set[str] = set()
    for path_key, run_id, case_id, flags in reconciled_findings:
        key = (run_id, case_id)
        if key not in grouped:
            grouped[key] = {
                "run_id": run_id,
                "case_id": case_id,
                "e2e_recoverable": False,
                "tool_events_recoverable": False,
                "model_request_events_recoverable": False,
                "token_counts_recoverable": False,
                "official_outcome_present": False,
                "sources": [],
            }
        record = grouped[key]
        for flag, value in flags.items():
            record[flag] = bool(record[flag] or value)
        if source_by_path[path_key] not in record["sources"]:
            record["sources"].append(source_by_path[path_key])
        assigned_paths.add(path_key)

    records = []
    flag_names = (
        "e2e_recoverable",
        "tool_events_recoverable",
        "model_request_events_recoverable",
        "token_counts_recoverable",
        "official_outcome_present",
    )
    for key in sorted(grouped):
        record = grouped[key]
        record["sources"].sort(key=lambda item: (item["path"], item["kind"]))
        record["complete_assignment_row_recoverable"] = all(record[name] for name in flag_names)
        records.append(record)

    unassigned = [source_by_path[key] for key in sorted(source_by_path) if key not in assigned_paths]
    skipped.sort(key=lambda item: (item["path"], item["reason"]))
    errors.sort(key=lambda item: (item["path"], item["reason"]))
    return {
        "schema_version": SCHEMA_VERSION,
        "roots": [
            {"root_id": root_ids[root], "basename": root.name}
            for root in resolved_roots
        ],
        "limits": {"max_file_bytes": max_file_bytes, "max_files": max_files},
        "summary": {
            "candidate_files_examined": examined_files,
            "complete_assignment_rows_recoverable": sum(
                1 for record in records if record["complete_assignment_row_recoverable"]
            ),
            "records": len(records),
            "skipped_paths": len(skipped),
            "sources": len(source_by_path),
            "unassigned_sources": len(unassigned),
        },
        "records": records,
        "unassigned_sources": unassigned,
        "skipped_paths": skipped,
        "errors": errors,
    }


def _atomic_write(path: Path, payload: bytes) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
        ) as handle:
            temporary_name = handle.name
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", action="append", required=True, type=Path, help="evidence root; repeatable")
    parser.add_argument("--json-out", type=Path, help="atomically write the deterministic report")
    parser.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    parser.add_argument("--max-file-bytes", type=int, default=DEFAULT_MAX_FILE_BYTES)
    args = parser.parse_args(argv)
    try:
        report = audit_roots(args.root, max_files=args.max_files, max_file_bytes=args.max_file_bytes)
    except AuditError as exc:
        parser.error(str(exc))
    payload = (json.dumps(report, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if args.json_out is not None:
        _atomic_write(args.json_out, payload)
        digest = hashlib.sha256(payload).hexdigest()
        sidecar = args.json_out.with_name(args.json_out.name + ".sha256")
        _atomic_write(sidecar, f"{digest}  {args.json_out.name}\n".encode("utf-8"))
    else:
        sys.stdout.buffer.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
