#!/usr/bin/env python3
"""Record request-level boundaries while transparently forwarding to vLLM.

The legacy boundary stream stores hashes, sizes, status, token counts, and
timing.  A v2 recorder additionally archives the exact request/response body
bytes per physical attempt under its run directory; transport/authentication
headers are never persisted.  Partial bodies are retained with an explicit
incomplete flag.  The proxy is intended for a separate profiled attempt, not
the baseline control.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import math
import os
import signal
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[2]
for candidate in (ROOT, ROOT / "src"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from agentic_sim.observability.nvtx import range as nvtx_range  # noqa: E402
from agentic_sim.telemetry.clock import clock_fields, monotonic_ns, utc_now  # noqa: E402
try:  # The reviewed fixture proxy may be copied without the optional v2 package.
    from agentic_sim.telemetry.v2 import TelemetryV2, stable_id  # noqa: E402
except ImportError:  # pragma: no cover - exercised by minimal offline fixtures
    TelemetryV2 = None  # type: ignore[assignment,misc]

    def stable_id(prefix: str, *parts: Any) -> str:  # type: ignore[no-redef]
        encoded = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        return f"{prefix}-{hashlib.sha256((prefix + chr(0)).encode() + encoded).hexdigest()[:32]}"
try:  # Serving metrics are optional for legacy/fixture proxy copies.
    from agentic_sim.telemetry.serving_metrics import (  # noqa: E402
        AccessWitness,
        SERVING_METRICS_SCHEMA,
        ServingMeasurement,
        ServingMetricsCollector,
        ServingMetricsError,
        ServingSnapshot,
        VLLM_REQUEST_METRICS,
        VLLM_VERSION,
        write_snapshot_pair,
    )
except ImportError:  # pragma: no cover - exercised by minimal offline fixtures
    AccessWitness = None  # type: ignore[assignment,misc]
    SERVING_METRICS_SCHEMA = "assignment.serving-metrics.v1"
    ServingMeasurement = None  # type: ignore[assignment,misc]
    ServingMetricsCollector = None  # type: ignore[assignment,misc]
    ServingMetricsError = ValueError  # type: ignore[assignment,misc]
    ServingSnapshot = None  # type: ignore[assignment,misc]
    VLLM_REQUEST_METRICS = {}
    VLLM_VERSION = "0.10.0"
    write_snapshot_pair = None  # type: ignore[assignment,misc]
from agentic_sim.runners.case_lifecycle import (  # noqa: E402
    deadline_from_env,
    remaining_seconds,
)


_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

# These headers are proxy-owned correlation identity.  Every case-insensitive
# spelling supplied by a client is removed before the authoritative values are
# added below; forwarding two values would make the ASGI observer record an
# ambiguous join instead of the bound case/attempt.
_PROXY_OWNED_IDENTITY_HEADERS = frozenset(
    {
        "x-eic-request-id",
        "x-eic-physical-request-id",
        "x-request-id",
        "x-eic-case-id",
        "x-eic-attempt-id",
        # No observer run-id header exists today, but reject a spoofed future
        # spelling rather than silently forwarding an unbound run identity.
        "x-eic-run-id",
    }
)

class JsonlWriter:
    """Thread-safe append-only writer with one JSON object per line."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError(f"request proxy events path must not be a symlink: {self.path}")
        self.path.touch(exist_ok=True)
        if self.path.is_symlink() or not self.path.is_file():
            raise ValueError(f"request proxy events path must be a regular file: {self.path}")
        self._lock = threading.Lock()

    def append(self, value: dict[str, Any]) -> None:
        encoded = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
        with self._lock, self.path.open("ab") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _usage_mapping(body: bytes) -> Mapping[str, Any] | None:
    """Return usage from a JSON or OpenAI-compatible SSE response body.

    The raw response bytes remain the authoritative artifact.  This helper
    only projects an explicitly supplied ``usage`` object; it never infers a
    cache value from prompt length or server policy.  vLLM commonly emits
    usage in the final SSE data frame, so treating every non-JSON body as
    unavailable would silently discard the only cache-token observation.
    """

    try:
        payload = json.loads(body.decode("utf-8"))
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
    usage = payload.get("usage") if isinstance(payload, Mapping) else None
    return usage if isinstance(usage, Mapping) else None


def _token_counts(body: bytes) -> dict[str, int | None]:
    usage = _usage_mapping(body)
    if not isinstance(usage, Mapping):
        return {
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
            "cached_tokens": None,
        }

    def integer(name: str) -> int | None:
        value = usage.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    prompt_details = usage.get("prompt_tokens_details")
    cached_value = prompt_details.get("cached_tokens") if isinstance(prompt_details, dict) else None
    cached_tokens = (
        cached_value
        if isinstance(cached_value, int) and not isinstance(cached_value, bool) and cached_value >= 0
        else None
    )
    return {
        "prompt_tokens": integer("prompt_tokens"),
        "completion_tokens": integer("completion_tokens"),
        "total_tokens": integer("total_tokens"),
        # vLLM only exposes cache accounting in the nested usage details.
        # Missing/null details stay null; a reset or a disabled server must
        # never be relabeled as a measured zero-cache response.
        "cached_tokens": cached_tokens,
    }


def _request_features(body: bytes) -> dict[str, int | float | None]:
    """Extract non-content request controls that are known before execution."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"max_output_tokens": None, "temperature": None}
    if not isinstance(payload, dict):
        return {"max_output_tokens": None, "temperature": None}
    maximum = payload.get("max_completion_tokens", payload.get("max_tokens"))
    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 0:
        maximum = None
    temperature = payload.get("temperature")
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
        temperature = None
    return {"max_output_tokens": maximum, "temperature": temperature}


def _partial_bytes(exc: BaseException) -> bytes | None:
    """Return bytes retained by an exception such as HTTP ``IncompleteRead``."""
    partial = getattr(exc, "partial", None)
    if isinstance(partial, bytes):
        return partial
    if isinstance(partial, (bytearray, memoryview)):
        return bytes(partial)
    return None


def _read_upstream_body(upstream: Any) -> bytes:
    """Read an upstream body incrementally so socket failures retain bytes."""
    read1 = getattr(upstream, "read1", None)
    if not callable(read1):
        # Test doubles and alternate HTTP implementations may only expose
        # read().  Their IncompleteRead.partial is still handled by the
        # caller; the incremental path below is used by http.client.
        return upstream.read()

    declared_length = getattr(upstream, "length", None)
    remaining: int | None = (
        declared_length
        if isinstance(declared_length, int) and not isinstance(declared_length, bool) and declared_length >= 0
        else None
    )
    chunks: list[bytes] = []
    buffered = b""
    while remaining is None or remaining > 0:
        size = 64 * 1024 if remaining is None else min(64 * 1024, remaining)
        try:
            chunk = read1(size)
        except BaseException as exc:
            buffered = b"".join(chunks)
            partial = _partial_bytes(exc)
            if partial:
                buffered += partial
            try:
                # Built-in socket and HTTP exceptions permit attributes. This
                # keeps the original exception type while exposing all bytes
                # observed before the failure to request finalization.
                exc.partial = buffered  # type: ignore[attr-defined]
            except Exception:
                pass
            raise
        if not isinstance(chunk, bytes):
            raise TypeError("upstream response read did not return bytes")
        if not chunk:
            if remaining is not None and remaining > 0:
                raise http.client.IncompleteRead(b"".join(chunks), remaining)
            break
        chunks.append(chunk)
        if remaining is not None:
            remaining -= len(chunk)
            if remaining < 0:
                partial = b"".join(chunks)
                exc = http.client.HTTPException("upstream response exceeded Content-Length")
                exc.partial = partial  # type: ignore[attr-defined]
                raise exc
    return b"".join(chunks)


SERVING_METRICS_CONFIG_SCHEMA = "assignment.serving-metrics-config.v1"
SERVING_METRICS_CONFIG_FIELDS = frozenset(
    {
        "enabled",
        "metrics_url",
        "server_identity",
        "counter_epoch",
        "timeout_seconds",
        "access_witness_path",
        "access_witness_evidence_kind",
        "vllm_version",
    }
)
SERVING_METRICS_WITNESS_EVIDENCE_KIND = "external_access_lease"
SERVING_METRICS_WITNESS_SCHEMA = "assignment.serving-access-witness.v1"
# Attribution modes.  ``per_request_scrape`` is the historical path: the proxy
# scrapes ``/metrics`` immediately before and after each physical request and
# derives phase timings from the count-delta-one pair plus an external access
# witness.  ``native_deferred`` performs no per-request scrape and no witness
# read: the physical request ID is still bound in ``X-Request-Id`` and the
# original record is written as unavailable, while the server-side ASGI
# observer/native FinishedRequestStats journal supplies attribution later.
SERVING_METRICS_MODE_PER_REQUEST = "per_request_scrape"
SERVING_METRICS_MODE_NATIVE_DEFERRED = "native_deferred"
SERVING_METRICS_MODES = frozenset({SERVING_METRICS_MODE_PER_REQUEST, SERVING_METRICS_MODE_NATIVE_DEFERRED})
NATIVE_DEFERRED_REASON = (
    "native_deferred: per-request metrics scrape and access-witness read are disabled; "
    "attribution is derived later from the server ASGI observer and native request statistics"
)


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


def _disabled_serving_metrics_config() -> dict[str, Any]:
    return {
        "schema_version": SERVING_METRICS_CONFIG_SCHEMA,
        "enabled": False,
        "metrics_url": None,
        "server_identity": None,
        "counter_epoch": None,
        "timeout_seconds": None,
        "access_witness_path": None,
        "access_witness_evidence_kind": SERVING_METRICS_WITNESS_EVIDENCE_KIND,
        "vllm_version": VLLM_VERSION,
    }


def _validate_serving_metrics_config(value: Any) -> dict[str, Any]:
    """Validate the explicit proxy-side native metrics capture descriptor.

    The descriptor intentionally contains no isolation claim.  It names the
    native endpoint and an external access-witness file; the witness record
    must be supplied by an independently running lease/access recorder and is
    checked after each physical request.  A missing witness therefore becomes
    an unavailable serving measurement rather than a proxy-generated proof.
    """

    if value is None:
        return _disabled_serving_metrics_config()
    if not isinstance(value, Mapping):
        raise ValueError("serving metrics config must be a mapping")
    expected = set(SERVING_METRICS_CONFIG_FIELDS) | {"schema_version"}
    # ``mode`` is optional so existing per-request-scrape configs and their
    # hashes remain valid; omitting it means the historical scrape path.
    if set(value) != expected and set(value) != expected | {"mode"}:
        raise ValueError(
            "serving metrics config has missing or unknown fields"
        )
    if value.get("schema_version") != SERVING_METRICS_CONFIG_SCHEMA:
        raise ValueError("serving metrics config has an unsupported schema")
    enabled = value.get("enabled")
    if not isinstance(enabled, bool):
        raise ValueError("serving metrics config enabled must be a JSON boolean")
    mode = value.get("mode", SERVING_METRICS_MODE_PER_REQUEST)
    if mode not in SERVING_METRICS_MODES:
        raise ValueError("serving metrics config mode is unsupported")
    # Keep the validated mapping byte-identical to the input so existing
    # config hashes are unchanged; the capture reads ``mode`` with a default.
    result = dict(value)
    if not enabled:
        if value.get("access_witness_evidence_kind") != SERVING_METRICS_WITNESS_EVIDENCE_KIND:
            raise ValueError(
                "serving metrics config must retain the external access-lease evidence kind"
            )
        if value.get("vllm_version") != VLLM_VERSION:
            raise ValueError("serving metrics config vllm_version is unsupported")
        if any(
            value.get(name) is not None
            for name in (
                "metrics_url",
                "server_identity",
                "counter_epoch",
                "timeout_seconds",
                "access_witness_path",
            )
        ):
            raise ValueError("disabled serving metrics config cannot carry capture settings")
        return result

    for name in ("metrics_url", "server_identity", "counter_epoch", "vllm_version"):
        item = value.get(name)
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"serving metrics config {name} must be non-empty text")
    parsed_url = urlsplit(value["metrics_url"])
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ValueError("serving metrics config metrics_url must be an HTTP(S) URL")
    timeout = value.get("timeout_seconds")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(float(timeout))
        or float(timeout) <= 0
    ):
        raise ValueError("serving metrics config timeout_seconds must be positive")
    witness_path = value.get("access_witness_path")
    if not isinstance(witness_path, str) or not witness_path.strip():
        raise ValueError("serving metrics config access_witness_path is required")
    if not Path(witness_path).expanduser().is_absolute() or "\x00" in witness_path:
        raise ValueError("serving metrics config access_witness_path must be absolute")
    if value.get("access_witness_evidence_kind") != SERVING_METRICS_WITNESS_EVIDENCE_KIND:
        raise ValueError(
            "serving metrics config must require external_access_lease witnesses"
        )
    if value.get("vllm_version") != VLLM_VERSION:
        raise ValueError("serving metrics config vllm_version is unsupported")
    return result


def load_serving_metrics_config(value: str | Path) -> dict[str, Any]:
    """Load and validate a manifest-bound serving metrics config.

    The command-line form accepts either a regular JSON file path or a
    canonical JSON object.  A caller that already has a manifest mapping can
    pass it directly to ``ProxyServer``; this loader exists so the proxy's
    subprocess boundary does not need to interpret the rest of a manifest.
    """

    if isinstance(value, Path):
        raw_value = str(value)
    elif isinstance(value, str) and value.strip():
        raw_value = value
    else:
        raise ValueError("serving metrics config argument must be a path or JSON object")

    looks_like_json = isinstance(value, str) and raw_value.lstrip().startswith("{")
    path = None if looks_like_json else Path(raw_value).expanduser()
    if path is not None and _contains_symlink_component(path):
        raise ValueError(f"serving metrics config must not be a symlink: {path}")
    if path is not None and path.is_file():
        try:
            payload = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"cannot read serving metrics config: {path}: {exc}") from exc
        source = str(path)
    else:
        payload = raw_value
        source = "command-line JSON"
    try:
        parsed = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"serving metrics config is not valid JSON ({source}): {exc}") from exc
    return _validate_serving_metrics_config(parsed)


def _durable_bytes(path: Path, payload: bytes, *, overwrite: bool = False) -> None:
    """Write exact bytes with a file and containing-directory durability fence."""

    path = path.expanduser()
    if _contains_symlink_component(path):
        raise ValueError(f"serving metrics artifact must not be a symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if _contains_symlink_component(path):
        raise ValueError(f"serving metrics artifact path contains a symlink: {path}")
    if path.exists() and not overwrite:
        if path.is_file() and path.read_bytes() == payload:
            return
        raise ValueError(f"serving metrics artifact already contains different bytes: {path}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        if hasattr(os, "O_DIRECTORY"):
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _serving_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:512]


@dataclass
class _ServingRequestState:
    physical_request_id: str
    before: Any


@dataclass
class _ServingCaptureResult:
    """One request's serving evidence and the safe values for end_request."""

    record: dict[str, Any]
    measurement: Any = None
    reference: dict[str, Any] | None = None

    @property
    def status(self) -> str:
        return str(self.record.get("status", "unavailable"))

    @property
    def reason(self) -> str | None:
        value = self.record.get("context_reason") or self.record.get("unavailable_reason")
        return str(value) if value else None

    def timing(self, name: str) -> float | None:
        measurement = self.measurement
        if measurement is None:
            return None
        item = measurement.metrics.get(name)
        if item is None or not item.measured:
            return None
        return float(item.value_ms)

    @property
    def timings_reliable(self) -> bool:
        values = [self.timing(name) for name in ("queue", "prefill", "decode")]
        return any(value is not None for value in values)


class _ServingMetricsCapture:
    """Proxy adapter around the strict native serving metrics collector."""

    def __init__(self, config: Mapping[str, Any], output_dir: Path | None):
        if ServingMetricsCollector is None or ServingSnapshot is None:
            raise ValueError("serving metrics capture requires the v2 telemetry source")
        self.config = _validate_serving_metrics_config(config)
        if not self.config["enabled"]:
            raise ValueError("cannot construct an enabled serving metrics capture from a disabled config")
        self.output_dir = None
        if output_dir is not None:
            candidate_output_dir = Path(output_dir).expanduser()
            if _contains_symlink_component(candidate_output_dir):
                raise ValueError(
                    f"serving metrics output directory must not be a symlink: {candidate_output_dir}"
                )
            candidate_output_dir.mkdir(parents=True, exist_ok=True)
            if _contains_symlink_component(candidate_output_dir):
                raise ValueError(
                    f"serving metrics output directory must not be a symlink: {candidate_output_dir}"
                )
            self.output_dir = candidate_output_dir.resolve()
        encoded_config = json.dumps(self.config, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.config_sha256 = _sha256(encoded_config)
        self.mode = str(self.config.get("mode", SERVING_METRICS_MODE_PER_REQUEST))
        # Native-deferred mode must not even construct the HTTP collector:
        # this keeps the zero-round-trip contract explicit and prevents a
        # future caller from accidentally using it as an implicit fallback.
        if self.mode == SERVING_METRICS_MODE_NATIVE_DEFERRED:
            self.collector = None
        else:
            self.collector = ServingMetricsCollector(
                self.config["metrics_url"],
                server_identity=self.config["server_identity"],
                counter_epoch=self.config["counter_epoch"],
                timeout=float(self.config["timeout_seconds"]),
                # The metrics endpoint is read-only and the Accept header is
                # the only request header needed.  No credentials enter the
                # capture.
            )
        if self.output_dir is not None:
            config_path = self.output_dir / "serving_metrics" / "collector_config.json"
            _durable_bytes(config_path, encoded_config + b"\n")
            self.config_artifact = {
                "path": str(config_path.relative_to(self.output_dir)),
                "sha256": _sha256(config_path.read_bytes()),
            }
        else:
            self.config_artifact = None

    @property
    def witness_path(self) -> Path:
        return Path(self.config["access_witness_path"]).expanduser()

    @property
    def native_deferred(self) -> bool:
        return self.mode == SERVING_METRICS_MODE_NATIVE_DEFERRED

    def _deferred_snapshot(self) -> Any:
        return ServingSnapshot.unavailable(
            url=self.config["metrics_url"],
            server_identity=self.config["server_identity"],
            counter_epoch=self.config["counter_epoch"],
            error=NATIVE_DEFERRED_REASON,
        )

    def begin(self, physical_request_id: str) -> _ServingRequestState:
        if self.native_deferred:
            # No remote round trip inside the measured request window.
            return _ServingRequestState(
                physical_request_id=physical_request_id, before=self._deferred_snapshot()
            )
        try:
            before = self.collector.before_request(physical_request_id=physical_request_id)
        except Exception as exc:  # collector normally converts fetch failures
            before = ServingSnapshot.unavailable(
                url=self.config["metrics_url"],
                server_identity=self.config["server_identity"],
                counter_epoch=self.config["counter_epoch"],
                error=_serving_error(exc),
            )
        return _ServingRequestState(physical_request_id=physical_request_id, before=before)

    def _read_witness(
        self, physical_request_id: str
    ) -> tuple[Any, bytes | None, str | None, str | None]:
        """Read one externally written witness without waiting for it."""

        path = self.witness_path
        if _contains_symlink_component(path):
            return None, None, None, "access witness path is a symlink"
        try:
            raw = path.read_bytes()
        except OSError as exc:
            return None, None, None, f"access witness unavailable: {_serving_error(exc)}"
        if not path.is_file():
            return None, None, None, "access witness is not a regular file"
        source_digest = _sha256(raw)
        matches: list[tuple[Mapping[str, Any], bytes]] = []
        for number, line in enumerate(raw.splitlines(keepends=True), 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                return None, None, source_digest, f"access witness line {number} is invalid JSON: {exc}"
            if not isinstance(value, Mapping):
                return None, None, source_digest, f"access witness line {number} is not an object"
            witness_request_id = value.get("request_id")
            if not isinstance(witness_request_id, str) or not witness_request_id.strip():
                return None, None, source_digest, f"access witness line {number} has no valid request id"
            if value.get("schema_version") != SERVING_METRICS_WITNESS_SCHEMA:
                return None, None, source_digest, f"access witness line {number} has an unsupported schema"
            if value.get("evidence_kind") != self.config["access_witness_evidence_kind"]:
                return None, None, source_digest, f"access witness line {number} has an unsupported evidence kind"
            if witness_request_id == physical_request_id:
                matches.append((value, line))
        if not matches:
            return None, None, source_digest, "no external access witness for physical request"
        if len(matches) != 1:
            return None, None, source_digest, "multiple external access witnesses for physical request"
        value, line = matches[0]
        if value.get("evidence_kind") != self.config["access_witness_evidence_kind"]:
            return None, line, source_digest, "access witness is not an external access-lease record"
        if value.get("schema_version") != SERVING_METRICS_WITNESS_SCHEMA:
            return None, line, source_digest, "access witness has an unsupported schema"
        try:
            witness = AccessWitness.from_mapping(value)
            witness.validate()
        except Exception as exc:
            return None, line, source_digest, f"access witness failed strict validation: {_serving_error(exc)}"
        if witness.request_id != physical_request_id:
            return None, line, source_digest, "access witness request identity differs from physical request"
        if witness.server_identity != self.config["server_identity"]:
            return None, line, source_digest, "access witness server identity differs from configured server"
        if witness.counter_epoch != self.config["counter_epoch"]:
            return None, line, source_digest, "access witness counter epoch differs from configured epoch"
        return witness, line, source_digest, None

    def _unavailable_record(
        self,
        *,
        physical_request_id: str,
        before: Any,
        after: Any,
        reason: str,
        before_path: str | None,
        after_path: str | None,
        witness_source_sha256: str | None,
        witness_artifact: dict[str, Any] | None,
        dispatched: bool,
    ) -> dict[str, Any]:
        return {
            "schema_version": SERVING_METRICS_SCHEMA,
            "status": "unavailable",
            "provenance": "unavailable",
            "request_id": physical_request_id,
            "server_identity": self.config["server_identity"],
            "server_lease_id": None,
            "counter_epoch": self.config["counter_epoch"],
            "vllm_version": self.config["vllm_version"],
            "native_metric_source": "vllm_metrics_endpoint",
            "proxy_elapsed_used": False,
            "context_reason": reason,
            "unavailable_reason": reason,
            "before_raw_sha256": before.raw_sha256,
            "after_raw_sha256": after.raw_sha256,
            "witness": None,
            "witness_source": {
                "path": str(self.witness_path),
                "sha256": witness_source_sha256,
                "evidence_kind": self.config["access_witness_evidence_kind"],
            },
            "snapshots": {
                "before": before.to_record(raw_path=before_path),
                "after": after.to_record(raw_path=after_path),
            },
            "metrics": {
                name: {
                    "value_ms": None,
                    "count_delta": None,
                    "status": "unavailable",
                    "provenance": "unavailable",
                    "scope": "unknown",
                    "reason": reason,
                }
                for name in VLLM_REQUEST_METRICS
            },
            "capture": {
                "config_sha256": self.config_sha256,
                "config_artifact": self.config_artifact,
                "physical_request_dispatched": dispatched,
                "scrape_count": 2 if dispatched else 1,
            },
            "witness_artifact": witness_artifact,
        }

    def finish(
        self,
        state: _ServingRequestState,
        *,
        dispatched: bool,
    ) -> _ServingCaptureResult:
        if self.native_deferred:
            after = self._deferred_snapshot()
        elif dispatched:
            try:
                after = self.collector.after_request(physical_request_id=state.physical_request_id)
            except Exception as exc:
                after = ServingSnapshot.unavailable(
                    url=self.config["metrics_url"],
                    server_identity=self.config["server_identity"],
                    counter_epoch=self.config["counter_epoch"],
                    error=_serving_error(exc),
                )
        else:
            after = ServingSnapshot.unavailable(
                url=self.config["metrics_url"],
                server_identity=self.config["server_identity"],
                counter_epoch=self.config["counter_epoch"],
                error="physical request was not dispatched",
            )

        witness = None
        witness_line = None
        witness_source_sha256 = None
        witness_reason = "physical request was not dispatched"
        if self.native_deferred:
            witness_reason = NATIVE_DEFERRED_REASON
        elif dispatched:
            witness, witness_line, witness_source_sha256, witness_reason = self._read_witness(
                state.physical_request_id
            )

        key = hashlib.sha256(state.physical_request_id.encode("utf-8")).hexdigest()[:32]
        before_path = after_path = None
        artifact_error: str | None = None
        if self.output_dir is None:
            artifact_error = "serving metrics output directory is unavailable"
        elif self.native_deferred:
            # Native-deferred attribution has no request-window snapshots at
            # all.  Its two explicit unavailable snapshots retain the reason
            # and identity, but no empty .prom files are emitted that could be
            # mistaken for a scrape pair during later derivation.
            pass
        else:
            try:
                assert write_snapshot_pair is not None
                paths = write_snapshot_pair(
                    state.before,
                    after,
                    self.output_dir,
                    prefix=f"serving_metrics/{key}",
                )
                before_path = str(paths["before"].relative_to(self.output_dir))
                after_path = str(paths["after"].relative_to(self.output_dir))
            except Exception as exc:
                artifact_error = f"serving metrics raw snapshot archive failed: {_serving_error(exc)}"

        witness_artifact = None
        if witness_line is not None and self.output_dir is not None:
            try:
                witness_path = self.output_dir / "serving_metrics" / f"{key}.witness.jsonl"
                _durable_bytes(witness_path, witness_line)
                witness_artifact = {
                    "path": str(witness_path.relative_to(self.output_dir)),
                    "sha256": _sha256(witness_line),
                    "bytes": len(witness_line),
                    "source_sha256": witness_source_sha256,
                }
            except Exception as exc:
                artifact_error = artifact_error or f"access witness archive failed: {_serving_error(exc)}"

        measurement = None
        reason = witness_reason
        if witness is not None:
            if self.collector is None:
                raise AssertionError("native-deferred capture cannot measure a witness pair")
            try:
                measurement = self.collector.measure(
                    state.before,
                    after,
                    witness,
                    vllm_version=self.config["vllm_version"],
                )
                reason = measurement.context_reason
                if reason is None:
                    for metric in measurement.metrics.values():
                        if not metric.measured and metric.reason:
                            reason = metric.reason
                            break
            except Exception as exc:
                reason = f"serving metrics derivation failed: {_serving_error(exc)}"
        if artifact_error:
            reason = artifact_error if reason is None else f"{reason}; {artifact_error}"
            # A derived value without both lossless raw snapshots and their
            # durable record cannot be used as serving evidence.  Keep the
            # request boundary itself intact while withholding all serving
            # timings from the v2 terminal row.
            measurement = None

        if measurement is not None:
            record = measurement.to_record(
                before_raw_path=before_path,
                after_raw_path=after_path,
            )
            record["witness_source"] = {
                "path": str(self.witness_path),
                "sha256": witness_source_sha256,
                "evidence_kind": self.config["access_witness_evidence_kind"],
            }
            record["capture"] = {
                "config_sha256": self.config_sha256,
                "config_artifact": self.config_artifact,
                "physical_request_dispatched": dispatched,
                "scrape_count": 2 if dispatched else 1,
            }
            record["witness_artifact"] = witness_artifact
            if reason and record.get("context_reason") is None:
                record["context_reason"] = reason
        else:
            record = self._unavailable_record(
                physical_request_id=state.physical_request_id,
                before=state.before,
                after=after,
                reason=reason or "serving metrics unavailable",
                before_path=before_path,
                after_path=after_path,
                witness_source_sha256=witness_source_sha256,
                witness_artifact=witness_artifact,
                dispatched=dispatched,
            )
        record["attribution_mode"] = self.mode
        if self.native_deferred:
            record["capture"]["scrape_count"] = 0
            record["witness_source"] = None

        record_path = None
        record_digest = None
        if self.output_dir is not None:
            try:
                record_path_value = self.output_dir / "serving_metrics" / f"{key}.json"
                encoded = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
                _durable_bytes(record_path_value, encoded)
                record_path = str(record_path_value.relative_to(self.output_dir))
                record_digest = _sha256(encoded)
            except Exception as exc:
                record_error = f"serving metrics record archive failed: {_serving_error(exc)}"
                record_reason = record_error if reason is None else f"{reason}; {record_error}"
                record["unavailable_reason"] = record_reason
                record["context_reason"] = record_reason
                record["status"] = "unavailable"
                record["provenance"] = "unavailable"
                for metric in record.get("metrics", {}).values():
                    if isinstance(metric, Mapping):
                        metric["value_ms"] = None
                        metric["count_delta"] = None
                        metric["status"] = "unavailable"
                        metric["provenance"] = "unavailable"
                        metric["scope"] = "unknown"
                        metric["reason"] = record_reason
                measurement = None
                reason = record_reason
        if record_path is not None:
            reference = {
                "schema_version": SERVING_METRICS_SCHEMA,
                "path": record_path,
                "sha256": record_digest,
                "status": record.get("status"),
                "before_raw_sha256": record["snapshots"]["before"].get("raw_sha256"),
                "after_raw_sha256": record["snapshots"]["after"].get("raw_sha256"),
            }
        else:
            reference = {
                "schema_version": SERVING_METRICS_SCHEMA,
                "path": None,
                "sha256": None,
                "status": record.get("status"),
                "before_raw_sha256": record["snapshots"]["before"].get("raw_sha256"),
                "after_raw_sha256": record["snapshots"]["after"].get("raw_sha256"),
                "unavailable_reason": reason or "serving metrics record was not archived",
            }
        record["record_artifact"] = reference
        return _ServingCaptureResult(record=record, measurement=measurement, reference=reference)


class ProxyHandler(BaseHTTPRequestHandler):
    server: "ProxyServer"

    def setup(self) -> None:
        super().setup()
        self._upstream_connection: http.client.HTTPConnection | None = None
        self._upstream_socket: Any = None
        self.server.register_handler(self)

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            self.server.unregister_handler(self)

    def close_active_sockets(self) -> None:
        """Best-effort close of client and upstream sockets during shutdown."""
        endpoints = (getattr(self, "connection", None), getattr(self, "_upstream_socket", None), getattr(self, "_upstream_connection", None))
        seen: set[int] = set()
        for endpoint in endpoints:
            if endpoint is None or id(endpoint) in seen:
                continue
            seen.add(id(endpoint))
            sock = getattr(endpoint, "sock", None)
            target = sock if sock is not None else endpoint
            shutdown = getattr(target, "shutdown", None)
            if callable(shutdown):
                try:
                    shutdown(2)
                except Exception:
                    pass
            close = getattr(endpoint, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _forward(self) -> None:
        request_id = f"request-{uuid.uuid4().hex}"
        proxy_started = monotonic_ns()
        self._upstream_connection = None
        method = getattr(self, "command", "") or ""
        raw_path = getattr(self, "path", "") or ""
        body = b""
        request_body_complete = False
        status: int | None = None
        response = b""
        response_body_complete = False
        response_headers: list[tuple[str, str]] = []
        error: str | None = None
        failure_phase: str | None = None
        prediction: dict[str, Any] | None = None
        label: dict[str, Any] | None = None
        reveal_attempted = False
        upstream_started: int | None = None
        upstream_ended: int | None = None

        # ``response_started`` is deliberately set before invoking any
        # response method.  A method can write a status line and then raise,
        # and sending a second status in that case corrupts the response.
        response_started = False
        connection: http.client.HTTPConnection | None = None
        process_control_error: BaseException | None = None
        v2 = getattr(self.server, "v2_telemetry", None)
        v2_span: Any | None = None
        v2_request_id: str | None = None
        v2_logical_request_id: str | None = None
        v2_retry_index = 0
        v2_retry_of: str | None = None
        v2_client_span_id: str | None = None
        request_payload_artifact: dict[str, Any] | None = None
        request_payload_pre_dispatch = False
        serving_state: _ServingRequestState | None = None
        serving_result: _ServingCaptureResult | None = None
        serving_capture_error: str | None = None
        physical_request_dispatched = False

        def record_failure(exc: BaseException, phase: str) -> None:
            nonlocal error, failure_phase, process_control_error
            if error is None:
                error = type(exc).__name__
            else:
                error = f"{error}+{type(exc).__name__}"
            if failure_phase is None:
                failure_phase = phase
            if not isinstance(exc, Exception) and process_control_error is None:
                process_control_error = exc

        def send_error_once(code: int, message: str) -> None:
            nonlocal response_started
            if response_started:
                return
            response_started = True
            try:
                self.send_error(code, message)
            except BaseException as exc:
                # send_error may itself fail after partially writing an error
                # response (for example, when the client has disconnected).
                # Keep the original failure first and retain this secondary
                # cause in the one request event.
                record_failure(exc, failure_phase or "downstream_write")

        phase = "request_read"
        try:
            with nvtx_range(request_id, category="eic.request"):
                phase = "request_read"
                content_length = self._content_length()
                body = self.rfile.read(content_length)
                if not isinstance(body, bytes):
                    raise TypeError("request body read did not return bytes")
                if len(body) != content_length:
                    raise ValueError("incomplete request body")
                request_body_complete = True

                # The proxy is the transport boundary: controls and the body
                # hash are known after request read and before upstream
                # dispatch.  Payload bytes never enter the v2 journal.
                if v2 is not None:
                    requested_logical_id = self.headers.get("X-EIC-Logical-Request-ID") or ""
                    v2_retry_of = self.headers.get("X-EIC-Retry-Of") or None
                    retry_text = self.headers.get("X-EIC-Retry-Index")
                    if retry_text is not None:
                        try:
                            parsed_retry = int(retry_text)
                        except ValueError:
                            parsed_retry = 0
                        if parsed_retry >= 0:
                            v2_retry_index = parsed_retry
                    if requested_logical_id:
                        # A propagated logical ID lets the proxy enforce
                        # monotone retry lineage while allocating one
                        # sequence value for this physical attempt.
                        v2_logical_request_id = requested_logical_id
                        v2_sequence, previous_proxy_id, v2_retry_index = self.server.next_v2_request_identity(
                            v2_logical_request_id,
                            requested_retry_index=v2_retry_index,
                        )
                        v2_retry_of = v2_retry_of or previous_proxy_id
                    else:
                        # Requests without correlation headers start a fresh
                        # logical call.  Consume one sequence value and do
                        # not pretend an unlinked retry was observed.
                        v2_sequence = self.server.next_v2_request_sequence()
                        v2_logical_request_id = stable_id(
                            "logical-request",
                            v2.run_id,
                            v2.attempt_id,
                            _sha256(body),
                            v2_sequence,
                        )
                        v2_retry_index = 0
                        v2_retry_of = None
                    v2_client_span_id = self.headers.get("X-EIC-Client-Span-ID") or None
                    v2_request_id = stable_id(
                        "proxy-request",
                        request_id,
                        v2.run_id,
                        v2.attempt_id,
                        v2_logical_request_id,
                        v2_retry_index,
                        v2_sequence,
                    )
                    self.server.remember_v2_request(v2_logical_request_id, v2_request_id)
                    request_features = _request_features(body)
                    request_features["request_sha256"] = _sha256(body)
                    v2_span = v2.begin_request(
                        request_features,
                        logical_request_id=v2_logical_request_id,
                        physical_request_id=v2_request_id,
                        retry_index=v2_retry_index,
                        retry_of=v2_retry_of,
                        parent_event_id=self.headers.get("X-EIC-Parent-Event-ID") or None,
                        start_mono_ns=proxy_started,
                    )

                    # Persist the exact request body before opening a socket
                    # or dispatching upstream.  The later terminal call adds
                    # the response artifact using the same physical ID.  A
                    # required production capture failure prevents dispatch,
                    # because a request without a durable pre-dispatch body
                    # cannot be recovered after interruption.
                    try:
                        request_payload_artifact = v2.record_request_payload(
                            physical_request_id=v2_request_id,
                            request_body=body,
                            response_body=None,
                            request_complete=request_body_complete,
                            response_complete=False,
                        )
                        request_payload_pre_dispatch = True
                    except BaseException as telemetry_exc:
                        record_failure(telemetry_exc, "v2_payload_archive")
                        if getattr(self.server, "v2_require_request_payloads", False):
                            raise

                adaptive_runtime = self.server.adaptive_runtime
                if adaptive_runtime is not None:
                    # This append is fsync'd before the upstream request is
                    # dispatched.  The request body is used only in memory to
                    # derive reviewed pre-execution features and is never
                    # persisted by the adaptive runtime.
                    phase = "adaptive"
                    prediction = adaptive_runtime.predict_model_request(request_id, body)

                # Scrape the native serving endpoint immediately before the
                # physical upstream attempt.  This is deliberately after the
                # adaptive prediction and its durable v2 request archive, so a
                # failed pre-dispatch control path never receives a serving
                # interval.  The capture adapter never polls for a later
                # counter update and does not infer isolation from this proxy's
                # own concurrency.
                if self.server.serving_metrics_capture is not None:
                    try:
                        serving_state = self.server.serving_metrics_capture.begin(
                            v2_request_id or request_id
                        )
                    except Exception as serving_exc:
                        # Serving telemetry is optional evidence.  Preserve the
                        # model request boundary when the capture itself cannot
                        # start, while exposing the reason in both journals.
                        serving_capture_error = _serving_error(serving_exc)

                phase = "send"
                headers = {
                    key: value
                    for key, value in self.headers.items()
                    if key.lower() not in _HOP_BY_HOP
                    and key.lower() not in {"host"}
                    and key.lower() not in _PROXY_OWNED_IDENTITY_HEADERS
                }
                headers["Content-Length"] = str(len(body))
                headers["X-EIC-Request-ID"] = request_id
                # The external access-witness recorder keys its independently
                # observed lease to this physical attempt.  Keep the legacy
                # request ID for existing consumers and add an explicit
                # physical alias so retries cannot collide.
                headers["X-EIC-Physical-Request-ID"] = v2_request_id or request_id
                # The pinned vLLM OpenAI frontend uses X-Request-Id to build
                # its engine request ID. Bind native per-request statistics to
                # this exact physical attempt; retries must have distinct IDs.
                headers["X-Request-Id"] = v2_request_id or request_id
                # The ASGI observer reads case/attempt identity from these
                # headers.  They must come from the recorder's immutable
                # TelemetryV2 binding, never from client input.  A missing
                # optional case identity is deliberately omitted; the
                # observer then records an unbound case rather than a fake
                # value.  There is no existing observer run-id header, so
                # X-EIC-Run-ID is rejected above rather than invented here.
                if self.server.v2_case_id is not None:
                    headers["X-EIC-Case-ID"] = self.server.v2_case_id
                headers["X-EIC-Attempt-ID"] = self.server.v2_attempt_id

                phase = "connect"
                timeout_provider = getattr(self.server, "upstream_timeout_seconds", None)
                timeout = (
                    timeout_provider()
                    if callable(timeout_provider)
                    else self.server.timeout_seconds
                )
                connection = http.client.HTTPConnection(
                    self.server.upstream_host,
                    self.server.upstream_port,
                    timeout=timeout,
                )
                self._upstream_connection = connection
                if getattr(self.server, "is_stopping", lambda: False)():
                    self.close_active_sockets()
                upstream_started = monotonic_ns()
                connect = getattr(connection, "connect", None)
                if callable(connect):
                    connect()
                self._upstream_socket = getattr(connection, "sock", None)

                phase = "send"
                # Mark the attempt before calling request(): even a transport
                # failure during the write means a physical dispatch was
                # attempted and must retain an unavailable/partial capture.
                physical_request_dispatched = True
                connection.request(self.command, self.path, body=body, headers=headers)

                phase = "response_headers"
                upstream = connection.getresponse()
                status = upstream.status
                response_headers = list(upstream.getheaders())

                phase = "response_body"
                response = _read_upstream_body(upstream)
                if not isinstance(response, bytes):
                    raise TypeError("upstream response read did not return bytes")
                response_body_complete = True
                upstream_ended = monotonic_ns()

                if adaptive_runtime is not None:
                    counts = _token_counts(response)
                    phase = "adaptive"
                    reveal_attempted = True
                    if 200 <= status < 300:
                        label = adaptive_runtime.reveal_model_request(
                            request_id,
                            observed_ms=(upstream_ended - upstream_started) / 1_000_000,
                            output_tokens=counts["completion_tokens"],
                            response_sha256=_sha256(response),
                        )
                    else:
                        label = adaptive_runtime.reveal_model_request(
                            request_id,
                            observed_ms=None,
                            unavailable_reason=f"upstream_http_{status}",
                        )

                phase = "downstream_write"
                response_started = True
                self.send_response(status)
                for key, value in response_headers:
                    if key.lower() not in _HOP_BY_HOP:
                        self.send_header(key, value)
                self.end_headers()
                written = self.wfile.write(response)
                if isinstance(written, int) and written != len(response):
                    raise OSError(f"downstream short write: {written}/{len(response)} bytes")
                flush = getattr(self.wfile, "flush", None)
                if callable(flush):
                    flush()
        except BaseException as exc:
            partial = _partial_bytes(exc)
            if partial is not None:
                if phase == "response_body":
                    response = partial
                    response_body_complete = False
                elif phase == "request_read":
                    body = partial
            record_failure(exc, phase)

            # Process-control exceptions must not be turned into an HTTP
            # response or silently swallowed. The finally block still emits
            # the boundary event before re-raising them below.
            if not isinstance(exc, Exception):
                return

            # A prediction that has returned is durable and must receive one
            # unavailable label for every upstream failure.  Never retry a
            # reveal which already started: it may have durably committed
            # before raising.
            if (
                prediction is not None
                and not reveal_attempted
                and self.server.adaptive_runtime is not None
                and phase != "downstream_write"
            ):
                reveal_attempted = True
                try:
                    label = self.server.adaptive_runtime.reveal_model_request(
                        request_id,
                        observed_ms=None,
                        unavailable_reason=f"upstream_{type(exc).__name__}",
                    )
                except BaseException as label_exc:
                    record_failure(label_exc, failure_phase or phase)

            if process_control_error is not None:
                return

            if phase == "request_read":
                send_error_once(400, "invalid request body")
            elif phase == "adaptive":
                # Adaptive prediction/reveal failures are safety failures. A
                # failed prediction occurs before dispatch; a failed reveal
                # is withheld from the caller rather than returning an
                # unscored response.
                send_error_once(500, "adaptive request protocol failed closed")
            elif phase != "downstream_write":
                send_error_once(502, "upstream vLLM unavailable")
        finally:
            if connection is not None:
                try:
                    connection.close()
                except BaseException as close_exc:
                    # Closing is part of request finalization, not an
                    # opportunity to replace the first failure.  If close is
                    # the only failure, classify it with the upstream body
                    # lifecycle so it remains visible as a failed request.
                    record_failure(close_exc, failure_phase or "response_body")
                finally:
                    self._upstream_connection = None
                    self._upstream_socket = None
            if upstream_started is not None and upstream_ended is None:
                upstream_ended = monotonic_ns()

            if serving_state is not None and self.server.serving_metrics_capture is not None:
                try:
                    serving_result = self.server.serving_metrics_capture.finish(
                        serving_state,
                        dispatched=physical_request_dispatched,
                    )
                except Exception as serving_exc:
                    # Never replace the transport result with a metrics
                    # archival error.  The event still states that serving
                    # evidence is unavailable and records the exact cause.
                    serving_capture_error = _serving_error(serving_exc)

            proxy_ended = monotonic_ns()
            started = upstream_started if upstream_started is not None else proxy_started
            ended = upstream_ended if upstream_ended is not None else proxy_ended
            counts = _token_counts(response) if response_body_complete else {
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": None,
                "cached_tokens": None,
            }
            request_features = _request_features(body)
            try:
                event_path = urlsplit(raw_path).path
            except Exception:
                event_path = ""
            serving_fields: dict[str, Any] = {}
            v2_serving_fields: dict[str, Any] = {}
            serving_queue_ms: float | None = None
            serving_prefill_ms: float | None = None
            serving_decode_ms: float | None = None
            serving_e2e_ms: float | None = None
            serving_timings_reliable = False
            if self.server.serving_metrics_capture is not None:
                if serving_result is not None:
                    serving_queue_ms = serving_result.timing("queue")
                    serving_prefill_ms = serving_result.timing("prefill")
                    serving_decode_ms = serving_result.timing("decode")
                    serving_e2e_ms = serving_result.timing("e2e")
                    serving_timings_reliable = serving_result.timings_reliable
                    serving_status = serving_result.status
                    serving_reason = serving_result.reason
                    serving_reference = serving_result.reference
                else:
                    serving_status = "unavailable"
                    serving_reason = serving_capture_error or "serving metrics capture did not complete"
                    serving_reference = None
                serving_fields = {
                    "serving_metrics_status": serving_status,
                    "serving_metrics_unavailable_reason": serving_reason,
                    "serving_metrics_record": serving_reference,
                    "serving_metrics_capture_config_sha256": (
                        self.server.serving_metrics_capture.config_sha256
                    ),
                    "serving_metrics_physical_request_dispatched": physical_request_dispatched,
                }
                v2_serving_fields = dict(serving_fields)
            self.server.writer.append(
                {
                    "schema_version": "observability.request-proxy.v1",
                    "event_type": "model_request_boundary",
                    "request_id": request_id,
                    "method": method,
                    "path": event_path,
                    "status_code": status,
                    "status": "success" if status is not None and 200 <= status < 300 and error is None else ("timeout" if error and "timeout" in error.lower() else "failure"),
                    "error": error,
                    "failure_phase": failure_phase,
                    "request_bytes": len(body),
                    "response_bytes": len(response),
                    "request_sha256": _sha256(body),
                    "response_sha256": _sha256(response),
                    **counts,
                    **request_features,
                    "start_mono_ns": started,
                    "end_mono_ns": ended,
                    "duration_ms": (ended - started) / 1_000_000,
                    "clock": clock_fields(),
                    "utc_recorded": utc_now(),
                    "provenance": "measured",
                    "request_mutation": False,
                    "adaptive_prediction_record_sha256": prediction.get("record_sha256") if prediction else None,
                    "adaptive_label_record_sha256": label.get("record_sha256") if label else None,
                    "prediction_durable_before_upstream": prediction is not None if self.server.adaptive_runtime is not None else None,
                    **serving_fields,
                }
            )
            if v2 is not None and v2_span is None:
                # Preserve a failed/partial request boundary even when body
                # validation failed before the normal pre-dispatch begin.
                v2_sequence = self.server.next_v2_request_sequence()
                v2_logical_request_id = stable_id(
                    "logical-request", v2.run_id, v2.attempt_id, _sha256(body), v2_sequence
                )
                v2_request_id = stable_id(
                    "proxy-request",
                    request_id,
                    v2.run_id,
                    v2.attempt_id,
                    v2_logical_request_id,
                    0,
                    v2_sequence,
                )
                fallback_features = _request_features(body)
                fallback_features["request_sha256"] = _sha256(body)
                try:
                    v2_span = v2.begin_request(
                        fallback_features,
                        logical_request_id=v2_logical_request_id,
                        physical_request_id=v2_request_id,
                        start_mono_ns=proxy_started,
                    )
                except BaseException as telemetry_exc:
                    record_failure(telemetry_exc, "v2_telemetry")
            if v2 is not None and v2_span is not None:
                # If request parsing failed before the normal begin path, the
                # fallback span still needs an archive.  For a normal
                # dispatch the request artifact was already durably written
                # above, so this branch only fills the exceptional gap.
                try:
                    physical_id = v2_request_id or v2_span.identity.get("physical_request_id") or v2_span.span_id
                    if not request_payload_pre_dispatch:
                        request_payload_artifact = v2.record_request_payload(
                            physical_request_id=physical_id,
                            request_body=body,
                            response_body=None,
                            request_complete=request_body_complete,
                            response_complete=False,
                        )
                    request_payload_artifact = v2.record_request_payload(
                        physical_request_id=physical_id,
                        request_body=body,
                        response_body=response,
                        request_complete=request_body_complete,
                        response_complete=response_body_complete,
                    )
                except BaseException as telemetry_exc:
                    # Preserve the request boundary even if the artifact
                    # filesystem is unavailable.  The terminal row states the
                    # archive failure instead of claiming raw-byte capture.
                    record_failure(telemetry_exc, "v2_payload_archive")
                v2_status = "success" if status is not None and 200 <= status < 300 and error is None else (
                    "timeout" if error and any(term in error.lower() for term in ("timeout", "deadline")) else "failure"
                )
                try:
                    v2.end_request(
                        v2_span,
                        status=v2_status,
                        end_mono_ns=proxy_ended,
                        input_tokens=counts["prompt_tokens"],
                        output_tokens=counts["completion_tokens"],
                        cached_tokens=counts["cached_tokens"],
                        response_sha256=_sha256(response) if response_body_complete else None,
                        error_type=error,
                        error_message=failure_phase,
                        transport_status_code=status,
                        transport_failure_phase=failure_phase,
                        transport_response_complete=response_body_complete,
                        request_body_complete=request_body_complete,
                        request_body_sha256=_sha256(body),
                        response_body_complete=response_body_complete,
                        response_body_sha256=_sha256(response),
                        request_payload_artifact=request_payload_artifact,
                        request_payload_pre_dispatch=request_payload_pre_dispatch,
                        client_span_id=v2_client_span_id,
                        transport_boundary="request_proxy",
                        queue_ms=serving_queue_ms,
                        prefill_ms=serving_prefill_ms,
                        decode_ms=serving_decode_ms,
                        serving_timings_reliable=serving_timings_reliable,
                        serving_e2e_ms=serving_e2e_ms,
                        **v2_serving_fields,
                    )
                except BaseException as telemetry_exc:
                    # Telemetry is append-only evidence.  Keep the proxy
                    # response semantics intact while retaining any original
                    # request failure in the legacy boundary event.
                    record_failure(telemetry_exc, "v2_telemetry")
            if process_control_error is not None:
                raise process_control_error

    def _content_length(self) -> int:
        value = self.headers.get("Content-Length", "0")
        try:
            length = int(value)
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length < 0 or length > self.server.max_body_bytes:
            raise ValueError("request body exceeds configured limit")
        return length

    do_POST = _forward
    do_GET = _forward


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    graceful_shutdown_seconds = 5.0

    def __init__(
        self,
        address: tuple[str, int],
        *,
        upstream_host: str,
        upstream_port: int,
        writer: JsonlWriter,
        timeout_seconds: float,
        max_body_bytes: int,
        adaptive_runtime: Any | None = None,
        deadline_monotonic_ns: int | None = None,
        v2_telemetry: TelemetryV2 | None = None,
        v2_output_dir: Path | None = None,
        v2_run_id: str = "request-proxy",
        v2_attempt_id: str = "attempt-001",
        v2_case_id: str | None = None,
        v2_instance_id: str | None = None,
        v2_hardware: dict[str, Any] | None = None,
        v2_model_hardware: Mapping[str, Any] | None = None,
        v2_hardware_profile_sha256: str | None = None,
        v2_require_request_payloads: bool = False,
        v2_cpu_collector_config: Mapping[str, Any] | None = None,
        v2_serving_metrics_config: Mapping[str, Any] | None = None,
        serving_metrics_config: Mapping[str, Any] | None = None,
    ):
        super().__init__(address, ProxyHandler)
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self.writer = writer
        self.timeout_seconds = timeout_seconds
        self.max_body_bytes = max_body_bytes
        self.adaptive_runtime = adaptive_runtime
        self.v2_telemetry = v2_telemetry
        # Keep the transport correlation binding on the server even when the
        # optional v2 recorder is supplied by a caller.  Once a recorder
        # exists, its immutable values below are authoritative.
        self.v2_run_id = v2_run_id
        self.v2_attempt_id = v2_attempt_id
        self.v2_case_id = v2_case_id
        self.v2_require_request_payloads = bool(v2_require_request_payloads)
        self.v2_cpu_collector_config = dict(v2_cpu_collector_config or {})
        if (
            v2_serving_metrics_config is not None
            and serving_metrics_config is not None
            and dict(v2_serving_metrics_config) != dict(serving_metrics_config)
        ):
            raise ValueError("v2 and manifest serving metrics configs disagree")
        selected_serving_config = (
            serving_metrics_config
            if serving_metrics_config is not None
            else v2_serving_metrics_config
        )
        self.serving_metrics_config = _validate_serving_metrics_config(selected_serving_config)
        # Keep the v2-prefixed attribute for callers that already use the
        # interrupted integration's name.
        self.v2_serving_metrics_config = self.serving_metrics_config
        if self.v2_require_request_payloads and self.v2_telemetry is None and v2_output_dir is None:
            raise ValueError("required v2 request payload capture needs a v2 recorder")
        if v2_output_dir is not None and self.v2_telemetry is None and TelemetryV2 is None:
            raise ValueError("v2 output was requested but TelemetryV2 is unavailable")
        if self.v2_telemetry is None and v2_output_dir is not None and TelemetryV2 is not None:
            self.v2_telemetry = TelemetryV2(
                v2_output_dir,
                run_id=v2_run_id,
                attempt_id=v2_attempt_id,
                case_id=v2_case_id,
                instance_id=v2_instance_id,
                hardware=v2_hardware,
                model_hardware=v2_model_hardware,
                hardware_profile_sha256=v2_hardware_profile_sha256,
                writer_role="proxy",
            )
        if self.v2_telemetry is not None:
            # Do not let constructor defaults or a second caller-supplied
            # value disagree with the journal's identity tuple.
            self.v2_run_id = self.v2_telemetry.run_id
            self.v2_attempt_id = self.v2_telemetry.attempt_id
            self.v2_case_id = self.v2_telemetry.case_id
        if not isinstance(self.v2_attempt_id, str) or not self.v2_attempt_id:
            raise ValueError("v2 attempt identity must be non-empty text")
        if self.v2_case_id is not None and (not isinstance(self.v2_case_id, str) or not self.v2_case_id):
            raise ValueError("v2 case identity must be non-empty text when supplied")
        self.serving_metrics_capture: _ServingMetricsCapture | None = None
        if self.v2_serving_metrics_config["enabled"]:
            serving_output_dir = v2_output_dir
            if serving_output_dir is None:
                writer_path = getattr(writer, "path", None)
                if writer_path is not None:
                    serving_output_dir = Path(writer_path).expanduser().parent
            self.serving_metrics_capture = _ServingMetricsCapture(
                self.v2_serving_metrics_config,
                serving_output_dir,
            )
        self._v2_sequence_lock = threading.Lock()
        self._v2_sequence = 0
        self._v2_last_proxy_request: dict[str, str] = {}
        self._v2_last_retry_index: dict[str, int] = {}
        self.case_deadline_monotonic_ns = (
            deadline_from_env()
            if deadline_monotonic_ns is None
            else int(deadline_monotonic_ns)
        )
        if self.case_deadline_monotonic_ns is not None and self.case_deadline_monotonic_ns <= 0:
            raise ValueError("case deadline must be a positive monotonic timestamp")
        self._active_lock = threading.Lock()
        self._active_handlers: dict[ProxyHandler, threading.Thread] = {}
        self._stopping = False
        self._shutdown_thread: threading.Thread | None = None

    def next_v2_request_sequence(self) -> int:
        with self._v2_sequence_lock:
            value = self._v2_sequence
            self._v2_sequence += 1
            return value

    def next_v2_request_identity(
        self,
        logical_request_id: str,
        *,
        requested_retry_index: int = 0,
    ) -> tuple[int, str | None, int]:
        """Allocate a sequence and return the prior physical request ID."""
        with self._v2_sequence_lock:
            value = self._v2_sequence
            self._v2_sequence += 1
            previous = self._v2_last_proxy_request.get(logical_request_id) if logical_request_id else None
            prior_index = self._v2_last_retry_index.get(logical_request_id, -1) if logical_request_id else -1
            retry_index = max(requested_retry_index, prior_index + 1 if previous is not None else 0)
            if logical_request_id:
                self._v2_last_retry_index[logical_request_id] = retry_index
            return value, previous, retry_index

    def remember_v2_request(self, logical_request_id: str, physical_request_id: str) -> None:
        if not logical_request_id:
            return
        with self._v2_sequence_lock:
            self._v2_last_proxy_request[logical_request_id] = physical_request_id

    def register_handler(self, handler: ProxyHandler) -> None:
        with self._active_lock:
            self._active_handlers[handler] = threading.current_thread()
            stopping = self._stopping
        if stopping:
            handler.close_active_sockets()

    def unregister_handler(self, handler: ProxyHandler) -> None:
        with self._active_lock:
            self._active_handlers.pop(handler, None)

    def verify_request(self, _request: Any, _client_address: Any) -> bool:
        with self._active_lock:
            return not self._stopping

    def is_stopping(self) -> bool:
        with self._active_lock:
            return self._stopping

    def close_active_sockets(self) -> None:
        with self._active_lock:
            handlers = tuple(self._active_handlers)
        for handler in handlers:
            handler.close_active_sockets()

    def join_request_threads(self, timeout: float | None = None) -> None:
        """Join active request threads after sockets have been shut down."""
        timeout = self.graceful_shutdown_seconds if timeout is None else max(0.0, timeout)
        deadline = time.monotonic() + timeout
        current = threading.current_thread()
        while True:
            with self._active_lock:
                threads = tuple(self._active_handlers.values())
            threads = tuple(thread for thread in threads if thread is not current)
            if not threads:
                return
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                return
            slice_timeout = remaining / len(threads)
            for thread in threads:
                thread.join(slice_timeout)

    def request_graceful_shutdown(self) -> None:
        """Stop accepting requests and asynchronously wake serve_forever."""
        with self._active_lock:
            if self._stopping:
                return
            self._stopping = True
        self.close_active_sockets()
        shutdown_thread = threading.Thread(
            target=self.shutdown,
            name="request-proxy-shutdown",
            daemon=True,
        )
        self._shutdown_thread = shutdown_thread
        shutdown_thread.start()

    def close_gracefully(self, timeout: float | None = None) -> None:
        self.close_active_sockets()
        self.join_request_threads(timeout)
        self.server_close()
        if self._shutdown_thread is not None:
            self._shutdown_thread.join(timeout=1.0)

    def requests_drained(self) -> bool:
        with self._active_lock:
            return not self._active_handlers

    def upstream_timeout_seconds(self) -> float:
        """Bound the upstream socket timeout by the absolute case deadline."""
        configured = float(self.timeout_seconds)
        if self.case_deadline_monotonic_ns is None:
            return configured
        remaining = remaining_seconds(self.case_deadline_monotonic_ns)
        if remaining <= 0:
            raise TimeoutError("case deadline has expired")
        return min(configured, remaining)


def _load_hardware_profile(path: Path, expected_sha256: str | None) -> tuple[dict[str, Any], str | None]:
    """Load the sealed remote hardware descriptor for the proxy recorder.

    The descriptor is descriptive telemetry.  It is hashed before parsing and
    passed to ``TelemetryV2`` as raw inventory; the recorder's model projection
    continues to accept only its reviewed numeric fields.
    """

    if path.is_symlink() or not path.is_file():
        raise ValueError(f"remote hardware profile must be a regular file: {path}")
    payload = path.read_bytes()
    digest = _sha256(payload)
    if expected_sha256 is not None and digest != expected_sha256.lower():
        raise ValueError("remote hardware profile SHA-256 does not match the manifest")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"remote hardware profile is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("remote hardware profile must be a JSON object")
    return value, digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=8001)
    parser.add_argument("--upstream-host", default="127.0.0.1")
    parser.add_argument("--upstream-port", type=int, default=8000)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=1800.0)
    parser.add_argument("--max-body-bytes", type=int, default=16 * 1024 * 1024)
    parser.add_argument(
        "--adaptive-runtime-config",
        type=Path,
        help="hashed holdout runtime config; enables prediction-before-dispatch and reveal-after-response",
    )
    parser.add_argument("--v2-output-dir", type=Path, help="append v2 lifecycle/model journals here")
    parser.add_argument("--run-id", default=os.environ.get("ASSIGNMENT_RUN_ID", "request-proxy"))
    parser.add_argument("--attempt-id", default=os.environ.get("ASSIGNMENT_ATTEMPT_ID", "attempt-001"))
    parser.add_argument("--case-id", default=os.environ.get("ASSIGNMENT_CASE_ID"))
    parser.add_argument("--instance-id", default=os.environ.get("ASSIGNMENT_INSTANCE_ID"))
    parser.add_argument(
        "--v2-hardware-profile",
        type=Path,
        help="sealed remote hardware descriptor retained in v2 raw inventory",
    )
    parser.add_argument(
        "--v2-hardware-profile-sha256",
        help="SHA-256 binding for --v2-hardware-profile",
    )
    parser.add_argument(
        "--v2-model-hardware",
        help="canonical JSON model-facing hardware projection",
    )
    parser.add_argument(
        "--v2-require-request-payloads",
        action="store_true",
        help="fail closed before upstream dispatch if request-body archival fails",
    )
    parser.add_argument(
        "--v2-cpu-collector-config",
        help="canonical JSON settings for the runtime Linux work collector",
    )
    parser.add_argument(
        "--serving-metrics-config",
        "--v2-serving-metrics-config",
        dest="serving_metrics_config",
        help="manifest-bound serving metrics JSON file or object",
    )
    args = parser.parse_args(argv)
    if args.listen_port == args.upstream_port and args.listen_host == args.upstream_host:
        parser.error("proxy and upstream addresses must differ")
    adaptive_runtime = None
    if args.adaptive_runtime_config is not None:
        from scripts.assignment.adaptive_runtime import AdaptiveRuntime

        adaptive_runtime = AdaptiveRuntime.load(args.adaptive_runtime_config)
    v2_hardware: dict[str, Any] | None = None
    if args.v2_hardware_profile is not None:
        v2_hardware, _ = _load_hardware_profile(
            args.v2_hardware_profile,
            args.v2_hardware_profile_sha256,
        )
    elif args.v2_hardware_profile_sha256 is not None:
        raise ValueError("a hardware profile path is required with its SHA-256 binding")
    v2_cpu_collector_config: dict[str, Any] | None = None
    if args.v2_cpu_collector_config is not None:
        try:
            parsed_cpu = json.loads(args.v2_cpu_collector_config)
        except json.JSONDecodeError as exc:
            raise ValueError(f"v2 CPU collector config is not valid JSON: {exc}") from exc
        if not isinstance(parsed_cpu, dict):
            raise ValueError("v2 CPU collector config must be a JSON object")
        v2_cpu_collector_config = parsed_cpu
    v2_model_hardware: dict[str, Any] | None = None
    if args.v2_model_hardware is not None:
        try:
            parsed_model_hardware = json.loads(args.v2_model_hardware)
        except json.JSONDecodeError as exc:
            raise ValueError(f"v2 model hardware projection is not valid JSON: {exc}") from exc
        if not isinstance(parsed_model_hardware, dict):
            raise ValueError("v2 model hardware projection must be a JSON object")
        v2_model_hardware = parsed_model_hardware
    serving_metrics_config: dict[str, Any] | None = None
    if args.serving_metrics_config is not None:
        serving_metrics_config = load_serving_metrics_config(args.serving_metrics_config)
    server = ProxyServer(
        (args.listen_host, args.listen_port),
        upstream_host=args.upstream_host,
        upstream_port=args.upstream_port,
        writer=JsonlWriter(args.events),
        timeout_seconds=args.timeout_seconds,
        max_body_bytes=args.max_body_bytes,
        adaptive_runtime=adaptive_runtime,
        v2_output_dir=args.v2_output_dir,
        v2_run_id=args.run_id,
        v2_attempt_id=args.attempt_id,
        v2_case_id=args.case_id,
        v2_instance_id=args.instance_id,
        v2_hardware=v2_hardware,
        v2_model_hardware=v2_model_hardware,
        v2_hardware_profile_sha256=args.v2_hardware_profile_sha256,
        v2_require_request_payloads=args.v2_require_request_payloads,
        v2_cpu_collector_config=v2_cpu_collector_config,
        serving_metrics_config=serving_metrics_config,
    )
    print(
        f"request proxy listening on {args.listen_host}:{args.listen_port}; "
        f"upstream={args.upstream_host}:{args.upstream_port}",
        flush=True,
    )

    signal_handlers: dict[int, Any] = {}
    if threading.current_thread() is threading.main_thread():
        def handle_signal(_signum: int, _frame: Any) -> None:
            # A signal may interrupt verify_request while the main thread
            # holds the active-handler lock. Never acquire that lock here.
            threading.Thread(target=server.request_graceful_shutdown, daemon=True).start()

        for signal_number in (signal.SIGTERM, signal.SIGINT):
            signal_handlers[signal_number] = signal.getsignal(signal_number)
            signal.signal(signal_number, handle_signal)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.request_graceful_shutdown()
    finally:
        server.close_gracefully()
        for signal_number, previous in signal_handlers.items():
            signal.signal(signal_number, previous)
    if not server.requests_drained():
        print("request proxy shutdown incomplete: request handlers remain", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
