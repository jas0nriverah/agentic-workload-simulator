#!/usr/bin/env python3
"""Fail-closed recurring monitor for the existing shared case queue.

This is deliberately a small operator layer around
``scripts/assignment/shared_case_queue.py``.  It does not create a queue,
assign a case, or infer a successful run from a missing process.  A root-owned
launch authorization, frozen plan/configuration, and a separate PASS receipt
must all be present and hash-bound before the optional ``--execute`` mode can
start an existing queue supervisor.

The module is staged outside the source checkout while the current combined
trajectory is frozen.  Copy it into the reviewed source only after the
production launch contract is accepted.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


MONITOR_SCHEMA = "assignment.acquisition-monitor.v1"
AUTH_SCHEMA = "assignment.production-launch-authorization.v1"
PASS_SCHEMA = "assignment.production-launch-pass.v1"
CHILD_SCHEMA = "assignment.acquisition-monitor-supervisor-launch.v1"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
WORKER_ID = re.compile(r"^worker-[0-9]{2}$")
MAX_JSON_BYTES = 16 * 1024 * 1024
QUEUE_SCHEMA = "assignment.shared-case-queue.v2"
QUEUE_DB_NAME = "queue.sqlite3"
QUEUE_STATUSES = {"pending", "running", "accepted", "retry_waiting", "orphaned", "blocked"}
ATTEMPT_STATUSES = {"active", "accepted", "retryable", "requeued", "orphaned", "blocked"}


class MonitorError(RuntimeError):
    """A fail-closed monitor precondition or integrity failure."""


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _reject_symlink_chain(path: Path) -> None:
    path = path.absolute()
    for parent in (path, *path.parents):
        if parent == parent.parent:
            break
        try:
            if parent.is_symlink():
                raise MonitorError(f"symlink is not allowed in bound path: {path}")
        except OSError as exc:
            raise MonitorError(f"cannot inspect bound path {path}: {exc}") from exc


def _regular(path: Path, label: str, *, executable: bool = False, allow_symlink: bool = False) -> Path:
    path = path.absolute()
    if not path.is_absolute():
        raise MonitorError(f"{label} must be absolute")
    if not allow_symlink:
        _reject_symlink_chain(path)
    if not path.is_file():
        raise MonitorError(f"{label} is not a regular file: {path}")
    if executable and not os.access(path, os.X_OK):
        raise MonitorError(f"{label} is not executable: {path}")
    return path


def _read_json(path: Path, label: str) -> Dict[str, Any]:
    path = _regular(path, label)
    try:
        size = path.stat().st_size
        if size > MAX_JSON_BYTES:
            raise MonitorError(f"{label} is unexpectedly large")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MonitorError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise MonitorError(f"{label} must contain a JSON object")
    return value


def _require_sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or HEX64.fullmatch(value) is None:
        raise MonitorError(f"{label} must be a lowercase SHA-256")
    return value


def _bound_file(value: Any, label: str, *, executable: bool = False) -> Tuple[Path, str]:
    if not isinstance(value, Mapping):
        raise MonitorError(f"{label} binding is malformed")
    path_value = value.get("path")
    if not isinstance(path_value, str) or not Path(path_value).is_absolute():
        raise MonitorError(f"{label}.path must be absolute")
    path = _regular(Path(path_value), label, executable=executable)
    declared = _require_sha(value.get("sha256"), f"{label}.sha256")
    actual = sha256_file(path)
    if actual != declared:
        raise MonitorError(f"{label} changed after authorization")
    return path, actual


def _bound_python(value: Any) -> Path:
    """Bind the interpreter without rejecting normal venv symlinks."""

    if not isinstance(value, str) or not Path(value).is_absolute():
        raise MonitorError("supervisor.python must be an absolute path")
    path = Path(value).absolute()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise MonitorError(f"supervisor.python is not executable: {path}")
    return path


def _worker_ids_digest(worker_ids: Iterable[str]) -> str:
    return _sha256_bytes((_canonical(sorted(worker_ids)) + "\n").encode("utf-8"))


def _nonempty_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise MonitorError(f"{label} must be a non-empty text value")
    return value


def _read_bound_receipt(value: Any, auth_id: str, bindings: Mapping[str, str]) -> Tuple[Path, str]:
    path, digest = _bound_file(value, "pass_receipt")
    receipt = _read_json(path, "pass_receipt")
    if receipt.get("schema_version") != PASS_SCHEMA or receipt.get("status") != "PASS":
        raise MonitorError("pass_receipt is not an explicit PASS receipt")
    if receipt.get("authorization_id") != auth_id:
        raise MonitorError("pass_receipt authorization identity differs")
    for field in ("plan_sha256", "frozen_config_sha256"):
        if receipt.get(field) != bindings[field]:
            raise MonitorError(f"pass_receipt does not bind {field}")
    if receipt.get("launch_authorized") is True:
        raise MonitorError("PASS receipt cannot itself grant launch authorization")
    return path, digest


def validate_authorization(path: Path, *, queue_override: Optional[Path] = None) -> Dict[str, Any]:
    """Validate all immutable launch inputs and return a normalized binding."""

    path = _regular(path, "authorization")
    auth_digest = sha256_file(path)
    raw = _read_json(path, "authorization")
    if raw.get("schema_version") != AUTH_SCHEMA:
        raise MonitorError("authorization schema is unsupported")
    if raw.get("status") != "PASS" or raw.get("launch_enabled") is not True:
        raise MonitorError("authorization is not an explicit PASS/launch-enabled record")
    if raw.get("written_by") != "root":
        raise MonitorError("authorization is not marked as root-written")
    auth_id = _nonempty_text(raw.get("authorization_id"), "authorization_id")
    if raw.get("acknowledge_paid_gpu_work") is not True:
        raise MonitorError("authorization must explicitly acknowledge paid GPU work")

    queue_value = raw.get("queue")
    if not isinstance(queue_value, Mapping):
        raise MonitorError("queue binding is missing")
    queue_value = dict(queue_value)
    queue_value["plan_sha256"] = _require_sha(queue_value.get("plan_sha256"), "queue.plan_sha256")
    queue_value_path = queue_value.get("dir")
    if not isinstance(queue_value_path, str) or not Path(queue_value_path).is_absolute():
        raise MonitorError("queue.dir must be absolute")
    queue_dir = Path(queue_value_path).absolute()
    _reject_symlink_chain(queue_dir)
    if not queue_dir.is_dir():
        raise MonitorError(f"queue.dir is not a directory: {queue_dir}")
    if queue_override is not None and queue_dir != queue_override.absolute():
        raise MonitorError("--queue-dir differs from the root authorization")
    db_path = queue_dir / QUEUE_DB_NAME
    _regular(db_path, "queue database")
    initial_db_sha = queue_value.get("initial_db_sha256")
    if initial_db_sha is not None:
        initial_db_sha = _require_sha(initial_db_sha, "queue.initial_db_sha256")

    plan_path, plan_sha = _bound_file(raw.get("plan"), "plan")
    frozen_path, frozen_sha = _bound_file(raw.get("frozen_config"), "frozen_config")
    if plan_sha != queue_value["plan_sha256"]:
        raise MonitorError("queue.plan_sha256 differs from bound plan")

    workers_value = raw.get("workers")
    if not isinstance(workers_value, list) or not workers_value:
        raise MonitorError("workers must be a non-empty predeclared list")
    workers: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(workers_value):
        if not isinstance(item, Mapping):
            raise MonitorError(f"workers[{index}] is malformed")
        worker_id = _nonempty_text(item.get("worker_id"), f"workers[{index}].worker_id")
        if WORKER_ID.fullmatch(worker_id) is None or worker_id == "worker-11":
            raise MonitorError(f"workers[{index}] is not an allowed worker identifier")
        if worker_id in seen:
            raise MonitorError(f"duplicate predeclared worker: {worker_id}")
        seen.add(worker_id)
        endpoint_id = item.get("endpoint_id")
        if endpoint_id is not None:
            endpoint_id = _nonempty_text(endpoint_id, f"workers[{index}].endpoint_id")
        workers.append({"worker_id": worker_id, "endpoint_id": endpoint_id})
    worker_digest = _worker_ids_digest(seen)
    if raw.get("worker_ids_sha256") is not None and _require_sha(raw["worker_ids_sha256"], "worker_ids_sha256") != worker_digest:
        raise MonitorError("authorization worker list hash differs")

    supervisor_value = raw.get("supervisor")
    if not isinstance(supervisor_value, Mapping):
        raise MonitorError("supervisor binding is missing")
    supervisor = dict(supervisor_value)
    python_path = _bound_python(supervisor.get("python"))
    # The queue CLI is intentionally invoked as ``python queue_script.py``;
    # current source-v6 snapshots are mode 0644. Bind readable regular bytes,
    # while the runner below remains a directly executable entry point.
    queue_script, queue_script_sha = _bound_file(supervisor.get("queue_script"), "supervisor.queue_script")
    runner_path, runner_sha = _bound_file(supervisor.get("runner"), "supervisor.runner", executable=True)
    if supervisor.get("queue_script_sha256") is not None and _require_sha(supervisor["queue_script_sha256"], "supervisor.queue_script_sha256") != queue_script_sha:
        raise MonitorError("supervisor queue script hash differs")
    if supervisor.get("runner_sha256") is not None and _require_sha(supervisor["runner_sha256"], "supervisor.runner_sha256") != runner_sha:
        raise MonitorError("supervisor runner hash differs")
    optional_bindings: Dict[str, Tuple[Path, str]] = {}
    for key in ("extra_argv_manifest", "confirmation_plan"):
        if supervisor.get(key) is not None:
            optional_bindings[key] = _bound_file(supervisor[key], f"supervisor.{key}")
    cwd = supervisor.get("cwd")
    if cwd is not None:
        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
            raise MonitorError("supervisor.cwd must be absolute")
        cwd_path = Path(cwd).absolute()
        _reject_symlink_chain(cwd_path)
        if not cwd_path.is_dir():
            raise MonitorError("supervisor.cwd is not a directory")
    else:
        cwd_path = None
    max_retries = supervisor.get("max_retries", 3)
    if type(max_retries) is not int or max_retries < 0 or max_retries > 100:
        raise MonitorError("supervisor.max_retries is invalid")
    timeout_seconds = supervisor.get("timeout_seconds")
    if timeout_seconds is not None and (type(timeout_seconds) not in (int, float) or timeout_seconds <= 0):
        raise MonitorError("supervisor.timeout_seconds is invalid")
    if supervisor.get("cpu_docker", False) is not False and supervisor.get("cpu_docker") is not True:
        raise MonitorError("supervisor.cpu_docker must be boolean")
    max_launches = raw.get("max_launches_per_tick", len(workers))
    if type(max_launches) is not int or not 0 < max_launches <= len(workers):
        raise MonitorError("max_launches_per_tick is invalid")
    receipt_path, receipt_sha = _read_bound_receipt(
        raw.get("pass_receipt"), auth_id, {"plan_sha256": plan_sha, "frozen_config_sha256": frozen_sha}
    )
    return {
        "path": path,
        "sha256": auth_digest,
        "authorization_id": auth_id,
        "queue_dir": queue_dir,
        "db_path": db_path,
        "plan_path": plan_path,
        "plan_sha256": plan_sha,
        "frozen_config_path": frozen_path,
        "frozen_config_sha256": frozen_sha,
        "initial_db_sha256": initial_db_sha,
        "pass_receipt_path": receipt_path,
        "pass_receipt_sha256": receipt_sha,
        "workers": workers,
        "worker_ids": sorted(seen),
        "worker_ids_sha256": worker_digest,
        "supervisor": {
            "python": python_path,
            "queue_script": queue_script,
            "queue_script_sha256": queue_script_sha,
            "runner": runner_path,
            "runner_sha256": runner_sha,
            "extra_argv_manifest": optional_bindings.get("extra_argv_manifest"),
            "confirmation_plan": optional_bindings.get("confirmation_plan"),
            "cwd": cwd_path,
            "max_retries": max_retries,
            "timeout_seconds": timeout_seconds,
            "cpu_docker": bool(supervisor.get("cpu_docker", False)),
        },
        "allow_supervisor_launch": raw.get("allow_supervisor_launch") is True,
        "allow_resume_after_supervisor_exit": raw.get("allow_resume_after_supervisor_exit") is True,
        "max_launches_per_tick": max_launches,
        "raw": raw,
    }


def _meta(connection: sqlite3.Connection) -> Dict[str, Any]:
    try:
        rows = connection.execute("SELECT key, value_json FROM meta").fetchall()
    except sqlite3.Error as exc:
        raise MonitorError(f"queue meta table is unreadable: {exc}") from exc
    values: Dict[str, Any] = {}
    for key, encoded in rows:
        try:
            values[str(key)] = json.loads(encoded)
        except (TypeError, json.JSONDecodeError) as exc:
            raise MonitorError(f"queue metadata {key!r} is not JSON") from exc
    return values


def _diagnostic_file_sha(path: Path) -> Optional[str]:
    """Return a best-effort file hash for diagnostics, never a gate."""

    try:
        return sha256_file(path) if path.is_file() else None
    except OSError:
        return None


def read_queue_snapshot(auth: Mapping[str, Any]) -> Dict[str, Any]:
    """Read one SQLite snapshot and validate its identity.

    Queue supervisors are expected to update the database while this monitor
    runs. A raw database-file hash therefore cannot be an integrity gate.
    The explicit read transaction gives all queried tables one consistent
    snapshot; database/WAL/SHM hashes remain diagnostic only.
    """

    db_path = Path(auth["db_path"])
    wal_path = Path(f"{db_path}-wal")
    shm_path = Path(f"{db_path}-shm")
    diagnostic_before = {
        "db_file_sha256": _diagnostic_file_sha(db_path),
        "wal_file_sha256": _diagnostic_file_sha(wal_path),
        "shm_file_sha256": _diagnostic_file_sha(shm_path),
    }
    uri = f"file:{db_path}?mode=ro"
    connection: Optional[sqlite3.Connection] = None
    snapshot: Dict[str, Any]
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise MonitorError(f"queue SQLite integrity check failed: {integrity}")
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if user_version != 2:
            raise MonitorError(f"unsupported queue user_version: {user_version}")
        meta = _meta(connection)
        if meta.get("schema_version") != QUEUE_SCHEMA:
            raise MonitorError("queue schema identity differs")
        if meta.get("plan_sha256") != auth["plan_sha256"]:
            raise MonitorError("queue plan identity differs from authorization")
        allowed = meta.get("allowed_worker_ids")
        if not isinstance(allowed, list) or sorted(str(item) for item in allowed) != auth["worker_ids"]:
            raise MonitorError("queue allowed worker set differs from authorization")
        case_rows = connection.execute("SELECT status, COUNT(*) FROM cases GROUP BY status").fetchall()
        counts = {str(row[0]): int(row[1]) for row in case_rows}
        if set(counts) - QUEUE_STATUSES:
            raise MonitorError("queue contains an unknown case status")
        worker_rows = connection.execute("SELECT worker_id, endpoint_id, enabled FROM workers ORDER BY worker_id").fetchall()
        registered = {
            str(row[0]): {"endpoint_id": str(row[1]), "enabled": bool(row[2])}
            for row in worker_rows
        }
        expected = {row["worker_id"]: row.get("endpoint_id") for row in auth["workers"]}
        if not set(registered).issubset(set(expected)):
            raise MonitorError("queue has a worker outside the authorization")
        for worker_id, row in registered.items():
            if expected[worker_id] is not None and expected[worker_id] != row["endpoint_id"]:
                raise MonitorError(f"queue endpoint binding differs for {worker_id}")
        attempt_rows = connection.execute(
            "SELECT attempt_id, case_id, worker_id, endpoint_id, status, launch_state, "
            "runner_pid, runner_start_ticks, runner_boot_id, runner_exit_json "
            "FROM attempts WHERE status IN ('active', 'orphaned') ORDER BY attempt_id"
        ).fetchall()
        attempts: List[Dict[str, Any]] = []
        for row in attempt_rows:
            item = {"attempt_id": str(row[0]), "case_id": str(row[1]), "worker_id": str(row[2]),
                    "endpoint_id": str(row[3]), "status": str(row[4]), "launch_state": str(row[5]),
                    "runner_pid": row[6], "runner_start_ticks": row[7], "runner_boot_id": row[8],
                    "runner_exit_json": row[9]}
            if item["worker_id"] not in expected:
                raise MonitorError("active queue attempt belongs to an unauthorized worker")
            if item["status"] not in {"active", "orphaned"}:
                raise MonitorError("queue returned an invalid active-attempt status")
            attempts.append(item)
        halted = meta.get("dispatch_halted") is True
        halt_reason = meta.get("halt_reason") if halted else None
        snapshot = {
            "user_version": user_version,
            "meta": meta,
            "case_status_counts": counts,
            "registered_workers": registered,
            "active_or_orphaned_attempts": attempts,
            "dispatch_halted": halted,
            "halt_reason": halt_reason,
        }
    except sqlite3.Error as exc:
        raise MonitorError(f"queue database read failed: {exc}") from exc
    finally:
        with contextlib.suppress(Exception):
            if connection is not None:
                connection.rollback()
                connection.close()
    diagnostic_after = {
        "db_file_sha256": _diagnostic_file_sha(db_path),
        "wal_file_sha256": _diagnostic_file_sha(wal_path),
        "shm_file_sha256": _diagnostic_file_sha(shm_path),
    }
    snapshot_sha = _sha256_bytes((_canonical(snapshot) + "\n").encode("utf-8"))
    return {
        # Keep the historical key as the canonical queried-snapshot identity;
        # raw file hashes below are diagnostics and never gate progress.
        "db_sha256": snapshot_sha,
        "snapshot_sha256": snapshot_sha,
        "diagnostic_file_hashes_before": diagnostic_before,
        "diagnostic_file_hashes_after": diagnostic_after,
        "schema_version": QUEUE_SCHEMA,
        "plan_sha256": auth["plan_sha256"],
        "case_status_counts": snapshot["case_status_counts"],
        "registered_workers": snapshot["registered_workers"],
        "active_or_orphaned_attempts": snapshot["active_or_orphaned_attempts"],
        "dispatch_halted": snapshot["dispatch_halted"],
        "halt_reason": snapshot["halt_reason"],
        "advisories": meta.get("advisories"),
    }


def _boot_id() -> Optional[str]:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        return None


def process_identity(pid: Any) -> Dict[str, Any]:
    if type(pid) is not int or pid <= 0:
        return {"status": "invalid", "pid": pid}
    proc = Path("/proc") / str(pid)
    try:
        raw = (proc / "stat").read_text(encoding="ascii")
        tail = raw[raw.rfind(")") + 2 :].split()
        start_ticks = int(tail[19])
        # A receipt-writing child can briefly remain as a zombie after it has
        # durably recorded its exit. Treat that as exited so the receipt path
        # is checked rather than allowing a stale PID to block resumption.
        if tail[0] == "Z":
            return {"status": "absent", "pid": pid, "start_ticks": start_ticks, "boot_id": _boot_id(), "reason": "zombie"}
        return {"status": "alive", "pid": pid, "start_ticks": start_ticks, "boot_id": _boot_id()}
    except FileNotFoundError:
        return {"status": "absent", "pid": pid}
    except (OSError, ValueError, IndexError) as exc:
        return {"status": "unknown", "pid": pid, "error": type(exc).__name__}


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path = path.absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(5)}.tmp")
    payload = (_canonical(value) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(str(temporary), flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(str(path.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def _append_event(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (_canonical(value) + "\n").encode("utf-8")
    with path.open("ab") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"schema_version": MONITOR_SCHEMA, "supervisors": {}}
    value = _read_json(path, "monitor state")
    if value.get("schema_version") != MONITOR_SCHEMA or not isinstance(value.get("supervisors"), dict):
        raise MonitorError("monitor state schema is invalid")
    return value


def _merge_launch_records(state_dir: Path, state: Dict[str, Any], worker_ids: Sequence[str]) -> List[str]:
    """Recover launch intents if cron died after Popen but before state write."""

    launches_dir = state_dir / "launches"
    if not launches_dir.exists():
        return []
    failures: List[str] = []
    active_records: Dict[str, List[Dict[str, Any]]] = {}
    closed_records: Dict[str, List[Dict[str, Any]]] = {}
    for path in sorted(launches_dir.glob("*.json")):
        if path.name.endswith(".exit.json"):
            continue
        try:
            record = _read_json(path, "supervisor launch record")
        except MonitorError as exc:
            failures.append(str(exc))
            continue
        if record.get("schema_version") != CHILD_SCHEMA:
            failures.append(f"unsupported supervisor launch record: {path.name}")
            continue
        worker_id = record.get("worker_id")
        if worker_id not in worker_ids:
            failures.append(f"launch record names an unauthorized worker: {worker_id}")
            continue
        status = record.get("status")
        if status in {"spawn_intent", "spawned"}:
            # A receipt written by an older child closes the record even if
            # that child could not update its launch JSON afterwards.
            if status == "spawned" and record.get("exit_receipt") and Path(str(record["exit_receipt"])).is_file():
                closed_records.setdefault(str(worker_id), []).append(record)
            else:
                active_records.setdefault(str(worker_id), []).append(record)
        elif status in {"receipt_written", "completed", "closed"}:
            closed_records.setdefault(str(worker_id), []).append(record)
    for worker_id, records in active_records.items():
        if len(records) > 1:
            failures.append(f"multiple unfinished launch records exist for {worker_id}")
            continue
        record = records[0]
        if record.get("status") == "spawn_intent" or type(record.get("pid")) is not int:
            failures.append(f"launch intent has no durable child identity for {worker_id}")
            continue
        state_value = state.setdefault("supervisors", {}).get(worker_id)
        candidate = {
            "worker_id": worker_id,
            "launch_id": record.get("launch_id"),
            "pid": record.get("pid"),
            "start_ticks": record.get("start_ticks"),
            "boot_id": record.get("boot_id"),
            "argv_sha256": record.get("argv_sha256"),
            "authorization_id": record.get("authorization_id"),
            "authorization_sha256": record.get("authorization_sha256"),
            "exit_receipt": record.get("exit_receipt"),
            "launch_record": str(launches_dir / f"{record.get('launch_id')}.json"),
        }
        if state_value is not None and any(state_value.get(key) != candidate.get(key) for key in ("launch_id", "pid", "start_ticks", "boot_id", "argv_sha256")):
            failures.append(f"monitor state differs from durable launch record for {worker_id}")
        else:
            state.setdefault("supervisors", {})[worker_id] = candidate
    # Closed records remain in place as the audit trail, but do not count as
    # unfinished launches. Recover the newest one only if a monitor crash left
    # no state entry; a newer active/state entry wins.
    for worker_id, records in closed_records.items():
        records.sort(key=lambda row: (int(row.get("finished_epoch_ns", 0)), str(row.get("launch_id", ""))))
        record = records[-1]
        if state.setdefault("supervisors", {}).get(worker_id) is not None:
            continue
        state.setdefault("supervisors", {})[worker_id] = {
            "worker_id": worker_id,
            "launch_id": record.get("launch_id"),
            "pid": record.get("pid"),
            "start_ticks": record.get("start_ticks"),
            "boot_id": record.get("boot_id"),
            "argv_sha256": record.get("argv_sha256"),
            "authorization_id": record.get("authorization_id"),
            "authorization_sha256": record.get("authorization_sha256"),
            "exit_receipt": record.get("exit_receipt"),
            "launch_record": str(launches_dir / f"{record.get('launch_id')}.json"),
        }
    return failures


def build_supervisor_argv(auth: Mapping[str, Any], worker_id: str) -> List[str]:
    if worker_id not in auth["worker_ids"]:
        raise MonitorError(f"worker is not predeclared: {worker_id}")
    spec = auth["supervisor"]
    argv = [
        str(spec["python"]), str(spec["queue_script"]), "supervise",
        "--queue-dir", str(auth["queue_dir"]), "--worker-id", worker_id,
        "--runner", str(spec["runner"]), "--max-retries", str(spec["max_retries"]),
        "--execute", "--acknowledge-paid-gpu-work",
    ]
    if spec["cwd"] is not None:
        argv += ["--cwd", str(spec["cwd"])]
    if spec["timeout_seconds"] is not None:
        argv += ["--timeout-seconds", str(spec["timeout_seconds"])]
    if spec["cpu_docker"]:
        argv.append("--cpu-docker")
    if spec["extra_argv_manifest"] is not None:
        path, digest = spec["extra_argv_manifest"]
        argv += ["--extra-argv-manifest", str(path), "--extra-argv-manifest-sha256", digest]
    if spec["confirmation_plan"] is not None:
        path, digest = spec["confirmation_plan"]
        argv += ["--confirmation-plan", str(path), "--confirmation-plan-sha256", digest]
    return argv


def _verify_exit_receipt(path: Path, launch: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    value = _read_json(path, "supervisor exit receipt")
    if value.get("schema_version") != CHILD_SCHEMA:
        raise MonitorError("supervisor exit receipt schema is invalid")
    for key in ("launch_id", "worker_id", "argv_sha256"):
        if value.get(key) != launch.get(key):
            raise MonitorError(f"supervisor exit receipt {key} differs")
    if value.get("authorization_id") != launch.get("authorization_id") or value.get("authorization_sha256") != launch.get("authorization_sha256"):
        raise MonitorError("supervisor exit receipt authorization differs")
    guard_identity = value.get("guard_identity")
    if not isinstance(guard_identity, Mapping) or value.get("guard_pid") != launch.get("pid"):
        raise MonitorError("supervisor exit receipt guardian identity differs")
    if guard_identity.get("start_ticks") != launch.get("start_ticks") or guard_identity.get("boot_id") != launch.get("boot_id"):
        raise MonitorError("supervisor exit receipt guardian start identity differs")
    if type(value.get("returncode")) is not int:
        raise MonitorError("supervisor exit receipt return code is invalid")
    return value


def _classify_supervisors(state: Mapping[str, Any], queue: Mapping[str, Any], auth: Mapping[str, Any]) -> Tuple[Dict[str, Any], List[str], set[str]]:
    result: Dict[str, Any] = {}
    failures: List[str] = []
    live_workers: set[str] = set()
    active_by_worker = {row["worker_id"]: row for row in queue["active_or_orphaned_attempts"]}
    for worker_id, launch_value in state.get("supervisors", {}).items():
        if worker_id not in auth["worker_ids"] or not isinstance(launch_value, Mapping):
            failures.append(f"state contains an unauthorized supervisor entry: {worker_id}")
            continue
        launch = dict(launch_value)
        pid_state = process_identity(launch.get("pid"))
        exit_path = Path(str(launch.get("exit_receipt", ""))) if launch.get("exit_receipt") else None
        if pid_state["status"] == "alive":
            if pid_state.get("start_ticks") != launch.get("start_ticks") or pid_state.get("boot_id") != launch.get("boot_id"):
                failures.append(f"supervisor PID identity changed for {worker_id}")
                status = "pid_identity_mismatch"
            else:
                live_workers.add(worker_id)
                status = "running"
        elif pid_state["status"] == "absent":
            if exit_path is None:
                failures.append(f"supervisor vanished without an exit receipt: {worker_id}")
                status = "missing_exit_receipt"
            else:
                receipt = _verify_exit_receipt(exit_path, launch)
                if receipt is None:
                    failures.append(f"supervisor vanished without an exit receipt: {worker_id}")
                    status = "missing_exit_receipt"
                else:
                    # A successful guardian exit still does not prove case
                    # success, but an incomplete/nonzero guardian is an
                    # explicit recovery incident and must never be retried by
                    # a recurring monitor tick.
                    if receipt.get("completed") is not True or receipt.get("returncode") != 0:
                        failures.append(
                            f"supervisor exited unsuccessfully for {worker_id}: "
                            f"completed={receipt.get('completed')!r}, returncode={receipt.get('returncode')!r}"
                        )
                        status = "exit_failure_requires_review"
                    else:
                        status = "exited_with_receipt_unverified"
        elif pid_state["status"] == "unknown":
            failures.append(f"supervisor process identity is unavailable: {worker_id}")
            status = "identity_unavailable"
        else:
            failures.append(f"supervisor process identity is malformed: {worker_id}")
            status = "identity_invalid"
        result[worker_id] = {
            "status": status,
            "pid": launch.get("pid"),
            "start_ticks": launch.get("start_ticks"),
            "boot_id": launch.get("boot_id"),
            "argv_sha256": launch.get("argv_sha256"),
            "active_attempt": active_by_worker.get(worker_id),
        }
    for row in queue["active_or_orphaned_attempts"]:
        if row["worker_id"] not in state.get("supervisors", {}):
            failures.append(f"queue attempt has no monitor-owned supervisor record: {row['attempt_id']}")
        if row["status"] == "orphaned":
            failures.append(f"queue attempt requires explicit orphan reconciliation: {row['attempt_id']}")
    return result, failures, live_workers


def _status_payload(*, run_id: str, status: str, reason: Optional[str], started_ns: int, **extra: Any) -> Dict[str, Any]:
    return {
        "schema_version": MONITOR_SCHEMA,
        "run_id": run_id,
        "status": status,
        "reason": reason,
        "started_epoch_ns": started_ns,
        "finished_epoch_ns": time.time_ns(),
        **extra,
    }


def _write_outcome(state_dir: Path, payload: Mapping[str, Any]) -> None:
    _atomic_json(state_dir / "monitor_status.json", payload)
    _append_event(state_dir / "monitor_events.jsonl", payload)


def _spawn_supervisor(auth: Mapping[str, Any], state_dir: Path, worker_id: str, argv: Sequence[str]) -> Dict[str, Any]:
    """Create a durable launch intent, then start a receipt-writing child."""

    launch_id = f"{worker_id}-{secrets.token_hex(12)}"
    launches_dir = state_dir / "launches"
    logs_dir = state_dir / "logs"
    launches_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    logs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    launch_path = launches_dir / f"{launch_id}.json"
    exit_path = launches_dir / f"{launch_id}.exit.json"
    argv_digest = _sha256_bytes((_canonical(list(argv)) + "\n").encode("utf-8"))
    record = {
        "schema_version": CHILD_SCHEMA,
        "launch_id": launch_id,
        "worker_id": worker_id,
        "authorization_id": auth["authorization_id"],
        "authorization_sha256": auth["sha256"],
        "queue_db_sha256": sha256_file(Path(auth["db_path"])),
        "argv": list(argv),
        "argv_sha256": argv_digest,
        "exit_receipt": str(exit_path),
        "status": "spawn_intent",
        "created_epoch_ns": time.time_ns(),
    }
    _atomic_json(launch_path, record)
    stdout = (logs_dir / f"{launch_id}.stdout.log").open("ab")
    stderr = (logs_dir / f"{launch_id}.stderr.log").open("ab")
    try:
        child_argv = [str(auth["supervisor"]["python"]), str(Path(__file__).absolute()), "--supervisor-child", "--launch-record", str(launch_path)]
        process = subprocess.Popen(child_argv, stdout=stdout, stderr=stderr, stdin=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    except Exception:
        stdout.close()
        stderr.close()
        raise
    finally:
        with contextlib.suppress(Exception):
            stdout.close()
        with contextlib.suppress(Exception):
            stderr.close()
    identity = process_identity(process.pid)
    if identity["status"] != "alive":
        raise MonitorError("supervisor child did not have a live identity after spawn")
    # The child can finish a trivial supervisor before this parent has written
    # its identity. Preserve a receipt-written close if it won that race;
    # otherwise publish the parent's same child identity as usual.
    try:
        latest = _read_json(launch_path, "supervisor launch record")
    except MonitorError:
        latest = record
    if latest.get("launch_id") != launch_id or latest.get("argv_sha256") != argv_digest:
        raise MonitorError("supervisor launch record changed during spawn")
    if latest.get("status") not in {"receipt_written", "completed", "closed"}:
        latest.update({"status": "spawned", "pid": process.pid, "start_ticks": identity.get("start_ticks"), "boot_id": identity.get("boot_id")})
        record = latest
        _atomic_json(launch_path, record)
    else:
        record = latest
    return {"worker_id": worker_id, "launch_id": launch_id, "pid": process.pid, "start_ticks": identity.get("start_ticks"), "boot_id": identity.get("boot_id"), "argv_sha256": argv_digest, "authorization_id": auth["authorization_id"], "authorization_sha256": auth["sha256"], "exit_receipt": str(exit_path), "launch_record": str(launch_path)}


def supervisor_child(record_path: Path) -> int:
    record = _read_json(record_path, "supervisor launch record")
    if record.get("schema_version") != CHILD_SCHEMA or record.get("status") not in {"spawn_intent", "spawned"}:
        raise MonitorError("supervisor launch record is not executable")
    argv = record.get("argv")
    if not isinstance(argv, list) or not argv or any(not isinstance(item, str) or not item or "\x00" in item for item in argv):
        raise MonitorError("supervisor launch argv is malformed")
    digest = _sha256_bytes((_canonical(argv) + "\n").encode("utf-8"))
    if digest != record.get("argv_sha256"):
        raise MonitorError("supervisor launch argv changed")
    started = time.time_ns()
    completed = False
    returncode = 125
    try:
        returncode = subprocess.call(argv, start_new_session=True)
        completed = True
        return returncode
    finally:
        # Reload the parent's durable identity if it won the spawn race. If
        # the child finished before that write, bind this guard's own identity
        # so the receipt and closed launch record still have a valid join key.
        with contextlib.suppress(MonitorError):
            latest = _read_json(record_path, "supervisor launch record")
            if latest.get("launch_id") == record.get("launch_id") and latest.get("argv_sha256") == record.get("argv_sha256"):
                record = latest
        guard_identity = process_identity(os.getpid())
        record.update({"pid": os.getpid(), "start_ticks": guard_identity.get("start_ticks"), "boot_id": guard_identity.get("boot_id")})
        receipt = {
            "schema_version": CHILD_SCHEMA,
            "launch_id": record.get("launch_id"),
            "worker_id": record.get("worker_id"),
            "authorization_id": record.get("authorization_id"),
            "authorization_sha256": record.get("authorization_sha256"),
            "queue_db_sha256": record.get("queue_db_sha256"),
            "argv_sha256": record.get("argv_sha256"),
            "guard_pid": os.getpid(),
            "guard_identity": guard_identity,
            "returncode": int(returncode),
            "completed": completed,
            "started_epoch_ns": started,
            "finished_epoch_ns": time.time_ns(),
        }
        _atomic_json(Path(str(record["exit_receipt"])), receipt)
        # Close the launch record after its receipt is durable. Keep the file
        # as the audit trail; closed status prevents a later tick from
        # treating this historical launch as an active duplicate.
        record["status"] = "receipt_written"
        record["finished_epoch_ns"] = receipt["finished_epoch_ns"]
        record["returncode"] = int(returncode)
        _atomic_json(record_path, record)


def run_once(authorization: Path, state_dir: Path, *, queue_dir: Optional[Path] = None, execute: bool = False) -> Tuple[int, Dict[str, Any]]:
    state_dir = state_dir.absolute()
    _reject_symlink_chain(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    _reject_symlink_chain(state_dir)
    started = time.time_ns()
    run_id = f"monitor-{started}-{secrets.token_hex(4)}"
    try:
        auth = validate_authorization(authorization, queue_override=queue_dir)
    except MonitorError as exc:
        payload = _status_payload(run_id=run_id, status="paused", reason="authorization_invalid", started_ns=started, detail=str(exc), execute_requested=execute)
        _write_outcome(state_dir, payload)
        return 2, payload
    try:
        state = _load_state(state_dir / "monitor_state.json")
        launch_record_failures = _merge_launch_records(state_dir, state, auth["worker_ids"])
        if launch_record_failures:
            payload = _status_payload(run_id=run_id, status="paused", reason="integrity_failure", started_ns=started, detail=launch_record_failures, execute_requested=execute, authorization_id=auth["authorization_id"], authorization_sha256=auth["sha256"])
            _write_outcome(state_dir, payload)
            return 2, payload
        queue = read_queue_snapshot(auth)
        supervisors, failures, live_workers = _classify_supervisors(state, queue, auth)
        if failures:
            payload = _status_payload(run_id=run_id, status="paused", reason="integrity_failure", started_ns=started, detail=failures, execute_requested=execute, authorization_id=auth["authorization_id"], authorization_sha256=auth["sha256"], queue_db_sha256=queue["db_sha256"], queue=queue, supervisors=supervisors)
            _write_outcome(state_dir, payload)
            state.update({"last_status": payload, "supervisors": state.get("supervisors", {})})
            _atomic_json(state_dir / "monitor_state.json", state)
            return 2, payload
        if queue["dispatch_halted"]:
            payload = _status_payload(run_id=run_id, status="paused", reason="queue_dispatch_halted", started_ns=started, detail=queue["halt_reason"], execute_requested=execute, authorization_id=auth["authorization_id"], authorization_sha256=auth["sha256"], queue_db_sha256=queue["db_sha256"], queue=queue, supervisors=supervisors)
            _write_outcome(state_dir, payload)
            return 2, payload
        registered = queue["registered_workers"]
        pending = sum(value for key, value in queue["case_status_counts"].items() if key != "accepted")
        if pending == 0:
            payload = _status_payload(run_id=run_id, status="complete", reason=None, started_ns=started, execute_requested=execute, authorization_id=auth["authorization_id"], authorization_sha256=auth["sha256"], queue_db_sha256=queue["db_sha256"], queue=queue, supervisors=supervisors)
            _write_outcome(state_dir, payload)
            return 0, payload
        if execute and not auth["allow_supervisor_launch"]:
            payload = _status_payload(run_id=run_id, status="paused", reason="execution_not_authorized", started_ns=started, detail="authorization does not permit supervisor launch", execute_requested=True, authorization_id=auth["authorization_id"], authorization_sha256=auth["sha256"], queue_db_sha256=queue["db_sha256"], queue=queue, supervisors=supervisors)
            _write_outcome(state_dir, payload)
            return 2, payload
        if not execute:
            payload = _status_payload(run_id=run_id, status="ready", reason=None, started_ns=started, execute_requested=False, authorization_id=auth["authorization_id"], authorization_sha256=auth["sha256"], queue_db_sha256=queue["db_sha256"], queue=queue, supervisors=supervisors, eligible_workers=sorted(worker_id for worker_id, row in registered.items() if row["enabled"] and worker_id not in supervisors))
            _write_outcome(state_dir, payload)
            return 0, payload
        launched: List[Dict[str, Any]] = []
        state_supervisors = dict(state.get("supervisors", {}))
        for worker_id in auth["worker_ids"]:
            if len(launched) >= auth["max_launches_per_tick"]:
                break
            # Registration and endpoint identity are still checked above;
            # administrative disablement is a normal queue state, not a
            # monitor integrity failure. Leave it for a later tick.
            if worker_id not in registered or not registered[worker_id]["enabled"]:
                continue
            if worker_id not in registered or worker_id in live_workers or worker_id in queue["registered_workers"] and any(row["worker_id"] == worker_id for row in queue["active_or_orphaned_attempts"]):
                continue
            old = state_supervisors.get(worker_id)
            if old is not None:
                if not auth["allow_resume_after_supervisor_exit"]:
                    continue
                if supervisors.get(worker_id, {}).get("status") != "exited_with_receipt_unverified":
                    continue
            argv = build_supervisor_argv(auth, worker_id)
            launched_row = _spawn_supervisor(auth, state_dir, worker_id, argv)
            state_supervisors[worker_id] = launched_row
            launched.append({"worker_id": worker_id, "launch_id": launched_row["launch_id"], "pid": launched_row["pid"], "argv_sha256": launched_row["argv_sha256"]})
        state.update({"schema_version": MONITOR_SCHEMA, "authorization_id": auth["authorization_id"], "authorization_sha256": auth["sha256"], "supervisors": state_supervisors, "last_queue_db_sha256": queue["db_sha256"]})
        _atomic_json(state_dir / "monitor_state.json", state)
        exited_needing_review = any(item.get("status") == "exited_with_receipt_unverified" for item in supervisors.values())
        registration_pending = not registered
        if launched:
            outcome_status, outcome_reason, outcome_code = "launched", None, 0
        elif exited_needing_review:
            outcome_status, outcome_reason, outcome_code = "paused", "supervisor_exit_requires_review", 2
        elif registration_pending:
            outcome_status, outcome_reason, outcome_code = "paused", "worker_registration_incomplete", 2
        else:
            outcome_status, outcome_reason, outcome_code = "running", "no_eligible_worker", 3
        payload = _status_payload(run_id=run_id, status=outcome_status, reason=outcome_reason, started_ns=started, execute_requested=True, authorization_id=auth["authorization_id"], authorization_sha256=auth["sha256"], queue_db_sha256=queue["db_sha256"], queue=queue, supervisors=supervisors, launched=launched)
        _write_outcome(state_dir, payload)
        return outcome_code, payload
    except (MonitorError, OSError, sqlite3.Error) as exc:
        payload = _status_payload(run_id=run_id, status="paused", reason="integrity_failure", started_ns=started, detail=str(exc), execute_requested=execute)
        _write_outcome(state_dir, payload)
        return 2, payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--queue-dir", type=Path)
    parser.add_argument("--execute", action="store_true", help="allow launching only after the authorization permits it")
    parser.add_argument("--supervisor-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--launch-record", type=Path, help=argparse.SUPPRESS)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if args.supervisor_child:
        if args.launch_record is None:
            raise SystemExit("--launch-record is required")
        try:
            return supervisor_child(args.launch_record)
        except (MonitorError, OSError) as exc:
            print(f"MONITOR_CHILD_NOT_READY: {exc}", file=sys.stderr)
            return 125
    if args.authorization is None or args.state_dir is None:
        raise SystemExit("--authorization and --state-dir are required")
    state_dir = args.state_dir.absolute()
    _reject_symlink_chain(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = state_dir / ".monitor.lock"
    with lock_path.open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(_canonical({"schema_version": MONITOR_SCHEMA, "status": "locked", "reason": "another monitor instance is active"}))
            return 3
        code, payload = run_once(args.authorization, state_dir, queue_dir=args.queue_dir, execute=args.execute)
        print(_canonical(payload))
        return code


if __name__ == "__main__":
    raise SystemExit(main())

