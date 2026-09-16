import http.client
import hashlib
import json
import os
import socket
import socketserver
import struct
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.observability.request_proxy import (
    SERVING_METRICS_CONFIG_SCHEMA,
    SERVING_METRICS_WITNESS_EVIDENCE_KIND,
    SERVING_METRICS_WITNESS_SCHEMA,
    JsonlWriter,
    ProxyServer,
    _token_counts,
    load_serving_metrics_config,
)


class AdaptiveRuntimeFake:
    """Small durable fake for proxy ordering and failure tests."""

    def __init__(self, root: Path, *, fail_prediction: bool = False):
        self.root = root
        self.marker = root / "prediction.durable"
        self.records: list[dict[str, object]] = []
        self.fail_prediction = fail_prediction
        self.predicted = False
        self.revealed = False

    def predict_model_request(self, request_id: str, body: bytes) -> dict[str, object]:
        if self.fail_prediction:
            raise RuntimeError("synthetic prediction failure")
        # The fake intentionally derives no record from body content.  The
        # marker is fsync'd before returning, matching the production contract.
        self.marker.write_text(request_id, encoding="utf-8")
        with self.marker.open("rb") as handle:
            os.fsync(handle.fileno())
        self.predicted = True
        self.records.append({"kind": "prediction", "request_id": request_id})
        return {"record_sha256": "a" * 64}

    def reveal_model_request(self, request_id: str, **kwargs: object) -> dict[str, object]:
        self.revealed = True
        record = {
            "kind": "label",
            "request_id": request_id,
            "status": "unavailable" if kwargs.get("unavailable_reason") else "completed",
            **kwargs,
        }
        self.records.append(record)
        return {"record_sha256": "b" * 64, **record}


class AdaptiveUpstreamHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self.server.events.append(("upstream_seen", self.server.adaptive_marker.exists()))
        time.sleep(0.03)
        response = json.dumps(
            {"ok": True, "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, _format, *_args):
        return


class CountingUpstreamHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.server.dispatch_count += 1
        self.send_response(500)
        self.end_headers()

    def log_message(self, _format, *_args):
        return


class RawUpstreamServer(socketserver.ThreadingTCPServer):
    """Tiny local upstream with deliberately malformed transport modes."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, mode: str):
        self.mode = mode
        self.dispatch_count = 0
        super().__init__(("127.0.0.1", 0), RawUpstreamHandler)


class RawUpstreamHandler(socketserver.BaseRequestHandler):
    def handle(self):
        server: RawUpstreamServer = self.server  # type: ignore[assignment]
        server.dispatch_count += 1
        self.request.settimeout(1)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.request.recv(4096)
            if not chunk:
                break
            data += chunk
        if server.mode == "before-headers":
            return

        payload = json.dumps(
            {"usage": {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13}}
        ).encode("ascii")
        declared_length = len(payload) + 17
        headers = (
            b"HTTP/1.1 200 OK\r\n"
            + f"Content-Length: {declared_length}\r\n".encode("ascii")
            + b"Content-Type: application/json\r\n\r\n"
        )
        self.request.sendall(headers + payload)
        if server.mode == "partial-timeout":
            # Keep the socket open after a valid complete JSON prefix.  The
            # proxy's read timeout must retain those bytes while marking the
            # body incomplete, rather than inventing token counts.
            time.sleep(0.5)


class CountingWriter(JsonlWriter):
    def __init__(self, path: Path):
        super().__init__(path)
        self.append_count = 0

    def append(self, value):
        self.append_count += 1
        super().append(value)


class DelayedSuccessUpstream(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        time.sleep(0.2)
        payload = b"x" * (1024 * 1024)
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except OSError:
            pass

    def log_message(self, _format, *_args):
        return


class DelayedCloseUpstream(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        time.sleep(0.2)
        # Closing before headers forces the proxy down its send_error path.

    def log_message(self, _format, *_args):
        return


def _start_proxy(upstream_port: int, events: Path, runtime=None, *, writer=None, timeout=1):
    proxy = ProxyServer(
        ("127.0.0.1", 0),
        upstream_host="127.0.0.1",
        upstream_port=upstream_port,
        writer=writer or JsonlWriter(events),
        timeout_seconds=timeout,
        max_body_bytes=4096,
        adaptive_runtime=runtime,
    )
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    return proxy, thread


def _wait_for_event(path: Path, timeout: float = 2) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text(encoding="utf-8").strip():
            return json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        time.sleep(0.01)
    raise AssertionError(f"proxy did not append an event: {path}")


def _post(
    proxy: ProxyServer,
    body: bytes,
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", proxy.server_address[1], timeout=2)
    request_headers = {"Content-Type": "application/json", **(headers or {})}
    connection.request("POST", "/v1/chat/completions", body=body, headers=request_headers)
    response = connection.getresponse()
    response_body = response.read()
    connection.close()
    return response.status, response_body


class UpstreamHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        response = json.dumps(
            {"ok": True, "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)
        self.server.received_body = body
        self.server.received_request_id = self.headers.get("X-EIC-Request-ID")

    def log_message(self, _format, *_args):
        return


_SERVING_FAMILIES = (
    "vllm:request_queue_time_seconds",
    "vllm:request_prefill_time_seconds",
    "vllm:request_decode_time_seconds",
    "vllm:e2e_request_latency_seconds",
)
_SERVING_RESPONSE = b'{"ok":true,"usage":{"prompt_tokens":7,"completion_tokens":3,"total_tokens":10,"prompt_tokens_details":{"cached_tokens":2}}}'


def _serving_metrics_raw(completed_requests: int) -> bytes:
    count = 10 + completed_requests
    sums = (
        1.0 + (0.125 * completed_requests),
        2.0 + (0.25 * completed_requests),
        3.0 + (0.5 * completed_requests),
        4.0 + (0.75 * completed_requests),
    )
    lines: list[str] = []
    for family, total in zip(_SERVING_FAMILIES, sums):
        lines.extend(
            (
                f"# TYPE {family} histogram",
                f'{family}_count{{engine="0"}} {count}',
                f'{family}_sum{{engine="0"}} {total}',
            )
        )
    return ("\n".join(lines) + "\n").encode("ascii")


class ServingMetricsFixture:
    def __init__(self, root: Path, *, witness_mode: str = "valid"):
        self.witness_path = root / "access-witness.jsonl"
        self.witness_mode = witness_mode
        self.completed_requests = 0
        self.physical_request_ids: list[str | None] = []
        self.engine_request_headers: list[list[str]] = []
        self.engine_case_headers: list[list[str]] = []
        self.engine_attempt_headers: list[list[str]] = []
        self.engine_run_headers: list[list[str]] = []
        self.request_bodies: list[bytes] = []
        self.scrape_calls = 0
        self._lock = threading.Lock()

    def scrape(self) -> bytes:
        with self._lock:
            completed = self.completed_requests
            self.scrape_calls += 1
        return _serving_metrics_raw(completed)

    def complete_request(self, physical_request_id: str | None, body: bytes) -> None:
        with self._lock:
            self.physical_request_ids.append(physical_request_id)
            self.request_bodies.append(body)
            if self.witness_mode != "missing" and physical_request_id:
                witness = {
                    "schema_version": SERVING_METRICS_WITNESS_SCHEMA,
                    "evidence_kind": SERVING_METRICS_WITNESS_EVIDENCE_KIND,
                    "request_id": physical_request_id,
                    "server_identity": "server-a",
                    "lease_id": "lease-1",
                    "counter_epoch": "epoch-1",
                    "observed_request_ids": [physical_request_id],
                    "other_request_ids": [],
                    "dedicated_server": True,
                    "no_other_requests": True,
                }
                lines = [witness]
                if self.witness_mode == "ambiguous":
                    lines.append(witness)
                with self.witness_path.open("ab") as handle:
                    for item in lines:
                        handle.write(
                            (json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n").encode(
                                "utf-8"
                            )
                        )
                    handle.flush()
                    os.fsync(handle.fileno())
            self.completed_requests += 1


class ServingMetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/metrics":
            self.send_error(404)
            return
        payload = self.server.serving_fixture.scrape()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format, *_args):
        return


class ServingUpstreamHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.server.serving_fixture.engine_request_headers.append(self.headers.get_all("X-Request-Id", []))
        self.server.serving_fixture.engine_case_headers.append(self.headers.get_all("X-EIC-Case-ID", []))
        self.server.serving_fixture.engine_attempt_headers.append(self.headers.get_all("X-EIC-Attempt-ID", []))
        self.server.serving_fixture.engine_run_headers.append(self.headers.get_all("X-EIC-Run-ID", []))
        self.server.serving_fixture.complete_request(
            self.headers.get("X-EIC-Physical-Request-ID"), body
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(_SERVING_RESPONSE)))
        self.end_headers()
        self.wfile.write(_SERVING_RESPONSE)

    def log_message(self, _format, *_args):
        return


def _serving_config(root: Path, metrics_port: int) -> dict[str, object]:
    return {
        "schema_version": SERVING_METRICS_CONFIG_SCHEMA,
        "enabled": True,
        "metrics_url": f"http://127.0.0.1:{metrics_port}/metrics",
        "server_identity": "server-a",
        "counter_epoch": "epoch-1",
        "timeout_seconds": 1.0,
        "access_witness_path": str(root / "access-witness.jsonl"),
        "access_witness_evidence_kind": SERVING_METRICS_WITNESS_EVIDENCE_KIND,
        "vllm_version": "0.10.0",
    }


def _start_serving_fixture(
    root: Path,
    *,
    witness_mode: str = "valid",
    with_v2: bool = True,
    mode: str | None = None,
    run_id: str = "serving-run",
    attempt_id: str = "attempt-001",
    case_id: str | None = None,
):
    serving_fixture = ServingMetricsFixture(root, witness_mode=witness_mode)
    serving_fixture.witness_path.touch()
    metrics = ThreadingHTTPServer(("127.0.0.1", 0), ServingMetricsHandler)
    metrics.serving_fixture = serving_fixture
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), ServingUpstreamHandler)
    upstream.serving_fixture = serving_fixture
    output_dir = root / "telemetry_v2" if with_v2 else None
    config = _serving_config(root, metrics.server_address[1])
    if mode is not None:
        config["mode"] = mode
    proxy = ProxyServer(
        ("127.0.0.1", 0),
        upstream_host="127.0.0.1",
        upstream_port=upstream.server_address[1],
        writer=JsonlWriter(root / "request_events.jsonl"),
        timeout_seconds=2,
        max_body_bytes=4096,
        v2_output_dir=output_dir,
        v2_run_id=run_id,
        v2_attempt_id=attempt_id,
        v2_case_id=case_id,
        serving_metrics_config=config,
    )
    servers = (metrics, upstream, proxy)
    threads = tuple(threading.Thread(target=server.serve_forever, daemon=True) for server in servers)
    for thread in threads:
        thread.start()
    return serving_fixture, metrics, upstream, proxy, threads


def _stop_serving_fixture(metrics, upstream, proxy, threads) -> None:
    for server in (proxy, upstream, metrics):
        server.shutdown()
    for server in (proxy, upstream, metrics):
        server.server_close()
    for thread in threads:
        thread.join(timeout=2)


class RequestProxyTests(unittest.TestCase):
    def test_token_counts_preserve_explicit_cache_details_and_missing_is_unavailable(self):
        self.assertEqual(
            _token_counts(
                b'{"usage":{"prompt_tokens":7,"completion_tokens":3,"total_tokens":10,'
                b'"prompt_tokens_details":{"cached_tokens":2}}}'
            ),
            {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10, "cached_tokens": 2},
        )
        self.assertIsNone(
            _token_counts(b'{"usage":{"prompt_tokens":7,"prompt_tokens_details":null}}')["cached_tokens"]
        )
        sse = (
            b'data: {"choices":[],"usage":null}\n\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3,'
            b'"total_tokens":10,"prompt_tokens_details":{"cached_tokens":4}}}\n\n'
            b'data: [DONE]\n\n'
        )
        self.assertEqual(_token_counts(sse)["cached_tokens"], 4)

    def test_writer_creates_events_file_before_any_request(self):
        with TemporaryDirectory() as temporary:
            events = Path(temporary) / "request_events.jsonl"
            self.assertFalse(events.exists())
            JsonlWriter(events)
            self.assertTrue(events.is_file())
            self.assertFalse(events.is_symlink())
            self.assertEqual(events.read_bytes(), b"")

    def test_forwards_without_recording_payloads(self):
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        with TemporaryDirectory() as temporary:
            events = Path(temporary) / "request_events.jsonl"
            proxy = ProxyServer(
                ("127.0.0.1", 0),
                upstream_host="127.0.0.1",
                upstream_port=upstream.server_address[1],
                writer=JsonlWriter(events),
                timeout_seconds=2,
                max_body_bytes=1024,
            )
            upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
            upstream_thread.start()
            proxy_thread.start()
            try:
                body = b'{"messages":[{"role":"user","content":"private prompt"}],"max_tokens":128,"temperature":0.2}'
                connection = http.client.HTTPConnection("127.0.0.1", proxy.server_address[1], timeout=2)
                connection.request("POST", "/v1/chat/completions", body=body, headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                response_body = response.read()
                connection.close()
            finally:
                proxy.shutdown()
                upstream.shutdown()
                proxy.server_close()
                upstream.server_close()
                proxy_thread.join(timeout=2)
                upstream_thread.join(timeout=2)

            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response_body)["usage"]["total_tokens"], 10)
            self.assertEqual(upstream.received_body, body)
            self.assertTrue(upstream.received_request_id.startswith("request-"))
            record = json.loads(events.read_text(encoding="utf-8"))
            self.assertEqual(record["event_type"], "model_request_boundary")
            self.assertEqual(record["status_code"], 200)
            self.assertEqual(record["prompt_tokens"], 7)
            self.assertEqual(record["completion_tokens"], 3)
            self.assertEqual(record["max_output_tokens"], 128)
            self.assertEqual(record["temperature"], 0.2)
            self.assertEqual(record["request_mutation"], False)
            self.assertNotIn("private prompt", record)
            self.assertNotIn("messages", record)

    def test_manifest_serving_config_loader_reads_the_exact_descriptor(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = _serving_config(root, 8123)
            path = root / "serving_metrics.json"
            path.write_text(json.dumps(config), encoding="utf-8")

            self.assertEqual(load_serving_metrics_config(path), config)
            self.assertEqual(load_serving_metrics_config(json.dumps(config)), config)

    def test_manifest_serving_capture_archives_raw_pair_and_keeps_v2_lifecycle(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture, metrics, upstream, proxy, threads = _start_serving_fixture(
                root, case_id="case-authoritative", attempt_id="attempt-authoritative"
            )
            body = b'{"messages":[{"role":"user","content":"private prompt"}],"max_tokens":8}'
            try:
                status, response_body = _post(
                    proxy,
                    body,
                    headers={
                        "X-EIC-Logical-Request-ID": "logical-serving-1",
                        "X-EIC-Client-Span-ID": "client-serving-span",
                        "x-request-id": "untrusted-reused-id",
                        "x-eic-physical-request-id": "untrusted-physical-id",
                        "X-EIC-CASE-ID": "spoofed-case",
                        "x-eic-case-id": "spoofed-case-duplicate",
                        "X-EIC-ATTEMPT-ID": "spoofed-attempt",
                        "x-eic-run-id": "spoofed-run",
                    },
                )
            finally:
                _stop_serving_fixture(metrics, upstream, proxy, threads)

            self.assertEqual(status, 200)
            self.assertEqual(response_body, _SERVING_RESPONSE)
            self.assertEqual(fixture.request_bodies, [body])
            self.assertEqual(len(fixture.physical_request_ids), 1)
            physical_request_id = fixture.physical_request_ids[0]
            self.assertIsInstance(physical_request_id, str)
            self.assertNotEqual(physical_request_id, "untrusted-physical-id")
            self.assertEqual(fixture.engine_request_headers, [[physical_request_id]])
            self.assertEqual(fixture.engine_case_headers, [["case-authoritative"]])
            self.assertEqual(fixture.engine_attempt_headers, [["attempt-authoritative"]])
            self.assertEqual(fixture.engine_run_headers, [[]])

            event = _wait_for_event(root / "request_events.jsonl")
            self.assertEqual(event["serving_metrics_status"], "measured")
            self.assertTrue(event["serving_metrics_physical_request_dispatched"])
            reference = event["serving_metrics_record"]
            self.assertEqual(reference["status"], "measured")
            record_path = root / "telemetry_v2" / reference["path"]
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(record["status"], "measured")
            self.assertEqual(record["request_id"], physical_request_id)
            self.assertEqual(record["server_lease_id"], "lease-1")
            self.assertEqual(record["capture"]["scrape_count"], 2)
            self.assertEqual(record["witness"]["observed_request_ids"], [physical_request_id])
            self.assertEqual(record["metrics"]["queue"]["value_ms"], 125.0)
            self.assertEqual(record["metrics"]["prefill"]["value_ms"], 250.0)
            self.assertEqual(record["metrics"]["decode"]["value_ms"], 500.0)
            self.assertEqual(record["metrics"]["e2e"]["value_ms"], 750.0)

            expected_raw = {
                "before": _serving_metrics_raw(0),
                "after": _serving_metrics_raw(1),
            }
            for phase, raw in expected_raw.items():
                snapshot = record["snapshots"][phase]
                snapshot_path = root / "telemetry_v2" / snapshot["raw_path"]
                self.assertEqual(snapshot_path.read_bytes(), raw)
                self.assertEqual(snapshot["raw_sha256"], hashlib.sha256(raw).hexdigest())
                self.assertEqual(snapshot["raw_sha256"], record[f"{phase}_raw_sha256"])
            witness_artifact = record["witness_artifact"]
            self.assertEqual(
                (root / "telemetry_v2" / witness_artifact["path"]).read_bytes().count(b"\n"),
                1,
            )
            self.assertEqual(record["capture"]["config_artifact"]["path"], "serving_metrics/collector_config.json")

            terminal_rows = [
                json.loads(line)
                for line in (root / "telemetry_v2" / "model_events.jsonl").read_text().splitlines()
                if json.loads(line)["terminal"]
            ]
            self.assertEqual(len(terminal_rows), 1)
            terminal = terminal_rows[0]
            self.assertEqual(terminal["physical_request_id"], physical_request_id)
            self.assertEqual(terminal["client_span_id"], "client-serving-span")
            self.assertEqual(terminal["serving_metrics_status"], "measured")
            self.assertEqual(terminal["queue_ms"], 125.0)
            self.assertEqual(terminal["prefill_ms"], 250.0)
            self.assertEqual(terminal["decode_ms"], 500.0)
            self.assertTrue(terminal["serving_timings_reliable"])
            payload_artifact = terminal["request_payload_artifact"]
            self.assertTrue(terminal["request_payload_pre_dispatch"])
            self.assertEqual(
                (root / "telemetry_v2" / payload_artifact["request"]["artifact_path"]).read_bytes(),
                body,
            )
            self.assertEqual(
                (root / "telemetry_v2" / payload_artifact["response"]["artifact_path"]).read_bytes(),
                _SERVING_RESPONSE,
            )
            self.assertEqual(
                payload_artifact["request"]["sha256"], hashlib.sha256(body).hexdigest()
            )
            self.assertEqual(
                payload_artifact["response"]["sha256"],
                hashlib.sha256(_SERVING_RESPONSE).hexdigest(),
            )
            self.assertNotIn("private prompt", (root / "telemetry_v2" / "model_events.jsonl").read_text())

    def test_serving_capture_preserves_retry_lineage_and_each_raw_payload(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture, metrics, upstream, proxy, threads = _start_serving_fixture(root)
            bodies = [b'{"max_tokens":8}', b'{"max_tokens":9}']
            statuses = []
            try:
                for body in bodies:
                    status, response_body = _post(
                        proxy,
                        body,
                        headers={"X-EIC-Logical-Request-ID": "logical-retry-1"},
                    )
                    statuses.append((status, response_body))
            finally:
                _stop_serving_fixture(metrics, upstream, proxy, threads)

            self.assertEqual(statuses, [(200, _SERVING_RESPONSE), (200, _SERVING_RESPONSE)])
            self.assertEqual(len(fixture.physical_request_ids), 2)
            self.assertNotEqual(fixture.physical_request_ids[0], fixture.physical_request_ids[1])
            self.assertEqual(fixture.engine_request_headers, [[value] for value in fixture.physical_request_ids])
            self.assertEqual(fixture.request_bodies, bodies)
            terminal_rows = [
                json.loads(line)
                for line in (root / "telemetry_v2" / "model_events.jsonl").read_text().splitlines()
                if json.loads(line)["terminal"]
            ]
            self.assertEqual([row["retry_index"] for row in terminal_rows], [0, 1])
            self.assertIsNone(terminal_rows[0]["retry_of"])
            self.assertEqual(terminal_rows[1]["retry_of"], terminal_rows[0]["physical_request_id"])
            self.assertEqual(
                [row["serving_metrics_status"] for row in terminal_rows], ["measured", "measured"]
            )
            for row, body in zip(terminal_rows, bodies):
                artifact = row["request_payload_artifact"]
                self.assertEqual(
                    (root / "telemetry_v2" / artifact["request"]["artifact_path"]).read_bytes(),
                    body,
                )
                self.assertEqual(
                    (root / "telemetry_v2" / artifact["response"]["artifact_path"]).read_bytes(),
                    _SERVING_RESPONSE,
                )
                serving_record = root / "telemetry_v2" / row["serving_metrics_record"]["path"]
                self.assertEqual(json.loads(serving_record.read_text())["status"], "measured")

    def test_independent_proxy_instances_disjoint_physical_ids_and_bind_native_headers(self):
        """A fresh proxy must not collide with another proxy's sequence zero.

        Each fixture has an independent v2 sequence counter, while the run,
        attempt, case, and request body are deliberately identical.  The
        per-dispatch transport request ID is therefore required as a physical
        identity input; logical identity remains shared and retry lineage is
        still represented independently in each proxy's journal.
        """
        body = b'{"messages":[{"role":"user","content":"same body"}],"max_tokens":8}'
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            roots = (root / "proxy-a", root / "proxy-b")
            fixtures = []
            resources = []
            for fixture_root in roots:
                fixture_root.mkdir()
                fixture, metrics, upstream, proxy, threads = _start_serving_fixture(
                    fixture_root,
                    run_id="same-run",
                    attempt_id="same-attempt",
                    case_id="same-case",
                )
                fixtures.append(fixture)
                resources.append((metrics, upstream, proxy, threads))
            try:
                results = [_post(proxy, body) for _fixture, (_metrics, _upstream, proxy, _threads) in zip(fixtures, resources)]
            finally:
                for metrics, upstream, proxy, threads in resources:
                    _stop_serving_fixture(metrics, upstream, proxy, threads)

            self.assertEqual(results, [(200, _SERVING_RESPONSE), (200, _SERVING_RESPONSE)])
            physical_ids = [fixture.physical_request_ids[0] for fixture in fixtures]
            self.assertTrue(all(isinstance(value, str) and value for value in physical_ids))
            self.assertEqual(len(set(physical_ids)), 2)
            for fixture, physical_id in zip(fixtures, physical_ids):
                self.assertEqual(fixture.request_bodies, [body])
                self.assertEqual(fixture.engine_request_headers, [[physical_id]])
                self.assertEqual(fixture.engine_case_headers, [["same-case"]])
                self.assertEqual(fixture.engine_attempt_headers, [["same-attempt"]])

            terminal_rows = []
            for fixture_root, physical_id in zip(roots, physical_ids):
                rows = [
                    json.loads(line)
                    for line in (fixture_root / "telemetry_v2" / "model_events.jsonl").read_text().splitlines()
                    if json.loads(line).get("terminal") is True
                ]
                self.assertEqual(len(rows), 1)
                terminal = rows[0]
                self.assertEqual(terminal["physical_request_id"], physical_id)
                self.assertEqual(terminal["retry_index"], 0)
                self.assertIsNone(terminal["retry_of"])
                terminal_rows.append(terminal)
            self.assertEqual(
                terminal_rows[0]["logical_request_id"], terminal_rows[1]["logical_request_id"]
            )

    def test_absent_or_ambiguous_witness_makes_serving_metrics_unavailable(self):
        for witness_mode, expected_reason in (
            ("missing", "no external access witness"),
            ("ambiguous", "multiple external access witnesses"),
        ):
            with self.subTest(witness_mode=witness_mode), TemporaryDirectory() as temporary:
                root = Path(temporary)
                _fixture, metrics, upstream, proxy, threads = _start_serving_fixture(
                    root, witness_mode=witness_mode
                )
                try:
                    status, response_body = _post(proxy, b'{"max_tokens":8}')
                finally:
                    _stop_serving_fixture(metrics, upstream, proxy, threads)

                self.assertEqual(status, 200)
                self.assertEqual(response_body, _SERVING_RESPONSE)
                event = _wait_for_event(root / "request_events.jsonl")
                self.assertEqual(event["serving_metrics_status"], "unavailable")
                self.assertIn(expected_reason, event["serving_metrics_unavailable_reason"])
                reference = event["serving_metrics_record"]
                record = json.loads((root / "telemetry_v2" / reference["path"]).read_text())
                self.assertEqual(record["status"], "unavailable")
                self.assertIn(expected_reason, record["unavailable_reason"])
                self.assertTrue(
                    all(metric["value_ms"] is None for metric in record["metrics"].values())
                )
                self.assertTrue(
                    all(
                        (root / "telemetry_v2" / record["snapshots"][phase]["raw_path"]).is_file()
                        for phase in ("before", "after")
                    )
                )

    def test_native_deferred_mode_performs_no_scrape_or_witness_read_and_binds_request_id(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture, metrics, upstream, proxy, threads = _start_serving_fixture(
                root,
                witness_mode="missing",
                mode="native_deferred",
                run_id="native-run",
                attempt_id="attempt-native-40",
                case_id="case-native-40",
            )
            try:
                status, response_body = _post(proxy, b'{"max_tokens":8}')
            finally:
                _stop_serving_fixture(metrics, upstream, proxy, threads)

            self.assertEqual(status, 200)
            self.assertEqual(response_body, _SERVING_RESPONSE)
            # Zero remote metrics round trips inside the measured request.
            self.assertEqual(fixture.scrape_calls, 0)
            self.assertIsNone(proxy.serving_metrics_capture.collector)
            # The physical ID still reaches the engine as X-Request-Id.
            physical_request_id = fixture.physical_request_ids[0]
            self.assertIsNotNone(physical_request_id)
            self.assertEqual(fixture.engine_request_headers[0], [physical_request_id])
            self.assertEqual(fixture.engine_case_headers[0], ["case-native-40"])
            self.assertEqual(fixture.engine_attempt_headers[0], ["attempt-native-40"])
            self.assertEqual(fixture.engine_run_headers[0], [])
            event = _wait_for_event(root / "request_events.jsonl")
            self.assertEqual(event["serving_metrics_status"], "unavailable")
            self.assertIn("native_deferred", event["serving_metrics_unavailable_reason"])
            self.assertTrue(event["serving_metrics_physical_request_dispatched"])
            reference = event["serving_metrics_record"]
            record = json.loads((root / "telemetry_v2" / reference["path"]).read_text())
            self.assertEqual(record["status"], "unavailable")
            self.assertEqual(record["attribution_mode"], "native_deferred")
            self.assertEqual(record["capture"]["scrape_count"], 0)
            self.assertIsNone(record["witness"])
            self.assertIsNone(record["witness_source"])
            self.assertEqual(record["request_id"], physical_request_id)
            self.assertTrue(all(metric["value_ms"] is None for metric in record["metrics"].values()))
            for phase in ("before", "after"):
                self.assertEqual(record["snapshots"][phase]["status"], "unavailable")
                self.assertIsNone(record["snapshots"][phase]["raw_path"])
            serving_artifacts = list((root / "telemetry_v2" / "serving_metrics").glob("*.prom"))
            self.assertEqual(serving_artifacts, [])
            terminal = next(
                row for row in (
                    json.loads(line)
                    for line in (root / "telemetry_v2" / "model_events.jsonl").read_text().splitlines()
                )
                if row.get("terminal") is True and row.get("event_kind") == "model_request"
            )
            self.assertEqual(terminal["cached_tokens"], 2)

    def test_serving_config_mode_is_optional_and_validated(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = _serving_config(root, 1)
            plain = load_serving_metrics_config(json.dumps(base))
            self.assertNotIn("mode", plain)
            deferred = load_serving_metrics_config(json.dumps({**base, "mode": "native_deferred"}))
            self.assertEqual(deferred["mode"], "native_deferred")
            with self.assertRaises(ValueError):
                load_serving_metrics_config(json.dumps({**base, "mode": "guess"}))

    def test_enabled_serving_capture_without_v2_dir_uses_events_parent_for_artifacts(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture, metrics, upstream, proxy, threads = _start_serving_fixture(root, with_v2=False)
            try:
                status, _response_body = _post(proxy, b'{"max_tokens":8}')
            finally:
                _stop_serving_fixture(metrics, upstream, proxy, threads)

            self.assertEqual(status, 200)
            event = _wait_for_event(root / "request_events.jsonl")
            self.assertEqual(event["serving_metrics_status"], "measured")
            reference = event["serving_metrics_record"]
            self.assertTrue(reference["path"].startswith("serving_metrics/"))
            self.assertTrue((root / reference["path"]).is_file())
            self.assertFalse((root / "telemetry_v2").exists())

    def test_adaptive_prediction_is_durable_before_dispatch_and_reveals_after_response(self):
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), AdaptiveUpstreamHandler)
        upstream.events = []
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = AdaptiveRuntimeFake(root)
            upstream.adaptive_marker = runtime.marker
            events = root / "request_events.jsonl"
            proxy, proxy_thread = _start_proxy(upstream.server_address[1], events, runtime)
            upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            upstream_thread.start()
            try:
                status, response_body = _post(
                    proxy,
                    b'{"messages":[{"role":"user","content":"secret prompt text"}],"max_tokens":32}',
                )
            finally:
                proxy.shutdown()
                upstream.shutdown()
                proxy.server_close()
                upstream.server_close()
                proxy_thread.join(timeout=2)
                upstream_thread.join(timeout=2)

            self.assertEqual(status, 200)
            self.assertEqual(json.loads(response_body)["usage"]["total_tokens"], 7)
            self.assertEqual(upstream.events, [("upstream_seen", True)])
            self.assertEqual([record["kind"] for record in runtime.records], ["prediction", "label"])
            self.assertTrue(runtime.revealed)
            self.assertEqual(runtime.records[1]["status"], "completed")
            self.assertGreaterEqual(runtime.records[1]["observed_ms"], 20)
            proxy_record = json.loads(events.read_text(encoding="utf-8"))
            self.assertTrue(proxy_record["prediction_durable_before_upstream"])
            self.assertIsNotNone(proxy_record["adaptive_prediction_record_sha256"])
            self.assertIsNotNone(proxy_record["adaptive_label_record_sha256"])
            serialized = events.read_text(encoding="utf-8") + json.dumps(runtime.records, sort_keys=True)
            self.assertNotIn("secret prompt text", serialized)
            self.assertNotIn("messages", serialized)

    def test_adaptive_prediction_failure_prevents_upstream_dispatch(self):
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), CountingUpstreamHandler)
        upstream.dispatch_count = 0
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = AdaptiveRuntimeFake(root, fail_prediction=True)
            events = root / "request_events.jsonl"
            proxy, proxy_thread = _start_proxy(upstream.server_address[1], events, runtime)
            upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            upstream_thread.start()
            try:
                status, _response_body = _post(
                    proxy,
                    b'{"messages":[{"role":"user","content":"must not dispatch"}],"max_tokens":16}',
                )
            finally:
                proxy.shutdown()
                upstream.shutdown()
                proxy.server_close()
                upstream.server_close()
                proxy_thread.join(timeout=2)
                upstream_thread.join(timeout=2)

            self.assertEqual(status, 500)
            self.assertEqual(upstream.dispatch_count, 0)
            self.assertFalse(runtime.predicted)
            record = json.loads(events.read_text(encoding="utf-8"))
            self.assertEqual(record["error"], "RuntimeError")
            self.assertIsNone(record["adaptive_prediction_record_sha256"])

    def test_upstream_connection_failure_reveals_prediction_as_unavailable(self):
        probe = ThreadingHTTPServer(("127.0.0.1", 0), CountingUpstreamHandler)
        unused_port = probe.server_address[1]
        probe.server_close()
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = AdaptiveRuntimeFake(root)
            events = root / "request_events.jsonl"
            proxy, proxy_thread = _start_proxy(unused_port, events, runtime)
            try:
                status, _response_body = _post(
                    proxy,
                    b'{"messages":[{"role":"user","content":"connection failure prompt"}],"max_tokens":16}',
                )
            finally:
                proxy.shutdown()
                proxy.server_close()
                proxy_thread.join(timeout=2)

            self.assertEqual(status, 502)
            self.assertTrue(runtime.predicted)
            self.assertTrue(runtime.revealed)
            self.assertEqual(runtime.records[1]["status"], "unavailable")
            self.assertTrue(runtime.records[1]["unavailable_reason"].startswith("upstream_"))
            serialized = events.read_text(encoding="utf-8") + json.dumps(runtime.records, sort_keys=True)
            self.assertNotIn("connection failure prompt", serialized)

    def test_upstream_disconnect_before_headers_appends_once_without_retry_or_tokens(self):
        upstream = RawUpstreamServer("before-headers")
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            events = root / "request_events.jsonl"
            writer = CountingWriter(events)
            proxy, proxy_thread = _start_proxy(
                upstream.server_address[1], events, writer=writer, timeout=0.5
            )
            upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            upstream_thread.start()
            try:
                status, _ = _post(proxy, b'{"messages":[],"max_tokens":16}')
                record = _wait_for_event(events)
            finally:
                proxy.shutdown()
                upstream.shutdown()
                proxy.server_close()
                upstream.server_close()
                proxy_thread.join(timeout=2)
                upstream_thread.join(timeout=2)

            self.assertEqual(status, 502)
            self.assertEqual(upstream.dispatch_count, 1)
            self.assertEqual(writer.append_count, 1)
            self.assertIsNone(record["status_code"])
            self.assertEqual(record["failure_phase"], "response_headers")
            self.assertGreaterEqual(len(record["error"]), 1)
            self.assertEqual(record["response_bytes"], 0)
            self.assertIsNone(record["prompt_tokens"])
            self.assertIsNone(record["completion_tokens"])
            self.assertIsNone(record["total_tokens"])
            self.assertEqual(
                record["duration_ms"],
                (record["end_mono_ns"] - record["start_mono_ns"]) / 1_000_000,
            )

    def test_absolute_proxy_deadline_caps_upstream_timeout_without_inventing_a_timestamp(self):
        upstream = RawUpstreamServer("before-headers")
        with TemporaryDirectory() as temporary:
            events = Path(temporary) / "request_events.jsonl"
            deadline = time.monotonic_ns() + 750_000_000
            proxy = ProxyServer(
                ("127.0.0.1", 0),
                upstream_host="127.0.0.1",
                upstream_port=upstream.server_address[1],
                writer=JsonlWriter(events),
                timeout_seconds=30,
                max_body_bytes=4096,
                deadline_monotonic_ns=deadline,
            )
            try:
                bounded = proxy.upstream_timeout_seconds()
                self.assertGreater(bounded, 0.0)
                self.assertLessEqual(bounded, 0.76)
                self.assertEqual(proxy.case_deadline_monotonic_ns, deadline)
            finally:
                proxy.server_close()
                upstream.server_close()

    def test_incomplete_response_retains_partial_bytes_and_never_labels_them_complete(self):
        upstream = RawUpstreamServer("partial")
        payload = json.dumps(
            {"usage": {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13}}
        ).encode("ascii")
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            events = root / "request_events.jsonl"
            writer = CountingWriter(events)
            proxy, proxy_thread = _start_proxy(
                upstream.server_address[1], events, writer=writer, timeout=0.5
            )
            upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            upstream_thread.start()
            try:
                status, _ = _post(proxy, b'{"messages":[],"max_tokens":16}')
                record = _wait_for_event(events)
            finally:
                proxy.shutdown()
                upstream.shutdown()
                proxy.server_close()
                upstream.server_close()
                proxy_thread.join(timeout=2)
                upstream_thread.join(timeout=2)

            self.assertEqual(status, 502)
            self.assertEqual(upstream.dispatch_count, 1)
            self.assertEqual(writer.append_count, 1)
            self.assertEqual(record["status_code"], 200)
            self.assertEqual(record["failure_phase"], "response_body")
            self.assertEqual(record["response_bytes"], len(payload))
            self.assertEqual(record["response_sha256"], hashlib.sha256(payload).hexdigest())
            self.assertIsNone(record["prompt_tokens"])
            self.assertIsNone(record["completion_tokens"])
            self.assertIsNone(record["total_tokens"])

    def test_response_timeout_after_body_bytes_retains_bytes_and_does_not_invent_usage(self):
        upstream = RawUpstreamServer("partial-timeout")
        payload = json.dumps(
            {"usage": {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13}}
        ).encode("ascii")
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            events = root / "request_events.jsonl"
            writer = CountingWriter(events)
            proxy, proxy_thread = _start_proxy(
                upstream.server_address[1], events, writer=writer, timeout=0.08
            )
            upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            upstream_thread.start()
            try:
                status, _ = _post(proxy, b'{"messages":[],"max_tokens":16}')
                record = _wait_for_event(events)
            finally:
                proxy.shutdown()
                upstream.shutdown()
                proxy.server_close()
                upstream.server_close()
                proxy_thread.join(timeout=2)
                upstream_thread.join(timeout=2)

            self.assertEqual(status, 502)
            self.assertEqual(upstream.dispatch_count, 1)
            self.assertEqual(writer.append_count, 1)
            self.assertEqual(record["status_code"], 200)
            self.assertEqual(record["failure_phase"], "response_body")
            self.assertEqual(record["response_bytes"], len(payload))
            self.assertIsNone(record["prompt_tokens"])
            self.assertIsNone(record["completion_tokens"])
            self.assertIsNone(record["total_tokens"])

    def test_downstream_write_disconnect_still_appends_exactly_once(self):
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), DelayedSuccessUpstream)
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            events = root / "request_events.jsonl"
            writer = CountingWriter(events)
            proxy, proxy_thread = _start_proxy(
                upstream.server_address[1], events, writer=writer, timeout=1
            )
            upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            upstream_thread.start()
            client = socket.create_connection(("127.0.0.1", proxy.server_address[1]), timeout=1)
            try:
                client.sendall(
                    b"POST /v1/chat/completions HTTP/1.1\r\n"
                    b"Host: localhost\r\nContent-Length: 2\r\n\r\n{}"
                )
                # RST makes both header and body writes fail deterministically
                # once the delayed upstream response arrives.
                client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            finally:
                client.close()
            try:
                record = _wait_for_event(events)
            finally:
                proxy.shutdown()
                upstream.shutdown()
                proxy.server_close()
                upstream.server_close()
                proxy_thread.join(timeout=2)
                upstream_thread.join(timeout=2)

            self.assertEqual(writer.append_count, 1)
            self.assertEqual(record["failure_phase"], "downstream_write")
            self.assertIsNotNone(record["error"])
            self.assertEqual(record["status_code"], 200)

    def test_send_error_after_downstream_disconnect_still_appends_exactly_once(self):
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), DelayedCloseUpstream)
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            events = root / "request_events.jsonl"
            writer = CountingWriter(events)
            proxy, proxy_thread = _start_proxy(
                upstream.server_address[1], events, writer=writer, timeout=1
            )
            upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            upstream_thread.start()
            client = socket.create_connection(("127.0.0.1", proxy.server_address[1]), timeout=1)
            try:
                client.sendall(
                    b"POST /v1/chat/completions HTTP/1.1\r\n"
                    b"Host: localhost\r\nContent-Length: 2\r\n\r\n{}"
                )
                client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            finally:
                client.close()
            try:
                record = _wait_for_event(events)
            finally:
                proxy.shutdown()
                upstream.shutdown()
                proxy.server_close()
                upstream.server_close()
                proxy_thread.join(timeout=2)
                upstream_thread.join(timeout=2)

            self.assertEqual(writer.append_count, 1)
            self.assertEqual(record["failure_phase"], "response_headers")
            self.assertIsNone(record["status_code"])
            self.assertIsNotNone(record["error"])
            self.assertIsNone(record["prompt_tokens"])
            self.assertIsNone(record["completion_tokens"])
            self.assertIsNone(record["total_tokens"])


if __name__ == "__main__":
    unittest.main()
