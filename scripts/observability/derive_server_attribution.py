#!/usr/bin/env python3
"""Derive one fail-closed native serving attribution from an ASGI journal.

The observer writes request starts and terminals for every ingress route,
complete watermarks, and exact Prometheus response/registry artifacts.  This
script runs after the request and does not wait for another request, poll for
a favorable counter value, or issue an inference request.  It appends one
sidecar record to a separate JSONL file; the original request record and any
earlier unavailable serving result are left untouched.

In aggregate mode the sidecar is measured only when all of these are true:

* the selected before and after scrape artifacts are complete, hash-verified,
  in the same observer clock/process/epoch, and their windows do not overlap;
* exactly one complete model request with the requested physical ID spans the
  interval, while all other ingress is either absent or explicitly classified
  observer traffic;
* no foreign request, competing model request, pending request, late prior
  sample, fatal observer state, or missing sequence occurs in the covered
  window; and
* a durable completeness watermark covers the end of the after scrape.

Native timing values are delegated to the existing
``agentic_sim.telemetry.serving_metrics.derive_serving_metrics`` function.
That preserves its exact histogram count-delta-one, reset, finite-value, and
clock checks.  A client or proxy duration is never used as a native value.

With --native-journal, exact ID-bound FinishedRequestStats replace aggregate
inference. That separate mode requires an installed hook/source binding,
successful HTTP reconciliation, native multiplicity and completeness checks,
and raw native journal hashes. It does not require a Prometheus baseline or
certify that the aggregate logger has published its counters.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from agentic_sim.observability.vllm_metrics import parse_prometheus_text  # noqa: E402
from agentic_sim.telemetry.serving_metrics import (  # noqa: E402
    AccessWitness,
    ServingMetricsError,
    ServingSnapshot,
    VLLM_VERSION,
    derive_serving_metrics,
)
from agentic_sim.telemetry.serving_observer import (  # noqa: E402
    OBSERVER_SCHEMA,
    SAFE_OBSERVER_ROUTES,
)


ATTRIBUTION_SCHEMA = "assignment.server-attribution.v1"
ATTRIBUTION_EVIDENCE_KIND = "asgi_server_observer"
REQUEST_RECORD_TYPES = frozenset({"request_start", "request_terminal"})
KNOWN_RECORD_TYPES = frozenset(
    {
        "observer_header",
        "request_start",
        "request_terminal",
        "completeness_watermark",
        "postcompletion_metrics",
        "metrics_snapshot",
        "observer_fatal",
        "observer_shutdown",
    }
)
REQUEST_CLASSES = frozenset({"model", "observer", "foreign"})
REQUEST_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"})


class AttributionError(ValueError):
    """The observer evidence cannot support a positive attribution."""


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
        raise AttributionError(f"{label} must be a regular non-symlink file: {path}")
    return path


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AttributionError(f"{label} must be non-empty text")
    return value.strip()


def _integer(value: Any, label: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AttributionError(f"{label} must be an integer")
    if value < (1 if positive else 0):
        raise AttributionError(f"{label} must be {'positive' if positive else 'non-negative'}")
    return value


def _bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise AttributionError(f"{label} must be a JSON boolean")
    return value


def _strict_json(raw: bytes, line_number: int) -> Mapping[str, Any]:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON constant {token}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise AttributionError(f"observer journal line {line_number} is invalid JSON: {exc}") from exc
    if not isinstance(value, Mapping):
        raise AttributionError(f"observer journal line {line_number} is not an object")
    return value


def _clock_identity(record: Mapping[str, Any], label: str) -> Tuple[Any, ...]:
    clock = record.get("clock")
    if not isinstance(clock, Mapping):
        raise AttributionError(f"{label} clock identity is missing")
    fields = (
        clock.get("hostname"),
        clock.get("boot_id"),
        clock.get("clock_id"),
        clock.get("clock_source"),
    )
    if any(value in (None, "") for value in fields):
        raise AttributionError(f"{label} clock identity is incomplete")
    return fields


def _record_identity(record: Mapping[str, Any], label: str) -> Tuple[Any, ...]:
    clock = _clock_identity(record, label)
    return clock + (
        _text(record.get("server_identity"), f"{label} server_identity"),
        _integer(record.get("server_pid"), f"{label} server_pid", positive=True),
        _integer(
            record.get("server_process_start_ticks"),
            f"{label} server_process_start_ticks",
            positive=True,
        ),
        _text(record.get("counter_epoch"), f"{label} counter_epoch"),
        _text(record.get("observer_source_sha256"), f"{label} observer_source_sha256"),
        _text(record.get("observer_instance_id"), f"{label} observer_instance_id"),
    )


def _validate_request_start(record: Mapping[str, Any], header: Mapping[str, Any], label: str) -> None:
    request_class = _text(record.get("request_class"), f"{label} request_class")
    if request_class not in REQUEST_CLASSES:
        raise AttributionError(f"{label} has unsupported request_class {request_class!r}")
    method = _text(record.get("method"), f"{label} method").upper()
    if method not in REQUEST_METHODS:
        raise AttributionError(f"{label} has unsupported method {method!r}")
    route = _text(record.get("route"), f"{label} route")
    physical = record.get("physical_request_id")
    if physical is not None:
        _text(physical, f"{label} physical_request_id")
    present = record.get("physical_request_id_present")
    if present is not None:
        if _bool(present, f"{label} physical_request_id_present") != (physical is not None):
            raise AttributionError(f"{label} physical_request_id_present disagrees with physical_request_id")
    metrics_path = header.get("metrics_path", "/metrics")
    metrics_path = _text(metrics_path, "observer header metrics_path")
    if not metrics_path.startswith("/"):
        raise AttributionError("observer header metrics_path must be an absolute URL path")
    safe_routes = set(SAFE_OBSERVER_ROUTES)
    safe_routes.add(metrics_path)
    if request_class == "observer":
        if method not in {"GET", "HEAD"} or route not in safe_routes:
            raise AttributionError(
                f"{label} observer classification is restricted to known read-only GET/HEAD routes"
            )
    elif request_class == "model":
        if physical is None:
            raise AttributionError(f"{label} model classification has no physical_request_id")
    elif physical is not None:
        raise AttributionError(f"{label} foreign classification carries a physical_request_id")


def _validate_optional_metadata(record: Mapping[str, Any], label: str) -> None:
    for key in ("case_id", "attempt_id"):
        value = record.get(key)
        if value is not None:
            _text(value, f"{label} {key}")
    if record.get("correlation_error") is not None:
        _text(record.get("correlation_error"), f"{label} correlation_error")


@dataclass(frozen=True)
class ObserverJournal:
    path: Path
    raw_sha256: str
    header: Mapping[str, Any]
    records: Tuple[Mapping[str, Any], ...]
    starts: Mapping[str, Mapping[str, Any]]
    terminals: Mapping[str, Mapping[str, Any]]
    watermarks: Tuple[Mapping[str, Any], ...]
    metric_records: Tuple[Mapping[str, Any], ...]
    fatal_records: Tuple[Mapping[str, Any], ...]
    shutdown_records: Tuple[Mapping[str, Any], ...]
    identity: Tuple[Any, ...]


def read_observer_journal(path: str | Path) -> ObserverJournal:
    """Read a journal strictly, including sequence and watermark invariants."""

    journal_path = _regular_file(Path(path), "observer journal")
    try:
        raw = journal_path.read_bytes()
    except OSError as exc:
        raise AttributionError(f"cannot read observer journal: {exc}") from exc
    if not raw:
        raise AttributionError("observer journal is empty")
    if not raw.endswith(b"\n"):
        raise AttributionError("observer journal has an unterminated final record")
    parsed: List[Mapping[str, Any]] = []
    for line_number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            raise AttributionError(f"observer journal line {line_number} is blank")
        parsed.append(_strict_json(line, line_number))
    if not parsed:
        raise AttributionError("observer journal has no records")
    header = parsed[0]
    if header.get("schema_version") != OBSERVER_SCHEMA or header.get("record_type") != "observer_header":
        raise AttributionError("observer journal must start with one observer_header")
    identity = _record_identity(header, "observer header")
    if header.get("observer_source") != "asgi_server_middleware":
        raise AttributionError("observer journal source is not the ASGI server middleware")
    if not _bool(header.get("append_only"), "observer header append_only"):
        raise AttributionError("observer journal is not marked append-only")
    if not _bool(header.get("durable"), "observer header durable"):
        raise AttributionError("observer journal is not marked durable")
    version = _text(header.get("vllm_version"), "observer header vllm_version")
    if version != VLLM_VERSION:
        raise AttributionError(f"observer journal vLLM version {version!r} is unsupported")
    dedicated = _bool(header.get("dedicated_server"), "observer header dedicated_server")
    _text(header.get("lease_id"), "observer header lease_id")
    _integer(header.get("server_started_monotonic_ns"), "observer header server_started_monotonic_ns")

    starts: Dict[str, Mapping[str, Any]] = {}
    terminals: Dict[str, Mapping[str, Any]] = {}
    watermarks: List[Mapping[str, Any]] = []
    metric_records: List[Mapping[str, Any]] = []
    fatal_records: List[Mapping[str, Any]] = []
    shutdown_records: List[Mapping[str, Any]] = []
    expected_sequence = 1
    active: Set[str] = set()

    for line_number, record in enumerate(parsed, 1):
        sequence = _integer(record.get("sequence"), f"journal line {line_number} sequence", positive=True)
        if sequence != expected_sequence:
            raise AttributionError(
                f"observer journal sequence has a gap or reorder at line {line_number}: "
                f"expected {expected_sequence}, got {sequence}"
            )
        expected_sequence += 1
        if record.get("schema_version") != OBSERVER_SCHEMA:
            raise AttributionError(f"journal line {line_number} has an unsupported schema")
        if _record_identity(record, f"journal line {line_number}") != identity:
            raise AttributionError(f"journal line {line_number} changes clock/process/epoch identity")
        record_type = record.get("record_type")
        if record_type not in KNOWN_RECORD_TYPES:
            raise AttributionError(f"journal line {line_number} has unknown record_type {record_type!r}")
        if record_type == "observer_header":
            if line_number != 1:
                raise AttributionError("observer journal contains a second observer_header")
            continue
        if record_type == "request_start":
            observation_id = _text(record.get("observation_id"), f"journal line {line_number} observation_id")
            if observation_id in starts:
                raise AttributionError(f"observation {observation_id} has duplicate starts")
            _validate_request_start(record, header, f"journal line {line_number}")
            _validate_optional_metadata(record, f"journal line {line_number}")
            _integer(record.get("started_monotonic_ns"), f"journal line {line_number} started_monotonic_ns")
            physical = record.get("physical_request_id")
            if physical is not None:
                _text(physical, f"journal line {line_number} physical_request_id")
            if not _bool(record.get("pending"), f"journal line {line_number} pending"):
                raise AttributionError("request_start must be marked pending")
            starts[observation_id] = record
            active.add(observation_id)
        elif record_type == "request_terminal":
            observation_id = _text(record.get("observation_id"), f"journal line {line_number} observation_id")
            if observation_id in terminals:
                raise AttributionError(f"observation {observation_id} has duplicate terminals")
            if observation_id not in starts:
                raise AttributionError(f"terminal {observation_id} has no durable start")
            start = starts[observation_id]
            if record.get("started_monotonic_ns") != start.get("started_monotonic_ns"):
                raise AttributionError(f"terminal {observation_id} changes its start timestamp")
            for key in ("physical_request_id", "request_class", "method", "route",
                        "scrape_id", "scrape_phase", "scrape_id_source", "http_request_id", "http_request_id_error"):
                if record.get(key) != start.get(key):
                    raise AttributionError(f"terminal {observation_id} changes request identity field {key}")
            for key in ("case_id", "attempt_id", "correlation_error"):
                if record.get(key) != start.get(key):
                    raise AttributionError(f"terminal {observation_id} changes request metadata field {key}")
            _validate_request_start(record, header, f"terminal {observation_id}")
            _validate_optional_metadata(record, f"terminal {observation_id}")
            terminal_ns = _integer(record.get("terminal_monotonic_ns"), f"journal line {line_number} terminal_monotonic_ns")
            started_ns = _integer(start.get("started_monotonic_ns"), f"start {observation_id} started_monotonic_ns")
            if terminal_ns <= started_ns:
                raise AttributionError(f"terminal {observation_id} is not after its start")
            if _bool(record.get("pending"), f"journal line {line_number} pending"):
                raise AttributionError("request_terminal must be marked not pending")
            terminal_status = _text(record.get("terminal_status"), f"journal line {line_number} terminal_status")
            if terminal_status not in {"complete", "failed", "disconnected", "incomplete"}:
                raise AttributionError(f"journal line {line_number} has unsupported terminal_status {terminal_status!r}")
            terminals[observation_id] = record
            active.discard(observation_id)
        elif record_type == "completeness_watermark":
            complete = _bool(record.get("complete"), f"journal line {line_number} complete")
            if not complete:
                raise AttributionError(f"journal line {line_number} is not a complete watermark")
            covered = _integer(record.get("covered_through_monotonic_ns"), f"journal line {line_number} covered_through_monotonic_ns")
            watermark_ns = _integer(record.get("watermark_monotonic_ns"), f"journal line {line_number} watermark_monotonic_ns")
            if covered > watermark_ns:
                raise AttributionError(f"journal line {line_number} watermark covers time after its own timestamp")
            pending = record.get("pending_observation_ids")
            if not isinstance(pending, list) or any(not isinstance(item, str) or not item for item in pending):
                raise AttributionError(f"journal line {line_number} pending_observation_ids is invalid")
            if pending != sorted(set(pending)):
                raise AttributionError(f"journal line {line_number} pending_observation_ids is not sorted and unique")
            pending_count = record.get("pending_count")
            if pending_count is not None and _integer(
                pending_count, f"journal line {line_number} pending_count"
            ) != len(pending):
                raise AttributionError(f"journal line {line_number} pending_count disagrees with pending IDs")
            if set(pending) != active:
                raise AttributionError(
                    f"journal line {line_number} pending set does not match request starts and terminals"
                )
            covers = record.get("covers_through_sequence")
            if covers is not None and covers not in {sequence - 1, sequence}:
                raise AttributionError(f"journal line {line_number} watermark sequence binding is invalid")
            if record.get("fatal_error") is not None or record.get("observer_alive") is not True:
                raise AttributionError(f"journal line {line_number} watermark is not healthy")
            watermarks.append(record)
        elif record_type in {"metrics_snapshot", "postcompletion_metrics"}:
            metric_records.append(record)
        elif record_type == "observer_fatal":
            fatal_records.append(record)
        elif record_type == "observer_shutdown":
            shutdown_records.append(record)

    pending_at_end = sorted(active)
    if pending_at_end:
        # A process may legitimately be alive with an in-flight request, but a
        # sidecar cannot call any window complete while that request is unknown.
        pass
    for observation_id, terminal in terminals.items():
        metrics = terminal.get("metrics_capture")
        if metrics is not None and not isinstance(metrics, Mapping):
            raise AttributionError(f"terminal {observation_id} metrics_capture is not an object")
    return ObserverJournal(
        path=journal_path,
        raw_sha256=_sha256(raw),
        header=header,
        records=tuple(parsed),
        starts=starts,
        terminals=terminals,
        watermarks=tuple(watermarks),
        metric_records=tuple(metric_records),
        fatal_records=tuple(fatal_records),
        shutdown_records=tuple(shutdown_records),
        identity=identity,
    )


@dataclass(frozen=True)
class MetricEvidence:
    record: Mapping[str, Any]
    payload: Mapping[str, Any]
    sequence: int
    scrape_id: str
    phase: Optional[str]
    raw_path: Path
    raw_sha256: str
    raw_bytes: int
    started_ns: int
    captured_ns: int
    ended_ns: int
    associated_observation_id: Optional[str]


def _metric_payloads(data: ObserverJournal) -> Iterable[Tuple[Mapping[str, Any], Mapping[str, Any]]]:
    for record in data.records:
        if record.get("record_type") == "request_terminal":
            payload = record.get("metrics_capture")
            if isinstance(payload, Mapping):
                yield record, payload
        elif record.get("record_type") in {"metrics_snapshot", "postcompletion_metrics"}:
            yield record, record


def _metric_evidence(data: ObserverJournal) -> Tuple[MetricEvidence, ...]:
    result: List[MetricEvidence] = []
    seen_ids: Set[str] = set()
    for record, payload in _metric_payloads(data):
        record_type = record.get("record_type")
        capture_status = payload.get("capture_status", payload.get("sample_status"))
        if capture_status != "complete":
            continue
        if record_type == "request_terminal":
            if record.get("request_class") != "observer":
                raise AttributionError("a complete terminal metrics capture is not observer traffic")
            if record.get("method") not in {"GET", "HEAD"}:
                raise AttributionError("a complete terminal metrics capture is not a GET/HEAD request")
            if record.get("route") != data.header.get("metrics_path"):
                raise AttributionError("a complete terminal metrics capture is not the configured metrics route")
            if record.get("terminal_status") != "complete":
                raise AttributionError("a complete terminal metrics capture has a non-complete request terminal")
            if record.get("response_status") != 200 or record.get("response_body_complete") is not True:
                raise AttributionError("a complete terminal metrics capture does not have a complete 200 response")
            if payload.get("source") != "observed_asgi_metrics_route":
                raise AttributionError("terminal metrics capture source is not the observed ASGI metrics route")
            record_scrape_id = _text(record.get("scrape_id"), "terminal metrics scrape_id")
            if payload.get("scrape_id") != record_scrape_id:
                raise AttributionError("terminal metrics capture scrape_id differs from its request terminal")
            if payload.get("scrape_phase") != record.get("scrape_phase"):
                raise AttributionError("terminal metrics capture phase differs from its request terminal")
            if payload.get("metrics_request_observation_id") != record.get("observation_id"):
                raise AttributionError("terminal metrics capture is not linked to its request observation")
            response_hash = _text(record.get("response_body_sha256"), "terminal metrics response_body_sha256")
            if payload.get("raw_sha256") != response_hash:
                raise AttributionError("terminal metrics raw_sha256 differs from the response body hash")
            response_bytes = _integer(record.get("response_body_bytes"), "terminal metrics response_body_bytes")
            if payload.get("raw_bytes") != response_bytes:
                raise AttributionError("terminal metrics raw_bytes differs from the response body byte count")
        elif record_type == "metrics_snapshot":
            if payload.get("source") != "in_process_registry":
                raise AttributionError("registry metrics snapshot has an unsupported source")
        elif record_type == "postcompletion_metrics":
            if payload.get("source") != "in_process_registry":
                raise AttributionError("post-completion metrics sample has an unsupported source")
        scrape_id_value = payload.get("scrape_id") or payload.get("sample_id")
        scrape_id = _text(scrape_id_value, "metrics scrape_id")
        if scrape_id in seen_ids:
            raise AttributionError(f"metrics scrape ID is duplicated: {scrape_id}")
        seen_ids.add(scrape_id)
        raw_path_value = payload.get("raw_path")
        if not isinstance(raw_path_value, str) or not raw_path_value.strip():
            raise AttributionError(f"complete metrics scrape {scrape_id} has no raw_path")
        raw_path = Path(raw_path_value).expanduser()
        if not raw_path.is_absolute():
            raw_path = data.path.parent / raw_path
        raw_path = _regular_file(raw_path, f"metrics artifact {scrape_id}")
        raw_hash = _text(payload.get("raw_sha256"), f"metrics scrape {scrape_id} raw_sha256")
        if len(raw_hash) != 64:
            raise AttributionError(f"metrics scrape {scrape_id} raw_sha256 is not SHA-256")
        try:
            int(raw_hash, 16)
            raw = raw_path.read_bytes()
        except (ValueError, OSError) as exc:
            raise AttributionError(f"cannot read metrics artifact {scrape_id}: {exc}") from exc
        if _sha256(raw) != raw_hash:
            raise AttributionError(f"metrics scrape {scrape_id} raw bytes do not match raw_sha256")
        raw_bytes = _integer(payload.get("raw_bytes"), f"metrics scrape {scrape_id} raw_bytes")
        if raw_bytes != len(raw):
            raise AttributionError(f"metrics scrape {scrape_id} raw_bytes does not match artifact")
        started = payload.get("scrape_started_monotonic_ns", payload.get("sample_started_monotonic_ns"))
        ended = payload.get("scrape_ended_monotonic_ns", payload.get("sample_ended_monotonic_ns"))
        captured = payload.get("captured_monotonic_ns")
        started_ns = _integer(started, f"metrics scrape {scrape_id} started_monotonic_ns")
        ended_ns = _integer(ended, f"metrics scrape {scrape_id} ended_monotonic_ns")
        captured_ns = _integer(captured, f"metrics scrape {scrape_id} captured_monotonic_ns")
        if not started_ns < captured_ns <= ended_ns:
            raise AttributionError(f"metrics scrape {scrape_id} capture timestamp is outside its window")
        if record_type == "request_terminal":
            request_start = data.starts[record["observation_id"]]
            if started_ns != request_start["started_monotonic_ns"]:
                raise AttributionError(f"ASGI scrape {scrape_id} start differs from request ingress")
            if ended_ns != record["terminal_monotonic_ns"]:
                raise AttributionError(f"ASGI scrape {scrape_id} end differs from request terminal")
            response_final_ns = _integer(
                record.get("response_final_monotonic_ns"), f"ASGI scrape {scrape_id} response_final_monotonic_ns"
            )
            if captured_ns != response_final_ns:
                raise AttributionError(f"ASGI scrape {scrape_id} capture differs from successful final response send")
        phase_value = payload.get("scrape_phase")
        phase = None if phase_value is None else _text(phase_value, f"metrics scrape {scrape_id} scrape_phase")
        associated = payload.get("associated_observation_id") or payload.get("metrics_request_observation_id")
        if associated is not None:
            associated = _text(associated, f"metrics scrape {scrape_id} associated_observation_id")
            if associated not in data.starts:
                raise AttributionError(f"metrics scrape {scrape_id} references an unknown request observation")
            if record_type == "postcompletion_metrics" and data.starts[associated].get("request_class") != "model":
                raise AttributionError(f"post-completion metrics scrape {scrape_id} is not bound to a model request")
        elif record_type == "postcompletion_metrics":
            raise AttributionError(f"post-completion metrics scrape {scrape_id} has no associated request")
        result.append(
            MetricEvidence(
                record=record,
                payload=payload,
                sequence=_integer(record.get("sequence"), f"metrics scrape {scrape_id} sequence", positive=True),
                scrape_id=scrape_id,
                phase=phase,
                raw_path=raw_path,
                raw_sha256=raw_hash,
                raw_bytes=raw_bytes,
                started_ns=started_ns,
                captured_ns=captured_ns,
                ended_ns=ended_ns,
                associated_observation_id=associated,
            )
        )
    return tuple(result)


def _select_metric(
    metrics: Sequence[MetricEvidence],
    requested_id: Optional[str],
    phase: str,
) -> MetricEvidence:
    if requested_id is not None:
        requested = _text(requested_id, f"{phase}_scrape_id")
        matches = [item for item in metrics if item.scrape_id == requested]
        if len(matches) != 1:
            raise AttributionError(f"{phase} scrape ID {requested!r} does not identify exactly one complete scrape")
        return matches[0]
    matches = [item for item in metrics if item.phase == phase]
    if len(matches) != 1:
        raise AttributionError(
            f"{phase} scrape phase must identify exactly one complete scrape; found {len(matches)}"
        )
    return matches[0]


def _snapshot(evidence: MetricEvidence, data: ObserverJournal) -> ServingSnapshot:
    try:
        raw = evidence.raw_path.read_bytes()
    except OSError as exc:
        raise AttributionError(f"cannot reread {evidence.scrape_id} metrics artifact: {exc}") from exc
    try:
        parsed = parse_prometheus_text(raw)
    except Exception as exc:
        raise AttributionError(f"metrics scrape {evidence.scrape_id} is not valid Prometheus text: {exc}") from exc
    header = data.header
    clock = dict(header["clock"])
    return ServingSnapshot(
        raw=raw,
        url=str(evidence.payload.get("metrics_url") or data.header.get("metrics_path") or "http://server/metrics"),
        server_identity=_text(header.get("server_identity"), "server_identity"),
        counter_epoch=_text(header.get("counter_epoch"), "counter_epoch"),
        captured_at_utc=str(evidence.payload.get("captured_at_utc") or "unknown"),
        captured_monotonic_ns=evidence.captured_ns,
        parsed=parsed,
        raw_sha256=evidence.raw_sha256,
        clock=clock,
        scrape_started_monotonic_ns=evidence.started_ns,
        scrape_ended_monotonic_ns=evidence.ended_ns,
        scrape_phase=evidence.phase,
    )


def _interval_overlaps(start: int, end: int, window_start: int, window_end: int) -> bool:
    return start < window_end and end > window_start


def _unavailable_row(
    *,
    request_id: str,
    journal_path: Path,
    journal_sha256: Optional[str],
    reason: str,
    header: Optional[Mapping[str, Any]] = None,
    before: Optional[MetricEvidence] = None,
    after: Optional[MetricEvidence] = None,
) -> Dict[str, Any]:
    header = header or {}
    clock = header.get("clock") if isinstance(header.get("clock"), Mapping) else None
    return {
        "schema_version": ATTRIBUTION_SCHEMA,
        "evidence_kind": ATTRIBUTION_EVIDENCE_KIND,
        "status": "unavailable",
        "producer_status": "unavailable",
        "provenance": "unavailable",
        "request_id": request_id,
        "server_identity": header.get("server_identity"),
        "server_pid": header.get("server_pid"),
        "server_process_start_ticks": header.get("server_process_start_ticks"),
        "lease_id": header.get("lease_id"),
        "dedicated_server": header.get("dedicated_server"),
        "counter_epoch": header.get("counter_epoch"),
        "vllm_version": header.get("vllm_version", VLLM_VERSION),
        "observer_instance_id": header.get("observer_instance_id"),
        "observer_source_sha256": header.get("observer_source_sha256"),
        "clock": dict(clock) if clock else None,
        "proxy_elapsed_used": False,
        "unavailable_reason": reason,
        "journal": {
            "path": str(journal_path),
            "sha256": journal_sha256,
        },
        "scrapes": {
            "before": _scrape_record(before),
            "after": _scrape_record(after),
        },
        "native_measurement": None,
        "derived_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _scrape_record(evidence: Optional[MetricEvidence]) -> Optional[Dict[str, Any]]:
    if evidence is None:
        return None
    return {
        "scrape_id": evidence.scrape_id,
        "phase": evidence.phase,
        "raw_path": str(evidence.raw_path),
        "raw_sha256": evidence.raw_sha256,
        "raw_bytes": evidence.raw_bytes,
        "scrape_started_monotonic_ns": evidence.started_ns,
        "captured_monotonic_ns": evidence.captured_ns,
        "scrape_ended_monotonic_ns": evidence.ended_ns,
        "associated_observation_id": evidence.associated_observation_id,
        "journal_sequence": evidence.sequence,
    }


def _successful_row(
    data: ObserverJournal,
    request_id: str,
    target_start: Mapping[str, Any],
    target_terminal: Mapping[str, Any],
    before: MetricEvidence,
    after: MetricEvidence,
    measurement: Any,
    watermark: Mapping[str, Any],
) -> Dict[str, Any]:
    row = measurement.to_record(
        before_raw_path=str(before.raw_path),
        after_raw_path=str(after.raw_path),
    )
    row.update({
        "schema_version": ATTRIBUTION_SCHEMA,
        "evidence_kind": ATTRIBUTION_EVIDENCE_KIND,
        "status": "measured" if measurement.measured else "unavailable",
        "producer_status": "measured" if measurement.measured else "unavailable",
        "provenance": "measured" if measurement.measured else "unavailable",
        "request_id": request_id,
        "server_identity": data.header["server_identity"],
        "server_pid": data.header["server_pid"],
        "server_process_start_ticks": data.header["server_process_start_ticks"],
        "lease_id": data.header["lease_id"],
        "dedicated_server": data.header["dedicated_server"],
        "counter_epoch": data.header["counter_epoch"],
        "vllm_version": data.header["vllm_version"],
        "observer_instance_id": data.header["observer_instance_id"],
        "observer_source_sha256": data.header["observer_source_sha256"],
        "clock": dict(data.header["clock"]),
        "proxy_elapsed_used": False,
        "unavailable_reason": None if measurement.measured else (
            measurement.context_reason
            or "; ".join(
                value.reason or "native metric unavailable"
                for value in measurement.metrics.values()
                if not value.measured
            )
        ),
        "journal": {
            "path": str(data.path),
            "sha256": data.raw_sha256,
            "first_sequence": 1,
            "last_sequence": data.records[-1]["sequence"],
            "completeness_watermark_sequence": watermark["sequence"],
            "covered_through_monotonic_ns": watermark["covered_through_monotonic_ns"],
        },
        "target_request": {
            "observation_id": target_start["observation_id"],
            "case_id": target_start.get("case_id"),
            "attempt_id": target_start.get("attempt_id"),
            "started_monotonic_ns": target_start["started_monotonic_ns"],
            "terminal_monotonic_ns": target_terminal["terminal_monotonic_ns"],
            "terminal_status": target_terminal.get("terminal_status"),
            "response_status": target_terminal.get("response_status"),
        },
        "scrapes": {
            "before": _scrape_record(before),
            "after": _scrape_record(after),
        },
        "native_count_delta_checks_reused": True,
        "derived_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    return row


def _derive_checked(
    data: ObserverJournal,
    request_id: str,
    before_scrape_id: Optional[str],
    after_scrape_id: Optional[str],
    expected_vllm_version: str,
) -> Dict[str, Any]:
    header = data.header
    if expected_vllm_version != VLLM_VERSION or header.get("vllm_version") != VLLM_VERSION:
        raise AttributionError("configured or observed vLLM version is not pinned 0.10.0")
    if header.get("dedicated_server") is not True:
        raise AttributionError("observer header does not prove a dedicated server lease")
    lease_id = _text(header.get("lease_id"), "observer lease_id")
    if lease_id == "unbound-observer-lease":
        raise AttributionError("observer lease is an unbound placeholder")
    metrics = _metric_evidence(data)
    before = _select_metric(metrics, before_scrape_id, "before")
    after = _select_metric(metrics, after_scrape_id, "after")
    if before.scrape_id == after.scrape_id:
        raise AttributionError("before and after scrapes must have different IDs")
    if before.ended_ns > after.started_ns:
        raise AttributionError("before and after scrape windows overlap")

    target_starts = [
        record
        for record in data.starts.values()
        if record.get("physical_request_id") == request_id
    ]
    if len(target_starts) != 1:
        raise AttributionError(
            f"physical request ID {request_id!r} must identify exactly one observer request start"
        )
    target_start = target_starts[0]
    target_observation_id = target_start["observation_id"]
    target_terminal = data.terminals.get(target_observation_id)
    if target_terminal is None:
        raise AttributionError("target physical request has no durable terminal record")
    if target_start.get("request_class") != "model":
        raise AttributionError("target physical request is not classified as a model request")
    if target_start.get("request_id_error") is not None:
        raise AttributionError("target physical request ID header was malformed or duplicated")
    if target_terminal.get("terminal_status") != "complete":
        raise AttributionError("target physical request did not complete")
    if target_terminal.get("request_body_complete") is not True:
        raise AttributionError("target physical request ingress body is incomplete")
    if target_terminal.get("response_body_complete") is not True:
        raise AttributionError("target physical request response body is incomplete")
    if target_terminal.get("client_disconnected") is not False:
        raise AttributionError("target physical request disconnected")
    if target_terminal.get("error") is not None:
        raise AttributionError("target physical request has an application or transport error")
    target_start_ns = _integer(target_start.get("started_monotonic_ns"), "target started_monotonic_ns")
    target_end_ns = _integer(target_terminal.get("terminal_monotonic_ns"), "target terminal_monotonic_ns")
    if before.ended_ns > target_start_ns:
        raise AttributionError("before scrape does not finish before target request ingress")
    if after.started_ns < target_end_ns:
        raise AttributionError("after scrape does not start after target request terminal")
    window_start = before.started_ns
    window_end = after.ended_ns

    for observation_id, start in data.starts.items():
        terminal = data.terminals.get(observation_id)
        started_ns = _integer(start.get("started_monotonic_ns"), f"request {observation_id} started_monotonic_ns")
        if terminal is None:
            if started_ns < window_end:
                raise AttributionError(f"pending request {observation_id} enters the attribution window")
            continue
        terminal_ns = _integer(terminal.get("terminal_monotonic_ns"), f"request {observation_id} terminal_monotonic_ns")
        if (observation_id != target_observation_id
                and start.get("request_class") in {"model", "foreign"}
                and terminal_ns <= window_start):
            # HTTP completion cannot certify engine cancellation or histogram
            # publication. No native publication acknowledgement is available
            # in this bounded observer; a count of one could belong to this
            # prior request while the target's update is still pending.
            raise AttributionError(
                f"late prior request {observation_id} has no native publication proof before baseline"
            )
        if not _interval_overlaps(started_ns, terminal_ns, window_start, window_end):
            continue
        if observation_id == target_observation_id:
            continue
        request_class = start.get("request_class")
        if request_class == "foreign":
            raise AttributionError(f"foreign request {observation_id} overlaps the native metrics window")
        if request_class == "model":
            raise AttributionError(f"competing model request {observation_id} overlaps the native metrics window")
        if request_class != "observer":
            raise AttributionError(f"unknown request class {request_class!r} overlaps the native metrics window")

    # An unfinished start before the baseline, or a delayed sample associated
    # with an earlier model request, makes a later aggregate counter update
    # ambiguous.  The target is the only permitted model association.
    for evidence in metrics:
        if not _interval_overlaps(evidence.started_ns, evidence.ended_ns, window_start, window_end):
            continue
        if evidence.scrape_id in {before.scrape_id, after.scrape_id}:
            continue
        raise AttributionError(
            f"unselected or late prior metrics sample {evidence.scrape_id} overlaps the attribution window"
        )

    eligible_watermarks = [
        watermark
        for watermark in data.watermarks
        if watermark["sequence"] > after.sequence
        and watermark["covered_through_monotonic_ns"] >= after.ended_ns
        and watermark.get("observer_alive") is True
        and watermark.get("fatal_error") is None
    ]
    if not eligible_watermarks:
        raise AttributionError("no healthy completeness watermark covers the after scrape")
    watermark = min(eligible_watermarks, key=lambda item: item["sequence"])
    pending = watermark.get("pending_observation_ids", [])
    if pending:
        pending_in_window = [
            observation_id
            for observation_id in pending
            if data.starts.get(observation_id, {}).get("started_monotonic_ns", window_end) < window_end
        ]
        if pending_in_window:
            raise AttributionError(
                "completeness watermark still has ingress requests pending: "
                + ", ".join(sorted(pending_in_window))
            )
    for fatal in data.fatal_records:
        if fatal["sequence"] <= watermark["sequence"]:
            raise AttributionError("observer fatal record precedes the completeness watermark")
    for shutdown in data.shutdown_records:
        if shutdown["sequence"] <= after.sequence:
            raise AttributionError("observer shut down before the after scrape was covered")

    before_snapshot = _snapshot(before, data)
    after_snapshot = _snapshot(after, data)
    witness = AccessWitness(
        request_id=request_id,
        server_identity=_text(header.get("server_identity"), "server_identity"),
        lease_id=lease_id,
        counter_epoch=_text(header.get("counter_epoch"), "counter_epoch"),
        observed_request_ids=(request_id,),
        other_request_ids=(),
        dedicated_server=True,
        no_other_requests=True,
    )
    try:
        measurement = derive_serving_metrics(before_snapshot, after_snapshot, witness, vllm_version=expected_vllm_version)
    except ServingMetricsError as exc:
        raise AttributionError(f"native serving metric derivation rejected evidence: {exc}") from exc
    return _successful_row(data, request_id, target_start, target_terminal, before, after, measurement, watermark)


def _derive_native_checked(data: ObserverJournal, path: Path, request_id: str) -> Dict[str, Any]:
    from agentic_sim.telemetry.native_vllm_observer import (
        NATIVE_SCHEMA, NATIVE_SOURCE, PHASE_FIELDS, canonical_bytes, finite_number,
    )
    descriptor = data.header.get("native_observer")
    if not isinstance(descriptor, Mapping):
        raise AttributionError("ASGI observer has no installed native hook binding")
    try:
        raw = _regular_file(path, "native journal").read_bytes()
    except OSError as exc:
        raise AttributionError(f"cannot read native journal: {exc}") from exc
    if not raw or not raw.endswith(b"\n"):
        raise AttributionError("native journal is empty or has an incomplete final record")
    rows = [_strict_json(line, index) for index, line in enumerate(raw.splitlines(), 1)]
    header = rows[0]
    if (header.get("record_type") != "native_header" or header.get("binding") != descriptor.get("binding")
            or header.get("vllm_version") != VLLM_VERSION
            or header.get("append_only") is not True or header.get("durable") is not True):
        raise AttributionError("native header differs from the installed ASGI source binding")
    finished = []
    watermarks = []
    reconciliations = []
    shutdown_seen = False
    for index, record in enumerate(rows, 1):
        if (record.get("schema_version") != NATIVE_SCHEMA or record.get("native_source") != NATIVE_SOURCE
                or _record_identity(record, "native record") != data.identity
                or record.get("native_instance_id") != descriptor.get("native_instance_id")):
            raise AttributionError("native journal changes source/clock/process/epoch identity")
        if _integer(record.get("sequence"), "native sequence", positive=True) != index:
            raise AttributionError("native journal sequence is missing or reordered")
        kind = record.get("record_type")
        if kind == "native_header" and index == 1:
            continue
        if shutdown_seen:
            raise AttributionError("native evidence occurs after shutdown")
        if kind == "native_error":
            raise AttributionError(f"native observer capture failed: {record.get('error')}")
        if kind == "native_shutdown":
            shutdown_seen = True
        elif kind == "native_watermark":
            covered = _integer(record.get("covered_through_monotonic_ns"), "native coverage")
            when = _integer(record.get("watermark_monotonic_ns"), "native watermark time")
            asgi_sequence = _integer(record.get("asgi_sequence"), "native ASGI sequence")
            if (covered > when or record.get("observer_alive") is not True
                    or record.get("fatal_error") is not None
                    or record.get("covers_through_sequence") != index
                    or asgi_sequence > len(data.records)):
                raise AttributionError("native completeness watermark is invalid")
            watermarks.append(record)
        elif kind == "native_http_terminal":
            linked = data.terminals.get(record.get("observation_id"))
            if linked is None or linked["sequence"] != record.get("http_terminal_sequence"):
                raise AttributionError("native HTTP reconciliation lacks its exact ASGI terminal")
            for key in ("physical_request_id", "http_request_id", "serving_request_id", "terminal_monotonic_ns"):
                if record.get(key) != linked.get(key):
                    raise AttributionError("native HTTP reconciliation differs from ASGI terminal identity")
            reconciliations.append(record)
        elif kind == "native_finished":
            payload = record.get("raw")
            if not isinstance(payload, Mapping):
                raise AttributionError("native finished record lacks raw projection")
            encoded = canonical_bytes(payload)
            if record.get("raw_sha256") != _sha256(encoded) or record.get("raw_bytes") != len(encoded):
                raise AttributionError("native finished raw hash/byte count mismatch")
            before = _integer(record.get("finished_list_count_before"), "native count before")
            after = _integer(record.get("finished_list_count_after"), "native count after")
            if after != before + 1:
                raise AttributionError("native finished append delta is not exactly one")
            _text(payload.get("engine_request_id"), "native engine request ID")
            _integer(record.get("observed_monotonic_ns"), "native observation time")
            finished.append(record)
        else:
            raise AttributionError(f"unknown native record type {kind!r}")

    targets = [r for r in data.starts.values() if r.get("physical_request_id") == request_id]
    if len(targets) != 1:
        raise AttributionError("native attribution requires exactly one physical request start")
    start = targets[0]
    terminal = data.terminals.get(start["observation_id"])
    if (start.get("request_class") != "model" or start.get("method") != "POST"
            or start.get("route") != "/v1/chat/completions"
            or start.get("request_id_error") is not None
            or start.get("http_request_id_error") is not None
            or start.get("http_request_id") != request_id):
        raise AttributionError("native target lacks the verified chat X-Request-Id/physical-ID mapping")
    expected_id = "chatcmpl-" + request_id  # Exact pinned serving_chat construction, not a fuzzy prefix join.
    if terminal is None or terminal.get("serving_request_id") != expected_id:
        raise AttributionError("native target lacks matching actual serving request metadata ID")
    target_reconciliations = [r for r in reconciliations if r.get("observation_id") == start["observation_id"]]
    if len(target_reconciliations) != 1 or target_reconciliations[0].get("native_capture_error") is not None:
        raise AttributionError("native target HTTP reconciliation is missing, duplicated or failed")
    if (terminal.get("terminal_status") != "complete" or terminal.get("request_body_complete") is not True
            or terminal.get("response_body_complete") is not True
            or terminal.get("client_disconnected") is not False or terminal.get("error") is not None
            or terminal.get("response_status") != 200):
        raise AttributionError("native target HTTP request is incomplete, failed or disconnected")
    candidates = [r for r in finished if r["raw"].get("engine_request_id") == expected_id
                  or r["raw"].get("parent_request_id") == expected_id]
    if len(candidates) != 1 or candidates[0]["raw"].get("parent_request_id") is not None:
        raise AttributionError("native engine request is missing, duplicated or has multiple/parented children")
    sample = candidates[0]
    started = start["started_monotonic_ns"]
    ended = max(terminal["terminal_monotonic_ns"], sample["observed_monotonic_ns"])
    if sample["observed_monotonic_ns"] < started:
        raise AttributionError("native sample predates target ingress")
    if data.header.get("dedicated_server") is not True or data.header.get("lease_id") == "unbound-observer-lease":
        raise AttributionError("native target lacks a dedicated server binding")
    for observation_id, other in data.starts.items():
        if observation_id == start["observation_id"]:
            continue
        other_end = data.terminals.get(observation_id)
        if other_end is None and other["started_monotonic_ns"] < ended:
            raise AttributionError("pending ingress overlaps native evidence window")
        if (other_end is not None and other.get("request_class") != "observer"
                and _interval_overlaps(other["started_monotonic_ns"], other_end["terminal_monotonic_ns"], started, ended)):
            raise AttributionError("foreign or competing model ingress overlaps native evidence window")
    for other in finished:
        if other is not sample and started <= other["observed_monotonic_ns"] <= ended:
            raise AttributionError("foreign or competing native completion overlaps target evidence window")
    asgi_marks = [r for r in data.watermarks if r["sequence"] > terminal["sequence"]
                  and r["covered_through_monotonic_ns"] >= terminal["terminal_monotonic_ns"]]
    if not asgi_marks:
        raise AttributionError("no ASGI completeness watermark covers native target terminal")
    asgi_mark = min(asgi_marks, key=lambda r: r["sequence"])
    native_marks = [r for r in watermarks if r["sequence"] > sample["sequence"]
                    and r["sequence"] > target_reconciliations[0]["sequence"]
                    and r["covered_through_monotonic_ns"] >= ended
                    and r["asgi_sequence"] >= asgi_mark["sequence"]]
    if not native_marks:
        raise AttributionError("no native completeness watermark covers request and native sample")
    native_mark = min(native_marks, key=lambda r: r["sequence"])
    if any(r["sequence"] <= native_mark["asgi_sequence"] for r in data.fatal_records):
        raise AttributionError("ASGI observer fatal before native evidence coverage")
    payload = sample["raw"]
    phases = payload.get("finished")
    if not isinstance(phases, Mapping):
        raise AttributionError("native finished phase values are missing")
    try:
        for field in PHASE_FIELDS:
            finite_number(phases.get(field), field)
            finite_number(phases[field] * 1000., field + " milliseconds")
    except ValueError as exc:
        raise AttributionError(str(exc)) from exc
    fields = {"queue": "queued_time", "prefill": "prefill_time", "decode": "decode_time", "e2e": "e2e_latency"}
    return {
        "schema_version": ATTRIBUTION_SCHEMA, "evidence_kind": ATTRIBUTION_EVIDENCE_KIND,
        "request_id": request_id, "producer_status": "measured", "status": "measured", "provenance": "measured",
        "native_metric_source": NATIVE_SOURCE, "native_count_delta_checks_reused": False,
        "proxy_elapsed_used": False, "cuda_kernel_timing": False, "unavailable_reason": None,
        "server_identity": data.header["server_identity"], "server_pid": data.header["server_pid"],
        "server_process_start_ticks": data.header["server_process_start_ticks"],
        "observer_instance_id": data.header["observer_instance_id"],
        "vllm_version": VLLM_VERSION, "dedicated_server": True,
        "counter_epoch": data.header["counter_epoch"], "clock": data.header["clock"],
        "lease_id": data.header["lease_id"], "observer_source_sha256": data.header["observer_source_sha256"],
        "journal": {"path": str(data.path), "sha256": data.raw_sha256,
                    "completeness_watermark_sequence": asgi_mark["sequence"]},
        "native_journal": {"path": str(path), "sha256": _sha256(raw),
                           "record_sequence": sample["sequence"], "raw_sha256": sample["raw_sha256"],
                           "completeness_watermark_sequence": native_mark["sequence"], "binding": header["binding"]},
        "target_request": {"observation_id": start["observation_id"], "engine_request_id": expected_id,
                           "case_id": start.get("case_id"), "attempt_id": start.get("attempt_id"),
                           "started_monotonic_ns": started, "terminal_monotonic_ns": terminal["terminal_monotonic_ns"]},
        "native_measurement": payload,
        "metrics": {name: {"value_ms": phases[field] * 1000., "count_delta": None,
                           "source_field": field, "status": "measured", "provenance": "measured",
                           "scope": "native_per_request", "reason": "exact finished-request ID and native field"}
                    for name, field in fields.items()},
    }


def derive_server_attribution(
    *,
    journal: str | Path,
    request_id: str,
    before_scrape_id: Optional[str] = None,
    after_scrape_id: Optional[str] = None,
    expected_vllm_version: str = VLLM_VERSION,
    native_journal: Optional[str | Path] = None,
) -> Dict[str, Any]:
    """Return a measured sidecar row or an explicit unavailable row."""

    request_id = _text(request_id, "request_id")
    journal_path = Path(journal).expanduser()
    journal_sha256: Optional[str] = None
    header: Optional[Mapping[str, Any]] = None
    try:
        raw = _regular_file(journal_path, "observer journal").read_bytes()
        journal_sha256 = _sha256(raw)
    except AttributionError as exc:
        return _unavailable_row(
            request_id=request_id,
            journal_path=journal_path,
            journal_sha256=journal_sha256,
            reason=str(exc),
        )
    try:
        data = read_observer_journal(journal_path)
        header = data.header
        if native_journal is not None:
            if expected_vllm_version != VLLM_VERSION:
                raise AttributionError("native attribution requires pinned vLLM 0.10.0")
            return _derive_native_checked(data, Path(native_journal), request_id)
        return _derive_checked(
            data,
            request_id,
            before_scrape_id,
            after_scrape_id,
            expected_vllm_version,
        )
    except AttributionError as exc:
        result = _unavailable_row(
            request_id=request_id,
            journal_path=journal_path,
            journal_sha256=journal_sha256,
            reason=str(exc),
            header=header,
        )
        if native_journal is not None:
            result["native_metric_source"] = "vllm_v1_finished_request_stats"
            native_path = Path(native_journal)
            try:
                native_digest = _sha256(_regular_file(native_path, "native journal").read_bytes())
            except (AttributionError, OSError):
                native_digest = None
            result["native_journal"] = {"path": str(native_path), "sha256": native_digest}
        return result


def _validate_existing_output(path: Path) -> Set[str]:
    if not path.exists():
        return set()
    existing = _regular_file(path, "attribution output")
    try:
        raw = existing.read_bytes()
    except OSError as exc:
        raise AttributionError(f"cannot read attribution output: {exc}") from exc
    ids: Set[str] = set()
    for line_number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            raise AttributionError(f"attribution output line {line_number} is blank")
        value = _strict_json(line, line_number)
        if value.get("schema_version") != ATTRIBUTION_SCHEMA or value.get("evidence_kind") != ATTRIBUTION_EVIDENCE_KIND:
            raise AttributionError(f"attribution output line {line_number} has an unsupported schema")
        item = _text(value.get("request_id"), f"attribution output line {line_number} request_id")
        if item in ids:
            raise AttributionError(f"attribution output already contains duplicate request ID {item}")
        ids.add(item)
    return ids


def append_sidecar(path: str | Path, row: Mapping[str, Any]) -> None:
    """Append one sidecar row with short-write and fsync handling."""

    output = Path(path).expanduser()
    if _contains_symlink_component(output):
        raise AttributionError(f"attribution output contains a symlink: {output}")
    request_id = _text(row.get("request_id"), "sidecar request_id")
    if request_id in _validate_existing_output(output):
        raise AttributionError(f"attribution output already contains request ID {request_id}")
    try:
        encoded = (json.dumps(dict(row), sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AttributionError(f"sidecar row is not finite canonical JSON: {exc}") from exc
    output.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(str(output), flags, 0o640)
    except OSError as exc:
        raise AttributionError(f"cannot open attribution output: {exc}") from exc
    try:
        if output.is_symlink() or not output.is_file():
            raise AttributionError(f"attribution output is not a regular file: {output}")
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            view = memoryview(encoded)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("sidecar append made no progress")
                view = view[written:]
            os.fsync(fd)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except AttributionError:
        raise
    except OSError as exc:
        raise AttributionError(f"attribution sidecar durability failed: {exc}") from exc
    finally:
        os.close(fd)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", required=True, type=Path)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--before-scrape-id", "--before-scrape", dest="before_scrape_id")
    parser.add_argument("--after-scrape-id", "--after-scrape", dest="after_scrape_id")
    parser.add_argument("--vllm-version", default=VLLM_VERSION)
    parser.add_argument("--native-journal", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    row = derive_server_attribution(
        journal=args.journal,
        request_id=args.request_id,
        before_scrape_id=args.before_scrape_id,
        after_scrape_id=args.after_scrape_id,
        expected_vllm_version=args.vllm_version,
        native_journal=args.native_journal,
    )
    if not args.dry_run:
        try:
            append_sidecar(args.output, row)
        except AttributionError as exc:
            print(f"UNAVAILABLE: {exc}", file=sys.stderr)
            return 3
    status = row.get("producer_status")
    if status == "measured":
        print(
            f"attribution {'validated' if args.dry_run else 'appended'}: "
            f"request_id={row['request_id']} journal_sha256={row['journal']['sha256']}"
        )
        return 0
    print(f"UNAVAILABLE: {row.get('unavailable_reason')}", file=sys.stderr)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ATTRIBUTION_EVIDENCE_KIND",
    "ATTRIBUTION_SCHEMA",
    "AttributionError",
    "ObserverJournal",
    "append_sidecar",
    "derive_server_attribution",
    "main",
    "read_observer_journal",
]
