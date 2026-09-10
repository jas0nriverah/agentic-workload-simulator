#!/usr/bin/env python3
"""Durable, endpoint-aware queueing for independent SWE-agent trajectories.

The queue is deliberately independent of the process that starts it.  SQLite
is the source of truth for case ownership and attempt history; the filesystem
holds the exact case bytes and every attempt's durable evidence.  A lease is
never made available again because a wall-clock timeout passed.  A new owner
can use a case only after the recorded supervisor and runner PIDs have been
reconciled and the previous attempt has been explicitly accepted or requeued.

This module does not probe endpoints, create tunnels, start inference, or
change an existing case runner.  Its optional ``supervise`` command invokes
the existing case runner only after the operator supplies ``--execute`` and
``--acknowledge-paid-gpu-work``.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import errno
import fcntl
import functools
import hashlib
import importlib.util
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlparse


QUEUE_SCHEMA = "assignment.shared-case-queue.v2"
WORKER_BINDING_SCHEMA = "assignment.worker-binding.v1"
LEASE_SCHEMA = "assignment.case-lease.v1"
ARTIFACT_MANIFEST_SCHEMA = "assignment.case-artifact-manifest.v1"
QUEUE_FAILURE_SCHEMA = "assignment.queue-failure.v1"
EFFECTIVE_FINGERPRINT_SCHEMA = "assignment.worker-fingerprint-effective.v1"
DISCOVERY_FINGERPRINT_SCHEMA = "assignment.worker-fingerprint-discovery.v1"
ADAPTER_MANIFEST_SCHEMA = "assignment.queue-adapter-manifest.v1"
STORAGE_POLICY_SCHEMA = "assignment.queue-storage-policy.v1"
CAPACITY_OBSERVATION_SCHEMA = "assignment.queue-capacity-observation.v1"
CONFIRMATION_PLAN_SCHEMA = "assignment.configuration-confirmation-execution-plan.v2"
CONFIRMATION_CASE_SCHEMA = "assignment.configuration-confirmation-case.v2"
BINDING_HISTORY_SCHEMA = "assignment.worker-binding-history.v1"
BINDING_REVISION_SCHEMA = "assignment.worker-binding-revision.v1"
MIGRATION_RECEIPT_SCHEMA = "assignment.queue-binding-migration-receipt.v1"

DB_NAME = "queue.sqlite3"
ARTIFACTS_DIR_NAME = "artifacts"
READY_WORKER_NUMBERS = tuple(list(range(0, 11)) + list(range(12, 23)))
READY_WORKER_IDS = tuple(f"worker-{number:02d}" for number in READY_WORKER_NUMBERS)
EXPIRED_WORKER_ID = "worker-11"

CASE_PENDING = "pending"
CASE_RUNNING = "running"
CASE_RETRY_WAITING = "retry_waiting"
CASE_ORPHANED = "orphaned"
CASE_ACCEPTED = "accepted"
CASE_BLOCKED = "blocked"

ATTEMPT_ACTIVE = "active"
ATTEMPT_ORPHANED = "orphaned"
ATTEMPT_ACCEPTED = "accepted"
ATTEMPT_RETRYABLE = "retryable"
ATTEMPT_REQUEUED = "requeued"
ATTEMPT_BLOCKED = "blocked"

CAPTURE_FAILURE_CLASSIFICATIONS = {
    "capture_integrity",
    "infrastructure_evidence",
    "model_transport_or_server_infrastructure",
}

KNOWN_CASE_SCHEMAS = {
    "assignment-steps-1-3-plan.v1",
    "assignment-production-v2-plan.v1",
    "assignment.configuration-confirmation-case.v2",
}


class QueueError(RuntimeError):
    """Base class for queue admission, ownership, and durability errors."""


class QueueBusy(QueueError):
    """The durable store could not be acquired within its bounded wait."""


class QueueNotReady(QueueError):
    """A fail-closed precondition prevents another case from being assigned."""


class QueueHalted(QueueNotReady):
    """Dispatch is halted by an integrity or infrastructure condition."""


class LeaseConflict(QueueError):
    """A worker, endpoint, or case is already owned by another attempt."""


class ArtifactIntegrityError(QueueError):
    """An attempt artifact is absent, changed, malformed, or not durable."""


class ReconciliationRequired(QueueNotReady):
    """A dead owner has left an attempt that must be reconciled explicitly."""


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _now_ns() -> int:
    return time.time_ns()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise ArtifactIntegrityError(f"cannot read artifact {path}: {exc}") from exc
    return digest.hexdigest()


def _sql_digest_value(value: Any) -> Any:
    """Make SQLite values deterministic without decoding case/artifact bytes."""

    if isinstance(value, bytes):
        return {"__bytes_hex__": value.hex()}
    return value


def _preserved_queue_state_digest(connection: sqlite3.Connection) -> str:
    """Digest immutable case/attempt evidence used by a binding revision."""

    state: Dict[str, Any] = {}
    for table in ("cases", "attempts", "observers"):
        rows = connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        state[table] = [
            {str(key): _sql_digest_value(value) for key, value in zip(row.keys(), tuple(row))}
            for row in rows
        ]
    return _sha256_bytes((_canonical(state) + "\n").encode("utf-8"))


def _reject_symlink_chain(path: Path, *, include_leaf: bool = True) -> None:
    """Reject symlink traversal before resolving a queue-owned path."""

    candidates = (path,) + tuple(path.parents) if include_leaf else tuple(path.parents)
    for candidate in candidates:
        try:
            if candidate.is_symlink():
                raise QueueError(f"path must not traverse a symlink: {path}")
        except OSError as exc:
            raise QueueError(f"cannot inspect path {path}: {exc}") from exc


def _absolute_path(value: Any, label: str, *, base: Optional[Path] = None) -> Path:
    if isinstance(value, Path):
        raw_value = str(value)
    elif isinstance(value, str):
        raw_value = value
    else:
        raw_value = ""
    if not raw_value.strip():
        raise QueueNotReady(f"{label} path is required")
    path = Path(raw_value)
    if not path.is_absolute():
        path = (base or Path.cwd()) / path
    path = path.absolute()
    _reject_symlink_chain(path)
    return path


def _validate_sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise QueueNotReady(f"{label} must be a lowercase SHA-256 digest")
    return value.lower()


def _fsync_artifact_descriptor(descriptor: int) -> None:
    """Flush writable evidence; immutable archive descriptors need no flush."""
    try:
        os.fsync(descriptor)
    except OSError as exc:
        if exc.errno not in {errno.EINVAL, errno.EROFS} or not (
            os.fstatvfs(descriptor).f_flag & os.ST_RDONLY
        ):
            raise


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        _fsync_artifact_descriptor(descriptor)
    finally:
        os.close(descriptor)


def _atomic_bytes(path: Path, payload: bytes, *, overwrite: bool = True) -> None:
    path = path.absolute()
    _reject_symlink_chain(path, include_leaf=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise QueueError(f"refusing to write a symlink: {path}")
    if not overwrite and path.exists():
        raise QueueError(f"refusing to overwrite existing artifact: {path}")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
        _fsync_directory(path.parent)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _atomic_json(path: Path, value: Any, *, overwrite: bool = True) -> None:
    _atomic_bytes(
        path,
        (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"),
        overwrite=overwrite,
    )


def _write_sidecar(path: Path, digest: Optional[str] = None) -> str:
    actual = digest or _sha256_file(path)
    _atomic_bytes(
        Path(str(path) + ".sha256"),
        f"{actual}  {path.name}\n".encode("ascii"),
    )
    return actual


def _verify_sidecar(path: Path, *, label: str, sidecar_path: Optional[Path] = None) -> str:
    if path.is_symlink() or not path.is_file():
        raise ArtifactIntegrityError(f"{label} is not a regular file: {path}")
    sidecar = sidecar_path or Path(str(path) + ".sha256")
    sidecar = Path(sidecar).absolute()
    if sidecar.is_symlink() or not sidecar.is_file():
        raise ArtifactIntegrityError(f"{label} SHA-256 sidecar is missing: {sidecar}")
    actual = _sha256_file(path)
    try:
        claim = sidecar.read_text(encoding="ascii")
    except (OSError, UnicodeError) as exc:
        raise ArtifactIntegrityError(f"cannot read {label} sidecar {sidecar}: {exc}") from exc
    if claim != f"{actual}  {path.name}\n":
        raise ArtifactIntegrityError(f"{label} or its SHA-256 sidecar changed: {path}")
    return actual


def _directory_digest(path: Path) -> str:
    """Hash a source directory without following links or unstable metadata."""

    rows: List[Dict[str, Any]] = []
    try:
        for directory, dirs, files in os.walk(str(path), topdown=True, followlinks=False):
            parent = Path(directory)
            linked_dirs = [name for name in dirs if (parent / name).is_symlink()]
            if linked_dirs:
                raise ArtifactIntegrityError(
                    f"source directory contains symlinked directories: {parent / linked_dirs[0]}"
                )
            dirs[:] = sorted(dirs)
            for name in sorted(files):
                candidate = parent / name
                if candidate.is_symlink() or not candidate.is_file():
                    raise ArtifactIntegrityError(f"source directory contains a non-regular file: {candidate}")
                before = candidate.stat()
                digest = _sha256_file(candidate)
                after = candidate.stat()
                if (before.st_ino, before.st_size, before.st_mtime_ns) != (
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                ):
                    raise ArtifactIntegrityError(f"source file changed while hashing: {candidate}")
                rows.append({
                    "path": str(candidate.relative_to(path)),
                    "sha256": digest,
                    "size": after.st_size,
                })
    except OSError as exc:
        raise ArtifactIntegrityError(f"cannot hash source directory {path}: {exc}") from exc
    return _sha256_bytes((_canonical(rows) + "\n").encode("utf-8"))


def _digest_path(path: Path) -> Tuple[str, str]:
    if path.is_symlink() or not path.exists():
        raise ArtifactIntegrityError(f"bound file is unavailable or symlinked: {path}")
    if path.is_file():
        before = path.stat()
        digest = _sha256_file(path)
        after = path.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ArtifactIntegrityError(f"bound file changed while hashing: {path}")
        return digest, "file"
    if path.is_dir():
        return _directory_digest(path), "directory"
    raise ArtifactIntegrityError(f"bound path is not a regular file or directory: {path}")


def _normalise_descriptor(
    value: Any,
    label: str,
    *,
    base: Optional[Path] = None,
    require_declared_hash: bool = False,
) -> Dict[str, Any]:
    if isinstance(value, (str, Path)):
        path = _absolute_path(str(value), label, base=base)
        declared: Optional[str] = None
        original: Dict[str, Any] = {}
    elif isinstance(value, Mapping):
        path = _absolute_path(value.get("path"), f"{label}.path", base=base)
        raw_hash = value.get("sha256")
        if raw_hash is None and require_declared_hash:
            raise QueueNotReady(f"{label}.sha256 is required for a production worker binding")
        declared = _validate_sha(raw_hash, f"{label}.sha256") if raw_hash is not None else None
        original = {str(key): item for key, item in value.items() if key not in {"path", "sha256"}}
    else:
        raise QueueNotReady(f"{label} must be a path or a path/hash descriptor")
    actual, kind = _digest_path(path)
    if declared is not None and declared != actual:
        raise ArtifactIntegrityError(f"{label} SHA-256 does not match its bound bytes: {path}")
    result = {"path": str(path), "sha256": actual, "kind": kind}
    result.update(original)
    return result


def _read_json_object(path: Path, *, label: str) -> Dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise QueueNotReady(f"{label} is not a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QueueNotReady(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise QueueNotReady(f"{label} must be a JSON object: {path}")
    return value


def _inside(path: Path, root: Path) -> bool:
    """Return whether ``path`` is below ``root`` without resolving links."""

    try:
        path.absolute().relative_to(root.absolute())
    except ValueError:
        return False
    return True


def _load_adapter_manifest(
    path_value: Any,
    *,
    declared_sha256: Optional[str] = None,
    label: str = "adapter manifest",
) -> Dict[str, Any]:
    """Load a hash-bound argv extension without invoking a shell.

    The queue records the manifest bytes and the exact argv list.  Callers
    rehash the file immediately before launch, so editing a manifest cannot
    silently change a running supervisor's command.
    """

    path = _absolute_path(str(path_value), label)
    actual = _sha256_file(path)
    if declared_sha256 is not None and _validate_sha(declared_sha256, f"{label}.sha256") != actual:
        raise ArtifactIntegrityError(f"{label} SHA-256 differs from the supplied binding: {path}")
    sidecar = Path(str(path) + ".sha256")
    if sidecar.exists():
        _verify_sidecar(path, label=label)
    value = _read_json_object(path, label=label)
    if value.get("schema_version") != ADAPTER_MANIFEST_SCHEMA:
        raise QueueNotReady(f"{label} has unsupported schema: {value.get('schema_version')!r}")
    argv = value.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item for item in argv):
        raise QueueNotReady(f"{label}.argv must be a non-empty string list")
    if any("\x00" in item for item in argv):
        raise QueueNotReady(f"{label}.argv contains a NUL")
    return {
        "schema_version": ADAPTER_MANIFEST_SCHEMA,
        "path": str(path),
        "sha256": actual,
        "argv": list(argv),
        "name": value.get("name"),
        "purpose": value.get("purpose"),
    }


def _case_key(value: Mapping[str, Any], *, label: str) -> str:
    for key in ("resume_key", "case_id", "candidate_case_id", "id"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    raise QueueNotReady(f"{label} has no stable resume_key/case_id")


def _case_bytes(value: Mapping[str, Any]) -> bytes:
    return (_canonical(value) + "\n").encode("utf-8")


def _extract_plan(path: Path) -> Tuple[Dict[str, Any], List[Tuple[str, bytes, Optional[Path], Dict[str, Any]]]]:
    """Read JSONL plans and object-shaped confirmation plans without editing them."""

    if path.is_symlink() or not path.is_file():
        raise QueueNotReady(f"plan is not a regular file: {path}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise QueueNotReady(f"cannot read plan {path}: {exc}") from exc
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        parsed = None

    header: Dict[str, Any]
    entries: List[Tuple[Any, bytes]] = []
    if isinstance(parsed, Mapping) and isinstance(parsed.get("cases"), list):
        header = {str(key): value for key, value in parsed.items() if key != "cases"}
        for entry in parsed["cases"]:
            if not isinstance(entry, Mapping):
                raise QueueNotReady("object-shaped plan contains a non-object case")
            descriptor = entry.get("case_spec")
            if isinstance(descriptor, Mapping):
                spec_path = _absolute_path(descriptor.get("path"), "case_spec.path", base=path.parent)
                expected = _validate_sha(descriptor.get("sha256"), "case_spec.sha256")
                actual = _sha256_file(spec_path)
                if actual != expected:
                    raise ArtifactIntegrityError(f"case_spec SHA-256 does not match the plan reference: {spec_path}")
                entries.append((entry, spec_path.read_bytes()))
            else:
                entries.append((entry, _case_bytes(entry)))
    elif isinstance(parsed, list):
        header = {"record_type": "plan", "plan_id": f"list-{_sha256_bytes(raw)[:16]}"}
        for entry in parsed:
            if not isinstance(entry, Mapping):
                raise QueueNotReady("case list contains a non-object case")
            entries.append((entry, _case_bytes(entry)))
    else:
        try:
            lines = raw.splitlines(keepends=True)
        except Exception as exc:  # pragma: no cover - bytes.splitlines is total
            raise QueueNotReady(f"cannot split plan {path}: {exc}") from exc
        if not lines:
            raise QueueNotReady("plan is empty")
        rows: List[Dict[str, Any]] = []
        for line_number, line in enumerate(lines, 1):
            if not line.strip():
                raise QueueNotReady(f"plan has a blank line at {line_number}")
            try:
                value = json.loads(line.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise QueueNotReady(f"plan line {line_number} is not valid JSON: {exc}") from exc
            if not isinstance(value, dict):
                raise QueueNotReady(f"plan line {line_number} is not an object")
            rows.append(value)
        if len(rows) < 2 or rows[0].get("record_type") != "plan":
            raise QueueNotReady("JSONL plan must contain a plan header and at least one case")
        header = rows[0]
        line_bytes = lines[1:]
        for entry, payload in zip(rows[1:], line_bytes):
            entries.append((entry, payload))

    plan_id = header.get("plan_id")
    if not isinstance(plan_id, str) or not plan_id.strip():
        header = {**header, "plan_id": f"plan-{_sha256_bytes(raw)[:16]}"}
    if not entries:
        raise QueueNotReady("plan contains no cases")
    records: List[Tuple[str, bytes, Optional[Path], Dict[str, Any]]] = []
    seen: set[str] = set()
    for entry, payload in entries:
        try:
            spec = json.loads(payload.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise QueueNotReady(f"case payload is not valid JSON: {exc}") from exc
        if not isinstance(spec, dict):
            raise QueueNotReady("case payload is not a JSON object")
        key = _case_key(spec, label="case")
        entry_key: Optional[str] = None
        for key_name in ("resume_key", "case_id", "candidate_case_id"):
            if isinstance(entry.get(key_name), str) and entry[key_name].strip():
                entry_key = str(entry[key_name])
                break
        if entry_key is not None and entry_key != key:
            raise QueueNotReady(f"plan membership key {entry_key} differs from staged case key {key}")
        if key in seen:
            raise QueueNotReady(f"plan contains duplicate case identity: {key}")
        seen.add(key)
        source_path: Optional[Path] = None
        if isinstance(entry.get("case_spec"), Mapping):
            source_path = _absolute_path(entry["case_spec"].get("path"), "case_spec.path", base=path.parent)
        records.append((key, payload, source_path, dict(entry)))
    return header, records


def _normalise_worker_id(value: Any, allowed: Sequence[str]) -> str:
    if not isinstance(value, str) or not value.strip():
        raise QueueNotReady("worker_id is required")
    raw = value.strip()
    match = re.fullmatch(r"(?:worker-)?(\d{2})", raw)
    worker_id = f"worker-{int(match.group(1)):02d}" if match else raw
    if worker_id == EXPIRED_WORKER_ID:
        raise QueueNotReady("worker-11 is expired and excluded from the dispatch pool")
    if worker_id not in allowed:
        raise QueueNotReady(f"worker {worker_id} is outside the declared dispatch pool")
    return worker_id


def _validate_discovery_fingerprints(value: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate the read-only discovery artifact without treating health as readiness."""

    if value.get("schema_version") != DISCOVERY_FINGERPRINT_SCHEMA:
        raise QueueNotReady("worker fingerprint artifact has an unsupported schema")
    nodes = value.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise QueueNotReady("worker fingerprint artifact has no nodes")
    endpoints: List[Tuple[str, str]] = []
    contexts: List[int] = []
    for node in nodes:
        if not isinstance(node, Mapping) or not isinstance(node.get("node"), str):
            raise QueueNotReady("worker fingerprint node is malformed")
        inventory = node.get("inventory")
        processes = inventory.get("serving_processes") if isinstance(inventory, Mapping) else None
        if not isinstance(processes, list):
            raise QueueNotReady("worker fingerprint node lacks serving_processes")
        for process in processes:
            if not isinstance(process, Mapping):
                raise QueueNotReady("worker fingerprint serving process is malformed")
            options = process.get("options")
            port = options.get("--port") if isinstance(options, Mapping) else None
            raw_context = options.get("--max-model-len") if isinstance(options, Mapping) else None
            try:
                context = int(raw_context)
                port_value = str(int(port))
            except (TypeError, ValueError):
                raise QueueNotReady("worker fingerprint serving process lacks numeric port/context")
            if context <= 0 or int(port_value) <= 0:
                raise QueueNotReady("worker fingerprint serving process has an invalid port/context")
            key = (str(node["node"]), port_value)
            if key in endpoints:
                raise QueueNotReady("worker fingerprint artifact contains duplicate endpoint rows")
            endpoints.append(key)
            contexts.append(context)
    if len(endpoints) != len(READY_WORKER_IDS):
        raise QueueNotReady(
            f"worker fingerprint artifact must describe exactly {len(READY_WORKER_IDS)} serving endpoints; found {len(endpoints)}"
        )
    return {
        "endpoint_count": len(endpoints),
        "contexts": sorted(set(contexts)),
        "all_contexts": contexts,
        "scope": value.get("scope"),
    }


def _validate_effective_fingerprints(
    value: Mapping[str, Any],
    *,
    expected_max_model_len: int,
    require_observer_non_leasing: bool,
) -> Dict[str, Any]:
    """Validate root's post-rollout configuration binding.

    The schema is intentionally separate from the discovery schema.  A
    discovered healthy endpoint is evidence that a server answered; this
    artifact is the reviewed claim about the effective launch configuration.
    """

    if value.get("schema_version") != EFFECTIVE_FINGERPRINT_SCHEMA:
        raise QueueNotReady("effective worker fingerprint artifact has an unsupported schema")
    if value.get("server_max_model_len") != expected_max_model_len:
        raise QueueNotReady(
            f"effective fingerprint server_max_model_len must be {expected_max_model_len}"
        )
    rows = value.get("workers")
    if not isinstance(rows, list) or not rows:
        raise QueueNotReady("effective fingerprint artifact must contain prepared worker rows")
    seen: set[str] = set()
    endpoint_ids: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise QueueNotReady("effective fingerprint worker row is malformed")
        worker_id = _normalise_worker_id(row.get("worker_id"), READY_WORKER_IDS)
        if worker_id in seen:
            raise QueueNotReady("effective fingerprint artifact contains duplicate worker IDs")
        seen.add(worker_id)
        endpoint_id = row.get("endpoint_id")
        if not isinstance(endpoint_id, str) or not endpoint_id.strip() or endpoint_id in endpoint_ids:
            raise QueueNotReady("effective fingerprint endpoint IDs are missing or duplicated")
        endpoint_ids.add(endpoint_id)
        if row.get("server_max_model_len") != expected_max_model_len:
            raise QueueNotReady(f"effective fingerprint for {worker_id} is not bound to the expected context")
        fingerprint = row.get("effective_config_fingerprint")
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", fingerprint) or fingerprint == "0" * 64:
            raise QueueNotReady(f"effective fingerprint for {worker_id} is missing")
    observer = value.get("observer_rollout")
    if require_observer_non_leasing:
        if not isinstance(observer, Mapping):
            raise QueueNotReady("effective fingerprint artifact lacks observer rollout binding")
        if observer.get("leases_cases") is not False or observer.get("active_trajectory_allowed") is not False:
            raise QueueNotReady("observer rollout is allowed to lease an active case")
    return {
        "worker_count": len(rows),
        "prepared_worker_ids": sorted(seen),
        "unprepared_worker_ids": sorted(set(READY_WORKER_IDS) - seen),
        "server_max_model_len": expected_max_model_len,
        "observer_non_leasing": (
            observer.get("leases_cases") is False
            and observer.get("active_trajectory_allowed") is False
            if isinstance(observer, Mapping)
            else not require_observer_non_leasing
        ),
    }


def _normalise_endpoint(value: Any, *, endpoint_id: Optional[str] = None, endpoint_url: Optional[str] = None) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        endpoint = dict(value)
    elif value is None:
        endpoint = {}
    else:
        endpoint = {"api_base": str(value)}
    if endpoint_id is not None:
        endpoint.setdefault("endpoint_id", endpoint_id)
    if endpoint_url is not None:
        endpoint.setdefault("api_base", endpoint_url)
    eid = endpoint.get("endpoint_id") or endpoint.get("id")
    api_base = endpoint.get("api_base") or endpoint.get("url") or endpoint.get("endpoint_url")
    if not isinstance(eid, str) or not eid.strip():
        raise QueueNotReady("endpoint.endpoint_id is required")
    if not isinstance(api_base, str) or not api_base.strip():
        raise QueueNotReady("endpoint.api_base is required")
    parsed = urlparse(api_base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise QueueNotReady("endpoint.api_base must be an HTTP(S) URL")
    result = dict(endpoint)
    result["endpoint_id"] = eid.strip()
    result["api_base"] = api_base.rstrip("/")
    result.setdefault("server_identity", result["endpoint_id"])
    return result


def _current_boot_id() -> Optional[str]:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return None
    return value or None


def _proc_start_ticks(pid: int) -> Optional[int]:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except (OSError, UnicodeError):
        return None
    try:
        tail = raw.rsplit(")", 1)[1].split()
        # tail[0] is field 3 (state); field 22 is therefore tail[19].
        if not tail or tail[0] == "Z":
            return None
        return int(tail[19])
    except (IndexError, TypeError, ValueError):
        return None


def _process_identity(pid: Optional[int]) -> Dict[str, Any]:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return {"status": "absent", "pid": pid}
    try:
        tail = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
        boot = _current_boot_id()
        if boot is None:
            return {"status": "unknown", "pid": pid}
        if tail[0] in {"Z", "X"}:
            return {"status": "dead", "pid": pid}
        return {"status": "alive", "pid": pid, "start_ticks": int(tail[19]),
                "boot_id": boot, "pgid": int(tail[2]), "sid": int(tail[3])}
    except FileNotFoundError:
        # ENOENT is proof only on our visible Linux proc filesystem.
        return {"status": "dead" if Path("/proc/self/stat").exists() else "unknown", "pid": pid}
    except (OSError, UnicodeError, IndexError, ValueError) as exc:
        return {"status": "unknown", "pid": pid, "error": str(exc)}


def _same_process_identity(row: Mapping[str, Any], prefix: str) -> Optional[bool]:
    def value(key: str) -> Any:
        if hasattr(row, "keys") and key not in row.keys():
            return None
        try:
            return row[key]
        except (KeyError, IndexError):
            return None

    pid = value(f"{prefix}_pid")
    observed = _process_identity(pid if isinstance(pid, int) else None)
    if observed["status"] == "dead" or observed["status"] == "absent":
        return False
    if observed["status"] == "unknown":
        return None
    expected_ticks = value(f"{prefix}_start_ticks")
    expected_boot = value(f"{prefix}_boot_id")
    if expected_ticks is None or expected_boot is None:
        return None
    if expected_ticks is not None and observed.get("start_ticks") != expected_ticks:
        return False
    if expected_boot is not None and observed.get("boot_id") not in {None, expected_boot}:
        return False
    return True


def _session_state(sid: int, *, exclude_pid: Optional[int] = None) -> Optional[bool]:
    """True means a live member; None means visibility cannot prove empty."""
    unknown = False
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdecimal() or int(entry.name) == exclude_pid:
            continue
        observed = _process_identity(int(entry.name))
        if observed["status"] == "unknown":
            unknown = True
        elif observed["status"] == "alive" and observed.get("sid") == sid:
            return True
    return None if unknown else False


def _serialized_attempt(method):
    """Serialize evidence writes/replays across processes, outside evidence trees."""
    @functools.wraps(method)
    def guarded(self, lease_or_attempt, *args, **kwargs):
        attempt_id = lease_or_attempt.attempt_id if isinstance(lease_or_attempt, Lease) else str(lease_or_attempt)
        with self._attempt_lock(attempt_id):
            return method(self, lease_or_attempt, *args, **kwargs)
    return guarded


@dataclass(frozen=True)
class Lease:
    """A capability returned by an atomic claim."""

    attempt_id: str
    lease_token: str
    case_id: str
    resume_key: str
    ordinal: int
    attempt_no: int
    worker_id: str
    endpoint_id: str
    endpoint_url: str
    artifact_dir: Path
    case_sha256: str
    worker_binding_sha256: str
    owner_pid: int
    owner_start_ticks: Optional[int]
    owner_boot_id: Optional[str]
    retry_of_attempt_id: Optional[str]
    case: Mapping[str, Any]

    @property
    def id(self) -> str:
        return self.attempt_id

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": LEASE_SCHEMA,
            "attempt_id": self.attempt_id,
            "lease_token": self.lease_token,
            "case_id": self.case_id,
            "resume_key": self.resume_key,
            "ordinal": self.ordinal,
            "attempt_no": self.attempt_no,
            "worker_id": self.worker_id,
            "endpoint_id": self.endpoint_id,
            "endpoint_url": self.endpoint_url,
            "artifact_dir": str(self.artifact_dir),
            "case_sha256": self.case_sha256,
            "worker_binding_sha256": self.worker_binding_sha256,
            "owner_pid": self.owner_pid,
            "owner_start_ticks": self.owner_start_ticks,
            "owner_boot_id": self.owner_boot_id,
            "retry_of_attempt_id": self.retry_of_attempt_id,
            "case": dict(self.case),
        }


class SharedCaseQueue:
    """SQLite/WAL-backed case queue shared by independent worker processes."""

    def __init__(self, queue_dir: Path):
        self.queue_dir = Path(queue_dir).absolute()
        _reject_symlink_chain(self.queue_dir, include_leaf=False)
        if self.queue_dir.is_symlink():
            raise QueueError(f"queue directory must not be a symlink: {self.queue_dir}")
        self.db_path = self.queue_dir / DB_NAME

    @classmethod
    def create(
        cls,
        queue_dir: Path,
        *,
        plan_path: Optional[Path] = None,
        plan_sha256: Optional[str] = None,
        plan_sha256_sidecar: Optional[Path] = None,
        cases: Optional[Sequence[Mapping[str, Any]]] = None,
        worker_ids: Optional[Sequence[str]] = None,
        artifact_root: Optional[Path] = None,
        require_all_workers: bool = True,
        require_plan_sidecar: bool = False,
        fingerprint_path: Optional[Path] = None,
        fingerprint_sha256: Optional[str] = None,
        expected_max_model_len: int = 65536,
        require_observer_non_leasing: bool = True,
        adapter_manifest_path: Optional[Path] = None,
        adapter_manifest_sha256: Optional[str] = None,
    ) -> "SharedCaseQueue":
        queue = cls(queue_dir)
        queue.queue_dir.mkdir(parents=True, exist_ok=True)
        if queue.db_path.exists():
            queue._check_existing()
            if plan_path is not None:
                supplied = _sha256_file(Path(plan_path).absolute())
                if supplied != queue.meta("plan_sha256"):
                    raise QueueNotReady("existing queue is bound to a different plan")
            if fingerprint_path is not None:
                supplied = _sha256_file(Path(fingerprint_path).absolute())
                bound = queue.meta("fingerprint_discovery")
                if not isinstance(bound, Mapping) or supplied != bound.get("sha256"):
                    raise QueueNotReady("existing queue is bound to a different fingerprint artifact")
            if adapter_manifest_path is not None:
                supplied = _sha256_file(Path(adapter_manifest_path).absolute())
                bound = queue.meta("adapter_manifest")
                if not isinstance(bound, Mapping) or supplied != bound.get("sha256"):
                    raise QueueNotReady("existing queue is bound to a different adapter manifest")
            return queue
        if plan_path is not None and cases is not None:
            raise QueueNotReady("supply plan_path or cases, not both")
        raw_allowed = tuple(worker_ids or READY_WORKER_IDS)
        allowed_values: List[str] = []
        for raw_worker_id in raw_allowed:
            if isinstance(raw_worker_id, str):
                match = re.fullmatch(r"(?:worker-)?(\d{2})", raw_worker_id.strip())
                allowed_values.append(f"worker-{int(match.group(1)):02d}" if match else raw_worker_id.strip())
            else:
                allowed_values.append(str(raw_worker_id))
        allowed = tuple(allowed_values)
        if len(set(allowed)) != len(allowed) or not allowed:
            raise QueueNotReady("worker pool contains duplicate or no worker IDs")
        if EXPIRED_WORKER_ID in allowed:
            raise QueueNotReady("worker-11 cannot be included in the dispatch pool")
        if require_all_workers and set(allowed) != set(READY_WORKER_IDS):
            raise QueueNotReady("production dispatch must use the exact ready worker IDs 00-10 and 12-22")
        if plan_path is not None:
            plan = Path(plan_path).absolute()
            if plan_sha256_sidecar is None:
                plan_sha256_sidecar = Path(str(plan) + ".sha256")
            if require_plan_sidecar:
                declared = _verify_sidecar(plan, label="plan", sidecar_path=plan_sha256_sidecar) if plan_sha256_sidecar else None
            elif plan_sha256_sidecar.exists():
                declared = _verify_sidecar(plan, label="plan", sidecar_path=plan_sha256_sidecar)
            else:
                declared = _sha256_file(plan)
            actual = _sha256_file(plan)
            if declared != actual:
                raise ArtifactIntegrityError("plan SHA-256 sidecar changed during initialization")
            if plan_sha256 is not None and _validate_sha(plan_sha256, "plan_sha256") != actual:
                raise ArtifactIntegrityError("supplied plan_sha256 differs from plan bytes")
            header, records = _extract_plan(plan)
            binding_status = "sealed" if plan_sha256_sidecar.exists() else "computed_unsealed"
            plan_path_value: Optional[str] = str(plan)
        elif cases is not None:
            values = [dict(case) for case in cases]
            if not values:
                raise QueueNotReady("cases is empty")
            synthetic = {"record_type": "plan", "plan_id": "direct-case-list", "cases": values}
            synthetic_bytes = (_canonical(synthetic) + "\n").encode("utf-8")
            actual = _sha256_bytes(synthetic_bytes)
            if plan_sha256 is not None and _validate_sha(plan_sha256, "plan_sha256") != actual:
                raise QueueNotReady("plan_sha256 does not match the direct case list")
            header = {"record_type": "plan", "plan_id": f"direct-{actual[:16]}"}
            records = [(key, _case_bytes(value), None, value) for key, value in (
                (_case_key(value, label="case"), value) for value in values
            )]
            keys = [item[0] for item in records]
            if len(keys) != len(set(keys)):
                raise QueueNotReady("direct case list contains duplicate identities")
            binding_status = "direct_case_list"
            plan_path_value = None
        else:
            raise QueueNotReady("queue initialization requires a plan or case list")
        if not isinstance(expected_max_model_len, int) or isinstance(expected_max_model_len, bool) or expected_max_model_len <= 0:
            raise QueueNotReady("expected_max_model_len must be a positive integer")
        fingerprint_binding: Optional[Dict[str, Any]] = None
        fingerprint_summary: Optional[Dict[str, Any]] = None
        fingerprint_gate_status = "test_unconfigured"
        dispatch_halted = False
        halt_reason: Optional[str] = None
        if fingerprint_path is not None:
            fingerprint_binding = _normalise_descriptor(fingerprint_path, "fingerprint discovery")
            if fingerprint_sha256 is not None and _validate_sha(fingerprint_sha256, "fingerprint_sha256") != fingerprint_binding["sha256"]:
                raise ArtifactIntegrityError("fingerprint discovery SHA-256 differs from the supplied binding")
            fingerprint_value = _read_json_object(Path(fingerprint_binding["path"]), label="fingerprint discovery")
            fingerprint_summary = _validate_discovery_fingerprints(fingerprint_value)
            fingerprint_gate_status = "discovery_only"
            dispatch_halted = True
            halt_reason = (
                "effective configured fingerprints are required before dispatch; discovery is not readiness "
                f"(observed contexts={fingerprint_summary['contexts']}, expected server_max_model_len={expected_max_model_len})"
            )
        elif require_all_workers:
            fingerprint_gate_status = "missing_discovery"
            dispatch_halted = True
            halt_reason = "fingerprint gate input is required before dispatch; health is not readiness"
        adapter_binding: Optional[Dict[str, Any]] = None
        if adapter_manifest_path is not None:
            adapter_path = Path(adapter_manifest_path).absolute()
            if require_all_workers and adapter_manifest_sha256 is None and not Path(str(adapter_path) + ".sha256").exists():
                raise QueueNotReady(
                    "production adapter binding requires --adapter-manifest-sha256 or an adapter manifest sidecar"
                )
            adapter_binding = _load_adapter_manifest(
                adapter_path,
                declared_sha256=adapter_manifest_sha256,
            )
        artifact_dir = Path(artifact_root).absolute() if artifact_root is not None else queue.queue_dir / ARTIFACTS_DIR_NAME
        _reject_symlink_chain(artifact_dir, include_leaf=False)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        if artifact_dir.is_symlink():
            raise QueueError(f"artifact root must not be a symlink: {artifact_dir}")
        queue._create_schema()
        meta = {
            "schema_version": QUEUE_SCHEMA,
            "plan_sha256": actual,
            "plan_path": plan_path_value,
            "plan_id": str(header.get("plan_id")),
            "case_count": len(records),
            "artifact_root": str(artifact_dir),
            "allowed_worker_ids": list(allowed),
            "require_all_workers": bool(require_all_workers),
            "dispatch_halted": dispatch_halted,
            "halt_reason": halt_reason,
            "binding_status": binding_status,
            "fingerprint_discovery": fingerprint_binding,
            "fingerprint_discovery_summary": fingerprint_summary,
            "fingerprint_effective": None,
            "fingerprint_gate_status": fingerprint_gate_status,
            "required_server_max_model_len": expected_max_model_len,
            "observer_required": bool(require_observer_non_leasing),
            "adapter_manifest": adapter_binding,
            "storage_policy": None,
            "created_epoch_ns": _now_ns(),
        }
        with queue._transaction() as connection:
            for key, value in meta.items():
                connection.execute(
                    "INSERT INTO meta(key, value_json) VALUES (?, ?)",
                    (key, _canonical(value)),
                )
            for ordinal, (case_id, payload, source_path, entry) in enumerate(records):
                connection.execute(
                    """INSERT INTO cases(
                        case_id, ordinal, resume_key, case_sha256, case_bytes,
                        source_path, source_sha256, entry_json, status,
                        attempt_count, created_epoch_ns
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
                    (
                        case_id,
                        ordinal,
                        case_id,
                        _sha256_bytes(payload),
                        sqlite3.Binary(payload),
                        str(source_path) if source_path is not None else None,
                        _sha256_bytes(payload),
                        _canonical(entry),
                        CASE_PENDING,
                        _now_ns(),
                    ),
                )
            queue._event_locked(
                connection,
                "queue_initialized",
                None,
                {
                    "plan_sha256": actual,
                    "case_count": len(records),
                    "binding_status": binding_status,
                    "dispatch_halted": dispatch_halted,
                    "fingerprint_gate_status": fingerprint_gate_status,
                },
            )
        return queue

    @classmethod
    def import_confirmation(
        cls,
        queue_dir: Path,
        *,
        confirmation_plan: Path,
        confirmation_plan_sha256: str,
        runtime_manifest: Path,
        fingerprint_path: Optional[Path] = None,
        fingerprint_sha256: Optional[str] = None,
        artifact_root: Optional[Path] = None,
        worker_ids: Optional[Sequence[str]] = None,
        require_all_workers: bool = True,
        adapter_manifest_path: Optional[Path] = None,
        adapter_manifest_sha256: Optional[str] = None,
        case_runner_path: Optional[Path] = None,
    ) -> "SharedCaseQueue":
        """Validate and import the reviewed 96-case confirmation inventory.

        Every queue case is the exact bytes of the plan's staged case-spec
        file.  The existing runner's ``load_case`` is called for every member
        with the explicit plan hash and runtime manifest.  This adapter never
        relabels a confirmation case and never edits the plan or its specs.
        """

        plan = _absolute_path(confirmation_plan, "confirmation plan")
        expected_plan_sha = _validate_sha(confirmation_plan_sha256, "confirmation_plan_sha256")
        actual_plan_sha = _verify_sidecar(plan, label="confirmation plan")
        if actual_plan_sha != expected_plan_sha:
            raise ArtifactIntegrityError("confirmation plan SHA-256 differs from its reviewed binding")
        plan_value = _read_json_object(plan, label="confirmation plan")
        if plan_value.get("schema_version") != CONFIRMATION_PLAN_SCHEMA:
            raise QueueNotReady("confirmation importer requires the reviewed confirmation execution plan schema")
        if plan_value.get("case_schema") not in {None, CONFIRMATION_CASE_SCHEMA}:
            raise QueueNotReady("confirmation plan case_schema is unsupported")
        if plan_value.get("execution_case_count") != 96 or not isinstance(plan_value.get("cases"), list) or len(plan_value["cases"]) != 96:
            raise QueueNotReady("confirmation importer requires exactly 96 execution cases")

        runtime = _absolute_path(runtime_manifest, "runtime manifest")
        runtime_sha = _verify_sidecar(runtime, label="runtime manifest")
        runtime_value = _read_json_object(runtime, label="runtime manifest")
        runtime_binding = _normalise_descriptor(runtime, "runtime manifest")
        if runtime_binding["sha256"] != runtime_sha:
            raise ArtifactIntegrityError("runtime manifest changed while it was being bound")

        _header, records = _extract_plan(plan)
        if len(records) != 96:
            raise QueueNotReady(f"confirmation plan extracted {len(records)} cases instead of 96")
        load_case, runner_path, runner_sha = _load_confirmation_case_validator(case_runner_path)
        validated_keys: List[str] = []
        for index, (case_id, payload, source_path, entry) in enumerate(records, 1):
            if source_path is None:
                raise QueueNotReady(f"confirmation case {index} does not reference an original staged case spec")
            if not _inside(source_path, plan.parent):
                raise QueueNotReady(f"confirmation case {case_id} escapes the plan snapshot")
            try:
                original_bytes = source_path.read_bytes()
            except OSError as exc:
                raise ArtifactIntegrityError(f"cannot read original confirmation case {source_path}: {exc}") from exc
            if original_bytes != payload:
                raise ArtifactIntegrityError(f"confirmation case {case_id} bytes changed during import")
            try:
                loaded = load_case(
                    source_path,
                    confirmation_plan=plan,
                    confirmation_plan_sha256=expected_plan_sha,
                    runtime_manifest=runtime_value,
                )
            except Exception as exc:
                raise QueueNotReady(f"confirmation case {case_id} failed existing load_case validation: {exc}") from exc
            if not isinstance(loaded, Mapping) or loaded.get("schema_version") != CONFIRMATION_CASE_SCHEMA:
                raise QueueNotReady(f"confirmation case {case_id} was not retained as a confirmation case")
            if loaded.get("resume_key") != case_id:
                raise QueueNotReady(f"confirmation case {case_id} changed identity during adapter validation")
            validated_keys.append(case_id)
            if entry.get("candidate_case_id") != case_id:
                raise QueueNotReady(f"confirmation plan membership differs for case {case_id}")
        if len(validated_keys) != len(set(validated_keys)):
            raise QueueNotReady("confirmation importer found duplicate case identities")
        if _verify_sidecar(plan, label="confirmation plan") != expected_plan_sha:
            raise ArtifactIntegrityError("confirmation plan changed during adapter validation")
        if _verify_sidecar(runtime, label="runtime manifest") != runtime_sha:
            raise ArtifactIntegrityError("runtime manifest changed during adapter validation")
        if _sha256_file(runner_path) != runner_sha:
            raise ArtifactIntegrityError("case runner changed during confirmation adapter validation")

        queue = cls.create(
            queue_dir,
            plan_path=plan,
            plan_sha256=expected_plan_sha,
            plan_sha256_sidecar=Path(str(plan) + ".sha256"),
            artifact_root=artifact_root,
            worker_ids=worker_ids,
            require_all_workers=require_all_workers,
            require_plan_sidecar=True,
            fingerprint_path=fingerprint_path,
            fingerprint_sha256=fingerprint_sha256,
            adapter_manifest_path=adapter_manifest_path,
            adapter_manifest_sha256=adapter_manifest_sha256,
        )
        import_record = {
            "schema_version": "assignment.confirmation-import.v1",
            "case_schema": CONFIRMATION_CASE_SCHEMA,
            "case_count": 96,
            "case_ids": validated_keys,
            "confirmation_plan": {"path": str(plan), "sha256": expected_plan_sha},
            "runtime_manifest": runtime_binding,
            "case_runner": {"path": str(runner_path), "sha256": runner_sha},
            "original_case_bytes_preserved": True,
            "validated_epoch_ns": _now_ns(),
        }
        with queue._transaction() as connection:
            existing = queue._meta_locked(connection, "confirmation_import")
            if existing is not None:
                existing_compare = dict(existing)
                existing_compare.pop("validated_epoch_ns", None)
                import_compare = dict(import_record)
                import_compare.pop("validated_epoch_ns", None)
                if _canonical(existing_compare) != _canonical(import_compare):
                    raise QueueNotReady("existing queue has a different confirmation import binding")
                return queue
            connection.execute(
                "INSERT OR REPLACE INTO meta(key, value_json) VALUES ('confirmation_import', ?)",
                (_canonical(import_record),),
            )
            queue._event_locked(
                connection,
                "confirmation_inventory_imported",
                None,
                {"case_count": 96, "plan_sha256": expected_plan_sha, "runner_sha256": runner_sha},
            )
        return queue

    def _create_schema(self) -> None:
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        connection = self._connect(raw=True)
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta(
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cases(
                    case_id TEXT PRIMARY KEY,
                    ordinal INTEGER NOT NULL UNIQUE,
                    resume_key TEXT NOT NULL UNIQUE,
                    case_sha256 TEXT NOT NULL,
                    case_bytes BLOB NOT NULL,
                    source_path TEXT,
                    source_sha256 TEXT NOT NULL,
                    entry_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    accepted_attempt_id TEXT,
                    accepted_result_sha256 TEXT,
                    created_epoch_ns INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workers(
                    worker_id TEXT PRIMARY KEY,
                    endpoint_id TEXT NOT NULL UNIQUE,
                    endpoint_url TEXT NOT NULL UNIQUE,
                    server_identity TEXT NOT NULL UNIQUE,
                    endpoint_json TEXT NOT NULL,
                    inventory_json TEXT NOT NULL,
                    runtime_json TEXT NOT NULL,
                    source_json TEXT NOT NULL,
                    binding_json TEXT NOT NULL,
                    binding_sha256 TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    registered_epoch_ns INTEGER NOT NULL,
                    last_heartbeat_epoch_ns INTEGER
                );
                CREATE TABLE IF NOT EXISTS observers(
                    observer_id TEXT PRIMARY KEY,
                    endpoint_id TEXT NOT NULL UNIQUE,
                    endpoint_url TEXT NOT NULL UNIQUE,
                    server_identity TEXT NOT NULL UNIQUE,
                    endpoint_json TEXT NOT NULL,
                    binding_json TEXT NOT NULL,
                    binding_sha256 TEXT NOT NULL,
                    can_lease_cases INTEGER NOT NULL DEFAULT 0 CHECK(can_lease_cases = 0),
                    registered_epoch_ns INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS attempts(
                    attempt_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL REFERENCES cases(case_id),
                    attempt_no INTEGER NOT NULL,
                    worker_id TEXT NOT NULL REFERENCES workers(worker_id),
                    endpoint_id TEXT NOT NULL,
                    endpoint_url TEXT NOT NULL,
                    lease_token_sha256 TEXT NOT NULL,
                    owner_pid INTEGER NOT NULL,
                    owner_start_ticks INTEGER,
                    owner_boot_id TEXT,
                    runner_pid INTEGER,
                    runner_start_ticks INTEGER,
                    runner_boot_id TEXT,
                    launch_state TEXT NOT NULL DEFAULT 'unstarted',
                    launch_command_json TEXT,
                    launch_command_sha256 TEXT,
                    runner_exit_json TEXT,
                    artifact_dir TEXT NOT NULL,
                    case_sha256 TEXT NOT NULL,
                    worker_binding_sha256 TEXT NOT NULL,
                    retry_of_attempt_id TEXT,
                    status TEXT NOT NULL,
                    claimed_epoch_ns INTEGER NOT NULL,
                    last_heartbeat_epoch_ns INTEGER NOT NULL,
                    ended_epoch_ns INTEGER,
                    result_path TEXT,
                    result_sha256 TEXT,
                    artifact_manifest_path TEXT,
                    artifact_manifest_sha256 TEXT,
                    outcome_classification TEXT,
                    outcome_json TEXT,
                    retry_provenance_json TEXT,
                    UNIQUE(case_id, attempt_no)
                );
                CREATE TABLE IF NOT EXISTS events(
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    entity_id TEXT,
                    payload_json TEXT NOT NULL,
                    created_epoch_ns INTEGER NOT NULL,
                    actor_pid INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS worker_binding_history(
                    worker_id TEXT NOT NULL,
                    binding_sha256 TEXT NOT NULL,
                    endpoint_id TEXT NOT NULL,
                    binding_json TEXT NOT NULL,
                    migration_id TEXT NOT NULL,
                    recorded_epoch_ns INTEGER NOT NULL,
                    PRIMARY KEY(worker_id, binding_sha256)
                );
                CREATE INDEX IF NOT EXISTS worker_binding_history_sha_idx
                    ON worker_binding_history(binding_sha256);
                CREATE UNIQUE INDEX IF NOT EXISTS active_case_idx
                    ON attempts(case_id) WHERE status = 'active';
                CREATE UNIQUE INDEX IF NOT EXISTS active_worker_idx
                    ON attempts(worker_id) WHERE status = 'active';
                CREATE UNIQUE INDEX IF NOT EXISTS active_endpoint_idx
                    ON attempts(endpoint_id) WHERE status = 'active';
                CREATE INDEX IF NOT EXISTS attempt_case_idx ON attempts(case_id, attempt_no);
                CREATE INDEX IF NOT EXISTS attempt_status_idx ON attempts(status);
                """
            )
            connection.execute("PRAGMA user_version = 2")
            connection.commit()
        finally:
            connection.close()

    def _connect(self, *, raw: bool = False) -> sqlite3.Connection:
        if not self.db_path.exists() and not raw:
            raise QueueNotReady(f"queue database is unavailable: {self.db_path}")
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        try:
            connection = sqlite3.connect(
                str(self.db_path),
                timeout=30.0,
                isolation_level=None,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
            if not raw and connection.execute("PRAGMA user_version").fetchone()[0] != 2:
                connection.close()
                raise QueueNotReady("queue schema requires v2 launch proof; preserve old databases for reviewed migration")
            return connection
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise QueueBusy(f"queue database is busy: {self.db_path}") from exc
            raise QueueError(f"cannot open queue database {self.db_path}: {exc}") from exc

    def _check_existing(self) -> None:
        connection = self._connect()
        try:
            row = connection.execute("SELECT value_json FROM meta WHERE key = 'schema_version'").fetchone()
            if row is None or json.loads(row[0]) != QUEUE_SCHEMA:
                raise QueueNotReady("queue database schema is unsupported")
        finally:
            connection.close()

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            try:
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                    raise QueueBusy("queue database lock could not be acquired") from exc
                raise
            yield connection
            connection.commit()
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise LeaseConflict(f"durable ownership constraint rejected the operation: {exc}") from exc
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _event_locked(
        self,
        connection: sqlite3.Connection,
        event_type: str,
        entity_id: Optional[str],
        payload: Mapping[str, Any],
    ) -> None:
        connection.execute(
            "INSERT INTO events(event_type, entity_id, payload_json, created_epoch_ns, actor_pid) VALUES (?, ?, ?, ?, ?)",
            (event_type, entity_id, _canonical(payload), _now_ns(), os.getpid()),
        )

    def meta(self, key: str) -> Any:
        connection = self._connect()
        try:
            row = connection.execute("SELECT value_json FROM meta WHERE key = ?", (key,)).fetchone()
            if row is None:
                raise QueueNotReady(f"queue metadata is missing: {key}")
            return json.loads(row[0])
        finally:
            connection.close()

    def _meta_exists(self, key: str) -> bool:
        connection = self._connect()
        try:
            return connection.execute("SELECT 1 FROM meta WHERE key = ?", (key,)).fetchone() is not None
        finally:
            connection.close()

    def _allowed_workers(self) -> Tuple[str, ...]:
        values = self.meta("allowed_worker_ids")
        if not isinstance(values, list) or not values or not all(isinstance(value, str) for value in values):
            raise QueueNotReady("queue worker pool metadata is malformed")
        return tuple(values)

    @staticmethod
    def _ensure_binding_history_table(connection: sqlite3.Connection) -> None:
        """Create the migration extension without changing the base schema."""

        connection.execute(
            """CREATE TABLE IF NOT EXISTS worker_binding_history(
                worker_id TEXT NOT NULL,
                binding_sha256 TEXT NOT NULL,
                endpoint_id TEXT NOT NULL,
                binding_json TEXT NOT NULL,
                migration_id TEXT NOT NULL,
                recorded_epoch_ns INTEGER NOT NULL,
                PRIMARY KEY(worker_id, binding_sha256)
            )"""
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS worker_binding_history_sha_idx ON worker_binding_history(binding_sha256)"
        )

    @staticmethod
    def _revision_binding_from_record(
        record: Mapping[str, Any],
        *,
        allowed: Sequence[str],
        base_dir: Path,
    ) -> Dict[str, Any]:
        """Normalize a worker-pool row exactly as normal registration does."""

        if not isinstance(record, Mapping):
            raise QueueNotReady("worker manifest row must be an object")
        if record.get("role") == "observer" or record.get("observer_id") is not None or record.get("can_lease_cases") is False:
            raise QueueNotReady("binding revision accepts dispatch workers only")
        worker_id = _normalise_worker_id(record.get("worker_id"), allowed)
        endpoint = record.get("endpoint")
        if endpoint is None:
            endpoint = {
                key: record[key]
                for key in ("endpoint_id", "api_base", "url", "server_identity", "served_model", "metrics_url", "counter_epoch")
                if key in record
            }
        endpoint_value = _normalise_endpoint(endpoint)
        inventory = record.get("inventory")
        runtime = record.get("runtime") or record.get("runtime_manifest")
        source = record.get("source") or record.get("source_bundle")
        if inventory is None or runtime is None or source is None:
            raise QueueNotReady("inventory, runtime, and source bindings are all required")
        inventory_value = _normalise_descriptor(inventory, "inventory", base=base_dir, require_declared_hash=True)
        runtime_value = _normalise_descriptor(runtime, "runtime", base=base_dir, require_declared_hash=True)
        source_value = _normalise_descriptor(source, "source", base=base_dir, require_declared_hash=True)
        binding = {
            "schema_version": WORKER_BINDING_SCHEMA,
            "worker_id": worker_id,
            "endpoint": endpoint_value,
            "inventory": inventory_value,
            "runtime": runtime_value,
            "source": source_value,
        }
        endpoint_id = str(endpoint_value["endpoint_id"])
        endpoint_url = str(endpoint_value["api_base"])
        server_identity = str(endpoint_value.get("server_identity") or endpoint_id)
        return {
            "worker_id": worker_id,
            "endpoint_id": endpoint_id,
            "endpoint_url": endpoint_url,
            "server_identity": server_identity,
            "endpoint_json": _canonical(endpoint_value),
            "inventory_json": _canonical(inventory_value),
            "runtime_json": _canonical(runtime_value),
            "source_json": _canonical(source_value),
            "binding_json": _canonical(binding),
            "binding_sha256": _sha256_bytes((_canonical(binding) + "\n").encode("utf-8")),
            "enabled": bool(record.get("enabled", True)),
        }

    @staticmethod
    def _validate_revision_id(migration_id: Any) -> str:
        if not isinstance(migration_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", migration_id):
            raise QueueNotReady("migration_id must be 1-128 ASCII letters, digits, dot, underscore, or hyphen")
        return migration_id

    def _resolve_audit_worker_binding(
        self,
        connection: sqlite3.Connection,
        row: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Resolve current or preserved historical binding for sealed evidence."""

        worker = connection.execute("SELECT * FROM workers WHERE worker_id = ?", (row["worker_id"],)).fetchone()
        expected_sha = str(row["worker_binding_sha256"])
        expected_endpoint = str(row["endpoint_id"])
        candidate: Optional[Mapping[str, Any]] = None
        if worker is not None and worker["binding_sha256"] == expected_sha and worker["endpoint_id"] == expected_endpoint:
            candidate = worker
        else:
            table = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'worker_binding_history'"
            ).fetchone()
            if table is not None:
                candidate = connection.execute(
                    "SELECT worker_id, endpoint_id, binding_json FROM worker_binding_history "
                    "WHERE worker_id = ? AND endpoint_id = ? AND binding_sha256 = ?",
                    (row["worker_id"], expected_endpoint, expected_sha),
                ).fetchone()
        if candidate is None:
            raise ArtifactIntegrityError("worker/endpoint binding differs and no canonical historical binding is retained")
        try:
            binding = json.loads(candidate["binding_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ArtifactIntegrityError("worker binding content is not valid JSON") from exc
        if not isinstance(binding, Mapping) or binding.get("schema_version") != WORKER_BINDING_SCHEMA:
            raise ArtifactIntegrityError("worker binding schema differs")
        if binding.get("worker_id") != row["worker_id"]:
            raise ArtifactIntegrityError("worker binding worker ID differs")
        endpoint = binding.get("endpoint")
        if not isinstance(endpoint, Mapping) or str(endpoint.get("endpoint_id")) != expected_endpoint:
            raise ArtifactIntegrityError("worker binding endpoint differs")
        digest = _sha256_bytes((_canonical(binding) + "\n").encode("utf-8"))
        if digest != expected_sha:
            raise ArtifactIntegrityError("worker binding content differs")
        if candidate is worker:
            check_row: Mapping[str, Any] = worker
        else:
            check_row = {
                "binding_json": candidate["binding_json"],
                "inventory_json": _canonical(binding.get("inventory")),
                "runtime_json": _canonical(binding.get("runtime")),
                "source_json": _canonical(binding.get("source")),
            }
        binding_error = self._verify_worker_row(check_row)
        if binding_error:
            raise ArtifactIntegrityError(binding_error)
        return check_row

    def revise_idle_worker_bindings(
        self,
        worker_manifest: Path,
        *,
        migration_id: str,
        source_queue_state_sha256: str,
        review_note: str,
        adapter_manifest_path: Optional[Path] = None,
        adapter_manifest_sha256: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Apply one reviewed source/runtime binding revision to an idle copy."""

        revision_id = self._validate_revision_id(migration_id)
        source_state_sha = _validate_sha(source_queue_state_sha256, "source_queue_state_sha256")
        if not isinstance(review_note, str) or not review_note.strip():
            raise QueueNotReady("review_note is required")
        manifest_path = _absolute_path(str(worker_manifest), "worker manifest")
        manifest_value = _read_json_object(manifest_path, label="worker manifest")
        if manifest_value.get("schema_version") not in {
            None,
            "assignment.worker-inventory-manifest.v1",
            "assignment.worker-pool-manifest.v1",
            WORKER_BINDING_SCHEMA,
        }:
            raise QueueNotReady(f"unsupported worker manifest schema: {manifest_value.get('schema_version')!r}")
        manifest_rows = manifest_value.get("workers")
        if not isinstance(manifest_rows, list) or not manifest_rows:
            raise QueueNotReady("worker manifest must contain a non-empty workers list")
        manifest_sha = _sha256_file(manifest_path)
        allowed = self._allowed_workers()
        desired: Dict[str, Dict[str, Any]] = {}
        for raw in manifest_rows:
            binding = self._revision_binding_from_record(raw, allowed=allowed, base_dir=manifest_path.parent)
            worker_id = binding["worker_id"]
            if worker_id in desired:
                raise QueueNotReady(f"worker manifest contains duplicate worker: {worker_id}")
            desired[worker_id] = binding
        adapter_binding = None
        if adapter_manifest_path is not None:
            adapter_binding = _load_adapter_manifest(
                adapter_manifest_path,
                declared_sha256=adapter_manifest_sha256,
            )

        with self._transaction() as connection:
            current_rows = {
                str(item["worker_id"]): item
                for item in connection.execute("SELECT * FROM workers ORDER BY worker_id").fetchall()
            }
            if set(desired) != set(current_rows):
                raise QueueNotReady("binding revision must cover exactly the currently registered worker IDs")
            held = connection.execute(
                "SELECT attempt_id, worker_id, status FROM attempts WHERE status IN ('active', 'orphaned') ORDER BY attempt_id"
            ).fetchall()
            if held:
                raise ReconciliationRequired("idle binding revision requires no active or orphaned attempts")
            existing_revision = self._meta_locked(connection, "binding_revision")
            if existing_revision is not None:
                if not isinstance(existing_revision, Mapping) or existing_revision.get("migration_id") != revision_id:
                    raise QueueNotReady("queue already contains a different worker binding revision")
                if existing_revision.get("worker_manifest_sha256") != manifest_sha:
                    raise QueueNotReady("binding revision ID is already bound to a different worker manifest")
                if all(current_rows[key]["binding_sha256"] == desired[key]["binding_sha256"] for key in desired):
                    return {
                        "schema_version": BINDING_REVISION_SCHEMA,
                        "status": "already_applied",
                        "migration_id": revision_id,
                        "worker_manifest_path": str(manifest_path),
                        "worker_manifest_sha256": manifest_sha,
                        "source_queue_state_sha256": source_state_sha,
                        "worker_count": len(desired),
                        "old_worker_bindings": existing_revision.get("old_worker_bindings", []),
                        "new_worker_bindings": sorted(
                            ({"worker_id": key, "binding_sha256": value["binding_sha256"]} for key, value in desired.items()),
                            key=lambda item: item["worker_id"],
                        ),
                    }
                raise QueueNotReady("existing binding revision metadata disagrees with current worker rows")
            self._ensure_binding_history_table(connection)
            old_bindings: List[Dict[str, str]] = []
            for worker_id, current in sorted(current_rows.items()):
                old_binding = json.loads(current["binding_json"])
                old_digest = _sha256_bytes((_canonical(old_binding) + "\n").encode("utf-8"))
                if old_digest != current["binding_sha256"]:
                    raise ArtifactIntegrityError(f"current worker binding content differs: {worker_id}")
                if old_binding.get("worker_id") != worker_id or old_binding.get("endpoint", {}).get("endpoint_id") != current["endpoint_id"]:
                    raise ArtifactIntegrityError(f"current worker identity binding differs: {worker_id}")
                if _canonical(json.loads(current["inventory_json"])) != _canonical(old_binding.get("inventory")):
                    raise ArtifactIntegrityError(f"current worker inventory binding differs: {worker_id}")
                target = desired[worker_id]
                if target["endpoint_id"] != current["endpoint_id"] or target["endpoint_url"] != current["endpoint_url"] or target["server_identity"] != current["server_identity"]:
                    raise QueueNotReady(f"binding revision cannot change endpoint identity: {worker_id}")
                if target["inventory_json"] != current["inventory_json"]:
                    raise QueueNotReady(f"binding revision cannot change inventory identity: {worker_id}")
                if target["enabled"] != bool(current["enabled"]):
                    raise QueueNotReady(f"binding revision cannot change enabled state: {worker_id}")
                history = connection.execute(
                    "SELECT endpoint_id, binding_json, migration_id FROM worker_binding_history "
                    "WHERE worker_id = ? AND binding_sha256 = ?",
                    (worker_id, current["binding_sha256"]),
                ).fetchone()
                if history is None:
                    connection.execute(
                        "INSERT INTO worker_binding_history(worker_id, binding_sha256, endpoint_id, binding_json, migration_id, recorded_epoch_ns) VALUES (?, ?, ?, ?, ?, ?)",
                        (worker_id, current["binding_sha256"], current["endpoint_id"], current["binding_json"], revision_id, _now_ns()),
                    )
                elif history["endpoint_id"] != current["endpoint_id"] or history["binding_json"] != current["binding_json"]:
                    raise ArtifactIntegrityError(f"historical binding digest collision: {worker_id}")
                old_bindings.append({"worker_id": worker_id, "binding_sha256": current["binding_sha256"]})
            for worker_id, target in sorted(desired.items()):
                connection.execute(
                    """UPDATE workers SET endpoint_id = ?, endpoint_url = ?, server_identity = ?,
                        endpoint_json = ?, inventory_json = ?, runtime_json = ?, source_json = ?,
                        binding_json = ?, binding_sha256 = ?, enabled = ?, registered_epoch_ns = ?,
                        last_heartbeat_epoch_ns = NULL WHERE worker_id = ?""",
                    (
                        target["endpoint_id"], target["endpoint_url"], target["server_identity"],
                        target["endpoint_json"], target["inventory_json"], target["runtime_json"], target["source_json"],
                        target["binding_json"], target["binding_sha256"], int(target["enabled"]), _now_ns(), worker_id,
                    ),
                )
            new_bindings = sorted(
                ({"worker_id": key, "binding_sha256": value["binding_sha256"]} for key, value in desired.items()),
                key=lambda item: item["worker_id"],
            )
            previous_adapter_binding = self._meta_locked(connection, "adapter_manifest")
            revision = {
                "schema_version": BINDING_REVISION_SCHEMA,
                "migration_id": revision_id,
                "source_queue_state_sha256": source_state_sha,
                "worker_manifest_path": str(manifest_path),
                "worker_manifest_sha256": manifest_sha,
                "old_worker_bindings": old_bindings,
                "new_worker_bindings": new_bindings,
                "previous_adapter_manifest": previous_adapter_binding,
                "review_note": review_note.strip(),
                "created_epoch_ns": _now_ns(),
            }
            if adapter_binding is not None:
                revision["adapter_manifest"] = adapter_binding
                connection.execute(
                    "INSERT OR REPLACE INTO meta(key, value_json) VALUES ('adapter_manifest', ?)",
                    (_canonical(adapter_binding),),
                )
            connection.execute(
                "INSERT OR REPLACE INTO meta(key, value_json) VALUES ('worker_manifest_path', ?)",
                (_canonical(str(manifest_path)),),
            )
            connection.execute(
                "INSERT OR REPLACE INTO meta(key, value_json) VALUES ('worker_manifest_sha256', ?)",
                (_canonical(manifest_sha),),
            )
            connection.execute(
                "INSERT INTO meta(key, value_json) VALUES ('binding_revision', ?)"
                " ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
                (_canonical(revision),),
            )
            self._event_locked(
                connection,
                "worker_binding_revision_applied",
                None,
                {
                    "migration_id": revision_id,
                    "worker_manifest_sha256": manifest_sha,
                    "source_queue_state_sha256": source_state_sha,
                    "old_worker_count": len(old_bindings),
                    "new_worker_count": len(new_bindings),
                    "adapter_manifest_sha256": adapter_binding["sha256"] if adapter_binding else None,
                },
            )
        return revision

    def _artifact_root(self) -> Path:
        root = _absolute_path(self.meta("artifact_root"), "artifact_root")
        if root.is_symlink():
            raise ArtifactIntegrityError(f"artifact root is a symlink: {root}")
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _assert_not_halted(self) -> None:
        if bool(self.meta("dispatch_halted")):
            raise QueueHalted(f"queue dispatch is halted: {self.meta('halt_reason')}")

    def _halt_locked(self, connection: sqlite3.Connection, reason: str, *, entity_id: Optional[str], category: str = "integrity") -> None:
        # Later checks cannot replace an unreconciled incident.
        if self._meta_locked(connection, "dispatch_halted", False):
            return
        connection.execute(
            "INSERT OR REPLACE INTO meta(key, value_json) VALUES ('dispatch_halted', 'true')"
        )
        connection.execute(
            "INSERT OR REPLACE INTO meta(key, value_json) VALUES ('halt_reason', ?)",
            (_canonical(reason),),
        )
        connection.execute(
            "INSERT OR REPLACE INTO meta(key, value_json) VALUES ('halted_epoch_ns', ?)",
            (_canonical(_now_ns()),),
        )
        self._event_locked(connection, "dispatch_halted", entity_id, {"reason": reason})
        connection.execute("INSERT OR REPLACE INTO meta(key, value_json) VALUES ('halt_category', ?)", (_canonical(category),))

    def _advisory_locked(self, connection: sqlite3.Connection, key: str, message: str) -> None:
        """Record a non-blocking disclosure once; it never halts dispatch."""
        advisories = self._meta_locked(connection, "advisories", {})
        if not isinstance(advisories, dict):
            advisories = {}
        if key in advisories:
            return
        advisories[key] = {"message": message, "first_observed_epoch_ns": _now_ns(), "actor_pid": os.getpid()}
        connection.execute("INSERT OR REPLACE INTO meta(key, value_json) VALUES ('advisories', ?)", (_canonical(advisories),))
        self._event_locked(connection, "advisory_recorded", None, {"key": key, "message": message})

    @staticmethod
    def _meta_locked(connection: sqlite3.Connection, key: str, default: Any = None) -> Any:
        row = connection.execute("SELECT value_json FROM meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        return json.loads(row[0])

    def _fingerprint_gate_error_locked(self, connection: sqlite3.Connection, worker_id: Optional[str] = None) -> Optional[str]:
        """Revalidate the root-controlled configuration gate before a claim."""

        required_all = bool(self._meta_locked(connection, "require_all_workers", True))
        expected = self._meta_locked(connection, "required_server_max_model_len", 65536)
        require_observer = bool(self._meta_locked(connection, "observer_required", True))
        discovery = self._meta_locked(connection, "fingerprint_discovery")
        effective = self._meta_locked(connection, "fingerprint_effective")
        gate_status = self._meta_locked(connection, "fingerprint_gate_status", "missing_discovery")

        if discovery is not None:
            try:
                current = _normalise_descriptor(discovery, "fingerprint discovery", require_declared_hash=True)
            except QueueError as exc:
                return f"fingerprint discovery binding cannot be verified: {exc}"
            if current.get("sha256") != discovery.get("sha256"):
                return "fingerprint discovery artifact changed after queue initialization"
            try:
                _validate_discovery_fingerprints(
                    _read_json_object(Path(str(discovery["path"])), label="fingerprint discovery")
                )
            except QueueError as exc:
                return f"fingerprint discovery artifact is no longer valid: {exc}"

        if effective is None:
            if required_all or gate_status != "test_unconfigured":
                observed = self._meta_locked(connection, "fingerprint_discovery_summary", {})
                contexts = observed.get("contexts") if isinstance(observed, Mapping) else None
                return (
                    "effective configured fingerprints are not bound; discovery/health evidence cannot make a worker claimable"
                    + (f" (discovery contexts={contexts})" if contexts is not None else "")
                )
            return None
        try:
            current_effective = _normalise_descriptor(
                effective, "effective fingerprints", require_declared_hash=True
            )
            if current_effective.get("sha256") != effective.get("sha256"):
                return "effective fingerprint artifact changed after root binding"
            value = _read_json_object(Path(str(effective["path"])), label="effective fingerprints")
            _validate_effective_fingerprints(
                value,
                expected_max_model_len=int(expected),
                require_observer_non_leasing=require_observer,
            )
        except (QueueError, TypeError, ValueError) as exc:
            return f"effective fingerprint binding cannot be verified: {exc}"

        rows = {
            str(item.get("worker_id")): item
            for item in value.get("workers", [])
            if isinstance(item, Mapping)
        }
        # Unprepared peers do not gate admission. Held endpoints still must match.
        registered = connection.execute(
            "SELECT worker_id, endpoint_id FROM workers WHERE worker_id = ? OR worker_id IN "
            "(SELECT worker_id FROM attempts WHERE status IN ('active', 'orphaned'))", (worker_id,)
        ).fetchall()
        for row in registered:
            effective_row = rows.get(str(row["worker_id"]))
            if effective_row is None and row["worker_id"] == worker_id:
                held = connection.execute("SELECT 1 FROM attempts WHERE worker_id = ? AND status IN ('active', 'orphaned')", (worker_id,)).fetchone()
                if held is None:
                    raise QueueNotReady(f"effective configured fingerprint is not yet bound for {worker_id}")
            if effective_row is None or effective_row.get("endpoint_id") != row["endpoint_id"]:
                return f"effective fingerprint endpoint binding does not match registered {row['worker_id']}"
        observer_rows = connection.execute("SELECT can_lease_cases FROM observers").fetchall()
        if any(bool(row["can_lease_cases"]) for row in observer_rows):
            return "observer registration has case-leasing permission"
        return None

    def _storage_policy(self, descriptor: Mapping[str, Any], connection: sqlite3.Connection) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        bound = _normalise_descriptor(descriptor, "storage policy", require_declared_hash=True)
        value = _read_json_object(Path(bound["path"]), label="storage policy")
        case_count = int(self._meta_locked(connection, "case_count"))
        if (value.get("schema_version") != STORAGE_POLICY_SCHEMA or value.get("case_count") != case_count
            or value.get("worker_count") != len(READY_WORKER_IDS)):
            raise QueueNotReady("storage policy must bind this case count and all 22 inflight workers")
        _normalise_descriptor(value.get("pilot_evidence"), "measured pilot storage evidence", require_declared_hash=True)
        resources = value.get("filesystems")
        if not isinstance(resources, list) or not resources:
            raise QueueNotReady("storage policy needs measured filesystem reserves")
        roles: Dict[str, Path] = {}
        devices = set()
        probes = []
        unfinished = int(connection.execute("SELECT COUNT(*) FROM cases WHERE status != ?", (CASE_ACCEPTED,)).fetchone()[0])
        for resource in resources:
            if not isinstance(resource, Mapping):
                raise QueueNotReady("storage filesystem entry must be an object")
            path = _absolute_path(resource.get("path"), "storage filesystem")
            if not path.is_dir():
                raise QueueNotReady(f"storage filesystem directory is unavailable: {path}")
            device = path.stat().st_dev
            if device in devices:
                raise QueueNotReady("combine reserves and roles sharing the same filesystem")
            devices.add(device)
            assigned_roles = resource.get("roles")
            if not isinstance(assigned_roles, list) or not assigned_roles:
                raise QueueNotReady("storage filesystem roles are required")
            for role in assigned_roles:
                if role not in {"queue", "artifacts", "container_storage"} or role in roles:
                    raise QueueNotReady("storage roles must be unique queue/artifacts/container_storage bindings")
                roles[role] = path
            numbers = {}
            for field in ("pilot_case_peak_bytes", "final_total_estimate_bytes", "safety_reserve_bytes"):
                number = resource.get(field)
                if type(number) is not int or number <= 0:
                    raise QueueNotReady(f"storage {field} must be an explicit measured/reviewed positive integer")
                numbers[field] = number
            # statvfs does not prove a user's quota. Unknown/enforced quotas need
            # a separate reviewed quota-aware adapter, not an invented allowance.
            if resource.get("quota_scope") != "no_user_quota":
                raise QueueNotReady("storage quota is unknown or enforced; statvfs alone is insufficient")
            fs = os.statvfs(path)
            required = (numbers["safety_reserve_bytes"]
                        + len(READY_WORKER_IDS) * numbers["pilot_case_peak_bytes"]
                        + (numbers["final_total_estimate_bytes"] * unfinished + case_count - 1) // case_count)
            probes.append({"path": str(path), "device": device, "filesystem_id": fs.f_fsid,
                           "available_bytes": fs.f_bavail * fs.f_frsize, "required_bytes": required})
        if set(roles) != {"queue", "artifacts", "container_storage"}:
            raise QueueNotReady("storage policy must cover queue, raw artifacts, and retained container storage")
        for role, target in (("queue", self.queue_dir), ("artifacts", Path(self._meta_locked(connection, "artifact_root")))):
            if not target.is_relative_to(roles[role]) or target.stat().st_dev != roles[role].stat().st_dev:
                raise QueueNotReady(f"storage {role} role does not cover the actual output filesystem")
        return value, probes

    def bind_storage_policy(self, path: Path, *, declared_sha256: str, review_note: str) -> Dict[str, Any]:
        if not isinstance(review_note, str) or not review_note.strip():
            raise QueueNotReady("storage policy requires root's measurement and reserve review note")
        descriptor = _normalise_descriptor({"path": str(path), "sha256": declared_sha256}, "storage policy", require_declared_hash=True)
        with self._transaction() as connection:
            if connection.execute("SELECT 1 FROM attempts WHERE status IN ('active', 'orphaned') LIMIT 1").fetchone():
                raise ReconciliationRequired("storage policy cannot change while a case or unknown child is held")
            _, probes = self._storage_policy(descriptor, connection)
            binding = {**descriptor, "filesystems": [{k: p[k] for k in ("path", "device", "filesystem_id")} for p in probes]}
            connection.execute("INSERT OR REPLACE INTO meta(key, value_json) VALUES ('storage_policy', ?)", (_canonical(binding),))
            self._event_locked(connection, "storage_policy_bound", None, {"binding": binding, "review_note": review_note, "probes": probes})
            error = self._storage_gate_error_locked(connection)
            if error:
                self._halt_locked(connection, error, entity_id=None)
        return {"status": "halted" if error else "bound", "policy": binding, "error": error}

    def _storage_gate_error_locked(self, connection: sqlite3.Connection) -> Optional[str]:
        descriptor = self._meta_locked(connection, "storage_policy")
        if descriptor is None:
            # A planning policy is advisory: its absence is disclosed, not a
            # halt.  Demonstrated shortfall or identity drift of a bound policy
            # below still halts dispatch.
            self._advisory_locked(connection, "storage_policy_missing",
                                  "no measured storage policy is bound; capacity is not being verified before claims")
            return None
        try:
            _, probes = self._storage_policy(descriptor, connection)
            identities = [{k: p[k] for k in ("path", "device", "filesystem_id")} for p in probes]
            if identities != descriptor.get("filesystems"):
                return "storage filesystem identity changed after policy binding"
            for probe in probes:
                if probe["available_bytes"] < probe["required_bytes"]:
                    return f"storage reserve insufficient: {probe['path']} available={probe['available_bytes']} required={probe['required_bytes']}"
        except (QueueError, OSError, TypeError, ValueError) as exc:
            return f"storage reserve cannot be verified: {exc}"
        return None

    def bind_effective_fingerprints(
        self,
        path: Path,
        *,
        expected_max_model_len: Optional[int] = None,
        require_observer_non_leasing: Optional[bool] = None,
        declared_sha256: Optional[str] = None,
        review_note: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Bind root's reviewed effective 65k configuration artifact.

        This is intentionally a separate operation from discovery import.  It
        is the only operation that can release the configuration gate, and it
        never registers an observer as a lease-capable worker.
        """

        descriptor = _normalise_descriptor(path, "effective fingerprints")
        if declared_sha256 is not None and _validate_sha(declared_sha256, "effective fingerprints.sha256") != descriptor["sha256"]:
            raise ArtifactIntegrityError("effective fingerprint SHA-256 differs from the supplied binding")
        value = _read_json_object(Path(descriptor["path"]), label="effective fingerprints")
        with self._transaction() as connection:
            expected = int(
                expected_max_model_len
                if expected_max_model_len is not None
                else self._meta_locked(connection, "required_server_max_model_len", 65536)
            )
            observer_required = bool(
                require_observer_non_leasing
                if require_observer_non_leasing is not None
                else self._meta_locked(connection, "observer_required", True)
            )
            summary = _validate_effective_fingerprints(
                value,
                expected_max_model_len=expected,
                require_observer_non_leasing=observer_required,
            )
            if bool(self._meta_locked(connection, "require_all_workers", True)) and self._meta_locked(connection, "fingerprint_discovery") is None:
                raise QueueNotReady("production effective binding requires the discovery artifact input as well")
            rows = {str(item["worker_id"]): item for item in value["workers"]}
            for row in connection.execute("SELECT worker_id, endpoint_id FROM workers").fetchall():
                if str(row["worker_id"]) in rows and rows[str(row["worker_id"])].get("endpoint_id") != row["endpoint_id"]:
                    raise QueueNotReady(
                        f"effective fingerprint endpoint binding differs from registered {row['worker_id']}"
                    )
            held_ids = {row[0] for row in connection.execute("SELECT worker_id FROM attempts WHERE status IN ('active', 'orphaned')")}
            if not held_ids.issubset(rows):
                raise ReconciliationRequired("effective binding cannot omit an active or orphaned endpoint")
            if any(bool(row["can_lease_cases"]) for row in connection.execute("SELECT can_lease_cases FROM observers").fetchall()):
                raise QueueNotReady("observer registration cannot lease cases")
            current_halted = bool(self._meta_locked(connection, "dispatch_halted", False))
            current_gate_status = self._meta_locked(connection, "fingerprint_gate_status", "missing_discovery")
            connection.execute(
                "INSERT OR REPLACE INTO meta(key, value_json) VALUES ('fingerprint_effective', ?)",
                (_canonical(descriptor),),
            )
            connection.execute(
                "INSERT OR REPLACE INTO meta(key, value_json) VALUES ('fingerprint_effective_summary', ?)",
                (_canonical(summary),),
            )
            connection.execute(
                "INSERT OR REPLACE INTO meta(key, value_json) VALUES ('fingerprint_gate_status', ?)",
                (_canonical("effective_ready"),),
            )
            # Binding configuration must never clear capture/storage incidents.
            # clear_halt independently rechecks every gate after explicit review.
            release_gate = False
            if release_gate and current_halted:
                connection.execute("INSERT OR REPLACE INTO meta(key, value_json) VALUES ('dispatch_halted', 'false')")
                connection.execute("INSERT OR REPLACE INTO meta(key, value_json) VALUES ('halt_reason', 'null')")
            note = review_note.strip() if isinstance(review_note, str) else None
            self._event_locked(
                connection,
                "effective_fingerprints_bound",
                None,
                {
                    "path": descriptor["path"],
                    "sha256": descriptor["sha256"],
                    "summary": summary,
                    "review_note": note,
                    "dispatch_released": bool(release_gate and current_halted),
                },
            )
        return {
            "status": "ready" if release_gate and current_halted else "bound",
            "path": descriptor["path"],
            "sha256": descriptor["sha256"],
            "summary": summary,
        }

    def _worker_row(self, connection: sqlite3.Connection, worker_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,)).fetchone()
        if row is None:
            raise QueueNotReady(f"worker is not registered: {worker_id}")
        if not bool(row["enabled"]):
            raise QueueNotReady(f"worker is disabled: {worker_id}")
        return row

    def _verify_worker_row(self, row: Mapping[str, Any]) -> Optional[str]:
        checks = (
            ("inventory_json", "inventory"),
            ("runtime_json", "runtime"),
            ("source_json", "source"),
        )
        for column, label in checks:
            try:
                descriptor = json.loads(row[column])
                normalised = _normalise_descriptor(descriptor, label, require_declared_hash=True)
            except QueueError as exc:
                return str(exc)
            if normalised.get("sha256") != descriptor.get("sha256"):
                return f"{label} binding changed on disk"
        return None

    def register_observer(
        self,
        observer_id: Any,
        *,
        endpoint: Any = None,
        endpoint_id: Optional[str] = None,
        endpoint_url: Optional[str] = None,
        can_lease_cases: bool = False,
    ) -> Dict[str, Any]:
        """Register an observer identity in a table with no lease path."""

        if not isinstance(observer_id, str) or not observer_id.strip():
            raise QueueNotReady("observer_id is required")
        observer_id = observer_id.strip()
        if observer_id in set(self._allowed_workers()) or observer_id == EXPIRED_WORKER_ID:
            raise QueueNotReady("an observer cannot use a dispatch worker ID")
        if can_lease_cases:
            raise QueueNotReady("observer registration cannot lease cases")
        endpoint_value = _normalise_endpoint(endpoint, endpoint_id=endpoint_id, endpoint_url=endpoint_url)
        endpoint_id_value = str(endpoint_value["endpoint_id"])
        endpoint_url_value = str(endpoint_value["api_base"])
        server_identity = str(endpoint_value.get("server_identity") or endpoint_id_value)
        binding = {
            "schema_version": "assignment.observer-binding.v1",
            "observer_id": observer_id,
            "endpoint": endpoint_value,
            "can_lease_cases": False,
        }
        binding_sha256 = _sha256_bytes((_canonical(binding) + "\n").encode("utf-8"))
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM observers WHERE observer_id = ?", (observer_id,)
            ).fetchone()
            if existing is not None:
                if existing["binding_sha256"] != binding_sha256:
                    raise QueueNotReady(f"observer {observer_id} is already registered with a different binding")
                return self._observer_public(existing)
            conflict = connection.execute(
                "SELECT worker_id AS identity FROM workers WHERE endpoint_id = ? OR endpoint_url = ? OR server_identity = ? "
                "UNION ALL SELECT observer_id AS identity FROM observers WHERE endpoint_id = ? OR endpoint_url = ? OR server_identity = ? LIMIT 1",
                (endpoint_id_value, endpoint_url_value, server_identity, endpoint_id_value, endpoint_url_value, server_identity),
            ).fetchone()
            if conflict is not None:
                raise LeaseConflict(f"endpoint identity is already registered to {conflict['identity']}")
            connection.execute(
                """INSERT INTO observers(
                    observer_id, endpoint_id, endpoint_url, server_identity,
                    endpoint_json, binding_json, binding_sha256, can_lease_cases,
                    registered_epoch_ns
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)""",
                (
                    observer_id,
                    endpoint_id_value,
                    endpoint_url_value,
                    server_identity,
                    _canonical(endpoint_value),
                    _canonical(binding),
                    binding_sha256,
                    _now_ns(),
                ),
            )
            self._event_locked(
                connection,
                "observer_registered_non_leasing",
                observer_id,
                {"observer_id": observer_id, "endpoint_id": endpoint_id_value, "binding_sha256": binding_sha256},
            )
            row = connection.execute("SELECT * FROM observers WHERE observer_id = ?", (observer_id,)).fetchone()
            assert row is not None
            return self._observer_public(row)

    def register_observer_record(
        self,
        record: Mapping[str, Any],
        *,
        base_dir: Optional[Path] = None,
    ) -> Dict[str, Any]:
        if not isinstance(record, Mapping):
            raise QueueNotReady("observer manifest row must be an object")
        observer_id = record.get("observer_id") or record.get("worker_id")
        endpoint = record.get("endpoint")
        if endpoint is None:
            endpoint = {
                key: record[key]
                for key in ("endpoint_id", "api_base", "url", "server_identity")
                if key in record
            }
        # ``base_dir`` is accepted to keep manifest adapter calls uniform. An
        # observer endpoint is metadata only and contains no filesystem path.
        del base_dir
        return self.register_observer(
            observer_id,
            endpoint=endpoint,
            can_lease_cases=record.get("can_lease_cases", False),
        )

    @staticmethod
    def _observer_public(row: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "observer_id": row["observer_id"],
            "role": "observer",
            "endpoint_id": row["endpoint_id"],
            "endpoint_url": row["endpoint_url"],
            "server_identity": row["server_identity"],
            "binding_sha256": row["binding_sha256"],
            "can_lease_cases": False,
        }

    def register_worker(
        self,
        worker_id: Any,
        *,
        endpoint: Any = None,
        inventory: Any = None,
        runtime: Any = None,
        source: Any = None,
        endpoint_id: Optional[str] = None,
        endpoint_url: Optional[str] = None,
        enabled: bool = True,
        base_dir: Optional[Path] = None,
        require_declared_hash: bool = False,
    ) -> Dict[str, Any]:
        allowed = self._allowed_workers()
        canonical_worker_id = _normalise_worker_id(worker_id, allowed)
        endpoint_value = _normalise_endpoint(endpoint, endpoint_id=endpoint_id, endpoint_url=endpoint_url)
        if inventory is None or runtime is None or source is None:
            raise QueueNotReady("inventory, runtime, and source bindings are all required")
        inventory_value = _normalise_descriptor(
            inventory, "inventory", base=base_dir, require_declared_hash=require_declared_hash
        )
        runtime_value = _normalise_descriptor(
            runtime, "runtime", base=base_dir, require_declared_hash=require_declared_hash
        )
        source_value = _normalise_descriptor(
            source, "source", base=base_dir, require_declared_hash=require_declared_hash
        )
        binding = {
            "schema_version": WORKER_BINDING_SCHEMA,
            "worker_id": canonical_worker_id,
            "endpoint": endpoint_value,
            "inventory": inventory_value,
            "runtime": runtime_value,
            "source": source_value,
        }
        binding_sha256 = _sha256_bytes((_canonical(binding) + "\n").encode("utf-8"))
        endpoint_id_value = str(endpoint_value["endpoint_id"])
        endpoint_url_value = str(endpoint_value["api_base"])
        server_identity = str(endpoint_value.get("server_identity") or endpoint_id_value)
        with self._transaction() as connection:
            existing = connection.execute("SELECT * FROM workers WHERE worker_id = ?", (canonical_worker_id,)).fetchone()
            conflict = connection.execute(
                "SELECT worker_id AS identity FROM workers WHERE endpoint_id = ? OR endpoint_url = ? OR server_identity = ? "
                "UNION ALL SELECT observer_id AS identity FROM observers WHERE endpoint_id = ? OR endpoint_url = ? OR server_identity = ? LIMIT 1",
                (endpoint_id_value, endpoint_url_value, server_identity, endpoint_id_value, endpoint_url_value, server_identity),
            ).fetchone()
            if conflict is not None and conflict["identity"] != canonical_worker_id:
                raise LeaseConflict(f"endpoint identity is already registered to {conflict['identity']}")
            if existing is not None:
                if existing["binding_sha256"] != binding_sha256 or bool(existing["enabled"]) != bool(enabled):
                    raise QueueNotReady(f"worker {canonical_worker_id} is already registered with a different binding")
                return self._worker_public(existing)
            connection.execute(
                """INSERT INTO workers(
                    worker_id, endpoint_id, endpoint_url, server_identity,
                    endpoint_json, inventory_json, runtime_json, source_json,
                    binding_json, binding_sha256, enabled, registered_epoch_ns
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    canonical_worker_id,
                    endpoint_id_value,
                    endpoint_url_value,
                    server_identity,
                    _canonical(endpoint_value),
                    _canonical(inventory_value),
                    _canonical(runtime_value),
                    _canonical(source_value),
                    _canonical(binding),
                    binding_sha256,
                    int(bool(enabled)),
                    _now_ns(),
                ),
            )
            self._event_locked(
                connection,
                "worker_registered",
                canonical_worker_id,
                {"worker_id": canonical_worker_id, "endpoint_id": endpoint_id_value, "binding_sha256": binding_sha256},
            )
            row = connection.execute("SELECT * FROM workers WHERE worker_id = ?", (canonical_worker_id,)).fetchone()
            assert row is not None
            return self._worker_public(row)

    def register_worker_record(
        self,
        record: Mapping[str, Any],
        *,
        base_dir: Optional[Path] = None,
        require_declared_hash: bool = True,
    ) -> Dict[str, Any]:
        if not isinstance(record, Mapping):
            raise QueueNotReady("worker manifest row must be an object")
        if record.get("role") == "observer" or record.get("observer_id") is not None or record.get("can_lease_cases") is False:
            return self.register_observer_record(record, base_dir=base_dir)
        endpoint = record.get("endpoint")
        if endpoint is None:
            endpoint = {
                key: record[key]
                for key in ("endpoint_id", "api_base", "url", "server_identity", "served_model", "metrics_url", "counter_epoch")
                if key in record
            }
        runtime = record.get("runtime") or record.get("runtime_manifest")
        source = record.get("source") or record.get("source_bundle")
        return self.register_worker(
            record.get("worker_id"),
            endpoint=endpoint,
            inventory=record.get("inventory"),
            runtime=runtime,
            source=source,
            enabled=bool(record.get("enabled", True)),
            base_dir=base_dir,
            require_declared_hash=require_declared_hash,
        )

    def register_workers_manifest(self, path: Path) -> List[Dict[str, Any]]:
        manifest_path = _absolute_path(str(path), "worker manifest")
        value = _read_json_object(manifest_path, label="worker manifest")
        schema = value.get("schema_version")
        if schema not in {
            None,
            "assignment.worker-inventory-manifest.v1",
            "assignment.worker-pool-manifest.v1",
            WORKER_BINDING_SCHEMA,
        }:
            raise QueueNotReady(f"unsupported worker manifest schema: {schema}")
        rows = value.get("workers")
        if not isinstance(rows, list) or not rows:
            raise QueueNotReady("worker manifest must contain a non-empty workers list")
        results = []
        for row in rows:
            results.append(
                self.register_worker_record(
                    row,
                    base_dir=manifest_path.parent,
                    require_declared_hash=True,
                )
            )
        manifest_digest = _sha256_file(manifest_path)
        with self._transaction() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO meta(key, value_json) VALUES ('worker_manifest_path', ?)",
                (_canonical(str(manifest_path)),),
            )
            connection.execute(
                "INSERT OR REPLACE INTO meta(key, value_json) VALUES ('worker_manifest_sha256', ?)",
                (_canonical(manifest_digest),),
            )
            self._event_locked(
                connection,
                "worker_manifest_bound",
                None,
                {"path": str(manifest_path), "sha256": manifest_digest, "worker_count": len(results)},
            )
        return results

    @staticmethod
    def _worker_public(row: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "worker_id": row["worker_id"],
            "endpoint_id": row["endpoint_id"],
            "endpoint_url": row["endpoint_url"],
            "server_identity": row["server_identity"],
            "binding_sha256": row["binding_sha256"],
            "inventory": json.loads(row["inventory_json"]),
            "runtime": json.loads(row["runtime_json"]),
            "source": json.loads(row["source_json"]),
            "enabled": bool(row["enabled"]),
        }

    def _lease_parts(self, lease_or_attempt: Any, lease_token: Optional[str]) -> Tuple[str, str]:
        if isinstance(lease_or_attempt, Lease):
            return lease_or_attempt.attempt_id, lease_or_attempt.lease_token
        if isinstance(lease_or_attempt, Mapping):
            attempt_id = lease_or_attempt.get("attempt_id") or lease_or_attempt.get("id")
            token = lease_or_attempt.get("lease_token") or lease_token
        else:
            attempt_id = lease_or_attempt
            token = lease_token
        if not isinstance(attempt_id, str) or not attempt_id.strip():
            raise QueueError("attempt_id is required")
        if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
            raise QueueError("lease_token is required")
        return attempt_id, token

    def _authorise_attempt(
        self,
        connection: sqlite3.Connection,
        lease_or_attempt: Any,
        lease_token: Optional[str],
    ) -> sqlite3.Row:
        attempt_id, token = self._lease_parts(lease_or_attempt, lease_token)
        row = connection.execute("SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
        if row is None:
            raise QueueNotReady(f"attempt is unknown: {attempt_id}")
        if _sha256_bytes(token.encode("ascii")) != row["lease_token_sha256"]:
            raise QueueError("lease token does not match attempt ownership")
        return row

    def _case_from_row(self, row: Mapping[str, Any]) -> Dict[str, Any]:
        try:
            value = json.loads(bytes(row["case_bytes"]).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError, TypeError) as exc:
            raise ArtifactIntegrityError(f"stored case bytes are not valid JSON: {row['case_id']}") from exc
        if not isinstance(value, dict):
            raise ArtifactIntegrityError(f"stored case bytes are not a JSON object: {row['case_id']}")
        return value

    def _lease_from_rows(self, attempt: Mapping[str, Any], case_row: Mapping[str, Any]) -> Lease:
        owner_pid = int(attempt["owner_pid"])
        token = str(attempt["lease_token_sha256"])
        # The plaintext token is intentionally never reconstructed from the
        # database.  Claim callers receive it directly; reloading a lease for
        # an active supervisor requires the original lease object/token.
        raise QueueError("internal lease conversion requires the plaintext token")

    def claim_case(self, worker_id: Any, *, owner_pid: Optional[int] = None) -> Optional[Lease]:
        allowed = self._allowed_workers()
        canonical_worker_id = _normalise_worker_id(worker_id, allowed)
        owner_pid_value = owner_pid if owner_pid is not None else os.getpid()
        if not isinstance(owner_pid_value, int) or isinstance(owner_pid_value, bool) or owner_pid_value <= 0:
            raise QueueError("owner_pid must be a positive integer")
        owner_identity = _process_identity(owner_pid_value)
        if owner_identity.get("status") != "alive":
            raise QueueNotReady("owner_pid must identify a live process before a case can be claimed")
        owner_start_ticks = owner_identity.get("start_ticks")
        owner_boot_id = owner_identity.get("boot_id")
        halt_error: Optional[str] = None
        with self._transaction() as connection:
            halted = json.loads(connection.execute("SELECT value_json FROM meta WHERE key = 'dispatch_halted'").fetchone()[0])
            if halted:
                halt_error = json.loads(connection.execute("SELECT value_json FROM meta WHERE key = 'halt_reason'").fetchone()[0])
            fingerprint_error = self._fingerprint_gate_error_locked(connection)
            if fingerprint_error is not None:
                self._halt_locked(connection, fingerprint_error, entity_id=canonical_worker_id)
                halt_error = fingerprint_error
            storage_error = self._storage_gate_error_locked(connection)
            if storage_error is not None:
                self._halt_locked(connection, storage_error, entity_id=canonical_worker_id)
                halt_error = storage_error
            worker = connection.execute("SELECT * FROM workers WHERE worker_id = ?", (canonical_worker_id,)).fetchone()
            if worker is None:
                raise QueueNotReady(f"worker is not registered: {canonical_worker_id}")
            if not bool(worker["enabled"]):
                raise QueueNotReady(f"worker is disabled: {canonical_worker_id}")
            required_all = bool(json.loads(connection.execute("SELECT value_json FROM meta WHERE key = 'require_all_workers'").fetchone()[0]))
            if required_all:
                # The full-pool barrier is advisory: a prepared worker may start
                # without waiting for every peer.  The partial pool is disclosed
                # once so the contention/placement record stays honest.
                count = int(connection.execute("SELECT COUNT(*) FROM workers WHERE enabled = 1").fetchone()[0])
                if count != len(allowed):
                    self._advisory_locked(connection, "worker_pool_partial",
                                          f"{count} of {len(allowed)} declared workers are registered and enabled at first dispatch")
            binding_error = self._verify_worker_row(worker)
            if binding_error:
                connection.execute("UPDATE workers SET enabled = 0 WHERE worker_id = ?", (canonical_worker_id,))
                self._halt_locked(connection, f"worker binding integrity failure for {canonical_worker_id}: {binding_error}", entity_id=canonical_worker_id)
                halt_error = f"worker binding integrity failure for {canonical_worker_id}: {binding_error}"
            if halt_error is None:
                held_worker = connection.execute(
                    "SELECT attempt_id, status FROM attempts WHERE worker_id = ? AND status IN ('active', 'orphaned') ORDER BY attempt_no DESC LIMIT 1",
                    (canonical_worker_id,),
                ).fetchone()
                if held_worker is not None:
                    raise LeaseConflict(
                        f"worker {canonical_worker_id} is held by attempt {held_worker['attempt_id']} ({held_worker['status']})"
                    )
                held_endpoint = connection.execute(
                    "SELECT attempt_id, status FROM attempts WHERE endpoint_id = ? AND status IN ('active', 'orphaned') ORDER BY attempt_no DESC LIMIT 1",
                    (worker["endpoint_id"],),
                ).fetchone()
                if held_endpoint is not None:
                    raise LeaseConflict(
                        f"endpoint {worker['endpoint_id']} is held by attempt {held_endpoint['attempt_id']} ({held_endpoint['status']})"
                    )
                case_row = connection.execute(
                    "SELECT * FROM cases WHERE status = ? ORDER BY ordinal ASC LIMIT 1", (CASE_PENDING,)
                ).fetchone()
                if case_row is None:
                    return None
                previous = connection.execute(
                    "SELECT attempt_id FROM attempts WHERE case_id = ? ORDER BY attempt_no DESC LIMIT 1",
                    (case_row["case_id"],),
                ).fetchone()
                attempt_no = int(case_row["attempt_count"]) + 1
                lease_token = uuid.uuid4().hex
                attempt_id = f"attempt-{_sha256_bytes(str(case_row['case_id']).encode('utf-8'))[:16]}-{attempt_no:03d}-{uuid.uuid4().hex[:12]}"
                artifact_root = self._artifact_root()
                artifact_dir = artifact_root / "cases" / f"{int(case_row['ordinal']):05d}-{_sha256_bytes(str(case_row['case_id']).encode('utf-8'))[:16]}" / attempt_id
                _reject_symlink_chain(artifact_dir, include_leaf=False)
                if artifact_dir.exists() or artifact_dir.is_symlink():
                    raise ArtifactIntegrityError(f"attempt artifact directory already exists: {artifact_dir}")
                artifact_dir.mkdir(parents=True, exist_ok=False)
                payload = bytes(case_row["case_bytes"])
                spec_path = artifact_dir / "case_spec.json"
                _atomic_bytes(spec_path, payload, overwrite=False)
                _write_sidecar(spec_path, str(case_row["case_sha256"]))
                binding = json.loads(worker["binding_json"])
                lease_record = {
                    "schema_version": LEASE_SCHEMA,
                    "queue_schema_version": QUEUE_SCHEMA,
                    "plan_sha256": self._meta_locked(connection, "plan_sha256"),
                    "attempt_id": attempt_id,
                    "lease_token": lease_token,
                    "lease_token_sha256": _sha256_bytes(lease_token.encode("ascii")),
                    "case_id": case_row["case_id"],
                    "resume_key": case_row["resume_key"],
                    "ordinal": case_row["ordinal"],
                    "attempt_no": attempt_no,
                    "worker_id": canonical_worker_id,
                    "endpoint_id": worker["endpoint_id"],
                    "endpoint_url": worker["endpoint_url"],
                    "case_sha256": case_row["case_sha256"],
                    "worker_binding_sha256": worker["binding_sha256"],
                    "owner_pid": owner_pid_value,
                    "owner_start_ticks": owner_start_ticks,
                    "owner_boot_id": owner_boot_id,
                    "retry_of_attempt_id": previous["attempt_id"] if previous else None,
                    "worker_binding": binding,
                    "created_epoch_ns": _now_ns(),
                }
                _atomic_json(artifact_dir / "lease.json", lease_record, overwrite=False)
                _write_sidecar(artifact_dir / "lease.json")
                now = _now_ns()
                connection.execute(
                    """INSERT INTO attempts(
                        attempt_id, case_id, attempt_no, worker_id, endpoint_id,
                        endpoint_url, lease_token_sha256, owner_pid,
                        owner_start_ticks, owner_boot_id, artifact_dir,
                        case_sha256, worker_binding_sha256, retry_of_attempt_id,
                        status, claimed_epoch_ns, last_heartbeat_epoch_ns
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        attempt_id,
                        case_row["case_id"],
                        attempt_no,
                        canonical_worker_id,
                        worker["endpoint_id"],
                        worker["endpoint_url"],
                        _sha256_bytes(lease_token.encode("ascii")),
                        owner_pid_value,
                        owner_start_ticks,
                        owner_boot_id,
                        str(artifact_dir),
                        case_row["case_sha256"],
                        worker["binding_sha256"],
                        previous["attempt_id"] if previous else None,
                        ATTEMPT_ACTIVE,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    "UPDATE cases SET status = ?, attempt_count = ? WHERE case_id = ? AND status = ?",
                    (CASE_RUNNING, attempt_no, case_row["case_id"], CASE_PENDING),
                )
                if connection.execute("SELECT changes()").fetchone()[0] != 1:
                    raise LeaseConflict("case ownership changed before the durable claim committed")
                self._event_locked(
                    connection,
                    "case_claimed",
                    attempt_id,
                    {
                        "case_id": case_row["case_id"],
                        "ordinal": case_row["ordinal"],
                        "worker_id": canonical_worker_id,
                        "endpoint_id": worker["endpoint_id"],
                        "attempt_no": attempt_no,
                        "retry_of_attempt_id": previous["attempt_id"] if previous else None,
                        "case_sha256": case_row["case_sha256"],
                        "worker_binding_sha256": worker["binding_sha256"],
                    },
                )
                return Lease(
                    attempt_id=attempt_id,
                    lease_token=lease_token,
                    case_id=str(case_row["case_id"]),
                    resume_key=str(case_row["resume_key"]),
                    ordinal=int(case_row["ordinal"]),
                    attempt_no=attempt_no,
                    worker_id=canonical_worker_id,
                    endpoint_id=str(worker["endpoint_id"]),
                    endpoint_url=str(worker["endpoint_url"]),
                    artifact_dir=artifact_dir,
                    case_sha256=str(case_row["case_sha256"]),
                    worker_binding_sha256=str(worker["binding_sha256"]),
                    owner_pid=owner_pid_value,
                    owner_start_ticks=owner_start_ticks,
                    owner_boot_id=owner_boot_id,
                    retry_of_attempt_id=str(previous["attempt_id"]) if previous else None,
                    case=self._case_from_row(case_row),
                )
        if halt_error is not None:
            raise QueueHalted(halt_error)
        return None

    def begin_launch(self, lease_or_attempt: Any, command: Sequence[str], *, lease_token: Optional[str] = None) -> None:
        """Persist uncertainty BEFORE Popen; one intent can register exactly once."""
        if not command or any(not isinstance(arg, str) or not arg or "\0" in arg for arg in command):
            raise QueueError("launch command must be nonempty argv")
        with self._transaction() as connection:
            row = self._authorise_attempt(connection, lease_or_attempt, lease_token)
            if row["status"] != ATTEMPT_ACTIVE or row["launch_state"] != "unstarted":
                raise LeaseConflict("launch intent already exists or attempt is not active")
            if _same_process_identity(row, "owner") is not True or row["owner_pid"] != os.getpid():
                raise LeaseConflict("only the recorded live owner may begin launch")
            encoded = _canonical(list(command))
            connection.execute(
                "UPDATE attempts SET launch_state = 'intent', launch_command_json = ?, launch_command_sha256 = ? WHERE attempt_id = ?",
                (encoded, _sha256_bytes(encoded.encode()), row["attempt_id"]),
            )
            self._event_locked(connection, "launch_intent", row["attempt_id"], {"command_sha256": _sha256_bytes(encoded.encode())})

    @_serialized_attempt
    def bind_runner_pid(self, lease_or_attempt: Any, *, runner_pid: int, lease_token: Optional[str] = None) -> Dict[str, Any]:
        if not isinstance(runner_pid, int) or isinstance(runner_pid, bool) or runner_pid <= 0:
            raise QueueError("runner_pid must be a positive integer")
        identity = _process_identity(runner_pid)
        if identity["status"] != "alive" or runner_pid != os.getpid() or identity.get("sid") != runner_pid:
            raise ReconciliationRequired("launch guardian must register its own proven live session leader PID")
        with self._transaction() as connection:
            row = self._authorise_attempt(connection, lease_or_attempt, lease_token)
            if row["status"] != ATTEMPT_ACTIVE:
                raise LeaseConflict(f"attempt {row['attempt_id']} is no longer active")
            if row["launch_state"] != "intent" or row["runner_pid"] is not None:
                raise LeaseConflict("launch intent has already been consumed; a second child is forbidden")
            connection.execute(
                "UPDATE attempts SET launch_state = 'bound', runner_pid = ?, runner_start_ticks = ?, runner_boot_id = ?, last_heartbeat_epoch_ns = ? WHERE attempt_id = ?",
                (runner_pid, identity.get("start_ticks"), identity.get("boot_id"), _now_ns(), row["attempt_id"]),
            )
            self._event_locked(
                connection, "runner_pid_bound", row["attempt_id"],
                {"runner_pid": runner_pid, "runner_start_ticks": identity.get("start_ticks"), "runner_boot_id": identity.get("boot_id")},
            )
            result = {"attempt_id": row["attempt_id"], "runner_pid": runner_pid, **identity}
        self._update_lease_file(row["artifact_dir"], {"runner_pid": runner_pid, "runner_identity": identity})
        return result

    def record_runner_exit(self, attempt_id: str, *, lease_token: str, returncode: int) -> None:
        """Only the live guardian can attest its reaped child and empty session."""
        with self._transaction() as connection:
            row = self._authorise_attempt(connection, attempt_id, lease_token)
            if (row["launch_state"] != "bound" or row["runner_pid"] != os.getpid()
                or _same_process_identity(row, "runner") is not True
                or _session_state(os.getpid(), exclude_pid=os.getpid()) is not False):
                raise ReconciliationRequired("child/session exit proof is incomplete; endpoint remains held")
            receipt = {"attempt_id": attempt_id, "guardian_pid": os.getpid(),
                       "guardian_start_ticks": row["runner_start_ticks"], "boot_id": row["runner_boot_id"],
                       "command_sha256": row["launch_command_sha256"], "returncode": returncode,
                       "recorded_epoch_ns": _now_ns()}
            connection.execute("UPDATE attempts SET launch_state = 'exited', runner_exit_json = ? WHERE attempt_id = ?", (_canonical(receipt), attempt_id))
            self._event_locked(connection, "runner_exit_proven", attempt_id, receipt)

    def _runner_execution_state(self, row: Mapping[str, Any]) -> Optional[bool]:
        state = row["launch_state"]
        if state == "unstarted" and row["runner_pid"] is None:
            return False
        if state == "intent":
            return None
        identity = _same_process_identity(row, "runner")
        if identity is True:
            return True
        if identity is None or state != "exited" or not row["runner_exit_json"]:
            # A dead guardian without its durable exit receipt is NOT child proof.
            return None
        receipt = json.loads(row["runner_exit_json"])
        if (receipt.get("attempt_id") != row["attempt_id"]
            or receipt.get("guardian_pid") != row["runner_pid"]
            or receipt.get("guardian_start_ticks") != row["runner_start_ticks"]
            or receipt.get("boot_id") != row["runner_boot_id"]
            or receipt.get("command_sha256") != row["launch_command_sha256"]):
            return None
        if _current_boot_id() != row["runner_boot_id"]:
            return None  # reboot alone cannot prove external work stopped
        return _session_state(int(row["runner_pid"]))

    def _assert_runner_stopped(self, row: Mapping[str, Any]) -> None:
        if self._runner_execution_state(row) is not False:
            raise ReconciliationRequired("child execution is live or unproven; case and endpoint must remain held")

    def heartbeat(self, lease_or_attempt: Any, *, lease_token: Optional[str] = None) -> None:
        with self._transaction() as connection:
            row = self._authorise_attempt(connection, lease_or_attempt, lease_token)
            if row["status"] != ATTEMPT_ACTIVE:
                raise LeaseConflict(f"attempt {row['attempt_id']} is no longer active")
            now = _now_ns()
            connection.execute(
                "UPDATE attempts SET last_heartbeat_epoch_ns = ? WHERE attempt_id = ?",
                (now, row["attempt_id"]),
            )
            connection.execute(
                "UPDATE workers SET last_heartbeat_epoch_ns = ? WHERE worker_id = ?",
                (now, row["worker_id"]),
            )
            self._event_locked(connection, "lease_heartbeat", row["attempt_id"], {"worker_id": row["worker_id"]})

    def _update_lease_file(self, artifact_dir_value: Any, updates: Mapping[str, Any]) -> None:
        artifact_dir = _absolute_path(str(artifact_dir_value), "attempt artifact directory")
        lease_path = artifact_dir / "lease.json"
        _verify_sidecar(lease_path, label="lease metadata")
        value = _read_json_object(lease_path, label="lease metadata")
        value.update(dict(updates))
        _atomic_json(lease_path, value)
        _write_sidecar(lease_path)

    def _attempt_row(self, attempt_id: str) -> sqlite3.Row:
        connection = self._connect()
        try:
            row = connection.execute("SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
            if row is None:
                raise QueueNotReady(f"attempt is unknown: {attempt_id}")
            return row
        finally:
            connection.close()

    def _result_path(self, row: Mapping[str, Any], result_path: Optional[Path]) -> Path:
        artifact_dir = _absolute_path(str(row["artifact_dir"]), "attempt artifact directory")
        candidate = artifact_dir / "case_result.json" if result_path is None else Path(result_path).absolute()
        _reject_symlink_chain(candidate)
        if candidate.parent != artifact_dir or candidate.name != "case_result.json":
            raise ArtifactIntegrityError("case result must be the attempt-local case_result.json")
        return candidate

    @contextlib.contextmanager
    def _attempt_lock(self, attempt_id: str) -> Iterator[None]:
        lock_dir = self.queue_dir / "attempt-locks"
        lock_dir.mkdir(exist_ok=True)
        lock_path = _absolute_path(lock_dir / (_sha256_bytes(attempt_id.encode()) + ".lock"), "attempt lock")
        with lock_path.open("a+b") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def _completion_rows(self, lease_or_attempt: Any, token: str) -> Tuple[sqlite3.Row, sqlite3.Row]:
        with self._transaction() as connection:
            row = self._authorise_attempt(connection, lease_or_attempt, token)
            if row["status"] == ATTEMPT_REQUEUED:
                raise LeaseConflict("a requeued attempt cannot be finalized again")
            self._assert_runner_stopped(row)
            case_row = connection.execute("SELECT * FROM cases WHERE case_id = ?", (row["case_id"],)).fetchone()
            if case_row is None:
                raise QueueNotReady("claimed case is missing")
            return row, case_row

    def _prepare_result(
        self,
        row: Mapping[str, Any],
        case_row: Mapping[str, Any],
        *,
        result: Optional[Mapping[str, Any]],
        result_path: Optional[Path],
    ) -> Dict[str, Any]:
        target = self._result_path(row, result_path)
        if result is not None:
            if not isinstance(result, Mapping):
                raise ArtifactIntegrityError("result must be a JSON object")
            payload = (json.dumps(dict(result), indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
            if target.exists() or target.is_symlink():
                if target.is_symlink() or target.read_bytes() != payload:
                    raise ArtifactIntegrityError("supplied result differs from existing case_result.json")
            else:
                if row["result_sha256"] is not None or (target.parent / "artifact_manifest.json").exists():
                    raise ArtifactIntegrityError("cannot recreate a result inside sealed evidence")
                _atomic_bytes(target, payload, overwrite=False)
                _write_sidecar(target)
        digest = _verify_sidecar(target, label="case result")
        value = _read_json_object(target, label="case result")
        expected_key = str(case_row["resume_key"])
        if value.get("resume_key") != expected_key:
            raise ArtifactIntegrityError("case result resume_key does not match the claimed case")
        status = value.get("status")
        failure = value.get("failure")
        integrity = value.get("integrity")
        if not isinstance(status, str) or (failure is not None and not isinstance(failure, Mapping)):
            raise ArtifactIntegrityError("terminal status/failure metadata is malformed")
        if integrity is not None and not isinstance(integrity, Mapping):
            raise ArtifactIntegrityError("terminal integrity metadata must be an object")
        if isinstance(integrity, Mapping) and "status" in integrity and not isinstance(integrity["status"], str):
            raise ArtifactIntegrityError("terminal integrity status is malformed")
        for source in (value, failure or {}):
            if "classification" in source and not isinstance(source["classification"], str):
                raise ArtifactIntegrityError("failure classification is malformed")
            if "halt_matrix" in source and type(source["halt_matrix"]) is not bool:
                raise ArtifactIntegrityError("halt_matrix must be a boolean when supplied")
        if "capture_integrity" in value and type(value["capture_integrity"]) is not bool:
            raise ArtifactIntegrityError("capture integrity must be a boolean when supplied")
        halt = (
            value.get("capture_integrity") is False
            or value.get("halt_matrix") is True
            or value.get("classification") in CAPTURE_FAILURE_CLASSIFICATIONS
            or (isinstance(failure, Mapping) and (
                failure.get("halt_matrix") is True
                or failure.get("classification") in CAPTURE_FAILURE_CLASSIFICATIONS))
            or (isinstance(integrity, Mapping) and integrity.get("status") in {"fail", "failed"})
        )
        if halt:
            classification = "capture_integrity"
        elif status == "completed":
            if value.get("accepted") is False:
                raise ArtifactIntegrityError("completed result cannot set accepted=false")
            classification = "accepted"
        elif status in {"failed", "timeout", "unavailable"}:
            classification = "retryable_failure"
        else:
            raise ArtifactIntegrityError("case result has no supported terminal status")
        return {
            "path": target,
            "digest": digest,
            "value": value,
            "classification": classification,
        }

    def _artifact_records(self, artifact_dir: Path) -> List[Dict[str, Any]]:
        if artifact_dir.is_symlink() or not artifact_dir.is_dir():
            raise ArtifactIntegrityError(f"attempt artifact directory is unavailable: {artifact_dir}")
        records: List[Dict[str, Any]] = []
        errors: List[str] = []
        def walk_error(exc: OSError) -> None:
            errors.append(str(exc))

        for directory, dirs, files in os.walk(str(artifact_dir), topdown=True, followlinks=False, onerror=walk_error):
            parent = Path(directory)
            linked_dirs = [name for name in dirs if (parent / name).is_symlink()]
            for name in sorted(linked_dirs):
                candidate = parent / name
                records.append({"path": str(candidate.relative_to(artifact_dir)), "kind": "symlink", "target": os.readlink(candidate)})
            dirs[:] = sorted(name for name in dirs if name not in linked_dirs)
            for name in sorted(files):
                candidate = parent / name
                relative = str(candidate.relative_to(artifact_dir))
                if relative in {"artifact_manifest.json", "artifact_manifest.json.sha256"}:
                    continue
                try:
                    before = candidate.lstat()
                    if candidate.is_symlink():
                        records.append({"path": relative, "kind": "symlink", "target": os.readlink(candidate)})
                        continue
                    if not candidate.is_file():
                        errors.append(f"special file: {relative}")
                        continue
                    digest = _sha256_file(candidate)
                    with candidate.open("rb") as durable:
                        _fsync_artifact_descriptor(durable.fileno())
                    after = candidate.stat()
                    if (before.st_ino, before.st_size, before.st_mtime_ns) != (
                        after.st_ino,
                        after.st_size,
                        after.st_mtime_ns,
                    ):
                        errors.append(f"artifact changed while inventoried: {relative}")
                    records.append({"path": relative, "kind": "file", "sha256": digest, "size": after.st_size})
                except (OSError, QueueError) as exc:
                    errors.append(f"{relative}: {exc}")
            _fsync_directory(parent)
        if errors:
            raise ArtifactIntegrityError("; ".join(errors))
        return sorted(records, key=lambda item: item["path"])

    def _write_artifact_manifest(
        self,
        row: Mapping[str, Any],
        case_row: Mapping[str, Any],
        prepared: Mapping[str, Any],
    ) -> Tuple[Path, str]:
        artifact_dir = _absolute_path(str(row["artifact_dir"]), "attempt artifact directory")
        if row["case_id"] != case_row["case_id"] or row["case_sha256"] != case_row["case_sha256"]:
            raise ArtifactIntegrityError("attempt/case identity differs")
        if _sha256_bytes(bytes(case_row["case_bytes"])) != case_row["case_sha256"]:
            raise ArtifactIntegrityError("stored original case bytes differ")
        if _verify_sidecar(artifact_dir / "case_spec.json", label="staged case") != row["case_sha256"]:
            raise ArtifactIntegrityError("staged case differs from original bytes")
        self._token_unavailable(row)
        lease_value = _read_json_object(artifact_dir / "lease.json", label="lease")
        for key in ("case_id", "resume_key", "attempt_no", "case_sha256", "worker_id", "endpoint_id", "worker_binding_sha256"):
            expected = case_row["resume_key"] if key == "resume_key" else row[key]
            if lease_value.get(key) != expected:
                raise ArtifactIntegrityError(f"lease {key} binding differs")
        with self._transaction() as connection:
            self._resolve_audit_worker_binding(connection, row)
        records = self._artifact_records(artifact_dir)
        manifest_path = artifact_dir / "artifact_manifest.json"
        value = {
            "schema_version": ARTIFACT_MANIFEST_SCHEMA,
            "attempt_id": row["attempt_id"],
            "case_id": row["case_id"],
            "resume_key": case_row["resume_key"],
            "attempt_no": row["attempt_no"],
            "case_sha256": row["case_sha256"],
            "worker_id": row["worker_id"],
            "endpoint_id": row["endpoint_id"],
            "worker_binding_sha256": row["worker_binding_sha256"],
            "result_path": str(Path(prepared["path"]).relative_to(artifact_dir)),
            "result_sha256": prepared["digest"],
            "launch_state": row["launch_state"],
            "launch_command_sha256": row["launch_command_sha256"],
            "runner_exit": json.loads(row["runner_exit_json"]) if row["runner_exit_json"] else None,
            "artifacts": records,
            "captured_epoch_ns": _now_ns(),
        }
        if manifest_path.exists() or manifest_path.is_symlink():
            existing_digest = _verify_sidecar(manifest_path, label="artifact manifest")
            existing = _read_json_object(manifest_path, label="artifact manifest")
            captured = existing.get("captured_epoch_ns")
            if type(captured) is not int or captured <= 0:
                raise ArtifactIntegrityError("artifact manifest capture time is invalid")
            value["captured_epoch_ns"] = captured
            if _canonical(existing) != _canonical(value):
                raise ArtifactIntegrityError("artifact manifest already exists with different bytes")
            if row["artifact_manifest_sha256"] is not None and (
                row["artifact_manifest_sha256"] != existing_digest
                or row["artifact_manifest_path"] != str(manifest_path)
                or row["result_sha256"] != prepared["digest"]
                or row["result_path"] != str(prepared["path"])
            ):
                raise ArtifactIntegrityError("sealed evidence differs from SQLite completion binding")
            return manifest_path, existing_digest
        if row["artifact_manifest_sha256"] is not None:
            raise ArtifactIntegrityError("committed artifact manifest is missing")
        _atomic_json(manifest_path, value, overwrite=False)
        return manifest_path, _write_sidecar(manifest_path)

    def _block_attempt(self, lease_or_attempt: Any, reason: str, *, lease_token: Optional[str] = None) -> None:
        with self._transaction() as connection:
            row = self._authorise_attempt(connection, lease_or_attempt, lease_token)
            if row["result_sha256"] is not None or row["status"] in {ATTEMPT_ACCEPTED, ATTEMPT_REQUEUED}:
                # Preserve terminal ownership; an integrity incident never permits rerunning it.
                self._halt_locked(connection, reason, entity_id=row["attempt_id"])
                return
            connection.execute(
                "UPDATE attempts SET status = ?, ended_epoch_ns = ?, outcome_classification = ?, outcome_json = ? WHERE attempt_id = ?",
                (ATTEMPT_BLOCKED, _now_ns(), "capture_integrity", _canonical({"reason": reason}), row["attempt_id"]),
            )
            connection.execute("UPDATE cases SET status = ? WHERE case_id = ?", (CASE_BLOCKED, row["case_id"]))
            self._halt_locked(connection, reason, entity_id=row["attempt_id"])

    def _commit_prepared(
        self,
        lease_or_attempt: Any,
        prepared: Mapping[str, Any],
        manifest_path: Path,
        manifest_digest: str,
        *,
        lease_token: Optional[str],
        allow_orphan: bool,
        process_returncode: Optional[int] = None,
    ) -> Dict[str, Any]:
        classification = str(prepared["classification"])
        initial = self._attempt_row(self._lease_parts(lease_or_attempt, lease_token)[0])
        if initial["runner_exit_json"]:
            observed_returncode = json.loads(initial["runner_exit_json"])["returncode"]
            if process_returncode is not None and process_returncode != observed_returncode:
                raise ArtifactIntegrityError("supplied returncode differs from guardian exit proof")
            process_returncode = observed_returncode
        if process_returncode is not None and classification == "accepted" and process_returncode != 0:
            # A durable completed result must never be quality-retried because
            # its process exit disagrees. Preserve evidence and require review.
            classification = "capture_integrity"
            prepared = {
                **dict(prepared),
                "classification": classification,
                "value": {**dict(prepared["value"]), "queue_returncode": process_returncode},
            }
        with self._transaction() as connection:
            row = self._authorise_attempt(connection, lease_or_attempt, lease_token)
            self._assert_runner_stopped(row)
            if row["result_sha256"] is not None:
                if (row["result_sha256"] != prepared["digest"]
                    or row["artifact_manifest_sha256"] != manifest_digest
                    or row["outcome_classification"] != classification
                    or json.loads(row["outcome_json"]) != dict(prepared["value"])):
                    raise ArtifactIntegrityError("terminal completion replay differs from the committed outcome")
                states = {ATTEMPT_ACCEPTED: "accepted", ATTEMPT_RETRYABLE: "retryable", ATTEMPT_BLOCKED: "halted"}
                if row["status"] in states:
                    return {"status": states[row["status"]], "idempotent": True, "attempt_id": row["attempt_id"], "result_sha256": prepared["digest"]}
            if row["status"] == ATTEMPT_REQUEUED:
                raise LeaseConflict(f"attempt {row['attempt_id']} was already requeued")
            if row["status"] == ATTEMPT_ORPHANED and not allow_orphan:
                raise ReconciliationRequired(f"attempt {row['attempt_id']} must be explicitly reconciled")
            if row["status"] == ATTEMPT_ORPHANED and self._live_attempt_state(row)["state"] != "dead":
                raise ReconciliationRequired("orphan owner/child proof changed before completion")
            if row["status"] not in {ATTEMPT_ACTIVE, ATTEMPT_ORPHANED, ATTEMPT_RETRYABLE, ATTEMPT_BLOCKED}:
                raise LeaseConflict(f"attempt {row['attempt_id']} cannot be finalized from {row['status']}")
            outcome_json = dict(prepared["value"])
            now = _now_ns()
            if classification == "accepted":
                case = connection.execute("SELECT * FROM cases WHERE case_id = ?", (row["case_id"],)).fetchone()
                if case is None:
                    raise QueueNotReady(f"case disappeared for attempt {row['attempt_id']}")
                if case["status"] == CASE_ACCEPTED or case["accepted_attempt_id"] is not None:
                    if case["accepted_attempt_id"] == row["attempt_id"] and case["accepted_result_sha256"] == prepared["digest"]:
                        return {"status": "accepted", "idempotent": True, "attempt_id": row["attempt_id"], "result_sha256": prepared["digest"]}
                    raise LeaseConflict("case already has a different accepted completion")
                connection.execute(
                    "UPDATE attempts SET status = ?, ended_epoch_ns = ?, result_path = ?, result_sha256 = ?, artifact_manifest_path = ?, artifact_manifest_sha256 = ?, outcome_classification = ?, outcome_json = ? WHERE attempt_id = ?",
                    (ATTEMPT_ACCEPTED, now, str(prepared["path"]), prepared["digest"], str(manifest_path), manifest_digest, "accepted", _canonical(outcome_json), row["attempt_id"]),
                )
                connection.execute(
                    "UPDATE cases SET status = ?, accepted_attempt_id = ?, accepted_result_sha256 = ? WHERE case_id = ? AND accepted_attempt_id IS NULL AND status != ?",
                    (CASE_ACCEPTED, row["attempt_id"], prepared["digest"], row["case_id"], CASE_ACCEPTED),
                )
                if connection.execute("SELECT changes()").fetchone()[0] != 1:
                    raise LeaseConflict("case accepted-completion uniqueness constraint rejected the completion")
                self._event_locked(
                    connection,
                    "case_accepted",
                    row["attempt_id"],
                    {
                        "case_id": row["case_id"],
                        "worker_id": row["worker_id"],
                        "endpoint_id": row["endpoint_id"],
                        "result_sha256": prepared["digest"],
                        "official_resolved": (outcome_json.get("evaluator") or {}).get("official_resolved") if isinstance(outcome_json.get("evaluator"), Mapping) else None,
                    },
                )
                return {"status": "accepted", "idempotent": False, "attempt_id": row["attempt_id"], "result_sha256": prepared["digest"]}
            if classification == "capture_integrity":
                connection.execute(
                    "UPDATE attempts SET status = ?, ended_epoch_ns = ?, result_path = ?, result_sha256 = ?, artifact_manifest_path = ?, artifact_manifest_sha256 = ?, outcome_classification = ?, outcome_json = ? WHERE attempt_id = ?",
                    (ATTEMPT_BLOCKED, now, str(prepared["path"]), prepared["digest"], str(manifest_path), manifest_digest, classification, _canonical(outcome_json), row["attempt_id"]),
                )
                connection.execute("UPDATE cases SET status = ? WHERE case_id = ?", (CASE_BLOCKED, row["case_id"]))
                self._halt_locked(connection, "capture or evidence integrity failure: " + str(outcome_json.get("reason") or row["attempt_id"]), entity_id=row["attempt_id"])
                return {"status": "halted", "attempt_id": row["attempt_id"], "result_sha256": prepared["digest"]}
            connection.execute(
                "UPDATE attempts SET status = ?, ended_epoch_ns = ?, result_path = ?, result_sha256 = ?, artifact_manifest_path = ?, artifact_manifest_sha256 = ?, outcome_classification = ?, outcome_json = ? WHERE attempt_id = ?",
                (ATTEMPT_RETRYABLE, now, str(prepared["path"]), prepared["digest"], str(manifest_path), manifest_digest, "retryable_failure", _canonical(outcome_json), row["attempt_id"]),
            )
            connection.execute("UPDATE cases SET status = ? WHERE case_id = ?", (CASE_RETRY_WAITING, row["case_id"]))
            self._event_locked(
                connection,
                "attempt_retryable_failure",
                row["attempt_id"],
                {"case_id": row["case_id"], "reason": outcome_json.get("reason"), "result_sha256": prepared["digest"]},
            )
            return {"status": "retryable", "attempt_id": row["attempt_id"], "result_sha256": prepared["digest"]}

    @_serialized_attempt
    def finish_attempt(
        self,
        lease_or_attempt: Any,
        *,
        result: Optional[Mapping[str, Any]] = None,
        result_path: Optional[Path] = None,
        lease_token: Optional[str] = None,
        allow_orphan: bool = False,
        process_returncode: Optional[int] = None,
    ) -> Dict[str, Any]:
        attempt_id, token = self._lease_parts(lease_or_attempt, lease_token)
        row, case_row = self._completion_rows(lease_or_attempt, token)
        try:
            prepared = self._prepare_result(row, case_row, result=result, result_path=result_path)
            manifest_path, manifest_digest = self._write_artifact_manifest(row, case_row, prepared)
            return self._commit_prepared(
                lease_or_attempt, prepared, manifest_path, manifest_digest,
                lease_token=token, allow_orphan=allow_orphan,
                process_returncode=process_returncode,
            )
        except (ArtifactIntegrityError, OSError) as exc:
            self._block_attempt(lease_or_attempt, str(exc), lease_token=token)
            raise

    @staticmethod
    def _quality_retry_reason(reason: str, classification: str) -> bool:
        text = f"{reason} {classification}".lower()
        return any(token in text for token in (
            "quality", "unresolved", "official_resolved", "not resolved", "score retry", "accuracy retry",
        ))

    @_serialized_attempt
    def fail_attempt(
        self,
        lease_or_attempt: Any,
        *,
        reason: str,
        classification: str = "runner_failure",
        capture_integrity: bool = False,
        lease_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not isinstance(reason, str) or not reason.strip():
            raise QueueError("failure reason is required")
        if not isinstance(classification, str) or not classification.strip():
            raise QueueError("failure classification is required")
        capture_failure = bool(capture_integrity) or classification in CAPTURE_FAILURE_CLASSIFICATIONS
        attempt_id, token = self._lease_parts(lease_or_attempt, lease_token)
        row, case_row = self._completion_rows(lease_or_attempt, token)
        artifact_dir = _absolute_path(str(row["artifact_dir"]), "attempt artifact directory")
        failure_path = artifact_dir / "queue_failure.json"
        failure = {
            "schema_version": QUEUE_FAILURE_SCHEMA,
            "attempt_id": attempt_id,
            "case_id": row["case_id"],
            "resume_key": case_row["resume_key"],
            "status": "failed",
            "accepted": False,
            "reason": reason,
            "classification": classification,
            "capture_integrity": capture_failure,
            "failure": {
                "classification": "capture_integrity" if capture_failure else classification,
                "halt_matrix": capture_failure,
                "message": reason,
                "recorded_epoch_ns": _now_ns(),
            },
        }
        try:
            if failure_path.exists() or failure_path.is_symlink():
                _verify_sidecar(failure_path, label="queue failure")
                existing = _read_json_object(failure_path, label="queue failure")
                recorded = existing.get("failure", {}).get("recorded_epoch_ns")
                failure["failure"]["recorded_epoch_ns"] = recorded
                if type(recorded) is not int or recorded <= 0 or existing != failure:
                    raise ArtifactIntegrityError("queue failure replay differs from original evidence")
                failure = existing
            else:
                if row["result_sha256"] is not None or (artifact_dir / "artifact_manifest.json").exists():
                    raise ArtifactIntegrityError("cannot add failure evidence to a sealed completion")
                _atomic_json(failure_path, failure, overwrite=False)
                _write_sidecar(failure_path)
            prepared = {
                "path": failure_path,
                "digest": _verify_sidecar(failure_path, label="queue failure"),
                "value": failure,
                "classification": "capture_integrity" if capture_failure else "retryable_failure",
            }
            manifest_path, manifest_digest = self._write_artifact_manifest(row, case_row, prepared)
            return self._commit_prepared(
                lease_or_attempt, prepared, manifest_path, manifest_digest,
                lease_token=token, allow_orphan=False,
            )
        except (ArtifactIntegrityError, OSError) as exc:
            self._block_attempt(lease_or_attempt, str(exc), lease_token=token)
            raise

    @_serialized_attempt
    def retry_attempt(
        self, lease_or_attempt: Any, **kwargs: Any,
    ) -> Dict[str, Any]:
        return self._retry_attempt(lease_or_attempt, **kwargs)

    def _retry_attempt(
        self,
        lease_or_attempt: Any,
        *,
        reason: str,
        classification: str = "infrastructure_retry",
        lease_token: Optional[str] = None,
        review_note: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not isinstance(reason, str) or not reason.strip():
            raise QueueError("retry reason is required")
        if self._quality_retry_reason(reason, classification):
            raise QueueNotReady("quality or unresolved outcomes are accepted outcomes and cannot be retried")
        with self._transaction() as connection:
            row = self._authorise_attempt(connection, lease_or_attempt, lease_token)
            self._assert_runner_stopped(row)
            if row["status"] == ATTEMPT_ORPHANED and self._live_attempt_state(row)["state"] != "dead":
                raise ReconciliationRequired("orphan process proof is incomplete")
            if row["status"] == ATTEMPT_ACCEPTED:
                raise QueueNotReady("an accepted completion cannot be retried")
            if row["outcome_json"] and json.loads(row["outcome_json"]).get("status") == "completed":
                raise QueueNotReady("a durable completed outcome cannot be retried, including after an exit/capture disagreement")
            if row["status"] == ATTEMPT_ACTIVE:
                raise LeaseConflict("active attempt must finish or be reconciled before retry")
            if row["status"] == ATTEMPT_REQUEUED:
                return {"status": "requeued", "idempotent": True, "attempt_id": row["attempt_id"], "case_id": row["case_id"]}
            if row["status"] == ATTEMPT_BLOCKED:
                if not review_note or not review_note.strip():
                    raise QueueHalted("blocked/capture attempt requires a review note before requeue")
            if row["status"] not in {ATTEMPT_RETRYABLE, ATTEMPT_ORPHANED, ATTEMPT_BLOCKED}:
                raise LeaseConflict(f"attempt {row['attempt_id']} cannot be retried from {row['status']}")
            case = connection.execute("SELECT status FROM cases WHERE case_id = ?", (row["case_id"],)).fetchone()
            if case is None or case["status"] == CASE_ACCEPTED:
                raise LeaseConflict("case cannot be retried after acceptance")
            provenance = {
                "schema_version": "assignment.retry-provenance.v1",
                "source_attempt_id": row["attempt_id"],
                "reason": reason,
                "classification": classification,
                "review_note": review_note,
                "recorded_epoch_ns": _now_ns(),
                "actor_pid": os.getpid(),
                "prior_result_sha256": row["result_sha256"],
                "prior_artifact_manifest_sha256": row["artifact_manifest_sha256"],
            }
            connection.execute(
                "UPDATE attempts SET status = ?, retry_provenance_json = ?, ended_epoch_ns = COALESCE(ended_epoch_ns, ?) WHERE attempt_id = ?",
                (ATTEMPT_REQUEUED, _canonical(provenance), _now_ns(), row["attempt_id"]),
            )
            connection.execute("UPDATE cases SET status = ? WHERE case_id = ? AND status != ?", (CASE_PENDING, row["case_id"], CASE_ACCEPTED))
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise LeaseConflict("case was accepted concurrently and cannot be requeued")
            self._event_locked(connection, "case_requeued", row["attempt_id"], {**provenance, "case_id": row["case_id"]})
            return {"status": "requeued", "idempotent": False, "attempt_id": row["attempt_id"], "case_id": row["case_id"], "retry_provenance": provenance}

    def _live_attempt_state(self, row: Mapping[str, Any]) -> Dict[str, Any]:
        owner = _same_process_identity(row, "owner")
        runner = self._runner_execution_state(row)
        if owner is None or runner is None:
            state = "identity_unknown"
        elif owner or runner:
            state = "live"
        else:
            state = "dead"
        return {"attempt_id": row["attempt_id"], "owner": owner, "runner": runner, "state": state}

    def reconcile(self, *, attempt_id: Optional[str] = None, worker_id: Optional[str] = None) -> List[Dict[str, Any]]:
        connection = self._connect()
        try:
            query = "SELECT * FROM attempts WHERE status = 'active'"
            parameters: List[Any] = []
            clauses: List[str] = []
            if attempt_id is not None:
                clauses.append("attempt_id = ?")
                parameters.append(attempt_id)
            if worker_id is not None:
                canonical = _normalise_worker_id(worker_id, self._allowed_workers())
                clauses.append("worker_id = ?")
                parameters.append(canonical)
            if clauses:
                query += " AND " + " AND ".join(clauses)
            rows = connection.execute(query, parameters).fetchall()
        finally:
            connection.close()
        results: List[Dict[str, Any]] = []
        for row in rows:
            state = self._live_attempt_state(row)
            if state["state"] != "dead":
                results.append(state)
                continue
            with self._transaction() as tx:
                current = tx.execute("SELECT * FROM attempts WHERE attempt_id = ?", (row["attempt_id"],)).fetchone()
                if current is None or current["status"] != ATTEMPT_ACTIVE:
                    continue
                state = self._live_attempt_state(current)
                if state["state"] != "dead":
                    results.append(state)
                    continue
                tx.execute(
                    "UPDATE attempts SET status = ?, ended_epoch_ns = ?, outcome_classification = ? WHERE attempt_id = ?",
                    (ATTEMPT_ORPHANED, _now_ns(), "orphaned_unreconciled", row["attempt_id"]),
                )
                tx.execute("UPDATE cases SET status = ? WHERE case_id = ? AND status = ?", (CASE_ORPHANED, row["case_id"], CASE_RUNNING))
                self._event_locked(
                    tx,
                    "attempt_orphaned",
                    row["attempt_id"],
                    {"case_id": row["case_id"], "worker_id": row["worker_id"], "endpoint_id": row["endpoint_id"], "owner": state["owner"], "runner": state["runner"]},
                )
            results.append({**state, "state": "orphaned", "reassignment": "blocked_until_explicit_reconciliation"})
        return results

    @_serialized_attempt
    def reconcile_orphan(
        self,
        attempt_id: str,
        *,
        action: str = "inspect",
        lease_token: Optional[str] = None,
        review_note: Optional[str] = None,
    ) -> Dict[str, Any]:
        if action not in {"inspect", "accept", "retry"}:
            raise QueueError("orphan action must be inspect, accept, or retry")
        row = self._attempt_row(attempt_id)
        if row["status"] == ATTEMPT_REQUEUED and action == "retry":
            return self._retry_attempt(attempt_id, lease_token=lease_token or self._token_unavailable(row), reason="crash_recovery", classification="crash_recovery", review_note=review_note)
        if row["status"] != ATTEMPT_ORPHANED and not (row["status"] == ATTEMPT_ACCEPTED and action == "accept"):
            raise ReconciliationRequired(f"attempt {attempt_id} is not an orphaned attempt")
        live = self._live_attempt_state(row)
        if live["state"] != "dead":
            raise ReconciliationRequired(f"attempt {attempt_id} still has a live or unidentifiable owner/runner PID")
        artifact_dir = _absolute_path(str(row["artifact_dir"]), "attempt artifact directory")
        result_path = artifact_dir / "case_result.json"
        failure_path = artifact_dir / "queue_failure.json"
        if not result_path.exists() and not failure_path.exists():
            if action == "inspect":
                return {"status": "orphaned", "attempt_id": attempt_id, "durable_result": False, "action_required": "retry"}
            if action == "accept":
                raise ArtifactIntegrityError("orphaned attempt has no durable case result")
            self._record_orphan_failure(row, reason="supervisor or runner crashed before a durable result", review_note=review_note)
        try:
            connection = self._connect()
            try:
                case_row = connection.execute("SELECT * FROM cases WHERE case_id = ?", (row["case_id"],)).fetchone()
            finally:
                connection.close()
            assert case_row is not None
            if result_path.exists():
                prepared = self._prepare_result(row, case_row, result=None, result_path=result_path)
            else:
                digest = _verify_sidecar(failure_path, label="orphan failure receipt")
                failure = _read_json_object(failure_path, label="orphan failure receipt")
                if (failure.get("schema_version") != QUEUE_FAILURE_SCHEMA
                    or failure.get("attempt_id") != attempt_id or failure.get("resume_key") != case_row["resume_key"]):
                    raise ArtifactIntegrityError("orphan failure receipt identity differs")
                classification = "capture_integrity" if (failure.get("capture_integrity") is True or failure.get("failure", {}).get("halt_matrix") is True) else "retryable_failure"
                prepared = {"path": failure_path, "digest": digest, "value": failure, "classification": classification}
            manifest_path, manifest_digest = self._write_artifact_manifest(row, case_row, prepared)
        except QueueError as exc:
            self._block_attempt_with_token_hash(row, str(exc))
            raise
        if action == "inspect":
            return {
                "status": "orphaned",
                "attempt_id": attempt_id,
                "durable_result": True,
                "classification": prepared["classification"],
                "result_sha256": prepared["digest"],
            }
        if action == "accept":
            if prepared["classification"] != "accepted":
                raise QueueNotReady("orphaned result is not an accepted completion")
            return self._commit_prepared(
                row["attempt_id"], prepared, manifest_path, manifest_digest,
                lease_token=lease_token or self._token_unavailable(row), allow_orphan=True,
            )
        if prepared["classification"] == "accepted":
            raise QueueNotReady("ordinary completed/unresolved results are accepted and cannot be retried")
        token = lease_token or self._token_unavailable(row)
        result = self._commit_prepared(
            row["attempt_id"], prepared, manifest_path, manifest_digest,
            lease_token=token, allow_orphan=True,
        )
        if result["status"] == "halted":
            return result
        return self._retry_attempt(row["attempt_id"], reason="orphan_recovery", classification="crash_recovery", lease_token=token, review_note=review_note)

    def _token_unavailable(self, row: Mapping[str, Any]) -> str:
        artifact_dir = _absolute_path(str(row["artifact_dir"]), "attempt artifact directory")
        lease_path = artifact_dir / "lease.json"
        try:
            _verify_sidecar(lease_path, label="durable lease metadata")
            value = _read_json_object(lease_path, label="durable lease metadata")
        except QueueError as exc:
            raise ReconciliationRequired(
                "orphan lease token is unavailable; preserve the orphan until its original attempt is reconciled"
            ) from exc
        token = value.get("lease_token")
        if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
            raise ReconciliationRequired("durable lease metadata does not contain a valid lease token")
        if value.get("attempt_id") != row["attempt_id"] or value.get("lease_token_sha256") != _sha256_bytes(token.encode("ascii")):
            raise ArtifactIntegrityError("durable lease metadata does not match the orphaned attempt")
        if _sha256_bytes(token.encode("ascii")) != row["lease_token_sha256"]:
            raise ArtifactIntegrityError("durable lease token does not match the queue ownership hash")
        return token

    def _block_attempt_with_token_hash(self, row: Mapping[str, Any], reason: str) -> None:
        with self._transaction() as connection:
            current = connection.execute("SELECT * FROM attempts WHERE attempt_id = ?", (row["attempt_id"],)).fetchone()
            if current is None or current["status"] not in {ATTEMPT_ORPHANED, ATTEMPT_ACTIVE}:
                return
            connection.execute(
                "UPDATE attempts SET status = ?, ended_epoch_ns = ?, outcome_classification = ?, outcome_json = ? WHERE attempt_id = ?",
                (ATTEMPT_BLOCKED, _now_ns(), "capture_integrity", _canonical({"reason": reason}), row["attempt_id"]),
            )
            connection.execute("UPDATE cases SET status = ? WHERE case_id = ?", (CASE_BLOCKED, row["case_id"]))
            self._halt_locked(connection, f"orphaned attempt cannot be verified: {reason}", entity_id=row["attempt_id"])

    def _record_orphan_failure(self, row: Mapping[str, Any], *, reason: str, review_note: Optional[str]) -> None:
        artifact_dir = _absolute_path(str(row["artifact_dir"]), "attempt artifact directory")
        marker = artifact_dir / "queue_failure.json"
        _atomic_json(marker, {
            "schema_version": QUEUE_FAILURE_SCHEMA,
            "attempt_id": row["attempt_id"],
            "case_id": row["case_id"],
            "resume_key": row["case_id"],
            "status": "failed",
            "accepted": False,
            "reason": reason,
            "classification": "crash_recovery",
            "capture_integrity": False,
            "failure": {"classification": "crash_recovery", "halt_matrix": False},
            "review_note": review_note,
            "recorded_epoch_ns": _now_ns(),
        }, overwrite=False)
        _write_sidecar(marker)

    def clear_halt(self, *, review_note: str) -> Dict[str, Any]:
        if not isinstance(review_note, str) or not review_note.strip():
            raise QueueNotReady("clearing a dispatch halt requires a non-empty review note")
        with self._transaction() as connection:
            current = json.loads(connection.execute("SELECT value_json FROM meta WHERE key = 'dispatch_halted'").fetchone()[0])
            if not current:
                return {"status": "running", "idempotent": True}
            gate_status = self._meta_locked(connection, "fingerprint_gate_status", "missing_discovery")
            require_all = bool(self._meta_locked(connection, "require_all_workers", True))
            if gate_status != "effective_ready" and (require_all or gate_status != "test_unconfigured"):
                raise QueueHalted(
                    "dispatch cannot be cleared while the effective configured fingerprint gate is incomplete"
                )
            gate_error = self._fingerprint_gate_error_locked(connection)
            if gate_error is not None:
                raise QueueHalted(f"dispatch cannot be cleared: {gate_error}")
            storage_error = self._storage_gate_error_locked(connection)
            if storage_error is not None:
                raise QueueHalted(f"dispatch cannot be cleared: {storage_error}")
            connection.execute("INSERT OR REPLACE INTO meta(key, value_json) VALUES ('dispatch_halted', 'false')")
            connection.execute("INSERT OR REPLACE INTO meta(key, value_json) VALUES ('halt_cleared_review', ?)", (_canonical(review_note),))
            self._event_locked(connection, "dispatch_halt_cleared_after_review", None, {"review_note": review_note})
        return {"status": "running", "idempotent": False, "review_note": review_note}

    def status(self) -> Dict[str, Any]:
        connection = self._connect()
        try:
            case_rows = connection.execute("SELECT status, COUNT(*) AS count FROM cases GROUP BY status ORDER BY status").fetchall()
            worker_rows = connection.execute("SELECT * FROM workers ORDER BY worker_id").fetchall()
            observer_rows = connection.execute("SELECT * FROM observers ORDER BY observer_id").fetchall()
            active_rows = connection.execute("SELECT attempt_id, case_id, worker_id, endpoint_id, status, artifact_dir FROM attempts WHERE status IN ('active', 'orphaned') ORDER BY attempt_id").fetchall()
            count = int(connection.execute("SELECT COUNT(*) FROM cases").fetchone()[0])
        finally:
            connection.close()
        return {
            "schema_version": QUEUE_SCHEMA,
            "queue_dir": str(self.queue_dir),
            "plan_id": self.meta("plan_id"),
            "plan_sha256": self.meta("plan_sha256"),
            "case_count": count,
            "case_status_counts": {str(row["status"]): int(row["count"]) for row in case_rows},
            "dispatch_halted": bool(self.meta("dispatch_halted")),
            "halt_reason": self.meta("halt_reason") if self.meta("dispatch_halted") else None,
            "allowed_worker_ids": list(self._allowed_workers()),
            "registered_workers": [self._worker_public(row) for row in worker_rows],
            "registered_observers": [self._observer_public(row) for row in observer_rows],
            "active_or_orphaned_attempts": [dict(row) for row in active_rows],
            "require_all_workers": bool(self.meta("require_all_workers")),
            "fingerprint_gate_status": self.meta("fingerprint_gate_status"),
            "required_server_max_model_len": self.meta("required_server_max_model_len"),
            "fingerprint_discovery": self.meta("fingerprint_discovery"),
            "fingerprint_discovery_summary": self.meta("fingerprint_discovery_summary"),
            "fingerprint_effective": self.meta("fingerprint_effective"),
            "adapter_manifest": self.meta("adapter_manifest"),
            "storage_policy": self.meta("storage_policy"),
            "advisories": self.meta("advisories") if self._meta_exists("advisories") else {},
            "confirmation_import": self.meta("confirmation_import") if self._meta_exists("confirmation_import") else None,
            "binding_revision": self.meta("binding_revision") if self._meta_exists("binding_revision") else None,
            "binding_revision_receipt": self.meta("binding_revision_receipt") if self._meta_exists("binding_revision_receipt") else None,
        }

    def audit_coverage(self) -> Dict[str, Any]:
        connection = self._connect()
        try:
            connection.execute("BEGIN")  # one consistent ownership snapshot during concurrent completion
            cases = connection.execute("SELECT * FROM cases ORDER BY ordinal").fetchall()
            attempts = connection.execute("SELECT * FROM attempts ORDER BY case_id, attempt_no").fetchall()
            accepted_attempts = connection.execute("SELECT case_id, COUNT(*) AS count FROM attempts WHERE status = 'accepted' GROUP BY case_id").fetchall()
        finally:
            connection.close()
        duplicate_cases = [row["case_id"] for row in accepted_attempts if int(row["count"]) != 1]
        errors: List[str] = []
        integrity_errors: List[str] = []
        case_by_id = {case["case_id"]: case for case in cases}
        attempt_by_id = {attempt["attempt_id"]: attempt for attempt in attempts}
        for attempt in attempts:
            if attempt["result_sha256"] is None:
                continue  # active/orphaned attempts have not committed evidence yet
            try:
                with self._attempt_lock(attempt["attempt_id"]):
                    case = case_by_id.get(attempt["case_id"])
                    if case is None:
                        raise ArtifactIntegrityError("attempt refers to a missing case")
                    artifact_dir = _absolute_path(attempt["artifact_dir"], "attempt artifacts")
                    result_path = _absolute_path(attempt["result_path"], "sealed result")
                    if result_path not in {artifact_dir / "case_result.json", artifact_dir / "queue_failure.json"}:
                        raise ArtifactIntegrityError("result pointer escapes attempt-local evidence")
                    digest = _verify_sidecar(result_path, label="sealed result")
                    if digest != attempt["result_sha256"]:
                        raise ArtifactIntegrityError("result differs from committed hash")
                    value = _read_json_object(result_path, label="sealed result")
                    if value.get("resume_key") != case["resume_key"]:
                        raise ArtifactIntegrityError("result resume key differs")
                    expected_outcome = dict(value)
                    outcome = json.loads(attempt["outcome_json"])
                    if "queue_returncode" in outcome:
                        expected_outcome["queue_returncode"] = outcome["queue_returncode"]
                    if outcome != expected_outcome:
                        raise ArtifactIntegrityError("SQLite outcome differs from sealed result")
                    if not attempt["artifact_manifest_sha256"]:
                        raise ArtifactIntegrityError("committed completion has no manifest binding")
                    self._write_artifact_manifest(attempt, case, {"path": result_path, "digest": digest})
                    if attempt["status"] == ATTEMPT_ACCEPTED:
                        prepared = self._prepare_result(attempt, case, result=None, result_path=result_path)
                        if prepared["classification"] != "accepted":
                            raise ArtifactIntegrityError("accepted attempt has capture/failure evidence")
                        self._assert_runner_stopped(attempt)
            except (QueueError, OSError, ValueError, TypeError) as exc:
                integrity_errors.append(f"attempt {attempt['attempt_id']}: {exc}")
        accepted = 0
        for case in cases:
            if case["status"] != CASE_ACCEPTED:
                errors.append(f"case {case['case_id']} is {case['status']}")
                continue
            accepted += 1
            if case["accepted_attempt_id"] is None:
                integrity_errors.append(f"accepted case {case['case_id']} has no accepted attempt")
                continue
            attempt = attempt_by_id.get(case["accepted_attempt_id"])
            if attempt is None or attempt["status"] != ATTEMPT_ACCEPTED or attempt["case_id"] != case["case_id"]:
                integrity_errors.append(f"case {case['case_id']} accepted pointer is invalid")
                continue
            if attempt["result_sha256"] != case["accepted_result_sha256"] or not attempt["result_sha256"]:
                integrity_errors.append(f"accepted result binding differs for {case['case_id']}")
        for attempt in attempts:
            if attempt["status"] == ATTEMPT_ACCEPTED:
                case = case_by_id.get(attempt["case_id"])
                if case is None or case["accepted_attempt_id"] != attempt["attempt_id"] or case["status"] != CASE_ACCEPTED:
                    integrity_errors.append(f"accepted attempt {attempt['attempt_id']} has no matching case pointer")
        if integrity_errors or duplicate_cases:
            with self._transaction() as connection:
                self._halt_locked(connection, "evidence audit failed: " + "; ".join(integrity_errors + duplicate_cases), entity_id=None)
        errors.extend(integrity_errors)
        return {
            "schema_version": "assignment.queue-coverage-audit.v1",
            "status": "pass" if not errors and accepted == len(cases) and not duplicate_cases else "fail",
            "case_count": len(cases),
            "accepted_count": accepted,
            "duplicate_accepted_case_ids": duplicate_cases,
            "errors": errors,
        }


def _open_queue_snapshot(queue_dir: Path) -> sqlite3.Connection:
    """Open an immutable SQLite view without changing the source queue."""

    queue_dir = Path(queue_dir).absolute()
    _reject_symlink_chain(queue_dir, include_leaf=False)
    db_path = queue_dir / DB_NAME
    if db_path.is_symlink() or not db_path.is_file():
        raise QueueNotReady(f"source queue database is unavailable: {db_path}")
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 2:
        connection.close()
        raise QueueNotReady("source queue schema requires v2 launch proof")
    schema_row = connection.execute("SELECT value_json FROM meta WHERE key = 'schema_version'").fetchone()
    if schema_row is None or json.loads(schema_row[0]) != QUEUE_SCHEMA:
        connection.close()
        raise QueueNotReady("source queue schema is unsupported")
    return connection


def migrate_idle_queue(
    source_queue_dir: Path,
    destination_queue_dir: Path,
    worker_manifest: Path,
    *,
    migration_id: str,
    review_note: str,
    adapter_manifest_path: Optional[Path] = None,
    adapter_manifest_sha256: Optional[str] = None,
) -> Dict[str, Any]:
    """Snapshot a quiescent queue and apply one explicit worker revision."""

    revision_id = SharedCaseQueue._validate_revision_id(migration_id)
    if not isinstance(review_note, str) or not review_note.strip():
        raise QueueNotReady("review_note is required")
    source_dir = Path(source_queue_dir).absolute()
    destination_dir = Path(destination_queue_dir).absolute()
    _reject_symlink_chain(source_dir, include_leaf=False)
    _reject_symlink_chain(destination_dir, include_leaf=False)
    if destination_dir.exists():
        if destination_dir.is_symlink() or not destination_dir.is_dir() or any(destination_dir.iterdir()):
            raise QueueNotReady(f"destination queue directory must be new and empty: {destination_dir}")
    else:
        destination_dir.mkdir(parents=True, exist_ok=False)
    source = _open_queue_snapshot(source_dir)
    try:
        held = source.execute(
            "SELECT attempt_id, worker_id, status FROM attempts WHERE status IN ('active', 'orphaned') ORDER BY attempt_id"
        ).fetchall()
        if held:
            raise ReconciliationRequired("queue snapshot requires no active or orphaned attempts")
        if source.execute("SELECT 1 FROM meta WHERE key = 'binding_revision'").fetchone() is not None:
            raise QueueNotReady("source queue already contains a worker binding revision")
        source_state_sha = _preserved_queue_state_digest(source)
        source_counts = {
            "cases": int(source.execute("SELECT COUNT(*) FROM cases").fetchone()[0]),
            "attempts": int(source.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]),
            "accepted_cases": int(source.execute("SELECT COUNT(*) FROM cases WHERE status = 'accepted'").fetchone()[0]),
            "pending_cases": int(source.execute("SELECT COUNT(*) FROM cases WHERE status = 'pending'").fetchone()[0]),
            "blocked_cases": int(source.execute("SELECT COUNT(*) FROM cases WHERE status = 'blocked'").fetchone()[0]),
        }
        target = sqlite3.connect(str(destination_dir / DB_NAME), timeout=30.0)
        try:
            source.backup(target)
            target.commit()
        finally:
            target.close()
    finally:
        source.close()

    queue = SharedCaseQueue(destination_dir)
    revision = queue.revise_idle_worker_bindings(
        worker_manifest,
        migration_id=revision_id,
        source_queue_state_sha256=source_state_sha,
        review_note=review_note,
        adapter_manifest_path=adapter_manifest_path,
        adapter_manifest_sha256=adapter_manifest_sha256,
    )
    check = _open_queue_snapshot(destination_dir)
    try:
        destination_state_sha = _preserved_queue_state_digest(check)
        if destination_state_sha != source_state_sha:
            raise ArtifactIntegrityError("case/attempt/observer state changed during binding migration")
        destination_counts = {
            "cases": int(check.execute("SELECT COUNT(*) FROM cases").fetchone()[0]),
            "attempts": int(check.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]),
            "accepted_cases": int(check.execute("SELECT COUNT(*) FROM cases WHERE status = 'accepted'").fetchone()[0]),
            "pending_cases": int(check.execute("SELECT COUNT(*) FROM cases WHERE status = 'pending'").fetchone()[0]),
            "blocked_cases": int(check.execute("SELECT COUNT(*) FROM cases WHERE status = 'blocked'").fetchone()[0]),
        }
    finally:
        check.close()
    receipt = {
        "schema_version": MIGRATION_RECEIPT_SCHEMA,
        "migration_id": revision_id,
        "source_queue_dir": str(source_dir),
        "destination_queue_dir": str(destination_dir),
        "source_queue_state_sha256": source_state_sha,
        "destination_queue_state_sha256": destination_state_sha,
        "source_counts": source_counts,
        "destination_counts": destination_counts,
        "worker_revision": revision,
        "accepted_rows_preserved": True,
        "artifact_pointers_preserved": True,
        "created_epoch_ns": _now_ns(),
    }
    receipt_path = destination_dir / "binding-migration-receipt.json"
    _atomic_json(receipt_path, receipt, overwrite=False)
    receipt_sha = _write_sidecar(receipt_path)
    with queue._transaction() as connection:
        connection.execute(
            "INSERT INTO meta(key, value_json) VALUES ('binding_revision_receipt', ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
            (_canonical({"path": str(receipt_path), "sha256": receipt_sha}),),
        )
        queue._event_locked(
            connection,
            "worker_binding_revision_receipt_bound",
            None,
            {"path": str(receipt_path), "sha256": receipt_sha, "migration_id": revision_id},
        )
    return {**receipt, "receipt_path": str(receipt_path), "receipt_sha256": receipt_sha}


def _load_confirmation_case_validator(case_runner_path: Optional[Path] = None) -> Tuple[Any, Path, str]:
    """Import the existing runner's reviewed ``load_case`` function."""

    runner_path = _absolute_path(
        case_runner_path or Path(__file__).resolve().with_name("sweagent_case_runner.py"),
        "case runner",
    )
    if runner_path.is_symlink() or not runner_path.is_file():
        raise QueueNotReady(f"case runner is not a regular file: {runner_path}")
    runner_sha = _sha256_file(runner_path)
    module_name = f"assignment_confirmation_runner_{os.getpid()}_{uuid.uuid4().hex}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, str(runner_path))
        if spec is None or spec.loader is None:
            raise QueueNotReady(f"cannot load case runner adapter: {runner_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except QueueError:
        raise
    except Exception as exc:
        raise QueueNotReady(f"cannot import case runner adapter {runner_path}: {exc}") from exc
    load_case = getattr(module, "load_case", None)
    if not callable(load_case):
        raise QueueNotReady(f"case runner adapter has no callable load_case: {runner_path}")
    return load_case, runner_path, runner_sha


def _runner_command(
    *,
    runner: Path,
    lease: Lease,
    runtime: Mapping[str, Any],
    confirmation_plan: Optional[Path],
    confirmation_plan_sha256: Optional[str],
    cpu_docker: bool,
    extra_args: Sequence[str],
    adapter_args: Sequence[str] = (),
) -> Tuple[List[str], str]:
    runner = _absolute_path(str(runner), "case runner")
    if runner.is_symlink() or not runner.is_file() or not os.access(str(runner), os.X_OK):
        raise QueueNotReady(f"case runner must be an executable regular file: {runner}")
    case_schema = lease.case.get("schema_version")
    if case_schema not in KNOWN_CASE_SCHEMAS:
        raise QueueNotReady(
            f"case schema {case_schema!r} has no reviewed queue adapter; queue core is ready but execution requires an explicit adapter"
        )
    command = [
        str(runner),
        "--case-spec", str(lease.artifact_dir / "case_spec.json"),
        "--output-dir", str(lease.artifact_dir),
        "--runtime-manifest", str(runtime["path"]),
        "--execute",
    ]
    if case_schema == "assignment.configuration-confirmation-case.v2":
        if confirmation_plan is None or confirmation_plan_sha256 is None:
            raise QueueNotReady(
                "confirmation case execution requires the explicit confirmation-plan adapter binding; queue core will not synthesize it"
            )
        command.extend(["--confirmation-plan", str(confirmation_plan), "--confirmation-plan-sha256", confirmation_plan_sha256])
    if cpu_docker:
        command.append("--cpu-docker")
    command.extend(str(value) for value in extra_args)
    command.extend(str(value) for value in adapter_args)
    return command, _sha256_bytes((_canonical(command) + "\n").encode("utf-8"))


def _terminate_process_group(process: subprocess.Popen[Any]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.terminate()
        except OSError:
            pass
    deadline = time.monotonic() + 5.0
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.kill()
            except OSError:
                pass


def _launch_child(args: argparse.Namespace) -> int:
    """CPU-only guardian protocol; actual command comes from the durable intent.

    Linux subreaping keeps even double-forked/setsid local descendants under
    observation. If this process crashes, no exit receipt exists and recovery
    holds the endpoint instead of inferring that its descendants stopped.
    """
    queue = SharedCaseQueue(args.queue_dir)
    token = os.environ.get("ASSIGNMENT_CASE_OWNER", "")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise ReconciliationRequired("cannot establish Linux child reaping proof")
    queue.bind_runner_pid(args.attempt_id, runner_pid=os.getpid(), lease_token=token)
    row = queue._attempt_row(args.attempt_id)
    encoded = row["launch_command_json"]
    if _sha256_bytes(encoded.encode()) != row["launch_command_sha256"]:
        raise ArtifactIntegrityError("durable launch command hash differs")
    command = json.loads(encoded)
    command_record = _read_json_object(Path(row["artifact_dir"]) / "supervisor_command.json", label="supervisor command")
    if command_record.get("command") != command or _sha256_file(Path(command[0])) != command_record.get("runner_sha256"):
        raise ArtifactIntegrityError("case runner or command changed before child launch")
    process = subprocess.Popen(command)
    returncode = process.wait()
    while True:
        try:
            os.wait()  # reap adopted descendants, including detached sessions
        except ChildProcessError:
            break
    queue.record_runner_exit(args.attempt_id, lease_token=token, returncode=returncode)
    # Return success for the guardian; the actual runner status is DB-bound.
    return 0


def supervise(args: argparse.Namespace) -> int:
    queue = SharedCaseQueue(Path(args.queue_dir))
    if not args.execute or not args.acknowledge_paid_gpu_work:
        raise QueueNotReady("supervise requires --execute and --acknowledge-paid-gpu-work; no runner was started")
    worker_id = _normalise_worker_id(args.worker_id, queue._allowed_workers())
    runner = Path(args.runner or Path(__file__).resolve().with_name("sweagent_case_runner.py"))
    confirmation_plan: Optional[Path] = None
    confirmation_hash: Optional[str] = None
    if args.confirmation_plan:
        confirmation_plan = _absolute_path(args.confirmation_plan, "confirmation plan")
        confirmation_hash = _verify_sidecar(confirmation_plan, label="confirmation plan")
        if not args.confirmation_plan_sha256:
            raise QueueNotReady("--confirmation-plan-sha256 is required with --confirmation-plan")
        if _validate_sha(args.confirmation_plan_sha256, "confirmation_plan_sha256") != confirmation_hash:
            raise ArtifactIntegrityError("confirmation plan hash differs from its sidecar")
    confirmation_import = queue.meta("confirmation_import") if queue._meta_exists("confirmation_import") else None
    if isinstance(confirmation_import, Mapping):
        bound_plan = confirmation_import.get("confirmation_plan")
        if confirmation_plan is None or not isinstance(bound_plan, Mapping) or confirmation_hash != bound_plan.get("sha256"):
            raise QueueNotReady(
                "confirmation queue supervision requires its bound --confirmation-plan and exact --confirmation-plan-sha256"
            )
    adapter_path: Optional[Path] = None
    adapter_declared_sha: Optional[str] = None
    extra_argv_manifest = getattr(args, "extra_argv_manifest", None)
    extra_argv_manifest_sha256 = getattr(args, "extra_argv_manifest_sha256", None)
    if extra_argv_manifest:
        adapter_path = _absolute_path(extra_argv_manifest, "extra argv manifest")
        adapter_declared_sha = extra_argv_manifest_sha256
        bound = queue.meta("adapter_manifest")
        if isinstance(bound, Mapping) and (
            str(bound.get("path")) != str(adapter_path)
            or (adapter_declared_sha is not None and adapter_declared_sha != bound.get("sha256"))
        ):
            raise QueueNotReady("extra argv manifest differs from the queue's bound adapter manifest")
    else:
        bound = queue.meta("adapter_manifest")
        if isinstance(bound, Mapping):
            adapter_path = _absolute_path(bound.get("path"), "bound extra argv manifest")
            adapter_declared_sha = _validate_sha(bound.get("sha256"), "bound extra argv manifest.sha256")
    completed_cases = 0
    while True:
        if args.max_cases is not None and completed_cases >= args.max_cases:
            return 3
        queue.reconcile(worker_id=worker_id)
        lease = queue.claim_case(worker_id)
        if lease is None:
            snapshot = queue.status()
            if snapshot["dispatch_halted"]:
                raise QueueHalted(str(snapshot["halt_reason"]))
            counts = snapshot["case_status_counts"]
            if counts.get(CASE_ACCEPTED, 0) == snapshot["case_count"]:
                return 0
            # A retry_waiting/orphaned/blocked case is intentionally not
            # silently skipped.  The supervisor exits for an operator to
            # inspect/reconcile it rather than spinning or changing ownership.
            raise ReconciliationRequired("no claimable case remains; inspect queue status before resuming")
        adapter_binding = (
            _load_adapter_manifest(adapter_path, declared_sha256=adapter_declared_sha)
            if adapter_path is not None
            else None
        )
        worker = next(item for item in queue.status()["registered_workers"] if item["worker_id"] == lease.worker_id)
        command, command_hash = _runner_command(
            runner=runner,
            lease=lease,
            runtime=worker["runtime"],
            confirmation_plan=confirmation_plan,
            confirmation_plan_sha256=confirmation_hash,
            cpu_docker=bool(args.cpu_docker),
            extra_args=args.runner_arg or [],
            adapter_args=adapter_binding["argv"] if adapter_binding else (),
        )
        runner_sha256 = _sha256_file(runner)
        _atomic_json(lease.artifact_dir / "supervisor_command.json", {
            "schema_version": "assignment.supervisor-command.v1",
            "attempt_id": lease.attempt_id,
            "worker_id": lease.worker_id,
            "endpoint_id": lease.endpoint_id,
            "runner": str(runner),
            "runner_sha256": runner_sha256,
            "command": command,
            "command_sha256": command_hash,
            "runtime_manifest": worker["runtime"],
            "source": worker["source"],
            "inventory": worker["inventory"],
            "adapter_manifest": adapter_binding,
            "runner_args": list(args.runner_arg or []),
            "recorded_epoch_ns": _now_ns(),
        }, overwrite=False)
        stdout_path = lease.artifact_dir / "supervisor.stdout.log"
        stderr_path = lease.artifact_dir / "supervisor.stderr.log"
        env = os.environ.copy()
        env.update({
            "ASSIGNMENT_CASE_OWNER": lease.lease_token,
            "ASSIGNMENT_QUEUE_ATTEMPT_ID": lease.attempt_id,
            "ASSIGNMENT_QUEUE_WORKER_ID": lease.worker_id,
            "ASSIGNMENT_RUNTIME_MANIFEST": str(worker["runtime"]["path"]),
        })
        process: Optional[subprocess.Popen[Any]] = None
        timed_out = False
        if _sha256_file(runner) != runner_sha256:
            raise ArtifactIntegrityError("case runner changed after command binding")
        if adapter_binding is not None:
            fresh_adapter = _load_adapter_manifest(
                adapter_path,
                declared_sha256=adapter_binding["sha256"],
            )
            if fresh_adapter["argv"] != adapter_binding["argv"]:
                raise ArtifactIntegrityError("extra argv manifest changed after command binding")
        try:
            with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
                queue.begin_launch(lease, command)
                process = subprocess.Popen(
                    [sys.executable, str(Path(__file__).resolve()), "_launch-child",
                     "--queue-dir", str(queue.queue_dir), "--attempt-id", lease.attempt_id],
                    cwd=str(Path(args.cwd).absolute()) if args.cwd else None,
                    env=env,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=True,
                    text=False,
                )
                deadline_seconds = args.timeout_seconds
                if deadline_seconds is None:
                    value = lease.case.get("per_case_deadline_seconds")
                    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                        deadline_seconds = value
                started = time.monotonic()
                while process.poll() is None:
                    queue.heartbeat(lease)
                    if deadline_seconds is not None and time.monotonic() - started >= deadline_seconds:
                        timed_out = True
                        _terminate_process_group(process)
                        break
                    time.sleep(0.25)
                process.wait()
        except KeyboardInterrupt:
            # Leave the active lease durable.  The next supervisor must
            # reconcile the recorded PIDs before it can reuse the endpoint.
            raise
        except OSError as exc:
            if process is not None and process.poll() is None:
                _terminate_process_group(process)
            # Popen/IO failure after intent cannot prove whether any child ran.
            # Preserve the active attempt; never spin an unbounded launch retry.
            raise ReconciliationRequired(f"launch requires child/attempt reconciliation: {exc}") from exc
        row = queue._attempt_row(lease.attempt_id)
        queue._assert_runner_stopped(row)
        returncode = json.loads(row["runner_exit_json"])["returncode"]
        _atomic_json(lease.artifact_dir / "process_exit.json", {
            "schema_version": "assignment.supervisor-process-exit.v1",
            "attempt_id": lease.attempt_id,
            "pid": process.pid if process else None,
            "returncode": returncode,
            "timed_out": timed_out,
            "recorded_epoch_ns": _now_ns(),
        }, overwrite=False)
        result_path = lease.artifact_dir / "case_result.json"
        if result_path.exists():
            outcome = queue.finish_attempt(lease, result_path=result_path, process_returncode=returncode)
        else:
            outcome = queue.fail_attempt(
                lease,
                reason="case runner exited without a durable case_result.json" if not timed_out else "case runner deadline expired without a durable case_result.json",
                classification="timeout" if timed_out else "runner_exit_without_result",
                capture_integrity=not timed_out,
            )
        if outcome["status"] == "halted":
            raise QueueHalted("capture integrity failure halted dispatch")
        if outcome["status"] == "retryable":
            # Retry the failed infrastructure attempt immediately, retaining
            # its full provenance.  The bounded retry cap prevents an
            # endpoint failure from consuming the whole production run.
            connection = queue._connect()
            try:
                count = int(connection.execute("SELECT COUNT(*) FROM attempts WHERE case_id = ?", (lease.case_id,)).fetchone()[0])
            finally:
                connection.close()
            if count > int(args.max_retries):
                queue._block_attempt(lease, f"retry budget exhausted after {count - 1} retries", lease_token=lease.lease_token)
                raise QueueHalted("retry budget exhausted; case remains blocked for review")
            queue.retry_attempt(lease, reason="runner/infrastructure failure", classification="infrastructure_retry")
        else:
            completed_cases += 1
        if args.max_cases is not None and completed_cases >= args.max_cases:
            return 3
        # The loop returns directly to claim_case: no fixed shard, sleep, or
        # caller-owned reassignment window sits between completed cases.


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    child = sub.add_parser("_launch-child", help=argparse.SUPPRESS)
    child.add_argument("--queue-dir", required=True, type=Path)
    child.add_argument("--attempt-id", required=True)

    init = sub.add_parser("init", help="create a queue from an immutable plan")
    init.add_argument("--queue-dir", required=True, type=Path)
    init.add_argument("--plan", required=True, type=Path)
    init.add_argument("--plan-sha256-sidecar", type=Path)
    init.add_argument("--plan-sha256")
    init.add_argument("--artifact-root", type=Path)
    init.add_argument("--worker-pool", type=Path, help="optional hash-bound worker manifest to register after initialization")
    init.add_argument("--fingerprints", "--fingerprint-discovery", dest="fingerprint_path", type=Path)
    init.add_argument("--fingerprints-sha256", "--fingerprint-discovery-sha256", dest="fingerprint_sha256")
    init.add_argument("--effective-fingerprints", type=Path)
    init.add_argument("--effective-fingerprints-sha256")
    init.add_argument("--fingerprint-review-note")
    init.add_argument("--adapter-manifest", "--extra-argv-manifest", dest="adapter_manifest_path", type=Path)
    init.add_argument("--adapter-manifest-sha256", "--extra-argv-manifest-sha256", dest="adapter_manifest_sha256")
    init.add_argument("--expected-max-model-len", type=int, default=65536)
    init.add_argument("--allow-partial-workers", action="store_true")

    migrate = sub.add_parser("migrate-bindings", help="snapshot an idle queue and apply a reviewed worker binding revision")
    migrate.add_argument("--source-queue-dir", required=True, type=Path)
    migrate.add_argument("--queue-dir", required=True, type=Path)
    migrate.add_argument("--worker-manifest", required=True, type=Path)
    migrate.add_argument("--migration-id", required=True)
    migrate.add_argument("--review-note", required=True)
    migrate.add_argument("--adapter-manifest", type=Path)
    migrate.add_argument("--adapter-manifest-sha256")

    imported = sub.add_parser("import-confirmation", help="validate and import the exact 96-case confirmation inventory")
    imported.add_argument("--queue-dir", required=True, type=Path)
    imported.add_argument("--confirmation-plan", required=True, type=Path)
    imported.add_argument("--confirmation-plan-sha256", required=True)
    imported.add_argument("--runtime-manifest", required=True, type=Path)
    imported.add_argument("--fingerprints", "--fingerprint-discovery", dest="fingerprint_path", type=Path)
    imported.add_argument("--fingerprints-sha256", "--fingerprint-discovery-sha256", dest="fingerprint_sha256")
    imported.add_argument("--artifact-root", type=Path)
    imported.add_argument("--adapter-manifest", "--extra-argv-manifest", dest="adapter_manifest_path", type=Path)
    imported.add_argument("--adapter-manifest-sha256", "--extra-argv-manifest-sha256", dest="adapter_manifest_sha256")
    imported.add_argument("--expected-max-model-len", type=int, default=65536)
    imported.add_argument("--allow-partial-workers", action="store_true")

    effective = sub.add_parser("bind-effective-fingerprints", help="bind root's reviewed effective server configuration")
    effective.add_argument("--queue-dir", required=True, type=Path)
    effective.add_argument("--effective-fingerprints", required=True, type=Path)
    effective.add_argument("--effective-fingerprints-sha256")
    effective.add_argument("--review-note", required=True)
    effective.add_argument("--expected-max-model-len", type=int)

    storage = sub.add_parser("bind-storage-policy", help="bind measured storage reserves without clearing a halt")
    storage.add_argument("--queue-dir", required=True, type=Path)
    storage.add_argument("--storage-policy", required=True, type=Path)
    storage.add_argument("--storage-policy-sha256", required=True)
    storage.add_argument("--review-note", required=True)

    workers = sub.add_parser("register-workers", help="register endpoint and source bindings")
    workers.add_argument("--queue-dir", required=True, type=Path)
    workers.add_argument("--worker-manifest", required=True, type=Path)

    claim = sub.add_parser("claim", help="atomically claim the next deterministic case")
    claim.add_argument("--queue-dir", required=True, type=Path)
    claim.add_argument("--worker-id", required=True)

    bind = sub.add_parser("bind-pid", help="bind a launched runner PID to a lease")
    bind.add_argument("--queue-dir", required=True, type=Path)
    bind.add_argument("--attempt-id", required=True)
    bind.add_argument("--lease-token", required=True)
    bind.add_argument("--runner-pid", required=True, type=int)

    beat = sub.add_parser("heartbeat", help="record a lease heartbeat; leases never expire automatically")
    beat.add_argument("--queue-dir", required=True, type=Path)
    beat.add_argument("--attempt-id", required=True)
    beat.add_argument("--lease-token", required=True)

    finish = sub.add_parser("finish", help="durably classify a runner result")
    finish.add_argument("--queue-dir", required=True, type=Path)
    finish.add_argument("--attempt-id", required=True)
    finish.add_argument("--lease-token", required=True)
    finish.add_argument("--result", type=Path)
    finish.add_argument("--returncode", type=int)

    failure = sub.add_parser("fail", help="record a durable retryable or capture failure")
    failure.add_argument("--queue-dir", required=True, type=Path)
    failure.add_argument("--attempt-id", required=True)
    failure.add_argument("--lease-token", required=True)
    failure.add_argument("--reason", required=True)
    failure.add_argument("--classification", default="runner_failure")
    failure.add_argument("--capture-integrity", action="store_true")

    reconcile = sub.add_parser("reconcile", help="inspect or explicitly resolve orphaned attempts")
    reconcile.add_argument("--queue-dir", required=True, type=Path)
    reconcile.add_argument("--attempt-id")
    reconcile.add_argument("--worker-id")
    reconcile.add_argument("--action", choices=["inspect", "accept", "retry"], default="inspect")
    reconcile.add_argument("--lease-token")
    reconcile.add_argument("--review-note")

    clear = sub.add_parser("clear-halt", help="clear a dispatch halt after human review")
    clear.add_argument("--queue-dir", required=True, type=Path)
    clear.add_argument("--review-note", required=True)

    sub.add_parser("status", help="show durable queue state").add_argument("--queue-dir", required=True, type=Path)
    sub.add_parser("audit", help="prove exact accepted coverage and artifact stability").add_argument("--queue-dir", required=True, type=Path)

    run = sub.add_parser("supervise", help="run one persistent worker slot through the existing case runner")
    run.add_argument("--queue-dir", required=True, type=Path)
    run.add_argument("--worker-id", required=True)
    run.add_argument("--runner", type=Path)
    run.add_argument("--confirmation-plan", type=Path)
    run.add_argument("--confirmation-plan-sha256")
    run.add_argument("--cwd", type=Path)
    run.add_argument("--runner-arg", action="append")
    run.add_argument("--extra-argv-manifest", "--adapter-manifest", dest="extra_argv_manifest", type=Path)
    run.add_argument("--extra-argv-manifest-sha256", "--adapter-manifest-sha256", dest="extra_argv_manifest_sha256")
    run.add_argument("--cpu-docker", action="store_true")
    run.add_argument("--timeout-seconds", type=float)
    run.add_argument("--max-retries", type=int, default=3)
    run.add_argument("--max-cases", type=int)
    run.add_argument("--execute", action="store_true")
    run.add_argument("--acknowledge-paid-gpu-work", action="store_true")
    return parser


def _print(value: Any) -> None:
    print(json.dumps(value, sort_keys=True, ensure_ascii=False))


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "_launch-child":
            try:
                return _launch_child(args)
            except (QueueError, OSError) as exc:
                queue = SharedCaseQueue(args.queue_dir)
                with queue._transaction() as connection:
                    queue._halt_locked(connection, f"launch guardian failed: {exc}", entity_id=args.attempt_id)
                raise
        if args.command == "init":
            queue = SharedCaseQueue.create(
                args.queue_dir,
                plan_path=args.plan,
                plan_sha256=args.plan_sha256,
                plan_sha256_sidecar=args.plan_sha256_sidecar,
                artifact_root=args.artifact_root,
                require_all_workers=not args.allow_partial_workers,
                require_plan_sidecar=True,
                fingerprint_path=args.fingerprint_path,
                fingerprint_sha256=args.fingerprint_sha256,
                expected_max_model_len=args.expected_max_model_len,
                adapter_manifest_path=args.adapter_manifest_path,
                adapter_manifest_sha256=args.adapter_manifest_sha256,
            )
            if args.worker_pool:
                queue.register_workers_manifest(args.worker_pool)
            if args.effective_fingerprints:
                queue.bind_effective_fingerprints(
                    args.effective_fingerprints,
                    expected_max_model_len=args.expected_max_model_len,
                    declared_sha256=args.effective_fingerprints_sha256,
                    review_note=args.fingerprint_review_note,
                )
            _print(queue.status())
            return 0
        if args.command == "migrate-bindings":
            _print(
                migrate_idle_queue(
                    args.source_queue_dir,
                    args.queue_dir,
                    args.worker_manifest,
                    migration_id=args.migration_id,
                    review_note=args.review_note,
                    adapter_manifest_path=args.adapter_manifest,
                    adapter_manifest_sha256=args.adapter_manifest_sha256,
                )
            )
            return 0
        queue = SharedCaseQueue(args.queue_dir)
        if args.command == "import-confirmation":
            queue = SharedCaseQueue.import_confirmation(
                args.queue_dir,
                confirmation_plan=args.confirmation_plan,
                confirmation_plan_sha256=args.confirmation_plan_sha256,
                runtime_manifest=args.runtime_manifest,
                fingerprint_path=args.fingerprint_path,
                fingerprint_sha256=args.fingerprint_sha256,
                artifact_root=args.artifact_root,
                require_all_workers=not args.allow_partial_workers,
                adapter_manifest_path=args.adapter_manifest_path,
                adapter_manifest_sha256=args.adapter_manifest_sha256,
            )
            _print(queue.status())
            return 0
        if args.command == "bind-effective-fingerprints":
            _print(
                queue.bind_effective_fingerprints(
                    args.effective_fingerprints,
                    expected_max_model_len=args.expected_max_model_len,
                    declared_sha256=args.effective_fingerprints_sha256,
                    review_note=args.review_note,
                )
            )
            return 0
        if args.command == "bind-storage-policy":
            _print(queue.bind_storage_policy(args.storage_policy, declared_sha256=args.storage_policy_sha256, review_note=args.review_note))
            return 0
        if args.command == "register-workers":
            _print({"workers": queue.register_workers_manifest(args.worker_manifest)})
            return 0
        if args.command == "claim":
            lease = queue.claim_case(args.worker_id)
            _print({"status": "idle" if lease is None else "claimed", "lease": lease.to_dict() if lease else None})
            return 0
        if args.command == "bind-pid":
            _print(queue.bind_runner_pid(args.attempt_id, runner_pid=args.runner_pid, lease_token=args.lease_token))
            return 0
        if args.command == "heartbeat":
            queue.heartbeat(args.attempt_id, lease_token=args.lease_token)
            _print({"status": "heartbeat", "attempt_id": args.attempt_id})
            return 0
        if args.command == "finish":
            _print(queue.finish_attempt(args.attempt_id, result_path=args.result, lease_token=args.lease_token, process_returncode=args.returncode))
            return 0
        if args.command == "fail":
            _print(queue.fail_attempt(args.attempt_id, reason=args.reason, classification=args.classification, capture_integrity=args.capture_integrity, lease_token=args.lease_token))
            return 0
        if args.command == "reconcile":
            if args.attempt_id is not None and args.action != "inspect":
                _print(queue.reconcile_orphan(args.attempt_id, action=args.action, lease_token=args.lease_token, review_note=args.review_note))
            else:
                _print({"attempts": queue.reconcile(attempt_id=args.attempt_id, worker_id=args.worker_id)})
            return 0
        if args.command == "clear-halt":
            _print(queue.clear_halt(review_note=args.review_note))
            return 0
        if args.command == "status":
            _print(queue.status())
            return 0
        if args.command == "audit":
            value = queue.audit_coverage()
            _print(value)
            return 0 if value["status"] == "pass" else 2
        if args.command == "supervise":
            return supervise(args)
        raise QueueError(f"unsupported command: {args.command}")
    except (QueueError, OSError, ValueError, sqlite3.Error) as exc:
        print(f"NOT_READY: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ADAPTER_MANIFEST_SCHEMA",
    "ARTIFACT_MANIFEST_SCHEMA",
    "BINDING_HISTORY_SCHEMA",
    "BINDING_REVISION_SCHEMA",
    "CASE_ACCEPTED",
    "CASE_BLOCKED",
    "CASE_ORPHANED",
    "CASE_PENDING",
    "CASE_RETRY_WAITING",
    "CASE_RUNNING",
    "EXPIRED_WORKER_ID",
    "EFFECTIVE_FINGERPRINT_SCHEMA",
    "DISCOVERY_FINGERPRINT_SCHEMA",
    "KNOWN_CASE_SCHEMAS",
    "MIGRATION_RECEIPT_SCHEMA",
    "Lease",
    "LeaseConflict",
    "QueueError",
    "QueueHalted",
    "QueueNotReady",
    "READY_WORKER_IDS",
    "ReconciliationRequired",
    "SharedCaseQueue",
    "CAPTURE_FAILURE_CLASSIFICATIONS",
    "migrate_idle_queue",
    "main",
]
