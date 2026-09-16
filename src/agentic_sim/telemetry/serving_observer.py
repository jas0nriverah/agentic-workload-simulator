"""A small, fail-closed ASGI observer for a vLLM serving process.

The middleware in this module is deliberately independent of Starlette,
FastAPI, and vLLM internals.  vLLM can import it through ``--middleware``
without changing the application or the bytes exchanged with a client.  It
records every HTTP route, including requests without an
``X-EIC-Physical-Request-ID`` header, and gives every observation its own
local ID.

The observer has two evidence paths for Prometheus data:

* a complete response from the observed ``/metrics`` route is retained as
  exact bytes and linked to the request observation and an explicit scrape ID;
* callers that have the actual registry used by vLLM may call
  :func:`capture_registry_snapshot`.  The helper uses ``generate_latest`` on
  that registry; when no registry is supplied it resolves vLLM's own
  ``get_prometheus_registry`` helper and otherwise records explicit
  unavailability.

Neither path performs an inference request.  Bounded post-completion samples
are scheduled in independent asyncio tasks and never delay the request that
was measured.  A later attribution process must prove that the selected
scrapes, complete ingress stream, clock identity, process identity, and
counter epoch all agree before it can use the existing native metric delta
checks.

This file intentionally uses only the Python standard library.  That keeps
the middleware importable in the pinned vLLM environment before optional
``prometheus_client`` imports are needed by the registry helper.
"""

from __future__ import annotations

import asyncio
import atexit
import fcntl
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import platform
import socket
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Mapping, Optional, Set, Tuple, Union


OBSERVER_SCHEMA = "assignment.serving-observer.v1"
OBSERVER_SOURCE = "asgi_server_middleware"
PHYSICAL_REQUEST_HEADER = "x-eic-physical-request-id"
SCRAPE_ID_HEADER = "x-eic-scrape-id"
SCRAPE_PHASE_HEADER = "x-eic-scrape-phase"
OBSERVER_CLASS_HEADER = "x-eic-observer-request"
CASE_ID_HEADER = "x-eic-case-id"
ATTEMPT_ID_HEADER = "x-eic-attempt-id"
DEFAULT_METRICS_PATH = "/metrics"
DEFAULT_MAX_METRICS_BYTES = 8 * 1024 * 1024
MAX_POSTCOMPLETION_DELAY_SECONDS = 60.0
VLLM_VERSION = "0.10.0"
SAFE_OBSERVER_ROUTES = frozenset({
    "/metrics",
    "/health",
    "/load",
    "/ping",
    "/version",
    "/server_info",
    "/v1/models",
    "/tokenizer_info",
    "/docs",
    "/redoc",
    "/openapi.json",
})


class ServingObserverError(ValueError):
    """The observer configuration or evidence is invalid."""


class ObserverFatalError(RuntimeError):
    """A durable evidence boundary failed and attribution must stop."""


def _clock_selection() -> Tuple[int, str]:
    raw_id = getattr(time, "CLOCK_MONOTONIC_RAW", None)
    if raw_id is not None:
        try:
            time.clock_gettime_ns(raw_id)
        except (AttributeError, OSError, OverflowError, ValueError):
            pass
        else:
            return raw_id, "CLOCK_MONOTONIC_RAW"
    fallback_id = getattr(time, "CLOCK_MONOTONIC", None)
    if fallback_id is None:
        raise RuntimeError("the platform exposes no monotonic clock")
    try:
        time.clock_gettime_ns(fallback_id)
    except (AttributeError, OSError, OverflowError, ValueError) as exc:
        raise RuntimeError("CLOCK_MONOTONIC is unavailable") from exc
    return fallback_id, "CLOCK_MONOTONIC"


_CLOCK_ID, _CLOCK_NAME = _clock_selection()


def monotonic_ns() -> int:
    """Return the observer's selected monotonic clock value."""

    return time.clock_gettime_ns(_CLOCK_ID)


def _read_boot_id() -> Optional[str]:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return None
    return value or None


def _clock_metadata() -> Dict[str, Any]:
    try:
        resolution_ns: Optional[int] = int(time.clock_getres(_CLOCK_ID) * 1_000_000_000)
    except (AttributeError, OSError, OverflowError, ValueError):
        resolution_ns = None
    return {
        "clock_id": _CLOCK_NAME,
        "clock_source": "time.clock_gettime_ns",
        "clock_resolution_ns": resolution_ns,
        "hostname": socket.gethostname(),
        "boot_id": _read_boot_id(),
        "platform": platform.system(),
    }


def _process_start_ticks(pid: int) -> Optional[int]:
    """Read Linux ``/proc/<pid>/stat`` starttime without following logs."""

    try:
        raw = Path("/proc", str(pid), "stat").read_text(encoding="ascii")
    except (OSError, UnicodeError):
        return None
    # The comm field can contain spaces and parentheses.  The final closing
    # parenthesis before the state field is the stable delimiter.
    closing = raw.rfind(")")
    if closing < 0:
        return None
    fields = raw[closing + 2 :].split()
    # fields[0] is field 3 (state); field 22 is therefore index 19.
    if len(fields) <= 19:
        return None
    try:
        return int(fields[19])
    except ValueError:
        return None


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _source_sha256() -> str:
    try:
        return _sha256(Path(__file__).read_bytes())
    except OSError as exc:
        raise ServingObserverError(f"cannot hash serving observer source: {exc}") from exc


def _contains_symlink_component(path: Path) -> bool:
    current = path.expanduser().absolute()
    while True:
        if current.is_symlink():
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def _validate_path(path: Union[str, Path], label: str, *, absolute: bool = False) -> Path:
    candidate = Path(path).expanduser()
    if "\x00" in str(candidate):
        raise ServingObserverError(f"{label} contains a NUL byte")
    if absolute and not candidate.is_absolute():
        raise ServingObserverError(f"{label} must be absolute")
    if _contains_symlink_component(candidate):
        raise ServingObserverError(f"{label} contains a symlink component: {candidate}")
    return candidate


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("durable append made no progress")
        view = view[written:]


def _fsync_directory(path: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return
    fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _durable_bytes(path: Path, payload: bytes) -> None:
    """Create one internal artifact atomically and fsync its directory."""

    path = _validate_path(path, "artifact path")
    path.parent.mkdir(parents=True, exist_ok=True)
    if _contains_symlink_component(path):
        raise ServingObserverError(f"artifact path contains a symlink: {path}")
    if path.exists():
        if not path.is_file() or path.is_symlink() or path.read_bytes() != payload:
            raise ServingObserverError(f"artifact already contains different bytes: {path}")
        return
    fd, temporary_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    temporary: Optional[Path] = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            if not path.is_file() or path.is_symlink() or path.read_bytes() != payload:
                raise ServingObserverError(f"artifact race produced different bytes: {path}")
        else:
            os.replace(str(temporary), str(path))
            temporary = None
        _fsync_directory(path.parent)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _safe_error(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}".replace("\x00", "")
    return text[:1000]


def _nonempty_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ServingObserverError(f"{label} must be non-empty text")
    return value.strip()


def _env_text(name: str) -> Optional[str]:
    value = os.environ.get(name)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _env_bool(name: str, default: bool) -> bool:
    value = _env_text(name)
    if value is None:
        return default
    if value.lower() in {"1", "true", "yes"}:
        return True
    if value.lower() in {"0", "false", "no"}:
        return False
    raise ServingObserverError(f"{name} must be a boolean")


def _header_values(scope: Mapping[str, Any], wanted: str) -> List[bytes]:
    wanted_bytes = wanted.lower().encode("ascii")
    values: List[bytes] = []
    for pair in scope.get("headers", ()) or ():
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            continue
        name, value = pair
        if isinstance(name, str):
            name = name.encode("ascii", "ignore")
        if isinstance(name, bytes) and name.lower() == wanted_bytes:
            if isinstance(value, str):
                value = value.encode("utf-8", "surrogatepass")
            if isinstance(value, bytes):
                values.append(value)
    return values


def _single_header(scope: Mapping[str, Any], wanted: str) -> Tuple[Optional[str], Optional[str]]:
    values = _header_values(scope, wanted)
    if not values:
        return None, None
    if len(values) != 1:
        return None, f"{wanted} appears {len(values)} times"
    try:
        value = values[0].decode("utf-8")
    except UnicodeDecodeError:
        return None, f"{wanted} is not UTF-8"
    value = value.strip()
    if not value:
        return None, f"{wanted} is empty"
    return value, None


def _route_from_scope(scope: Mapping[str, Any]) -> str:
    value = scope.get("path")
    if isinstance(value, str) and value:
        return value
    raw = scope.get("raw_path")
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace") or "/"
    return "/"


def _validate_delay_values(values: Iterable[Union[int, float]]) -> Tuple[float, ...]:
    result: List[float] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ServingObserverError("post-completion sample delays must be numbers")
        number = float(value)
        if not math.isfinite(number) or number < 0 or number > MAX_POSTCOMPLETION_DELAY_SECONDS:
            raise ServingObserverError(
                f"post-completion sample delay must be between 0 and {MAX_POSTCOMPLETION_DELAY_SECONDS:g} seconds"
            )
        result.append(number)
    return tuple(result)


class AppendOnlyDurableJournal:
    """One-process append-only JSONL journal with contiguous durable sequence."""

    def __init__(self, path: Union[str, Path]) -> None:
        self.path = _validate_path(path, "observer journal", absolute=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if _contains_symlink_component(self.path):
            raise ServingObserverError(f"observer journal contains a symlink: {self.path}")
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            self._fd = os.open(str(self.path), flags, 0o640)
        except OSError as exc:
            raise ServingObserverError(f"cannot open observer journal: {self.path}: {exc}") from exc
        try:
            if not os.path.isfile(self.path) or self.path.is_symlink():
                raise ServingObserverError(f"observer journal is not a regular file: {self.path}")
            if os.fstat(self._fd).st_size:
                raise ServingObserverError(
                    f"observer journal must be a new file for one server process: {self.path}"
                )
            _fsync_directory(self.path.parent)
        except BaseException:
            os.close(self._fd)
            raise
        self._lock = threading.RLock()
        self._sequence = 0
        self._closed = False

    @property
    def last_sequence(self) -> int:
        return self._sequence

    def append(self, record: Mapping[str, Any]) -> Dict[str, Any]:
        if not isinstance(record, Mapping):
            raise ServingObserverError("observer journal record must be a mapping")
        with self._lock:
            if self._closed:
                raise ServingObserverError("observer journal is closed")
            value = dict(record)
            if "sequence" in value:
                raise ServingObserverError("observer journal assigns sequence numbers")
            next_sequence = self._sequence + 1
            value["sequence"] = next_sequence
            try:
                encoded = (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode(
                    "utf-8"
                )
            except (TypeError, ValueError) as exc:
                raise ServingObserverError(f"observer journal record is not JSON: {exc}") from exc
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX)
                try:
                    _write_all(self._fd, encoded)
                    os.fsync(self._fd)
                finally:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
            except BaseException as exc:
                raise ServingObserverError(f"observer journal durability failed: {exc}") from exc
            self._sequence = next_sequence
            return value

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                os.fsync(self._fd)
            finally:
                os.close(self._fd)
                self._closed = True


@dataclass
class _Observation:
    observation_id: str
    physical_request_id: Optional[str]
    request_id_error: Optional[str]
    case_id: Optional[str]
    attempt_id: Optional[str]
    correlation_error: Optional[str]
    http_request_id: Optional[str]
    http_request_id_error: Optional[str]
    request_class: str
    method: str
    route: str
    is_metrics: bool
    scrape_id: Optional[str]
    scrape_id_source: Optional[str]
    scrape_phase: Optional[str]
    started_monotonic_ns: int
    request_hasher: Any = field(default_factory=hashlib.sha256)
    response_hasher: Any = field(default_factory=hashlib.sha256)
    request_body_bytes: int = 0
    response_body_bytes: int = 0
    request_body_complete: bool = False
    response_body_complete: bool = False
    request_disconnected: bool = False
    response_disconnected: bool = False
    response_started: bool = False
    response_status: Optional[int] = None
    response_final_monotonic_ns: Optional[int] = None
    postcompletion_receive_cancelled_ns: Optional[int] = None
    postcompletion_disconnect_ns: Optional[int] = None
    postcompletion_application_cancelled_ns: Optional[int] = None
    error: Optional[str] = None
    error_phase: Optional[str] = None
    metrics_buffer: bytearray = field(default_factory=bytearray)
    metrics_capture_overflow: bool = False
    metrics_capture_error: Optional[str] = None
    serving_request_id: Optional[str] = None

    def fail(self, exc: BaseException, phase: str) -> None:
        if self.error is None:
            self.error = _safe_error(exc)
        elif _safe_error(exc) not in self.error:
            self.error = (self.error + "; " + _safe_error(exc))[:1000]
        if self.error_phase is None:
            self.error_phase = phase


MetricsSampler = Callable[[], Union[bytes, str, Awaitable[Union[bytes, str]]]]


class ServingObserver:
    """Transparent ASGI middleware that records server-owned observations.

    The second positional argument is accepted for easy use in tests and
    copied vLLM launch environments.  A vLLM ``--middleware`` construction
    supplies only ``app``; in that mode the journal path and server binding
    come from the ``EIC_*`` environment variables documented in
    ``docs/SERVING_OBSERVER.md``.
    """

    def __init__(
        self,
        app: Callable[..., Awaitable[Any]],
        journal_path: Optional[Union[str, Path]] = None,
        *,
        artifact_dir: Optional[Union[str, Path]] = None,
        server_identity: Optional[str] = None,
        lease_id: Optional[str] = None,
        counter_epoch: Optional[str] = None,
        dedicated_server: Optional[bool] = None,
        metrics_path: str = DEFAULT_METRICS_PATH,
        max_metrics_bytes: int = DEFAULT_MAX_METRICS_BYTES,
        metrics_sampler: Optional[MetricsSampler] = None,
        postcompletion_sample_delays: Optional[Iterable[Union[int, float]]] = None,
        source_hash: Optional[str] = None,
        server_pid: Optional[int] = None,
        vllm_version: str = VLLM_VERSION,
    ) -> None:
        if not callable(app):
            raise ServingObserverError("ASGI application must be callable")
        journal_value = journal_path or _env_text("EIC_SERVING_OBSERVER_JOURNAL")
        if journal_value is None:
            raise ServingObserverError(
                "journal_path or EIC_SERVING_OBSERVER_JOURNAL is required for durable observation"
            )
        artifact_value = artifact_dir or _env_text("EIC_SERVING_OBSERVER_ARTIFACT_DIR")
        if artifact_value is None:
            artifact_value = str(Path(journal_value).expanduser().parent / "serving_observer_artifacts")
        self.app = app
        self.journal_path = _validate_path(journal_value, "observer journal", absolute=True)
        self.artifact_dir = _validate_path(artifact_value, "observer artifact directory", absolute=True)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        if _contains_symlink_component(self.artifact_dir):
            raise ServingObserverError(f"observer artifact directory contains a symlink: {self.artifact_dir}")
        if not isinstance(metrics_path, str) or not metrics_path.startswith("/"):
            raise ServingObserverError("metrics_path must be an absolute URL path")
        self.metrics_path = metrics_path
        self.vllm_version = _nonempty_text(vllm_version, "vllm_version")
        if self.vllm_version != VLLM_VERSION:
            raise ServingObserverError(f"only pinned vLLM {VLLM_VERSION} is supported")
        if isinstance(max_metrics_bytes, bool) or not isinstance(max_metrics_bytes, int) or max_metrics_bytes <= 0:
            raise ServingObserverError("max_metrics_bytes must be a positive integer")
        self.max_metrics_bytes = max_metrics_bytes
        self.metrics_sampler_error: Optional[str] = None
        if metrics_sampler is None:
            try:
                metrics_sampler = make_prometheus_registry_sampler()
                self.metrics_sampler_source = "vllm_get_prometheus_registry"
            except ServingObserverError as exc:
                # The ASGI /metrics response path remains usable in a
                # minimal environment.  In-process sampling is explicitly
                # unavailable until the real vLLM registry is bound.
                self.metrics_sampler_error = _safe_error(exc)
                self.metrics_sampler_source = "unavailable"
        else:
            self.metrics_sampler_source = "explicit"
        self.metrics_sampler = metrics_sampler
        if metrics_sampler is not None and not callable(metrics_sampler):
            raise ServingObserverError("metrics_sampler must be callable")
        if postcompletion_sample_delays is None:
            configured_delays = _env_text("EIC_SERVING_OBSERVER_POSTCOMPLETION_DELAYS")
            if configured_delays:
                try:
                    postcompletion_sample_delays = tuple(
                        float(item.strip()) for item in configured_delays.split(",") if item.strip()
                    )
                except ValueError as exc:
                    raise ServingObserverError(
                        "EIC_SERVING_OBSERVER_POSTCOMPLETION_DELAYS must be comma-separated numbers"
                    ) from exc
            else:
                postcompletion_sample_delays = ()
        self.postcompletion_sample_delays = _validate_delay_values(postcompletion_sample_delays)
        if self.postcompletion_sample_delays and self.metrics_sampler is None:
            unavailable_reason = self.metrics_sampler_error or "actual vLLM registry is unavailable"

            def unavailable_sampler() -> bytes:
                raise ServingObserverError(unavailable_reason)

            self.metrics_sampler = unavailable_sampler
            self.metrics_sampler_source = "unavailable"

        self.clock = _clock_metadata()
        self.server_pid = os.getpid() if server_pid is None else server_pid
        if isinstance(self.server_pid, bool) or not isinstance(self.server_pid, int) or self.server_pid <= 0:
            raise ServingObserverError("server_pid must be a positive integer")
        self.server_process_start_ticks = _process_start_ticks(self.server_pid)
        self.server_identity = _nonempty_text(
            server_identity or _env_text("EIC_SERVER_IDENTITY") or f"pid-{self.server_pid}",
            "server_identity",
        )
        self.lease_id = _nonempty_text(
            lease_id or _env_text("EIC_SERVER_LEASE_ID") or "unbound-observer-lease",
            "lease_id",
        )
        if dedicated_server is None:
            dedicated_server = _env_bool("EIC_SERVER_DEDICATED", False)
        if not isinstance(dedicated_server, bool):
            raise ServingObserverError("dedicated_server must be a boolean")
        self.dedicated_server = dedicated_server
        epoch_value = counter_epoch or _env_text("EIC_COUNTER_EPOCH")
        self.counter_epoch_source = "configured" if epoch_value else "observer_start_generated"
        self.counter_epoch = _nonempty_text(epoch_value or f"observer-epoch-{uuid.uuid4().hex}", "counter_epoch")
        self.source_hash = _nonempty_text(source_hash or _source_sha256(), "source_hash")
        if len(self.source_hash) != 64:
            raise ServingObserverError("source_hash must be a SHA-256 hex digest")
        try:
            int(self.source_hash, 16)
        except ValueError as exc:
            raise ServingObserverError("source_hash must be a SHA-256 hex digest") from exc
        self.observer_instance_id = "observer-" + uuid.uuid4().hex
        self.server_started_monotonic_ns = monotonic_ns()
        self._state_lock = threading.RLock()
        self._active: Dict[str, _Observation] = {}
        self._tasks: Set[asyncio.Task[Any]] = set()
        self._fatal_error: Optional[str] = None
        self._fatal_marker_attempted = False
        self._closed = False
        self._native_observer = None
        self.journal = AppendOnlyDurableJournal(self.journal_path)
        try:
            if _env_bool("EIC_NATIVE_VLLM_OBSERVER", False):
                native_path = _env_text("EIC_NATIVE_VLLM_JOURNAL")
                if native_path is None:
                    raise ServingObserverError("EIC_NATIVE_VLLM_JOURNAL is required with native observer opt-in")
                if __package__:
                    from .native_vllm_observer import install
                else:
                    from native_vllm_observer import install
                self._native_observer = install(self, native_path)
            self.journal.append(self._base_record("observer_header", {
                "observer_source": OBSERVER_SOURCE,
                "vllm_version": self.vllm_version,
                "metrics_sampler_source": self.metrics_sampler_source,
                "metrics_sampler_error": self.metrics_sampler_error,
                "observer_instance_id": self.observer_instance_id,
                "server_started_monotonic_ns": self.server_started_monotonic_ns,
                "server_process_start_ticks": self.server_process_start_ticks,
                "counter_epoch_source": self.counter_epoch_source,
                "lease_id": self.lease_id,
                "dedicated_server": self.dedicated_server,
                "metrics_path": self.metrics_path,
                "safe_observer_routes": sorted(SAFE_OBSERVER_ROUTES),
                "max_metrics_bytes": self.max_metrics_bytes,
                "append_only": True,
                "durable": True,
                "observer_alive": True,
                "artifact_dir": str(self.artifact_dir),
                "native_observer": None if self._native_observer is None else self._native_observer.descriptor(),
            }))
        except BaseException:
            if self._native_observer is not None:
                self._native_observer.close()
            self.journal.close()
            raise
        atexit.register(self._atexit_close)

    @property
    def fatal_error(self) -> Optional[str]:
        return self._fatal_error

    @property
    def healthy(self) -> bool:
        return self._fatal_error is None and not self._closed

    def _base_record(self, record_type: str, values: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "schema_version": OBSERVER_SCHEMA,
            "record_type": record_type,
            "observer_source": OBSERVER_SOURCE,
            "observer_instance_id": self.observer_instance_id,
            "clock": dict(self.clock),
            "hostname": self.clock.get("hostname"),
            "boot_id": self.clock.get("boot_id"),
            "clock_id": self.clock.get("clock_id"),
            "clock_source": self.clock.get("clock_source"),
            "server_identity": self.server_identity,
            "server_pid": self.server_pid,
            "server_process_start_ticks": self.server_process_start_ticks,
            "server_started_monotonic_ns": self.server_started_monotonic_ns,
            "counter_epoch": self.counter_epoch,
            "observer_source_sha256": self.source_hash,
            "source_sha256": self.source_hash,
        }
        if values:
            result.update(dict(values))
        return result

    def _set_fatal(self, exc: BaseException, phase: str, *, emit_marker: bool = True) -> None:
        message = f"{phase}: {_safe_error(exc)}"
        with self._state_lock:
            if self._fatal_error is None:
                self._fatal_error = message[:1200]
            if not emit_marker or self._fatal_marker_attempted:
                return
            self._fatal_marker_attempted = True
            marker = self._base_record("observer_fatal", {
                "fatal_error": self._fatal_error,
                "fatal_phase": phase,
                "observer_alive": False,
                "fatal_monotonic_ns": monotonic_ns(),
            })
            try:
                self.journal.append(marker)
            except BaseException:
                # The original durability error is the useful evidence.  A
                # missing marker is itself visible because no later complete
                # watermark can be produced.
                pass

    def _append(self, record: Mapping[str, Any], phase: str) -> Dict[str, Any]:
        try:
            return self.journal.append(record)
        except BaseException as exc:
            self._set_fatal(exc, phase)
            raise ObserverFatalError(self._fatal_error or phase) from exc

    def _watermark(self, covered_through_ns: int) -> Dict[str, Any]:
        with self._state_lock:
            pending = sorted(self._active)
            record = self._base_record("completeness_watermark", {
                "watermark_monotonic_ns": monotonic_ns(),
                "covered_through_monotonic_ns": covered_through_ns,
                "pending_observation_ids": pending,
                "pending_count": len(pending),
                "complete": True,
                "fatal_error": self._fatal_error,
                "observer_alive": self._fatal_error is None and not self._closed,
                # The journal appends this record at the next sequence.  This
                # binds the covered range to every record before this marker.
                "covers_through_sequence": self.journal.last_sequence + 1,
            })
            appended = self._append(record, "completeness_watermark")
            if self._native_observer is not None:
                self._native_observer.watermark(appended["sequence"])
            return appended

    def _new_observation(self, scope: Mapping[str, Any]) -> _Observation:
        physical_id, physical_error = _single_header(scope, PHYSICAL_REQUEST_HEADER)
        http_request_id, http_request_id_error = _single_header(scope, "x-request-id")
        case_id, case_error = _single_header(scope, CASE_ID_HEADER)
        attempt_id, attempt_error = _single_header(scope, ATTEMPT_ID_HEADER)
        correlation_errors = [value for value in (case_error, attempt_error) if value]
        route = _route_from_scope(scope)
        method = scope.get("method")
        if not isinstance(method, str) or not method:
            method = "UNKNOWN"
        is_metrics = route == self.metrics_path and method.upper() in {"GET", "HEAD"}
        scrape_id: Optional[str] = None
        scrape_source: Optional[str] = None
        scrape_phase: Optional[str] = None
        if is_metrics:
            forwarded_scrape_id, scrape_error = _single_header(scope, SCRAPE_ID_HEADER)
            if scrape_error:
                physical_error = scrape_error if physical_error is None else physical_error + "; " + scrape_error
            if forwarded_scrape_id:
                scrape_id = forwarded_scrape_id
                scrape_source = "forwarded_header"
            else:
                scrape_id = "scrape-" + uuid.uuid4().hex
                scrape_source = "observer_generated"
            forwarded_phase, phase_error = _single_header(scope, SCRAPE_PHASE_HEADER)
            if phase_error:
                physical_error = phase_error if physical_error is None else physical_error + "; " + phase_error
            scrape_phase = forwarded_phase
        observer_header, observer_header_error = _single_header(scope, OBSERVER_CLASS_HEADER)
        if observer_header_error:
            physical_error = observer_header_error if physical_error is None else physical_error + "; " + observer_header_error
        safe_read_only_route = method.upper() in {"GET", "HEAD"} and route in SAFE_OBSERVER_ROUTES
        if is_metrics or (safe_read_only_route and observer_header and observer_header.lower() in {"1", "true", "yes"}):
            request_class = "observer"
        elif safe_read_only_route and physical_id is None:
            # Health and metadata probes are independent observer traffic even
            # when a monitor does not know the EIC header contract.  The safe
            # route list is fixed; an arbitrary caller cannot downgrade an
            # inference route with X-EIC-Observer-Request.
            request_class = "observer"
        elif physical_id:
            request_class = "model"
        else:
            request_class = "foreign"
        return _Observation(
            observation_id="observation-" + uuid.uuid4().hex,
            physical_request_id=physical_id,
            request_id_error=physical_error,
            case_id=case_id,
            attempt_id=attempt_id,
            correlation_error="; ".join(correlation_errors) if correlation_errors else None,
            http_request_id=http_request_id,
            http_request_id_error=http_request_id_error,
            request_class=request_class,
            method=method.upper(),
            route=route,
            is_metrics=is_metrics,
            scrape_id=scrape_id,
            scrape_id_source=scrape_source,
            scrape_phase=scrape_phase,
            started_monotonic_ns=monotonic_ns(),
        )

    def _start_observation(self, observation: _Observation) -> None:
        with self._state_lock:
            if self._fatal_error:
                raise ObserverFatalError(self._fatal_error)
            if self._closed:
                raise ObserverFatalError("serving observer is closed")
            self._active[observation.observation_id] = observation
            start_record = self._base_record("request_start", {
                "observation_id": observation.observation_id,
                "physical_request_id": observation.physical_request_id,
                "physical_request_id_present": observation.physical_request_id is not None,
                "request_id_error": observation.request_id_error,
                "case_id": observation.case_id,
                "attempt_id": observation.attempt_id,
                "correlation_error": observation.correlation_error,
                "http_request_id": observation.http_request_id,
                "http_request_id_error": observation.http_request_id_error,
                "request_class": observation.request_class,
                "method": observation.method,
                "route": observation.route,
                "started_monotonic_ns": observation.started_monotonic_ns,
                "pending": True,
                "scrape_id": observation.scrape_id,
                "scrape_id_source": observation.scrape_id_source,
                "scrape_phase": observation.scrape_phase,
            })
            try:
                self._append(start_record, "request_start")
            except BaseException:
                self._active.pop(observation.observation_id, None)
                raise

    def _receive_wrapper(self, observation: _Observation, receive: Callable[[], Awaitable[Mapping[str, Any]]]) -> Callable[[], Awaitable[Mapping[str, Any]]]:
        async def wrapped() -> Mapping[str, Any]:
            try:
                message = await receive()
            except BaseException as exc:
                if isinstance(exc, asyncio.CancelledError) and observation.response_body_complete:
                    # Starlette stops its disconnect listener after the final
                    # response send. Keep that cleanup visible without turning
                    # a completed response into a failed inference request.
                    observation.postcompletion_receive_cancelled_ns = monotonic_ns()
                else:
                    observation.fail(exc, "request_receive")
                raise
            if not isinstance(message, Mapping):
                observation.fail(TypeError("ASGI receive message is not a mapping"), "request_receive")
                return message
            message_type = message.get("type")
            if message_type == "http.request":
                body = message.get("body", b"")
                if isinstance(body, bytes):
                    observation.request_hasher.update(body)
                    observation.request_body_bytes += len(body)
                else:
                    observation.fail(TypeError("ASGI request body is not bytes"), "request_receive")
                if not bool(message.get("more_body", False)):
                    observation.request_body_complete = True
            elif message_type == "http.disconnect":
                if observation.response_body_complete:
                    observation.postcompletion_disconnect_ns = monotonic_ns()
                else:
                    observation.request_disconnected = True
            return message

        return wrapped

    def _send_wrapper(self, observation: _Observation, send: Callable[[Mapping[str, Any]], Awaitable[Any]]) -> Callable[[Mapping[str, Any]], Awaitable[Any]]:
        async def wrapped(message: Mapping[str, Any]) -> Any:
            message_type = message.get("type") if isinstance(message, Mapping) else None
            final_body = message_type == "http.response.body" and not bool(message.get("more_body", False))
            if message_type == "http.response.start":
                observation.response_started = True
                status = message.get("status")
                if isinstance(status, bool) or not isinstance(status, int):
                    observation.fail(TypeError("ASGI response status is not an integer"), "response_send")
                else:
                    observation.response_status = status
            elif message_type == "http.response.body":
                body = message.get("body", b"")
                if isinstance(body, bytes):
                    observation.response_hasher.update(body)
                    observation.response_body_bytes += len(body)
                    if observation.is_metrics and not observation.metrics_capture_overflow:
                        if len(observation.metrics_buffer) + len(body) <= self.max_metrics_bytes:
                            observation.metrics_buffer.extend(body)
                        else:
                            observation.metrics_capture_overflow = True
                            observation.metrics_buffer.clear()
                            observation.metrics_capture_error = (
                                f"metrics response exceeds max_metrics_bytes={self.max_metrics_bytes}"
                            )
                else:
                    observation.fail(TypeError("ASGI response body is not bytes"), "response_send")
            try:
                result = await send(message)
            except BaseException as exc:
                observation.response_disconnected = True
                observation.fail(exc, "response_send")
                raise
            if final_body:
                observation.response_body_complete = True
                observation.response_final_monotonic_ns = monotonic_ns()
            return result

        return wrapped

    def _metrics_artifact(self, observation: _Observation, terminal_ns: int) -> Optional[Dict[str, Any]]:
        if not observation.is_metrics:
            return None
        if observation.metrics_capture_overflow:
            return {
                "capture_status": "unavailable",
                "capture_error": observation.metrics_capture_error,
                "scrape_id": observation.scrape_id,
                "scrape_id_source": observation.scrape_id_source,
                "scrape_phase": observation.scrape_phase,
                "raw_sha256": None,
                "raw_bytes": observation.response_body_bytes,
                "raw_path": None,
            }
        raw = bytes(observation.metrics_buffer)
        raw_hash = _sha256(raw)
        scrape_id = observation.scrape_id or ("scrape-" + observation.observation_id)
        safe_id = hashlib.sha256(scrape_id.encode("utf-8")).hexdigest()[:32]
        path = self.artifact_dir / "metrics" / (safe_id + ".prom")
        if not observation.response_body_complete:
            return {
                "capture_status": "unavailable",
                "capture_error": "metrics response body is incomplete",
                "scrape_id": scrape_id,
                "scrape_id_source": observation.scrape_id_source,
                "scrape_phase": observation.scrape_phase,
                "raw_sha256": raw_hash,
                "raw_bytes": len(raw),
                "raw_path": None,
            }
        if observation.response_status != 200:
            return {
                "capture_status": "unavailable",
                "capture_error": f"metrics response status is {observation.response_status!r}, expected 200",
                "scrape_id": scrape_id,
                "scrape_id_source": observation.scrape_id_source,
                "scrape_phase": observation.scrape_phase,
                "raw_sha256": raw_hash,
                "raw_bytes": len(raw),
                "raw_path": None,
            }
        if not raw:
            return {
                "capture_status": "unavailable",
                "capture_error": "metrics response body is empty",
                "scrape_id": scrape_id,
                "scrape_id_source": observation.scrape_id_source,
                "scrape_phase": observation.scrape_phase,
                "raw_sha256": raw_hash,
                "raw_bytes": len(raw),
                "raw_path": None,
            }
        try:
            _durable_bytes(path, raw)
        except BaseException as exc:
            self._set_fatal(exc, "metrics_archive")
            observation.metrics_capture_error = _safe_error(exc)
            return {
                "capture_status": "unavailable",
                "capture_error": observation.metrics_capture_error,
                "scrape_id": scrape_id,
                "scrape_id_source": observation.scrape_id_source,
                "scrape_phase": observation.scrape_phase,
                "raw_sha256": raw_hash,
                "raw_bytes": len(raw),
                "raw_path": None,
            }
        return {
            "capture_status": "complete",
            "capture_error": None,
            "scrape_id": scrape_id,
            "scrape_id_source": observation.scrape_id_source,
            "scrape_phase": observation.scrape_phase,
            "raw_sha256": raw_hash,
            "raw_bytes": len(raw),
            "raw_path": str(path),
            "scrape_started_monotonic_ns": observation.started_monotonic_ns,
            "captured_monotonic_ns": observation.response_final_monotonic_ns,
            "scrape_ended_monotonic_ns": terminal_ns,
            "captured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source": "observed_asgi_metrics_route",
            "metrics_request_observation_id": observation.observation_id,
        }

    def _terminal_record(self, observation: _Observation, terminal_ns: int, metrics: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
        if observation.request_body_complete:
            request_body_state = "complete"
        elif observation.request_disconnected:
            request_body_state = "disconnect"
        elif observation.error_phase == "request_receive":
            request_body_state = "error"
        else:
            request_body_state = "not_consumed"
        if observation.response_body_complete:
            response_body_state = "complete"
        elif observation.response_disconnected:
            response_body_state = "disconnect"
        elif not observation.response_started:
            response_body_state = "not_started"
        elif observation.error:
            response_body_state = "error"
        else:
            response_body_state = "partial"
        if observation.request_disconnected or observation.response_disconnected:
            terminal_status = "disconnected"
        elif observation.error:
            terminal_status = "failed"
        elif observation.response_body_complete:
            terminal_status = "complete"
        else:
            terminal_status = "incomplete"
        return self._base_record("request_terminal", {
            "observation_id": observation.observation_id,
            "physical_request_id": observation.physical_request_id,
            "physical_request_id_present": observation.physical_request_id is not None,
            "request_id_error": observation.request_id_error,
            "case_id": observation.case_id,
            "attempt_id": observation.attempt_id,
            "correlation_error": observation.correlation_error,
            "http_request_id": observation.http_request_id,
            "http_request_id_error": observation.http_request_id_error,
            "serving_request_id": observation.serving_request_id,
            "request_class": observation.request_class,
            "method": observation.method,
            "route": observation.route,
            "started_monotonic_ns": observation.started_monotonic_ns,
            "terminal_monotonic_ns": terminal_ns,
            "terminal_status": terminal_status,
            "request_body_sha256": observation.request_hasher.hexdigest(),
            "request_body_bytes": observation.request_body_bytes,
            "request_body_complete": observation.request_body_complete,
            "request_body_completeness": request_body_state,
            "response_status": observation.response_status,
            "response_body_sha256": observation.response_hasher.hexdigest(),
            "response_body_bytes": observation.response_body_bytes,
            "response_body_complete": observation.response_body_complete,
            "response_body_completeness": response_body_state,
            "response_final_monotonic_ns": observation.response_final_monotonic_ns,
            "client_disconnected": observation.request_disconnected or observation.response_disconnected,
            "postcompletion_receive_cancelled_ns": observation.postcompletion_receive_cancelled_ns,
            "postcompletion_disconnect_ns": observation.postcompletion_disconnect_ns,
            "postcompletion_application_cancelled_ns": observation.postcompletion_application_cancelled_ns,
            "error": observation.error,
            "error_phase": observation.error_phase,
            "scrape_id": observation.scrape_id,
            "scrape_id_source": observation.scrape_id_source,
            "scrape_phase": observation.scrape_phase,
            "metrics_capture": dict(metrics) if metrics is not None else None,
            "pending": False,
        })

    def _finish_observation(self, observation: _Observation, raised: Optional[BaseException]) -> None:
        terminal_ns = monotonic_ns()
        if raised is not None:
            if isinstance(raised, asyncio.CancelledError) and observation.response_body_complete:
                observation.postcompletion_application_cancelled_ns = terminal_ns
            else:
                phase = observation.error_phase or "application"
                observation.fail(raised, phase)
                if isinstance(raised, asyncio.CancelledError):
                    observation.request_disconnected = True
        metrics: Optional[Mapping[str, Any]] = None
        if observation.is_metrics:
            metrics = self._metrics_artifact(observation, terminal_ns)
            if metrics.get("capture_status") != "complete":
                # A metrics capture failure is an evidence failure.  The
                # request itself remains visible, but the observer cannot
                # advertise a positive scrape artifact.
                observation.error = observation.error or metrics.get("capture_error")
                observation.error_phase = observation.error_phase or "metrics_archive"
        terminal = self._terminal_record(observation, terminal_ns, metrics)
        with self._state_lock:
            try:
                appended_terminal = self._append(terminal, "request_terminal")
            except BaseException:
                # Keeping the active ID is intentional: a start without a
                # durable terminal is a pending request for later rejection.
                raise
            self._active.pop(observation.observation_id, None)
            if self._native_observer is not None and observation.request_class != "observer":
                self._native_observer.http_terminal(appended_terminal)
            self._watermark(terminal_ns)
        if raised is None and observation.request_class == "model" and observation.physical_request_id and self.metrics_sampler:
            for index, delay in enumerate(self.postcompletion_sample_delays):
                task = asyncio.create_task(self._postcompletion_sample(observation, index, delay))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    async def _postcompletion_sample(self, observation: _Observation, index: int, delay: float) -> None:
        try:
            if delay:
                await asyncio.sleep(delay)
            started = monotonic_ns()
            result = await asyncio.to_thread(self.metrics_sampler)  # type: ignore[arg-type]
            if inspect.isawaitable(result):
                result = await result
            ended = monotonic_ns()
            if isinstance(result, str):
                raw = result.encode("utf-8")
            elif isinstance(result, bytes):
                raw = result
            else:
                raise ServingObserverError("metrics_sampler must return bytes or text")
            if not raw:
                raise ServingObserverError("metrics_sampler returned empty bytes")
            if len(raw) > self.max_metrics_bytes:
                raise ServingObserverError(
                    f"registry sample exceeds max_metrics_bytes={self.max_metrics_bytes}"
                )
            raw_hash = _sha256(raw)
            sample_id = f"post-{observation.observation_id}-{index}"
            safe_id = hashlib.sha256(sample_id.encode("utf-8")).hexdigest()[:32]
            path = self.artifact_dir / "metrics" / (safe_id + ".prom")
            try:
                _durable_bytes(path, raw)
            except BaseException as exc:
                self._set_fatal(exc, "postcompletion_metrics_archive")
                raise
            record = self._base_record("postcompletion_metrics", {
                "sample_id": sample_id,
                "sample_index": index,
                "associated_observation_id": observation.observation_id,
                "associated_physical_request_id": observation.physical_request_id,
                "sample_started_monotonic_ns": started,
                "captured_monotonic_ns": ended,
                "sample_ended_monotonic_ns": ended,
                "raw_sha256": raw_hash,
                "raw_bytes": len(raw),
                "raw_path": str(path),
                "sample_status": "complete",
                "source": "in_process_registry",
                "bounded_delay_seconds": delay,
            })
            self._append(record, "postcompletion_metrics")
            self._watermark(ended)
        except asyncio.CancelledError:
            # Cancellation during shutdown is an uncompleted optional sample;
            # the last watermark remains the authoritative coverage boundary.
            return
        except BaseException as exc:
            try:
                self._append(self._base_record("postcompletion_metrics", {
                    "sample_id": f"post-{observation.observation_id}-{index}",
                    "sample_index": index,
                    "associated_observation_id": observation.observation_id,
                    "associated_physical_request_id": observation.physical_request_id,
                    "sample_status": "unavailable",
                    "sample_error": _safe_error(exc),
                    "source": "in_process_registry",
                    "bounded_delay_seconds": delay,
                }), "postcompletion_metrics_error")
                self._watermark(monotonic_ns())
            except BaseException:
                # _append already marks observer fatal.  No task can convert a
                # failed durability boundary into a positive attribution.
                return

    def __call__(self, scope: Mapping[str, Any], receive: Callable[[], Awaitable[Mapping[str, Any]]], send: Callable[[Mapping[str, Any]], Awaitable[Any]]) -> Awaitable[Any]:
        return self._dispatch(scope, receive, send)

    async def _dispatch(self, scope: Mapping[str, Any], receive: Callable[[], Awaitable[Mapping[str, Any]]], send: Callable[[Mapping[str, Any]], Awaitable[Any]]) -> Any:
        scope_type = scope.get("type") if isinstance(scope, Mapping) else None
        if scope_type != "http":
            try:
                return await self.app(scope, receive, send)
            finally:
                if scope_type == "lifespan":
                    self.close()
        observation = self._new_observation(scope)
        self._start_observation(observation)
        raised: Optional[BaseException] = None
        try:
            return await self.app(
                scope,
                self._receive_wrapper(observation, receive),
                self._send_wrapper(observation, send),
            )
        except BaseException as exc:
            raised = exc
            raise
        finally:
            state = scope.get("state")
            metadata = state.get("request_metadata") if isinstance(state, Mapping) else None
            serving_id = getattr(metadata, "request_id", None)
            if isinstance(serving_id, str):
                observation.serving_request_id = serving_id
            self._finish_observation(observation, raised)

    def capture_registry_snapshot(
        self,
        *,
        scrape_id: str,
        phase: str,
        registry: Any = None,
        sampler: Optional[MetricsSampler] = None,
    ) -> Dict[str, Any]:
        """Capture exact bytes from the supplied server Prometheus registry.

        Supplying ``registry`` is the preferred integration when the vLLM
        ``mount_metrics`` registry is available.  With no registry, the
        helper resolves vLLM's pinned registry helper and records an explicit
        unavailable sample if that helper is absent.  This method never calls
        the ASGI app and therefore cannot recurse into the ``/metrics`` route.
        """

        scrape_id = _nonempty_text(scrape_id, "scrape_id")
        phase = _nonempty_text(phase, "phase")
        started = monotonic_ns()
        try:
            if sampler is None:
                sampler = make_prometheus_registry_sampler(registry)
            raw_value = sampler()
            if inspect.isawaitable(raw_value):
                raise ServingObserverError("capture_registry_snapshot sampler must be synchronous")
            raw = raw_value.encode("utf-8") if isinstance(raw_value, str) else raw_value
            if not isinstance(raw, bytes) or not raw:
                raise ServingObserverError("registry sampler must return non-empty bytes or text")
            if len(raw) > self.max_metrics_bytes:
                raise ServingObserverError(
                    f"registry sample exceeds max_metrics_bytes={self.max_metrics_bytes}"
                )
        except BaseException as exc:
            ended = monotonic_ns()
            record = self._base_record("metrics_snapshot", {
                "scrape_id": scrape_id,
                "scrape_phase": phase,
                "scrape_started_monotonic_ns": started,
                "captured_monotonic_ns": ended,
                "scrape_ended_monotonic_ns": ended,
                "raw_sha256": None,
                "raw_bytes": 0,
                "raw_path": None,
                "sample_status": "unavailable",
                "sample_error": _safe_error(exc),
                "source": "in_process_registry",
            })
            self._append(record, "metrics_snapshot")
            self._watermark(ended)
            return record
        ended = monotonic_ns()
        raw_hash = _sha256(raw)
        safe_id = hashlib.sha256(scrape_id.encode("utf-8")).hexdigest()[:32]
        path = self.artifact_dir / "metrics" / (safe_id + ".prom")
        try:
            _durable_bytes(path, raw)
        except BaseException as exc:
            self._set_fatal(exc, "metrics_snapshot_archive")
            raise ObserverFatalError(self._fatal_error or "metrics_snapshot_archive") from exc
        record = self._base_record("metrics_snapshot", {
            "scrape_id": scrape_id,
            "scrape_phase": phase,
            "scrape_started_monotonic_ns": started,
            "captured_monotonic_ns": ended,
            "scrape_ended_monotonic_ns": ended,
            "raw_sha256": raw_hash,
            "raw_bytes": len(raw),
            "raw_path": str(path),
            "sample_status": "complete",
            "source": "in_process_registry",
        })
        self._append(record, "metrics_snapshot")
        self._watermark(ended)
        return record

    def close(self) -> None:
        """Append an explicit shutdown marker and close the journal."""

        with self._state_lock:
            if self._closed:
                return
            if self._fatal_error is None:
                if self._active:
                    self._set_fatal(
                        ServingObserverError("observer closed with pending requests"),
                        "observer_shutdown",
                    )
                else:
                    try:
                        self._append(self._base_record("observer_shutdown", {
                            "shutdown_monotonic_ns": monotonic_ns(),
                            "observer_alive": False,
                            "pending_observation_ids": sorted(self._active),
                            "complete": not self._active,
                        }), "observer_shutdown")
                    except BaseException:
                        pass
            self._closed = True
            if self._native_observer is not None:
                self._native_observer.close()
            try:
                self.journal.close()
            except BaseException as exc:
                self._set_fatal(exc, "observer_journal_close", emit_marker=False)

    def _atexit_close(self) -> None:
        try:
            self.close()
        except BaseException:
            pass


# Names commonly used in middleware configuration are aliases to the same
# implementation.  They are kept here so a launch manifest can choose a
# descriptive class name without a framework adapter.
ServingObserverMiddleware = ServingObserver
VLLMServingObserver = ServingObserver


def make_prometheus_registry_sampler(registry: Any = None) -> MetricsSampler:
    """Return a sampler that calls ``generate_latest`` on the actual registry.

    If a registry is supplied, it is used exactly as supplied; this is how a
    vLLM ``mount_metrics`` registry should be bound.  If it is omitted, the
    helper resolves vLLM's pinned ``get_prometheus_registry`` function.  It
    never silently falls back to the process-global registry: that could omit
    the registry mounted by vLLM or merge the wrong multiprocess files.
    """

    try:
        from prometheus_client import generate_latest
    except ImportError as exc:  # pragma: no cover - depends on the vLLM env
        raise ServingObserverError("prometheus_client is required for registry capture") from exc
    if registry is None:
        try:
            from vllm.v1.metrics.prometheus import get_prometheus_registry
        except ImportError as exc:  # pragma: no cover - depends on the vLLM env
            raise ServingObserverError(
                "the pinned vLLM get_prometheus_registry helper is unavailable; supply the actual registry"
            ) from exc
        try:
            registry = get_prometheus_registry()
        except Exception as exc:  # pragma: no cover - depends on the vLLM env
            raise ServingObserverError(
                f"the pinned vLLM registry could not be resolved: {type(exc).__name__}: {exc}"
            ) from exc
        if registry is None:
            raise ServingObserverError("the pinned vLLM registry helper returned no registry")

    def sample() -> bytes:
        result = generate_latest(registry)
        if not isinstance(result, bytes):
            raise ServingObserverError("prometheus generate_latest returned non-bytes")
        return result

    return sample


__all__ = [
    "AppendOnlyDurableJournal",
    "DEFAULT_METRICS_PATH",
    "MetricsSampler",
    "OBSERVER_SCHEMA",
    "OBSERVER_SOURCE",
    "ObserverFatalError",
    "ATTEMPT_ID_HEADER",
    "CASE_ID_HEADER",
    "PHYSICAL_REQUEST_HEADER",
    "SCRAPE_ID_HEADER",
    "SCRAPE_PHASE_HEADER",
    "SAFE_OBSERVER_ROUTES",
    "ServingObserver",
    "ServingObserverError",
    "ServingObserverMiddleware",
    "VLLMServingObserver",
    "VLLM_VERSION",
    "make_prometheus_registry_sampler",
    "monotonic_ns",
]
