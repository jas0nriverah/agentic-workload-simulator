"""Minimal telemetry capture that is safe to attach to SWE-agent boundaries."""

from __future__ import annotations

import contextlib
import hashlib
import time
import uuid
from pathlib import Path
from typing import Any, Iterator, Mapping

from .jsonl_writer import AppendOnlyJSONLWriter

_PROVENANCE = {"measured", "derived", "simulated", "unavailable", "dev"}


def monotonic_ns() -> int:
    clock = getattr(time, "CLOCK_MONOTONIC_RAW", time.CLOCK_MONOTONIC)
    return time.clock_gettime_ns(clock)


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


class ThinTelemetry:
    """Write correlated event/model/tool streams without request mutation.

    `record_model_request` and `record_tool_call` accept caller-owned payloads
    and copy only metadata into the stream. Raw trajectories remain the
    SWE-agent-owned files referenced by the run summary.
    """

    def __init__(self, output_dir: str | Path, *, run_id: str, attempt_id: str = "attempt-001", instance_id: str | None = None):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.attempt_id = attempt_id
        self.instance_id = instance_id
        self.events = AppendOnlyJSONLWriter(self.output_dir / "events.jsonl")
        self.model_calls = AppendOnlyJSONLWriter(self.output_dir / "model_calls.jsonl")
        self.tool_calls = AppendOnlyJSONLWriter(self.output_dir / "tool_calls.jsonl")
        self._seq = self._existing_count(self.output_dir / "events.jsonl")

    @staticmethod
    def _existing_count(path: Path) -> int:
        try:
            with path.open(encoding="utf-8") as handle:
                return sum(1 for _ in handle)
        except FileNotFoundError:
            return 0

    def _event(self, event_type: str, *, start_ns: int | None = None, end_ns: int | None = None, request_id: str | None = None, action_id: str | None = None, step_id: int | None = None, payload: Mapping[str, Any] | None = None, provenance: str = "measured") -> dict[str, Any]:
        start = monotonic_ns() if start_ns is None else start_ns
        end = start if end_ns is None else end_ns
        if end < start:
            raise ValueError("event end precedes start")
        if provenance not in _PROVENANCE:
            raise ValueError(f"unsupported provenance: {provenance}")
        row: dict[str, Any] = {
            "schema_version": "cr6.telemetry.v1", "seq": self._seq, "event_id": _id("event"),
            "run_id": self.run_id, "attempt_id": self.attempt_id, "instance_id": self.instance_id,
            "event_type": event_type, "request_id": request_id, "action_id": action_id,
            "step_id": step_id, "start_mono_ns": start, "end_mono_ns": end,
            "duration_ms": (end - start) / 1_000_000, "utc_recorded": utc_now(),
            "provenance": provenance, "payload": dict(payload or {}),
        }
        self.events.append(row)
        self._seq += 1
        return row

    def run_start(self, *, config_hash: str | None = None) -> str:
        self._event("run_start", payload={"config_hash": config_hash} if config_hash else {})
        return self.run_id

    def run_end(self, *, status: str, exit_code: int | None = None) -> None:
        self._event("run_end", payload={"status": status, "exit_code": exit_code})

    def step_start(self, step_id: int) -> None:
        self._event("step_start", step_id=step_id)

    def step_end(self, step_id: int, *, status: str = "completed") -> None:
        self._event("step_end", step_id=step_id, payload={"status": status})

    def record_model_request(self, *, step_id: int | None = None, request: Mapping[str, Any] | None = None, response: Mapping[str, Any] | None = None, request_id: str | None = None, start_ns: int | None = None, end_ns: int | None = None, provenance: str = "measured", **metadata: Any) -> str:
        correlation_id = request_id or _id("request")
        end = monotonic_ns() if end_ns is None else end_ns
        start = end if start_ns is None else start_ns
        self._event("model_request", start_ns=start, end_ns=end, request_id=correlation_id, step_id=step_id, payload={"metadata": metadata}, provenance=provenance)
        row = {"schema_version": "cr6.telemetry.v1", "run_id": self.run_id, "attempt_id": self.attempt_id, "instance_id": self.instance_id, "request_id": correlation_id, "step_id": step_id, "start_mono_ns": start, "end_mono_ns": end, "duration_ms": (end - start) / 1_000_000, "provenance": provenance, "request": dict(request or {}), "response": dict(response or {}), **metadata}
        self.model_calls.append(row)
        return correlation_id

    def record_tool_call(self, *, step_id: int | None = None, action_id: str | None = None, request_id: str | None = None, command: str | None = None, result: Mapping[str, Any] | None = None, start_ns: int | None = None, end_ns: int | None = None, provenance: str = "measured", **metadata: Any) -> str:
        correlation_id = action_id or _id("action")
        end = monotonic_ns() if end_ns is None else end_ns
        start = end if start_ns is None else start_ns
        self._event("tool_call", start_ns=start, end_ns=end, request_id=request_id, action_id=correlation_id, step_id=step_id, payload={"command_sha256": hashlib.sha256((command or "").encode()).hexdigest() if command is not None else None}, provenance=provenance)
        row = {"schema_version": "cr6.telemetry.v1", "run_id": self.run_id, "attempt_id": self.attempt_id, "instance_id": self.instance_id, "request_id": request_id, "action_id": correlation_id, "step_id": step_id, "start_mono_ns": start, "end_mono_ns": end, "duration_ms": (end - start) / 1_000_000, "provenance": provenance, "result": dict(result or {}), **metadata}
        self.tool_calls.append(row)
        return correlation_id

    def record_vllm_metrics(self, metrics: Mapping[str, Any], *, request_id: str | None = None, provenance: str = "measured") -> None:
        self._event("vllm_metrics", request_id=request_id, payload={"metrics": dict(metrics)}, provenance=provenance)

    def record_gpu_sample(self, sample: Mapping[str, Any], *, provenance: str = "measured") -> None:
        self._event("gpu_sample", payload={"sample": dict(sample)}, provenance=provenance)

    @contextlib.contextmanager
    def model_span(self, record: Mapping[str, Any] | None = None, **metadata: Any) -> Iterator[dict[str, Any]]:
        values = {**dict(record or {}), **metadata}
        request_id = str(values.pop("request_id", _id("request")))
        step_id = values.pop("step_id", None)
        start = monotonic_ns()
        outcome: dict[str, Any] = {}
        try:
            yield outcome
        except BaseException as exc:
            outcome["error"] = repr(exc)
            raise
        finally:
            end = monotonic_ns()
            self.record_model_request(request_id=request_id, step_id=step_id, start_ns=start, end_ns=end, response=outcome, **values)

    @contextlib.contextmanager
    def tool_call(self, record: Mapping[str, Any] | None = None, **metadata: Any) -> Iterator[dict[str, Any]]:
        values = {**dict(record or {}), **metadata}
        action_id = str(values.pop("action_id", _id("action")))
        request_id = values.pop("request_id", None)
        command = values.pop("command", None)
        step_id = values.pop("step_id", None)
        start = monotonic_ns()
        outcome: dict[str, Any] = {}
        try:
            yield outcome
        except BaseException as exc:
            outcome["error"] = repr(exc)
            raise
        finally:
            self.record_tool_call(action_id=action_id, request_id=request_id, step_id=step_id, command=command, result=outcome, start_ns=start, end_ns=monotonic_ns(), **values)


Telemetry = ThinTelemetry
