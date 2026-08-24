#!/usr/bin/env python3
"""Collect sealed H100 request telemetry from an interactive Nsight session.

This is the production ``H100_TRACE_PROVIDER``.  It never invents timings: a
row is successful only when Nsight Systems has produced a report and SQLite
export containing target-process CPU activity and at least one target-process
CUDA kernel intersecting the runner's CLOCK_MONOTONIC_RAW request window.

The vLLM process must already be launched in an Nsight Systems interactive
session.  The runner calls this program with ``arm`` immediately before a
request and ``collect`` immediately after it.  ``abort`` is best-effort cleanup
for a request which failed before telemetry could be collected.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shlex
import sqlite3
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping
from urllib.parse import quote


TRACE_SCHEMA = "h100-trace-summary.v1"
PROVIDER_VERSION = "h100-nsight-trace-provider.v1"
NSYS_COMMAND_TIMEOUT_SECONDS = 110.0
NSYS_CONTAINER_ENV = "H100_NSYS_CONTAINER"
NSYS_SESSION_ENV = "H100_NSYS_SESSION"
TRACE_MOUNT_ROOT_ENV = "H100_TRACE_MOUNT_ROOT"
TRACE_CONTAINER_ROOT_ENV = "H100_TRACE_CONTAINER_ROOT"


class ProviderError(RuntimeError):
    """A fail-closed provider error."""


def _fail(message: str) -> int:
    print(f"h100_nsight_trace_provider: {message}", file=sys.stderr)
    return 2


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _required_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProviderError(f"{label} is not an integer")
    return value


def _host_and_container_paths(output_dir: Path) -> tuple[Path, PurePosixPath]:
    mount_value = os.environ.get(TRACE_MOUNT_ROOT_ENV)
    if not mount_value:
        raise ProviderError(f"{TRACE_MOUNT_ROOT_ENV} must identify the host trace mount")
    mount_root = Path(mount_value).expanduser().resolve()
    host_dir = output_dir.expanduser().resolve()
    try:
        relative = host_dir.relative_to(mount_root)
    except ValueError as exc:
        raise ProviderError("request output directory is outside H100_TRACE_MOUNT_ROOT") from exc
    container_root = PurePosixPath(os.environ.get(TRACE_CONTAINER_ROOT_ENV, "/trace"))
    if not container_root.is_absolute():
        raise ProviderError(f"{TRACE_CONTAINER_ROOT_ENV} must be an absolute container path")
    container_dir = container_root.joinpath(*relative.parts)
    return host_dir, container_dir


def _docker_exec(arguments: Iterable[str]) -> subprocess.CompletedProcess[str]:
    container = os.environ.get(NSYS_CONTAINER_ENV, "h100-final-vllm")
    nsys_bin = os.environ.get("H100_NSYS_BIN", "/host-cuda/bin/nsys")
    command = ["docker", "exec", container, nsys_bin, *arguments]
    try:
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=NSYS_COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        rendered = " ".join(shlex.quote(part) for part in command)
        raise ProviderError(f"could not invoke Nsight command: {rendered}") from exc


def _command_details(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return {
        "returncode": int(result.returncode),
        "stdout_tail": result.stdout[-4000:],
        "stderr_tail": result.stderr[-4000:],
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--split", required=True)
    parser.add_argument("--input-tokens", required=True, type=int)
    parser.add_argument("--output-tokens", required=True, type=int)
    parser.add_argument("--repeat-id", required=True)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--action", required=True, choices=("arm", "collect", "abort"))
    parser.add_argument("--start-mono-ns", required=True, type=int)
    parser.add_argument("--end-mono-ns", required=True, type=int)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args()


def _trace_paths(host_dir: Path) -> dict[str, Path]:
    return {
        "arm": host_dir / "trace_arm.json",
        "collect": host_dir / "trace_collect.json",
        "report": host_dir / "trace.nsys-rep",
        "sqlite": host_dir / "trace.sqlite",
        "summary": host_dir / "trace_summary.json",
    }


def _arm(args: argparse.Namespace, host_dir: Path, container_dir: PurePosixPath) -> None:
    paths = _trace_paths(host_dir)
    if any(path.exists() for path in paths.values()):
        raise ProviderError("request output directory already contains trace artifacts")
    host_dir.mkdir(parents=True, exist_ok=True)
    raw_ns = time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)
    realtime_ns = time.time_ns()
    result = _docker_exec(
        (
            "start",
            f"--session={os.environ.get(NSYS_SESSION_ENV, 'h100-final-validation')}",
            "--sample=none",
            f"--output={container_dir / 'trace'}",
            "--force-overwrite=true",
            "--stats=true",
        )
    )
    if result.returncode != 0:
        raise ProviderError(f"Nsight start failed: {result.stderr[-1000:]}")
    _write_json_atomic(
        paths["arm"],
        {
            "schema_version": "h100-trace-arm.v1",
            "provenance": "measured",
            "provider_version": PROVIDER_VERSION,
            "action": "arm",
            "case_id": args.case_id,
            "split": args.split,
            "repeat_id": args.repeat_id,
            "phase": args.phase,
            "session": os.environ.get(NSYS_SESSION_ENV, "h100-final-validation"),
            "container": os.environ.get(NSYS_CONTAINER_ENV, "h100-final-vllm"),
            "container_trace_dir": str(container_dir),
            "clock_id": "CLOCK_MONOTONIC_RAW",
            "clock": {"raw_ns": raw_ns, "realtime_ns": realtime_ns},
            "nsys_start": _command_details(result),
        },
    )


def _abort(args: argparse.Namespace) -> None:
    session = os.environ.get(NSYS_SESSION_ENV, "h100-final-validation")
    try:
        result = _docker_exec(("stop", f"--session={session}"))
    except ProviderError as exc:
        print(f"h100_nsight_trace_provider: abort cleanup failed: {exc}", file=sys.stderr)
        return
    if result.returncode != 0:
        print(
            f"h100_nsight_trace_provider: abort cleanup returned {result.returncode}: "
            f"{result.stderr[-1000:]}",
            file=sys.stderr,
        )


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _processes(connection: sqlite3.Connection, tables: set[str]) -> tuple[dict[int, str], set[int]]:
    if "PROCESSES" not in tables:
        raise ProviderError("Nsight SQLite export has no PROCESSES table")
    columns = _table_columns(connection, "PROCESSES")
    if not {"globalPid", "name"}.issubset(columns):
        raise ProviderError("Nsight PROCESSES table lacks globalPid/name")
    all_processes: dict[int, str] = {}
    for global_pid, name in connection.execute('SELECT "globalPid", "name" FROM "PROCESSES"'):
        if global_pid is None:
            continue
        all_processes[int(global_pid)] = str(name or "")
    target_pids = {
        pid for pid, name in all_processes.items() if not name.lower().startswith("nsys")
    }
    if not target_pids:
        raise ProviderError("Nsight SQLite export has no non-Nsight target process")
    return all_processes, target_pids


def _target_for_tid(global_tid: Any, target_pids: set[int]) -> bool:
    if global_tid is None:
        return False
    try:
        tid = int(global_tid)
    except (TypeError, ValueError):
        return False
    # Nsight's globalTid is the target globalPid plus the Linux thread id
    # offset.  Use a bounded nearest-process match rather than assuming the
    # offset is a particular size across Nsight releases.
    return min(abs(tid - pid) for pid in target_pids) <= 1_000_000


def _event_intersection(
    event_start: Any,
    event_end: Any,
    session_epoch_ns: int,
    raw_minus_realtime_ns: int,
    request_start_ns: int,
    request_end_ns: int,
) -> tuple[int, int] | None:
    try:
        start = int(event_start)
        end = int(event_end)
    except (TypeError, ValueError):
        return None
    if end <= start or start < 0:
        return None
    raw_start = session_epoch_ns + start + raw_minus_realtime_ns
    raw_end = session_epoch_ns + end + raw_minus_realtime_ns
    clipped_start = max(raw_start, request_start_ns)
    clipped_end = min(raw_end, request_end_ns)
    if clipped_end <= clipped_start:
        return None
    return clipped_start, clipped_end


def _intervals_for_cpu(
    connection: sqlite3.Connection,
    tables: set[str],
    target_pids: set[int],
    session_epoch_ns: int,
    raw_minus_realtime_ns: int,
    request_start_ns: int,
    request_end_ns: int,
) -> tuple[list[tuple[int, int]], dict[str, int]]:
    intervals: list[tuple[int, int]] = []
    counts: dict[str, int] = {}
    for table in ("OSRT_API", "CUPTI_ACTIVITY_KIND_RUNTIME"):
        if table not in tables:
            continue
        columns = _table_columns(connection, table)
        if not {"start", "end", "globalTid"}.issubset(columns):
            continue
        count = 0
        query = f'SELECT "start", "end", "globalTid" FROM "{table}"'
        for start, end, global_tid in connection.execute(query):
            if not _target_for_tid(global_tid, target_pids):
                continue
            interval = _event_intersection(
                start,
                end,
                session_epoch_ns,
                raw_minus_realtime_ns,
                request_start_ns,
                request_end_ns,
            )
            if interval is not None:
                intervals.append(interval)
                count += 1
        counts[table] = count
    return intervals, counts


def _intervals_for_gpu(
    connection: sqlite3.Connection,
    tables: set[str],
    target_pids: set[int],
    session_epoch_ns: int,
    raw_minus_realtime_ns: int,
    request_start_ns: int,
    request_end_ns: int,
) -> tuple[list[tuple[int, int]], list[tuple[int, int]], dict[str, int]]:
    gpu_intervals: list[tuple[int, int]] = []
    kernel_intervals: list[tuple[int, int]] = []
    counts: dict[str, int] = {}
    for table in (
        "CUPTI_ACTIVITY_KIND_KERNEL",
        "CUPTI_ACTIVITY_KIND_MEMCPY",
        "CUPTI_ACTIVITY_KIND_MEMSET",
    ):
        if table not in tables:
            continue
        columns = _table_columns(connection, table)
        if not {"start", "end", "globalPid"}.issubset(columns):
            continue
        count = 0
        query = f'SELECT "start", "end", "globalPid" FROM "{table}"'
        for start, end, global_pid in connection.execute(query):
            if global_pid is None or int(global_pid) not in target_pids:
                continue
            interval = _event_intersection(
                start,
                end,
                session_epoch_ns,
                raw_minus_realtime_ns,
                request_start_ns,
                request_end_ns,
            )
            if interval is None:
                continue
            gpu_intervals.append(interval)
            if table == "CUPTI_ACTIVITY_KIND_KERNEL":
                kernel_intervals.append(interval)
            count += 1
        counts[table] = count
    return gpu_intervals, kernel_intervals, counts


def _merge(intervals: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    ordered = sorted(intervals)
    if not ordered:
        return []
    merged: list[tuple[int, int]] = [ordered[0]]
    for start, end in ordered[1:]:
        previous_start, previous_end = merged[-1]
        if start <= previous_end:
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


def _session_epoch(connection: sqlite3.Connection, tables: set[str]) -> int:
    if "TARGET_INFO_SESSION_START_TIME" not in tables:
        raise ProviderError("Nsight SQLite export has no session start-time table")
    row = connection.execute(
        'SELECT "utcEpochNs" FROM "TARGET_INFO_SESSION_START_TIME" LIMIT 1'
    ).fetchone()
    if not row or row[0] is None:
        raise ProviderError("Nsight session start-time table is empty")
    epoch = int(row[0])
    if epoch <= 0:
        raise ProviderError("Nsight session epoch is invalid")
    return epoch


def _parse_report(
    args: argparse.Namespace,
    paths: Mapping[str, Path],
    arm: Mapping[str, Any],
) -> dict[str, Any]:
    start_ns = _required_int(args.start_mono_ns, "start-mono-ns")
    end_ns = _required_int(args.end_mono_ns, "end-mono-ns")
    if end_ns <= start_ns:
        raise ProviderError("request window is not positive")
    clock = arm.get("clock")
    if not isinstance(clock, Mapping):
        raise ProviderError("trace_arm.json lacks clock calibration")
    raw_at_arm = _required_int(clock.get("raw_ns"), "arm raw clock")
    realtime_at_arm = _required_int(clock.get("realtime_ns"), "arm realtime clock")
    raw_minus_realtime = raw_at_arm - realtime_at_arm
    sqlite_path = paths["sqlite"]
    if not sqlite_path.is_file() or sqlite_path.stat().st_size == 0:
        raise ProviderError("Nsight stop did not produce a non-empty SQLite export")
    if not paths["report"].is_file() or paths["report"].stat().st_size == 0:
        raise ProviderError("Nsight stop did not produce a non-empty .nsys-rep artifact")
    uri = f"file:{quote(str(sqlite_path), safe='/')}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise ProviderError("could not open Nsight SQLite export read-only") from exc
    try:
        tables = _table_names(connection)
        session_epoch = _session_epoch(connection, tables)
        processes, target_pids = _processes(connection, tables)
        cpu_intervals, cpu_counts = _intervals_for_cpu(
            connection,
            tables,
            target_pids,
            session_epoch,
            raw_minus_realtime,
            start_ns,
            end_ns,
        )
        gpu_intervals, kernel_intervals, gpu_counts = _intervals_for_gpu(
            connection,
            tables,
            target_pids,
            session_epoch,
            raw_minus_realtime,
            start_ns,
            end_ns,
        )
    except (sqlite3.Error, ValueError, TypeError) as exc:
        raise ProviderError("could not parse Nsight SQLite export") from exc
    finally:
        connection.close()

    if not kernel_intervals:
        raise ProviderError("no target-process CUDA kernel intersects request window")
    if not cpu_intervals:
        raise ProviderError("no target-process CPU activity intersects request window")
    cpu_union = _merge(cpu_intervals)
    cuda_union = _merge(gpu_intervals)
    kernel_duration_ns = sum(end - start for start, end in kernel_intervals)
    cpu_union_ns = sum(end - start for start, end in cpu_union)
    cuda_union_ns = sum(end - start for start, end in cuda_union)
    values = {
        "cpu_activity_union_ms": cpu_union_ns / 1_000_000.0,
        "cuda_activity_union_ms": cuda_union_ns / 1_000_000.0,
        "kernel_duration_sum_ms": kernel_duration_ns / 1_000_000.0,
    }
    if not all(math.isfinite(value) and value >= 0 for value in values.values()):
        raise ProviderError("Nsight-derived timing values are not finite and non-negative")
    raw_artifacts = []
    for kind, key in (
        ("trace_arm", "arm"),
        ("trace_collect", "collect"),
        ("nsight_report", "report"),
        ("nsight_sqlite", "sqlite"),
    ):
        path = paths[key]
        if not path.is_file() or path.stat().st_size == 0:
            raise ProviderError(f"raw artifact is missing or empty: {path.name}")
        raw_artifacts.append({"kind": kind, "path": path.name, "sha256": _sha256(path)})
    target_processes = [
        {"globalPid": pid, "name": processes[pid]}
        for pid in sorted(target_pids)
        if pid in processes
    ]
    return {
        "schema_version": TRACE_SCHEMA,
        "provenance": "measured",
        "provider_version": PROVIDER_VERSION,
        "clock_id": "CLOCK_MONOTONIC_RAW",
        "cuda_union_rule": "overlap_aware_request_window",
        **values,
        "raw_artifacts": raw_artifacts,
        "measurement": {
            "source": "Nsight Systems SQLite CUPTI/OSRT activity",
            "session_epoch_utc_ns": session_epoch,
            "raw_minus_realtime_ns": raw_minus_realtime,
            "request_start_mono_ns": start_ns,
            "request_end_mono_ns": end_ns,
            "target_processes": target_processes,
            "cpu_event_counts": cpu_counts,
            "gpu_event_counts": gpu_counts,
            "kernel_event_count": len(kernel_intervals),
        },
    }


def _collect(args: argparse.Namespace, host_dir: Path, container_dir: PurePosixPath) -> None:
    paths = _trace_paths(host_dir)
    if not paths["arm"].is_file():
        raise ProviderError("collect called without trace_arm.json")
    try:
        arm = json.loads(paths["arm"].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProviderError("trace_arm.json is unreadable") from exc
    if not isinstance(arm, Mapping) or arm.get("clock_id") != "CLOCK_MONOTONIC_RAW":
        raise ProviderError("trace_arm.json has invalid provenance")
    session = os.environ.get(NSYS_SESSION_ENV, "h100-final-validation")
    result = _docker_exec(("stop", f"--session={session}"))
    _write_json_atomic(
        paths["collect"],
        {
            "schema_version": "h100-trace-collect.v1",
            "provenance": "measured",
            "provider_version": PROVIDER_VERSION,
            "action": "collect",
            "session": session,
            "container_trace_dir": str(container_dir),
            "clock": {
                "raw_ns": time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW),
                "realtime_ns": time.time_ns(),
            },
            "nsys_stop": _command_details(result),
        },
    )
    if result.returncode != 0:
        raise ProviderError(f"Nsight stop failed: {result.stderr[-1000:]}")
    summary = _parse_report(args, paths, arm)
    _write_json_atomic(paths["summary"], summary)


def main() -> int:
    args = _parse_args()
    try:
        host_dir, container_dir = _host_and_container_paths(args.output_dir)
        if args.action == "arm":
            _arm(args, host_dir, container_dir)
        elif args.action == "collect":
            _collect(args, host_dir, container_dir)
        else:
            _abort(args)
        return 0
    except ProviderError as exc:
        return _fail(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
