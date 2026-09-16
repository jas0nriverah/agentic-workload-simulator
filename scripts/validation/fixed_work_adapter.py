#!/usr/bin/env python3
"""Execute one measured condition of the bounded four-fixture replay.

The replay runner owns snapshot restoration and pair comparison.  This adapter
owns one condition: it validates the immutable action/request fixtures,
executes the fixed work, and emits the exact v2 terminal result.  CPU work is
run in one persistent bash process and uses the reviewed BCC service when the
condition is instrumented.  Model work is sent through the reviewed request
proxy with an explicitly supplied serving-metrics configuration.

The raw-bash path remains diagnostic and never sets
``full_production_capture_enabled``.  A CPU fixture can opt into the pinned
SWE-agent/SWE-ReX Docker path with an explicit ``swe_runtime`` descriptor.
That path uses the real callback and script-state hooks and sets the full
capture flag only after the hook journals, BPF joins, and measured cgroup
start/end context all validate.

There are deliberately no model, endpoint, cache, or workload defaults.  A
missing live integration is a blocked condition with an evidence file; it is
never represented as a completed measurement.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import contextlib
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import shlex
import sys
import tarfile
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[2]
for candidate in (ROOT, ROOT / "src"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from agentic_sim.telemetry.bpf_work import (  # noqa: E402
    BpfAttachError,
    BpfProtocolError,
    ProcessTarget,
    _run_bash_action_unmodified,
    _spawn_bash_fixture,
    _stop_bash_fixture,
    iter_bpf_events,
    launch_bpf_work_service,
)


FIXTURE_MANIFEST_SCHEMA = "assignment.fixed-work-fixture-manifest.v1"
RESULT_SCHEMA = "assignment.instrumentation-replay-result.v2"
ADAPTER_ERROR_SCHEMA = "assignment.fixed-work-adapter-error.v1"
MODES = frozenset({"instrument_off", "instrument_on"})
CPU_KINDS = frozenset({"cpu_filesystem_traversal", "cpu_test_script_subprocess"})
MODEL_KINDS = frozenset({"model_short_request", "model_long_context_request"})
HEX64 = frozenset("0123456789abcdef")
REQUIRED_CGROUP_CONTEXT_FILES = (
    "cpu.stat",
    "cpu.max",
    "io.stat",
    "cpu.pressure",
    "io.pressure",
    "memory.pressure",
)
REQUIRED_HOST_PRESSURE_FILES = ("pressure/cpu", "pressure/io", "pressure/memory")
# This standalone CLI does not run the pinned SWE-agent/SWE-ReX callback
# lifecycle.  Its BPF/proxy journals are useful diagnostic evidence, but a
# result from this adapter cannot be labeled full-production gate evidence.
FULL_PRODUCTION_CAPTURE_ENABLED = False


class AdapterError(ValueError):
    """A fixture condition cannot produce safe measured v2 evidence."""


@dataclass(frozen=True)
class Action:
    index: int
    event_id: str
    command: str
    source_sha256: str
    expected_status: str = "success"


@dataclass(frozen=True)
class Request:
    index: int
    method: str
    path: str
    body: bytes
    headers: dict[str, str]
    logical_request_id: str


@dataclass(frozen=True)
class Fixture:
    case_id: str
    kind: str
    action_path: Path
    request_path: Path
    action_sha256: str
    request_sha256: str
    workload_sha256: str
    snapshot_sha256: str
    snapshot_path: Path
    policy_sha256: str
    spec: dict[str, Any]
    actions: tuple[Action, ...]
    request_rows: tuple[dict[str, Any], ...]


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise AdapterError(f"cannot hash {path}: {exc}") from exc
    return digest.hexdigest()


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or set(value.lower()) - HEX64:
        raise AdapterError(f"{label} must be a SHA-256 digest")
    return value.lower()


def _absolute_regular(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise AdapterError(f"{label} must be a non-empty path")
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raise AdapterError(f"{label} must be absolute")
    if raw.is_symlink():
        raise AdapterError(f"{label} must not be a symlink")
    path = raw.resolve()
    if not path.is_file() or path.is_symlink():
        raise AdapterError(f"{label} must be a regular file: {raw}")
    return path


def _absolute_directory(value: Any, label: str, *, create: bool = False) -> Path:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise AdapterError(f"{label} must be a non-empty path")
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raise AdapterError(f"{label} must be absolute")
    if raw.is_symlink():
        raise AdapterError(f"{label} must not be a symlink")
    if create:
        raw.mkdir(parents=True, exist_ok=True)
    path = raw.resolve()
    if not path.is_dir() or path.is_symlink():
        raise AdapterError(f"{label} must be an existing directory: {raw}")
    return path


def _under(path: Path, parent: Path, label: str) -> Path:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError as exc:
        raise AdapterError(f"{label} must be beneath {parent}") from exc
    return path


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    if path.is_symlink():
        raise AdapterError(f"refusing to write through symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (_canonical(dict(value)) + "\n").encode("utf-8")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{path.name}.", dir=str(path.parent), delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(str(path.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink()
        raise AdapterError(f"cannot durably write {path}: {exc}") from exc


def _write_bytes(path: Path, value: bytes) -> None:
    if path.is_symlink():
        raise AdapterError(f"refusing to write through symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise AdapterError(f"refusing to overwrite existing artifact: {path}") from exc
    except OSError as exc:
        raise AdapterError(f"cannot write {path}: {exc}") from exc


def _append_jsonl(path: Path, value: Mapping[str, Any]) -> None:
    if path.is_symlink():
        raise AdapterError(f"refusing to write through symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (_canonical(dict(value)) + "\n").encode("utf-8")
    try:
        with path.open("ab") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise AdapterError(f"cannot append {path}: {exc}") from exc


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdapterError(f"cannot read {label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AdapterError(f"{label} must be a JSON object")
    return value


def _read_jsonl(path: Path, label: str) -> list[dict[str, Any]]:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise AdapterError(f"cannot read {label}: {path}: {exc}") from exc
    if not payload:
        raise AdapterError(f"{label} is empty: {path}")
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise AdapterError(f"{label} is not UTF-8: {path}") from exc
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            raise AdapterError(f"{label} has a blank line at {number}")
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AdapterError(f"{label} line {number} is not JSON: {exc}") from exc
        if not isinstance(row, dict):
            raise AdapterError(f"{label} line {number} must be a JSON object")
        rows.append(row)
    return rows


def _safe_snapshot(path: Path) -> None:
    try:
        with tarfile.open(path, mode="r:*") as archive:
            members = archive.getmembers()
    except (OSError, tarfile.TarError) as exc:
        raise AdapterError(f"pretrajectory snapshot is not a readable tar archive: {path}") from exc
    if not members:
        raise AdapterError(f"pretrajectory snapshot is empty: {path}")
    names: set[str] = set()
    for member in members:
        name = member.name
        pure = Path(name)
        if name in {"", "."} or pure.is_absolute() or ".." in pure.parts or "\\" in name:
            raise AdapterError(f"snapshot contains an unsafe member: {name!r}")
        if name in names:
            raise AdapterError(f"snapshot contains duplicate member: {name}")
        names.add(name)
        if not (member.isdir() or member.isreg()) or member.issym() or member.islnk():
            raise AdapterError(f"snapshot member is not a regular file/directory: {name}")
        if member.mode & 0o6000:
            raise AdapterError(f"snapshot contains a set-id member: {name}")


def _fixture_entry(manifest: Mapping[str, Any], fixture_id: str) -> dict[str, Any]:
    if manifest.get("schema_version") not in {FIXTURE_MANIFEST_SCHEMA, "assignment.instrumentation-replay-manifest.v2"}:
        raise AdapterError("unsupported fixed-work fixture manifest schema")
    entries = manifest.get("fixtures")
    if isinstance(entries, list):
        candidates = [item for item in entries if isinstance(item, Mapping) and item.get("fixture_id") == fixture_id]
    elif isinstance(entries, Mapping):
        item = entries.get(fixture_id)
        candidates = [item] if isinstance(item, Mapping) else []
    else:
        candidates = []
    if not candidates and isinstance(manifest.get("fixture"), Mapping):
        if manifest["fixture"].get("fixture_id", fixture_id) == fixture_id:
            candidates = [manifest["fixture"]]
    if not candidates and isinstance(manifest.get("cases"), list):
        case = next((item for item in manifest["cases"] if isinstance(item, Mapping) and item.get("case_id") == fixture_id), None)
        if case is not None:
            candidates = [case]
            fixture_dir_value = case.get("fixture_dir")
            if isinstance(fixture_dir_value, str):
                fixture_dir = Path(fixture_dir_value).expanduser()
                for sidecar in (fixture_dir / "fixed_work_fixture.json", fixture_dir / "fixture.json"):
                    if sidecar.is_file() and not sidecar.is_symlink():
                        sidecar_value = _read_json(sidecar, "fixture descriptor")
                        if sidecar_value.get("fixture_id", fixture_id) != fixture_id:
                            raise AdapterError(f"fixture descriptor identity mismatch: {sidecar}")
                        merged = dict(case)
                        merged.update(sidecar_value)
                        candidates = [merged]
                        break
    if not candidates:
        raise AdapterError(f"fixture id is absent from fixture manifest: {fixture_id}")
    return dict(candidates[0])


def _policy_sha256(spec: Mapping[str, Any]) -> str:
    policy = spec.get("serving_and_cache_policy")
    if not isinstance(policy, Mapping) or not policy:
        raise AdapterError(
            "fixture must provide a non-empty serving_and_cache_policy mapping; "
            "the adapter will not invent a cache or endpoint identity"
        )
    digest = _sha_bytes(_canonical(dict(policy)).encode("utf-8"))
    declared = spec.get("serving_and_cache_policy_sha256")
    if declared is not None and _digest(declared, "serving_and_cache_policy_sha256") != digest:
        raise AdapterError("serving_and_cache_policy_sha256 does not match the explicit policy")
    return digest


def _parse_actions(rows: Sequence[Mapping[str, Any]]) -> tuple[Action, ...]:
    actions: list[Action] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        command = row.get("command", row.get("action"))
        if not isinstance(command, str) or not command.strip() or "\x00" in command:
            raise AdapterError(f"action fixture line {index + 1} has no safe command")
        event_id = row.get("event_id", row.get("action_id", f"fixed-action-{index:04d}"))
        if not isinstance(event_id, str) or not event_id.strip() or "\x00" in event_id or event_id in seen:
            raise AdapterError(f"action fixture line {index + 1} has an invalid or duplicate event_id")
        expected_status = row.get("expected_status", row.get("status", "success"))
        if not isinstance(expected_status, str) or expected_status not in {"success", "failure", "timeout"}:
            raise AdapterError(
                f"action fixture line {index + 1} has unsupported expected_status: {expected_status!r}"
            )
        seen.add(event_id)
        actions.append(
            Action(
                index,
                event_id,
                command,
                _sha_bytes(command.encode("utf-8")),
                str(expected_status),
            )
        )
    if not actions:
        raise AdapterError("action fixture has no actions")
    return tuple(actions)


def _load_fixture(manifest_path: Path, fixture_id: str) -> Fixture:
    manifest = _read_json(_absolute_regular(str(manifest_path), "fixture manifest"), "fixture manifest")
    spec = _fixture_entry(manifest, fixture_id)
    if spec.get("fixture_id") not in {None, fixture_id}:
        raise AdapterError("fixture descriptor case identity differs from --fixture-id")
    spec["fixture_id"] = fixture_id
    kind = spec.get("fixture_kind", spec.get("kind"))
    if not isinstance(kind, str) or not kind.strip():
        raise AdapterError(f"{fixture_id}: fixture_kind is required")
    action_path = _absolute_regular(spec.get("action_fixture"), f"{fixture_id}.action_fixture")
    request_path = _absolute_regular(spec.get("request_fixture"), f"{fixture_id}.request_fixture")
    snapshot_path = _absolute_regular(spec.get("pretrajectory_snapshot"), f"{fixture_id}.pretrajectory_snapshot")
    action_sha = _sha_file(action_path)
    request_sha = _sha_file(request_path)
    snapshot_sha = _sha_file(snapshot_path)
    if action_sha != _digest(spec.get("action_fixture_sha256"), f"{fixture_id}.action_fixture_sha256"):
        raise AdapterError(f"{fixture_id}: action fixture hash mismatch")
    if request_sha != _digest(spec.get("request_fixture_sha256"), f"{fixture_id}.request_fixture_sha256"):
        raise AdapterError(f"{fixture_id}: request fixture hash mismatch")
    if action_sha != _digest(spec.get("action_sequence_sha256"), f"{fixture_id}.action_sequence_sha256"):
        raise AdapterError(f"{fixture_id}: action sequence hash mismatch")
    if request_sha != _digest(spec.get("request_sequence_sha256"), f"{fixture_id}.request_sequence_sha256"):
        raise AdapterError(f"{fixture_id}: request sequence hash mismatch")
    workload_sha = _sha_bytes(f"{action_sha}\0{request_sha}".encode("ascii"))
    if workload_sha != _digest(spec.get("workload_sha256"), f"{fixture_id}.workload_sha256"):
        raise AdapterError(f"{fixture_id}: workload hash mismatch")
    if snapshot_sha != _digest(spec.get("pretrajectory_snapshot_sha256"), f"{fixture_id}.pretrajectory_snapshot_sha256"):
        raise AdapterError(f"{fixture_id}: pretrajectory snapshot hash mismatch")
    _safe_snapshot(snapshot_path)
    action_rows = _read_jsonl(action_path, f"{fixture_id} action fixture")
    request_rows = _read_jsonl(request_path, f"{fixture_id} request fixture")
    return Fixture(
        case_id=fixture_id,
        kind=kind,
        action_path=action_path,
        request_path=request_path,
        action_sha256=action_sha,
        request_sha256=request_sha,
        workload_sha256=workload_sha,
        snapshot_sha256=snapshot_sha,
        snapshot_path=snapshot_path,
        policy_sha256=_policy_sha256(spec),
        spec=spec,
        actions=_parse_actions(action_rows),
        request_rows=tuple(request_rows),
    )


def _validate_timeout(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) <= 0:
        raise AdapterError(f"{label} must be positive")
    return float(value)


def _cpu_config(fixture: Fixture) -> dict[str, Any] | None:
    value = fixture.spec.get("cpu_collector", fixture.spec.get("cpu_work"))
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise AdapterError(f"{fixture.case_id}: cpu_collector must be a mapping")
    config = dict(value)
    if config.get("backend") != "bcc":
        raise AdapterError(f"{fixture.case_id}: only the reviewed BCC CPU backend is supported")
    if config.get("attach_existing_process") is not True:
        raise AdapterError(f"{fixture.case_id}: CPU collector must attach an existing process")
    if config.get("require_persistent_runtime_pid") is not True:
        raise AdapterError(f"{fixture.case_id}: CPU collector must require a persistent runtime PID")
    trace_format = str(config.get("trace_format", ""))
    if "raw" not in trace_format.lower() or "individual" not in trace_format.lower():
        raise AdapterError(f"{fixture.case_id}: CPU collector must retain raw individual records")
    return config


def _require_action_capture(fixture: Fixture, mode: str, cpu_config: Mapping[str, Any] | None) -> None:
    if mode == "instrument_on" and fixture.actions and cpu_config is None:
        raise AdapterError(
            f"{fixture.case_id}: instrument_on would run uncaptured fixture actions; "
            "explicit cpu_collector configuration is required"
        )


def _model_config(fixture: Fixture) -> dict[str, Any]:
    value = fixture.spec.get("model")
    if not isinstance(value, Mapping):
        raise AdapterError(f"{fixture.case_id}: explicit model proxy configuration is required")
    config = dict(value)
    upstream = config.get("upstream")
    if isinstance(upstream, Mapping):
        host = upstream.get("host")
        port = upstream.get("port")
    else:
        host = config.get("upstream_host")
        port = config.get("upstream_port")
    if not isinstance(host, str) or not host.strip() or "\x00" in host:
        raise AdapterError(f"{fixture.case_id}: model upstream host is required")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise AdapterError(f"{fixture.case_id}: model upstream port is required")
    endpoint = config.get("endpoint")
    if isinstance(endpoint, Mapping):
        method = endpoint.get("method")
        path = endpoint.get("path")
    else:
        method = config.get("method")
        path = config.get("path", config.get("endpoint_path"))
    if not isinstance(method, str) or not method or any(ch.isspace() for ch in method):
        raise AdapterError(f"{fixture.case_id}: model endpoint method is required")
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
        raise AdapterError(f"{fixture.case_id}: model endpoint path is required")
    if not isinstance(config.get("cache_policy"), Mapping) or not config["cache_policy"]:
        raise AdapterError(f"{fixture.case_id}: explicit model cache_policy is required")
    serving = config.get("serving_metrics_config")
    if isinstance(serving, Mapping):
        serving_config = dict(serving)
    elif isinstance(serving, str) and serving.strip():
        from scripts.observability.request_proxy import load_serving_metrics_config

        try:
            serving_config = load_serving_metrics_config(serving)
        except (OSError, ValueError) as exc:
            raise AdapterError(f"{fixture.case_id}: serving_metrics_config is invalid: {exc}") from exc
    else:
        raise AdapterError(f"{fixture.case_id}: explicit serving_metrics_config is required")
    if serving_config.get("enabled") is not True:
        raise AdapterError(f"{fixture.case_id}: model serving metrics must be explicitly enabled")
    timeout = _validate_timeout(config.get("timeout_seconds"), f"{fixture.case_id}.model.timeout_seconds")
    max_body = config.get("max_body_bytes")
    if isinstance(max_body, bool) or not isinstance(max_body, int) or max_body <= 0:
        raise AdapterError(f"{fixture.case_id}: model max_body_bytes is required")
    headers = config.get("headers", {})
    if not isinstance(headers, Mapping) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()):
        raise AdapterError(f"{fixture.case_id}: model headers must be a string mapping")
    return {
        **config,
        "upstream_host": host,
        "upstream_port": port,
        "method": method.upper(),
        "path": path,
        "serving_metrics_config": serving_config,
        "timeout_seconds": timeout,
        "max_body_bytes": max_body,
        "headers": dict(headers),
    }


def _decode_body(row: Mapping[str, Any], fixture: Fixture, index: int) -> bytes:
    supplied = [key for key in ("body_base64", "body", "body_path") if key in row]
    if len(supplied) != 1:
        raise AdapterError(f"{fixture.case_id}: request line {index + 1} needs exactly one explicit body source")
    source = supplied[0]
    if source == "body_base64":
        value = row[source]
        if not isinstance(value, str):
            raise AdapterError(f"{fixture.case_id}: request line {index + 1} body_base64 is not text")
        try:
            body = base64.b64decode(value.encode("ascii"), validate=True)
        except (UnicodeEncodeError, binascii.Error) as exc:
            raise AdapterError(f"{fixture.case_id}: request line {index + 1} body_base64 is invalid") from exc
    elif source == "body_path":
        path = _absolute_regular(row[source], f"{fixture.case_id}.request[{index}].body_path")
        _under(path, fixture.request_path.parent, f"{fixture.case_id}.body_path")
        body = path.read_bytes()
    else:
        value = row[source]
        if isinstance(value, str):
            body = value.encode("utf-8")
        elif isinstance(value, (dict, list)):
            body = _canonical(value).encode("utf-8")
        else:
            raise AdapterError(f"{fixture.case_id}: request line {index + 1} body is not text or JSON")
    declared = row.get("body_sha256")
    if declared is not None and _digest(declared, f"{fixture.case_id}.request[{index}].body_sha256") != _sha_bytes(body):
        raise AdapterError(f"{fixture.case_id}: request line {index + 1} body hash mismatch")
    return body


def _parse_requests(fixture: Fixture, model: bool) -> tuple[Request, ...]:
    if not model:
        for index, row in enumerate(fixture.request_rows):
            if row.get("requests") not in (None, []):
                raise AdapterError(f"{fixture.case_id}: CPU request fixture contains model requests at line {index + 1}")
            forbidden = {"body", "body_base64", "body_path", "method", "path", "endpoint", "request"} & set(row)
            if forbidden:
                raise AdapterError(f"{fixture.case_id}: CPU request fixture contains request fields: {sorted(forbidden)}")
        return ()
    config = _model_config(fixture)
    requests: list[Request] = []
    for index, row in enumerate(fixture.request_rows):
        method = row.get("method")
        path = row.get("path")
        if not isinstance(method, str) or method.upper() != config["method"]:
            raise AdapterError(f"{fixture.case_id}: request line {index + 1} method differs from explicit endpoint")
        if not isinstance(path, str) or path != config["path"]:
            raise AdapterError(f"{fixture.case_id}: request line {index + 1} path differs from explicit endpoint")
        headers = row.get("headers", {})
        if not isinstance(headers, Mapping) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()):
            raise AdapterError(f"{fixture.case_id}: request line {index + 1} headers are invalid")
        logical_id = row.get("logical_request_id", row.get("request_id", f"{fixture.case_id}:request:{index}"))
        if not isinstance(logical_id, str) or not logical_id.strip() or "\x00" in logical_id:
            raise AdapterError(f"{fixture.case_id}: request line {index + 1} has an invalid logical request id")
        requests.append(
            Request(
                index=index,
                method=config["method"],
                path=config["path"],
                body=_decode_body(row, fixture, index),
                headers={str(key): str(value) for key, value in headers.items()},
                logical_request_id=logical_id,
            )
        )
    if not requests:
        raise AdapterError(f"{fixture.case_id}: model request fixture has no requests")
    return tuple(requests)


def _capture_command(command: str, stdout_path: Path, stderr_path: Path) -> str:
    # Pass the fixture command as one argument to a child shell.  This keeps
    # quotes, newlines, and dollar signs in the fixture from being interpreted
    # by the persistent shell that carries the action boundary.
    return (
        "/bin/bash -c "
        + shlex.quote(command)
        + " > "
        + shlex.quote(str(stdout_path))
        + " 2> "
        + shlex.quote(str(stderr_path))
    )


def _action_error(exc: BaseException) -> tuple[str, str | None, bool]:
    message = str(exc)
    if message.startswith("fixture failed:"):
        return "failure", message[:512], True
    if "did not reach marker" in message:
        return "timeout", message[:512], False
    return "failure", message[:512], False


def _prepare_persistent_shell(process: Any, scratch_dir: Path, timeout_seconds: float) -> None:
    """Make the orchestrator-provided scratch directory the shell's cwd."""

    try:
        _run_bash_action_unmodified(
            process,
            ":",
            f"__FIXED_WORK_READY_{os.getpid()}__",
            timeout_seconds,
        )
        _run_bash_action_unmodified(
            process,
            "cd " + shlex.quote(str(scratch_dir)),
            f"__FIXED_WORK_CWD_{os.getpid()}__",
            timeout_seconds,
        )
    except BaseException as exc:
        raise AdapterError(f"persistent shell setup failed: {type(exc).__name__}") from exc


def _run_actions(
    process: Any,
    actions: Sequence[Action],
    output_dir: Path,
    timeout_seconds: float,
    collector: Any | None,
) -> list[dict[str, Any]]:
    journal = output_dir / "actions" / "action_audit.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    result: list[dict[str, Any]] = []
    for action in actions:
        stdout_path = output_dir / "actions" / f"{action.index:04d}.stdout"
        stderr_path = output_dir / "actions" / f"{action.index:04d}.stderr"
        command = _capture_command(action.command, stdout_path, stderr_path)
        command_sha = _sha_bytes(command.encode("utf-8"))
        event_id = action.event_id
        marker = f"__FIXED_WORK_ACTION_{action.index:04d}_{os.getpid()}__"
        status = "success"
        error: str | None = None
        marker_reached = False
        started = time.perf_counter_ns()
        if collector is not None:
            try:
                collector.start_action(event_id, command, start_mono_ns=time.monotonic_ns())
            except BaseException as exc:
                raise AdapterError(f"BPF action start failed for action {action.index}: {type(exc).__name__}") from exc
        try:
            _run_bash_action_unmodified(process, command, marker, timeout_seconds)
            marker_reached = True
        except BaseException as exc:
            status, error, marker_reached = _action_error(exc)
        finally:
            if collector is not None:
                try:
                    collector.end_action(
                        event_id,
                        status=status,
                        end_mono_ns=time.monotonic_ns(),
                        timeout=status == "timeout",
                        error=error,
                    )
                except BaseException as exc:
                    raise AdapterError(f"BPF action end failed for action {action.index}: {type(exc).__name__}") from exc
        for path in (stdout_path, stderr_path):
            if not path.is_file() or path.is_symlink():
                raise AdapterError(f"action {action.index} did not produce its output artifact")
            try:
                with path.open("rb") as handle:
                    os.fsync(handle.fileno())
            except OSError as exc:
                raise AdapterError(f"action {action.index} output durability failed") from exc
        ended = time.perf_counter_ns()
        row = {
            "schema_version": "assignment.fixed-work-action-audit.v1",
            "index": action.index,
            "event_id": event_id,
            "source_command_sha256": action.source_sha256,
            "dispatched_command_sha256": command_sha,
            "status": status,
            "marker_reached": marker_reached,
            "error": error,
            "duration_ms": (ended - started) / 1_000_000,
            "stdout_path": str(stdout_path.relative_to(output_dir)),
            "stderr_path": str(stderr_path.relative_to(output_dir)),
        }
        _append_jsonl(journal, row)
        result.append(row)
        if not marker_reached:
            raise AdapterError(f"action {action.index} did not reach its completion marker")
    return result


def _read_summary(trace_dir: Path) -> dict[str, Any]:
    path = trace_dir / "work_summary.json"
    if not path.is_file() or path.is_symlink():
        raise AdapterError(f"BPF service did not leave a regular work summary: {path}")
    return _read_json(path, "BPF work summary")


def _bpf_capture_counts(summary: Mapping[str, Any], trace_dir: Path, action_audit: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    actions = summary.get("actions")
    finalizations = summary.get("action_finalizations")
    if not isinstance(actions, list) or not isinstance(finalizations, list) or len(actions) != len(action_audit):
        raise AdapterError("BPF summary action count does not match the dispatched action audit")
    finals: dict[int, Mapping[str, Any]] = {}
    for row in finalizations:
        if isinstance(row, Mapping) and isinstance(row.get("action_token"), int):
            finals[int(row["action_token"])] = row
    stream = trace_dir / "raw_events.bin"
    if not stream.is_file() or stream.is_symlink():
        raise AdapterError("BPF raw individual event stream is unavailable")
    stream_descriptor = summary.get("raw_event_stream")
    stream_schema: str | None = None
    stream_record_size: int | None = None
    if isinstance(stream_descriptor, Mapping):
        candidate_schema = stream_descriptor.get("schema_version")
        candidate_size = stream_descriptor.get("record_size_bytes")
        if isinstance(candidate_schema, str) and candidate_schema:
            stream_schema = candidate_schema
        if isinstance(candidate_size, int) and not isinstance(candidate_size, bool) and candidate_size > 0:
            stream_record_size = candidate_size
        if stream_schema is None or stream_record_size is None:
            raise AdapterError("BPF raw event stream ABI descriptor is incomplete")
    individual = 0
    dropped = 0
    map_failures = 0
    for index, (action, audit) in enumerate(zip(actions, action_audit)):
        if not isinstance(action, Mapping) or not isinstance(action.get("raw"), Mapping):
            raise AdapterError(f"BPF summary action {index} has no raw evidence")
        raw = action["raw"]
        token = raw.get("action_token")
        if not isinstance(token, int):
            raise AdapterError(f"BPF summary action {index} has no action token")
        chosen = finals.get(token, raw)
        if chosen.get("command_sha256") != audit.get("dispatched_command_sha256"):
            raise AdapterError(f"BPF action {index} command hash differs from observed dispatch audit")
        if chosen.get("event_records_complete") is not True:
            raise AdapterError(f"BPF action {index} individual records are incomplete")
        aggregate = chosen.get("raw_aggregate")
        if not isinstance(aggregate, Mapping):
            raise AdapterError(f"BPF action {index} has no raw aggregate")
        observed = 0
        try:
            if stream_schema is None or stream_record_size is None:
                # Minimal historical test doubles predate the summary stream
                # descriptor.  Keep this compatibility path metadata-free;
                # production summaries always take the explicit ABI path.
                events = iter_bpf_events(stream, token=token)
            else:
                events = iter_bpf_events(
                    stream,
                    token=token,
                    schema_version=stream_schema,
                    record_size_bytes=stream_record_size,
                )
            for _event in events:
                observed += 1
        except Exception as exc:
            raise AdapterError(f"BPF raw event stream cannot be decoded for action {index}") from exc
        declared = chosen.get("event_count")
        if not isinstance(declared, int) or declared != observed:
            raise AdapterError(f"BPF action {index} event count differs from the binary stream")
        required = chosen.get("required_event_count")
        if not isinstance(required, int) or required < 0 or observed < required:
            raise AdapterError(f"BPF action {index} is missing required individual records")
        loss_fields = ("lost_event_records", "lost_pending_records", "lost_path_records")
        dropped += int(chosen.get("perf_lost_events", 0) or 0)
        for field in loss_fields:
            value = aggregate.get(field, 0)
            if not isinstance(value, int) or value < 0:
                raise AdapterError(f"BPF action {index} has an invalid {field} count")
            dropped += value
        value = aggregate.get("lineage_map_failures", 0)
        if not isinstance(value, int) or value < 0:
            raise AdapterError(f"BPF action {index} has an invalid lineage map failure count")
        map_failures += value
        if chosen.get("censored_pending"):
            raise AdapterError(f"BPF action {index} has censored pending operations")
        individual += observed
    if dropped or map_failures:
        raise AdapterError(
            f"BPF full capture has loss: dropped_cpu_records={dropped}, "
            f"cpu_capture_map_failures={map_failures}"
        )
    return {
        "individual_cpu_operation_records": individual,
        "dropped_cpu_records": dropped,
        "cpu_capture_map_failures": map_failures,
    }


def _swe_runtime_config(fixture: Fixture) -> dict[str, Any] | None:
    """Return the explicit Docker runtime opt-in from the fixture descriptor."""

    values: list[tuple[str, Any]] = []
    for key in ("swe_runtime", "production_runtime"):
        if key in fixture.spec:
            values.append((key, fixture.spec[key]))
    if not values:
        return None
    if len(values) != 1:
        raise AdapterError(f"{fixture.case_id}: provide only one pinned SWE runtime descriptor")
    key, value = values[0]
    if not isinstance(value, Mapping):
        raise AdapterError(f"{fixture.case_id}: {key} must be a mapping")
    config = dict(value)
    if config.get("enabled", True) is not True:
        raise AdapterError(f"{fixture.case_id}: {key} must explicitly enable the Docker hook path")
    return config


@contextlib.contextmanager
def _temporary_environment(values: Mapping[str, str | None]):
    previous = {key: os.environ.get(key) for key in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _runtime_string(config: Mapping[str, Any], key: str, default: str) -> str:
    value = config.get(key, default)
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise AdapterError(f"pinned SWE runtime {key} must be non-empty text")
    return value


def _load_pinned_swe_runtime(config: Mapping[str, Any], output_dir: Path) -> dict[str, Any]:
    """Load the exact SWEEnv construction used by the reviewed live helper."""

    from scripts.validation import check_persistent_shell_capture as live

    swe_root = Path(
        _runtime_string(config, "swe_agent_root", str(live.DEFAULT_SWE_AGENT_ROOT))
    ).expanduser().resolve()
    if (
        not (swe_root / "sweagent").is_dir()
        or not (swe_root / ".venv" / "bin" / "python").is_file()
    ):
        raise AdapterError(f"pinned SWE-agent checkout/.venv is unavailable: {swe_root}")

    # The adapter is normally launched by the replay runner's lightweight
    # environment.  Add only the pinned checkout and its own site-packages so
    # SWEEnv/SWE-ReX are imported from the reviewed runtime.
    site_packages = sorted(
        (swe_root / ".venv" / "lib").glob("python*/site-packages"),
        key=str,
        reverse=True,
    )
    if not site_packages:
        raise AdapterError(f"pinned SWE-agent site-packages are unavailable: {swe_root / '.venv'}")
    for candidate in (swe_root, ROOT / "src", *site_packages):
        if str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))

    try:
        import sweagent
        import swerex
        from sweagent.environment.swe_env import SWEEnv
        from swerex.deployment.config import DockerDeploymentConfig
        from swerex.deployment.docker import DockerDeployment
        from swerex.runtime.abstract import UploadRequest
        from agentic_sim.telemetry.sweagent_hooks import (
            SWEAgentEnvironmentTelemetryHook,
            SWEAgentTelemetryHook,
        )
        from agentic_sim.telemetry.v2 import TelemetryV2
    except (ImportError, OSError) as exc:
        raise AdapterError(f"pinned SWEEnv dependencies are unavailable: {type(exc).__name__}: {exc}") from exc

    agent_commit = live.git_value(swe_root, "rev-parse", "HEAD")
    if agent_commit != live.EXPECTED_SWE_AGENT_COMMIT:
        raise AdapterError(f"SWE-agent checkout is not pinned: {agent_commit}")
    if (
        sweagent.__version__ != live.EXPECTED_SWE_AGENT_VERSION
        or swerex.__version__ != live.EXPECTED_SWE_REX_VERSION
    ):
        raise AdapterError(
            "pinned versions mismatch: "
            f"sweagent={sweagent.__version__} swerex={swerex.__version__}"
        )

    base_image = _runtime_string(config, "base_image", live.DEFAULT_BASE_IMAGE)
    if "@sha256:" not in base_image or any(ch.isspace() for ch in base_image):
        raise AdapterError("pinned SWE runtime base_image must include a SHA-256 digest")
    image = _runtime_string(config, "image", live.DEFAULT_IMAGE)
    if any(ch.isspace() for ch in image):
        raise AdapterError("pinned SWE runtime image must not contain whitespace")
    no_build_image = config.get("no_build_image", False)
    if not isinstance(no_build_image, bool):
        raise AdapterError("pinned SWE runtime no_build_image must be boolean")
    try:
        image_evidence = live.build_pinned_image(
            output_dir,
            image,
            base_image,
            skip_build=no_build_image,
        )
    except (live.ValidationError, OSError, ValueError) as exc:
        raise AdapterError(f"pinned Docker image validation failed: {type(exc).__name__}: {exc}") from exc

    _write_json(
        output_dir / "swe_runtime.json",
        {
            "schema_version": "assignment.fixed-work-adapter.swe-runtime.v1",
            "swe_agent_root": str(swe_root),
            "swe_agent_commit": agent_commit,
            "swe_agent_version": sweagent.__version__,
            "swe_rex_version": swerex.__version__,
            "base_image": base_image,
            "image": image_evidence,
            "no_build_image": no_build_image,
            "runtime_construction": "SWEEnv + DockerDeployment + SWE-ReX persistent bash",
            "action_driver": "check_persistent_shell_capture.run_action",
        },
    )
    return {
        "live": live,
        "SWEEnv": SWEEnv,
        "DockerDeploymentConfig": DockerDeploymentConfig,
        "DockerDeployment": DockerDeployment,
        "SWEAgentEnvironmentTelemetryHook": SWEAgentEnvironmentTelemetryHook,
        "SWEAgentTelemetryHook": SWEAgentTelemetryHook,
        "TelemetryV2": TelemetryV2,
        "UploadRequest": UploadRequest,
        "ProbeAgent": live.ProbeAgent,
        "image": image,
        "image_evidence": image_evidence,
        "swe_root": swe_root,
        "agent_commit": agent_commit,
        "sweagent_version": sweagent.__version__,
        "swerex_version": swerex.__version__,
    }


def _upload_fixture_scratch(
    env: Any,
    scratch_dir: Path,
    fixture: Fixture,
    repeat: int,
    timeout_seconds: float,
    upload_request_type: Any | None = None,
) -> str:
    """Copy the immutable orchestrator snapshot into the running container."""

    try:
        if upload_request_type is None:
            from swerex.runtime.abstract import UploadRequest
        else:
            UploadRequest = upload_request_type

        target_digest = _sha_bytes(
            f"{fixture.case_id}:{repeat}:{fixture.snapshot_sha256}".encode("utf-8")
        )[:24]
        target = f"/tmp/fixed-work-{target_digest}"
        asyncio.run(
            env.deployment.runtime.upload(
                UploadRequest(source_path=str(scratch_dir), target_path=target)
            )
        )
        env.communicate(
            "cd " + shlex.quote(target),
            timeout=timeout_seconds,
            check="raise",
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise AdapterError(f"Docker fixture scratch upload/setup failed: {type(exc).__name__}: {exc}") from exc
    return target


class _NoTelemetryHook:
    """The same reviewed action driver with every telemetry callback disabled."""

    _tool_span = None
    _runtime_command = None
    _runtime_exit_code = None
    _runtime_exit_code_observed = False
    _runtime_timeout = False
    _runtime_error = None
    _work_collector_event_id = None

    def on_step_start(self) -> None:
        return

    def on_actions_generated(self, *, step: Mapping[str, Any]) -> None:
        del step

    def on_action_started(self, *, step: Mapping[str, Any]) -> None:
        del step

    def on_action_executed(self, *, step: Mapping[str, Any]) -> None:
        del step

    def on_step_done(self, *, step: Mapping[str, Any], info: Mapping[str, Any]) -> None:
        del step, info


def _run_sweenv_actions(
    live: Any,
    env: Any,
    hook: Any,
    fixture: Fixture,
    output_dir: Path,
    timeout_seconds: float,
    runtime_config: Mapping[str, Any],
) -> list[dict[str, Any]]:
    action_timeout = _validate_timeout(
        runtime_config.get("action_timeout_seconds", 25.0),
        "SWEEnv action timeout",
    )
    action_timeout = min(action_timeout, timeout_seconds)
    records: list[dict[str, Any]] = []
    output_manifest: list[dict[str, Any]] = []
    for action in fixture.actions:
        record = live.run_action(
            label=action.event_id,
            command=action.command,
            expected_status=action.expected_status,
            env=env,
            hook=hook,
            output=output_dir,
            timeout=action_timeout,
            check="warn",
        )
        if record.get("command") != action.command:
            raise AdapterError(f"SWEEnv action {action.index} command changed in the action driver")
        stdout = record.get("stdout")
        if not isinstance(stdout, str):
            raise AdapterError(f"SWEEnv action {action.index} returned no observed output string")
        stdout_path = output_dir / "actions" / f"{action.index:04d}.stdout"
        _write_bytes(stdout_path, stdout.encode("utf-8"))
        output_manifest.append(
            {
                "index": action.index,
                "event_id": action.event_id,
                "stdout_path": str(stdout_path.relative_to(output_dir)),
                "stdout_sha256": _sha_bytes(stdout.encode("utf-8")),
                "stdout_bytes": len(stdout.encode("utf-8")),
                "error_observed": record.get("exception"),
            }
        )
        records.append(record)
    _write_json(
        output_dir / "actions" / "output_manifest.json",
        {"schema_version": "assignment.fixed-work-adapter.action-output.v1", "actions": output_manifest},
    )
    return records


def _validate_script_hook_rows(telemetry_dir: Path, lifecycle_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    reads = [
        row
        for row in lifecycle_rows
        if row.get("event_kind") == "script_read" and row.get("terminal") is True
    ]
    evidence: list[dict[str, Any]] = []
    previous_generation = 0
    for index, row in enumerate(reads):
        if row.get("status") != "success" or row.get("availability") != "measured":
            raise AdapterError(f"script hook row {index} is not a measured success")
        if row.get("script_read_count") != 1:
            raise AdapterError(f"script hook row {index} has no single native read witness")
        cwd = row.get("script_container_cwd")
        if not isinstance(cwd, str) or not cwd.startswith("/") or "\x00" in cwd:
            raise AdapterError(f"script hook row {index} has no absolute container cwd witness")
        state = row.get("script_state")
        if not isinstance(state, Mapping) or state.get("status") != "known":
            raise AdapterError(f"script hook row {index} has no known script state")
        generation = state.get("generation")
        if not isinstance(generation, int) or generation <= previous_generation:
            raise AdapterError(f"script hook state generation did not advance at row {index}")
        previous_generation = generation
        source_event_id = state.get("source_event_id")
        if not isinstance(source_event_id, str) or not source_event_id:
            raise AdapterError(f"script hook row {index} has no source BPF event identity")
        witness = row.get("script_cwd_witness")
        if not isinstance(witness, Mapping) or witness.get("status") != "measured":
            raise AdapterError(f"script hook row {index} cwd witness is not measured")
        witness_source = witness.get("source")
        if witness_source == "bpf_service_procfs":
            # Service procfs witness: no shell action exists for the BPF join,
            # so the namespace proof must be bound in the witness itself.
            snapshot = witness.get("service_snapshot")
            if (
                not isinstance(snapshot, Mapping)
                or snapshot.get("status") != "measured"
                or snapshot.get("container_cwd") != cwd
                or not isinstance(snapshot.get("namespace_proof"), Mapping)
                or not isinstance(snapshot.get("identity"), Mapping)
            ):
                raise AdapterError(f"script hook row {index} procfs cwd witness lacks a bound namespace proof")
        elif witness_source != "swerex_pwd":
            raise AdapterError(f"script hook row {index} has unknown cwd witness source {witness_source!r}")
        paths = state.get("paths")
        if not isinstance(paths, list) or not paths:
            raise AdapterError(f"script hook row {index} has no observed path descriptors")
        descriptors: list[dict[str, Any]] = []
        for path_index, descriptor in enumerate(paths):
            if not isinstance(descriptor, Mapping):
                raise AdapterError(f"script hook row {index} path {path_index} is not an object")
            path_value = descriptor.get("path")
            digest = descriptor.get("sha256")
            size = descriptor.get("size_bytes")
            if (
                not isinstance(path_value, str)
                or not path_value.startswith("/")
                or "\x00" in path_value
                or not isinstance(digest, str)
                or len(digest) != 64
                or not isinstance(size, int)
                or size < 0
            ):
                raise AdapterError(f"script hook row {index} path {path_index} identity is incomplete")
            artifact = descriptor.get("content_artifact")
            if not isinstance(artifact, Mapping):
                raise AdapterError(f"script hook row {index} path {path_index} has no content artifact")
            artifact_path = _telemetry_artifact(
                telemetry_dir,
                artifact.get("artifact_path"),
                f"script hook row {index} path {path_index}",
            )
            content = artifact_path.read_bytes()
            observed_digest = _sha_bytes(content)
            if (
                observed_digest != digest
                or len(content) != size
                or artifact.get("sha256") != digest
                or artifact.get("size_bytes") != size
                or artifact.get("truncated") is not False
            ):
                raise AdapterError(f"script hook row {index} path {path_index} content artifact differs from state")
            if artifact.get("hash_basis") != "decoded_text_utf8_reencoding" or artifact.get("byte_exact") is not False:
                raise AdapterError(f"script hook row {index} path {path_index} has unbound artifact provenance")
            descriptors.append(
                {
                    "path": path_value,
                    "sha256": digest,
                    "size_bytes": size,
                    "artifact_path": str(artifact_path.relative_to(telemetry_dir)),
                    "artifact_bytes": len(content),
                }
            )
        evidence.append(
            {
                "label": str(row.get("event_id")),
                "event_id": row.get("event_id"),
                "source_event_id": source_event_id,
                "generation": generation,
                "container_cwd": cwd,
                "cwd_witness_source": witness_source,
                "cwd_witness_shell_action": witness_source == "swerex_pwd",
                "paths": descriptors,
            }
        )
    return {"count": len(evidence), "reads": evidence}


def _validate_container_resource(
    snapshot: Mapping[str, Any] | None,
    identity: Mapping[str, Any],
    label: str,
) -> dict[str, Any]:
    if not isinstance(snapshot, Mapping):
        raise AdapterError(f"BPF boundary has no {label} snapshot")
    resource = snapshot.get("container_resources")
    if not isinstance(resource, Mapping):
        raise AdapterError(f"BPF boundary {label} has no container resource sample")
    if resource.get("schema_version") != "assignment.container-resources.v1":
        raise AdapterError(f"BPF boundary {label} has an unknown container resource schema")
    if resource.get("status") != "measured":
        raise AdapterError(f"BPF boundary {label} container resource sample is unavailable")
    if resource.get("target_pid") != identity.get("pid"):
        raise AdapterError(f"BPF boundary {label} cgroup target PID is not the mapped process")
    if resource.get("target_start_ticks") != identity.get("start_ticks"):
        raise AdapterError(f"BPF boundary {label} cgroup target start identity changed")
    if resource.get("boot_id") != identity.get("boot_id"):
        raise AdapterError(f"BPF boundary {label} cgroup boot identity changed")
    membership = resource.get("cgroup_membership")
    cgroup_path = resource.get("cgroup_path")
    if not isinstance(membership, str) or not membership.startswith("0::/"):
        raise AdapterError(f"BPF boundary {label} has no unified cgroup membership")
    if not isinstance(cgroup_path, str) or not cgroup_path.startswith("/sys/fs/cgroup/"):
        raise AdapterError(f"BPF boundary {label} cgroup path is not mounted and bound")
    for key in ("cgroup_device", "cgroup_inode"):
        value = resource.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise AdapterError(f"BPF boundary {label} has no stable {key}")
    started = resource.get("started_monotonic_ns")
    ended = resource.get("ended_monotonic_ns")
    if (
        isinstance(started, bool)
        or not isinstance(started, int)
        or isinstance(ended, bool)
        or not isinstance(ended, int)
        or ended < started
    ):
        raise AdapterError(f"BPF boundary {label} cgroup sample bracket is invalid")
    files = resource.get("files")
    host_context = resource.get("host_context")
    if not isinstance(files, Mapping) or not isinstance(host_context, Mapping):
        raise AdapterError(f"BPF boundary {label} resource context is incomplete")
    for name in REQUIRED_CGROUP_CONTEXT_FILES:
        value = files.get(name)
        if not isinstance(value, Mapping) or not isinstance(value.get("raw"), str):
            raise AdapterError(f"BPF boundary {label} lacks raw cgroup {name}")
        raw = value["raw"]
        try:
            raw_digest = _sha_bytes(raw.encode("ascii"))
        except UnicodeEncodeError as exc:
            raise AdapterError(f"BPF boundary {label} cgroup {name} is not ASCII kernel text") from exc
        if value.get("sha256") != raw_digest:
            raise AdapterError(f"BPF boundary {label} cgroup {name} hash is invalid")
    cpu_stat = files["cpu.stat"]["raw"]
    usage_usec: int | None = None
    for line in cpu_stat.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0] == "usage_usec":
            try:
                usage_usec = int(fields[1])
            except ValueError as exc:
                raise AdapterError(f"BPF boundary {label} cgroup cpu.stat usage is invalid") from exc
            break
    if usage_usec is None or usage_usec < 0:
        raise AdapterError(f"BPF boundary {label} cgroup cpu.stat has no non-negative usage_usec")
    for name in REQUIRED_HOST_PRESSURE_FILES:
        if name not in host_context:
            raise AdapterError(f"BPF boundary {label} lacks host pressure context {name}")
    return {
        "status": resource.get("status"),
        "cgroup_membership": membership,
        "cgroup_path": cgroup_path,
        "cgroup_device": resource.get("cgroup_device"),
        "cgroup_inode": resource.get("cgroup_inode"),
        "usage_usec": usage_usec,
        "files": sorted(str(name) for name in files),
        "host_context": sorted(str(name) for name in host_context),
        "started_monotonic_ns": started,
        "ended_monotonic_ns": ended,
    }


def _validate_container_context(
    linux_dir: Path,
    bpf_event_ids: Sequence[str],
) -> dict[str, Any]:
    summary = _read_json(linux_dir / "work_summary.json", "BPF work summary")
    manifest = _read_json(linux_dir / "bpf_collector_manifest.json", "BPF collector manifest")
    identity = manifest.get("identity")
    if not isinstance(identity, Mapping):
        raise AdapterError("BPF collector manifest has no identity for cgroup binding")
    by_event: dict[str, Mapping[str, Any]] = {}
    actions = summary.get("actions")
    if not isinstance(actions, list):
        raise AdapterError("BPF work summary has no action rows for cgroup binding")
    for item in actions:
        raw = item.get("raw") if isinstance(item, Mapping) else None
        boundary = raw.get("boundary") if isinstance(raw, Mapping) else None
        event_id = boundary.get("event_id") if isinstance(boundary, Mapping) else None
        if isinstance(event_id, str) and isinstance(raw, Mapping):
            by_event[event_id] = raw
    finalizations = summary.get("action_finalizations", [])
    if not isinstance(finalizations, list):
        raise AdapterError("BPF work summary finalizations are not a list")
    for raw in finalizations:
        boundary = raw.get("boundary") if isinstance(raw, Mapping) else None
        event_id = boundary.get("event_id") if isinstance(boundary, Mapping) else None
        if isinstance(event_id, str) and isinstance(raw, Mapping):
            by_event[event_id] = raw
    evidence: dict[str, Any] = {}
    for event_id in bpf_event_ids:
        raw = by_event.get(event_id)
        if not isinstance(raw, Mapping):
            raise AdapterError(f"BPF summary is missing cgroup context for action {event_id}")
        boundary = raw.get("boundary")
        if not isinstance(boundary, Mapping):
            raise AdapterError(f"BPF action {event_id} has no completed boundary")
        start_mono = boundary.get("start_mono_ns")
        end_mono = boundary.get("end_mono_ns")
        if (
            isinstance(start_mono, bool)
            or not isinstance(start_mono, int)
            or isinstance(end_mono, bool)
            or not isinstance(end_mono, int)
            or end_mono < start_mono
        ):
            raise AdapterError(f"BPF action {event_id} has an invalid action interval")
        start = _validate_container_resource(boundary.get("start_snapshot"), identity, f"start/{event_id}")
        end = _validate_container_resource(boundary.get("end_snapshot"), identity, f"end/{event_id}")
        for key in ("cgroup_membership", "cgroup_path", "cgroup_device", "cgroup_inode"):
            if start[key] != end[key]:
                raise AdapterError(f"BPF action {event_id} changed cgroup binding between boundaries")
        if start["usage_usec"] > end["usage_usec"]:
            raise AdapterError(f"BPF action {event_id} cgroup CPU usage decreased")
        evidence[event_id] = {
            "action_start_mono_ns": start_mono,
            "action_end_mono_ns": end_mono,
            "start": start,
            "end": end,
            "cpu_delta_usec": end["usage_usec"] - start["usage_usec"],
            "cgroup_binding_stable": True,
        }
    return {
        "schema_version": "assignment.fixed-work-adapter.container-context.v1",
        "sample_count": len(evidence),
        "samples": evidence,
        "all_samples_measured": True,
        "all_cgroup_bindings_stable": True,
    }


def _validate_full_sweenv_capture(
    fixture: Fixture,
    output_dir: Path,
    action_records: list[dict[str, Any]],
    runtime: Mapping[str, Any],
    repeat: int,
) -> tuple[dict[str, int], dict[str, Any]]:
    telemetry_dir = output_dir / "telemetry"
    linux_dir = output_dir / "linux_work"
    lifecycle_rows = _read_jsonl(telemetry_dir / "lifecycle_events.jsonl", "SWEEnv lifecycle journal")
    tool_rows = _read_jsonl(telemetry_dir / "tool_events.jsonl", "SWEEnv tool journal")
    manifest = _read_json(telemetry_dir / "telemetry_manifest.json", "SWEEnv telemetry manifest")
    if manifest.get("schema_version") != "assignment.telemetry.v2.manifest":
        raise AdapterError("SWEEnv telemetry manifest schema is not v2")
    if manifest.get("case_id") != fixture.case_id:
        raise AdapterError("SWEEnv telemetry manifest case identity differs from fixture")
    expected_run_id = f"fixed-work-{fixture.case_id}-repeat-{repeat:03d}"
    if manifest.get("run_id") != expected_run_id:
        raise AdapterError("SWEEnv telemetry manifest run identity differs from the fixed condition")
    if manifest.get("attempt_id") != f"repeat-{repeat:03d}":
        raise AdapterError("SWEEnv telemetry manifest attempt identity differs from the fixed condition")
    for event_kind, phase in (
        ("deployment_start", "startup"),
        ("setup", "setup"),
        ("teardown", "teardown"),
        ("outer_swe_agent", "outer_swe_agent"),
    ):
        rows = [
            row
            for row in lifecycle_rows
            if row.get("event_kind") == event_kind
            and row.get("phase") == phase
            and row.get("terminal") is True
        ]
        if len(rows) != 1 or rows[0].get("status") != "success" or rows[0].get("availability") != "measured":
            raise AdapterError(f"SWEEnv lifecycle is missing one measured successful {event_kind} terminal")
    try:
        tool_evidence = runtime["live"].validate_tool_rows(tool_rows, action_records)
    except runtime["live"].ValidationError as exc:
        raise AdapterError(f"SWEEnv hook/tool journal validation failed: {exc}") from exc
    for record in action_records:
        if not isinstance(record.get("pre_event_id"), str) or not record.get("pre_event_id"):
            raise AdapterError(f"SWEEnv action {record.get('label')} has no measured pre-event identity")
        if record.get("callback_error") is not None:
            raise AdapterError(f"SWEEnv action {record.get('label')} has a callback error")
    script_evidence = _validate_script_hook_rows(telemetry_dir, lifecycle_rows)
    try:
        bpf_evidence = runtime["live"].validate_bpf(
            linux_dir,
            telemetry_dir,
            tool_rows,
            script_evidence,
            action_records,
            expected_case_id=fixture.case_id,
        )
    except runtime["live"].ValidationError as exc:
        raise AdapterError(f"SWEEnv hook/BPF identity join failed: {exc}") from exc
    event_ids = [str(item["event_id"]) for item in bpf_evidence.get("actions", [])]
    if not event_ids:
        raise AdapterError("SWEEnv hook/BPF validation produced no measured action identities")
    if len(set(event_ids)) != len(event_ids):
        raise AdapterError("SWEEnv hook/BPF validation duplicated an action identity")
    container_evidence = _validate_container_context(linux_dir, event_ids)
    record_count = sum(
        int(item.get("token_record_count", 0))
        for item in bpf_evidence.get("actions", [])
        if isinstance(item, Mapping)
    )
    if record_count <= 0:
        raise AdapterError("SWEEnv hook/BPF validation decoded no individual CPU records")
    capture = {
        "full_production_capture_enabled": True,
        "individual_cpu_operation_records": record_count,
        "physical_requests": 0,
        "raw_model_request_records": 0,
        "dropped_cpu_records": 0,
        "cpu_capture_map_failures": 0,
        "missing_raw_request_bodies": 0,
    }
    evidence = {
        "schema_version": "assignment.fixed-work-adapter.full-capture-audit.v1",
        "runtime": {
            "swe_agent_commit": runtime["agent_commit"],
            "swe_agent_version": runtime["sweagent_version"],
            "swe_rex_version": runtime["swerex_version"],
            "image": runtime["image_evidence"],
        },
        "hook": {
            "lifecycle_terminal_count": sum(1 for row in lifecycle_rows if row.get("terminal") is True),
            "tool_evidence": tool_evidence,
            "script_evidence": script_evidence,
        },
        "bpf": bpf_evidence,
        "container_resources": container_evidence,
        "capture": capture,
        "full_capture_requires": [
            "pinned SWEEnv Docker runtime",
            "measured lifecycle and tool callback journals",
            "lossless hook-to-BPF action identity joins",
            "measured cgroup CPU/io/quota/pressure start/end brackets",
            "collector stop and telemetry durable close before work end",
        ],
    }
    _write_json(output_dir / "production_capture_audit.json", evidence)
    return capture, evidence


def _cpu_condition_sweenv(
    fixture: Fixture,
    mode: str,
    scratch_dir: Path,
    output_dir: Path,
    repeat: int,
    timeout_seconds: float,
    runtime_config: Mapping[str, Any],
) -> dict[str, Any]:
    from agentic_sim.telemetry import cpu_policy

    startup_started = time.perf_counter_ns()
    placement = cpu_policy.from_environment()
    if runtime_config.get("cpu_policy_required") is True and placement is None:
        raise AdapterError("fixture requires the runtime-policy owner's hash-bound CPU placement")
    if placement is not None:
        expected_control = cpu_policy.cpu_set(placement["control_cpuset"])
        if os.sched_getaffinity(0) != expected_control:
            raise AdapterError("adapter controller affinity differs from the bound CPU policy")
    _parse_requests(fixture, model=False)
    cpu_config = _cpu_config(fixture)
    _require_action_capture(fixture, mode, cpu_config)
    if mode == "instrument_on" and cpu_config is not None and any(
        key in cpu_config for key in ("target", "pid", "host_pid", "container_pid")
    ):
        raise AdapterError(
            f"{fixture.case_id}: full Docker hook capture refuses an explicit collector target"
        )
    collector_timeout_value = (cpu_config or {}).get(
        "startup_timeout_s",
        (cpu_config or {}).get("startup_timeout_seconds", 20.0),
    )
    collector_timeout = _validate_timeout(collector_timeout_value, "CPU collector startup timeout")
    collector_python = (cpu_config or {}).get("python_executable", "/usr/bin/python3")
    if not isinstance(collector_python, str) or not collector_python.strip() or "\x00" in collector_python:
        raise AdapterError("CPU collector python_executable is invalid")
    runtime = _load_pinned_swe_runtime(runtime_config, output_dir)
    live = runtime["live"]
    run_id = f"fixed-work-{fixture.case_id}-repeat-{repeat:03d}"
    attempt_id = f"repeat-{repeat:03d}"
    telemetry = None
    environment_hook = None
    hook = _NoTelemetryHook()
    env = None
    env_started = False
    telemetry_started = False
    action_records: list[dict[str, Any]] = []
    startup_wall_ms: float | None = None
    work_started: int | None = None
    work_ended: int | None = None
    run_error: BaseException | None = None
    cleanup_error: BaseException | None = None
    socket_directory: Path | None = None
    try:
        if mode == "instrument_on":
            # UNIX socket names have a short kernel limit. Durable artifacts
            # stay under output_dir even for deeply nested submission paths.
            socket_directory = Path(tempfile.mkdtemp(prefix="fw-bpf-"))
            telemetry = runtime["TelemetryV2"](
                output_dir / "telemetry",
                run_id=run_id,
                attempt_id=attempt_id,
                case_id=fixture.case_id,
                instance_id="docker-persistent-bash",
                model=None,
                model_revision=None,
            )
            environment_hook = runtime["SWEAgentEnvironmentTelemetryHook"](telemetry)
        deployment = runtime["DockerDeployment"].from_config(
            runtime["DockerDeploymentConfig"](
                image=runtime["image"],
                pull="never",
                remove_container=True,
                remove_images=False,
                python_standalone_dir=None,
                **({"docker_args": cpu_policy.worker_docker_args(placement, [])} if placement else {}),
            )
        )
        env = runtime["SWEEnv"](deployment=deployment, repo=None, post_startup_commands=[])
        if environment_hook is not None:
            env.add_hook(environment_hook)
        agent = runtime["ProbeAgent"](env)
        if mode == "instrument_on":
            hook = runtime["SWEAgentTelemetryHook"](telemetry)
        with _temporary_environment(
            {
                "ASSIGNMENT_TELEMETRY_V2_REQUIRED": "1" if mode == "instrument_on" else None,
                "ASSIGNMENT_TELEMETRY_V2_CPU_COLLECTOR_CONFIG": (
                    json.dumps(
                        {
                            **dict(cpu_config or {}),
                            "backend": "bcc",
                            "attach_existing_process": True,
                            "require_persistent_runtime_pid": True,
                            "trace_format": "bcc raw individual syscall and process events plus action aggregates v2",
                            "output_dir": str(output_dir / "linux_work"),
                            "socket_path": str(socket_directory / "collector.sock"),
                            "session": "default",
                            "startup_timeout_s": collector_timeout,
                            "python_executable": collector_python,
                        },
                        sort_keys=True,
                    )
                    if mode == "instrument_on"
                    else None
                ),
            }
        ):
            env.start()
            env_started = True
            container_name = getattr(deployment, "container_name", None)
            if not isinstance(container_name, str) or not container_name:
                raise AdapterError("SWE-ReX Docker deployment did not expose a container name")
            _write_json(
                output_dir / "docker_container_inspect_before_close.json",
                live.container_inspect(container_name) or {},
            )
            if placement is not None:
                inspection = live.container_inspect(container_name) or {}
                inspect_rows = inspection.get("inspect")
                if (inspection.get("inspect_returncode") != 0 or not isinstance(inspect_rows, list)
                        or len(inspect_rows) != 1 or not isinstance(inspect_rows[0], Mapping)):
                    raise AdapterError("Docker helper did not return one successful daemon inspection")
                inspected = inspect_rows[0]
                verified = cpu_policy.verify_container(inspected, placement["worker_cpuset"])
                init_pid = inspected.get("State", {}).get("Pid")
                if not isinstance(init_pid, int) or init_pid <= 0:
                    raise AdapterError("placed Docker container has no actual host init PID")
                actual_affinity = os.sched_getaffinity(init_pid)
                if actual_affinity != cpu_policy.cpu_set(placement["worker_cpuset"]):
                    raise AdapterError("Docker host process affinity differs from the worker CPU policy")
                _write_json(output_dir / "cpu_placement.json", {
                    "status": "measured", "policy": placement, "container": verified,
                    "container_host_init_pid": init_pid, "container_host_init_affinity": sorted(actual_affinity),
                    "controller_pid": os.getpid(), "controller_affinity": sorted(os.sched_getaffinity(0)),
                    "runtime_manifest_sha256": os.environ[cpu_policy.RUNTIME_SHA_ENV],
                })
            container_scratch = _upload_fixture_scratch(
                env,
                scratch_dir,
                fixture,
                repeat,
                timeout_seconds,
                runtime["UploadRequest"],
            )
            _write_json(
                output_dir / "container_fixture_witness.json",
                {
                    "container_name": container_name,
                    "container_scratch": container_scratch,
                    "uploaded_source_snapshot_sha256": fixture.snapshot_sha256,
                    "input_transfer": "SWE-ReX UploadRequest",
                    "action_count": len(fixture.actions),
                },
            )
            if mode == "instrument_on":
                hook.on_init(agent=agent)
                hook.on_run_start()
                telemetry_started = True
                hook.on_setup_attempt()
                discovered_target = getattr(env, "_assignment_v2_process_target", None)
                if not isinstance(discovered_target, Mapping):
                    raise AdapterError("required collector did not persist the Docker shell target mapping")
                if discovered_target.get("mapping_source") != "swerex_docker_persistent_bash_nspid":
                    raise AdapterError("required collector did not use the Docker persistent-shell mapping")
                hook.on_setup_done()
            else:
                # The off condition uses the identical container construction,
                # uploaded snapshot, and action driver, while emitting no v2
                # telemetry or serving/capture configuration.
                hook = _NoTelemetryHook()
            startup_wall_ms = (time.perf_counter_ns() - startup_started) / 1_000_000
            work_started = time.perf_counter_ns()
            action_records = _run_sweenv_actions(
                live,
                env,
                hook,
                fixture,
                output_dir,
                timeout_seconds,
                runtime_config,
            )
    except BaseException as exc:  # noqa: BLE001 - preserve failure for durable cleanup and blocked output
        run_error = exc
    finally:
        if env_started and env is not None:
            try:
                _write_json(
                    output_dir / "docker_container_inspect_pre_shutdown.json",
                    live.container_inspect(getattr(env.deployment, "container_name", None)) or {},
                )
            except BaseException as exc:
                cleanup_error = exc
            try:
                env.close()
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
        if telemetry_started and telemetry is not None:
            try:
                outer = getattr(telemetry, "_outer", None)
                if outer is not None and not outer.closed:
                    telemetry.finish_outer(
                        status="failure" if run_error or cleanup_error else "success",
                        error_type=type(run_error or cleanup_error).__name__
                        if run_error or cleanup_error
                        else None,
                        error_message=str(run_error or cleanup_error)[:512]
                        if run_error or cleanup_error
                        else None,
                    )
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
        if socket_directory is not None:
            try:
                socket_directory.rmdir()
            except OSError as exc:
                cleanup_error = cleanup_error or exc
        work_ended = time.perf_counter_ns()
    if run_error is not None:
        raise AdapterError(f"Docker SWEEnv fixed-work execution failed: {type(run_error).__name__}: {run_error}") from run_error
    if cleanup_error is not None:
        raise AdapterError(f"Docker SWEEnv fixed-work cleanup failed: {type(cleanup_error).__name__}") from cleanup_error
    if startup_wall_ms is None or work_started is None or work_ended is None:
        raise AdapterError("Docker SWEEnv condition did not establish startup/work boundaries")
    if mode == "instrument_on":
        capture, _evidence = _validate_full_sweenv_capture(
            fixture, output_dir, action_records, runtime, repeat
        )
    else:
        if (output_dir / "telemetry").exists() or (output_dir / "linux_work").exists():
            raise AdapterError("instrument_off Docker condition emitted telemetry or BPF capture files")
        capture = {
            "full_production_capture_enabled": False,
            "individual_cpu_operation_records": 0,
            "physical_requests": 0,
            "raw_model_request_records": 0,
            "dropped_cpu_records": 0,
            "cpu_capture_map_failures": 0,
            "missing_raw_request_bodies": 0,
        }
        _write_json(
            output_dir / "action_capture_audit.json",
            {
                "schema_version": "assignment.fixed-work-adapter.off-capture-audit.v1",
                "capture": capture,
                "telemetry_emitted": False,
                "actions": action_records,
            },
        )
    result_capture = {
        "full_production_capture_enabled": capture["full_production_capture_enabled"],
        "individual_cpu_operation_records": capture["individual_cpu_operation_records"],
        "physical_requests": 0,
        "raw_model_request_records": 0,
        "dropped_cpu_records": capture["dropped_cpu_records"],
        "cpu_capture_map_failures": capture["cpu_capture_map_failures"],
        "missing_raw_request_bodies": 0,
    }
    _write_json(
        output_dir / "action_capture_audit.json",
        {"actions": action_records, "capture": result_capture},
    )
    return _base_result(
        fixture,
        repeat,
        mode,
        output_tokens=0,
        output_provenance="no_model_requests",
        capture=result_capture,
        work_wall_ms=(work_ended - work_started) / 1_000_000,
        startup_wall_ms=startup_wall_ms,
    )


def _launch_cpu_service(
    fixture: Fixture,
    process: Any,
    output_dir: Path,
    repeat: int,
    scratch_dir: Path,
    config: Mapping[str, Any],
) -> Any:
    target = ProcessTarget(
        pid=process.pid,
        run_id=f"fixed-work:{fixture.case_id}",
        attempt_id=f"repeat-{repeat:03d}",
        case_id=fixture.case_id,
        mapping_source="fixed_work_adapter_persistent_bash",
    )
    bpf_dir = output_dir / "bpf"
    socket_path = bpf_dir / "collector.sock"
    bpf_dir.mkdir(parents=True, exist_ok=False)
    startup_timeout = _validate_timeout(config.get("startup_timeout_seconds", 15.0), "CPU collector startup timeout")
    executable = config.get("python_executable")
    if executable is not None and (not isinstance(executable, str) or not executable.strip()):
        raise AdapterError("CPU collector python_executable is invalid")
    try:
        service = launch_bpf_work_service(
            target,
            socket_path=socket_path,
            trace_dir=bpf_dir,
            python_executable=executable or sys.executable,
            cwd=scratch_dir,
            startup_timeout_s=startup_timeout,
            force=False,
        )
        service.client.ping()
        return service
    except (BpfAttachError, BpfProtocolError, OSError) as exc:
        raise AdapterError(f"BPF service is unavailable: {type(exc).__name__}: {exc}") from exc


def _base_result(fixture: Fixture, repeat: int, mode: str, *, output_tokens: int, output_provenance: str, capture: Mapping[str, Any], work_wall_ms: float, startup_wall_ms: float) -> dict[str, Any]:
    result = {
        "schema_version": RESULT_SCHEMA,
        "case_id": fixture.case_id,
        "repeat": repeat,
        "instrumentation_mode": mode,
        "status": "completed",
        "workload_sha256": fixture.workload_sha256,
        "pretrajectory_snapshot_sha256": fixture.snapshot_sha256,
        "action_sequence_sha256": fixture.action_sha256,
        "request_sequence_sha256": fixture.request_sha256,
        "output_token_count": output_tokens,
        "evidence_provenance": "measured_fixed_workload",
        "output_token_provenance": output_provenance,
        "capture": dict(capture),
        "serving_and_cache_policy_sha256": fixture.policy_sha256,
        "work_wall_ms": work_wall_ms,
        "startup_wall_ms": startup_wall_ms,
    }
    if set(result) != {
        "schema_version", "case_id", "repeat", "instrumentation_mode", "status",
        "workload_sha256", "pretrajectory_snapshot_sha256", "action_sequence_sha256",
        "request_sequence_sha256", "output_token_count", "evidence_provenance",
        "output_token_provenance", "capture", "serving_and_cache_policy_sha256",
        "work_wall_ms", "startup_wall_ms",
    }:
        raise AdapterError("internal result schema construction error")
    return result


def _cpu_condition(fixture: Fixture, mode: str, scratch_dir: Path, output_dir: Path, repeat: int, timeout_seconds: float, bpf_launcher: Callable[..., Any] | None = None) -> dict[str, Any]:
    if fixture.kind not in CPU_KINDS:
        raise AdapterError(f"{fixture.case_id}: CPU adapter received fixture kind {fixture.kind}")
    runtime_config = _swe_runtime_config(fixture)
    if runtime_config is not None:
        return _cpu_condition_sweenv(
            fixture,
            mode,
            scratch_dir,
            output_dir,
            repeat,
            timeout_seconds,
            runtime_config,
        )
    _parse_requests(fixture, model=False)
    config = _cpu_config(fixture)
    _require_action_capture(fixture, mode, config)
    startup_started = time.perf_counter_ns()
    process = _spawn_bash_fixture()
    service = None
    action_audit: list[dict[str, Any]] = []
    work_started = None
    work_ended = None
    capture = {"individual_cpu_operation_records": 0, "dropped_cpu_records": 0, "cpu_capture_map_failures": 0}
    try:
        _prepare_persistent_shell(process, scratch_dir, timeout_seconds)
        if mode == "instrument_on":
            if config is None:
                raise AdapterError(
                    f"{fixture.case_id}: instrument_on requires explicit cpu_collector configuration"
                )
            service = (bpf_launcher or _launch_cpu_service)(fixture, process, output_dir, repeat, scratch_dir, config)
        startup_wall_ms = (time.perf_counter_ns() - startup_started) / 1_000_000
        work_started = time.perf_counter_ns()
        action_audit = _run_actions(process, fixture.actions, output_dir, timeout_seconds, service.client if service is not None else None)
    finally:
        cleanup_error: BaseException | None = None
        if service is not None:
            try:
                service.stop()
            except BaseException as exc:
                cleanup_error = exc
        try:
            _stop_bash_fixture(process)
        except BaseException as exc:
            cleanup_error = cleanup_error or exc
        if callable(getattr(process, "poll", None)) and process.poll() is None:
            cleanup_error = cleanup_error or AdapterError("persistent shell did not terminate during condition cleanup")
        work_ended = time.perf_counter_ns()
        if cleanup_error is not None:
            raise AdapterError(f"CPU condition cleanup failed: {type(cleanup_error).__name__}") from cleanup_error
    if mode == "instrument_on":
        capture.update(_bpf_capture_counts(_read_summary(output_dir / "bpf"), output_dir / "bpf", action_audit))
    if work_started is None or work_ended is None:
        raise AdapterError("CPU condition did not establish a measured work interval")
    result_capture = {
        "full_production_capture_enabled": FULL_PRODUCTION_CAPTURE_ENABLED,
        "individual_cpu_operation_records": capture["individual_cpu_operation_records"],
        "physical_requests": 0,
        "raw_model_request_records": 0,
        "dropped_cpu_records": capture["dropped_cpu_records"],
        "cpu_capture_map_failures": capture["cpu_capture_map_failures"],
        "missing_raw_request_bodies": 0,
    }
    result = _base_result(
        fixture,
        repeat,
        mode,
        output_tokens=0,
        output_provenance="no_model_requests",
        capture=result_capture,
        work_wall_ms=(work_ended - work_started) / 1_000_000,
        startup_wall_ms=startup_wall_ms,
    )
    _write_json(
        output_dir / "action_capture_audit.json",
        {
            "actions": action_audit,
            "capture": result_capture,
            "container_resources": {
                "status": "unavailable",
                "reason": (
                    "raw host persistent Bash target has no container PID mapping; "
                    "use the explicit pinned SWEEnv Docker hook path for production capture"
                ),
            },
        },
    )
    return result


def _request_bytes(row: Request, global_headers: Mapping[str, str]) -> dict[str, str]:
    headers = dict(global_headers)
    for key, value in row.headers.items():
        headers[key] = value
    if not any(key.lower() == "x-eic-logical-request-id" for key in headers):
        headers["X-EIC-Logical-Request-ID"] = row.logical_request_id
    return headers


def _partial_bytes(exc: BaseException) -> bytes | None:
    value = getattr(exc, "partial", None)
    if isinstance(value, bytes):
        return value
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    return None


def _usage(body: bytes) -> int | None:
    return _model_usage(body).get("completion_tokens")


def _usage_mapping(body: bytes) -> Mapping[str, Any] | None:
    """Project an explicit usage object from JSON or the final SSE frame."""

    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return None
        usage: Mapping[str, Any] | None = None
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].lstrip()
            if not data or data == "[DONE]":
                continue
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            candidate = event.get("usage") if isinstance(event, Mapping) else None
            if isinstance(candidate, Mapping):
                usage = candidate
        return usage
    usage = value.get("usage") if isinstance(value, Mapping) else None
    return usage if isinstance(usage, Mapping) else None


def _model_usage(body: bytes) -> dict[str, Any]:
    usage = _usage_mapping(body)
    if not isinstance(usage, Mapping):
        return {}
    details = usage.get("prompt_tokens_details")
    counts = {"prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
              "cached_tokens": details.get("cached_tokens") if isinstance(details, Mapping) else None}
    return {key: value if type(value) is int and value >= 0 else None for key, value in counts.items()}


def _model_reference(ref: Any, label: str) -> tuple[Path, dict[str, Any]]:
    if not isinstance(ref, Mapping):
        raise AdapterError(f"{label} requires an independent artifact path and sha256")
    path = _absolute_regular(ref.get("path"), label)
    if _sha_file(path) != _digest(ref.get("sha256"), label + ".sha256"):
        raise AdapterError(f"{label} artifact hash mismatch")
    return path, _read_json(path, label)


def _model_runtime(config: Mapping[str, Any], mode: str, phase: str) -> dict[str, Any]:
    """Check retained procfs observations, not caller-supplied enabled booleans.

    model.server_runtime.{before,after} are {path,sha256} references to a
    procfs report: source='procfs', hostname, boot_id, gpu_uuid,
    observed_monotonic_ns, process={pid,start_ticks,argv,environment}.
    Main owns collecting these and same-GPU restarts. No restart is executed
    here. Keep --enable-prompt-tokens-details on both sides for work equality.
    """
    refs = config.get("server_runtime")
    path, report = _model_reference(refs.get(phase) if isinstance(refs, Mapping) else None,
                                    "server runtime " + phase)
    process = report.get("process", {})
    argv, env = process.get("argv"), process.get("environment")
    if (report.get("source") != "procfs" or not isinstance(argv, list) or not argv
            or any(not isinstance(arg, str) for arg in argv) or not isinstance(env, Mapping)
            or any(not report.get(key) for key in ("hostname", "boot_id", "gpu_uuid"))
            or any(type(process.get(key)) is not int or process[key] <= 0 for key in ("pid", "start_ticks"))
            or type(report.get("observed_monotonic_ns")) is not int):
        raise AdapterError("server runtime lacks actual process/host/boot/GPU observation")
    has_observer = any("serving_observer" in arg or "native_vllm_observer" in arg for arg in argv)
    native = str(env.get("EIC_NATIVE_VLLM_OBSERVER", "")).lower() in ("1", "true", "yes", "on")
    if "--enable-prompt-tokens-details" not in argv:
        raise AdapterError("actual server lacks --enable-prompt-tokens-details reporting")
    if mode == "instrument_off":
        if has_observer or native or any(arg == "--middleware" or arg.startswith("--middleware=") for arg in argv):
            raise AdapterError("instrument_off still has server middleware/native hook; proxy-only off is invalid")
    elif not has_observer or not native:
        raise AdapterError("instrument_on lacks actual server observer/native opt-in")
    return {"path": str(path), "sha256": _sha_file(path), "hostname": report["hostname"],
            "boot_id": report["boot_id"], "gpu_uuid": report["gpu_uuid"],
            "pid": process["pid"], "start_ticks": process["start_ticks"],
            "observed_monotonic_ns": report["observed_monotonic_ns"],
            "argv_sha256": _sha_bytes(_canonical(argv).encode()), "instrumentation_mode": mode,
            "native_hook_sha256": env.get("EIC_NATIVE_VLLM_EXPECTED_HOOK_SHA256"),
            "counter_epoch": env.get("EIC_COUNTER_EPOCH")}


def _model_cache_reset(config: Mapping[str, Any], output_dir: Path, condition_id: str) -> dict[str, Any]:
    recipe = config.get("cache_policy", {}).get("cache_reset", {})
    if (recipe.get("method") != "POST" or recipe.get("path") != "/reset_prefix_cache"
            or recipe.get("phase") != "startup_outside_work" or recipe.get("requests_before_each_condition") != 1):
        raise AdapterError("model condition requires the reviewed single cold-prefix reset recipe")
    request_id = "cold-reset-" + _sha_bytes(condition_id.encode())[:32]
    headers = {key: value for key, value in config["headers"].items()
               if key.lower() not in {"x-request-id", "x-eic-physical-request-id", "content-length"}}
    headers.update({"X-Request-Id": request_id, "X-EIC-Physical-Request-ID": request_id,
                    "X-EIC-Case-ID": condition_id, "Content-Length": "0"})
    raw, status, error = b"", None, None
    connection = http.client.HTTPConnection(config["upstream_host"], config["upstream_port"],
                                            timeout=config["timeout_seconds"])
    try:
        connection.request("POST", "/reset_prefix_cache", body=b"", headers=headers)
        response = connection.getresponse()
        status, raw = response.status, response.read(65537)
        if len(raw) > 65536:
            raise AdapterError("cache reset response exceeds bounded archive size")
    except (OSError, http.client.HTTPException, AdapterError) as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        connection.close()
    _write_bytes(output_dir / "model/cache_reset.response.bin", raw)
    record = {"physical_request_id": request_id, "method": "POST", "path": "/reset_prefix_cache",
              "phase": "startup_outside_work", "request_sha256": _sha_bytes(b""),
              "response_sha256": _sha_bytes(raw), "status_code": status, "error": error,
              "reset_success_proven": False, "proof_required": "request-linked cached_tokens == 0"}
    _write_json(output_dir / "model/cache_reset.json", record)
    if status != 200 or error is not None:
        raise AdapterError("cold-prefix reset HTTP call failed; retained outside measured work")
    return record


def _model_work_proof(fixture: Fixture, request: Request, audit: Mapping[str, Any],
                      response: bytes, native: Mapping[str, Any] | None) -> dict[str, Any]:
    expected_prompt = {"model_short_request": 512, "model_long_context_request": 60000}[fixture.kind]
    body = json.loads(request.body)
    policy = fixture.spec.get("serving_and_cache_policy", {})
    params = {"temperature": 0, "top_p": 1, "seed": 0, "n": 1,
              "stream": False, "max_tokens": 128, "ignore_eos": True}
    if (any(body.get(key) != value for key, value in params.items()) or
            any(policy.get("output_policy", {}).get(key) != value for key, value in params.items()) or
            not policy.get("model_revision") or not policy.get("model") or body.get("model") != policy.get("model")):
        raise AdapterError("model request differs from exact frozen model/revision/sampling/output policy")
    counts = _model_usage(response)
    expected = {"prompt_tokens": expected_prompt, "completion_tokens": 128, "cached_tokens": 0}
    if counts != expected or audit.get("completion_tokens") != 128:
        raise AdapterError(f"actual model usage must equal cold fixed work {expected}; observed {counts}")
    if native is not None:
        if json.loads(response).get("id") != native["target_request"]["engine_request_id"]:
            raise AdapterError("raw API response ID differs from direct native request identity")
        finished = native["native_measurement"]["finished"]
        if (finished.get("num_prompt_tokens") != expected_prompt or finished.get("num_generation_tokens") != 128
                or finished.get("max_tokens_param") != 128):
            raise AdapterError("native FinishedRequestStats token work differs from exact API usage/request parameters")
        native_cached = finished.get("cached_tokens")
        if native_cached is not None and native_cached != expected["cached_tokens"]:
            raise AdapterError("native cache-token work differs from exact API usage/request parameters")
    return {**counts, "parameters": params, "model": body["model"], "model_revision": policy["model_revision"],
            "request_sha256": _sha_bytes(request.body), "response_sha256": _sha_bytes(response),
            "usage_provenance": "retained_vllm_api_response.usage", "cuda_kernel_timing": False}


def _model_server_batch(config: Mapping[str, Any], output_dir: Path):
    """One unchanged batch for all condition requests; shared archives can be reused.

    model.server_archive is {path,sha256} or {fetch:{ssh_host,ssh_control,
    journal,native_journal,remote_python?}}. Fetch is one post-work read, without
    polling. In-memory raw_path remapping lets the existing scrape validator
    inspect archived bytes; original journals/sidecars retain their raw hashes.
    """
    from dataclasses import replace
    import fcntl
    from types import SimpleNamespace
    from scripts.validation import check_live_native_serving as live
    spec = config.get("server_archive", {})
    if "fetch" in spec:
        archive = output_dir / "model/server_batch.tar"
        live.fetch(SimpleNamespace(**{"remote_python": "python3", **spec["fetch"], "output": str(archive)}))
    else:
        archive = _absolute_regular(spec.get("path"), "server archive")
        if _sha_file(archive) != _digest(spec.get("sha256"), "server archive.sha256"):
            raise AdapterError("server archive hash mismatch")
    archive_hash = _sha_file(archive)
    cache = Path(spec.get("archive_cache_dir", archive.parent / "serving_archive_cache"))
    if not cache.is_absolute():
        raise AdapterError("server archive cache directory must be absolute")
    cache.mkdir(parents=True, exist_ok=True)
    root = cache / archive_hash
    # Reuse one hash-bound extraction across cases; no full-prefix copy per case.
    with (cache / (archive_hash + ".lock")).open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if not root.exists():
            manifest = live.unpack_archive(archive, root)
        else:
            with tarfile.open(archive, "r:") as tar:
                manifest_raw = tar.extractfile("manifest.json").read()
            if (root / "manifest.json").read_bytes() != manifest_raw:
                raise AdapterError("shared extraction manifest differs from original archive")
            manifest = json.loads(manifest_raw)
            for original, info in manifest["files"].items():
                path = _absolute_regular(str(live.resolve_artifact(manifest, root, original)), "shared server artifact")
                if _sha_file(path) != info["sha256"] or path.stat().st_size != info["bytes"]:
                    raise AdapterError("shared server artifact hash/size mismatch")
    if manifest.get("complete_jsonl_prefixes") is not True:
        raise AdapterError("server batch contains incomplete journal tails")
    journal = live.resolve_artifact(manifest, root, manifest["journal"])
    native = live.resolve_artifact(manifest, root, manifest["native_journal"])
    derive = live.load_derive()
    data = derive.read_observer_journal(journal)
    records = json.loads(json.dumps(data.records))
    for ref in live.metric_references(records):
        ref["raw_path"] = str(live.resolve_artifact(manifest, root, ref["raw_path"]))
    metrics = derive._metric_evidence(replace(data, records=tuple(records)))
    return derive, data, native, metrics, {"path": str(archive), "sha256": archive_hash,
                                         "resolved_root": str(root), "manifest_sha256": _sha_file(root / "manifest.json")}


def _model_native_results(fixture: Fixture, output_dir: Path, requests: Sequence[Request],
                          audits: Sequence[Mapping[str, Any]], events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Append new evidence only. Failed requests and their native partials stay unavailable."""
    rows, setup_error = [], None
    try:
        config = _model_config(fixture)
        derive, data, native, metrics, archive = _model_server_batch(config, output_dir)
        runtime = _read_json(output_dir / "model/server_runtime.before.json", "server runtime before")
        reset = _read_json(output_dir / "model/cache_reset.json", "cold reset")
        if (data.header["server_pid"] != runtime["pid"] or data.header["server_process_start_ticks"] != runtime["start_ticks"]
                or data.header["clock"]["hostname"] != runtime["hostname"] or data.header["clock"]["boot_id"] != runtime["boot_id"]
                or data.header["counter_epoch"] != runtime["counter_epoch"]
                or data.header["native_observer"]["binding"]["hook_source_sha256"] != runtime["native_hook_sha256"]):
            raise AdapterError("native archive differs from actual runtime process/clock/epoch/source pin")
    except (ValueError, OSError, KeyError, TypeError) as exc:
        setup_error = str(exc)
    telemetry = output_dir / "model/telemetry_v2"
    terminals = [r for r in _jsonl_if_present(telemetry / "model_events.jsonl", "model events") if
                 r.get("terminal") is True and r.get("event_kind") == "model_request"]
    for request, audit, event in zip(requests, audits, events):
        proof = {"index": request.index, "logical_request_id": request.logical_request_id,
                 "status": "unavailable", "native_status": "unavailable", "native": None,
                 "request_sha256": _sha_bytes(request.body), "response_sha256": audit.get("response_sha256"),
                 "original_serving_metrics_status": event.get("serving_metrics_status"),
                 "original_serving_metrics_record": event.get("serving_metrics_record")}
        try:
            candidates = [r for r in terminals if r.get("logical_request_id") == request.logical_request_id]
            if len(candidates) != 1 or not candidates[0].get("physical_request_id"):
                raise AdapterError("model logical ID lacks exactly one retained physical attempt")
            physical = candidates[0]["physical_request_id"]
            proof["physical_request_id"] = physical
            ref = event.get("serving_metrics_record", {})
            record_path = _telemetry_artifact(telemetry, ref.get("path"), "original serving record")
            if _sha_file(record_path) != ref.get("sha256"):
                raise AdapterError("original serving record hash mismatch")
            original = _read_json(record_path, "original serving record")
            if original.get("request_id") != physical or candidates[0].get("request_body_sha256") != _sha_bytes(request.body):
                raise AdapterError("proxy physical ID/request hash differs from original serving record")
            if setup_error:
                raise AdapterError(setup_error)
            if (original.get("server_identity") != data.header["server_identity"] or
                    original.get("counter_epoch") != data.header["counter_epoch"]):
                raise AdapterError("original serving record server/epoch differs from native observer")
            proof["archive"] = archive
            sidecar = derive.derive_server_attribution(journal=data.path, native_journal=native, request_id=physical)
            proof["native"] = sidecar
            proof["native_status"] = sidecar["status"]
            if audit.get("status_code") != 200 or audit.get("response_complete") is not True or audit.get("error"):
                proof["native_status"] = "unavailable"
                raise AdapterError("failed/incomplete model request; native partials retained, no successful timing claim")
            if sidecar["status"] != "measured" or sidecar.get("native_metric_source") != "vllm_v1_finished_request_stats":
                raise AdapterError("direct native attribution unavailable: " + str(sidecar.get("unavailable_reason")))
            target = data.terminals[sidecar["target_request"]["observation_id"]]
            if target["request_body_sha256"] != _sha_bytes(request.body) or target["response_body_sha256"] != audit.get("response_sha256"):
                raise AdapterError("ASGI HTTP body hashes differ from physical proxy request/response")
            attribution_mode = original.get("attribution_mode", "per_request_scrape")
            selected = []
            if attribution_mode == "native_deferred":
                # The proxy performed no per-request scrape by configuration.
                # The native derivation above already required an ASGI and a
                # native completeness watermark past the target terminal and
                # no overlapping ingress; there is no scrape pair to bracket.
                if original.get("capture", {}).get("scrape_count") != 0 or original.get("status") != "unavailable":
                    raise AdapterError("native_deferred original record must be unavailable with zero proxy scrapes")
                scrapes = None
            elif attribution_mode != "per_request_scrape":
                raise AdapterError(f"unknown proxy attribution mode {attribution_mode!r}")
            else:
                for phase in ("before", "after"):
                    snapshot = original["snapshots"][phase]
                    raw = _telemetry_artifact(telemetry, snapshot.get("raw_path"), phase + " raw snapshot")
                    if (snapshot.get("associated_physical_request_id") != physical or snapshot.get("scrape_phase") != phase
                            or _sha_file(raw) != snapshot.get("raw_sha256")):
                        raise AdapterError("original raw scrape lacks exact physical ID/phase/hash binding")
                    matches = [m for m in metrics if m.scrape_id == snapshot.get("scrape_id")]
                    if len(matches) != 1 or matches[0].raw_sha256 != snapshot["raw_sha256"] or matches[0].phase != phase:
                        raise AdapterError("proxy scrape ID/hash/phase lacks one matching server capture")
                    selected.append(matches[0])
                before, after = selected
                if before.ended_ns > target["started_monotonic_ns"] or after.started_ns < target["terminal_monotonic_ns"]:
                    raise AdapterError("raw proxy scrapes do not bracket target in the server clock")
                if not any(m["sequence"] > after.sequence and m["covered_through_monotonic_ns"] >= after.ended_ns for m in data.watermarks):
                    raise AdapterError("after scrape lacks ASGI completeness watermark")
                scrapes = {m.phase: {"scrape_id": m.scrape_id, "raw_sha256": m.raw_sha256,
                                     "server_start_ns": m.started_ns, "server_end_ns": m.ended_ns} for m in selected}
            resets = [r for r in data.terminals.values() if r.get("physical_request_id") == reset["physical_request_id"]]
            if (len(resets) != 1 or resets[0]["route"] != "/reset_prefix_cache" or resets[0]["method"] != "POST"
                    or resets[0]["response_status"] != 200 or resets[0]["response_body_sha256"] != reset["response_sha256"]
                    or resets[0]["terminal_status"] != "complete" or resets[0]["request_body_complete"] is not True
                    or resets[0]["request_body_sha256"] != _sha_bytes(b"") or resets[0]["response_body_complete"] is not True
                    or resets[0].get("client_disconnected") is not False or resets[0].get("error") is not None
                    or resets[0]["terminal_monotonic_ns"] > target["started_monotonic_ns"]):
                raise AdapterError("cold reset lacks exact preceding server HTTP proof")
            for obs, start in data.starts.items():
                if obs in (target["observation_id"], resets[0]["observation_id"]) or start["request_class"] == "observer":
                    continue
                terminal = data.terminals.get(obs)
                if start["started_monotonic_ns"] < target["terminal_monotonic_ns"] and (
                        terminal is None or terminal["terminal_monotonic_ns"] > resets[0]["started_monotonic_ns"]):
                    raise AdapterError("intervening/overlapping ingress prevents cold fixed-work isolation")
            response_path = _under(output_dir / audit["response_path"], output_dir, "model response")
            response = response_path.read_bytes()
            if _sha_bytes(response) != audit["response_sha256"]:
                raise AdapterError("retained response hash mismatch")
            proof.update(status="measured", work=_model_work_proof(fixture, request, audit, response, sidecar),
                         attribution_mode=attribution_mode, scrapes=scrapes)
        except (ValueError, KeyError, OSError, TypeError) as exc:
            proof["unavailable_reason"] = str(exc)
        _append_jsonl(output_dir / "model/native_attribution.jsonl", proof)
        rows.append(proof)
    return rows


def _model_off_results(fixture: Fixture, output_dir: Path, requests: Sequence[Request],
                       audits: Sequence[Mapping[str, Any]]) -> None:
    failures = []
    events = _jsonl_if_present(output_dir / "model/request_events.jsonl", "off proxy events")
    for index, (request, audit) in enumerate(zip(requests, audits)):
        row = {"index": request.index, "logical_request_id": request.logical_request_id,
               "status": "unavailable", "native_status": "unavailable", "native": None,
               "unavailable_reason": "server observer/native hook disabled for actual off condition"}
        try:
            event = events[index] if index < len(events) else {}
            row["physical_request_id"] = event.get("request_id")  # Off proxy uses this exact upstream X-Request-Id.
            if (not event.get("request_id") or event.get("request_sha256") != _sha_bytes(request.body)
                    or event.get("response_sha256") != audit.get("response_sha256")):
                raise AdapterError("off proxy physical ID/hash join is unavailable")
            if audit.get("status_code") != 200 or audit.get("response_complete") is not True or audit.get("error"):
                raise AdapterError("failed/incomplete off request; raw HTTP evidence retained unavailable")
            raw = _under(output_dir / audit["response_path"], output_dir, "off response").read_bytes()
            if _sha_bytes(raw) != audit["response_sha256"]:
                raise AdapterError("off response artifact hash mismatch")
            if json.loads(raw).get("id") != "chatcmpl-" + event["request_id"]:
                raise AdapterError("off API response ID differs from proxy upstream physical ID")
            row["work"] = _model_work_proof(fixture, request, audit, raw, None)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            row["work_unavailable_reason"] = str(exc)
            failures.append(str(exc))
        _append_jsonl(output_dir / "model/native_attribution.jsonl", row)
    if failures:
        raise AdapterError("off model work unavailable: " + failures[0])


def compare_model_work(on_output: Path, off_output: Path) -> dict[str, Any]:
    """Read-only pair check for main's replay review; never starts either condition."""
    on = _read_json(on_output / "model/server_runtime.before.json", "on runtime")
    off = _read_json(off_output / "model/server_runtime.before.json", "off runtime")
    if (on["gpu_uuid"] != off["gpu_uuid"] or on["hostname"] != off["hostname"] or on["boot_id"] != off["boot_id"]
            or on["instrumentation_mode"] != "instrument_on" or off["instrumentation_mode"] != "instrument_off"
            or (on["pid"], on["start_ticks"]) == (off["pid"], off["start_ticks"])):
        raise AdapterError("model pair lacks observed same-GPU server restart with actual on/off modes")
    left = _read_jsonl(on_output / "model/native_attribution.jsonl", "on native sidecars")
    right = _read_jsonl(off_output / "model/native_attribution.jsonl", "off work sidecars")
    if not left or len(left) != len(right):
        raise AdapterError("model pair request count differs")
    comparable = ("prompt_tokens", "completion_tokens", "cached_tokens", "parameters", "model", "model_revision", "request_sha256")
    for a, b in zip(left, right):
        if a.get("status") != "measured" or b.get("native_status") != "unavailable" or not a.get("work") or not b.get("work"):
            raise AdapterError("model pair lacks complete native-on/API-off fixed-work evidence")
        if any(a["work"].get(key) != b["work"].get(key) for key in comparable):
            raise AdapterError("actual on/off model token work or request parameters differ")
    return {"status": "equal_fixed_work", "requests": len(left), "gpu_uuid": on["gpu_uuid"],
            "on_sidecars_sha256": _sha_file(on_output / "model/native_attribution.jsonl"),
            "off_sidecars_sha256": _sha_file(off_output / "model/native_attribution.jsonl"),
            "off_native_timing": "unavailable_observer_disabled", "full_production_capture_enabled": False}


def _model_request_loop(config: Mapping[str, Any], requests: Sequence[Request], proxy: Any, output_dir: Path) -> list[dict[str, Any]]:
    audit_path = output_dir / "model" / "request_audit.jsonl"
    rows: list[dict[str, Any]] = []
    for request in requests:
        response_body = b""
        status_code: int | None = None
        error: str | None = None
        response_complete = False
        started = time.perf_counter_ns()
        connection: http.client.HTTPConnection | None = None
        try:
            connection = http.client.HTTPConnection("127.0.0.1", proxy.server_address[1], timeout=float(config["timeout_seconds"]))
            connection.request(request.method, request.path, body=request.body, headers=_request_bytes(request, config["headers"]))
            response = connection.getresponse()
            status_code = int(response.status)
            try:
                response_body = response.read()
                response_complete = True
            except BaseException as exc:
                response_body = _partial_bytes(exc) or b""
                error = f"{type(exc).__name__}: response body incomplete"
        except BaseException as exc:
            partial = _partial_bytes(exc)
            if partial is not None:
                response_body = partial
            error = f"{type(exc).__name__}: {str(exc)[:384]}"
        finally:
            if connection is not None:
                connection.close()
        response_path = output_dir / "model" / "responses" / f"{request.index:04d}.bin"
        _write_bytes(response_path, response_body)
        ended = time.perf_counter_ns()
        row = {
            "schema_version": "assignment.fixed-work-request-audit.v1",
            "index": request.index,
            "logical_request_id": request.logical_request_id,
            "method": request.method,
            "path": request.path,
            "request_sha256": _sha_bytes(request.body),
            "request_bytes": len(request.body),
            "status_code": status_code,
            "response_sha256": _sha_bytes(response_body),
            "response_bytes": len(response_body),
            "response_complete": response_complete,
            "completion_tokens": _usage(response_body) if response_complete else None,
            "usage": _model_usage(response_body) if response_complete else {},
            # Keep cache accounting at the request-audit boundary as well as
            # inside the exact response artifact.  ``None`` is deliberate
            # when prompt-token details were omitted by the server.
            "cached_tokens": (
                _model_usage(response_body).get("cached_tokens")
                if response_complete
                else None
            ),
            "native_status": "unavailable",
            "native_unavailable_reason": "deferred evidence required" if status_code == 200 and response_complete and not error else "request failed/incomplete",
            "error": error,
            "duration_ms": (ended - started) / 1_000_000,
            "response_path": str(response_path.relative_to(output_dir)),
        }
        _append_jsonl(audit_path, row)
        rows.append(row)
    return rows


def _jsonl_if_present(path: Path, label: str) -> list[dict[str, Any]]:
    if not path.is_file() or path.is_symlink():
        return []
    payload = path.read_bytes()
    if not payload:
        return []
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(payload.decode("utf-8").splitlines(), 1):
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AdapterError(f"{label} line {number} is invalid JSON") from exc
        if not isinstance(value, dict):
            raise AdapterError(f"{label} line {number} is not an object")
        rows.append(value)
    return rows


def _telemetry_artifact(root: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise AdapterError(f"{label} path is invalid")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or "\\" in value:
        raise AdapterError(f"{label} path escapes the telemetry directory")
    path = (root / relative).resolve()
    _under(path, root, f"{label} path")
    if not path.is_file() or path.is_symlink():
        raise AdapterError(f"{label} artifact is unavailable: {path}")
    return path


def _model_capture_counts(fixture: Fixture, mode: str, output_dir: Path, requests: Sequence[Request], request_audit: Sequence[Mapping[str, Any]]) -> tuple[dict[str, int], int]:
    telemetry_dir = output_dir / "model" / "telemetry_v2"
    if mode == "instrument_off" and telemetry_dir.exists():
        raise AdapterError("instrument_off emitted a v2 raw capture directory")
    event_path = output_dir / "model" / "request_events.jsonl"
    events = _jsonl_if_present(event_path, "request proxy events")
    if len(events) != len(requests) or len(request_audit) != len(requests):
        raise AdapterError(f"request proxy emitted {len(events)} events for {len(requests)} fixed requests")
    native_results = _model_native_results(fixture, output_dir, requests, request_audit, events) if mode == "instrument_on" else []
    for request, audit, event in zip(requests, request_audit, events):
        if event.get("request_sha256") != audit.get("request_sha256") or event.get("request_sha256") != _sha_bytes(request.body):
            raise AdapterError(f"request {request.index} body hash differs between fixture and observed proxy audit")
        dispatched = event.get("serving_metrics_physical_request_dispatched")
        if mode == "instrument_on":
            if not isinstance(dispatched, bool):
                raise AdapterError(
                    "instrument_on request proxy did not emit a physical dispatch witness; "
                    "the explicit serving metrics capture is unavailable"
                )
            if not dispatched:
                raise AdapterError(f"request {request.index} has no observed physical dispatch")
        elif dispatched is not None:
            raise AdapterError(
                f"instrument_off request {request.index} emitted a serving-metrics capture field"
            )
        elif not isinstance(event.get("status_code"), int):
            raise AdapterError(
                f"request {request.index} has no off-mode physical dispatch witness; "
                "a response status or instrument_on serving witness is required"
            )
        if event.get("status_code") != audit.get("status_code"):
            raise AdapterError(f"request {request.index} response status differs from proxy audit")
        for field in (
            "request_bytes",
            "response_bytes",
            "response_sha256",
            "completion_tokens",
            "cached_tokens",
        ):
            if event.get(field) != audit.get(field):
                raise AdapterError(f"request {request.index} observed {field} differs from proxy audit")
    physical = len(events)
    raw_model_records = 0
    missing_bodies = 0
    if mode == "instrument_on":
        model_rows = _jsonl_if_present(telemetry_dir / "model_events.jsonl", "v2 model events")
        terminal = [row for row in model_rows if row.get("terminal") is True and row.get("event_kind") == "model_request"]
        raw_model_records = len(terminal)
        by_logical: dict[str, list[dict[str, Any]]] = {}
        for row in terminal:
            by_logical.setdefault(row.get("logical_request_id"), []).append(row)
        for request, audit in zip(requests, request_audit):
            candidates = by_logical.get(request.logical_request_id, [])
            if len(candidates) != 1:
                raise AdapterError("raw model request lacks exactly one logical/physical attempt join")
            row = candidates[0]
            if isinstance(row, Mapping) and row.get("status") != "success":
                raise AdapterError(f"v2 model request {request.index} did not complete successfully")
            artifact = row.get("request_payload_artifact") if isinstance(row, Mapping) else None
            request_value = artifact.get("request") if isinstance(artifact, Mapping) else None
            artifact_path = request_value.get("artifact_path") if isinstance(request_value, Mapping) else None
            complete = request_value.get("complete") if isinstance(request_value, Mapping) else False
            if not isinstance(artifact_path, str) or complete is not True:
                missing_bodies += 1
                continue
            request_digest = request_value.get("sha256") if isinstance(request_value, Mapping) else None
            expected_request_digest = _sha_bytes(request.body)
            if request_digest != expected_request_digest:
                missing_bodies += 1
                continue
            payload_path = _telemetry_artifact(telemetry_dir, artifact_path, f"request {request.index}")
            if _sha_file(payload_path) != expected_request_digest:
                missing_bodies += 1
            response_value = artifact.get("response") if isinstance(artifact, Mapping) else None
            response_path = response_value.get("artifact_path") if isinstance(response_value, Mapping) else None
            response_complete = response_value.get("complete") if isinstance(response_value, Mapping) else False
            if not isinstance(response_path, str) or response_complete is not True:
                raise AdapterError(f"v2 response payload capture is incomplete for request {request.index}")
            response_digest = response_value.get("sha256") if isinstance(response_value, Mapping) else None
            if response_digest != audit.get("response_sha256"):
                raise AdapterError(f"v2 response payload hash differs from observed response {request.index}")
            observed_response = _telemetry_artifact(telemetry_dir, response_path, f"response {request.index}")
            if (
                _sha_file(observed_response) != audit.get("response_sha256")
            ):
                raise AdapterError(f"v2 response payload capture differs from observed response {request.index}")
            if row.get("output_tokens") != audit.get("completion_tokens"):
                raise AdapterError(f"v2 output usage differs from observed response {request.index}")
        if raw_model_records != physical:
            raise AdapterError(
                f"v2 model record count {raw_model_records} differs from physical request count {physical}"
            )
        if missing_bodies:
            raise AdapterError(f"v2 request predispatch capture is missing {missing_bodies} raw request body artifact(s)")
        for row in native_results:
            if row.get("status") != "measured":
                raise AdapterError("direct native model proof unavailable: " + str(row.get("unavailable_reason")))
    capture = {
        "full_production_capture_enabled": FULL_PRODUCTION_CAPTURE_ENABLED,
        "individual_cpu_operation_records": 0,
        "physical_requests": physical,
        "raw_model_request_records": raw_model_records,
        "dropped_cpu_records": 0,
        "cpu_capture_map_failures": 0,
        "missing_raw_request_bodies": missing_bodies,
    }
    return capture, physical


def _model_condition(fixture: Fixture, mode: str, scratch_dir: Path, output_dir: Path, repeat: int, timeout_seconds: float, bpf_launcher: Callable[..., Any] | None = None) -> dict[str, Any]:
    if fixture.kind not in MODEL_KINDS:
        raise AdapterError(f"{fixture.case_id}: model adapter received fixture kind {fixture.kind}")
    config = _model_config(fixture)
    requests = _parse_requests(fixture, model=True)
    cpu_config = _cpu_config(fixture)
    _require_action_capture(fixture, mode, cpu_config)
    if timeout_seconds <= 0:
        raise AdapterError("adapter timeout must be positive")
    startup_started = time.perf_counter_ns()
    runtime_before = _model_runtime(config, mode, "before")
    _write_json(output_dir / "model/server_runtime.before.json", runtime_before)
    process = _spawn_bash_fixture()
    service = None
    proxy = None
    proxy_thread: threading.Thread | None = None
    request_audit: list[dict[str, Any]] = []
    action_audit: list[dict[str, Any]] = []
    work_started = None
    work_ended = None
    cleanup_error: BaseException | None = None
    try:
        _prepare_persistent_shell(process, scratch_dir, timeout_seconds)
        if mode == "instrument_on":
            if cpu_config is None:
                raise AdapterError(
                    f"{fixture.case_id}: instrument_on would run uncaptured fixture actions; "
                    "explicit cpu_collector configuration is required"
                )
            service = (bpf_launcher or _launch_cpu_service)(fixture, process, output_dir, repeat, scratch_dir, cpu_config)
        from scripts.observability.request_proxy import JsonlWriter, ProxyServer

        serving_config = config["serving_metrics_config"]
        proxy = ProxyServer(
            ("127.0.0.1", 0),
            upstream_host=config["upstream_host"],
            upstream_port=config["upstream_port"],
            writer=JsonlWriter(output_dir / "model" / "request_events.jsonl"),
            timeout_seconds=config["timeout_seconds"],
            max_body_bytes=config["max_body_bytes"],
            v2_output_dir=(output_dir / "model" / "telemetry_v2") if mode == "instrument_on" else None,
            v2_run_id=f"fixed-work:{fixture.case_id}",
            v2_attempt_id=f"repeat-{repeat:03d}",
            v2_case_id=fixture.case_id,
            v2_require_request_payloads=mode == "instrument_on",
            # The off condition keeps ordinary proxy dispatch/response audit,
            # but must not emit serving-metrics snapshots or v2 raw capture.
            serving_metrics_config=serving_config if mode == "instrument_on" else None,
        )
        proxy_thread = threading.Thread(target=proxy.serve_forever, name="fixed-work-request-proxy", daemon=True)
        proxy_thread.start()
        _model_cache_reset(config, output_dir, f"{fixture.case_id}:repeat-{repeat}:{mode}:{output_dir}")
        startup_wall_ms = (time.perf_counter_ns() - startup_started) / 1_000_000
        work_started = time.perf_counter_ns()
        action_audit = _run_actions(process, fixture.actions, output_dir, min(timeout_seconds, config["timeout_seconds"]), service.client if service is not None else None)
        request_audit = _model_request_loop(config, requests, proxy, output_dir)
    finally:
        if proxy is not None:
            try:
                proxy.request_graceful_shutdown()
                proxy.close_gracefully(timeout=min(5.0, timeout_seconds))
            except BaseException as exc:
                cleanup_error = exc
            if proxy_thread is not None:
                proxy_thread.join(timeout=min(5.0, timeout_seconds))
                if proxy_thread.is_alive():
                    cleanup_error = cleanup_error or AdapterError("request proxy thread did not terminate during condition cleanup")
        if service is not None:
            try:
                service.stop()
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
        try:
            _stop_bash_fixture(process)
        except BaseException as exc:
            cleanup_error = cleanup_error or exc
        if callable(getattr(process, "poll", None)) and process.poll() is None:
            cleanup_error = cleanup_error or AdapterError("persistent shell did not terminate during condition cleanup")
        work_ended = time.perf_counter_ns()
        if cleanup_error is not None:
            raise AdapterError(f"model condition cleanup failed: {type(cleanup_error).__name__}") from cleanup_error
    if work_started is None or work_ended is None:
        raise AdapterError("model condition did not establish a measured work interval")
    runtime_after = _model_runtime(config, mode, "after")
    _write_json(output_dir / "model/server_runtime.after.json", runtime_after)
    for key in ("hostname", "boot_id", "gpu_uuid", "pid", "start_ticks", "argv_sha256", "native_hook_sha256", "counter_epoch"):
        if runtime_after.get(key) != runtime_before.get(key):
            raise AdapterError("server runtime changed during condition: " + key)
    if runtime_after["observed_monotonic_ns"] <= runtime_before["observed_monotonic_ns"]:
        raise AdapterError("server runtime before/after observation ordering is invalid")
    if mode == "instrument_off":
        _model_off_results(fixture, output_dir, requests, request_audit)
    capture, _physical = _model_capture_counts(fixture, mode, output_dir, requests, request_audit)
    for row in request_audit:
        if row.get("status_code") is None or not (200 <= int(row["status_code"]) < 300):
            raise AdapterError(f"model request {row.get('index')} failed; raw response/error is retained in model/request_audit.jsonl")
        if row.get("response_complete") is not True or not isinstance(row.get("completion_tokens"), int):
            raise AdapterError(f"model request {row.get('index')} has no measured response usage")
    if service is not None and mode == "instrument_on":
        capture.update(_bpf_capture_counts(_read_summary(output_dir / "bpf"), output_dir / "bpf", action_audit))
    output_tokens = sum(int(row["completion_tokens"]) for row in request_audit)
    _write_json(output_dir / "model" / "capture_audit.json", {"capture": capture, "request_count": len(requests), "action_count": len(action_audit)})
    result_capture = {
        "full_production_capture_enabled": capture["full_production_capture_enabled"],
        "individual_cpu_operation_records": capture["individual_cpu_operation_records"],
        "physical_requests": capture["physical_requests"],
        "raw_model_request_records": capture["raw_model_request_records"],
        "dropped_cpu_records": capture["dropped_cpu_records"],
        "cpu_capture_map_failures": capture["cpu_capture_map_failures"],
        "missing_raw_request_bodies": capture["missing_raw_request_bodies"],
    }
    return _base_result(
        fixture,
        repeat,
        mode,
        output_tokens=output_tokens,
        output_provenance="measured_response_usage",
        capture=result_capture,
        work_wall_ms=(work_ended - work_started) / 1_000_000,
        startup_wall_ms=startup_wall_ms,
    )


def run_adapter(
    fixture_manifest: Path,
    fixture_id: str,
    instrumentation_mode: str,
    scratch_dir: Path,
    output_dir: Path,
    result_path: Path,
    repeat: int,
    *,
    timeout_seconds: float = 600.0,
    bpf_launcher: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    if instrumentation_mode not in MODES:
        raise AdapterError("instrumentation mode must be instrument_off or instrument_on")
    if isinstance(repeat, bool) or not isinstance(repeat, int) or repeat < 0:
        raise AdapterError("repeat must be a non-negative integer")
    timeout = _validate_timeout(timeout_seconds, "adapter timeout_seconds")
    fixture = _load_fixture(fixture_manifest, fixture_id)
    scratch = _absolute_directory(str(scratch_dir), "scratch directory")
    output = _absolute_directory(str(output_dir), "output directory", create=True)
    result = _absolute_target(result_path, "result path")
    _under(result, output, "result path")
    if result.exists():
        raise AdapterError(f"refusing to overwrite existing result path: {result}")
    model = fixture.kind in MODEL_KINDS or fixture.case_id.startswith("model-")
    if fixture.kind not in CPU_KINDS | MODEL_KINDS:
        raise AdapterError(f"unsupported fixed-work fixture kind: {fixture.kind}")
    if model:
        value = _model_condition(fixture, instrumentation_mode, scratch, output, repeat, timeout, bpf_launcher)
    else:
        value = _cpu_condition(fixture, instrumentation_mode, scratch, output, repeat, timeout, bpf_launcher)
    _write_json(result, value)
    return value


def _absolute_target(value: Path | str, label: str) -> Path:
    raw = Path(value).expanduser()
    if not raw.is_absolute() or "\x00" in str(raw):
        raise AdapterError(f"{label} must be absolute")
    if raw.is_symlink():
        raise AdapterError(f"{label} must not be a symlink")
    return raw


def _write_blocked(output_dir: Path, error: BaseException) -> None:
    try:
        _write_json(
            output_dir / "adapter_error.json",
            {
                "schema_version": ADAPTER_ERROR_SCHEMA,
                "status": "blocked",
                "error_type": type(error).__name__,
                "error": str(error)[:1000],
                "no_values_imputed": True,
            },
        )
    except Exception:
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-manifest", type=Path, required=True)
    parser.add_argument("--fixture-id", required=True)
    parser.add_argument("--instrumentation-mode", choices=sorted(MODES), required=True)
    parser.add_argument("--scratch-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--result-path", type=Path, required=True)
    parser.add_argument("--repeat", type=int, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run_adapter(
            args.fixture_manifest,
            args.fixture_id,
            args.instrumentation_mode,
            args.scratch_dir,
            args.output_dir,
            args.result_path,
            args.repeat,
            timeout_seconds=args.timeout_seconds,
        )
    except (AdapterError, ValueError, OSError, BpfAttachError, BpfProtocolError) as exc:
        try:
            output = _absolute_directory(str(args.output_dir), "output directory", create=True)
            _write_blocked(output, exc)
        except Exception:
            pass
        print(f"fixed-work adapter: BLOCKED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({key: result[key] for key in ("case_id", "repeat", "instrumentation_mode", "work_wall_ms", "startup_wall_ms")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
