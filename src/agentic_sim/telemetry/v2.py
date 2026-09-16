"""Append-only v2 telemetry for the actual SWE-agent execution boundaries.

The recorder is intentionally transport-neutral: the pinned SWE-agent hook
and the request proxy can write the same event shapes without importing one
another.  Every pre-execution action/request has a durable identity, followed
by a terminal record for success, failure, or timeout.  No event is rewritten
when a retry occurs.

Timing reconciliation is based on interval unions.  Nested or concurrent
spans are therefore counted once, while the complement of measured intervals
is emitted separately as ``unknown_residual``.  The outer wrapper is excluded
from useful coverage so a broad wrapper cannot manufacture attribution.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .clock import clock_fields, monotonic_ns, utc_now
from .features import (
    build_model_features,
    build_tool_features,
    canonical_json,
    canonical_sha256,
    model_vector,
    tool_model_vector,
)
from .hardware import MODEL_HARDWARE_FIELDS, model_hardware_features, raw_hardware_inventory
from .script_state import ScriptStateLedger
from .process_resources import capture_process_resources


TELEMETRY_SCHEMA = "assignment.telemetry.v2"
LIFECYCLE_SCHEMA = "assignment.telemetry.v2.lifecycle"
TOOL_SCHEMA = "assignment.telemetry.v2.tool"
MODEL_SCHEMA = "assignment.telemetry.v2.model"
HARDWARE_SCHEMA = "assignment.telemetry.v2.hardware"
MANIFEST_SCHEMA = "assignment.telemetry.v2.manifest"
TELEMETRY_VERSION = "telemetry-v2-20260908"
SCRIPT_ARTIFACTS_DIR = "script_artifacts"
REQUEST_PAYLOADS_DIR = "request_payloads"

TERMINAL_STATUSES = frozenset({"success", "failure", "timeout", "unavailable"})
ALL_STATUSES = TERMINAL_STATUSES | {"pending", "running", "incomplete"}
PROVENANCE = frozenset({"measured", "declared", "derived", "unavailable", "conditional_replay", "estimated"})

LIFECYCLE_PHASES = frozenset(
    {
        "outer_swe_agent",
        "setup",
        "startup",
        "client_processing",
        "model_client_call",
        "get_state",
        "state_query",
        "tool_execution",
        "model_request",
        "retry",
        "failure",
        "teardown",
        "generic_wrapper",
        "unknown_residual",
        "e2e_reconciliation",
    }
)
_NON_ATTRIBUTING_PHASES = frozenset({"outer_swe_agent", "generic_wrapper", "unknown_residual", "e2e_reconciliation"})
_ERROR_TEXT_LIMIT = 512
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")


class TelemetryContractError(ValueError):
    """A telemetry record would violate the v2 contract."""


class AppendOnlyWriter:
    """Small canonical JSONL writer that preserves measured token fields.

    The older artifact writer redacts every key containing ``token`` because
    it is also used for credentials.  v2 stores token *counts* by contract and
    never stores credential values, so this writer performs no broad key-name
    redaction and callers are responsible for keeping payloads allowlisted.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise TelemetryContractError(f"telemetry path must not be a symlink: {self.path}")
        self.path.touch(exist_ok=True)
        if self.path.is_symlink() or not self.path.is_file():
            raise TelemetryContractError(f"telemetry path must be a regular file: {self.path}")
        self._lock = threading.Lock()

    def append(self, value: Mapping[str, Any]) -> None:
        try:
            payload = (canonical_json(dict(value)) + "\n").encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise TelemetryContractError(f"telemetry record is not canonical JSON: {exc}") from exc
        with self._lock:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    pending = memoryview(payload)
                    while pending:
                        written = os.write(fd, pending)
                        if written <= 0:
                            raise OSError("telemetry append made no progress")
                        pending = pending[written:]
                    os.fsync(fd)
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    write = append

    @property
    def count(self) -> int:
        try:
            with self.path.open(encoding="utf-8") as handle:
                return sum(1 for line in handle if line.strip())
        except OSError as exc:
            raise TelemetryContractError(f"cannot count telemetry journal {self.path}: {exc}") from exc


def stable_id(prefix: str, *parts: Any) -> str:
    """Return an identity stable for the immutable inputs and retry lineage."""

    digest = hashlib.sha256((prefix + "\0" + canonical_json(parts)).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:32]}"


def _safe_error(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\x00", "")
    return text[:_ERROR_TEXT_LIMIT]


def _status(value: str, *, allow_pending: bool = False) -> str:
    allowed = ALL_STATUSES if allow_pending else TERMINAL_STATUSES
    if value not in allowed:
        raise TelemetryContractError(f"unsupported telemetry status: {value}")
    return value


def _interval(start_ns: Any, end_ns: Any) -> tuple[int, int]:
    if isinstance(start_ns, bool) or not isinstance(start_ns, int):
        raise TelemetryContractError("start_mono_ns must be an integer")
    if isinstance(end_ns, bool) or not isinstance(end_ns, int):
        raise TelemetryContractError("end_mono_ns must be an integer")
    if start_ns < 0 or end_ns < start_ns:
        raise TelemetryContractError("telemetry interval is negative or reversed")
    return start_ns, end_ns


def interval_union(intervals: Sequence[Mapping[str, Any] | Sequence[int]]) -> list[tuple[int, int]]:
    """Merge measured half-open monotonic intervals, rejecting bad inputs."""

    values: list[tuple[int, int]] = []
    for item in intervals:
        if isinstance(item, Mapping):
            if item.get("start_mono_ns") is None or item.get("end_mono_ns") is None:
                continue
            values.append(_interval(item["start_mono_ns"], item["end_mono_ns"]))
        else:
            if len(item) != 2:
                raise TelemetryContractError("interval tuples require start and end")
            values.append(_interval(item[0], item[1]))
    values.sort()
    merged: list[list[int]] = []
    for start, end in values:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        elif end > merged[-1][1]:
            merged[-1][1] = end
    return [(start, end) for start, end in merged]


def union_duration_ms(intervals: Sequence[Mapping[str, Any] | Sequence[int]]) -> float:
    return sum(end - start for start, end in interval_union(intervals)) / 1_000_000


@dataclass
class Span:
    recorder: "TelemetryV2"
    stream: str
    span_id: str
    event_kind: str
    phase: str
    start_mono_ns: int
    parent_event_id: str | None = None
    # ``identity`` also carries the immutable pre-execution attributes so the
    # terminal row can repeat them without reading a post-execution result.
    identity: dict[str, Any] = field(default_factory=dict)
    started_at_utc: str | None = None
    pre_event_id: str | None = None
    closed: bool = False

    @property
    def id(self) -> str:
        return self.span_id

    def finish(self, *, status: str = "success", end_mono_ns: int | None = None, **values: Any) -> dict[str, Any]:
        return self.recorder.finish_span(self, status=status, end_mono_ns=end_mono_ns, **values)


class TelemetryV2:
    """Record v2 lifecycle, tool, model, and hardware streams."""

    def __init__(
        self,
        output_dir: str | Path,
        *,
        run_id: str,
        attempt_id: str = "attempt-001",
        case_id: str | None = None,
        instance_id: str | None = None,
        hardware: Mapping[str, Any] | None = None,
        model_hardware: Mapping[str, Any] | None = None,
        model: str | None = None,
        model_revision: str | None = None,
        clock: Mapping[str, Any] | None = None,
        writer_role: str = "agent",
        hardware_profile_sha256: str | None = None,
        write_manifest: bool = True,
    ):
        if not isinstance(run_id, str) or not run_id:
            raise TelemetryContractError("run_id must be non-empty text")
        if not isinstance(attempt_id, str) or not attempt_id:
            raise TelemetryContractError("attempt_id must be non-empty text")
        if not isinstance(writer_role, str) or not writer_role.strip():
            raise TelemetryContractError("writer_role must be non-empty text")
        if hardware_profile_sha256 is not None and not _HEX64.fullmatch(str(hardware_profile_sha256)):
            raise TelemetryContractError("hardware_profile_sha256 must be a SHA-256 string or null")
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.attempt_id = attempt_id
        self.case_id = case_id
        self.instance_id = instance_id
        self.writer_role = writer_role.strip()
        self.hardware_profile_sha256 = (
            str(hardware_profile_sha256).lower() if hardware_profile_sha256 is not None else None
        )
        self.model = model
        self.model_revision = model_revision
        self.clock = dict(clock or clock_fields())
        self.hardware = raw_hardware_inventory(hardware)
        self._model_hardware_explicit = model_hardware is not None
        # A raw inventory can contain descriptive fields such as host thread
        # counts.  Only an explicit model projection crosses the feature
        # boundary; when omitted, conservatively retain known top-level terms
        # while dropping everything else.
        self.model_hardware = model_hardware_features(
            model_hardware if model_hardware is not None else self._project_hardware(hardware)
        )
        self.lifecycle_writer = AppendOnlyWriter(self.output_dir / "lifecycle_events.jsonl")
        self.tool_writer = AppendOnlyWriter(self.output_dir / "tool_events.jsonl")
        self.model_writer = AppendOnlyWriter(self.output_dir / "model_events.jsonl")
        self.hardware_writer = AppendOnlyWriter(self.output_dir / "hardware_snapshots.jsonl")
        self.lifecycle = self.lifecycle_writer
        self.tools = self.tool_writer
        self.models = self.model_writer
        self.hardware_snapshots = self.hardware_writer
        self._sequence = {
            "lifecycle": self.lifecycle_writer.count,
            "tool": self.tool_writer.count,
            "model": self.model_writer.count,
            "hardware": self.hardware_writer.count,
        }
        self._spans: dict[str, Span] = {}
        self._rows: list[dict[str, Any]] = []
        self._outer: Span | None = None
        self._outer_terminal: dict[str, Any] | None = None
        self._unknown_written = False
        self._artifact_lock = threading.Lock()
        if self.hardware or model_hardware:
            self.record_hardware_snapshot(
                self.hardware,
                model_hardware=self.model_hardware,
                provenance="measured",
                availability="measured",
            )
        if write_manifest:
            self._write_manifest()

    @staticmethod
    def _project_hardware(hardware: Mapping[str, Any] | None) -> dict[str, Any]:
        if not isinstance(hardware, Mapping):
            return {}
        projected = {key: value for key, value in hardware.items() if key in MODEL_HARDWARE_FIELDS}
        availability = projected.get("availability")
        if isinstance(availability, Mapping):
            projected["availability"] = {
                key: value for key, value in availability.items() if key in MODEL_HARDWARE_FIELDS
            }
        return projected

    def _write_manifest(self) -> None:
        manifest = {
            "schema_version": MANIFEST_SCHEMA,
            "telemetry_schema": TELEMETRY_SCHEMA,
            "instrumentation_version": TELEMETRY_VERSION,
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "case_id": self.case_id,
            "instance_id": self.instance_id,
            "writer_role": self.writer_role,
            "hardware_profile_sha256": self.hardware_profile_sha256,
            "model": self.model,
            "model_revision": self.model_revision,
            "clock": dict(self.clock),
            "streams": {
                "lifecycle": "lifecycle_events.jsonl",
                "tool": "tool_events.jsonl",
                "model": "model_events.jsonl",
                "hardware": "hardware_snapshots.jsonl",
            },
            "prospective_feature_schema": "assignment.d9-feature.v2",
            "prospective_feature_builder": "d9-feature-builder.v2.action-boundary-20260908",
            "hardware_model_fields": sorted(self.model_hardware.keys()),
            "hardware_model_policy": {
                "numeric_terms": [
                    "cpu_frequency_hz",
                    "gpu_memory_bandwidth_bytes_per_s",
                    "gpu_compute_tflops",
                ],
                "clock_assumption": "numeric terms are interpreted under the recorded host/boot/clock metadata; no thread or I/O scaling",
                "storage_policy": "no storage term until measured work volume is paired with bandwidth",
                "fitted_model": False,
            },
            "raw_hardware_inventory": self.hardware,
            "append_only": True,
            "request_payload_persisted": True,
            "request_payload_policy": {
                "body_bytes_are_exact": True,
                "response_bytes_are_exact_when_complete": True,
                "credential_headers_persisted": False,
                "headers_persisted": False,
            },
            "script_artifact_directory": SCRIPT_ARTIFACTS_DIR,
            "request_payload_directory": REQUEST_PAYLOADS_DIR,
            "process_resource_policy": {
                "schema_version": "assignment.process-resource-snapshot.v1",
                "field": "process_resources_at_record",
                "source": "resource.getrusage(RUSAGE_SELF)",
                "sample_time": "independent native-clock bracket at journal emission",
                "scope": "recording process including threads; not container/child work",
                "prospective_model_input": False,
            },
            "provenance": "measured",
        }
        path = self.output_dir / "telemetry_manifest.json"
        encoded = (canonical_json(manifest) + "\n").encode("utf-8")
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise TelemetryContractError("telemetry manifest exists but is not valid JSON") from exc
            immutable_keys = (
                "schema_version",
                "telemetry_schema",
                "instrumentation_version",
                "run_id",
                "attempt_id",
                "case_id",
                "instance_id",
                "hardware_profile_sha256",
                "streams",
                "prospective_feature_schema",
                "prospective_feature_builder",
                "append_only",
                "request_payload_persisted",
                "request_payload_policy",
                "script_artifact_directory",
                "request_payload_directory",
                "process_resource_policy",
            )
            if any(existing.get(key) != manifest.get(key) for key in immutable_keys):
                raise TelemetryContractError("telemetry manifest exists with different immutable metadata")
            return
        temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
        temporary.write_bytes(encoded)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    def _store_bytes(self, relative_path: str, payload: bytes) -> str:
        """Persist one exact, content-addressed artifact beneath this recorder.

        Artifact names are generated by the recorder and are never accepted as
        caller-provided paths.  If two writer processes race to retain the
        same artifact, the loser verifies the existing bytes rather than
        replacing them.  A conflicting existing artifact is a contract error.
        """

        if not isinstance(payload, bytes):
            raise TelemetryContractError("artifact payload must be bytes")
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise TelemetryContractError("artifact path must be a relative recorder path")
        path = self.output_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise TelemetryContractError(f"artifact path must not be a symlink: {path}")
        with self._artifact_lock:
            if path.exists():
                if not path.is_file() or path.is_symlink() or path.read_bytes() != payload:
                    raise TelemetryContractError(f"artifact path already contains different bytes: {path}")
                return relative.as_posix()
            temporary = path.with_name(path.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
            if temporary.exists() or temporary.is_symlink():
                raise TelemetryContractError(f"temporary artifact path already exists: {temporary}")
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                try:
                    temporary.unlink()
                except OSError:
                    pass
                raise
            try:
                # ``os.replace`` would silently overwrite a file created by a
                # concurrent writer.  A hard-link create is atomic and fails
                # when the target already exists, so the existing bytes can be
                # verified before discarding our temporary inode.
                os.link(temporary, path)
            except FileExistsError:
                if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
                    raise TelemetryContractError(f"artifact race produced different bytes: {path}")
            finally:
                try:
                    temporary.unlink()
                except OSError:
                    pass
            # The request archive is a pre-dispatch durability boundary.  A
            # file fsync alone does not make the newly linked directory entry
            # durable across a host crash, so sync the containing directory
            # before returning to the proxy.
            if hasattr(os, "O_DIRECTORY"):
                try:
                    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                except OSError as exc:
                    raise TelemetryContractError(
                        f"artifact directory durability could not be established: {path.parent}"
                    ) from exc
        return relative.as_posix()

    def record_script_artifact(
        self,
        *,
        container_path: str,
        content: str | bytes,
        encoding: str = "utf-8",
        generation: int,
        truncated: bool = False,
        hash_basis: str | None = None,
        byte_exact: bool | None = None,
    ) -> dict[str, Any]:
        """Retain a bounded native-container script source representation.

        The artifact is separate from prospective feature vectors.  The journal
        receives only its relative path, byte hash, declared encoding, byte
        length, truncation flag, and the ledger generation.  ``hash_basis``
        records whether the bytes are exact source bytes or a decoded text
        representation returned by a native API.  Callers must mark oversized
        reads unavailable instead of passing a fabricated prefix as complete
        source.  The bound applies to retained bytes; the pinned native text
        API does not expose a server-side read limit.
        """

        if not isinstance(container_path, str) or not container_path.strip():
            raise TelemetryContractError("script artifact container_path must be non-empty text")
        if isinstance(content, str):
            try:
                payload = content.encode(encoding)
            except (LookupError, UnicodeEncodeError) as exc:
                raise TelemetryContractError(f"script artifact encoding failed: {exc}") from exc
        elif isinstance(content, bytes):
            payload = content
        else:
            raise TelemetryContractError("script artifact content must be text or bytes")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise TelemetryContractError("script artifact generation must be a non-negative integer")
        if not isinstance(truncated, bool):
            raise TelemetryContractError("script artifact truncated must be boolean")
        if hash_basis is not None and (not isinstance(hash_basis, str) or not hash_basis.strip()):
            raise TelemetryContractError("script artifact hash_basis must be non-empty text or null")
        if byte_exact is not None and not isinstance(byte_exact, bool):
            raise TelemetryContractError("script artifact byte_exact must be boolean or null")
        if hash_basis is None:
            hash_basis = "exact_bytes" if isinstance(content, bytes) else "encoded_text"
        if byte_exact is None:
            byte_exact = isinstance(content, bytes)
        digest = hashlib.sha256(payload).hexdigest()
        relative = f"{SCRIPT_ARTIFACTS_DIR}/{digest}.source"
        artifact_path = self._store_bytes(relative, payload)
        return {
            "artifact_path": artifact_path,
            "sha256": digest,
            "encoding": encoding,
            "size_bytes": len(payload),
            "truncated": truncated,
            "hash_basis": hash_basis,
            "byte_exact": byte_exact,
            "generation": generation,
            "container_path": container_path,
        }

    def record_request_payload(
        self,
        *,
        physical_request_id: str,
        request_body: bytes,
        response_body: bytes | None = None,
        request_complete: bool = True,
        response_complete: bool | None = None,
    ) -> dict[str, Any]:
        """Persist exact request/response bytes for one physical attempt.

        Authentication and transport headers are intentionally excluded.  The
        body bytes are retained verbatim, including partial bytes on a failed
        request; completeness is explicit so a later analysis cannot treat a
        partial response as a full serving observation.  The request may be
        archived before dispatch by omitting ``response_body``.  A later call
        with the same physical identity appends the response artifact while
        verifying that the request bytes are unchanged.  This split is
        required so an interrupted process still leaves durable pre-dispatch
        request evidence.
        """

        if not isinstance(physical_request_id, str) or not physical_request_id.strip():
            raise TelemetryContractError("physical_request_id must be non-empty text")
        if not isinstance(request_body, bytes):
            raise TelemetryContractError("request payload must be bytes")
        if response_body is not None and not isinstance(response_body, bytes):
            raise TelemetryContractError("response payload must be bytes or null")
        if response_complete is None:
            response_complete = response_body is not None
        if not isinstance(request_complete, bool) or not isinstance(response_complete, bool):
            raise TelemetryContractError("payload completeness flags must be boolean")
        if response_body is None and response_complete:
            raise TelemetryContractError("a missing response body cannot be complete")
        key = hashlib.sha256(physical_request_id.encode("utf-8")).hexdigest()[:32]
        request_digest = hashlib.sha256(request_body).hexdigest()
        request_path = self._store_bytes(f"{REQUEST_PAYLOADS_DIR}/{key}.request.bin", request_body)
        response_path: str | None = None
        response_digest: str | None = None
        response_bytes: int | None = None
        if response_body is not None:
            response_digest = hashlib.sha256(response_body).hexdigest()
            response_bytes = len(response_body)
            response_path = self._store_bytes(
                f"{REQUEST_PAYLOADS_DIR}/{key}.response.bin", response_body
            )
        return {
            "physical_request_id": physical_request_id,
            "request": {
                "artifact_path": request_path,
                "sha256": request_digest,
                "bytes": len(request_body),
                "complete": request_complete,
            },
            "response": {
                "artifact_path": response_path,
                "sha256": response_digest,
                "bytes": response_bytes,
                "complete": response_complete,
            },
            "headers_persisted": False,
            "credential_headers_persisted": False,
            "encoding": "binary",
        }

    def _event_base(
        self,
        *,
        stream: str,
        event_kind: str,
        phase: str,
        status: str,
        start_mono_ns: int | None,
        end_mono_ns: int | None,
        span_id: str,
        parent_event_id: str | None,
        provenance: str,
        availability: str,
        terminal: bool,
        error_type: str | None = None,
        error_message: str | None = None,
        **values: Any,
    ) -> dict[str, Any]:
        if phase not in LIFECYCLE_PHASES:
            raise TelemetryContractError(f"unsupported lifecycle phase: {phase}")
        _status(status, allow_pending=not terminal)
        if provenance not in PROVENANCE:
            raise TelemetryContractError(f"unsupported provenance: {provenance}")
        if availability not in PROVENANCE:
            raise TelemetryContractError(f"unsupported availability: {availability}")
        if start_mono_ns is not None and end_mono_ns is not None:
            start_mono_ns, end_mono_ns = _interval(start_mono_ns, end_mono_ns)
            duration_ms: float | None = (end_mono_ns - start_mono_ns) / 1_000_000
        else:
            duration_ms = None
        sequence = self._sequence[stream]
        self._sequence[stream] += 1
        event_id = stable_id("event", self.run_id, self.attempt_id, self.writer_role, stream, sequence, span_id, event_kind)
        return {
            "schema_version": {"lifecycle": LIFECYCLE_SCHEMA, "tool": TOOL_SCHEMA, "model": MODEL_SCHEMA, "hardware": HARDWARE_SCHEMA}[stream],
            "event_id": event_id,
            "span_id": span_id,
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "case_id": self.case_id,
            "instance_id": self.instance_id,
            "writer_role": self.writer_role,
            "hardware_profile_sha256": self.hardware_profile_sha256,
            "sequence": sequence,
            "event_kind": event_kind,
            "phase": phase,
            "parent_event_id": parent_event_id,
            "start_mono_ns": start_mono_ns,
            "end_mono_ns": end_mono_ns,
            "duration_ms": duration_ms,
            "status": status,
            "terminal": terminal,
            "error_type": _safe_error(error_type),
            "error_message": _safe_error(error_message),
            "provenance": provenance,
            "availability": availability,
            "clock": dict(self.clock),
            "started_at_utc": utc_now() if start_mono_ns is not None else None,
            "ended_at_utc": utc_now() if end_mono_ns is not None else None,
            "utc_recorded": utc_now(),
            **values,
        }

    def _append(self, stream: str, row: Mapping[str, Any]) -> dict[str, Any]:
        value = dict(row)
        # Keep these independently timed, post-observation counters outside
        # every prospective feature envelope and vector.
        value["process_resources_at_record"] = capture_process_resources()
        writer = {"lifecycle": self.lifecycle_writer, "tool": self.tool_writer, "model": self.model_writer, "hardware": self.hardware_writer}[stream]
        writer.append(value)
        self._rows.append(value)
        return value

    def _begin(
        self,
        *,
        stream: str,
        event_kind: str,
        phase: str,
        start_mono_ns: int | None,
        parent_event_id: str | None = None,
        identity: Mapping[str, Any] | None = None,
        pre_values: Mapping[str, Any] | None = None,
    ) -> Span:
        start = monotonic_ns() if start_mono_ns is None else start_mono_ns
        if isinstance(start, bool) or not isinstance(start, int) or start < 0:
            raise TelemetryContractError("span start must be a non-negative monotonic integer")
        sequence = self._sequence[stream]
        identity_map = dict(identity or {})
        span_id = str(
            identity_map.pop(
                "span_id",
                stable_id(
                    "span",
                    self.run_id,
                    self.attempt_id,
                    self.writer_role,
                    stream,
                    event_kind,
                    sequence,
                    identity_map,
                ),
            )
        )
        if span_id in self._spans and not self._spans[span_id].closed:
            raise TelemetryContractError(f"duplicate open span identity: {span_id}")
        start_values = {**identity_map, **dict(pre_values or {})}
        started_at_utc = utc_now()
        row = self._event_base(
            stream=stream,
            event_kind=f"{event_kind}_start",
            phase=phase,
            status="pending",
            start_mono_ns=start,
            end_mono_ns=None,
            span_id=span_id,
            parent_event_id=parent_event_id,
            provenance="measured",
            availability="measured",
            terminal=False,
            started_at_utc=started_at_utc,
            **start_values,
        )
        self._append(stream, row)
        span = Span(
            self,
            stream,
            span_id,
            event_kind,
            phase,
            start,
            parent_event_id,
            start_values,
            started_at_utc,
            row["event_id"],
        )
        self._spans[span_id] = span
        return span

    def finish_span(
        self,
        span: Span,
        *,
        status: str = "success",
        end_mono_ns: int | None = None,
        provenance: str = "measured",
        availability: str = "measured",
        error_type: str | None = None,
        error_message: str | None = None,
        **values: Any,
    ) -> dict[str, Any]:
        if span.recorder is not self or span.closed:
            raise TelemetryContractError("span is already closed or belongs to another recorder")
        end = monotonic_ns() if end_mono_ns is None else end_mono_ns
        _interval(span.start_mono_ns, end)
        row = self._event_base(
            stream=span.stream,
            event_kind=span.event_kind,
            phase=span.phase,
            status=status,
            start_mono_ns=span.start_mono_ns,
            end_mono_ns=end,
            span_id=span.span_id,
            parent_event_id=span.parent_event_id,
            provenance=provenance,
            availability=availability,
            terminal=True,
            error_type=error_type,
            error_message=error_message,
            started_at_utc=span.started_at_utc,
            **{**span.identity, **values},
        )
        span.closed = True
        self._append(span.stream, row)
        if status in {"failure", "timeout"} and span.phase != "failure":
            # Keep failure handling visible as its own lifecycle category.
            # The instant marker contributes no duration, so it cannot inflate
            # interval-union coverage or turn a wrapper into attribution.
            self._append(
                "lifecycle",
                self._event_base(
                    stream="lifecycle",
                    event_kind="failure",
                    phase="failure",
                    status=status,
                    start_mono_ns=end,
                    end_mono_ns=end,
                    span_id=stable_id("failure", self.run_id, self.attempt_id, self.writer_role, span.span_id, end),
                    parent_event_id=row.get("event_id"),
                    provenance="measured",
                    availability="measured",
                    terminal=True,
                    error_type=error_type,
                    error_message=error_message,
                    failed_event_id=row.get("event_id"),
                    failed_phase=span.phase,
                ),
            )
        return row

    def start_phase(
        self,
        phase: str,
        *,
        start_mono_ns: int | None = None,
        parent_event_id: str | None = None,
        phase_id: str | None = None,
        event_kind: str | None = None,
        reason: str | None = None,
    ) -> Span:
        if phase not in LIFECYCLE_PHASES:
            raise TelemetryContractError(f"unsupported lifecycle phase: {phase}")
        kind = event_kind or phase
        return self._begin(
            stream="lifecycle",
            event_kind=kind,
            phase=phase,
            start_mono_ns=start_mono_ns,
            parent_event_id=parent_event_id,
            identity={"span_id": phase_id} if phase_id else {},
            pre_values={"reason": reason},
        )

    begin_phase = start_phase

    def begin_runtime_command(
        self, command: str, *, phase: str,
        parent_event_id: str | None = None,
        start_mono_ns: int | None = None,
        reason: str | None = None,
    ) -> Span:
        """Persist one dispatched auxiliary command before its BPF action starts.

        Setup/state phases can contain many physical shell commands. Each needs
        its own immutable command identity and start/terminal pair; a surrounding
        lifecycle span is not a substitute for those independent boundaries.
        Normal tool callbacks already supply their own physical action identity.
        """
        # Pinned SWE-agent resets an empty tool-reset list by dispatching an
        # empty BashAction. Retain that physical call and its exact bytes;
        # missing/non-text input is different from a measured shell no-op.
        if not isinstance(command, str) or "\x00" in command:
            raise TelemetryContractError("runtime command must be text without NUL")
        if phase not in LIFECYCLE_PHASES:
            raise TelemetryContractError(f"unsupported lifecycle phase: {phase}")
        return self._begin(
            stream="lifecycle", event_kind="runtime_command", phase=phase,
            start_mono_ns=start_mono_ns, parent_event_id=parent_event_id,
            identity={
                "runtime_command": command,
                "runtime_command_sha256": hashlib.sha256(command.encode("utf-8")).hexdigest(),
                "cpu_action_required": True,
            },
            pre_values={"reason": reason},
        )

    @contextlib.contextmanager
    def phase(self, phase: str, **kwargs: Any) -> Iterator[Span]:
        span = self.start_phase(phase, **kwargs)
        try:
            yield span
        except TimeoutError as exc:
            self.finish_span(span, status="timeout", error_type=type(exc).__name__, error_message=str(exc))
            raise
        except BaseException as exc:
            self.finish_span(span, status="failure", error_type=type(exc).__name__, error_message=str(exc))
            raise
        else:
            self.finish_span(span, status="success")

    def start_outer(self, *, start_mono_ns: int | None = None, parent_event_id: str | None = None) -> Span:
        if self._outer is not None and not self._outer.closed:
            raise TelemetryContractError("outer SWE-agent span already open")
        self._outer = self.start_phase("outer_swe_agent", start_mono_ns=start_mono_ns, parent_event_id=parent_event_id, event_kind="outer_swe_agent")
        return self._outer

    begin_outer = start_outer

    def finish_outer(
        self,
        *,
        status: str = "success",
        end_mono_ns: int | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        if self._outer is None:
            raise TelemetryContractError("outer SWE-agent span has not started")
        row = self.finish_span(self._outer, status=status, end_mono_ns=end_mono_ns, error_type=error_type, error_message=error_message)
        self._outer_terminal = row
        return row

    end_outer = finish_outer

    def begin_tool(
        self,
        action: str,
        *,
        action_id: str | None = None,
        logical_operation_id: str | None = None,
        retry_index: int = 0,
        retry_of: str | None = None,
        step_id: int | str | None = None,
        parent_event_id: str | None = None,
        start_mono_ns: int | None = None,
        script_state: Mapping[str, Any] | None = None,
        script_path: str | None = None,
        script_revision: str | None = None,
        actual_action: str | None = None,
    ) -> Span:
        if isinstance(retry_index, bool) or not isinstance(retry_index, int) or retry_index < 0:
            raise TelemetryContractError("tool retry_index must be a non-negative integer")
        features = build_tool_features(action, script_state=script_state, action_id=action_id)
        operation_id = logical_operation_id or stable_id("operation", self.run_id, self.case_id, step_id, features["action_sha256"])
        physical_id = action_id or stable_id("action", self.run_id, self.attempt_id, operation_id, retry_index)
        if features.get("action_id") != physical_id:
            features = build_tool_features(action, script_state=script_state, action_id=physical_id)
        actual_features = None
        if actual_action is not None:
            if not isinstance(actual_action, str) or not actual_action.strip():
                raise TelemetryContractError("actual_action must be non-empty text or null")
            actual_features = build_tool_features(
                actual_action,
                script_state=script_state,
                action_id=physical_id,
            )
        values = {
            "logical_operation_id": operation_id,
            "action_id": physical_id,
            "retry_index": retry_index,
            "retry_of": retry_of,
            "step_id": step_id,
            "action": action,
            "action_sha256": features["action_sha256"],
            "features": features,
            "feature_vector": tool_model_vector(features),
            "feature_vector_sha256": canonical_sha256(tool_model_vector(features)),
            "feature_sha256": features["feature_sha256"],
            "executable": features["tool_name"],
            "tool_name": features["tool_name"],
            "subcommand": features["subcommand"] or None,
            "module": features["module"],
            "command_prefix": features["command_prefix"],
            "operation_class": features["operation_class"],
            "declared_command_bytes": features["declared_command_bytes"],
            "declared_path_count": features["declared_path_count"],
            "path_scope": features["path_scope"],
            "test_runner": features["test_runner"],
            "test_scope": features["test_scope"],
            "traversal_mode": features["traversal_mode"],
            "find_exec": features["find_exec"],
            "find_exec_kind": features["find_exec_kind"],
            "find_exec_child": features["find_exec_child"],
            "pipeline": features["pipeline"],
            "has_pipe": features["pipeline"]["has_pipeline"],
            "has_glob": features["operation_class"] == "glob" or "*" in action or "?" in action,
            "script_path": script_path,
            "script_revision": script_revision,
            "actual_action": actual_action,
            "actual_action_sha256": (
                actual_features["action_sha256"] if actual_features is not None else None
            ),
            "actual_features": actual_features,
            # Work volumes are populated only from an explicit runtime child
            # probe at terminalization.  Keeping nulls in the pre-row makes
            # the measurement boundary and availability unambiguous.
            "bytes_read": None,
            "bytes_written": None,
            "files_touched": None,
            "subprocess_count": None,
            "measurement_availability": {
                "bytes_read": "unavailable",
                "bytes_written": "unavailable",
                "files_touched": "unavailable",
                "subprocess_count": "unavailable",
                "script_revision": "measured" if script_revision else "unavailable",
            },
        }
        return self._begin(
            stream="tool",
            event_kind="tool_event",
            phase="tool_execution",
            start_mono_ns=start_mono_ns,
            parent_event_id=parent_event_id,
            identity={"logical_operation_id": operation_id, "action_id": physical_id, "retry_index": retry_index, "retry_of": retry_of, "step_id": step_id},
            pre_values=values,
        )

    start_tool = begin_tool

    def record_tool_intent(
        self,
        action: str,
        *,
        action_id: str | None = None,
        logical_operation_id: str | None = None,
        retry_index: int = 0,
        retry_of: str | None = None,
        step_id: int | str | None = None,
        parent_event_id: str | None = None,
        start_mono_ns: int | None = None,
        script_state: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist a generated action before policy filters or execution.

        SWE-agent emits ``on_actions_generated`` before it checks blocklists
        and before ``on_action_started``.  This declared intent row preserves
        that exact action even when no execution occurs; a later tool span is
        emitted only from ``on_action_started``.
        """

        if isinstance(retry_index, bool) or not isinstance(retry_index, int) or retry_index < 0:
            raise TelemetryContractError("tool retry_index must be a non-negative integer")
        features = build_tool_features(action, script_state=script_state, action_id=action_id)
        logical = logical_operation_id or stable_id(
            "operation", self.run_id, self.case_id, step_id, features["action_sha256"]
        )
        physical = action_id or stable_id("action", self.run_id, self.attempt_id, logical, retry_index)
        if features.get("action_id") != physical:
            features = build_tool_features(action, script_state=script_state, action_id=physical)
        start = monotonic_ns() if start_mono_ns is None else start_mono_ns
        _interval(start, start)
        vector = tool_model_vector(features)
        return self._append(
            "tool",
            self._event_base(
                stream="tool",
                event_kind="tool_intent",
                phase="tool_execution",
                status="unavailable",
                start_mono_ns=start,
                end_mono_ns=start,
                span_id=stable_id("intent", self.run_id, self.attempt_id, self.writer_role, physical),
                parent_event_id=parent_event_id,
                provenance="declared",
                availability="declared",
                terminal=True,
                logical_operation_id=logical,
                action_id=physical,
                retry_index=retry_index,
                retry_of=retry_of,
                step_id=step_id,
                action=action,
                action_sha256=features["action_sha256"],
                features=features,
                feature_vector=vector,
                feature_vector_sha256=canonical_sha256(vector),
                feature_sha256=features["feature_sha256"],
                executable=features["tool_name"],
                tool_name=features["tool_name"],
                subcommand=features["subcommand"],
                module=features["module"],
                command_prefix=features["command_prefix"],
                operation_class=features["operation_class"],
                path_scope=features["path_scope"],
                test_runner=features["test_runner"],
                test_scope=features["test_scope"],
                traversal_mode=features["traversal_mode"],
                find_exec=features["find_exec"],
                find_exec_kind=features["find_exec_kind"],
                find_exec_child=features["find_exec_child"],
                pipeline=features["pipeline"],
                has_pipe=features["has_pipe"],
                has_glob=features["has_glob"],
                script_state=features["script_state"],
                intent_only=True,
                execution_observed=False,
                measurement_availability={
                    "bytes_read": "unavailable",
                    "bytes_written": "unavailable",
                    "files_touched": "unavailable",
                    "subprocess_count": "unavailable",
                },
            ),
        )

    def end_tool(
        self,
        span: Span,
        *,
        status: str = "success",
        end_mono_ns: int | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
        bytes_read: int | None = None,
        bytes_written: int | None = None,
        files_touched: int | None = None,
        subprocess_count: int | None = None,
        runtime_child_telemetry: bool = False,
        **values: Any,
    ) -> dict[str, Any]:
        measured = {"bytes_read": bytes_read, "bytes_written": bytes_written, "files_touched": files_touched, "subprocess_count": subprocess_count}
        for name, value in measured.items():
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise TelemetryContractError(f"{name} must be a non-negative measured integer or null")
        availability = {name: "measured" if value is not None else "unavailable" for name, value in measured.items()}
        return self.finish_span(
            span,
            status=status,
            end_mono_ns=end_mono_ns,
            error_type=error_type,
            error_message=error_message,
            bytes_read=bytes_read,
            bytes_written=bytes_written,
            files_touched=files_touched,
            subprocess_count=subprocess_count,
            runtime_child_telemetry=bool(runtime_child_telemetry),
            measurement_availability={**availability, "runtime_child_telemetry": "measured" if runtime_child_telemetry else "unavailable"},
            **values,
        )

    finish_tool = end_tool

    def record_tool(
        self,
        action: str,
        *,
        status: str = "success",
        start_mono_ns: int | None = None,
        end_mono_ns: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        span = self.begin_tool(action, start_mono_ns=start_mono_ns, **{key: value for key, value in kwargs.items() if key in {"action_id", "logical_operation_id", "retry_index", "retry_of", "step_id", "parent_event_id", "script_state", "script_path", "script_revision", "actual_action"}})
        final = {key: value for key, value in kwargs.items() if key not in {"action_id", "logical_operation_id", "retry_index", "retry_of", "step_id", "parent_event_id", "script_state", "script_path", "script_revision", "actual_action"}}
        return self.end_tool(span, status=status, end_mono_ns=end_mono_ns, **final)

    def begin_request(
        self,
        request: Mapping[str, Any] | None = None,
        *,
        logical_request_id: str | None = None,
        physical_request_id: str | None = None,
        request_id: str | None = None,
        retry_index: int = 0,
        retry_of: str | None = None,
        step_id: int | str | None = None,
        parent_event_id: str | None = None,
        start_mono_ns: int | None = None,
        model: str | None = None,
        model_revision: str | None = None,
        hardware: Mapping[str, Any] | None = None,
        phase: str = "model_request",
        event_kind: str = "model_request",
    ) -> Span:
        if isinstance(retry_index, bool) or not isinstance(retry_index, int) or retry_index < 0:
            raise TelemetryContractError("request retry_index must be a non-negative integer")
        body = dict(request or {})
        prospective = build_model_features(body, hardware=hardware if hardware is not None else self.model_hardware)
        digest = prospective.get("feature_sha256")
        logical = logical_request_id or stable_id("logical-request", self.run_id, self.case_id, step_id, digest)
        physical = physical_request_id or request_id or stable_id("request", self.run_id, self.attempt_id, logical, retry_index)
        values = {
            "logical_request_id": logical,
            "physical_request_id": physical,
            "request_id": physical,
            "retry_index": retry_index,
            "retry_of": retry_of,
            "step_id": step_id,
            "model": model if model is not None else self.model,
            "model_revision": model_revision if model_revision is not None else self.model_revision,
            "feature_mode": "prospective",
            "features": prospective,
            "feature_vector": model_vector(prospective),
            "feature_vector_sha256": canonical_sha256(model_vector(prospective)),
            "feature_sha256": prospective["feature_sha256"],
            "input_tokens": prospective.get("input_tokens"),
            "context_tokens": prospective.get("context_tokens"),
            "context_tokens_provenance": (
                "measured" if prospective.get("context_tokens") is not None else "unavailable"
            ),
            "max_output_tokens": prospective.get("max_output_tokens"),
            "request_body_sha256": prospective.get("request_sha256"),
            "output_tokens": None,
            "queue_ms": None,
            "prefill_ms": None,
            "decode_ms": None,
            "timing_availability": {"queue_ms": "unavailable", "prefill_ms": "unavailable", "decode_ms": "unavailable"},
            "gpu_hardware": model_hardware_features(hardware if hardware is not None else self.model_hardware),
        }
        return self._begin(
            stream="model",
            event_kind=event_kind,
            phase=phase,
            start_mono_ns=start_mono_ns,
            parent_event_id=parent_event_id,
            identity={"logical_request_id": logical, "physical_request_id": physical, "request_id": physical, "retry_index": retry_index, "retry_of": retry_of, "step_id": step_id},
            pre_values=values,
        )

    start_request = begin_request
    begin_model_request = begin_request

    @staticmethod
    def _usage(response: Mapping[str, Any] | None) -> dict[str, int | None]:
        usage = response.get("usage") if isinstance(response, Mapping) else None
        if not isinstance(usage, Mapping):
            return {
                "input_tokens": None,
                "output_tokens": None,
                "total_tokens": None,
                "cached_tokens": None,
            }
        def integer(*names: str) -> int | None:
            for name in names:
                value = usage.get(name)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    return value
            return None
        details = usage.get("prompt_tokens_details")
        cached_tokens = (
            details.get("cached_tokens")
            if isinstance(details, Mapping)
            else None
        )
        if not isinstance(cached_tokens, int) or isinstance(cached_tokens, bool) or cached_tokens < 0:
            cached_tokens = None
        return {
            "input_tokens": integer("prompt_tokens", "input_tokens"),
            "output_tokens": integer("completion_tokens", "output_tokens"),
            "total_tokens": integer("total_tokens"),
            # Keep null when vLLM's prompt-token-details field is omitted or
            # null.  The exact response artifact remains the authoritative
            # source for cache accounting; this terminal projection is a
            # request-identity join convenience, never an inferred zero.
            "cached_tokens": cached_tokens,
        }

    def end_request(
        self,
        span: Span,
        *,
        status: str = "success",
        end_mono_ns: int | None = None,
        response: Mapping[str, Any] | None = None,
        output_tokens: int | None = None,
        input_tokens: int | None = None,
        cached_tokens: int | None = None,
        context_tokens: int | None = None,
        queue_ms: float | None = None,
        prefill_ms: float | None = None,
        decode_ms: float | None = None,
        serving_timings_reliable: bool = False,
        response_sha256: str | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
        **values: Any,
    ) -> dict[str, Any]:
        usage = self._usage(response)
        if input_tokens is None:
            input_tokens = usage["input_tokens"]
        if output_tokens is None:
            output_tokens = usage["output_tokens"]
        if cached_tokens is None:
            cached_tokens = usage["cached_tokens"]
        context_tokens_provenance = "measured"
        if context_tokens is None and input_tokens is not None:
            # vLLM/OpenAI usage exposes prompt_tokens at response finalization.
            # Preserve the prospective feature object (which remains null until
            # a tokenizer measured the request before dispatch), while exposing
            # this terminal alias explicitly for retrospective request joins.
            context_tokens = input_tokens
            context_tokens_provenance = "derived_alias_of_prompt_tokens"
        elif context_tokens is None:
            context_tokens_provenance = "unavailable"
        for name, value in (("input_tokens", input_tokens), ("output_tokens", output_tokens),
                            ("cached_tokens", cached_tokens), ("context_tokens", context_tokens)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise TelemetryContractError(f"{name} must be a non-negative integer or null")
        timings = {"queue_ms": queue_ms, "prefill_ms": prefill_ms, "decode_ms": decode_ms}
        for name, value in timings.items():
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0):
                raise TelemetryContractError(f"{name} must be non-negative or null")
            if value is not None and not serving_timings_reliable:
                raise TelemetryContractError(f"{name} requires reliable serving-stack evidence")
        timing_availability = {name: "measured" if value is not None else "unavailable" for name, value in timings.items()}
        if response_sha256 is not None and not _HEX64.fullmatch(response_sha256):
            raise TelemetryContractError("response_sha256 must be a SHA-256 string")
        if status != "success" and response is not None and output_tokens is None:
            output_tokens = None
        return self.finish_span(
            span,
            status=status,
            end_mono_ns=end_mono_ns,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            context_tokens=context_tokens,
            context_tokens_provenance=context_tokens_provenance,
            queue_ms=queue_ms,
            prefill_ms=prefill_ms,
            decode_ms=decode_ms,
            timing_availability=timing_availability,
            response_sha256=response_sha256,
            serving_timings_reliable=bool(serving_timings_reliable),
            error_type=error_type,
            error_message=error_message,
            **values,
        )

    finish_request = end_request
    end_model_request = end_request

    def record_model_request(
        self,
        request: Mapping[str, Any] | None = None,
        *,
        status: str = "success",
        start_mono_ns: int | None = None,
        end_mono_ns: int | None = None,
        response: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        identity_keys = {"logical_request_id", "physical_request_id", "request_id", "retry_index", "retry_of", "step_id", "parent_event_id", "model", "model_revision", "hardware"}
        begin_values = {key: value for key, value in kwargs.items() if key in identity_keys}
        span = self.begin_request(request, start_mono_ns=start_mono_ns, **begin_values)
        end_values = {key: value for key, value in kwargs.items() if key not in identity_keys}
        return self.end_request(span, status=status, end_mono_ns=end_mono_ns, response=response, **end_values)

    def record_hardware_snapshot(
        self,
        hardware: Mapping[str, Any] | None = None,
        *,
        model_hardware: Mapping[str, Any] | None = None,
        timestamp_mono_ns: int | None = None,
        provenance: str = "measured",
        availability: str = "measured",
        snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        timestamp = monotonic_ns() if timestamp_mono_ns is None else timestamp_mono_ns
        if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
            raise TelemetryContractError("hardware timestamp must be a non-negative monotonic integer")
        values = raw_hardware_inventory(hardware if hardware is not None else self.hardware)
        model_values = model_hardware_features(
            model_hardware
            if model_hardware is not None
            else self._project_hardware(hardware if hardware is not None else self.hardware)
        )
        identity = snapshot_id or stable_id("hardware", self.run_id, self.attempt_id, timestamp, canonical_sha256(values))
        row = self._event_base(
            stream="hardware",
            event_kind="hardware_snapshot",
            phase="startup",
            status="success",
            start_mono_ns=timestamp,
            end_mono_ns=timestamp,
            span_id=identity,
            parent_event_id=None,
            provenance=provenance,
            availability=availability,
            terminal=True,
            snapshot_id=identity,
            timestamp_mono_ns=timestamp,
            model_hardware=model_values,
            raw_hardware=values,
        )
        return self._append("hardware", row)

    def _read_journal_rows(self) -> list[dict[str, Any]]:
        """Read and validate all v2 journals in the shared output directory.

        The runner and request proxy intentionally use separate recorder
        instances.  Reconciliation therefore rereads disk, but it must never
        quietly union a row from another run, boot, clock domain, or a
        truncated/duplicated append.  A contract error is preferable to a
        plausible coverage number with foreign evidence.
        """

        stream_files = {
            "lifecycle_events.jsonl": LIFECYCLE_SCHEMA,
            "tool_events.jsonl": TOOL_SCHEMA,
            "model_events.jsonl": MODEL_SCHEMA,
            "hardware_snapshots.jsonl": HARDWARE_SCHEMA,
        }
        expected_identity = (self.run_id, self.attempt_id, self.case_id)
        expected_clock = (
            self.clock.get("hostname"),
            self.clock.get("boot_id"),
            self.clock.get("clock_id"),
        )
        if any(value is None for value in expected_clock):
            raise TelemetryContractError("reconciliation requires hostname, boot_id, and clock_id metadata")

        rows_by_id: dict[str, dict[str, Any]] = {}
        disk_event_ids: set[str] = set()
        memory_event_ids: set[str] = set()

        def accept(raw: Mapping[str, Any], *, source: str) -> None:
            row = dict(raw)
            event_id = row.get("event_id")
            if not isinstance(event_id, str) or not event_id:
                raise TelemetryContractError(f"{source} row has no event_id")
            if source == "in-memory":
                if event_id in memory_event_ids:
                    raise TelemetryContractError(f"duplicate in-memory event_id: {event_id}")
                memory_event_ids.add(event_id)
            else:
                if event_id in disk_event_ids:
                    raise TelemetryContractError(f"duplicate event_id in journals: {event_id}")
                disk_event_ids.add(event_id)
            if (row.get("run_id"), row.get("attempt_id"), row.get("case_id")) != expected_identity:
                raise TelemetryContractError(f"{source} row belongs to another run/attempt/case")
            clock = row.get("clock")
            if not isinstance(clock, Mapping):
                raise TelemetryContractError(f"{source} row has no clock metadata")
            if (clock.get("hostname"), clock.get("boot_id"), clock.get("clock_id")) != expected_clock:
                raise TelemetryContractError(f"{source} row belongs to another host/boot/clock domain")
            if not isinstance(row.get("writer_role"), str) or not row["writer_role"].strip():
                raise TelemetryContractError(f"{source} row has no writer_role")
            if not isinstance(row.get("terminal"), bool):
                raise TelemetryContractError(f"{source} row has non-boolean terminal state")
            previous = rows_by_id.get(event_id)
            if previous is not None:
                if previous != row:
                    raise TelemetryContractError(f"event_id has conflicting journal rows: {event_id}")
                # The local in-memory row is expected to be seen again when
                # the same append-only file is reread.  A second disk row is
                # rejected above, even when its bytes are identical.
                return
            rows_by_id[event_id] = row

        for row in self._rows:
            if not isinstance(row, Mapping):
                raise TelemetryContractError("in-memory telemetry row is not a mapping")
            accept(row, source="in-memory")

        for filename, schema in stream_files.items():
            path = self.output_dir / filename
            try:
                handle = path.open(encoding="utf-8")
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise TelemetryContractError(f"cannot read telemetry journal {path}: {exc}") from exc
            with handle:
                for line_number, line in enumerate(handle, 1):
                    if not line.strip():
                        continue
                    try:
                        loaded = json.loads(line)
                    except (TypeError, ValueError) as exc:
                        raise TelemetryContractError(
                            f"invalid JSON in {path}:{line_number}"
                        ) from exc
                    if not isinstance(loaded, Mapping):
                        raise TelemetryContractError(f"journal row is not an object: {path}:{line_number}")
                    if loaded.get("schema_version") != schema:
                        raise TelemetryContractError(f"unexpected schema in {path}:{line_number}")
                    accept(loaded, source=f"{path}:{line_number}")

        rows = list(rows_by_id.values())
        terminal_physical: dict[tuple[str, str], str] = {}
        for row in rows:
            if not row.get("terminal"):
                continue
            phase = row.get("phase")
            if phase == "model_request" and row.get("event_kind") == "model_request":
                physical = row.get("physical_request_id") or row.get("request_id")
                if isinstance(physical, str) and physical:
                    key = ("model_request", physical)
                    previous = terminal_physical.get(key)
                    if previous is not None and previous != str(row["event_id"]):
                        raise TelemetryContractError(f"duplicate physical request identity: {physical}")
                    terminal_physical[key] = str(row["event_id"])
            elif phase == "tool_execution" and row.get("event_kind") == "tool_event":
                physical = row.get("action_id")
                if isinstance(physical, str) and physical:
                    key = ("tool_execution", physical)
                    previous = terminal_physical.get(key)
                    if previous is not None and previous != str(row["event_id"]):
                        raise TelemetryContractError(f"duplicate physical action identity: {physical}")
                    terminal_physical[key] = str(row["event_id"])
        return rows

    def _measured_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for row in self._read_journal_rows():
            if not row.get("terminal") or row.get("event_kind") in _NON_ATTRIBUTING_PHASES:
                continue
            if row.get("phase") in _NON_ATTRIBUTING_PHASES or row.get("event_kind") == "hardware_snapshot":
                continue
            if row.get("availability") != "measured":
                continue
            if row.get("start_mono_ns") is None or row.get("end_mono_ns") is None:
                continue
            rows.append(row)
        return rows

    def reconcile_e2e(
        self,
        *,
        outer_start_mono_ns: int | None = None,
        outer_end_mono_ns: int | None = None,
        tolerance_ms: float | None = None,
        write_record: bool = True,
    ) -> dict[str, Any]:
        outer = self._outer_terminal
        start = outer_start_mono_ns if outer_start_mono_ns is not None else (outer.get("start_mono_ns") if outer else None)
        end = outer_end_mono_ns if outer_end_mono_ns is not None else (outer.get("end_mono_ns") if outer else None)
        if start is None or end is None:
            raise TelemetryContractError("E2E reconciliation requires a closed outer interval")
        start, end = _interval(start, end)
        useful_rows = [row for row in self._measured_rows() if row["event_kind"] != "outer_swe_agent" and row["phase"] not in _NON_ATTRIBUTING_PHASES]
        clipped: list[dict[str, Any]] = []
        for row in useful_rows:
            left = max(start, int(row["start_mono_ns"]))
            right = min(end, int(row["end_mono_ns"]))
            if right > left or (left == right == start):
                clipped.append({"start_mono_ns": left, "end_mono_ns": right})
        merged = interval_union(clipped)
        measured_ns = sum(right - left for left, right in merged)
        e2e_ns = end - start
        residual_ns = max(0, e2e_ns - measured_ns)
        coverage = (100.0 * measured_ns / e2e_ns) if e2e_ns else 100.0
        residual_pct = (100.0 * residual_ns / e2e_ns) if e2e_ns else 0.0
        unknown_intervals: list[tuple[int, int]] = []
        cursor = start
        for left, right in merged:
            if left > cursor:
                unknown_intervals.append((cursor, left))
            cursor = max(cursor, right)
        if cursor < end:
            unknown_intervals.append((cursor, end))
        if tolerance_ms is None:
            tolerance_ms = max(1.0, e2e_ns / 1_000_000 * 0.001)
        category_union: dict[str, float] = {}
        for phase in sorted({row["phase"] for row in useful_rows}):
            phase_rows = [row for row in useful_rows if row["phase"] == phase]
            category_union[phase] = union_duration_ms(
                [{"start_mono_ns": max(start, int(row["start_mono_ns"])), "end_mono_ns": min(end, int(row["end_mono_ns"]))} for row in phase_rows if int(row["end_mono_ns"]) >= start and int(row["start_mono_ns"]) <= end]
            )
        result = {
            "schema_version": LIFECYCLE_SCHEMA,
            "event_kind": "e2e_reconciliation",
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "case_id": self.case_id,
            "instance_id": self.instance_id,
            "outer_start_mono_ns": start,
            "outer_end_mono_ns": end,
            "e2e_duration_ms": e2e_ns / 1_000_000,
            "measured_phase_union_ms": measured_ns / 1_000_000,
            "unknown_residual_ms": residual_ns / 1_000_000,
            "measured_coverage_percent": coverage,
            "unknown_residual_percent": residual_pct,
            "closure_error_ms": (measured_ns + residual_ns - e2e_ns) / 1_000_000,
            "tolerance_ms": tolerance_ms,
            "within_tolerance": abs(measured_ns + residual_ns - e2e_ns) <= tolerance_ms * 1_000_000,
            "phase_union_ms": category_union,
            "measured_intervals": [[left, right] for left, right in merged],
            "unknown_intervals": [[left, right] for left, right in unknown_intervals],
            "outer_wrapper_excluded": True,
            "unknown_residual_policy": "separate_unassigned_complement",
            "availability": "derived",
            "provenance": "derived",
            "clock": dict(self.clock),
            "utc_recorded": utc_now(),
        }
        if write_record:
            self._append(
                "lifecycle",
                self._event_base(
                    stream="lifecycle",
                    event_kind="e2e_reconciliation",
                    phase="e2e_reconciliation",
                    status="success" if result["within_tolerance"] else "failure",
                    start_mono_ns=start,
                    end_mono_ns=end,
                    span_id=stable_id("reconciliation", self.run_id, self.attempt_id, start, end),
                    parent_event_id=outer.get("event_id") if outer else None,
                    provenance="derived",
                    availability="derived",
                    terminal=True,
                    **{key: value for key, value in result.items() if key not in {"schema_version", "event_kind", "run_id", "attempt_id", "case_id", "instance_id", "availability", "provenance", "clock", "utc_recorded"}},
                ),
            )
            if not self._unknown_written:
                for index, (unknown_start, unknown_end) in enumerate(unknown_intervals):
                    self._append(
                        "lifecycle",
                        self._event_base(
                            stream="lifecycle",
                            event_kind="unknown_residual",
                            phase="unknown_residual",
                            status="unavailable",
                            start_mono_ns=unknown_start,
                            end_mono_ns=unknown_end,
                            span_id=stable_id("unknown", self.run_id, self.attempt_id, start, end, index, unknown_start, unknown_end),
                            parent_event_id=outer.get("event_id") if outer else None,
                            provenance="unavailable",
                            availability="unavailable",
                            terminal=True,
                            reason="complement of measured phase interval union; no causal assignment",
                            unassigned=True,
                        ),
                    )
                self._unknown_written = True
        return result

    close = reconcile_e2e

    def record_unknown(self, *, start_mono_ns: int, end_mono_ns: int, reason: str = "unassigned residual") -> dict[str, Any]:
        start, end = _interval(start_mono_ns, end_mono_ns)
        return self._append(
            "lifecycle",
            self._event_base(
                stream="lifecycle",
                event_kind="unknown_residual",
                phase="unknown_residual",
                status="unavailable",
                start_mono_ns=start,
                end_mono_ns=end,
                span_id=stable_id("unknown", self.run_id, self.attempt_id, start, end, reason),
                parent_event_id=None,
                provenance="unavailable",
                availability="unavailable",
                terminal=True,
                reason=reason,
                unassigned=True,
            ),
        )

    def rows(self, stream: str | None = None) -> list[dict[str, Any]]:
        if stream is None:
            return [dict(row) for row in self._rows]
        return [dict(row) for row in self._rows if row.get("schema_version", "").endswith(stream)]


# Names used by early pilot drafts and external adapters.
AssignmentTelemetry = TelemetryV2
TelemetryV2Recorder = TelemetryV2


__all__ = [
    "ALL_STATUSES",
    "AssignmentTelemetry",
    "AppendOnlyWriter",
    "HARDWARE_SCHEMA",
    "LIFECYCLE_PHASES",
    "LIFECYCLE_SCHEMA",
    "MODEL_SCHEMA",
    "MANIFEST_SCHEMA",
    "REQUEST_PAYLOADS_DIR",
    "SCRIPT_ARTIFACTS_DIR",
    "PROVENANCE",
    "Span",
    "TELEMETRY_SCHEMA",
    "TelemetryContractError",
    "TelemetryV2",
    "TelemetryV2Recorder",
    "TERMINAL_STATUSES",
    "TOOL_SCHEMA",
    "interval_union",
    "stable_id",
    "union_duration_ms",
    "ScriptStateLedger",
]
