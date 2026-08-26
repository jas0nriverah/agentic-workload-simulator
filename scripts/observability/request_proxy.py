#!/usr/bin/env python3
"""Record request-level boundaries while transparently forwarding to vLLM.

The proxy deliberately stores hashes, sizes, status, token counts, and timing
only. Prompts, responses, credentials, and authorization headers never enter
the telemetry stream. It is intended for a separate profiled attempt, not the
baseline control.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[2]
for candidate in (ROOT, ROOT / "src"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from agentic_sim.observability.nvtx import range as nvtx_range
from agentic_sim.telemetry.clock import clock_fields, monotonic_ns, utc_now


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


class JsonlWriter:
    """Thread-safe append-only writer with one JSON object per line."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(self, value: dict[str, Any]) -> None:
        encoded = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
        with self._lock, self.path.open("ab") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _token_counts(body: bytes) -> dict[str, int | None]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}

    def integer(name: str) -> int | None:
        value = usage.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    return {
        "prompt_tokens": integer("prompt_tokens"),
        "completion_tokens": integer("completion_tokens"),
        "total_tokens": integer("total_tokens"),
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


class ProxyHandler(BaseHTTPRequestHandler):
    server: "ProxyServer"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _forward(self) -> None:
        request_id = f"request-{uuid.uuid4().hex}"
        proxy_started = monotonic_ns()
        body = self.rfile.read(self._content_length())
        status: int | None = None
        response = b""
        error: str | None = None
        prediction: dict[str, Any] | None = None
        label: dict[str, Any] | None = None
        upstream_started: int | None = None
        upstream_ended: int | None = None
        with nvtx_range(request_id, category="eic.request"):
            try:
                if self.server.adaptive_runtime is not None:
                    # This append is fsync'd before the upstream request is
                    # dispatched.  The request body is used only in memory to
                    # derive reviewed pre-execution features and is never
                    # persisted by the adaptive runtime.
                    prediction = self.server.adaptive_runtime.predict_model_request(request_id, body)
                connection = http.client.HTTPConnection(
                    self.server.upstream_host,
                    self.server.upstream_port,
                    timeout=self.server.timeout_seconds,
                )
                headers = {
                    key: value
                    for key, value in self.headers.items()
                    if key.lower() not in _HOP_BY_HOP and key.lower() != "host"
                }
                headers["Content-Length"] = str(len(body))
                headers["X-EIC-Request-ID"] = request_id
                upstream_started = monotonic_ns()
                connection.request(self.command, self.path, body=body, headers=headers)
                upstream = connection.getresponse()
                status = upstream.status
                response = upstream.read()
                upstream_ended = monotonic_ns()
                response_headers = upstream.getheaders()
                connection.close()
                if self.server.adaptive_runtime is not None:
                    counts = _token_counts(response)
                    if 200 <= status < 300:
                        label = self.server.adaptive_runtime.reveal_model_request(
                            request_id,
                            observed_ms=(upstream_ended - upstream_started) / 1_000_000,
                            output_tokens=counts["completion_tokens"],
                            response_sha256=_sha256(response),
                        )
                    else:
                        label = self.server.adaptive_runtime.reveal_model_request(
                            request_id,
                            observed_ms=None,
                            unavailable_reason=f"upstream_http_{status}",
                        )
                self.send_response(status)
                for key, value in response_headers:
                    if key.lower() not in _HOP_BY_HOP:
                        self.send_header(key, value)
                self.end_headers()
                self.wfile.write(response)
            except (OSError, http.client.HTTPException) as exc:
                error = type(exc).__name__
                if prediction is not None and label is None and self.server.adaptive_runtime is not None:
                    try:
                        label = self.server.adaptive_runtime.reveal_model_request(
                            request_id,
                            observed_ms=None,
                            unavailable_reason=f"upstream_{error}",
                        )
                    except Exception as label_exc:  # fail closed; preserve both causes in telemetry
                        error = f"{error}+{type(label_exc).__name__}"
                self.send_error(502, "upstream vLLM unavailable")
            except Exception as exc:
                # Adaptive prediction/reveal failures are safety failures.  A
                # failed prediction occurs before dispatch; a failed reveal is
                # withheld from the caller rather than silently returning an
                # unscored response.
                error = type(exc).__name__
                self.send_error(500, "adaptive request protocol failed closed")
        proxy_ended = monotonic_ns()
        started = upstream_started if upstream_started is not None else proxy_started
        ended = upstream_ended if upstream_ended is not None else proxy_ended
        counts = _token_counts(response)
        request_features = _request_features(body)
        self.server.writer.append(
            {
                "schema_version": "observability.request-proxy.v1",
                "event_type": "model_request_boundary",
                "request_id": request_id,
                "method": self.command,
                "path": urlsplit(self.path).path,
                "status_code": status,
                "error": error,
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
            }
        )

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
    ):
        super().__init__(address, ProxyHandler)
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self.writer = writer
        self.timeout_seconds = timeout_seconds
        self.max_body_bytes = max_body_bytes
        self.adaptive_runtime = adaptive_runtime


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
    args = parser.parse_args(argv)
    if args.listen_port == args.upstream_port and args.listen_host == args.upstream_host:
        parser.error("proxy and upstream addresses must differ")
    adaptive_runtime = None
    if args.adaptive_runtime_config is not None:
        from scripts.assignment.adaptive_runtime import AdaptiveRuntime

        adaptive_runtime = AdaptiveRuntime.load(args.adaptive_runtime_config)
    server = ProxyServer(
        (args.listen_host, args.listen_port),
        upstream_host=args.upstream_host,
        upstream_port=args.upstream_port,
        writer=JsonlWriter(args.events),
        timeout_seconds=args.timeout_seconds,
        max_body_bytes=args.max_body_bytes,
        adaptive_runtime=adaptive_runtime,
    )
    print(
        f"request proxy listening on {args.listen_host}:{args.listen_port}; "
        f"upstream={args.upstream_host}:{args.upstream_port}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
