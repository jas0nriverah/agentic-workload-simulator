#!/usr/bin/env python3
"""Produce an exclusive-serving witness from an independent server access log.

This adapter deliberately does not read request-proxy events, query vLLM, or
guess that the proxy was the only caller.  It accepts one strict JSONL access
log contract emitted by a gateway/server observer.  The log must contain every
request in the covered interval, including non-model observer traffic such as
the proxy's ``/metrics`` GETs, and must classify each record as ``model`` or
``observer``.  A model request must carry the physical ID forwarded by the
proxy; an observer request must carry no model request ID.

The caller supplies the exact native-metrics window.  A witness is emitted
only when the complete server-owned log contains exactly one model request ID
over that window and the external lease header marks the server dedicated.
Missing, incomplete, malformed, duplicated, or overlapping model evidence
fails closed.  A complete log that proves another model request was present
is retained as a negative witness so the serving collector reports it as
unavailable rather than treating it as a silent omission.

Input JSONL schema (``assignment.serving-access-log.v1``)::

    {"schema_version":"assignment.serving-access-log.v1",
     "record_type":"stream_header", "server_identity":"...",
     "lease_id":"...", "counter_epoch":"...", "vllm_version":"0.10.0",
     "access_scope":"all_requests_with_classification",
     "stream_complete":true, "dedicated_server":true,
     "coverage_start_monotonic_ns":1000,
     "coverage_end_monotonic_ns":9000,
     "first_sequence":1, "last_sequence":4}
    {"schema_version":"assignment.serving-access-log.v1",
     "record_type":"request", "source_sequence":1,
     "request_class":"observer", "request_id":null,
     "server_identity":"...", "lease_id":"...", "counter_epoch":"...",
     "started_monotonic_ns":1000, "ended_monotonic_ns":1100}

The producer is intentionally an adapter contract for a real gateway/server
logger.  Ordinary text vLLM logs and the current launcher's ``log_path`` do
not satisfy it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from agentic_sim.telemetry.serving_metrics import (  # noqa: E402
    AccessWitness,
    ServingMetricsError,
    VLLM_VERSION,
)


ACCESS_LOG_SCHEMA = "assignment.serving-access-log.v1"
ACCESS_LOG_HEADER_TYPE = "stream_header"
ACCESS_LOG_REQUEST_TYPE = "request"
ACCESS_SCOPE = "all_requests_with_classification"
WITNESS_SCHEMA = "assignment.serving-access-witness.v1"
WITNESS_EVIDENCE_KIND = "external_access_lease"
REQUEST_CLASSES = frozenset({"model", "observer"})

_HEADER_FIELDS = frozenset(
    {
        "schema_version",
        "record_type",
        "server_identity",
        "lease_id",
        "counter_epoch",
        "vllm_version",
        "access_scope",
        "stream_complete",
        "dedicated_server",
        "coverage_start_monotonic_ns",
        "coverage_end_monotonic_ns",
        "first_sequence",
        "last_sequence",
    }
)
_REQUEST_FIELDS = frozenset(
    {
        "schema_version",
        "record_type",
        "source_sequence",
        "request_class",
        "request_id",
        "server_identity",
        "lease_id",
        "counter_epoch",
        "started_monotonic_ns",
        "ended_monotonic_ns",
    }
)


class WitnessProducerError(ValueError):
    """The server access evidence cannot support a witness."""


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _contains_symlink_component(path: Path) -> bool:
    current = path.expanduser().absolute()
    while True:
        if current.is_symlink():
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def _regular_file(path: Path, label: str) -> Path:
    path = path.expanduser()
    if _contains_symlink_component(path) or not path.is_file():
        raise WitnessProducerError(f"{label} must be a regular non-symlink file: {path}")
    return path


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WitnessProducerError(f"{label} must be non-empty text")
    return value


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WitnessProducerError(f"{label} must be an integer")
    return value


@dataclass(frozen=True)
class AccessLogHeader:
    server_identity: str
    lease_id: str
    counter_epoch: str
    vllm_version: str
    stream_complete: bool
    dedicated_server: bool
    coverage_start_monotonic_ns: int
    coverage_end_monotonic_ns: int
    first_sequence: int
    last_sequence: int


@dataclass(frozen=True)
class AccessLogEntry:
    source_sequence: int
    request_class: str
    request_id: str | None
    server_identity: str
    lease_id: str
    counter_epoch: str
    started_monotonic_ns: int
    ended_monotonic_ns: int


def _parse_header(value: Any, line_number: int) -> AccessLogHeader:
    if not isinstance(value, Mapping):
        raise WitnessProducerError(f"access log line {line_number} is not an object")
    if set(value) != _HEADER_FIELDS:
        raise WitnessProducerError(
            f"access log header has missing or unknown fields at line {line_number}"
        )
    if value.get("schema_version") != ACCESS_LOG_SCHEMA:
        raise WitnessProducerError("access log header has an unsupported schema")
    if value.get("record_type") != ACCESS_LOG_HEADER_TYPE:
        raise WitnessProducerError("access log first record must be a stream_header")
    stream_complete = value.get("stream_complete")
    dedicated_server = value.get("dedicated_server")
    if not isinstance(stream_complete, bool) or not isinstance(dedicated_server, bool):
        raise WitnessProducerError("access log header lease flags must be JSON booleans")
    start = _integer(value.get("coverage_start_monotonic_ns"), "coverage_start_monotonic_ns")
    end = _integer(value.get("coverage_end_monotonic_ns"), "coverage_end_monotonic_ns")
    if start < 0 or end <= start:
        raise WitnessProducerError(
            "access log coverage interval must be increasing and non-negative"
        )
    first = _integer(value.get("first_sequence"), "first_sequence")
    last = _integer(value.get("last_sequence"), "last_sequence")
    if first < 0 or last < first:
        raise WitnessProducerError("access log source sequence range is invalid")
    return AccessLogHeader(
        server_identity=_text(value.get("server_identity"), "server_identity"),
        lease_id=_text(value.get("lease_id"), "lease_id"),
        counter_epoch=_text(value.get("counter_epoch"), "counter_epoch"),
        vllm_version=_text(value.get("vllm_version"), "vllm_version"),
        stream_complete=stream_complete,
        dedicated_server=dedicated_server,
        coverage_start_monotonic_ns=start,
        coverage_end_monotonic_ns=end,
        first_sequence=first,
        last_sequence=last,
    )


def _parse_entry(value: Any, line_number: int, header: AccessLogHeader) -> AccessLogEntry:
    if not isinstance(value, Mapping):
        raise WitnessProducerError(f"access log line {line_number} is not an object")
    if set(value) != _REQUEST_FIELDS:
        raise WitnessProducerError(
            f"access log request has missing or unknown fields at line {line_number}"
        )
    if (
        value.get("schema_version") != ACCESS_LOG_SCHEMA
        or value.get("record_type") != ACCESS_LOG_REQUEST_TYPE
    ):
        raise WitnessProducerError(
            f"access log request has an unsupported record at line {line_number}"
        )
    request_class = value.get("request_class")
    if request_class not in REQUEST_CLASSES:
        raise WitnessProducerError(
            f"access log line {line_number} has an unsupported request class"
        )
    request_id = value.get("request_id")
    if request_class == "model":
        request_id = _text(request_id, f"access log line {line_number} model request_id")
    elif request_id is not None:
        raise WitnessProducerError(
            f"access log line {line_number} observer request must not carry a model request_id"
        )
    sequence = _integer(
        value.get("source_sequence"), f"access log line {line_number} source_sequence"
    )
    started = _integer(
        value.get("started_monotonic_ns"),
        f"access log line {line_number} started_monotonic_ns",
    )
    ended = _integer(
        value.get("ended_monotonic_ns"),
        f"access log line {line_number} ended_monotonic_ns",
    )
    if started < header.coverage_start_monotonic_ns or ended > header.coverage_end_monotonic_ns:
        raise WitnessProducerError(
            f"access log line {line_number} lies outside the declared complete coverage interval"
        )
    if ended <= started:
        raise WitnessProducerError(
            f"access log line {line_number} has a non-increasing request interval"
        )
    server_identity = _text(
        value.get("server_identity"), f"access log line {line_number} server_identity"
    )
    lease_id = _text(value.get("lease_id"), f"access log line {line_number} lease_id")
    counter_epoch = _text(
        value.get("counter_epoch"), f"access log line {line_number} counter_epoch"
    )
    if (
        server_identity != header.server_identity
        or lease_id != header.lease_id
        or counter_epoch != header.counter_epoch
    ):
        raise WitnessProducerError(
            f"access log line {line_number} disagrees with its lease header"
        )
    return AccessLogEntry(
        source_sequence=sequence,
        request_class=request_class,
        request_id=request_id,
        server_identity=server_identity,
        lease_id=lease_id,
        counter_epoch=counter_epoch,
        started_monotonic_ns=started,
        ended_monotonic_ns=ended,
    )


def read_access_log(path: str | Path) -> tuple[AccessLogHeader, tuple[AccessLogEntry, ...], str]:
    """Read and validate the complete server-owned access stream."""

    access_path = _regular_file(Path(path), "access log")
    try:
        raw = access_path.read_bytes()
    except OSError as exc:
        raise WitnessProducerError(f"cannot read access log: {access_path}: {exc}") from exc
    header: AccessLogHeader | None = None
    entries: list[AccessLogEntry] = []
    for line_number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WitnessProducerError(
                f"access log line {line_number} is invalid JSON: {exc}"
            ) from exc
        if header is None:
            header = _parse_header(value, line_number)
            if not header.stream_complete:
                raise WitnessProducerError("access log stream is not marked complete")
            if header.vllm_version != VLLM_VERSION:
                raise WitnessProducerError("access log vLLM version is unsupported")
            if value.get("access_scope") != ACCESS_SCOPE:
                raise WitnessProducerError(
                    "access log does not claim complete classified request coverage"
                )
            continue
        entries.append(_parse_entry(value, line_number, header))
    if header is None:
        raise WitnessProducerError("access log is empty")
    expected_count = header.last_sequence - header.first_sequence + 1
    if len(entries) != expected_count:
        raise WitnessProducerError("access log source sequence range does not match its records")
    seen_sequences: set[int] = set()
    seen_model_ids: set[str] = set()
    expected_sequence = header.first_sequence
    for entry in entries:
        if entry.source_sequence != expected_sequence:
            raise WitnessProducerError(
                "access log source sequence has a gap, duplicate, or reordering"
            )
        expected_sequence += 1
        if entry.source_sequence in seen_sequences:
            raise WitnessProducerError("access log source sequence is duplicated")
        seen_sequences.add(entry.source_sequence)
        if entry.request_id is not None:
            if entry.request_id in seen_model_ids:
                raise WitnessProducerError("access log model request_id is duplicated")
            seen_model_ids.add(entry.request_id)
    return header, tuple(entries), _sha256(raw)


def _validate_existing_output(path: Path) -> set[str]:
    if not path.exists():
        return set()
    output_path = _regular_file(path, "witness output")
    try:
        raw = output_path.read_bytes()
    except OSError as exc:
        raise WitnessProducerError(f"cannot read witness output: {output_path}: {exc}") from exc
    request_ids: set[str] = set()
    for line_number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WitnessProducerError(
                f"witness output line {line_number} is invalid JSON: {exc}"
            ) from exc
        if not isinstance(value, Mapping):
            raise WitnessProducerError(f"witness output line {line_number} is not an object")
        if (
            value.get("schema_version") != WITNESS_SCHEMA
            or value.get("evidence_kind") != WITNESS_EVIDENCE_KIND
        ):
            raise WitnessProducerError(
                f"witness output line {line_number} has an unsupported schema"
            )
        request_id = _text(value.get("request_id"), f"witness output line {line_number} request_id")
        try:
            witness = AccessWitness.from_mapping(value)
        except ServingMetricsError as exc:
            raise WitnessProducerError(
                f"witness output line {line_number} failed strict parsing: {exc}"
            ) from exc
        producer_status = value.get("producer_status")
        if producer_status not in {"measured", "unavailable"}:
            raise WitnessProducerError(
                f"witness output line {line_number} has an unsupported producer status"
            )
        if producer_status == "measured":
            try:
                witness.validate()
            except ServingMetricsError as exc:
                raise WitnessProducerError(
                    f"witness output line {line_number} is not a positive witness: {exc}"
                ) from exc
        if request_id in request_ids:
            raise WitnessProducerError("witness output already contains duplicate request IDs")
        request_ids.add(request_id)
    return request_ids


def produce_witness(
    *,
    access_log: str | Path,
    output: str | Path,
    request_id: str,
    window_start_monotonic_ns: int,
    window_end_monotonic_ns: int,
    expected_server_identity: str | None = None,
    expected_lease_id: str | None = None,
    expected_counter_epoch: str | None = None,
    expected_vllm_version: str = VLLM_VERSION,
) -> dict[str, Any]:
    """Build and append one witness from a complete independent access log."""

    request_id = _text(request_id, "request_id")
    start = _integer(window_start_monotonic_ns, "window_start_monotonic_ns")
    end = _integer(window_end_monotonic_ns, "window_end_monotonic_ns")
    if start < 0 or end <= start:
        raise WitnessProducerError("native metrics window must be increasing and non-negative")
    header, entries, access_log_sha256 = read_access_log(access_log)
    if (
        expected_vllm_version != header.vllm_version
        or expected_vllm_version != VLLM_VERSION
    ):
        raise WitnessProducerError("configured vLLM version does not match pinned vLLM 0.10.0")
    for expected, actual, label in (
        (expected_server_identity, header.server_identity, "server identity"),
        (expected_lease_id, header.lease_id, "lease ID"),
        (expected_counter_epoch, header.counter_epoch, "counter epoch"),
    ):
        if expected is not None and expected != actual:
            raise WitnessProducerError(f"configured {label} does not match access log")
    if start < header.coverage_start_monotonic_ns or end > header.coverage_end_monotonic_ns:
        raise WitnessProducerError(
            "native metrics window is outside complete access-log coverage"
        )

    overlapping = [
        entry
        for entry in entries
        if entry.started_monotonic_ns < end and entry.ended_monotonic_ns > start
    ]
    target = [entry for entry in overlapping if entry.request_id == request_id]
    if not target:
        raise WitnessProducerError(
            "no server access-log record for physical request in the metrics window"
        )
    if len(target) != 1:
        raise WitnessProducerError(
            "multiple server access-log records for physical request in the metrics window"
        )
    observed = [entry.request_id for entry in overlapping if entry.request_id is not None]
    if request_id not in observed:
        raise WitnessProducerError("target physical request is not a model access record")
    other_ids = [item for item in observed if item != request_id]
    no_other_requests = header.dedicated_server and not other_ids
    row: dict[str, Any] = {
        "schema_version": WITNESS_SCHEMA,
        "evidence_kind": WITNESS_EVIDENCE_KIND,
        "request_id": request_id,
        "server_identity": header.server_identity,
        "lease_id": header.lease_id,
        "counter_epoch": header.counter_epoch,
        "observed_request_ids": observed,
        "other_request_ids": other_ids,
        "dedicated_server": header.dedicated_server,
        "no_other_requests": no_other_requests,
        "vllm_version": header.vllm_version,
        "producer_status": "measured" if no_other_requests else "unavailable",
        "unavailable_reason": (
            None
            if no_other_requests
            else (
                "server lease is not marked dedicated"
                if not header.dedicated_server
                else "server access log contains another model request in the metrics window"
            )
        ),
        "access_log": {
            "schema_version": ACCESS_LOG_SCHEMA,
            "sha256": access_log_sha256,
            "source_sequence_first": header.first_sequence,
            "source_sequence_last": header.last_sequence,
            "coverage_start_monotonic_ns": header.coverage_start_monotonic_ns,
            "coverage_end_monotonic_ns": header.coverage_end_monotonic_ns,
            "window_start_monotonic_ns": start,
            "window_end_monotonic_ns": end,
            "overlapping_record_count": len(overlapping),
        },
    }

    # Validate the positive row against the exact witness implementation used
    # by request_proxy.  Negative rows intentionally remain in the stream so
    # the proxy can classify a real competing request as unavailable.
    if no_other_requests:
        try:
            AccessWitness.from_mapping(row).validate()
        except ServingMetricsError as exc:
            raise WitnessProducerError(
                f"produced witness failed strict validation: {exc}"
            ) from exc

    output_path = Path(output).expanduser()
    if _contains_symlink_component(output_path):
        raise WitnessProducerError(f"witness output path contains a symlink: {output_path}")
    access_path = Path(access_log).expanduser()
    if output_path.resolve() == access_path.resolve():
        raise WitnessProducerError("witness output must be separate from the source access log")
    existing_ids = _validate_existing_output(output_path)
    if request_id in existing_ids:
        raise WitnessProducerError("witness output already contains this physical request ID")
    encoded = (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(output_path, flags, 0o640)
    except OSError as exc:
        raise WitnessProducerError(f"cannot open witness output: {output_path}: {exc}") from exc
    try:
        if output_path.is_symlink() or not output_path.is_file():
            raise WitnessProducerError(f"witness output is not a regular file: {output_path}")
        view = memoryview(encoded)
        while view:
            view = view[os.write(descriptor, view) :]
        os.fsync(descriptor)
    except WitnessProducerError:
        raise
    except OSError as exc:
        raise WitnessProducerError(f"cannot append witness output: {output_path}: {exc}") from exc
    finally:
        os.close(descriptor)
    return row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--access-log", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--request-id", required=True)
    parser.add_argument(
        "--window-start-monotonic-ns",
        "--window-start-ns",
        dest="window_start_monotonic_ns",
        required=True,
        type=int,
    )
    parser.add_argument(
        "--window-end-monotonic-ns",
        "--window-end-ns",
        dest="window_end_monotonic_ns",
        required=True,
        type=int,
    )
    parser.add_argument("--server-identity")
    parser.add_argument("--lease-id")
    parser.add_argument("--counter-epoch")
    parser.add_argument("--vllm-version", default=VLLM_VERSION)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.dry_run:
        try:
            header, entries, access_log_sha256 = read_access_log(args.access_log)
            if args.vllm_version != header.vllm_version or args.vllm_version != VLLM_VERSION:
                raise WitnessProducerError(
                    "configured vLLM version does not match pinned vLLM 0.10.0"
                )
            for expected, actual, label in (
                (args.server_identity, header.server_identity, "server identity"),
                (args.lease_id, header.lease_id, "lease ID"),
                (args.counter_epoch, header.counter_epoch, "counter epoch"),
            ):
                if expected is not None and expected != actual:
                    raise WitnessProducerError(f"configured {label} does not match access log")
            start = _integer(args.window_start_monotonic_ns, "window_start_monotonic_ns")
            end = _integer(args.window_end_monotonic_ns, "window_end_monotonic_ns")
            if start < header.coverage_start_monotonic_ns or end > header.coverage_end_monotonic_ns:
                raise WitnessProducerError(
                    "native metrics window is outside complete access-log coverage"
                )
            if end <= start:
                raise WitnessProducerError("native metrics window must be increasing")
            overlapping = [
                entry
                for entry in entries
                if entry.started_monotonic_ns < end and entry.ended_monotonic_ns > start
            ]
            model_ids = [entry.request_id for entry in overlapping if entry.request_id is not None]
            if model_ids.count(args.request_id) != 1:
                raise WitnessProducerError(
                    "native metrics window must contain exactly one target model access record"
                )
            if not header.dedicated_server or set(model_ids) != {args.request_id}:
                raise WitnessProducerError(
                    "native metrics window does not prove an exclusive dedicated server"
                )
            print(
                "DRY-RUN: valid complete server access log; "
                f"model_request_ids={model_ids!r}; access_log_sha256={access_log_sha256}; "
                "append no witness artifact"
            )
            return 0
        except WitnessProducerError as exc:
            print(f"UNAVAILABLE: {exc}", file=sys.stderr)
            return 3
    try:
        row = produce_witness(
            access_log=args.access_log,
            output=args.output,
            request_id=args.request_id,
            window_start_monotonic_ns=args.window_start_monotonic_ns,
            window_end_monotonic_ns=args.window_end_monotonic_ns,
            expected_server_identity=args.server_identity,
            expected_lease_id=args.lease_id,
            expected_counter_epoch=args.counter_epoch,
            expected_vllm_version=args.vllm_version,
        )
    except WitnessProducerError as exc:
        print(f"UNAVAILABLE: {exc}", file=sys.stderr)
        return 3
    status = row["producer_status"]
    if status != "measured":
        print(f"UNAVAILABLE: {row['unavailable_reason']}", file=sys.stderr)
        return 3
    print(
        f"witness appended: request_id={row['request_id']} "
        f"access_log_sha256={row['access_log']['sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
