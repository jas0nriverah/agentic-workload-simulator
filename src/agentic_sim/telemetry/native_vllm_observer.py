"""Opt-in capture of pinned vLLM V1 finished-request stats.

No vLLM import occurs until install(). The original synchronous callback runs
once, with its original arguments, result and exceptions. Capture failures
invalidate only native evidence; they do not replace serving outputs.
"""
from __future__ import annotations

import enum
import functools
import hashlib
import importlib
import inspect
import json
import math
import os
from pathlib import Path
import threading
import time
import uuid

NATIVE_SCHEMA = "assignment.native-vllm-observer.v1"
NATIVE_SOURCE = "vllm_v1_finished_request_stats"
PHASE_FIELDS = ("e2e_latency", "queued_time", "prefill_time", "decode_time", "inference_time")
TOKEN_FIELDS = ("num_prompt_tokens", "num_generation_tokens", "max_tokens_param")
# vLLM 0.10.0's FinishedRequestStats does not expose either spelling.  Keep
# this optional probe so a compatible newer pinned source can be admitted only
# after its binding is reviewed; never synthesize a cache hit/miss from the
# prefix-cache policy or from prompt-token counts.
CACHE_TOKEN_FIELDS = ("num_cached_tokens", "cached_tokens")
STATE_FIELDS = ("arrival_time", "queued_ts", "scheduled_ts", "first_token_ts", "last_token_ts")


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def finite_number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and nonnegative")
    return value


def _reason(value):
    if isinstance(value, enum.Enum):
        return {"name": value.name, "value": value.value}
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        return value
    raise ValueError("finish_reason is not a supported scalar/enum")


def _cached_tokens(finished):
    for name in CACHE_TOKEN_FIELDS:
        value = getattr(finished, name, None)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value, "finished_request_stats." + name
        if value is not None:
            raise ValueError("finished request cache-token field is not a nonnegative integer")
    return None, "unavailable_finished_request_stats_field_absent"


def project_finished(req_state, finished, cache_observation=None):
    request_id = req_state.request_id
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("native request_id is missing")
    parent = req_state.parent_req
    parent_id = None if parent is None else parent.request_id
    if parent is not None and (not isinstance(parent_id, str) or not parent_id):
        raise ValueError("native parent request_id is missing")
    phases = {name: finite_number(getattr(finished, name), name) for name in PHASE_FIELDS}
    tokens = {}
    for name in TOKEN_FIELDS:
        value = getattr(finished, name)
        if name == "max_tokens_param" and value is None:
            tokens[name] = None
        elif isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            tokens[name] = value
        else:
            raise ValueError(f"{name} must be a nonnegative integer")
    cached_tokens, cached_tokens_provenance = _cached_tokens(finished)
    if cache_observation is not None:
        observed = cache_observation["cached_tokens"]
        if type(observed) is not int or not 0 <= observed <= tokens["num_prompt_tokens"]:
            raise ValueError("engine cache-token field exceeds prompt work or is invalid")
        if cached_tokens is not None and cached_tokens != observed:
            raise ValueError("finished and engine cache-token measurements disagree")
        cached_tokens = observed
        cached_tokens_provenance = cache_observation["provenance"]
    state = {name: finite_number(getattr(req_state.stats, name), name) for name in STATE_FIELDS}
    return {
        "engine_request_id": request_id,
        "parent_request_id": parent_id,
        "finished": {
            **phases,
            **tokens,
            "cached_tokens": cached_tokens,
            "cached_tokens_provenance": cached_tokens_provenance,
            "finish_reason": _reason(finished.finish_reason),
        },
        "request_state": state,
        "native_clocks": {
            "arrival_time": "frontend.time.time",
            "e2e_latency": "frontend.time.time.duration_seconds",
            "queued_ts/scheduled_ts/first_token_ts/last_token_ts": "engine_core.time.monotonic",
            "queued_time/prefill_time/decode_time/inference_time": "engine_core.time.monotonic.duration_seconds",
        },
        "cuda_kernel_timing": False,
    }


def capture_caller_cache(frame, *, caller_code, processor, req_state):
    """Copy one scalar from the pinned finish caller, not on every decode step.

    vLLM 0.10.0 omits zero cache counts from API usage and FinishedRequestStats.
    The actual EngineCoreOutput is still a caller local at this callback. Never
    equate absent API details with zero, and never retain frame/object references.
    """
    if frame is None or caller_code is None or frame.f_code is not caller_code:
        return None
    values = frame.f_locals
    try:
        output = values.get("engine_core_output")
        request_id = getattr(req_state, "request_id", None)
        if (values.get("self") is not processor or values.get("req_state") is not req_state
                or not isinstance(request_id, str) or not request_id
                or values.get("req_id") != request_id
                or getattr(output, "request_id", None) != request_id):
            return None
        cached = getattr(output, "num_cached_tokens", None)
        if cached is None:
            return None
        if type(cached) is not int or cached < 0:
            raise ValueError("EngineCoreOutput.num_cached_tokens is not a nonnegative integer")
        if type(values.get("num_cached_tokens")) is not int or values.get("num_cached_tokens") != cached:
            raise ValueError("pinned caller and EngineCoreOutput cache-token values disagree")
        return {"cached_tokens": cached,
                "provenance": "engine_core_output.num_cached_tokens@pinned_process_outputs_finish_caller"}
    finally:
        del values


class NativeFinishedObserver:
    def __init__(self, owner, journal_path, processor_class, original, binding):
        self.owner = owner
        self.processor_class = processor_class
        self.original = original
        self.binding = binding
        self.instance_id = "native-" + uuid.uuid4().hex
        self.fatal_error = None
        self.closed = False
        self._lock = threading.RLock()
        self.journal_path = Path(journal_path)
        self.journal = type(owner.journal)(self.journal_path)
        self.wrapper = None
        self._append("native_header", {
            "vllm_version": "0.10.0", "binding": binding,
            "append_only": True, "durable": True,
            "lease_id": owner.lease_id, "dedicated_server": owner.dedicated_server,
        })

    def _now(self):
        # Use the very same clock selected by the ASGI owner, including RAW.
        return time.clock_gettime_ns(getattr(time, self.owner.clock["clock_id"]))

    def descriptor(self):
        return {"path": str(self.journal_path), "native_instance_id": self.instance_id,
                "binding": self.binding, "schema_version": NATIVE_SCHEMA}

    def _append(self, kind, values):
        row = self.owner._base_record(kind, values)
        row.update(schema_version=NATIVE_SCHEMA, native_source=NATIVE_SOURCE,
                   native_instance_id=self.instance_id)
        return self.journal.append(row)

    def watermark(self, asgi_sequence):
        with self._lock:
            if self.closed or self.fatal_error:
                return
            try:
                now = self._now()
                self._append("native_watermark", {
                    "watermark_monotonic_ns": now, "covered_through_monotonic_ns": now,
                    "asgi_sequence": asgi_sequence,
                    "covers_through_sequence": self.journal.last_sequence + 1,
                    "observer_alive": True, "fatal_error": None,
                })
            except Exception as exc:
                self.fail(exc)

    def http_terminal(self, terminal):
        """Retain reconciliation even when no finish callback ever occurs."""
        with self._lock:
            if self.closed:
                return
            try:
                self._append("native_http_terminal", {
                    "observation_id": terminal["observation_id"],
                    "physical_request_id": terminal.get("physical_request_id"),
                    "http_request_id": terminal.get("http_request_id"),
                    "serving_request_id": terminal.get("serving_request_id"),
                    "http_terminal_sequence": terminal["sequence"],
                    "http_terminal_status": terminal["terminal_status"],
                    "terminal_monotonic_ns": terminal["terminal_monotonic_ns"],
                    "native_status": "unverified_requires_exact_finished_record",
                    "native_capture_error": self.fatal_error,
                })
            except Exception as exc:
                self.fail(exc)

    def fail(self, exc):
        with self._lock:
            if self.fatal_error is not None:
                return
            self.fatal_error = f"{type(exc).__name__}: {exc}"[:1000]
            try:
                self._append("native_error", {"error": self.fatal_error,
                                             "observed_monotonic_ns": self._now(), "observer_alive": False})
            except Exception:
                pass

    def capture(self, processor, req_state, iteration_stats, previous, previous_count, cache_observation=None):
        with self._lock:
            if self.closed or self.fatal_error:
                return
            current = iteration_stats.finished_requests
            if current is not previous or len(current) != previous_count + 1:
                raise ValueError("native finished_requests append delta must be exactly one")
            raw = project_finished(req_state, current[previous_count], cache_observation)
            encoded = canonical_bytes(raw)
            self._append("native_finished", {
                "observed_monotonic_ns": self._now(),
                "processor_identity": f"{os.getpid()}:{id(processor)}",
                "finished_list_count_before": previous_count,
                "finished_list_count_after": len(current),
                "raw": raw, "raw_sha256": sha256(encoded), "raw_bytes": len(encoded),
            })
            self.watermark(self.owner.journal.last_sequence)

    def close(self):
        with self._lock:
            if self.closed:
                return
            try:
                if not self.fatal_error:
                    self._append("native_shutdown", {"observed_monotonic_ns": self._now(),
                                                     "observer_alive": False})
            except Exception as exc:
                self.fail(exc)
            finally:
                self.closed = True
                if self.processor_class._update_stats_from_finished is self.wrapper:
                    self.processor_class._update_stats_from_finished = self.original
                try:
                    self.journal.close()
                except Exception:
                    pass


def install(owner, journal_path):
    """Validate the actual installed hook and patch it only on explicit opt-in."""
    vllm = importlib.import_module("vllm")
    if vllm.__version__ != "0.10.0":
        raise ValueError("native observer requires actual vLLM version 0.10.0")
    module = importlib.import_module("vllm.v1.engine.output_processor")
    stats_module = importlib.import_module("vllm.v1.metrics.stats")
    processor_class = module.OutputProcessor
    original = processor_class._update_stats_from_finished
    if getattr(original, "__eic_native_observer__", False):
        raise ValueError("native vLLM observer is already installed")
    signature = inspect.signature(original)
    if tuple(signature.parameters) != ("self", "req_state", "finish_reason", "iteration_stats"):
        raise ValueError("pinned native callback signature differs")
    if inspect.iscoroutinefunction(original):
        raise ValueError("pinned native callback must be synchronous")
    source = inspect.getsource(original).encode("utf-8")
    caller = getattr(processor_class, "process_outputs", None)
    caller_code = getattr(caller, "__code__", None)
    caller_source = inspect.getsource(caller).encode("utf-8") if caller_code is not None else None
    expected = os.environ.get("EIC_NATIVE_VLLM_EXPECTED_HOOK_SHA256")
    if expected and sha256(source) != expected:
        raise ValueError("native hook source differs from expected SHA-256")
    binding = {
        "hook": "vllm.v1.engine.output_processor.OutputProcessor._update_stats_from_finished",
        "hook_source_sha256": sha256(source),
        "signature": str(signature), "signature_sha256": sha256(str(signature).encode()),
        "output_processor_source_sha256": sha256(Path(module.__file__).read_bytes()),
        "stats_source_sha256": sha256(Path(stats_module.__file__).read_bytes()),
        "adapter_source_sha256": sha256(Path(__file__).read_bytes()),
        "publication_stage": "after_finished_stats_before_prometheus_logger",
        "cache_capture": {
            "source": "EngineCoreOutput.num_cached_tokens",
            "caller": "OutputProcessor.process_outputs",
            "caller_source_sha256": sha256(caller_source) if caller_source is not None else None,
            "capture_frequency": "once_per_finished_request",
            "requires_exact_caller_code_and_request_object": True,
            "missing_api_details_are_not_zero": True,
        },
    }
    engine_source_path = Path(module.__file__).with_name("__init__.py")
    try:
        engine_source_hash = sha256(engine_source_path.read_bytes())
        engine_source_error = None
    except OSError as exc:
        engine_source_hash = None
        engine_source_error = f"{type(exc).__name__}: {exc}"
    binding["clock_provenance"] = {
        "frontend_runtime": {
            "pid": os.getpid(), "hostname": owner.clock.get("hostname"), "boot_id": owner.clock.get("boot_id"),
            "time.monotonic": vars(time.get_clock_info("monotonic")),
            "time.time": vars(time.get_clock_info("time")),
        },
        "native_engine_timestamp_origin": {
            "origin": "EngineCoreOutput.events[].timestamp and EngineCoreOutputs.timestamp",
            "definition": "RequestStateStats / IterationStats.update_from_output and update_from_events",
            "source_sha256": binding["stats_source_sha256"],
            "source_declared_clock": "engine_core.time.monotonic",
            "timestamp_creation": "EngineCoreEvent.new_event and EngineCoreOutputs.__post_init__: time.monotonic()",
            "engine_source_path": str(engine_source_path),
            "engine_source_sha256": engine_source_hash,
            "engine_source_error": engine_source_error,
            "engine_process_runtime_probe": "unavailable_from_frontend_hook",
        },
        "native_e2e_origin": {
            "definition": "IterationStats.iteration_timestamp=time.time(); _time_since(arrival_time)",
            "source_sha256": binding["stats_source_sha256"],
        },
        "cross_clock_alignment_claimed": False,
    }
    recorder = NativeFinishedObserver(owner, journal_path, processor_class, original, binding)

    @functools.wraps(original)
    def wrapped(self, req_state, finish_reason, iteration_stats):
        previous = None
        previous_count = None
        cache_observation = None
        try:
            previous = iteration_stats.finished_requests
            previous_count = len(previous)
            frame = inspect.currentframe()
            try:
                cache_observation = capture_caller_cache(
                    frame.f_back if frame is not None else None,
                    caller_code=caller_code, processor=self, req_state=req_state)
            finally:
                del frame
        except Exception as exc:
            recorder.fail(exc)
        try:
            result = original(self, req_state, finish_reason, iteration_stats)
        except BaseException as exc:
            recorder.fail(exc)
            raise
        try:
            if previous_count is not None:
                recorder.capture(self, req_state, iteration_stats, previous, previous_count, cache_observation)
        except Exception as exc:
            recorder.fail(exc)
        return result

    wrapped.__eic_native_observer__ = True
    recorder.wrapper = wrapped
    processor_class._update_stats_from_finished = wrapped
    return recorder
