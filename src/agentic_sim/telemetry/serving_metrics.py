"""Fail-closed attribution of one isolated request to native vLLM metrics.

The vLLM ``/metrics`` endpoint exposes server aggregates.  A pair of scrapes
can be associated with one request only when the caller supplies an explicit
server-lease witness proving that the server was dedicated to that request.
This module keeps that distinction visible in every result:

* a native per-request sample carrying the expected request id is preferred;
* otherwise, the pinned vLLM 0.10 histogram families are attributed to an
  isolated request only when every relevant ``_count`` delta is exactly one;
* missing, reset, ambiguous, non-finite, or cross-epoch data is unavailable.

No value is calculated from proxy elapsed time.  Raw Prometheus bytes and
their SHA-256 hashes remain attached to snapshots and can be durably written
before the derived record is exported.
"""

from __future__ import annotations

import hashlib
import math
import os
import tempfile
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit

from agentic_sim.observability.vllm_metrics import (
    PrometheusSample,
    PrometheusSnapshot,
    parse_prometheus_text,
)
from agentic_sim.telemetry.clock import clock_fields, monotonic_ns, utc_now


SERVING_METRICS_SCHEMA = "assignment.serving-metrics.v1"
VLLM_VERSION = "0.10.0"

# These names are the native vLLM 0.10.0 names recorded in
# agentic_sim.observability.vllm_metrics.  The values are seconds; exported
# result values are milliseconds for consistency with the v2 telemetry rows.
VLLM_REQUEST_METRICS: Mapping[str, str] = {
    "queue": "vllm:request_queue_time_seconds",
    "prefill": "vllm:request_prefill_time_seconds",
    "decode": "vllm:request_decode_time_seconds",
    "e2e": "vllm:e2e_request_latency_seconds",
}
_REQUEST_LABELS = ("request_id", "request", "rid")


def _contains_symlink_component(path: Path) -> bool:
    """Check an un-resolved path and its existing ancestors for symlinks."""

    current = path.expanduser().absolute()
    while True:
        if current.is_symlink():
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def _same_prometheus_snapshot(left: PrometheusSnapshot, right: PrometheusSnapshot) -> bool:
    """Compare parsed snapshots while treating NaN as the same raw token."""

    if left.types != right.types or len(left) != len(right):
        return False
    for old, new in zip(left, right):
        if (
            old.name != new.name
            or dict(old.labels) != dict(new.labels)
            or old.metric_type != new.metric_type
            or old.timestamp_ms != new.timestamp_ms
        ):
            return False
        if math.isnan(old.value) and math.isnan(new.value):
            continue
        if old.value != new.value:
            return False
    return True


class ServingMetricsError(ValueError):
    """The serving-metrics witness or snapshot cannot be used safely."""


@dataclass(frozen=True)
class AccessWitness:
    """Evidence that one physical request owned a dedicated vLLM server.

    ``observed_request_ids`` is the access log/lease union for the complete
    before/after interval.  It must contain exactly ``request_id``.  The two
    explicit boolean fields prevent a caller from accidentally treating a
    merely named server as an isolated server.
    """

    request_id: str
    server_identity: str
    lease_id: str
    counter_epoch: str
    observed_request_ids: tuple[str, ...] = ()
    other_request_ids: tuple[str, ...] = ()
    dedicated_server: bool = False
    no_other_requests: bool = False

    def __post_init__(self) -> None:
        for name in ("request_id", "server_identity", "lease_id", "counter_epoch"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ServingMetricsError(f"{name} must be non-empty text")
        for name in ("dedicated_server", "no_other_requests"):
            if not isinstance(getattr(self, name), bool):
                raise ServingMetricsError(f"{name} must be a JSON boolean")
        for name in ("observed_request_ids", "other_request_ids"):
            value = getattr(self, name)
            if isinstance(value, str) or not isinstance(value, tuple):
                raise ServingMetricsError(f"{name} must be a tuple of request ids")
            if any(not isinstance(item, str) or not item.strip() for item in value):
                raise ServingMetricsError(f"{name} contains an invalid request id")
            if len(set(value)) != len(value):
                raise ServingMetricsError(f"{name} contains duplicate request ids")

    @property
    def server_lease_id(self) -> str:
        """Compatibility spelling used by server lease manifests."""

        return self.lease_id

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AccessWitness":
        """Construct a witness from a JSON manifest or proxy hook payload."""

        if not isinstance(value, Mapping):
            raise ServingMetricsError("access witness must be a mapping")

        def resolve(name: str, *aliases: str) -> Any:
            present = [key for key in (name,) + aliases if key in value]
            if not present:
                raise ServingMetricsError(f"{name} is required")
            selected = value[present[0]]
            if any(value[key] != selected for key in present[1:]):
                raise ServingMetricsError(f"{name} and its aliases disagree")
            return selected

        def ids(name: str, *aliases: str) -> tuple[str, ...]:
            raw = resolve(name, *aliases)
            if not isinstance(raw, (list, tuple)):
                raise ServingMetricsError(f"{name} must be a JSON array of request ids")
            return tuple(raw)

        def text(name: str, *aliases: str) -> str:
            raw = resolve(name, *aliases)
            if not isinstance(raw, str):
                raise ServingMetricsError(f"{name} must be text")
            return raw

        def boolean(name: str, *aliases: str) -> bool:
            raw = resolve(name, *aliases)
            if not isinstance(raw, bool):
                raise ServingMetricsError(f"{name} must be a JSON boolean")
            return raw

        return cls(
            request_id=text("request_id"),
            server_identity=text("server_identity", "server_id"),
            lease_id=text("lease_id", "server_lease_id"),
            counter_epoch=text("counter_epoch", "metrics_counter_epoch"),
            observed_request_ids=ids("observed_request_ids", "request_ids"),
            other_request_ids=ids("other_request_ids", "other_ids"),
            dedicated_server=boolean("dedicated_server", "isolated"),
            no_other_requests=boolean("no_other_requests", "no_other_request_ids"),
        )

    def validate(self) -> None:
        """Require a complete, positive isolation witness."""

        if not self.dedicated_server:
            raise ServingMetricsError("server lease is not marked dedicated")
        if not self.no_other_requests:
            raise ServingMetricsError("server lease does not verify absence of other requests")
        if set(self.other_request_ids):
            raise ServingMetricsError("server lease lists other request ids")
        observed = set(self.observed_request_ids)
        if observed != {self.request_id}:
            raise ServingMetricsError(
                "server lease request-id union must contain exactly the expected request"
            )


@dataclass(frozen=True)
class ServingSnapshot:
    """One lossless native ``/metrics`` scrape."""

    raw: bytes
    url: str
    server_identity: str
    counter_epoch: str
    captured_at_utc: str
    captured_monotonic_ns: int
    parsed: Optional[PrometheusSnapshot]
    raw_sha256: str
    parse_error: Optional[str] = None
    scrape_error: Optional[str] = None
    clock: Mapping[str, Any] = field(default_factory=dict)
    scrape_started_monotonic_ns: int | None = None
    scrape_ended_monotonic_ns: int | None = None
    scrape_phase: str | None = None
    scrape_id: str | None = None
    associated_physical_request_id: str | None = None

    def __post_init__(self) -> None:
        bounds = (self.scrape_started_monotonic_ns, self.scrape_ended_monotonic_ns)
        if any(value is not None for value in bounds):
            if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in bounds):
                raise ServingMetricsError("scrape bounds must both be non-negative integer timestamps")
            if not bounds[0] <= self.captured_monotonic_ns <= bounds[1]:
                raise ServingMetricsError("snapshot capture timestamp is outside scrape bounds")
        if not isinstance(self.raw, bytes):
            raise ServingMetricsError("raw metrics must be bytes")
        expected = hashlib.sha256(self.raw).hexdigest()
        if self.raw_sha256 != expected:
            raise ServingMetricsError("raw metrics SHA-256 does not match snapshot bytes")
        for name in ("url", "server_identity", "counter_epoch"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ServingMetricsError(f"{name} must be non-empty text")
        if self.parsed is not None and self.parse_error is not None:
            raise ServingMetricsError("parsed snapshot cannot carry parse_error")
        if self.parsed is not None:
            try:
                exact = parse_prometheus_text(self.raw)
            except Exception as exc:
                raise ServingMetricsError(
                    "parsed snapshot cannot be validated from raw metrics bytes"
                ) from exc
            if not _same_prometheus_snapshot(self.parsed, exact):
                raise ServingMetricsError("parsed snapshot does not match raw metrics bytes")
        elif self.parse_error is None and self.scrape_error is None:
            # A successful raw response must carry the parser result used for
            # derivation.  An unavailable scrape may intentionally carry no
            # raw bytes and no parsed content.
            try:
                parse_prometheus_text(self.raw)
            except Exception as exc:
                raise ServingMetricsError(
                    "unparsed raw metrics require parse_error or scrape_error"
                ) from exc
            else:
                raise ServingMetricsError("successful raw metrics require parsed content")

    @classmethod
    def from_raw(
        cls,
        raw: bytes | str,
        *,
        url: str,
        server_identity: str,
        counter_epoch: str,
        captured_at_utc: Optional[str] = None,
        captured_monotonic_ns: Optional[int] = None,
    ) -> "ServingSnapshot":
        if isinstance(raw, str):
            raw_bytes = raw.encode("utf-8")
        elif isinstance(raw, bytes):
            raw_bytes = raw
        else:
            raise ServingMetricsError("raw metrics must be bytes or text")
        parsed: Optional[PrometheusSnapshot]
        parse_error: Optional[str]
        try:
            parsed = parse_prometheus_text(raw_bytes)
            parse_error = None
        except Exception as exc:  # preserve the bytes and report an unavailable result later
            parsed = None
            parse_error = f"{type(exc).__name__}: {exc}"
        return cls(
            raw=raw_bytes,
            url=url,
            server_identity=server_identity,
            counter_epoch=counter_epoch,
            captured_at_utc=captured_at_utc or utc_now(),
            captured_monotonic_ns=(
                captured_monotonic_ns
                if captured_monotonic_ns is not None
                else monotonic_ns()
            ),
            parsed=parsed,
            raw_sha256=hashlib.sha256(raw_bytes).hexdigest(),
            parse_error=parse_error,
            clock=clock_fields(),
        )

    @classmethod
    def unavailable(
        cls,
        *,
        url: str,
        server_identity: str,
        counter_epoch: str,
        error: str,
        raw: bytes = b"",
        captured_at_utc: Optional[str] = None,
        captured_monotonic_ns: Optional[int] = None,
    ) -> "ServingSnapshot":
        if not isinstance(error, str) or not error:
            raise ServingMetricsError("unavailable snapshot requires an error")
        if not isinstance(raw, bytes):
            raise ServingMetricsError("unavailable snapshot raw metrics must be bytes")
        return cls(
            raw=raw,
            url=url,
            server_identity=server_identity,
            counter_epoch=counter_epoch,
            captured_at_utc=captured_at_utc or utc_now(),
            captured_monotonic_ns=(
                captured_monotonic_ns
                if captured_monotonic_ns is not None
                else monotonic_ns()
            ),
            parsed=None,
            raw_sha256=hashlib.sha256(raw).hexdigest(),
            scrape_error=error,
            clock=clock_fields(),
        )

    @property
    def raw_text(self) -> str:
        return self.raw.decode("utf-8", "replace")

    @property
    def available(self) -> bool:
        return self.parsed is not None and self.scrape_error is None

    def to_record(self, *, raw_path: Optional[str] = None) -> dict[str, Any]:
        """Return metadata for a durable record without dropping hash/reason."""

        return {
            "schema_version": SERVING_METRICS_SCHEMA + ".snapshot",
            "url": self.url,
            "server_identity": self.server_identity,
            "counter_epoch": self.counter_epoch,
            "captured_at_utc": self.captured_at_utc,
            "captured_monotonic_ns": self.captured_monotonic_ns,
            "scrape_started_monotonic_ns": self.scrape_started_monotonic_ns,
            "scrape_ended_monotonic_ns": self.scrape_ended_monotonic_ns,
            "scrape_phase": self.scrape_phase,
            "scrape_id": self.scrape_id,
            "associated_physical_request_id": self.associated_physical_request_id,
            "clock": dict(self.clock),
            "raw_sha256": self.raw_sha256,
            "raw_bytes": len(self.raw),
            "raw_path": raw_path,
            "status": "measured" if self.available else "unavailable",
            "parse_error": self.parse_error,
            "scrape_error": self.scrape_error,
            "sample_count": len(self.parsed) if self.parsed is not None else 0,
        }

    def write_raw(self, path: str | Path, *, overwrite: bool = False) -> Path:
        """Durably preserve the exact response bytes at a regular-file path."""

        destination = Path(path).expanduser()
        if _contains_symlink_component(destination):
            raise ServingMetricsError(f"refusing symlink raw snapshot: {destination}")
        if destination.exists() and not overwrite:
            raise FileExistsError(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if _contains_symlink_component(destination):
            raise ServingMetricsError(
                f"refusing symlink raw snapshot path: {destination}"
            )
        fd, temporary_name = tempfile.mkstemp(
            prefix=destination.name + ".", dir=str(destination.parent)
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(self.raw)
                handle.flush()
                os.fsync(handle.fileno())
            temporary = Path(temporary_name)
            if destination.exists() and not overwrite:
                temporary.unlink(missing_ok=True)
                raise FileExistsError(destination)
            os.replace(temporary, destination)
            if hasattr(os, "O_DIRECTORY"):
                directory_fd = os.open(
                    destination.parent, os.O_RDONLY | os.O_DIRECTORY
                )
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except BaseException:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return destination


@dataclass(frozen=True)
class ServingMetricValue:
    """One per-request serving metric, or an explicit unavailable value."""

    metric: str
    value_ms: Optional[float]
    count_delta: Optional[float]
    status: str
    provenance: str
    scope: str
    reason: Optional[str] = None

    @property
    def measured(self) -> bool:
        return self.status == "measured"

    def to_record(self) -> dict[str, Any]:
        return {
            "value_ms": self.value_ms,
            "count_delta": self.count_delta,
            "status": self.status,
            "provenance": self.provenance,
            "scope": self.scope,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ServingMeasurement:
    """Derived result plus the snapshots and witness that justify it."""

    request_id: str
    server_identity: str
    lease_id: str
    counter_epoch: str
    vllm_version: str
    before: ServingSnapshot
    after: ServingSnapshot
    witness: AccessWitness
    metrics: Mapping[str, ServingMetricValue]
    context_reason: Optional[str] = None

    @property
    def measured(self) -> bool:
        return bool(self.metrics) and all(item.measured for item in self.metrics.values())

    @property
    def status(self) -> str:
        measured = sum(item.measured for item in self.metrics.values())
        if measured == len(self.metrics) and measured:
            return "measured"
        if measured:
            return "incomplete"
        return "unavailable"

    def to_record(
        self,
        *,
        before_raw_path: Optional[str] = None,
        after_raw_path: Optional[str] = None,
    ) -> dict[str, Any]:
        return {
            "schema_version": SERVING_METRICS_SCHEMA,
            "status": self.status,
            "provenance": "measured" if self.measured else "unavailable",
            "request_id": self.request_id,
            "server_identity": self.server_identity,
            "server_lease_id": self.lease_id,
            "counter_epoch": self.counter_epoch,
            "vllm_version": self.vllm_version,
            "native_metric_source": "vllm_metrics_endpoint",
            "proxy_elapsed_used": False,
            "context_reason": self.context_reason,
            "before_raw_sha256": self.before.raw_sha256,
            "after_raw_sha256": self.after.raw_sha256,
            "witness": {
                "dedicated_server": self.witness.dedicated_server,
                "no_other_requests": self.witness.no_other_requests,
                "observed_request_ids": list(self.witness.observed_request_ids),
                "other_request_ids": list(self.witness.other_request_ids),
            },
            "snapshots": {
                "before": self.before.to_record(raw_path=before_raw_path),
                "after": self.after.to_record(raw_path=after_raw_path),
            },
            "metrics": {name: value.to_record() for name, value in sorted(self.metrics.items())},
        }


def _unavailable(metric: str, reason: str) -> ServingMetricValue:
    return ServingMetricValue(
        metric=metric,
        value_ms=None,
        count_delta=None,
        status="unavailable",
        provenance="unavailable",
        scope="unknown",
        reason=reason,
    )


def _finite_nonnegative(value: float) -> bool:
    return math.isfinite(value) and value >= 0.0


def _request_label(sample: PrometheusSample) -> Optional[str]:
    for name in _REQUEST_LABELS:
        if name in sample.labels:
            return name
    return None


def _direct_per_request_value(
    snapshot: ServingSnapshot, family: str, request_id: str
) -> tuple[Optional[float], Optional[str], bool]:
    """Return (value, reason, found) for a native request-labelled sample."""

    if snapshot.parsed is None:
        return (
            None,
            snapshot.parse_error or snapshot.scrape_error or "metrics scrape unavailable",
            False,
        )
    candidates: list[PrometheusSample] = []
    for sample in snapshot.parsed:
        if sample.name != family:
            continue
        label = _request_label(sample)
        if label is None:
            continue
        if sample.labels.get(label) != request_id:
            return None, "native metric contains another request id", True
        # A quantile or bucket is not a single-request measurement.
        if "quantile" in sample.labels or "le" in sample.labels:
            continue
        candidates.append(sample)
    if not candidates:
        return None, None, False
    if len(candidates) != 1:
        return None, "multiple native per-request samples", True
    value = candidates[0].value
    if not _finite_nonnegative(value):
        return None, "native per-request sample is non-finite or negative", True
    return value, None, True


def _series(
    snapshot: ServingSnapshot,
    family: str,
    part: str,
    *,
    request_id: Optional[str] = None,
) -> Optional[dict[tuple[tuple[str, str], ...], float]]:
    if snapshot.parsed is None:
        return None
    name = family + "_" + part
    result: dict[tuple[tuple[str, str], ...], float] = {}
    for sample in snapshot.parsed:
        if sample.name != name:
            continue
        if sample.metric_type != "histogram":
            return None
        label = _request_label(sample)
        if request_id is not None:
            if label is None:
                # An unlabelled server aggregate is not the native
                # per-request path; the caller only supplies request_id here
                # when filtering a labelled series.
                continue
            if sample.labels.get(label) != request_id:
                continue
        elif label is not None:
            # A different request id in an ostensibly aggregate scrape would
            # make attribution ambiguous.  The caller detects this via None.
            pass
        key = tuple(sorted(sample.labels.items()))
        if key in result:
            return None
        result[key] = sample.value
    return result


def _labelled_request_ids(snapshot: ServingSnapshot, family: str) -> set[str]:
    if snapshot.parsed is None:
        return set()
    ids: set[str] = set()
    for sample in snapshot.parsed:
        if sample.family_name != family:
            continue
        label = _request_label(sample)
        if label is not None:
            ids.add(sample.labels[label])
    return ids


def _aggregate_histogram_value(
    before: ServingSnapshot,
    after: ServingSnapshot,
    family: str,
    request_id: str,
) -> tuple[Optional[float], Optional[float], str, str]:
    """Return seconds, count delta, status, reason for one histogram family."""

    if before.parsed is None or after.parsed is None:
        reason = (
            before.parse_error
            or before.scrape_error
            or after.parse_error
            or after.scrape_error
        )
        return None, None, "unavailable", reason or "metrics scrape unavailable"

    before_ids = _labelled_request_ids(before, family)
    after_ids = _labelled_request_ids(after, family)
    all_ids = before_ids | after_ids
    if all_ids and all_ids != {request_id}:
        return None, None, "unavailable", "native histogram contains another request id"
    # A request-labelled histogram is safe to filter to the expected request;
    # an ordinary vLLM 0.10 scrape is server aggregate and uses no request id.
    filtered_request = request_id if all_ids == {request_id} else None

    counts_before = _series(before, family, "count", request_id=filtered_request)
    counts_after = _series(after, family, "count", request_id=filtered_request)
    sums_before = _series(before, family, "sum", request_id=filtered_request)
    sums_after = _series(after, family, "sum", request_id=filtered_request)
    if any(value is None for value in (counts_before, counts_after, sums_before, sums_after)):
        return None, None, "unavailable", "histogram series missing or duplicated"
    assert counts_before is not None
    assert counts_after is not None
    assert sums_before is not None
    assert sums_after is not None
    if not counts_before or not counts_after or not sums_before or not sums_after:
        return None, None, "unavailable", "histogram count/sum series unavailable"
    if set(counts_before) != set(counts_after) or set(sums_before) != set(sums_after):
        return None, None, "unavailable", "histogram series changed between scrapes"
    if set(counts_before) != set(sums_before) or set(counts_after) != set(sums_after):
        return None, None, "unavailable", "histogram count/sum label sets differ"

    all_values = (
        list(counts_before.values())
        + list(counts_after.values())
        + list(sums_before.values())
        + list(sums_after.values())
    )
    for value in all_values:
        if not _finite_nonnegative(value):
            return None, None, "unavailable", "histogram count/sum is non-finite or negative"
    if any(counts_after[key] < counts_before[key] for key in counts_before):
        return None, None, "unavailable", "histogram count counter reset or decreased"
    if any(sums_after[key] < sums_before[key] for key in sums_before):
        return None, None, "unavailable", "histogram sum counter reset or decreased"

    count_delta = sum(counts_after.values()) - sum(counts_before.values())
    sum_delta = sum(sums_after.values()) - sum(sums_before.values())
    if not _finite_nonnegative(count_delta) or not _finite_nonnegative(sum_delta):
        return None, None, "unavailable", "histogram delta is non-finite or negative"
    if count_delta != 1.0:
        return (
            None,
            count_delta,
            "unavailable",
            f"relevant histogram count delta is {count_delta:g}, expected 1",
        )
    return sum_delta, count_delta, "measured", "isolated server aggregate with exact count delta 1"


def _context_reason(
    before: ServingSnapshot,
    after: ServingSnapshot,
    witness: AccessWitness,
) -> Optional[str]:
    for phase, snapshot in (("before", before), ("after", after)):
        if snapshot.scrape_error is not None:
            return f"{phase} metrics scrape failed: {snapshot.scrape_error}"
    try:
        witness.validate()
    except ServingMetricsError as exc:
        return str(exc)
    for key in ("hostname", "boot_id", "clock_id"):
        if not before.clock.get(key) or before.clock.get(key) != after.clock.get(key):
            return f"metrics scrape clock identity missing or changed: {key}"
    if before.captured_monotonic_ns > after.captured_monotonic_ns:
        return "metrics scrape order is reversed"
    if (before.scrape_ended_monotonic_ns is not None
            and after.scrape_started_monotonic_ns is not None
            and before.scrape_ended_monotonic_ns > after.scrape_started_monotonic_ns):
        return "metrics scrape intervals overlap"
    if (
        before.server_identity != witness.server_identity
        or after.server_identity != witness.server_identity
    ):
        return "server identity does not match access witness"
    if before.server_identity != after.server_identity:
        return "server identity changed between scrapes"
    if (
        before.counter_epoch != witness.counter_epoch
        or after.counter_epoch != witness.counter_epoch
    ):
        return "metrics counter epoch does not match access witness"
    if before.counter_epoch != after.counter_epoch:
        return "metrics counter epoch changed between scrapes"
    return None


def derive_serving_metrics(
    before: ServingSnapshot,
    after: ServingSnapshot,
    witness: AccessWitness | Mapping[str, Any],
    *,
    vllm_version: str = VLLM_VERSION,
) -> ServingMeasurement:
    """Derive four native timings for one witnessed physical request.

    The function never reads a proxy duration.  A valid isolated-server
    witness allows the server aggregate histogram delta to be associated with
    the one request; without it every metric is explicitly unavailable.
    """

    if not isinstance(before, ServingSnapshot) or not isinstance(after, ServingSnapshot):
        raise ServingMetricsError("before and after must be ServingSnapshot values")
    if isinstance(witness, Mapping):
        witness = AccessWitness.from_mapping(witness)
    if not isinstance(witness, AccessWitness):
        raise ServingMetricsError("witness must be AccessWitness or mapping")
    if not isinstance(vllm_version, str) or not vllm_version.strip():
        raise ServingMetricsError("vLLM version must be non-empty text")

    reason = _context_reason(before, after, witness)
    values: dict[str, ServingMetricValue] = {}
    for key, family in VLLM_REQUEST_METRICS.items():
        metric_name = family
        if reason is not None:
            values[key] = _unavailable(metric_name, reason)
            continue

        # Native request-labelled samples take precedence over aggregate
        # deltas.  This path has an exact request identity and therefore does
        # not guess from a server-wide value.
        after_direct, direct_reason, after_found = _direct_per_request_value(
            after, family, witness.request_id
        )
        before_direct, before_reason, before_found = _direct_per_request_value(
            before, family, witness.request_id
        )
        if after_found or before_found:
            if before_found and before_direct is None:
                values[key] = _unavailable(
                    metric_name,
                    before_reason or "native per-request sample unavailable before request",
                )
            elif after_direct is None:
                values[key] = _unavailable(
                    metric_name,
                    direct_reason or "native per-request sample unavailable after request",
                )
            elif (
                before_found
                and before_direct is not None
                and not _finite_nonnegative(before_direct)
            ):
                values[key] = _unavailable(
                    metric_name,
                    before_reason or "native per-request sample unavailable",
                )
            else:
                values[key] = ServingMetricValue(
                    metric=metric_name,
                    value_ms=after_direct * 1000.0,
                    count_delta=1.0,
                    status="measured",
                    provenance="measured",
                    scope="native_per_request",
                    reason="native request-labelled sample",
                )
            continue

        seconds, count_delta, status, aggregate_reason = _aggregate_histogram_value(
            before, after, family, witness.request_id
        )
        if status != "measured" or seconds is None:
            values[key] = ServingMetricValue(
                metric=metric_name,
                value_ms=None,
                count_delta=count_delta,
                status="unavailable",
                provenance="unavailable",
                scope="isolated_server_aggregate",
                reason=aggregate_reason,
            )
        else:
            values[key] = ServingMetricValue(
                metric=metric_name,
                value_ms=seconds * 1000.0,
                count_delta=count_delta,
                status="measured",
                provenance="measured",
                scope="isolated_server_aggregate",
                reason=aggregate_reason,
            )

    return ServingMeasurement(
        request_id=witness.request_id,
        server_identity=witness.server_identity,
        lease_id=witness.lease_id,
        counter_epoch=witness.counter_epoch,
        vllm_version=vllm_version,
        before=before,
        after=after,
        witness=witness,
        metrics=values,
        context_reason=reason,
    )


class _HTTPFetchError(OSError):
    """An HTTP error whose response body remains available as raw evidence."""

    def __init__(self, status: int, reason: Any, raw: bytes):
        super().__init__(f"HTTP {status}: {reason}")
        self.status = status
        self.raw = raw


def _http_fetch(url: str, headers: Mapping[str, str], timeout: float) -> bytes:
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except OSError:
            raw = b""
        raise _HTTPFetchError(exc.code, exc.reason, raw) from exc


Fetcher = Callable[[str, Mapping[str, str], float], bytes]


class ServingMetricsCollector:
    """Read-only before/after collector for one leased vLLM server."""

    def __init__(
        self,
        metrics_url: str,
        *,
        server_identity: str,
        counter_epoch: str,
        timeout: float = 3.0,
        headers: Optional[Mapping[str, str]] = None,
        fetcher: Optional[Fetcher] = None,
    ) -> None:
        if not isinstance(metrics_url, str) or not metrics_url.strip():
            raise ServingMetricsError("metrics_url must be non-empty text")
        parsed_url = urlsplit(metrics_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ServingMetricsError("metrics_url must be an HTTP(S) URL")
        if not isinstance(server_identity, str) or not server_identity.strip():
            raise ServingMetricsError("server_identity must be non-empty text")
        if not isinstance(counter_epoch, str) or not counter_epoch.strip():
            raise ServingMetricsError("counter_epoch must be non-empty text")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or timeout <= 0
        ):
            raise ServingMetricsError("timeout must be positive")
        self.metrics_url = metrics_url
        self.server_identity = server_identity
        self.counter_epoch = counter_epoch
        self.timeout = float(timeout)
        self.headers = {"Accept": "text/plain; version=0.0.4", **dict(headers or {})}
        self.fetcher = fetcher or _http_fetch

    def scrape(self, *, phase: str = "interval", physical_request_id: str | None = None) -> ServingSnapshot:
        """Bracket the complete fetch/parser interval in the recorded local clock.

        The server's counter sample happened somewhere inside this interval;
        the response timestamp alone cannot prove an external access window.
        Raw bytes, errors and brackets also survive failed scrapes.
        """

        scrape_id = "scrape-" + uuid.uuid4().hex
        headers = {**self.headers, "X-EIC-Scrape-ID": scrape_id,
                   "X-EIC-Scrape-Phase": phase, "X-EIC-Observer-Request": "1"}
        started = monotonic_ns()
        snapshot = self._scrape(headers)
        # _scrape has already validated the exact raw/parsed pair. This fresh
        # object is still private to this call. Set only acquisition metadata
        # here: dataclasses.replace would unnecessarily parse the raw bytes
        # again, including work after the recorded end of the scrape.
        object.__setattr__(snapshot, "scrape_started_monotonic_ns", started)
        object.__setattr__(snapshot, "scrape_phase", phase)
        object.__setattr__(snapshot, "scrape_id", scrape_id)
        object.__setattr__(snapshot, "associated_physical_request_id", physical_request_id)
        ended = monotonic_ns()
        object.__setattr__(snapshot, "scrape_ended_monotonic_ns", ended)
        return snapshot

    def _scrape(self, headers: Mapping[str, str]) -> ServingSnapshot:
        """GET only the native metrics endpoint and preserve its response."""

        try:
            raw = self.fetcher(self.metrics_url, headers, self.timeout)
            if isinstance(raw, str):
                raw = raw.encode("utf-8")
            if not isinstance(raw, bytes):
                raise ServingMetricsError("metrics fetcher must return bytes or text")
            return ServingSnapshot.from_raw(
                raw,
                url=self.metrics_url,
                server_identity=self.server_identity,
                counter_epoch=self.counter_epoch,
            )
        except _HTTPFetchError as exc:
            if exc.raw:
                snapshot = ServingSnapshot.from_raw(
                    exc.raw,
                    url=self.metrics_url,
                    server_identity=self.server_identity,
                    counter_epoch=self.counter_epoch,
                )
                return replace(snapshot, scrape_error=f"{type(exc).__name__}: {exc}")
            return ServingSnapshot.unavailable(
                url=self.metrics_url,
                server_identity=self.server_identity,
                counter_epoch=self.counter_epoch,
                error=f"{type(exc).__name__}: {exc}",
            )
        except Exception as exc:
            return ServingSnapshot.unavailable(
                url=self.metrics_url,
                server_identity=self.server_identity,
                counter_epoch=self.counter_epoch,
                error=f"{type(exc).__name__}: {exc}",
            )

    def before_request(self, *, physical_request_id: str | None = None) -> ServingSnapshot:
        return self.scrape(phase="before", physical_request_id=physical_request_id)

    def after_request(self, *, physical_request_id: str | None = None) -> ServingSnapshot:
        return self.scrape(phase="after", physical_request_id=physical_request_id)

    def measure(
        self,
        before: ServingSnapshot,
        after: ServingSnapshot,
        witness: AccessWitness | Mapping[str, Any],
        *,
        vllm_version: str = VLLM_VERSION,
    ) -> ServingMeasurement:
        return derive_serving_metrics(before, after, witness, vllm_version=vllm_version)


def write_snapshot_pair(
    before: ServingSnapshot,
    after: ServingSnapshot,
    output_dir: str | Path,
    *,
    prefix: str = "vllm_metrics",
    overwrite: bool = False,
) -> dict[str, Path]:
    """Write exact before/after bytes and return their durable paths."""

    root = Path(output_dir).expanduser()
    if _contains_symlink_component(root):
        raise ServingMetricsError(f"refusing symlink raw snapshot directory: {root}")
    root.mkdir(parents=True, exist_ok=True)
    if _contains_symlink_component(root):
        raise ServingMetricsError(f"refusing symlink raw snapshot directory: {root}")
    paths = {"before": root / f"{prefix}_before.prom", "after": root / f"{prefix}_after.prom"}
    before.write_raw(paths["before"], overwrite=overwrite)
    try:
        after.write_raw(paths["after"], overwrite=overwrite)
    except BaseException:
        if overwrite is False:
            # Do not remove an existing caller-owned file.  Only clean the
            # file created by this call when the pair could not be completed.
            try:
                paths["before"].unlink()
            except OSError:
                pass
        raise
    return paths


__all__ = [
    "AccessWitness",
    "Fetcher",
    "SERVING_METRICS_SCHEMA",
    "ServingMeasurement",
    "ServingMetricValue",
    "ServingMetricsCollector",
    "ServingMetricsError",
    "ServingSnapshot",
    "VLLM_REQUEST_METRICS",
    "VLLM_VERSION",
    "derive_serving_metrics",
    "write_snapshot_pair",
]
